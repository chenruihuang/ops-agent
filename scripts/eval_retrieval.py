"""召回率评估：context recall@k 的真实产出工具。

用法
----
    python -m scripts.eval_retrieval --backend bm25                  # 关键词召回基线（离线可跑）
    python -m scripts.eval_retrieval --backend local                 # 向量召回（需 embedding 模型）
    python -m scripts.eval_retrieval --backend local --rerank        # 向量召回 + CrossEncoder 重排
    python -m scripts.eval_retrieval --backend qdrant --rerank       # 走真实向量库
    python -m scripts.eval_retrieval --backend chroma --rerank       # 换后端不改代码，只改这个参数
    python -m scripts.eval_retrieval --backend qdrant --fusion rrf --rerank   # 多路融合 + 重排

为什么这个脚本值得单独存在
-------------------------
"context recall@5 从 x 提升到 y"这句话的分量，全在这句能不能被复现。
所以脚本必须满足三条：
    1. **可比**：同一批题、同一口径、同一 k。`--rerank` 只切换一个开关，
       产出的是"同条件下的前后对比"，而不是两次条件不同的数字。
    2. **可追溯**：每次运行落一份 JSON，含口径、后端、参数、逐题明细。
       指标漂了要能查到是哪次改动导致的。
    3. **可解释**：逐题明细给出"哪几篇 golden 没召回到"。
       只报总分会让人无法判断是召回问题还是标注问题。

本脚本不再自己实现检索
----------------------
这是本文件最重要的一条设计约束，值得单独说明。

之前它自己写了 `rrf_fuse`、自己拼 BM25 排名、自己扩池重排。结果是
"评估"与"线上"成了两条代码路径，而两者的差异**至少**有四处：

    | 环节 | 旧评估脚本 | 线上（app/pipeline/rag.py） |
    |------|-----------|---------------------------|
    | kb_version 过滤 | 无 | 有（store 侧 + Chunk 侧双保险） |
    | 每路文档占比上限 | 无 | 有（融合前） |
    | 融合粒度 | 文档级 | 块级 |
    | 重排候选池 | 命中文档的**全部**块 | 只重排已召回的块 |

这四处每一处都会改变分数，而且**方向不一致**：没有 kb_version 过滤会
略微虚高（旧版本点也算命中），扩池重排也会虚高（候选更多），
而没有占比上限又会压低。四个偏差叠在一起，最后得到的数字
既不是线上的，也不是干净的消融 —— 它只是"某个别的系统"的分数。

所以现在：**检索全部走 `app.pipeline.rag.recall_detailed`**，
本脚本只做三件它该做的事 —— 参数化、投影到文档级、算指标。
消融变成了 `RecallConfig` 的显式字段（哪些通道 / 是否融合 / 池子多大），
差异是**参数**而不是**代码分叉**。

两个重排口径都报
----------------
- `pool`：把重排分当排序信号，池子里全部候选按分重排后取 top-5 文档。
  这是与"未重排基线"唯一变量可比的口径 —— 它回答"重排的**排序**有没有变好"。
- `served`：线上真实口径（每篇最多 N 块、只带 k 块进上下文）后再投影到文档级。
  它回答"用户实际拿到的东西里有没有目标文档"。

只报 `pool` 会让重排显得比实际更好（因为线上还受"块数上限"限制）；
只报 `served` 又无法与未重排基线对比（块数上限是另一个变量）。
两个都给，且都注明，是诚实而不是美化。

如果看到 `expanded` 口径已经饱和在 1.0
--------------------------------------
那不是"检索完美"，而是**图谱一跳扩展太强**：几乎所有 golden 都能从
top-5 的邻居里够到。所以判断改动有没有用，要看 strict 口径与 MRR，
而不是看 expanded —— 一个恒为 1.0 的指标没有分辨力。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ingest.evaluate import evaluate, format_table, load_questions    # noqa: E402
from app.ingest.indexer import neighbor_map                              # noqa: E402
from app.pipeline.fusion import RRF_K, cap_per_unit, dedupe_docs         # noqa: E402
from app.pipeline.rag import (                                           # noqa: E402
    CH_BM25,
    CH_VECTOR,
    Chunk,
    RecallConfig,
    get_bundle,
    recall_detailed,
    rerank_chunks,
)
from app.core.config import settings                                     # noqa: E402

log = logging.getLogger("eval")

# 兼容旧引用：这几个名字以前定义在本文件里，现在只是转发。
from app.pipeline.fusion import rrf_fuse                                 # noqa: E402

__all__ = ["rrf_fuse", "dedupe_docs", "RRF_K"]


# =====================================================================
# 配置：CLI → RecallConfig
# =====================================================================
def build_config(args: argparse.Namespace) -> RecallConfig:
    """把命令行的"后端 + 开关"翻译成一条召回链路的参数。

    一次只改一个变量的原则体现在这里：
        --backend 决定**哪一路向量索引**
        --fusion  决定**是否并入 BM25 并做 RRF**
        --rerank  决定**是否精排**
    三者正交，所以 2×2×2 的对比矩阵里每一步的增量都是可归因的。
    """
    if args.backend == "bm25":
        if args.fusion == "rrf":
            log.warning(
                "--backend bm25 只有 BM25 一路，没有第二路可融合；--fusion rrf 已忽略。"
            )
        return RecallConfig(
            channels=(CH_BM25,),
            fusion=False,
            pool=args.pool,
            max_per_doc=args.max_per_doc,
        )

    channels = (CH_VECTOR, CH_BM25) if args.fusion == "rrf" else (CH_VECTOR,)
    return RecallConfig(
        channels=channels,
        vector_source="local" if args.backend == "local" else "store",
        backend="" if args.backend == "local" else args.backend,
        fusion=args.fusion == "rrf",
        pool=args.pool,
        max_per_doc=args.max_per_doc,
    )


# =====================================================================
# 投影：块级召回 → 文档级排名
# =====================================================================
def docs_of(chunks: Sequence[Chunk]) -> list[str]:
    """块级结果投影到文档级（保序去重）。

    这**不是**"另一套检索逻辑"，而是一个纯粹的投影：评估口径是文档级
    （标注锚点是 doc_id），线上口径是块级（进上下文的是块）。
    两者之间只差这一个函数，且它无分支、无参数 —— 出不了偏差。
    """
    return dedupe_docs([c.chunk_id for c in chunks], {c.chunk_id: c.doc_id for c in chunks})


def serving_top(chunks: Sequence[Chunk], *, max_per_doc: int, k: int) -> list[Chunk]:
    """线上真实的截断方式：先限制每篇块数，再取 k 块。

    用的是 `cap_per_unit` 本身 —— 与 `rerank_chunks` 里同一份实现。
    这里重写一遍顺序（先 cap 后截断）就足以让两个口径分叉，
    所以刻意不重写。
    """
    if max_per_doc > 0:
        chunks = cap_per_unit(chunks, lambda c: c.doc_id, max_per_unit=max_per_doc)
    return list(chunks[:k])


# =====================================================================
# 单格实验：一种召回配置 × 是否重排
# =====================================================================
@dataclass(slots=True)
class CellResult:
    """一格实验的全部产出。

    抽成结构体的动机是"对比矩阵"：一次模型加载跑 5~6 格，
    每格共用的东西（题目、邻接表、索引产物）只准备一次。
    如果每格都起一个进程，光是 BGE 的加载（本机实测 110 秒）
    就会重复 6 遍 —— 那不是在评估检索，是在评估耐心。
    """

    config: RecallConfig
    rerank: bool
    retrieved: dict[int, list[str]]
    retrieved_served: dict[int, list[str]]
    channel_hits: dict[str, list[int]]
    rerank_seconds: list[float]
    recall_ms: list[float]
    degraded_questions: int
    elapsed: float

    @property
    def label(self) -> str:
        parts = ["+".join(self.config.channels)]
        if self.config.vector_source == "local":
            parts.append("local")
        if self.config.fusion and len(self.config.channels) > 1:
            parts.append("rrf")
        if self.rerank:
            parts.append("rerank")
        return " · ".join(parts)


async def run_cell(
    questions: Sequence,
    *,
    config: RecallConfig,
    rerank: bool,
    rerank_max_per_doc: int,
    rerank_top_k: int,
) -> CellResult:
    """跑一格：逐题召回 →（可选）精排 → 两个文档级投影。"""
    # `0` 在调用方语义里是"没指定"，不是"取 0 块"。
    # 必须在这里解析掉：`serving_top(..., k=0)` 会老老实实返回空列表，
    # 把线上口径变成 0 —— 而这个错误不会报错，只会让指标莫名归零。
    served_k = rerank_top_k or settings.top_k_rerank
    served_cap = rerank_max_per_doc or settings.rerank_max_per_doc

    retrieved: dict[int, list[str]] = {}
    retrieved_served: dict[int, list[str]] = {}
    channel_hits: dict[str, list[int]] = {}
    degraded_questions = 0
    rerank_seconds: list[float] = []
    recall_ms: list[float] = []

    started = time.perf_counter()
    for q in questions:
        t0 = time.perf_counter()
        outcome = await recall_detailed(q.question, config=config)
        recall_ms.append((time.perf_counter() - t0) * 1000)

        for name, n in outcome.channels.items():
            channel_hits.setdefault(name, []).append(n)
        if outcome.degraded:
            degraded_questions += 1
            log.warning("q-%s 召回降级：%s", q.qid, outcome.degraded)

        chunks = outcome.chunks
        if rerank and chunks:
            t1 = time.perf_counter()
            # 一次精排，两个投影：
            # top_k=len(chunks) 且不设文档上限 → 拿到"完整重排顺序"，
            # 从同一份分数里再 derive 出线上口径。跑两遍精排纯属浪费，
            # 而且两遍之间若有任何非确定性，两个口径就不可比了。
            scored = await rerank_chunks(
                q.question, list(chunks), top_k=len(chunks), max_per_doc=0
            )
            rerank_seconds.append(time.perf_counter() - t1)
            retrieved[q.qid] = docs_of(scored)
            retrieved_served[q.qid] = docs_of(
                serving_top(scored, max_per_doc=served_cap, k=served_k)
            )
        else:
            retrieved[q.qid] = docs_of(chunks)
            # ⚠ 关掉重排 ≠ 不截断。
            # 线上 `RERANK_ENABLED=false` 时走的是 `chat._no_rerank_order()`：
            # **先限每篇块数、再取前 k 块**（顺序不能反）。所以非重排格也必须
            # 算 served 口径 —— 否则"重排值不值"就变成了
            # 「重排 + 真实截断」与「不重排 + 不截断」的对比：
            # 后者在线上根本不存在，结论会系统性偏袒重排。
            retrieved_served[q.qid] = docs_of(
                serving_top(chunks, max_per_doc=served_cap, k=served_k)
            )

    return CellResult(
        config=config,
        rerank=rerank,
        retrieved=retrieved,
        retrieved_served=retrieved_served,
        channel_hits=channel_hits,
        rerank_seconds=rerank_seconds,
        recall_ms=recall_ms,
        degraded_questions=degraded_questions,
        elapsed=time.perf_counter() - started,
    )


# =====================================================================
async def run(args: argparse.Namespace) -> int:
    # --index-dir / --collection 覆盖配置：让评估能指向另一份索引产物
    # （比如"改语料前后"的两份 build），而不必改 .env。
    if args.index_dir:
        settings.kb_index_dir = args.index_dir
    if args.collection:
        settings.qdrant_collection = args.collection

    config = build_config(args)
    # 预热索引与（local 后端的）向量缓存，避免把加载时间算进单题延迟
    bundle = get_bundle()
    if not bundle.available:
        raise SystemExit(
            "索引产物不可用。请先执行 python -m scripts.ingest --stage build"
        )

    questions = load_questions(args.questions)
    registry = {} if args.no_expand else bundle.registry()
    adjacency = {} if args.no_expand else neighbor_map(registry)

    log.info(
        "题数 %d · 通道 %s · 向量源 %s · k=%d · pool=%d · 文档上限=%s · rerank=%s",
        len(questions), ",".join(config.channels), config.vector_source,
        args.k, config.pool or settings.top_k_retrieve,
        config.max_per_doc or settings.recall_max_per_doc, args.rerank,
    )
    log.info("索引：kb_version=%s · %d 块 · BM25 %s",
             bundle.kb_version, len(bundle.chunk_to_doc),
             f"{bundle.bm25.size} 块" if bundle.bm25 is not None else "不可用")

    if args.rerank:
        from app.pipeline.embedding import get_reranker

        # 提前加载：CrossEncoder 首次加载是秒级，不能算进单题延迟
        get_reranker()

    rerank_max_per_doc = args.rerank_max_per_doc or settings.rerank_max_per_doc
    rerank_top_k = args.rerank_top_k or settings.top_k_rerank

    try:
        cell = await run_cell(
            questions,
            config=config,
            rerank=args.rerank,
            rerank_max_per_doc=rerank_max_per_doc,
            rerank_top_k=rerank_top_k,
        )
    finally:
        # 评估进程用完即退，但 store 后端（qdrant/chroma）留着连接会让
        # asyncio.run 收尾时报 "Unclosed client"，所以显式关掉。
        from app.pipeline.rag import aclose_store

        await aclose_store()

    retrieved = cell.retrieved
    retrieved_served = cell.retrieved_served
    channel_hits = cell.channel_hits
    degraded_any = cell.degraded_questions
    rerank_seconds = cell.rerank_seconds
    recall_ms = cell.recall_ms
    elapsed = cell.elapsed

    # ---- 评估与展示 ----
    report = evaluate(questions, retrieved, k=args.k, adjacency=adjacency)
    print()
    print(format_table(report))
    print()

    sweep: dict[int, dict] = {}
    if args.sweep:
        # 一次检索、多档评估：避免为了扫 k 而重复跑 embedding
        print("=" * 72)
        print("k 扫描（说明提升空间在哪个档位）")
        print("=" * 72)
        print(f"  {'k':>4}{'recall@k':>12}{'扩展口径':>12}{'hit_rate':>12}")
        for kk in (1, 3, 5, 10):
            r = evaluate(questions, retrieved, k=kk, adjacency=adjacency)
            sweep[kk] = r.summary()
            print(f"  {kk:>4}{r.recall:>12.4f}{r.expanded_recall:>12.4f}{r.hit_rate:>12.4f}")
        full = evaluate(questions, retrieved, k=max(20, config.pool or 20), adjacency=adjacency)
        print(f"  {'MRR':>4}{full.mrr:>12.4f}   （不分 k，衡量目标文档排在多前面）")
        print()

    summary = report.summary()
    title = f"文档级 context recall · 通道 {'+'.join(config.channels)}"
    if config.vector_source == "local":
        title += "（本地向量）"
    elif config.backend:
        title += f"（{config.backend}）"
    if config.fusion and len(config.channels) > 1:
        title += " + RRF 融合"
    if args.rerank:
        title += " + CrossEncoder 重排"
    print("=" * 72)
    print(f"口径：{title}")
    print("=" * 72)
    for kk, vv in summary.items():
        print(f"  {kk}: {vv}")

    # 线上口径单独算一遍：它才是"用户实际拿到的上下文"。
    # 与上面的 pool 口径同时列出，避免用扩池后的漂亮数字充当线上数字。
    # ⚠ 不加 `if args.rerank`：关掉重排时线上**照样**做每篇限块 + 取 top-k，
    # 只给重排格算 served 会让重排跟一个线上不存在的宽松口径比。
    served_report = evaluate(questions, retrieved_served, k=args.k, adjacency=adjacency)
    served_summary = served_report.summary()
    print()
    print("  ---- 线上口径（每篇最多 "
          f"{rerank_max_per_doc} 块，"
          f"取 {rerank_top_k} 块进上下文）----")
    print(f"   context_recall@{args.k}: {served_summary[f'context_recall@{args.k}']}")
    print(f"   mrr: {served_summary['mrr']}")
    print("   （低于上面的 pool 口径是正常的：线上还受块数上限约束）")

    print()
    print(f"  检索耗时 {elapsed:.1f}s / {len(questions)} 题"
          f"（{elapsed / len(questions) * 1000:.0f} ms/题）")
    if recall_ms:
        ordered = sorted(recall_ms)
        print(f"  召回 P50 {ordered[len(ordered) // 2]:.0f} ms / "
              f"P95 {ordered[int(len(ordered) * 0.95) - 1]:.0f} ms"
              "（不含生成，纯检索）")
    if rerank_seconds:
        avg = sum(rerank_seconds) / len(rerank_seconds)
        print(f"  其中重排平均 {avg * 1000:.0f} ms/题（换召回率付出的延迟代价，必须一起报）")
    if config.fusion and len(config.channels) > 1:
        print(f"  每路命中数均值：" + " · ".join(
            f"{name} {sum(v) / len(v):.1f}" for name, v in sorted(channel_hits.items())
        ))
    if degraded_any:
        print(f"  ⚠ 有 {degraded_any} 题发生召回降级（该题分数不可信，看 above 的 warning）")
    if args.no_expand:
        print("  注：已关闭图谱一跳扩展，扩展口径等于严格口径")

    # ---- 产物 ----
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tags = "".join(
        t for t in (
            "_rrf" if config.fusion and len(config.channels) > 1 else "",
            "_rerank" if args.rerank else "",
            f"_{args.label}" if args.label else "",
        )
    )
    out_path = out_dir / f"retrieval_{args.backend}{tags}_{stamp}.json"
    out_path.write_text(
        json.dumps(
            {
                "backend": args.backend,
                "channels": list(config.channels),
                "vector_source": config.vector_source,
                "fusion": config.fusion,
                "rerank": args.rerank,
                "expand_graph": not args.no_expand,
                "k": args.k,
                "pool": config.pool or settings.top_k_retrieve,
                # 参数落盘是"可复现"的一部分：只存指标的话，
                # 下次想复现就得靠猜当时用的上限是多少。
                "recall_max_per_doc": config.max_per_doc or settings.recall_max_per_doc,
                "rerank_max_per_doc": rerank_max_per_doc if args.rerank else None,
                "rerank_top_k": rerank_top_k if args.rerank else None,
                "kb_version": bundle.kb_version,
                "questions_file": args.questions,
                "elapsed_seconds": round(elapsed, 2),
                "recall_ms_p50": round(sorted(recall_ms)[len(recall_ms) // 2], 1) if recall_ms else None,
                "rerank_ms_per_query": round(sum(rerank_seconds) / len(rerank_seconds) * 1000, 1)
                if rerank_seconds else None,
                "summary": summary,
                "summary_served": served_summary or None,
                "k_sweep": sweep,
                "mrr": round(
                    evaluate(questions, retrieved, k=max(20, config.pool or 20),
                             adjacency=adjacency).mrr, 4
                ),
                "channel_hits_avg": {
                    name: round(sum(v) / len(v), 2) for name, v in sorted(channel_hits.items())
                },
                "degraded_questions": degraded_any,
                "per_question": [
                    {
                        "qid": q.qid, "notes": q.notes, "intent": q.intent,
                        "recall": round(q.recall, 4),
                        "expanded_recall": round(q.expanded_recall, 4),
                        "first_hit_rank": q.first_hit_rank,
                        "golden": q.golden, "retrieved_top": q.retrieved_top,
                        "missed": q.missed,
                    }
                    for q in report.scored
                ],
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n  详细产物：{out_path.as_posix()}")
    print("  （含逐题明细与 missed 列表 —— 指标漂了可以按题号回溯到具体改动）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="召回率评估")
    parser.add_argument("--backend", choices=["bm25", "local", "qdrant", "chroma"], default="local")
    parser.add_argument("--collection", default="", help="覆盖集合名（默认取配置）")
    parser.add_argument("--index-dir", default="", help="覆盖索引产物目录（默认取配置）")
    parser.add_argument("--questions", default="eval/questions.json")
    parser.add_argument("--k", type=int, default=5, help="主口径的 k")
    parser.add_argument("--pool", type=int, default=20,
                        help="每路候选池大小（重排/融合的输入规模）")
    parser.add_argument("--max-per-doc", type=int, default=0,
                        help="每路同一文档的块数上限（0 → 取 settings.recall_max_per_doc）")
    parser.add_argument("--rerank", action="store_true", help="对候选池做 CrossEncoder 重排")
    parser.add_argument("--rerank-max-per-doc", type=int, default=0,
                        help="线上口径下每篇进上下文的块数上限（0 → 取 settings）")
    parser.add_argument("--rerank-top-k", type=int, default=0,
                        help="线上口径下进上下文的块数（0 → 取 settings.top_k_rerank）")
    parser.add_argument("--fusion", choices=["none", "rrf"], default="none",
                        help="多路融合方式（向量 + BM25）")
    parser.add_argument("--no-expand", action="store_true", help="关闭图谱一跳扩展口径")
    parser.add_argument("--sweep", action="store_true",
                        help="额外输出 recall@1/3/5/10 与 MRR 的 k 扫描（一次检索多档评估）")
    parser.add_argument("--out-dir", default="eval_output")
    parser.add_argument("--label", default="", help="本次运行标签，写进产物文件名")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

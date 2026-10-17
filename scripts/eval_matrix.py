"""对比矩阵：一次模型加载，跑完"基线 → 融合 → 重排"的完整增量链。

为什么要有这个脚本
------------------
简历上那句"context recall@5 由 x 提升至 y"，分量全在**同一批题、同一条链路、
只改一个变量**这三点上。手工敲三到六条 `eval_retrieval` 命令能跑出这些数字，
但有三个具体问题：

1. **慢**：BGE 首次加载本机实测 **110 秒**（torch + sentence-transformers 的
   导入与初始化占了绝大部分）。6 格各起一个进程就是重复 11 分钟。
   本脚本在**一个进程内**跑完全部格子，模型只加载一次。
2. **不可比的风险**：手工跑容易在不同格之间忘了带同一个 `--pool` 或 `--k`。
   这里参数只解析一次，格子由代码派生，不存在"这一格少个开关"。
3. **容易只报好看的那一格**：所以它把每一格都打出来，
   并显式标注"相对上一格的增量"—— 增量是负数时同样打出来。

同时它做一件 `eval_retrieval.py` 做不了的事：**两条向量路径的一致性校验**
（本地暴力扫描 vs 向量库）。这正是 `brute_force_search` 存在的理由 ——
如果两者的 top-k 文档明显不一致，问题在向量库侧的配置
（距离度量 / 归一化 / 过滤），而不在模型。
这条校验不通过时，上面所有向量格的数字都不该被引用。

用法
----
    python -m scripts.eval_matrix                       # 离线（本地向量），约 3 分钟
    python -m scripts.eval_matrix --vector-source qdrant --parity   # 顺带做一致性校验
    python -m scripts.eval_matrix --vector-source qdrant --rerank   # 加重排（更慢）
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

from app.core.config import settings                                  # noqa: E402
from app.ingest.evaluate import EvalReport, evaluate, load_questions  # noqa: E402
from app.ingest.indexer import neighbor_map                           # noqa: E402
from app.pipeline.rag import (                                        # noqa: E402
    CH_BM25,
    CH_VECTOR,
    RecallConfig,
    get_bundle,
)
from scripts.eval_retrieval import CellResult, run_cell                  # noqa: E402

log = logging.getLogger("matrix")


# =====================================================================
@dataclass(slots=True)
class Row:
    name: str
    cell: CellResult
    report: EvalReport
    served: EvalReport | None

    def line(self) -> str:
        r = self.report
        cells = [
            f"{self.name:<22}",
            f"{r.recall:>10.4f}",
            f"{r.expanded_recall:>10.4f}",
            f"{r.hit_rate:>9.4f}",
            f"{r.mrr:>8.4f}",
        ]
        if self.served is not None:
            cells.append(f"{self.served.recall:>10.4f}")
        return "".join(cells)


def build_rows(
    vector_source: str,
    *,
    pool: int,
    with_rerank: bool,
    rerank_max_per_doc: int,
    rerank_top_k: int,
    only: str = "",
):
    """派生矩阵格子。

    三行构成一条可归因的增量链，每行只比上一行多一个东西：
        BM25            →  关键词能力的天花板（也是"没有向量"的对照）
        向量            →  加上语义召回，与上一行只差一个通道
        向量 + BM25 + RRF → 再多一个通道与融合
    重排作为独立的第二列，作用在每一行上 —— 这样"重排值不值"是在
    三个不同召回质量下分别回答的，而不是只在最好的那个召回下回答。
    召回质量差时重排更值钱，这个结论只有矩阵能看出来。
    """
    plans = [
        ("BM25", RecallConfig(channels=(CH_BM25,), fusion=False, pool=pool)),
        (
            f"向量（{vector_source}）",
            RecallConfig(
                channels=(CH_VECTOR,),
                vector_source="local" if vector_source == "local" else "store",
                backend="" if vector_source == "local" else vector_source,
                fusion=False,
                pool=pool,
            ),
        ),
        (
            "向量+BM25+RRF",
            RecallConfig(
                channels=(CH_VECTOR, CH_BM25),
                vector_source="local" if vector_source == "local" else "store",
                backend="" if vector_source == "local" else vector_source,
                fusion=True,
                pool=pool,
            ),
        ),
    ]

    cells = []
    for name, config in plans:
        # `--only` 按名字子串筛选。存在的理由很实际：重排一格要几十分钟，
        # 而"想验证的新假设"一次通常只有一个（比如"重排能不能把 MRR 再抬上去"）。
        # 没有筛选就只能整表重跑，成本高到没人愿意做实验。
        if only and only not in name:
            continue
        cells.append((name, config, False, rerank_max_per_doc, rerank_top_k))
        if with_rerank:
            cells.append((name + " +重排", config, True, rerank_max_per_doc, rerank_top_k))
    return cells


# =====================================================================
def overlap_at(a: Sequence[str], b: Sequence[str], k: int) -> float:
    """两个排名的 top-k 文档重合率（Jaccard）。"""
    sa, sb = set(a[:k]), set(b[:k])
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb) if (sa | sb) else 0.0


async def parity_check(questions, *, pool: int, k: int) -> dict:
    """本地向量 vs 向量库：同一批题、同一条链路，只换向量索引来源。

    判据：top-k 文档集合的重合率。两者都"差不多好"不算通过 ——
    必须**基本一致**，因为底层是同一个模型对同一批向量算同一种余弦相似度，
    差异只应来自近似索引（HNSW）的取舍，量级是"个别题换了一篇"。
    """
    results: dict[str, dict] = {}
    for source in ("local", "store"):
        config = RecallConfig(
            channels=(CH_VECTOR,), vector_source=source, fusion=False, pool=pool
        )
        cell = await run_cell(
            questions, config=config, rerank=False,
            rerank_max_per_doc=0, rerank_top_k=0,
        )
        results[source] = {"config": config, "cell": cell}

    local, store = results["local"]["cell"], results["store"]["cell"]
    per_q = []
    for q in questions:
        a, b = local.retrieved.get(q.qid, []), store.retrieved.get(q.qid, [])
        if not a and not b:
            continue
        per_q.append(overlap_at(a, b, k))

    mean = sum(per_q) / len(per_q) if per_q else 0.0
    degraded = store.degraded_questions
    return {
        "questions": len(per_q),
        "mean_topk_jaccard": round(mean, 4),
        "store_degraded_questions": degraded,
        "ok": degraded == 0 and mean >= 0.8,
        "store_recall": round(
            evaluate(questions, store.retrieved, k=k).recall, 4
        ),
        "local_recall": round(
            evaluate(questions, local.retrieved, k=k).recall, 4
        ),
    }


# =====================================================================
async def run(args: argparse.Namespace) -> int:
    all_questions = load_questions(args.questions)
    # `--limit` 是为了让"重排"这一列在小样本上也能被量出来。
    # 重排在纯 CPU 上位几十秒/题（见 scripts/bench_rerank.py），
    # 全套 52 题 × 3 格是小时级。**小样本数字必须显式标注样本量**，
    # 绝不能把它当作全套结论引用。
    questions = all_questions[: args.limit] if args.limit else all_questions
    if args.limit:
        log.warning(
            "--limit %d：只跑前 %d 题（共 %d）。小样本结论不得当作全套结论引用。",
            args.limit, len(questions), len(all_questions),
        )
    bundle = get_bundle()
    if not bundle.available:
        raise SystemExit("索引产物不可用；请先执行 python -m scripts.ingest --stage build")

    adjacency = {} if args.no_expand else neighbor_map(bundle.registry())

    if args.rerank:
        from app.pipeline.embedding import get_reranker

        t0 = time.perf_counter()
        get_reranker()
        log.info("重排模型已加载（%.1fs）", time.perf_counter() - t0)

    log.info(
        "题数 %d · 索引 kb_version=%s · %d 块 · 向量源 %s · k=%d · pool=%d",
        len(questions), bundle.kb_version, len(bundle.chunk_to_doc),
        args.vector_source, args.k, args.pool,
    )

    rows: list[Row] = []
    rerank_max_per_doc = args.rerank_max_per_doc or settings.rerank_max_per_doc
    rerank_top_k = args.rerank_top_k or settings.top_k_rerank

    planned = build_rows(
        args.vector_source, pool=args.pool, with_rerank=args.rerank,
        rerank_max_per_doc=rerank_max_per_doc, rerank_top_k=rerank_top_k,
        only=args.only,
    )
    if not planned:
        raise SystemExit(
            f"--only {args.only!r} 没有匹配任何格子。"
            "可用名字：BM25 / 向量 / 向量+BM25+RRF（可用子串）"
        )

    for name, config, rerank, rmpd, rtk in planned:
        log.info("→ 跑 %s …", name)
        cell = await run_cell(
            questions, config=config, rerank=rerank,
            rerank_max_per_doc=rmpd, rerank_top_k=rtk,
        )
        report = evaluate(questions, cell.retrieved, k=args.k, adjacency=adjacency)
        # served 口径**每一格都算**，不再只对重排格算。
        # 因为关掉重排时线上依然会做"每篇限块数 + 取 top-k"截断
        # （见 `scripts/eval_retrieval.py::run_cell` 的说明）——
        # 只给重排格算 served，等于让重排跟一个线上不存在的宽松口径比。
        served = evaluate(questions, cell.retrieved_served, k=args.k, adjacency=adjacency)
        rows.append(Row(name=name, cell=cell, report=report, served=served))

        if cell.degraded_questions:
            log.warning(
                "  ⚠ %s 有 %d 题发生召回降级 —— 该行数字不可信，先看上面的 warning",
                name, cell.degraded_questions,
            )

    # ---- 输出 ----
    header = f"{'口径':<22}{'recall@k':>10}{'扩展':>10}{'hit_rate':>9}{'MRR':>8}{'线上口径':>10}"
    print()
    print("=" * len(header))
    print(f"召回对比矩阵 · 题数 {len(questions)} · k={args.k} · pool={args.pool}")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for row in rows:
        print(row.line())
    print("-" * len(header))
    print("「线上口径」= 每篇最多 "
          f"{rerank_max_per_doc} 块、取 {rerank_top_k} 块进上下文后的文档级 recall。")
    print("每一格都算它 —— 因为关掉重排时线上照样截断，只有两边都是截断口径才可比。")
    print("它与左侧各列只差「块数上限」这一个变量，不是同一件事，别混用。")
    print()
    print(f"{'增量（相对上一行）':<22}")
    prev: Row | None = None
    for row in rows:
        if prev is not None:
            d_recall = row.report.recall - prev.report.recall
            d_mrr = row.report.mrr - prev.report.mrr
            arrow = "↑" if d_recall > 0 else ("↓" if d_recall < 0 else "=")
            print(f"  {prev.name} → {row.name}")
            print(f"      recall@k {d_recall:+.4f} {arrow}   MRR {d_mrr:+.4f}")
        prev = row

    # ---- 一致性校验 ----
    parity: dict | None = None
    if args.parity:
        print()
        print("=" * 72)
        print("两条向量路径一致性校验（本地暴力扫描 vs 向量库）")
        print("=" * 72)
        parity = await parity_check(questions, pool=args.pool, k=args.k)
        print(f"  题目数 {parity['questions']} · top-{args.k} 文档平均重合率 "
              f"{parity['mean_topk_jaccard']:.4f}")
        print(f"  本地向量 recall@{args.k} {parity['local_recall']} · "
              f"向量库 recall@{args.k} {parity['store_recall']}")
        if parity["store_degraded_questions"]:
            print(f"  ⚠ 向量库有 {parity['store_degraded_questions']} 题降级"
                  "（向量库没起来或集合为空？）—— 这不叫「不一致」，叫「没跑通」")
        print("  " + ("✓ 通过：差异在近似索引的取舍量级内"
                      if parity["ok"] else
                      "✗ 未通过：先查向量库的距离度量/归一化/过滤配置，再引用上面的向量格数字"))

    # ---- 产物 ----
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"matrix_{args.vector_source}{'_rerank' if args.rerank else ''}_{stamp}.json"
    out_path.write_text(
        json.dumps(
            {
                "vector_source": args.vector_source,
                "k": args.k,
                "pool": args.pool,
                "questions_run": len(questions),
                "questions_total": len(all_questions),
                "limited_sample": bool(args.limit),
                "only_filter": args.only or None,
                "rerank": args.rerank,
                "rerank_max_per_doc": rerank_max_per_doc if args.rerank else None,
                "rerank_top_k": rerank_top_k if args.rerank else None,
                "kb_version": bundle.kb_version,
                "expand_graph": not args.no_expand,
                "questions_file": args.questions,
                "rows": [
                    {
                        "name": row.name,
                        "channels": list(row.cell.config.channels),
                        "fusion": row.cell.config.fusion,
                        "rerank": row.cell.rerank,
                        "summary": row.report.summary(),
                        "summary_served": row.served.summary() if row.served else None,
                        "degraded_questions": row.cell.degraded_questions,
                        "recall_ms_p50": round(
                            sorted(row.cell.recall_ms)[len(row.cell.recall_ms) // 2], 1
                        ) if row.cell.recall_ms else None,
                        "rerank_ms_avg": round(
                            sum(row.cell.rerank_seconds) / len(row.cell.rerank_seconds) * 1000, 1
                        ) if row.cell.rerank_seconds else None,
                    }
                    for row in rows
                ],
                "parity": parity,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n  产物：{out_path.as_posix()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="召回对比矩阵")
    parser.add_argument("--vector-source", choices=["local", "qdrant", "chroma"],
                        default="local", help="向量索引来源（local 不需要向量库在线）")
    parser.add_argument("--index-dir", default="", help="覆盖索引产物目录")
    parser.add_argument("--questions", default="eval/questions.json")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--pool", type=int, default=20)
    parser.add_argument("--limit", type=int, default=0,
                        help="只跑前 N 题（0=全部）。重排太慢时用小样本，"
                             "但结论必须标注样本量")
    parser.add_argument("--only", default="",
                        help="只跑名字含该子串的格子（如 --only RRF），"
                             "避免为了验证一个新假设而整表重跑")
    parser.add_argument("--rerank", action="store_true", help="额外跑一列「加重排」")
    parser.add_argument("--rerank-max-per-doc", type=int, default=0)
    parser.add_argument("--rerank-top-k", type=int, default=0)
    parser.add_argument("--parity", action="store_true",
                        help="额外做「本地向量 vs 向量库」一致性校验")
    parser.add_argument("--no-expand", action="store_true")
    parser.add_argument("--out-dir", default="eval_output")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.index_dir:
        settings.kb_index_dir = args.index_dir

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

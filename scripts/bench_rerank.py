"""重排延迟基准：把"重排值不值"的代价侧量出来。

为什么必须单独有这么一个工具
---------------------------
本项目的规矩是「报重排收益必须同时报延迟代价」。但重排的延迟有两个特点，
使得随手测是测不准的：

1. **它极慢，且慢得不线性。** 本机实测首次调用 ~50 s、
   连续调用则取决于 batch 里有多少 pair。所以"单题耗时"必须说明
   **候选池多大**，否则这个数字没有意义。
2. **它对 CPU 争用极其敏感。** 同一台机器上，有无其它进程争用 CPU
   会让单题耗时差一到两个数量级。所以报告里必须带上
   `torch.get_num_threads()` 与 `cpu_count`，否则不可复现。

所以这个脚本做三件事：分层测（1 / 5 / 20 pair）、报机器参数、
并给出"扫一遍全套标注集要多久"的换算 —— 后者决定了它能进哪个环节
（在线？还是只能走缓存？）。

还有一个**更容易犯的错**：把候选池大小当成一个可以随手写死的常数。
`--pairs 20` 里的 20 是人为指定的，而线上真正送进重排的块数由
`top_k_retrieve`（每路召回）、两路重叠率、`recall_max_per_doc`（每篇上限）
共同决定 —— 在默认 `top_k_retrieve=20` + RRF 下，两路各回 20 块、
去重后剩下多少块，只有跑一遍才知道。用错的池子换算"单题耗时"，
数字会系统性偏乐观（偏多少取决于重叠率）。所以提供 `--from-recall`：
先实测真实候选池，再把实测到的池子加进测量档位，最后按**实测池**换算。

用法
----
    python -m scripts.bench_rerank                    # 默认 1 / 5 / 20 pair
    python -m scripts.bench_rerank --pairs 10,30      # 自定义档位
    python -m scripts.bench_rerank --from-recall 12   # 先实测真实候选池再换算
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402


def probe_cpu(repeat: int = 3) -> dict:
    """给这台机器测一个"算力标尺"，用来判断本次数字有多大不确定性。

    为什么要有：延迟数字不标注机器就没有意义。但"16 核 / torch 12 线程"
    说明不了什么 —— 一台 16 vCPU 的低频共享 VM 和一台 16 核桌面 CPU，
    参数完全一样，实际算力能差一个数量级。

    这里用 1000×1000 fp32 矩阵乘做标尺：它纯走 BLAS、不涉及 Python 解释器开销。
    正常桌面 CPU 约 20~60 ms。

    ⚠️ **别把它当成"折算倍率"。** 本项目实测过：同一条命令在同一台机上
    一小时内给出过 257 / 65.5 / 34.4 ms —— 漂移 7.5 倍，而且 257 ms 那次
    是因为我自己有并发任务在抢 CPU。所以这个标尺的正确用途是
    **说明"这台机器上的绝对毫秒值不可引用"**，而不是提供一个可乘的系数。
    真正可引用的是同轮内的相对差异与质量指标（召回率 / MRR 与算力无关）。
    """
    import numpy as np

    a = np.random.rand(1000, 1000).astype("float32")
    b = np.random.rand(1000, 1000).astype("float32")
    a @ b                                   # 预热（首次调用含 BLAS 初始化）
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        a @ b
        samples.append(time.perf_counter() - t0)
    median_ms = statistics.median(samples) * 1000
    return {
        "matmul_1000_fp32_ms": round(median_ms, 1),
        "reference_desktop_ms": "20~60",
        "slowdown_vs_desktop": round(median_ms / 40.0, 1),
        "caveat": "该标尺本身在共享 vCPU 上可漂移数倍（实测 257/65.5/34.4 ms），"
                  "只能用于判断不确定性，不能作为折算倍率",
    }


async def observe_pool(*, questions_file: str, limit: int) -> dict:
    """实测"线上重排一次到底要打多少对"。

    走的是**真实的 `recall_detailed`**，不是另写一份检索 —— 否则就回到了
    "评估口径与线上口径两张皮"的老问题（见 README「评估与线上同源」一节）。
    这里只数块数，不打分，所以比带重排的评测便宜几个数量级。
    """
    from app.ingest.evaluate import load_questions
    from app.pipeline.rag import recall_detailed

    questions = load_questions(questions_file)
    if limit:
        questions = questions[:limit]
    sizes: list[int] = []
    for q in questions:
        outcome = await recall_detailed(q.question)
        sizes.append(len(outcome.chunks))
    ordered = sorted(sizes)
    return {
        "questions": len(sizes),
        "min": ordered[0] if ordered else 0,
        "median": ordered[len(ordered) // 2] if ordered else 0,
        "max": ordered[-1] if ordered else 0,
        "mean": round(statistics.fmean(sizes), 1) if sizes else 0.0,
        "sizes": sizes,
    }


def merge_specs(pairs_arg: str, observed: dict | None) -> list[int]:
    """测量档位 = 用户手填的 + **实测到的线上池子**。

    这两件事必须合并到一个列表里：只测手填档位，就等于继续用手填的池子换算，
    `--from-recall` 白做；只测实测档位，就丢了 `--pairs` 的意图。
    抽成纯函数是为了能被单测覆盖 —— 这段逻辑一旦错了
    （比如忘了 append），现象只是"数字偏乐观"，不报错。
    """
    specs = sorted({int(s.strip()) for s in pairs_arg.split(",") if s.strip()})
    median = (observed or {}).get("median") or 0
    if median and median not in specs:
        specs.append(median)
        specs.sort()
    return specs


def pick_serving(rows: list[dict], observed: dict | None) -> dict:
    """挑出"代表单次线上请求"的那一行，用它做换算。

    ⚠ 不能用 `rows[-1]`：那是"我指定的最大档位"，不等于线上真实规模。
    有实测值就用实测中位；万一那个档位没被测到（理论上不会，
    因为 merge_specs 会把它加进去），退回到最大档位并保持保守。
    """
    if not rows:
        raise ValueError("没有任何测量行，无法换算")
    median = (observed or {}).get("median") or 0
    if median:
        hit = next((r for r in rows if r["pairs"] == median), None)
        if hit is not None:
            return hit
    return rows[-1]


def build_pairs(n: int, *, doc_chars: int) -> list[tuple[str, str]]:
    """构造贴近真实分布的 (query, doc) 对。

    长度取值不是随手写的：query 用真实问题长度（~30 字），
    doc 用 `chunker.MAX_CHARS` 对应的量级（正文约 460 字）。
    用 'a'*5000 这种超长文档测出来的数字会显著偏慢，
    而用 'x' 这种超短文档会显著偏快 —— 两者都不能代表线上。
    """
    query = "数据库连接池耗尽导致服务雪崩，应该先做什么？"
    doc = (
        "现象：连接池活跃连接数打满，应用侧报 cannot get connection。"
        "处置：先查 processlist 定位阻塞 SQL，确认是否存在长事务；"
        "再核对 max_connections 与池上限是否匹配。"
    )
    doc = (doc * (doc_chars // len(doc) + 1))[:doc_chars]
    return [(query, doc) for _ in range(n)]


def main() -> int:
    parser = argparse.ArgumentParser(description="重排延迟基准")
    parser.add_argument("--pairs", default="1,5,20",
                        help="要测的候选池规模，逗号分隔（默认 1,5,20）")
    parser.add_argument("--doc-chars", type=int, default=460,
                        help="每篇文档的字符数（默认对齐 chunker.MAX_CHARS）")
    parser.add_argument("--repeat", type=int, default=3, help="每档重复次数，取中位数")
    parser.add_argument("--questions", type=int, default=52,
                        help="用于换算'扫一遍标注集要多久'的题数")
    parser.add_argument("--from-recall", type=int, default=0, metavar="N",
                        help="先跑前 N 题的真实召回，实测线上候选池大小，"
                             "并把实测池加进测量档位（0=不测）")
    parser.add_argument("--questions-file", default="eval/questions.json")
    parser.add_argument("--out-dir", default="eval_output")
    args = parser.parse_args()

    import torch

    from app.pipeline.embedding import get_reranker

    machine = {
        "cpu_count": os.cpu_count(),
        "torch_threads": torch.get_num_threads(),
        "torch": torch.__version__,
        "model": settings.reranker_model,
        "doc_chars": args.doc_chars,
        "repeat": args.repeat,
    }

    print("=" * 72)
    print("重排延迟基准")
    print("=" * 72)
    print(f"  机器：{machine['cpu_count']} 核 / torch {machine['torch_threads']} 线程"
          f" / torch {machine['torch']}")
    print(f"  模型：{machine['model']} · 文档 {args.doc_chars} 字")

    # 先标定机器算力。这一步要在加载模型之前做 ——
    # 否则测出来的是"模型加载把 CPU 占满时的算力"，没有参照价值。
    cpu = probe_cpu()
    machine.update(cpu)
    print(f"  算力标尺：1000×1000 fp32 matmul {cpu['matmul_1000_fp32_ms']} ms"
          f"（正常桌面约 {cpu['reference_desktop_ms']} ms"
          f" → 本机慢约 {cpu['slowdown_vs_desktop']}×）")

    # 先实测真实候选池，再加载重排模型。
    # 顺序不能反：这一步只做召回，若排在模型加载之后，
    # 两个阶段的内存/线程占用会互相干扰，加载耗时就不可比了。
    observed: dict | None = None
    if args.from_recall:
        print()
        print(f"  实测线上候选池（前 {args.from_recall} 题 · 真实 recall_detailed · "
              f"top_k_retrieve={settings.top_k_retrieve} · "
              f"recall_max_per_doc={settings.recall_max_per_doc}）…")
        observed = asyncio.run(
            observe_pool(questions_file=args.questions_file, limit=args.from_recall)
        )
        print(f"    候选池 min {observed['min']} / 中位 {observed['median']} / "
              f"max {observed['max']} / 均值 {observed['mean']} 块")
        print("    这解释了为什么不能拿固定的 20 去换算单题耗时。")

    t0 = time.perf_counter()
    reranker = get_reranker()
    load_seconds = time.perf_counter() - t0
    print(f"  模型加载：{load_seconds:.1f}s（含 import，只付一次）")
    print()

    # 首次调用单独计：CrossEncoder 的第一次 predict 会做一次性初始化
    # （tokenizer/图优化/线程池），混进平均会把后续档位都算慢。
    t0 = time.perf_counter()
    reranker.predict(build_pairs(1, doc_chars=args.doc_chars))
    first_call = time.perf_counter() - t0
    print(f"  首次调用（含一次性初始化）：{first_call:.1f}s")
    print()

    # 档位 = 用户指定的 + **实测到的线上池子**。
    # 缺了后半句，`--from-recall` 就只是打印了一段好看的信息，
    # 换算仍然用着手填的那个错数字 —— 那等于没做这件事。
    specs = merge_specs(args.pairs, observed)
    if observed and observed["median"] in specs and str(observed["median"]) not in args.pairs:
        print(f"    已把实测中位 {observed['median']} 块加入测量档位")

    rows: list[dict] = []
    print(f"  {'候选数':>6}{'单次中位数':>12}{'每次/篇':>12}{'每对':>12}")
    for n in specs:
        pairs = build_pairs(n, doc_chars=args.doc_chars)
        samples: list[float] = []
        for _ in range(args.repeat):
            t0 = time.perf_counter()
            reranker.predict(pairs)
            samples.append(time.perf_counter() - t0)

        median = statistics.median(samples)
        per_pair = median / n
        rows.append({
            "pairs": n,
            "median_seconds": round(median, 3),
            "min_seconds": round(min(samples), 3),
            "max_seconds": round(max(samples), 3),
            "seconds_per_pair": round(per_pair, 4),
        })
        print(f"  {n:>6}{median:>11.2f}s{median:>11.2f}s{per_pair:>11.3f}s")

    # 换算必须按**实测到的线上池子**来，不能用 rows[-1]：
    # rows[-1] 是"我指定的最大档位"，而线上池子是我测出来的，
    # 两者不一定相等（而且真正线上那个值可能比任何指定档位都大）。
    serving = pick_serving(rows, observed)
    wall = serving["median_seconds"]
    full_suite = wall * args.questions
    print()
    print("  换算：")
    print(f"    单次问答（候选池 {serving['pairs']}"
          + ("，**实测线上中位**" if observed else "，手填档位") + f"）：重排约 {wall:.1f}s"
          " —— 这个量级**不能**放在在线路径里")
    print(f"    扫一遍 {args.questions} 题标注集：约 {full_suite / 60:.1f} 分钟"
          + ("（按实测池换算）" if observed else "（按手填档位换算，仅供参考）"))
    if not observed:
        print("    ⚠ 没有 --from-recall：候选池是手填的，单题耗时很可能偏乐观。")

    # 关键设计结论：既然单次就是几十秒，重排只能在两条路里选一条 ——
    # 走缓存摊薄，或换更小的模型/加 GPU。把这句话写进产物，
    # 免得后人只看数字看不出该怎么做。
    conclusion = (
        "重排单次耗时已到几十秒量级，不适合放在在线首答路径。"
        "可选处置：① 一键关掉（RERANK_ENABLED=false）；"
        "② 用缓存摊薄（CACHE_RERANK_TTL，同一 query 复用重排结果）；"
        "③ 换更小的重排模型 / 量化 / ONNX；④ 上 GPU；"
        "⑤ 缩小重排候选池（代价是召回率）。"
        f"本次单题耗时按候选池 {serving['pairs']} 块换算"
        + ("（实测线上中位）" if observed else "（手填档位，偏乐观）") + "。"
        "⚠ 关于机器算力标尺：把它当成'折算倍率'是错的 —— "
        f"本机 matmul 标尺 {cpu['matmul_1000_fp32_ms']} ms"
        f"（相对桌面 {cpu['slowdown_vs_desktop']}×），"
        "但同一条命令在共享 vCPU 上一小时内实测漂移过 7.5 倍"
        "（257 / 65.5 / 34.4 ms）。它的用途是**判断本次数字有多大不确定性**，"
        "而不是提供一个可乘的系数。所以：绝对延迟只报量级，"
        "报告重排收益时必须同时给出 ms/题与候选池规模。"
    )
    print()
    print(f"  ⚠ {conclusion}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"bench_rerank_{stamp}.json"
    out_path.write_text(
        json.dumps(
            {
                "machine": machine,
                "load_seconds": round(load_seconds, 2),
                "first_call_seconds": round(first_call, 2),
                "rows": rows,
                # 实测的线上候选池原样落盘：这是"该按多少对换算"的唯一依据，
                # 也是别人复核这个数字时最需要看到的东西。
                "observed_pool": observed,
                "serving_pool": serving["pairs"],
                "full_suite_minutes": round(full_suite / 60, 1),
                "conclusion": conclusion,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n  产物：{out_path.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""埋点开销基准：把"可观测值多少钱"变成一个能重跑的命令。

为什么需要这个脚本
------------------
README 里那张开销表（P50 0.6ms / P95 1.6ms / 2.4KB 每轮）如果只写在文档里，
它迟早会变成"上个季度某台机器上的传说" —— 换机器、改 sink、调字段之后就没人
知道还准不准。可观测本身的开销，最好也用可观测的方式给出：一条命令、可重跑。

测什么、不测什么
----------------
· 测：`Tracer` 这一层的**纯本地开销** —— 建 span 对象、序列化一行 JSON、同步写盘。
  这正是请求路径上真实发生的事（flush 只在关机，不在路径上）。
· 不测：Langfuse 上报（网络抖动会把数字搞成噪声，且它本来就被设计成旁路）、
  也不测编排器本身的耗时（那是业务）。
· 对照组的做法：同样的 span 调用序列，`enabled=False` 走 `NullTurn`。
  这验的是"开埋点相对不开埋点多付了多少"，而不是"埋点消耗了多少 CPU"。

口径说明（跑之前先看这四条，否则数字没法比较）
--------------------------------------------
1. 单轮 = 1 个 turn 根 + 10 个阶段 span + 1 个 rerank + 2 个工具（=`orchestrator`
   实测的 span 数）。span 数变了，数字自然变。
2. 每个 span 都**同步** `write + flush`（JsonlSink 的设计选择，见其 docstring），
   所以这里量到的是"含一次系统调用"的耗时，与生产一致。
3. 全部在同一台机器、同一进程、单线程内串行 —— 这是**上界**：真实并发下写盘
   摊到多个请求，单请求的分摊成本只会更低。
4. 输出的是**分位数**不是均值。均值会被偶发的 GC/杀毒扫描拉走，看不出长尾。

用法
----
    python -m scripts.bench_trace                 # 默认 500 轮
    python -m scripts.bench_trace --rounds 2000
    python -m scripts.bench_trace --req-ms 3435   # 用它折算占比（默认取自测热路径实测值）
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

from app.observability.tracing import Tracer
from scripts.selftest import reconcile_stdout

# 一轮典型 RAG 请求的 span 序列：与 orchestrator 的实测 span 集一致（10 个阶段 + rerank + 2 工具）
# 括号里是 as_type，因为它影响"是否带 model / 是否算 generation"这类分支
STAGES: list[tuple[str, str]] = [
    ("route", "span"),
    ("memory_load", "span"),
    ("embed", "embedding"),
    ("cache_lookup", "span"),
    ("singleflight", "span"),
    ("recall", "retriever"),
    ("rerank", "reranker"),
    ("generate", "generation"),
    ("cache_store", "span"),
    ("memory_save", "span"),
]

TOOL_NAMES = ("query_ticket_by_no", "search_kb")

# 预热轮数：让文件句柄、目录项、OS 缓存先热起来。这几轮也会写盘（要的就是同一个文件被反复写），
# 所以统计落盘体积时要按实际行数反推轮数，别用 args.rounds 当分母。
WARMUP = 20


def one_turn(tracer: Tracer, i: int) -> None:
    """跑一轮"形状正确"的埋点，不跑真实业务。

    刻意让 metadata 的字段名/长度贴近 orchestrator（rank_scores 是个 5 元列表、
    answer 是几百字），因为 JSON 序列化的成本主要取决于**字段体积**：
    用一个 `{"a":1}` 的空 span 去测，会得到偏乐观一个数量级的数字。
    """
    turn = tracer.start_turn(
        message="如何排查 502 与 504",
        session_id=f"bench-{i % 20}",       # 20 个会话轮转，模拟多用户
        use_cache=False,
        kb_version=1,
    )
    for name, as_type in STAGES:
        kwargs: dict = {}
        if as_type == "embedding":
            kwargs["model"] = "BAAI/bge-small-zh-v1.5"
        if as_type == "retriever":
            kwargs.update(channels=["vector", "bm25"], order_by="rrf")
        if as_type == "reranker":
            kwargs["parent"] = "recall"
        if as_type == "generation":
            kwargs["model"] = "deepseek-chat"
        with turn.stage(name, as_type=as_type, **kwargs) as sp:
            if as_type == "retriever":
                sp.set(output={
                    "n_chunks": 5,
                    "top_sources": ["data/raw/faq/faq-05.md", "data/raw/runbooks/rb-gateway-5xx.md"],
                    "rank_scores": [0.032787, 0.032258, 0.029437, 0.02886, 0.015873],
                })
            if as_type == "generation":
                sp.first_token()               # TTFT 的唯一写入点

    for name in TOOL_NAMES:
        with turn.stage("tools", as_type="tool", tool=name) as sp:
            sp.set(output={"rows": 1})

    turn.mark_ttft()
    turn.finish(
        answer="排查 502 与 504 的要点如下：" + "…" * 300,   # 几百字的答案，贴近真实
        route="rag",
        cache="miss",
    )


def measure(tracer: Tracer, rounds: int, warmup: int = WARMUP) -> list[float]:
    """返回每轮耗时（毫秒）。warmup 单独跑，不进入统计。"""
    for i in range(warmup):
        one_turn(tracer, i)
    samples: list[float] = []
    for i in range(rounds):
        t0 = time.perf_counter()
        one_turn(tracer, i)
        samples.append((time.perf_counter() - t0) * 1000)
    return samples


def pct(samples: list[float], q: float) -> float:
    """分位数（线性插值）。样本已排序时 O(1)，这里为了短数据直接现算。"""
    if not samples:
        return 0.0
    s = sorted(samples)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.glob("*.jsonl") if p.is_file())


def report(label: str, samples: list[float]) -> None:
    print(f"  {label:<26} n={len(samples)}  "
          f"P50 {pct(samples, 0.50):7.3f}ms  "
          f"P95 {pct(samples, 0.95):7.3f}ms  "
          f"P99 {pct(samples, 0.99):7.3f}ms  "
          f"max {max(samples):7.3f}ms  "
          f"mean {statistics.fmean(samples):7.3f}ms")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="埋点开销基准（本地 JSONL，同步写盘；不含 Langfuse 网络上报）")
    parser.add_argument("--rounds", type=int, default=500, help="每组的轮数（默认 500）")
    parser.add_argument("--req-ms", type=float, default=3435.0,
                        help="用它把开销折算成占比；默认 3435ms 取自 selftest 热路径实测（--only stream 的 total_ms）")
    parser.add_argument("--json", action="store_true", help="只输出机器可读结果（便于贴进报告/流水线）")
    args = parser.parse_args(argv)

    hint = reconcile_stdout()
    if hint and not args.json:
        print(hint)

    tmp = Path(tempfile.mkdtemp(prefix="bench-trace-"))

    # 对照组：enabled=False → NullTurn（编排器里的埋点调用一行没少，只是不记录）
    off = measure(Tracer(enabled=False), args.rounds)
    # 实验组：真写盘
    on_tracer = Tracer(enabled=True, jsonl_dir=tmp, sample_rate=0.0)  # 采样 0：只测本地，不碰网络
    on = measure(on_tracer, args.rounds)
    on_tracer.close()

    rows = sum(1 for p in tmp.glob("*.jsonl") for _ in p.open("r", encoding="utf-8"))
    size = dir_bytes(tmp)
    spans_per_turn = 1 + len(STAGES) + len(TOOL_NAMES)     # turn 根 + 阶段 + 工具
    # warmup 也写盘（要的就是同一个文件被反复写热），所以行数里含 warmup。
    # 用"行数 ÷ 每轮行数"反推轮数，别拿 args.rounds 当分母 —— 那样会高估 ~4%。
    turns_written = rows / spans_per_turn if spans_per_turn else 0
    delta_p50 = pct(on, 0.50) - pct(off, 0.50)

    if args.json:
        print(json.dumps({
            "rounds": args.rounds,
            "spans_per_turn": spans_per_turn,
            "rows": rows,
            "bytes": size,
            "bytes_per_turn": round(size / turns_written, 1) if turns_written else 0,
            "off_ms": {"p50": round(pct(off, 0.5), 3), "p95": round(pct(off, 0.95), 3), "max": round(max(off), 3)},
            "on_ms": {"p50": round(pct(on, 0.5), 3), "p95": round(pct(on, 0.95), 3), "max": round(max(on), 3)},
            "delta_p50_ms": round(delta_p50, 3),
            "req_ms": args.req_ms,
            "overhead_pct": round(delta_p50 / args.req_ms * 100, 4),
            "dir": str(tmp),
            "cond": {"py": sys.version.split()[0], "os": platform.platform()},
        }, ensure_ascii=False, indent=2))
        return 0

    print("=" * 72)
    print("埋点开销基准 · 本地 JSONL，同步 write+flush，单线程串行")
    print("=" * 72)
    print(f"  rounds={args.rounds} · spans/turn={spans_per_turn} · "
          f"py {sys.version.split()[0]} · {platform.platform()}")
    print()
    report("埋点关（NullTurn）", off)
    report("埋点开（JSONL 全量）", on)
    print()
    print(f"  增量（P50）：{delta_p50:+.3f}ms/轮")
    print(f"  落盘：{rows} 行 / {size / 1024 / 1024:.2f} MB "
          f"（≈ {size / turns_written / 1024:.2f} KB/轮，含 {WARMUP} 轮 warmup）"
          if turns_written else f"  落盘：{rows} 行 / {size / 1024 / 1024:.2f} MB")
    print(f"  折算：{delta_p50:.3f}ms / {args.req_ms:.0f}ms 请求 = "
          f"{delta_p50 / args.req_ms * 100:.4f}%")
    print()
    print(f"  trace 目录：{tmp}")
    print("  （跑完不自动删：留着能自己核对那 6.6KB 一行都装了什么；不用了手工删）")
    print()
    print("口径提醒：")
    print("  · 这是**上界**——单线程串行写盘；并发下写盘摊到多请求，单请求分摊更低。")
    print("  · 不含 Langfuse 上报（旁路 + 仅在关机 flush），网络抖动不进这个数。")
    print("  · 换机器/加字段后重跑同一条命令，才谈得上比较。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

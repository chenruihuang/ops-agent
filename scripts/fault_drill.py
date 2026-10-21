"""故障演练：多类故障 × 多轮，产出可用性 / 检测延迟 / MTTR 报告。

与 `scripts/fault_inject.py` 的分工
----------------------------------
- `fault_inject.py`：单次注入 + 采 T1/T2/T3（它自己会 `docker stop`，但**容器名是按
  compose 服务名写的**，本机实际名是 `ops-redis` 这种，所以直接跑会"注入失败"）；
- 本文件：解析真实容器名 → 循环多轮 → **每轮 finally 恢复并等健康** → 汇总成报告。

跑法
----
    python -m scripts.fault_drill --dry-run                 # 只打印计划，不动任何容器
    python -m scripts.fault_drill --rounds 5                # 三类故障各 5 轮（会停容器！）
    python -m scripts.fault_drill --faults stop-redis --rounds 5

安全性
------
1. 每一轮都在 `finally` 里 `docker start`，**中途 Ctrl-C 也会恢复**（KeyboardInterrupt 同样走 finally）。
2. 恢复后等 healthcheck 变 healthy，再进下一轮 —— 否则下一轮的 T1 会落在"还没起来"的状态上，
   把恢复时间算成负数或 0。
3. `--dry-run` 是默认推荐的第一次运行方式：先确认要停哪些容器、解析到的名字对不对。

口径
----
    T1 注入 · T2 告警 firing · T3 连续 3 次收到 token
    MTTR = T3 - T2    检测延迟 = T2 - T1    端到端 = T3 - T1

**非关键依赖的故障不一定影响对外可用性**（例如缓存挂了，回答仍能生成，只是变慢）。
这种情况下可用性 100% 是**正确结论**，MTTR 只描述"恢复到无降级"的时间；
报告里必须同时给 `degraded` 观测，否则读者会以为"故障没发生"。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from app.eval.fault_report import RoundResult, median, summarize, to_markdown
from scripts.fault_inject import FAULTS, docker, probe_api, prometheus_alert_firing

DEFAULT_QUESTION = "订单服务对单用户的下单限流是多少？超限返回什么错误码？"


class AlertWatcher(threading.Thread):
    """独立线程按秒轮询告警。

    为什么不能用"每次探测之后顺便查一次"：故障期间单次请求可能长达 30s
    （Redis 连接超时 × 两次），探测循环因此变成 30s 一次 ——
    而告警在 1 分钟窗口的 `rate()` 里可能只 firing 十几秒，就被完美错过。
    实测踩过：告警确实 firing（Prometheus 里有 ALERTS 序列），演练却报"未检测到"。
    **测量代码的采样频率必须高于被测信号的持续时长。**
    """

    def __init__(self, prometheus: str, alert: str, interval: float = 1.0) -> None:
        super().__init__(daemon=True)
        self.prometheus = prometheus
        self.alert = alert
        self.interval = interval
        self.fired_at: float | None = None
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            if prometheus_alert_firing(self.prometheus, self.alert):
                self.fired_at = time.time()
                return
            self._stop_event.wait(self.interval)

    def stop(self) -> None:
        self._stop_event.set()


def run_round(
    *,
    fault: str,
    container: str,
    alert: str,
    api: str,
    prometheus: str,
    key: str,
    question: str,
    detect_timeout: float,
    recover_timeout: float,
) -> RoundResult:
    """一轮：注入 → **边打流量边等告警** → 等恢复。

    为什么检测窗口里必须持续发请求：
    告警表达式算的是 `rate(cache_requests_total{result="error"}[1m])`，
    没有流量就恒为 0 —— 第一版在窗口里只轮询 Prometheus，结果"永远检测不到"。
    错误率类告警天生需要流量，检测循环必须自己制造流量（这也更贴近真实用户）。

    为什么检测失败时**不把 T2 回填成 T1**：
    回填会让 MTTR 退化成"端到端时间"，看起来有个数，实际是编的。
    宁可报 `—`（未检测到），也不能让一个不存在的检测时刻变成结论。
    """
    t1 = time.time()
    print(f"\n[注入] docker stop {container}")
    code, out = docker("stop", container)
    if code != 0:
        return RoundResult(fault=fault, round=0, note=f"注入失败：{out[:80]}")

    samples: list[dict[str, Any]] = []
    t2: float | None = None
    t3: float | None = None

    watcher = AlertWatcher(prometheus, alert)
    if prometheus_alert_firing(prometheus, alert):
        # 上一轮的 `rate(...[1m])` 可能还没衰减完 —— 此时 T2 会偏早。
        # 不隐藏这个事实：报告里能看到 T2 明显小于注入耗时。
        print("  [警告] 注入前告警已在 firing（上一轮残留），本轮检测延迟偏小")
    watcher.start()
    print("  等待监控检测（期间持续发请求制造流量）…", end="", flush=True)
    deadline = t1 + detect_timeout
    try:
        while time.time() < deadline:
            if watcher.fired_at is not None:
                t2 = watcher.fired_at
                print(f" 已检测（{t2 - t1:.1f}s）")
                break
            samples.append(probe_api(api, key, question, timeout=30.0))
        if t2 is None:
            # 探测本身可能刚回来，再看一眼 —— 不要让最后一跳的告警白等
            if watcher.fired_at is not None:
                t2 = watcher.fired_at
                print(f" 已检测（{t2 - t1:.1f}s）")
            else:
                print(f" {detect_timeout:.0f}s 内未检测到告警")
                samples.append(probe_api(api, key, question, timeout=30.0))
    finally:
        watcher.stop()

    print("  等待服务恢复（连续 3 次成功）…", end="", flush=True)
    consecutive = 0
    recover_deadline = time.time() + recover_timeout
    while time.time() < recover_deadline:
        samples.append(probe_api(api, key, question, timeout=30.0))
        if samples[-1].get("ok"):
            consecutive += 1
            if consecutive >= 3:
                t3 = time.time()
                break
        else:
            consecutive = 0
        time.sleep(1.0)
    if t3 is None:
        print(f" {recover_timeout:.0f}s 内未恢复")
    else:
        print(f" 已恢复（{t3 - t1:.1f}s）")

    attempts = len(samples)
    successes = sum(1 for s in samples if s.get("ok"))
    latencies = [float(s.get("elapsed") or 0) * 1000 for s in samples if s.get("elapsed")]
    notes: list[str] = []
    if t2 is None:
        notes.append("窗口内未检测到告警")
    if t3 is None:
        # 这不是脚本失败，而是**这条故障没有自愈路径**：缓存没有熔断器，
        # 每个请求都要等 Redis 连接超时（实测 30s+），窗口内凑不齐 3 连成功。
        # 结论应写成"恢复依赖依赖本身回来"，而不是编一个 MTTR。
        notes.append("窗口内未恢复（该故障无熔断自愈路径，恢复依赖依赖本身）")
    return RoundResult(
        fault=fault,
        round=0,
        availability=(successes / attempts) if attempts else 0.0,
        detection_s=(t2 - t1) if t2 else None,
        mttr_s=(t3 - t2) if (t2 and t3) else None,
        end_to_end_s=(t3 - t1) if t3 else None,
        attempts=attempts,
        successes=successes,
        probe_ms_median=median(latencies),
        note="；".join(notes),
    )


def resolve_container(name: str) -> str | None:
    """把 compose 服务名解析成真实容器名。

    compose 里有些服务写了 `container_name`，有些没写（会变成 `项目-服务-序号`）。
    `docker stop redis` 在本机直接失败 —— 而失败信息是"注入失败"，
    看起来像故障没注入成功，实际是名字错了。
    """
    candidates = [name, f"ops-{name}"]
    for cand in candidates:
        code, _ = docker("inspect", "-f", "{{.Name}}", cand, timeout=15)
        if code == 0:
            return cand
    code, out = docker(
        "ps", "--filter", f"label=com.docker.compose.service={name}",
        "--format", "{{.Names}}", timeout=15,
    )
    if code == 0 and out.strip():
        return out.strip().splitlines()[0]
    return None


def wait_healthy(container: str, timeout: float = 90.0) -> bool:
    """等 healthcheck 变 healthy（无 healthcheck 的容器只看 running）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, out = docker("inspect", "-f",
                           "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                           container, timeout=15)
        state = out.strip().lower()
        if state in ("healthy", "running"):
            return True
        time.sleep(1.5)
    return False


def restore(container: str) -> bool:
    docker("start", container, timeout=60)
    return wait_healthy(container)


def api_key(cli: str | None) -> str:
    if cli:
        return cli.strip()
    env = Path(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("API_KEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("API_KEY", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="故障演练矩阵（会停容器，默认 dry-run 需显式 --run）")
    parser.add_argument("--faults", default="stop-redis,stop-qdrant,stop-mysql")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--api", default="http://localhost:8080")
    parser.add_argument("--prometheus", default="http://localhost:9090")
    parser.add_argument("--key", default=None)
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--detect-timeout", type=float, default=45.0)
    parser.add_argument("--recover-timeout", type=float, default=90.0)
    parser.add_argument("--out-dir", default="eval_output")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划（默认行为，显式 --run 才会停容器）")
    parser.add_argument("--run", action="store_true", help="真的执行（会 docker stop）")
    args = parser.parse_args(argv)

    names = [n.strip() for n in args.faults.split(",") if n.strip()]
    unknown = [n for n in names if n not in FAULTS]
    if unknown:
        print(f"[FATAL] 未知故障：{unknown}；可选 {sorted(FAULTS)}", file=sys.stderr)
        return 2

    resolved: dict[str, str] = {}
    for name in names:
        container = resolve_container(FAULTS[name]["container"])
        if container is None:
            print(f"[FATAL] 找不到容器：{FAULTS[name]['container']}（服务没起？）", file=sys.stderr)
            return 2
        resolved[name] = container

    print(f"计划：{len(names)} 类 × {args.rounds} 轮 = {len(names) * args.rounds} 轮")
    for name, container in resolved.items():
        print(f"  {name:<12} → 容器 {container:<12} 告警 {FAULTS[name]['alert']}")
    print(f"  api={args.api} prometheus={args.prometheus}")
    print("  每轮结束恢复容器并等健康检查通过")

    if args.dry_run or not args.run:
        print("\n[dry-run] 未执行任何操作。确认上面是你要停的容器后加 --run。")
        return 0

    key = api_key(args.key)
    if not key:
        print("[FATAL] 找不到 API_KEY", file=sys.stderr)
        return 2

    rounds: list[RoundResult] = []
    for name in names:
        container = resolved[name]
        for i in range(1, args.rounds + 1):
            print(f"\n===== {name} 第 {i}/{args.rounds} 轮 =====")
            try:
                result = run_round(
                    fault=name,
                    container=container,
                    alert=FAULTS[name]["alert"],
                    api=args.api,
                    prometheus=args.prometheus,
                    key=key,
                    question=args.question,
                    detect_timeout=args.detect_timeout,
                    recover_timeout=args.recover_timeout,
                )
                result.round = i
                rounds.append(result)
            finally:
                # 无论成功、异常还是 Ctrl-C，都必须把容器拉起来 ——
                # 否则一次演练会把本机环境留在"少一个依赖"的状态。
                alive = restore(container)
                print(f"  恢复 {container}：{'健康' if alive else '未在 90s 内 healthy（需人工检查）'}")

    summary = summarize(rounds)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"fault_drill_{stamp}.json"
    md_path = out_dir / f"fault_drill_{stamp}.md"
    summary["environment"] = {
        "note": "单机 Docker Desktop；共享 vCPU —— 绝对秒数只报量级",
        "api": args.api,
        "prometheus": args.prometheus,
        "rounds_per_fault": args.rounds,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(to_markdown(summary, environment=summary["environment"]["note"]),
                       encoding="utf-8")
    print("\n" + to_markdown(summary))
    print(f"\n产物：{json_path} · {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

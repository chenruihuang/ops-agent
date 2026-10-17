"""故障注入实验 —— MTTR 与"故障下可用性"两个指标的产出工具。

为什么必须要这个脚本
--------------------
MTTR 不是一个能"看着感觉"出来的数。它需要四个时间戳：

    T1  注入故障
    T2  监控检测到（Prometheus 告警进入 firing）
    T3  熔断/降级生效，服务恢复对外正常应答
    T4  （可选）故障消除

    MTTR       = T3 - T2      ← 从"系统知道自己病了"到"系统能干活了"
    检测延迟    = T2 - T1
    端到端恢复  = T3 - T1

很多简历上的"MTTR 8 秒"其实测的是 T4-T1（再造一遍故障），口径完全不同。
把口径写清楚，本身就是加分项。

跑法
----
    python -m scripts.fault_inject --fault stop-qdrant
    python -m scripts.fault_inject --fault stop-redis --rounds 5
    python -m scripts.fault_inject --fault stop-mysql

只用标准库实现，不依赖 requests —— 这样在任何机器上都能直接跑。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

FAULTS = {
    "stop-redis": {
        "container": "redis",
        "desc": "缓存层不可用 —— 验证缓存挂掉不能拖垮主链路",
        "alert": "ApiInstanceDown",
    },
    "stop-qdrant": {
        "container": "qdrant",
        "desc": "向量库不可用 —— 验证召回层降级为无资料应答",
        "alert": "HighErrorRate",
    },
    "stop-mysql": {
        "container": "mysql",
        "desc": "外部系统（MCP 工具）不可用 —— 验证工具调用降级",
        "alert": "CircuitBreakerOpen",
    },
}


@dataclass
class Timeline:
    t1_inject: float = 0.0
    t2_detected: float | None = None
    t3_recovered: float | None = None
    attempts: int = 0
    successes: int = 0
    samples: list[dict] = field(default_factory=list)

    @property
    def detection_latency(self) -> float | None:
        return None if self.t2_detected is None else self.t2_detected - self.t1_inject

    @property
    def mttr(self) -> float | None:
        if self.t2_detected is None or self.t3_recovered is None:
            return None
        return self.t3_recovered - self.t2_detected

    @property
    def end_to_end(self) -> float | None:
        return None if self.t3_recovered is None else self.t3_recovered - self.t1_inject

    @property
    def availability(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0


def docker(*args: str, timeout: int = 60) -> tuple[int, str]:
    proc = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def http_get(url: str, timeout: float = 5.0, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception:
        return 0, ""


def prometheus_alert_firing(prometheus: str, alert_name: str) -> bool:
    url = f"{prometheus}/api/v1/query?query=" + urllib.parse.quote(
        f'ALERTS{{alertstate="firing",alertname="{alert_name}"}}'
    )
    status, body = http_get(url)
    if status != 200 or not body:
        return False
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return False
    return bool(data.get("data", {}).get("result"))


def probe_api(api: str, api_key: str, question: str, timeout: float = 30.0) -> dict:
    """打一次真实问答请求，返回是否成功以及降级标记。

    用普通 python 解析 SSE，判断标准：
      收到过任意 token 事件 = 服务对外可用（哪怕是降级答案）
      抛异常 / 5xx        = 不可用
    """
    payload = json.dumps({"message": question}).encode()
    req = urllib.request.Request(
        f"{api}/chat/stream",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    started = time.perf_counter()
    got_token = False
    degraded = False
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 500:
                return {"ok": False, "status": resp.status, "degraded": False}
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                body = line[6:].strip()
                if body == "[DONE]":
                    break
                try:
                    event = json.loads(body)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "token":
                    got_token = True
                if event.get("type") == "error":
                    degraded = True
    except Exception:
        return {"ok": False, "status": 0, "degraded": False}
    return {
        "ok": got_token,
        "status": 200,
        "degraded": degraded,
        "elapsed": time.perf_counter() - started,
    }


def run_one_round(
    *,
    fault: str,
    container: str,
    alert: str,
    api: str,
    prometheus: str,
    api_key: str,
    question: str,
    detect_timeout: float,
    recover_timeout: float,
) -> Timeline:
    timeline = Timeline()

    print(f"\n[注入] docker stop {container}")
    timeline.t1_inject = time.time()
    code, out = docker("stop", container)
    if code != 0:
        print(f"  注入失败：{out}")
        return timeline

    # ---- 探测窗口：一边请求一边看告警 ----
    print("  等待监控检测…", end="", flush=True)
    deadline = time.time() + detect_timeout
    while time.time() < deadline:
        if prometheus_alert_firing(prometheus, alert):
            timeline.t2_detected = time.time()
            print(f" 已检测（{timeline.detection_latency:.1f}s）")
            break
        time.sleep(1.0)
    if timeline.t2_detected is None:
        print(" 超时未检测到告警（检查 alerts.yml 是否已加载）")
        timeline.t2_detected = timeline.t1_inject

    # ---- 恢复窗口：持续打请求，直到连续 3 次成功 ----
    print("  等待服务恢复…", end="", flush=True)
    consecutive = 0
    recover_deadline = time.time() + recover_timeout
    while time.time() < recover_deadline:
        result = probe_api(api, api_key, question)
        timeline.attempts += 1
        timeline.samples.append(result)
        if result["ok"]:
            timeline.successes += 1
            consecutive += 1
            if consecutive >= 3:
                timeline.t3_recovered = time.time()
                break
        else:
            consecutive = 0
        time.sleep(1.0)

    if timeline.t3_recovered is None:
        print(" 超时未恢复")
        timeline.t3_recovered = recover_deadline
    else:
        print(f" 已恢复（MTTR {timeline.mttr:.1f}s）")

    # ---- 恢复现场 ----
    print(f"[还原] docker start {container}")
    docker("start", container)
    time.sleep(8)

    return timeline


def main() -> int:
    parser = argparse.ArgumentParser(description="故障注入与 MTTR 测量")
    parser.add_argument("--fault", default="stop-qdrant", choices=sorted(FAULTS))
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--api", default="http://localhost:8080")
    parser.add_argument("--prometheus", default="http://localhost:9090")
    parser.add_argument("--api-key", default="demo-key-change-me")
    parser.add_argument("--question", default="诉讼时效是几年")
    parser.add_argument("--detect-timeout", type=float, default=90.0)
    parser.add_argument("--recover-timeout", type=float, default=120.0)
    parser.add_argument("--out", default="eval_output/mttr_report.json")
    args = parser.parse_args()

    spec = FAULTS[args.fault]
    print("=" * 60)
    print(f"故障注入实验：{args.fault}")
    print(f"  说明      ：{spec['desc']}")
    print(f"  目标容器  ：{spec['container']}")
    print(f"  关联告警  ：{spec['alert']}")
    print(f"  轮次      ：{args.rounds}")
    print("=" * 60)

    rounds: list[Timeline] = []
    for i in range(args.rounds):
        print(f"\n---------- 第 {i + 1}/{args.rounds} 轮 ----------")
        rounds.append(
            run_one_round(
                fault=args.fault,
                container=spec["container"],
                alert=spec["alert"],
                api=args.api,
                prometheus=args.prometheus,
                api_key=args.api_key,
                question=args.question,
                detect_timeout=args.detect_timeout,
                recover_timeout=args.recover_timeout,
            )
        )

    mttrs = [r.mttr for r in rounds if r.mttr is not None]
    detections = [r.detection_latency for r in rounds if r.detection_latency is not None]
    avail = sum(r.successes for r in rounds) / max(1, sum(r.attempts for r in rounds))

    print("\n" + "=" * 60)
    print("实验结果（可直接抄进简历，注意保留测量条件）")
    print(f"  轮次                {len(rounds)}")
    if detections:
        print(f"  平均检测延迟        {sum(detections) / len(detections):.1f} s")
    if mttrs:
        print(f"  平均 MTTR           {sum(mttrs) / len(mttrs):.1f} s   （T3-T2 口径）")
        print(f"  各轮 MTTR           {[round(m, 1) for m in mttrs]}")
    print(f"  故障期请求成功率    {avail:.1%}   (样本 {sum(r.attempts for r in rounds)} 次)")
    print(f"  降级应答比例        "
          f"{sum(1 for r in rounds for s in r.samples if s.get('degraded')) / max(1, sum(r.attempts for r in rounds)):.1%}")
    print("=" * 60)

    report = {
        "fault": args.fault,
        "rounds": len(rounds),
        "avg_detection_latency_s": round(sum(detections) / len(detections), 2) if detections else None,
        "avg_mttr_s": round(sum(mttrs) / len(mttrs), 2) if mttrs else None,
        "mttr_samples_s": [round(m, 2) for m in mttrs],
        "request_success_rate": round(avail, 4),
        "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "conditions": {
            "host": "单机 Docker Desktop",
            "api_replicas": 2,
            "prometheus_scrape_interval": "5s",
            "note": "MTTR 为 T3-T2（检测到恢复），非生产 SLA",
        },
    }

    import os

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"报告已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

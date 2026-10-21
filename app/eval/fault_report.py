"""故障演练的结果聚合与报告（纯函数，不碰 docker）。

为什么要独立一层
----------------
`scripts/fault_inject.py` 负责"制造故障 + 采时间戳"，它必须碰 docker（有副作用、不可单测）。
但**"这些时间戳怎么汇总成 MTTR / 可用性"**是纯算术，必须能离线单测 ——
否则一个 median 写错，报告里的数字会稳稳地错，而且没人能发现。

口径（写死在代码里，报告里也印一遍）
----------------------------------
    T1 注入    T2 监控检测到（Prometheus 告警 firing）
    T3 服务恢复（连续 3 次收到 token）    T4 故障消除

    MTTR      = T3 - T2      ← 从"系统知道自己病了"到"能干活"
    检测延迟  = T2 - T1
    端到端    = T3 - T1

**MTTR 不是 T4 - T1。** 很多简历上的"MTTR 8 秒"其实测的是"再造一遍故障要多久"，
两者差一个检测延迟加一个恢复时间，口径完全不同。

单轮只有一个数，**多轮才有分布**：一次 30 秒的恢复可能只是运气。
5 轮报中位数 + 范围，比报一个平均数诚实得多（异常值会直接污染均值）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


@dataclass(slots=True)
class RoundResult:
    """一轮演练的结果。所有时间单位都是秒。"""

    fault: str
    round: int
    availability: float = 0.0
    detection_s: float | None = None
    mttr_s: float | None = None
    end_to_end_s: float | None = None
    attempts: int = 0
    successes: int = 0
    # 故障窗口内的请求耗时中位数。为什么要记它：
    # 缓存挂掉时请求**照样成功**（可用性 100%），但 TTFT 会从 2s 涨到几十秒。
    # 只看可用性会得出"没影响"的错误结论。
    probe_ms_median: float | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "fault": self.fault,
            "round": self.round,
            "availability": round(self.availability, 4),
            "detection_s": _round_or_none(self.detection_s),
            "mttr_s": _round_or_none(self.mttr_s),
            "end_to_end_s": _round_or_none(self.end_to_end_s),
            "attempts": self.attempts,
            "successes": self.successes,
            "probe_ms_median": _round_or_none(self.probe_ms_median),
            "note": self.note,
        }


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def median(values: Sequence[float]) -> float | None:
    """中位数。偶数个取中间两个的均值（**不用**最近秩：这里是统计量，不是分位阈值）。"""
    items = sorted(v for v in values if v is not None)
    if not items:
        return None
    mid = len(items) // 2
    if len(items) % 2:
        return items[mid]
    return (items[mid - 1] + items[mid]) / 2


def summarize(rounds: Iterable[RoundResult]) -> dict[str, Any]:
    """按故障类型汇总，再给一个总表。

    `availability` 的口径是**本轮探测请求的成功率**（收到 token 即成功），
    不是 SLA。探测失败包括超时/5xx/断流 —— 也就是用户会真实感受到的那些。
    """
    rows = list(rounds)
    by_fault: dict[str, list[RoundResult]] = {}
    for r in rows:
        by_fault.setdefault(r.fault, []).append(r)

    def block(items: list[RoundResult]) -> dict[str, Any]:
        avails = [i.availability for i in items if i.attempts > 0]
        mttrs = [i.mttr_s for i in items if i.mttr_s is not None]
        detects = [i.detection_s for i in items if i.detection_s is not None]
        e2e = [i.end_to_end_s for i in items if i.end_to_end_s is not None]
        return {
            "rounds": len(items),
            "rounds_with_samples": len(avails),
            "availability_min": round(min(avails), 4) if avails else None,
            "availability_median": round(median(avails), 4) if avails else None,
            "detection_s_median": _round_or_none(median(detects)),
            "mttr_s_median": _round_or_none(median(mttrs)),
            "mttr_s_range": (
                [_round_or_none(min(mttrs)), _round_or_none(max(mttrs))] if mttrs else None
            ),
            "end_to_end_s_median": _round_or_none(median(e2e)),
            "probe_ms_median": _round_or_none(median(
                [i.probe_ms_median for i in items if i.probe_ms_median is not None]
            )),
        }

    summary = {
        "total_rounds": len(rows),
        "faults": {name: block(items) for name, items in sorted(by_fault.items())},
        "overall": block(rows),
        "definition": {
            "mttr": "T3 - T2（监控检测到 → 服务恢复）",
            "detection": "T2 - T1（注入 → 检测到）",
            "end_to_end": "T3 - T1",
            "availability": "本轮探测请求成功率（收到 token 即成功）",
            "not_sla": "这是故障注入下的请求成功率，不是 SLA / 可用性承诺",
        },
        "rounds": [r.as_dict() for r in rows],
    }
    return summary


def to_markdown(summary: Mapping[str, Any], *, environment: str = "") -> str:
    """渲染成可直接贴进复盘文档的表格。

    环境那一行不是装饰：共享 vCPU 上的绝对延迟会漂（同一条 matmul 标尺
    一小时内实测差 7.5 倍），不写机器与并发条件，这些秒数就没法被别人引用。
    """
    lines: list[str] = [
        "# 故障演练报告",
        "",
        f"- 轮次：{summary.get('total_rounds', 0)}",
        f"- MTTR 口径：{summary['definition']['mttr']}",
        f"- 可用性口径：{summary['definition']['availability']}（{summary['definition']['not_sla']}）",
    ]
    if environment:
        lines.append(f"- 环境：{environment}")
    lines += [
        "",
        "| 故障 | 轮次 | 可用性(中位) | 最低 | 检测延迟(中位) | MTTR(中位) | MTTR 范围 | 端到端(中位) | 请求耗时(中位) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, block in summary.get("faults", {}).items():
        rng = block.get("mttr_s_range")
        rng_text = "—" if not rng else f"{rng[0]}~{rng[1]}s"
        lines.append(
            f"| {name} | {block['rounds']} | {_fmt(block['availability_median'], pct=True)} "
            f"| {_fmt(block['availability_min'], pct=True)} "
            f"| {_fmt(block['detection_s_median'], 's')} "
            f"| {_fmt(block['mttr_s_median'], 's')} | {rng_text} "
            f"| {_fmt(block['end_to_end_s_median'], 's')} "
            f"| {_fmt((block.get('probe_ms_median') or 0) / 1000 if block.get('probe_ms_median') else None, 's')} |"
        )
    overall = summary.get("overall", {})
    lines += [
        "",
        f"整体可用性中位：{_fmt(overall.get('availability_median'), pct=True)} · "
        f"MTTR 中位：{_fmt(overall.get('mttr_s_median'), 's')}",
        "",
        "> 单轮数字不可引用；中位数与范围才是结论。异常值（如某轮恢复超时）必须保留，不能剔除。",
    ]
    return "\n".join(lines)


def _fmt(value: Any, unit: str = "", pct: bool = False) -> str:
    if value is None:
        return "—"
    if pct:
        return f"{value * 100:.1f}%"
    if unit == "s":
        return f"{value:.1f}s"
    return str(value)


def from_timelines(timelines: Iterable[Any], fault: str) -> list[RoundResult]:
    """把 fault_inject.Timeline 转成本模块的 RoundResult（鸭子类型，不 import 它）。"""
    out: list[RoundResult] = []
    for i, t in enumerate(timelines, start=1):
        out.append(RoundResult(
            fault=fault,
            round=i,
            availability=getattr(t, "availability", 0.0),
            detection_s=getattr(t, "detection_latency", None),
            mttr_s=getattr(t, "mttr", None),
            end_to_end_s=getattr(t, "end_to_end", None),
            attempts=getattr(t, "attempts", 0),
            successes=getattr(t, "successes", 0),
        ))
    return out


@dataclass(slots=True)
class DrillPlan:
    """演练计划：先打印出来，让人看清楚要停哪些容器。"""

    faults: list[str] = field(default_factory=list)
    rounds: int = 5
    api: str = "http://localhost:8080"
    prometheus: str = "http://localhost:9090"

    def describe(self) -> str:
        return (
            f"计划：{len(self.faults)} 类故障 × {self.rounds} 轮 = "
            f"{len(self.faults) * self.rounds} 轮；"
            f"api={self.api} prometheus={self.prometheus}；"
            "每轮结束会 docker start 恢复并等健康检查通过"
        )

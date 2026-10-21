"""故障演练报告的口径测试（纯算术，不碰 docker）。

真正危险的错不是"演练失败"，而是**演练成功但汇总错了**：
MTTR 用错时间差、均值被一轮异常值带偏、可用性把"没采到样本"算成 0。
这些错都会让报告看起来很专业，且完全不可复现。
"""

from __future__ import annotations

from app.eval.fault_report import (
    RoundResult,
    from_timelines,
    median,
    summarize,
    to_markdown,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}{'  ' + detail if detail else ''}")


def test_median() -> None:
    print("中位数：偶数取中间均值，空集合是 None 不是 0")
    check("奇数", median([3, 1, 2]) == 2)
    check("偶数取均值", median([1, 2, 3, 4]) == 2.5)
    check("单值", median([7]) == 7)
    check("空集合 → None", median([]) is None)
    check("忽略 None", median([1, 3, None]) == 2)
    check("不剔除异常值（[1,2,100] → 2）", median([1, 2, 100]) == 2)


def test_summary() -> None:
    print("汇总：可用性分母与 MTTR 口径")
    rounds = [
        # 正常恢复：可用 100%，MTTR 20s
        RoundResult("stop-redis", 1, availability=1.0, detection_s=5.0, mttr_s=20.0,
                    end_to_end_s=25.0, attempts=10, successes=10),
        # 抖动：可用 80%，MTTR 30s
        RoundResult("stop-redis", 2, availability=0.8, detection_s=7.0, mttr_s=30.0,
                    end_to_end_s=37.0, attempts=10, successes=8),
        # 检测失败：detection=0（工具会回填 T1），MTTR None —— 不能当 0
        RoundResult("stop-redis", 3, availability=0.9, detection_s=0.0, mttr_s=None,
                    end_to_end_s=12.0, attempts=10, successes=9),
        # 完全没采到样本（例如 API 一直不可达）：不进可用性均值
        RoundResult("stop-qdrant", 1, availability=0.0, detection_s=4.0, mttr_s=10.0,
                    end_to_end_s=14.0, attempts=0, successes=0),
    ]
    summary = summarize(rounds)
    redis = summary["faults"]["stop-redis"]
    check("轮次统计", redis["rounds"] == 3, str(redis["rounds"]))
    check("可用性中位 = 0.9（0.8/0.9/1.0）", redis["availability_median"] == 0.9,
          str(redis["availability_median"]))
    check("可用性最低 = 0.8", redis["availability_min"] == 0.8, str(redis["availability_min"]))
    check("MTTR 中位只用有值的两轮 = 25.0", redis["mttr_s_median"] == 25.0,
          str(redis["mttr_s_median"]))
    check("MTTR 范围保留异常值 20~30", redis["mttr_s_range"] == [20.0, 30.0],
          str(redis["mttr_s_range"]))
    check("检测延迟中位 = 5.0（0/5/7）", redis["detection_s_median"] == 5.0,
          str(redis["detection_s_median"]))

    qdrant = summary["faults"]["stop-qdrant"]
    check("无样本的轮次不参与可用性（None 而不是 0）",
          qdrant["availability_median"] is None, str(qdrant["availability_median"]))
    check("无样本仍保留 MTTR", qdrant["mttr_s_median"] == 10.0, str(qdrant["mttr_s_median"]))
    check("rounds_with_samples 如实报告", qdrant["rounds_with_samples"] == 0,
          str(qdrant["rounds_with_samples"]))

    check("口径写进产物（MTTR=T3-T2）",
          "T3 - T2" in summary["definition"]["mttr"], summary["definition"]["mttr"])
    check("明确声明不是 SLA", "不是 SLA" in summary["definition"]["not_sla"],
          summary["definition"]["not_sla"])


def test_markdown() -> None:
    print("报告渲染：缺值用 — 表示，不写 0")
    summary = summarize([
        RoundResult("stop-redis", 1, availability=1.0, detection_s=5.0, mttr_s=20.0,
                    end_to_end_s=25.0, attempts=10, successes=10),
        RoundResult("stop-redis", 2, availability=0.8, detection_s=7.0, mttr_s=30.0,
                    end_to_end_s=37.0, attempts=10, successes=8),
    ])
    md = to_markdown(summary, environment="单机 / 共享 vCPU")
    check("有表头", "| 故障 |" in md)
    check("有环境说明", "单机 / 共享 vCPU" in md)
    check("可用性按百分比渲染", "90.0%" in md, md)
    check("MTTR 范围渲染", "20.0~30.0s" in md, md)
    check("口径提醒保留异常值", "异常值" in md)

    empty_md = to_markdown(summarize([RoundResult("x", 1, availability=0.0, attempts=0)]))
    check("缺值渲染成 — 而不是 0", "—" in empty_md, empty_md)


def test_from_timelines() -> None:
    print("Timeline → RoundResult：鸭子类型转换，缺字段不炸")
    class FakeTimeline:
        availability = 0.75
        detection_latency = 3.0
        mttr = 12.0
        end_to_end = 15.0
        attempts = 4
        successes = 3

    class Minimal:
        pass

    rows = from_timelines([FakeTimeline(), Minimal()], "stop-redis")
    check("轮次从 1 开始", [r.round for r in rows] == [1, 2], str([r.round for r in rows]))
    check("字段透传", rows[0].availability == 0.75 and rows[0].mttr_s == 12.0)
    check("缺字段的 Timeline 退化为 0 / None", rows[1].attempts == 0 and rows[1].mttr_s is None,
          str(rows[1].as_dict()))


def main() -> int:
    print("=" * 70)
    print("故障演练报告测试")
    print("=" * 70)
    for fn in (test_median, test_summary, test_markdown, test_from_timelines):
        fn()
    print(f"\n{PASS} 条通过 · {FAIL} 条失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

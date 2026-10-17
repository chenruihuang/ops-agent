"""意图路由测试。

分两层，**这两层的性质完全不同，不能混着看**：

1. `ADVERSARIAL` —— 我手写的边界用例，**刻意不用标注集里的措辞**。
   它检验的是泛化：规则到底是学到了"实时查询的语义特征"，
   还是只是把 52 句话背下来了。

2. 标注集回归 —— 用 `eval/questions.json` 跑混淆矩阵。
   68% 的题是 rag（45/52 里 45 条），所以**总准确率会被 rag 的占比掩盖**：
   全判 rag 就有 86.5%。因此这里同时卡两个下限，
   其中"非 rag 召回"才是路由能力的真实读数。

为什么第 2 层要设阈值而不是要求 100%
-------------------------------------
现在的 100% 是**在这批数据上调出来的** —— 等于训练集准确率，必然乐观。
把它写成 `>= 1.0` 会制造一种虚假的严格感，掩盖"没有留出集"这个事实。
所以阈值设在合理水平，同时用第 1 层（真正未见过的句子）来兜住泛化风险。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pipeline.intent import (  # noqa: E402
    decide,
    evaluate_routing,
    extract_entities,
    intent_stats,
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
        print(f"  [FAIL] {name}  {detail}")


# 刻意与标注集措辞不同。每条后面注明它守的是什么。
ADVERSARIAL: list[tuple[str, str]] = [
    # ---- tool：实时数据 ----
    ("单号 INC-2026-0389 现在什么情况？", "tool"),
    ("SRV-0077 这台机器在哪、还活着吗？", "tool"),
    ("order-service 现在有没有未关闭的告警", "tool"),
    ("把 notify-service 的工单都列一下", "tool"),
    # ---- hybrid：两路都要 ----
    ("ES red 的判断标准是什么？那次 ES 宕机的工单最后咋收的？", "hybrid"),
    ("支付回调积压超过多少条会告警？相关的历史工单也给我看看", "hybrid"),
    # ---- rag：以下每条都是一个曾经踩过的坑 ----
    ("限流阈值是怎么定的？", "rag"),
    # 「慢查询」里含「查询」—— 裸动词表会把它误判成实时查询，
    # 所以 LIVE_VERBS 现在是"动词 + 对象"组合。
    ("慢查询怎么优化？", "rag"),
    # 「告警」在这里是被讨论的概念，不是要取的数据。
    ("P1 告警的响应时限要求是多少？", "rag"),
    ("缓存穿透和雪崩的区别是什么？", "rag"),
    ("ORD-429-001 是什么意思？", "rag"),
    ("网关的连接数上限是多少？", "rag"),
    ("集群的读写分离是怎么做的？", "rag"),
    # ---- chitchat ----
    ("在吗", "chitchat"),
    ("谢谢你！", "chitchat"),
    ("你能做什么", "chitchat"),
    ("你好，你是谁", "chitchat"),          # 多分句：整串正则在这里会漏
    ("嗯嗯，好的", "chitchat"),
    # ---- 边界：实体优先于寒暄 ----
    ("你好，工单 INC-2026-0412 处理到哪了", "tool"),
]


def test_entities() -> None:
    print("实体抽取")
    e = extract_entities("工单 INC-2026-0412 报 ORD-429-001，机器 SRV-0088 上的 order-service 挂了")
    check("工单号", e.ticket_nos == ["INC-2026-0412"], str(e.ticket_nos))
    check("资产编码", e.asset_codes == ["SRV-0088"], str(e.asset_codes))
    check("错误码", e.error_codes == ["ORD-429-001"], str(e.error_codes))
    check("服务名", e.services == ["order-service"], str(e.services))

    # 大小写归一化：否则 "INC-2026-0412" 与 "inc-2026-0412" 会变成两个实体
    e2 = extract_entities("看下 srV-0088 和 inc-2026-0412")
    check("资产编码大小写归一", e2.asset_codes == ["SRV-0088"], str(e2.asset_codes))
    check("工单号大小写归一", e2.ticket_nos == ["INC-2026-0412"], str(e2.ticket_nos))

    # 不该被抽出来的东西
    e3 = extract_entities("Redis 的 max_connections 是多少")
    check("普通技术名词不算实体", not e3.any, str(e3.as_dict()))


def test_adversarial() -> None:
    print("对抗样例（未参与规则调参）")
    wrong = [(q, want, decide(q).intent) for q, want in ADVERSARIAL if decide(q).intent != want]
    for q, want, got in wrong:
        check(f"{want:<8} ← {q[:38]}", False, f"实际 {got}")
    if not wrong:
        check(f"全部 {len(ADVERSARIAL)} 条措辞不同的句子路由正确", True)


def test_reason_exposed() -> None:
    """决策必须可解释 —— 这是规则路由相对 LLM 分类的核心优势，
    不能只是内部有个字段而没被用起来。"""
    print("可解释性")
    for q, want in ADVERSARIAL:
        d = decide(q)
        if d.intent != want:
            continue
        if not d.reason or not d.signals:
            check(f"决策含 reason/signals ← {q[:30]}", False, f"{d.reason!r} {d.signals}")
            return
    check("每条决策都带 reason 与 signals", True)

    ev = decide("查一下工单 INC-2026-0412 的处理进度").as_event()
    check("事件含 intent", ev["intent"] == "tool", str(ev))
    check("事件含实体", ev["entities"]["ticket_nos"] == ["INC-2026-0412"], str(ev["entities"]))
    check("事件含工具需求标记", ev["needs_tool"] is True and ev["needs_rag"] is False, str(ev))

    # chitchat 必须两路都不需要 —— 演示时"闲聊不触发检索与工具"是可见能力
    d = decide("你好，你是谁")
    check("闲聊不检索不调工具", d.needs_rag is False and d.needs_tool is False, str(d))


def test_annotation_regression() -> None:
    print("标注集回归（注意：规则在这批数据上调过，存在乐观偏差）")
    path = Path("eval/questions.json")
    if not path.exists():
        check("标注集存在", False, f"{path} 不存在")
        return

    qs = json.loads(path.read_text(encoding="utf-8"))
    stats = intent_stats(qs)
    print(f"    意图分布：{stats}")

    overall = evaluate_routing(qs)
    nonrag = evaluate_routing(qs, include=("tool", "hybrid", "chitchat"))

    print(f"    总准确率 {overall.accuracy:.4f}（{overall.correct}/{overall.total}）"
          f" · 非 rag 召回 {nonrag.accuracy:.4f}（{nonrag.correct}/{nonrag.total}）")
    for qid, question, exp, act in overall.errors:
        print(f"      误判 q-{qid}: 期望 {exp} 实际 {act} | {question[:44]}")

    # 阈值取 0.94 / 0.85：留出余量，避免"改一条措辞就红"的脆弱断言。
    # 不写 1.0 —— 见模块 docstring 关于乐观偏差的说明。
    check("总准确率 ≥ 0.94", overall.accuracy >= 0.94, f"{overall.accuracy:.4f}")
    check("非 rag 召回 ≥ 0.85", nonrag.accuracy >= 0.85, f"{nonrag.accuracy:.4f}")
    check("rag 题零误判为 tool/hybrid", all(
        v == 0 for k, v in overall.confusion.get("rag", {}).items() if k in ("tool", "hybrid")
    ), str(overall.confusion.get("rag")))


def main() -> int:
    print("=" * 70)
    print("意图路由测试")
    print("=" * 70)
    test_entities()
    test_adversarial()
    test_reason_exposed()
    test_annotation_regression()
    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

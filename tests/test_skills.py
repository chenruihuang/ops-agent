"""Skill 注册表测试：声明解析、匹配、横编排与横向降级。

这里最想钉住的一条是**YAML 与代码内置定义不能漂移**：
两处维护同一份 skill，靠自觉一定会分叉，而分叉的表现是"改了配置没生效"——
比报错难查得多。所以有一条用例直接把两边结构逐字段比对。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools.base import ToolRegistry, ToolResult, ToolSpec
from app.tools.skills import (
    DEFAULT_SKILLS,
    SkillRegistry,
    SkillSpec,
    SkillStep,
    build_skill_registry,
    entities_of,
    execute_skill,
    load_skills,
)

PASS = 0
FAIL = 0
MOCK_PATH = Path("eval/mock_records.json")


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def _registry_with_mock(records: dict | None = None) -> ToolRegistry:
    from app.tools.providers import TOOL_SPECS, MockProvider

    mock = MockProvider(records=records) if records is not None else MockProvider(path=MOCK_PATH)
    reg = ToolRegistry()
    for spec in TOOL_SPECS:
        reg.register(spec, getattr(mock, spec.name))
    reg.source = "mock"
    return reg


class _Decision:
    """最小替身：只提供 entities（skill 只需要这个）。"""

    def __init__(self, ticket_nos=(), asset_codes=(), services=()) -> None:
        self.entities = type("E", (), {
            "ticket_nos": list(ticket_nos),
            "asset_codes": list(asset_codes),
            "services": list(services),
        })()


# =====================================================================
def test_yaml_matches_default() -> None:
    print("声明一致性：skills.yaml 与内置定义不漂移")
    specs = load_skills()
    left = [s.as_dict() for s in specs]
    right = [s.as_dict() for s in DEFAULT_SKILLS]
    check("数量一致", len(left) == len(right), f"{len(left)} vs {len(right)}")
    check("结构逐字段一致（含步骤 when/args）", left == right, f"\n{left}\n{right}")

    names = [s.name for s in specs]
    check("含 ticket_diagnosis", "ticket_diagnosis" in names, str(names))
    check("含 service_health", "service_health" in names, str(names))


def test_bad_yaml_falls_back(tmp_path: Path) -> None:
    print("容错：配置坏了不能拖垮工具层")
    bad = tmp_path / "skills.yaml"
    bad.write_text("skills:\n  - name: broken\n    steps: []\n", encoding="utf-8")
    specs = load_skills(bad)
    check("空步骤的 skill 被拒绝 → 退回内置", specs == list(DEFAULT_SKILLS),
          str([s.name for s in specs]))

    bad.write_text("这不是 yaml: [unclosed", encoding="utf-8")
    specs2 = load_skills(bad)
    check("坏 YAML → 退回内置", specs2 == list(DEFAULT_SKILLS))

    missing = load_skills(tmp_path / "nope.yaml")
    check("文件不存在 → 退回内置", missing == list(DEFAULT_SKILLS))


def test_registry_match() -> None:
    print("匹配：按实体命中数与声明顺序，确定性地挑一个")
    reg = build_skill_registry()
    check("只有工单号 → ticket_diagnosis",
          reg.match({"ticket_no": "INC-2026-0412"}).name == "ticket_diagnosis")
    check("只有服务名 → service_health",
          reg.match({"service": "order-service"}).name == "service_health")
    check("工单号 + 服务名 → 仍选 ticket_diagnosis（requires 更具体）",
          reg.match({"ticket_no": "INC-2026-0412", "service": "order-service"}).name
          == "ticket_diagnosis")
    check("什么都没有 → 不匹配", reg.match({}) is None)
    check("空实体不算命中（空串不能当有值）", reg.match({"ticket_no": ""}) is None)

    reg_empty = SkillRegistry()
    check("空注册表返回 None", reg_empty.match({"ticket_no": "x"}) is None)

    try:
        reg_empty.register(SkillSpec(name="bad", description="", requires=(), steps=()))
        check("无步骤的 skill 应被拒绝", False)
    except ValueError:
        check("无步骤的 skill 被拒绝", True)


def test_entities_of() -> None:
    print("实体展开：把路由决策压成模板可用的字典")
    d = _Decision(ticket_nos=["INC-2026-0412", "INC-2026-0389"], services=["order-service"])
    ents = entities_of(d, "另外把数据库连接相关的历史工单也给我看一下")
    check("取第一个工单号", ents.get("ticket_no") == "INC-2026-0412", str(ents))
    check("取第一个服务名", ents.get("service") == "order-service", str(ents))
    check("关键词也被抽出（供兜底步骤用）", bool(ents.get("keyword")), str(ents))

    empty = entities_of(_Decision(), "")
    check("无实体时为空字典", empty == {}, str(empty))


def test_execute_vertical_chain() -> None:
    print("编排：单号查不到 → 跳过缺参步骤 → 关键词兜底")
    reg = _registry_with_mock()
    skill = next(s for s in DEFAULT_SKILLS if s.name == "ticket_diagnosis")

    async def run() -> None:
        ctx = {"ticket_no": "INC-2026-9999", "keyword": "数据库 连接"}
        result = await execute_skill(skill, reg, ctx)
        tools = [r.tool for r in result.results]
        check("第 1 步按单号查", tools[0] == "query_ticket_by_no", str(tools))
        check("单号查不到（data 空但 ok）", result.results[0].ok and not result.results[0].data,
              str(result.results[0].as_event()))
        check("第 2 步因缺 service 被跳过",
              {"tool": "query_ticket_by_service", "reason": "missing_arg"} in result.skipped,
              str(result.skipped))
        check("第 3 步关键词兜底真的执行了",
              any(r.tool == "query_ticket_by_keyword" for r in result.results), str(tools))
        check("兜底有结果（说明降级路径有效）", result.ok_any)
        check("stop_reason=done", result.stop_reason == "done", result.stop_reason)

    asyncio.run(run())


def test_execute_skips_when_not_empty() -> None:
    print("编排：单号查到了就不再走兜底（省调用、省预算）")
    reg = _registry_with_mock()
    skill = next(s for s in DEFAULT_SKILLS if s.name == "ticket_diagnosis")

    async def run() -> None:
        ctx = {"ticket_no": "INC-2026-0412", "service": "order-service",
               "keyword": "订单 OOM"}
        result = await execute_skill(skill, reg, ctx)
        check("只执行了第 1 步", [r.tool for r in result.results] == ["query_ticket_by_no"],
              str([r.tool for r in result.results]))
        check("后续步骤标记 previous_not_empty",
              all(s["reason"] == "previous_not_empty" for s in result.skipped),
              str(result.skipped))

    asyncio.run(run())


def test_execute_budget() -> None:
    print("预算：超时带部分结果返回，而不是拖死整轮")
    reg = _registry_with_mock()
    # 预算 0 → 第一步之前就超预算
    skill = SkillSpec(
        name="tiny_budget", description="", requires=("ticket_no",),
        steps=(SkillStep("query_ticket_by_no", {"ticket_no": "{ticket_no}"}),),
        budget_ms=0,
    )

    async def run() -> None:
        result = await execute_skill(skill, reg, {"ticket_no": "INC-2026-0412"})
        check("预算耗尽停止", result.stop_reason == "budget_exhausted", result.stop_reason)
        check("没有发出调用", result.results == [], str(result.results))
        check("记录跳过原因",
              result.skipped and result.skipped[0]["reason"] == "budget_exhausted",
              str(result.skipped))

    asyncio.run(run())


def test_execute_callbacks() -> None:
    print("回调：先 on_before 再 on_step（顺序决定屏幕上的观感）")
    reg = _registry_with_mock()
    skill = next(s for s in DEFAULT_SKILLS if s.name == "service_health")
    events: list[str] = []

    async def run() -> None:
        result = await execute_skill(
            skill, reg, {"service": "order-service"},
            on_before=lambda step, args: events.append(f"before:{step.tool}"),
            on_step=lambda step, r, run: events.append(f"after:{r.tool}:{r.ok}"),
        )
        check("两步都执行", len(result.results) == 2, str(result.results))
        check("回调顺序 before→after 交替",
              events == ["before:query_alerts_by_service", "after:query_alerts_by_service:True",
                         "before:query_ticket_by_service", "after:query_ticket_by_service:True"],
              str(events))

        # 回调抛异常不能影响执行
        events.clear()

        def boom(_step, _args):
            raise RuntimeError("callback crash")

        result2 = await execute_skill(
            skill, reg, {"service": "order-service"},
            on_before=boom,
            on_step=lambda step, r, run: (_ for _ in ()).throw(RuntimeError("x")),
        )
        check("回调炸了不影响执行", len(result2.results) == 2, str(result2.results))

    asyncio.run(run())


def test_empty_result_semantics() -> None:
    print("语义：工具失败与工具查空，是两件事")
    reg = _registry_with_mock()
    skill = next(s for s in DEFAULT_SKILLS if s.name == "service_health")

    async def run() -> None:
        # inventory-service 有工单但无未关闭告警 → 告警结果 ok 且 data 空
        result = await execute_skill(skill, reg, {"service": "inventory-service"})
        alerts = result.results[0]
        check("告警工具 ok=True", alerts.ok is True, str(alerts.as_event()))
        check("告警 data 为空（0 条 firing）", not alerts.data, str(alerts.data))
        check("文本明确说 0 条（不是空串）", "0 条" in alerts.text, alerts.text[:60])

    asyncio.run(run())


def main() -> int:
    import tempfile

    print("=" * 70)
    print("Skill 注册表测试")
    print("=" * 70)
    test_yaml_matches_default()
    with tempfile.TemporaryDirectory() as tmp:
        test_bad_yaml_falls_back(Path(tmp))
    test_registry_match()
    test_entities_of()
    test_execute_vertical_chain()
    test_execute_skips_when_not_empty()
    test_execute_budget()
    test_execute_callbacks()
    test_empty_result_semantics()
    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

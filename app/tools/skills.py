"""Skill 注册表：把「一组工具 + 失败替代路径 + 时间预算」声明成一个业务能力。

Skill 与 Tool 的区别（这是本文件存在的理由）
------------------------------------------
    Tool  = 一次 IO 调用（查工单、查告警）
    Skill = 一个业务意图怎么被满足（"定位这张工单"要查单号 → 查不到就按关键词捞）

两者都有降级，但**方向不同，别混为一谈**：

    纵向降级（Tool 层，`resilience.run_chain`）：同一个工具的多个数据源
        mcp → cache → mock → 失败
        —— 解决"这个数据源不健康"

    横向降级（Skill 层，本文件的 fallback）：同一个意图的多条查询路径
        by_no 查不到 → by_keyword 捞 → by_service 兜底
        —— 解决"这个参数没命中，但意图还能用别的方式满足"

只做纵向会怎样：MCP 一挂，全线退到 mock，答案里全是假数据；
只做横向会怎样：换个查询方式重试一个已经宕了的依赖，纯属浪费。

执行语义
--------
1. **串行**：后一步的触发条件依赖前一步的结果（`previous_empty` / `previous_failed`）。
   工具级并行是 `node_tools` 的事，不能在这里偷偷并发 —— 那会让"前一步没查到"
   这个判断本身失去意义。
2. **预算**：`budget_ms` 是**整条 skill 的总时长上限**。每一步开始前检查，
   超了就带着已有结果返回并标 `budget_exhausted`。宁可给部分答案，
   也不能让"查工单"这件事把整轮问答拖到 10s。
3. **参数模板**：`{ticket_no}` / `{service}` / `{keyword}` 从路由阶段抽出的实体填；
   填不满就跳过该步并记 `skip:missing_arg` —— 不构造空参数去查（见 planner 的注释）。

为什么用 YAML 声明
------------------
Skill 是**配置**，不是代码：运维想加一条"查工单前先查资产"的顺序，不该改 Python。
YAML 与代码里的 `DEFAULT_SKILLS` 是"文件优先、代码兜底"的关系：
文件坏了不能导致工具层起不来（见 `load_skills` 的异常处理）。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.tools.base import ToolRegistry, ToolResult
from app.tools.planner import extract_keywords

log = logging.getLogger(__name__)

DEFAULT_SKILLS_PATH = Path("app/tools/skills.yaml")

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@dataclass(frozen=True, slots=True)
class SkillStep:
    """一步 = 一次工具调用。

    `when` 语义：
        always           —— 总是执行（第一步必须用它，否则没东西可判断）
        previous_empty   —— 上一步成功但数据为空（"没查到"才换路径）
        previous_failed  —— 上一步失败（ok=False）
    """

    tool: str
    args: Mapping[str, str] = field(default_factory=dict)
    when: str = "always"
    label: str = ""

    def title(self) -> str:
        return self.label or self.tool


@dataclass(frozen=True, slots=True)
class SkillSpec:
    name: str
    description: str
    requires: tuple[str, ...]
    steps: tuple[SkillStep, ...]
    budget_ms: int = 3000
    tags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "requires": list(self.requires),
            "budget_ms": self.budget_ms,
            "steps": [{"tool": s.tool, "when": s.when, "label": s.title()} for s in self.steps],
        }


@dataclass(slots=True)
class SkillRun:
    """一次 skill 执行的结果。`stop_reason` 是**可解释性字段**：
    "为什么只查了一部分"必须能从结果里读出来，而不是靠猜。"""

    skill: str
    results: list[ToolResult] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: float = 0.0
    stop_reason: str = "done"          # done | budget_exhausted | no_entity | error

    @property
    def ok_any(self) -> bool:
        return any(r.ok and r.data for r in self.results)

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "steps": len(self.results),
            "skipped": self.skipped,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "stop_reason": self.stop_reason,
            "results": [
                {"tool": r.tool, "ok": r.ok, "source": r.source,
                 "chars": len(r.text or "")}
                for r in self.results
            ],
        }


# =====================================================================
# 注册表
# =====================================================================


class SkillRegistry:
    """声明式能力目录。

    匹配规则是**确定性**的：按 `requires` 的命中数降序、同分按注册顺序。
    不做模糊打分 —— "这次为什么走了 ticket_diagnosis 而不是 service_health"
    在线上必须能一句话回答。
    """

    def __init__(self) -> None:
        self._skills: dict[str, SkillSpec] = {}
        self._order: list[str] = []

    def register(self, spec: SkillSpec) -> None:
        if not spec.steps:
            raise ValueError(f"skill {spec.name} 没有任何步骤")
        if spec.name not in self._skills:
            self._order.append(spec.name)
        self._skills[spec.name] = spec

    def get(self, name: str) -> SkillSpec | None:
        return self._skills.get(name)

    def names(self) -> list[str]:
        return list(self._order)

    def specs(self) -> list[SkillSpec]:
        return [self._skills[n] for n in self._order]

    def __len__(self) -> int:
        return len(self._skills)

    def match(self, entities: Mapping[str, Any]) -> SkillSpec | None:
        """按实体可用性挑最合适的 skill。

        `entities` 形如 `{"ticket_no": "INC-2026-0412", "service": "order-service"}`，
        由路由阶段的 `RouteDecision.entities` 展开而来（见 `entities_of`）。
        命中越多优先级越高：同时有单号和服务时，"定位工单"比"看服务健康"更具体。
        """
        best: SkillSpec | None = None
        best_score = 0
        for name in self._order:
            spec = self._skills[name]
            if not spec.requires:
                continue
            if not all(entities.get(k) for k in spec.requires):
                continue
            score = len(spec.requires)
            if score > best_score:
                best, best_score = spec, score
        return best

    def describe(self) -> str:
        """给 LLM / `/ready` 看的能力清单。"""
        lines = []
        for spec in self.specs():
            req = "、".join(spec.requires) or "无"
            lines.append(f"- {spec.name}（需要 {req}）：{spec.description}")
        return "\n".join(lines)

    def snapshot(self) -> list[dict[str, Any]]:
        return [s.as_dict() for s in self.specs()]


def entities_of(decision: Any, message: str = "") -> dict[str, Any]:
    """把路由决策里的实体压成"一层字典"，供 skill 模板取值。

    多个单号/服务时只取第一个：skill 是"一个意图一次执行"，
    批量查询是 planner 的职责（它会为每个实体发一次调用）。
    """
    ents = getattr(decision, "entities", None)
    out: dict[str, Any] = {}
    if ents is not None:
        nos = list(getattr(ents, "ticket_nos", []) or [])
        codes = list(getattr(ents, "asset_codes", []) or [])
        svcs = list(getattr(ents, "services", []) or [])
        if nos:
            out["ticket_no"] = nos[0]
        if codes:
            out["asset_code"] = codes[0]
        if svcs:
            out["service"] = svcs[0]
    if message:
        kws = extract_keywords(message)
        if kws:
            out["keyword"] = " ".join(kws)
    return out


def _render_args(template: Mapping[str, str], ctx: Mapping[str, Any]) -> dict[str, Any] | None:
    """填模板。任一占位符填不满就返回 None（→ 跳过该步）。

    不填空串：`query_ticket_by_no("")` 在 mock 里会命中"未提供工单号"，
    在真实系统里可能返回全表 —— 两种都是比"跳过"更糟的行为。
    """
    filled: dict[str, Any] = {}
    for key, raw in template.items():
        if isinstance(raw, str):
            missing = [p for p in _PLACEHOLDER.findall(raw) if not ctx.get(p)]
            if missing:
                return None
            filled[key] = _PLACEHOLDER.sub(lambda m: str(ctx[m.group(1)]), raw)
        else:
            filled[key] = raw
    return filled


async def execute_skill(
    spec: SkillSpec,
    registry: ToolRegistry,
    ctx: Mapping[str, Any],
    *,
    on_before: Any = None,
    on_step: Any = None,
) -> SkillRun:
    """按步骤执行 skill。

    `on_before(step)` 在调用前触发（用于先推 `tool_call` 帧，让屏幕先出现
    "正在查工单"）；`on_step(step, result, run)` 在执行后触发（推结果）。
    两个回调的异常都被吞掉 —— 埋点不能影响业务（与 `Tracer._emit` 同一原则）。
    """
    run = SkillRun(skill=spec.name)
    started = time.perf_counter()
    previous: ToolResult | None = None

    for step in spec.steps:
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms >= spec.budget_ms:
            run.stop_reason = "budget_exhausted"
            run.skipped.append({"tool": step.tool, "reason": "budget_exhausted"})
            break

        if step.when == "previous_empty":
            if previous is None or not previous.ok or previous.data:
                run.skipped.append({"tool": step.tool, "reason": "previous_not_empty"})
                continue
        elif step.when == "previous_failed":
            if previous is None or previous.ok:
                run.skipped.append({"tool": step.tool, "reason": "previous_ok"})
                continue

        args = _render_args(step.args, ctx)
        if args is None:
            run.skipped.append({"tool": step.tool, "reason": "missing_arg"})
            continue

        if on_before is not None:
            try:
                on_before(step, args)
            except Exception:  # noqa: BLE001
                pass

        # registry.call 永不抛（契约见 tools/base），且内部已有超时 + 纵向降级链
        result = await registry.call(step.tool, **args)
        run.results.append(result)
        previous = result

        if on_step is not None:
            try:
                on_step(step, result, run)
            except Exception:  # noqa: BLE001 - 埋点失败不影响执行
                pass

    run.elapsed_ms = (time.perf_counter() - started) * 1000
    if run.stop_reason == "done" and not run.results:
        run.stop_reason = "no_entity"
    return run


# =====================================================================
# 加载
# =====================================================================

# 代码内兜底定义：YAML 缺失/损坏时用它，保证工具层永远起得来。
# 内容与 skills.yaml 保持一致 —— 单测会断言两者不漂移。
DEFAULT_SKILLS: tuple[SkillSpec, ...] = (
    SkillSpec(
        name="ticket_diagnosis",
        description="定位一张工单的进展：先按单号精确查，查不到就按关键词捞同类工单",
        requires=("ticket_no",),
        budget_ms=2500,
        tags=("ticket",),
        steps=(
            SkillStep("query_ticket_by_no", {"ticket_no": "{ticket_no}"}, "always", "按单号查"),
            SkillStep("query_ticket_by_service", {"service": "{service}"},
                      "previous_empty", "同服务工单"),
            SkillStep("query_ticket_by_keyword", {"keyword": "{keyword}"},
                      "previous_empty", "关键词兜底"),
        ),
    ),
    SkillSpec(
        name="service_health",
        description="看一个服务现在健不健康：当前未关闭告警 + 相关工单",
        requires=("service",),
        budget_ms=2000,
        tags=("health", "alert"),
        steps=(
            SkillStep("query_alerts_by_service", {"service": "{service}"}, "always", "当前告警"),
            SkillStep("query_ticket_by_service", {"service": "{service}"}, "always", "相关工单"),
        ),
    ),
)


def _parse_skill(raw: Mapping[str, Any]) -> SkillSpec:
    steps: list[SkillStep] = []
    for item in raw.get("steps") or []:
        if not isinstance(item, Mapping) or not item.get("tool"):
            raise ValueError(f"skill {raw.get('name')} 的步骤缺少 tool 字段")
        steps.append(
            SkillStep(
                tool=str(item["tool"]),
                args={str(k): str(v) for k, v in (item.get("args") or {}).items()},
                when=str(item.get("when") or "always"),
                label=str(item.get("label") or ""),
            )
        )
    return SkillSpec(
        name=str(raw["name"]),
        description=str(raw.get("description") or ""),
        requires=tuple(str(x) for x in (raw.get("requires") or ())),
        steps=tuple(steps),
        budget_ms=int(raw.get("budget_ms") or 3000),
        tags=tuple(str(x) for x in (raw.get("tags") or ())),
    )


def load_skills(path: Path | str | None = None) -> list[SkillSpec]:
    """从 YAML 读 skill 定义。**任何异常都退回 DEFAULT_SKILLS**。

    理由和工具层一致：配置文件写错是运维常态，它不该让整个 API 起不来。
    但必须 WARNING 出声 —— 静默退回默认值会让你以为新配置生效了。
    """
    target = Path(path or DEFAULT_SKILLS_PATH)
    if not target.exists():
        log.info("未找到 %s，使用内置 skill 定义（%d 个）", target, len(DEFAULT_SKILLS))
        return list(DEFAULT_SKILLS)

    try:
        import yaml  # 延迟导入：离线脚本/单测不装 pyyaml 也能用内置定义

        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        items = raw.get("skills") if isinstance(raw, Mapping) else None
        if not isinstance(items, list) or not items:
            raise ValueError("skills.yaml 里没有 skills 列表")
        specs = [_parse_skill(item) for item in items if isinstance(item, Mapping)]
        if not specs:
            raise ValueError("skills.yaml 解析出 0 个 skill")
        # 空步骤的 skill 没有业务含义，还会在注册期炸出来 —— 在加载层就整体拒绝，
        # 走统一的「退回内置」路径，而不是让半套配置生效（部分生效比全不生效更难排查）。
        broken = [s.name for s in specs if not s.steps]
        if broken:
            raise ValueError(f"skill 缺少任何步骤: {broken}")
        return specs
    except Exception as exc:  # noqa: BLE001
        log.warning("skills.yaml 解析失败（%s），退回内置定义 —— 检查缩进与字段名", exc)
        return list(DEFAULT_SKILLS)


def build_skill_registry(
    path: Path | str | None = None,
    specs: Iterable[SkillSpec] | None = None,
) -> SkillRegistry:
    reg = SkillRegistry()
    for spec in (specs if specs is not None else load_skills(path)):
        reg.register(spec)
    return reg

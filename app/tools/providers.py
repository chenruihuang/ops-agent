"""Mock provider —— 离线可跑的工具实现，同时是 MCP 的职业替身。

它存在的三层意义
----------------
1. **离线可测**：没有 Java MCP Server 也能把 tool / hybrid 两条链路跑到端到端，
   否则工具相关的一切都要等 D2 才能验证，主链路会被卡住。
2. **契约占位**：它和 MCP provider 的工具签名**完全一致**（同名、同参数、同返回结构），
   所以 D2 接真实 MCP 时上层代码一行都不用改。
3. **降级兜底**：MCP 挂了就用它 —— 演示时"把 MCP 停掉，服务仍然回答"这个场景
   靠的就是这里。`ToolResult.source` 字段如实标注数据来自 mock 还是 mcp，
   不把 mock 数据当实时数据讲。

一个刻意的实现选择：关键词检索复用 BM25
--------------------------------------
`query_ticket_by_keyword` 没有另写一套模糊匹配，而是直接把工单拼成小语料
喂给 `app.ingest.textindex.BM25Index`。

理由：同一份打分实现意味着**同一套已知缺陷和同一套测试**。
新写一个"看起来差不多"的匹配函数，等于新增一处没人测过的排序逻辑，
而它出错时表现为"工单搜不到"，极难归因。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Sequence

from app.ingest.textindex import BM25Index, build_bm25
from app.tools.base import ToolRegistry, ToolResult, ToolSpec

log = logging.getLogger(__name__)

DEFAULT_MOCK_PATH = Path("eval/mock_records.json")

# =====================================================================
# 工具签名表 —— **唯一来源**
# =====================================================================
# mock provider 与 MCP provider 都注册这一份，所以"两边签名一致"是结构保证的，
# 不靠人工同步。D2 写 Java MCP Server 时，照着这份表实现即可 ——
# 反过来说，如果 Java 侧签名不一致，`ToolRegistry.call` 会因为参数不匹配
# 返回 ok=False 并打 warning，而不是静默传错参数。
#
# 覆盖 D2 计划暴露的 5 个工具（与 eval/gen_eval.py 的 KNOWN_TOOLS 对齐）。
TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "query_ticket_by_no",
        "按工单号精确查询工单的处理进度、责任人与结论",
        {"ticket_no": "工单号，形如 INC-2026-0412"},
        ("ticket_no",),
    ),
    ToolSpec(
        "query_ticket_by_keyword",
        "按关键词检索历史工单（支持现象、服务名、组件名）",
        {"keyword": "检索关键词", "top_k": "返回条数，默认 3"},
        ("keyword",),
    ),
    ToolSpec(
        "query_ticket_by_service",
        "查询某个服务名下的全部工单",
        {"service": "服务名，形如 order-service"},
        ("service",),
    ),
    ToolSpec(
        "get_asset_by_code",
        "按资产编码查询服务器的主机名、IP、状态与承载服务",
        {"asset_code": "资产编码，形如 SRV-0088"},
        ("asset_code",),
    ),
    ToolSpec(
        "query_alerts_by_service",
        "查询某个服务当前未关闭（firing）的告警；无告警时明确返回 0 条",
        {"service": "服务名，形如 order-service"},
        ("service",),
    ),
)

# 工单关键词检索时，参与打分的字段及其权重（重复出现的字段 = 提高权重）。
# title 重复 3 次：工单标题是人工写的摘要，信息密度最高。
_TICKET_FIELD_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("title", 3),
    ("summary", 2),
    ("service", 2),
    ("owner", 1),
)


# =====================================================================
# 写工具 —— 与只读工具**分开声明**
# =====================================================================
# 为什么不并进 TOOL_SPECS：
#   1. TOOL_SPECS 是「Java MCP 必须实现的清单」，`scripts/mcp_probe.py` 会断言
#      远端 tools/list 与它一一对应。写操作不走 MCP（远端没有、也不该有），
#      并进去会让探针立刻变红。
#   2. 只读工具的降级链是 cache → mcp → mock；写工具**没有降级** ——
#      "重启失败就换个数据源重启一次"是事故，不是韧性。
#
# `simulated=True` 是这套演示的诚实性基线：这些工具**不会真的动线上**，
# 返回文本里必须写清楚，事件流里也能看出来。演示时把它讲成"真重启了"，
# 面试官只要问一句"重启的 API 是哪个"就穿帮。
WRITE_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "restart_service",
        "重启指定服务的全部实例（写操作，需人工确认；当前为模拟执行）",
        {
            "service": "服务名，形如 order-service",
            "reason": "变更原因，会写进审计日志",
        },
        ("service",),
        risk="dangerous",
        simulated=True,
    ),
    ToolSpec(
        "scale_service",
        "调整指定服务的副本数（写操作，需人工确认；当前为模拟执行）",
        {
            "service": "服务名，形如 order-service",
            "replicas": "目标副本数，1~10",
        },
        ("service", "replicas"),
        risk="write",
        simulated=True,
    ),
)

ALL_TOOL_SPECS: tuple[ToolSpec, ...] = TOOL_SPECS + WRITE_TOOL_SPECS


def _load_records(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        log.warning("mock 数据不存在：%s —— 工具将返回空结果", p)
        return {"tickets": [], "assets": [], "alerts": []}
    data = json.loads(p.read_text(encoding="utf-8"))
    for key in ("tickets", "assets", "alerts"):
        data.setdefault(key, [])
    return data


# =====================================================================
# 文本化：工具结果 → 给 LLM 读的中文
# =====================================================================
# 这一步不是美化。工具返回的是 JSON，直接把 JSON 丢给模型有两个问题：
#   1. 模型会更倾向于"复述字段名"而不是组织成答案
#   2. 关键值（状态、责任方、IP）在 JSON 里不突出，容易被漏答
# 所以这里按"人看工单会先看什么"的顺序排版，并把关键字段用中文标签标出来。
def render_ticket(t: dict[str, Any]) -> str:
    lines = [f"工单 {t.get('ticket_no', '?')} ｜ {t.get('title', '')}"]
    meta = [
        f"服务：{t['service']}" if t.get("service") else "",
        f"状态：{t['status']}" if t.get("status") else "",
        f"责任方：{t['owner']}" if t.get("owner") else "",
    ]
    lines.append("  " + " ｜ ".join(m for m in meta if m))
    if t.get("summary"):
        lines.append(f"  详情：{t['summary']}")
    if t.get("related_runbook"):
        lines.append(f"  关联处置手册：{t['related_runbook']}")
    return "\n".join(lines)


def render_asset(a: dict[str, Any]) -> str:
    lines = [f"资产 {a.get('asset_code', '?')} ｜ 主机名 {a.get('hostname', '')}"]
    meta = [
        f"IP：{a['ip']}" if a.get("ip") else "",
        f"状态：{a['status']}" if a.get("status") else "",
        f"位置：{a['room']}" if a.get("room") else "",
    ]
    lines.append("  " + " ｜ ".join(m for m in meta if m))
    services = a.get("services") or []
    if services:
        lines.append(f"  承载服务：{'、'.join(services)}")
    return "\n".join(lines)


def render_alerts(service: str, alerts: Sequence[dict[str, Any]]) -> str:
    """告警文本化。

    "查不到"必须被显式表达成「0 条 / 当前没有未关闭告警」——
    而不是返回空字符串。空字符串会让模型不知道是"没查到"还是"本来就没有"，
    于是拿知识库内容硬答，这正是工具题最常见的失败方式。
    """
    if not alerts:
        return (
            f"服务 {service} 的未关闭告警：0 条\n"
            f"  （{service} 当前没有未关闭的告警）"
        )
    lines = [f"服务 {service} 的未关闭告警：{len(alerts)} 条"]
    for i, a in enumerate(alerts, start=1):
        when = a.get("started_at", "")
        lines.append(
            f"  {i}. [{a.get('severity', '-')}] {a.get('alert_name', '-')}"
            f" ｜ {when} 起 ｜ {a.get('summary', '')}"
        )
    return "\n".join(lines)


def render_tickets(items: Sequence[dict[str, Any]], *, label: str) -> str:
    if not items:
        return f"{label}：未找到匹配的工单"
    blocks = [f"{label}：{len(items)} 条"] + [render_ticket(t) for t in items]
    return "\n".join(blocks)


# =====================================================================
# Provider
# =====================================================================
class MockProvider:
    """把 mock 数据注册成一组工具。"""

    name = "mock"

    def __init__(self, records: dict[str, Any] | None = None, *, path: Path | str = DEFAULT_MOCK_PATH):
        self.records = records if records is not None else _load_records(path)
        self._bm25: BM25Index | None = None
        self._bm25_ids: list[str] = []

    # ---------------- 工单关键词索引 ----------------
    def _ticket_index(self) -> BM25Index:
        if self._bm25 is None:
            ids: list[str] = []
            texts: list[str] = []
            for t in self.records.get("tickets", []):
                no = str(t.get("ticket_no") or "")
                if not no:
                    continue
                parts: list[str] = []
                for field_name, weight in _TICKET_FIELD_WEIGHTS:
                    value = t.get(field_name)
                    if isinstance(value, str) and value:
                        parts.extend([value] * weight)
                ids.append(no)
                texts.append(" ".join(parts))
            self._bm25 = build_bm25(ids, texts)
            self._bm25_ids = ids
        return self._bm25

    def _ticket_by_no(self, no: str) -> dict[str, Any] | None:
        for t in self.records.get("tickets", []):
            if str(t.get("ticket_no", "")).upper() == no.upper():
                return t
        return None

    # ---------------- 工具实现 ----------------
    async def query_ticket_by_no(self, ticket_no: str = "") -> ToolResult:
        if not ticket_no:
            return ToolResult("query_ticket_by_no", False, source=self.name,
                              error="缺少参数 ticket_no", text="[未提供工单号]")
        t = self._ticket_by_no(ticket_no)
        if t is None:
            return ToolResult("query_ticket_by_no", True, {"ticket_no": ticket_no},
                              data=[], source=self.name,
                              text=f"工单 {ticket_no}：未找到（请确认单号是否正确）")
        return ToolResult("query_ticket_by_no", True, {"ticket_no": ticket_no},
                          data=[t], source=self.name, text=render_ticket(t))

    async def query_ticket_by_keyword(self, keyword: str = "", top_k: int = 3) -> ToolResult:
        if not keyword.strip():
            return ToolResult("query_ticket_by_keyword", False, source=self.name,
                              error="缺少参数 keyword", text="[未提供关键词]")
        index = self._ticket_index()
        hits = index.search(keyword, top_k=top_k)
        by_no = {str(t.get("ticket_no")): t for t in self.records.get("tickets", [])}
        items = [by_no[cid] for cid, _ in hits if cid in by_no]
        return ToolResult(
            "query_ticket_by_keyword", True, {"keyword": keyword},
            data=items, source=self.name,
            text=render_tickets(items, label=f"按关键词「{keyword}」检索工单"),
        )

    async def query_ticket_by_service(self, service: str = "") -> ToolResult:
        if not service:
            return ToolResult("query_ticket_by_service", False, source=self.name,
                              error="缺少参数 service", text="[未提供服务名]")
        items = [
            t for t in self.records.get("tickets", [])
            if str(t.get("service", "")).lower() == service.lower()
        ]
        return ToolResult(
            "query_ticket_by_service", True, {"service": service},
            data=items, source=self.name,
            text=render_tickets(items, label=f"服务 {service} 的工单"),
        )

    async def get_asset_by_code(self, asset_code: str = "") -> ToolResult:
        if not asset_code:
            return ToolResult("get_asset_by_code", False, source=self.name,
                              error="缺少参数 asset_code", text="[未提供资产编码]")
        for a in self.records.get("assets", []):
            if str(a.get("asset_code", "")).upper() == asset_code.upper():
                return ToolResult("get_asset_by_code", True, {"asset_code": asset_code},
                                  data=[a], source=self.name, text=render_asset(a))
        return ToolResult("get_asset_by_code", True, {"asset_code": asset_code},
                          data=[], source=self.name,
                          text=f"资产 {asset_code}：未找到（请确认资产编码是否正确）")

    async def query_alerts_by_service(self, service: str = "") -> ToolResult:
        if not service:
            return ToolResult("query_alerts_by_service", False, source=self.name,
                              error="缺少参数 service", text="[未提供服务名]")
        # 只回未关闭的：firing。resolved 的告警对"现在有没有问题"没有意义，
        # 混进去会让模型把已恢复的故障说成正在发生（时间性错误最难被发现）。
        firing = [
            a for a in self.records.get("alerts", [])
            if str(a.get("service", "")).lower() == service.lower()
            and str(a.get("status", "")).lower() == "firing"
        ]
        return ToolResult(
            "query_alerts_by_service", True, {"service": service},
            data=firing, source=self.name,
            text=render_alerts(service, firing),
        )

    # ---------------- 写工具（模拟执行）----------------
    # 这两个方法的**前置闸门不在这里**：能不能进到这个函数，由
    # app/tools/sandbox.py 在执行前判定（策略 + 人工确认）。
    # 也就是说 —— 它们被调用时，一定已经有人按过"确认"。
    # 职责分开的好处：写工具的"业务动作"与"安全边界"可以各自单测。
    async def restart_service(self, service: str = "", reason: str = "") -> ToolResult:
        if not service:
            return ToolResult(tool="restart_service", ok=False, source=self.name,
                              error="缺少参数 service", text="[未提供服务名]")
        return ToolResult(
            tool="restart_service", ok=True,
            args={"service": service, "reason": reason},
            data=[{
                "service": service, "action": "restart", "instances": 3,
                "strategy": "rolling", "simulated": True,
            }],
            source=self.name,
            text=(
                f"[模拟执行 · 未真正调用编排接口]\n"
                f"服务 {service} 的重启动作已放行（滚动重启 3 个实例）\n"
                f"  变更原因：{reason or '（未填写）'}\n"
                f"  影响窗口：约 40s，期间约 1/3 请求失败\n"
                f"  回滚方式：kubectl rollout undo deployment/{service}"
            ),
        )

    async def scale_service(self, service: str = "", replicas: Any = 0) -> ToolResult:
        if not service:
            return ToolResult(tool="scale_service", ok=False, source=self.name,
                              error="缺少参数 service", text="[未提供服务名]")
        try:
            target = int(replicas)
        except (TypeError, ValueError):
            return ToolResult(tool="scale_service", ok=False, source=self.name,
                              error=f"副本数不是整数：{replicas!r}",
                              text="[副本数参数非法]")
        # 契约用逗号而不是 em dash：这两个数是"之前/之后"，读的时候不该被破折号
        # 糊成一个区间
        return ToolResult(
            tool="scale_service", ok=True,
            args={"service": service, "replicas": target},
            data=[{
                "service": service, "action": "scale", "from": 3, "to": target,
                "simulated": True,
            }],
            source=self.name,
            text=(
                f"[模拟执行 · 未真正调用编排接口]\n"
                f"服务 {service} 副本数 3 → {target}（模拟）\n"
                f"  影响：扩容会新增实例并接入负载；缩容会先摘流量再终止\n"
                f"  成本变化：按副本数线性变化，请在变更单里注明"
            ),
        )

    # ---------------- 统一入口 ----------------
    async def call(self, name: str, **args: Any) -> ToolResult:
        """按工具名分派。存在的意义是让 mock 和 MCP 有**同一个调用形状**，
        这样降级逻辑（见 app/tools/__init__.py）不需要区分 provider。
        """
        handler = getattr(self, name, None)
        if handler is None:
            return ToolResult(name, False, source=self.name,
                              error=f"mock 未实现 {name}", text=f"[mock 未实现 {name}]")
        return await handler(**args)

    # ---------------- 注册 ----------------
    def install(self, registry: ToolRegistry) -> None:
        """按 ALL_TOOL_SPECS 注册本 provider 的实现。

        用 `getattr` 而不是把 spec 和 handler 成对写死：如果哪天加了 spec
        却忘了实现，会在启动时直接 `AttributeError` 报出来，
        而不是等到线上调用时才返回 500。
        """
        for spec in ALL_TOOL_SPECS:
            handler = getattr(self, spec.name, None)
            if handler is None:
                raise AttributeError(
                    f"MockProvider 缺少 {spec.name} 的实现，"
                    f"但 TOOL_SPECS 里声明了它 —— 两边必须同步"
                )
            registry.register(spec, handler)

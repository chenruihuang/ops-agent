"""工具层测试。

覆盖三类**必须成立**的行为，它们都是"出错时不会报错、只会静默变差"的：

1. **永不抛异常**：任何失败（未注册 / 超时 / 参数错 / 下游炸）都收敛成 ok=False。
   工具层是辅助信息源，不该有能力把整个问答打断成 500。

2. **降级与来源标注**：MCP 不可用必须自动转 mock，且 `source` 字段如实反映
   数据来自哪里。演示时把 mock 数据当实时数据讲，是这类项目最容易翻车的地方，
   所以这条要用测试锁死。

3. **"无数据"必须被显式表达**：`query_alerts_by_service` 在没有未关闭告警时，
   要返回"0 条 / 当前没有未关闭的告警"，而不是空字符串。
   空字符串会让模型分不清"没查到"和"本来就没有"，于是拿知识库内容硬答 ——
   这正是工具类题最常见的失败方式（标注 q-36 考的就是这个）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools import MockProvider, ToolRegistry, build_registry, plan, render_tool_context
from app.tools.base import ToolResult, ToolSpec
from app.tools.mcp_client import McpProvider
from app.tools.planner import extract_keywords
from app.pipeline.intent import decide

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


def _run(coro):
    return asyncio.run(coro)


# =====================================================================
def test_registry_never_raises() -> None:
    print("注册表：永不抛异常")
    reg = ToolRegistry(timeout=0.2)

    r = _run(reg.call("not_registered"))
    check("未注册 → ok=False 不抛", r.ok is False and "未注册" in r.error, str(r))

    async def slow(**kwargs):
        await asyncio.sleep(1.0)
        return ToolResult("slow", True, text="too late")

    reg.register(ToolSpec("slow", "故意很慢"), slow)
    r = _run(reg.call("slow"))
    check("超时 → ok=False 不抛", r.ok is False and "超时" in r.error, str(r))

    async def needs_arg(required_arg: str):
        return ToolResult("needs_arg", True, text="ok")

    reg.register(ToolSpec("needs_arg", "需要参数", {"required_arg": "x"}, ("required_arg",)), needs_arg)
    r = _run(reg.call("needs_arg"))          # 故意不传参
    check("参数错误 → ok=False 不抛", r.ok is False and "参数错误" in r.error, str(r))

    async def boom(**kwargs):
        raise RuntimeError("下游 500")

    reg.register(ToolSpec("boom", "会炸"), boom)
    r = _run(reg.call("boom"))
    check("下游异常 → ok=False 不抛", r.ok is False and "RuntimeError" in r.error, str(r))

    # 失败结果也必须能渲染进上下文 —— 否则模型不知道"这条查不到"
    ctx = render_tool_context([r])
    check("失败结果写进上下文并说明原因", "调用失败" in ctx and "RuntimeError" in ctx, ctx)


def test_mock_provider() -> None:
    print("mock provider：五个工具")
    provider = MockProvider(path=MOCK_PATH)

    r = _run(provider.query_ticket_by_no("INC-2026-0412"))
    check("按工单号命中", r.ok and r.data and r.data[0]["status"] == "处理中", str(r.data))
    check("工单文本含责任方", "@trade-oncall" in r.text, r.text[:80])
    check("来源标注为 mock", r.source == "mock", r.source)

    r = _run(provider.query_ticket_by_no("INC-9999-0000"))
    check("工单号不存在 → 明确说未找到", r.ok and r.data == [] and "未找到" in r.text, r.text)

    r = _run(provider.get_asset_by_code("SRV-0088"))
    check("按资产编码命中", r.ok and r.data[0]["hostname"] == "app-prod-01", str(r.data))
    check("资产文本含 IP 与状态", "10.0.1.11" in r.text and "online" in r.text, r.text)

    r = _run(provider.query_ticket_by_service("order-service"))
    check("按服务查工单", r.ok and len(r.data) == 2, f"{len(r.data)} 条")

    # ---- 关键词检索 ----
    r = _run(provider.query_ticket_by_keyword("数据库连接"))
    ids = [t["ticket_no"] for t in (r.data or [])]
    check("关键词「数据库连接」命中慢查询工单", "INC-2026-0415" in ids, str(ids))

    r = _run(provider.query_ticket_by_keyword("用户重复收到短信"))
    ids = [t["ticket_no"] for t in (r.data or [])]
    check("关键词命中重复短信工单", "INC-2026-0420" in ids, str(ids))

    r = _run(provider.query_ticket_by_keyword("完全不存在的东西zzz"))
    check("关键词无命中 → 明确说未找到", r.ok and "未找到" in r.text, r.text)


def test_alert_filtering() -> None:
    print("告警：只回未关闭的（q-36 的关键）")
    provider = MockProvider(path=MOCK_PATH)

    r = _run(provider.query_alerts_by_service("order-service"))
    check("有 firing → 返回 1 条", r.ok and len(r.data) == 1, str(r.data))
    check("文本写明条数", "1 条" in r.text, r.text)

    # inventory-service 在数据里只有一条 resolved。
    # 这题考的就是"查不到必须说无，不能拿已解决的充数"。
    r = _run(provider.query_alerts_by_service("inventory-service"))
    check("只有 resolved → 返回 0 条", r.ok and r.data == [], str(r.data))
    check("0 条必须显式表达（不是空串）", "0 条" in r.text and "没有未关闭" in r.text, r.text)

    # resolved 的告警对"现在有没有问题"没有意义，混进去会让模型
    # 把已恢复的故障说成正在发生。
    for a in provider.records["alerts"]:
        if a["service"] == "search-service":
            check("search-service 的 resolved 未被当成未关闭", a["status"] == "resolved", str(a))


def test_planner() -> None:
    print("工具规划")
    cases = [
        ("帮我查一下工单 INC-2026-0412 的处理进度。", ["query_ticket_by_no"]),
        ("资产编码 SRV-0088 这台机器现在是什么状态？", ["get_asset_by_code"]),
        ("查一下库存服务（inventory-service）今天有没有未关闭的告警。",
         ["query_alerts_by_service", "query_ticket_by_service"]),
    ]
    for q, expect_subset in cases:
        calls = plan(decide(q), q)
        names = [c.name for c in calls]
        check(f"计划含 {expect_subset}", all(n in names for n in expect_subset), str(names))

    # rag / chitchat 不该产生任何工具调用
    for q, want in [("限流阈值是怎么定的？", "rag"), ("你好，你是谁", "chitchat")]:
        check(f"{want} 不调工具", plan(decide(q), q) == [], str(plan(decide(q), q)))

    # 无实体时用关键词兜底，否则实时路等于空转
    calls = plan(decide("把数据库连接相关的历史工单也给我看一下"), "把数据库连接相关的历史工单也给我看一下")
    check("无实体 → 关键词兜底", [c.name for c in calls] == ["query_ticket_by_keyword"], str(calls))
    check("兜底关键词抽对了", "数据库连接" in str(calls[0].args), str(calls[0].args))

    # 参数抽不出来就不发 —— 空参数查出来的无关数据比查不到更糟
    check("空问题不产生调用", plan(decide(""), "") == [], "空问题产生了调用")


def test_keyword_extraction() -> None:
    print("关键词抽取")
    cases = [
        ("另外把数据库连接相关的历史工单也给我看一下", "数据库连接"),
        ("用户重复收到短信的投诉工单处理到哪一步了", "用户重复收到短信"),
        ("之前那次 ES red 的事故工单最后是怎么解决的", "es"),
    ]
    for q, expect in cases:
        kws = extract_keywords(q)
        check(f"「{q[:16]}…」→ 含 {expect!r}", any(expect in k for k in kws), str(kws))

    check("空输入安全", extract_keywords("") == [], "空输入异常")
    check("纯标点安全", extract_keywords("？？？") == [], "纯标点异常")


def test_cache_layer() -> None:
    print("工具缓存层")
    calls: list[str] = []

    class FakeCache:
        def __init__(self):
            self.store: dict = {}

        async def get_json(self, namespace, *parts):
            calls.append("get")
            return self.store.get((namespace, parts))

        async def set_json(self, namespace, value, *, ttl, parts):
            calls.append("set")
            self.store[(namespace, parts)] = value

    cache = FakeCache()
    reg = build_registry(mock_path=MOCK_PATH, cache=cache)

    r1 = _run(reg.call("query_ticket_by_no", ticket_no="INC-2026-0412"))
    check("首次调用走真实 provider", r1.source == "mock" and r1.ok, r1.source)

    r2 = _run(reg.call("query_ticket_by_no", ticket_no="INC-2026-0412"))
    check("二次调用命中缓存", r2.source == "cache", r2.source)
    check("缓存命中内容一致", r2.text == r1.text, "内容不一致")

    # 参数顺序不同必须命中同一条缓存 —— 否则命中率凭空减半
    r3 = _run(reg.call("query_ticket_by_keyword", top_k=3, keyword="数据库连接"))
    r4 = _run(reg.call("query_ticket_by_keyword", keyword="数据库连接", top_k=3))
    check("参数顺序无关（缓存 key 稳定）", r4.source == "cache", r4.source)

    # 失败结果不缓存：否则一次抖动会被固化，故障恢复后仍返回"查不到"
    before = list(calls)
    _run(reg.call("get_asset_by_code", asset_code="SRV-9999"))
    _run(reg.call("get_asset_by_code", asset_code="SRV-9999"))
    check("不存在的资产仍会查两次（未被缓存）", calls.count("set") - before.count("set") == 1,
          f"{calls.count('set') - before.count('set')}")


def test_mcp_fallback() -> None:
    print("MCP 降级与来源标注")

    class DeadMcp:
        """永远不可用的 MCP。"""

        name = "mcp"

        def __init__(self):
            self.url = "http://java-mcp:8080"
            self._probe_ok = None

        async def available(self):
            return False

        async def call(self, tool, **args):  # pragma: no cover - 不该被调到
            raise AssertionError("不可用的 MCP 不该被调用")

    reg = build_registry(mock_path=MOCK_PATH, mcp_provider=DeadMcp())
    r = _run(reg.call("query_ticket_by_no", ticket_no="INC-2026-0412"))
    check("MCP 不可用 → 自动降级 mock", r.ok and r.source == "mock", f"ok={r.ok} source={r.source}")

    class FakeMcp:
        name = "mcp"

        def __init__(self):
            self.url = "http://fake-mcp"
            self._probe_ok = True
            self.received: list[tuple] = []

        async def available(self):
            return True

        async def call(self, tool, **args):
            self.received.append((tool, args))
            return ToolResult(tool, True, args, data=[{"ticket_no": args.get("ticket_no")}],
                              text="来自 MCP 的工单", source="mcp")

    fake = FakeMcp()
    reg = build_registry(mock_path=MOCK_PATH, mcp_provider=fake)
    r = _run(reg.call("query_ticket_by_no", ticket_no="INC-2026-0412"))
    check("MCP 可用 → 走 MCP", r.ok and r.source == "mcp", r.source)
    check("参数原样透传", fake.received[0][1] == {"ticket_no": "INC-2026-0412"}, str(fake.received))

    # MCP 返回失败 → 降级 mock（而不是把失败直接吐给上层）
    class FailingMcp(FakeMcp):
        async def call(self, tool, **args):
            return ToolResult(tool, False, args, source="mcp", error="远端 500", text="错误")

    reg = build_registry(mock_path=MOCK_PATH, mcp_provider=FailingMcp())
    r = _run(reg.call("query_ticket_by_no", ticket_no="INC-2026-0412"))
    check("MCP 返回错误 → 降级 mock", r.ok and r.source == "mock", f"ok={r.ok} source={r.source}")


def test_mock_data_integrity() -> None:
    print("mock 数据自洽性")
    data = json.loads(MOCK_PATH.read_text(encoding="utf-8"))
    check("含 alerts 段", "alerts" in data, str(list(data.keys())))
    check("alerts 有 firing 与 resolved 两种状态",
          {a["status"] for a in data["alerts"]} == {"firing", "resolved"},
          str({a["status"] for a in data["alerts"]}))
    # 标注 q-36 期望 inventory-service 无未关闭告警，数据必须与之自洽
    inv = [a for a in data["alerts"]
           if a["service"] == "inventory-service" and a["status"] == "firing"]
    check("inventory-service 无 firing（与 q-36 标注自洽）", inv == [], str(inv))


def main() -> int:
    print("=" * 70)
    print("工具层测试")
    print("=" * 70)
    test_registry_never_raises()
    test_mock_provider()
    test_alert_filtering()
    test_planner()
    test_keyword_extraction()
    test_cache_layer()
    test_mcp_fallback()
    test_mock_data_integrity()
    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

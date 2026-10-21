"""安全沙箱测试。

测的不是"确认功能能跑通"，而是**四条边界是否真的成立** ——
每一条被绕过的后果都是"线上被改了一个没人批过的状态"，而它在测试里
和正常路径长得一模一样（都返回 ok=True）。所以这里断言的是结构性质：

1. 策略先于执行：非法参数连挂起都不给（拿不到 token）。
2. 确认绑定参数：带着 token 传另一套参数去调用，执行的仍是**快照里那套**。
3. 一次性：同一个 token 第二次必然失效。
4. 留痕：pending / approved / denied / rejected 都落审计。

另外钉住 fail-closed：沙箱关掉、工具没声明策略、token 属于别的工具，
一律拒绝 —— 反过来（fail-open）会让"Redis 挂了"变成"绕过审批的后门"。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.chat import ConfirmRequest, confirm_action          # noqa: E402
from app.core.config import settings                              # noqa: E402
from app.pipeline.intent import decide                            # noqa: E402
from app.tools import build_registry, plan, render_tool_context   # noqa: E402
from app.tools.base import ToolResult                             # noqa: E402
from app.tools.sandbox import (                                   # noqa: E402
    ActionStore,
    AuditLog,
    Sandbox,
    call_context,
    validate_call,
)
from tests.fake_redis import FakeRedis                            # noqa: E402
# 复用 SSE 用例里那套"不联网、不加载模型"的假依赖 —— 沙箱的端到端验证
# 需要完整跑一遍图，而那套夹具已经为此调好了。
from tests.test_chat_sse import FakeCache, ask                    # noqa: E402

PASS = 0
FAIL = 0
MOCK_PATH = Path("eval/mock_records.json")
API_KEY = settings.api_key.get_secret_value()


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


def _env(*, enabled: bool = True, ttl: int = 600, use_redis: bool = True):
    """一套离线可跑的沙箱 + 工具注册表。"""
    tmp = Path(tempfile.mkdtemp(prefix="sbx-"))
    client = FakeRedis() if use_redis else None
    store = ActionStore(client, ttl=ttl)
    audit = AuditLog(tmp / "sandbox.jsonl")
    sandbox = Sandbox(store, audit=audit, enabled=enabled, ttl=ttl)
    registry = build_registry(mock_path=MOCK_PATH, sandbox=sandbox)
    return registry, sandbox, audit


class _FakeRequest:
    def __init__(self, tools):
        from types import SimpleNamespace

        self.app = SimpleNamespace(state=SimpleNamespace(tools=tools))


# =====================================================================
def test_read_tools_untouched() -> None:
    print("只读工具：直接放行，不产生凭据")
    reg, _, _ = _env()
    r = _run(reg.call("query_ticket_by_no", ticket_no="INC-2026-0412"))
    check("只读工具正常返回", r.ok and r.status == "", str(r))
    check("不产生确认凭据", not r.confirm and not r.needs_confirm, str(r.confirm))

    ev = r.as_event()
    check("事件里没有 status（不需要确认）", "status" not in ev, str(ev))


def test_write_pends_without_token() -> None:
    print("写工具：没有确认凭据 → 挂起，且**未执行**")
    reg, _, audit = _env()
    r = _run(reg.call("restart_service", service="order-service", reason="发布后连接泄漏"))

    check("状态是 pending_confirmation", r.status == "pending_confirmation", r.status)
    check("needs_confirm 为真", r.needs_confirm, str(r.status))
    check("返回了 token", bool(r.confirm.get("token")), str(r.confirm))
    check("没有真的执行（data 为空）", r.data is None, str(r.data))
    check("文本明确写未执行", "尚未执行" in r.text, r.text)

    ctx = render_tool_context([r])
    check("上下文里禁止声称已完成", "尚未执行" in ctx and "不得声称" in ctx, ctx)

    events = [e.get("event") for e in audit.tail(10)]
    check("审计记了 pending", "pending" in events, str(events))
    detail = audit.tail(1)[0]
    check("审计里有工具与参数", detail.get("tool") == "restart_service"
          and detail.get("args", {}).get("service") == "order-service", json.dumps(detail, ensure_ascii=False))
    check("审计里有风险等级", detail.get("risk") == "dangerous", str(detail.get("risk")))


def test_confirm_executes_snapshot_args() -> None:
    print("确认执行：参数只认快照，调用方传什么都不看")
    reg, _, _ = _env()
    pending = _run(reg.call("restart_service", service="order-service", reason="发布后连接泄漏"))
    token = pending.confirm["token"]

    # 关键用例：带 token 但把参数换成 payment-gateway（危险动作 + 敏感服务）。
    # 如果实现里"确认"和"参数"是两条独立通道，这里就会真的去重启支付服务。
    done = _run(reg.call("restart_service", service="payment-gateway", reason="hack",
                         _confirm=token))

    check("执行成功", done.ok, str(done.error))
    check("执行的是快照里的服务", done.args.get("service") == "order-service", str(done.args))
    check("调用方塞的参数被丢弃", done.args.get("reason") != "hack", str(done.args))
    check("标了模拟执行", done.simulated is True, str(done.simulated))
    check("事件里带 simulated 标记", done.as_event().get("simulated") is True, str(done.as_event()))
    check("恢复正常（不再挂起）", done.status == "", done.status)


def test_token_single_use() -> None:
    print("凭据：一次性，重放失效")
    reg, _, _ = _env()
    pending = _run(reg.call("scale_service", service="order-service", replicas=5))
    token = pending.confirm["token"]

    first = _run(reg.call("scale_service", _confirm=token))
    second = _run(reg.call("scale_service", _confirm=token))

    check("第一次执行成功", first.ok, str(first.error))
    check("第二次被拒", not second.ok and second.status == "denied", str(second.status))
    check("拒绝原因说清是失效", "无效" in second.error or "过期" in second.error, second.error)


def test_token_bound_to_tool() -> None:
    print("凭据：绑定动作，不能挪用到别的工具")
    reg, _, _ = _env()
    pending = _run(reg.call("restart_service", service="order-service"))
    token = pending.confirm["token"]

    other = _run(reg.call("scale_service", _confirm=token))
    check("换工具被拒", not other.ok and other.status == "denied", str(other.status))
    check("原因指出不匹配", "不匹配" in other.error, other.error)
    # 被挪用后凭据已销毁：原工具也再用不了（防止反复试探）
    again = _run(reg.call("restart_service", _confirm=token))
    check("挪用过即作废（原工具也不能再用）", not again.ok, str(again.status))


def test_policy_denies_before_confirm() -> None:
    print("策略：非法参数直接拒绝，连确认机会都不给")
    reg, _, _ = _env()

    cases = [
        ("restart_service", {"service": "prod-db-primary"}, "服务不在允许清单"),
        ("restart_service", {"service": "order-service; rm -rf /"}, "带 shell 元字符"),
        ("restart_service", {"service": "payment-gateway"}, "禁止重启支付服务"),
        ("restart_service", {"service": "order-service", "sudo": "1"}, "未声明参数"),
        ("scale_service", {"service": "order-service", "replicas": 100}, "副本数越界"),
    ]
    for tool, args, label in cases:
        r = _run(reg.call(tool, **args))
        check(f"{label} → denied", not r.ok and r.status == "denied", f"{r.status} {r.error}")
        check(f"{label} → 不发凭据", not r.confirm, str(r.confirm))

    # 白名单命中的合法调用仍然能挂起（拒绝的是形状，不是动作本身）
    ok = _run(reg.call("scale_service", service="order-service", replicas=5))
    check("合法参数仍可挂起", ok.status == "pending_confirmation", ok.status)


def test_forbidden_reason_cites_runbook() -> None:
    print("策略：禁止动作给出可核对的依据")
    ok, reason = validate_call("restart_service", {"service": "payment-gateway"})
    check("payment-gateway 被禁止", not ok, reason)
    check("依据来自知识库条目", "rb-payment-flow" in reason, reason)


def test_reject_flow() -> None:
    print("拒绝：不发执行，凭据同时作废")
    reg, sandbox, audit = _env()
    pending = _run(reg.call("restart_service", service="order-service"))
    token = pending.confirm["token"]

    rejected = _run(sandbox.reject(token))
    check("拒绝返回了动作", rejected is not None and rejected.tool == "restart_service",
          str(rejected))
    after = _run(reg.call("restart_service", _confirm=token))
    check("拒绝后再确认无效", not after.ok, str(after.status))
    check("审计记了 rejected", "rejected" in [e.get("event") for e in audit.tail(10)],
          str([e.get("event") for e in audit.tail(10)]))


def test_fail_closed() -> None:
    print("fail-closed：关掉沙箱 / 工具没策略 → 写操作一律拒绝")
    reg_off, _, _ = _env(enabled=False)
    r = _run(reg_off.call("restart_service", service="order-service"))
    check("沙箱关闭 → 拒绝执行", r.status == "denied", r.status)
    check("不是挂起（不能靠关沙箱绕过）", not r.confirm, str(r.confirm))

    read = _run(reg_off.call("query_alerts_by_service", service="order-service"))
    check("只读工具不受影响", read.ok, str(read.error))

    ok, reason = validate_call("drop_database", {})
    check("未声明策略的工具 → 拒绝", not ok and "未声明" in reason, reason)


def test_expired_token() -> None:
    print("凭据：过期即失效（不需要定时清理任务）")
    tmp = Path(tempfile.mkdtemp(prefix="sbx-"))
    store = ActionStore(None, ttl=-1)          # 立刻过期
    sandbox = Sandbox(store, audit=AuditLog(tmp / "a.jsonl"), ttl=-1)
    reg = build_registry(mock_path=MOCK_PATH, sandbox=sandbox)

    pending = _run(reg.call("restart_service", service="order-service"))
    check("过期凭据仍会挂起（expires_in=0）", pending.needs_confirm, pending.status)
    check("事件里 expires_in 不为负", pending.confirm.get("expires_in") == 0,
          str(pending.confirm.get("expires_in")))
    used = _run(reg.call("restart_service", _confirm=pending.confirm["token"]))
    check("过期后不能执行", not used.ok and used.status == "denied", used.status)


def test_session_context_in_audit() -> None:
    print("审计：记录是谁在什么场景下申请的")
    reg, _, audit = _env()
    with call_context(session_id="sess-42", message="帮我重启 order-service"):
        _run(reg.call("restart_service", service="order-service"))
    last = audit.tail(1)[0]
    check("审计带 session_id", last.get("session_id") == "sess-42", str(last.get("session_id")))
    check("审计带原始问题", "重启" in str(last.get("message")), str(last.get("message")))
    check("token 只存前 8 位（日志泄露不等于凭据泄露）",
          len(str(last.get("token"))) <= 8, str(last.get("token")))


# =====================================================================
# 规划与路由
# =====================================================================
def test_planner_emits_write_calls() -> None:
    print("规划：写意图 → 写工具；参数抽不出就不规划")
    d_restart = decide("帮我重启 order-service")
    calls = plan(d_restart, "帮我重启 order-service")
    names = [c.name for c in calls]
    check("重启 → 规划 restart_service", "restart_service" in names, str(names))
    check("先读现场（告警/工单）再动手",
          names.index("query_alerts_by_service") < names.index("restart_service"), str(names))
    restart = next(c for c in calls if c.name == "restart_service")
    check("原因取自用户原话", "重启" in restart.args.get("reason", ""), str(restart.args))

    d_scale = decide("把 order-service 扩容到 5 个副本")
    scale = [c for c in plan(d_scale, "把 order-service 扩容到 5 个副本")
             if c.name == "scale_service"]
    check("扩容 → 规划 scale_service", len(scale) == 1, str(scale))
    check("副本数抽对了", scale and scale[0].args.get("replicas") == 5, str(scale))

    vague = plan(decide("把 order-service 扩容一下"), "把 order-service 扩容一下")
    check("没说副本数 → 不规划（不猜默认值）",
          not any(c.name == "scale_service" for c in vague), str([c.name for c in vague]))

    ask_steps = plan(decide("重启 order-service 的步骤是什么"),
                     "重启 order-service 的步骤是什么")
    check("问步骤（知识题）不规划写操作",
          not any(c.name in ("restart_service", "scale_service") for c in ask_steps),
          str([c.name for c in ask_steps]))


def test_route_flags_write() -> None:
    print("路由：needs_write 只在「真要动线上」时置真")
    check("重启 + 服务名 → needs_write",
          decide("帮我重启 order-service").needs_write is True)
    check("扩容 + 服务名 → needs_write",
          decide("把 order-service 扩容到 5 个副本").needs_write is True)
    check("只查状态 → 不需要写", decide("order-service 现在有没有告警").needs_write is False)
    check("无服务名的「重启现象」→ 不需要写",
          decide("Pod 反复重启应该按哪个文档处理").needs_write is False)
    check("事件里带 needs_write 字段",
          "needs_write" in decide("帮我重启 order-service").as_event())


# =====================================================================
# 端到端：一轮挂起 + 一次确认
# =====================================================================
def test_end_to_end_pending_then_confirm(monkeypatch) -> None:
    print("端到端：/chat/stream 挂起 → /chat/confirm 执行")
    from app.pipeline import rag  # noqa: PLC0415

    async def fake_embed_one(text):
        return [0.1] * 8

    async def fake_recall_detailed(query, **kwargs):
        return rag.RecallOutcome(chunks=[], channels={}, fused=False)

    async def fake_rerank(query, items, top_k=None, **kwargs):
        return list(items)

    generated = {"n": 0}

    async def fake_generate(*a, **k):
        generated["n"] += 1
        yield "不该有这一步"

    monkeypatch.setattr("app.pipeline.orchestrator.embed_one", fake_embed_one)
    monkeypatch.setattr("app.pipeline.rag.recall_detailed", fake_recall_detailed)
    monkeypatch.setattr("app.pipeline.rag.rerank_chunks", fake_rerank)
    monkeypatch.setattr("app.pipeline.rag.generate_stream", fake_generate)

    reg, _, _ = _env()
    events = asyncio.run(ask("帮我重启 order-service", cache=FakeCache(), tools=reg))
    types = [e.get("type") for e in events]

    check("有 confirm_required 事件", "confirm_required" in types, str(types))
    check("没有生成任何正文 token", "token" not in types, str(types))
    check("没有调 LLM", generated["n"] == 0, str(generated))
    check("done 标了 awaiting_confirmation",
          events[-1].get("type") == "done"
          and events[-1].get("status") == "awaiting_confirmation", str(events[-1]))
    check("done 里带 pending 摘要",
          events[-1].get("pending") and events[-1]["pending"][0]["tool"] == "restart_service",
          str(events[-1].get("pending")))

    confirm_ev = next(e for e in events if e.get("type") == "confirm_required")
    check("confirm_required 带参数（要批的是什么得看得见）",
          confirm_ev.get("args", {}).get("service") == "order-service", str(confirm_ev))
    check("confirm_required 带风险等级", confirm_ev.get("risk") == "dangerous",
          str(confirm_ev.get("risk")))
    check("confirm_required 给出调用方式", "confirm" in str(confirm_ev.get("confirm_hint")),
          str(confirm_ev.get("confirm_hint")))

    token = confirm_ev["token"]
    resp = asyncio.run(confirm_action(
        ConfirmRequest(token=token, approve=True),
        _FakeRequest(reg),
        x_api_key=API_KEY,
    ))
    check("确认接口执行成功", resp.get("ok") is True and resp.get("status") == "executed",
          json.dumps(resp, ensure_ascii=False)[:200])
    check("确认接口返回的是快照参数", resp.get("args", {}).get("service") == "order-service",
          str(resp.get("args")))
    check("确认接口标了模拟执行", resp.get("simulated") is True, str(resp.get("simulated")))
    check("确认结果含可读回执", "模拟执行" in str(resp.get("text")), str(resp.get("text"))[:80])

    # 第二次确认必须 404（凭据已消费）
    try:
        again = asyncio.run(confirm_action(
            ConfirmRequest(token=token, approve=True),
            _FakeRequest(reg),
            x_api_key=API_KEY,
        ))
        status = getattr(again, "status_code", 200)
    except Exception as exc:                      # noqa: BLE001 - HTTPException 也是预期
        status = getattr(exc, "status_code", 500)
    check("重复确认被拒（404）", status == 404, f"status={status}")


def test_end_to_end_reject(monkeypatch) -> None:
    print("端到端：拒绝路径不执行")
    from app.pipeline import rag  # noqa: PLC0415

    async def fake_embed_one(text):
        return [0.1] * 8

    async def fake_recall_detailed(query, **kwargs):
        return rag.RecallOutcome(chunks=[], channels={}, fused=False)

    async def fake_rerank(query, items, top_k=None, **kwargs):
        return list(items)

    async def fake_generate(*a, **k):
        yield "不该有这一步"

    monkeypatch.setattr("app.pipeline.orchestrator.embed_one", fake_embed_one)
    monkeypatch.setattr("app.pipeline.rag.recall_detailed", fake_recall_detailed)
    monkeypatch.setattr("app.pipeline.rag.rerank_chunks", fake_rerank)
    monkeypatch.setattr("app.pipeline.rag.generate_stream", fake_generate)

    reg, _, _ = _env()
    events = asyncio.run(ask("帮我重启 order-service", cache=FakeCache(), tools=reg))
    token = next(e for e in events if e.get("type") == "confirm_required")["token"]

    resp = asyncio.run(confirm_action(
        ConfirmRequest(token=token, approve=False),
        _FakeRequest(reg),
        x_api_key=API_KEY,
    ))
    check("拒绝返回 rejected", resp.get("status") == "rejected", str(resp))

    try:
        again = asyncio.run(confirm_action(
            ConfirmRequest(token=token, approve=True), _FakeRequest(reg), x_api_key=API_KEY,
        ))
        status = getattr(again, "status_code", 200)
    except Exception as exc:                      # noqa: BLE001
        status = getattr(exc, "status_code", 500)
    check("拒绝后凭据不能再用于执行（404）", status == 404, f"status={status}")


def test_route_flags_write_in_compound_sentence() -> None:
    """复合句里的写操作必须仍然成立 —— 这类漏判是"该拦的没拦"。

    "帮我重启 order-service，并查一下 OOM 怎么办"被早期实现判成
    hybrid+只读：知识信号在**整句**范围一票否决了写操作，于是既不挂起、
    也不拒绝，答案里还照常写"重启命令参考"。用户以为在走审批流程。
    """
    print("路由：复句里的写操作不被知识信号吞掉")
    compound = decide("帮我重启 order-service，并查一下 OOM 怎么办")
    check("复句判 hybrid（两路都要跑）", compound.intent == "hybrid", compound.intent)
    check("复句仍标 needs_write", compound.needs_write is True, str(compound.needs_write))
    check("复句仍要检索知识", compound.needs_rag is True, str(compound.needs_rag))

    ask_how = decide("重启 order-service 的步骤是什么")
    check("同分句问步骤不算写操作（不误判）", ask_how.needs_write is False,
          f"{ask_how.intent} {ask_how.signals}")

    # 中文与英文紧邻是最自然的打字方式，`\b` 在 Python 里认汉字为单词字符，
    # 早期版本因此把 "重启order-service" 整个丢掉（既不写也不查）。
    tight = decide("帮我重启order-service")
    check("中文紧邻服务名也能抽出实体", tight.entities.services == ["order-service"],
          str(tight.entities.services))
    check("中文紧邻时写操作照样成立", tight.needs_write is True, f"{tight.intent} {tight.signals}")


def test_end_to_end_hybrid_keeps_answer(monkeypatch) -> None:
    """hybrid 挂起：知识照答，写操作照挂，两者都不能丢。

    这一条锁住三个容易回退的行为：
      1. 不能因为要答知识就把写操作降级成只读（安全）
      2. 不能因为要挂起就吞掉知识答案（体验）
      3. 挂起轮的答案不进缓存（否则下次同问直接命中一段"等你确认"的旧文字）
    """
    print("端到端：hybrid 既答知识又挂起写操作")
    from tests.test_chat_sse import install_fakes  # noqa: PLC0415

    seen: list[str] = []
    install_fakes(monkeypatch, tool_context_seen=seen)

    reg, _, _ = _env()
    cache = FakeCache()
    events = asyncio.run(ask(
        "帮我重启 order-service，并查一下 OOM 怎么办", cache=cache, tools=reg,
    ))
    types = [e.get("type") for e in events]
    done = events[-1]

    check("正文有内容（知识部分得到回答）", "token" in types, str(types))
    check("同时发了 confirm_required（写操作没被吞）", "confirm_required" in types, str(types))
    check("done.status=awaiting_confirmation",
          done.get("type") == "done" and done.get("status") == "awaiting_confirmation",
          str(done.get("status")))
    check("done.pending 里有 restart_service",
          bool(done.get("pending")) and done["pending"][0]["tool"] == "restart_service",
          str(done.get("pending")))
    check("路由标的是 hybrid", done.get("route") == "hybrid", str(done.get("route")))

    joined = "\n".join(seen)
    check("模型上下文里写明了「尚未执行」", "尚未执行" in joined, joined[:200])
    check("模型上下文里带了重启参数", "restart_service" in joined, joined[:200])
    check("挂起轮的答案不进缓存", not cache.stored, f"stored={len(cache.stored)}")

    confirm_ev = next(e for e in events if e.get("type") == "confirm_required")
    check("凭据仍然可用（挂起不等于失败）", bool(confirm_ev.get("token")), str(confirm_ev)[:120])
    resp = asyncio.run(confirm_action(
        ConfirmRequest(token=confirm_ev["token"], approve=True),
        _FakeRequest(reg), x_api_key=API_KEY,
    ))
    check("确认后执行的是快照参数", resp.get("args", {}).get("service") == "order-service",
          str(resp.get("args")))


def test_metrics_and_dashboard_contract() -> None:
    print("指标：决策计数真的在写（死指标比没指标更糟）")
    from app.observability.metrics import SANDBOX_DECISIONS  # noqa: PLC0415

    reg, _, _ = _env()
    _run(reg.call("restart_service", service="order-service"))
    _run(reg.call("restart_service", service="prod-db"))
    values = {
        tuple(sorted(sample.labels.items())): sample.value
        for metric in SANDBOX_DECISIONS.collect()
        for sample in metric.samples
        if sample.name.endswith("_total")
    }
    check("pending 有计数", any(lbl and "pending" in dict(lbl).values() for lbl in values),
          str(list(values)[:4]))
    check("denied 有计数", any(lbl and "denied" in dict(lbl).values() for lbl in values),
          str(list(values)[:4]))


def main() -> int:
    print("=" * 70)
    print("安全沙箱测试")
    print("=" * 70)
    test_read_tools_untouched()
    test_write_pends_without_token()
    test_confirm_executes_snapshot_args()
    test_token_single_use()
    test_token_bound_to_tool()
    test_policy_denies_before_confirm()
    test_forbidden_reason_cites_runbook()
    test_reject_flow()
    test_fail_closed()
    test_expired_token()
    test_session_context_in_audit()
    test_planner_emits_write_calls()
    test_route_flags_write()
    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

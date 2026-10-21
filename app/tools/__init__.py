"""工具层装配：MCP 优先 → mock 兜底，外面再套一层短 TTL 结果缓存。

一次调用的完整路径
------------------
    规划器给出 ToolCall
      └─ 缓存查（30s TTL，命中直接返回，source=cache）
         └─ MCP 可用？ ──是──▶ 远端调用 ──成功──▶ 返回（source=mcp）
            │                      └─失败──┐
            └──否────────────────────────┐│
                                          ▼▼
                                    mock 兜底（source=mock）
                                          └─成功─▶ 写缓存

为什么降级放在这一层，而不是让 MCP 自己重试
------------------------------------------"
MCP 重试解决的是"远端抖了一下"，而这里要解决的是"远端根本不在"（D1 阶段
Java Server 还没写）。后者重试只会浪费时间。所以在**调用点**做降级，
让一次失败立刻转 mock，而不是先等三次超时。

为什么缓存要单独一层
--------------------
工具结果 TTL 只有 30s（`CACHE_TOOL_TTL`），比语义缓存短得多 —— 这是刻意的：
工单状态是动态数据，缓存 10 分钟就会把"已解决"说成"处理中"。
30s 的收益在于压测和连续追问场景（演示时同一题问两遍），
既拿到缓存收益，又不会把过期状态当真。
"""

from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import asdict
from typing import Any, Awaitable, Callable

from app.observability.metrics import SANDBOX_WRITE_TOOLS
from app.tools.base import DEFAULT_TOOL_TIMEOUT, ToolRegistry, ToolResult, render_tool_context
from app.tools.mcp_client import DEFAULT_MCP_TIMEOUT, McpProvider
from app.tools.providers import (
    ALL_TOOL_SPECS,
    DEFAULT_MOCK_PATH,
    TOOL_SPECS,
    MockProvider,
)
from app.tools.planner import ToolCall, extract_keywords, plan, plan_summary
from app.tools.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    DependencyUnavailableError,
    RetryPolicy,
    observe_call,
    run_chain,
)
from app.tools.sandbox import (
    RISK_READ,
    ActionStore,
    AuditLog,
    PendingAction,
    Sandbox,
    build_sandbox,
    call_context,
    validate_call,
)
from app.tools.skills import (
    SkillRegistry,
    SkillRun,
    SkillSpec,
    build_skill_registry,
    entities_of,
    execute_skill,
)

log = logging.getLogger(__name__)

__all__ = [
    "ALL_TOOL_SPECS",
    "ActionStore",
    "AuditLog",
    "DEFAULT_MOCK_PATH",
    "DEFAULT_TOOL_TIMEOUT",
    "McpProvider",
    "MockProvider",
    "PendingAction",
    "RISK_READ",
    "Sandbox",
    "SkillRegistry",
    "SkillRun",
    "SkillSpec",
    "ToolCall",
    "ToolRegistry",
    "ToolResult",
    "build_registry",
    "build_skill_registry",
    "build_sandbox",
    "call_context",
    "entities_of",
    "execute_skill",
    "extract_keywords",
    "plan",
    "plan_summary",
    "render_tool_context",
    "validate_call",
]


def _tool_cache_parts(name: str, args: dict[str, Any]) -> tuple[Any, ...]:
    """缓存 key 的组成部分。

    参数按 key 排序后入 key —— 否则 `("a", {"x":1,"y":2})` 与
    `{"y":2,"x":1}` 会算出两个不同的缓存项，缓存命中率凭空减半。
    这是 dict 顺序导致缓存失效的典型坑，Python 3.7+ 的 dict 保序反而让它更容易发生。
    """
    return (name, tuple(sorted((k, str(v)) for k, v in args.items())))


def build_registry(
    *,
    settings: Any = None,
    mock_path: Any = None,
    mcp_provider: McpProvider | None = None,
    cache: Any = None,
    skills: Any = None,
    sandbox: Sandbox | None = None,
) -> ToolRegistry:
    """装配工具注册表。

    参数全部可注入，因此测试里能塞一个假的 cache / 假 MCP / 假沙箱，
    不需要真实 Redis 或 Java 服务 —— 这是工具层能被单测覆盖的前提。
    """
    timeout = DEFAULT_TOOL_TIMEOUT
    cache_ttl = 30
    mcp_enabled = False
    mcp_url = ""
    breaker = CircuitBreaker("mcp")
    retry = RetryPolicy()

    if settings is not None:
        timeout = float(getattr(settings, "tool_timeout_seconds", DEFAULT_TOOL_TIMEOUT))
        cache_ttl = int(getattr(settings, "cache_tool_ttl", 30))
        mcp_enabled = bool(getattr(settings, "mcp_enabled", False))
        mcp_url = str(getattr(settings, "mcp_server_url", ""))
        breaker = CircuitBreaker(
            "mcp",
            fail_threshold=int(getattr(settings, "mcp_cb_fail_threshold", 5)),
            cool_down=float(getattr(settings, "mcp_cb_cool_down", 15.0)),
            half_open_max=int(getattr(settings, "mcp_cb_half_open_max", 1)),
        )
        retry = RetryPolicy(
            max_attempts=int(getattr(settings, "mcp_retry_max_attempts", 3)),
            base_delay=float(getattr(settings, "mcp_retry_base_delay", 0.1)),
            max_delay=float(getattr(settings, "mcp_retry_max_delay", 1.0)),
            jitter=float(getattr(settings, "mcp_retry_jitter", 0.2)),
        )

    mock = MockProvider(path=mock_path or DEFAULT_MOCK_PATH)
    mcp = mcp_provider
    if mcp is None and mcp_enabled and mcp_url:
        # 超时取配置值；未配置时按"工具总超时 / 重试次数"倒算，
        # 保证 重试 × 单次超时 + 退避 ≤ 工具总超时（否则重试走不完就被切断）
        mcp_timeout = float(getattr(settings, "mcp_timeout_seconds", 0.0) or 0.0)
        if mcp_timeout <= 0:
            mcp_timeout = max(0.3, min(DEFAULT_MCP_TIMEOUT,
                                       timeout / max(1, retry.max_attempts) * 0.8))
        mcp = McpProvider(
            mcp_url, timeout=mcp_timeout, breaker=breaker, retry=retry,
            chaos=str(getattr(settings, "mcp_chaos", "") or "") if settings is not None else "",
        )
    if mcp is not None:
        # 注入的 provider 也让它共用同一个熔断器 —— 否则 /ready 看到的状态
        # 与实际调用用的不是同一个对象，会出现"面板显示 closed 但请求一直被拒"。
        breaker = getattr(mcp, "breaker", breaker)
        retry = getattr(mcp, "retry", retry)

    registry = ToolRegistry(timeout=timeout)

    # 沙箱晚于 registry 创建也不影响：它只被 handler 引用，注册期不需要它。
    # 这里先建，是为了让写工具的 handler 与只读工具走**同一个** handler 工厂 ——
    # 闸门如果在两个地方各写一遍，早晚会有一处漏掉。
    sandbox = sandbox if sandbox is not None else build_sandbox(
        settings=settings,
        client=getattr(cache, "client", None),
    )

    for spec in ALL_TOOL_SPECS:
        # 写工具**不接 MCP、不接缓存**：
        #   - MCP 侧没有写接口（也不该有），转发过去只会得到"工具不存在"
        #   - 缓存一个"已重启"的结果 30 秒，等于把一次动作伪装成可重复查询的事实
        readonly = getattr(spec, "risk", RISK_READ) == RISK_READ
        registry.register(
            spec,
            _make_handler(
                spec.name,
                spec=spec,
                mock=mock,
                mcp=mcp if readonly else None,
                cache=cache if readonly else None,
                cache_ttl=cache_ttl,
                sandbox=sandbox,
            ),
        )

    # 把装配信息挂在 registry 上，供 /ready、/chat/confirm 与日志展示。
    # 演示时要能一句话说清"现在的工具结果从哪来"。
    registry.source = "mcp+mock" if mcp is not None else "mock"
    # 在册写工具数：面板上与 pending 对照，用来发现"闸门没接上"（见 metrics.py）
    SANDBOX_WRITE_TOOLS.set(
        sum(1 for s in ALL_TOOL_SPECS if getattr(s, "risk", RISK_READ) != RISK_READ)
    )
    registry.mcp_provider = mcp  # type: ignore[attr-defined]
    registry.mock_provider = mock  # type: ignore[attr-defined]
    registry.breaker = breaker  # type: ignore[attr-defined]
    registry.retry = retry  # type: ignore[attr-defined]
    registry.sandbox = sandbox  # type: ignore[attr-defined]
    registry.skills = skills if skills is not None else build_skill_registry(
        getattr(settings, "skills_path", None) if settings is not None else None
    )  # type: ignore[attr-defined]
    return registry


def _pending_result(tool: str, pending: PendingAction, simulated: bool) -> ToolResult:
    """挂起待确认。`ok=False` 但不计入降级（见 base.ToolResult.status 的注释）。"""
    arg_text = "、".join(f"{k}={v}" for k, v in pending.args.items())
    return ToolResult(
        tool=tool, ok=False, args=dict(pending.args), source="sandbox",
        status="pending_confirmation",
        confirm=pending.as_event(),
        simulated=simulated,
        error="",
        text=(
            f"[等待人工确认] {tool}({arg_text})\n"
            f"  风险等级：{pending.risk}；{pending.reason}\n"
            f"  该操作**尚未执行**，确认后才会真正下发\n"
            f"  凭据有效期 {max(0, int(pending.expires_at - time.time()))}s，只能使用一次"
        ),
    )


def _denied_result(tool: str, args: dict[str, Any], reason: str) -> ToolResult:
    return ToolResult(
        tool=tool, ok=False, args=dict(args), source="sandbox", status="denied",
        error=reason,
        text=f"[安全策略拒绝] {tool} 未执行（{reason}）",
    )


async def _sandbox_gate(
    tool: str,
    risk: str,
    simulated: bool,
    args: dict[str, Any],
    token: str | None,
    sandbox: Sandbox | None,
) -> tuple[ToolResult | None, dict[str, Any]]:
    """写操作的闸门。返回 (拦截结果, 本次真正要用的参数)。

    第二项是这套设计的关键：确认执行时参数**只从存储里的快照读**，
    调用方传进来的 kwargs 在这个分支被整体丢弃。所以「确认过 A 却执行了 B」
    在结构上不可能发生 —— 不是靠校验，是靠没有第二条数据通路。
    """
    if sandbox is None:
        # fail-closed：没有沙箱就没有审批，没有审批就不能写
        return _denied_result(tool, args, "沙箱未启用"), args

    if token:
        action, why = await sandbox.grant(token, tool)
        if action is None:
            return _denied_result(tool, args, why), args
        # 再校验一次：策略表可能在"挂起"与"确认"之间被改过。
        # 用快照里的参数校验，而不是请求里的 —— 校验对象必须是真正要执行的那份。
        ok, reason = validate_call(tool, action.args)
        if not ok:
            sandbox.denied(tool, risk, action.args, reason)
            return _denied_result(tool, action.args, reason), args
        sandbox.approved(action)
        return None, dict(action.args)

    decision = sandbox.precheck(tool, risk, args)
    if decision.action == "deny":
        sandbox.denied(tool, risk, args, decision.reason)
        return _denied_result(tool, args, decision.reason), args

    pending = await sandbox.pend(
        tool, risk, args, reason=decision.reason, simulated=simulated
    )
    return _pending_result(tool, pending, simulated), args


def _make_handler(
    tool_name: str,
    *,
    mock: MockProvider,
    mcp: McpProvider | None,
    cache: Any,
    cache_ttl: int,
    spec: Any = None,
    sandbox: Sandbox | None = None,
) -> Callable[..., Awaitable[ToolResult]]:
    """一次工具调用的完整路径，写成**显式三步链**：

        [沙箱闸门] → cache（30s） → mcp（熔断+重试） → mock

    之前是嵌套 if/else，加一条路径就要动三处缩进；改成 steps 列表后，
    "降级顺序"就是代码的阅读顺序，改顺序 = 调整列表顺序。

    闸门放在**最前面**，而且在这里（而不是在 planner / node_tools）：
    工具调用只有这一个收口，绕过它得改构建图；放在上游则可以有一条路径忘记加。
    """

    risk = str(getattr(spec, "risk", RISK_READ) or RISK_READ)
    simulated = bool(getattr(spec, "simulated", False))
    gated = risk != RISK_READ

    async def handler(**kwargs: Any) -> ToolResult:
        # `_confirm` 是**确认凭据**，不是工具参数：它永远不会转发给 provider
        confirm_token = kwargs.pop("_confirm", None) if gated else None

        if gated:
            blocked, kwargs = await _sandbox_gate(
                tool_name, risk, simulated, kwargs, confirm_token, sandbox
            )
            if blocked is not None:
                return blocked

        parts = _tool_cache_parts(tool_name, kwargs)

        async def from_cache() -> ToolResult | None:
            if cache is None:
                return None
            return await _cache_get(cache, parts)

        async def from_mcp() -> ToolResult:
            assert mcp is not None
            if not await mcp.available():
                # 区分"探测失败"与"熔断打开"：前者是依赖被动不可用，
                # 后者是我们主动拒绝，排障方向完全不同。
                # 用 getattr 而不是直接访问：mcp 是注入点（测试里的假 provider
                # 不必知道熔断器存在），生产代码不能假设它带这个属性
                brk = getattr(mcp, "breaker", None)
                if brk is not None and brk.is_open:
                    raise CircuitOpenError("mcp")
                raise DependencyUnavailableError("MCP 探测失败")
            return await mcp.call(tool_name, **kwargs)

        async def from_mock() -> ToolResult:
            return await mock.call(tool_name, **kwargs)

        steps: list[tuple[str, Callable[[], Awaitable[Any]]]] = []
        if cache is not None and not gated:
            steps.append(("cache", from_cache))
        if mcp is not None:
            steps.append(("mcp", from_mcp))
        steps.append(("mock", from_mock))

        result, hops = await run_chain(steps, metric_stage="tools")

        if simulated and result is not None:
            # 沿着降级链一路带下去：命中 mock 时也要能看出"这是模拟执行的结果"
            result.simulated = True

        if result is None:  # mock 也失败 —— 理论上不该发生，但不能假装成功
            result = ToolResult(
                tool=tool_name, ok=False, args=kwargs, source="none",
                error="全部数据源失败", text=f"[工具 {tool_name} 的缓存/MCP/mock 全部失败]",
                chain=[h.as_dict() for h in hops],
            )
        else:
            # 命中的那一跳的耗时口径以 ToolResult 自己为准（mock/mcp 内部测），
            # 链上耗时只用于回答"降级一共花了多久"
            result.chain = [h.as_dict() for h in hops]
            brk = getattr(mcp, "breaker", None) if mcp is not None else None
            if brk is not None and not result.breaker:
                # 降级到 mock 时也要带上熔断状态，否则前端/回放里看不出
                # "这次是熔断期还是正常期" —— 两者的处置完全不同
                result.breaker = brk.state_name
            if cache is not None and result.source == "cache" and not result.cached_from:
                # 缓存命中时把"这份数据原本从哪来"补回去 —— 否则 30s 内
                # "实时还是 mock"这个事实会被永久抹掉
                result.cached_from = "unknown"

        observe_call(tool_name, result.source, result.ok, result.elapsed_ms)

        # 只缓存成功结果：否则一次抖动会被固化 30 秒，
        # 故障恢复后仍然返回"查不到"，看起来像没恢复。
        #
        # 注意只缓存**来自 mcp 或 mock 的结果**，不缓存 cache 自己（无意义），
        # 也不缓存降级到 mock 的结果 —— 那会把"临时降级"固化 30s。
        if cache is not None and result.ok and result.source in ("mcp", "mock"):
            if result.source == "mock" and any(h.source == "mcp" for h in hops):
                pass  # 真降级：不写缓存，等 MCP 恢复后立刻回到实时数据
            else:
                await _cache_set(cache, parts, result, ttl=cache_ttl)
        return result

    handler.__name__ = f"tool_{tool_name}"
    return handler


async def _cache_get(cache: Any, parts: tuple[Any, ...]) -> ToolResult | None:
    try:
        raw = await cache.get_json("tool", *parts)
    except Exception as exc:  # noqa: BLE001 - 缓存故障绝不能影响工具可用性
        log.debug("工具缓存读取失败：%s", exc)
        return None
    if not isinstance(raw, dict):
        return None
    try:
        fields = {f.name for f in dataclasses.fields(ToolResult)}
        payload = {k: v for k, v in raw.items() if k in fields}
        result = ToolResult(**payload)
    except Exception:  # noqa: BLE001 - 旧格式/脏数据直接当未命中
        return None
    # 命中缓存后 source 变成 cache，但**原始来源不能丢** ——
    # 否则 30s 内"这是实时数据还是 mock"就再也说不清了
    if not result.cached_from:
        result.cached_from = result.source
    result.source = "cache"
    return result


async def _cache_set(cache: Any, parts: tuple[Any, ...], result: ToolResult, *, ttl: int) -> None:
    try:
        await cache.set_json("tool", asdict(result), ttl=ttl, parts=parts)
    except Exception as exc:  # noqa: BLE001
        log.debug("工具缓存写入失败：%s", exc)


def health_summary(registry: ToolRegistry) -> dict[str, Any]:
    """给 /ready 用的工具层状态。"""
    mcp = getattr(registry, "mcp_provider", None)
    breaker = getattr(registry, "breaker", None)
    skills = getattr(registry, "skills", None)
    retry = getattr(registry, "retry", None)
    sandbox = getattr(registry, "sandbox", None)
    return {
        "tools": registry.names(),
        # 只读与写分开列：/ready 一眼能看出"这个实例敢不敢写"
        "read_tools": [s.name for s in registry.specs() if getattr(s, "risk", RISK_READ) == RISK_READ],
        "write_tools": [s.name for s in registry.specs() if getattr(s, "risk", RISK_READ) != RISK_READ],
        "source": registry.source,
        "mcp_url": getattr(mcp, "url", "") if mcp is not None else "",
        "mcp_probe": getattr(mcp, "_probe_ok", None) if mcp is not None else None,
        "timeout_seconds": registry.timeout,
        "breaker": breaker.snapshot().as_dict() if breaker is not None else None,
        "retry": {
            "max_attempts": retry.max_attempts,
            "base_delay": retry.base_delay,
            "max_delay": retry.max_delay,
        } if retry is not None else None,
        "skills": skills.snapshot() if skills is not None else [],
        # 沙箱状态。`store` 是 redis 还是 memory 必须暴露 ——
        # memory 意味着"确认请求打到别的副本就找不到"，多副本下等于不可用。
        "sandbox": {
            "enabled": bool(getattr(sandbox, "enabled", False)),
            "store": getattr(getattr(sandbox, "store", None), "source", "none"),
            "pending_ttl": getattr(sandbox, "ttl", 0),
        } if sandbox is not None else None,
    }

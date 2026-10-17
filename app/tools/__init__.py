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
from dataclasses import asdict
from typing import Any, Awaitable, Callable

from app.tools.base import DEFAULT_TOOL_TIMEOUT, ToolRegistry, ToolResult, render_tool_context
from app.tools.mcp_client import DEFAULT_MCP_TIMEOUT, McpProvider
from app.tools.providers import DEFAULT_MOCK_PATH, TOOL_SPECS, MockProvider
from app.tools.planner import ToolCall, extract_keywords, plan, plan_summary

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MOCK_PATH",
    "DEFAULT_TOOL_TIMEOUT",
    "McpProvider",
    "MockProvider",
    "ToolCall",
    "ToolRegistry",
    "ToolResult",
    "build_registry",
    "extract_keywords",
    "plan",
    "plan_summary",
    "render_tool_context",
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
) -> ToolRegistry:
    """装配工具注册表。

    参数全部可注入，因此测试里能塞一个假的 cache / 假 MCP，
    不需要真实 Redis 或 Java 服务 —— 这是工具层能被单测覆盖的前提。
    """
    timeout = DEFAULT_TOOL_TIMEOUT
    cache_ttl = 30
    mcp_enabled = False
    mcp_url = ""

    if settings is not None:
        timeout = float(getattr(settings, "tool_timeout_seconds", DEFAULT_TOOL_TIMEOUT))
        cache_ttl = int(getattr(settings, "cache_tool_ttl", 30))
        mcp_enabled = bool(getattr(settings, "mcp_enabled", False))
        mcp_url = str(getattr(settings, "mcp_server_url", ""))

    mock = MockProvider(path=mock_path or DEFAULT_MOCK_PATH)
    mcp = mcp_provider
    if mcp is None and mcp_enabled and mcp_url:
        mcp = McpProvider(mcp_url, timeout=DEFAULT_MCP_TIMEOUT)

    registry = ToolRegistry(timeout=timeout)

    for spec in TOOL_SPECS:
        registry.register(spec, _make_handler(spec.name, mock=mock, mcp=mcp, cache=cache,
                                             cache_ttl=cache_ttl))

    # 把装配信息挂在 registry 上，供 /ready 与日志展示。
    # 演示时要能一句话说清"现在的工具结果从哪来"。
    registry.source = "mcp+mock" if mcp is not None else "mock"
    registry.mcp_provider = mcp  # type: ignore[attr-defined]
    registry.mock_provider = mock  # type: ignore[attr-defined]
    return registry


def _make_handler(
    tool_name: str,
    *,
    mock: MockProvider,
    mcp: McpProvider | None,
    cache: Any,
    cache_ttl: int,
) -> Callable[..., Awaitable[ToolResult]]:
    async def handler(**kwargs: Any) -> ToolResult:
        parts = _tool_cache_parts(tool_name, kwargs)

        # ---- ① 短 TTL 缓存 ----
        if cache is not None:
            cached = await _cache_get(cache, parts)
            if cached is not None:
                return cached

        # ---- ② MCP 优先，失败立刻降级 ----
        result: ToolResult | None = None
        if mcp is not None:
            try:
                if await mcp.available():
                    candidate = await mcp.call(tool_name, **kwargs)
                    if candidate.ok:
                        result = candidate
                    else:
                        log.warning("MCP 工具 %s 返回失败（%s），降级到 mock",
                                    tool_name, candidate.error)
            except Exception as exc:  # noqa: BLE001 - 远端任何异常都降级
                log.warning("MCP 工具 %s 调用异常（%s: %s），降级到 mock",
                            tool_name, type(exc).__name__, exc)

        # ---- ③ mock 兜底 ----
        if result is None:
            result = await mock.call(tool_name, **kwargs)

        # ---- ④ 只缓存成功结果 ----
        # 失败结果不缓存：否则一次抖动会被固化 30 秒，
        # 故障恢复后仍然返回"查不到"，看起来像没恢复。
        if cache is not None and result.ok:
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
    return {
        "tools": registry.names(),
        "source": registry.source,
        "mcp_url": getattr(mcp, "url", "") if mcp is not None else "",
        "mcp_probe": getattr(mcp, "_probe_ok", None) if mcp is not None else None,
        "timeout_seconds": registry.timeout,
    }

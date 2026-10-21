"""MCP 客户端骨架 —— 对接 D2 的 Java MCP Server。

为什么现在就写
--------------
因为"工具层能不能在 MCP 缺席时工作"是一个**必须提前验证**的架构问题，
而不是 D2 写完 Java 服务之后再说的事。如果工具层硬依赖 MCP，
D1 的所有端到端验证都做不了，主链路会被一个还没写的服务卡住。

现在的职责是：把 MCP 接进来，但**不假设它一定在**。

三个设计要点
------------
1. **探测结果带 TTL 缓存**：不缓存的话，MCP 挂掉之后每个请求都要等一次
   连接超时（比如 2s），TTL 缓存把它压成"每个 30s 只有第一个请求付这个代价"。
2. **失败不是异常，是降级**：`call` 抛错时由 `RoutedToolSource` 接住并转 mock。
   对上层来说"工具能返回结果"这件事不受影响，只是 `source` 从 mcp 变成 mock。
3. **超时要短**：MCP 是辅助信息源，不该拖垮主链路。默认 2s，
   比 `ToolRegistry` 的 3s 上限更严 —— 留 1s 给降级路径走完。

关于协议：这里用的是 **JSON-RPC over HTTP**（MCP 的 HTTP transport 形态），
不是 stdio。选 HTTP 的原因很实际：Java 侧 Spring Boot 起 HTTP 端点最自然，
而 stdio 需要额外的进程管理。D2 实现时若改用 SSE transport，
只需要换掉 `_rpc` 这一个方法。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Sequence

from app.tools.base import ToolRegistry, ToolResult, ToolSpec
from app.tools.providers import TOOL_SPECS
from app.tools.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    PermanentToolError,
    RetryPolicy,
    TransientToolError,
    guarded_call,
)

log = logging.getLogger(__name__)

DEFAULT_MCP_TIMEOUT = 0.8
# 探测结果缓存时长。见模块 docstring 第 1 点。
PROBE_TTL = 30.0
# 探测**失败**时的缓存时长：比成功时短得多。
# 理由：失败后要尽快给熔断器攒够信号（5 次失败 × 5s ≈ 25s 后打开），
# 而不是把"不可用"当成事实缓存 30s —— 那会让恢复慢半分钟。
PROBE_TTL_DOWN = 5.0


class McpProvider:
    """JSON-RPC over HTTP 的 MCP 客户端。

    `RoutedToolSource`（见 app/tools/__init__.py）会先问 `available()`，
    拿不到才走 mock —— 所以这里不需要自己实现 fallback。

    D2 起多了两件事：**熔断器**与**重试**。
        breaker(外) → retry(内) → HTTP
    熔断打开时 `call()` 直接抛 `CircuitOpenError`（微秒级），
    上层降级链立刻转 mock，不再付 3 次超时的代价。

    **超时预算**（这里踩过一个坑）：`DEFAULT_MCP_TIMEOUT` 必须满足
    `timeout × max_attempts + 退避 < 工具总超时`，否则 `ToolRegistry.call`
    会在重试走完之前就把整个调用切断 —— 表现为"日志说在重试，实际早就超时了"。
    0.8s × 3 + ~0.3s 退避 ≈ 2.7s < 3s。
    """

    name = "mcp"

    def __init__(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_MCP_TIMEOUT,
        endpoint: str = "/mcp",
        breaker: CircuitBreaker | None = None,
        retry: RetryPolicy | None = None,
        chaos: str = "",
    ) -> None:
        self.url = (url or "").rstrip("/")
        self.timeout = timeout
        self.endpoint = endpoint
        self._probe_ok: bool | None = None
        self._probe_at: float = 0.0
        self._rpc_id = 0
        self.breaker = breaker or CircuitBreaker("mcp")
        self.retry = retry or RetryPolicy()
        # 故障注入开关（演示用）。空字符串 = 不发这个头。
        self.chaos = chaos

    # ---------------- 探测 ----------------
    async def available(self) -> bool:
        """带 TTL 的能力探测。失败也缓存 —— 那正是最需要缓存的情况。

        **探测结果会喂给熔断器**：否则会出现一个隐蔽的死循环 ——
        探测失败 → 不发业务请求 → 熔断器永远收不到失败 → 永远不打开 →
        永远每 30s 付一次探测超时。把探测当作一次健康信号，
        熔断器才能"确认持续不可用后快速失败，冷却后再放一个探测"。
        """
        if self.breaker.is_open:
            return False

        now = time.monotonic()
        if self._probe_ok is not None:
            ttl = PROBE_TTL if self._probe_ok else PROBE_TTL_DOWN
            if (now - self._probe_at) < ttl:
                return self._probe_ok

        ok = False
        try:
            result = await self._rpc("tools/list", {}, timeout=self.timeout)
            ok = bool(result is not None)
        except Exception as exc:  # noqa: BLE001 - 探测失败是预期情况
            log.info("MCP 不可用（%s）：%s —— 本次将使用 mock 工具结果", self.url,
                     f"{type(exc).__name__}: {exc}")

        # 探测结果即健康信号。注意 record 而不是 guarded_call：
        # 探测不重试（重试探测只是把一次超时变成三次）
        await self.breaker.record(ok)

        self._probe_ok = ok
        self._probe_at = now
        if ok:
            log.info("MCP 可用：%s", self.url)
        return ok

    def invalidate_probe(self) -> None:
        """手动让探测结果失效。故障恢复演示里用得上。"""
        self._probe_ok = None
        self._probe_at = 0.0

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._rpc("tools/list", {}, timeout=self.timeout)
        if isinstance(result, dict):
            tools = result.get("tools")
            if isinstance(tools, list):
                return [t for t in tools if isinstance(t, dict)]
        return []

    # ---------------- 调用 ----------------
    async def call(self, tool: str, **args: Any) -> ToolResult:
        """调用远端工具。**失败直接抛异常** —— 由上层决定降级到 mock。

        异常类型决定上层行为：
            CircuitOpenError    → 立刻降级，不重试（熔断已判定）
            TransientToolError  → 重试已用尽（见 with_retry），降级
            PermanentToolError  → 不重试，降级并保留原因
        """
        started = time.perf_counter()
        retries = {"n": 0}

        def on_retry(attempt: int, exc: BaseException, delay: float) -> None:
            retries["n"] += 1
            log.info("MCP %s 第 %d 次重试（%.0fms 后）：%s",
                     tool, attempt, delay * 1000, type(exc).__name__)

        async def once() -> Any:
            return await self._rpc(
                "tools/call", {"name": tool, "arguments": args}, timeout=self.timeout
            )

        try:
            guarded = await guarded_call(
                self.breaker, once, policy=self.retry, on_retry=on_retry
            )
        except CircuitOpenError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转成 ToolResult 由上层处理
            log.warning("MCP 工具 %s 失败（重试 %d 次）：%s: %s",
                        tool, retries["n"], type(exc).__name__, exc)
            raise

        elapsed = (time.perf_counter() - started) * 1000
        result = guarded.value

        if isinstance(result, dict) and result.get("isError"):
            # 协议成功、业务失败（例如参数不合法）：**不计入熔断**。
            # 把"下游健康但这次问错了"算成依赖故障，会让熔断器永远关不上。
            return ToolResult(
                tool=tool, ok=False, args=args, source=self.name,
                error=str(result.get("content") or "MCP 返回 isError"),
                text=f"[MCP 工具 {tool} 返回错误]", elapsed_ms=elapsed,
                attempts=guarded.attempts, breaker=guarded.breaker_state,
            )

        text, data = _parse_content(result)
        return ToolResult(
            tool=tool, ok=True, args=args, data=data, text=text,
            source=self.name, elapsed_ms=elapsed,
            attempts=guarded.attempts, breaker=guarded.breaker_state,
        )

    # ---------------- 传输 ----------------
    async def _rpc(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        """JSON-RPC 调用。**在这里做错误分类** —— 分类只该有一个地方。

            httpx 超时 / 连接失败       → TransientToolError（重试）
            HTTP 5xx                    → TransientToolError（重试）
            HTTP 4xx                    → PermanentToolError（不重试）
            JSON-RPC error 字段         → PermanentToolError（业务错误，不重试）
            响应不是 JSON 对象          → TransientToolError（可能被网关截断）
        """
        import httpx

        self._rpc_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": method,
            "params": params,
        }
        try:
            # trust_env=False：**内网服务不走系统代理**。
            # 踩过的坑：Windows 宿主机上配了 HTTP_PROXY，httpx 默认会读它，
            # 于是发往 127.0.0.1 / java-mcp 的请求也被送进代理 —— 表现为
            # "curl 能通、Python 客户端一直超时"，且日志里只有 TimeoutError，
            # 看不出是代理干的。容器内没代理，但宿主机自测/本机调试会撞上。
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                resp = await client.post(
                    f"{self.url}{self.endpoint}", json=payload,
                    headers={"X-Chaos": self.chaos} if self.chaos else None,
                )
        except httpx.TimeoutException as exc:
            raise TransientToolError(f"MCP 超时（{timeout}s）：{method}") from exc
        except httpx.HTTPError as exc:  # 连接失败 / 读超时 / 协议错误
            raise TransientToolError(f"MCP 连接失败：{type(exc).__name__}: {exc}") from exc

        if resp.status_code >= 500:
            raise TransientToolError(f"MCP 5xx（{resp.status_code}）：{method}")
        if resp.status_code >= 400:
            # 4xx 重试无意义：路径错、鉴权错、参数错，重试三次还是错
            raise PermanentToolError(f"MCP 4xx（{resp.status_code}）：{method}")

        try:
            body = resp.json()
        except ValueError as exc:
            raise TransientToolError(f"MCP 响应不是 JSON：{exc}") from exc

        if not isinstance(body, dict):
            raise TransientToolError(f"MCP 响应不是 JSON 对象：{type(body).__name__}")
        if "error" in body and body["error"]:
            err = body["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            # JSON-RPC 错误是"请求本身有问题"，重试同样无意义
            raise PermanentToolError(f"MCP 错误：{message}")
        return body.get("result")

    # ---------------- 注册 ----------------
    def install(self, registry: ToolRegistry) -> None:
        """注册**同一份 TOOL_SPECS**，把调用转发到远端。

        spec 与 mock 共用一份常量，所以"签名一致"不是靠自觉 ——
        D2 的 Java 侧只要照这份表实现，两边就不会漂移。
        """
        for spec in TOOL_SPECS:
            registry.register(spec, self._make_handler(spec.name))

    def _make_handler(self, tool_name: str):
        async def handler(**kwargs: Any) -> ToolResult:
            return await self.call(tool_name, **kwargs)

        handler.__name__ = f"mcp_{tool_name}"
        return handler


def _parse_content(result: Any) -> tuple[str, Any]:
    """解析 MCP 的 content 数组。

    MCP 规定的返回形状是 `{"content": [{"type": "text", "text": "..."}]}`。
    这里额外兼容 `structuredContent`（结构化数据优先，因为它能被后续代码消费，
    而文本只适合喂给模型）。
    """
    if not isinstance(result, dict):
        return str(result or ""), None

    data = result.get("structuredContent")
    content = result.get("content")

    texts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                texts.append(block)
    elif isinstance(content, str):
        texts.append(content)

    text = "\n".join(t for t in texts if t)
    if not text and data is not None:
        import json

        text = json.dumps(data, ensure_ascii=False)
    return text, data


def specs_for_prompt(specs: Sequence[ToolSpec] | None = None) -> str:
    """给 LLM 的工具清单（MCP 与 mock 用同一份，所以说明文字不会两套）。"""
    items = specs or TOOL_SPECS
    return "\n".join(f"- {s.signature()}：{s.description}" for s in items)

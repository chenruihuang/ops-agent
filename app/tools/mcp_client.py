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

log = logging.getLogger(__name__)

DEFAULT_MCP_TIMEOUT = 2.0
# 探测结果缓存时长。见模块 docstring 第 1 点。
PROBE_TTL = 30.0


class McpProvider:
    """JSON-RPC over HTTP 的 MCP 客户端。

    `RoutedToolSource`（见 app/tools/__init__.py）会先问 `available()`，
    拿不到才走 mock —— 所以这里不需要自己实现 fallback。
    """

    name = "mcp"

    def __init__(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_MCP_TIMEOUT,
        endpoint: str = "/mcp",
    ) -> None:
        self.url = (url or "").rstrip("/")
        self.timeout = timeout
        self.endpoint = endpoint
        self._probe_ok: bool | None = None
        self._probe_at: float = 0.0
        self._rpc_id = 0

    # ---------------- 探测 ----------------
    async def available(self) -> bool:
        """带 TTL 的能力探测。失败也缓存 —— 那正是最需要缓存的情况。"""
        now = time.monotonic()
        if self._probe_ok is not None and (now - self._probe_at) < PROBE_TTL:
            return self._probe_ok

        ok = False
        try:
            result = await self._rpc("tools/list", {}, timeout=self.timeout)
            ok = bool(result is not None)
        except Exception as exc:  # noqa: BLE001 - 探测失败是预期情况
            log.info("MCP 不可用（%s）：%s —— 本次将使用 mock 工具结果", self.url,
                     f"{type(exc).__name__}: {exc}")
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
        """调用远端工具。**失败直接抛异常** —— 由上层决定降级到 mock。"""
        started = time.perf_counter()
        result = await self._rpc(
            "tools/call", {"name": tool, "arguments": args}, timeout=self.timeout
        )
        elapsed = (time.perf_counter() - started) * 1000

        if isinstance(result, dict) and result.get("isError"):
            return ToolResult(
                tool=tool, ok=False, args=args, source=self.name,
                error=str(result.get("content") or "MCP 返回 isError"),
                text=f"[MCP 工具 {tool} 返回错误]", elapsed_ms=elapsed,
            )

        text, data = _parse_content(result)
        return ToolResult(
            tool=tool, ok=True, args=args, data=data, text=text,
            source=self.name, elapsed_ms=elapsed,
        )

    # ---------------- 传输 ----------------
    async def _rpc(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        import httpx

        self._rpc_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": method,
            "params": params,
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{self.url}{self.endpoint}", json=payload)
            resp.raise_for_status()
            body = resp.json()

        if not isinstance(body, dict):
            raise ValueError(f"MCP 响应不是 JSON 对象：{type(body).__name__}")
        if "error" in body and body["error"]:
            err = body["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise RuntimeError(f"MCP 错误：{message}")
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

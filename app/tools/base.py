"""工具层核心：声明式注册表 + 统一的调用契约。

为什么 RAG 之外还要工具
----------------------
"SRV-0088 现在什么状态"这句话，答案不在任何一篇文档里 —— 它在资产台账里。
把这类实时数据塞进向量库有两个后果：
  1. **数据永远过期**：入库那一刻的状态被冻结成向量，之后再也不更新
  2. **污染检索**：一堆"xx 服务当前正常"的句子会跟真正的手册内容抢 top-k

所以工具与检索的边界是**数据性质**，不是技术偏好：
    稳定知识（手册/规范/复盘）→ 检索
    实时状态（工单/资产/告警）→ 工具

统一调用契约
------------
`ToolRegistry.call()` **永不抛异常**。任何失败（工具不存在、参数缺失、超时、
下游 500）都收敛成一个 `ok=False` 的 `ToolResult`。

理由：工具失败必须降级成"这条信息拿不到"，而不是把整个问答打断。
Java 侧的经验在这里直接迁移：外部依赖的异常不能穿透到业务主流程。
`ok=False` 的 ToolResult 会进上下文，让 LLM 明确说"工单系统暂时查不到"，
而不是编一个工单状态出来 —— 后者才是最糟的失败方式。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

log = logging.getLogger(__name__)

# 单个工具的超时上限。取 3s 的理由：工具是"辅助信息"，
# 它拖慢主链路的时间不该超过一次向量检索（实测同量级）。
# 超时就降级成 ok=False，让生成继续，而不是让用户等。
DEFAULT_TOOL_TIMEOUT = 3.0


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """工具声明。故意不做成 JSON Schema —— 参数只有 1~2 个且全是字符串，
    上 schema 只会让"加一个工具"从 5 行变成 30 行。"""

    name: str
    description: str
    params: Mapping[str, str] = field(default_factory=dict)
    required: tuple[str, ...] = ()

    def signature(self) -> str:
        args = ", ".join(self.params) or ""
        return f"{self.name}({args})"


@dataclass(slots=True)
class ToolResult:
    """工具调用结果。`source` 字段是**诚实性要求**，不是装饰：

    演示时如果 MCP Server 没起、数据来自 mock，事件流里必须能看出来。
    把 mock 数据当实时数据讲，是这类项目最容易翻车的地方。
    """

    tool: str
    ok: bool
    args: dict[str, Any] = field(default_factory=dict)
    data: Any = None
    text: str = ""
    source: str = "unknown"        # mock | mcp | cache | none
    elapsed_ms: float = 0.0
    error: str = ""

    @property
    def empty(self) -> bool:
        return self.ok and not self.data

    def as_event(self) -> dict[str, Any]:
        """SSE 事件负载。`preview` 截断到一行，避免把大 JSON 灌进事件流。"""
        preview = (self.text or "").replace("\n", " ").strip()
        return {
            "type": "tool_result",
            "tool": self.tool,
            "ok": self.ok,
            "source": self.source,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "records": len(self.data) if isinstance(self.data, (list, tuple)) else int(bool(self.data)),
            "preview": preview[:120],
            "error": self.error,
        }


Handler = Callable[..., Awaitable[ToolResult]]


class ToolRegistry:
    """工具注册表。

    调用方只依赖 `names()` / `specs()` / `call()`，
    因此**换 provider（mock → MCP）不需要改上层任何代码**。
    """

    def __init__(self, *, timeout: float = DEFAULT_TOOL_TIMEOUT) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, Handler] = {}
        self.timeout = timeout
        self.source = "unknown"

    # ---------------- 注册 ----------------
    def register(self, spec: ToolSpec, handler: Handler) -> None:
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def has(self, name: str) -> bool:
        return name in self._handlers

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[ToolSpec]:
        return [self._specs[n] for n in self.names()]

    def describe(self) -> str:
        """给 LLM 看的工具清单。放在 prompt 里让模型知道有哪些能力可用。"""
        return "\n".join(f"- {s.signature()}：{s.description}" for s in self.specs())

    # ---------------- 调用 ----------------
    async def call(self, name: str, **args: Any) -> ToolResult:
        """**永不抛异常** —— 见模块 docstring。"""
        started = time.perf_counter()

        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(
                tool=name, ok=False, args=args, source="none",
                error=f"未注册的工具：{name}", text=f"[工具 {name} 不存在]",
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )

        try:
            result = await asyncio.wait_for(handler(**args), timeout=self.timeout)
        except asyncio.TimeoutError:
            elapsed = (time.perf_counter() - started) * 1000
            log.warning("工具 %s 超时（%.1fs）", name, self.timeout)
            return ToolResult(
                tool=name, ok=False, args=args, source=self.source,
                error=f"超时（>{self.timeout}s）",
                text=f"[工具 {name} 调用超时，未取得数据]",
                elapsed_ms=elapsed,
            )
        except TypeError as exc:
            # 参数不匹配 —— 通常是 planner 传错了参数名，属于代码缺陷，
            # 但要降级而不是 500，同时用 warning 让它可见
            log.warning("工具 %s 参数错误：%s", name, exc)
            return ToolResult(
                tool=name, ok=False, args=args, source=self.source,
                error=f"参数错误：{exc}", text=f"[工具 {name} 参数错误]",
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:  # noqa: BLE001 - 任何下游异常都降级
            log.warning("工具 %s 调用失败：%s: %s", name, type(exc).__name__, exc)
            return ToolResult(
                tool=name, ok=False, args=args, source=self.source,
                error=f"{type(exc).__name__}: {exc}",
                text=f"[工具 {name} 暂时不可用，未取得数据]",
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )

        if result.elapsed_ms <= 0:
            result.elapsed_ms = (time.perf_counter() - started) * 1000
        if not result.source or result.source == "unknown":
            result.source = self.source
        return result


def render_tool_context(results: Sequence[ToolResult]) -> str:
    """把工具结果渲染成给 LLM 的上下文块。

    失败的调用**也要写进去**，内容显式说明"没查到"。
    如果直接丢掉，模型看不到任何约束，就会拿知识库内容硬答实时问题 ——
    那正是这套设计最想避免的失败模式。
    """
    parts: list[str] = []
    for r in results:
        if r.ok:
            parts.append(f"[工具 {r.tool} · 来源 {r.source}]\n{r.text}")
        else:
            parts.append(f"[工具 {r.tool} · 调用失败]\n{r.text}（原因：{r.error}）")
    return "\n\n".join(parts)

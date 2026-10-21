"""LangGraph 编排：核心问答链路的状态机。

为什么换成 LangGraph（以及为什么**只**换编排）
----------------------------------------------
旧的实现是 chat.py 里的一个手写 async generator：
`generator()` 按顺序做路由 → 缓存 → 分支 → 生成，状态全靠函数参数
在调用栈里传。链路短的时候这样最简单；但往上叠三层记忆、
断点续跑、工具人工确认时，这个 generator 会膨胀成难以维护的隐式状态机。
LangGraph 把「状态」和「节点」显式化，换来的新能力：
checkpointer（持久化/断点续跑）、interrupt（工具调用人工确认）、
时间旅行调试。本次只落前两项的**地基**（state + graph 结构），
checkpointer 接 Redis 是 backlog（见 README）。

与旧实现的等价性契约（这是重构的验收标准）
------------------------------------------
**SSE 事件协议一字不改**：meta → route → tool_call* → tool_result* →
docs → token* → (degraded/error) → done → [DONE]。
tests/test_chat_sse.py 的全部用例不改断言、只改 patch 目标 ——
事件顺序错了前端不会报错，只会显示得不对。

图结构（对照 chat.py 旧 docstring 那张图）
------------------------------------------

    START → route ──chitchat──▶ chitchat → END
              │ main
              ▼
         memory_load → cache_exact ──exact 命中──▶ END
              │                │  （exact 命中不付编码费 —— 见 node_cache_exact）
              │               miss
              │                ▼
              │              embed → cache_semantic ──semantic 命中──▶ END
              │                （BGE 编码只由「未命中」的请求支付，
              │                 编码结果同时供语义缓存与向量召回共用）
              │                │ miss
              │                ▼
              │          singleflight ──follower 命中──▶ END
              │                │ leader
              │                ▼
              │             dispatch ──┬──▶ tools ──┐
              │                        └──▶ recall ─┴─▶ tools_done ──┬──▶ generate → END
              │                                                       └──(有挂起)──▶ await_confirm → END

节点内的流式输出走 `stream_mode="custom"`（`get_stream_writer`）：
token 是逐段产生的，节点不能等整段生成完再返回 ——
writer 让节点在执行中途把帧推给外层 SSE 生成器。
"""

from __future__ import annotations

import asyncio
import json
import logging
import operator
import time
from typing import Annotated, Any, AsyncIterator, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.cache.manager import CachedAnswer
from app.core.config import settings
from app.memory.facts import extract_facts
from app.memory.session import render_memory_context
from app.observability.metrics import (
    DEGRADED_TOTAL,
    STAGE_SECONDS,
    TTFT_SECONDS,
)
from app.observability.tracing import TurnTrace, null_span
from app.pipeline import intent as intent_mod
from app.pipeline import rag
from app.pipeline.embedding import embed_one
from app.pipeline.fusion import cap_per_unit
from app.tools import ToolRegistry, entities_of, execute_skill, plan
from app.tools.base import ToolResult, render_tool_context
from app.tools.sandbox import call_context

log = logging.getLogger(__name__)

# SSE 心跳间隔。防中间代理（nginx 默认 60s）在检索/重排静默期断连。
KEEPALIVE_SECONDS = 15.0

CHITCHAT_REPLY = (
    "我是企业 IT 运维知识助手。可以帮你做三件事：\n"
    "1. 查处置手册与规范：报错码、阈值参数、故障复盘、操作步骤\n"
    "2. 查实时数据：工单进度、资产状态、服务当前告警\n"
    "3. 两件事一起做：比如「连接数超过多少告警」+「相关工单处理到哪了」\n\n"
    "直接问就行，比如「ORD-429-001 是什么含义」。"
)


# =====================================================================
# SSE 编码（从 chat.py 迁入：帧编码只有编排器该用，端点只透传）
# =====================================================================
def sse_frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def ping_frame() -> str:
    """SSE 注释帧。客户端 onmessage 收不到，但字节实时流过去 —— 保活。"""
    return ": keepalive\n\n"


def split_text(text: str, size: int = 24) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


class StageTimer:
    """分阶段耗时。TTFT 劣化时只有 stage_ms 能定位是哪段慢了。"""

    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.marks: dict[str, float] = {}

    def mark(self, stage: str) -> None:
        self.marks[stage] = round((time.perf_counter() - self.t0) * 1000, 1)

    def summary(self) -> dict[str, float]:
        return dict(self.marks)


# =====================================================================
# 追踪辅助
# =====================================================================
# 埋点为什么写成三个小函数、而不是在每个节点里 `if state["trace"]:`：
# 漏埋是**静默**的 —— trace 里少一段，没有任何报警，等发现时已经过去两周。
# 把"可能没有 trace"这件事收敛到一处，节点里就只剩一条必执行语句。
def _seg(state: AgentState, stage: str, **kwargs: Any):
    trace = state.get("trace")
    return trace.stage(stage, **kwargs) if trace is not None else null_span()


def _mark_ttft(state: AgentState, ms: float | None = None) -> None:
    trace = state.get("trace")
    if trace is not None:
        trace.mark_ttft(ms)


def _trace_fields(state: AgentState) -> dict[str, Any]:
    """meta 帧里的 `trace_id`。

    没有它，trace 就是个黑盒：用户看到一次慢请求，却不知道该用哪个 id
    去回放 / 去 Langfuse 里搜。加在 meta 上是**新增字段**，老前端不会坏
    （不认识的键自然被忽略），但它把"链路追踪"从后台能力变成了可见入口。
    """
    tid = getattr(state.get("trace"), "trace_id", "") or ""
    return {"trace_id": tid} if tid else {}


def _finish_trace(
    state: AgentState,
    *,
    answer: str,
    route: str,
    cache: str,
    degraded: list[str] | None = None,
    status: str = "ok",
    **extra: Any,
) -> None:
    """五个终局（闲聊 / 缓存命中 / 单飞跟随 / 生成 / 挂起待确认）各调一次。

    `finish` 自身幂等（`_closed` 守卫），所以 chat.py 在异常兜底里再调一次
    不会写重复记录 —— 那条路径专门用于"图崩了但 trace 还没收尾"。
    """
    trace = state.get("trace")
    if trace is not None:
        trace.finish(
            answer=answer, route=route, cache=cache,
            degraded=degraded, status=status, **extra,
        )


async def run_with_pings(coro, *, interval: float = KEEPALIVE_SECONDS):
    """执行协程，期间按间隔产出心跳（见旧实现的 asyncio.wait 说明）。"""
    task = asyncio.ensure_future(coro)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval)
            if done:
                break
            yield "ping", None
        yield "result", task.result()
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


# =====================================================================
# 状态定义 —— ① 工作记忆的载体
# =====================================================================
class AgentState(TypedDict):
    """单次请求的全部中间状态。

    `degraded` 用 `operator.add` reducer：tools / recall / memory 三处
    **并行**产出降级原因，LangGraph 靠 reducer 把它们合并 ——
    旧实现里这是靠一个手工传递的 list 拼起来的。
    `cache` / `registry` / `memory` 是服务句柄（请求级依赖），
    与数据同放在 state 里随图流动，节点不再各自从全局取。
    """

    # ---- 请求入参 ----
    message: str
    session_id: str | None
    use_cache: bool
    kb_version: int
    t0: float
    timer: StageTimer

    # ---- 服务句柄 ----
    cache: Any | None
    registry: ToolRegistry | None
    memory: Any | None
    # 追踪句柄（请求级）。与 timer 一样随 state 流动 —— 节点不各自取全局，
    # 测试才能注入假的。`None` 时由 `_seg()` 换成 no-op，不影响任何分支。
    trace: TurnTrace | None

    # ---- 中间产物 ----
    decision: intent_mod.RouteDecision | None
    embedding: list[float] | None
    hit: CachedAnswer | None           # 两级缓存命中
    waited: CachedAnswer | None        # singleflight 跟随者拿到的结果
    cache_error: str | None            # exact 段的缓存异常，由 semantic 段继承后统一发降级帧
    leader: bool
    chunks: list[rag.Chunk]
    recall_meta: dict[str, Any]
    tool_context: str
    results: list[ToolResult]
    memory_context: str                # ②③ 层记忆渲染出的注入文本
    memory_stats: dict[str, int]

    # ---- 汇聚 ----
    degraded: Annotated[list[str], operator.add]
    answer: str

    # ---- 安全沙箱 ----
    # 本轮被挂起、等待人工确认的动作（已构造好的 SSE 事件体）。
    # 非空时**不生成答案**，直接走 await_confirm 终局 ——
    # 见 node_await_confirm 与 app/tools/sandbox.py 的模块说明。
    pending: list[dict[str, Any]]


# =====================================================================
# 记忆辅助
# =====================================================================
def _memory_degraded_events(state: AgentState) -> list[str]:
    """从 state.degraded 里挑出记忆相关的条目（load 阶段产生的）。"""
    return [d for d in (state.get("degraded") or []) if d.startswith("memory:")]


def _emit_memory_degraded(writer, state: AgentState) -> None:
    """记忆降级必须可见 —— 但 meta 必须仍是第一个事件，
    所以记忆降级事件由携带 meta 的节点在 meta 之后补发。"""
    for d in _memory_degraded_events(state):
        DEGRADED_TOTAL.labels(stage="memory", reason=d.split(":", 1)[1]).inc()
        writer(sse_frame({
            "type": "degraded",
            "stage": "memory",
            "reason": d.split(":", 1)[1],
            "message": "会话记忆暂不可用，本轮回答不带历史上下文",
        }))


def _emit_save_degraded(writer, entries: list[str]) -> None:
    """保存失败（写历史/事实时断连）同样可见。必须在 done **之前**发：
    前端以 done 收尾，done 之后的事件帧会被丢弃。"""
    for d in entries:
        if not d.startswith("memory:"):
            continue
        DEGRADED_TOTAL.labels(stage="memory", reason=d.split(":", 1)[1]).inc()
        writer(sse_frame({
            "type": "degraded",
            "stage": "memory",
            "reason": d.split(":", 1)[1],
            "message": "会话记忆写入失败，本轮对话不会被记住",
        }))


async def _save_turn(state: AgentState, answer_text: str) -> list[str]:
    """终局节点共用的"记一轮"：写会话历史 + 抽长期事实。

    抽取发生在**保存时**而不是加载时：本轮消息里的稳定属性
    从下一轮开始生效 —— 本轮的问题文本 LLM 本来就看得到。
    返回 degraded 条目（记忆失败时），由调用方并入自己的 state 更新。
    """
    memory = state.get("memory")
    sid = state.get("session_id")
    if memory is None or not sid:
        return []
    span = _seg(state, "memory_save", facts=None)
    try:
        facts = extract_facts(state["message"])
        span.add_meta(facts=len(facts))
        err = await memory.save_turn(
            sid,
            question=state["message"],
            answer=answer_text,
            route=(state.get("decision").intent if state.get("decision") else "unknown"),
            facts=facts,
        )
        if err:
            span.set(level="WARNING", status_message=err)
        return [err] if err else []
    except Exception as exc:  # noqa: BLE001 - 保存失败绝不影响收尾帧
        log.warning("save_turn failed: %s", exc)
        span.set(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
        return [f"memory:{type(exc).__name__}"]
    finally:
        span.end()


def _stream_cached_frames(hit: CachedAnswer, t0: float, state: AgentState | None = None):
    """缓存命中路径的帧序列（与旧 `_stream_cached` 逐帧一致）。

    TTFT 在**第一片**处测：缓存命中的看点就是"首字快"，若把整段推完才记，
    这个路径相对真实生成的唯一优势就被抹掉了。
    """
    first = True
    for piece in split_text(hit.answer):
        if first:
            TTFT_SECONDS.labels(cache=hit.source, route=hit.route).observe(
                time.perf_counter() - t0
            )
            if state is not None:
                _mark_ttft(state)
            first = False
        yield sse_frame({"type": "token", "content": piece})
    yield sse_frame({
        "type": "done",
        "cache": hit.source,
        "route": hit.route,
        "similarity": round(hit.similarity, 4),
        "total_ms": round((time.perf_counter() - t0) * 1000, 1),
        "tokens_saved": hit.tokens_saved,
    })
    yield "data: [DONE]\n\n"


# =====================================================================
# 召回（含两层降级，逻辑与旧 `_recall` 一致）
# =====================================================================
def _no_rerank_order(chunks: list[rag.Chunk]) -> list[rag.Chunk]:
    """不用重排时的取上下文顺序：先保证文档多样性，再截断。顺序不能反。"""
    return cap_per_unit(
        chunks,
        lambda c: c.doc_id,
        max_per_unit=int(getattr(settings, "rerank_max_per_doc", 2)),
    )[: settings.top_k_rerank]


async def _recall(
    question: str, embedding: list[float] | None, state: AgentState | None = None
) -> tuple[list[rag.Chunk], dict[str, Any], list[str]]:
    """召回 + 精排。两层降级：召回整体失败 → 空结果 + degraded；
    重排失败 / 被显式关闭 → 融合序（同一条 `_no_rerank_order` 路径）。

    重排单独一个子 span：它是本链路最贵的一环（CPU 上候选池 20 时单题约 29.6s），
    而"召回慢"和"重排慢"在对外表现上完全一样（都是首字慢）。
    不拆开埋点，TTFT 劣化时就只能靠猜。
    """
    degraded: list[str] = []
    try:
        outcome = await rag.recall_detailed(question, embedding=embedding)
    except Exception as exc:  # noqa: BLE001
        log.exception("recall failed entirely: %s", exc)
        DEGRADED_TOTAL.labels(stage="recall", reason="exception").inc()
        return [], {"channels": {}, "fused": False}, [f"recall:{type(exc).__name__}"]

    rerank_mode = "enabled"
    if not getattr(settings, "rerank_enabled", True):
        # 关闭重排是**配置事实**，不是"通道不可用"：走融合序，答案依据并不残缺。
        # 曾经把它 append 进 degraded，后果是整个降级口径作废 ——
        # 每次缓存未命中都恰好产出 1 条降级帧，于是压测里
        # "降级 72 次" 与 "miss 72 次" 永远相等，指标看起来有数、其实恒等于未命中数。
        # 现在改成 meta 里的模式标记 + 一条 `notice` 事件：可见，但不算故障。
        rerank_mode = "disabled"
        top = _no_rerank_order(outcome.chunks)
    else:
        span = _seg(
            state, "rerank",
            parent="recall",
            input={"question": question[:200]},
            pool=len(outcome.chunks),
            top_k=settings.top_k_rerank,
        )
        try:
            top = await rag.rerank_chunks(question, outcome.chunks)
            span.set(output={"pool": len(outcome.chunks), "kept": len(top)})
        except Exception as exc:  # noqa: BLE001
            log.warning("rerank failed: %s", exc)
            DEGRADED_TOTAL.labels(stage="rerank", reason="exception").inc()
            degraded.append(f"rerank:{type(exc).__name__}")
            span.set(level="WARNING", status_message=f"rerank 降级为融合序：{type(exc).__name__}")
            top = _no_rerank_order(outcome.chunks)
        finally:
            span.end()

    meta: dict[str, Any] = {"channels": outcome.channels, "fused": outcome.fused,
                            "rerank": rerank_mode}
    degraded.extend(outcome.degraded)
    if degraded:
        meta["degraded"] = degraded
    return top, meta, degraded


async def _execute_calls(
    registry: ToolRegistry, calls: list, state: AgentState | None = None
) -> list[ToolResult]:
    """并发执行工具计划。结果顺序与计划顺序一致（gather 保序）。

    每个工具一个 span，**在各自的 task 里计时** —— 不能等 gather 回来再补：
    `ToolResult.elapsed_ms` 是 call 内部测的，而并发时"整体墙钟"与
    "单个工具耗时"是两个不同的数，用后者冒充前者会在排障时指错方向。
    """

    async def one(call) -> ToolResult:
        span = _seg(state, f"tool:{call.name}", as_type="tool",
                    parent="tools", input=call.args)
        try:
            result = await registry.call(call.name, **call.args)
            span.set(
                output={
                    "ok": result.ok,
                    "source": result.source,
                    "elapsed_ms": round(result.elapsed_ms, 1),
                    "chars": len(result.text or ""),
                },
                level="DEFAULT" if result.ok else "WARNING",
                status_message=None if result.ok else (result.error or "failed"),
            )
            return result
        except Exception as exc:  # noqa: BLE001 - registry 契约是不抛，防御性兜底
            span.set(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            span.end()

    return list(await asyncio.gather(*[one(c) for c in calls]))


# =====================================================================
# 节点
# =====================================================================
async def node_route(state: AgentState) -> dict[str, Any]:
    """① 意图路由：纯规则、零 IO。只算不发事件 ——
    meta 必须是第一个事件，而 meta 要等缓存查询结果。"""
    with _seg(state, "route", input={"message": state["message"][:200]}) as span:
        decision = intent_mod.decide(state["message"])
        span.add_meta(confidence=decision.confidence, reason=decision.reason)
        span.set(output={
            "intent": decision.intent,
            "signals": decision.signals,
            "needs_rag": decision.needs_rag,
            "needs_tool": decision.needs_tool,
        })
    state["timer"].mark("route")
    log.info(
        "路由：%s（conf=%.2f, signals=%s）| %s",
        decision.intent, decision.confidence, ",".join(decision.signals),
        state["message"][:60],
    )
    return {"decision": decision}


def _after_route(state: AgentState) -> str:
    if state["decision"].intent == "chitchat":
        return "chitchat"
    return "memory_load"


async def node_chitchat(state: AgentState) -> dict[str, Any]:
    """闲聊：模板直答，不走 LLM、不检索、不向量化、不进缓存。

    闲聊也记会话历史：连续对话里"那你详细说说第 2 点"这种追问
    需要知道上一轮聊了什么 —— 哪怕上一轮是闲聊。
    """
    w = get_stream_writer()
    timer = state["timer"]
    t0 = state["t0"]
    decision = state["decision"]
    span = _seg(state, "chitchat", parent="route", input={"message": state["message"][:200]})
    # 刻意不进缓存：闲聊答案没有知识价值，写进语义索引只会污染 KNN 候选。
    w(sse_frame({"type": "meta", "cache": "bypass", "route": "chitchat",
                 "kb_version": state["kb_version"], **_trace_fields(state)}))
    w(sse_frame(decision.as_event()))
    _emit_memory_degraded(w, state)
    TTFT_SECONDS.labels(cache="miss", route="chitchat").observe(time.perf_counter() - t0)
    _mark_ttft(state)
    for piece in split_text(CHITCHAT_REPLY):
        w(sse_frame({"type": "token", "content": piece}))
    timer.mark("chitchat")
    degraded = await _save_turn(state, CHITCHAT_REPLY)
    _emit_save_degraded(w, degraded)
    w(sse_frame({
        "type": "done", "cache": "bypass", "route": "chitchat",
        "stage_ms": timer.summary(),
        "total_ms": round((time.perf_counter() - t0) * 1000, 1),
    }))
    w("data: [DONE]\n\n")
    span.set(output={"chars": len(CHITCHAT_REPLY), "template": True})
    span.end()
    _finish_trace(state, answer=CHITCHAT_REPLY, route="chitchat",
                  cache="bypass", degraded=degraded)
    return {"answer": CHITCHAT_REPLY, "degraded": degraded}


async def node_memory_load(state: AgentState) -> dict[str, Any]:
    """②③ 层记忆加载：会话历史 + 长期事实 → 渲染成注入文本。

    失败不抛：记进 degraded（`memory:<Err>`），由携带 meta 的节点补发
    降级事件 —— 记忆降级必须可见，但不能抢在 meta 前面。
    """
    memory = state.get("memory")
    sid = state.get("session_id")
    if memory is None or not sid:
        return {"memory_context": "", "memory_stats": {}}
    with _seg(state, "memory_load", parent="route") as span:
        try:
            ctx = await memory.load(sid)
        except Exception as exc:  # noqa: BLE001 - 防御性兜底：store 契约是"不抛"，
            # 但万一实现违约，也不能让一个锦上添花的依赖炸掉整条链路。
            log.warning("memory.load raised（store 违约，按降级处理）: %s", exc)
            span.set(level="ERROR", status_message=f"store 违约：{type(exc).__name__}")
            return {"memory_context": "", "memory_stats": {},
                    "degraded": [f"memory:{type(exc).__name__}"]}
        stats = {"history_turns": len(ctx.get("history", [])),
                 "facts": len(ctx.get("facts", []))}
        update: dict[str, Any] = {
            "memory_context": render_memory_context(ctx),
            "memory_stats": stats,
        }
        span.add_meta(**stats)
        span.set(output=stats)
        err = getattr(memory, "last_error", None)
        if err:
            update["degraded"] = [f"memory:{err}"]
            span.set(level="WARNING", status_message=err)
        return update


async def node_embed(state: AgentState) -> dict[str, Any]:
    """② 向量化，一次算完（缓存与检索共用）。失败只降级不抛 ——
    embedding 挂了 = 语义缓存与向量召回不可用，BM25 路还在。"""
    embedding: list[float] | None = None
    span = _seg(state, "embed", as_type="embedding", parent="route",
                model=settings.embedding_model)
    try:
        with STAGE_SECONDS.labels(stage="embed_query").time():
            embedding = await embed_one(state["message"])
        span.set(output={"dim": len(embedding)})
    except Exception as exc:  # noqa: BLE001
        log.warning("embedding failed, semantic cache & vector recall disabled: %s", exc)
        DEGRADED_TOTAL.labels(stage="embed", reason="exception").inc()
        # embedding 挂了 = 语义缓存与向量召回不可用，BM25 路还在 ——
        # 这一条在 trace 里必须是 WARNING，否则"答案变差"会被当成模型问题。
        span.set(level="WARNING", status_message=f"{type(exc).__name__}: {exc}")
    finally:
        span.end()
    state["timer"].mark("embed")
    return {"embedding": embedding}


async def _finish_cache_hit(state: AgentState, hit: CachedAnswer) -> dict[str, Any]:
    """缓存命中（exact / semantic）共用的收尾：meta → route → 存记忆 → 回放 → done。

    抽成一个函数的原因与 `_stream_cached_frames` 相同：两条命中路径若各写一份，
    "这边带了 similarity、那边忘带" 的漂移迟早发生，而前端只会静默少一个字段。
    """
    w = get_stream_writer()
    timer = state["timer"]
    t0 = state["t0"]
    decision = state["decision"]
    # meta 里报的 similarity 与 trace 里记的是同一个值 —— 否则
    # 用户看到 0.97、trace 里 0.9688，会以为有两个相似度
    w(sse_frame({
        "type": "meta",
        "cache": hit.source,
        "route": hit.route or decision.intent,
        # similarity 必须报：语义命中的看点就是"改写了还是命中"。
        "similarity": round(hit.similarity, 4),
        "kb_version": state["kb_version"],
        **({"memory": state["memory_stats"]} if state.get("memory_stats") else {}),
        **_trace_fields(state),
    }))
    # 缓存命中也推 route 事件：用户能看到"本来会走哪条路"。
    w(sse_frame(decision.as_event()))
    _emit_memory_degraded(w, state)
    # 保存先行：degraded 帧必须在 done 之前发（done 之后前端不再消费）
    degraded = await _save_turn(state, hit.answer)
    _emit_save_degraded(w, degraded)
    for frame in _stream_cached_frames(hit, t0, state):
        w(frame)
    _finish_trace(state, answer=hit.answer, route=hit.route or decision.intent,
                  cache=hit.source, degraded=degraded,
                  similarity=round(hit.similarity, 4))
    return {"hit": hit, "answer": hit.answer, "degraded": degraded}


async def node_cache_exact(state: AgentState) -> dict[str, Any]:
    """缓存第 1 段：**只查 exact**，不付编码费。

    为什么把缓存拆成两段（这是 D4 性能重构的核心）
    ------------------------------------------------
    旧图是 `embed → cache_lookup`，于是 **exact 命中也要先付一次 BGE 编码**
    （实测 stage p50 `embed_query 6.33s`）——缓存命中"首字快"的卖点被编码吃干，
    压测里 TTFT[hot] ≈ TTFT[cold]。现在 exact 查询只需要一个 Redis GET，
    把它挪到编码**之前**：命中的请求根本不进 CPU 队列。

    - exact 命中 → 本节点内推完 meta/token/done，条件边进 END（全程无编码）；
    - miss → **静默返回**（一个帧都不发！meta 由下游 `node_cache_semantic` 发 ——
      因为"最终命中没有"要等语义查询结束才知道，meta 必须带着准确结论当第一帧）；
      编码留给 embed 节点，其结果同时供语义缓存与向量召回共用 —— 一次编码两处受益。

    缓存查询异常按 miss 处理并降级（旧实现这里没兜，异常会直接断流）。
    """
    timer = state["timer"]
    decision = state["decision"]
    cache = state.get("cache")
    span = _seg(state, "cache_exact", parent="route",
                use_cache=state["use_cache"], cache_available=cache is not None)

    hit: CachedAnswer | None = None
    cache_error: str | None = None
    if state["use_cache"] and cache is not None:
        try:
            # embedding=None → manager.lookup 只走 exact 段（见 RedisCache.lookup）
            hit = await cache.lookup(state["message"], embedding=None)
            if hit.source == "miss":
                hit = None
        except Exception as exc:  # noqa: BLE001
            log.warning("exact cache lookup failed（按 miss 处理）: %s", exc)
            DEGRADED_TOTAL.labels(stage="cache", reason="exception").inc()
            cache_error = type(exc).__name__
            hit = None
    timer.mark("cache_exact")
    span.add_meta(lookup_ms=timer.summary().get("cache_exact"))

    if hit is not None:
        span.set(output={"hit": hit.source, "similarity": round(hit.similarity, 4),
                         "cached_route": hit.route, "tokens_saved": hit.tokens_saved})
        span.end()
        return await _finish_cache_hit(state, hit)

    span.set(output={"hit": "miss", "error": cache_error})
    if cache_error:
        span.set(level="WARNING", status_message=f"缓存查询异常，按 miss 处理：{cache_error}")
    span.end()
    # 静默 miss：帧与降级提示都由 node_cache_semantic 统一发（它才是 meta 的发出者）。
    # exact 段的异常记进 state，由 semantic 段继承 —— 否则"Redis 已经挂了"这条
    # 降级会被 exact 段的静默吞掉。
    return {"hit": None, "cache_error": cache_error}


def _after_cache_exact(state: AgentState) -> str:
    return "embed" if state.get("hit") is None else END


async def node_cache_semantic(state: AgentState) -> dict[str, Any]:
    """缓存第 2 段：语义查询 + meta/route 事件的统一发出点。

    meta 为什么在这里而不是 cache_exact 发：meta 的 `cache` 字段必须报告
    **最终**结论（exact/semantic/miss）。exact 命中在上一节点就终局了，
    自己发自己的 meta；走到这里说明 exact 未命中，结论要等语义查询结束——
    所以 meta 挪到本节点，保证它仍是第一帧且内容准确。
    """
    w = get_stream_writer()
    timer = state["timer"]
    decision = state["decision"]
    cache = state.get("cache")
    span = _seg(state, "cache_lookup", parent="route",
                use_cache=state["use_cache"], cache_available=cache is not None)

    hit: CachedAnswer | None = None
    cache_error = state.get("cache_error")   # 继承 exact 段的异常（Redis 挂了的证据）
    embedding = state.get("embedding")
    if state["use_cache"] and cache is not None and embedding is not None:
        try:
            hit = await cache.get_semantic(state["message"], embedding)
        except Exception as exc:  # noqa: BLE001
            log.warning("semantic cache lookup failed（按 miss 处理）: %s", exc)
            DEGRADED_TOTAL.labels(stage="cache", reason="exception").inc()
            cache_error = type(exc).__name__
            hit = None
    timer.mark("cache_lookup")
    span.add_meta(lookup_ms=timer.summary().get("cache_lookup"))

    if hit is not None:
        span.set(output={"hit": hit.source, "similarity": round(hit.similarity, 4),
                         "cached_route": hit.route, "tokens_saved": hit.tokens_saved})
        span.end()
        return await _finish_cache_hit(state, hit)

    span.set(output={"hit": "miss", "error": cache_error})
    if cache_error:
        span.set(level="WARNING", status_message=f"缓存查询异常，按 miss 处理：{cache_error}")
    span.end()
    w(sse_frame({
        "type": "meta", "cache": "miss", "route": decision.intent,
        "kb_version": state["kb_version"],
        **({"memory": state["memory_stats"]} if state.get("memory_stats") else {}),
        **_trace_fields(state),
    }))
    w(sse_frame(decision.as_event()))
    if cache_error:
        w(sse_frame({
            "type": "degraded", "stage": "cache", "reason": cache_error,
            "message": "缓存查询异常，本次按未命中处理",
        }))
    _emit_memory_degraded(w, state)
    return {"hit": None}


def _after_cache_semantic(state: AgentState) -> str:
    return "singleflight" if state.get("hit") is None else END


async def node_singleflight(state: AgentState) -> dict[str, Any]:
    """⑤ 单飞：抢到锁走完整链路；没抢到等领跑者结果。

    跟随者命中 → 节点内推完缓存流并 END；领跑者 → dispatch。
    `leader=True` 的释放责任在 node_generate 的 finally ——
    它是主链路的最后一个节点，无论生成成败都能释放。
    """
    w = get_stream_writer()
    t0 = state["t0"]
    cache = state.get("cache")
    decision = state["decision"]
    span = _seg(state, "singleflight", parent="route",
                use_cache=state["use_cache"], cache_available=cache is not None)

    leader = True
    if state["use_cache"] and cache is not None:
        waited_since = time.perf_counter()
        leader = await cache.acquire_singleflight(state["message"])
        if not leader:
            waited = await cache.wait_for_result(
                state["message"], embedding=state.get("embedding")
            )
            span.add_meta(waited_ms=round((time.perf_counter() - waited_since) * 1000, 1))
            if waited is not None:
                span.set(output={"leader": False, "adopted": True,
                                 "cache": "singleflight"})
                span.end()
                w(sse_frame({
                    "type": "meta",
                    "cache": "singleflight",
                    "route": waited.route or decision.intent,
                    "kb_version": state["kb_version"],
                    **_trace_fields(state),
                }))
                degraded = await _save_turn(state, waited.answer)
                _emit_save_degraded(w, degraded)
                for frame in _stream_cached_frames(waited, t0, state):
                    w(frame)
                _finish_trace(state, answer=waited.answer,
                              route=waited.route or decision.intent,
                              cache="singleflight", degraded=degraded,
                              singleflight="follower")
                return {"waited": waited, "leader": False,
                        "answer": waited.answer, "degraded": degraded}
    span.set(output={"leader": leader, "adopted": False})
    span.end()
    return {"leader": leader, "waited": None}


def _after_singleflight(state: AgentState) -> str:
    return "dispatch" if state.get("waited") is None else END


async def node_dispatch(state: AgentState) -> dict[str, Any]:
    """扇出中转：tools ∥ recall 并行（hybrid 的关键 —— 两路无依赖，
    顺序执行等于把耗时相加）。"""
    return {}


def _emit_pending(writer, results: list[ToolResult]) -> list[dict[str, Any]]:
    """把挂起的动作推成 `confirm_required` 帧，并返回已发出的负载。

    为什么单独一帧、而不是复用 tool_result：这两件事对前端的语义完全不同 ——
    `tool_result` 是"查到了什么"，`confirm_required` 是"请你决策"。
    挤在一个事件里，前端只能靠 `status` 字段分支，而这个字段一旦漏判，
    用户看到的就是一条"调用失败"的提示，然后以为系统坏了。
    """
    out: list[dict[str, Any]] = []
    for r in results:
        if not r.needs_confirm:
            continue
        payload = dict(r.confirm or {})
        payload.setdefault("type", "confirm_required")
        payload.setdefault("tool", r.tool)
        out.append(payload)
        writer(sse_frame(payload))
    return out


async def node_tools(state: AgentState) -> dict[str, Any]:
    """工具阶段：先推 tool_call 再执行（屏幕先出现"正在查工单"），
    结果渲染进 tool_context。registry.call 永不抛异常（契约见 tools/base）。

    写工具在这里会被沙箱拦成"待确认"，本轮**不产生答案** ——
    由 `_after_tools` 改道 `await_confirm` 终局。
    """
    decision = state["decision"]
    if decision.intent not in ("tool", "hybrid"):
        return {}

    # 沙箱要记"谁在什么场景下申请了这个动作"（审计与去重都要）。
    # 用 ContextVar 而不是工具参数：工具参数是给 LLM 看的，会话 id 不该
    # 混进模型的可见视野（它会被编造、也会被套问）。
    with call_context(session_id=state.get("session_id") or "",
                      message=state["message"]):
        return await _run_tools(state, decision)


async def _run_tools(state: AgentState, decision: Any) -> dict[str, Any]:
    w = get_stream_writer()
    timer = state["timer"]
    registry = state.get("registry")
    tool_context = ""
    results: list[ToolResult] = []
    pending: list[dict[str, Any]] = []
    degraded: list[str] = []
    span = _seg(state, "tools", as_type="tool", parent="dispatch",
                intent=decision.intent, registry=registry is not None)

    if registry is None:
        span.set(level="WARNING", status_message="工具层不可用")
        w(sse_frame({
            "type": "degraded", "stage": "tools", "reason": "unavailable",
            "message": "工具层不可用，本次无法查询工单/资产/告警实时数据",
        }))
        tool_context = "[工具层不可用] 本次无法查询工单/资产/告警等实时数据。"
    else:
        # ---- 先看能不能匹配一个 skill（业务意图层），匹配不到再走单工具计划 ----
        #
        # 带写操作时**跳过 skill**：skill 是"怎么排查/诊断"的只读编排，
        # 它的每一步都是一次查询。让写操作挤进 skill 会有两个问题：
        #   1. skill 的 `previous_empty` 语义是"没查到就换路径"，而写操作没有
        #      "换条路径再重启一次"这种说法；
        #   2. 写操作需要构造原因参数、需要挂起，这些只在 planner 里做。
        # 所以写操作一律走 planner（它自己也会先把告警/工单查出来垫上下文）。
        skill_registry = getattr(registry, "skills", None)
        entities: dict[str, Any] = {}
        skill = None
        if skill_registry is not None and not getattr(decision, "needs_write", False):
            try:
                entities = entities_of(decision, state["message"])
                skill = skill_registry.match(entities)
            except Exception as exc:  # noqa: BLE001 - 匹配失败退回工具计划，不断流
                log.warning("skill 匹配失败（%s），退回工具计划", exc)

        if skill is not None:
            skill_run = await _run_skill(
                state, skill, registry, entities, span, w, timer
            )
            results = skill_run.results
            tool_context = render_tool_context(results)
            pending = _emit_pending(w, results)
            if skill_run.stop_reason == "budget_exhausted":
                degraded.append("tools:budget_exhausted")
                w(sse_frame({
                    "type": "degraded", "stage": "tools", "reason": "budget_exhausted",
                    "message": f"技能 {skill.name} 超出 {skill.budget_ms}ms 预算，已返回部分结果",
                }))
            # 挂起/拒绝不计入"失败"：它们是策略的正常结果，混进去会让
            # 工具失败率凭空上涨，排障时会把"有人在审批"误读成"工具在坏"。
            failed = [r for r in results if not r.ok and not r.status]
            if failed:
                DEGRADED_TOTAL.labels(stage="tools", reason="failed").inc()
            span.set(output={
                "skill": skill.name,
                "stop_reason": skill_run.stop_reason,
                "tools": [{"name": r.tool, "ok": r.ok, "source": r.source,
                           "attempts": r.attempts, "status": r.status,
                           "elapsed_ms": round(r.elapsed_ms, 1)} for r in results],
                "skipped": skill_run.skipped,
                "pending": len(pending),
                "failed": len(failed),
            })
            timer.mark("tools")
            span.end()
            return {"tool_context": tool_context, "results": results,
                    "degraded": degraded, "pending": pending}

        # ---- 无 skill 命中：单工具计划（每条计划一个调用，并行）----
        try:
            calls = plan(decision, state["message"])
            span.add_meta(plan=[{"name": c.name, "args": c.args} for c in calls])
        except Exception as exc:  # noqa: BLE001 - 计划失败按空计划降级，不断流
            log.warning("tool plan failed: %s", exc)
            calls = []
            degraded.append(f"tools:{type(exc).__name__}")
            span.set(level="ERROR", status_message=f"计划失败：{type(exc).__name__}")

        if not calls:
            # 计划为空 = 实体没抽出来 —— 必须可见，否则用户不知道系统压根没查。
            span.set(level="WARNING", status_message="未抽出可查询实体")
            w(sse_frame({
                "type": "degraded", "stage": "tools", "reason": "no_plan",
                "message": "未能从问题中抽取出可查询实体（工单号 / 资产编码 / 服务名）",
            }))
        else:
            for call in calls:
                w(sse_frame({"type": "tool_call", "name": call.name, "args": call.args}))
            async for kind, payload in run_with_pings(_execute_calls(registry, calls, state)):
                if kind == "ping":
                    w(ping_frame())
                else:
                    results = payload
            for r in results:
                w(sse_frame(r.as_event()))
            pending = _emit_pending(w, results)
            tool_context = render_tool_context(results)
            failed = [r for r in results if not r.ok and not r.status]
            if failed:
                DEGRADED_TOTAL.labels(stage="tools", reason="failed").inc()
            span.set(output={
                "tools": [{"name": r.tool, "ok": r.ok, "source": r.source,
                           "attempts": r.attempts, "status": r.status,
                           "elapsed_ms": round(r.elapsed_ms, 1)} for r in results],
                "pending": len(pending),
                "failed": len(failed),
            })
    timer.mark("tools")
    span.end()
    return {"tool_context": tool_context, "results": results,
            "degraded": degraded, "pending": pending}


async def _run_skill(
    state: AgentState,
    skill: Any,
    registry: ToolRegistry,
    entities: dict[str, Any],
    span: Any,
    w: Any,
    timer: Any,
) -> Any:
    """执行 skill，并把每一步的 tool_call / tool_result 按**真实顺序**推给前端。

    与工具计划路径的区别：那边是"先发全部 tool_call、再并发执行、最后补结果"，
    这边是串行的，所以每步都能实时推送 —— 用户在屏幕上看到的是
    "按单号查 → 没查到 → 换关键词"的真实过程，而不是一个冻结的"正在查询"。
    """
    skill_span = _seg(state, "skill", as_type="agent", parent="tools",
                      skill=skill.name, requires=list(skill.requires),
                      budget_ms=skill.budget_ms)
    try:
        run = await execute_skill(
            skill,
            registry,
            entities,
            on_before=lambda step, args: w(sse_frame({
                "type": "tool_call", "name": step.tool, "args": args,
                "skill": skill.name, "label": step.title(),
            })),
            on_step=lambda step, result, r: w(sse_frame(result.as_event())),
        )
    finally:
        skill_span.end()
    span.add_meta(skill=skill.name, stop_reason=run.stop_reason,
                  steps=len(run.results), skipped=run.skipped)
    return run


async def node_recall(state: AgentState) -> dict[str, Any]:
    """召回阶段（hybrid 时与 tools 并行，可能早就跑完了）。
    等待期间发心跳；有降级就发 degraded 事件。"""
    decision = state["decision"]
    if decision.intent not in ("rag", "hybrid"):
        return {}
    w = get_stream_writer()
    timer = state["timer"]
    chunks: list[rag.Chunk] = []
    recall_meta: dict[str, Any] = {}
    degraded: list[str] = []
    span = _seg(state, "recall", as_type="retriever", parent="dispatch",
                intent=decision.intent)

    async for kind, payload in run_with_pings(
        _recall(state["message"], state.get("embedding"), state)
    ):
        if kind == "ping":
            w(ping_frame())
        else:
            chunks, recall_meta, degraded = payload
            if recall_meta.get("rerank") == "disabled":
                # 不是 degraded：融合序本身就是一条完整可用的路径。
                # 但也不能静默 —— 否则看事件流的人以为这是带重排的完整链路。
                w(sse_frame({
                    "type": "notice", "stage": "recall", "code": "rerank_disabled",
                    "message": "重排已关闭（配置 RERANK_ENABLED=false），本次按融合序取上下文",
                }))
            if degraded:
                w(sse_frame({
                    "type": "degraded", "stage": "recall",
                    "reason": ",".join(degraded),
                    "message": "部分召回通道不可用，本次答案依据可能不完整",
                }))
            span.add_meta(channels=recall_meta.get("channels"),
                          fused=recall_meta.get("fused"),
                          rerank=recall_meta.get("rerank"))
            if degraded:
                span.set(level="WARNING", status_message=",".join(degraded))
    timer.mark("recall")
    if chunks:
        order_by, rank_scores = _rank_key(chunks)
        # top_sources 是"答案依据了哪几篇"——回放时靠它还原现场，
        # 只记数量的话，两周后看到 n=5 根本不知道当时检索到了什么
        span.set(output={
            "n_chunks": len(chunks),
            "order_by": order_by,
            "rank_scores": rank_scores[:5],
            "top_sources": [c.source or c.doc_id for c in chunks],
        })
    else:
        span.set(output={"n_chunks": 0})
    span.end()
    return {"chunks": chunks, "recall_meta": recall_meta, "degraded": degraded}


def _rank_key(chunks: list[rag.Chunk]) -> tuple[str, list[float]]:
    """返回 (排序口径, 排序分)。用来回答"这个顺序到底是被什么排出来的"。

    三种口径互斥，按优先级取第一个命中的：

    · `rerank` —— 重排分（RERANK_ENABLED=true 时唯一有意义的口径）；
    · `rrf`    —— 多路融合分（只由排名决定，见 app/pipeline/fusion.py）；
    · `score`  —— 单路原始分（只启用一路召回时的退化情形）。

    为什么不能拿 `scores` 判排序：`scores` 装的是**各通道原始分** ——
    向量是 cosine（0~1），BM25 无上界（实测 5.98 很常见）。融合用的是 RRF，
    只用排名、不用分值，所以最终顺序与原始分**本来就不单调**。
    把原始分当排序证据，在关掉重排的链路上必然误判（实测前 3 个是
    0.6466 / 0.7045 / 0.6619 —— 顺序完全正确，只是这个字段不该单调）。
    """
    if any(c.rerank_score is not None for c in chunks):
        return "rerank", [round(float(c.rerank_score), 6) for c in chunks]
    rrf_scores = [(c.metadata or {}).get("_rrf_score") for c in chunks]
    if chunks and all(v is not None for v in rrf_scores):
        return "rrf", [round(float(v), 6) for v in rrf_scores]
    return "score", [round(float(c.score), 4) for c in chunks]


_PENDING_FIELDS = ("token", "tool", "args", "risk", "reason", "simulated", "expires_in")


def _pending_context(pending: list[dict[str, Any]]) -> str:
    """把"本轮有一批动作卡在确认环节"作为**本轮事实**交给模型。

    为什么拼进 tool_context 而不是改 system prompt：system prompt 是全局的，
    这句话只对本轮成立。写进全局提示后，模型会把"你有待确认动作"这个状态
    带到后面每一轮，包括那些根本没有写操作的提问 —— 实测会演变成
    "我还没执行重启，请先确认"这种对无关问题的回答。

    为什么需要它：hybrid 挂起时模型同时拿到检索资料和"要重启"的诉求，
    没有任何约束时它会顺着写的惯性说"已为你重启"。这里明确给出未执行事实。
    """
    if not pending:
        return ""
    lines = ["【本轮待确认的动作 · 尚未执行】"]
    for p in pending:
        lines.append(
            f"- {p.get('tool')}({json.dumps(p.get('args') or {}, ensure_ascii=False)})"
            f" · 风险={p.get('risk')} · 原因={p.get('reason')}"
        )
    lines.append(
        "以上动作都在等你确认，本轮**没有执行任何写操作**。"
        "回答里可以说明「需要确认后才能执行」，但禁止出现「已重启/已扩容/已恢复/已执行」这类表述，"
        "也不要编造凭据编号或有效期。"
    )
    return "\n".join(lines)


def _gen_tool_context(state: AgentState, pending: list[dict[str, Any]]) -> str:
    """生成阶段用的 tool_context：待确认说明在前，真实工具结果在后。

    顺序是有意的：模型先读到"这些动作没执行"，再读到"查到了什么"，
    就不会把后面的实时数据当成"重启之后的状态"。
    """
    ctx = state.get("tool_context", "")
    if not pending:
        return ctx
    notice = _pending_context(pending)
    return f"{notice}\n\n{ctx}" if ctx else notice


def _pending_done(pending: list[dict[str, Any]]) -> dict[str, Any]:
    """done 帧里描述待确认动作。字段与 await_confirm 保持一致 ——
    调用方只写一份解析逻辑，不必区分"这一轮有没有正文"。"""
    return {
        "status": "awaiting_confirmation",
        "pending": [{k: p.get(k) for k in _PENDING_FIELDS} for p in pending],
    }


async def node_generate(state: AgentState) -> dict[str, Any]:
    """⑥⑦⑧ 生成 + 推流 + 写缓存。所有需要 LLM 的分支共用这一个节点。"""
    w = get_stream_writer()
    timer = state["timer"]
    t0 = state["t0"]
    decision = state["decision"]
    cache = state.get("cache")
    chunks = state.get("chunks") or []
    recall_meta = state.get("recall_meta") or {}
    # hybrid 挂起时非空：这一轮既要答知识，又有一批写操作在等人确认。
    # 两者必须同时体现在输出里 —— 答案体现"读"，done.status 体现"写没做"。
    pending = state.get("pending") or []
    degraded: list[str] = []
    # docs 段里赋值；先给默认值是为了"docs 之前就抛异常"的路径不会 NameError
    order_by: str = "none"

    try:
        sources = [c.source or c.doc_id for c in chunks]
        pages = [c.page for c in chunks if c.page is not None]
        # scores：各通道原始分（cosine / BM25 混在一起，量纲不同）——**不是排序键**。
        # 排序键单独给：order_by + rank_scores，见 _rank_key 的说明。
        scores = [
            round(c.rerank_score if c.rerank_score is not None else c.score, 4)
            for c in chunks
        ]
        order_by, rank_scores = _rank_key(chunks)

        if chunks or recall_meta:
            w(sse_frame({
                "type": "docs",
                "sources": sources,
                "pages": pages,
                "scores": scores,
                "order_by": order_by,
                "rank_scores": rank_scores,
                "recall_sources": [c.recall_source for c in chunks],
                **(recall_meta or {}),
            }))
        timer.mark("docs")

        collected: list[str] = []
        first_token = True
        # generation span 只包 LLM 流式段：写缓存、存记忆、推 done 都是"收尾"，
        # 把它们算进生成耗时会让"模型变慢了"和"Redis 变慢了"混在一起。
        gen_span = _seg(
            state, "generate", as_type="generation", parent="dispatch",
            model=settings.deepseek_model,
            input={
                "question": state["message"][:200],
                "n_chunks": len(chunks),
                "tool_context_chars": len(state.get("tool_context", "")),
                "history_chars": len(state.get("memory_context", "")),
            },
        )
        try:
            async for delta in rag.generate_stream(
                state["message"], chunks,
                tool_context=_gen_tool_context(state, pending),
                history=state.get("memory_context", ""),
            ):
                collected.append(delta)
                if first_token:
                    first_token = False
                    TTFT_SECONDS.labels(cache="miss", route=decision.intent).observe(
                        time.perf_counter() - t0
                    )
                    timer.mark("first_token")
                    # TTFT 的唯一测量点：Span 记 completion_start_time（Langfuse 用它
                    # 算 Latency to First Token），turn 记 ttft_ms（JSONL 与 done 帧用它）
                    gen_span.first_token()
                    _mark_ttft(state)
                w(sse_frame({"type": "token", "content": delta}))
            gen_span.set(output={"chars": sum(len(x) for x in collected)})
        except Exception as exc:  # noqa: BLE001 - 生成失败降级成提示，不是 500
            log.exception("generation failed: %s", exc)
            DEGRADED_TOTAL.labels(stage="generate", reason="exception").inc()
            gen_span.set(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
            # 记进 degraded 是为了 trace 里能看出"这一轮降级了"。
            # `_emit_save_degraded` 只对 `memory:` 前缀发帧，所以不会多出 SSE 事件 ——
            # 协议不变，但回放时一眼能看到这轮答案是降级来的。
            degraded.append(f"generate:{type(exc).__name__}")
            w(sse_frame({
                "type": "degraded", "stage": "generate",
                "reason": f"{type(exc).__name__}",
                "message": "生成服务暂时不可用，请稍后重试",
            }))
            w(sse_frame({"type": "error", "message": "生成服务暂时不可用，请稍后重试"}))
        finally:
            gen_span.end()

        answer_text = "".join(collected)
        timer.mark("generate")

        # 挂起的那一轮**不写缓存**：答案描述的是"某个动作还在等人确认"这个
        # 瞬时状态，缓存下来会让同样的问题下次直接命中，把审批单换成一段旧文字。
        if answer_text and cache is not None and not pending:
            with _seg(state, "cache_store", parent="generate",
                      chars=len(answer_text),
                      tokens_saved=rag.estimate_tokens(answer_text)
                      + rag.estimate_tokens(state["message"])) as store_span:
                try:
                    await cache.store(
                        state["message"],
                        CachedAnswer(
                            answer=answer_text,
                            queried=state["message"],
                            pages=pages,
                            scores=scores,
                            sources=sources,
                            route=decision.intent,
                            tokens_saved=rag.estimate_tokens(answer_text)
                            + rag.estimate_tokens(state["message"]),
                        ),
                        embedding=state.get("embedding"),
                    )
                    timer.mark("cache_store")
                    store_span.set(output={"stored": True})
                except Exception as exc:  # noqa: BLE001
                    log.warning("cache store failed: %s", exc)
                    store_span.set(level="WARNING",
                                   status_message=f"写缓存失败：{type(exc).__name__}")

        # 记一轮 + 抽事实。degraded 帧必须在 done 之前发（done 后前端不消费）。
        degraded.extend(await _save_turn(state, answer_text))
        _emit_save_degraded(w, degraded)

        done_frame: dict[str, Any] = {
            "type": "done", "cache": "miss", "route": decision.intent,
            "stage_ms": timer.summary(),
            "total_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
        if pending:
            # 有正文也有待确认：调用方按 status 弹确认框，正文照常显示。
            # 这与 await_confirm 的 done 是同一个契约，只是多了 token 流。
            done_frame.update(_pending_done(pending))
        w(sse_frame(done_frame))
        w("data: [DONE]\n\n")

        _finish_trace(state, answer=answer_text, route=decision.intent,
                      cache="miss", degraded=degraded,
                      n_chunks=len(chunks), order_by=order_by)
        return {"answer": answer_text, "degraded": degraded}
    finally:
        # 单飞锁的释放必须跟着主链路走：无论生成成败都要释放，
        # 否则同一句要等 singleflight 超时才能被下一个请求接手。
        if state.get("leader") and cache is not None:
            try:
                await cache.release_singleflight(state["message"])
            except Exception:  # noqa: BLE001
                pass


# =====================================================================
# 图装配
# =====================================================================
async def node_tools_done(state: AgentState) -> dict[str, Any]:
    """汇合点：tools 与 recall 两路都跑完才继续。

    为什么要多这一个空节点，而不是像以前一样让 generate 直接接两条入边：
    挂起待确认时要在**汇合之后**才决定去向。如果条件边直接挂在 tools 上，
    recall→generate 那条边仍会触发 generate —— 于是"等人确认"的同时
    模型已经生成了一段"已为你重启"的答案。这个 bug 只会在真有人确认的场景
    里出现，平时跑测试永远看不见。
    """
    return {}


def _after_tools_done(state: AgentState) -> str:
    """汇合之后决定去向：答 / 挂起 / 边答边挂。

    三个分支各有明确理由：
      · 没有待确认动作        → generate（常规路）
      · 有待确认 + 不需要检索  → await_confirm（纯写操作，没有"已完成的事实"可总结，
                                调 LLM 只会得到一段编造的确认话术）
      · 有待确认 + 需要检索    → generate（hybrid：知识部分已有检索结果，
                                是一条能独立回答的诉求，吞掉它等于少答一半）

    第三种不能走 await_confirm，也不能只走 generate —— 前者让"OOM 怎么办"
    永远得不到回答，后者会让 done 帧丢掉 pending，调用方不知道该弹确认框。
    所以 generate 必须自己带上 status=awaiting_confirmation，见 `_pending_done`。
    """
    if not state.get("pending"):
        return "generate"
    # 用 getattr 兜底：这个函数是纯判定，不该因为构造了一个缺 decision 的
    # 局部状态就抛 KeyError。缺省按"不答"处理 —— 挂起优先于半截答案。
    decision = state.get("decision")
    return "generate" if getattr(decision, "needs_rag", False) else "await_confirm"


async def node_await_confirm(state: AgentState) -> dict[str, Any]:
    """终局④：本轮挂起了写操作，等人确认。

    **不调 LLM**：这一轮没有任何"已完成"的事实可以总结，让模型生成
    只会得到一段编造的确认话术。这里只做两件事：
      1. done 带 `status=awaiting_confirmation` + pending 摘要，让调用方知道该走确认接口
      2. 记一轮会话历史，下一轮"确认吧"能接上

    `confirm_required` 帧**不在这里重发** —— node_tools 已经发过了。
    重发看着"更幂等"，实际效果是前端弹两次确认框，而带 token 的帧弹两次
    会让人以为有两个待批动作（实测第一版就是这样）。
    """
    w = get_stream_writer()
    timer = state["timer"]
    t0 = state["t0"]
    decision = state["decision"]
    present = state.get("pending") or []

    span = _seg(state, "await_confirm", parent="tools",
                tools=[p.get("tool") for p in present])
    degraded: list[str] = []
    answer = ""
    try:
        tools = "、".join(str(p.get("tool")) for p in present)
        answer = (
            f"这次请求涉及写操作（{tools}），需要你确认后才会执行。\n"
            f"确认凭据已随事件流下发，{present[0].get('expires_in', 0) if present else 0} 秒内有效，"
            f"只能用一次。"
        )

        degraded = await _save_turn(state, answer)
        _emit_save_degraded(w, degraded)

        timer.mark("await_confirm")
        w(sse_frame({
            "type": "done",
            "cache": "bypass",
            "route": decision.intent,
            # 新增字段：调用方据此决定"要不要弹确认框"，而不是去猜 done 之后为什么没 token
            "status": "awaiting_confirmation",
            "pending": [
                {k: p.get(k) for k in
                 ("token", "tool", "args", "risk", "reason", "simulated", "expires_in")}
                for p in present
            ],
            "stage_ms": timer.summary(),
            "total_ms": round((time.perf_counter() - t0) * 1000, 1),
        }))
        w("data: [DONE]\n\n")
        span.set(output={"pending": len(present)})
    finally:
        span.end()

    _finish_trace(state, answer=answer, route=decision.intent,
                  cache="bypass", status="awaiting_confirmation",
                  degraded=degraded)
    return {"answer": answer, "degraded": degraded}


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("route", node_route)
    g.add_node("chitchat", node_chitchat)
    g.add_node("memory_load", node_memory_load)
    g.add_node("embed", node_embed)
    g.add_node("cache_exact", node_cache_exact)
    g.add_node("cache_semantic", node_cache_semantic)
    g.add_node("singleflight", node_singleflight)
    g.add_node("dispatch", node_dispatch)
    g.add_node("tools", node_tools)
    g.add_node("recall", node_recall)
    g.add_node("tools_done", node_tools_done)
    g.add_node("await_confirm", node_await_confirm)
    g.add_node("generate", node_generate)

    g.add_edge(START, "route")
    g.add_conditional_edges("route", _after_route,
                            {"chitchat": "chitchat", "memory_load": "memory_load"})
    # 缓存拆两段：exact 先行（命中不付编码费），miss 才编码、才查语义。
    # 修的是"exact 命中也付 BGE 编码（stage p50 6.33s）"的已知瓶颈。
    g.add_edge("memory_load", "cache_exact")
    g.add_conditional_edges("cache_exact", _after_cache_exact,
                            {"embed": "embed", END: END})
    g.add_edge("embed", "cache_semantic")
    g.add_conditional_edges("cache_semantic", _after_cache_semantic,
                            {"singleflight": "singleflight", END: END})
    g.add_conditional_edges("singleflight", _after_singleflight,
                            {"dispatch": "dispatch", END: END})
    # 扇出：两路并行，汇合点从 generate 前移到了 tools_done ——
    # 这样"挂起待确认"才有机会在**汇合之后**改道（见 node_tools_done）。
    g.add_edge("dispatch", "tools")
    g.add_edge("dispatch", "recall")
    g.add_edge("tools", "tools_done")
    g.add_edge("recall", "tools_done")
    g.add_conditional_edges("tools_done", _after_tools_done,
                            {"await_confirm": "await_confirm", "generate": "generate"})
    g.add_edge("generate", END)
    g.add_edge("await_confirm", END)
    g.add_edge("chitchat", END)
    return g.compile()


_GRAPH = None


def get_graph():
    """编译一次、复用多次。图的拓扑不变，变的只是 state。"""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH

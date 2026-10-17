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
         memory_load → embed → cache_lookup ──hit──▶ END
              （工作/会话/长期 三层记忆在这里汇入 state）
                                 │ miss
                                 ▼
                            singleflight ──follower 命中──▶ END
                                 │ leader
                                 ▼
                              dispatch ──┬──▶ tools ──┐
                                         └──▶ recall ─┴─▶ generate → END

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
from app.pipeline import intent as intent_mod
from app.pipeline import rag
from app.pipeline.embedding import embed_one
from app.pipeline.fusion import cap_per_unit
from app.tools import ToolRegistry, plan
from app.tools.base import ToolResult, render_tool_context

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

    # ---- 中间产物 ----
    decision: intent_mod.RouteDecision | None
    embedding: list[float] | None
    hit: CachedAnswer | None           # 两级缓存命中
    waited: CachedAnswer | None        # singleflight 跟随者拿到的结果
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
    try:
        facts = extract_facts(state["message"])
        err = await memory.save_turn(
            sid,
            question=state["message"],
            answer=answer_text,
            route=(state.get("decision").intent if state.get("decision") else "unknown"),
            facts=facts,
        )
        return [err] if err else []
    except Exception as exc:  # noqa: BLE001 - 保存失败绝不影响收尾帧
        log.warning("save_turn failed: %s", exc)
        return [f"memory:{type(exc).__name__}"]


def _stream_cached_frames(hit: CachedAnswer, t0: float):
    """缓存命中路径的帧序列（与旧 `_stream_cached` 逐帧一致）。"""
    first = True
    for piece in split_text(hit.answer):
        if first:
            TTFT_SECONDS.labels(cache=hit.source, route=hit.route).observe(
                time.perf_counter() - t0
            )
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
    question: str, embedding: list[float] | None
) -> tuple[list[rag.Chunk], dict[str, Any], list[str]]:
    """召回 + 精排。两层降级：召回整体失败 → 空结果 + degraded；
    重排失败 / 被显式关闭 → 融合序（同一条 `_no_rerank_order` 路径）。"""
    degraded: list[str] = []
    try:
        outcome = await rag.recall_detailed(question, embedding=embedding)
    except Exception as exc:  # noqa: BLE001
        log.exception("recall failed entirely: %s", exc)
        DEGRADED_TOTAL.labels(stage="recall", reason="exception").inc()
        return [], {"channels": {}, "fused": False}, [f"recall:{type(exc).__name__}"]

    if not getattr(settings, "rerank_enabled", True):
        # 显式关闭：写进 degraded 而不是静默跳过。
        top = _no_rerank_order(outcome.chunks)
        degraded.append("rerank:disabled")
    else:
        try:
            top = await rag.rerank_chunks(question, outcome.chunks)
        except Exception as exc:  # noqa: BLE001
            log.warning("rerank failed: %s", exc)
            DEGRADED_TOTAL.labels(stage="rerank", reason="exception").inc()
            degraded.append(f"rerank:{type(exc).__name__}")
            top = _no_rerank_order(outcome.chunks)

    meta: dict[str, Any] = {"channels": outcome.channels, "fused": outcome.fused}
    degraded.extend(outcome.degraded)
    if degraded:
        meta["degraded"] = degraded
    return top, meta, degraded


async def _execute_calls(registry: ToolRegistry, calls: list) -> list[ToolResult]:
    """并发执行工具计划。结果顺序与计划顺序一致（gather 保序）。"""
    return list(await asyncio.gather(*[registry.call(c.name, **c.args) for c in calls]))


# =====================================================================
# 节点
# =====================================================================
async def node_route(state: AgentState) -> dict[str, Any]:
    """① 意图路由：纯规则、零 IO。只算不发事件 ——
    meta 必须是第一个事件，而 meta 要等缓存查询结果。"""
    decision = intent_mod.decide(state["message"])
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
    # 刻意不进缓存：闲聊答案没有知识价值，写进语义索引只会污染 KNN 候选。
    w(sse_frame({"type": "meta", "cache": "bypass", "route": "chitchat",
                 "kb_version": state["kb_version"]}))
    w(sse_frame(decision.as_event()))
    _emit_memory_degraded(w, state)
    for piece in split_text(CHITCHAT_REPLY):
        w(sse_frame({"type": "token", "content": piece}))
    TTFT_SECONDS.labels(cache="miss", route="chitchat").observe(time.perf_counter() - t0)
    timer.mark("chitchat")
    degraded = await _save_turn(state, CHITCHAT_REPLY)
    _emit_save_degraded(w, degraded)
    w(sse_frame({
        "type": "done", "cache": "bypass", "route": "chitchat",
        "stage_ms": timer.summary(),
        "total_ms": round((time.perf_counter() - t0) * 1000, 1),
    }))
    w("data: [DONE]\n\n")
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
    try:
        ctx = await memory.load(sid)
    except Exception as exc:  # noqa: BLE001 - 防御性兜底：store 契约是"不抛"，
        # 但万一实现违约，也不能让一个锦上添花的依赖炸掉整条链路。
        log.warning("memory.load raised（store 违约，按降级处理）: %s", exc)
        return {"memory_context": "", "memory_stats": {},
                "degraded": [f"memory:{type(exc).__name__}"]}
    stats = {"history_turns": len(ctx.get("history", [])),
             "facts": len(ctx.get("facts", []))}
    update: dict[str, Any] = {
        "memory_context": render_memory_context(ctx),
        "memory_stats": stats,
    }
    err = getattr(memory, "last_error", None)
    if err:
        update["degraded"] = [f"memory:{err}"]
    return update


async def node_embed(state: AgentState) -> dict[str, Any]:
    """② 向量化，一次算完（缓存与检索共用）。失败只降级不抛 ——
    embedding 挂了 = 语义缓存与向量召回不可用，BM25 路还在。"""
    embedding: list[float] | None = None
    try:
        with STAGE_SECONDS.labels(stage="embed_query").time():
            embedding = await embed_one(state["message"])
    except Exception as exc:  # noqa: BLE001
        log.warning("embedding failed, semantic cache & vector recall disabled: %s", exc)
        DEGRADED_TOTAL.labels(stage="embed", reason="exception").inc()
    state["timer"].mark("embed")
    return {"embedding": embedding}


async def node_cache_lookup(state: AgentState) -> dict[str, Any]:
    """③④ 两级缓存。meta + route 事件在这里发出（缓存结果决定 meta 内容）。

    命中 → 本节点内直接推完 token + done（随后条件边进 END）；
    未命中 → 条件边进 singleflight。缓存查询异常按 miss 处理并降级 ——
    旧实现这里没兜，异常会直接断流；这是本次重构顺手补上的洞。
    """
    w = get_stream_writer()
    timer = state["timer"]
    t0 = state["t0"]
    decision = state["decision"]
    cache = state.get("cache")

    hit: CachedAnswer | None = None
    cache_error: str | None = None
    if state["use_cache"] and cache is not None:
        try:
            hit = await cache.lookup(state["message"], embedding=state.get("embedding"))
            if hit.source == "miss":
                hit = None
        except Exception as exc:  # noqa: BLE001
            log.warning("cache lookup failed（按 miss 处理）: %s", exc)
            DEGRADED_TOTAL.labels(stage="cache", reason="exception").inc()
            cache_error = type(exc).__name__
            hit = None
    timer.mark("cache_lookup")

    if hit is not None:
        w(sse_frame({
            "type": "meta",
            "cache": hit.source,
            "route": hit.route or decision.intent,
            # similarity 必须报：语义命中的看点就是"改写了还是命中"。
            "similarity": round(hit.similarity, 4),
            "kb_version": state["kb_version"],
            **({"memory": state["memory_stats"]} if state.get("memory_stats") else {}),
        }))
        # 缓存命中也推 route 事件：用户能看到"本来会走哪条路"。
        w(sse_frame(decision.as_event()))
        _emit_memory_degraded(w, state)
        # 保存先行：degraded 帧必须在 done 之前发（done 之后前端不再消费）
        degraded = await _save_turn(state, hit.answer)
        _emit_save_degraded(w, degraded)
        for frame in _stream_cached_frames(hit, t0):
            w(frame)
        return {"hit": hit, "answer": hit.answer, "degraded": degraded}

    w(sse_frame({
        "type": "meta", "cache": "miss", "route": decision.intent,
        "kb_version": state["kb_version"],
        **({"memory": state["memory_stats"]} if state.get("memory_stats") else {}),
    }))
    w(sse_frame(decision.as_event()))
    if cache_error:
        w(sse_frame({
            "type": "degraded", "stage": "cache", "reason": cache_error,
            "message": "缓存查询异常，本次按未命中处理",
        }))
    _emit_memory_degraded(w, state)
    return {"hit": None}


def _after_cache_lookup(state: AgentState) -> str:
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

    leader = True
    if state["use_cache"] and cache is not None:
        leader = await cache.acquire_singleflight(state["message"])
        if not leader:
            waited = await cache.wait_for_result(
                state["message"], embedding=state.get("embedding")
            )
            if waited is not None:
                w(sse_frame({
                    "type": "meta",
                    "cache": "singleflight",
                    "route": waited.route or decision.intent,
                    "kb_version": state["kb_version"],
                }))
                degraded = await _save_turn(state, waited.answer)
                _emit_save_degraded(w, degraded)
                for frame in _stream_cached_frames(waited, t0):
                    w(frame)
                return {"waited": waited, "leader": False,
                        "answer": waited.answer, "degraded": degraded}
    return {"leader": leader, "waited": None}


def _after_singleflight(state: AgentState) -> str:
    return "dispatch" if state.get("waited") is None else END


async def node_dispatch(state: AgentState) -> dict[str, Any]:
    """扇出中转：tools ∥ recall 并行（hybrid 的关键 —— 两路无依赖，
    顺序执行等于把耗时相加）。"""
    return {}


async def node_tools(state: AgentState) -> dict[str, Any]:
    """工具阶段：先推 tool_call 再执行（屏幕先出现"正在查工单"），
    结果渲染进 tool_context。registry.call 永不抛异常（契约见 tools/base）。"""
    decision = state["decision"]
    if decision.intent not in ("tool", "hybrid"):
        return {}
    w = get_stream_writer()
    timer = state["timer"]
    registry = state.get("registry")
    tool_context = ""
    results: list[ToolResult] = []
    degraded: list[str] = []

    try:
        calls = plan(decision, state["message"])
    except Exception as exc:  # noqa: BLE001 - 计划失败按空计划降级，不断流
        log.warning("tool plan failed: %s", exc)
        calls = []
        degraded.append(f"tools:{type(exc).__name__}")

    if registry is None:
        w(sse_frame({
            "type": "degraded", "stage": "tools", "reason": "unavailable",
            "message": "工具层不可用，本次无法查询工单/资产/告警实时数据",
        }))
        tool_context = "[工具层不可用] 本次无法查询工单/资产/告警等实时数据。"
    elif not calls:
        # 计划为空 = 实体没抽出来 —— 必须可见，否则用户不知道系统压根没查。
        w(sse_frame({
            "type": "degraded", "stage": "tools", "reason": "no_plan",
            "message": "未能从问题中抽取出可查询实体（工单号 / 资产编码 / 服务名）",
        }))
    else:
        for call in calls:
            w(sse_frame({"type": "tool_call", "name": call.name, "args": call.args}))
        async for kind, payload in run_with_pings(_execute_calls(registry, calls)):
            if kind == "ping":
                w(ping_frame())
            else:
                results = payload
        for r in results:
            w(sse_frame(r.as_event()))
        tool_context = render_tool_context(results)
        failed = [r for r in results if not r.ok]
        if failed:
            DEGRADED_TOTAL.labels(stage="tools", reason="failed").inc()
    timer.mark("tools")
    return {"tool_context": tool_context, "results": results, "degraded": degraded}


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

    async for kind, payload in run_with_pings(
        _recall(state["message"], state.get("embedding"))
    ):
        if kind == "ping":
            w(ping_frame())
        else:
            chunks, recall_meta, degraded = payload
            if degraded:
                w(sse_frame({
                    "type": "degraded", "stage": "recall",
                    "reason": ",".join(degraded),
                    "message": "部分召回通道不可用，本次答案依据可能不完整",
                }))
    timer.mark("recall")
    return {"chunks": chunks, "recall_meta": recall_meta, "degraded": degraded}


async def node_generate(state: AgentState) -> dict[str, Any]:
    """⑥⑦⑧ 生成 + 推流 + 写缓存。所有需要 LLM 的分支共用这一个节点。"""
    w = get_stream_writer()
    timer = state["timer"]
    t0 = state["t0"]
    decision = state["decision"]
    cache = state.get("cache")
    chunks = state.get("chunks") or []
    recall_meta = state.get("recall_meta") or {}
    degraded: list[str] = []

    try:
        sources = [c.source or c.doc_id for c in chunks]
        pages = [c.page for c in chunks if c.page is not None]
        scores = [
            round(c.rerank_score if c.rerank_score is not None else c.score, 4)
            for c in chunks
        ]

        if chunks or recall_meta:
            w(sse_frame({
                "type": "docs",
                "sources": sources,
                "pages": pages,
                "scores": scores,
                "recall_sources": [c.recall_source for c in chunks],
                **(recall_meta or {}),
            }))
        timer.mark("docs")

        collected: list[str] = []
        first_token = True
        try:
            async for delta in rag.generate_stream(
                state["message"], chunks,
                tool_context=state.get("tool_context", ""),
                history=state.get("memory_context", ""),
            ):
                collected.append(delta)
                if first_token:
                    TTFT_SECONDS.labels(cache="miss", route=decision.intent).observe(
                        time.perf_counter() - t0
                    )
                    timer.mark("first_token")
                    first_token = False
                w(sse_frame({"type": "token", "content": delta}))
        except Exception as exc:  # noqa: BLE001 - 生成失败降级成提示，不是 500
            log.exception("generation failed: %s", exc)
            DEGRADED_TOTAL.labels(stage="generate", reason="exception").inc()
            w(sse_frame({
                "type": "degraded", "stage": "generate",
                "reason": f"{type(exc).__name__}",
                "message": "生成服务暂时不可用，请稍后重试",
            }))
            w(sse_frame({"type": "error", "message": "生成服务暂时不可用，请稍后重试"}))

        answer_text = "".join(collected)
        timer.mark("generate")

        if answer_text and cache is not None:
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
            except Exception as exc:  # noqa: BLE001
                log.warning("cache store failed: %s", exc)

        # 记一轮 + 抽事实。degraded 帧必须在 done 之前发（done 后前端不消费）。
        degraded.extend(await _save_turn(state, answer_text))
        _emit_save_degraded(w, degraded)

        w(sse_frame({
            "type": "done", "cache": "miss", "route": decision.intent,
            "stage_ms": timer.summary(),
            "total_ms": round((time.perf_counter() - t0) * 1000, 1),
        }))
        w("data: [DONE]\n\n")

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
def build_graph():
    g = StateGraph(AgentState)
    g.add_node("route", node_route)
    g.add_node("chitchat", node_chitchat)
    g.add_node("memory_load", node_memory_load)
    g.add_node("embed", node_embed)
    g.add_node("cache_lookup", node_cache_lookup)
    g.add_node("singleflight", node_singleflight)
    g.add_node("dispatch", node_dispatch)
    g.add_node("tools", node_tools)
    g.add_node("recall", node_recall)
    g.add_node("generate", node_generate)

    g.add_edge(START, "route")
    g.add_conditional_edges("route", _after_route,
                            {"chitchat": "chitchat", "memory_load": "memory_load"})
    g.add_edge("memory_load", "embed")
    g.add_edge("embed", "cache_lookup")
    g.add_conditional_edges("cache_lookup", _after_cache_lookup,
                            {"singleflight": "singleflight", END: END})
    g.add_conditional_edges("singleflight", _after_singleflight,
                            {"dispatch": "dispatch", END: END})
    # 扇出：两路并行，LangGraph 在 generate 前自动汇合（等两路都完成）
    g.add_edge("dispatch", "tools")
    g.add_edge("dispatch", "recall")
    g.add_edge("tools", "generate")
    g.add_edge("recall", "generate")
    g.add_edge("generate", END)
    g.add_edge("chitchat", END)
    return g.compile()


_GRAPH = None


def get_graph():
    """编译一次、复用多次。图的拓扑不变，变的只是 state。"""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH

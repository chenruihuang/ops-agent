"""问答端点：意图路由 + 三层记忆 + 缓存 + 多路召回 + 工具 + 流式生成。

编排已迁入 LangGraph（app/pipeline/orchestrator.py）—— 本文件只保留
**端点职责**：鉴权、并发闸门、SSE 透传。图结构与状态流转见编排器 docstring。

四条硬约束（重构后依然成立，验收方式见 tests/test_chat_sse.py）
--------------------------------------------------------------
a) 缓存查询必须早于任何检索动作 —— 图序保证（cache_exact/cache_semantic 在 dispatch 之前）。
b) 任何依赖挂掉都不能返回 500，只能降级 —— 各节点内兜 + 端点层兜两层。
c) 路由必须先于缓存 —— 图序保证（route 是 START 后第一个节点）。
d) 降级必须可见 —— degraded 事件由各节点经 stream writer 推出，
   记忆降级由携带 meta 的节点补发（保证 meta 仍是第一帧）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.memory.session import safe_sid
from app.observability.metrics import CHAT_REQUESTS, DEGRADED_TOTAL
from app.observability.tracing import get_tracer
from app.pipeline import orchestrator
from app.pipeline.orchestrator import AgentState, StageTimer, get_graph, sse_frame
from app.tools.sandbox import call_context

log = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

# 进程内并发闸门，保护后端不被打爆。多副本场景要换成 Redis 令牌桶。
_semaphore = asyncio.Semaphore(settings.max_concurrency)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None
    use_cache: bool = True


class WarmupRequest(BaseModel):
    questions: list[str] = Field(min_length=1, max_length=200)


class ConfirmRequest(BaseModel):
    """人工确认请求。

    **故意只有一个 token + 一个布尔**：没有 `tool`、没有 `args`。
    要执行什么由服务端从待办记录里读（见 app/tools/sandbox.py 模块说明）。
    只要这里留一个 args 字段，"确认了 A 却执行 B"就会立刻变成一个
    「前端传错参数」级别的 bug —— 而它是安全边界，不该有这种 bug。
    """

    token: str = Field(min_length=8, max_length=128)
    approve: bool = True
    session_id: str | None = None


def _require_api_key(x_api_key: str | None) -> None:
    if not x_api_key or x_api_key != settings.api_key.get_secret_value():
        raise HTTPException(status_code=401, detail="invalid api key")


# =====================================================================
# 端点
# =====================================================================
@router.post("/stream")
async def chat_stream(
    req: ChatRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    _require_api_key(x_api_key)
    t0 = time.perf_counter()
    cache = getattr(request.app.state, "cache", None)
    registry = getattr(request.app.state, "tools", None)
    memory = getattr(request.app.state, "memory", None)

    # 运行时版本号必须从 cache 读，不能用 settings.kb_version。
    # settings 是**启动时**的快照；打一次 /admin/kb/bump 之后它就过期了，
    # 于是事件流里报的版本号与缓存实际使用的版本号不一致，
    # 用户看到"版本还是 1"会以为 bump 没生效 —— 而其实生效了。
    async def _kb_version() -> int:
        if cache is None:
            return settings.kb_version
        try:
            return await cache.current_kb_version()
        except Exception as exc:  # noqa: BLE001
            log.debug("读取运行时版本号失败，回退配置值：%s", exc)
            return settings.kb_version

    async def generator() -> AsyncIterator[str]:
        async with _semaphore:
            sid = safe_sid(req.session_id)
            kb_version = await _kb_version()
            # 追踪句柄在**端点**创建、随 state 进图 —— 与 timer 同一个模式。
            # 放在这里而不是编排器内部：trace 的边界应当等于一次 HTTP 请求，
            # 从收到请求算起（含并发闸门排队时间）。否则"P95 慢"里那段排队
            # 会被算丢，看起来像后端没有任何问题。
            trace = get_tracer().start_turn(
                message=req.message,
                session_id=sid or "",
                use_cache=req.use_cache,
                kb_version=kb_version,
            )
            state: AgentState = {
                "message": req.message,
                # 非法 id 在这里就归零成""（= 无会话），而不是交给下游 store 兜。
                # 否则它会走一遍 load 再拿到全 0 的计数，meta 里出现一个
                # {"history_turns":0,"facts":0} —— 和"合法的新会话"长得一模一样，
                # 于是"非法 id 被静默忽略"这件事变得不可观测，也没法用断言钉住。
                "session_id": sid,
                "use_cache": req.use_cache,
                "kb_version": kb_version,
                "t0": t0,
                "timer": StageTimer(),
                "cache": cache,
                "registry": registry,
                "memory": memory,
                "trace": trace,
                "decision": None,
                "embedding": None,
                "hit": None,
                "waited": None,
                "leader": False,
                "chunks": [],
                "recall_meta": {},
                "tool_context": "",
                "results": [],
                "memory_context": "",
                "memory_stats": {},
                "degraded": [],
                "answer": "",
                # 写操作挂起后由 node_tools 填充；非空时本轮走 await_confirm 终局
                "pending": [],
            }
            outcome = "ok"
            try:
                # custom 模式：节点中途经 stream writer 推出的帧
                # （meta / route / tool_call / docs / token / degraded / done）
                # 原样透传给客户端 —— 端点不做任何加工，协议归编排器管。
                async for frame in get_graph().astream(state, stream_mode="custom"):
                    yield frame
            except GeneratorExit:
                # 客户端提前断开（压测里常见）→ 请求发生了、但没跑完。
                # 不与 result=ok 混算，否则"完成率"永远 100%，断连率不可见。
                outcome = "aborted"
                raise
            except Exception as exc:  # noqa: BLE001 - 兜底：图崩溃也不能 500 断流
                outcome = "error"
                log.exception("pipeline crashed: %s", exc)
                DEGRADED_TOTAL.labels(stage="pipeline", reason="exception").inc()
                trace.finish(answer="", route="", cache="", status="error",
                             error=f"{type(exc).__name__}: {exc}")
                yield sse_frame({
                    "type": "degraded",
                    "stage": "pipeline",
                    "reason": f"{type(exc).__name__}",
                    "message": "处理链路出现异常，请稍后重试",
                })
                yield sse_frame({"type": "error", "message": "处理链路出现异常，请稍后重试"})
            finally:
                # HTTP 层计数：每一次 /chat/stream 都算数 —— 包括被缓存短路
                # （不发生 LLM 调用）和客户端提前断开的。顶行"流量自检"看这个，
                # 不能看 llm_requests_total（那只是 LLM 调用数）。
                CHAT_REQUESTS.labels(result=outcome).inc()
                # 兜底：客户端提前断开 / 生成器被回收时，图跑到一半就没了 ——
                # 终局节点没机会 finish，trace 会缺 turn 记录，回放直接断在这里。
                # finish 是幂等的，正常路径（四个终局已调过）不受影响。
                trace.finish(answer="", route="", cache="", status="aborted")

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.post("/confirm")
async def confirm_action(
    req: ConfirmRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """确认 / 拒绝一条挂起的写操作，确认后**在这里执行**。

    为什么执行放在这个端点、而不是"确认后回到图里继续跑"：
    图那一轮已经结束了（SSE 连接早关了）。要"继续"就得靠 checkpointer 恢复
    —— 本项目还没接（见 README backlog）。而重新跑一遍图会把工具再规划一次，
    等于让模型有机会"第二次抽出不同的参数"，正好破坏第 2 条边界。
    所以这里是**直接执行那条记录**，不经过规划器、不经过 LLM。

    返回体是执行结果（不是 SSE）：确认动作是"一次性、同步、要有明确回执"的，
    用流式反而让"到底成没成功"变得含糊。
    """
    _require_api_key(x_api_key)
    registry = getattr(request.app.state, "tools", None)
    sandbox = getattr(registry, "sandbox", None) if registry is not None else None
    if registry is None or sandbox is None:
        raise HTTPException(status_code=503, detail="tool layer unavailable")

    sid = safe_sid(req.session_id)

    # 先看一眼这条记录属于谁：token 是动作级凭证，跨会话用它就是越权。
    # peek 不消费（消费留给执行路径的 GETDEL），所以这里读两次是安全的。
    action = await sandbox.store.peek(req.token)
    if action is None:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "status": "invalid_grant",
                     "message": "确认凭据无效、已用过或已过期"},
        )
    if action.session_id and sid and action.session_id != sid:
        # 不删记录：合法持有者可能还要用它。只拒绝这次越权尝试。
        log.warning("确认凭据跨会话使用：token=%s 属于 %s，请求方 %s",
                    req.token[:8], action.session_id, sid)
        raise HTTPException(status_code=403, detail="token 不属于该会话")

    if not req.approve:
        rejected = await sandbox.reject(req.token)
        if rejected is None:
            return JSONResponse(
                status_code=404,
                content={"ok": False, "status": "invalid_grant",
                         "message": "确认凭据已失效"},
            )
        return {"ok": True, "status": "rejected", "tool": rejected.tool,
                "args": rejected.args}

    # 执行。参数**只从记录里读** —— 这个调用连 args 都不传。
    with call_context(session_id=sid or action.session_id, message=action.message):
        result = await registry.call(action.tool, _confirm=req.token)

    if result.status == "denied":
        # token 用掉了但执行被拒（策略变更 / 越权参数）：这是失败回执，不是 404
        return JSONResponse(
            status_code=409,
            content={"ok": False, "status": "denied", "tool": result.tool,
                     "message": result.error},
        )

    return {
        "ok": result.ok,
        "status": "executed" if result.ok else "failed",
        "tool": result.tool,
        "args": result.args,
        "source": result.source,
        "simulated": result.simulated,
        "elapsed_ms": round(result.elapsed_ms, 1),
        "text": result.text,
        "error": result.error,
    }


@router.post("/warmup")
async def warmup_cache(
    req: WarmupRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """缓存预热。

    演示与压测都用得上：提前把高频问题灌进语义缓存，
    压测时的命中率才是真实的（线上也是这么干的，不算是作弊）。
    """
    _require_api_key(x_api_key)
    cache = getattr(request.app.state, "cache", None)
    if cache is None:
        raise HTTPException(status_code=503, detail="cache unavailable")

    from app.pipeline.embedding import embed

    vectors = await embed(req.questions)
    from app.cache.manager import CachedAnswer

    warmed = 0
    for question, vector in zip(req.questions, vectors):
        # 预热只写"问题"占位，真实答案在第一次被问到时回填
        await cache.set_semantic(
            question,
            CachedAnswer(answer="", queried=question, route="warmup"),
            vector,
        )
        warmed += 1
    return {"warmed": warmed, "kb_version": settings.kb_version}


@router.post("/route")
async def route_preview(
    req: ChatRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """只做路由，不执行任何检索/工具/生成。

    单独暴露它的理由：这是**唯一能低成本验证路由正确性的入口** ——
    不用跑完整链路，也不用等 LLM。面试现场可以当场问"这句话走哪条路、
    为什么"，屏幕上直接出决策依据。
    """
    _require_api_key(x_api_key)
    from app.pipeline import intent as intent_mod
    from app.tools.planner import plan, plan_summary

    decision = intent_mod.decide(req.message)
    return {
        **decision.as_event(),
        "tool_plan": [{"name": c.name, "args": c.args} for c in plan(decision, req.message)],
        "tool_plan_summary": plan_summary(plan(decision, req.message)),
    }

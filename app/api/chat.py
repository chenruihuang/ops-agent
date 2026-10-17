"""问答端点：意图路由 + 三层记忆 + 缓存 + 多路召回 + 工具 + 流式生成。

编排已迁入 LangGraph（app/pipeline/orchestrator.py）—— 本文件只保留
**端点职责**：鉴权、并发闸门、SSE 透传。图结构与状态流转见编排器 docstring。

四条硬约束（重构后依然成立，验收方式见 tests/test_chat_sse.py）
--------------------------------------------------------------
a) 缓存查询必须早于任何检索动作 —— 图序保证（cache_lookup 在 dispatch 之前）。
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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.observability.metrics import DEGRADED_TOTAL
from app.pipeline import orchestrator
from app.pipeline.orchestrator import AgentState, StageTimer, get_graph, sse_frame

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
            state: AgentState = {
                "message": req.message,
                "session_id": req.session_id,
                "use_cache": req.use_cache,
                "kb_version": await _kb_version(),
                "t0": t0,
                "timer": StageTimer(),
                "cache": cache,
                "registry": registry,
                "memory": memory,
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
            }
            try:
                # custom 模式：节点中途经 stream writer 推出的帧
                # （meta / route / tool_call / docs / token / degraded / done）
                # 原样透传给客户端 —— 端点不做任何加工，协议归编排器管。
                async for frame in get_graph().astream(state, stream_mode="custom"):
                    yield frame
            except Exception as exc:  # noqa: BLE001 - 兜底：图崩溃也不能 500 断流
                log.exception("pipeline crashed: %s", exc)
                DEGRADED_TOTAL.labels(stage="pipeline", reason="exception").inc()
                yield sse_frame({
                    "type": "degraded",
                    "stage": "pipeline",
                    "reason": f"{type(exc).__name__}",
                    "message": "处理链路出现异常，请稍后重试",
                })
                yield sse_frame({"type": "error", "message": "处理链路出现异常，请稍后重试"})

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


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

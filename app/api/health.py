from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from app.core.config import settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    """存活探针。

    刻意做得轻：只报进程存活与配置快照，不做下游依赖探测。
    依赖探测放在 /ready 里 —— 否则 Redis 抖一下就会被容器编排判定为
    "服务挂了"然后重启，反而把一次小故障放大成一次雪崩。
    """
    return {
        "status": "ok",
        "kb_version": settings.kb_version,
        "prompt_version": settings.prompt_version,
        "model": settings.deepseek_model,
    }


@router.get("/ready")
async def ready(request: Request):
    """就绪探针：逐个检查依赖，并明确标注哪些依赖不可用会导致降级。

    `critical=True` 的项挂了就不能对外服务（目前只有 embedding 模型：
    没有它连语义缓存都算不了）。其余全部标 critical=False，
    因为它们各自都有降级路径：
        向量库挂  → BM25 单路继续召回
        索引缺失  → 检索不可用，但闲聊与工具题仍能答
        工具层挂  → 只能回答知识题
        缓存挂    → 全部走真实链路，只是慢
    """
    checks: dict[str, dict] = {}

    from app.pipeline.embedding import get_embedder

    try:
        get_embedder()
        checks["embedding_model"] = {"ok": True, "critical": True}
    except Exception as exc:
        checks["embedding_model"] = {"ok": False, "critical": True, "detail": str(exc)}

    try:
        from app.pipeline.rag import get_qdrant

        client = get_qdrant()
        await client.get_collections()
        checks["qdrant"] = {"ok": True, "critical": False}
    except Exception as exc:
        checks["qdrant"] = {
            "ok": False,
            "critical": False,
            "detail": f"{exc}；向量召回将降级为 BM25 单路",
        }

    # ---- 索引产物 ----
    # 这一项以前没被检查，而它是最容易"服务起来了但一个问题都答不了"的原因：
    # data/ 是挂载进来的，忘了跑 build 就启动服务时，容器是 healthy，
    # 请求却全部 500 —— 一个存在于现实里的运维事故场景。
    try:
        import asyncio

        from app.pipeline.rag import get_bundle

        bundle = await asyncio.to_thread(get_bundle)
        checks["index"] = {
            "ok": bool(bundle.available),
            "critical": False,
            "detail": (
                f"kb_version={bundle.kb_version} · {len(bundle.chunk_to_doc)} 块 · "
                f"BM25={'ok' if bundle.bm25 is not None else 'unavailable'}"
                if bundle.available
                else "索引产物不存在，请先执行 python -m scripts.ingest --stage build"
            ),
        }
    except Exception as exc:
        checks["index"] = {"ok": False, "critical": False, "detail": str(exc)}

    # ---- 工具层 ----
    tools = getattr(request.app.state, "tools", None)
    if tools is None:
        checks["tools"] = {
            "ok": False,
            "critical": False,
            "detail": "工具层未初始化，tool/hybrid 意图将降级",
        }
    else:
        from app.tools import health_summary

        info = health_summary(tools)
        mcp_probe = info.get("mcp_probe")
        detail = f"{len(info['tools'])} 个工具 · 来源 {info['source']}"
        if info.get("mcp_url"):
            detail += f" · MCP({info['mcp_url']}) " + (
                "已连接" if mcp_probe else ("未连接，已降级到 mock" if mcp_probe is False else "未探测")
            )
        checks["tools"] = {"ok": True, "critical": False, "detail": detail}

    checks["cache_stub"] = {"ok": True, "critical": False,
                            "detail": "缓存挂掉只影响命中率，不影响可用性"}

    ready_ok = all(c["ok"] for c in checks.values() if c["critical"])
    return {"ready": ready_ok, "checks": checks}

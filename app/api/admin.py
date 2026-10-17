"""运维端点。

这几个接口不是"顺手加的"，它们是**可演示能力**：
    POST /admin/kb/bump     演示"知识库更新 → 全副本缓存秒级失效"
    GET  /admin/cache/stats 演示缓存占用与版本号
    POST /admin/cache/purge 演示主动失效

面试演示时这几个接口比 PPT 有说服力得多 —— 你能当场证明时效性设计是真的。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from app.cache.keys import EXACT_PREFIX, SEMANTIC_PREFIX
from app.core.config import settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin_key(x_api_key: str | None) -> None:
    if not x_api_key or x_api_key != settings.api_key.get_secret_value():
        raise HTTPException(status_code=401, detail="invalid api key")


def _cache(request: Request):
    cache = getattr(request.app.state, "cache", None)
    if cache is None:
        raise HTTPException(status_code=503, detail="cache unavailable")
    return cache


@router.get("/cache/stats")
async def cache_stats(request: Request, x_api_key: str | None = Header(default=None)):
    _require_admin_key(x_api_key)
    return await _cache(request).stats()


@router.post("/kb/bump")
async def bump_kb_version(request: Request, x_api_key: str | None = Header(default=None)):
    """知识库更新后调用。

    做了什么事：只把版本号 +1。
    效果：**所有副本**的新请求立刻走上"未命中"路径，拿到新知识库的答案。
    没有做的事：没有删除任何缓存、没有停机、没有预热窗口。
    """
    _require_admin_key(x_api_key)
    cache = _cache(request)
    previous = await cache.current_kb_version()
    current = await cache.bump_kb_version()

    import time

    from app.observability.metrics import EVAL_LAST_RUN

    EVAL_LAST_RUN.set(time.time())
    return {"previous": previous, "current": current, "note": "全副本缓存已即时失效"}


class PurgeRequest(BaseModel):
    layer: str = "all"      # all | exact | semantic


@router.post("/cache/purge")
async def purge_cache(
    req: PurgeRequest,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    """主动释放缓存。正常流程用不到（版本号就够了），
    用于运维上需要立刻回收内存的场景。"""
    _require_admin_key(x_api_key)
    cache = _cache(request)

    targets = []
    if req.layer in ("all", "exact"):
        targets.append(EXACT_PREFIX)
    if req.layer in ("all", "semantic"):
        targets.append(SEMANTIC_PREFIX)

    removed = 0
    for prefix in targets:
        removed += await cache.invalidate_by_prefix(prefix)
    return {"removed": removed, "layer": req.layer}

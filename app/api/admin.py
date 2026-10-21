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
    return {"previous": previous, "current": current, "note": "全副本缓存已即时失效"}


class EvalMetrics(BaseModel):
    """评估产物上报体。字段全部可选 —— 没算出来的指标就不传，**不要传 0**。

    `anchor_hit_rate` 不是 faithfulness（它只回答"标注事实有没有出现在答案里"），
    所以它单独一个字段，绝不复用 faithfulness 的 gauge。
    """

    context_recall: float | None = None
    context_precision: float | None = None
    anchor_hit_rate: float | None = None
    top_k: int = 5


@router.post("/eval/metrics")
async def publish_eval_metrics(
    payload: EvalMetrics,
    x_api_key: str | None = Header(default=None),
):
    """把 `scripts.run_eval` 的汇总结果写进**本进程**的 registry，供 /metrics 抓取。

    为什么需要这个端点：评估脚本是独立进程，它写自己的 REGISTRY 没人抓 ——
    大盘上那两个面板会永远是空的，而"空"和"没跑过评估"看起来一模一样。
    这是把"评估产物"接进可观测层的最小正确做法（不引入 Pushgateway）。
    """
    _require_admin_key(x_api_key)

    import time

    from app.observability.metrics import (
        ANCHOR_HIT_RATE,
        CONTEXT_PRECISION,
        CONTEXT_RECALL,
        EVAL_LAST_RUN,
    )

    top_k = str(payload.top_k)

    # 先校验全部字段再落库：三项里有一项越界就整体拒绝，
    # 避免"写进去两项、第三项 422"制造半新半旧的指标组合。
    checks = [
        ("context_recall", payload.context_recall),
        ("context_precision", payload.context_precision),
        ("anchor_hit_rate", payload.anchor_hit_rate),
    ]
    for name, value in checks:
        # 比例类指标越界几乎一定是口径 bug（历史踩过：AP 未按文档去重算出 1.31）。
        # 让它 422 而不是把 1.31 画到仪表盘上 —— 面板不会报警，人会。
        if value is not None and not 0.0 <= value <= 1.0:
            raise HTTPException(
                status_code=422,
                detail=f"{name}={value} 不在 [0,1]，拒绝写入（比例口径有 bug）",
            )

    # 显式逐个写：不走 symbol 循环，是为了让"谁在写这个指标"在代码里可 grep 到
    # （大盘守门用例正是靠每个指标有明确写入点来判断有没有死面板）。
    written: list[str] = []
    skipped: list[str] = []
    if payload.context_recall is not None:
        CONTEXT_RECALL.labels(top_k=top_k).set(payload.context_recall)
        written.append("context_recall")
    else:
        skipped.append("context_recall")      # 没测过 ≠ 0 分，宁可缺席
    if payload.context_precision is not None:
        CONTEXT_PRECISION.labels(top_k=top_k).set(payload.context_precision)
        written.append("context_precision")
    else:
        skipped.append("context_precision")
    if payload.anchor_hit_rate is not None:
        ANCHOR_HIT_RATE.labels(top_k=top_k).set(payload.anchor_hit_rate)
        written.append("anchor_hit_rate")
    else:
        skipped.append("anchor_hit_rate")

    if written:
        EVAL_LAST_RUN.set(time.time())
        try:
            from app.observability.metrics import persist_eval

            # 落盘再返回：否则下一次 `docker compose up -d api` 会把面板清空，
            # 而"评估结果没了"和"评估没跑过"看起来一样。
            persist_eval(
                {
                    "context_recall": payload.context_recall,
                    "context_precision": payload.context_precision,
                    "anchor_hit_rate": payload.anchor_hit_rate,
                },
                payload.top_k,
            )
        except OSError as exc:      # 只读目录等情况不该让上报失败
            log.warning("eval metrics 落盘失败（下次重启会丢）: %s", exc)
    log.info("eval metrics published top_k=%s written=%s skipped=%s", top_k, written, skipped)
    return {"written": written, "skipped": skipped, "top_k": payload.top_k}



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


@router.get("/sandbox/audit")
async def sandbox_audit(
    request: Request,
    limit: int = 20,
    x_api_key: str | None = Header(default=None),
):
    """最近的沙箱审计记录（写/危险动作与被拒绝的调用）。

    单独做一个只读端点，是因为审计的价值在**事后核对**：
    演示时被问"凭什么说这次重启是人批的"，直接把这 20 条甩出来，
    比任何架构图都有说服力。只读、只返回尾部 N 条，不做过滤/聚合 ——
    需要复杂查询就上日志系统，这里只负责"当场能看"。
    """
    _require_admin_key(x_api_key)
    tools = getattr(request.app.state, "tools", None)
    sandbox = getattr(tools, "sandbox", None) if tools is not None else None
    audit = getattr(sandbox, "audit", None) if sandbox is not None else None
    if audit is None:
        return {"records": [], "available": False}
    return {
        "available": True,
        # `writable=false` 时 records 为空**不代表没有动作**，而是没写下去。
        # 这两个信息必须一起给，否则排障方向会完全错（去看业务了）。
        "writable": audit.writable,
        "last_error": audit.last_error,
        "path": str(audit.path),
        "store": getattr(getattr(sandbox, "store", None), "source", "none"),
        "enabled": bool(getattr(sandbox, "enabled", False)),
        "records": audit.tail(max(1, min(limit, 200))),
    }

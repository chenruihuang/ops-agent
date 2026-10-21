from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from app.core.config import settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    """存活探针。

    刻意做得轻：只报进程存活与配置快照，**不做任何下游调用**。
    依赖探测放在 /ready 里 —— 否则 Redis 抖一下就会被容器编排判定为
    "服务挂了"然后重启，反而把一次小故障放大成一次雪崩。

    字段叫 `config_kb_version` 而不是 `kb_version`，是因为它真的是
    「进程启动时的配置快照」。运行时版本（Redis 里那个，`/chat/stream`
    的 `meta.kb_version` 用的就是它）在 `/ready` 的 `cache` 项里报。

    这个改名来自一次自测日志的自相矛盾：打过一次 `/admin/kb/bump` 之后
    `/health` 说 `kb_version=1`、`/chat/stream` 的 meta 说 `kb_version=2` ——
    同一个名字在两个接口上指向两个不同的值，人会以为 bump 没生效。
    """
    return {
        "status": "ok",
        "config_kb_version": settings.kb_version,
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
    index_kb_version: int | None = None
    try:
        import asyncio

        from app.pipeline.rag import get_bundle

        bundle = await asyncio.to_thread(get_bundle)
        index_kb_version = getattr(bundle, "kb_version", None) if bundle.available else None
        checks["index"] = {
            "ok": bool(bundle.available),
            "critical": False,
            "detail": (
                f"index_kb_version={bundle.kb_version} · {len(bundle.chunk_to_doc)} 块 · "
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
        # 沙箱状态必须出现在就绪探针里：`store=memory` 意味着多副本下
        # 确认请求打到另一个副本就找不到待办 —— 这不是"降级"，是**功能不可用**，
        # 但它不会报错，只会 404。所以让它在一眼能看到的地方。
        sbx = info.get("sandbox") or {}
        if sbx:
            write_tools = len(info.get("write_tools") or [])
            if not sbx.get("enabled"):
                detail += f" · 沙箱已关闭（{write_tools} 个写工具将被拒绝）"
            else:
                detail += (f" · 沙箱 {sbx.get('store')}（{write_tools} 个写工具需确认，"
                           f"TTL {sbx.get('pending_ttl')}s）")
        checks["tools"] = {"ok": True, "critical": False, "detail": detail}

    # ---- 缓存 ----
    # 以前这里是个写死的 `{"ok": True, "detail": "缓存挂掉只影响命中率"}` ——
    # 一句正确的废话，什么都没探。它同时是**唯一**能报出运行时 kb_version 的地方
    # （/health 只报配置快照，见 health() 的注释），所以必须真的去读一次。
    #
    # 为什么值得探版本号本身：`/admin/kb/bump` 只改 Redis 里的版本号、不动索引产物。
    # bump 完却没重建索引，缓存命名空间就"跑在"索引版本前面 —— 正确性没问题
    # （旧 key 只是没人读，等价于全量冷启动），但这事必须以可读的形式暴露出来，
    # 否则排查"缓存为什么全 miss"时要翻 Redis key 才能看出来。
    runtime_kb: int | None = None
    cache = getattr(request.app.state, "cache", None)
    if cache is None:
        checks["cache"] = {
            "ok": False,
            "critical": False,
            "detail": "缓存未初始化，全部走真实链路（只是慢）",
        }
    else:
        try:
            import asyncio

            # 2s 上限：这是探针，宁可报"缓存不可用"也不能把 /ready 拖住。
            runtime_kb = await asyncio.wait_for(cache.current_kb_version(), timeout=2.0)
            detail = f"kb_version={runtime_kb}（运行时/缓存命名空间）"
            if index_kb_version is not None and int(index_kb_version) != int(runtime_kb):
                detail += (
                    f" · 索引版本 {index_kb_version} ≠ 缓存版本 {runtime_kb}："
                    "bump 过但没重建索引，缓存等价于全量冷启动（不影响答案正确性）"
                )
            checks["cache"] = {"ok": True, "critical": False, "detail": detail}
        except Exception as exc:  # noqa: BLE001
            checks["cache"] = {
                "ok": False,
                "critical": False,
                "detail": f"{exc}；缓存不可用，命中率降为 0，可用性不受影响",
            }

    ready_ok = all(c["ok"] for c in checks.values() if c["critical"])
    return {
        "ready": ready_ok,
        # 顶层也放一份运行时版本：程序化比对（自测脚本、压测报告）不该去解析 detail 字符串。
        # 与 /chat/stream 的 meta.kb_version 同源。
        "kb_version": runtime_kb if runtime_kb is not None else settings.kb_version,
        "kb_version_source": "runtime" if runtime_kb is not None else "config",
        "checks": checks,
    }

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from app.api import admin, chat, health
from app.core.config import settings

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- 缓存 ----
    try:
        from app.cache.factory import build_cache

        app.state.cache = build_cache()
        await app.state.cache.ensure_semantic_index()
        log.info("cache ready (kb_version=%s)", await app.state.cache.current_kb_version())
    except Exception as exc:
        # 缓存不可用绝不能导致服务起不来 —— 这是可用性设计的一部分
        log.error("cache init failed, running without cache: %s", exc)
        app.state.cache = None

    # ---- 三层记忆（会话 + 长期事实；工作记忆 = LangGraph state） ----
    # 同样不允许拖垮启动：Redis 不可用时 build_session_store 仍会成功
    # （客户端是懒连接），真正的失败在各次 load/save 时降级并进 degraded。
    try:
        from app.memory.session import build_session_store

        app.state.memory = build_session_store()
        log.info("session memory ready (%s)", type(app.state.memory).__name__)
    except Exception as exc:
        log.error("session memory init failed, 会话记忆将整层降级: %s", exc)
        app.state.memory = None

    # ---- 工具 ----
    # 工具层同样不允许拖垮启动：MCP 未就绪时 build_registry 不会去连它
    # （探测是懒的、带 TTL），mock provider 保证 tool/hybrid 链路离线可用。
    try:
        from app.tools import build_registry

        app.state.tools = build_registry(settings=settings, cache=app.state.cache)
        log.info(
            "tools ready: %d 个（%s）· 超时 %.1fs",
            len(app.state.tools.names()), app.state.tools.source, app.state.tools.timeout,
        )
    except Exception as exc:
        log.error("tool registry init failed, tool/hybrid 链路将降级: %s", exc)
        app.state.tools = None

    # ---- 索引（提前加载）----
    # 不预热的话，第一个请求会承担读 points.jsonl + 建 BM25 内存索引的时间，
    # 那一次的首 token 延迟会被算进 TTFT 基线；压测时更糟 ——
    # 这一个请求的 P95 会污染整个并发档位的判定。
    try:
        from app.pipeline.rag import get_bundle

        bundle = await asyncio.to_thread(get_bundle)
        if bundle.available:
            log.info(
                "index ready: kb_version=%s · %d 块 · BM25 %s",
                bundle.kb_version, len(bundle.chunk_to_doc),
                f"{bundle.bm25.size} 块" if bundle.bm25 is not None else "不可用",
            )
        else:
            log.warning(
                "索引产物不存在（%s）—— 检索会失败。请先执行 "
                "python -m scripts.ingest --stage build",
                settings.kb_index_dir,
            )
    except Exception as exc:
        log.error("index preload failed: %s", exc)

    # ---- 向量库预热 ----
    # 实测数据（本机 Windows，468 点）：Qdrant 的**首次**查询要 8.9 秒
    # —— 服务端要把 HNSW 索引加载进内存；之后稳定在 200ms 上下。
    #
    # 不预热的话，第一个用户请求要白等这 9 秒。更糟的是压测场景：
    # 阶梯加压的第一档就是第一个请求，这 9 秒会被当成
    # "这一档并发撑不住"的证据 —— 一个纯粹的误判，而且看起来很像真的。
    # 所以预热不是优化，是让后续测量有效的前提。
    try:
        from app.pipeline.rag import get_store

        store = get_store()
        t0 = asyncio.get_event_loop().time()
        points = await store.count()
        log.info(
            "vector store warmed up: %s（%d 点，首次往返 %.1fs）",
            store.name, points, asyncio.get_event_loop().time() - t0,
        )
    except Exception as exc:
        log.error("vector store warmup failed（向量召回将降级为 BM25 单路）: %s", exc)

    # ---- 模型预热 ----
    # 不预热的话，第一个真实请求会承担 BGE 模型加载的时间。
    # 实测本机为 **110 秒**（主要是 torch + sentence-transformers 的导入与初始化），
    # 那样测出来的 TTFT 基线完全是假的（压测时 P95 会被这一个请求污染）。
    try:
        from app.pipeline.embedding import warmup

        t0 = asyncio.get_event_loop().time()
        await asyncio.to_thread(warmup)
        log.info("models warmed up（%.1fs）", asyncio.get_event_loop().time() - t0)
    except Exception as exc:
        log.error("model warmup failed: %s", exc)

    yield

    # ---- 关闭 ----
    # 向量库客户端必须显式关：否则 --scale api=3 滚动重启时，
    # 旧副本的连接会挂到对端超时，Qdrant 侧表现为"连接数缓慢泄漏"。
    try:
        from app.pipeline.rag import aclose_store

        await aclose_store()
    except Exception:  # noqa: BLE001
        pass

    cache = getattr(app.state, "cache", None)
    if cache is not None:
        try:
            await cache._redis.aclose()  # noqa: SLF001
        except Exception:
            pass

    memory = getattr(app.state, "memory", None)
    if memory is not None:
        try:
            await memory.aclose()
        except Exception:
            pass


app = FastAPI(title="Ops Agent Platform", version="0.1.0", lifespan=lifespan)

# /metrics —— Prometheus 抓取入口
Instrumentator(excluded_handlers=["/metrics", "/health", "/ready"]).instrument(app).expose(app)

app.include_router(chat.router)
app.include_router(health.router)
app.include_router(admin.router)

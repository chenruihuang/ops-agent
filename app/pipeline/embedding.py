"""向量化与重排模型。

注意：这两个模型是**同步且吃 CPU** 的。在 async 端点里直接调用会阻塞事件循环，
并发一上来 TTFT 就会崩。所以统一走线程池（asyncio.to_thread）。

这也是 cs-agent 里没有做、但压测一定会暴露的问题。
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Sequence

from app.core.config import settings

log = logging.getLogger(__name__)

_embedder = None
_reranker = None


def get_embedder():
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer

        log.info("loading embedding model: %s", settings.embedding_model)
        _embedder = SentenceTransformer(settings.embedding_model, device="cpu")
    return _embedder


def get_reranker():
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder

        log.info("loading reranker model: %s", settings.reranker_model)
        _reranker = CrossEncoder(settings.reranker_model, device="cpu")
    return _reranker


def warmup() -> None:
    """容器启动时预热，避免第一个真实请求承担模型加载时间（否则 TTFT 基线是假的）。"""
    get_embedder().encode(["预热"], normalize_embeddings=True)
    get_reranker().predict([("预热", "预热")])


def _embed_sync(texts: Sequence[str]) -> list[list[float]]:
    vectors = get_embedder().encode(
        list(texts), normalize_embeddings=True, batch_size=16
    )
    return [v.tolist() for v in vectors]


async def embed(texts: Sequence[str]) -> list[list[float]]:
    return await asyncio.to_thread(_embed_sync, texts)


async def embed_one(text: str) -> list[float]:
    return (await embed([text]))[0]


def _score_pairs_sync(pairs: Sequence[tuple[str, str]]) -> list[float]:
    return [float(s) for s in get_reranker().predict(list(pairs))]


async def score_pairs(pairs: Sequence[tuple[str, str]]) -> list[float]:
    """CrossEncoder 打分。放在这里是因为它和 embedding 一样都是"同步吃 CPU"的重活。"""
    if not pairs:
        return []
    return await asyncio.to_thread(_score_pairs_sync, pairs)


@lru_cache(maxsize=2048)
def _cached_embed_cached_text(text: str) -> tuple[float, ...]:
    """进程内 L0 缓存：同一段文本（文档切块）不会重复算向量。"""
    return tuple(_embed_sync([text])[0])

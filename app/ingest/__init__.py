"""知识库入库（ingestion）。

链路：语料文件 → 切块 → 多模态补全 → 向量 + 关键词索引 → 版本号推进 → 报告

对外入口是 `scripts/ingest.py`；本包只提供可组合、可单测的构件。
"""

from app.ingest.chunker import Chunk, DocChunks, chunk_document, chunk_stats
from app.ingest.frontmatter import parse_frontmatter
from app.ingest.images import ImageDoc, build_image_chunks, build_rule_caption, load_images
from app.ingest.indexer import (
    Point,
    build_points,
    build_registry,
    neighbor_map,
    point_id_for,
    read_points,
    read_vector_meta,
    read_vectors,
    texts_fingerprint,
    write_points,
    write_vectors,
)
from app.ingest.loader import LoadResult, load_markdown
from app.ingest.textindex import BM25Index, build_bm25, tokenize
from app.ingest.version import read_local_version, sync_redis_version, write_local_version

__all__ = [
    "BM25Index",
    "Chunk",
    "DocChunks",
    "ImageDoc",
    "LoadResult",
    "Point",
    "build_bm25",
    "build_image_chunks",
    "build_points",
    "build_registry",
    "build_rule_caption",
    "chunk_document",
    "chunk_stats",
    "load_images",
    "load_markdown",
    "neighbor_map",
    "parse_frontmatter",
    "point_id_for",
    "read_local_version",
    "read_points",
    "read_vector_meta",
    "read_vectors",
    "sync_redis_version",
    "texts_fingerprint",
    "tokenize",
    "write_local_version",
    "write_points",
    "write_vectors",
]

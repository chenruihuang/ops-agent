"""Markdown 语料装载：扫描 → 解析 frontmatter → 切块。

只做"文件 → DocChunks"这一件事，不做 IO 之外的事，
这样 build 阶段可以完全离线跑，也能被单元测试直接调用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.ingest.chunker import (
    MAX_CHARS,
    MERGE_MIN_CHARS,
    OVERLAP_CHARS,
    DocChunks,
    chunk_document,
)
from app.ingest.frontmatter import parse_frontmatter

log = logging.getLogger(__name__)

# 目录名/文件名黑名单：这些不是知识库内容
_SKIP_DIRS = {"_tmp", "__pycache__", "images", "transcripts"}
_SKIP_FILES = {"README.md", "MANIFEST.json"}


@dataclass(slots=True)
class LoadResult:
    docs: list[DocChunks] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    by_category: dict[str, int] = field(default_factory=dict)
    source_files: int = 0


def iter_markdown(root: Path) -> list[Path]:
    files: list[Path] = []
    for p in sorted(root.rglob("*.md")):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.name in _SKIP_FILES:
            continue
        files.append(p)
    return files


def load_markdown(
    root: Path,
    *,
    max_chars: int = MAX_CHARS,
    merge_min_chars: int = MERGE_MIN_CHARS,
    overlap: int = OVERLAP_CHARS,
) -> LoadResult:
    """扫描目录下全部 .md，返回切好块的文档集合。

    没有 `doc_id` 的 markdown 会被跳过并记入 `skipped` —— 这类文件通常是
    说明文档，误入库会污染检索。**跳过要留痕**，否则"少了 3 篇"这种问题
    在评估阶段才会暴露，而那时已经很难回溯。
    """
    result = LoadResult()

    for path in iter_markdown(root):
        text = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        doc_id = str(meta.get("doc_id") or "").strip()
        if not doc_id:
            result.skipped.append(f"{path.as_posix()} (缺少 doc_id)")
            continue

        rel = path.relative_to(root.parent.parent).as_posix() if root.parent.parent in path.parents else path.as_posix()
        doc = chunk_document(
            doc_id=doc_id,
            meta=meta,
            body=body,
            source=rel,
            max_chars=max_chars,
            merge_min_chars=merge_min_chars,
            overlap=overlap,
        )
        if not doc.chunks:
            result.skipped.append(f"{doc_id} (切块后为空)")
            continue

        result.docs.append(doc)
        result.by_category[doc.category] = result.by_category.get(doc.category, 0) + 1
        result.source_files += 1

    result.docs.sort(key=lambda d: (d.category, d.doc_id))
    return result


def all_chunks(docs: list[DocChunks]):
    for d in docs:
        yield from d.chunks


def build_scope_summary(docs: list[DocChunks]) -> dict[str, object]:
    """检索范围摘要，写进报告 —— 面试被问"你索引了多少数据"时要能立刻答。"""
    chunks = list(all_chunks(docs))
    return {
        "documents": len(docs),
        "chunks": len(chunks),
        "by_category": {
            cat: sum(1 for c in chunks if c.category == cat)
            for cat in sorted({c.category for c in chunks})
        },
        "categories": sorted({c.category for c in docs}),
    }

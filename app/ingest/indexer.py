"""索引层：点构建 → 向量入库 → 关键词索引 → 文档注册表。

产物清单（`data/index/`）
------------------------
    points.jsonl        全部 chunk 的 payload（不含向量）—— build 阶段产出，离线可查
    vectors.bin/.meta   向量缓存（float32）—— index 阶段产出，供本地暴力检索与回归对比
    bm25.json           BM25 关键词索引 —— build 阶段产出
    registry.json       文档/截图注册表 + 图谱边 —— build 阶段产出
    index_manifest.json 索引元信息（维度/模型/块数/版本）—— 供服务端做兼容性校验
    ingest_report.json  本次入库报告

两个刻意的设计
--------------
1. **点 id 用 uuid5(chunk_id) 派生**，不用随机 uuid。
   同一份语料重复入库 → 生成完全相同的 id → upsert 幂等，不会产生重复点。
   随机 uuid 在"重跑一次入库"之后会让库里出现双份数据，而且很难察觉。
2. **不做整库删除重建**。依赖"新版本号 + 旧数据自然过期"，
   代价是存储有短暂冗余，收益是**入库过程中不出现空库窗口**。
   这与缓存失效用的是同一套版本号机制。
"""

from __future__ import annotations

import array
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.ingest.chunker import Chunk

log = logging.getLogger(__name__)

POINT_NAMESPACE = uuid.UUID("6f3d1c2a-9b4e-5f6a-8c7d-1e2f3a4b5c6d")

POINTS_FILE = "points.jsonl"
BM25_FILE = "bm25.json"
REGISTRY_FILE = "registry.json"
MANIFEST_FILE = "index_manifest.json"
REPORT_FILE = "ingest_report.json"
VECTORS_BIN = "vectors.bin"
VECTORS_META = "vectors.meta.json"


# =====================================================================
# 点构建
# =====================================================================
@dataclass(slots=True)
class Point:
    id: str
    chunk_id: str
    doc_id: str
    text: str
    payload: dict[str, Any] = field(default_factory=dict)
    vector: list[float] | None = None


def point_id_for(chunk_id: str) -> str:
    """稳定 id：同 chunk_id 永远得到同一个点 id（幂等重入的基础）。"""
    return str(uuid.uuid5(POINT_NAMESPACE, chunk_id))


def chunk_to_payload(chunk: Chunk, *, kb_version: int) -> dict[str, Any]:
    return {
        "doc_id": chunk.doc_id,
        "chunk_id": chunk.chunk_id,
        "text": chunk.text,
        "body": chunk.body,
        "heading": chunk.heading,
        "section_path": " > ".join(chunk.section_path),
        "category": chunk.category,
        "title": chunk.title,
        "tags": list(chunk.tags),
        "source": chunk.source,
        "modality": chunk.modality,
        "kind": chunk.kind,
        "char_len": chunk.char_len,
        "kb_version": kb_version,
        "ref": chunk.ref(),
    }


def build_points(
    chunks: Iterable[Chunk],
    *,
    kb_version: int,
    related_by_doc: dict[str, list[str]] | None = None,
) -> list[Point]:
    """Chunk → Point。`related_by_doc` 把图谱边写进 payload，
    这样即使只连 Qdrant（不读 registry），多跳扩展也能工作。"""
    related_by_doc = related_by_doc or {}
    points: list[Point] = []
    for c in chunks:
        payload = chunk_to_payload(c, kb_version=kb_version)
        payload["related_docs"] = related_by_doc.get(c.doc_id, [])
        points.append(
            Point(
                id=point_id_for(c.chunk_id),
                chunk_id=c.chunk_id,
                doc_id=c.doc_id,
                text=c.text,
                payload=payload,
            )
        )
    return points


# =====================================================================
# points.jsonl 读写
# =====================================================================
def write_points(path: Path, points: Sequence[Point]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for p in points:
            fh.write(
                json.dumps(
                    {"id": p.id, "chunk_id": p.chunk_id, "doc_id": p.doc_id, "payload": p.payload},
                    ensure_ascii=False,
                )
                + "\n"
            )
    log.info("已写出 %d 个点 → %s", len(points), path)


def read_points(path: Path) -> list[Point]:
    points: list[Point] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            points.append(
                Point(
                    id=raw["id"],
                    chunk_id=raw["chunk_id"],
                    doc_id=raw["doc_id"],
                    text=raw["payload"].get("text", ""),
                    payload=raw["payload"],
                )
            )
    return points


# =====================================================================
# 向量缓存（float32 二进制 + 元信息）
# =====================================================================
def texts_fingerprint(ids: Sequence[str], texts: Sequence[str]) -> str:
    """(chunk_id, 文本) 序列的内容指纹 —— 「向量能否安全复用」的判据。

    为什么不能只比 chunk_id：改文档正文时 chunk 边界经常不变（把阈值 800 改成 1000，
    段落数、块数、id 全都一样），此时 ids 序列完全相同，但向量是**旧文本**算出来的。
    只比 id 就会静默复用旧向量，表现为「BM25 能查到新内容、向量路查不到」——极难排查。

    这里把 id 与文本一起哈希，因此以下任一变化都会让指纹改变：
    增删块、块顺序变化、任意一个块的正文字符变化。
    """
    if len(ids) != len(texts):
        raise ValueError(f"ids 与 texts 长度不一致：{len(ids)} vs {len(texts)}")
    digest = hashlib.sha256()
    for cid, text in zip(ids, texts):
        digest.update(cid.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(text.encode("utf-8"))
        digest.update(b"\x01")
    return digest.hexdigest()


def read_vector_meta(meta_path: Path) -> dict[str, Any]:
    """只读向量缓存的 meta（不加载 bin），用于复用前的指纹比对。"""
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_vectors(
    path: Path,
    meta_path: Path,
    ids: list[str],
    vectors: Sequence[Sequence[float]],
    *,
    text_sha: str | None = None,
) -> str:
    """写入向量缓存（float32 二进制 + 元信息）。

    text_sha 由 `texts_fingerprint` 生成，随缓存一起落盘，供下次复用前校验。
    老格式（无该字段）仍然可读，只是复用时会保守重算一次。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    dim = len(vectors[0]) if vectors else 0
    buf = array.array("f")
    for v in vectors:
        if len(v) != dim:
            raise ValueError(f"向量维度不一致：期望 {dim}，实际 {len(v)}")
        buf.extend(v)
    with path.open("wb") as fh:
        fh.write(buf.tobytes())
    meta: dict[str, Any] = {
        "dim": dim,
        "count": len(ids),
        "dtype": "float32",
        "ids": ids,
    }
    if text_sha is not None:
        meta["text_sha"] = text_sha
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    log.info("向量缓存已落盘：%s（%d × %d）", path, len(ids), dim)
    return text_sha or ""


def read_vectors(path: Path, meta_path: Path) -> tuple[list[str], list[list[float]], int]:
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    dim = int(meta["dim"])
    buf = array.array("f")
    raw = path.read_bytes()
    buf.frombytes(raw)
    ids: list[str] = list(meta["ids"])
    vectors = [list(buf[i * dim : (i + 1) * dim]) for i in range(len(ids))]
    return ids, vectors, dim


# =====================================================================
# 注册表（文档 + 截图 + 图谱边）
# =====================================================================
def build_registry(
    *,
    docs: list,
    images: list,
    kb_version: int,
    generated: str,
) -> dict[str, Any]:
    doc_entries: dict[str, Any] = {}
    edges: set[tuple[str, str]] = set()

    for d in docs:
        doc_entries[d.doc_id] = {
            "type": "document",
            "title": d.title,
            "category": d.category,
            "tags": list(d.tags),
            "source": d.path,
            "chunk_ids": [c.chunk_id for c in d.chunks],
            "related": list(d.related),
            "mentions": list(d.mentions),
            "char_len": d.char_len,
        }
        for t in d.related:
            edges.add((d.doc_id, t))
        for t in d.mentions:
            edges.add((d.doc_id, t))

    image_entries: dict[str, Any] = {}
    for im in images:
        image_entries[im.image_id] = {
            "type": "image",
            "caption": im.caption,
            "kind": im.kind,
            "source": im.file,
            "chunk_ids": [c.chunk_id for c in im.chunks],
            "related_docs": list(im.related_docs),
            "ocr_chars": im.ocr_chars,
        }
        for t in im.related_docs:
            edges.add((im.image_id, t))

    # mentions 里可能引用到不存在的 id（语料撰写笔误），过滤掉并留痕
    known = set(doc_entries) | set(image_entries)
    dangling = sorted({t for _, t in edges if t not in known})
    edges = {(s, t) for s, t in edges if s in known and t in known}

    return {
        "kb_version": kb_version,
        "generated": generated,
        "documents": doc_entries,
        "images": image_entries,
        "edges": sorted([list(e) for e in edges]),
        "dangling_refs": dangling,
        "stats": {
            "documents": len(doc_entries),
            "images": len(image_entries),
            "edges": len(edges),
            "dangling_refs": len(dangling),
        },
    }


def load_registry(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def neighbor_map(registry: dict[str, Any]) -> dict[str, list[str]]:
    """无向邻接表：图谱一跳扩展用。"""
    adj: dict[str, list[str]] = {}
    for src, dst in registry.get("edges", []):
        adj.setdefault(src, []).append(dst)
        adj.setdefault(dst, []).append(src)
    return adj


# =====================================================================
# 向量库写入 —— 实现已抽到 app.ingest.stores，这里只保留兼容入口
# =====================================================================
# 说明：`QdrantUpserter` 曾是这个模块里的具体类。现在存储层统一走
# `stores.VectorStore` 协议，支持 Qdrant / Chroma / Local 三种后端，
# 这里保留这个名字作为别名，避免已有调用方（含测试）一起改。
from app.ingest.stores import LocalStore, QdrantStore, VectorStore, build_store  # noqa: E402

QdrantUpserter = QdrantStore


# =====================================================================
# 本地暴力检索（无向量库时的基线通道）
# =====================================================================
def brute_force_search(
    query_vector: Sequence[float],
    ids: list[str],
    vectors: Sequence[Sequence[float]],
    *,
    top_k: int = 20,
) -> list[tuple[str, float]]:
    """余弦相似度暴力检索。

    有了它，"召回率基线"不必等向量库起来就能测 —— 而且它同时是
    向量库结果的对照组：两者分数若明显不一致，说明向量库侧的配置
    （距离度量 / 归一化 / 过滤）有问题，而不是模型有问题。
    """
    from app.ingest.stores import _cosine

    scores = [(cid, _cosine(query_vector, vec)) for cid, vec in zip(ids, vectors)]
    scores.sort(key=lambda kv: kv[1], reverse=True)
    return [(cid, round(s, 6)) for cid, s in scores[:top_k]]

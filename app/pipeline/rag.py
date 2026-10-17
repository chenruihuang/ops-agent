"""检索与生成主链路。

数据流
------
    query
      ├─ 向量召回（走 stores 抽象层，Qdrant/Chroma/Local 同一路径）
      └─ BM25 召回（精准救错误码、命令、参数名 —— 向量的钝区）
            └─ RRF 融合（只用排名，免疫"向量 0~1 vs BM25 无上界"的量纲问题）
                  └─ CrossEncoder 精排 → top_k → 拼上下文 → 生成

三条不能省的约束
----------------
1. **向量召回必须走 `stores.build_store`，不能用裸 qdrant_client。**
   评估脚本走抽象层、服务端走裸客户端，是"评估说好、线上不是那回事"的
   经典来源。之前服务端就是裸客户端，现已统一 —— 换后端只需改 .env 一行。

2. **必须按 kb_version 过滤。**
   入库刻意**不做整库删除重建**（见 app/ingest/indexer.py 的设计说明），
   代价是旧版本的点仍在库里。所以过滤不是优化，是那个设计成立的前提：
   少了它，知识库更新后新旧点会一起被召回，用户拿到两个版本混起来的答案。
   这个缺陷**不会报错**，只会让答案悄悄变质。

3. **索引热重载。**
   服务端进程长驻，而 `data/index/` 会被重新生成。所以每次检索前检查
   manifest 的 (size, mtime)，变了就重建内存索引 —— 入库后不必重启服务。
   检查成本是一次 stat（微秒级），远小于一次向量检索。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

from app.core.config import settings
from app.observability.metrics import DEGRADED_TOTAL, REQUEST_TOTAL, STAGE_SECONDS
from app.pipeline.embedding import embed_one, score_pairs
from app.pipeline.fusion import cap_per_unit, rrf_fuse_items

log = logging.getLogger(__name__)

# 召回通道名
CH_VECTOR = "vector"
CH_BM25 = "bm25"


# =====================================================================
# 索引产物
# =====================================================================
@dataclass(slots=True)
class Chunk:
    """检索结果块。

    注意 `doc_id` 是**评估集标注的锚点**，绝不能丢 —— 召回率口径是文档级，
    doc_id 丢了整个评估链路就断了。所以它在第一个字段。
    """

    doc_id: str
    chunk_id: str
    text: str
    source: str = ""
    page: int | None = None
    modality: str = "text"          # text | image_ocr | image_caption | table
    score: float = 0.0
    rerank_score: float | None = None
    recall_source: str = "vector"   # vector | bm25 | vector+bm25 | graph
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RecallOutcome:
    """召回结果 + 每路命中数。

    `channels` 一定要往 SSE 里推：不然用户（和面试官）无法判断
    "答案里那个错误码到底是向量捞到的还是 BM25 捞到的"，
    而区分这两者恰恰是这套多路设计存在的理由。
    """

    chunks: list[Chunk]
    channels: dict[str, int] = field(default_factory=dict)
    fused: bool = False
    degraded: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RecallConfig:
    """一次召回的**全部可调项**。

    存在的理由不是"把参数收进一个类显得整齐"，而是评估要做**消融**：
    只向量 / 只 BM25 / 加融合 / 再加重排，四个数字才凑得出
    "从 x 提升到 y" 这句话。而消融必须是**同一条链路的参数子集**。

    之前 `eval_retrieval.py` 自己实现了一遍融合与重排，于是同一个概念
    有两份代码：改一份忘了另一份，"评估说融合有效、线上不是那回事"
    就会发生，且没有任何告警。现在两条路径都调 `recall_detailed`，
    消融只是改这里的字段 —— 差异变成**显式参数**，而不是隐性代码分叉。

    字段取值 0 一律表示"用 settings 的默认值"，而不是"禁用"：
    禁用通道请从 `channels` 里去掉它。
    """

    channels: tuple[str, ...] = (CH_VECTOR, CH_BM25)
    # store：走向量库（生产路径）；local：暴力扫描 build 落盘的向量（离线对照）
    vector_source: str = "store"
    # 向量库后端覆盖（"" → settings.vector_backend）。
    # 存在的理由：评估要能"不动 .env 就换后端"，否则 README 里
    # "换后端不改代码，只改这个参数" 这句话是假的 —— 而它恰恰是
    # 抽象层存在的卖点。注意覆盖的是**后端选择**，代码路径仍然只有一条。
    backend: str = ""
    fusion: bool = True
    pool: int = 0          # 每路候选池大小（0 → settings.top_k_retrieve）
    max_per_doc: int = 0   # 每路文档占比上限（0 → settings.recall_max_per_doc）

    def with_overrides(self, *, top_k: int | None = None, fusion: bool | None = None) -> "RecallConfig":
        """兼容旧的按关键字调用（`top_k=` / `fusion=`）。见 recall_detailed。"""
        changes: dict[str, Any] = {}
        if top_k is not None:
            changes["pool"] = top_k
        if fusion is not None:
            changes["fusion"] = fusion
        return replace(self, **changes) if changes else self


class IndexBundle:
    """索引产物的内存副本，支持按文件变更热重载。

    用 `threading.Lock` 而不是 asyncio 锁：加载是同步 IO（读 json），
    而且可能在 `asyncio.to_thread` 里被调用，threading 锁才对。
    """

    def __init__(self, index_dir: Path) -> None:
        self.index_dir = Path(index_dir)
        self._lock = threading.Lock()
        self._stamp: tuple[int, float] | None = None
        self.kb_version: int = 0
        self.dim: int = 0
        self.chunk_to_doc: dict[str, str] = {}
        self.chunk_text: dict[str, str] = {}
        self.chunk_payload: dict[str, dict[str, Any]] = {}
        self.chunks_by_doc: dict[str, list[str]] = {}
        self.bm25: Any = None
        self.available: bool = False

    # ---------------- 加载 ----------------
    def _manifest_path(self) -> Path:
        return self.index_dir / "index_manifest.json"

    def _stamp_now(self) -> tuple[int, float] | None:
        try:
            st = self._manifest_path().stat()
        except OSError:
            return None
        return (st.st_size, st.st_mtime)

    def ensure_fresh(self) -> bool:
        """文件变了就重载。返回是否**发生了重载**。"""
        current = self._stamp_now()
        if current is None:
            # manifest 不存在：离线开发态（未跑 build）。不算异常，静默降级。
            with self._lock:
                self.available = False
            return False
        if current == self._stamp and self.available:
            return False
        with self._lock:
            if current == self._stamp and self.available:
                return False
            self._load()
            self._stamp = current
            return True

    def _load(self) -> None:
        from app.ingest.indexer import BM25_FILE, POINTS_FILE, read_points
        from app.ingest.textindex import BM25Index

        manifest_path = self._manifest_path()
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("索引 manifest 读取失败：%s", exc)
            self.available = False
            return

        self.kb_version = int(manifest.get("kb_version", 0))
        self.dim = int(manifest.get("dim", settings.embedding_dim))

        points_path = self.index_dir / POINTS_FILE
        try:
            points = read_points(points_path)
        except (OSError, ValueError) as exc:
            log.warning("points.jsonl 读取失败：%s", exc)
            self.available = False
            return

        # 只保留**当前版本**的点。
        # manifest 与 points.jsonl 是同一次 build 的产物，理论上版本一致；
        # 但服务端可能运行在"刚 bump、还没重建索引"的时间窗里，
        # 所以这里以 manifest 为准做一次过滤，宁可召回少也不召回错的。
        current: dict[str, str] = {}
        text_of: dict[str, str] = {}
        payload_of: dict[str, dict[str, Any]] = {}
        by_doc: dict[str, list[str]] = {}
        skipped = 0
        for p in points:
            version = p.payload.get("kb_version")
            if version is not None and int(version) != self.kb_version:
                skipped += 1
                continue
            current[p.chunk_id] = p.doc_id
            text_of[p.chunk_id] = p.text
            payload_of[p.chunk_id] = p.payload
            by_doc.setdefault(p.doc_id, []).append(p.chunk_id)

        self.chunk_to_doc = current
        self.chunk_text = text_of
        self.chunk_payload = payload_of
        self.chunks_by_doc = by_doc

        bm25 = None
        bm25_path = self.index_dir / BM25_FILE
        if bm25_path.exists():
            try:
                bm25 = BM25Index.load(bm25_path)
            except (OSError, ValueError) as exc:
                log.warning("BM25 索引读取失败：%s（向量路仍可用）", exc)
        self.bm25 = bm25
        self.available = True

        log.info(
            "索引已加载：kb_version=%s · %d 块 · BM25 %s（跳过旧版本点 %d 个）",
            self.kb_version, len(current),
            f"{bm25.size} 块" if bm25 is not None else "不可用", skipped,
        )

    # ---------------- 查询 ----------------
    def is_current_chunk(self, chunk_id: str) -> bool:
        return chunk_id in self.chunk_to_doc

    def payload(self, chunk_id: str) -> dict[str, Any]:
        return self.chunk_payload.get(chunk_id, {})

    def text(self, chunk_id: str) -> str:
        return self.chunk_text.get(chunk_id, "")

    def registry(self) -> dict[str, Any]:
        """图谱注册表（多跳扩展用）。不存在时返回空表，不抛异常。"""
        path = self.index_dir / "registry.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}


_bundle: IndexBundle | None = None
_bundle_lock = threading.Lock()


def get_bundle() -> IndexBundle:
    global _bundle
    if _bundle is None:
        with _bundle_lock:
            if _bundle is None:
                _bundle = IndexBundle(Path(settings.kb_index_dir))
                _bundle.ensure_fresh()
    else:
        _bundle.ensure_fresh()
    return _bundle


def reset_bundle() -> None:
    """测试/热重载用。"""
    global _bundle
    with _bundle_lock:
        _bundle = None


# =====================================================================
# 向量库（统一走 stores 抽象层）
# =====================================================================
# 按后端名缓存，而不是单个全局 —— 评估要在一次进程里对比 qdrant 与 chroma，
# 用一个全局的话第二次调用会**静默返回第一次的后端**，
# 于是"两个后端的对比报告"其实是同一个后端跑了两次。
_stores: dict[str, Any] = {}
_store_lock = threading.Lock()


def get_store(backend: str | None = None) -> Any:
    """向量库单例（按后端名缓存）。

    刻意不直接 new `AsyncQdrantClient`：换后端（Chroma）只改 .env，
    而且评估脚本与服务端走**同一个** `build_store` ——
    两条路径的召回数字才可比。
    """
    name = backend or getattr(settings, "vector_backend", "qdrant")
    store = _stores.get(name)
    if store is None:
        with _store_lock:
            store = _stores.get(name)
            if store is None:
                from app.ingest.stores import build_store

                store = build_store(
                    name,
                    dim=settings.embedding_dim,
                    collection=settings.qdrant_collection,
                    settings=settings,
                )
                _stores[name] = store
                log.info("向量库已就绪：%s（集合 %s）", store.name, settings.qdrant_collection)
    return store


async def aclose_store() -> None:
    """关闭并清空所有已建连接。"""
    with _store_lock:
        pending = list(_stores.values())
        _stores.clear()
    for store in pending:
        try:
            await store.aclose()
        except Exception:  # noqa: BLE001
            pass


def get_qdrant():
    """兼容旧调用（`/ready` 健康检查）。

    保留它是因为健康检查只需要一个"能 ping 通"的客户端，
    没必要为它把 store 拉起来。但它只用于探测，**不参与检索**。
    """
    from qdrant_client import AsyncQdrantClient

    return AsyncQdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)


# =====================================================================
# 各路召回
# =====================================================================
class Recaller:
    """多路召回基类。"""

    name: str = "base"

    async def recall(
        self, query: str, *, embedding: Sequence[float] | None, top_k: int
    ) -> list[Chunk]:
        raise NotImplementedError


def _chunk_from_payload(
    payload: dict[str, Any],
    *,
    chunk_id: str,
    score: float,
    recall_source: str,
    bundle: IndexBundle,
) -> Chunk:
    """把"一条命中"统一转成 `Chunk`。

    抽出来的原因很具体：`Chunk.doc_id` 是**评估集标注的锚点**。
    如果三条召回路各自拼一遍字典，总有一天某一条会漏掉 doc_id 或
    把 page 从 int 变成 str —— 而那时评估口径已经悄悄变了。
    三路共用一次构造，字段就只可能有一种形状。

    `text` 的兜底顺序（payload → 索引产物）体现了"谁更权威"：
    payload 里的 text 是入库时的原文切片，索引产物是 build 阶段的快照，
    两者正常情况下相同；不同则说明库里存了别的版本，取 payload。
    """
    return Chunk(
        doc_id=str(payload.get("doc_id", bundle.chunk_to_doc.get(chunk_id, ""))),
        chunk_id=chunk_id,
        text=payload.get("text") or bundle.text(chunk_id),
        source=payload.get("source", ""),
        page=payload.get("page"),
        modality=payload.get("modality", "text"),
        score=float(score),
        recall_source=recall_source,
        metadata={k: v for k, v in payload.items() if k != "text"},
    )


class VectorRecaller(Recaller):
    """向量召回，走 stores 抽象层 + kb_version 过滤。"""

    name = CH_VECTOR

    def __init__(self, backend: str = "") -> None:
        # 空字符串 = 用 settings.vector_backend（生产路径）。
        # 非空 = 评估显式指定后端，走的是同一个 get_store / 同一条检索代码。
        self.backend = backend

    async def recall(self, query, *, embedding, top_k) -> list[Chunk]:
        bundle = get_bundle()
        if not bundle.available:
            raise RuntimeError("索引产物不存在，请先执行 python -m scripts.ingest --stage build")

        if embedding is None:
            embedding = await embed_one(query)
        store = get_store(self.backend or None)

        hits = await store.search(
            list(embedding),
            limit=top_k,
            filters={"kb_version": bundle.kb_version},
        )

        chunks: list[Chunk] = []
        for h in hits:
            payload = h.payload or {}
            chunk_id = str(payload.get("chunk_id") or h.chunk_id)
            if not bundle.is_current_chunk(chunk_id):
                # 双保险：store 侧已过滤，这里再挡一次。
                # 向量库侧的过滤依赖后端对 filter 的支持；万一某个后端的
                # 过滤实现被静默忽略，这一层能兜住"旧版本点混进来"。
                continue
            chunks.append(
                _chunk_from_payload(
                    payload, chunk_id=chunk_id, score=h.score,
                    recall_source=self.name, bundle=bundle,
                )
            )
        return chunks


class LocalVectorRecaller(Recaller):
    """向量召回，但直接暴力扫描 build 落盘的向量 —— 不需要向量库在线。

    与 `VectorRecaller` 的关系是**只换向量索引来源，下游完全一致**：
    同一个 `Chunk` 构造、同一个 kb_version 双保险、同一个 cap/融合/重排。

    两个用途：
      1. 评估的离线对照 —— 没有 Qdrant 也能给出向量基线，而且它是
         `brute_force_search` 存在的理由（见 indexer 里那段 docstring）：
         与向量库结果分数若明显不一致，问题在向量库侧的配置
         （距离度量 / 归一化 / 过滤），而不在模型。
      2. 向量库故障时的兜底 —— 468 点的暴力扫描是毫秒级。

    刻意**不**把它写成第二个检索实现。一旦它自己构造 Chunk、
    自己做过滤，它就会变成一条"看起来一样但不一样"的链路，
    而评估恰恰会拿它当对照 —— 对照与被对照者不同源，比没有对照更糟。
    """

    name = CH_VECTOR

    async def recall(self, query, *, embedding, top_k) -> list[Chunk]:
        from app.ingest.indexer import brute_force_search

        bundle = get_bundle()
        if not bundle.available:
            raise RuntimeError("索引产物不存在，请先执行 python -m scripts.ingest --stage build")

        if embedding is None:
            embedding = await embed_one(query)
        # 磁盘读取放线程里（热重载判据是一次 stat）；扫描本身是纯计算，
        # 468 × 512 只有 24 万次乘加，inline 反而省一次线程切换。
        ids, vectors = await asyncio.to_thread(_load_local_vectors, bundle.index_dir)
        hits = brute_force_search(list(embedding), ids, vectors, top_k=top_k)

        chunks: list[Chunk] = []
        for chunk_id, score in hits:
            if not bundle.is_current_chunk(chunk_id):
                continue          # 与向量库路同一道保险：挡掉非当前版本的块
            chunks.append(
                _chunk_from_payload(
                    bundle.payload(chunk_id), chunk_id=chunk_id, score=score,
                    recall_source=self.name, bundle=bundle,
                )
            )
        return chunks


_local_vectors: dict[str, tuple[tuple[int, float], list[str], list[list[float]]]] = {}
_local_vectors_lock = threading.Lock()


def _load_local_vectors(index_dir: Path) -> tuple[list[str], list[list[float]]]:
    """读取 build 阶段落盘的向量缓存，按 (size, mtime) 缓存。

    热重载判据与 `IndexBundle` 一致：重新入库后 vectors.bin 会变，
    缓存必须跟着失效 —— 否则评估会拿着上一版向量算这一版的召回率。
    """
    from app.ingest.indexer import VECTORS_BIN, VECTORS_META, read_vectors

    bin_path = index_dir / VECTORS_BIN
    meta_path = index_dir / VECTORS_META
    try:
        st = bin_path.stat()
    except OSError as exc:
        raise RuntimeError(
            f"缺少 {bin_path}；请先执行 "
            "python -m scripts.ingest --stage index --backend local"
        ) from exc
    stamp = (st.st_size, st.st_mtime)
    key = str(bin_path)

    with _local_vectors_lock:
        cached = _local_vectors.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1], cached[2]

    ids, vectors, _dim = read_vectors(bin_path, meta_path)
    with _local_vectors_lock:
        _local_vectors[key] = (stamp, ids, vectors)
    return ids, vectors


class BM25Recaller(Recaller):
    """关键词召回。

    存在的理由不是"补一路召回显得多"，而是**向量对精确串天生钝感**：
    `ORD-429-001`、`max_connections`、`df -i` 这类串在 embedding 空间里
    跟其他错误码几乎重合，而 BM25 的字符二元组能精确命中。
    运维场景里这类串恰恰是答案的关键部分。
    """

    name = CH_BM25

    async def recall(self, query, *, embedding, top_k) -> list[Chunk]:
        bundle = get_bundle()
        index = bundle.bm25
        if index is None or not index.size:
            raise RuntimeError("BM25 索引不可用")

        hits = index.search(query, top_k=top_k)
        chunks: list[Chunk] = []
        for chunk_id, score in hits:
            if not bundle.is_current_chunk(chunk_id):
                continue          # 同向量路：挡掉非当前版本的块
            chunks.append(
                _chunk_from_payload(
                    bundle.payload(chunk_id), chunk_id=chunk_id, score=score,
                    recall_source=self.name, bundle=bundle,
                )
            )
        return chunks


def get_recallers(config: RecallConfig | None = None) -> list[Recaller]:
    """按配置装配召回路。

    结构化那一路不在检索层：工单/资产走 `app/tools`，
    由意图路由决定是否调用。理由见 README §1 的表格 ——
    实时数据进向量库会被冻结成过期快照。
    """
    config = config or RecallConfig()
    factory: dict[str, Any] = {
        CH_VECTOR: LocalVectorRecaller if config.vector_source == "local" else VectorRecaller,
        CH_BM25: BM25Recaller,
    }
    recallers: list[Recaller] = []
    for name in config.channels:
        cls = factory.get(name)
        if cls is None:
            # 打错的通道名静默忽略，等于"以为开了三路其实只有两路"。
            # 这种错误必须立刻炸，而不是等到看召回率时才发现对不上。
            raise ValueError(f"未知召回路 {name!r}；可选：{sorted(factory)}")
        if cls is VectorRecaller:
            recallers.append(cls(backend=config.backend))
        else:
            recallers.append(cls())
    return recallers


async def recall_detailed(
    query: str,
    *,
    embedding: Sequence[float] | None = None,
    top_k: int | None = None,
    fusion: bool | None = None,
    config: RecallConfig | None = None,
) -> RecallOutcome:
    """多路召回 + 融合。**任何一路挂掉都不影响另一路** —— 这是可用性设计。

    降级语义很关键：向量库挂了不能返回 500，而应该用 BM25 继续回答，
    并在 `degraded` 里如实记一笔（会写进 SSE 事件，用户能看到这次答案质量降了）。

    `config` 与 `top_k`/`fusion` 两条入口并存是**刻意**的：
    - 服务端只关心 settings 默认值，调 `recall_detailed(q, embedding=...)`；
    - 评估脚本要显式消融，传完整 `config`。
    两者最终都会被解析成同一个 `RecallConfig`，不存在"两套逻辑"。
    """
    # 显式触发一次索引热重载检查。
    # 各路 recaller 内部也会调 get_bundle()，所以这里不是功能必需 ——
    # 它的作用是**把意图写出来**：每次检索都先确认索引版本，
    # 避免服务端拿着旧索引回答新问题。成本是一次 stat（微秒级）。
    get_bundle()
    if config is None:
        config = RecallConfig(
            fusion=bool(getattr(settings, "recall_fusion_enabled", True)),
        )
    config = config.with_overrides(top_k=top_k, fusion=fusion)

    pool = config.pool or settings.top_k_retrieve
    cap = config.max_per_doc or int(getattr(settings, "recall_max_per_doc", 3))

    recallers = get_recallers(config)
    degraded: list[str] = []

    with STAGE_SECONDS.labels(stage="recall").time():
        results = await asyncio.gather(
            *[r.recall(query, embedding=embedding, top_k=pool) for r in recallers],
            return_exceptions=True,
        )

    groups: list[list[Chunk]] = []
    channels: dict[str, int] = {}
    for recaller, result in zip(recallers, results):
        if isinstance(result, BaseException):
            degraded.append(f"{recaller.name}:{type(result).__name__}")
            DEGRADED_TOTAL.labels(stage="recall", reason=recaller.name).inc()
            log.warning("召回路 %s 失败：%s: %s", recaller.name, type(result).__name__, result)
            channels[recaller.name] = 0
            continue
        channels[recaller.name] = len(result)
        # 每路先做"文档多样性"约束，再进融合。
        # 放在融合之前而不是之后：否则一篇长文档在**每一路**里都占满坑位，
        # 融合时它还会因为"两路都命中"而拿到加成，问题被放大而不是缓解。
        groups.append(
            cap_per_unit(result, lambda c: c.doc_id, max_per_unit=cap) if cap > 0 else list(result)
        )

    if not groups:
        return RecallOutcome(chunks=[], channels=channels, fused=config.fusion, degraded=degraded)

    if not config.fusion or len(groups) == 1:
        merged: dict[str, Chunk] = {}
        for group in groups:
            for chunk in group:
                prev = merged.get(chunk.chunk_id)
                if prev is None or chunk.score > prev.score:
                    merged[chunk.chunk_id] = chunk
        chunks = sorted(merged.values(), key=lambda c: c.score, reverse=True)
        return RecallOutcome(chunks=chunks, channels=channels, fused=False, degraded=degraded)

    # ---- RRF 融合 ----
    # 与评估脚本共用同一份实现（app/pipeline/fusion.py），
    # 所以"评估口径"与"线上口径"是同一个函数，不会两张皮。
    fused_items = rrf_fuse_items(groups, key=lambda c: c.chunk_id)
    hit_count: dict[str, int] = {}
    for group in groups:
        for chunk in group:
            hit_count[chunk.chunk_id] = hit_count.get(chunk.chunk_id, 0) + 1

    chunks = []
    for chunk, rrf_score in fused_items:
        chunk.metadata["_rrf_score"] = rrf_score
        chunk.metadata["_channels"] = hit_count.get(chunk.chunk_id, 1)
        if hit_count.get(chunk.chunk_id, 1) > 1:
            chunk.recall_source = f"{CH_VECTOR}+{CH_BM25}"
        chunks.append(chunk)

    return RecallOutcome(chunks=chunks, channels=channels, fused=True, degraded=degraded)


async def recall(
    query: str, *, embedding: Sequence[float] | None = None, top_k: int | None = None
) -> list[Chunk]:
    """兼容入口。需要每路命中数时用 `recall_detailed`。"""
    outcome = await recall_detailed(query, embedding=embedding, top_k=top_k)
    return outcome.chunks


async def rewrite_query(query: str) -> str:
    """查询改写。运维场景的价值在于把口语化描述映射到标准术语/错误码。

    D1 下午接：结果走 `cache.get_json("rewrite", ...)`，TTL 用 CACHE_REWRITE_TTL。
    现在保持 identity —— 因为**没有评估支撑的改写是真空中做优化**：
    得先有"改写前的基线"，才知道改写值不值那一次 LLM 调用。
    44 条 rag 题的 BM25 基线（recall@5=0.86）就是那个对照点。
    """
    return query


async def rerank_chunks(
    query: str,
    chunks: list[Chunk],
    top_k: int | None = None,
    *,
    max_per_doc: int | None = None,
) -> list[Chunk]:
    """CrossEncoder 精排。空列表必须安全返回 —— 召回层降级时会传空进来。

    `max_per_doc`：截断**之前**先限制每篇文档的块数。
    顺序很重要 —— 先截断再做多样性约束的话，5 个坑位可能已经全被同一篇文档占满，
    约束就无从下手了。所以是"先按相关度排序 → 限制文档占比 → 再截断"。

    代价是返回条数可能少于 top_k（池子里文档不够多时）。
    这是刻意接受的：3 个来自不同文档的块，比 5 个同一文档的块更有价值 ——
    后者会让答案只反映一篇文档的视角，而用户没法从答案里看出这一点。
    """
    if not chunks:
        return []
    top_k = top_k or settings.top_k_rerank
    if max_per_doc is None:
        max_per_doc = int(getattr(settings, "rerank_max_per_doc", 2)) or None

    with STAGE_SECONDS.labels(stage="rerank").time():
        scores = await score_pairs([(query, c.text) for c in chunks])
    for chunk, score in zip(chunks, scores):
        chunk.rerank_score = score
    chunks.sort(key=lambda c: c.rerank_score or 0.0, reverse=True)

    if max_per_doc:
        chunks = cap_per_unit(chunks, lambda c: c.doc_id, max_per_unit=max_per_doc)
    return chunks[:top_k]


def build_context(chunks: Sequence[Chunk]) -> str:
    parts = []
    for c in chunks:
        tag = f"[{c.source or c.doc_id}]"
        if c.page is not None:
            tag += f"[第{c.page}页]"
        if c.modality != "text":
            tag += f"[{c.modality}]"
        parts.append(f"{tag} {c.text}")
    return "\n\n".join(parts)


SYSTEM_PROMPT = """你是企业 IT 运维知识助手。请严格依据下方资料回答问题。

规则：
1. 只使用资料中的信息，不要引入你自己的知识或猜测
2. 资料中与问题相关的要点必须完整覆盖，不要只答一部分
3. 若资料确实未覆盖问题的某个方面，明确说明"资料未涉及"
4. 引用来源时标注在关键结论后，格式如（来源：xxx）
5. 若资料完全无法回答问题，回答"根据现有资料无法回答"，不要编造
6. 涉及操作步骤时按序号列出，涉及数值参数时原样引用，不要近似
7. 资料中若包含"工具查询结果"，其中的实时数据（工单状态/资产状态/告警）
   必须原样引用，不要用知识库里的历史描述去推断当前状态
8. 若工具调用失败或返回 0 条，必须明确说"未查询到"，绝不能编造实时数据
"""

# 工具类题的补充说明：让模型知道工单/资产数据是"此刻的事实"
TOOL_CONTEXT_HEADER = "【工具查询结果】"


async def generate_stream(
    question: str,
    chunks: Sequence[Chunk],
    *,
    tool_context: str = "",
    history: str = "",
) -> AsyncIterator[str]:
    """流式生成。

    `tool_context` 与检索上下文分开传入并分开标注：模型需要知道
    "哪一段是文档、哪一段是此刻的实时数据" —— 前者可引用，后者是事实。
    混在一起容易让模型把工单里的历史描述当成当前状态。

    `history` 是三层记忆注入的"会话历史 + 长期事实"文本块（可为空）。
    单独一个参数而不是拼进 question：历史和问题在 prompt 里的地位不同 ——
    历史是背景，问题是本轮任务；拼在一起模型容易把上一轮的问题
    当成本轮要回答的对象。
    """
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key.get_secret_value(),
        base_url=settings.deepseek_base_url,
    )
    context = build_context(chunks)
    user_parts = [f"问题：{question}"]
    if history.strip():
        user_parts.append(f"【对话历史与已知背景】\n{history}")
    if tool_context.strip():
        user_parts.append(f"{TOOL_CONTEXT_HEADER}\n{tool_context}")
    user_parts.append(f"【知识库资料】\n{context}" if context else "【知识库资料】\n（本次没有检索到资料）")

    with STAGE_SECONDS.labels(stage="generate").time():
        stream = await client.chat.completions.create(
            model=settings.deepseek_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
            temperature=0.1,
            stream=True,
        )
        REQUEST_TOTAL.labels(model=settings.deepseek_model, result="ok").inc()
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content


async def generate_once(question: str, *, system: str | None = None) -> str:
    """非流式生成。闲聊/固定话术这类"不需要检索"的路径用它，省一次完整链路。"""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key.get_secret_value(),
        base_url=settings.deepseek_base_url,
    )
    resp = await client.chat.completions.create(
        model=settings.deepseek_model,
        messages=[
            {"role": "system", "content": system or SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        temperature=0.3,
    )
    return (resp.choices[0].message.content or "").strip()


def estimate_tokens(text: str) -> int:
    """粗估 token，用于统计"缓存省了多少成本"。中文按 1.6 字符/token 估算。"""
    return max(1, int(len(text) / 1.6))

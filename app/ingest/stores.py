"""向量存储抽象层 —— 让"换向量库"从改业务代码降为改一个配置项。

为什么必须要有这一层
====================
向量库是 RAG 链路里**最容易被替换的一层**，而且替换的理由通常不是技术偏好，
是客户环境：

- 客户已经上云、有托管向量服务 → 跟着用托管的，别引入新运维对象
- 客户已有一套 ES / Milvus / pgvector → 复用现成的比新引入更划算
- 纯离线私有化交付 → 必须能二进制部署、不依赖外部 SaaS

把这层抽出来有三个收益：

1. 换库成本从"改业务代码"降到"改 `.env` 里一行"。这是交付能力，不是代码洁癖。
2. 检索策略（多路融合、重排、评估口径）与存储解耦，能被独立测试与回归对比。
3. `LocalStore`（内存暴力检索）天然成为向量库的**对照组**：
   两者召回若明显不一致，问题在向量库配置（距离度量 / 归一化 / 过滤条件），
   不在 embedding 模型。排查"召回为什么不对"时，这一步能把范围直接砍掉一半。

接口契约（所有实现必须一致，否则上层融合会静默算错）
====================================================
1. `search()` 返回按 `score` **降序**的 `SearchHit` 列表。
2. `score` 统一为**余弦相似度**（越大越相似），即使底层存的是距离。
   Chroma 返回的是 cosine distance（= 1 − similarity），实现里显式换算。
   这是换库时最容易踩的坑：分数"看起来正常"，但量纲不同或方向相反，
   后续加权融合／阈值判断会**不报错地**产生错误排序。
3. `upsert()` 幂等：同一 `chunk_id` 反复写入结果一致（点 id 由 uuid5 稳定派生，
   见 `indexer.point_id_for`）。这条是"重跑一次入库不会产生双份数据"的保证。
4. 存储层的连接／超时异常**向上抛**，不做静默兜底。
   降级属于调用方的职责；藏在存储层里的兜底会让降级链无法被验证。

后端选型（详见 README §2 的对比表）
===================================
- **Qdrant（默认）**：原生 `/metrics` 可直接接 Prometheus；支持分片与副本，
  能真实支撑"多副本横向扩展"的验证；payload filter 是完整布尔／范围表达式；
  有官方维护的 Java client —— 后续 Java 侧若需读向量，不用自己糊 HTTP。
- **Chroma**：嵌入式部署极简（一行 `PersistentClient`），单机原型与本地开发最省事。
  但 server 模式的分片／副本能力弱，也不直接暴露 Prometheus 指标，
  因此"横向扩展"与"可观测"两条叙事需要另找支撑。
- **Local**：零依赖内存实现，供离线基线与结果对照，不用于生产。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

log = logging.getLogger(__name__)

# 过滤表达式约定（各实现负责翻译成自家语法）：
#   {"字段": "值"}                     → 等值
#   {"字段": {"$in": [...]}}           → 属于集合
#   {"字段": {"$gte": n, "$lte": m}}   → 范围（单边可省）
# 多个字段之间为 AND。刻意只用这一小套子集：表达能力足够覆盖
# kb_version / modality / category / doc_id 四类过滤，
# 而且三个实现都能无损支持 —— 追求"完整表达式支持"会让 Chroma 先掉队。
FilterExpr = dict[str, Any]


# =====================================================================
# 返回类型与点契约
# =====================================================================
@dataclass(slots=True)
class SearchHit:
    chunk_id: str
    score: float                      # 余弦相似度，越大越相似
    payload: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PointLike(Protocol):
    """`indexer.Point` 的结构契约（用 Protocol 而非 import，避免循环依赖）。"""

    id: str
    chunk_id: str
    vector: Sequence[float] | None
    payload: dict[str, Any]


# =====================================================================
# 接口
# =====================================================================
@runtime_checkable
class VectorStore(Protocol):
    name: str
    dim: int

    async def ensure_ready(self) -> None: ...

    async def upsert(self, points: Sequence[PointLike], *, batch_size: int = 64) -> int: ...

    async def search(
        self,
        vector: Sequence[float],
        *,
        limit: int = 20,
        filters: FilterExpr | None = None,
    ) -> list[SearchHit]: ...

    async def count(self) -> int: ...

    async def health(self) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


# =====================================================================
# 过滤器翻译
# =====================================================================
def translate_filter_qdrant(filters: FilterExpr | None) -> Any:
    if not filters:
        return None
    from qdrant_client.models import (
        FieldCondition,
        Filter,
        MatchAny,
        MatchValue,
        Range,
    )

    must: list[Any] = []
    for key, expr in filters.items():
        if isinstance(expr, dict):
            if "$in" in expr:
                must.append(FieldCondition(key=key, match=MatchAny(any=list(expr["$in"]))))
            if "$gte" in expr or "$lte" in expr:
                must.append(
                    FieldCondition(
                        key=key,
                        range=Range(gte=expr.get("$gte"), lte=expr.get("$lte")),
                    )
                )
        else:
            must.append(FieldCondition(key=key, match=MatchValue(value=expr)))
    return Filter(must=must) if must else None


def translate_filter_chroma(filters: FilterExpr | None) -> dict[str, Any] | None:
    """Chroma 的 where 语法本就接近本层约定，无需转换 —— 只需注意
    **Chroma 的 metadata 只接受 str / int / float / bool**（见 `_flatten_metadata`），
    所以 list 类字段（tags / related_docs）在写入时已序列化成 JSON 字符串，
    过滤时也要用同样的字符串形态比较。这一点必须写进文档，否则会出现
    "同一套过滤条件在 Qdrant 有效、换到 Chroma 静默返回空"的问题。
    """
    return dict(filters) if filters else None


def _match_local(payload: dict[str, Any], filters: FilterExpr | None) -> bool:
    if not filters:
        return True
    for key, expr in filters.items():
        actual = payload.get(key)
        if isinstance(expr, dict):
            if "$in" in expr and actual not in expr["$in"]:
                return False
            if "$gte" in expr and (actual is None or actual < expr["$gte"]):
                return False
            if "$lte" in expr and (actual is None or actual > expr["$lte"]):
                return False
        elif actual != expr:
            return False
    return True


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# =====================================================================
# Qdrant
# =====================================================================
class QdrantStore:
    """Qdrant 实现。客户端从外部注入 —— 这样这个类在没有 Qdrant 的环境里也能被测试。"""

    name = "qdrant"

    def __init__(
        self,
        client: Any,
        collection: str,
        *,
        dim: int,
        distance: str = "Cosine",
        host: str = "",
        port: int = 6333,
    ) -> None:
        self._client = client
        self._collection = collection
        self.dim = dim
        self._distance = distance
        self.host = host
        self.port = port

    # ---- 生命周期 ----
    async def ensure_ready(self) -> None:
        from qdrant_client.models import Distance, VectorParams

        if await self._client.collection_exists(self._collection):
            return
        dist = getattr(Distance, self._distance.upper(), Distance.COSINE)
        await self._client.create_collection(
            collection_name=self._collection,
            vectors_config=VectorParams(size=self.dim, distance=dist),
        )
        log.info("已创建集合 %s（dim=%d, %s）", self._collection, self.dim, self._distance)

    # 向后兼容旧调用名
    async def ensure_collection(self) -> None:
        await self.ensure_ready()

    async def upsert(self, points: Sequence[PointLike], *, batch_size: int = 64) -> int:
        from qdrant_client.models import PointStruct

        written = 0
        batch: list[Any] = []
        for p in points:
            if p.vector is None:
                raise ValueError(f"点 {p.chunk_id} 缺少向量")
            batch.append(PointStruct(id=p.id, vector=list(p.vector), payload=p.payload))
            if len(batch) >= batch_size:
                await self._client.upsert(self._collection, points=batch, wait=True)
                written += len(batch)
                batch = []
        if batch:
            await self._client.upsert(self._collection, points=batch, wait=True)
            written += len(batch)
        return written

    async def search(
        self,
        vector: Sequence[float],
        *,
        limit: int = 20,
        filters: FilterExpr | None = None,
    ) -> list[SearchHit]:
        qf = translate_filter_qdrant(filters)
        points = await self._query(list(vector), limit=limit, query_filter=qf)
        hits: list[SearchHit] = []
        for p in points:
            payload = dict(getattr(p, "payload", None) or {})
            hits.append(
                SearchHit(
                    chunk_id=str(payload.get("chunk_id") or getattr(p, "id", "")),
                    score=float(getattr(p, "score", 0.0)),
                    payload=payload,
                )
            )
        return hits

    async def _query(self, vector: list[float], *, limit: int, query_filter: Any) -> list[Any]:
        """兼容 qdrant-client 新旧 API：>=1.7 用 query_points，更早只有 search。"""
        kwargs = dict(
            collection_name=self._collection,
            limit=limit,
            query_filter=query_filter,
            with_payload=True,
        )
        query_points = getattr(self._client, "query_points", None)
        if query_points is not None:
            resp = await query_points(query=vector, **kwargs)
            return list(getattr(resp, "points", resp) or [])
        return list(await self._client.search(query_vector=vector, **kwargs))

    async def count(self) -> int:
        result = await self._client.count(self._collection, exact=True)
        return int(getattr(result, "count", 0))

    async def health(self) -> dict[str, Any]:
        info: dict[str, Any] = {"backend": self.name, "collection": self._collection}
        try:
            info["points_count"] = await self.count()
            info["ok"] = True
        except Exception as exc:  # 健康检查本身不该抛
            info["ok"] = False
            info["error"] = f"{type(exc).__name__}: {exc}"
        return info

    async def aclose(self) -> None:
        """释放连接。连接生命周期归存储层管，调用方不必知道背后是哪个 SDK。"""
        close = getattr(self._client, "close", None)
        if close is None:
            return
        result = close()
        if asyncio.iscoroutine(result):
            await result


# =====================================================================
# Chroma
# =====================================================================
def _flatten_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """把 payload 压成 Chroma 能接受的 metadata。

    Chroma 的 metadata 只支持 str / int / float / bool，且不接受 None。
    所以：
    - list（tags / related_docs）→ JSON 字符串
    - None → 直接丢弃（不写空值，避免 Chroma 报类型错误）
    代价是这些字段在 Chroma 里无法做"集合包含"过滤。这是真实的表达力差异，
    不是实现偷懒 —— 需要按 tag 过滤的场景应该选 Qdrant。
    """
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if v is None:
            continue
        if isinstance(v, bool) or isinstance(v, (str, int, float)):
            out[k] = v
        elif isinstance(v, (list, tuple, dict)):
            out[k] = json.dumps(v, ensure_ascii=False)
        else:
            out[k] = str(v)
    return out


class ChromaStore:
    """Chroma 实现。

    Chroma 的 Python client 是**同步**的，所以每个调用都用 `asyncio.to_thread`
    包起来 —— 否则会在事件循环里阻塞，并发指标（这是本项目要测的指标之一）
    会被这一处直接拖垮，而且表现得像"模型慢"，很难定位。
    """

    name = "chroma"

    def __init__(self, client: Any, collection: str, *, dim: int) -> None:
        self._client = client
        self._collection = collection
        self.dim = dim
        self._col: Any = None

    async def ensure_ready(self) -> None:
        def _do() -> Any:
            # hnsw:space=cosine 必须显式指定：Chroma 默认是 l2，
            # 用 l2 距离跑归一化向量虽然排序大体一致，但阈值与融合权重会偏离预期。
            return self._client.get_or_create_collection(
                name=self._collection, metadata={"hnsw:space": "cosine"}
            )

        self._col = await asyncio.to_thread(_do)
        log.info("Chroma 集合就绪：%s（cosine）", self._collection)

    async def ensure_collection(self) -> None:
        await self.ensure_ready()

    def _c(self) -> Any:
        if self._col is None:
            raise RuntimeError("ChromaStore 尚未初始化，请先 await ensure_ready()")
        return self._col

    async def upsert(self, points: Sequence[PointLike], *, batch_size: int = 64) -> int:
        col = self._c()
        written = 0
        buf: list[PointLike] = []
        for p in points:
            if p.vector is None:
                raise ValueError(f"点 {p.chunk_id} 缺少向量")
            buf.append(p)
            if len(buf) >= batch_size:
                written += await self._flush(col, buf)
                buf = []
        if buf:
            written += await self._flush(col, buf)
        return written

    async def _flush(self, col: Any, batch: Sequence[PointLike]) -> int:
        ids = [p.chunk_id for p in batch]        # 用 chunk_id 而非 uuid：Chroma id 是普通字符串

        def _do() -> None:
            col.upsert(
                ids=ids,
                embeddings=[list(p.vector or ()) for p in batch],
                documents=[str(p.payload.get("text") or "") for p in batch],
                metadatas=[_flatten_metadata(p.payload) for p in batch],
            )

        await asyncio.to_thread(_do)
        return len(batch)

    async def search(
        self,
        vector: Sequence[float],
        *,
        limit: int = 20,
        filters: FilterExpr | None = None,
    ) -> list[SearchHit]:
        col = self._c()
        where = translate_filter_chroma(filters)

        def _do() -> dict[str, Any]:
            return col.query(
                query_embeddings=[list(vector)],
                n_results=limit,
                where=where,
                include=["distances", "metadatas"],
            )

        res = await asyncio.to_thread(_do)
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]

        hits: list[SearchHit] = []
        for i, d, m in zip(ids, dists, metas):
            payload = dict(m or {})
            # cosine distance → similarity。这一步是契约要求，不是美化：
            # 忘了转换的话分数会变成"越小越相似"，融合时排序整个反过来。
            hits.append(SearchHit(str(i), 1.0 - float(d), payload))
        return hits

    async def count(self) -> int:
        col = self._c()
        return int(await asyncio.to_thread(col.count))

    async def health(self) -> dict[str, Any]:
        info: dict[str, Any] = {"backend": self.name, "collection": self._collection}
        try:
            info["points_count"] = await self.count()
            info["ok"] = True
        except Exception as exc:
            info["ok"] = False
            info["error"] = f"{type(exc).__name__}: {exc}"
        return info

    async def aclose(self) -> None:
        # Chroma 的 client 没有显式关闭语义（HttpClient 用连接池），接口对齐即可。
        return None


# =====================================================================
# Local（内存暴力检索）
# =====================================================================
class LocalStore:
    """零依赖内存实现。

    两个用途：① 离线环境跑基线，不必等容器起来；
    ② 作为向量库的**结果对照** —— 同一批向量在两处的 top-k 若不一致，
    优先怀疑向量库侧的配置（距离度量、归一化、过滤），而不是模型。
    """

    name = "local"

    def __init__(self, *, dim: int) -> None:
        self.dim = dim
        self._ids: list[str] = []
        self._vectors: dict[str, list[float]] = {}
        self._payloads: dict[str, dict[str, Any]] = {}

    async def ensure_ready(self) -> None:  # 无事可做
        return None

    async def upsert(self, points: Sequence[PointLike], *, batch_size: int = 64) -> int:
        written = 0
        for p in points:
            if p.vector is None:
                raise ValueError(f"点 {p.chunk_id} 缺少向量")
            if p.chunk_id not in self._vectors:
                self._ids.append(p.chunk_id)
            self._vectors[p.chunk_id] = [float(x) for x in p.vector]
            self._payloads[p.chunk_id] = dict(p.payload)
            written += 1
        return written

    async def search(
        self,
        vector: Sequence[float],
        *,
        limit: int = 20,
        filters: FilterExpr | None = None,
    ) -> list[SearchHit]:
        scored: list[SearchHit] = []
        for cid in self._ids:
            payload = self._payloads[cid]
            if not _match_local(payload, filters):
                continue
            scored.append(SearchHit(cid, _cosine(vector, self._vectors[cid]), payload))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:limit]

    async def count(self) -> int:
        return len(self._ids)

    async def health(self) -> dict[str, Any]:
        return {"backend": self.name, "ok": True, "points_count": len(self._ids)}

    async def aclose(self) -> None:
        self._ids.clear()
        self._vectors.clear()
        self._payloads.clear()


# =====================================================================
# 工厂
# =====================================================================
def build_store(
    backend: str,
    *,
    dim: int,
    collection: str,
    client: Any = None,
    settings: Any = None,
) -> VectorStore:
    """按后端名构造存储实现。`client` 可注入，便于离线测试。"""
    backend = (backend or "qdrant").strip().lower()

    if backend == "qdrant":
        if client is None:
            from qdrant_client import AsyncQdrantClient

            host = getattr(settings, "qdrant_host", "localhost") if settings else "localhost"
            port = int(getattr(settings, "qdrant_port", 6333) if settings else 6333)
            client = AsyncQdrantClient(host=host, port=port)
        return QdrantStore(
            client,
            collection,
            dim=dim,
            host=getattr(settings, "qdrant_host", "") if settings else "",
            port=int(getattr(settings, "qdrant_port", 6333) if settings else 6333),
        )

    if backend == "chroma":
        if client is None:
            import chromadb

            if settings is not None and getattr(settings, "chroma_persist_dir", ""):
                client = chromadb.PersistentClient(path=str(settings.chroma_persist_dir))
            else:
                host = getattr(settings, "chroma_host", "localhost") if settings else "localhost"
                port = int(getattr(settings, "chroma_port", 8000) if settings else 8000)
                client = chromadb.HttpClient(host=host, port=port)
        return ChromaStore(client, collection, dim=dim)

    if backend == "local":
        return LocalStore(dim=dim)

    raise ValueError(f"未知的向量库后端：{backend}（可选 qdrant / chroma / local）")


# =====================================================================
# 契约自检
# =====================================================================
async def assert_contract(store: VectorStore, *, dim: int = 8) -> list[str]:
    """对任意实现跑一遍接口契约检查，返回失败项（空列表 = 全部通过）。

    存在的理由：三个后端的行为差异（分数方向、过滤语义、幂等性）**都不会报错**，
    只会让指标悄悄变差。把契约做成可执行检查，是唯一能防止
    "换库之后召回率莫名下降"的办法。
    """
    from uuid import uuid4

    failures: list[str] = []
    tag = f"contract-{uuid4().hex[:8]}"

    def pt(cid: str, vec: list[float], **extra: Any) -> Any:
        from types import SimpleNamespace

        payload = {"chunk_id": cid, "doc_id": "doc-x", "kb_version": 1, **extra}
        return SimpleNamespace(id=cid, chunk_id=cid, vector=vec, payload=payload)

    await store.ensure_ready()
    vec_a = [1.0] + [0.0] * (dim - 1)
    vec_b = [0.0, 1.0] + [0.0] * (dim - 2)
    await store.upsert([pt(f"{tag}-a", vec_a), pt(f"{tag}-b", vec_b, category="runbook")])

    # 1. 幂等：重复 upsert 不增加点数
    before = await store.count()
    await store.upsert([pt(f"{tag}-a", vec_a)])
    after = await store.count()
    if before != after:
        failures.append(f"upsert 不幂等：{before} → {after}")

    # 2. 相似度为余弦、降序返回
    hits = await store.search(vec_a, limit=5)
    mine = [h for h in hits if h.chunk_id.startswith(tag)]
    if not mine:
        failures.append("search 未返回刚写入的点")
    else:
        if abs(mine[0].score - 1.0) > 1e-3:
            failures.append(f"自相似度应为 1.0，实际 {mine[0].score:.4f}（可能未做 distance→similarity 换算）")
        if any(a.score < b.score for a, b in zip(mine, mine[1:])):
            failures.append("结果未按 score 降序")

    # 3. 过滤语义
    only_runbook = await store.search(vec_a, limit=5, filters={"category": "runbook"})
    if any(h.payload.get("category") != "runbook" for h in only_runbook):
        failures.append("filters 等值过滤失效")

    return failures

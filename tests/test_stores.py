"""向量存储抽象层的契约测试（离线可跑）。

为什么这批测试值得存在
----------------------
三后端的差异**全都不会报错**，只会让指标悄悄变差：

- Chroma 返回 cosine **distance**（越小越相似），Qdrant 返回**相似度**。
  忘了换算 → 排序整个反过来，而分数看起来"正常"。
- Chroma 的 metadata 只接受标量，list 字段必须序列化；
  忘了处理 → 直接抛类型异常，或者过滤静默返回空。
- 过滤表达式的语义差异 → "同一套条件在 A 有效、在 B 返回空"。

所以这里测的不是"能不能跑"，而是**三个实现的行为是否一致**——
最后一项 `test_cross_implementation_consistency` 是这批测试的核心。

沙箱环境没有 qdrant_client / chromadb，用两个最小 shim 注入 sys.modules。
shim 不是"模拟整个 SDK"，只提供被引用到的符号，所以它同时也是一份
"我们到底依赖了 SDK 的哪些部分"的清单 —— 这份清单越小，换版本越安全。
"""

from __future__ import annotations

import asyncio
import sys
import traceback
import types
from types import SimpleNamespace
from typing import Any

from app.ingest.stores import (
    ChromaStore,
    LocalStore,
    QdrantStore,
    SearchHit,
    _cosine,
    _flatten_metadata,
    assert_contract,
    build_store,
)

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  → {detail}" if detail else ""))
    return ok


# =====================================================================
# 最小 SDK shim
# =====================================================================
class _PointStruct:
    def __init__(self, id: str, vector: list[float], payload: dict) -> None:  # noqa: A002
        self.id = id
        self.vector = vector
        self.payload = payload


def _install_qdrant_shim() -> bool:
    """返回 True 表示用的是真实的 qdrant_client。"""
    if "qdrant_client" in sys.modules:
        return True
    try:
        import qdrant_client  # noqa: F401

        return True
    except ImportError:
        pass

    root = types.ModuleType("qdrant_client")
    models = types.ModuleType("qdrant_client.models")

    class Distance:
        COSINE = "Cosine"
        EUCLID = "Euclid"
        DOT = "Dot"

    def _dict_fn(**kw: Any) -> dict:
        return kw

    models.Distance = Distance
    models.PointStruct = _PointStruct
    models.VectorParams = _dict_fn
    models.Filter = _dict_fn
    models.FieldCondition = _dict_fn
    models.MatchValue = _dict_fn
    models.MatchAny = _dict_fn
    models.Range = _dict_fn
    root.models = models
    sys.modules["qdrant_client"] = root
    sys.modules["qdrant_client.models"] = models
    return False


QDRANT_REAL = _install_qdrant_shim()


def _filter_to_dict(qf: Any) -> dict | None:
    """把过滤条件归一化成 dict。

    真实 qdrant-client 在场时，`translate_filter_qdrant` 返回的是 pydantic 的 `Filter` 模型；
    shim 下是纯 dict。假 client 只认 dict，所以这里做一层归一化。

   副作用是好事：装了真实 SDK 的环境跑这组测试，就等于顺带验证了
    「我们构造的过滤条件真的能被 Qdrant SDK 表达出来」—— 只靠 shim 是验不到的。
    """
    if qf is None:
        return None
    if isinstance(qf, dict):
        return qf
    must: list[dict[str, Any]] = []
    for cond in getattr(qf, "must", None) or []:
        item: dict[str, Any] = {"key": getattr(cond, "key", None)}
        match = getattr(cond, "match", None)
        if match is not None:
            raw_any = getattr(match, "any", None)
            item["match"] = (
                {"any": list(raw_any)}
                if raw_any is not None
                else {"value": getattr(match, "value", None)}
            )
        rng = getattr(cond, "range", None)
        if rng is not None:
            item["range"] = {"gte": getattr(rng, "gte", None), "lte": getattr(rng, "lte", None)}
        must.append(item)
    return {"must": must}


def _qdrant_filter_match(payload: dict, qf: Any) -> bool:
    """解析 translate_filter_qdrant 的输出结构（真实 SDK 下是 pydantic 模型）。"""
    qf = _filter_to_dict(qf)
    if not qf:
        return True
    for cond in qf.get("must") or []:
        key = cond.get("key")
        val = payload.get(key)
        m = cond.get("match")
        if m is not None:
            if "value" in m and val != m["value"]:
                return False
            if "any" in m and val not in m["any"]:
                return False
        rng = cond.get("range")
        if rng is not None:
            gte, lte = rng.get("gte"), rng.get("lte")
            if gte is not None and (val is None or val < gte):
                return False
            if lte is not None and (val is None or val > lte):
                return False
    return True


class FakeQdrantClient:
    """模拟 AsyncQdrantClient 的被用到的部分。"""

    def __init__(self) -> None:
        self._points: dict[str, tuple[list[float], dict]] = {}
        self._collections: set[str] = set()
        self.create_calls = 0

    async def collection_exists(self, name: str) -> bool:
        return name in self._collections

    async def create_collection(self, collection_name: str, vectors_config: Any) -> None:
        self._collections.add(collection_name)
        self.create_calls += 1

    async def upsert(self, collection_name: str, points: list, wait: bool = True) -> None:
        for p in points:
            self._points[p.id] = (list(p.vector), dict(p.payload))

    async def count(self, collection_name: str, exact: bool = True) -> Any:
        return SimpleNamespace(count=len(self._points))

    async def query_points(
        self, collection_name: str, query: list[float], limit: int,
        query_filter: Any = None, with_payload: bool = True,
    ) -> Any:
        scored = [
            (pid, _cosine(query, vec), pl)
            for pid, (vec, pl) in self._points.items()
            if _qdrant_filter_match(pl, query_filter)
        ]
        scored.sort(key=lambda t: t[1], reverse=True)
        return SimpleNamespace(
            points=[
                SimpleNamespace(id=pid, score=s, payload=pl)
                for pid, s, pl in scored[:limit]
            ]
        )

    async def close(self) -> None:
        self.closed = True


class FakeChromaCollection:
    """模拟 chromadb Collection。**query 返回 distance 而非相似度** —— 与真实行为一致。"""

    def __init__(self) -> None:
        self._ids: list[str] = []
        self._emb: dict[str, list[float]] = {}
        self._meta: dict[str, dict] = {}
        self._docs: dict[str, str] = {}

    def upsert(self, ids: list[str], embeddings: list[list[float]],
               documents: list[str], metadatas: list[dict]) -> None:
        for i, e, d, m in zip(ids, embeddings, documents, metadatas):
            if i not in self._emb:
                self._ids.append(i)
            self._emb[i] = list(e)
            self._docs[i] = d
            self._meta[i] = m

    def count(self) -> int:
        return len(self._ids)

    def query(self, query_embeddings: list[list[float]], n_results: int,
              where: dict | None = None, include: list[str] | None = None) -> dict:
        q = query_embeddings[0]
        scored: list[tuple[str, float]] = []
        for i in self._ids:
            meta = self._meta[i]
            ok = True
            for k, v in (where or {}).items():
                if isinstance(v, dict):
                    if "$in" in v and meta.get(k) not in v["$in"]:
                        ok = False
                    if "$gte" in v and (meta.get(k) is None or meta.get(k) < v["$gte"]):
                        ok = False
                elif meta.get(k) != v:
                    ok = False
            if not ok:
                continue
            scored.append((i, 1.0 - _cosine(q, self._emb[i])))   # cosine DISTANCE
        scored.sort(key=lambda t: t[1])
        top = scored[:n_results]
        return {
            "ids": [[i for i, _ in top]],
            "distances": [[d for _, d in top]],
            "metadatas": [[self._meta[i] for i, _ in top]],
            "documents": [[self._docs[i] for i, _ in top]],
        }


class FakeChromaClient:
    def __init__(self) -> None:
        self._cols: dict[str, FakeChromaCollection] = {}
        self.requested_metadata: dict = {}

    def get_or_create_collection(self, name: str, metadata: dict | None = None) -> FakeChromaCollection:
        self.requested_metadata = dict(metadata or {})
        return self._cols.setdefault(name, FakeChromaCollection())


def make_point(cid: str, vec: list[float], **extra: Any) -> Any:
    payload = {"chunk_id": cid, "doc_id": "doc-x", "kb_version": 1, **extra}
    return SimpleNamespace(id=cid, chunk_id=cid, vector=vec, payload=payload)


# =====================================================================
# 测试
# =====================================================================
async def test_local_contract() -> None:
    print("\n【LocalStore】内存实现")
    store = LocalStore(dim=4)
    failures = await assert_contract(store, dim=4)
    check("接口契约（幂等 / 相似度方向 / 过滤）", not failures, "; ".join(failures))

    # 覆盖写：同 chunk_id 二次写入不重复计点
    await store.upsert([make_point("dup", [1.0, 0.0, 0.0, 0.0])])
    n1 = await store.count()
    await store.upsert([make_point("dup", [1.0, 0.0, 0.0, 0.0])])
    check("重复 upsert 不产生重复点", await store.count() == n1, f"{n1} → {await store.count()}")

    hits = await store.search([1.0, 0.0, 0.0, 0.0], limit=3)
    check("search 返回 SearchHit 且降序", isinstance(hits[0], SearchHit) and hits[0].score >= hits[-1].score)
    check("payload 随命中一起返回", hits[0].payload.get("doc_id") == "doc-x")


async def test_qdrant_contract() -> None:
    print(f"\n【QdrantStore】{'真实 qdrant_client' if QDRANT_REAL else 'shim（沙箱无该依赖）'}")
    client = FakeQdrantClient()
    store = QdrantStore(client, "test_kb", dim=4)
    failures = await assert_contract(store, dim=4)
    check("接口契约（幂等 / 相似度方向 / 过滤）", not failures, "; ".join(failures))

    check("ensure_ready 会建集合", client.create_calls == 1, str(client.create_calls))
    await store.ensure_ready()
    check("ensure_ready 幂等（不重复建集合）", client.create_calls == 1, str(client.create_calls))

    # 批大小分片：5 个点、batch=2 → 应写满 3 次
    store2 = QdrantStore(FakeQdrantClient(), "batch_kb", dim=2)
    await store2.ensure_ready()
    written = await store2.upsert(
        [make_point(f"b{i}", [1.0, 0.0]) for i in range(5)], batch_size=2
    )
    check("upsert 按 batch_size 分片且总数正确", written == 5, str(written))

    await store.aclose()
    check("aclose 释放客户端", getattr(client, "closed", False))

    missing = QdrantStore(FakeQdrantClient(), "x", dim=2)
    try:
        await missing.upsert([SimpleNamespace(id="a", chunk_id="a", vector=None, payload={})])
        check("缺向量的点应报错", False, "未抛异常")
    except ValueError:
        check("缺向量的点应报错", True)


async def test_chroma_contract() -> None:
    print("\n【ChromaStore】shim（沙箱无 chromadb）")
    client = FakeChromaClient()
    store = ChromaStore(client, "test_kb", dim=4)
    failures = await assert_contract(store, dim=4)
    check("接口契约（幂等 / 相似度方向 / 过滤）", not failures, "; ".join(failures))

    check(
        "显式指定 hnsw:space=cosine（默认是 l2，会改变阈值语义）",
        client.requested_metadata.get("hnsw:space") == "cosine",
        str(client.requested_metadata),
    )

    # 这一项是整批测试里最关键的一条。
    # 用独立集合：契约检查写入的点与 near 向量**完全相同**，
    # 留在同一集合里会以同分抢占 top-1，掩盖真正的换算验证。
    iso = ChromaStore(client, "iso_kb", dim=4)
    await iso.ensure_ready()
    await iso.upsert([
        make_point("near", [1.0, 0.0, 0.0, 0.0]),
        make_point("far", [0.0, 1.0, 0.0, 0.0]),
    ])
    hits = await iso.search([1.0, 0.0, 0.0, 0.0], limit=2)
    ids = [h.chunk_id for h in hits]
    check("distance 已换算为相似度（最近者排第一）", ids[0] == "near", str(ids))
    check(
        "自相似度为 1.0（未换算时会是 0.0）",
        abs(hits[0].score - 1.0) < 1e-3,
        f"{hits[0].score:.4f}",
    )
    check(
        "分数为升序反例保护：相似度必须降序",
        hits[0].score > hits[1].score,
        f"{hits[0].score:.4f} vs {hits[1].score:.4f}",
    )

    # metadata 类型约束
    flat = _flatten_metadata({"tags": ["A", "B"], "n": 3, "ok": True, "none": None})
    check("list 字段序列化为 JSON 字符串", flat["tags"] == '["A", "B"]', str(flat.get("tags")))
    check("None 字段被丢弃（Chroma 不接受）", "none" not in flat)
    check("标量字段类型保持", flat["n"] == 3 and flat["ok"] is True)

    # 未初始化就调用要给明确错误，而不是 AttributeError
    fresh = ChromaStore(FakeChromaClient(), "nope", dim=4)
    try:
        await fresh.upsert([make_point("a", [1.0, 0.0, 0.0, 0.0])])
        check("未 ensure_ready 就写入应报明确错误", False, "未抛异常")
    except RuntimeError:
        check("未 ensure_ready 就写入应报明确错误", True)


async def test_factory() -> None:
    print("\n【工厂】后端分发")
    local = build_store("local", dim=8, collection="c")
    check("local → LocalStore", isinstance(local, LocalStore))

    check("未知后端应报错", _raises(lambda: build_store("milvus", dim=8, collection="c")))
    check("大小写与空白容错", isinstance(build_store(" LOCAL ", dim=8, collection="c"), LocalStore))

    # 注入 client 时不应触发真实 SDK 导入
    q = build_store("qdrant", dim=8, collection="c", client=FakeQdrantClient())
    check("qdrant + 注入 client → QdrantStore", isinstance(q, QdrantStore))
    ch = build_store("chroma", dim=8, collection="c", client=FakeChromaClient())
    check("chroma + 注入 client → ChromaStore", isinstance(ch, ChromaStore))


def _raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    except Exception:
        return False
    return False


async def test_cross_implementation_consistency() -> None:
    """核心测试：三个实现对同一批数据和同一套过滤条件，必须给出**相同的排序与分数**。

    这就是"换向量库不改行为"这个承诺的可执行版本。
    如果这条挂了，那么不同后端跑出的召回率数字就不可比 ——
    而不可比的数字写进简历是灾难性的。
    """
    print("\n【跨实现一致性】同一数据 + 同一过滤器 → 相同结果")

    points = [
        make_point("hit-a", [1.0, 0.0, 0.0, 0.0], category="runbook", kb_version=1),
        make_point("hit-b", [0.9, 0.1, 0.0, 0.0], category="runbook", kb_version=1),
        make_point("miss-c", [0.0, 1.0, 0.0, 0.0], category="faq", kb_version=1),
        make_point("old-d", [1.0, 0.0, 0.0, 0.0], category="runbook", kb_version=0),
    ]
    qv = [1.0, 0.0, 0.0, 0.0]

    stores: list[tuple[str, Any]] = [
        ("local", LocalStore(dim=4)),
        ("qdrant", QdrantStore(FakeQdrantClient(), "c", dim=4)),
        ("chroma", ChromaStore(FakeChromaClient(), "c", dim=4)),
    ]

    results: dict[str, list[tuple[str, float]]] = {}
    for name, st in stores:
        await st.ensure_ready()
        await st.upsert(points)
        hits = await st.search(qv, limit=10, filters={"category": "runbook", "kb_version": 1})
        results[name] = [(h.chunk_id, h.score) for h in hits]

    baseline = results["local"]
    check("local 过滤后命中 2 条（排除 faq 与旧版本）", len(baseline) == 2, str(baseline))

    for name, got in results.items():
        if name == "local":
            continue
        same_order = [c for c, _ in got] == [c for c, _ in baseline]
        check(f"{name} 与 local 排序一致", same_order, f"{got} vs {baseline}")
        check(
            f"{name} 与 local 分数一致（1e-3 容差）",
            len(got) == len(baseline)
            and all(abs(a[1] - b[1]) < 1e-3 for a, b in zip(got, baseline)),
            f"{got} vs {baseline}",
        )

    # 范围过滤
    for name, st in stores:
        hits = await st.search(qv, limit=10, filters={"kb_version": {"$gte": 1}})
        check(f"{name} 支持 $gte 范围过滤", len(hits) == 3, f"{len(hits)} 条")

    # $in
    for name, st in stores:
        hits = await st.search(qv, limit=10, filters={"category": {"$in": ["faq"]}})
        check(f"{name} 支持 $in 集合过滤", [h.chunk_id for h in hits] == ["miss-c"],
              str([h.chunk_id for h in hits]))


async def main() -> int:
    print("=" * 70)
    print("向量存储抽象层 —— 行为契约测试")
    print("=" * 70)

    await test_local_contract()
    await test_qdrant_contract()
    await test_chroma_contract()
    await test_factory()
    await test_cross_implementation_consistency()

    print("\n" + "=" * 70)
    print(f"通过 {PASS} / 失败 {FAIL}")
    if not QDRANT_REAL:
        print("注：qdrant_client 未安装，QdrantStore 在 shim 上验证逻辑；")
        print("    真实部署后请重跑本测试以确认 SDK 版本兼容。")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)

"""召回链路「评估 == 线上」的收敛测试。

为什么需要这个文件
------------------
评估脚本曾经自己实现了一遍融合与重排，与服务端成了两条代码路径。
两者的差异**至少**有四处（kb_version 过滤、每路文档上限、融合粒度、
重排候选池），而且方向不一致 —— 有的让分数虚高，有的让它虚低。
合起来的结果不是"略高"或"略低"，而是**一个别的系统的分数**。

更麻烦的是：这种分叉**不会报错、不会有告警、不会让任何测试变红**。
它只会让简历上那句"由 x 提升到 y"失去意义。

所以这个文件测的不是"功能对不对"，而是**结构约束**：
    1. 评估脚本必须通过服务端的 `recall_detailed` 取结果
       （有人重新在 eval 里写一份检索，`test_eval_calls_serving_recall` 会红）
    2. 消融只能是 `RecallConfig` 的字段，不能是代码分支
    3. 两条向量路径（本地 vs 向量库）产出**同形状**的 Chunk
       —— 形状不同的话，"一致性校验"比没有校验更误导
    4. 每路文档上限、截断顺序这些"看起来是细节"的地方，
       一旦顺序写反，分数会变而不报错

第 3 条最容易漏：`LocalVectorRecaller` 存在的唯一价值是当对照，
如果它自己拼一遍 Chunk，它就不再是对照了。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings                      # noqa: E402
from app.pipeline import rag                              # noqa: E402
from app.pipeline.rag import (                            # noqa: E402
    CH_BM25,
    CH_VECTOR,
    BM25Recaller,
    IndexBundle,
    LocalVectorRecaller,
    RecallConfig,
    VectorRecaller,
    _chunk_from_payload,
    get_recallers,
    recall_detailed,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


# =====================================================================
# 测试替身
# =====================================================================
class FakeBM25:
    """够用的 BM25 替身：给定固定的命中顺序。"""

    def __init__(self, hits: list[tuple[str, float]]):
        self._hits = hits
        self.size = len(hits)

    def search(self, query: str, *, top_k: int = 20) -> list[tuple[str, float]]:
        return self._hits[:top_k]


class FakeHit:
    def __init__(self, chunk_id: str, score: float, payload: dict):
        self.chunk_id = chunk_id
        self.score = score
        self.payload = payload


class FakeStore:
    """假向量库。记录收到的 filters，用来验证 kb_version 过滤确实传下去了。"""

    name = "fake"

    def __init__(self, hits: list[FakeHit], *, raise_on_search: bool = False):
        self._hits = hits
        self.raise_on_search = raise_on_search
        self.seen_filters: list[dict | None] = []

    async def search(self, vector, *, limit=20, filters=None):
        self.seen_filters.append(filters)
        if self.raise_on_search:
            raise RuntimeError("向量库连接失败（模拟）")
        return self._hits[:limit]

    async def count(self) -> int:
        return len(self._hits)

    async def aclose(self) -> None:
        pass


def make_bundle(chunks: dict[str, str], *, kb_version: int = 1, bm25=None) -> IndexBundle:
    """构造一个"已加载"的 IndexBundle，绕开磁盘 IO。

    IndexBundle.__init__ 不做 IO，字段注入即可 —— 这让测试不需要造一份
    完整的 build 产物，也就不会因为 build 逻辑改动而连带失败。
    """
    bundle = IndexBundle(Path("."))
    bundle.available = True
    bundle.kb_version = kb_version
    bundle.dim = 3
    bundle.chunk_to_doc = dict(chunks)
    bundle.chunk_text = {cid: f"正文 {cid}" for cid in chunks}
    bundle.chunk_payload = {
        cid: {"doc_id": doc, "chunk_id": cid, "kb_version": kb_version, "text": f"正文 {cid}"}
        for cid, doc in chunks.items()
    }
    by_doc: dict[str, list[str]] = {}
    for cid, doc in chunks.items():
        by_doc.setdefault(doc, []).append(cid)
    bundle.chunks_by_doc = by_doc
    bundle.bm25 = bm25
    return bundle


# 一份小语料：doc-a 有 5 块（用来验每路占比上限），doc-b / doc-c 各 1 块
CHUNKS = {
    "a#1": "doc-a", "a#2": "doc-a", "a#3": "doc-a", "a#4": "doc-a", "a#5": "doc-a",
    "b#1": "doc-b",
    "c#1": "doc-c",
}


def local_vectors() -> tuple[list[str], list[list[float]]]:
    """按"与查询向量 [1,0,0] 的余弦"排出的确定性顺序：a#1 > a#2 > b#1 > c#1 …"""
    ids = ["a#1", "a#2", "a#3", "a#4", "a#5", "b#1", "c#1"]
    vecs = [
        [1.0, 0.0, 0.0],
        [0.9, 0.1, 0.0],
        [0.3, 0.9, 0.0],
        [0.2, 0.9, 0.0],
        [0.1, 0.9, 0.0],
        [0.8, 0.2, 0.0],
        [0.7, 0.3, 0.0],
    ]
    return ids, vecs


# =====================================================================
# 1. 配置层：消融是参数，不是分支
# =====================================================================
def test_config_overrides() -> None:
    print("配置层：消融只能是参数")
    base = RecallConfig()
    over = base.with_overrides(top_k=7, fusion=False)
    check("top_k 映射到 pool", over.pool == 7, str(over))
    check("fusion 被覆盖", over.fusion is False, str(over))
    check("未指定的字段不动", over.channels == base.channels, str(over))
    check(
        "无覆盖时返回同一个对象（避免无谓拷贝）",
        base.with_overrides() is base,
        "返回了新对象",
    )


def test_channel_selection() -> None:
    print("按通道装配召回路")
    only_bm25 = get_recallers(RecallConfig(channels=(CH_BM25,), fusion=False))
    check("只配 BM25 时只装一路", [type(r).__name__ for r in only_bm25] == ["BM25Recaller"])

    local = get_recallers(RecallConfig(channels=(CH_VECTOR,), vector_source="local"))
    check("vector_source=local 走本地向量", isinstance(local[0], LocalVectorRecaller))

    store = get_recallers(RecallConfig(channels=(CH_VECTOR,), backend="chroma"))
    check("vector_source=store 走向量库", isinstance(store[0], VectorRecaller))
    check("后端名被传下去（换后端不改代码）", store[0].backend == "chroma", store[0].backend)

    both = get_recallers(RecallConfig(channels=(CH_VECTOR, CH_BM25)))
    check("两路顺序与配置一致", [r.name for r in both] == [CH_VECTOR, CH_BM25], str(both))


def test_unknown_channel_is_loud() -> None:
    print("未知通道必须立刻炸")
    # 打错的通道名如果被静默忽略，表现是"以为开了三路其实只有两路"，
    # 而这只会在看召回率对不上时才发现 —— 那时已经查了很久了。
    try:
        get_recallers(RecallConfig(channels=("vecotr",)))
    except ValueError as exc:
        check("抛 ValueError", True)
        check("错误信息列出可选通道", "vector" in str(exc), str(exc))
    else:
        check("抛 ValueError", False, "未知通道被静默忽略了")


# =====================================================================
# 2. 结构约束：评估脚本必须走服务端召回
# =====================================================================
def test_eval_calls_serving_recall(monkeypatch) -> None:
    """这条是本文件的核心：有人在 eval 里重新实现一份检索，它会红。"""
    print("评估脚本必须通过服务端 recall_detailed 取结果")
    import scripts.eval_retrieval as ev

    seen: list[RecallConfig] = []

    async def spy(query, *, embedding=None, top_k=None, fusion=None, config=None):
        seen.append(config)
        return rag.RecallOutcome(
            chunks=[
                rag.Chunk(doc_id="doc-a", chunk_id="a#1", text="t", score=0.9),
                rag.Chunk(doc_id="doc-b", chunk_id="b#1", text="t", score=0.5),
            ],
            channels={"vector": 2},
            fused=False,
        )

    monkeypatch.setattr(ev, "recall_detailed", spy)
    questions = [SimpleNamespace(qid=1, question="连接池耗尽怎么处理？")]

    cfg = RecallConfig(channels=(CH_VECTOR,), vector_source="local", pool=9)
    cell = asyncio.run(ev.run_cell(
        questions, config=cfg, rerank=False, rerank_max_per_doc=0, rerank_top_k=0,
    ))

    check("确实调用了服务端召回", len(seen) == 1, f"调用 {len(seen)} 次")
    check("传的是同一个 config 对象", seen and seen[0] is cfg, "config 被换掉了")
    check("块级结果被投影成文档级", cell.retrieved[1] == ["doc-a", "doc-b"], str(cell.retrieved))
    # 传 0 表示"没指定"，必须解析成 settings 的默认预算（2 块/篇、5 块）。
    # 这里只有 2 个块、各属一篇，默认预算下不会被砍 → 两个口径自然一致。
    # （注意：这**不**意味着"不重排时线上不截断" —— 见下面那条用例。）
    check("预算宽松时两个口径一致", cell.retrieved_served == cell.retrieved, "两个口径不一致")


def test_eval_served_projection_is_stricter(monkeypatch) -> None:
    """两个口径的语义必须被钉住：块数预算让它更严，占比上限让它更宽。

    这两个开关方向相反，很容易被混为一谈：
      - `rerank_top_k` 是**预算**：只带 k 块进上下文 → 严格时文档数变少
      - `rerank_max_per_doc` 是**多样性约束**：限制单篇占比 → 文档数变多
    如果只测其中一个，另一个写反了也不会有人发现。
    """
    print("线上口径的两个开关：预算变严、占比变宽")
    import scripts.eval_retrieval as ev

    async def fake_recall(query, *, embedding=None, top_k=None, fusion=None, config=None):
        # 前 4 块全部来自 doc-a，第 5 块来自 doc-b
        return rag.RecallOutcome(
            chunks=[
                rag.Chunk(doc_id="doc-a", chunk_id=f"a#{i}", text="t", score=1.0 - i * 0.1)
                for i in range(1, 5)
            ] + [rag.Chunk(doc_id="doc-b", chunk_id="b#1", text="t", score=0.1)],
            channels={"vector": 5},
            fused=False,
        )

    async def fake_rerank(query, chunks, top_k=None, *, max_per_doc=None):
        return list(chunks)          # 保序，只验投影

    monkeypatch.setattr(ev, "recall_detailed", fake_recall)
    monkeypatch.setattr(ev, "rerank_chunks", fake_rerank)

    questions = [SimpleNamespace(qid=1, question="q")]
    base = RecallConfig(channels=(CH_VECTOR,))

    # 预算收紧到 1 块 → 线上只看到 doc-a
    tight = asyncio.run(ev.run_cell(
        questions, config=base, rerank=True, rerank_max_per_doc=2, rerank_top_k=1,
    ))
    check("pool 口径包含两篇文档", tight.retrieved[1] == ["doc-a", "doc-b"],
          str(tight.retrieved))
    check("预算=1 块时线上只剩 doc-a",
          tight.retrieved_served[1] == ["doc-a"], str(tight.retrieved_served))

    # 占比上限收紧到 1 → 反而让 doc-b 挤进前两名（多样性约束的方向）
    diverse = asyncio.run(ev.run_cell(
        questions, config=base, rerank=True, rerank_max_per_doc=1, rerank_top_k=5,
    ))
    check("占比上限=1 时线上拿到两篇文档",
          diverse.retrieved_served[1] == ["doc-a", "doc-b"], str(diverse.retrieved_served))


def test_baseline_also_gets_served_truncation(monkeypatch) -> None:
    """不重排的格子**也要**算线上口径，且 0 不能被理解成"取 0 块"。

    这是从一个真实的口径漏洞来的：早期只给重排格算 served，
    非重排格直接沿用 pool 值 —— 等于拿「重排 + 真实截断」
    比「不重排 + 不截断」。而后者在线上根本不存在
    （`RERANK_ENABLED=false` 时走 `_no_rerank_order()`，照样先限每篇块数再截断），
    所以那个对比会**系统性偏袒重排**。

    修掉之后，重排的线上增益从"看起来 +2.43pp"缩到了 **0.69pp**
    （低于 48 题下 2.08pp 的分辨率）。所以这条用例护住的
    不是一个数字，而是**"重排到底值不值"这个结论本身**。
    """
    print("不重排的格子也要算线上口径")
    import scripts.eval_retrieval as ev

    async def fake_recall(query, *, embedding=None, top_k=None, fusion=None, config=None):
        # 前 3 块来自 doc-a，第 4 块来自 doc-b
        return rag.RecallOutcome(
            chunks=[
                rag.Chunk(doc_id="doc-a", chunk_id=f"a#{i}", text="t", score=1.0 - i * 0.1)
                for i in range(1, 4)
            ] + [rag.Chunk(doc_id="doc-b", chunk_id="b#1", text="t", score=0.1)],
            channels={"vector": 4},
            fused=False,
        )

    monkeypatch.setattr(ev, "recall_detailed", fake_recall)
    questions = [SimpleNamespace(qid=1, question="q")]
    base = RecallConfig(channels=(CH_VECTOR,))

    cell = asyncio.run(ev.run_cell(
        questions, config=base, rerank=False, rerank_max_per_doc=2, rerank_top_k=2,
    ))
    check("pool 口径含两篇文档", cell.retrieved[1] == ["doc-a", "doc-b"],
          str(cell.retrieved))
    check("不重排时线上口径照样被截断（只带 2 块 → 只剩 doc-a）",
          cell.retrieved_served[1] == ["doc-a"], str(cell.retrieved_served))
    check("两个口径确实不同（漏洞没有回来）",
          cell.retrieved_served != cell.retrieved, "两个口径相同")

    # 预算足够宽时两个口径应当一致 —— 否则就是把"截断"错误地永久开启了
    loose = asyncio.run(ev.run_cell(
        questions, config=base, rerank=False, rerank_max_per_doc=2, rerank_top_k=5,
    ))
    check("预算 =5 块时两个口径一致", loose.retrieved_served == loose.retrieved,
          f"{loose.retrieved} vs {loose.retrieved_served}")


def test_serving_budget_zero_means_default(monkeypatch) -> None:
    """`rerank_top_k=0` 必须被解析成 settings 默认，不是"取 0 块"。

    这条护住一个差点溜过去的坑：把 served 口径也用到非重排格之后，
    原来那些传 `rerank_top_k=0` 的调用方（包括 `eval_matrix` 的 parity 校验）
    会让 `serving_top(..., k=0)` 老老实实返回**空列表** ——
    线上口径静默变成 0。不报错、不告警，只是数字不对。
    """
    print("served 预算传 0 = 用默认，不是取 0 块")
    import scripts.eval_retrieval as ev

    async def fake_recall(query, *, embedding=None, top_k=None, fusion=None, config=None):
        return rag.RecallOutcome(
            chunks=[
                rag.Chunk(doc_id="doc-a", chunk_id=f"a#{i}", text="t", score=1.0 - i * 0.1)
                for i in range(1, 4)
            ],
            channels={"vector": 3},
            fused=False,
        )

    monkeypatch.setattr(ev, "recall_detailed", fake_recall)
    questions = [SimpleNamespace(qid=1, question="q")]

    cell = asyncio.run(ev.run_cell(
        questions, config=RecallConfig(channels=(CH_VECTOR,)), rerank=False,
        rerank_max_per_doc=0, rerank_top_k=0,
    ))
    check("文档没被清空（0 被解析成默认预算）",
          cell.retrieved_served[1] == ["doc-a"], str(cell.retrieved_served))


def test_serving_top_caps_before_truncate() -> None:
    print("截断顺序：先限每篇块数，再截断")
    from scripts.eval_retrieval import serving_top

    chunks = [
        rag.Chunk(doc_id="doc-a", chunk_id=f"a#{i}", text="t", score=1.0 - i * 0.01)
        for i in range(1, 6)
    ] + [rag.Chunk(doc_id="doc-b", chunk_id="b#1", text="t", score=0.5)]

    out = serving_top(chunks, max_per_doc=1, k=2)
    # 先限每篇 1 块 → [a#1, b#1] → 取 2 → 两篇文档都在
    check("每篇上限生效", [c.doc_id for c in out] == ["doc-a", "doc-b"], str([c.doc_id for c in out]))
    # 若顺序写反（先截断 2 块再限每篇），会得到 [a#1] —— 只剩一篇。
    # 这个差别就是"多样性约束能不能生效"，所以顺序必须被钉住。
    reversed_order = serving_top(chunks[:2], max_per_doc=1, k=2)
    check(
        "顺序写反会丢掉第二个文档（所以顺序不能反）",
        [c.doc_id for c in reversed_order] == ["doc-a"],
        str([c.doc_id for c in reversed_order]),
    )


# =====================================================================
# 3. 召回层：占比上限与降级
# =====================================================================
def test_per_channel_cap_applied(monkeypatch) -> None:
    print("每路文档占比上限（融合之前）")
    bundle = make_bundle(CHUNKS, bm25=FakeBM25([("b#1", 3.0), ("c#1", 2.0)]))
    monkeypatch.setattr(rag, "get_bundle", lambda: bundle)
    monkeypatch.setattr(settings, "recall_max_per_doc", 2)

    hits = [
        FakeHit(cid, 1.0 - i * 0.01, bundle.payload(cid))
        for i, cid in enumerate(["a#1", "a#2", "a#3", "a#4", "a#5"])
    ]
    monkeypatch.setattr(rag, "get_store", lambda backend=None: FakeStore(hits))

    outcome = asyncio.run(recall_detailed(
        "q", embedding=[1.0, 0.0, 0.0],
        config=RecallConfig(channels=(CH_VECTOR,), fusion=False, pool=10),
    ))
    docs = [c.doc_id for c in outcome.chunks]
    check("doc-a 被限制在 2 块", docs.count("doc-a") == 2, str(docs))
    check("通道命中数如实上报（原始条数，不是截断后）", outcome.channels == {CH_VECTOR: 5},
          str(outcome.channels))


def test_kb_version_filter_passed(monkeypatch) -> None:
    print("kb_version 过滤确实传给了向量库")
    bundle = make_bundle(CHUNKS, kb_version=7)
    monkeypatch.setattr(rag, "get_bundle", lambda: bundle)
    store = FakeStore([FakeHit("a#1", 0.9, bundle.payload("a#1"))])
    monkeypatch.setattr(rag, "get_store", lambda backend=None: store)

    asyncio.run(recall_detailed(
        "q", embedding=[1.0, 0.0, 0.0],
        config=RecallConfig(channels=(CH_VECTOR,), fusion=False),
    ))
    check("filters 里带上了运行时 kb_version（不是 settings 快照）",
          store.seen_filters == [{"kb_version": 7}], str(store.seen_filters))


def test_local_and_store_recallers_agree_on_shape(monkeypatch) -> None:
    """两条向量路径必须产出同形状的 Chunk —— 否则"对照"本身不可信。"""
    print("本地向量 vs 向量库：Chunk 形状与文档顺序一致")
    bundle = make_bundle(CHUNKS)
    monkeypatch.setattr(rag, "get_bundle", lambda: bundle)

    ids, vecs = local_vectors()
    monkeypatch.setattr(rag, "_load_local_vectors", lambda index_dir: (ids, vecs))

    local = asyncio.run(LocalVectorRecaller().recall(
        "q", embedding=[1.0, 0.0, 0.0], top_k=3
    ))

    # 让假向量库返回同样的顺序，两边只应差在索引来源上
    store_ids = ["a#1", "a#2", "b#1"]
    store = FakeStore([FakeHit(cid, 1.0, bundle.payload(cid)) for cid in store_ids])
    monkeypatch.setattr(rag, "get_store", lambda backend=None: store)
    from_store = asyncio.run(VectorRecaller().recall("q", embedding=[1.0, 0.0, 0.0], top_k=3))

    check("本地向量按余弦排出预期顺序",
          [c.chunk_id for c in local] == store_ids,
          str([c.chunk_id for c in local]))
    check("两条路径的 doc_id 完全一致",
          [c.doc_id for c in local] == [c.doc_id for c in from_store],
          f"{[c.doc_id for c in local]} vs {[c.doc_id for c in from_store]}")
    # Chunk 是 slots dataclass，没有 __dict__ —— 用 dataclasses.fields 比。
    # 形状相同是"两条路径可互为对照"的前提：任一字段缺失或改名，
    # 对照就变成了两个不同系统的比较。
    import dataclasses

    local_fields = {f.name for f in dataclasses.fields(local[0])}
    store_fields = {f.name for f in dataclasses.fields(from_store[0])}
    check("字段集合一致（形状相同才谈得上对照）",
          local_fields == store_fields == {
              "doc_id", "chunk_id", "text", "source", "page",
              "modality", "score", "rerank_score", "recall_source", "metadata",
          },
          f"{sorted(local_fields)} vs {sorted(store_fields)}")
    check("两条路径的 recall_source 标签相同",
          local[0].recall_source == from_store[0].recall_source == CH_VECTOR,
          f"{local[0].recall_source} vs {from_store[0].recall_source}")


def test_local_vector_respects_version_guard(monkeypatch) -> None:
    print("本地向量同样挡掉非当前版本的块")
    bundle = make_bundle({"a#1": "doc-a"})          # 只有 a#1 是当前版本
    monkeypatch.setattr(rag, "get_bundle", lambda: bundle)
    # 向量缓存里还有上一版留下的 x#9，不能让它进来
    monkeypatch.setattr(
        rag, "_load_local_vectors",
        lambda index_dir: (["x#9", "a#1"], [[1.0, 0.0, 0.0], [0.9, 0.0, 0.0]]),
    )
    out = asyncio.run(LocalVectorRecaller().recall("q", embedding=[1.0, 0.0, 0.0], top_k=5))
    check("旧版本块被挡掉", [c.chunk_id for c in out] == ["a#1"], str([c.chunk_id for c in out]))


def test_recall_degrades_one_channel(monkeypatch) -> None:
    print("一路挂掉不影响另一路（可用性设计）")
    bundle = make_bundle(CHUNKS, bm25=FakeBM25([("b#1", 3.0)]))
    monkeypatch.setattr(rag, "get_bundle", lambda: bundle)
    monkeypatch.setattr(
        rag, "get_store", lambda backend=None: FakeStore([], raise_on_search=True)
    )
    outcome = asyncio.run(recall_detailed(
        "q", embedding=[1.0, 0.0, 0.0],
        config=RecallConfig(channels=(CH_VECTOR, CH_BM25), fusion=True),
    ))
    check("BM25 结果仍然返回", [c.doc_id for c in outcome.chunks] == ["doc-b"],
          str([c.doc_id for c in outcome.chunks]))
    check("降级被如实记录", outcome.degraded and outcome.degraded[0].startswith("vector:"),
          str(outcome.degraded))
    check("挂掉那一路命中数记 0", outcome.channels.get(CH_VECTOR) == 0, str(outcome.channels))
    # 语义钉死：`fused` 表示"**真的发生了**融合"，不是"配置里开了融合"。
    # 只活下来一路时没有东西可融合，报 True 就是谎报 ——
    # 而 SSE 的 docs 事件会把这个字段推给前端，谎报会让演示时说错话。
    check("单路存活时不谎报 fused", outcome.fused is False, f"fused={outcome.fused}")
    # 于是有一个必须被记住的后果：光看 fused 无法区分
    # "本来就只有一路" 与 "两路配了但一路挂了"。区分它们的唯一线索是 degraded。
    check("degraded 是区分「只有一路」与「一路挂了」的唯一线索",
          bool(outcome.degraded), str(outcome.degraded))


def test_single_channel_doc_order_unchanged_by_cap(monkeypatch) -> None:
    """占比上限只"删块"，不该改变文档的排序。

    推理：文档级顺序 = 各文档**首次出现**的位置；删掉第 4 块以后的块
    不影响首次出现。所以带 cap 与不带 cap 的文档序列应当一致
    （只要池子里仍有足够多不同文档）。这条不成立就说明 cap 的实现
    用错了比较对象。
    """
    print("占比上限不改变文档顺序")
    bundle = make_bundle(CHUNKS)
    monkeypatch.setattr(rag, "get_bundle", lambda: bundle)
    hits = [
        FakeHit(cid, 1.0 - i * 0.01, bundle.payload(cid))
        for i, cid in enumerate(["a#1", "a#2", "a#3", "a#4", "a#5", "b#1", "c#1"])
    ]
    monkeypatch.setattr(rag, "get_store", lambda backend=None: FakeStore(hits))

    def docs(cap: int) -> list[str]:
        outcome = asyncio.run(recall_detailed(
            "q", embedding=[1.0, 0.0, 0.0],
            config=RecallConfig(channels=(CH_VECTOR,), fusion=False, pool=10, max_per_doc=cap),
        ))
        from app.pipeline.fusion import dedupe_docs
        return dedupe_docs([c.chunk_id for c in outcome.chunks],
                           {c.chunk_id: c.doc_id for c in outcome.chunks})

    check("cap=1 与 cap=5 的文档序列一致", docs(1) == docs(5), f"{docs(1)} vs {docs(5)}")
    check("文档序列是 [doc-a, doc-b, doc-c]", docs(5) == ["doc-a", "doc-b", "doc-c"], str(docs(5)))


# =====================================================================
# 4. 统一构造：doc_id 只可能有一种来源
# =====================================================================
def test_chunk_from_payload_doc_id() -> None:
    print("Chunk 构造：doc_id 是评估锚点，必须有明确来源")
    bundle = make_bundle({"a#1": "doc-from-bundle"})
    from_payload = _chunk_from_payload(
        {"doc_id": "doc-from-payload", "chunk_id": "a#1", "text": "T"},
        chunk_id="a#1", score=0.5, recall_source=CH_VECTOR, bundle=bundle,
    )
    check("payload 有 doc_id 时以 payload 为准",
          from_payload.doc_id == "doc-from-payload", from_payload.doc_id)

    fallback = _chunk_from_payload(
        {"chunk_id": "a#1"},
        chunk_id="a#1", score=0.5, recall_source=CH_BM25, bundle=bundle,
    )
    check("payload 缺 doc_id 时回退到索引产物",
          fallback.doc_id == "doc-from-bundle", fallback.doc_id)
    check("text 回退到索引产物", fallback.text == "正文 a#1", fallback.text)
    check("modality 有默认值", fallback.modality == "text", fallback.modality)
    check("metadata 里不重复存 text（省内存）", "text" not in fallback.metadata,
          str(list(fallback.metadata)))


# =====================================================================
# 5. 向量库按后端名缓存
# =====================================================================
def test_store_cache_per_backend(monkeypatch) -> None:
    print("向量库按后端名缓存")
    created: list[str] = []

    def fake_build_store(backend, **kwargs):
        created.append(backend)
        return FakeStore([])

    import app.ingest.stores as stores_mod

    monkeypatch.setattr(stores_mod, "build_store", fake_build_store)
    rag._stores.clear()
    try:
        a1 = rag.get_store("qdrant")
        a2 = rag.get_store("qdrant")
        b1 = rag.get_store("chroma")
        check("同一后端复用同一个实例", a1 is a2, "重复创建了")
        check("不同后端各自建实例", a1 is not b1, "两个后端共用了实例")
        check("构建次数 = 后端种数", created == ["qdrant", "chroma"], str(created))
    finally:
        rag._stores.clear()
        asyncio.run(rag.aclose_store())
    check("aclose_store 后缓存被清空", rag._stores == {}, str(rag._stores))


def main() -> int:
    """只跑不依赖 pytest fixture 的那几条。

    带 `monkeypatch` 的用例（评估链路、召回层）需要 pytest 提供 fixture，
    所以必须用 `pytest tests/test_recall_convergence.py` 跑 —— 这也是 CI 的口径。
    直接 `python -m tests.test_recall_convergence` 只能覆盖下面这几条，
    刻意保留是为了在没有 pytest 的环境里也能做一次最小自检。
    """
    print("=" * 70)
    print("召回链路收敛测试（评估 == 线上）· 最小自检")
    print("=" * 70)
    test_config_overrides()
    test_channel_selection()
    test_unknown_channel_is_loud()
    test_serving_top_caps_before_truncate()
    test_chunk_from_payload_doc_id()
    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

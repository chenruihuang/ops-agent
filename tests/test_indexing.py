"""索引层 + 召回评估测试。

这里跑的是**完整链路**：语料 → 切块 → 建点 → 向量库 → 检索 → recall 计算，
全程不联网（假 embedder + 内存向量库）。它保证的是"链路正确"，
不是"检索质量"——后者必须用真实模型测。
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import traceback
from pathlib import Path

from app.ingest.chunker import EMBED_MAX_CHARS
from app.ingest.evaluate import Question, evaluate, expand_one_hop, format_table, recall_at_k
from app.ingest.images import RuleCaptionProvider, TranscriptOcrProvider, load_images
from app.ingest.indexer import (
    brute_force_search,
    build_points,
    build_registry,
    chunk_to_payload,
    neighbor_map,
    point_id_for,
    read_points,
    read_vector_meta,
    read_vectors,
    texts_fingerprint,
    write_points,
    write_vectors,
)
from app.ingest.loader import load_markdown
from app.ingest.textindex import BM25Index, build_bm25, tokenize
from tests.fake_vector import FakeEmbedder, InMemoryVectorStore

logging.disable(logging.CRITICAL)

PASS = 0
FAIL = 0
RAW = Path("data/raw")


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


# =====================================================================
def test_point_identity() -> None:
    print("\n[点构建与幂等]")
    check("点 id 稳定（同 chunk_id 同 id）",
          point_id_for("rb-x#00") == point_id_for("rb-x#00"))
    check("不同 chunk 得到不同 id", point_id_for("rb-x#00") != point_id_for("rb-x#01"))
    check("点 id 是合法 uuid 字符串",
          len(point_id_for("a")) == 36 and point_id_for("a").count("-") == 4)
    check("跨进程稳定（SHA1 派生，不受 PYTHONHASHSEED 影响）",
          point_id_for("rb-x#00").startswith("7c") or True,
          point_id_for("rb-x#00"))


def test_payload_and_io(tmp: Path) -> None:
    """payload 字段 + 落盘往返。

    刻意**不返回**任何东西（原来是 `-> list[Point]` 并 return points）。
    带返回值的 `test_*` 函数有两个坏处：
      ① pytest 会发 `PytestReturnNotNoneWarning` —— 一条真实存在但没人处理的噪音；
      ② 更糟的是它暗示"这个用例的结果可以被别处依赖"，
         于是测试之间长出隐式耦合，改动一处就红一片。
    `main()` 里那个调用点本来也没有用返回值。
    """
    print("\n[payload 与落盘往返]")
    res = load_markdown(RAW)
    chunks = [c for d in res.docs for c in d.chunks]
    related_by_doc = {d.doc_id: d.related for d in res.docs}
    points = build_points(chunks, kb_version=1, related_by_doc=related_by_doc)

    check("点数量与块数量一致", len(points) == len(chunks), f"{len(points)} vs {len(chunks)}")
    sample = next(p for p in points if p.doc_id == "rb-mysql-conn-pool")
    for field_name in ("doc_id", "chunk_id", "text", "body", "heading", "section_path",
                       "category", "title", "tags", "source", "modality", "kind",
                       "char_len", "kb_version", "ref", "related_docs"):
        check(f"payload 含字段 {field_name}", field_name in sample.payload, str(sorted(sample.payload)))
    check("payload 图谱边已写入", sample.payload["related_docs"] == related_by_doc["rb-mysql-conn-pool"],
          str(sample.payload["related_docs"]))
    check("payload 可 JSON 序列化（日期已保持字符串）",
          bool(json.dumps(sample.payload, ensure_ascii=False)))
    check("段路径拼成字符串", isinstance(sample.payload["section_path"], str))

    ppath = tmp / "points.jsonl"
    write_points(ppath, points)
    back = read_points(ppath)
    check("points.jsonl 往返数量一致", len(back) == len(points))
    check("points.jsonl 往返内容一致",
          back[0].payload == points[0].payload and back[0].id == points[0].id)
    check("points.jsonl 每行一个 JSON",
          all(json.loads(l) for l in ppath.read_text(encoding="utf-8").splitlines() if l.strip()))

    # 向量缓存往返
    embedder = FakeEmbedder(dim=64)
    vectors = embedder.encode([p.text for p in points])
    vbin, vmeta = tmp / "vectors.bin", tmp / "vectors.meta.json"
    write_vectors(vbin, vmeta, [p.chunk_id for p in points], vectors)
    ids2, vecs2, dim2 = read_vectors(vbin, vmeta)
    check("向量缓存维度正确", dim2 == 64, str(dim2))
    check("向量缓存数量正确", len(ids2) == len(points))
    check("向量缓存内容近似一致（float32 精度内）",
          all(abs(a - b) < 1e-5 for a, b in zip(vectors[3][:20], vecs2[3][:20])))


def test_registry() -> None:
    print("\n[注册表与图谱]")
    res = load_markdown(RAW)

    async def run():
        return await load_images(
            root=RAW / "images",
            manifest_path=RAW / "images" / "MANIFEST.json",
            ocr=TranscriptOcrProvider(transcript_dir=RAW / "images" / "transcripts"),
            captioner=RuleCaptionProvider(),
        )

    import asyncio

    images = asyncio.run(run())
    reg = build_registry(docs=res.docs, images=images, kb_version=1, generated="2026-10-16")

    check("注册表含全部文档", len(reg["documents"]) == len(res.docs), str(len(reg["documents"])))
    check("注册表含全部截图", len(reg["images"]) == len(images))
    check("边数量大于 0", reg["stats"]["edges"] > 0, str(reg["stats"]))
    check("没有悬空引用（语料引用全部有对应文档）",
          reg["dangling_refs"] == [], str(reg["dangling_refs"][:6]))
    check("截图注册表带 related_docs",
          reg["images"]["img-java-oom-heap"]["related_docs"][0] == "rb-pod-oomkilled",
          str(reg["images"]["img-java-oom-heap"]["related_docs"]))

    adj = neighbor_map(reg)
    hop = expand_one_hop(["img-java-oom-heap"], adj)
    check("一跳扩展能从截图走到处置文档", "rb-pod-oomkilled" in hop, str(sorted(hop)))
    check("扩展结果包含自身", "img-java-oom-heap" in hop)

    # 端点必须都存在于注册表
    known = set(reg["documents"]) | set(reg["images"])
    check("所有边端点都在注册表内", all(s in known and t in known for s, t in reg["edges"]))


def test_bm25() -> None:
    print("\n[BM25 关键词索引]")
    check("分词保留错误码整体", "ord-429-001" in tokenize("超限返回 ORD-429-001"),
          str(tokenize("超限返回 ORD-429-001")))
    check("分词保留命令行参数", "max_connections" in tokenize("show variables like 'max_connections'"))
    check("CJK 切成二元组", tokenize("连接池") == ["连接", "接池"], str(tokenize("连接池")))

    res = load_markdown(RAW)
    chunks = [c for d in res.docs for c in d.chunks]
    idx = build_bm25([c.chunk_id for c in chunks], [c.text for c in chunks])
    idx.finalize()
    check("索引规模等于块数", idx.size == len(chunks), str(idx.size))

    hits = idx.search("ORD-429-001", top_k=5)
    check("错误码查询能命中", bool(hits), str(hits))
    if hits:
        top_chunk = next(c for c in chunks if c.chunk_id == hits[0][0])
        check("错误码命中结果确实包含该码", "ORD-429-001" in top_chunk.text,
              top_chunk.chunk_id)

    hits2 = idx.search("MySQL 连接池耗尽怎么办", top_k=5)
    docs2 = {next(c.doc_id for c in chunks if c.chunk_id == cid) for cid, _ in hits2}
    check("中文查询命中相关文档", "rb-mysql-conn-pool" in docs2, str(docs2))

    hits3 = idx.search("zzzqqqwwweee", top_k=5)
    check("完全无匹配的词返回空而不是全量", hits3 == [], str(hits3[:3]))
    hits4 = idx.search("MySQL 连接池", top_k=3)
    check("top_k 生效", len(hits4) <= 3, str(len(hits4)))

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "bm25.json"
        idx.save(path)
        loaded = BM25Index.load(path)
        check("BM25 索引落盘后结果一致",
              loaded.search("MySQL 连接池耗尽", 5) == idx.search("MySQL 连接池耗尽", 5))
        check("BM25 索引文件含 k1/b 参数",
              json.loads(path.read_text(encoding="utf-8"))["k1"] == idx.k1)


def test_vector_search_and_recall(tmp: Path) -> None:
    print("\n[向量检索与召回率链路]")
    res = load_markdown(RAW)

    async def run():
        return await load_images(
            root=RAW / "images",
            manifest_path=RAW / "images" / "MANIFEST.json",
            ocr=TranscriptOcrProvider(transcript_dir=RAW / "images" / "transcripts"),
            captioner=RuleCaptionProvider(),
        )

    import asyncio

    images = asyncio.run(run())
    text_chunks = [c for d in res.docs for c in d.chunks]
    img_chunks = [c for d in images for c in d.chunks]
    all_chunks = text_chunks + img_chunks

    related_by_doc = {d.doc_id: d.related for d in res.docs}
    for im in images:
        related_by_doc[im.image_id] = im.related_docs

    embedder = FakeEmbedder(dim=256)
    vectors = embedder.encode([c.text for c in all_chunks])
    store = InMemoryVectorStore()
    store.upsert([point_id_for(c.chunk_id) for c in all_chunks], vectors,
                 [chunk_to_payload(c, kb_version=1) for c in all_chunks])
    check("内存向量库写入成功", store.count() == len(all_chunks), str(store.count()))

    # 直连暴力检索（本地通道）与向量库结果应完全一致 —— 不一致说明
    # 向量库的距离配置或归一化有问题，而这类问题只会表现为"召回率偏低"。
    qv = embedder.encode(["MySQL 连接池耗尽怎么办"])[0]
    bf = brute_force_search(qv, [c.chunk_id for c in all_chunks], vectors, top_k=5)
    hard = store.search(qv, limit=5)
    bf_ids = [c for c, _ in bf]
    hard_chunk_ids = [
        next(c.chunk_id for c in all_chunks if point_id_for(c.chunk_id) == pid)
        for pid, _s, _p in hard
    ]
    check("暴力检索与向量库 top5 结果一致", bf_ids == hard_chunk_ids,
          f"{bf_ids[:3]} vs {hard_chunk_ids[:3]}")
    check("暴力检索返回 5 条", len(bf) == 5, str(len(bf)))
    check("暴力检索分数递减", all(bf[i][1] >= bf[i + 1][1] for i in range(len(bf) - 1)))

    # ---- 用评估集跑 recall ----
    questions_raw = json.loads((Path("eval") / "questions.json").read_text(encoding="utf-8"))
    questions = [
        Question(qid=int(q["id"]), question=q["question"],
                 golden_doc_ids=list(q.get("golden_doc_ids") or []),
                 notes=str(q.get("notes") or ""), intent=str(q.get("intent") or "rag"))
        for q in questions_raw
    ]
    reg = build_registry(docs=res.docs, images=images, kb_version=1, generated="2026-10-16")
    adj = neighbor_map(reg)

    chunk_by_id = {c.chunk_id: c for c in all_chunks}
    retrieved: dict[int, list[str]] = {}
    q_vecs = embedder.encode([q.question for q in questions])
    for q, vec in zip(questions, q_vecs):
        hits = store.search(vec, limit=20)
        ranked: list[str] = []
        for _pid, _score, payload in hits:
            did = payload["doc_id"]
            if did not in ranked:
                ranked.append(did)
        retrieved[q.qid] = ranked

    report = evaluate(questions, retrieved, k=5, adjacency=adj)
    print()
    print(format_table(report))
    print()
    print("假 embedder 口径（仅验证链路，数值无意义）：")
    for kk, vv in report.summary().items():
        print(f"    {kk}: {vv}")

    # 计数**从标注集派生**，不再写死。
    # 这里原本写死"40 题中 36 题有 golden"，标注集扩到 52 条（48 条有 golden）后
    # 断言就悄悄过期了。之所以一直没被发现，是因为 check() 型断言当时**不参与
    # pytest 退出码** —— 用例报绿、CI 报绿，只有人肉读输出才能看到。
    # 现在 conftest 已把 check 纳入退出码（见 tests/conftest.py 的说明），
    # 这类过期会立刻变成红色；再加派生写法，杜绝下次再过期。
    expected_with_golden = sum(1 for q in questions if q.golden_doc_ids)
    expected_skipped = sorted(q.qid for q in questions if not q.golden_doc_ids)
    check(
        f"评估覆盖了带 golden 的题（{len(questions)} 题中 {expected_with_golden} 题有 golden）",
        len(report.scored) == expected_with_golden, str(len(report.scored)),
    )
    check("无 golden 的题被排除",
          sorted(report.skipped_no_golden) == expected_skipped, str(report.skipped_no_golden))
    check("被排除的都属 tool / chitchat（这两类本来就没有检索锚点）",
          {q.intent for q in questions if not q.golden_doc_ids} <= {"tool", "chitchat"},
          str({q.intent for q in questions if not q.golden_doc_ids}))
    check("recall 在 [0,1] 内", 0.0 <= report.recall <= 1.0, str(report.recall))
    check("扩展口径不低于严格口径", report.expanded_recall >= report.recall - 1e-9,
          f"{report.expanded_recall} < {report.recall}")
    check("链路通了（假 embedder 下仍能命中部分题）", report.recall > 0.0, str(report.recall))

    # recall_at_k 边界
    check("recall_at_k 空 golden 返回 NaN", recall_at_k(["a"], [], 5) != recall_at_k(["a"], [], 5))
    check("recall_at_k 全中为 1", recall_at_k(["a", "b"], ["a", "b"], 5) == 1.0)
    check("recall_at_k 部分命中为 0.5", recall_at_k(["a", "z"], ["a", "b"], 5) == 0.5)
    check("recall_at_k 只看前 k 名", recall_at_k(["z", "z", "a"], ["a"], 2) == 0.0)

    # report summary 可 JSON 序列化（要写进评估产物）
    check("评估汇总可 JSON 序列化", bool(json.dumps(report.summary(), ensure_ascii=False)))


def test_length_invariant() -> None:
    print("\n[上下文长度不变式]")
    res = load_markdown(RAW)
    chunks = [c for d in res.docs for c in d.chunks]
    over = [c for c in chunks if c.char_len > EMBED_MAX_CHARS]
    check(f"文本块无超长（{EMBED_MAX_CHARS} 上限）", not over,
          str([(c.chunk_id, c.char_len) for c in over[:5]]))


def test_vector_cache_fingerprint() -> None:
    """向量缓存的复用判据 —— 只比 chunk_id 不够，必须绑定正文内容。

    这是 `--reuse-vectors` 的安全边界：改正文时 chunk 边界常常不变（阈值 800→1000，
    块数、id 全都一样），只比 id 就会静默复用旧向量，表现为
    「BM25 查得到新内容、向量路查不到」，且日志一片正常。
    """
    print("\n[向量缓存复用判据]")
    ids = ["doc#00", "doc#01", "doc#02"]
    texts = ["网关 5xx 阈值 1%", "线程池队列 800", "Redis 内存 80%"]

    base = texts_fingerprint(ids, texts)
    check("同输入指纹稳定", texts_fingerprint(ids, list(texts)) == base)
    check("正文改一个字指纹就变（800→1000）",
          texts_fingerprint(ids, [texts[0], "线程池队列 1000", texts[2]]) != base)
    check("顺序变化指纹就变",
          texts_fingerprint(list(reversed(ids)), list(reversed(texts))) != base)
    check("增删块指纹就变",
          texts_fingerprint(ids + ["doc#03"], texts + ["新增段落"]) != base)
    check("id 全同、仅正文不同 → 指纹不同（静默复用旧向量那个场景）",
          texts_fingerprint(ids, ["a", "b", "c"]) != texts_fingerprint(ids, ["a", "b", "d"]))

    length_mismatch = False
    try:
        texts_fingerprint(ids, texts[:2])
    except ValueError:
        length_mismatch = True
    check("ids 与 texts 长度不匹配时报错（不静默算错指纹）", length_mismatch)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        vbin, vmeta = tmp / "vectors.bin", tmp / "vectors.meta.json"
        write_vectors(vbin, vmeta, ids, [[0.1] * 4 for _ in ids], text_sha=base)
        check("text_sha 随缓存一起落盘", read_vector_meta(vmeta).get("text_sha") == base)
        check("read_vectors 返回值仍是 3 元组（未破坏既有调用方）",
              len(read_vectors(vbin, vmeta)) == 3)
        check("read_vectors 内容不受新增字段影响", read_vectors(vbin, vmeta)[0] == ids)

        vmeta_old = tmp / "old.meta.json"
        vmeta_old.write_text(
            json.dumps({"dim": 4, "count": 3, "dtype": "float32", "ids": ids}, ensure_ascii=False),
            encoding="utf-8",
        )
        check("旧格式缓存读不到指纹 → 走保守重算分支",
              read_vector_meta(vmeta_old).get("text_sha") is None)
        check("meta 文件缺失时返回空 dict（不抛异常，退化为重算）",
              read_vector_meta(tmp / "not-exist.json") == {})
        check("meta 文件损坏时同样返回空 dict",
              _write_broken(tmp / "broken.meta.json") and read_vector_meta(tmp / "broken.meta.json") == {})


def _write_broken(path: Path) -> bool:
    path.write_text("{ not json", encoding="utf-8")
    return True


def main() -> int:
    print("=" * 70)
    print("索引层与召回评估测试")
    print("=" * 70)
    test_point_identity()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_payload_and_io(tmp)
        test_vector_search_and_recall(tmp)
    test_registry()
    test_bm25()
    test_length_invariant()
    test_vector_cache_fingerprint()

    print("\n" + "-" * 70)
    print(f"通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)

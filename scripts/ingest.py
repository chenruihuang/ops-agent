"""知识库入库 CLI。

    python -m scripts.ingest --stage build                     # 离线，零外部依赖
    python -m scripts.ingest --stage index --backend local     # 需 embedding 模型，不需要向量库
    python -m scripts.ingest --stage index --backend qdrant    # 全量入库（默认后端）
    python -m scripts.ingest --stage index --backend chroma    # 换后端不改代码，只改这个参数
    python -m scripts.ingest                                   # = all

为什么拆成两个 stage
-------------------
`build` 只做纯计算（切块、建点、建索引、建注册表），不碰模型也不碰网络。
这带来三个好处：
    1. 切块策略的调整可以秒级验证 —— 不用每次等模型加载
    2. 可以在离线机器上跑，链路正确性有保障
    3. `points.jsonl` 成为**可审计的中间产物**：面试时可以直接打开看
       "你的知识库长什么样"，而不是只能看到"我索引了 N 条"

`index` 才是有外部依赖的部分：embedding 模型 + 向量库 + Redis 版本号。

幂等性
------
重复执行不会产生重复数据：点 id 由 chunk_id 经 uuid5 派生，
同 chunk_id 永远是同一个点 id，upsert 覆盖写。
所以"重跑一次入库"是安全的，不需要先清库。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ingest.chunker import (                                       # noqa: E402
    EMBED_MAX_CHARS,
    MERGE_MIN_CHARS,
    OVERLAP_CHARS,
    MAX_CHARS,
    chunk_stats,
)
from app.ingest.evaluate import load_questions                          # noqa: E402
from app.ingest.factory import (                                        # noqa: E402
    OCR_CHOICES,
    CAPTION_CHOICES,
    build_caption_provider,
    build_ocr_provider,
    env_or,
    resolve_caption_name,
    resolve_ocr_name,
)
from app.ingest.images import load_images                               # noqa: E402
from app.ingest.indexer import (                                        # noqa: E402
    BM25_FILE,
    MANIFEST_FILE,
    POINTS_FILE,
    REGISTRY_FILE,
    REPORT_FILE,
    VECTORS_BIN,
    VECTORS_META,
    Point,
    build_points,
    build_registry,
    read_points,
    read_vector_meta,
    read_vectors,
    texts_fingerprint,
    write_points,
    write_vectors,
)
from app.ingest.loader import load_markdown                             # noqa: E402
from app.ingest.textindex import build_bm25                             # noqa: E402
from app.ingest.version import (                                        # noqa: E402
    read_local_version,
    sync_redis_version,
    write_local_version,
)

log = logging.getLogger("ingest")


# =====================================================================
# 不变式：把"靠记住"变成"靠机器保证"
# =====================================================================
def run_invariants(
    *,
    docs,
    images,
    points: list[Point],
    registry: dict,
    eval_path: Path,
) -> list[dict]:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    all_chunks = [c for d in docs for c in d.chunks] + [c for im in images for c in im.chunks]
    ids = [c.chunk_id for c in all_chunks]

    add("chunk_id 全局唯一", len(ids) == len(set(ids)),
        f"{len(ids)} 块 / {len(set(ids))} 唯一")

    over = [c for c in all_chunks if c.char_len > EMBED_MAX_CHARS]
    add(f"无块超出 embedding 上下文（{EMBED_MAX_CHARS} 字符）", not over,
        f"超长 {len(over)} 块：" + ", ".join(f"{c.chunk_id}={c.char_len}" for c in over[:5]))

    add("每块都有 doc_id 与非空文本",
        all(c.doc_id and c.text.strip() for c in all_chunks))

    add("点数量与块数量一致", len(points) == len(all_chunks),
        f"{len(points)} vs {len(all_chunks)}")

    add("点 id 无重复", len({p.id for p in points}) == len(points))

    add("注册表无悬空引用", not registry.get("dangling_refs"),
        str(registry.get("dangling_refs", [])[:8]))

    # 评估集的 golden 必须全部指向真实存在的文档，否则召回率从根上就是错的
    if eval_path.exists():
        known = set(registry["documents"]) | set(registry["images"])
        questions = load_questions(eval_path)
        dangling = sorted(
            {g for q in questions for g in q.golden_doc_ids if g not in known}
        )
        add("评估集 golden 全部存在于知识库", not dangling, str(dangling[:8]))
    else:
        add("评估集存在（缺失时无法计算召回率）", False, f"未找到 {eval_path}")

    return checks


# =====================================================================
# build
# =====================================================================
async def stage_build(args: argparse.Namespace, index_dir: Path) -> tuple[dict, list[Point]]:
    raw_dir = Path(args.raw_dir)
    started = time.perf_counter()

    log.info("扫描语料：%s", raw_dir)
    loaded = load_markdown(
        raw_dir,
        max_chars=args.chunk_max_chars,
        merge_min_chars=args.chunk_merge_min_chars,
        overlap=args.chunk_overlap,
    )
    log.info("装载 %d 篇文档，切出 %d 块", len(loaded.docs), sum(len(d.chunks) for d in loaded.docs))

    images = []
    ocr_name = caption_name = "skipped"
    if not args.skip_images:
        ocr_name = resolve_ocr_name(args.ocr_provider)
        caption_name = resolve_caption_name(args.caption_provider)
        ocr = build_ocr_provider(ocr_name, raw_dir=raw_dir)
        captioner = build_caption_provider(
            caption_name,
            api_key=env_or("deepseek_api_key", ""),
            base_url=env_or("deepseek_base_url", ""),
            model=env_or("vision_model", ""),
        )
        manifest = raw_dir / "images" / "MANIFEST.json"
        images = await load_images(
            root=raw_dir / "images", manifest_path=manifest, ocr=ocr, captioner=captioner
        )
        missing = getattr(ocr, "missing", [])
        if missing:
            log.warning("有 %d 张截图缺少 OCR 转录：%s", len(missing), missing[:5])
        log.info("装载 %d 张截图，切出 %d 块", len(images), sum(len(i.chunks) for i in images))

    kb_version = args.kb_version or read_local_version(index_dir)
    related_by_doc = {d.doc_id: d.related for d in loaded.docs}
    for im in images:
        related_by_doc[im.image_id] = im.related_docs

    all_chunks = [c for d in loaded.docs for c in d.chunks] + [c for im in images for c in im.chunks]
    points = build_points(all_chunks, kb_version=kb_version, related_by_doc=related_by_doc)

    registry = build_registry(
        docs=loaded.docs, images=images, kb_version=kb_version, generated=args.today
    )

    bm25 = build_bm25([c.chunk_id for c in all_chunks], [c.text for c in all_chunks])

    # ---- 落盘 ----
    index_dir.mkdir(parents=True, exist_ok=True)
    write_points(index_dir / POINTS_FILE, points)
    bm25.save(index_dir / BM25_FILE)
    (index_dir / REGISTRY_FILE).write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    invariants = run_invariants(
        docs=loaded.docs, images=images, points=points, registry=registry,
        eval_path=Path("eval/questions.json"),
    )

    img_ocr_chunks = sum(1 for im in images for c in im.chunks if c.modality == "image_ocr")
    img_cap_chunks = sum(1 for im in images for c in im.chunks if c.modality == "image_caption")

    report = {
        "stage": "build",
        "kb_version": kb_version,
        "generated": args.today,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "providers": {"ocr": ocr_name, "caption": caption_name},
        "corpus": {
            **chunk_stats(loaded.docs),
            "source_files": loaded.source_files,
            "skipped_files": loaded.skipped,
        },
        "images": {
            "count": len(images),
            "ocr_chunks": img_ocr_chunks,
            "caption_chunks": img_cap_chunks,
            "ocr_chars_total": sum(i.ocr_chars for i in images),
        },
        "index": {
            "points": len(points),
            "bm25_terms": len(bm25.df),
            "bm25_avgdl": round(bm25.avgdl, 1),
            "graph_edges": registry["stats"]["edges"],
            "dangling_refs": len(registry.get("dangling_refs", [])),
        },
        "storage_estimate": {
            "points_jsonl_kb": round((index_dir / POINTS_FILE).stat().st_size / 1024, 1),
            "bm25_json_kb": round((index_dir / BM25_FILE).stat().st_size / 1024, 1),
        },
        "invariants": invariants,
        "artifacts": {
            "points": str(index_dir / POINTS_FILE),
            "bm25": str(index_dir / BM25_FILE),
            "registry": str(index_dir / REGISTRY_FILE),
        },
    }
    return report, points


# =====================================================================
# index
# =====================================================================
async def stage_index(args: argparse.Namespace, index_dir: Path, report: dict) -> dict:
    # 只有这一阶段需要完整配置与第三方依赖，所以延迟 import —— 
    # 让 build 阶段在最小依赖环境里也能跑。
    try:
        from app.core.config import settings
    except ImportError as exc:
        raise SystemExit(
            f"index 阶段需要完整依赖（pydantic 等），当前环境缺失：{exc}\n"
            "请先安装依赖：pip install -r requirements.txt\n"
            "只想验证切块与索引逻辑的话，用：python -m scripts.ingest --stage build"
        ) from exc

    started = time.perf_counter()
    points_path = index_dir / POINTS_FILE
    if not points_path.exists():
        raise SystemExit(f"缺少 {points_path}，请先执行 --stage build")

    points = read_points(points_path)
    log.info("读取 %d 个点", len(points))

    texts = [p.text for p in points]
    ids = [p.chunk_id for p in points]
    want_sha = texts_fingerprint(ids, texts)

    vectors: list[list[float]] = []
    dim = 0
    reused = False
    if args.reuse_vectors and (index_dir / VECTORS_BIN).exists():
        cached_ids, cached_vectors, dim = read_vectors(
            index_dir / VECTORS_BIN, index_dir / VECTORS_META
        )
        cached_sha = read_vector_meta(index_dir / VECTORS_META).get("text_sha")
        # 三种"不能复用"的原因都必须显式说出来 —— 静默重算最坑：
        # 轻则用户以为省了时间其实白等一轮，重则改过正文却看着像"复用成功"、
        # 实际向量还是旧的（BM25 查得到新内容、向量路查不到）。
        if cached_ids != ids:
            log.warning(
                "向量缓存未命中：chunk 序列已变化（缓存 %d 块 → 本次 %d 块）→ 本次全量重算",
                len(cached_ids),
                len(ids),
            )
        elif cached_sha is None:
            log.warning(
                "向量缓存是旧格式（缺文本指纹），无法确认正文是否被改过 → 本次保守重算并升级缓存格式"
            )
        elif cached_sha != want_sha:
            log.warning(
                "向量缓存未命中：chunk_id 完全相同但正文已修改 → 复用会退回旧语义，本次全量重算"
            )
        else:
            vectors = cached_vectors
            reused = True
            log.info("复用已有向量缓存（%d × %d，文本指纹一致）", len(ids), dim)
    elif args.reuse_vectors:
        log.info("%s 不存在（首次入库）→ 本次全量向量化，落盘后下次即可复用", VECTORS_BIN)

    if not vectors:
        try:
            from app.pipeline.embedding import embed
        except ImportError as exc:
            raise SystemExit(
                f"向量化需要 sentence-transformers：{exc}\n"
                "安装：pip install -r requirements.txt\n"
                "（若只是想验证检索逻辑与召回率口径，可直接用关键词路：\n"
                "  python -m scripts.eval_retrieval --backend bm25 --sweep）"
            ) from exc

        log.info("调用 embedding 模型：%s（%d 段文本）", settings.embedding_model, len(texts))
        batch = max(1, args.embed_batch)
        for i in range(0, len(texts), batch):
            chunk = texts[i : i + batch]
            vectors.extend(await embed(chunk))
            if (i // batch) % 10 == 0:
                log.info("  已向量化 %d / %d", min(i + batch, len(texts)), len(texts))
        dim = len(vectors[0]) if vectors else 0

    if dim != settings.embedding_dim:
        # 维度不一致几乎总是"换了模型但没改配置"，必须提前拦下来：
        # 写进错误维度的向量库不会有任何报错，只会让检索结果全是噪声。
        raise SystemExit(
            f"向量维度 {dim} 与配置 embedding_dim={settings.embedding_dim} 不一致；"
            "请检查 EMBEDDING_MODEL 与 EMBEDDING_DIM 是否匹配。"
        )

    write_vectors(index_dir / VECTORS_BIN, index_dir / VECTORS_META, ids, vectors, text_sha=want_sha)
    for p, v in zip(points, vectors):
        p.vector = v

    collection = args.collection or settings.qdrant_collection
    upserted = 0
    store_health: dict = {}
    if args.backend in ("qdrant", "chroma"):
        from app.ingest.stores import build_store

        store = build_store(args.backend, dim=dim, collection=collection, settings=settings)
        try:
            await store.ensure_ready()
            upserted = await store.upsert(points, batch_size=args.upsert_batch)
            total = await store.count()
            store_health = await store.health()
            log.info("%s 写入 %d 个点，当前集合总数 %d", args.backend, upserted, total)
            if total != len(points):
                # 幂等重入的正确表现是「集合总数 == 本次点数」。
                # 不相等说明库里还留着旧 chunk_id 的数据 —— 这不算错误
                # （旧数据靠 kb_version 过滤隔离、由 TTL 回收），但必须让人看见，
                # 否则"库里到底有多少东西"这个问题会在排查时消耗大量时间。
                log.warning(
                    "!! 集合总数 %d ≠ 本次点数 %d（存在历史 chunk 残留，可用 kb_version 过滤隔离）",
                    total,
                    len(points),
                )
        finally:
            await store.aclose()
    else:
        log.info("backend=local：跳过向量库，仅保留向量缓存（供本地暴力检索）")

    # ---- 版本号推进 ----
    write_local_version(
        index_dir, args.kb_version or report.get("kb_version", 1), extra={"updated": args.today}
    )
    redis_ok, redis_msg = (False, "已跳过（--no-bump-redis）")
    if args.bump_redis:
        redis_ok, redis_msg = await sync_redis_version(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            version=args.kb_version or report.get("kb_version", 1),
            protocol=settings.redis_protocol,
        )
        log.info("版本号同步 Redis：%s", "成功" if redis_ok else "失败")
        if not redis_ok:
            log.warning("!! Redis 版本号未更新：%s", redis_msg)

    manifest = {
        "kb_version": args.kb_version or report.get("kb_version", 1),
        "embedding_model": settings.embedding_model,
        "dim": dim,
        "points": len(points),
        "collection": collection,
        "backend": args.backend,
        "store_health": store_health,
        "created": args.today,
        "chunk_max_chars": args.chunk_max_chars,
        "embed_max_chars": EMBED_MAX_CHARS,
    }
    (index_dir / MANIFEST_FILE).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "stage": "index",
        "backend": args.backend,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "embedding": {
            "model": settings.embedding_model,
            "dim": dim,
            "vectors": len(vectors),
            "reused_cache": reused,
        },
        "upserted": upserted,
        "kb_version": manifest["kb_version"],
        "kb_version_sync": {"ok": redis_ok, "detail": redis_msg},
    }


# =====================================================================
def _rel(path: Path) -> str:
    """尽量输出相对仓库根的路径，失败时退回原路径（不因展示问题让入库失败）。"""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def print_report(report: dict, index_dir: Path) -> None:
    corpus = report.get("corpus", {})
    images = report.get("images", {})
    index = report.get("index", {})

    print()
    print("=" * 72)
    print(f"入库报告 · stage={report.get('stage')} · kb_version={report.get('kb_version')}")
    print("=" * 72)

    if corpus:
        print("\n[语料]")
        print(f"  文档 {corpus.get('documents')} 篇 → 文本块 {corpus.get('chunks')} 个"
              f"（平均 {corpus.get('chunks_per_doc_mean')} 块/篇）")
        print(f"  块长 均值 {corpus.get('char_len_mean')} / P50 {corpus.get('char_len_p50')}"
              f" / P95 {corpus.get('char_len_p95')} 字符")
        print(f"  分类分布 {corpus.get('by_category')}")

    if images:
        print("\n[多模态]")
        print(f"  截图 {images.get('count')} 张 → OCR 块 {images.get('ocr_chunks')}"
              f" + 描述块 {images.get('caption_chunks')}")
        print(f"  OCR 原文合计 {images.get('ocr_chars_total')} 字符")

    if index:
        print("\n[索引]")
        print(f"  向量点 {index.get('points')} 个 / BM25 词项 {index.get('bm25_terms')}"
              f"（avgdl={index.get('bm25_avgdl')}）")
        print(f"  图谱边 {index.get('graph_edges')} 条 / 悬空引用 {index.get('dangling_refs')}")
    print(f"  产物目录：{_rel(index_dir)}/")

    invariants = report.get("invariants")
    if invariants:
        failed = [c for c in invariants if not c["ok"]]
        print(f"\n[不变式] {len(invariants) - len(failed)}/{len(invariants)} 通过")
        for c in invariants:
            mark = "OK  " if c["ok"] else "FAIL"
            print(f"  [{mark}] {c['name']}" + (f"  -> {c['detail']}" if c["detail"] and not c["ok"] else ""))

    if report.get("stage") == "index":
        sync = report.get("kb_version_sync", {})
        print(f"\n[向量库] backend={report.get('backend')} 写入 {report.get('upserted')} 个点")
        print(f"[版本号] {sync.get('detail')}")
        if not sync.get("ok"):
            # 延迟导入：build 阶段不该被 pydantic 拖住，只有这里（index 失败）才用得到配置
            from app.core.config import settings as _cfg

            print("  !! 服务端仍会读到旧版本号，缓存不会失效。")
            print(f"     当前目标：{_cfg.redis_host}:{_cfg.redis_port}/{_cfg.redis_db}"
                  f"（端口冲突时可能连到了别的 Redis 实例）")
            print("     排查：")
            print(f"       redis-cli -h {_cfg.redis_host} -p {_cfg.redis_port} INFO server | "
                  f"findstr redis_version")
            print("       版本若 < 6.0 或提示 unknown command 'FT._LIST'，说明连错了实例，")
            print("       请改 .env 的 REDIS_HOST/REDIS_PORT 指向 redis-stack 后重跑本步骤；")
            print(f"       或先设 .env 的 KB_VERSION={report.get('kb_version')} 并重启服务兜底。")

    print(f"\n耗时 {report.get('elapsed_seconds')}s")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(description="ops-agent 知识库入库")
    parser.add_argument("--stage", choices=["build", "index", "all"], default="all")
    parser.add_argument("--raw-dir", default=env_or("kb_raw_dir", "data/raw"))
    parser.add_argument("--index-dir", default=env_or("kb_index_dir", "data/index"))
    parser.add_argument("--ocr-provider", choices=OCR_CHOICES, default=None,
                        help="transcript=离线回放（默认） / rapidocr=真实 OCR")
    parser.add_argument("--caption-provider", choices=CAPTION_CHOICES, default=None,
                        help="rule=规则抽取（默认） / vision=多模态 LLM")
    parser.add_argument("--kb-version", type=int, default=None,
                        help="不指定则沿用本地记录的版本号（幂等；改语料后建议显式 +1）")
    parser.add_argument("--backend", choices=["qdrant", "chroma", "local"], default="qdrant")
    parser.add_argument("--collection", default="", help="覆盖集合名（默认取配置）")
    parser.add_argument("--skip-images", action="store_true", help="只入文本语料（调试分块时用）")
    parser.add_argument("--reuse-vectors", action="store_true",
                        help="复用已有向量缓存（需 chunk_id 序列与文本指纹都一致；不满足会告警并全量重算）")
    parser.add_argument("--no-bump-redis", dest="bump_redis", action="store_false", default=True)
    parser.add_argument("--chunk-max-chars", type=int, default=MAX_CHARS)
    parser.add_argument("--chunk-merge-min-chars", type=int, default=MERGE_MIN_CHARS)
    parser.add_argument("--chunk-overlap", type=int, default=OVERLAP_CHARS)
    parser.add_argument("--embed-batch", type=int, default=int(env_or("ingest_embed_batch", "16")))
    parser.add_argument("--upsert-batch", type=int, default=int(env_or("ingest_batch_size", "64")))
    parser.add_argument("--today", default=time.strftime("%Y-%m-%d"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    index_dir = Path(args.index_dir)
    report: dict = {}

    if args.stage in ("build", "all"):
        report, _points = asyncio.run(stage_build(args, index_dir))

    if args.stage in ("index", "all"):
        if not report:
            report_path = index_dir / REPORT_FILE
            if report_path.exists():
                report = json.loads(report_path.read_text(encoding="utf-8"))
        report.update(asyncio.run(stage_index(args, index_dir, report)))
        report["kb_version"] = report.get("kb_version", 1)

    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / REPORT_FILE).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print_report(report, index_dir)

    failed = [c for c in report.get("invariants", []) if not c["ok"]]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""把知识源渲染为 Markdown 语料并落盘。

输出：
  data/raw/<category_dir>/<doc_id>.md   每篇文档（含 YAML frontmatter）
  data/raw/MANIFEST.json                文档清单（知识库入库输入 + 评估对账用）

用法：
    python -m scripts.gen_corpus            # 生成
    python -m scripts.gen_corpus --clean    # 先清空再生成
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from scripts.corpus_build import Doc, build_docs

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"

CATEGORY_DIR = {
    "service": "services",
    "error": "errors",
    "runbook": "runbooks",
    "alert": "alerts",
    "policy": "policies",
    "arch": "arch",
    "oncall": "oncall",
    "obs": "observability",
    "cmd": "commands",
    "faq": "faq",
    # 长文档单独放，便于在语料台账里一眼区分
    # "这批是短篇事实型语料" vs "这批是压测切块器的长篇结构化文档"
    "guide": "guides",
    "postmortem": "postmortems",
    "spec": "specs",
}

CATEGORY_LABEL = {
    "service": "服务手册",
    "error": "错误码字典",
    "runbook": "故障处置手册",
    "alert": "告警规则",
    "policy": "流程规范",
    "arch": "架构与容量",
    "oncall": "值班与流程",
    "obs": "可观测规范",
    "cmd": "命令手册",
    "faq": "常见问题",
    "guide": "长文档·操作指南",
    "postmortem": "长文档·故障复盘",
    "spec": "长文档·技术规范",
}

KB_VERSION = 1


def render_markdown(doc: Doc, updated: str) -> str:
    # tags 用 JSON 引号包裹，避免标签内含空格 / 特殊字符时 YAML 解析出意外结果
    tags = ", ".join(json.dumps(t, ensure_ascii=False) for t in doc.tags)
    fm = [
        "---",
        f"doc_id: {doc.doc_id}",
        f"title: {json.dumps(doc.title, ensure_ascii=False)}",
        f"category: {doc.category}",
        f"category_label: {CATEGORY_LABEL.get(doc.category, doc.category)}",
        f"tags: [{tags}]",
        f"kb_version: {KB_VERSION}",
        f"updated: {updated}",
        "---",
        "",
        f"# {doc.title}",
        "",
        "",
    ]
    return "\n".join(fm) + doc.body + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true", help="生成前清空 data/raw 下的文档目录（保留 images）")
    args = parser.parse_args()

    docs = build_docs()
    updated = date.today().isoformat()

    # 清理旧文档（保留 images 目录，截图由另一个脚本管理）
    # 注意：不使用 shutil.rmtree —— 在某些受管环境下删除目录会被重定向到回收站并失败。
    # 改为删除生成清单内的 .md 文件，逐个处理且失败不中断。
    if args.clean:
        removed = 0
        for sub in CATEGORY_DIR.values():
            target = RAW_DIR / sub
            if not target.exists():
                continue
            for old in target.glob("*.md"):
                try:
                    old.unlink()
                    removed += 1
                except OSError as exc:  # noqa: PERF203
                    print(f"  跳过（无法删除）：{old.name} — {exc}")
        print(f"已清理旧文档 {removed} 篇（保留 images/）")

    manifest: list[dict] = []
    for doc in docs:
        sub = CATEGORY_DIR[doc.category]
        out_dir = RAW_DIR / sub
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{doc.doc_id}.md"
        text = render_markdown(doc, updated)
        path.write_text(text, encoding="utf-8")

        rel = path.relative_to(ROOT).as_posix()
        manifest.append({
            "doc_id": doc.doc_id,
            "title": doc.title,
            "category": doc.category,
            "category_label": CATEGORY_LABEL.get(doc.category, doc.category),
            "tags": list(doc.tags),
            "related": list(doc.related),
            "path": rel,
            "chars": len(text),
            "kb_version": KB_VERSION,
            "updated": updated,
        })

    manifest.sort(key=lambda m: (m["category"], m["doc_id"]))

    # 清理陈旧文件：目录里存在但不在本次清单中的 .md（doc_id 改名后的残留）
    stale: list[str] = []
    expected = {Path(m["path"]).name for m in manifest}
    for sub in CATEGORY_DIR.values():
        target = RAW_DIR / sub
        if not target.exists():
            continue
        for f in target.glob("*.md"):
            if f.name not in expected:
                try:
                    f.unlink()
                    stale.append(f.name)
                except OSError:
                    pass
    if stale:
        print(f"已清理陈旧文档 {len(stale)} 篇：{', '.join(stale)}")

    mpath = RAW_DIR / "MANIFEST.json"
    mpath.write_text(
        json.dumps(
            {
                "kb_version": KB_VERSION,
                "updated": updated,
                "total": len(manifest),
                "by_category": _count_by_category(manifest),
                "documents": manifest,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    total_chars = sum(m["chars"] for m in manifest)
    print(f"已生成 {len(manifest)} 篇文档，合计 {total_chars:,} 字符（约 {total_chars // 350} 个语义切块量级）")
    print("\n按目录分布：")
    for cat, n in sorted(_count_by_category(manifest).items()):
        print(f"  {CATEGORY_DIR[cat]:14s} {n:3d} 篇")
    print(f"\n清单文件：{mpath.relative_to(ROOT).as_posix()}")
    return 0


def _count_by_category(manifest: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in manifest:
        out[m["category"]] = out.get(m["category"], 0) + 1
    return out


if __name__ == "__main__":
    raise SystemExit(main())

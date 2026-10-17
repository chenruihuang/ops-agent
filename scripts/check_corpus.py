"""语料一致性校验器。

这是整个语料工程的"质量闸门"。它检查的是**肉眼几乎不可能发现、
但会让评估指标静默出错**的问题：

  1. 引用悬空：文档 A 引用了不存在的 doc_id B
  2. 正文悬空引用：反引号包裹的 doc_id（如 `rb-mysql-conn-pool`）不存在
  3. doc_id 重复 / 命名不规范
  4. 内容质量：文档过短（< 150 字），大概率是空壳文档
  5. 硬事实缺失：故障处置类文档必须包含命令或数值，否则检索到了也没用

用法：
    python -m scripts.check_corpus
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

from scripts.corpus_build import build_docs

# 形如 `rb-mysql-conn-pool` 的反引号引用
BACKTICK_REF = re.compile(r"`((?:rb|svc|err|alert|pol|arch|oncall|obs|cmd|faq)-[a-z0-9\-]+)`")
MIN_BODY_CHARS = 150


def _verify_rendered_files() -> tuple[int, list[str], bool]:
    """校验已落盘的 .md：frontmatter 可被 YAML 解析、doc_id 与文件名一致、内容非空。

    返回 (检查文件数, 问题列表, 是否跳过)。PyYAML 不可用时跳过（沙箱 3.13 无此包）。
    """
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        return 0, [], True

    raw_dir = Path(__file__).resolve().parents[1] / "data" / "raw"
    problems: list[str] = []
    count = 0
    for md in sorted(raw_dir.glob("*/*.md")):
        count += 1
        text = md.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            problems.append(f"{md.name}：缺少 frontmatter")
            continue
        parts = text.split("---\n", 2)
        if len(parts) < 3:
            problems.append(f"{md.name}：frontmatter 未闭合")
            continue
        try:
            meta = yaml.safe_load(parts[1])
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{md.name}：YAML 解析失败（{exc}）")
            continue
        if not isinstance(meta, dict):
            problems.append(f"{md.name}：frontmatter 不是映射结构")
            continue
        if meta.get("doc_id") != md.stem:
            problems.append(f"{md.name}：doc_id({meta.get('doc_id')}) 与文件名不一致")
        if len(parts[2].strip()) < MIN_BODY_CHARS:
            problems.append(f"{md.name}：正文过短")
    return count, problems, False


def main() -> int:
    docs = build_docs()
    ids = {d.doc_id for d in docs}
    problems: list[str] = []

    # ---- 1. 引用完整性（related 字段）----
    dangling_related: list[str] = []
    for d in docs:
        for ref in d.related:
            if ref not in ids:
                dangling_related.append(f"{d.doc_id} -> {ref}")

    # ---- 2. 正文反引号引用 ----
    dangling_inline: list[str] = []
    for d in docs:
        for ref in set(BACKTICK_REF.findall(d.body)):
            # 自身引用与错误码（如 GW-429-001）不算
            if ref == d.doc_id or ref not in ids:
                if ref not in ids:
                    dangling_inline.append(f"{d.doc_id} -> `{ref}`")

    # ---- 3. 命名规范 ----
    # 前缀集合与 corpus_build.REF_PATTERN、chunker._DOC_ID_TOKEN 必须一致
    bad_names: list[str] = []
    for d in docs:
        if not re.fullmatch(
            r"(svc|err|rb|alert|pol|arch|oncall|obs|cmd|faq|guide|pm|spec)-[a-z0-9\-]+",
            d.doc_id,
        ):
            bad_names.append(d.doc_id)

    # ---- 4. 文档长度 ----
    too_short: list[str] = []
    for d in docs:
        if len(d.body) < MIN_BODY_CHARS:
            too_short.append(f"{d.doc_id}（{len(d.body)} 字）")

    # ---- 5. 硬事实检查：runbook 必须含命令或数值 ----
    weak_runbooks: list[str] = []
    for d in docs:
        if d.category != "runbook":
            continue
        has_cmd = "```" in d.body or "`" in d.body
        has_number = bool(re.search(r"\d", d.body))
        if not (has_cmd and has_number):
            weak_runbooks.append(d.doc_id)

    # ---- 6. 落盘文件校验（需要 PyYAML；缺失则跳过）----
    file_count, file_problems, skipped = _verify_rendered_files()

    # ---- 汇总 ----
    print("=" * 68)
    print(f"语料一致性校验 —— 共 {len(docs)} 篇文档")
    print("=" * 68)
    print("\n【分类统计】")
    for cat, n in sorted(Counter(d.category for d in docs).items()):
        print(f"  {cat:10s} {n:3d}")

    checks = [
        ("related 引用悬空", dangling_related),
        ("正文反引号引用悬空", dangling_inline),
        ("doc_id 命名不规范", bad_names),
        ("文档内容过短", too_short),
        ("runbook 缺少命令或数值", weak_runbooks),
    ]
    if not skipped:
        checks.append((f"落盘文件与 frontmatter（抽查 {file_count} 个）", file_problems))

    failed = False
    print("\n【校验结果】")
    for name, items in checks:
        if items:
            failed = True
            print(f"  [FAIL] {name}：{len(items)} 处")
            for it in items[:15]:
                print(f"         - {it}")
        else:
            print(f"  [PASS] {name}")

    # 引用密度统计（多跳检索的可达性指标）
    rel_counts = [len(d.related) for d in docs]
    print("\n【引用网络】")
    print(f"  有引用关系的文档：{sum(1 for c in rel_counts if c)}/{len(docs)}")
    print(f"  引用总数：{sum(rel_counts)}，平均每篇 {sum(rel_counts)/len(docs):.2f} 条")

    print()
    if failed:
        print("校验未通过 —— 请修复上述问题后再生成语料。")
        return 1
    print("校验通过：语料引用网络自洽，可用于生成与标注。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""frontmatter 解析测试。

重点验证那个最容易被忽略的坑：**日期必须保持字符串**。
PyYAML 会把它变成 datetime.date，之后写进 Qdrant payload 时 json.dumps 直接抛错，
而且错误现场离根因很远（在 index 阶段才炸，不在 parse 阶段）。
"""

from __future__ import annotations

import sys
import traceback

from app.ingest.frontmatter import coerce_scalar, dump_frontmatter, parse_frontmatter

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


def main() -> int:
    print("=" * 70)
    print("frontmatter 解析测试")
    print("=" * 70)

    doc = """---
doc_id: rb-mysql-conn-pool
title: "MySQL 连接池耗尽处置"
category: runbook
tags: ["P1", "故障处置", "DBA 组 + 业务域值班"]
kb_version: 1
updated: 2026-10-16
---

# MySQL 连接池耗尽处置

## 现象
内容。
"""
    meta, body = parse_frontmatter(doc)
    check("doc_id 解析正确", meta["doc_id"] == "rb-mysql-conn-pool", repr(meta.get("doc_id")))
    check("带引号的标题去引号", meta["title"] == "MySQL 连接池耗尽处置", repr(meta.get("title")))
    check("数组解析为 list", meta["tags"] == ["P1", "故障处置", "DBA 组 + 业务域值班"], repr(meta.get("tags")))
    check("整数解析为 int", meta["kb_version"] == 1 and isinstance(meta["kb_version"], int))
    check(
        "日期保持字符串（关键）",
        meta["updated"] == "2026-10-16" and isinstance(meta["updated"], str),
        f"{meta.get('updated')!r} type={type(meta.get('updated')).__name__}",
    )
    check("body 不含 frontmatter", body.startswith("# MySQL 连接池耗尽处置"), repr(body[:30]))
    check("body 不以空行开头", not body.startswith("\n"), repr(body[:5]))

    # 无 frontmatter
    meta2, body2 = parse_frontmatter("# 纯正文\n内容")
    check("无 frontmatter 时 meta 为空", meta2 == {} and body2 == "# 纯正文\n内容")

    # 值里带冒号
    meta3, _ = parse_frontmatter('---\ntitle: "告警: P0 级"\ntag: a:b\n---\nx')
    check("标题里的冒号不被切断", meta3["title"] == "告警: P0 级", repr(meta3.get("title")))
    check("裸值里的冒号保留", meta3["tag"] == "a:b", repr(meta3.get("tag")))

    # 数组内含逗号
    meta4, _ = parse_frontmatter('---\ntags: ["a, b", c]\n---\nx')
    check("数组内引号里的逗号不切分", meta4["tags"] == ["a, b", "c"], repr(meta4.get("tags")))

    # 空数组
    meta5, _ = parse_frontmatter("---\ntags: []\n---\nx")
    check("空数组解析为空 list", meta5["tags"] == [])

    # 容错：无法识别的行不崩
    meta6, _ = parse_frontmatter("---\nok: 1\n  - 缩进列表\n---\nx")
    check("无法识别的行被跳过而非抛错", meta6 == {"ok": 1}, repr(meta6))

    # frontmatter 未闭合
    meta7, body7 = parse_frontmatter("---\nkey: v\n没有结束分隔符")
    check("未闭合 frontmatter 回退为纯正文", meta7 == {} and body7.startswith("---"))

    # 类型推导
    check("coerce_scalar 浮点", coerce_scalar("0.96") == 0.96)
    check("coerce_scalar 布尔", coerce_scalar("true") is True)
    check("coerce_scalar 空值", coerce_scalar("") == "")
    check("coerce_scalar 带小数点版本号保持字符串", coerce_scalar("v1.2") == "v1.2")

    # 往返
    rt = parse_frontmatter(dump_frontmatter(meta))[0]
    check("dump→parse 往返一致", rt == meta, f"{rt} != {meta}")

    print("-" * 70)
    print(f"通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)

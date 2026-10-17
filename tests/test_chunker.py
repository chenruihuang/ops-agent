"""分块器测试。

分块是入库链路的**不可逆起点**：块切错了，后面再好的 embedding / rerank
也救不回来。所以这里覆盖的全是"错了很难发现"的行为。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

from app.ingest.chunker import (
    EMBED_MAX_CHARS,
    MIN_CHUNK_CHARS,
    chunk_document,
    context_header,
    pack_blocks,
    split_blocks,
    split_sections,
    split_table,
)
from app.ingest.loader import load_markdown

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


DOC = """# 标题

## 现象
现象正文，含错误码 ORD-429-001，服务 P99 耗时上涨。

## 快速判定
| 命令 | 判读 |
| --- | --- |
| `cmd a` | 说明 a |
| `cmd b` | 说明 b |

## 关联文档
- `rb-aaa-bbb` — 甲
- `rb-ccc-ddd` — 乙

## 处置步骤
第一步先止血，第二步再定位根因，见 rb-other-doc。
"""


def main() -> int:
    print("=" * 70)
    print("分块器测试")
    print("=" * 70)

    # ---------- 结构解析 ----------
    title, sections = split_sections(DOC)
    check("提取文档标题", title == "标题", repr(title))
    check("section 数量正确（不含关联文档过滤）", len(sections) == 4, str(len(sections)))
    check("第一个 section 是现象", sections[0].heading == "现象")
    check("section 路径为元组", sections[0].path == ("现象",), repr(sections[0].path))
    check("关联文档被标记为 reference", sections[2].is_reference)

    # 三级标题归属父 section
    _, sec2 = split_sections("# T\n\n## A\n甲\n\n### A1\n乙\n\n## B\n丙\n")
    check("### 归属父 section 而不独立成段", [s.heading for s in sec2] == ["A", "B"], str([s.heading for s in sec2]))
    check("### 内容留在父 section 内", "乙" in sec2[0].text, sec2[0].text)
    check("### 行本身保留在正文（小标题不丢失）", "### A1" in sec2[0].text, sec2[0].text)
    check("path 只由二级标题构成", sec2[0].path == ("A",), str(sec2[0].path))

    # ---------- 上下文头 ----------
    check("上下文头拼接标题与路径",
          context_header("MySQL 连接池耗尽处置", ("处置步骤",)) == "MySQL 连接池耗尽处置 · 处置步骤")
    check("无标题时只用路径", context_header("", ("现象",)) == "现象")
    check("无路径时只用标题", context_header("标题", ()) == "标题")

    # ---------- 表格与分段 ----------
    blocks = split_blocks("甲\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n乙")
    check("表格作为独立块", len(blocks) == 3, str(len(blocks)))
    check("表格未被空行拆开", "| 1 | 2 |" in blocks[1] and "| a | b |" in blocks[1])

    big = "| 列A | 列B |\n| --- | --- |\n" + "\n".join(f"| row{i} | v{i} |" for i in range(60))
    parts = split_table(big, 200)
    check("超长表格被切分", len(parts) > 1, str(len(parts)))
    check("每个片段都重复表头", all(p.startswith("| 列A | 列B |") for p in parts), str([p[:14] for p in parts]))

    # 表格片**不做 overlap** —— 回归测试。
    # 反例：overlap 把上一片末尾的数据行搬到下一片的表头之上，表格变成
    # 「数据行 / 表头 / 分隔行 / 数据行…」，完全读不通。实测在长文档上出现过。
    tbl = "| 列A | 列B |\n| --- | --- |\n" + "\n".join(f"| r{i} | v{i} |" for i in range(40))
    tw = pack_blocks([tbl], 200, 80)
    check("超长表格在打包阶段被切成多片", len(tw) > 1, str(len(tw)))
    check(
        "每片都以表头开头（数据行没跑到表头之上）",
        all(w.lstrip().startswith("| 列A | 列B |") for w in tw),
        str([w.splitlines()[0] for w in tw]),
    )
    # 对照：普通文本的 overlap 必须仍然生效，不能被这次修复一起关掉
    ov = pack_blocks([f"段落{i}：" + "内容" * 40 for i in range(5)], 200, 40)
    check(
        "普通文本仍保留 overlap（未被一并关掉）",
        any(a[-24:].strip() and a[-24:].strip()[:12] in b for a, b in zip(ov, ov[1:])),
        str([w[-14:] for w in ov[:3]]),
    )

    # 表格尾部也不得被 overlap 搬走 —— 回归测试。
    # 反例：`文字 | 表格 | 文字` 时，表格恰为一个窗口的结尾，
    # overlap 会取走表格最后一行（无表头的数据），让它成为
    # 下一个纯文字块的开头。实测出现过：一个讲"注意余量为正的服务"的
    # 文字块，第一行是 `| svc-file-service | 4 | 280 | ... |`。
    blocks_mix = ["说" * 200, tbl, "后" * 60]
    wins_mix = pack_blocks(blocks_mix, 200, 80)
    orphan = [
        w.splitlines()[0][:36]
        for w in wins_mix
        if w.lstrip().startswith("|")
        and (len(w.splitlines()) < 2 or "---" not in w.splitlines()[1])
    ]
    check(
        "没有窗口以「无表头的孤立表格行」开头",
        not orphan,
        str(orphan),
    )

    # ---------- 窗口打包与 overlap ----------
    paras = [f"段落{i}：" + "内容" * 40 for i in range(5)]
    wins = pack_blocks(paras, 200, 40)
    check("长文本被打包成多个窗口", len(wins) > 1, str(len(wins)))
    check("每个窗口不超过上限", all(len(w) <= 200 for w in wins), str([len(w) for w in wins]))

    # ---------- 端到端分块 ----------
    # merge_min_chars=0：先单独验证结构切分，合并行为在下面单独测
    doc = chunk_document(
        doc_id="rb-x",
        meta={"title": "标题", "category": "runbook", "tags": ["P1"]},
        body=DOC,
        merge_min_chars=0,
    )
    check("关联文档小节未生成内容块", all("关联文档" not in c.heading for c in doc.chunks))
    check("关联文档抽成图谱边", doc.related == ["rb-aaa-bbb", "rb-ccc-ddd"], str(doc.related))
    check("显式关联不出现在 mentions（边语义不重叠）",
          "rb-aaa-bbb" not in doc.mentions and "rb-ccc-ddd" not in doc.mentions,
          str(doc.mentions))
    check("正文里的引用进入 mentions", "rb-other-doc" in doc.mentions, str(doc.mentions))
    check("chunk_id 连续编号",
          [c.chunk_id for c in doc.chunks] == [f"rb-x#{i:02d}" for i in range(len(doc.chunks))],
          str([c.chunk_id for c in doc.chunks]))
    check("按 section 切出 3 块", len(doc.chunks) == 3, str([c.heading for c in doc.chunks]))
    check("每块都带上下文头", all(c.text.startswith("标题 ·") for c in doc.chunks))
    check("chunk.body 不含上下文头", all(not c.body.startswith("标题 ·") for c in doc.chunks))
    check("表格块保留表格", any("| `cmd a` | 说明 a |" in c.body for c in doc.chunks))

    # 小 section 合并
    small = "## 甲\n" + "甲" * 30 + "\n\n## 乙\n" + "乙" * 10 + "\n"
    d2 = chunk_document(doc_id="d2", meta={"title": "T"}, body=small, merge_min_chars=80)
    check("过短 section 并入上一块", len(d2.chunks) == 1, str([c.heading for c in d2.chunks]))
    check("合并后 heading 记录两段", d2.chunks[0].heading == "甲 / 乙", d2.chunks[0].heading)
    # 跨 section 合并后，section_path 必须是**公共前缀**而不是两段串联：
    # 甲、乙是两个顶层小节，没有公共前缀，所以退回到空路径（上下文头只剩文档标题）。
    # 若用 ("甲", "乙") 或把乙追加成 ("甲", "乙")，检索时这个块会同时"像"两个小节，
    # 归属含糊 —— 这是实测中真实出现过的误导。
    check(
        "跨 section 合并后 path 退回到公共前缀",
        d2.chunks[0].section_path == (),
        str(d2.chunks[0].section_path),
    )
    check(
        "退回后上下文头只剩文档标题",
        d2.chunks[0].text.startswith("T\n"),
        d2.chunks[0].text[:40],
    )

    # 同 section 内的窗口合并，path 必须保留（信息不应丢失）
    inner = "## 甲\n" + "甲" * 300 + "\n\n" + "乙" * 20 + "\n"
    d2b = chunk_document(doc_id="d2b", meta={"title": "T"}, body=inner, merge_min_chars=80)
    check(
        "同 section 内合并保留 path",
        all(c.section_path == ("甲",) for c in d2b.chunks) and len(d2b.chunks) >= 1,
        str([c.section_path for c in d2b.chunks]),
    )

    # 合并后的 heading 必须剥掉窗口编号：`甲(3/3) / 乙(1/3)` 这种编号
    # 一个结束一个开始，连在一起读只会更困惑
    mixed = "## 术语\n" + "术" * 400 + "\n\n" + "尾" * 20 + "\n\n## 属性表\n短引言\n"
    d2c = chunk_document(doc_id="d2c", meta={"title": "T"}, body=mixed, merge_min_chars=80)
    joined = [c.heading for c in d2c.chunks if " / " in c.heading]
    check(
        "合并后的 heading 不含窗口编号",
        all("(" not in h for h in joined),
        str(joined),
    )

    # 过短块丢弃
    d3 = chunk_document(doc_id="d3", meta={"title": "T"}, body="## 甲\n好\n", merge_min_chars=0)
    check(f"短于 {MIN_CHUNK_CHARS} 字符的块被丢弃", len(d3.chunks) == 0, str([c.body for c in d3.chunks]))
    check("被丢弃的小节留痕", "甲" in d3.dropped_sections, str(d3.dropped_sections))

    # 自引用不入边
    d4 = chunk_document(
        doc_id="self-doc", meta={"title": "T"}, body="## 正文\n见 self-doc 与 rb-other-doc\n"
    )
    check("自引用从 mentions 剔除", "self-doc" not in d4.mentions, str(d4.mentions))
    check("其他引用保留", "rb-other-doc" in d4.mentions, str(d4.mentions))

    # ---------- 真实语料回归 ----------
    root = Path("data/raw")
    if root.exists():
        res = load_markdown(root)
        chunks = [c for d in res.docs for c in d.chunks]
        ids = [c.chunk_id for c in chunks]
        check("真实语料全部装载（100+ 篇）", len(res.docs) >= 100, str(len(res.docs)))
        check("真实语料无跳过文件", res.skipped == [], str(res.skipped[:3]))
        check("全局 chunk_id 唯一", len(ids) == len(set(ids)), f"{len(ids)} vs {len(set(ids))}")
        check("块数在合理区间 200~600", 200 <= len(chunks) <= 600, str(len(chunks)))
        check("每块都有 doc_id", all(c.doc_id for c in chunks))
        check("每块都有非空文本", all(c.text.strip() for c in chunks))
        check(
            f"没有任何块超出 embedding 上下文（{EMBED_MAX_CHARS} 字符）",
            all(c.char_len <= EMBED_MAX_CHARS for c in chunks),
            f"超出 {sum(1 for c in chunks if c.char_len > EMBED_MAX_CHARS)} 块，最长 "
            f"{max(c.char_len for c in chunks)}",
        )
    else:
        print("  [SKIP] 真实语料目录不存在")

    print("-" * 70)
    print(f"通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)

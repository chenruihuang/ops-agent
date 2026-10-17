"""按标题层级切块 —— 入库的核心逻辑，纯标准库实现。

切块策略（按重要性排序）
------------------------
1. **一个二级 section 一个 chunk**，块内不跨语义边界。
   语料本身就是"标题 + 若干 section"的结构，按 section 切能让每个 chunk
   恰好回答一类问题（现象 / 判定 / 处置），也让引用能精确到小节。
2. **每块前置上下文头**：`{文档标题} · {section}` 拼进待向量化文本。
   语料里"处置步骤"这种标题本身零信息量，单看它向量化出来全是噪声；
   加上文档标题后，"MySQL 连接池耗尽处置 · 处置步骤"才具备可检索语义。
   这是最便宜也最有效的一处改动。
3. **表格不拆**：markdown 表格拆开就失去表头语义。若单表超长，
   按行切并**重复表头**。
4. **超长 section 二次切分**：按空行分段打包，窗口之间保留 overlap。
5. **过短 section 合并**：正文不足 MERGE_MIN_CHARS 的小节并入上一块
   （如只有一句话的"影响面"）。单独成块会稀释 top-k 名额，是负收益。
6. **"关联文档"小节不进向量库**，而是抽取成**图谱边**。
   理由：这一节全是文档 id 字符串，向量化后会和"我要查 xx"类问题产生
   大量虚假相似。它的真正价值是**多跳检索的一跳扩展**，应该走 metadata
   而不是走向量相似度。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---- 切块参数（改这几个值会改变召回率，属于实验变量，必须记进实验台账）----
#
# EMBED_MAX_CHARS 与 MAX_CHARS 的关系是本模块最容易被忽略的约束：
#   bge-small-zh-v1.5 的 max_seq_length = 512 token，中文约 1 token/字。
#   超过上限的部分会被 tokenizer **静默截断** —— 不报错、不告警，
#   只是 embedding 里少了一段。块尾恰好常是"处置步骤""回滚方案"这类关键内容，
#   截断后检索表现会莫名其妙地差，而且查不出来。
#   所以块长上限必须倒推自模型上下文，而不是拍脑袋取 1000。
EMBED_MAX_CHARS = 512
MAX_CHARS = 460           # 460 + 上下文头(约 40) 仍留有余量
MERGE_MIN_CHARS = 80
OVERLAP_CHARS = 80
MIN_CHUNK_CHARS = 12          # 比这还短的块直接丢弃（纯分隔符残渣）

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

# 这些小节名不作为内容块，而是转成图谱边
REFERENCE_HEADINGS = frozenset({"关联文档", "相关文档", "关联", "参见"})

# 文档 id 形态：rb-xxx / svc-xxx / alert-xxx / guide-xxx / pm-xxx / spec-xxx ...
# ⚠ 前缀集合与 scripts/corpus_build.py 的 REF_PATTERN、
#   scripts/check_corpus.py 的命名检查**必须保持一致**（三处同步）。
#   漏掉一个前缀的后果是：该前缀的文档能入库，但正文里的引用
#   抽不成图谱边 —— 而"多跳检索少了一跳"不会报错，只会让召回率偏低。
# 这里的 `\b` 前缀同时也是子串保护：`svc-search-service` 不会被误匹配出 `arch-service`。
_DOC_ID_TOKEN = re.compile(
    r"\b(?:rb|svc|err|alert|pol|arch|oncall|obs|cmd|faq|guide|pm|spec)"
    r"-[a-z0-9][a-z0-9-]{2,}\b"
)
_BACKTICKED = re.compile(r"`([^`\n]+)`")


@dataclass(slots=True)
class Chunk:
    doc_id: str
    chunk_id: str
    text: str                     # 送进 embedding 的文本（含上下文头）
    body: str                     # 原始正文（进 prompt 用，避免上下文头污染证据）
    heading: str
    section_path: tuple[str, ...]
    category: str = ""
    title: str = ""
    tags: tuple[str, ...] = ()
    source: str = ""
    modality: str = "text"        # text | image_ocr | image_caption
    kind: str = "content"         # content | image_ocr | image_caption
    char_len: int = 0

    def ref(self) -> str:
        """人类可读的引用标识，用于 prompt 里的来源标注与演示。"""
        return f"{self.title} · {self.heading}" if self.title else self.doc_id


@dataclass(slots=True)
class Section:
    level: int
    heading: str
    path: tuple[str, ...]
    text: str

    @property
    def is_reference(self) -> bool:
        return self.heading in REFERENCE_HEADINGS


@dataclass(slots=True)
class DocChunks:
    doc_id: str
    title: str
    category: str
    tags: tuple[str, ...]
    path: str
    chunks: list[Chunk] = field(default_factory=list)
    related: list[str] = field(default_factory=list)      # 显式关联（关联文档小节 + frontmatter）
    mentions: list[str] = field(default_factory=list)     # 正文里出现过的其他文档 id
    dropped_sections: list[str] = field(default_factory=list)

    @property
    def char_len(self) -> int:
        return sum(c.char_len for c in self.chunks)


# =====================================================================
# 结构解析
# =====================================================================
def split_sections(body: str) -> tuple[str, list[Section]]:
    """把正文拆成 (文档标题, sections)。`#` 视为文档标题，`##` 及以下为 section。"""
    lines = body.splitlines()
    doc_title = ""
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []

    cur_heading = ""
    cur_level = 0
    cur_path: tuple[str, ...] = ()
    cur_lines: list[str] = []
    preamble: list[str] = []

    def flush() -> None:
        nonlocal cur_lines
        if cur_heading:
            sections.append(
                Section(
                    level=cur_level,
                    heading=cur_heading,
                    path=cur_path,
                    text="\n".join(cur_lines).strip("\n"),
                )
            )
        cur_lines = []

    for line in lines:
        m = _HEADING.match(line)
        if not m:
            (cur_lines if cur_heading else preamble).append(line)
            continue

        level = len(m.group(1))
        heading = m.group(2).strip()
        if level == 1:
            # `#` 是文档标题，不作为切分点
            if not doc_title:
                doc_title = heading
            continue

        if level == 2:
            # 只有二级标题才是切分点 —— 这与"一个 section 一个 chunk"的策略一致。
            # 更深的标题（###）留在父 section 正文里，保留小标题可见性，
            # 但不额外产生 chunk（碎片化会稀释 top-k 名额）。
            flush()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading))
            cur_level = level
            cur_heading = heading
            cur_path = tuple(h for _, h in stack)
        else:
            (cur_lines if cur_heading else preamble).append(line)

    flush()

    # 首个 section 之前若还有散落正文（无标题前言），并入第一个 section
    if sections and any(p.strip() for p in preamble):
        merged = "\n".join(preamble).strip() + "\n" + sections[0].text
        sections[0] = Section(
            sections[0].level, sections[0].heading, sections[0].path, merged.strip()
        )
    return doc_title, sections


def split_blocks(text: str) -> list[str]:
    """按空行分段；连续的表格行视为一个不可分块。"""
    blocks: list[str] = []
    cur: list[str] = []
    in_table = False

    for raw in text.splitlines():
        line = raw.rstrip()
        is_row = line.lstrip().startswith("|")

        if not line.strip() and not in_table:
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue

        if is_row and not in_table:
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            in_table = True
        elif in_table and not is_row:
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            in_table = False

        cur.append(line)

    if cur:
        blocks.append("\n".join(cur))
    return [b for b in blocks if b.strip()]


def split_table(table: str, max_chars: int) -> list[str]:
    """超长表格按行切分并**重复表头**，否则后续片段读起来是没头的数据行。"""
    rows = [r for r in table.splitlines() if r.strip()]
    if len(rows) < 3:
        return [table]
    header = rows[:2]                     # 表头 + 分隔行
    body = rows[2:]
    head_text = "\n".join(header)
    out: list[str] = []
    cur: list[str] = []

    for row in body:
        trial = "\n".join(header + cur + [row])
        if cur and len(trial) > max_chars:
            out.append("\n".join(header + cur))
            cur = [row]
        else:
            cur.append(row)
    if cur:
        out.append("\n".join(header + cur))
    if not out:
        return [head_text]
    return out


def _ends_in_table_row(text: str) -> bool:
    """判断文本的最后一行是否落在表格内部。"""
    lines = [ln for ln in text.rstrip().splitlines() if ln.strip()]
    return bool(lines) and lines[-1].lstrip().startswith("|")


def pack_blocks(blocks: list[str], max_chars: int, overlap: int) -> list[str]:
    """把段落块打包成不超过 max_chars 的窗口，窗口间保留 overlap 字符。

    不变式：**每个返回窗口的长度 ≤ max_chars**。
    包括 overlap 尾巴在内的所有成分都受这个约束 —— 早期版本在
    "窗口 = overlap 尾巴 + 新块"这一步漏算了尾巴长度，结果窗口可以
    超出上限 60 字符，进而让最终文本越过模型上下文被静默截断。
    这类 bug 不会报错，只会让召回率莫名偏低，所以在这里显式保证。

    overlap 的两条**禁则**（都是实测踩出来的，不是预想的）：
    ① **当前片是表格 → 不取尾巴。** 否则上一片的数据行会被搬到本片表头之上，
       表格变成「数据行 / 表头 / 分隔行 / 数据行…」，完全读不通。
    ② **前一片的尾部落在表格内 → 不取尾巴。** 否则会搬走一行**没有表头**的数据，
       让它成为下一个窗口的开头 —— 形成"一个纯文字块以孤立表格行开头"的错乱，
       而且这行数据（如 `| svc-file-service | 4 | 280 | ... |`）没有任何上下文，
       既是噪声，又可能引发错误召回。
    表格的自解释性由**重复表头**保证；overlap 对表格没有增益，只有副作用。
    """
    if not blocks:
        return []

    pieces: list[str] = []
    for b in blocks:
        if len(b) <= max_chars:
            pieces.append(b)
            continue
        if b.lstrip().startswith("|"):
            pieces.extend(split_table(b, max_chars))
            continue
        # 单段超长：先按句子边界切，仍超长再按字符硬切
        for seg in re.split(r"(?<=[。；！？\n])", b):
            if not seg:
                continue
            if len(seg) <= max_chars:
                pieces.append(seg)
                continue
            step = max(1, max_chars - overlap)
            pieces.extend(seg[i : i + max_chars] for i in range(0, len(seg), step))

    windows: list[str] = []
    cur = ""
    for piece in pieces:
        if not cur:
            cur = piece
            continue
        if len(cur) + len(piece) + 2 > max_chars:
            windows.append(cur.strip())
            is_table = piece.lstrip().startswith("|")
            allowance = max(0, max_chars - len(piece) - 1)
            tail = (
                cur[-min(overlap, allowance) :]
                if (
                    overlap > 0
                    and allowance > 0
                    and not is_table
                    and not _ends_in_table_row(cur)
                )
                else ""
            )
            if tail:
                cut = tail.find("\n")
                tail = tail[cut + 1 :] if cut >= 0 else tail
            cur = f"{tail}\n{piece}" if tail else piece
        else:
            cur = f"{cur}\n\n{piece}"
    if cur.strip():
        windows.append(cur.strip())
    return [w for w in windows if w]


def context_header(title: str, path: tuple[str, ...]) -> str:
    """上下文头。这是检索质量的关键：给孤立的 section 标题补上所属文档。"""
    joined = " · ".join(path) if path else ""
    if title and joined:
        return f"{title} · {joined}"
    return title or joined


_WINDOW_TAG = re.compile(r"\(\d+/\d+\)\s*$")


def strip_window_tag(heading: str) -> str:
    """剥掉 `(1/3)` 这类窗口编号，用于跨块合并时拼接可读标题。"""
    return _WINDOW_TAG.sub("", heading).strip()


def common_path_prefix(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
    """两个 section 路径的公共前缀。

    用于**跨 section 合并**时的上下文头。一个块里同时含着两个小节的内容时，
    任何单一小节名都是误导 —— 读者（和模型）会以为整块都属于那一节。

    实测的例子：`Span 属性必填项` 的引言段只有 50 字，被并入上一节
    `术语定义`，上下文头就成了 `… · 术语定义 · Span 属性必填项(1/3)`；
    检索时这个块既像"术语定义"又像"属性表"，归属含糊。

    退回到公共前缀是"宁可少说、不要错说"的选择：
    同 section 内合并时前缀即本节（信息不丢）；跨顶层 section 合并时
    前缀为空，上下文头只剩下文档标题（虽然信息少，但不会误导）。
    """
    out: list[str] = []
    for x, y in zip(a, b):
        if x != y:
            break
        out.append(x)
    return tuple(out)


# =====================================================================
# 主入口
# =====================================================================
def chunk_document(
    *,
    doc_id: str,
    meta: dict,
    body: str,
    source: str = "",
    max_chars: int = MAX_CHARS,
    merge_min_chars: int = MERGE_MIN_CHARS,
    overlap: int = OVERLAP_CHARS,
) -> DocChunks:
    """把一篇文档切成 chunk 集合。纯函数，无 IO、无第三方依赖。"""
    title = str(meta.get("title") or "")
    category = str(meta.get("category") or "")
    tags = tuple(str(t) for t in (meta.get("tags") or ()))
    related = [str(r) for r in (meta.get("related") or ())]

    parsed_title, sections = split_sections(body)
    if parsed_title and not title:
        title = parsed_title

    doc = DocChunks(doc_id=doc_id, title=title, category=category, tags=tags, path=source)

    # ---- 1) 抽取图谱边，并把"关联文档"小节排除出内容块 ----
    content_sections: list[Section] = []
    for sec in sections:
        if sec.is_reference:
            for token in _BACKTICKED.findall(sec.text):
                token = token.strip()
                if _DOC_ID_TOKEN.fullmatch(token):
                    related.append(token)
            continue
        content_sections.append(sec)

    # ---- 2) 正文里出现过的其他文档 id → mentions（软边，用于图扩展）----
    # 口径说明：mentions 只统计**内容小节**里的引用，且剔除已进 related 的显式边。
    # 这样 related 是"显式声明的关系"，mentions 是"行文中顺带提到的关系"，
    # 两类边的语义不重叠，图谱扩展时可以做不同权重的衰减。
    explicit = set(related)
    content_body = "\n".join(s.text for s in content_sections)
    mentions: list[str] = []
    for token in _DOC_ID_TOKEN.findall(content_body):
        if token != doc_id and token not in explicit:
            mentions.append(token)

    related = _dedupe(related, exclude=doc_id)
    doc.related = related
    doc.mentions = _dedupe(mentions, exclude=doc_id)

    # ---- 3) 逐 section 生成块 ----
    # (heading, section_path, 完整待向量化文本, 纯正文)
    raw_chunks: list[tuple[str, tuple[str, ...], str, str]] = []
    for sec in content_sections:
        text = sec.text.strip()
        if not text:
            doc.dropped_sections.append(sec.heading)
            continue
        header = context_header(title, sec.path)
        # 约束加在"最终文本"上：预算 = 上限 - 上下文头长度。
        # 否则头部越长、正文越容易被挤过模型上下文而被静默截断。
        budget = max(80, max_chars - len(header) - 1)
        windows = pack_blocks(split_blocks(text), budget, overlap)
        if not windows:
            doc.dropped_sections.append(sec.heading)
            continue
        for i, win in enumerate(windows):
            heading = sec.heading if len(windows) == 1 else f"{sec.heading}({i + 1}/{len(windows)})"
            body_text = f"{header}\n{win}"
            raw_chunks.append((heading, sec.path, body_text, win))

    # ---- 4) 合并过短块（同样以最终文本长度为准）----
    merged: list[tuple[str, tuple[str, ...], str, str]] = []
    for heading, path, full, win in raw_chunks:
        if merged:
            p_heading, p_path, p_full, p_win = merged[-1]
            if len(win) < merge_min_chars:
                new_path = common_path_prefix(p_path, path)
                new_heading = f"{strip_window_tag(p_heading)} / {strip_window_tag(heading)}"
                new_full = f"{context_header(title, new_path)}\n{p_win}\n{win}"
                if len(new_full) <= max_chars:
                    merged[-1] = (new_heading, new_path, new_full, f"{p_win}\n{win}")
                    continue
        merged.append((heading, path, full, win))

    # ---- 5) 落成 Chunk ----
    for idx, (heading, path, full, win) in enumerate(merged):
        if len(win) < MIN_CHUNK_CHARS:
            doc.dropped_sections.append(heading)
            continue
        chunk_id = f"{doc_id}#{len(doc.chunks):02d}"
        doc.chunks.append(
            Chunk(
                doc_id=doc_id,
                chunk_id=chunk_id,
                text=full,
                body=win,
                heading=heading,
                section_path=path,
                category=category,
                title=title,
                tags=tags,
                source=source,
                char_len=len(full),
            )
        )
    return doc


def _dedupe(items: list[str], *, exclude: str = "") -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        it = it.strip()
        if not it or it == exclude or it in seen:
            continue
        seen.add(it)
        out.append(it)
    return out


def chunk_stats(docs: list[DocChunks]) -> dict[str, object]:
    """入库报告用的统计口径，集中在这里避免口径漂移。"""
    chunks = [c for d in docs for c in d.chunks]
    sizes = sorted(c.char_len for c in chunks) or [0]

    def pct(p: float) -> int:
        return sizes[min(len(sizes) - 1, int(len(sizes) * p))]

    by_cat: dict[str, int] = {}
    for d in docs:
        by_cat[d.category] = by_cat.get(d.category, 0) + len(d.chunks)

    edges = sum(len(d.related) for d in docs)
    return {
        "documents": len(docs),
        "chunks": len(chunks),
        "chunks_per_doc_mean": round(len(chunks) / len(docs), 2) if docs else 0.0,
        "char_len_mean": int(sum(sizes) / len(sizes)) if chunks else 0,
        "char_len_p50": pct(0.50),
        "char_len_p95": pct(0.95),
        "by_category": dict(sorted(by_cat.items())),
        "graph_edges_related": edges,
        "graph_edges_mentions": sum(len(d.mentions) for d in docs),
        "dropped_sections": sum(len(d.dropped_sections) for d in docs),
    }

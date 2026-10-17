"""BM25 关键词索引 —— 纯标准库实现，可独立持久化。

为什么自己写而不直接用 bm25s
---------------------------
1. **中文分词**：bm25s 需要外部 tokenizer（jieba 之类）。而对**运维语料**来说，
   真正要救的是错误码、命令、参数名这类"精确串"——它们恰恰是向量检索最钝的地方。
   这类串用「英文单词 + CJK 字符二元组」就能精确命中，反而不需要词典分词。
2. **零依赖**：索引构建必须在离线环境可跑、可单测。
3. **可控**：参数（k1/b）与分词规则要显式记进实验台账，成为可复现的实验变量。

分词规则
--------
- 英文/数字/连字符串整体保留：`ORD-429-001`、`-Xmx4g`、`max_connections`、`df -i`
- CJK 连续串切成**字符二元组**：`连接池耗尽` → 连接/接池/池耗/耗尽
  二元组在中文上接近词典分词的召回，且完全无歧义、可复现。
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_ASCII_TOKEN = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

DEFAULT_K1 = 1.5
DEFAULT_B = 0.75


def tokenize(text: str) -> list[str]:
    """CJK 二元组 + ASCII 词。顺序无关，供 BM25 统计词频。"""
    lowered = (text or "").lower()
    tokens: list[str] = []
    for m in _ASCII_TOKEN.finditer(lowered):
        tokens.append(m.group(0))
    for m in _CJK_RUN.finditer(lowered):
        run = m.group(0)
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


@dataclass
class BM25Index:
    """Okapi BM25。语料规模在万级 chunk 以内时性能完全够用。"""

    k1: float = DEFAULT_K1
    b: float = DEFAULT_B
    chunk_ids: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    doc_len: list[int] = field(default_factory=list)
    df: dict[str, int] = field(default_factory=dict)
    # token -> {doc_index: term_freq}
    postings: dict[str, dict[int, int]] = field(default_factory=dict)
    avgdl: float = 0.0

    # ---------------- 构建 ----------------
    def add(self, chunk_id: str, text: str) -> None:
        idx = len(self.chunk_ids)
        self.chunk_ids.append(chunk_id)
        self.texts.append(text)

        toks = tokenize(text)
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        self.doc_len.append(len(toks))

        for t, freq in tf.items():
            self.postings.setdefault(t, {})[idx] = freq
            self.df[t] = self.df.get(t, 0) + 1

    def finalize(self) -> None:
        n = len(self.chunk_ids)
        self.avgdl = (sum(self.doc_len) / n) if n else 0.0

    @property
    def size(self) -> int:
        return len(self.chunk_ids)

    def _idf(self, token: str) -> float:
        n = self.size
        if n == 0:
            return 0.0
        df = self.df.get(token, 0)
        # 标准的 BM25 idf 正数化写法，避免高频词产生负分
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    # ---------------- 查询 ----------------
    def search(self, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        if not self.size:
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []

        scores: dict[int, float] = {}
        for token in set(q_tokens):
            posting = self.postings.get(token)
            if not posting:
                continue
            idf = self._idf(token)
            for doc_idx, freq in posting.items():
                dl = self.doc_len[doc_idx]
                denom = freq + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1.0))
                scores[doc_idx] = scores.get(doc_idx, 0.0) + idf * freq * (self.k1 + 1) / denom

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        return [(self.chunk_ids[i], round(s, 6)) for i, s in ranked]

    def text_of(self, chunk_id: str) -> str:
        try:
            return self.texts[self.chunk_ids.index(chunk_id)]
        except ValueError:
            return ""

    # ---------------- 持久化 ----------------
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "k1": self.k1,
            "b": self.b,
            "avgdl": self.avgdl,
            "size": self.size,
            "chunk_ids": self.chunk_ids,
            "texts": self.texts,
            "doc_len": self.doc_len,
            "df": self.df,
            "postings": {t: {str(i): f for i, f in p.items()} for t, p in self.postings.items()},
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        log.info("BM25 索引已落盘：%s（%d 块 / %d 词项）", path, self.size, len(self.df))

    @classmethod
    def load(cls, path: Path) -> "BM25Index":
        payload = json.loads(path.read_text(encoding="utf-8"))
        idx = cls(k1=payload.get("k1", DEFAULT_K1), b=payload.get("b", DEFAULT_B))
        idx.chunk_ids = list(payload["chunk_ids"])
        idx.texts = list(payload["texts"])
        idx.doc_len = list(payload["doc_len"])
        idx.df = dict(payload["df"])
        idx.postings = {
            t: {int(i): int(f) for i, f in p.items()} for t, p in payload["postings"].items()
        }
        idx.avgdl = float(payload.get("avgdl") or 0.0)
        return idx


def build_bm25(chunk_ids: list[str], texts: list[str]) -> BM25Index:
    idx = BM25Index()
    for cid, text in zip(chunk_ids, texts):
        idx.add(cid, text)
    idx.finalize()
    return idx

"""测试替身：确定性假 embedder + 内存向量库。

存在的理由
----------
真实入库依赖 `sentence-transformers` 下载模型 + Qdrant 服务，两者都需要联网。
如果测试也依赖它们，那么"入库链路正确"这件事在离线环境下就**无法验证** ——
而无法验证的代码等于没写。

所以这里用一对替身把链路打通：分块 → 建点 → 向量化 → 检索 → 算 recall
全程不需要网络。它验证的是**代码路径与不变式**，不是检索质量。

注意：假 embedder 给的是词法相似度（字符二元组哈希），
所以召回率的**绝对值没有意义**，只有相对对比和"链路是否通"有意义。
绝不可以把这里的数字写进简历 —— 那是假数据。
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

_ASCII = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
_CJK = re.compile(r"[\u4e00-\u9fff]+")


def _tokens(text: str) -> list[str]:
    lowered = (text or "").lower()
    out = [m.group(0) for m in _ASCII.finditer(lowered)]
    for m in _CJK.finditer(lowered):
        run = m.group(0)
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return out


class FakeEmbedder:
    """字符二元组 → 哈希桶 → L2 归一化。跨进程稳定（用 md5 而非内置 hash）。"""

    def __init__(self, dim: int = 256):
        self.dim = dim
        self.calls = 0

    def encode(self, texts: Sequence[str], **_: Any) -> list[list[float]]:
        self.calls += len(texts)
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in _tokens(text):
                bucket = int.from_bytes(hashlib.md5(tok.encode("utf-8")).digest()[:4], "big")
                vec[bucket % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


@dataclass
class _Point:
    id: str
    vector: list[float]
    payload: dict[str, Any]


@dataclass
class InMemoryVectorStore:
    """内存向量库，接口刻意贴近 Qdrant 的 upsert/query，
    这样同一段检索代码可以在两套后端之间切换而不需要分支。"""

    points: dict[str, _Point] = field(default_factory=dict)
    upsert_calls: int = 0

    def upsert(self, ids: Sequence[str], vectors: Sequence[Sequence[float]], payloads: Sequence[dict]) -> int:
        for pid, vec, payload in zip(ids, vectors, payloads):
            self.points[pid] = _Point(pid, list(vec), dict(payload))
        self.upsert_calls += 1
        return len(ids)

    def count(self) -> int:
        return len(self.points)

    def search(
        self,
        vector: Sequence[float],
        *,
        limit: int = 20,
        kb_version: int | None = None,
    ) -> list[tuple[str, float, dict]]:
        qn = math.sqrt(sum(x * x for x in vector)) or 1.0
        scored: list[tuple[str, float, dict]] = []
        for p in self.points.values():
            if kb_version is not None and int(p.payload.get("kb_version", kb_version)) != kb_version:
                continue
            vn = math.sqrt(sum(x * x for x in p.vector)) or 1.0
            dot = sum(a * b for a, b in zip(vector, p.vector))
            scored.append((p.id, dot / (qn * vn), p.payload))
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:limit]

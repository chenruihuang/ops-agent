"""语义缓存的命中护栏。

放在独立模块的原因：它是纯函数，可以脱离 Redis 做单元测试，
而且未来换成别的向量库（Qdrant / pgvector）时这块逻辑不需要动。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.cache.keys import extract_anchors


@dataclass(frozen=True, slots=True)
class GuardResult:
    accepted: bool
    reason: str


def check_anchor_consistency(new_query: str, cached_query: str) -> GuardResult:
    """数字锚点一致性校验。

    规则（保守优先）：
      - 两边锚点完全相同           → 放行
      - 两边都没有锚点             → 放行（纯文本问题，靠向量相似度就够）
      - 一边有一边没有             → 拒绝（多了一个约束条件，是不同的问题）
      - 两边都有但不相同           → 拒绝（最危险的情况）

    代价是损失一部分潜在命中率。这个取舍是刻意的：
    在"答错一个数字导致用户按错条款办事"的场景里，宁可多打一次 LLM。
    """
    a = extract_anchors(new_query)
    b = extract_anchors(cached_query)

    if a == b:
        return GuardResult(True, "anchors_match")
    if not a and not b:
        return GuardResult(True, "no_anchors")
    return GuardResult(False, f"anchor_mismatch:{sorted(a)}!={sorted(b)}")


def check_length_ratio(new_query: str, cached_query: str, *, max_ratio: float = 3.0) -> GuardResult:
    """长度比护栏：一句话被扩写成一大段，通常意味着意图已经变了。"""
    a, b = len(new_query.strip()), len(cached_query.strip())
    if a == 0 or b == 0:
        return GuardResult(False, "empty_query")
    ratio = max(a, b) / min(a, b)
    if ratio > max_ratio:
        return GuardResult(False, f"length_ratio={ratio:.2f}")
    return GuardResult(True, "length_ok")

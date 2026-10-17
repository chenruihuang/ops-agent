"""多路召回融合 —— 服务端与评估脚本**共用同一份实现**。

为什么必须抽出来
----------------
之前 `scripts/eval_retrieval.py` 里有一份 `rrf_fuse`，服务端再写一份是必然的事。
只要存在两份，"评估报告说融合有效、线上却不是那回事"就会发生 ——
而且这种偏差**没有任何告警**，只有人工逐行比代码才能发现。
所以这里的原则是：**融合算法只有一份，评估脚本从服务端 import。**

为什么用 RRF 而不是分数加权
---------------------------
向量相似度是 0~1 有界余弦，BM25 分数无上界（实测同一次检索里能从 0.8 跨到 27）。
两者量纲不可比：
  - 直接相加 → BM25 单方面决定排名，向量路等于没接
  - 归一化后相加 → 归一化系数随查询集变化，同一个查询在不同批次里排名会漂

RRF 只用**排名**，天然免疫量纲问题，也天然免疫"某一路分数整体偏高"。
这是它在多路检索里几乎是默认选择的原因。

另一个细节：RRF 的 k 常数
-------------------------
k 越大，头部排名的差异被压得越平（k=60 时 rank1 与 rank2 的差距只有 k=1 时的 1/60）。
所以 k 小 → 更相信单路的第一名，k 大 → 更依赖"多路都命中"的一致性。
默认 60 是原论文（Cormack et al. 2009）的经验值，这里保持不动，
并且**把它写进实验台账** —— 改它就是改评估口径，必须重跑基线。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence, TypeVar

# RRF 排名平滑常数。见模块 docstring：改这个数等于改评估口径。
RRF_K = 60

T = TypeVar("T")


def rrf_scores(rankings: Sequence[Sequence[str]], *, k: int = RRF_K) -> dict[str, float]:
    """多路排名的 RRF 分数表（id → 融合分，越大越靠前）。

    同一路内部重复出现的 id 只记**首次出现**的名次，并且**去重后重新编号**。

    为什么是"重新编号"而不是"跳过但保留原位置"：后者会让重复项占位，
    把后面的文档无端推后。举例（k=60）：
        路 A = [x, x, x, y]  → 若保留原位置，y 得到 1/64
        路 B = [x, y]        → y 得到 1/62
    同一批数据、同一个 y，仅因为 A 路里 x 多重复了两次，y 的名次就变差了。
    在服务端这不是假想：候选池是 chunk 级的，一个文档有 5 个相似块
    就会挤掉 5 个名次 —— 表现为"块多的文档占便宜"，
    而这恰好是文档级召回口径要防的退化。
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        seen_in_this_ranking: set[str] = set()
        rank = 0
        for doc_id in ranking:
            if doc_id in seen_in_this_ranking:
                continue
            seen_in_this_ranking.add(doc_id)
            rank += 1
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


def rrf_fuse(rankings: Sequence[Sequence[str]], *, k: int = RRF_K) -> list[str]:
    """Reciprocal Rank Fusion：把多路召回按"排名的倒数"融合成一条有序列表。

    保持这个签名不变 —— 评估脚本 `eval_retrieval.py` 直接调它。
    """
    scores = rrf_scores(rankings, k=k)
    return [d for d, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]


def rrf_fuse_items(
    groups: Sequence[Sequence[T]],
    key: Any,
    *,
    k: int = RRF_K,
) -> list[tuple[T, float]]:
    """融合"对象分组"，返回 (对象, 融合分) 有序列表。

    服务端要的是 `Chunk` 对象而不是 id 字符串（后续还要重排、拼上下文），
    但融合又必须按 id 去重，所以这里用 `key` 把对象映射到 id，
    保留**首次出现**的那个对象实例（同一 chunk 在不同路径下内容相同，取谁都不影响）。
    """
    first_seen: dict[str, T] = {}
    rankings: list[list[str]] = []
    for group in groups:
        ids: list[str] = []
        for item in group:
            cid = str(key(item))
            if cid not in first_seen:
                first_seen[cid] = item
            ids.append(cid)
        rankings.append(ids)

    scores = rrf_scores(rankings, k=k)
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [(first_seen[cid], score) for cid, score in ordered if cid in first_seen]


def cap_per_unit(
    items: Sequence[T],
    unit_of: Any,
    *,
    max_per_unit: int = 3,
) -> list[T]:
    """限制"同一来源"在候选池里的占比，保持原有顺序。

    为什么必须有这一层：候选池是 chunk 级的，而一个长文档会被切成几十块。
    如果它的内容恰好与问题相关，前 20 个坑位可能全被同一篇文档的块占满 ——
    于是另外 2 篇真正需要的文档连候选池都进不去，重排再强也救不回来。

    这是"检索多样性"，不是"检索精度"的问题，所以放在召回之后、重排之前。
    取值 3 的理由：一个文档真正需要的通常是 1~2 个块（现象 + 处置），
    给到 3 已经足够容错，同时保证池子里至少能容纳 6~7 篇不同文档。
    """
    counts: dict[Any, int] = {}
    out: list[T] = []
    for item in items:
        unit = unit_of(item)
        n = counts.get(unit, 0)
        if n >= max_per_unit:
            continue
        counts[unit] = n + 1
        out.append(item)
    return out


def dedupe_docs(chunk_ids: Iterable[str], chunk_to_doc: Mapping[str, str]) -> list[str]:
    """把 chunk 排名压成 doc 排名（保留首次出现的顺序）。

    评估口径是**文档级**召回：用户关心的是"该看的文档有没有被找到"，
    而不是"同一文档的第 3 块还是第 5 块被找到"。压成文档级还能让
    "一个文档的多块占满 top-k"这种退化现象无处藏身。
    """
    seen: set[str] = set()
    out: list[str] = []
    for cid in chunk_ids:
        doc = chunk_to_doc.get(cid)
        if doc and doc not in seen:
            seen.add(doc)
            out.append(doc)
    return out

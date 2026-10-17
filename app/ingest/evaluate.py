"""检索质量评估的纯逻辑（无 IO、无第三方依赖）。

为什么单独成模块
----------------
评估口径必须**只有一份**。脚本里算一遍、测试里算一遍、面试时手算一遍，
三者不一致是评估体系最常见的失信来源 —— 数字看起来都对，但没人能复现。

两个口径
--------
    strict   : 只有 chunk 的 doc_id 直接命中 golden 才算召回
    expanded : 允许**图谱一跳扩展** —— 命中截图/文档的邻居也算

为什么要区分：多模态题的 golden 标的是"该看哪篇处置文档"，而向量库里
先被召回的是截图块（doc_id 是 img-xxx）。如果不允许一跳扩展，这类题
永远算不中，而这不是检索错了，是**口径没定义清**。
两个数字都报，才是诚实的做法；只报大的那个是自我美化。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence


@dataclass(slots=True)
class Question:
    qid: int
    question: str
    golden_doc_ids: list[str]
    notes: str = ""
    intent: str = "rag"


@dataclass(slots=True)
class QuestionScore:
    qid: int
    question: str
    golden: list[str]
    retrieved_top: list[str]
    hit: list[str]
    missed: list[str]
    recall: float
    expanded_recall: float
    notes: str = ""
    intent: str = "rag"
    first_hit_rank: int | None = None    # 第一个命中 golden 的名次（1 起）；未命中为 None

    @property
    def reciprocal_rank(self) -> float:
        return 1.0 / self.first_hit_rank if self.first_hit_rank else 0.0


@dataclass(slots=True)
class EvalReport:
    k: int
    scored: list[QuestionScore] = field(default_factory=list)
    skipped_no_golden: list[int] = field(default_factory=list)

    @property
    def recall(self) -> float:
        vals = [q.recall for q in self.scored]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def expanded_recall(self) -> float:
        vals = [q.expanded_recall for q in self.scored]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def mrr(self) -> float:
        """MRR：第一个命中名次的倒数均值。

        为什么必须和 recall@k 一起报：recall@5 只说明"东西在池子里"，
        MRR 说明"它排在第几位"。Rerank 主要改善 MRR，如果只看 recall@5，
        重排的价值会被严重低估 —— 这是做检索评估最常见的自欺。
        """
        vals = [q.reciprocal_rank for q in self.scored]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def hit_rate(self) -> float:
        """至少命中一个 golden 的题占比（全丢率 = 1 - hit_rate）。"""
        if not self.scored:
            return 0.0
        return sum(1 for q in self.scored if q.hit) / len(self.scored)

    def by_intent(self) -> dict[str, float]:
        buckets: dict[str, list[float]] = {}
        for q in self.scored:
            buckets.setdefault(q.intent, []).append(q.recall)
        return {k: sum(v) / len(v) for k, v in sorted(buckets.items())}

    def by_notes(self) -> dict[str, float]:
        buckets: dict[str, list[float]] = {}
        for q in self.scored:
            buckets.setdefault(q.notes, []).append(q.recall)
        return {k: sum(v) / len(v) for k, v in sorted(buckets.items())}

    def summary(self) -> dict[str, object]:
        return {
            "k": self.k,
            "questions_scored": len(self.scored),
            "questions_skipped_no_golden": len(self.skipped_no_golden),
            f"context_recall@{self.k}": round(self.recall, 4),
            f"context_recall@{self.k}_expanded": round(self.expanded_recall, 4),
            f"hit_rate@{self.k}": round(self.hit_rate, 4),
            "mrr": round(self.mrr, 4),
            "by_intent": {k: round(v, 4) for k, v in self.by_intent().items()},
        }


def recall_at_k(retrieved: Sequence[str], golden: Sequence[str], k: int) -> float:
    """context recall@k = 命中 golden 数 / golden 总数。"""
    if not golden:
        return float("nan")
    top = set(retrieved[:k])
    return sum(1 for g in golden if g in top) / len(golden)


def expand_one_hop(doc_ids: Iterable[str], adjacency: Mapping[str, Sequence[str]]) -> set[str]:
    out: set[str] = set()
    for d in doc_ids:
        out.add(d)
        out.update(adjacency.get(d, ()))
    return out


def evaluate(
    questions: Sequence[Question],
    retrieved_by_qid: Mapping[int, Sequence[str]],
    *,
    k: int = 5,
    adjacency: Mapping[str, Sequence[str]] | None = None,
    top_preview: int = 5,
) -> EvalReport:
    """逐题算 recall。

    `retrieved_by_qid` 是"每题检索到的 doc_id 有序列表"，顺序即排名 —— 
    顺序错了整张表就错了，所以这个接口只接受**有序**序列，不接受 set。
    """
    report = EvalReport(k=k)
    adjacency = adjacency or {}

    for q in questions:
        if not q.golden_doc_ids:
            report.skipped_no_golden.append(q.qid)
            continue

        ranked = list(retrieved_by_qid.get(q.qid, ()))
        top = ranked[:k]
        top_set = set(top)
        hit = [g for g in q.golden_doc_ids if g in top_set]

        first_rank: int | None = None
        for i, doc in enumerate(ranked, start=1):
            if doc in q.golden_doc_ids:
                first_rank = i
                break

        expanded = expand_one_hop(top, adjacency)
        hit_expanded = [g for g in q.golden_doc_ids if g in expanded]

        report.scored.append(
            QuestionScore(
                qid=q.qid,
                question=q.question,
                golden=list(q.golden_doc_ids),
                retrieved_top=top[:top_preview],
                hit=hit,
                missed=[g for g in q.golden_doc_ids if g not in hit],
                recall=len(hit) / len(q.golden_doc_ids),
                expanded_recall=len(hit_expanded) / len(q.golden_doc_ids),
                notes=q.notes,
                intent=q.intent,
                first_hit_rank=first_rank,
            )
        )
    return report


def load_questions(path) -> list[Question]:
    import json
    from pathlib import Path

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        Question(
            qid=int(q["id"]),
            question=str(q["question"]),
            golden_doc_ids=list(q.get("golden_doc_ids") or []),
            notes=str(q.get("notes") or ""),
            intent=str(q.get("intent") or "rag"),
        )
        for q in raw
    ]


def mean(values: Sequence[float]) -> float:
    vals = [v for v in values if not math.isnan(v)]
    return sum(vals) / len(vals) if vals else 0.0


def format_table(report: EvalReport, *, width: int = 34) -> str:
    """逐题明细表。给的不是"漂亮汇总"，而是能被人工复核的证据。"""
    lines = [
        f"{'题号':<5}{'类型':<12}{'recall':>7}{'扩展':>7}{'首中位':>8}  问题",
        "-" * 96,
    ]
    for q in report.scored:
        qtext = q.question.replace("\n", " ")[:width]
        mark = "" if q.recall >= 1.0 else ("  <== 未全中" if q.recall > 0 else "  <== 全丢")
        rank = str(q.first_hit_rank) if q.first_hit_rank else "-"
        lines.append(
            f"{q.qid:<5}{q.notes.replace('检索类型: ', ''):<12}"
            f"{q.recall:>7.2f}{q.expanded_recall:>7.2f}{rank:>8}  {qtext}{mark}"
        )
    return "\n".join(lines)

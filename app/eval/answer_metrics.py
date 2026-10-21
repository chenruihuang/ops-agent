"""答案侧评估：RAGAS 口径的**确定性实现**（不调用 LLM judge）。

为什么不是 `pip install ragas`
------------------------------
ragas 的核心指标（faithfulness / answer_relevancy）依赖 LLM 判定：把答案拆成
claims，再逐条问模型"这条能否被 context 支撑"。本项目当前环境装不上它
（沙箱 pip 离线），而**编一组看起来很专业的分数**比没有分数更糟 ——
所以这里只实现不需要模型的、可复现的部分，并把口径差异写在下面。

| 指标 | RAGAS 原义 | 本文件的实现 | 能不能互相比较 |
|---|---|---|---|
| context_recall | context 覆盖参考答案要点的比例（需 LLM 拆要点） | **文档级**：golden 文档是否被召回（与检索评估同口径） | 否 |
| context_precision | context 中相关内容排在前面的程度（需 LLM 判相关） | **文档级 AP**：golden 文档在召回序列里的平均精度 | 否 |
| faithfulness | 答案的每条 claim 是否被 context 支撑 | `anchor_hit_rate`：答案是否包含标注的关键事实（工单号/阈值/错误码） | **否**（方向相近，不是同一件事） |
| answer_relevancy | 答案与问题的语义相关度 | 未实现 | — |

结论：**这里的数字不能和论文/榜单上的 RAGAS 分数比**，只能与本项目自己的历史
同口径数字比（趋势可用，绝对值不可外推）。要真跑 RAGAS，装好库后换 judge 即可，
本文件的 `aggregate` 与产物格式可以直接复用。

为什么 anchor_hit_rate 用"标注的锚点"而不是从答案里抽数字
--------------------------------------------------------
从答案里抽数字再回查 context 只能证明"这个数字出现过"，不能证明它是**对的**：
模型把 5 QPS 答成 50 QPS，抽取式校验照样通过。用 `answer_anchors`（人工标注的
关键事实：`ORD-429-001`、`5`、`QPS`）才有判别力 —— 答错就是没命中。

边界匹配不是洁癖
----------------
`5` 会命中 `15`、`SRV-0088` 会命中 `SRV-00881`（本项目在文档 id 上踩过同一个坑：
`arch-service` 被 `svc-search-service` 的子串命中）。所以拉丁/数字锚点必须带
左右边界 `(?<![A-Za-z0-9-])…(?![A-Za-z0-9-])`，**不能用 `\\b`** —— `-` 在 Python
正则里是 `\\b` 的分隔符，`SRV-0088` 的 `0088` 前后都会被判成词边界。
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping, Sequence

# 中文标点/全角数字统一：模型偶尔会用全角输出，不归一会造成假阴性
_FULLWIDTH = str.maketrans("０１２３４５６７８９％", "0123456789%")

_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3})")
_SPACES = re.compile(r"[\s\u3000]+")

# 明确"没查到"的措辞。注意**不能**只看"没有"：`没有超时` 是正常答案。
_ABSTAIN_MARKERS = (
    "资料未涉及", "知识库未涉及", "未提及", "没有找到", "未找到", "未收录",
    "无法确定", "没有相关资料", "暂无相关", "不在知识库中", "未提供相关信息",
)


def normalize(text: str) -> str:
    """归一化后比较：全角→半角、数字千分位、大小写、连续空白。"""
    if not text:
        return ""
    out = text.translate(_FULLWIDTH)
    out = _THOUSANDS.sub("", out)
    return _SPACES.sub(" ", out).strip().lower()


def _anchor_pattern(anchor: str) -> re.Pattern[str]:
    """锚点正则。拉丁/数字边界必须显式写，`\\b` 对 `-` 无效（见模块注释）。"""
    escaped = re.escape(normalize(anchor))
    return re.compile(rf"(?<![a-z0-9-]){escaped}(?![a-z0-9-])")


def contains_anchor(text: str, anchor: str) -> bool:
    if not text or not anchor:
        return False
    return bool(_anchor_pattern(anchor).search(normalize(text)))


def anchor_hit_rate(answer: str, anchors: Sequence[str]) -> tuple[float | None, list[str], list[str]]:
    """答案命中了多少标注锚点。返回 `(命中率, 命中列表, 漏掉列表)`。

    没有锚点的题返回 `None`（不是 0）—— 把"无法评分"和"全错"混成一个数，
    均值会被无声地拉低，这是评估里最常见的假信号来源。
    """
    items = [a for a in (anchors or []) if str(a).strip()]
    if not items:
        return None, [], []
    hit = [a for a in items if contains_anchor(answer, str(a))]
    missed = [a for a in items if a not in hit]
    return len(hit) / len(items), hit, missed


def doc_recall(golden: Iterable[str], retrieved: Sequence[str]) -> float | None:
    """文档级召回：golden 里有多少篇被召回到（与检索评估同一口径）。"""
    gold = {str(g) for g in (golden or []) if str(g).strip()}
    if not gold:
        return None
    got = {str(r) for r in (retrieved or [])}
    return len(gold & got) / len(gold)


def doc_precision(golden: Iterable[str], retrieved: Sequence[str]) -> float | None:
    """文档级 AP（average precision）：相关文档排得越靠前分越高。

    这是 RAGAS `context_precision` 的**文档级近似**：RAGAS 在句/块级判相关，
    我们只有文档级标注。口径小一号，但方向一致（把无关文档排前面就是会掉分）。

    **必须先按首次出现去重**：同一篇文档的多个块会重复出现在 sources 里
    （本项目每篇最多 3 块），不去重时同一篇会被算成多次命中 ——
    实测 AP 会算出 1.31、2.0 这种 >1 的值。一个越界的精度指标比没有指标更糟：
    它看起来"很好"，但含义已经不成立。
    """
    gold = {str(g) for g in (golden or []) if str(g).strip()}
    seen: list[str] = []
    for r in (retrieved or []):
        doc = str(r)
        if doc not in seen:
            seen.append(doc)
    if not gold or not seen:
        return None
    hits = 0
    total = 0.0
    for i, doc in enumerate(seen, start=1):
        if doc in gold:
            hits += 1
            total += hits / i
    if hits == 0:
        return 0.0
    return total / len(gold)


def abstains(answer: str) -> bool:
    text = normalize(answer)
    return any(marker in text for marker in _ABSTAIN_MARKERS)


def _percentile(values: Sequence[float], q: float) -> float:
    """最近秩（nearest-rank）分位：取第 ceil(q·n) 小的值。

    刻意不用插值：延迟的 p95 应该是一个**真实观测到的值**，
    插值会造出一个谁都没经历过的数（两题 100/300ms 插出 290ms 没有意义）。
    注意 p50 对两个数是**下侧**（100ms）：ceil(0.5×2)=1 → 第 1 个。测试钉住了这一点。
    """
    ordered = sorted(values)
    n = len(ordered)
    idx = max(0, min(n - 1, math.ceil(q * n) - 1))
    return ordered[idx]


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """把逐题结果聚合成一份报告。

    均值只对**有值**的题求平均，并分别报分母 —— `questions_scored` 与
    `questions_skipped` 是这份报告的可信度前提：48 题报 0.9 和 12 题报 0.9
    完全是两件事。
    """
    def mean_of(key: str) -> tuple[float | None, int]:
        vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        if not vals:
            return None, 0
        return sum(vals) / len(vals), len(vals)

    metrics: dict[str, Any] = {}
    for key in ("context_recall", "context_precision", "anchor_hit_rate"):
        value, n = mean_of(key)
        metrics[key] = None if value is None else round(value, 4)
        metrics[f"{key}_n"] = n

    answered = [r for r in rows if r.get("answer")]
    metrics["answers"] = len(answered)
    # 分母是**全部尝试过的题**，不是"有答案的题"：整轮失败（超时/断流）的题
    # 恰恰没有答案，只按有答案的题算会把故障率算成 0 —— 这种假绿比没指标更危险。
    metrics["degraded_rate"] = round(
        sum(1 for r in rows if r.get("degraded")) / len(rows), 4
    ) if rows else None

    # 弃答率只对**该弃答的题**有意义：
    #   intent 是 rag/hybrid（知识问题）且没有 golden（资料里确实没有答案）。
    # 工单/资产这类 tool 题的"没有 golden"是因为答案来自实时系统，不是资料缺失；
    # 闲聊题也不该要求弃答。早期把所有无 golden 的题都算进去，
    # 分母混进 tool/chitchat，指标变成 0.25 这种无法解释的数。
    should_abstain = [
        r for r in rows
        if not r.get("golden_doc_ids") and r.get("intent") in ("rag", "hybrid")
    ]
    metrics["unanswerable_n"] = len(should_abstain)
    metrics["abstention_ok_rate"] = round(
        sum(1 for r in should_abstain if r.get("abstained")) / len(should_abstain), 4
    ) if should_abstain else None

    lat = [r["total_ms"] for r in answered if isinstance(r.get("total_ms"), (int, float))]
    if lat:
        metrics["total_ms_p50"] = round(_percentile(lat, 0.50), 1)
        metrics["total_ms_p95"] = round(_percentile(lat, 0.95), 1)

    ttft = [r["ttft_ms"] for r in answered if isinstance(r.get("ttft_ms"), (int, float))]
    if ttft:
        metrics["ttft_ms_p50"] = round(_percentile(ttft, 0.50), 1)

    metrics["questions_total"] = len(rows)
    return metrics

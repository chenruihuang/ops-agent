"""工具规划：从「路由决策 + 问题原文」推导出该调哪些工具。

为什么单独成模块，而不是塞进 provider
------------------------------------
规划是**纯函数**（输入字符串，输出调用列表），provider 是 IO。
分开之后，规划逻辑可以在没有 mock 数据、没有 MCP、没有 Redis 的情况下
被穷举测试 —— 而它恰恰是最容易出错的一环（多调/漏调工具）。

规划原则：宁多勿漏
------------------
一个多余的调用成本极低（本地/mock 微秒级，MCP 也就几十毫秒，且有 30s 缓存），
漏调的成本是**答案直接缺失**。所以只要能构造出合法参数，就发出去；
provider 侧对查不到的情况返回空结果即可，不需要在规划阶段"猜准"。

但有一条硬边界：**参数抽不出来就不发**。
不构造空参数去查（`query_ticket_by_no("")` 会返回一堆无关数据，
比查不到更糟 —— 它会让模型拿着无关工单编答案）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.pipeline.intent import RouteDecision, replicas_of

# 句首的礼貌/衔接词（可连续出现）
_LEAD = re.compile(
    r"^(?:(?:帮我|给我|麻烦|请|另外|然后|还有|以及|顺便|把|将|再|也|就|那个|这个)\s*)+"
)
# 句首/句尾的查询动作短语
_ACTION = re.compile(
    r"^(?:查一下|查下|查查|查询一下|查询|看一下|看下|看看|列出|列一下|找一下|搜一下|拉一下|调一下)"
)
_ACTION_TAIL = re.compile(
    r"(?:给我|帮我)?(?:看一下|看下|看看|查一下|查下|查查|列出|列一下|找一下|搜一下|拉一下|调一下)"
    r"(?:好|吧)?$"
)
# 疑问尾
_TAIL_Q = re.compile(
    r"(?:是|有|在)?(?:什么|哪些|多少|怎么样|怎么办|如何|怎么(?:样|办)?|"
    r"是什么|有哪些|是多少|"
    r"处理到哪一步|到哪一步|进度如何|进展如何|"
    r"怎么处理|如何处理|怎么解决|如何解决|"
    r"的?情况|的?状态|的?进度|的?进展|的?记录|的?列表)+$"
)
# 句中功能词：去掉后剩下的才是"内容词"。
#
# 「数据」刻意**不在**这里 —— 它在「数据库连接」里是内容的一部分，
# 去掉会得到「库连接」这种搜不到东西的碎片（实测踩过）。
# 功能词表的每一条都要问一遍："它会不会是某个专有名词的内部成分？"
_FILLER = re.compile(
    r"(?:相关的?|有关的?|历史|以往|之前|上次|那次|曾经的?|"
    r"工单|告警|资产|记录|列表|情况|进度|进展|信息|"
    r"最后|现在|当前|目前|今天|昨天|是否|有没有|"
    r"的|了|吗|呢|吧|啊|也|都|还|就|请|这|那|个|条|下|一下|所有|全部)"
)
# 内容词：中文连续串（≥2 字）与英文/数字串
_CN_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
_ASCII_RUN = re.compile(r"[a-zA-Z][a-zA-Z0-9_]*")

_SENT_SPLIT = re.compile(r"[，,。.！!？?；;、\n]+")


@dataclass(slots=True)
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"{self.name}({', '.join(f'{k}={v!r}' for k, v in self.args.items())})"


# 写操作里"原因"字段的清洗：去掉 shell 元字符与换行。
#
# 为什么清洗而不是直接拒绝：原因来自**用户原话**，里面出现分号是正常的
# （"发布后连接泄漏；先重启"）。把合法输入判成非法，用户只会学会"少说话"，
# 而审计日志要的恰恰是他多说。所以这里做**收敛**（只留安全字符），
# 让它在策略层一定通过 —— 策略层不为"用户语气"服务，只为"参数形状"服务。
_REASON_CLEAN = re.compile(r"[;|&$`<>\\\n\r\t]")

_RESTART_HINT = re.compile(r"(?:重启|重新启动|滚动重启)")
_SCALE_HINT = re.compile(r"(?:扩容|缩容|扩到|缩到|副本)")


def _reason_of(query: str, limit: int = 60) -> str:
    text = _REASON_CLEAN.sub(" ", query or "")
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _write_calls(service: str, query: str) -> list[ToolCall]:
    """从「服务名 + 动作」推出写工具调用。

    抽不出关键参数就**不规划**：`scale_service` 没有目标副本数时宁可不执行，
    也不拿默认值去改线上 —— "默认 3 副本"这种善意猜测在容量场景里就是事故。
    """
    calls: list[ToolCall] = []

    if _SCALE_HINT.search(query):
        target = replicas_of(query)
        if target is not None:
            calls.append(ToolCall("scale_service",
                                  {"service": service, "replicas": target}))

    if _RESTART_HINT.search(query):
        calls.append(ToolCall("restart_service",
                              {"service": service, "reason": _reason_of(query)}))

    return calls


def extract_keywords(query: str, *, limit: int = 3) -> list[str]:
    """从中文问句里抽"内容词"，用于关键词检索工单。

    这不是分词，也不需要是 —— 它只要做到一件事：
    把「另外把数据库连接相关的历史工单也给我看一下」压成「数据库连接」。
    去掉动作、疑问、功能词之后剩下的中文串，就是这个句子的检索意图。

    按出现顺序保留、按长度降序优先 —— 长的串信息量更大
    （"数据库连接"比"连接"更能定位工单）。
    """
    out: list[str] = []
    seen: set[str] = set()

    for sentence in _SENT_SPLIT.split(query or ""):
        text = sentence.strip()
        if not text:
            continue
        prev = None
        while prev != text:                      # 反复剥离，处理"另外把…"这类叠加前缀
            prev = text
            text = _LEAD.sub("", text)
            text = _ACTION.sub("", text)
            text = _ACTION_TAIL.sub("", text)
            text = _TAIL_Q.sub("", text)
        text = text.strip()
        if not text:
            continue

        stripped = _FILLER.sub("", text)
        candidates = _CN_RUN.findall(stripped) + [
            m.lower() for m in _ASCII_RUN.findall(stripped)
        ]
        for cand in sorted(candidates, key=len, reverse=True):
            if cand and cand not in seen:
                seen.add(cand)
                out.append(cand)

    return out[:limit]


def plan(decision: RouteDecision, query: str) -> list[ToolCall]:
    """规划工具调用。

    顺序是**确定性的**：先精确号（工单号/资产号），再维度查询，最后写操作。
    确定性对测试很重要 —— 否则没法断言"这题应该调这几个工具"。

    写操作放**最后**：先读现场（告警/工单），再改。哪怕两者在 node_tools 里
    是并行发出的，结果列表的顺序也保持"先看后动"，事件流读起来才是人话。
    """
    if not decision.needs_tool:
        return []

    ents = decision.entities
    calls: list[ToolCall] = []

    for no in ents.ticket_nos:
        calls.append(ToolCall("query_ticket_by_no", {"ticket_no": no}))

    for code in ents.asset_codes:
        calls.append(ToolCall("get_asset_by_code", {"asset_code": code}))

    # 服务维度：抽到服务名时，告警与工单一起查（宁多勿漏）
    for svc in ents.services:
        calls.append(ToolCall("query_alerts_by_service", {"service": svc}))
        calls.append(ToolCall("query_ticket_by_service", {"service": svc}))

    # 兜底：既没号也没服务名时按关键词捞，否则实时路等于空转
    if not calls:
        keywords = extract_keywords(query)
        if keywords:
            calls.append(ToolCall("query_ticket_by_keyword", {"keyword": " ".join(keywords)}))

    # 写操作最后追加。`needs_write` 由路由给出（唯一的"想干什么"判定点），
    # planner 不自己再解析一遍消息 —— 两处判定必然漂。
    if getattr(decision, "needs_write", False):
        for svc in ents.services:
            calls.extend(_write_calls(svc, query))

    return calls


def plan_summary(calls: list[ToolCall]) -> str:
    return " → ".join(str(c) for c in calls) if calls else "（无需工具）"
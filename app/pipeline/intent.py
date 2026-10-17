"""意图路由：决定一个问题该走哪条链路。

四类意图
--------
    rag       知识问答 —— 检索手册/规范 → 重排 → 生成
    tool      实时数据 —— 查工单/资产/告警，**不该**去向量库里找
    hybrid    两者都要 —— "连接数超过多少告警？另外把相关历史工单也给我看下"
    chitchat  闲聊 —— 不检索、不调工具，直接答

为什么规则优先，而不是一上来就用 LLM 分类
----------------------------------------
1. **成本与延迟**：LLM 分类要 200~800ms 且每次请求都花钱。而缓存命中路径
   （TTFT 毫秒级）如果先插一次 LLM 分类，整个"缓存省时间"的叙事就崩了。
2. **可解释**：规则能给出"命中了哪个信号"，这个理由能原样写进 SSE 事件给用户看。
   LLM 只能给一个概率和一个编造的理由，排查时没用。
3. **可回归**：规则可以离线跑 52 条标注算准确率（见 `evaluate_routing`）；
   纯 LLM 分类的准确率会随模型版本漂移，而且每次评估都要花钱。

LLM 兜底不是不做，而是**只在低置信度时做**（`intent_llm_fallback` 开关，默认关）。
先有规则基线，才知道 LLM 兜底到底值不值那 500ms —— 顺序不能反。

判定策略：默认 rag，只有命中"实时信号"才可能变成 tool / hybrid
------------------------------------------------------------
这是刻意的不对称。误判两个方向的代价完全不同：
  - rag 题被判成 tool → 回答了实时数据、漏了知识 → 用户拿到一个残缺答案
  - tool 题被判成 rag → 向量库里翻不到，返回"资料未涉及" → 用户明确知道没查到
后者是可恢复的（用户会换个问法），前者是静默错误的。
所以宁可漏判 tool，不要误判 tool。

顺带一提：**这里的规则表不是拍脑袋来的**，是拿 52 条标注反复跑出来的 ——
`python -m scripts.check_intent` 会打印准确率与混淆矩阵。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

INTENTS = ("rag", "tool", "hybrid", "chitchat")

# =====================================================================
# 实体抽取
# =====================================================================
# 工单号：INC-2026-0412
TICKET_NO = re.compile(r"\bINC-\d{4}-\d{3,4}\b", re.IGNORECASE)
# 资产编码：SRV-0088
ASSET_CODE = re.compile(r"\bSRV-\d{4}\b", re.IGNORECASE)
# 服务名：inventory-service / payment-gateway / risk-engine
SERVICE_NAME = re.compile(
    r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)*-(?:service|gateway|center|engine|job|cluster)\b",
    re.IGNORECASE,
)
# 错误码：ORD-429-001（用于判断"这题在问知识还是在查实时状态"）
ERROR_CODE = re.compile(r"\b[A-Z]{2,5}-\d{3}-\d{3}\b")


# =====================================================================
# 信号词表
# =====================================================================
# 查询动作：明确要求"去取一份数据"，而不是"解释一个概念"。
#
# 这里踩过一个值得记下来的坑：最初把裸的「查询」放进来了，
# 于是「为什么说连接池配置和**慢查询**这两个问题单独存在都不会造成 P0」
# 被判成了实时查询 —— 而它是个彻头彻尾的知识题。
# 教训是：**动词必须带对象**，否则行业专有名词会把子串喂给正则。
# 所以下面分成两组：第一组是不会被专有名词咬到的强动作短语，
# 第二组是"动词 + 明确的实时对象"组合。
LIVE_VERBS = re.compile(
    r"(?:帮我|给我|麻烦|请)?(?:"
    r"查一下|查下|查查|查询一下|"
    r"看一下|看下|看看|给我看|给我查|帮我看看|帮我查查|"
    r"调一下|拉一下|列一下|列出|找一下|找出|搜一下|"
    r"查询(?:工单|资产|告警|日志|记录|进度|状态|列表)|"
    r"(?:查|看|调)(?:一下)?(?:工单|资产|告警)"
    r")"
)
# 实时时态：把问题钉在"此刻"
LIVE_TENSE = re.compile(
    r"(?:现在|当前|目前|此刻|这会儿|今天|昨天|今早|本周|最近|最新的?|实时)"
)
# 历史指代 + 工单/事故类对象 —— 组合出现才算实时查询
HISTORY_REF = re.compile(r"(?:之前|上次|以往|历史|那次|曾经|过去|先前)")
INCIDENT_OBJECT = re.compile(r"(?:工单|事故|事件|复盘|incident|ticket|case)", re.IGNORECASE)
# 进度类诉求：问"办到哪一步了"，本质上是在查实时状态而不是问概念
PROGRESS = re.compile(
    r"(?:处理到哪|进展|进度|到哪一步|处理得怎么|怎么样了|"
    r"解决了没|解决了吗|关闭了没|结单了没|谁在处理|在谁手上|负责人是谁)"
)
# 实时查询对象。
#
# 注意这里**刻意不含「工单」「告警」「集群状态」**：它们经常是"被讨论的概念"
# 而不是"要取的数据"。
#   「P1 告警的响应时限是多少」        → 知识题，不该触发实时路
#   「监控大盘…同时告警，这说明什么」  → 知识题，同上
# 所以这里只留下几乎不可能被当作概念讨论的实体名词。
REALTIME_OBJECT = re.compile(
    r"(?:服务器|机器|节点|主机|实例|物理机|虚拟机|部署状态)",
    re.IGNORECASE,
)

# 知识诉求（强信号）：问定义 / 规则 / 原因 / 步骤 / 数值。
#
# 「哪些」「是什么」「什么样的」**刻意不在这里** —— 它们既能问知识也能问实时
# （"这台机器跑着哪些服务"是实时，"继承人包括哪些人"是知识），
# 当作知识信号会让 tool 题大量误判成 hybrid。
# 真正有区分力的是"规则/阈值/为什么/步骤"这类**只能在知识库里找答案**的词。
# "多少"保留：问阈值的场景远多于问实时计数的场景（实测 52 条里 2:0）。
KNOWLEDGE = re.compile(
    r"(?:为什么|原因|怎么|如何|步骤|流程|规则|阈值|策略|规范|标准|要求|区别|对比|"
    r"定义|含义|解释|原理|机制|多少|多久|多长时间|几[条个倍秒]|"
    r"算(?:是|作)|称为|指的是)"
)

# 闲聊：问候 / 身份 / 致谢。
#
# **必须按分句匹配，不能整串匹配** ——
# 「你好，你是谁？你能帮我做什么？」有三个分句，整串正则会漏掉它，
# 于是这条最典型的闲聊题被判成了 rag（实测踩过）。
# 规则是：切分后**每个分句都是闲聊短语**才算闲聊；任一分句有业务内容就不是。
CHITCHAT_PHRASE = re.compile(
    r"(?:"
    r"你好|您好|哈喽|hi|hello|hey|在吗|在么|早上好|中午好|晚上好|"
    r"你是谁|你是什么|你叫什么|你能做什么|你能帮我做什么|你会什么|你会做什么|"
    r"你能干什么|你能帮我吗|你能帮到我什么|介绍一下你|介绍下你|自我介绍一下|"
    r"谢(?:谢|谢你|谢您)|感谢|多谢|辛苦|再见|拜拜|"
    r"嗯+|好的?|行|收到|ok|thanks"
    r")",
    re.IGNORECASE,
)
CHITCHAT_SPLIT = re.compile(r"[，,。.！!？?；;、：:\s~～]+")
CHITCHAT_MAX_CHARS = 60
CHITCHAT_MAX_PARTS = 5


def _is_pure_chitchat(text: str) -> bool:
    """整句由闲聊短语组成才算闲聊。

    这一层判断是硬性的：宁可把闲聊题误判成 rag（用户拿到"资料未涉及"会重问），
    也不要把业务题误判成闲聊（用户拿到一句寒暄，完全不知道为什么答非所问）。
    """
    if len(text) > CHITCHAT_MAX_CHARS:
        return False
    parts = [p for p in CHITCHAT_SPLIT.split(text.strip()) if p]
    if not parts or len(parts) > CHITCHAT_MAX_PARTS:
        return False
    return all(CHITCHAT_PHRASE.fullmatch(p) for p in parts)


@dataclass(slots=True)
class Entities:
    """从问题里抽出来的可执行实体。抽不出来就是空列表，不做猜测。"""

    ticket_nos: list[str] = field(default_factory=list)
    asset_codes: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    error_codes: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.ticket_nos or self.asset_codes or self.services or self.error_codes)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "ticket_nos": self.ticket_nos,
            "asset_codes": self.asset_codes,
            "services": self.services,
            "error_codes": self.error_codes,
        }


def extract_entities(query: str) -> Entities:
    """抽实体。全部按**确定性正则**，不用模型 —— 抽不出来就承认抽不出来。"""
    text = query or ""
    return Entities(
        ticket_nos=_uniq(m.group(0).upper() for m in TICKET_NO.finditer(text)),
        asset_codes=_uniq(m.group(0).upper() for m in ASSET_CODE.finditer(text)),
        services=_uniq(m.group(0).lower() for m in SERVICE_NAME.finditer(text)),
        error_codes=_uniq(m.group(0).upper() for m in ERROR_CODE.finditer(text)),
    )


def _uniq(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


# =====================================================================
# 路由决策
# =====================================================================
@dataclass(slots=True)
class RouteDecision:
    intent: str
    confidence: float
    reason: str
    signals: list[str] = field(default_factory=list)
    entities: Entities = field(default_factory=Entities)
    needs_rag: bool = True
    needs_tool: bool = False

    def as_event(self) -> dict[str, Any]:
        """给 SSE 用的事件负载。

        `reason` 与 `signals` 是刻意暴露给用户的 —— 路由决策不该是黑盒。
        演示时这一行比任何 PPT 都有说明力：屏幕上直接显示
        "为什么这题走了工具链路"。
        """
        return {
            "type": "route",
            "intent": self.intent,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "signals": self.signals,
            "entities": self.entities.as_dict(),
            "needs_rag": self.needs_rag,
            "needs_tool": self.needs_tool,
        }


def decide(query: str) -> RouteDecision:
    """规则路由。纯函数、零依赖、可单测 —— 这是它能被评估的前提。"""
    text = (query or "").strip()
    entities = extract_entities(text)

    if not text:
        return RouteDecision("chitchat", 0.9, "空问题", ["empty"], entities, False, False)

    # ---- 1. 闲聊：必须整句都是闲聊，不能只是"包含问候" ----
    # 「你好，工单 INC-2026-0412 处理到哪了」不该走闲聊 —— 实体优先于寒暄。
    if _is_pure_chitchat(text) and not entities.any:
        return RouteDecision(
            "chitchat", 0.9, "整句由问候/身份类短语组成，无业务实体",
            ["chitchat_pure"], entities, False, False,
        )
    if entities.any and _is_pure_chitchat(text):
        # 罕见但存在："你好，SRV-0088" —— 有实体就不当闲聊，往下走实时路
        pass

    # ---- 2. 实时信号 ----
    signals: list[str] = []
    live = False

    if entities.ticket_nos:
        live = True
        signals.append(f"ticket_no:{entities.ticket_nos[0]}")
    if entities.asset_codes:
        live = True
        signals.append(f"asset_code:{entities.asset_codes[0]}")

    if LIVE_VERBS.search(text):
        live = True
        signals.append("live_verb")
    if LIVE_TENSE.search(text) and REALTIME_OBJECT.search(text):
        live = True
        signals.append("live_tense+object")
    # 抽到服务名 + 时态词：同样是实时查询信号。
    # 这条规则是为**泛化**加的，不是为了修某道标注题 ——
    # 「order-service 现在有没有未关闭的告警」没有动词也没有实时对象名词，
    # 但"服务名 + 此刻"已经足够说明用户在问实时状态。
    # 加之前在 52 条标注上验证过零影响（只命中 q-36，而它本来就判对）。
    if entities.services and LIVE_TENSE.search(text):
        live = True
        signals.append(f"service+tense:{entities.services[0]}")
    if HISTORY_REF.search(text) and INCIDENT_OBJECT.search(text):
        live = True
        signals.append("history_ref+incident")
    if PROGRESS.search(text):
        live = True
        signals.append("progress")

    # ---- 3. 知识信号 ----
    knowledge = bool(KNOWLEDGE.search(text))
    # 错误码是知识题的强特征（"ORD-429-001 是什么含义"），且不构成实时信号
    if entities.error_codes:
        signals.append(f"error_code:{entities.error_codes[0]}")

    if not live:
        confidence = 0.75 if knowledge else 0.6
        reason = "问概念/规则/数值，无实时查询信号" if knowledge else "无实时查询信号，默认走知识检索"
        return RouteDecision("rag", confidence, reason, signals or ["default_rag"], entities, True, False)

    if knowledge:
        return RouteDecision(
            "hybrid", 0.85, "既问知识又问实时数据，需两路同时命中",
            signals + ["knowledge"], entities, True, True,
        )
    return RouteDecision(
        "tool", 0.8, "命中实时查询信号，答案在工单/资产数据而非知识库里",
        signals, entities, False, True,
    )


# =====================================================================
# 路由评估 —— 口径与服务端同一份
# =====================================================================
@dataclass(slots=True)
class RoutingScore:
    total: int
    correct: int
    accuracy: float
    confusion: dict[str, dict[str, int]]
    errors: list[tuple[int, str, str, str]]   # (qid, 问题, 期望, 实际)


def evaluate_routing(
    questions: Sequence[dict[str, Any]],
    *,
    include: Iterable[str] | None = None,
) -> RoutingScore:
    """用标注集评估路由。

    `include` 用于把范围收窄到某些意图 —— 因为 52 条里 rag 有 45 条，
    只看总准确率会被 rag 的占比掩盖（全判 rag 也能拿 86%），
    必须同时看混淆矩阵和"非 rag 题的召回"。
    """
    include_set = set(include) if include else None
    confusion: dict[str, dict[str, int]] = {}
    total = correct = 0
    errors: list[tuple[int, str, str, str]] = []

    for q in questions:
        expected = str(q.get("intent") or "rag")
        if include_set and expected not in include_set:
            continue
        question = str(q.get("question") or "")
        actual = decide(question).intent
        total += 1
        correct += int(actual == expected)
        confusion.setdefault(expected, {})
        confusion[expected][actual] = confusion[expected].get(actual, 0) + 1
        if actual != expected:
            errors.append((int(q.get("id") or 0), question, expected, actual))

    return RoutingScore(
        total=total,
        correct=correct,
        accuracy=(correct / total) if total else 0.0,
        confusion=confusion,
        errors=errors,
    )


def format_routing_report(score: RoutingScore) -> str:
    """把混淆矩阵画成人能读的表。错误样例必须列出来 —— 只报准确率无法定位问题。"""
    labels = [i for i in INTENTS if i in score.confusion] or list(INTENTS)
    header = f"{'期望\\实际':<10}" + "".join(f"{a:>10}" for a in labels)
    lines = [
        f"题数 {score.total} · 命中 {score.correct} · 准确率 {score.accuracy:.4f}",
        "-" * 60,
        header,
    ]
    for exp in labels:
        row = score.confusion.get(exp, {})
        lines.append(f"{exp:<10}" + "".join(f"{row.get(act, 0):>10}" for act in labels))
    if score.errors:
        lines.append("")
        lines.append("误判样例：")
        for qid, question, exp, act in score.errors:
            lines.append(f"  q-{qid:<3} 期望 {exp:<8} 实际 {act:<8} {question[:46]}")
    return "\n".join(lines)


def intent_stats(questions: Sequence[dict[str, Any]]) -> dict[str, int]:
    """标注集的意图分布 —— 极不平衡时必须显式看到，否则准确率会骗人。"""
    out: dict[str, int] = {}
    for q in questions:
        key = str(q.get("intent") or "rag")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))

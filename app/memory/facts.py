"""长期事实抽取（规则版 v1）。

为什么先用规则而不是 LLM 抽取
------------------------------
事实抽取的失败模式很刁钻：LLM 抽错一条事实（比如把
"我们不用 MySQL" 抽成 "用 MySQL"），它会在**后续每一轮**都被当作背景
注入 prompt —— 一个错误事实的半衰期是整个会话，而且用户看不到。
规则版虽然覆盖率低，但每条抽取都可解释、可单测、可回滚。
LLM 抽取（带置信度 + 人工确认）列为 backlog，没想清楚护栏前不上。

只抽「稳定属性」，不抽「瞬时状态」
--------------------------------
"我是 DBA"、"生产用的是 MySQL 8.0" —— 这些在会话内不会变，值得存。
"现在连接池满了" —— 这是本轮问题的内容，LLM 本来就看得到，存了反而
在下一轮变成过期快照（和"实时数据不进向量库"是同一条原则）。
"""

from __future__ import annotations

import re

# 每条消息最多抽出几条事实：防止一句长文本把事实库灌爆
MAX_FACTS_PER_MESSAGE = 3
# 单条事实长度上限：事实是"背景"，不是"摘录"
MAX_FACT_LEN = 60

_PATTERNS = [
    # "我们生产用的是 MySQL 8.0" / "公司目前使用 Kubernetes" / "线上用的是 Redis"
    # 主体词、环境词、时间词都允许省略 —— 真实语句不会按模板说话
    re.compile(
        r"(?:我们|公司)?(?:生产环境|线上环境|生产|线上)?(?:目前|现在)?"
        r"(?:用的?是|使用|部署的是)[^，。；,;！!？?\n]{1,40}"
    ),
    # "我是 DBA" / "我是数据库工程师"
    re.compile(
        r"我是[^，。；,;！!？?\n]{0,16}?"
        r"(?:工程师|管理员|运维|DBA|开发|架构师|负责人|值班)"
    ),
    # "我们的数据保留 30 天" / "我的工号是 12345"
    re.compile(
        r"(?:我们的|我的)[^，。；,;！!？?\n]{1,12}"
        r"(?:是|为|保留|限制在)[^，。；,;！!？?\n]{1,30}"
    ),
]


def extract_facts(text: str) -> list[str]:
    """从一句话里抽「稳定属性」。纯函数、零依赖、可单测。

    过滤规则：
    - 长度截到 MAX_FACT_LEN（背景不需要全文）
    - 含否定词的片段整条丢弃（"我们不用 MySQL" 存进去比不存更糟）
    - 同一条消息内去重
    """
    t = (text or "").strip()
    if not t:
        return []
    facts: list[str] = []
    for pat in _PATTERNS:
        for m in pat.finditer(t):
            fact = m.group(0).strip()[:MAX_FACT_LEN]
            # 否定护栏：抽出来的是反义事实时，宁可不要
            if re.search(r"不用|不是|没有|不再|别用|禁用|放弃", fact):
                continue
            if fact and fact not in facts:
                facts.append(fact)
            if len(facts) >= MAX_FACTS_PER_MESSAGE:
                return facts
    return facts

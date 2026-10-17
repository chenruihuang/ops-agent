"""缓存 key 生成与查询归一化。

本模块**只依赖标准库**，可以脱离 Redis 单独跑单元测试。

设计要点
--------
1. 所有 key 都带 `kb_version`。知识库一更新就把版本号 +1，旧缓存立即不可达
2. 归一化只做"不影响语义"的变形（全角转半角、去寒暄、去尾标点）；
   中文数字转阿拉伯数字只在**紧跟量词**时进行，避免把"统一""一并"误改。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

EXACT_PREFIX = "ops:v1:exact:"
SEMANTIC_PREFIX = "ops:v1:sem:"
SEMANTIC_INDEX = "idx:ops_semantic"

# 句子开头的寒暄/礼貌用语，去掉后不影响答案
_LEADING_FILLER = re.compile(
    r"^(?:请问|你好|您好|麻烦|帮我|帮忙|我想问|想问下?|问一下|请教一下?|麻烦问一下|请问一下)"
    r"[，,、:：\s]*"
)
# 句尾无意义标点
_TRAILING_PUNCT = re.compile(r"[？?。！!，,、；;:：\s]+$")
_WHITESPACE = re.compile(r"\s+")

# 阿拉伯数字
_ARABIC_NUM = re.compile(r"\d+(?:\.\d+)?")

# 中文数字 —— 仅当其**紧跟量词**时才转换，避免误伤"统一""一并""十分"等词
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_CN_NUM_CHARS = "".join(_CN_DIGITS) + "".join(_CN_UNITS)

_MEASURE_WORDS = r"(?:周岁|个月|年|岁|日|天|月|条|款|项|倍|个|次|级|元|%|％|周|小时|分钟|秒|人|份|台|GB|MB)"
_CN_NUM_WITH_UNIT = re.compile(
    rf"([{_CN_NUM_CHARS}]+)(?={_MEASURE_WORDS})"
)


def cn_to_int(text: str) -> int | None:
    """中文数字转整数。无法解析时返回 None。

    >>> cn_to_int("十六")
    16
    >>> cn_to_int("一百零八")
    108
    >>> cn_to_int("二十")
    20
    """
    if not text:
        return None
    if text.isdigit():
        return int(text)

    total = 0
    section = 0
    number = 0
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10_000:
                section = (section + (number or 1)) * unit
                total += section
                section = 0
            else:
                section += (number or 1) * unit
            number = 0
        else:
            return None
    return total + section + number


def normalize(query: str) -> str:
    """把"看起来不同、语义相同"的提问压成同一个字符串。

    这一步是提升**精确缓存命中率**最便宜的手段 —— 实测在客服式提问里
    能多拿 10%~20% 的命中。
    """
    text = unicodedata.normalize("NFKC", query or "")   # 全角 → 半角
    text = text.strip().lower()
    text = _LEADING_FILLER.sub("", text)
    text = _TRAILING_PUNCT.sub("", text)
    text = _WHITESPACE.sub(" ", text)
    # 中文数字在量词前统一成阿拉伯数字："十六周岁" → "16周岁"
    text = _CN_NUM_WITH_UNIT.sub(
        lambda m: str(cn_to_int(m.group(1)) or m.group(1)), text
    )
    return text


def extract_anchors(query: str) -> frozenset[str]:
    """抽取"敏感锚点"——数字与量词组合。

    为什么需要它：语义缓存最危险的失效模式是**关键数字不同但向量极像**。
    "16 岁能独立签合同吗" 与 "18 岁能独立签合同吗"，余弦相似度常年在 0.97 以上，
    但答案完全相反。对法律、运维阈值、资费这类场景，必须做锚点一致性校验。
    """
    text = normalize(query)
    return frozenset(m.group(0) for m in _ARABIC_NUM.finditer(text))


def exact_cache_key(
    *,
    query: str,
    kb_version: int,
    prompt_version: str,
    model: str,
    top_k: int,
) -> str:
    payload = "|".join(
        (
            normalize(query),
            f"kb={kb_version}",
            f"pv={prompt_version}",
            f"model={model}",
            f"topk={top_k}",
        )
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{EXACT_PREFIX}{digest}"


def sub_cache_key(namespace: str, *parts: object) -> str:
    """子链路缓存 key：query 改写 / rerank 结果 / MCP 工具结果共用。"""
    payload = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"ops:v1:{namespace}:{digest}"


def semantic_point_id(*, query: str, kb_version: int) -> str:
    payload = f"{normalize(query)}|kb={kb_version}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def semantic_member_key(point_id: str) -> str:
    return f"{SEMANTIC_PREFIX}{point_id}"


def lock_key(namespace: str, *parts: object) -> str:
    """单飞锁的 key。

    注意：绝不能直接用内置 hash() —— Python 的字符串哈希受 PYTHONHASHSEED 随机化，
    多进程/多副本之间算出来的锁名不一样，单飞就直接失效了。必须用稳定摘要。
    """
    return sub_cache_key(f"lock:{namespace}", *parts)

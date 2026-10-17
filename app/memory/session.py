"""会话记忆 + 长期事实的存储层。

设计约束
--------
1. **状态外置**：进程内不存任何会话状态，多副本横向扩展时
   任意副本都能接任意请求 —— 会话状态全在 Redis。
2. **永不抛异常**：记忆是"锦上添花"的依赖，它挂掉的正确姿态是
   降级（返回空上下文 / 返回错误串），而不是把问答请求打挂。
   错误串形如 `memory:ConnectionError`，调用方把它记进 degraded，
   SSE 里就能看到"会话记忆暂不可用"。
3. **键不信任用户输入**：session_id 直接拼进 Redis key，
   必须先过白名单，否则一个 `session_id="a: b"` 就是键注入。

两层用的是**不同的 TTL**：会话历史 24h（过夜即失效），
长期事实 7 天（背景比对话活得久）。这不是随手写的数字：
事实的失效成本更高 —— 它被注入每一轮的 prompt。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from app.core.config import settings

log = logging.getLogger(__name__)

# session_id 白名单：字母数字下划线短横线，1~64 位
_SID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

# 单轮保存的截断长度：记忆是"最近聊了什么"，不是全文归档
_TURN_FIELD_LEN = 500


def _safe_sid(session_id: str | None) -> str:
    """非法 session_id 一律按"没有会话"处理，而不是抛错。"""
    if not session_id or not _SID_RE.match(session_id):
        return ""
    return session_id


def _hist_key(sid: str) -> str:
    return f"ops:sess:{sid}:hist"


def _facts_key(sid: str) -> str:
    return f"ops:sess:{sid}:facts"


class InMemorySessionStore:
    """进程内实现：单测用 + Redis 不可用时的兜底。

    刻意和 Redis 版共用一套接口 —— 单测里测的行为
    （截断、去重、TTL 语义）在线上不因换存储而变形。
    进程内存储不满足"无状态横向扩展"，所以**只用于测试**，
    生产路径（main.py lifespan）只会装 Redis 版。
    """

    def __init__(self) -> None:
        self.hist: dict[str, list[dict]] = {}
        self.facts: dict[str, dict[str, float]] = {}
        self.last_error: str | None = None

    async def load(self, session_id: str | None) -> dict[str, Any]:
        sid = _safe_sid(session_id)
        if not sid:
            return {"history": [], "facts": []}
        self.last_error = None
        bucket = self.facts.get(sid, {})
        return {
            "history": list(self.hist.get(sid, [])),
            "facts": sorted(bucket, key=bucket.get),
        }

    async def save_turn(
        self,
        session_id: str | None,
        *,
        question: str,
        answer: str,
        route: str,
        facts: list[str] | None = None,
    ) -> str | None:
        sid = _safe_sid(session_id)
        if not sid:
            return None
        self.hist.setdefault(sid, []).append(
            {"q": question[:_TURN_FIELD_LEN], "a": (answer or "")[:_TURN_FIELD_LEN],
             "route": route, "ts": time.time()}
        )
        self.hist[sid] = self.hist[sid][-settings.session_max_turns:]
        if facts:
            bucket = self.facts.setdefault(sid, {})
            for f in facts:
                bucket.setdefault(f, time.time())
            keep = sorted(bucket, key=bucket.get)[-settings.facts_max:]
            self.facts[sid] = {k: bucket[k] for k in keep}
        return None

    async def aclose(self) -> None:  # 接口对齐 Redis 版
        return None


class RedisSessionStore:
    """Redis 实现。所有方法吞异常 —— 见模块 docstring 第 2 条。"""

    def __init__(self, client: Any, *, cfg: Any = None) -> None:
        self._redis = client
        self._cfg = cfg or settings
        self.last_error: str | None = None

    async def load(self, session_id: str | None) -> dict[str, Any]:
        sid = _safe_sid(session_id)
        if not sid:
            return {"history": [], "facts": []}
        try:
            raw = await self._redis.lrange(
                _hist_key(sid), -self._cfg.session_max_turns, -1
            )
            fmap = await self._redis.hgetall(_facts_key(sid)) or {}
            self.last_error = None
            history = [json.loads(x) for x in raw if x]
            # 按写入时间戳排序：事实的"新旧"只看 ts，不看插入顺序
            facts = sorted(fmap, key=lambda f: float(fmap[f]))
            return {"history": history, "facts": facts}
        except Exception as exc:  # noqa: BLE001 - 记忆挂了不能拖垮问答
            self.last_error = type(exc).__name__
            log.warning("session load failed（记忆降级）: %s", exc)
            return {"history": [], "facts": []}

    async def save_turn(
        self,
        session_id: str | None,
        *,
        question: str,
        answer: str,
        route: str,
        facts: list[str] | None = None,
    ) -> str | None:
        """写一轮对话（+ 可选新事实）。返回错误串（memory:<Err>）或 None。"""
        sid = _safe_sid(session_id)
        if not sid:
            return None
        try:
            turn = json.dumps(
                {"q": question[:_TURN_FIELD_LEN], "a": (answer or "")[:_TURN_FIELD_LEN],
                 "route": route, "ts": time.time()},
                ensure_ascii=False,
            )
            hkey = _hist_key(sid)
            pipe = self._redis.pipeline(transaction=True)
            pipe.rpush(hkey, turn)
            # 只留最近 N 轮：会话历史是滑窗，不是日志
            pipe.ltrim(hkey, -self._cfg.session_max_turns, -1)
            pipe.expire(hkey, self._cfg.session_ttl_seconds)
            if facts:
                fkey = _facts_key(sid)
                fmap = await self._redis.hgetall(fkey) or {}
                for f in facts:
                    fmap.setdefault(f, time.time())
                keep = sorted(fmap, key=lambda f: float(fmap[f]))[-self._cfg.facts_max:]
                pipe.delete(fkey)
                for f in keep:
                    pipe.hset(fkey, f, fmap[f])
                pipe.expire(fkey, self._cfg.facts_ttl_seconds)
            await pipe.execute()
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("session save failed（记忆降级）: %s", exc)
            return f"memory:{type(exc).__name__}"

    async def aclose(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:  # noqa: BLE001
            pass


def build_session_store(client: Any = None) -> RedisSessionStore | None:
    """lifespan 用。`SESSION_MEMORY_ENABLED=false` 时返回 None（整层关闭）。"""
    if not getattr(settings, "session_memory_enabled", True):
        return None
    from app.cache.factory import create_redis

    return RedisSessionStore(client or create_redis())


def render_memory_context(ctx: dict[str, Any]) -> str:
    """把会话上下文渲染成注入 prompt 的文本块。

    渲染成"用户/助手"对而不是原始 JSON：这段文本是给模型看的，
    结构越接近对话本身，模型越不容易把元数据当内容复述。
    """
    lines: list[str] = []
    for turn in ctx.get("history", []):
        lines.append(f"用户：{turn.get('q', '')}\n助手：{turn.get('a', '')}")
    facts = ctx.get("facts") or []
    if facts:
        lines.append("已知背景（用户此前说明，可直接引用）：" + "；".join(facts))
    return "\n".join(lines)

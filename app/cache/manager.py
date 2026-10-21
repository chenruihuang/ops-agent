"""两级缓存管理器：精确缓存 + 语义缓存。

为什么是两级
------------
L1 精确缓存（Redis String）
    归一化后完全相同的提问直接命中。企业场景里重复提问占比很高
    （FAQ、固定运维流程），这一层几乎零成本地吃掉最热的流量。

L2 语义缓存（Redis Stack + RediSearch KNN）
    "这个季度奖金怎么算" 与 "奖金如何计算" 是两个 key，但同一个答案。
    用 query 向量做 KNN，相似度过阈值就算命中。

为什么必须配"护栏"
------------------
语义缓存最危险的失效是**关键数字不同但向量极像**。
"16 岁能独立签合同吗" / "18 岁能独立签合同吗" 相似度常年 0.97+，答案却相反。
所以命中前必须过 anchors 一致性校验（见 guards.py）。

时效性怎么保证
--------------
所有 key 都带 kb_version。知识库更新 → 版本号 +1 → 旧缓存整体不可达，
不需要任何批量删除。这是"分钟级生效"最省事也最可靠的做法。
配合分级 TTL：稳定知识长 TTL，动态数据（工单状态、资产）短 TTL 甚至不缓存。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import struct
import time
from dataclasses import asdict, dataclass, field, fields
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol, Sequence

from app.cache.guards import check_anchor_consistency, check_length_ratio
from app.cache.keys import (
    SEMANTIC_INDEX,
    SEMANTIC_PREFIX,
    exact_cache_key,
    lock_key,
    normalize,
    semantic_member_key,
    semantic_point_id,
    sub_cache_key,
)
from app.observability.metrics import (
    CACHE_GUARD_REJECTED,
    CACHE_LOOKUP_SECONDS,
    CACHE_REQUESTS,
    CACHE_SEMANTIC_SIMILARITY,
    CACHE_STORES,
    CACHE_TOKENS_SAVED,
)

if TYPE_CHECKING:  # 仅用于类型标注，运行时不需要 pydantic 在场
    from app.core.config import Settings

log = logging.getLogger(__name__)

# 知识库版本号的唯一真源（存 Redis，多副本共享）
KB_VERSION_KEY = "ops:v1:meta:kb_version"

class AsyncRedisLike(Protocol):
    """只声明我们真正用到的方法 —— 方便注入假实现做单测。"""

    async def get(self, key: str) -> Any: ...
    async def set(self, key: str, value: Any, *, nx: bool = ..., px: int = ...) -> Any: ...
    async def delete(self, *keys: str) -> Any: ...
    async def hset(self, name: str, mapping: dict | None = ..., **kwargs: Any) -> Any: ...
    async def expire(self, name: str, time: int) -> Any: ...
    async def scan_iter(self, match: str = ..., count: int = ...): ...
    async def execute_command(self, *args: Any) -> Any: ...


@dataclass(slots=True)
class CachedAnswer:
    """缓存里存的内容。带上溯源字段，方便在 Langfuse / 日志里还原命中路径。"""

    answer: str
    queried: str = ""
    pages: list[int] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    route: str = "rag"
    created_at: float = 0.0
    kb_version: int = 0
    tokens_saved: int = 0
    source: str = "miss"        # miss | exact | semantic
    similarity: float = 1.0

    def dump(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def load(cls, raw: str | bytes) -> "CachedAnswer":
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in allowed})

    def as_hit(self, *, source: str, similarity: float = 1.0) -> "CachedAnswer":
        return CachedAnswer(**{**asdict(self), "source": source, "similarity": similarity})


def _to_str(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class RedisCache:
    def __init__(
        self,
        client: AsyncRedisLike,
        cfg: "Settings | None" = None,
        *,
        embedding_fn: Callable[[Sequence[str]], Awaitable[list[list[float]]]] | None = None,
    ) -> None:
        if cfg is None:
            from app.core.config import settings as _settings

            cfg = _settings
        self._redis = client
        self._cfg = cfg
        self._embed = embedding_fn
        self._index_ready = False
        self._kb_cache: tuple[int, float] | None = None

    @property
    def client(self) -> AsyncRedisLike:
        """裸客户端。只给**需要原子指令**的模块用（目前是安全沙箱的 `GETDEL`）。

        为什么开这个口子而不是再加一层封装：确认凭据的"取出即作废"必须是一条
        原子命令，任何 try/except 包裹的封装层都会在异常重试时破坏这个语义。
        """
        return self._redis

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    async def current_kb_version(self) -> int:
        """知识库版本号 —— 缓存失效的总开关。

        为什么以 Redis 为准而不是读本地配置：
        服务是多副本的。如果版本号只存在进程内存里，A 副本自增了、
        B 副本不知道，就会出现"一半新数据一半旧缓存"的脏读。
        放进 Redis 后所有副本看到同一个值，失效才是真的一致。

        进程内加 1 秒缓存，避免每个请求都打一次 Redis。
        """
        cached = self._kb_cache
        if cached is not None and time.monotonic() - cached[1] < 1.0:
            return cached[0]
        try:
            raw = await self._redis.get(KB_VERSION_KEY)
            if raw is None:
                # 首次运行：用配置值初始化。SET NX 保证多副本同时启动时不会互相覆盖。
                await self._redis.set(KB_VERSION_KEY, int(self._cfg.kb_version), nx=True)
                raw = await self._redis.get(KB_VERSION_KEY)
            version = int(raw) if raw is not None else int(self._cfg.kb_version)
        except Exception as exc:
            log.warning("kb_version read failed, fallback to local config: %s", exc)
            version = int(self._cfg.kb_version)
        self._kb_cache = (version, time.monotonic())
        return version

    async def bump_kb_version(self) -> int:
        """知识库更新后调用：版本号 +1，全副本缓存立即不可达。

        注意这里**不删除任何缓存**。旧 key 因为版本号变了自然没人去读，
        剩下的交给 Redis 的 LRU + TTL 回收。
        这比"SCAN 全库 + 批量 DEL"既快又不会在删除过程中产生脏读。
        """
        try:
            await self._ensure_kb_version_seeded()
            new_version = await self._redis.incr(KB_VERSION_KEY)
        except Exception as exc:
            log.error("bump kb_version failed: %s", exc)
            return int(self._cfg.kb_version)
        self._kb_cache = None
        log.info("kb_version bumped to %s", new_version)
        return int(new_version)

    async def _ensure_kb_version_seeded(self) -> None:
        self._kb_cache = None
        await self.current_kb_version()

    def _ttl(self, base: int) -> int:
        """给 TTL 加抖动，防止大批 key 同时过期造成缓存雪崩。"""
        jitter = self._cfg.cache_ttl_jitter
        if jitter <= 0:
            return base
        return max(1, int(base * (1 + random.uniform(-jitter, jitter))))

    def _answer_key(self, kb_version: int, **kw: Any) -> str:
        return exact_cache_key(
            query=kw["query"],
            kb_version=kb_version,
            prompt_version=self._cfg.prompt_version,
            model=self._cfg.deepseek_model,
            top_k=self._cfg.top_k_rerank,
        )

    # ------------------------------------------------------------------
    # L1 精确缓存
    # ------------------------------------------------------------------
    async def get_exact(self, query: str) -> CachedAnswer | None:
        if not self._cfg.cache_enabled:
            return None
        kb_version = await self.current_kb_version()
        key = self._answer_key(kb_version, query=query)
        started = time.perf_counter()
        try:
            raw = await self._redis.get(key)
        except Exception as exc:  # 缓存故障绝不能拖垮主链路
            log.warning("exact cache read failed: %s", exc)
            CACHE_REQUESTS.labels(layer="exact", result="error").inc()
            return None
        finally:
            CACHE_LOOKUP_SECONDS.labels(layer="exact").observe(time.perf_counter() - started)

        if raw is None:
            CACHE_REQUESTS.labels(layer="exact", result="miss").inc()
            return None

        value = CachedAnswer.load(raw)
        if value.kb_version != kb_version:
            # 理论上 key 里已含版本号，这里只是双保险
            CACHE_REQUESTS.labels(layer="exact", result="stale").inc()
            return None

        CACHE_REQUESTS.labels(layer="exact", result="hit").inc()
        if value.tokens_saved:
            CACHE_TOKENS_SAVED.inc(value.tokens_saved)
        return value.as_hit(source="exact", similarity=1.0)

    async def set_exact(self, query: str, value: CachedAnswer) -> None:
        if not self._cfg.cache_enabled:
            return
        kb_version = await self.current_kb_version()
        payload = value.as_hit(source="miss")
        payload.kb_version = kb_version
        payload.created_at = payload.created_at or time.time()
        payload.queried = query
        ttl = self._ttl(
            self._cfg.cache_null_ttl if not payload.answer else self._cfg.cache_exact_ttl
        )
        try:
            await self._redis.set(
                self._answer_key(kb_version, query=query), payload.dump(), px=ttl * 1000
            )
            CACHE_STORES.labels(layer="exact").inc()
        except Exception as exc:
            log.warning("exact cache write failed: %s", exc)

    # ------------------------------------------------------------------
    # L2 语义缓存
    # ------------------------------------------------------------------
    async def ensure_semantic_index(self) -> bool:
        """幂等地创建 RediSearch 索引。首次调用会真正建索引。"""
        if self._index_ready:
            return True

        def _fmt(vector_field: Any, tag_field: Any, num_field: Any) -> tuple:
            return (
                "FT.CREATE", SEMANTIC_INDEX,
                "ON", "HASH",
                "PREFIX", "1", SEMANTIC_PREFIX,
                "SCHEMA",
                "vec", "VECTOR", "HNSW", "6",
                "TYPE", "FLOAT32",
                "DIM", str(self._cfg.embedding_dim),
                "DISTANCE_METRIC", "COSINE",
                "payload", "TEXT",
                "kb_version", "NUMERIC",
                "created_at", "NUMERIC",
            )

        try:
            await self._redis.execute_command("FT.INFO", SEMANTIC_INDEX)
            self._index_ready = True
            return True
        except Exception:
            pass

        try:
            await self._redis.execute_command(*_fmt(None, None, None))
            self._index_ready = True
            log.info("semantic cache index created: %s", SEMANTIC_INDEX)
            return True
        except Exception as exc:
            log.warning("semantic index creation failed: %s", exc)
            return False

    async def get_semantic(
        self, query: str, embedding: Sequence[float]
    ) -> CachedAnswer | None:
        if not self._cfg.cache_enabled:
            return None
        if not await self.ensure_semantic_index():
            return None

        started = time.perf_counter()
        kb_version = await self.current_kb_version()
        blob = struct.pack(f"<{len(embedding)}f", *embedding)
        knn_query = (
            f"(@kb_version:[{kb_version} {kb_version}])"
            f"=>[KNN 3 @vec $B AS __score]"
        )
        args = (
            "FT.SEARCH", SEMANTIC_INDEX, knn_query,
            "PARAMS", "2", "B", blob,
            "SORTBY", "__score", "ASC",
            "RETURN", "3", "payload", "__score", "kb_version",
            "DIALECT", "2",
            "LIMIT", "0", "3",
        )
        try:
            reply = await self._redis.execute_command(*args)
        except Exception as exc:
            log.warning("semantic cache search failed: %s", exc)
            CACHE_REQUESTS.labels(layer="semantic", result="error").inc()
            return None
        finally:
            CACHE_LOOKUP_SECONDS.labels(layer="semantic").observe(time.perf_counter() - started)

        candidates = self._parse_search_reply(reply)
        for payload_raw, distance in candidates:
            similarity = 1.0 - distance      # COSINE 下 RediSearch 返回的是距离
            CACHE_SEMANTIC_SIMILARITY.observe(similarity)
            if similarity < self._cfg.cache_semantic_threshold:
                continue

            candidate = CachedAnswer.load(payload_raw)

            # ---- 护栏：向量像不代表语义等价 ----
            anchor = check_anchor_consistency(query, candidate.queried)
            if not anchor.accepted:
                CACHE_GUARD_REJECTED.labels(reason=anchor.reason.split(":")[0]).inc()
                log.info("semantic hit rejected (%s): %r vs %r", anchor.reason, query, candidate.queried)
                continue
            length = check_length_ratio(query, candidate.queried)
            if not length.accepted:
                CACHE_GUARD_REJECTED.labels(reason="length_ratio").inc()
                continue

            CACHE_REQUESTS.labels(layer="semantic", result="hit").inc()
            if candidate.tokens_saved:
                CACHE_TOKENS_SAVED.inc(candidate.tokens_saved)
            return candidate.as_hit(source="semantic", similarity=similarity)

        # 有候选但全被拒 → 记 rejected，别记 miss（两者要能分开看）
        if candidates:
            CACHE_REQUESTS.labels(layer="semantic", result="rejected").inc()
        else:
            CACHE_REQUESTS.labels(layer="semantic", result="miss").inc()
        return None

    @staticmethod
    def _parse_search_reply(reply: Any) -> list[tuple[str, float]]:
        """把 FT.SEARCH 的扁平回复解析成 [(payload, distance), ...]。"""
        if not reply or not isinstance(reply, (list, tuple)) or len(reply) < 2:
            return []
        out: list[tuple[str, float]] = []
        idx = 1
        while idx + 1 < len(reply):
            attributes = reply[idx + 1]
            payload: str | None = None
            distance = 1.0
            if isinstance(attributes, (list, tuple)):
                for j in range(0, len(attributes) - 1, 2):
                    name = _to_str(attributes[j])
                    if name == "payload":
                        payload = _to_str(attributes[j + 1])
                    elif name == "__score":
                        distance = _to_float(attributes[j + 1])
            if payload:
                out.append((payload, distance))
            idx += 2
        return out

    async def set_semantic(
        self, query: str, value: CachedAnswer, embedding: Sequence[float]
    ) -> None:
        if not self._cfg.cache_enabled:
            return
        if not await self.ensure_semantic_index():
            return

        kb_version = await self.current_kb_version()
        payload = value.as_hit(source="miss")
        payload.kb_version = kb_version
        payload.created_at = payload.created_at or time.time()
        payload.queried = query

        point_id = semantic_point_id(query=query, kb_version=kb_version)
        member = semantic_member_key(point_id)
        blob = struct.pack(f"<{len(embedding)}f", *embedding)
        ttl = self._ttl(
            self._cfg.cache_null_ttl if not payload.answer else self._cfg.cache_semantic_ttl
        )
        try:
            await self._redis.hset(
                member,
                mapping={
                    "vec": blob,
                    "payload": payload.dump(),
                    "kb_version": kb_version,
                    "created_at": payload.created_at,
                },
            )
            await self._redis.expire(member, ttl)
            CACHE_STORES.labels(layer="semantic").inc()
        except Exception as exc:
            log.warning("semantic cache write failed: %s", exc)

    # ------------------------------------------------------------------
    # 子链路缓存
    # ------------------------------------------------------------------
    async def get_json(self, namespace: str, *parts: object) -> Any | None:
        if not self._cfg.cache_enabled:
            return None
        try:
            raw = await self._redis.get(sub_cache_key(namespace, *parts))
        except Exception:
            return None
        if raw is None:
            return None
        try:
            return json.loads(_to_str(raw))
        except json.JSONDecodeError:
            return None

    async def set_json(self, namespace: str, value: Any, *, ttl: int, parts: Sequence[object]) -> None:
        if not self._cfg.cache_enabled:
            return
        try:
            await self._redis.set(
                sub_cache_key(namespace, *parts),
                json.dumps(value, ensure_ascii=False, default=str),
                px=self._ttl(ttl) * 1000,
            )
        except Exception as exc:
            log.warning("sub cache write failed (%s): %s", namespace, exc)

    # ------------------------------------------------------------------
    # 统一入口 + 单飞
    # ------------------------------------------------------------------
    def lock_name(self, query: str) -> str:
        """单飞锁名。刻意不含版本号 —— 锁是秒级的，跨版本复用不影响正确性。"""
        return lock_key("answer", normalize(query))

    async def acquire_singleflight(self, query: str) -> bool:
        """尝试成为"领跑者"。拿不到不报错，调用方决定是等待还是自己算。"""
        if not self._cfg.cache_singleflight_enabled:
            return True
        try:
            return bool(
                await self._redis.set(
                    self.lock_name(query),
                    "1",
                    nx=True,
                    px=int(self._cfg.cache_singleflight_hold * 1000),
                )
            )
        except Exception as exc:
            log.warning("singleflight lock unavailable, degrade to direct compute: %s", exc)
            return True

    async def release_singleflight(self, query: str) -> None:
        try:
            await self._redis.delete(self.lock_name(query))
        except Exception:
            pass

    async def wait_for_result(
        self,
        query: str,
        *,
        embedding: Sequence[float] | None = None,
        timeout: float | None = None,
    ) -> CachedAnswer | None:
        """跟随者：等领跑者把结果写进缓存。超时返回 None，由调用方自己算。"""
        deadline = time.monotonic() + (
            self._cfg.cache_singleflight_wait if timeout is None else timeout
        )
        while time.monotonic() < deadline:
            await asyncio.sleep(0.15)
            hit = await self.lookup(query, embedding=embedding)
            if hit.source != "miss":
                return hit
        return None

    async def lookup(self, query: str, *, embedding: Sequence[float] | None = None) -> CachedAnswer:
        hit = await self.get_exact(query)
        if hit is not None:
            return hit
        if embedding is not None:
            hit = await self.get_semantic(query, embedding)
            if hit is not None:
                return hit
        return CachedAnswer(answer="", queried=query, source="miss")

    async def store(
        self,
        query: str,
        value: CachedAnswer,
        *,
        embedding: Sequence[float] | None = None,
    ) -> None:
        await self.set_exact(query, value)
        if embedding is not None:
            await self.set_semantic(query, value, embedding)

    async def get_or_compute(
        self,
        query: str,
        compute: Callable[[], Awaitable[CachedAnswer]],
        *,
        embedding: Sequence[float] | None = None,
    ) -> CachedAnswer:
        """缓存主入口。

        除了两级查询，这里还实现了**单飞（singleflight）**：
        同一个问题被 N 个用户同时问到时，只让一个请求去打 LLM，
        其余请求短暂轮询等结果。这是防缓存击穿最直接的手段，
        也是压测时错率能不能压下来的关键。
        """
        hit = await self.lookup(query, embedding=embedding)
        if hit.source != "miss":
            return hit

        lock_key_name = self.lock_name(query)
        acquired = False
        try:
            acquired = bool(
                await self._redis.set(
                    lock_key_name,
                    "1",
                    nx=True,
                    px=int(self._cfg.request_timeout_seconds * 1000),
                )
            )
        except Exception as exc:
            log.warning("singleflight lock failed, fallback to direct compute: %s", exc)

        if not acquired:
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                await asyncio.sleep(0.15)
                hit = await self.lookup(query, embedding=embedding)
                if hit.source != "miss":
                    return hit
            log.info("singleflight wait timed out, computing anyway: %r", query)

        try:
            value = await compute()
            await self.store(query, value, embedding=embedding)
            return value
        finally:
            if acquired:
                try:
                    await self._redis.delete(lock_key_name)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # 失效
    # ------------------------------------------------------------------
    async def invalidate_by_prefix(self, prefix: str, *, batch: int = 500) -> int:
        """按前缀清理。

        正常流程下用不到 —— 版本号 +1 就已经让旧 key 不可达了。
        但运维上需要"立即释放内存"时用得上，也方便演示"主动失效"。
        """
        removed = 0
        try:
            async for key in self._redis.scan_iter(match=f"{prefix}*", count=batch):
                await self._redis.delete(_to_str(key))
                removed += 1
        except Exception as exc:
            log.warning("invalidate_by_prefix failed: %s", exc)
        return removed

    async def stats(self) -> dict[str, Any]:
        """给 /admin/cache 用，压测和演示都要看这个。"""
        try:
            info = await self._redis.execute_command("INFO", "memory")
        except Exception:
            info = ""
        used = 0
        for line in _to_str(info).splitlines():
            if line.startswith("used_memory:"):
                used = int(line.split(":", 1)[1] or 0)
                break
        return {"kb_version": await self.current_kb_version(), "used_memory_bytes": used}

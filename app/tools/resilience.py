"""韧性内核：熔断（Circuit Breaker）+ 重试（Retry）+ 降级链（Degradation Chain）。

为什么这三件事要放在一个文件里
------------------------------
它们单独看都简单，但**顺序错了就全错**：

    breaker(外层) → retry(内层) → 真实调用

- retry 在内：一次"抖动"要靠重试吃掉，不能计成失败；
- breaker 在外：只有"重试用尽仍然失败"才算一个失败信号。
  反过来（retry 在外套 breaker）会让熔断器看到一串瞬时失败，
  三次重试就能把阈值打满 —— 明明只是抖了一下，熔断却开了。
- 熔断打开后**不再重试**：快速失败，把 3 次超时（3 × 2s）压成一次
  `allow() == False`（微秒级）。这正是熔断存在的意义。

重试白名单：只重试"重试有意义"的错误
----------------------------------
    TransientToolError / TimeoutError / ConnectionError / 5xx  → 重试
    PermanentToolError / 4xx / 参数错误 / JSON-RPC 业务错误   → 不重试

"4xx 也重试三次"是新手最典型的浪费：错的东西重试 3 次还是错，
只是把一次失败变成三次失败、三倍耗时，还顺带把熔断器打满。

退避带抖动（jitter）
-------------------
纯指数退避会在下游恢复的同一瞬间放出所有积压请求（惊群）。
加 0~20% 随机抖动，把重试打散。

为什么不用 pybreaker / tenacity
-------------------------------
两个库都在 requirements 里躺了很久，但真正接线时发现：

1. **pybreaker 是同步 API**，异步场景要靠 `call_async` 包线程池，
   而我们需要的是"每次调用都带 trace span 与 attempts 计数"，
   包装层比自研（本文件 ~200 行）更厚。它的 `state` 读取也不带快照，
   没法直接喂给 `/ready` 与 `/metrics`。
2. **tenacity 管不了"失败信号只算一次"**：它在重试内部循环，
   拿不到"这几次尝试算不算一个熔断失败"的边界。硬接要自己返写状态。

所以这里保留 Resilience4j 的**语义**（三态机、半开配额、退避抖动、
异常白名单），实现自研：时钟与 sleep 可注入（测试不用真等），
每次状态迁移都打指标 —— 这是"能演示"和"只能声称"的区别。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Awaitable, Callable, Sequence, TypeVar

from app.observability.metrics import (
    CIRCUIT_STATE,
    TOOL_CALLS_TOTAL,
    TOOL_CALL_SECONDS,
    TOOL_DEGRADED_TOTAL,
    TOOL_RETRIES_TOTAL,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

_RNG = random.Random()


class BreakerState(IntEnum):
    """数值与 `metrics.CIRCUIT_STATE` 的约定一致（0=closed 1=half_open 2=open），
    这样上报时不需要一张映射表 —— 少一处会写错的地方。"""

    CLOSED = 0
    HALF_OPEN = 1
    OPEN = 2


class TransientToolError(RuntimeError):
    """下游暂时性故障：连接重置、5xx、响应截断。**重试有意义**。"""


class PermanentToolError(RuntimeError):
    """重试没有意义的错误：4xx、参数错误、明确的业务拒绝。"""


class CircuitOpenError(RuntimeError):
    """熔断打开、本次调用被快速拒绝。调用方应直接走降级。"""

    def __init__(self, name: str, remaining: float = 0.0) -> None:
        super().__init__(f"熔断器 {name} 打开中（剩余冷却 {remaining:.1f}s）")
        self.name = name
        self.remaining = remaining


class DependencyUnavailableError(RuntimeError):
    """依赖不可用（探测失败 / 未配置）—— 与"熔断打开"区分开：

    熔断打开是**我们主动拒绝**（有冷却期，会自愈）；
    探测失败是**依赖被动不可用**（不知道什么时候好）。
    降级链里两者的 outcome 不同，排障时指向的结论也不同。
    """


# =====================================================================
# 熔断器
# =====================================================================


@dataclass(slots=True)
class BreakerSnapshot:
    name: str
    state: str
    failures: int
    success: int = 0
    failure: int = 0
    rejected: int = 0
    transitions: int = 0
    opened_age_s: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "failures": self.failures,
            "success": self.success,
            "failure": self.failure,
            "rejected": self.rejected,
            "transitions": self.transitions,
            "opened_age_s": round(self.opened_age_s, 2) if self.opened_age_s is not None else None,
        }


class CircuitBreaker:
    """三态熔断器（closed / open / half_open）。

    状态迁移
    --------
        closed  --连续失败 N 次-->  open
        open    --冷却期到-->       half_open（放行至多 half_open_max 个探测）
        half_open --探测成功-->     closed（清零失败计数）
        half_open --探测失败-->     open（重新计冷却）

    **为什么是"连续失败"而不是"滑动窗口失败率"**：本项目的依赖是单个
    MCP Server，不是几十个实例的集群；"连续 N 次失败"在语义上就是
    "最近 N 次全挂"，比维护一个时间窗更简单、也更好解释。集群场景才需要
    失败率 + 最小请求数。

    **为什么半开要限流**：冷却期一到，积压的请求会一起涌进来。如果全放行，
    下游刚恢复就被打垮 —— 这叫"熔断恢复雪崩"。只放 1 个探测，成功才全开。
    """

    def __init__(
        self,
        name: str,
        *,
        fail_threshold: int = 5,
        cool_down: float = 15.0,
        half_open_max: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.fail_threshold = max(1, int(fail_threshold))
        self.cool_down = max(0.0, float(cool_down))
        self.half_open_max = max(1, int(half_open_max))
        self._clock = clock

        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open_inflight = 0
        self._lock = asyncio.Lock()

        self._success = 0
        self._failure = 0
        self._rejected = 0
        self._transitions = 0
        self._publish()

    # ---------------- 读 ----------------
    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def state_name(self) -> str:
        return self._state.name.lower()

    @property
    def is_open(self) -> bool:
        """给探测路径用的**无锁快照**判断。

        只读、不迁移状态：OPEN 且冷却未到才算打开。
        冷却到了仍返回 True（探测会在 `allow()` 里真正放行），
        这样 `available()` 不会在冷却边界上闪来闪去。
        """
        if self._state is not BreakerState.OPEN:
            return False
        opened = self._opened_at if self._opened_at is not None else self._clock()
        return (self._clock() - opened) < self.cool_down

    def snapshot(self) -> BreakerSnapshot:
        opened_age = (self._clock() - self._opened_at) if self._opened_at is not None else None
        return BreakerSnapshot(
            name=self.name,
            state=self.state_name,
            failures=self._failures,
            success=self._success,
            failure=self._failure,
            rejected=self._rejected,
            transitions=self._transitions,
            opened_age_s=opened_age,
        )

    # ---------------- 准入 / 记账 ----------------
    async def allow(self) -> bool:
        """是否放行本次调用。放行时若处于半开，占用一个探测配额。"""
        async with self._lock:
            now = self._clock()
            if self._state is BreakerState.OPEN:
                # 注意：`_opened_at` 可能是 0.0（注入的假时钟从 0 开始），
                # 用 `or now` 会把它当成"未设置" → 永远算冷却未到。
                # 这类 falsy 陷阱在单测里表现为"冷却死活不生效"，线上则表现为
                # "熔断永远恢复不了"，都很难查。
                opened = self._opened_at if self._opened_at is not None else now
                remaining = self.cool_down - (now - opened)
                if remaining > 0:
                    self._rejected += 1
                    return False
                self._transition(BreakerState.HALF_OPEN, now)

            if self._state is BreakerState.HALF_OPEN:
                if self._half_open_inflight >= self.half_open_max:
                    self._rejected += 1
                    return False
                self._half_open_inflight += 1
            return True

    async def record(self, ok: bool) -> None:
        """记录一次**最终结果**（重试已用尽后的结论），不是每次尝试。"""
        async with self._lock:
            now = self._clock()
            if ok:
                self._success += 1
                if self._state is BreakerState.HALF_OPEN:
                    self._half_open_inflight = max(0, self._half_open_inflight - 1)
                    self._transition(BreakerState.CLOSED, now)
                else:
                    self._failures = 0
                return

            self._failure += 1
            if self._state is BreakerState.HALF_OPEN:
                # 探测失败：立刻回到 open，重新计冷却
                self._half_open_inflight = max(0, self._half_open_inflight - 1)
                self._transition(BreakerState.OPEN, now)
                return

            self._failures += 1
            if self._failures >= self.fail_threshold:
                self._transition(BreakerState.OPEN, now)

    def reset(self) -> None:
        """人工恢复（运维接口 / 测试）。真实故障恢复后不必等冷却。"""
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = None
        self._half_open_inflight = 0
        self._publish()

    # ---------------- 内部 ----------------
    def _transition(self, to: BreakerState, now: float) -> None:
        if to is self._state:
            return
        previous = self._state
        self._state = to
        self._transitions += 1

        if to is BreakerState.CLOSED:
            self._failures = 0
            self._opened_at = None
            self._half_open_inflight = 0
        elif to is BreakerState.OPEN:
            self._opened_at = now
            self._half_open_inflight = 0
        elif to is BreakerState.HALF_OPEN:
            self._half_open_inflight = 0

        # WARNING 而非 INFO：熔断开合是**线上事故的强信号**，
        # 日志级别太低会淹没在访问日志里，事后 grep 不到。
        log.warning(
            "熔断器 %s：%s → %s（failures=%d/%d, cool_down=%.0fs）",
            self.name, previous.name.lower(), to.name.lower(),
            self._failures, self.fail_threshold, self.cool_down,
        )
        self._publish()

    def _publish(self) -> None:
        for state, value in (
            ("closed", BreakerState.CLOSED),
            ("half_open", BreakerState.HALF_OPEN),
            ("open", BreakerState.OPEN),
        ):
            CIRCUIT_STATE.labels(dependency=self.name, state=state).set(
                1 if self._state is value else 0
            )


# =====================================================================
# 重试
# =====================================================================


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.08
    max_delay: float = 1.0
    jitter: float = 0.2

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        """第 attempt 次失败后要等多久。attempt 从 1 开始：1 → base。"""
        exp = min(self.base_delay * (2 ** max(0, attempt - 1)), self.max_delay)
        r = rng or _RNG
        return exp + r.uniform(0.0, self.jitter * exp)


def is_retryable(exc: BaseException) -> bool:
    """重试白名单。**默认不重试** —— 白名单漏掉的错误会被直接抛出，
    比"默认重试"安全：至少不会把参数错误重试三次。"""
    return isinstance(exc, (TransientToolError, TimeoutError, ConnectionError, asyncio.TimeoutError))


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy | None = None,
    *,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    metric_name: str = "unknown",
) -> tuple[T, int]:
    """执行 fn，按策略重试。返回 `(结果, 总尝试次数)`，失败时抛**最后一次**异常。

    `on_retry(attempt, exc, delay)` 用来把重试写进 trace span ——
    "重试了两次才成功"和"一次成功"在事后排障里是完全不同的信息。
    """
    pol = policy or RetryPolicy()
    attempts = max(1, pol.max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            return await fn(), attempt
        except BaseException as exc:  # noqa: BLE001 - 分类在下面
            if not is_retryable(exc) or attempt >= attempts:
                # 把"试了几次"钉在异常上：降级链要能回答
                # "MCP 是第一次就失败，还是重试 3 次才放弃" —— 这两件事
                # 在排障时的含义完全不同（后者说明依赖在抖）
                try:
                    setattr(exc, "attempts", attempt)
                except Exception:  # noqa: BLE001 - 有些内建异常不允许设置属性
                    pass
                raise
            delay = pol.delay_for(attempt)
            TOOL_RETRIES_TOTAL.labels(dependency=metric_name).inc()
            if on_retry is not None:
                try:
                    on_retry(attempt, exc, delay)
                except Exception:  # noqa: BLE001 - 埋点失败不能影响重试
                    pass
            log.info(
                "重试 %s（第 %d/%d 次，%.0fms 后）：%s",
                metric_name, attempt, attempts, delay * 1000, type(exc).__name__,
            )
            await sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


# =====================================================================
# 熔断 + 重试组合
# =====================================================================


@dataclass(slots=True)
class GuardedResult:
    value: Any
    attempts: int
    breaker_state: str


async def guarded_call(
    breaker: CircuitBreaker,
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> GuardedResult:
    """breaker(外) → retry(内) → fn。

    熔断打开时抛 `CircuitOpenError`（不重试、不计失败）——
    被拒绝的请求不该继续恶化熔断统计，否则永远恢复不了。
    """
    if not await breaker.allow():
        snap = breaker.snapshot()
        remaining = max(0.0, breaker.cool_down - (snap.opened_age_s or 0.0))
        raise CircuitOpenError(breaker.name, remaining)

    try:
        value, attempts = await with_retry(
            fn, policy, on_retry=on_retry, sleep=sleep, metric_name=breaker.name
        )
    except BaseException:
        await breaker.record(False)
        raise
    await breaker.record(True)
    return GuardedResult(value=value, attempts=attempts, breaker_state=breaker.state_name)


# =====================================================================
# 降级链
# =====================================================================


@dataclass(frozen=True, slots=True)
class Hop:
    """降级链上的一次尝试记录。这是**可解释性**的来源：
    用户看到的是"工单查不到"，日志/回放里要能回答"为什么查不到"。"""

    source: str
    # ok | miss | not_ok | circuit_open | unavailable | timeout | error:<Type>
    #
    # miss 与 not_ok 必须分开：缓存未命中是**正常运营事件**（新 key 第一次查必然 miss），
    # 把它记成 not_ok 会让"降级链"看起来每次调用都在降级 —— 实测健康路径下
    # 每个 tool_result 都带 `degraded_from:["cache"]`，读事件的人会以为缓存一直是坏的。
    outcome: str
    elapsed_ms: float
    attempts: int = 0     # 这一跳内部重试了几次（0 = 不是"尝试型"步骤）

    def as_dict(self) -> dict[str, Any]:
        out = {"source": self.source, "outcome": self.outcome,
               "elapsed_ms": round(self.elapsed_ms, 1)}
        if self.attempts > 1:
            out["attempts"] = self.attempts
        return out


async def run_chain(
    steps: Sequence[tuple[str, Callable[[], Awaitable[Any]]]],
    *,
    ok_of: Callable[[Any], bool] = lambda r: bool(getattr(r, "ok", r is not None)),
    on_hop: Callable[[Hop], None] | None = None,
    metric_stage: str = "tools",
) -> tuple[Any | None, list[Hop]]:
    """按顺序尝试降级链，返回第一个"成功"的结果。

    每一跳都记录 outcome，全部失败返回 `(None, hops)` —— 让上层决定
    是拼一句"查不到"还是整个阶段降级。**绝不把失败伪装成成功**。
    """
    hops: list[Hop] = []
    for source, fn in steps:
        started = time.perf_counter()
        try:
            value = await fn()
        except CircuitOpenError as exc:
            hop = Hop(source, "circuit_open", (time.perf_counter() - started) * 1000,
                      getattr(exc, "attempts", 0) or 0)
            hops.append(hop)
            TOOL_DEGRADED_TOTAL.labels(stage=metric_stage, reason="circuit_open").inc()
            log.info("降级链跳过 %s：%s", source, exc)
            if on_hop:
                on_hop(hop)
            continue
        except DependencyUnavailableError as exc:
            hop = Hop(source, "unavailable", (time.perf_counter() - started) * 1000,
                      getattr(exc, "attempts", 0) or 0)
            hops.append(hop)
            TOOL_DEGRADED_TOTAL.labels(stage=metric_stage, reason="unavailable").inc()
            log.info("降级链跳过 %s：%s", source, exc)
            if on_hop:
                on_hop(hop)
            continue
        except asyncio.TimeoutError as exc:
            hop = Hop(source, "timeout", (time.perf_counter() - started) * 1000,
                      getattr(exc, "attempts", 0) or 0)
            hops.append(hop)
            if on_hop:
                on_hop(hop)
            continue
        except Exception as exc:  # noqa: BLE001 - 任何下游异常都算这一跳失败
            hop = Hop(source, f"error:{type(exc).__name__}", (time.perf_counter() - started) * 1000,
                      getattr(exc, "attempts", 0) or 0)
            hops.append(hop)
            if on_hop:
                on_hop(hop)
            continue

        elapsed = (time.perf_counter() - started) * 1000
        if value is None:
            # 这一跳"没有东西可给"（缓存未命中是最常见的情况），不是故障。
            # 继续往下走，但记 miss 而不是 not_ok —— 见 Hop.outcome 的注释。
            hop = Hop(source, "miss", elapsed)
            hops.append(hop)
            if on_hop:
                on_hop(hop)
            continue

        if ok_of(value):
            hop = Hop(source, "ok", elapsed)
            hops.append(hop)
            if on_hop:
                on_hop(hop)
            return value, hops

        hop = Hop(source, "not_ok", elapsed)
        hops.append(hop)
        if on_hop:
            on_hop(hop)

    return None, hops


def observe_call(tool: str, source: str, ok: bool, elapsed_ms: float) -> None:
    """工具级指标：次数 + 耗时。放在这里让三个调用点（mcp/cache/mock）共用一个口径。"""
    TOOL_CALLS_TOTAL.labels(tool=tool, source=source,
                            result="ok" if ok else "fail").inc()
    TOOL_CALL_SECONDS.labels(tool=tool, source=source).observe(elapsed_ms / 1000.0)


def summarize_hops(hops: Sequence[Hop]) -> dict[str, Any]:
    """给 trace span / /ready / SSE 事件用的紧凑快照。"""
    return {
        "chain": [h.as_dict() for h in hops],
        "attempted": [h.source for h in hops],
        "final": hops[-1].source if hops and hops[-1].outcome == "ok" else None,
        "degraded": bool(hops) and (not hops or hops[-1].outcome != "ok"),
    }

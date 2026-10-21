"""韧性内核测试：熔断状态机、重试分类、降级链顺序。

这三件事的共同点是**错了也不会立刻报错**：
- 熔断阈值写错 → 平时看不出，故障时要么不熔断（被打垮）要么误熔断（可用性骤降）；
- 重试白名单漏了 4xx → 参数错误被重试三次，延迟翻三倍，还顺手把熔断打满；
- 降级顺序写反 → MCP 好着却一直用 mock 数据（最糟：答案看起来正常）。

所以这里覆盖的是**状态迁移的每一条边**，而不是"跑一次成功"。
时钟、sleep 全部注入 —— 测试不真等、不抖动。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools.resilience import (
    BreakerState,
    CircuitBreaker,
    CircuitOpenError,
    DependencyUnavailableError,
    Hop,
    PermanentToolError,
    RetryPolicy,
    TransientToolError,
    guarded_call,
    is_retryable,
    run_chain,
    summarize_hops,
    with_retry,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


async def _no_sleep(_delay: float) -> None:
    """重试不真等 —— 否则一条用例就要跑好几秒。"""


# =====================================================================
def test_breaker_open_on_threshold() -> None:
    print("熔断：连续失败到阈值才打开")
    clock = FakeClock()
    b = CircuitBreaker("t", fail_threshold=3, cool_down=10, clock=clock)

    async def run() -> None:
        await b.allow()
        await b.record(False)
        await b.allow()
        await b.record(False)
        check("失败 2 次仍是 closed（阈值 3）", b.state is BreakerState.CLOSED, b.state_name)
        check("closed 期间放行", await b.allow() is True)
        await b.record(False)
        check("第 3 次失败 → open", b.state is BreakerState.OPEN, b.state_name)
        check("open 期间拒绝", await b.allow() is False)

        # 成功一次就把连续失败清零（"连续失败"语义，不是累计）
        b2 = CircuitBreaker("t2", fail_threshold=2, cool_down=10, clock=clock)
        await b2.allow()
        await b2.record(False)
        await b2.allow()
        await b2.record(True)
        await b2.allow()
        await b2.record(False)
        check("成功会清零连续失败计数", b2.state is BreakerState.CLOSED, b2.state_name)

    asyncio.run(run())


def test_breaker_half_open_cycle() -> None:
    print("熔断：冷却 → 半开 → 探测成功/失败")
    clock = FakeClock()
    b = CircuitBreaker("t", fail_threshold=2, cool_down=10, clock=clock)

    async def run() -> None:
        for _ in range(2):
            await b.allow()
            await b.record(False)
        check("已 open", b.state is BreakerState.OPEN, b.state_name)

        clock.advance(9.9)
        check("冷却未到仍拒绝", await b.allow() is False)

        clock.advance(0.2)
        check("冷却到期放行（转 half_open）", await b.allow() is True)
        check("状态变 half_open", b.state is BreakerState.HALF_OPEN, b.state_name)
        check("半开只放 1 个探测（并发第 2 个被拒）", await b.allow() is False)

        await b.record(True)
        check("探测成功 → closed", b.state is BreakerState.CLOSED, b.state_name)
        check("closed 后恢复放行", await b.allow() is True)

        # 探测失败 → 立刻回 open，并重新计冷却
        for _ in range(2):
            await b.allow()
            await b.record(False)
        clock.advance(10.1)
        await b.allow()                       # 进入 half_open
        await b.record(False)                 # 探测失败
        check("探测失败 → 回到 open", b.state is BreakerState.OPEN, b.state_name)
        clock.advance(5.0)
        check("重新冷却（不是沿用旧时间）", await b.allow() is False)

    asyncio.run(run())


def test_breaker_zero_opened_at() -> None:
    """回归：注入的时钟从 0 开始时，`_opened_at or now` 会把 0.0 当成未设置，
    导致冷却永远不生效（熔断永远恢复不了）。这类 falsy 陷阱必须在测试里钉住。"""
    print("熔断：时钟从 0 开始也能正常冷却")
    clock = FakeClock(0.0)
    b = CircuitBreaker("t", fail_threshold=1, cool_down=5, clock=clock)

    async def run() -> None:
        await b.allow()
        await b.record(False)
        check("已 open", b.state is BreakerState.OPEN, b.state_name)
        check("冷却未到拒绝", await b.allow() is False)
        clock.advance(5.1)
        check("冷却到期放行（0.0 不被当成未设置）", await b.allow() is True)

    asyncio.run(run())


def test_retry_policy() -> None:
    print("重试：可重试与不可重试")
    policy = RetryPolicy(max_attempts=3, base_delay=0.1, max_delay=1.0, jitter=0.0)

    check("退避指数增长 0.1/0.2/0.4",
          [round(policy.delay_for(i), 3) for i in (1, 2, 3)] == [0.1, 0.2, 0.4],
          str([round(policy.delay_for(i), 3) for i in (1, 2, 3)]))
    check("退避有上限", round(policy.delay_for(10), 3) == 1.0, str(policy.delay_for(10)))
    check("瞬态错误可重试", is_retryable(TransientToolError("x")))
    check("超时可重试", is_retryable(TimeoutError()))
    check("连接错误可重试", is_retryable(ConnectionError()))
    check("永久错误不重试", not is_retryable(PermanentToolError("bad args")))
    check("ValueError 默认不重试（白名单之外）", not is_retryable(ValueError("x")))

    async def run() -> None:
        calls = {"n": 0}

        async def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise TransientToolError("boom")
            return "ok"

        value, attempts = await with_retry(flaky, policy, sleep=_no_sleep)
        check("两次失败后成功", value == "ok" and attempts == 3, f"{value}/{attempts}")
        check("实际调用了 3 次", calls["n"] == 3, str(calls))

        calls["n"] = 0

        async def perm() -> str:
            calls["n"] += 1
            raise PermanentToolError("bad args")

        try:
            await with_retry(perm, policy, sleep=_no_sleep)
            check("永久错误应抛出", False)
        except PermanentToolError as exc:
            check("永久错误只试 1 次", calls["n"] == 1, str(calls))
            check("attempts 标记在异常上", getattr(exc, "attempts", 0) == 1)

        # 重试耗尽后抛最后一次异常，且 attempts 是总尝试数
        calls["n"] = 0

        async def always() -> str:
            calls["n"] += 1
            raise TransientToolError("still down")

        try:
            await with_retry(always, policy, sleep=_no_sleep)
            check("重试耗尽应抛出", False)
        except TransientToolError as exc:
            check("重试耗尽：总尝试 3 次", calls["n"] == 3, str(calls))
            check("attempts=3 标记在异常上", getattr(exc, "attempts", 0) == 3)

    asyncio.run(run())


def test_guarded_call_order() -> None:
    """breaker 在外、retry 在内：一次调用内重试 3 次全失败，只记 **1 次**熔断失败。
    顺序写反的话，3 次重试会把 fail_threshold=3 的熔断器直接打满 —— 抖一下就熔断。"""
    print("组合：重试耗尽只计一次熔断失败")
    clock = FakeClock()
    b = CircuitBreaker("t", fail_threshold=3, cool_down=10, clock=clock)
    policy = RetryPolicy(max_attempts=3, base_delay=0.01, jitter=0.0)
    calls = {"n": 0}

    async def always_fail() -> str:
        calls["n"] += 1
        raise TransientToolError("down")

    async def run() -> None:
        try:
            await guarded_call(b, always_fail, policy=policy, sleep=_no_sleep)
        except TransientToolError:
            pass
        check("内部重试 3 次", calls["n"] == 3, str(calls))
        check("熔断器只记 1 次失败", b.snapshot().failure == 1, str(b.snapshot().as_dict()))
        check("仍 closed（3 次重试≠3 次失败）", b.state is BreakerState.CLOSED, b.state_name)

        # 熔断打开后：不再发起调用，直接抛 CircuitOpenError
        for _ in range(2):
            try:
                await guarded_call(b, always_fail, policy=policy, sleep=_no_sleep)
            except TransientToolError:
                pass
        check("累计 3 次最终失败 → open", b.state is BreakerState.OPEN, b.state_name)
        before = calls["n"]
        try:
            await guarded_call(b, always_fail, policy=policy, sleep=_no_sleep)
            check("熔断打开应抛 CircuitOpenError", False)
        except CircuitOpenError:
            check("熔断打开抛 CircuitOpenError", True)
        check("熔断打开后不再发起调用", calls["n"] == before, f"{before}->{calls['n']}")

    asyncio.run(run())


def test_run_chain() -> None:
    print("降级链：顺序、原因、全失败")
    ok_result = type("R", (), {"ok": True, "source": "mock"})()

    async def run() -> None:
        order: list[str] = []

        async def cache_miss():
            order.append("cache")
            return None

        async def mcp_ok():
            order.append("mcp")
            return ok_result

        async def mock_ok():
            order.append("mock")
            return ok_result

        value, hops = await run_chain([("cache", cache_miss), ("mcp", mcp_ok), ("mock", mock_ok)])
        check("命中第一跳成功就停（顺序 cache→mcp）", order == ["cache", "mcp"], str(order))
        check("返回成功结果", value is ok_result)
        # 缓存未命中是 miss，不是 not_ok：它是正常运营事件（新 key 必 miss），
        # 记成 not_ok 会让"每次调用都在降级"成为事件流的默认观感。
        check("缓存未命中记 miss、命中记 ok", [h.outcome for h in hops] == ["miss", "ok"],
              str([h.outcome for h in hops]))
        check("summarize 能给出最终来源", summarize_hops(hops)["final"] == "mcp",
              str(summarize_hops(hops)))

        # 四类失败原因各有不同 outcome —— 排障方向完全不同
        order.clear()

        async def open_circuit():
            raise CircuitOpenError("mcp", 5)

        async def unavailable():
            raise DependencyUnavailableError("probe failed")

        async def timeout():
            raise asyncio.TimeoutError()

        async def crash():
            raise ValueError("boom")

        value2, hops2 = await run_chain([
            ("mcp", open_circuit), ("probe", unavailable),
            ("slow", timeout), ("bad", crash), ("mock", mock_ok),
        ])
        outcomes = [h.outcome for h in hops2]
        check("熔断打开 → circuit_open", outcomes[0] == "circuit_open", str(outcomes))
        check("探测失败 → unavailable", outcomes[1] == "unavailable", str(outcomes))
        check("超时 → timeout", outcomes[2] == "timeout", str(outcomes))
        check("异常 → error:<Type>", outcomes[3] == "error:ValueError", str(outcomes))
        check("兜底成功", value2 is ok_result and hops2[-1].source == "mock")

        # 全失败：必须返回 None，不能假装成功
        async def bad_mock():
            raise RuntimeError("mock broken")

        value3, hops3 = await run_chain([("mcp", open_circuit), ("mock", bad_mock)])
        check("全部失败返回 None", value3 is None)
        check("全失败时 degraded=True", summarize_hops(hops3)["degraded"] is True)

    asyncio.run(run())


def test_hop_attempts() -> None:
    """降级链要能回答"MCP 是第一次就失败，还是重试 3 次才放弃" —— 后者说明依赖在抖。"""
    print("降级链：记录每一跳的重试次数")

    async def run() -> None:
        async def flaky():
            exc = TransientToolError("down")
            exc.attempts = 3
            raise exc

        async def mock_ok():
            return type("R", (), {"ok": True, "source": "mock"})()

        _, hops = await run_chain([("mcp", flaky), ("mock", mock_ok)])
        check("失败的跳带 attempts=3", hops[0].attempts == 3, str(hops[0].as_dict()))
        check("as_dict 只在 >1 时输出 attempts", "attempts" in hops[0].as_dict())
        check("成功跳不带 attempts 噪音", "attempts" not in hops[1].as_dict())

    asyncio.run(run())


def test_event_degraded_contract() -> None:
    print("事件契约：缓存未命中不算降级，真降级必须带 degraded_from")

    from app.tools.base import ToolResult

    def ev(chain: list[dict], ok: bool = True, source: str = "mcp") -> dict:
        return ToolResult(tool="t", ok=ok, args={}, source=source,
                          text="x", chain=chain).as_event()

    # 健康路径：cache miss → mcp ok。这是线上绝大多数请求的形态，
    # 带着 degraded_from 就会让人以为缓存一直在坏（实测确实发生过）。
    healthy = ev([{"source": "cache", "outcome": "miss"},
                  {"source": "mcp", "outcome": "ok"}])
    check("cache miss 不产生 degraded_from", "degraded_from" not in healthy, str(healthy))

    # 真降级：cache miss → mcp 熔断 → mock ok，降级自 mcp（不是 cache）
    degraded = ev([{"source": "cache", "outcome": "miss"},
                   {"source": "mcp", "outcome": "circuit_open"},
                   {"source": "mock", "outcome": "ok"}], source="mock")
    check("真降级只报失败的那几跳", degraded.get("degraded_from") == ["mcp"],
          str(degraded.get("degraded_from")))

    # 全失败：没有命中跳，最后一跳的失败原因也不能丢
    failed = ev([{"source": "cache", "outcome": "miss"},
                 {"source": "mcp", "outcome": "timeout"},
                 {"source": "mock", "outcome": "not_ok"}], ok=False, source="none")
    check("全失败时包含最后一跳", failed.get("degraded_from") == ["mcp", "mock"],
          str(failed.get("degraded_from")))


def test_breaker_snapshot() -> None:
    print("熔断：快照供 /ready 与 /metrics 使用")

    async def run() -> None:
        b = CircuitBreaker("mcp", fail_threshold=2, cool_down=10, clock=FakeClock())
        await b.allow()
        await b.record(False)
        snap = b.snapshot().as_dict()
        check("快照含状态与计数", snap["state"] == "closed" and snap["failures"] == 1, str(snap))
        check("快照含被拒绝次数", "rejected" in snap)
        check("快照可 JSON 序列化", isinstance(snap["state"], str))

        b.reset()
        check("reset 后回到 closed 且计数清零",
              b.state is BreakerState.CLOSED and b.snapshot().failures == 0)

    asyncio.run(run())


def main() -> int:
    print("=" * 70)
    print("韧性内核测试（熔断 / 重试 / 降级链）")
    print("=" * 70)
    test_breaker_open_on_threshold()
    test_breaker_half_open_cycle()
    test_breaker_zero_opened_at()
    test_retry_policy()
    test_guarded_call_order()
    test_run_chain()
    test_hop_attempts()
    test_event_degraded_contract()
    test_breaker_snapshot()
    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""缓存管理器的集成测试（不需要真 Redis）。

运行：
    python -m tests.test_cache_manager
    pytest tests/test_cache_manager.py -v
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from app.cache.manager import CachedAnswer, RedisCache
from tests.fake_redis import FakeRedis


def make_cfg(**overrides):
    base = dict(
        cache_enabled=True,
        kb_version=1,
        prompt_version="v1",
        deepseek_model="deepseek-chat",
        top_k_rerank=5,
        cache_exact_ttl=86400,
        cache_semantic_ttl=21600,
        cache_semantic_threshold=0.96,
        cache_null_ttl=60,
        cache_ttl_jitter=0.0,
        cache_singleflight_enabled=True,
        cache_singleflight_wait=6.0,
        cache_singleflight_hold=30,
        embedding_dim=3,
        request_timeout_seconds=5.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def make_cache(cfg=None):
    client = FakeRedis()
    return RedisCache(client, cfg or make_cfg()), client


# ======================================================== L1 精确缓存


async def test_exact_hit_roundtrip():
    cache, _ = make_cache()
    await cache.set_exact("诉讼时效是几年", CachedAnswer(answer="三年", pages=[31], scores=[0.9]))
    hit = await cache.get_exact("诉讼时效是几年")
    assert hit is not None
    assert hit.answer == "三年"
    assert hit.source == "exact"
    assert hit.pages == [31]


async def test_exact_hit_ignores_politeness_and_punctuation():
    cache, _ = make_cache()
    await cache.set_exact("诉讼时效是几年", CachedAnswer(answer="三年"))
    assert await cache.get_exact("请问，诉讼时效是几年？") is not None


async def test_exact_miss_on_different_question():
    cache, _ = make_cache()
    await cache.set_exact("诉讼时效是几年", CachedAnswer(answer="三年"))
    assert await cache.get_exact("宣告失踪需要多久") is None


async def test_kb_version_bump_invalidates_everything():
    """这是"知识库更新后分钟级生效"的实现方式：改版本号，不删缓存。"""
    cache, client = make_cache()
    await cache.set_exact("诉讼时效是几年", CachedAnswer(answer="旧答案"))
    assert await cache.get_exact("诉讼时效是几年") is not None

    # 模拟知识库更新
    new_version = await cache.bump_kb_version()
    assert new_version == 2

    cache._kb_cache = None          # 清掉 1 秒的进程内缓存，立刻生效
    assert await cache.get_exact("诉讼时效是几年") is None, "版本号变更后旧缓存必须不可达"
    assert await cache.get_exact("诉讼时效是几年") is None


async def test_kb_version_is_shared_across_instances():
    """两个副本共用一个 Redis —— 版本号必须一致，否则会出现一半新一半旧的脏读。"""
    client = FakeRedis()
    replica_a = RedisCache(client, make_cfg())
    replica_b = RedisCache(client, make_cfg())

    await replica_a.set_exact("同一个问题", CachedAnswer(answer="A"))
    assert await replica_b.get_exact("同一个问题") is not None

    await replica_a.bump_kb_version()
    replica_b._kb_cache = None
    assert await replica_b.get_exact("同一个问题") is None


async def test_ttl_jitter_stays_in_range():
    cache, _ = make_cache(make_cfg(cache_ttl_jitter=0.1))
    values = {cache._ttl(1000) for _ in range(50)}
    assert min(values) >= 900
    assert max(values) <= 1100
    assert len(values) > 1, "抖动应当产生不同取值，否则防不了雪崩"


# ======================================================== L2 语义缓存


async def test_semantic_hit_for_near_duplicate_question():
    cache, _ = make_cache()
    e1 = [1.0, 0.0, 0.0]
    e2 = [1.0, 0.1, 0.0]

    await cache.set_semantic(
        "奖金怎么算", CachedAnswer(answer="按绩效系数", queried="奖金怎么算"), e1
    )
    hit = await cache.get_semantic("奖金如何计算", e2)

    assert hit is not None, "相似提问应命中语义缓存"
    assert hit.answer == "按绩效系数"
    assert hit.source == "semantic"
    assert hit.similarity > 0.96


async def test_semantic_rejected_when_key_number_differs():
    """核心安全测试：向量几乎相同，但 16 与 18 是两个完全不同的法律后果。"""
    cache, _ = make_cache()
    same_vector = [1.0, 0.0, 0.0]          # 故意用完全相同的向量，排除阈值因素

    await cache.set_semantic(
        "我今年16岁，能独立签合同吗",
        CachedAnswer(answer="属于完全民事行为能力人，可以", queried="我今年16岁，能独立签合同吗"),
        same_vector,
    )
    hit = await cache.get_semantic("我今年18岁，能独立签合同吗", same_vector)

    assert hit is None, "数字锚点不一致时必须拒绝命中"


async def test_semantic_rejected_when_extra_constraint_added():
    cache, _ = make_cache()
    v = [1.0, 0.0, 0.0]
    await cache.set_semantic("能签合同吗", CachedAnswer(answer="视情况而定", queried="能签合同吗"), v)
    # 一方多出"16岁"，是不同的问题
    assert await cache.get_semantic("16岁能签合同吗", v) is None


async def test_semantic_miss_below_threshold():
    cache, _ = make_cache()
    await cache.set_semantic("奖金怎么算", CachedAnswer(answer="A", queried="奖金怎么算"), [1.0, 0.0, 0.0])
    # 正交向量 → 相似度 0，远低于阈值
    assert await cache.get_semantic("完全不相关的问题", [0.0, 1.0, 0.0]) is None


async def test_semantic_index_created_once():
    cache, client = make_cache()
    await cache.ensure_semantic_index()
    await cache.ensure_semantic_index()
    created = [c for c in client.commands if c[0] == "FT.CREATE"]
    assert len(created) == 1


# ======================================================== 单飞 / 降级


async def test_singleflight_computes_only_once():
    """同题并发：只允许一个请求打 LLM，其余等结果。这是压测错率的关键。"""
    cache, _ = make_cache()
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.2)
        return CachedAnswer(answer="答案", queried="并发问题")

    results = await asyncio.gather(
        *[cache.get_or_compute("并发问题", compute) for _ in range(5)]
    )

    assert calls == 1, f"compute 应当只执行一次，实际 {calls} 次"
    assert all(r.answer == "答案" for r in results)


async def test_get_or_compute_uses_cache_on_second_call():
    cache, _ = make_cache()
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        return CachedAnswer(answer="答案", queried="同一个问题")

    await cache.get_or_compute("同一个问题", compute)
    await cache.get_or_compute("同一个问题", compute)
    assert calls == 1


async def test_cache_disabled_still_works():
    cache, _ = make_cache(make_cfg(cache_enabled=False))
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        return CachedAnswer(answer="答案", queried="q")

    await cache.get_or_compute("q", compute)
    await cache.get_or_compute("q", compute)
    assert calls == 2, "关闭缓存后每次都应走真实链路"
    assert await cache.get_exact("q") is None


async def test_cache_failure_does_not_break_main_path():
    """缓存挂了不能把主链路带崩 —— 这是"降级"思路在缓存上的体现。"""

    class BrokenRedis(FakeRedis):
        async def get(self, key):
            raise ConnectionError("redis is down")

        async def set(self, key, value, **kwargs):
            raise ConnectionError("redis is down")

    cache = RedisCache(BrokenRedis(), make_cfg())
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        return CachedAnswer(answer="答案", queried="q")

    result = await cache.get_or_compute("q", compute)
    assert result.answer == "答案"
    assert calls == 1


async def test_singleflight_helpers():
    cache, _ = make_cache()
    assert await cache.acquire_singleflight("q") is True
    assert await cache.acquire_singleflight("q") is False, "第二个请求不应拿到锁"
    await cache.release_singleflight("q")
    assert await cache.acquire_singleflight("q") is True


async def test_singleflight_lock_name_is_process_independent():
    """内置 hash() 受 PYTHONHASHSEED 影响，多副本下锁名会不一致 —— 必须用稳定摘要。"""
    a, _ = make_cache()
    b, _ = make_cache()
    assert a.lock_name("同一个问题") == b.lock_name("同一个问题")
    assert a.lock_name("请问，同一个问题？") == b.lock_name("同一个问题")


# ======================================================== 子链路缓存


async def test_sub_cache_ttl_and_roundtrip():
    cache, client = make_cache()
    await cache.set_json("tool", {"status": "open"}, ttl=30, parts=["ticket", 1001])
    assert await cache.get_json("tool", "ticket", 1001) == {"status": "open"}

    await cache.set_json("tool", {"status": "closed"}, ttl=30, parts=["ticket", 1001])
    # 覆盖写：确认拿到的是新值
    assert await cache.get_json("tool", "ticket", 1001) == {"status": "closed"}


async def test_null_answer_uses_short_ttl():
    """防穿透：查不到也要缓存，但只缓存很短时间。"""
    cache, client = make_cache()
    await cache.set_exact("不存在的问题", CachedAnswer(answer=""))
    key = cache._answer_key(await cache.current_kb_version(), query="不存在的问题")
    assert await client.ttl(key) <= 60


# ======================================================== 运行器


def _run_all():
    passed, failed = 0, []
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        try:
            asyncio.run(fn())
            passed += 1
        except AssertionError as exc:
            failed.append((name, repr(exc)))
        except Exception as exc:  # noqa: BLE001
            failed.append((name, f"{type(exc).__name__}: {exc}"))
    print(f"\n{'=' * 56}")
    print(f"通过 {passed} 项，失败 {len(failed)} 项")
    for name, err in failed:
        print(f"  FAIL  {name}: {err}")
    print("=" * 56)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())

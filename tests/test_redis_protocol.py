"""Redis 协议版本与能力探测的回归测试（不需要真 Redis）。

锁住两个"不报错、只是悄悄失效"的坑 —— 它们都真实发生过：

  1. **RESP 协议必须是 2。** redis-py >= 6 的 `DEFAULT_RESP_VERSION` 是 3，
     连接时会先发 `HELLO 3`：
        - Redis < 6.0 不认识该命令 → `unknown command 'HELLO'`，握手直接失败；
        - RediSearch 在 RESP3 下返回**结构化 map** 而非扁平数组，而
          `_parse_search_reply` 是按扁平数组写的 → 循环进不去、返回空候选，
          表现为"语义缓存永远 miss"，日志里只有一条 warning。
     实测：Redis 3.0.504 报 HELLO 未知；redis-stack 7.4.7 的 FT.SEARCH
     在 RESP2 下是 `[1, "key", ["f","hello"]]`，RESP3 下是
     `[1, {"results": [...], "extra_attributes": ...}]`。

  2. **"连上了"不等于"连对了"。** 端口冲突时连到另一个 Redis 实例
     （老版本、别人的数据）也能 `SET` 成功并返回 OK，但服务端读的是另一个库 ——
     版本号同步等于没做、缓存永不失效。所以写入前必须探测版本与 RediSearch 能力。

运行：
    python -m tests.test_redis_protocol
    pytest tests/test_redis_protocol.py -v
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.cache.factory import probe_redis, redis_connection_kwargs
from app.cache.manager import RedisCache
from app.core.config import settings
from app.ingest.version import sync_redis_version


# ======================================================== 假客户端


class FakeCapClient:
    """只实现探测用到的两个命令。"""

    def __init__(self, *, version: str = "7.4.7", has_search: bool = True, fail_info: bool = False):
        self.version = version
        self.has_search = has_search
        self.fail_info = fail_info
        self.commands: list[str] = []

    async def info(self, section: str | None = None):
        if self.fail_info:
            raise ConnectionError("connect refused")
        return {"redis_version": self.version}

    async def execute_command(self, *args, **kwargs):
        self.commands.append(str(args[0]))
        if args[0] == "FT._LIST":
            if not self.has_search:
                raise Exception("ERR unknown command 'FT._LIST'")
            return []
        raise NotImplementedError(args[0])


# ======================================================== 协议版本


def test_settings_defaults_to_resp2():
    """默认必须是 RESP2 —— 这是 RediSearch 解析与老 Redis 兼容的共同前提。"""
    assert settings.redis_protocol == 2, (
        f"redis_protocol 被改成了 {settings.redis_protocol}；"
        "改成 3 会让 _parse_search_reply 静默返回空候选，必须同时重写解析并补 RESP3 回归测试"
    )


def test_connection_kwargs_carry_resp2():
    kwargs = redis_connection_kwargs()
    assert kwargs["protocol"] == 2
    # 超时也应该是单一来源，不要各处自定义
    assert "socket_connect_timeout" in kwargs and "socket_timeout" in kwargs


def test_connection_kwargs_can_be_overridden():
    kwargs = redis_connection_kwargs(socket_connect_timeout=9.0)
    assert kwargs["socket_connect_timeout"] == 9.0
    assert kwargs["protocol"] == 2, "覆盖超时不应该顺带把协议改掉"


def test_create_redis_passes_resp2():
    """真实工厂产出的连接池里，protocol 也必须是 2。"""
    try:
        from app.cache.factory import create_redis
    except Exception as exc:  # noqa: BLE001
        print(f"    (跳过：redis 未安装 {exc})")
        return
    client = create_redis()
    try:
        conn_kwargs = client.connection_pool.connection_kwargs
        assert conn_kwargs.get("protocol") == 2, f"连接池 protocol={conn_kwargs.get('protocol')}"
    finally:
        # 只是构造了对象，没有真正连接，直接丢弃即可
        pass


# ======================================================== RESP2/RESP3 返回结构


def test_parse_resp2_reply():
    """RESP2：FT.SEARCH 返回扁平数组 [总数, key, [字段, 值, ...], ...]。"""
    reply = [
        2,
        "ops:v1:sem:cache:a",
        ["payload", '{"answer":"x"}', "__score", "0.05"],
        "ops:v1:sem:cache:b",
        ["payload", '{"answer":"y"}', "__score", "0.20"],
    ]
    out = RedisCache._parse_search_reply(reply)
    assert len(out) == 2, out
    assert out[0] == ('{"answer":"x"}', 0.05), out[0]
    assert out[1][1] == 0.20, out[1]


def test_parse_resp3_reply_yields_nothing():
    """RESP3 的结构化回复解析不出候选 —— 这正是"必须固定 RESP2"的原因。

    这条测试不是在容忍缺陷，而是在**固化这个认知**：一旦有人把协议改成 3，
    语义缓存就会变成永远 miss（而精确缓存 L1 照常工作，指标上只表现为命中率偏低）。
    如果哪天真的支持了 RESP3，这条断言会失败，提醒你把它改成"能解析出 2 条"。
    """
    resp3_reply = [
        2,
        {
            "attributes": ["format"],
            "format": "STRING",
            "results": [
                {"id": "ops:v1:sem:cache:a", "extra_attributes": {"payload": '{"answer":"x"}', "__score": "0.05"}},
            ],
            "total_results": 2,
            "warning": [],
        },
    ]
    assert RedisCache._parse_search_reply(resp3_reply) == []


def test_parse_reply_handles_garbage():
    for bad in (None, [], [1], "not-a-list", [1, "k"]):
        assert RedisCache._parse_search_reply(bad) == []


# ======================================================== 能力探测


async def test_probe_accepts_redis_stack():
    cap = await probe_redis(FakeCapClient())
    assert cap["ok"] is True, cap
    assert cap["has_search"] is True
    assert cap["redis_version"] == "7.4.7"
    assert "通过" in cap["detail"]


async def test_probe_rejects_old_redis():
    """3.0.504（Windows 移植版）—— 本项目要求 >= 6.0。"""
    cap = await probe_redis(FakeCapClient(version="3.0.504"))
    assert cap["ok"] is False
    assert "3.0.504" in cap["detail"]
    assert "6.0" in cap["detail"]


async def test_probe_rejects_missing_redisearch():
    """版本够但没带 RediSearch —— 语义缓存整层不可用。"""
    cap = await probe_redis(FakeCapClient(version="7.4.7", has_search=False))
    assert cap["ok"] is False
    assert cap["has_search"] is False
    assert "RediSearch" in cap["detail"]


async def test_probe_survives_unreachable_redis():
    """探测函数自己不能把异常抛给调用方（它是 best-effort 路径上的第一环）。"""
    cap = await probe_redis(FakeCapClient(fail_info=True))
    assert cap["ok"] is False
    assert "无法读取 Redis 版本" in cap["detail"]


# ======================================================== 端到端：写版本号


class _ScriptedRedis:
    """记录被调用的命令，用于断言"探测不过就不该写"。"""

    def __init__(self, *args, version: str = "3.0.504", has_search: bool = False, **kwargs):
        self.kwargs = kwargs
        self.version = version
        self.has_search = has_search
        self.calls: list[str] = []

    async def info(self, section=None):
        return {"redis_version": self.version}

    async def execute_command(self, *args, **kwargs):
        self.calls.append(f"cmd:{args[0]}")
        if not self.has_search:
            raise Exception("ERR unknown command 'FT._LIST'")
        return []

    async def set(self, *args, **kwargs):
        self.calls.append("SET")
        return True

    async def get(self, *args, **kwargs):
        self.calls.append("GET")
        return b"7"

    async def aclose(self):
        self.calls.append("CLOSE")


async def test_sync_skips_write_when_target_is_wrong_redis():
    """探测不过时**不能**写版本号 —— 否则就是"报成功但写进了另一个库"。"""
    created: list[_ScriptedRedis] = []

    def factory(**kwargs):
        inst = _ScriptedRedis(**kwargs)
        created.append(inst)
        return inst

    with patch("redis.asyncio.Redis", factory):
        ok, msg = await sync_redis_version(host="localhost", port=6379, db=0, version=7)

    assert ok is False, msg
    assert created, "应当尝试建立过连接"
    assert "SET" not in created[0].calls, f"探测失败却写了 Redis：{created[0].calls}"
    assert "3.0.504" in msg and "localhost:6379" in msg
    assert "CLOSE" in created[0].calls, "连接必须被关闭，不能泄漏"


async def test_sync_writes_when_capabilities_ok():
    created: list[_ScriptedRedis] = []

    def factory(**kwargs):
        inst = _ScriptedRedis(**kwargs)
        inst.version = "7.4.7"
        inst.has_search = True
        created.append(inst)
        return inst

    with patch("redis.asyncio.Redis", factory):
        ok, msg = await sync_redis_version(host="localhost", port=16379, db=0, version=7)

    assert ok is True, msg
    assert "SET" in created[0].calls and "GET" in created[0].calls
    assert "7" in msg, msg


async def test_sync_can_skip_probe_for_explicit_callers():
    """`probe=False` 保留给确定目标是 redis-stack 的场景（例如容器内）。"""
    created: list[_ScriptedRedis] = []

    def factory(**kwargs):
        inst = _ScriptedRedis(**kwargs)
        created.append(inst)
        return inst

    with patch("redis.asyncio.Redis", factory):
        ok, msg = await sync_redis_version(host="redis", port=6379, db=0, version=3, probe=False)

    assert ok is True, msg
    assert "SET" in created[0].calls
    assert not any(c.startswith("cmd:") for c in created[0].calls), "probe=False 不应发探测命令"


# ======================================================== 运行器


def _run_all() -> int:
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

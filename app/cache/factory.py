"""缓存客户端工厂。

延迟导入 redis：让不装 redis 也能 import 本模块（评估脚本、单测都用得上）。

两条 Redis 连接路径（服务端的 `create_redis` 与入库脚本的 `app.ingest.version`）
都从这里取连接参数，避免"同一件事在两处各写一遍"——上一次 `kb_version` 的
Redis key 名不一致（`ops:v1:kb_version` vs `ops:v1:meta:kb_version`）就是这种重复
造成的，入库日志全绿而缓存永不失效。
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings

log = logging.getLogger(__name__)

# 连接超时统一在这里定，避免"脚本 3 秒、服务 2 秒"这种不可解释的差异
CONNECT_TIMEOUT = 2.0
SOCKET_TIMEOUT = 3.0


def redis_connection_kwargs(**overrides: Any) -> dict[str, Any]:
    """Redis 客户端的公共连接参数 —— 单一来源。

    `protocol` 固定取 `settings.redis_protocol`（默认 2），原因见 config.py 里的注释：
    redis-py >= 6 默认走 RESP3，会导致 (a) 老 Redis 上 `HELLO` 握手失败，
    (b) RediSearch 返回结构变化使 `_parse_search_reply` 静默失效。
    """
    kwargs: dict[str, Any] = {
        "protocol": settings.redis_protocol,
        "socket_connect_timeout": CONNECT_TIMEOUT,
        "socket_timeout": SOCKET_TIMEOUT,
    }
    kwargs.update(overrides)
    return kwargs


def create_redis() -> Any:
    import redis.asyncio as aioredis

    return aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        health_check_interval=30,
        max_connections=64,
        **redis_connection_kwargs(),
    )


def build_cache(client: Any = None):
    from app.cache.manager import RedisCache

    return RedisCache(client or create_redis(), settings)


async def probe_redis(client: Any) -> dict[str, Any]:
    """探测对面 Redis 是否满足本项目要求。返回结构化结果，不抛异常。

    为什么需要这个：本项目对 Redis 有两项**硬要求**，不满足时症状是
    「不报错、只是功能悄悄失效」，最难排查：

        1. Redis >= 6.0   —— 否则新版客户端连 `HELLO` 都发不出去
        2. 带 RediSearch  —— 否则 `FT.CREATE`/`FT.SEARCH` 全部 unknown command，
                             语义缓存 L2 整层不可用（而 L1 精确缓存照常工作，
                             所以从指标上只看到"命中率偏低"，看不出是坏的）

    而且"连上了 ≠ 连对了"：本机 6379 上完全可能跑着另一个 Redis 实例
    （老版本、自己的数据），端口冲突时连过去也一切正常，只是版本号同步、
    缓存失效全部写进了错误的库。所以探测必须显式做，并把版本号打出来。
    """
    result: dict[str, Any] = {
        "ok": False,
        "redis_version": None,
        "has_search": False,
        "detail": "",
    }

    try:
        info = await client.info("server")
        version = str((info or {}).get("redis_version") or "unknown")
        result["redis_version"] = version
    except Exception as exc:  # noqa: BLE001 - 探测函数不该把异常抛给调用方
        result["detail"] = f"无法读取 Redis 版本（{type(exc).__name__}: {exc}）"
        return result

    # RediSearch 在不在：FT._LIST 是它自带的最轻量命令（不会像 FT.SEARCH 那样要索引）
    try:
        await client.execute_command("FT._LIST")
        result["has_search"] = True
    except Exception as exc:  # noqa: BLE001
        result["detail"] = f"Redis {version} 上没有 RediSearch 模块（{exc}）"
        return result

    try:
        major = int(version.split(".")[0])
    except (ValueError, IndexError):
        major = 0
    if major < 6:
        result["detail"] = (
            f"Redis 版本过低（{version}，需要 >= 6.0）："
            f"新版客户端默认 RESP3，会在握手阶段发 HELLO 而被拒绝"
        )
        return result

    result["ok"] = True
    result["detail"] = f"Redis {version} + RediSearch，能力检查通过"
    return result

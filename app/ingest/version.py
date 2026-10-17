"""知识库版本号管理。

版本号是"缓存失效"与"索引替换"的共同开关，所以它必须是**单一可信来源**。
这里定义两级：

    本地文件 data/index/kb_version.json   —— 入库脚本写入，作为下一次自增的基准
    Redis  key `ops:v1:kb_version`        —— 服务端读取，多副本共享

为什么服务端不直接读文件：多副本场景下每个容器的文件系统是独立的，
文件会各写各的，导致"一半副本是新版本、一半是旧版本"的脏读。
Redis 是所有副本的唯一共享点。

本地文件的作用是**幂等**：连续跑两次入库不会把版本号推两次，
除非显式指定 `--kb-version`。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from app.cache.manager import KB_VERSION_KEY

log = logging.getLogger(__name__)

KB_VERSION_FILE = "kb_version.json"
# REDIS_KB_VERSION_KEY = "ops:v1:kb_version"


def local_version_path(index_dir: Path) -> Path:
    return index_dir / KB_VERSION_FILE


def read_local_version(index_dir: Path, *, fallback: int = 1) -> int:
    path = local_version_path(index_dir)
    if not path.exists():
        return fallback
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("kb_version", fallback))
    except (ValueError, TypeError, OSError) as exc:
        log.warning("读取本地版本号失败，回退到 %s：%s", fallback, exc)
        return fallback


def write_local_version(index_dir: Path, version: int, *, extra: dict | None = None) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    payload = {"kb_version": int(version)}
    if extra:
        payload.update(extra)
    local_version_path(index_dir).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def sync_redis_version(
    *,
    host: str,
    port: int,
    db: int,
    version: int,
    timeout: float = 3.0,
    protocol: int = 2,
    probe: bool = True,
) -> tuple[bool, str]:
    """把版本号推到 Redis。**Best-effort** —— Redis 不通不能让入库失败。

    返回 (是否成功, 说明)。失败时调用方需要在输出里显著提示：
    版本号没推到 Redis 的话，服务端仍在读旧版本，缓存不会失效。

    关于 `protocol`：默认 2（RESP2）。redis-py >= 6 默认走 RESP3，连接时先发
    `HELLO 3`；Redis 6.0 以下不认识这个命令，会直接握手失败。参数默认值与
    `settings.redis_protocol` 保持一致，由调用方显式传入。

    关于 `probe`：**"连上了"不等于"连对了"**。本机 6379 上完全可能跑着另一个
    Redis 实例（老版本、自己的数据），端口冲突时连过去一切正常，`SET` 也返回 OK ——
    但服务端读的是另一个库，版本号同步等于没做，缓存永远不会失效。
    所以写入前先确认对面版本 >= 6.0 且带 RediSearch；不满足就判为失败并说明原因，
    而不是让它"看起来成功了"。
    """
    try:
        import redis.asyncio as aioredis
    except ImportError:
        return False, "未安装 redis 客户端（pip install redis），跳过 Redis 版本号同步"

    from app.cache.factory import probe_redis, redis_connection_kwargs

    client = None
    try:
        client = aioredis.Redis(
            host=host,
            port=port,
            db=db,
            **redis_connection_kwargs(socket_connect_timeout=timeout, socket_timeout=timeout),
        )

        if probe:
            cap = await probe_redis(client)
            if not cap["ok"]:
                return False, (
                    f"{cap['detail']} —— 目标 {host}:{port}/{db}。"
                    f"这很可能不是服务端在用的那个 Redis，版本号写进去也不会生效"
                )

        await client.set(KB_VERSION_KEY, int(version))
        current = await client.get(KB_VERSION_KEY)
        if current is None:
            return False, f"写入后回读为空（{host}:{port}/{db}）—— 版本号未落盘"
        version_note = f"（Redis {cap['redis_version']} @ {host}:{port}/{db}）" if probe else ""
        return True, f"Redis 版本号已设为 {int(current)}{version_note}"
    except Exception as exc:  # noqa: BLE001 - 任何 Redis 异常都不应中断入库
        return False, f"Redis 同步失败（{type(exc).__name__}: {exc}）"
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass

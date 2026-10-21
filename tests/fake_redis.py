"""一个够用的假 Redis，用于在 D0 阶段验证缓存逻辑。

它不是 mock：真的实现了 GET/SET/HSET/EXPIRE/SCAN 和
RediSearch 的 FT.CREATE / FT.SEARCH（暴力 KNN），
所以语义缓存那条路径也能被真正跑通，而不是"假设它没问题"。

生产环境当然用真 Redis Stack，这只是让单测可以在没有 Docker 的机器上跑。
"""

from __future__ import annotations

import math
import re
import struct
import time
from typing import Any

_KB_FILTER = re.compile(r"@kb_version:\[(\d+)\s+(\d+)\]")


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 1.0
    return 1.0 - dot / (na * nb)


class FakeRedis:
    def __init__(self) -> None:
        self._kv: dict[str, tuple[str, float | None]] = {}
        self._hashes: dict[str, dict[str, Any]] = {}
        self._expiry: dict[str, float] = {}
        self._index_created = False
        self.commands: list[tuple] = []

    # ---------------- 内部 ----------------
    def _alive(self, key: str) -> bool:
        exp = self._expiry.get(key)
        if exp is not None and exp < time.monotonic():
            self._kv.pop(key, None)
            self._hashes.pop(key, None)
            self._expiry.pop(key, None)
            return False
        return True

    # ---------------- String ----------------
    async def get(self, key: str) -> str | None:
        if not self._alive(key):
            return None
        item = self._kv.get(key)
        return item[0] if item else None

    async def set(
        self,
        key: str,
        value: Any,
        *,
        nx: bool = False,
        px: int | None = None,
        ex: int | None = None,
    ) -> bool | None:
        # `ex`（秒）是安全沙箱用的：待确认动作的 TTL 是 600s，
        # 用 px 表示要写 600000，可读性差且容易少写一个 0。
        if nx and self._alive(key) and key in self._kv:
            return None
        self._kv[key] = (value, None)
        if px:
            self._expiry[key] = time.monotonic() + px / 1000.0
        elif ex:
            self._expiry[key] = time.monotonic() + ex
        return True

    async def getdel(self, key: str) -> str | None:
        """原子取出并删除 —— 确认凭据的一次性语义全靠它。

        FakeRedis 是单线程事件循环，天然原子；真实 Redis 的 GETDEL 也是原子的。
        两者语义一致，所以这里不需要额外加锁（加了反而会掩盖真实现的差异）。
        """
        value = await self.get(key)
        self._kv.pop(key, None)
        self._expiry.pop(key, None)
        return value

    async def delete(self, *keys: str) -> int:
        n = 0
        for key in keys:
            n += int(self._kv.pop(key, None) is not None)
            n += int(self._hashes.pop(key, None) is not None)
            self._expiry.pop(key, None)
        return n

    async def incr(self, key: str, amount: int = 1) -> int:
        current = 0
        if self._alive(key) and key in self._kv:
            current = int(self._kv[key][0])
        new = current + amount
        self._kv[key] = (str(new), None)
        return new

    # ---------------- Hash ----------------
    async def hset(self, name: str, mapping: dict | None = None, **kwargs: Any) -> int:
        bucket = self._hashes.setdefault(name, {})
        payload = dict(mapping or {})
        payload.update(kwargs)
        new = sum(1 for k in payload if k not in bucket)
        bucket.update(payload)
        return new

    async def expire(self, name: str, seconds: int) -> bool:
        if name in self._hashes or name in self._kv:
            self._expiry[name] = time.monotonic() + seconds
            return True
        return False

    async def ttl(self, name: str) -> int:
        exp = self._expiry.get(name)
        if exp is None:
            return -1
        return max(0, int(exp - time.monotonic()))

    async def scan_iter(self, match: str = "*", count: int = 100):
        prefix = match.rstrip("*")
        for key in list(self._kv) + list(self._hashes):
            if key.startswith(prefix) and self._alive(key):
                yield key

    # ---------------- 命令 ----------------
    async def execute_command(self, *args: Any) -> Any:
        self.commands.append(args)
        name = args[0].decode() if isinstance(args[0], bytes) else str(args[0])

        if name == "FT.INFO":
            if not self._index_created:
                raise Exception("Unknown Index name")
            return ["index_name", args[1]]

        if name == "FT.CREATE":
            self._index_created = True
            return "OK"

        if name == "FT.SEARCH":
            return self._fake_knn(args)

        if name == "INFO":
            return f"used_memory:{sum(len(k) * 8 for k in self._kv)}\r\n"

        raise NotImplementedError(f"FakeRedis 未实现命令: {name}")

    def _fake_knn(self, args: tuple) -> list:
        query = args[2].decode() if isinstance(args[2], bytes) else str(args[2])
        match = _KB_FILTER.search(query)
        wanted_version = int(match.group(1)) if match else None

        blob = None
        for i, token in enumerate(args):
            if isinstance(token, bytes) and token and token not in (b"0", b"1"):
                try:
                    n = len(token) // 4
                    blob = list(struct.unpack(f"<{n}f", token))
                    break
                except struct.error:
                    continue

        scored: list[tuple[float, str]] = []
        for key, bucket in self._hashes.items():
            if not key.startswith("ops:v1:sem"):
                continue
            if not self._alive(key):
                continue
            if wanted_version is not None and int(bucket.get("kb_version", 0)) != wanted_version:
                continue
            vec_raw = bucket.get("vec")
            if not isinstance(vec_raw, bytes):
                continue
            vec = list(struct.unpack(f"<{len(vec_raw) // 4}f", vec_raw))
            if blob is not None and len(vec) != len(blob):
                continue
            dist = _cosine_distance(blob or [], vec) if blob else 1.0
            scored.append((dist, key))

        scored.sort(key=lambda x: x[0])
        scored = scored[:3]

        reply: list = [len(scored)]
        for dist, key in scored:
            bucket = self._hashes[key]
            reply.append(key)
            reply.append(
                [
                    "payload", bucket.get("payload", ""),
                    "__score", str(dist),
                    "kb_version", str(bucket.get("kb_version", "")),
                ]
            )
        return reply

    # ---------------- 测试辅助 ----------------
    def count_expiring(self, prefix: str) -> int:
        return sum(1 for k in self._hashes if k.startswith(prefix) and k in self._expiry)

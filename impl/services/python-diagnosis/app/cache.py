"""多级缓存（L1 进程内 TTL + L2 Redis）。

设计取舍：
- L1 用分片字典 + 单调时钟，避免全局锁；单 key 命中在微秒级
- L2 可选（Redis 不可用时自动降级为 L1-only，不抛错）
- 缓存只存"纯函数结果"，key 由请求的规范化指纹决定，天然幂等
- TTL 故意短（默认 30s）：设备健康是时效数据，宁可多算一次也不能给旧结论

为什么不缓存"降级结果"：
降级代表数据不足，缓存它会让系统在数据恢复后仍返回降级，属于"缓存放大故障"。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

SHARDS = 16


class TTLCache:
    """分片 TTL + LRU 缓存。非线程安全场景由 GIL + 单事件循环保证。"""

    def __init__(self, maxsize: int = 8192, ttl: float = 30.0) -> None:
        self.ttl = ttl
        self.per_shard = max(1, maxsize // SHARDS)
        self._shards: list[OrderedDict[str, tuple[float, Any]]] = [OrderedDict() for _ in range(SHARDS)]
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def _shard(self, key: str) -> OrderedDict[str, tuple[float, Any]]:
        return self._shards[hash(key) % SHARDS]

    def get(self, key: str) -> Any | None:
        shard = self._shard(key)
        item = shard.get(key)
        if item is None:
            self.misses += 1
            return None
        expire_at, value = item
        if expire_at < time.monotonic():
            shard.pop(key, None)
            self.misses += 1
            return None
        shard.move_to_end(key)
        self.hits += 1
        return value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        shard = self._shard(key)
        shard[key] = (time.monotonic() + (ttl if ttl is not None else self.ttl), value)
        shard.move_to_end(key)
        while len(shard) > self.per_shard:
            shard.popitem(last=False)
            self.evictions += 1

    def clear(self) -> None:
        for s in self._shards:
            s.clear()

    @property
    def size(self) -> int:
        return sum(len(s) for s in self._shards)

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "size": self.size,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
        }


@dataclass
class MultiLevelCache:
    """L1 + L2 组合。L2 为 None 时自动单级运行。"""

    l1: TTLCache = field(default_factory=lambda: TTLCache(maxsize=8192, ttl=30.0))
    l2: Any | None = None  # redis.asyncio.Redis
    prefix: str = "pdm:diag:"
    l2_ttl: int = 120
    l2_hits: int = 0
    l2_errors: int = 0

    @staticmethod
    def make_key(payload: dict[str, Any]) -> str:
        """规范化指纹：按键排序后序列化，保证同语义请求命中同一 key。"""
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]

    async def get(self, key: str) -> Any | None:
        hit = self.l1.get(key)
        if hit is not None:
            return hit
        if self.l2 is None:
            return None
        try:
            raw = await self.l2.get(self.prefix + key)
            if raw:
                value = json.loads(raw)
                self.l1.set(key, value)  # 回填 L1
                self.l2_hits += 1
                return value
        except Exception:  # noqa: BLE001 - 缓存故障不得影响主流程
            self.l2_errors += 1
        return None

    async def set(self, key: str, value: Any) -> None:
        self.l1.set(key, value)
        if self.l2 is None:
            return
        try:
            await self.l2.set(self.prefix + key, json.dumps(value, ensure_ascii=False), ex=self.l2_ttl)
        except Exception:  # noqa: BLE001
            self.l2_errors += 1

    async def get_or_compute(
        self,
        key: str,
        factory: Callable[[], Awaitable[Any]],
        cacheable: Callable[[Any], bool] | None = None,
    ) -> tuple[Any, bool]:
        """返回 (值, 是否命中缓存)。"""
        cached = await self.get(key)
        if cached is not None:
            return cached, True
        value = await factory()
        if cacheable is None or cacheable(value):
            await self.set(key, value)
        return value, False

    def stats(self) -> dict[str, Any]:
        s = self.l1.stats()
        s.update({"l2_enabled": self.l2 is not None, "l2_hits": self.l2_hits, "l2_errors": self.l2_errors})
        return s


async def build_l2(url: str | None):
    """尝试建立 Redis L2；失败返回 None（降级为单级缓存）。"""
    if not url:
        return None
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(url, encoding="utf-8", decode_responses=True, socket_timeout=0.2, socket_connect_timeout=0.2)
        await asyncio.wait_for(client.ping(), timeout=0.5)
        return client
    except Exception:  # noqa: BLE001
        return None

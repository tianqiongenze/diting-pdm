"""warmup：预热多级缓存。

冷启动问题：服务重启后 L1/L2 全空，前几百个请求全部打穿到计算层，
P95 会被拉到 500ms+。warmup 在启动后（或定时）用真实热点数据把缓存填满。

数据源：asset_metric_snapshot（每台设备 1 行最新 5 维指标）。
为什么用它而不是 feature_point：诊断需要完整 5 维才不降级，
而"降级结果不缓存"是刻意设计——用快照正好能构造出完整请求。

用法（容器内）：
    python -m app.warmup --limit 200 --concurrency 20
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from typing import Any

import msgspec

from . import repository
from .cache import MultiLevelCache, build_l2
from .schemas import DiagnoseRequest, Metrics
from .server import cache as shared_cache


def _build_request(tenant: str, asset: str, snap: dict[str, Any]) -> DiagnoseRequest:
    m = Metrics(
        vib_rms_mm_s=snap["vib_rms_mm_s"],
        vib_baseline_mm_s=snap["vib_baseline_mm_s"],
        kurtosis=snap["kurtosis"],
        temp_c=snap["temp_c"],
        temp_baseline_c=snap["temp_baseline_c"],
        current_dev_pct=snap["current_dev_pct"],
        trend_pct_per_day=snap["trend_pct_per_day"],
    )
    return DiagnoseRequest(tenant_id=tenant, asset_id=asset, metrics=m)


async def _warm_one(cache: MultiLevelCache, tenant: str, asset: str, snap: dict[str, Any],
                    sem: asyncio.Semaphore, stats: dict[str, int]) -> None:
    async with sem:
        req = _build_request(tenant, asset, snap)
        key = cache.make_key(domain_cache_key(req))
        if await cache.get(key) is not None:
            stats["already"] += 1
            return
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, domain_diagnose, req)
            # 只缓存完整维度结果（与服务端逻辑一致）
            if not result["degraded"]:
                await cache.set(key, result)
                stats["warmed"] += 1
            else:
                stats["degraded"] += 1
        except Exception:  # noqa: BLE001
            stats["failed"] += 1


def domain_cache_key(req: DiagnoseRequest) -> dict[str, Any]:
    from .domain import cache_key_payload

    return cache_key_payload(req)


def domain_diagnose(req: DiagnoseRequest) -> dict[str, Any]:
    from .domain import diagnose

    return diagnose(req)


async def run(limit: int, concurrency: int, tenants: list[str]) -> dict[str, Any]:
    t0 = time.time()
    cache = MultiLevelCache()
    cache.l2 = await build_l2(os.getenv("PDM_REDIS_URL"))

    stats = {"warmed": 0, "already": 0, "degraded": 0, "failed": 0}
    sem = asyncio.Semaphore(concurrency)

    for tenant in tenants:
        try:
            hot = repository.list_hot_assets(tenant, limit=limit)
        except Exception:  # noqa: BLE001 - 库不可用时 warmup 必须静默失败，不能影响服务启动
            continue
        if not hot:
            continue

        tasks = []
        for h in hot:
            asset = h["asset_id"]
            snap = repository.get_snapshot(tenant, asset)
            if not snap:
                stats["no_snapshot"] = stats.get("no_snapshot", 0) + 1
                continue
            # 只有快照齐全（5 维）才进 warmup，与"降级不缓存"对齐
            if None in (snap.get("vib_rms_mm_s"), snap.get("kurtosis"), snap.get("temp_c"),
                        snap.get("current_dev_pct"), snap.get("trend_pct_per_day")):
                stats["incomplete"] = stats.get("incomplete", 0) + 1
                continue
            tasks.append(_warm_one(cache, tenant, asset, snap, sem, stats))

        if tasks:
            await asyncio.gather(*tasks)

    stats["elapsed_s"] = round(time.time() - t0, 2)
    stats["cache"] = cache.stats()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--tenants", default=os.getenv("PDM_WARMUP_TENANTS", "site-a,site-b"))
    a = ap.parse_args()

    result = asyncio.run(run(a.limit, a.concurrency, [t.strip() for t in a.tenants.split(",") if t.strip()]))
    print(f"warmup done: {result}")


if __name__ == "__main__":
    main()

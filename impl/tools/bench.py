"""轻量压测：对比同一应用在不同 ASGI 服务器下的吞吐与延迟。

用法：
    python tools/bench.py http://127.0.0.1:18081 2000 50
    python tools/bench.py http://127.0.0.1:18082 2000 50

不引入 locust/wrk，避免额外依赖；httpx 的 async 客户端足够压出差异。
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time

import httpx

PAYLOAD = {
    "tenant_id": "site-a",
    "asset_id": "KA-101",
    "metrics": {
        "vib_rms_mm_s": 4.0,
        "vib_baseline_mm_s": 2.0,
        "kurtosis": 6.5,
        "temp_c": 68.0,
        "temp_baseline_c": 55.0,
        "current_dev_pct": 3.0,
        "trend_pct_per_day": 1.2,
    },
}
HEADERS = {"X-Tenant-Id": "site-a", "Content-Type": "application/json"}


async def one(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str, lat: list[float], errors: list[int]) -> None:
    async with sem:
        t0 = time.perf_counter()
        try:
            r = await client.post(url, json=PAYLOAD, headers=HEADERS)
            if r.status_code != 200:
                errors.append(r.status_code)
        except Exception:  # noqa: BLE001
            errors.append(-1)
        lat.append((time.perf_counter() - t0) * 1000)


async def bench(base: str, total: int, concurrency: int) -> None:
    url = base.rstrip("/") + "/v1/diagnose"
    lat: list[float] = []
    errors: list[int] = []
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        # 预热，避免首请求编译路径影响统计
        for _ in range(min(50, total)):
            await one(client, sem, url, [], [])
        lat.clear()
        t0 = time.perf_counter()
        await asyncio.gather(*[one(client, sem, url, lat, errors) for _ in range(total)])
        elapsed = time.perf_counter() - t0

    lat.sort()

    def p(q: float) -> float:
        idx = min(len(lat) - 1, int(len(lat) * q))
        return lat[idx]

    print(f"target      : {url}")
    print(f"requests    : {total}  concurrency={concurrency}")
    print(f"elapsed     : {elapsed:.2f}s")
    print(f"throughput  : {total / elapsed:,.0f} req/s")
    print(f"latency p50 : {p(0.50):.2f} ms")
    print(f"latency p95 : {p(0.95):.2f} ms")
    print(f"latency p99 : {p(0.99):.2f} ms")
    print(f"mean        : {statistics.mean(lat):.2f} ms")
    print(f"errors      : {len(errors)} {set(errors) if errors else ''}")


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18081"
    total = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
    conc = int(sys.argv[3]) if len(sys.argv) > 3 else 50
    asyncio.run(bench(base, total, conc))

"""诊断服务入口（Litestar + Granian）。

为什么换掉 FastAPI：
- Litestar 的路由/DI/序列化链路更薄，msgspec 原生集成，单请求开销更低
- Granian（Rust/Hyper + uvloop）在同样 worker 数下吞吐显著高于 uvicorn+h11
- 两者都保留 OpenAPI，不影响对外契约

并发模型：
- HTTP 层全异步
- CPU 密集（numpy/scipy）通过 asyncio.to_thread 下放到线程池，避免阻塞事件循环
- 结果经多级缓存（L1 进程内 / L2 Redis）短路
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

import msgspec
from litestar import Litestar, Request, get, post
from litestar.exceptions import HTTPException
from litestar.status_codes import HTTP_400_BAD_REQUEST, HTTP_403_FORBIDDEN, HTTP_422_UNPROCESSABLE_ENTITY

from . import domain, repository
from .cache import MultiLevelCache, build_l2
from .schemas import DiagnoseRequest, Metrics

SERVICE_NAME = "python-diagnosis"
SERVICE_VERSION = "0.2.0"
MAX_WAVEFORM_SAMPLES = 1_000_000
MIN_WAVEFORM_SAMPLES = 256

cache = MultiLevelCache()


@get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME, "version": SERVICE_VERSION}


@get("/version")
async def version() -> dict[str, str]:
    return {"service": SERVICE_NAME, "version": SERVICE_VERSION, "model": domain.MODEL_VERSION}


@get("/cache/stats")
async def cache_stats() -> dict[str, Any]:
    return cache.stats()


@get("/warmup")
async def warmup_endpoint(limit: int = 100) -> dict[str, Any]:
    """预热缓存：按关键度取热点设备，用快照数据构造完整 5 维请求。"""
    from .warmup import run as warmup_run

    tenants = [t.strip() for t in os.getenv("PDM_WARMUP_TENANTS", "site-a,site-b").split(",") if t.strip()]
    return await warmup_run(limit=limit, concurrency=20, tenants=tenants)


@post("/v1/diagnose", status_code=200)
async def diagnose_endpoint(request: Request, data: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()

    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")

    try:
        req = msgspec.convert(data, DiagnoseRequest, strict=False)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=HTTP_422_UNPROCESSABLE_ENTITY, detail=f"invalid payload: {exc}") from exc

    if req.tenant_id != tenant:
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="tenant mismatch")

    # 数据库兜底：请求缺维度时，从快照表补全（一次查询拿全 5 维，避免降级）
    if req.metrics.vib_rms_mm_s is None and os.getenv("PDM_DB_ENABLED", "1") == "1":
        try:
            snap = await asyncio.to_thread(repository.get_snapshot, tenant, req.asset_id)
        except Exception:  # noqa: BLE001 - 库不可用不阻塞诊断，仅回退为请求自带数据
            snap = None
        if snap:
            m = req.metrics
            if m.vib_rms_mm_s is None:
                m.vib_rms_mm_s = snap["vib_rms_mm_s"]
                m.vib_baseline_mm_s = snap["vib_baseline_mm_s"]
            if m.kurtosis is None:
                m.kurtosis = snap["kurtosis"]
            if m.temp_c is None:
                m.temp_c = snap["temp_c"]
                m.temp_baseline_c = snap["temp_baseline_c"]
            if m.current_dev_pct is None:
                m.current_dev_pct = snap["current_dev_pct"]
            if m.trend_pct_per_day is None:
                m.trend_pct_per_day = snap["trend_pct_per_day"]

    if req.waveform is not None:
        n = len(req.waveform.samples)
        if n < MIN_WAVEFORM_SAMPLES:
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"waveform too short: {n} < {MIN_WAVEFORM_SAMPLES}",
            )
        if n > MAX_WAVEFORM_SAMPLES:
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"waveform too long: {n} > {MAX_WAVEFORM_SAMPLES}",
            )

    key = cache.make_key(domain.cache_key_payload(req))

    async def _compute() -> dict[str, Any]:
        try:
            # CPU 密集下放线程池；numpy 在 C 层会释放 GIL
            return await asyncio.to_thread(domain.diagnose, req)
        except domain.NoUsableMetrics as exc:
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                detail="no usable metrics; at least one of vib/kurtosis/temp/current/trend is required",
            ) from exc

    # 降级结果不缓存：否则数据恢复后仍返回"数据不足"
    result, cached = await cache.get_or_compute(key, _compute, cacheable=lambda r: not r["degraded"])

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    # 诊断落库 + 读模型刷新（失败不影响响应）
    if os.getenv("PDM_DB_ENABLED", "1") == "1":
        def _persist() -> None:
            try:
                out = {**result, "asset_id": req.asset_id}
                repository.save_diagnosis(tenant, out)
                repository.refresh_latest(
                    tenant, req.asset_id, result["hi"], result["level"],
                    result["contributions"][0]["s"] if result["contributions"] else 0.0, 0.0,
                    None, len(result["contributions"]),
                )
            except Exception:  # noqa: BLE001
                pass

        await asyncio.to_thread(_persist)

    return {
        "trace_id": str(uuid.uuid4()),
        "tenant_id": tenant,
        "asset_id": req.asset_id,
        "hi": result["hi"],
        "level": result["level"],
        "sla_hours": result["sla_hours"],
        "degraded": result["degraded"],
        "degraded_reason": result["degraded_reason"],
        "contributions": result["contributions"],
        "hypotheses": result["hypotheses"],
        "evidence": result["evidence"],
        "model_version": result["model_version"],
        "elapsed_ms": elapsed_ms,
        "cached": cached,
        "disclaimer": domain.DISCLAIMER,
    }


async def _on_startup() -> None:
    cache.l2 = await build_l2(os.getenv("PDM_REDIS_URL"))

app = Litestar(
    route_handlers=[healthz, version, cache_stats, warmup_endpoint, diagnose_endpoint],
    on_startup=[_on_startup],
    debug=os.getenv("PDM_ENV", "prod") != "prod",
)

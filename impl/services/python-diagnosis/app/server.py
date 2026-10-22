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
from litestar.response import Response
from litestar.exceptions import HTTPException
from litestar.status_codes import HTTP_400_BAD_REQUEST, HTTP_403_FORBIDDEN, HTTP_409_CONFLICT, HTTP_422_UNPROCESSABLE_ENTITY

from . import decision, domain, events, repository
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


@get("/dashboard")
async def dashboard(request: Request) -> Any:
    """健康看板（静态页，ConfigMap 挂载到 /app/static/dashboard.html）。

    鉴权：HTTP Basic Auth。账号密码由 PDM_DASHBOARD_USER / PDM_DASHBOARD_PASS
    注入（K8s Secret）。未配置时拒绝访问（fail-closed，不提供匿名降级）。
    """
    user = os.getenv("PDM_DASHBOARD_USER", "")
    pwd = os.getenv("PDM_DASHBOARD_PASS", "")
    if not user or not pwd:
        return Response(content="<h3>dashboard auth not configured</h3>",
                        media_type="text/html; charset=utf-8", status_code=503)
    auth = request.headers.get("Authorization", "")
    if not _check_basic_auth(auth, user, pwd):
        return Response(content="<h3>401 Unauthorized</h3>",
                        media_type="text/html; charset=utf-8", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="diting-pdm"'})

    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "dashboard.html")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return Response(content=fh.read(), media_type="text/html; charset=utf-8")
    return Response(
        content="<h3>dashboard not mounted</h3><p>ConfigMap pdm-dashboard 未挂载</p>",
        media_type="text/html; charset=utf-8",
    )


def _check_basic_auth(header: str, expect_user: str, expect_pass: str) -> bool:
    """常量时间比较，防时序攻击。"""
    import base64
    import hmac

    if not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header[6:]).decode()
        got_user, _, got_pass = raw.partition(":")
    except Exception:  # noqa: BLE001
        return False
    return (hmac.compare_digest(got_user.encode(), expect_user.encode())
            and hmac.compare_digest(got_pass.encode(), expect_pass.encode()))


@get("/events/stats")
async def events_stats() -> dict[str, Any]:
    return events.stats_snapshot()


@get("/warmup")
async def warmup_endpoint(limit: int = 100) -> dict[str, Any]:
    """预热缓存：按关键度取热点设备，用快照数据构造完整 5 维请求。"""
    from .warmup import run as warmup_run

    tenants = [t.strip() for t in os.getenv("PDM_WARMUP_TENANTS", "site-a,site-b").split(",") if t.strip()]
    return await warmup_run(limit=limit, concurrency=20, tenants=tenants)


@post("/v1/consult", status_code=200)
async def consult_endpoint(request: Request) -> dict[str, Any]:
    payload: dict[str, Any] = await request.json()
    """决策会诊：四角色并行 + 独立评估者 → 维护方案卡。

    入参最小化：{tenant_id, asset_id}，其余从库读（快照/资产/备件/排产）。
    可选覆盖：spare/crew/plan/downtime_cost_per_hour。
    """
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")

    tenant_id = str(payload.get("tenant_id", ""))
    asset_id = str(payload.get("asset_id", ""))
    if not tenant_id or not asset_id:
        raise HTTPException(status_code=HTTP_422_UNPROCESSABLE_ENTITY, detail="tenant_id and asset_id required")
    if tenant_id != tenant:
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="tenant mismatch")

    def _run() -> dict[str, Any]:
        asset = repository.get_asset(tenant, asset_id) or {"criticality": "B", "category": "UNKNOWN"}
        snap = repository.get_snapshot(tenant, asset_id)
        if not snap:
            raise HTTPException(status_code=HTTP_422_UNPROCESSABLE_ENTITY, detail="no snapshot for asset")

        # 先用快照做一次诊断，拿到 HI / 等级 / 假设
        m = Metrics(
            vib_rms_mm_s=snap["vib_rms_mm_s"],
            vib_baseline_mm_s=snap["vib_baseline_mm_s"],
            kurtosis=snap["kurtosis"],
            temp_c=snap["temp_c"],
            temp_baseline_c=snap["temp_baseline_c"],
            current_dev_pct=snap["current_dev_pct"],
            trend_pct_per_day=snap["trend_pct_per_day"],
        )
        diag = domain.diagnose(DiagnoseRequest(tenant_id=tenant, asset_id=asset_id, metrics=m))
        diag["conclusion"] = (
            f"健康指数 {diag['hi']}（{diag['level']}），"
            f"主要贡献 {diag['contributions'][0]['name'] if diag['contributions'] else 'NA'}"
        )

        # 约束：允许请求覆盖，否则用默认值（后续接 EAM/MES）
        spare = payload.get("spare") or {"sku": "SKF-6316-C3", "qty": 1, "eta": None}
        crew = payload.get("crew") or {"available": 3, "required": 2, "hours": 6}
        plan = payload.get("plan") or {"load_pct": 82, "bottleneck": "总装线", "window_available": "周日 08:00"}

        opinions = [
            decision.role_shopfloor(diag, snap),
            decision.role_reliability(diag, asset, payload.get("history")),
            decision.role_production(diag, plan),
            decision.role_maintenance(diag, spare, crew),
        ]
        evaluation = decision.evaluate(opinions)
        card = decision.build_card(
            tenant, asset_id, diag, snap, opinions, evaluation, spare, crew, plan,
            downtime_cost_per_hour=float(payload.get("downtime_cost_per_hour", 6000.0)),
        )
        # 方案卡落库 + 工单闭环起点（同步段，随后统一在 async 侧执行）
        if os.getenv("PDM_DB_ENABLED", "1") == "1":
            try:
                repository.save_recommendation_card(tenant, card)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] card persist failed: {exc}", flush=True)

        events.emit(
            "com.yadea.pdm.decision.cardCreated.v1",
            tenant,
            subject=f"{tenant}/{asset_id}",
            data={
                "card_id": card["card_id"],
                "asset_id": asset_id,
                "verdict": card["verdict"],
                "net_benefit": card["economics"]["net_benefit"],
                "options": len(card["options"]),
                "conflicts": len(card["conflicts"]),
            },
        )
        return {"card": card, "diagnosis": diag, "evaluation": {
            "consistency": evaluation.consistency,
            "conflicts": evaluation.conflicts,
            "evidence_gaps": evaluation.evidence_gaps,
            "risk_note": evaluation.risk_note,
        }}

    return await asyncio.to_thread(_run)


@post("/v1/cards/{card_id:str}/confirm", status_code=200)
async def confirm_card(request: Request) -> dict[str, Any]:
    """人工确认/驳回方案卡。齐备 required_roles 后状态自动转 CONFIRMED。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    payload = await request.json()
    role = str(payload.get("role", ""))
    user_id = str(payload.get("user_id", ""))
    decision = str(payload.get("decision", "confirm"))
    if not role or not user_id:
        raise HTTPException(status_code=HTTP_422_UNPROCESSABLE_ENTITY, detail="role and user_id required")
    if decision not in ("confirm", "reject"):
        raise HTTPException(status_code=HTTP_422_UNPROCESSABLE_ENTITY, detail="decision must be confirm|reject")

    card_id = request.path_params["card_id"]

    def _run() -> dict[str, Any]:
        return repository.confirm_recommendation_card(
            tenant, card_id, role, user_id, decision, str(payload.get("comment", ""))
        )

    try:
        return await asyncio.to_thread(_run)
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail=str(exc)) from exc


@get("/v1/cards")
async def list_cards(request: Request, limit: int = 50) -> dict[str, Any]:
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    rows = await asyncio.to_thread(repository.list_pending_cards, tenant, limit)
    return {"tenant_id": tenant, "count": len(rows), "cards": rows}


@post("/mcp", status_code=200)
async def mcp_endpoint(request: Request) -> dict[str, Any]:
    payload: dict[str, Any] = await request.json()
    """最小 MCP 服务端（JSON-RPC 2.0 over HTTP）。

    支持方法：initialize / tools/list / tools/call
    工具：get_asset / assess_health / diagnose_asset / consult_maintenance / list_hot_assets
    """
    from .mcp import handle_rpc

    tenant = request.headers.get("X-Tenant-Id")
    return await asyncio.to_thread(handle_rpc, payload, tenant)


@post("/v1/diagnose", status_code=200)
async def diagnose_endpoint(request: Request) -> dict[str, Any]:
    started = time.perf_counter()
    payload: dict[str, Any] = await request.json()

    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")

    try:
        req = msgspec.convert(payload, DiagnoseRequest, strict=False)
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

    # 领域事件：诊断定稿（失败不影响响应）
    events.emit(
        "com.yadea.pdm.diagnosis.finalized.v1",
        tenant,
        subject=f"{tenant}/{req.asset_id}",
        data={
            "diag_id": result.get("diag_id"),
            "asset_id": req.asset_id,
            "hi": result["hi"],
            "level": result["level"],
            "degraded": result["degraded"],
            "cached": cached,
        },
        trace_id=str(uuid.uuid4()),
    )

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
    events.init(os.getenv("PDM_REDIS_URL"))

app = Litestar(
    route_handlers=[healthz, version, cache_stats, events_stats, warmup_endpoint, diagnose_endpoint, consult_endpoint, confirm_card, list_cards, mcp_endpoint, dashboard],
    on_startup=[_on_startup],
    debug=os.getenv("PDM_ENV", "prod") != "prod",
)

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
from litestar import Litestar, Request, get, post, put
from litestar.response import Response
from litestar.exceptions import HTTPException
from litestar.status_codes import HTTP_400_BAD_REQUEST, HTTP_403_FORBIDDEN, HTTP_404_NOT_FOUND, HTTP_409_CONFLICT, HTTP_422_UNPROCESSABLE_ENTITY

from . import auth as auth_mod
from . import decision, domain, events, integration, repository
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


@get("/v1/assets/{asset_id:str}/trend")
async def asset_trend(request: Request, days: int = 7) -> dict[str, Any]:
    """设备 HI 趋势（看板折线图）。days 1-90，超出自动截断。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    if os.getenv("PDM_DB_ENABLED", "1") != "1":
        return {"asset_id": request.path_params["asset_id"], "points": 0, "series": [], "summary": {}}
    try:
        return await asyncio.to_thread(
            repository.get_health_trend, tenant, request.path_params["asset_id"], days
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)[:120]) from exc


@get("/integration/stats")
async def integration_stats() -> dict[str, Any]:
    """EAM/MES 出站信箱状态。"""
    if os.getenv("PDM_DB_ENABLED", "1") != "1":
        return {"enabled": False}
    try:
        return await asyncio.to_thread(integration.outbox_stats)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:120]}


# 前端页面白名单：文件名与 URL 路径同名，均从 ConfigMap 挂载的 /app/static 读取
_PAGES = {"dashboard": "dashboard.html", "screen": "screen.html", "console": "console.html"}


def _serve_page(request: Request, page: str) -> Any:
    """前端页面统一入口（Basic Auth + 静态文件）。

    鉴权：HTTP Basic Auth，账号密码由 PDM_DASHBOARD_USER / PDM_DASHBOARD_PASS
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

    static = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    path = os.path.join(static, _PAGES[page])
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return Response(content=fh.read(), media_type="text/html; charset=utf-8")
    return Response(
        content=f"<h3>{page} not mounted</h3><p>ConfigMap pdm-dashboard 缺少 "
                f"{_PAGES[page]}</p>",
        media_type="text/html; charset=utf-8",
    )


@get("/dashboard")
async def dashboard(request: Request) -> Any:
    """运营看板（浅色管理台入口，兼容旧链接）。"""
    return _serve_page(request, "dashboard")


@get("/screen")
async def screen_page(request: Request) -> Any:
    """大屏驾驶舱（深色，1920×1080 全屏投屏用）。"""
    return _serve_page(request, "screen")


@get("/console")
async def console_page(request: Request) -> Any:
    """管理台（多视图 SPA：看板/设备/工单/诊断）。"""
    return _serve_page(request, "console")


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
        # 先落库再做后续动作：去重窗口内同设备同 verdict 会复用已有卡，
        # 必须先拿到「实际生效的 card_id」，否则 EAM 会为一张并未新建的卡
        # 重复建工单（幂等键是 card:{id}，id 不同就绕过了幂等）。
        deduplicated = False
        if os.getenv("PDM_DB_ENABLED", "1") == "1":
            try:
                effective_id = repository.save_recommendation_card(tenant, card)
                if effective_id and effective_id != card.get("card_id"):
                    card["card_id"] = effective_id
                    card["deduplicated"] = True
                    deduplicated = True
                    # 复用旧卡时必须回填真实状态：新建卡的 human_confirm.status
                    # 恒为 PENDING，而旧卡可能已 CONFIRMED/REJECTED。
                    # 不回填会让前端显示"待确认"、点确认却收到 409。
                    old = repository.get_recommendation_card(tenant, effective_id)
                    if old:
                        card["human_confirm"]["status"] = old.get("status", "PENDING")
                        card["human_confirm"]["confirmed_roles"] = \
                            repository.list_confirmed_roles(tenant, effective_id)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] card persist failed: {exc}", flush=True)

        # 红色等级即触发工单预入队（不等人工确认，避免延误）
        # 理由：ACT_NOW 等级下等确认会错过 4h SLA，先建单后由人工确认/撤销
        # 去重复用时跳过——该卡此前已入队过，再入会重复建单
        if (os.getenv("PDM_EAM_ENABLED", "1") == "1"
                and card.get("verdict") == "ACT_NOW" and not deduplicated):
            try:
                integration.enqueue(tenant, "eam", "workOrder.create",
                                    f"card:{card['card_id']}",
                                    integration.work_order_payload_from_card(card, tenant))
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] eam enqueue failed: {exc}", flush=True)

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


@post("/v1/alerts/sync", status_code=200)
async def sync_alerts_endpoint(request: Request) -> dict[str, Any]:
    """从当前健康状态生成告警（幂等）。FR-01 / UC-01-2。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    threshold = float(payload.get("threshold_hi", 70))
    limit = int(payload.get("limit", 200))
    res = await asyncio.to_thread(repository.sync_alerts, tenant, threshold, limit)
    return {"tenant_id": tenant, "threshold_hi": threshold, **res}


@get("/v1/alerts")
async def list_alerts_endpoint(request: Request, status: str = "", level: str = "",
                               limit: int = 100) -> dict[str, Any]:
    """告警列表，超时优先。FR-01 / UC-01-1。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    rows = await asyncio.to_thread(repository.list_alerts, tenant, status, level, limit)
    return {"tenant_id": tenant, "count": len(rows), "alerts": rows}


@post("/v1/alerts/{alert_id:str}/ack", status_code=200)
async def ack_alert_endpoint(request: Request, alert_id: str) -> dict[str, Any]:
    """处置告警：ack / escalate / close。FR-01 / UC-01-3、UC-01-4。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    action = str(payload.get("action", "ack"))
    by = str(payload.get("by") or "unknown")
    comment = str(payload.get("comment") or "")
    # 权限：ack → alert.ack，escalate → alert.escalate，close → alert.close
    perm = {"ack": "alert.ack", "escalate": "alert.escalate",
            "close": "alert.close"}.get(action, "alert.ack")
    try:
        ident = auth_mod.require(request.headers.get("X-Auth-Token"), perm)
        if ident:
            by = ident["sub"]
    except auth_mod.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        return await asyncio.to_thread(repository.ack_alert, tenant, alert_id, action, by, comment)
    except ValueError as exc:
        msg = str(exc)
        code = HTTP_404_NOT_FOUND if "not found" in msg else HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=msg) from exc


@get("/v1/diagnoses")
async def list_diagnoses_endpoint(request: Request, asset_id: str = "",
                                  limit: int = 50) -> dict[str, Any]:
    """诊断历史。FR-03 / UC-03-1、UC-03-2。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    rows = await asyncio.to_thread(repository.list_diagnoses, tenant, asset_id, limit)
    return {"tenant_id": tenant, "count": len(rows), "items": rows}


@get("/v1/stats/cost")
async def cost_stats_endpoint(request: Request, limit: int = 10) -> dict[str, Any]:
    """成本效益聚合。FR-04 / UC-04-1~4。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    data = await asyncio.to_thread(repository.cost_stats, tenant, limit)
    return {"tenant_id": tenant, **data}


@get("/v1/org/tree")
async def org_tree_endpoint(request: Request) -> dict[str, Any]:
    """组织树 + 节点健康汇总。FR-05 / UC-05-1~3。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    nodes = await asyncio.to_thread(repository.org_tree, tenant)
    return {"tenant_id": tenant, "count": len(nodes), "nodes": nodes}


@get("/v1/plans")
async def list_plans_endpoint(request: Request, limit: int = 200) -> dict[str, Any]:
    """保养计划列表。技术债 #2。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    rows = await asyncio.to_thread(repository.list_plans, tenant, limit)
    return {"tenant_id": tenant, "count": len(rows), "plans": rows}


@post("/v1/inspections/generate", status_code=200)
async def generate_inspections_endpoint(request: Request) -> dict[str, Any]:
    """按保养计划生成点检任务（幂等）。需 plan.edit 权限。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        auth_mod.require(request.headers.get("X-Auth-Token"), "plan.edit")
    except auth_mod.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    res = await asyncio.to_thread(repository.generate_inspections, tenant,
                                 int(payload.get("days", 7)), int(payload.get("limit", 100)))
    return {"tenant_id": tenant, **res}


@get("/v1/inspections")
async def list_inspections_endpoint(request: Request, status: str = "",
                                    asset_id: str = "", limit: int = 100) -> dict[str, Any]:
    """点检任务列表，逾期优先。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    rows = await asyncio.to_thread(repository.list_inspections, tenant, status, asset_id, limit)
    return {"tenant_id": tenant, "count": len(rows), "tasks": rows}


@post("/v1/inspections/{task_id:str}/submit", status_code=200)
async def submit_inspection_endpoint(request: Request, task_id: str) -> dict[str, Any]:
    """提交点检结果。需 inspect.execute 权限。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    by = "unknown"
    try:
        ident = auth_mod.require(request.headers.get("X-Auth-Token"), "inspect.execute")
        if ident:
            by = ident["sub"]
    except auth_mod.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    if not payload.get("by"):
        payload["by"] = by
    try:
        return await asyncio.to_thread(repository.submit_inspection, tenant, task_id,
                                      payload.get("items") or [],
                                      str(payload.get("by") or "unknown"),
                                      str(payload.get("note") or ""))
    except ValueError as exc:
        msg = str(exc)
        code = HTTP_404_NOT_FOUND if "not found" in msg else HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=msg) from exc


@get("/v1/stats/overview")
async def overview_stats_endpoint(request: Request) -> dict[str, Any]:
    """总览：在监设备总数、已评估覆盖度、四级分布。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    data = await asyncio.to_thread(repository.overview_stats, tenant)
    return {"tenant_id": tenant, **data}


@get("/v1/stats/inspection")
async def inspection_stats_endpoint(request: Request) -> dict[str, Any]:
    """点检与保养汇总。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    data = await asyncio.to_thread(repository.inspection_stats, tenant)
    return {"tenant_id": tenant, **data}


# ---------- 工单执行跟踪 ----------
@post("/v1/worklogs", status_code=201)
async def append_worklog_endpoint(request: Request) -> dict[str, Any]:
    """记录工单执行动作（派工/开始/完成/验收/关闭）。需 card.confirm 权限。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    ident = None
    try:
        ident = auth_mod.require(request.headers.get("X-Auth-Token"), "card.confirm")
    except auth_mod.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    actor = (ident or {}).get("sub") or str(payload.get("actor") or "unknown")
    try:
        return await asyncio.to_thread(
            repository.append_work_log, tenant, str(payload.get("card_id") or ""),
            str(payload.get("action") or ""), actor, str(payload.get("note") or ""),
            payload.get("actual_cost"))
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@get("/v1/worklogs")
async def list_worklogs_endpoint(request: Request, card_id: str = "",
                                 limit: int = 200) -> dict[str, Any]:
    """工单执行流水。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    rows = await asyncio.to_thread(repository.list_work_logs, tenant, card_id, limit)
    return {"tenant_id": tenant, "count": len(rows), "items": rows}


# ---------- 备件管理 ----------
@get("/v1/spares")
async def list_spares_endpoint(request: Request, low_only: bool = False) -> dict[str, Any]:
    """备件库存。low_only=true 只返回低于安全库存的。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    rows = await asyncio.to_thread(repository.list_spares, tenant, low_only)
    low = [r for r in rows if r["is_low"]]
    return {"tenant_id": tenant, "count": len(rows), "low_count": len(low),
            "total_value": round(sum(r["stock_value"] for r in rows), 2), "items": rows}


@post("/v1/spares/{part_code:str}/move", status_code=200)
async def move_spare_endpoint(request: Request, part_code: str) -> dict[str, Any]:
    """备件入库（delta>0，需 plan.edit）或领用（delta<0，需 card.confirm）。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    delta = int(payload.get("delta") or 0)
    if delta == 0:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="delta must not be 0")
    perm = "plan.edit" if delta > 0 else "card.confirm"
    ident = None
    try:
        ident = auth_mod.require(request.headers.get("X-Auth-Token"), perm)
    except auth_mod.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    actor = (ident or {}).get("sub") or str(payload.get("actor") or "unknown")
    try:
        return await asyncio.to_thread(
            repository.move_spare, tenant, part_code, delta, actor,
            str(payload.get("card_id") or ""), str(payload.get("asset_id") or ""),
            str(payload.get("note") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@get("/v1/spares/issues")
async def list_spare_issues_endpoint(request: Request, limit: int = 100) -> dict[str, Any]:
    """备件领用/入库记录。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    rows = await asyncio.to_thread(repository.list_spare_issues, tenant, limit)
    return {"tenant_id": tenant, "count": len(rows), "items": rows}


# ---------- 知识库 ----------
@get("/v1/knowledge")
async def list_cases_endpoint(request: Request, q: str = "",
                              limit: int = 100) -> dict[str, Any]:
    """故障案例检索（标题/根因/标签/设备号）。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    rows = await asyncio.to_thread(repository.list_cases, tenant, q, limit)
    return {"tenant_id": tenant, "count": len(rows), "items": rows}


@post("/v1/knowledge", status_code=201)
async def create_case_endpoint(request: Request) -> dict[str, Any]:
    """新增故障案例。需 card.confirm 权限。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        auth_mod.require(request.headers.get("X-Auth-Token"), "card.confirm")
    except auth_mod.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    try:
        return await asyncio.to_thread(repository.create_case, tenant, payload)
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


# ---------- 告警规则 ----------
@get("/v1/rules")
async def list_rules_endpoint(request: Request) -> dict[str, Any]:
    """告警规则列表（阈值可配置）。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    rows = await asyncio.to_thread(repository.list_alert_rules, tenant)
    return {"tenant_id": tenant, "count": len(rows), "items": rows}


@put("/v1/rules/{rule_id:str}")
async def upsert_rule_endpoint(request: Request, rule_id: str) -> dict[str, Any]:
    """新增/修改告警规则。需 plan.approve 权限（改阈值影响告警分级）。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        auth_mod.require(request.headers.get("X-Auth-Token"), "plan.approve")
    except auth_mod.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    payload["rule_id"] = rule_id
    try:
        return await asyncio.to_thread(repository.upsert_alert_rule, tenant, payload)
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@post("/v1/auth/login", status_code=200)
async def login_endpoint(request: Request) -> dict[str, Any]:
    """登录签发 token。技术债 #4。"""
    tenant = request.headers.get("X-Tenant-Id") or "site-a"
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    username = str(payload.get("username") or "")
    password = str(payload.get("password") or "")
    if not username or not password:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST,
                            detail="username and password required")
    user = await asyncio.to_thread(repository.get_user, tenant, username)
    if not user or not user.get("enabled"):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not auth_mod.verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    tok = auth_mod.issue_token(username, user["role"], tenant)
    return {"tenant_id": tenant, "username": username,
            "role": user["role"], "display_name": user.get("display_name"),
            "role_label": auth_mod.ROLE_LABELS.get(user["role"], user["role"]),
            "permissions": sorted(auth_mod.PERMISSIONS.get(user["role"], set())),
            **tok}


@get("/v1/auth/me")
async def me_endpoint(request: Request) -> dict[str, Any]:
    """校验 token 并返回当前身份。"""
    token = (request.headers.get("X-Auth-Token") or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = auth_mod.decode_token(token)
    except auth_mod.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {**payload,
            "role_label": auth_mod.ROLE_LABELS.get(payload.get("role", ""), ""),
            "permissions": sorted(auth_mod.PERMISSIONS.get(payload.get("role", ""), set()))}


@get("/v1/users")
async def list_users_endpoint(request: Request) -> dict[str, Any]:
    """用户列表（需 user.view 权限）。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        auth_mod.require(request.headers.get("X-Auth-Token"), "user.view")
    except auth_mod.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    rows = await asyncio.to_thread(repository.list_users, tenant)
    return {"tenant_id": tenant, "count": len(rows), "users": rows}


@get("/v1/assets/{asset_id:str}/rul")
async def asset_rul_endpoint(request: Request, asset_id: str,
                             critical_hi: float = 40.0) -> dict[str, Any]:
    """单设备剩余寿命估算（线性外推，可解释）。技术债 #3。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    est = await asyncio.to_thread(repository.estimate_rul, tenant, asset_id, critical_hi)
    return {"tenant_id": tenant, **est}


@post("/v1/rul/refresh", status_code=200)
async def rul_refresh_endpoint(request: Request) -> dict[str, Any]:
    """批量重算 RUL 并写回 read_asset_health。供 CronJob 调用。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    res = await asyncio.to_thread(repository.refresh_rul, tenant,
                                 float(payload.get("critical_hi", 40)))
    return {"tenant_id": tenant, **res}


@get("/v1/stats/system")
async def system_stats_endpoint(request: Request) -> dict[str, Any]:
    """系统运维总览。FR-06 / UC-06-1~5。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    tables = await asyncio.to_thread(repository.system_stats, tenant)
    return {"tenant_id": tenant, **tables}


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


async def _run_single_diagnose(tenant: str, req: DiagnoseRequest) -> dict[str, Any]:
    """诊断核心：快照补全 → 规则计算 → 落库 → 读模型刷新 → 领域事件。

    单点（POST /v1/diagnose）与批量（POST /v1/diagnose/batch）共用，
    避免两条路径行为漂移。返回响应体。
    """
    started = time.perf_counter()
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


@post("/v1/diagnose/batch", status_code=200)
async def diagnose_batch_endpoint(request: Request) -> dict[str, Any]:
    """批量诊断：按租户全量（或前缀）跑诊断，刷新读模型。

    存在意义：这是生产环境唯一能让 read_asset_health 覆盖全部在监设备的入口。
    此前只有测试脚本会逐台调 /v1/diagnose，导致 1200 台设备里只有被人工点过的
    几台有健康指数。由 CronJob `batch-diagnose` 定期调用。

    需 plan.edit 权限（属运维/计划动作，不是日常读操作）。
    """
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        auth_mod.require(request.headers.get("X-Auth-Token"), "plan.edit")
    except auth_mod.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    limit = max(1, min(int(payload.get("limit", 2000)), 5000))
    prefix = str(payload.get("asset_prefix") or "")

    assets = await asyncio.to_thread(repository.list_asset_ids, tenant, prefix, limit)
    if not assets:
        return {"tenant_id": tenant, "total": 0, "ok": 0, "failed": 0,
                "detail": "no assets matched"}

    sem = asyncio.Semaphore(8)          # 诊断是 CPU 密集，8 并发足够且不挤爆连接池
    failures: list[dict[str, str]] = []

    async def _one(asset_id: str) -> bool:
        async with sem:
            try:
                req = msgspec.convert({"tenant_id": tenant, "asset_id": asset_id},
                                      DiagnoseRequest, strict=False)
                await _run_single_diagnose(tenant, req)
                return True
            except Exception as exc:  # noqa: BLE001
                failures.append({"asset_id": asset_id, "error": str(exc)[:120]})
                return False

    t0 = time.perf_counter()
    done = await asyncio.gather(*[_one(a) for a in assets])
    ok = sum(1 for x in done if x)
    return {
        "tenant_id": tenant,
        "total": len(assets),
        "ok": ok,
        "failed": len(failures),
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "failures": failures[:10],
    }


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

    return await _run_single_diagnose(tenant, req)


async def _on_startup() -> None:
    cache.l2 = await build_l2(os.getenv("PDM_REDIS_URL"))
    events.init(os.getenv("PDM_REDIS_URL"))

app = Litestar(
    route_handlers=[healthz, version, cache_stats, events_stats, integration_stats, asset_trend, warmup_endpoint, diagnose_endpoint, diagnose_batch_endpoint, consult_endpoint, confirm_card, list_cards, mcp_endpoint, dashboard, screen_page, console_page,
                   sync_alerts_endpoint, list_alerts_endpoint, ack_alert_endpoint,
                   list_diagnoses_endpoint, cost_stats_endpoint, org_tree_endpoint,
                   system_stats_endpoint, asset_rul_endpoint, rul_refresh_endpoint,
                   login_endpoint, me_endpoint, list_users_endpoint,
                   list_plans_endpoint, generate_inspections_endpoint,
                   list_inspections_endpoint, submit_inspection_endpoint,
                   inspection_stats_endpoint, overview_stats_endpoint,
                   append_worklog_endpoint, list_worklogs_endpoint,
                   list_spares_endpoint, move_spare_endpoint, list_spare_issues_endpoint,
                   list_cases_endpoint, create_case_endpoint,
                   list_rules_endpoint, upsert_rule_endpoint],
    on_startup=[_on_startup],
    debug=os.getenv("PDM_ENV", "prod") != "prod",
)

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
    rows, summary = await asyncio.gather(
        asyncio.to_thread(repository.list_alerts, tenant, status, level, limit),
        asyncio.to_thread(repository.alert_summary, tenant),
    )
    return {"tenant_id": tenant, "count": len(rows), "alerts": rows,
            "summary": summary}


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
    action = str(payload.get("action") or "")
    try:
        out = await asyncio.to_thread(
            repository.append_work_log, tenant, str(payload.get("card_id") or ""),
            action, actor, str(payload.get("note") or ""),
            payload.get("actual_cost"))
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # 预测反馈闭环（Phase F3）：工单关闭时若带上预测/实际结果，自动回写
    # prediction_feedback（模型准确率的唯一事实源）。缺字段不阻断关闭。
    if action == "CLOSED" and payload.get("predicted_level") and payload.get("actual_outcome"):
        try:
            from .model import record_feedback
            await asyncio.to_thread(
                record_feedback, tenant,
                str(payload.get("asset_id") or ""),
                str(payload.get("model_id") or domain.MODEL_VERSION),
                str(payload.get("predicted_level")),
                str(payload.get("actual_outcome")),
                diag_id=str(payload.get("diag_id") or ""),
                card_id=str(payload.get("card_id") or ""),
                predicted_hi=payload.get("predicted_hi"),
                actual_hi=payload.get("actual_hi"),
                note=str(payload.get("feedback_note") or ""),
                closed_by=actor)
            out["feedback_recorded"] = True
        except ValueError:
            out["feedback_recorded"] = False   # 非法枚举值：不阻断
        except Exception:  # noqa: BLE001 - 反馈表缺失等不阻断工单关闭
            out["feedback_recorded"] = False
    return out


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
async def list_rules_endpoint(request: Request, rule_scope: str = "site",
                              version: int | None = None) -> dict[str, Any]:
    """告警规则列表（阈值可配置）。rule_scope=group|site，version=None 取最新。

    注意：参数名不能用 `scope`——Litestar 2.25 会把它当作保留名跳过
    （OpenAPI 里只出现 version，运行时 scope 收到 None 触发 400）。
    """
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    if rule_scope not in ("group", "site"):
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="rule_scope must be group|site")
    rows = await asyncio.to_thread(repository.list_alert_rules, tenant, rule_scope, version)
    return {"tenant_id": tenant, "scope": rule_scope, "version": version,
            "count": len(rows), "items": rows}


@post("/v1/rules/group/publish", status_code=200)
async def publish_group_rule_endpoint(request: Request) -> dict[str, Any]:
    """集团发布新判据（version 自动递增，基地 24h 内生效）。需 plan.approve 权限。FR-904。"""
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
    try:
        return await asyncio.to_thread(repository.publish_group_rules, tenant, payload)
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@get("/v1/stats/fleet")
async def fleet_stats_endpoint() -> dict[str, Any]:
    """集团对标统计：跨基地设备数/覆盖率/红橙比/平均 HI。FR-905。"""
    data = await asyncio.to_thread(repository.fleet_stats)
    return {**data}


# ---------- 集团站点注册与边缘上行（docs/44 §3） ----------
@post("/v1/sites/register", status_code=200)
async def site_register_endpoint(request: Request) -> dict[str, Any]:
    """边缘站点注册/心跳（幂等）。边缘启动与每次上行同步时调用。

    无需业务登录（机器账号场景），但要求 X-Tenant-Id 且写操作走 Secret
    注入的机器凭据由边缘侧自行保管；此处仅记录注册信息，不读业务数据。
    """
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    return await asyncio.to_thread(
        repository.register_site, tenant,
        str(payload.get("name") or tenant),
        str(payload.get("region") or "CN"),
        int(payload.get("device_count") or 0),
        str(payload.get("edge_version") or ""))


@get("/v1/sites")
async def list_sites_endpoint(request: Request) -> dict[str, Any]:
    """站点清单（含在线状态）。需 user.view 权限。"""
    try:
        auth_mod.require(request.headers.get("X-Auth-Token"), "user.view")
    except auth_mod.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    rows = await asyncio.to_thread(repository.list_sites)
    return {"count": len(rows), "sites": rows}


@post("/v1/sites/{site_id:str}/summary", status_code=200)
async def site_summary_endpoint(request: Request, site_id: str) -> dict[str, Any]:
    """边缘上行健康摘要（覆盖式更新）。只收聚合指标，不收单台数据。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    if tenant != site_id:
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="site mismatch")
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    return await asyncio.to_thread(repository.upsert_edge_summary, site_id, payload)


@get("/v1/sites/summaries")
async def site_summaries_endpoint(request: Request) -> dict[str, Any]:
    """各边缘站点最新摘要（集团对标大屏数据源）。"""
    rows = await asyncio.to_thread(repository.list_edge_summaries)
    return {"count": len(rows), "sites": rows}


# ---------- Harness 运行时（Phase A） ----------
@get("/v1/harness/tools")
async def harness_tools_endpoint() -> dict[str, Any]:
    """工具注册表（Harness 与 MCP 共用的契约，含副作用分级）。"""
    from . import harness
    return {"count": len(harness.list_tools()),
            "tools": [t.public_spec() for t in harness.list_tools()]}


@post("/v1/harness/run", status_code=200)
async def harness_run_endpoint(request: Request) -> dict[str, Any]:
    """执行一个 Harness 计划（带权限门 + Hooks + 熔断）。

    请求：{"plan":[{"tool":"...","arguments":{...},"rationale":"..."}],
           "approvals":{"<tool>": true}, "downtime_cost_per_hour": 6000}
    响应：DONE / PENDING_APPROVAL（含待确认步骤）；失败抛 LoopError → 409。
    """
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    from . import harness

    plan = [harness.Step(tool=str(s.get("tool") or ""),
                         arguments=dict(s.get("arguments") or {}),
                         rationale=str(s.get("rationale") or ""))
            for s in (payload.get("plan") or [])]
    if not plan:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="plan required")
    ctl = harness.LoopController(
        tenant, plan,
        approvals={str(k): bool(v) for k, v in (payload.get("approvals") or {}).items()},
        downtime_cost_per_hour=float(payload.get("downtime_cost_per_hour") or 6000.0))
    span = harness.otel.invoke_agent(tenant, "harness_run")
    try:
        out = await asyncio.to_thread(ctl.run)
        harness.otel.end_span(span, status=out.get("status", "?"), turns=out.get("trace", {}).get("turns", 0))
        return out
    except harness.LoopError as exc:
        harness.otel.end_span(span, status="ESCALATE", error=str(exc)[:120])
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail=str(exc)) from exc


@get("/v1/harness/traces")
async def harness_traces_endpoint(limit: int = 50) -> dict[str, Any]:
    """OTel span 环形缓冲查询（未配 OTLP 端点时的本地视图）。"""
    from . import harness
    return harness.otel.stats() | {"spans": harness.otel.recent(max(1, min(int(limit), 500)))}


@get("/v1/harness/eval")
async def harness_eval_endpoint() -> dict[str, Any]:
    """Eval Loop：golden cases 回放结果。"""
    from . import harness
    return await asyncio.to_thread(harness.eval.run_cases)


# ---------- Hermes 记忆与受控技能（Phase B） ----------
@post("/v1/memory/session", status_code=200)
async def memory_session_endpoint(request: Request) -> dict[str, Any]:
    """会话滚动摘要（L2 记忆）。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    from . import memory
    return await asyncio.to_thread(
        memory.layers.roll_summary, tenant,
        str(payload.get("session_id") or "default"), str(payload.get("turn") or ""))


@get("/v1/memory/recall")
async def memory_recall_endpoint(request: Request, q: str = "", limit: int = 10) -> dict[str, Any]:
    """事件记忆检索（L3，只读）。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    from . import memory
    rows = await asyncio.to_thread(memory.layers.recall, tenant, q, limit)
    return {"tenant_id": tenant, "count": len(rows), "items": rows}


@get("/v1/memory/profile/{asset_id:str}")
async def memory_profile_endpoint(request: Request, asset_id: str) -> dict[str, Any]:
    """设备档案（L1，上下文注入用）。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    from . import memory
    return await asyncio.to_thread(memory.layers.asset_profile, tenant, asset_id)


@get("/v1/skills")
async def list_skills_endpoint(request: Request, status: str = "") -> dict[str, Any]:
    """技能列表（PENDING 需审批；APPROVED 才会被加载）。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    from . import memory
    rows = await asyncio.to_thread(memory.skills.list_skills, tenant, status)
    return {"tenant_id": tenant, "count": len(rows), "items": rows}


@post("/v1/skills", status_code=201)
async def propose_skill_endpoint(request: Request) -> dict[str, Any]:
    """提议候选技能（不生效——工业红线）。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    from . import memory
    try:
        return await asyncio.to_thread(
            memory.skills.propose, tenant,
            str(payload.get("name") or ""), str(payload.get("body") or ""),
            str(payload.get("source_case_id") or ""),
            str(payload.get("proposed_by") or "agent"))
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@post("/v1/skills/{skill_id:str}/approve", status_code=200)
async def approve_skill_endpoint(request: Request, skill_id: str) -> dict[str, Any]:
    """审批技能（生效唯一入口）。需 plan.approve 权限。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        ident = auth_mod.require(request.headers.get("X-Auth-Token"), "plan.approve")
    except auth_mod.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    operator = (ident or {}).get("sub") or "manager"
    from . import memory
    try:
        return await asyncio.to_thread(memory.skills.approve, tenant, skill_id, operator)
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@post("/v1/skills/{skill_id:str}/reject", status_code=200)
async def reject_skill_endpoint(request: Request, skill_id: str) -> dict[str, Any]:
    """驳回候选技能。需 plan.approve 权限。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        ident = auth_mod.require(request.headers.get("X-Auth-Token"), "plan.approve")
    except auth_mod.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    operator = (ident or {}).get("sub") or "manager"
    from . import memory
    try:
        return await asyncio.to_thread(memory.skills.reject, tenant, skill_id, operator)
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@post("/v1/skills/{skill_id:str}/rollback", status_code=200)
async def rollback_skill_endpoint(request: Request, skill_id: str) -> dict[str, Any]:
    """回滚已生效技能 → SUPERSEDED。需 plan.approve 权限。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        ident = auth_mod.require(request.headers.get("X-Auth-Token"), "plan.approve")
    except auth_mod.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    operator = (ident or {}).get("sub") or "manager"
    from . import memory
    try:
        return await asyncio.to_thread(memory.skills.rollback, tenant, skill_id, operator)
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@post("/v1/memory/freeze", status_code=201)
async def memory_freeze_endpoint(request: Request) -> dict[str, Any]:
    """创建记忆/技能冻结快照。需 plan.approve 权限。"""
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
    from . import memory
    return await asyncio.to_thread(memory.freeze.create_snapshot, tenant,
                                   str(payload.get("note") or ""))


@get("/v1/memory/freeze")
async def memory_freeze_list_endpoint(request: Request) -> dict[str, Any]:
    """冻结快照列表。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    from . import memory
    rows = await asyncio.to_thread(memory.freeze.list_snapshots, tenant)
    return {"tenant_id": tenant, "count": len(rows), "items": rows}


@post("/v1/memory/freeze/{freeze_id:str}/restore", status_code=200)
async def memory_freeze_restore_endpoint(request: Request, freeze_id: str) -> dict[str, Any]:
    """恢复到指定快照。需 plan.approve 权限。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        ident = auth_mod.require(request.headers.get("X-Auth-Token"), "plan.approve")
    except auth_mod.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    operator = (ident or {}).get("sub") or "manager"
    from . import memory
    try:
        return await asyncio.to_thread(memory.freeze.restore, tenant, freeze_id, operator)
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


# ---------- 世界模型 L1：反事实推演（Phase C） ----------
@post("/v1/world/counterfactual", status_code=200)
async def world_counterfactual_endpoint(request: Request) -> dict[str, Any]:
    """反事实推演：推迟/提前保养的 HI 轨迹 + 区间 + 预计跌破天数。

    工业红线：输出必带不确定性区间；样本不足返回 null（不编数字）。
    """
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    asset_id = str(payload.get("asset_id") or "")
    if not asset_id:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="asset_id required")
    maintain_at = payload.get("maintain_at_day")
    from . import world
    return await asyncio.to_thread(
        world.counterfactual, tenant, asset_id,
        horizon_days=max(1, min(int(payload.get("horizon_days") or 14), 90)),
        maintain_at_day=(int(maintain_at) if maintain_at is not None else None),
        maintain_reset_hi=(float(payload["maintain_reset_hi"])
                           if payload.get("maintain_reset_hi") is not None else None),
        critical_hi=float(payload.get("critical_hi") or 40.0),
        history_days=max(7, min(int(payload.get("history_days") or 90), 365)))


@get("/v1/world/model/{asset_id:str}")
async def world_model_endpoint(request: Request, asset_id: str) -> dict[str, Any]:
    """设备退化模型摘要（斜率/残差/窗口），无趋势返回 model=null。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    import datetime

    from . import world
    since = datetime.datetime.now() - datetime.timedelta(days=90)

    def _fit() -> dict[str, Any]:
        from app import repository
        rows = (
            repository.TenantQueryBuilder(tenant)
            .query("SELECT ts, hi FROM read_asset_health_history "
                   "WHERE tenant_id=%s AND asset_id=%s AND ts >= %s ORDER BY ts",
                   asset_id, since)
            .fetchall()
        )
        m = world.fit(rows)
        if m is None:
            return {"asset_id": asset_id, "model": None}
        return {"asset_id": asset_id, "model": {
            "slope_per_day": round(m.slope_per_day, 4),
            "resid_std": round(m.resid_std, 3),
            "n_points": m.n_points, "window_days": round(m.window_days, 1)}}

    return await asyncio.to_thread(_fit)


# ---------- A2A 智能体互联（Phase E） ----------
@get("/.well-known/agent.json")
async def agent_card_endpoint(request: Request) -> dict[str, Any]:
    """A2A Agent Card：能力发现（docs/43 E1）。"""
    from . import a2a
    tenant = request.headers.get("X-Tenant-Id") or ""
    return a2a.agent_card(base_url=str(request.base_url), tenant=tenant)


@post("/a2a/tasks/send", status_code=201)
async def a2a_task_send_endpoint(request: Request) -> dict[str, Any]:
    """A2A tasks/send：创建任务（白名单类型）。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    from . import a2a
    try:
        return await asyncio.to_thread(
            a2a.create_task, tenant,
            str(payload.get("task_type") or ""),
            dict(payload.get("payload") or {}),
            str(payload.get("requester") or "anonymous"))
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@get("/a2a/tasks/{task_id:str}")
async def a2a_task_get_endpoint(request: Request, task_id: str,
                                execute: bool = False) -> dict[str, Any]:
    """A2A tasks/get：查询任务；execute=true 时同步执行（无工作进程场景）。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    from . import a2a
    if execute:
        try:
            return await asyncio.to_thread(a2a.execute_task, tenant, task_id)
        except ValueError as exc:
            raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    row = await asyncio.to_thread(a2a.get_task, tenant, task_id)
    if row is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="task not found")
    return row


# ---------- 模型治理（Phase F） ----------
@get("/v1/models")
async def list_models_endpoint(request: Request, status: str = "") -> dict[str, Any]:
    """模型版本注册表。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    from .model import list_models
    rows = await asyncio.to_thread(list_models, tenant, status)
    return {"tenant_id": tenant, "current": domain.MODEL_VERSION,
            "count": len(rows), "items": rows}


@post("/v1/models", status_code=201)
async def register_model_endpoint(request: Request) -> dict[str, Any]:
    """注册模型版本。需 plan.approve 权限。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        ident = auth_mod.require(request.headers.get("X-Auth-Token"), "plan.approve")
    except auth_mod.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    from .model import register_model
    return await asyncio.to_thread(
        register_model, tenant,
        str(payload.get("model_id") or ""),
        str(payload.get("model_kind") or "rules"),
        str(payload.get("description") or ""),
        str(payload.get("feature_set") or ""),
        payload.get("metrics") or {},
        (ident or {}).get("sub") or "manager")


@get("/v1/models/drift")
async def model_drift_endpoint(request: Request, window_days: int = 7) -> dict[str, Any]:
    """HI 分布漂移检测（双窗口 z 检验）。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    from .model import detect_drift
    return await asyncio.to_thread(detect_drift, tenant,
                                   window_days=max(1, min(int(window_days), 30)))


@get("/v1/models/accuracy")
async def model_accuracy_endpoint(request: Request, model_id: str = "") -> dict[str, Any]:
    """模型准确率（预测命中/误报/漏报，来自 prediction_feedback）。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    from .model import accuracy_report
    return await asyncio.to_thread(accuracy_report, tenant, model_id)


@post("/v1/models/feedback", status_code=201)
async def model_feedback_endpoint(request: Request) -> dict[str, Any]:
    """回写预测反馈（"预测 vs 实际"）。"""
    tenant = request.headers.get("X-Tenant-Id")
    if not tenant:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    from .model import record_feedback
    try:
        return await asyncio.to_thread(
            record_feedback, tenant,
            str(payload.get("asset_id") or ""),
            str(payload.get("model_id") or domain.MODEL_VERSION),
            str(payload.get("predicted_level") or ""),
            str(payload.get("actual_outcome") or ""),
            diag_id=str(payload.get("diag_id") or ""),
            card_id=str(payload.get("card_id") or ""),
            predicted_hi=payload.get("predicted_hi"),
            actual_hi=payload.get("actual_hi"),
            note=str(payload.get("note") or ""),
            closed_by=str(payload.get("closed_by") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


# ---------- Prometheus 指标（Phase G2 数据源） ----------
@get("/metrics")
async def metrics_endpoint() -> Response:
    """Prometheus 文本格式指标（与 prometheus-rules.yaml 的指标名一一对应）。

    设计：指标查询失败时输出 NaN/跳过（不阻塞抓取），保证 /metrics 永不 5xx。
    """
    lines: list[str] = []

    def _add(name: str, value: Any, labels: str = "", help_text: str = "") -> None:
        if help_text:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name}{labels} {value}")

    # 服务存活
    _add("pdm_up", 1, help_text="服务存活（1=存活）")

    # 覆盖率 + 四级分布（site-a 为默认展示租户）
    try:
        ov = await asyncio.to_thread(repository.overview_stats, "site-a")
        _add("pdm_coverage_pct", ov.get("coverage_pct", 0), '{tenant="site-a"}',
             "诊断覆盖率百分比")
        _add("pdm_assets_total", ov.get("total_assets", 0), '{tenant="site-a"}',
             "在监设备总数")
    except Exception:  # noqa: BLE001
        lines.append('pdm_coverage_pct{tenant="site-a"} NaN')

    # 消费统计
    try:
        es = events.stats_snapshot()
        _add("pdm_consumer_emitted_total", es.get("emitted", 0), help_text="事件发射总数")
        _add("pdm_consumer_failures_total", es.get("failures", 0),
             help_text="事件发射失败数")
    except Exception:  # noqa: BLE001
        pass

    # 边缘心跳（每站点一个序列）
    try:
        sites = await asyncio.to_thread(repository.list_sites, 30)
        for s in sites:
            hb = s.get("last_seen_min_ago")
            ts = 0 if hb is None else max(0, int(time.time() - float(hb) * 60))
            _add("pdm_edge_heartbeat_timestamp_seconds", ts,
                 f'{{site_id="{s["site_id"]}"}}', "边缘最近心跳 Unix 时间戳")
            _add("pdm_edge_online", 1 if s.get("online") else 0,
                 f'{{site_id="{s["site_id"]}"}}', "边缘在线（1=在线）")
    except Exception:  # noqa: BLE001
        pass

    # 判据版本（site vs group，供版本漂移告警）
    try:
        for scope in ("site", "group"):
            for r in await asyncio.to_thread(repository.list_alert_rules, "site-a", scope, None):
                _add("pdm_rule_version", r.get("version", 1),
                     f'{{tenant_id="site-a",rule_id="{r["rule_id"]}",scope="{scope}"}}',
                     "判据版本号")
    except Exception:  # noqa: BLE001
        pass

    # 模型漂移与准确率
    try:
        from .model import accuracy_report, detect_drift
        drift = await asyncio.to_thread(detect_drift, "site-a", window_days=7)
        if drift.get("z") is not None:
            _add("pdm_model_drift_z", abs(float(drift["z"])), help_text="HI 分布漂移 z 值")
        acc = await asyncio.to_thread(accuracy_report, "site-a", "")
        if acc.get("precision") is not None:
            _add("pdm_model_precision", acc["precision"], help_text="模型精确率")
        if acc.get("recall") is not None:
            _add("pdm_model_recall", acc["recall"], help_text="模型召回率")
    except Exception:  # noqa: BLE001
        pass

    return Response(content="\n".join(lines) + "\n",
                    media_type="text/plain; version=0.0.4; charset=utf-8")


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
    payload.setdefault("scope", "site")
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
    redis_url = os.getenv("PDM_REDIS_URL")
    # 记录 URL 供懒加载自愈使用：启动瞬间 Redis 未就绪时不判死，
    # 首次请求会按冷却窗口自动重试建连（cache._ensure_l2）
    cache.l2_url = redis_url
    cache.l2 = await build_l2(redis_url)
    events.init(redis_url)

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
                   list_rules_endpoint, upsert_rule_endpoint,
                   publish_group_rule_endpoint, fleet_stats_endpoint,
                   site_register_endpoint, list_sites_endpoint,
                   site_summary_endpoint, site_summaries_endpoint,
                   harness_tools_endpoint, harness_run_endpoint,
                   harness_traces_endpoint, harness_eval_endpoint,
                   memory_session_endpoint, memory_recall_endpoint,
                   memory_profile_endpoint, list_skills_endpoint,
                   propose_skill_endpoint, approve_skill_endpoint,
                   reject_skill_endpoint, rollback_skill_endpoint,
                   memory_freeze_endpoint, memory_freeze_list_endpoint,
                   memory_freeze_restore_endpoint,
                   world_counterfactual_endpoint, world_model_endpoint,
                   agent_card_endpoint, a2a_task_send_endpoint,
                   a2a_task_get_endpoint,
                   list_models_endpoint, register_model_endpoint,
                   model_drift_endpoint, model_accuracy_endpoint,
                   model_feedback_endpoint, metrics_endpoint],
    on_startup=[_on_startup],
    debug=os.getenv("PDM_ENV", "prod") != "prod",
)

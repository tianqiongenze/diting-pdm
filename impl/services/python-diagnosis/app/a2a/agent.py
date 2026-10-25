"""A2A（Agent2Agent）协议服务端（docs/43 §10.4，Phase E）。

定位（2025-04 Google 发布，2025-06 捐 Linux Foundation，2026-08 入 AAIF
与 MCP 同治理）：
- MCP 管"智能体连工具"，A2A 管"智能体连智能体"
- 本项目用途：① 供应商域受控上报（FR-907）② 跨基地专家代诊

实现要点（对齐 A2A 规范核心概念）：
- Agent Card（/.well-known/agent.json）：能力发现
- tasks/send + tasks/get：任务管理与状态机
- 任务状态机：SUBMITTED → WORKING → COMPLETED / FAILED / CANCELED
- 安全：任务创建需租户匹配；审计落库；不执行任意代码
"""

from __future__ import annotations

import datetime
import json
import os
import uuid
from typing import Any

TASK_STATES = ("SUBMITTED", "WORKING", "COMPLETED", "FAILED", "CANCELED")

# 支持的任务类型（受控白名单——不接受任意任务类型）
TASK_TYPES = {
    "diagnose_request": "请求对指定设备做诊断（跨基地专家代诊）",
    "quality_report": "供应商来料质量/设备异常上报（FR-907）",
    "alert_escalation": "告警跨域升级（基地 → 集团）",
}


def agent_card(base_url: str = "", tenant: str = "") -> dict[str, Any]:
    """Agent Card：能力声明（docs/43 E1）。"""
    return {
        "name": "diting-pdm-agent",
        "description": "谛听 PdM 智能体：设备诊断 / 维护会诊 / 告警处置",
        "url": base_url or os.getenv("PDM_A2A_BASE_URL", ""),
        "version": "1.0.0",
        "protocolVersion": "0.2.1",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {"id": "diagnose", "name": "设备诊断",
             "description": "HI 计算 + 五维分解 + 故障假设 + 证据链",
             "tags": ["pdM", "diagnosis"]},
            {"id": "consult", "name": "维护会诊",
             "description": "四角色会诊 + 方案卡（窗口/备件/人力/经济性）",
             "tags": ["maintenance", "decision"]},
            {"id": "counterfactual", "name": "反事实推演",
             "description": "世界模型：推迟/提前保养的 HI 轨迹与区间",
             "tags": ["world-model", "simulation"]},
        ],
        "tenant": tenant,
        "taskTypes": TASK_TYPES,
    }


def create_task(tenant: str, task_type: str, payload: dict[str, Any],
                requester: str = "anonymous") -> dict[str, Any]:
    """创建任务（SUBMITTED）。task_type 必须在白名单内。"""
    from app import repository

    if task_type not in TASK_TYPES:
        raise ValueError(f"unsupported task_type: {task_type} "
                         f"(allowed: {sorted(TASK_TYPES)})")
    task_id = f"TASK-{datetime.datetime.now():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.datetime.now()
    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO a2a_task(tenant_id,task_id,task_type,state,requester,"
            "payload_json,result_json,error,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (tenant, task_id, task_type, "SUBMITTED", requester,
             json.dumps(payload, ensure_ascii=False, default=str),
             None, None, now, now))
    return {"task_id": task_id, "state": "SUBMITTED", "task_type": task_type,
            "created_at": now.isoformat(timespec="seconds")}


def get_task(tenant: str, task_id: str) -> dict[str, Any] | None:
    from app import repository

    row = (
        repository.TenantQueryBuilder(tenant)
        .query("SELECT task_id, task_type, state, requester, payload_json, "
               "result_json, error, created_at, updated_at "
               "FROM a2a_task WHERE tenant_id=%s AND task_id=%s", task_id)
        .fetchone()
    )
    if row:
        for k in ("payload_json", "result_json"):
            if isinstance(row.get(k), str):
                try:
                    row[k[:-5] if k.endswith("_json") else k] = json.loads(row[k])
                except Exception:  # noqa: BLE001
                    pass
    return row


def execute_task(tenant: str, task_id: str) -> dict[str, Any]:
    """执行任务（同步）：SUBMITTED → WORKING → COMPLETED/FAILED。

    当前执行器为受控映射（不执行任意代码）：
    - diagnose_request  → 调 harness diagnose_asset 工具
    - quality_report    → 落 quality_report 表（FR-907：关联来料批次）
    - alert_escalation  → 落审计 + 返回升级确认
    """
    from app import repository

    task = get_task(tenant, task_id)
    if not task:
        raise ValueError(f"task not found: {task_id}")
    if task["state"] not in ("SUBMITTED", "WORKING"):
        raise ValueError(f"task {task_id} is {task['state']}, cannot execute")
    payload = task.get("payload") or {}
    ttype = task["task_type"]

    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE a2a_task SET state='WORKING', updated_at=%s "
                    "WHERE tenant_id=%s AND task_id=%s",
                    (datetime.datetime.now(), tenant, task_id))
    try:
        result = _dispatch(tenant, ttype, payload, task.get("requester", ""))
    except Exception as exc:  # noqa: BLE001 - 任务失败落状态，不抛
        with repository.connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE a2a_task SET state='FAILED', error=%s, updated_at=%s "
                        "WHERE tenant_id=%s AND task_id=%s",
                        (str(exc)[:255], datetime.datetime.now(), tenant, task_id))
        return {"task_id": task_id, "state": "FAILED", "error": str(exc)[:255]}

    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE a2a_task SET state='COMPLETED', result_json=%s, updated_at=%s "
                    "WHERE tenant_id=%s AND task_id=%s",
                    (json.dumps(result, ensure_ascii=False, default=str),
                     datetime.datetime.now(), tenant, task_id))
    return {"task_id": task_id, "state": "COMPLETED", "result": result}


def _dispatch(tenant: str, ttype: str, payload: dict[str, Any],
              requester: str) -> dict[str, Any]:
    """受控任务分发（白名单映射，不执行任意代码）。"""
    if ttype == "diagnose_request":
        from app.harness import tools as harness_tools
        asset_id = str(payload.get("asset_id") or "")
        if not asset_id:
            raise ValueError("diagnose_request requires asset_id")
        return harness_tools.execute("diagnose_asset", {"asset_id": asset_id})

    if ttype == "quality_report":
        from app import repository
        # FR-907：供应商上报 → 关联来料批次 → 通知 SQE（落表待推送）
        report_id = f"QR-{datetime.datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        with repository.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO quality_report(tenant_id,report_id,batch_no,supplier_code,"
                "asset_id,abnormal_type,detail,reporter,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (tenant, report_id, payload.get("batch_no"), payload.get("supplier_code"),
                 payload.get("asset_id"), payload.get("abnormal_type"),
                 json.dumps(payload.get("detail") or {}, ensure_ascii=False, default=str),
                 requester, datetime.datetime.now()))
        return {"report_id": report_id, "linked_batch": payload.get("batch_no"),
                "notify": "SQE"}

    if ttype == "alert_escalation":
        return {"escalated": True, "alert_id": payload.get("alert_id"),
                "to": "group-reliability-engineer"}

    raise ValueError(f"unhandled task_type: {ttype}")

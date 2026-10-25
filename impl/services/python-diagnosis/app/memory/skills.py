"""Skill 沉淀管线（Hermes 式闭环的**受控版**；docs/12 红线，Phase B2）。

Hermes 的技能循环：任务 → 提炼技能 → 更新记忆 → 下次加载。
本项目的约束版：任务 → **候选技能** → 人工审批 → 版本化技能库 → 下次加载。

红线（docs/12 + docs/43 §13.2）：**禁止在线自生效**。
原因：工厂里一次错误诊断被自动固化为技能，等于把未评审判据植入系统。
因此候选技能默认 status=PENDING，必须 manager 角色 approve 后才可被加载。

生命周期：PENDING → APPROVED（可加载）/ REJECTED（留档）；
APPROVED 可再版本化（version+1），旧版本标记 SUPERSEDED（可回滚）。
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any

SKILL_STATUSES = ("PENDING", "APPROVED", "REJECTED", "SUPERSEDED")


def propose(tenant_id: str, name: str, body: str, source_case_id: str = "",
            proposed_by: str = "agent") -> dict[str, Any]:
    """从一次任务/案例提炼候选技能（不生效）。"""
    from app import repository

    name = str(name or "").strip()
    if not name:
        raise ValueError("skill name required")
    skill_id = f"SK-{datetime.datetime.now():%Y%m%d}-{os.urandom(3).hex().upper()}"
    now = datetime.datetime.now()
    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skill_registry(tenant_id,skill_id,name,body,version,status,"
            "source_case_id,proposed_by,approved_by,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (tenant_id, skill_id, name, body, 1, "PENDING", source_case_id or None,
             proposed_by, None, now, now))
    return {"skill_id": skill_id, "status": "PENDING", "version": 1}


def list_skills(tenant_id: str, status: str = "") -> list[dict[str, Any]]:
    from app import repository

    sql = ("SELECT skill_id,name,body,version,status,source_case_id,proposed_by,"
           "approved_by,created_at FROM skill_registry WHERE tenant_id=%s")
    params: list[Any] = []
    if status:
        if status not in SKILL_STATUSES:
            raise ValueError(f"invalid status: {status}")
        sql += " AND status=%s"
        params.append(status)
    sql += " ORDER BY created_at DESC"
    return list(repository.TenantQueryBuilder(tenant_id).query(sql, *params).fetchall())


def approve(tenant_id: str, skill_id: str, approved_by: str) -> dict[str, Any]:
    """审批候选技能 → APPROVED（生效的唯一入口）。调用方须已校验 plan.approve 权限。"""
    from app import repository

    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM skill_registry WHERE tenant_id=%s AND skill_id=%s",
                    (tenant_id, skill_id))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"skill not found: {skill_id}")
        if row["status"] != "PENDING":
            raise ValueError(f"skill {skill_id} is {row['status']}, only PENDING can approve")
        cur.execute(
            "UPDATE skill_registry SET status='APPROVED', approved_by=%s, "
            "updated_at=%s WHERE tenant_id=%s AND skill_id=%s",
            (approved_by, datetime.datetime.now(), tenant_id, skill_id))
    return {"skill_id": skill_id, "status": "APPROVED", "approved_by": approved_by}


def reject(tenant_id: str, skill_id: str, rejected_by: str) -> dict[str, Any]:
    from app import repository

    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM skill_registry WHERE tenant_id=%s AND skill_id=%s",
                    (tenant_id, skill_id))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"skill not found: {skill_id}")
        if row["status"] != "PENDING":
            raise ValueError(f"skill {skill_id} is {row['status']}, only PENDING can reject")
        cur.execute(
            "UPDATE skill_registry SET status='REJECTED', approved_by=%s, "
            "updated_at=%s WHERE tenant_id=%s AND skill_id=%s",
            (rejected_by, datetime.datetime.now(), tenant_id, skill_id))
    return {"skill_id": skill_id, "status": "REJECTED"}


def load_active(tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """加载已审批技能（下次任务时注入上下文）。只读 APPROVED。"""
    from app import repository

    return list(repository.TenantQueryBuilder(tenant_id).query(
        "SELECT skill_id,name,body,version,approved_by,updated_at FROM skill_registry "
        "WHERE tenant_id=%s AND status='APPROVED' ORDER BY updated_at DESC LIMIT %s",
        limit).fetchall())


def rollback(tenant_id: str, skill_id: str, operator: str) -> dict[str, Any]:
    """回滚：把 APPROVED 技能降为 SUPERSEDED（留档可追溯）。

    简化实现（v1）：单版本技能直接置 SUPERSEDED；多版本回滚由
    approve 新版本 + 旧版本 SUPERSEDED 组合完成（与判据版本化同一模式）。
    """
    from app import repository

    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM skill_registry WHERE tenant_id=%s AND skill_id=%s",
                    (tenant_id, skill_id))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"skill not found: {skill_id}")
        if row["status"] != "APPROVED":
            raise ValueError(f"skill {skill_id} is {row['status']}, only APPROVED can rollback")
        cur.execute(
            "UPDATE skill_registry SET status='SUPERSEDED', updated_at=%s "
            "WHERE tenant_id=%s AND skill_id=%s",
            (datetime.datetime.now(), tenant_id, skill_id))
    return {"skill_id": skill_id, "status": "SUPERSEDED", "by": operator}

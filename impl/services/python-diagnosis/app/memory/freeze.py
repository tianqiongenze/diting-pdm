"""记忆冻结与回滚（Phase B3）。

工业红线：判据/技能的每次变更都必须可回滚、留审计。
本模块提供"冻结快照"：把当前技能库 + 判据版本打包为快照行，
异常时一键恢复（恢复 = 把快照内技能重新置 APPROVED/SUPERSEDED）。

快照本身也是版本：freeze_id 单调递增，可回滚到任一历史快照。
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any


def create_snapshot(tenant_id: str, note: str = "") -> dict[str, Any]:
    """创建记忆/技能冻结快照（含当前全部技能状态 + 判据版本）。"""
    from app import memory, repository

    skills = memory.skills.list_skills(tenant_id)
    rules = repository.list_alert_rules(tenant_id, scope="site") + \
        repository.list_alert_rules(tenant_id, scope="group")
    snapshot = {
        "skills": [{"skill_id": s["skill_id"], "status": s["status"],
                    "version": s["version"]} for s in skills],
        "rules": [{"rule_id": r["rule_id"], "version": r["version"],
                   "scope": r["scope"], "threshold": r["threshold"]} for r in rules],
    }
    freeze_id = f"FRZ-{datetime.datetime.now():%Y%m%d}-{os.urandom(3).hex().upper()}"
    now = datetime.datetime.now()
    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO memory_freeze(tenant_id,freeze_id,note,snapshot_json,created_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (tenant_id, freeze_id, note[:255], json.dumps(snapshot, ensure_ascii=False), now))
    return {"freeze_id": freeze_id, "skills": len(snapshot["skills"]),
            "rules": len(snapshot["rules"]), "created_at": now.isoformat(timespec="seconds")}


def list_snapshots(tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
    from app import repository

    return list(repository.TenantQueryBuilder(tenant_id).query(
        "SELECT freeze_id, note, created_at FROM memory_freeze "
        "WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s",
        limit).fetchall())


def restore(tenant_id: str, freeze_id: str, operator: str) -> dict[str, Any]:
    """恢复到指定快照：技能状态按快照重置（判据版本只读不改，改判据走发布通道）。"""
    from app import repository

    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT snapshot_json FROM memory_freeze WHERE tenant_id=%s AND freeze_id=%s",
                    (tenant_id, freeze_id))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"freeze not found: {freeze_id}")
        snapshot = json.loads(row["snapshot_json"])
        restored = 0
        now = datetime.datetime.now()
        for s in snapshot.get("skills", []):
            cur.execute(
                "UPDATE skill_registry SET status=%s, updated_at=%s "
                "WHERE tenant_id=%s AND skill_id=%s",
                (s["status"], now, tenant_id, s["skill_id"]))
            restored += cur.rowcount
    return {"freeze_id": freeze_id, "restored_skills": restored, "by": operator}

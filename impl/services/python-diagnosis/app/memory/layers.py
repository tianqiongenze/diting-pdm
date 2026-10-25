"""三层记忆架构（借鉴 Hermes，加"冻结"约束；docs/05 §6，Phase B1）。

Hermes Agent（Nous Research）的持久记忆分三层，本项目按工业场景取舍：

| 层 | Hermes | 本项目 | 冻结约束 |
|---|---|---|---|
| L1 设备档案 | 用户画像 | 静态台账/关键度/历史摘要 | 只读，变更走变更管理 |
| L2 会话记忆 | 滚动摘要 | 会话滚动摘要（最近 N 轮） | 会话级，过期即弃 |
| L3 事件记忆 | FTS 全文 | 诊断/告警/工单事件流 | 只追加，禁止改写 |

为什么不能照搬 Hermes 的"自主技能创建"：工厂里 Agent 把一次错误诊断
提炼成技能并自动生效，等于把未经评审的判据植入系统。**本项目的技能必须
人工审批后才生效**（docs/12 红线，见 skills.py）。
"""

from __future__ import annotations

import datetime
import json
from typing import Any

SESSION_SUMMARY_MAX_CHARS = 2000


# ---------------------------------------------------------------------------
# L1 设备档案（静态注入）
# ---------------------------------------------------------------------------
def asset_profile(tenant_id: str, asset_id: str) -> dict[str, Any]:
    """设备档案：台账 + 最新健康 + 最近诊断摘要（供上下文注入）。"""
    from app import repository

    asset = repository.get_asset(tenant_id, asset_id)
    if not asset:
        return {"asset_id": asset_id, "found": False}
    health = (
        repository.TenantQueryBuilder(tenant_id)
        .query("SELECT hi, level, open_alerts, updated_at FROM read_asset_health "
               "WHERE tenant_id=%s AND asset_id=%s", asset_id)
        .fetchone()
    )
    diag = (
        repository.TenantQueryBuilder(tenant_id)
        .query("SELECT diag_id, hi, level, model_version, created_at FROM diagnosis "
               "WHERE tenant_id=%s AND asset_id=%s ORDER BY created_at DESC LIMIT 1",
               asset_id)
        .fetchone()
    )
    return {"asset_id": asset_id, "found": True, "asset": asset,
            "health": health, "latest_diagnosis": diag}


# ---------------------------------------------------------------------------
# L2 会话滚动摘要
# ---------------------------------------------------------------------------
def roll_summary(tenant_id: str, session_id: str, new_turn: str) -> dict[str, Any]:
    """滚动摘要：取既有摘要 + 新回合，超长截断（保留头尾）。

    生产环境可换 LLM 摘要；当前用确定性截断——无 LLM 依赖、可测试、
    不引入幻觉（摘要只可能是原文子集）。
    """
    from app import repository

    row = (
        repository.TenantQueryBuilder(tenant_id)
        .query("SELECT summary_json FROM agent_session WHERE tenant_id=%s AND session_id=%s",
               session_id)
        .fetchone()
    )
    prior = ""
    if row and row.get("summary_json"):
        try:
            prior = json.loads(row["summary_json"]).get("summary", "")
        except Exception:  # noqa: BLE001
            prior = ""
    merged = (prior + "\n" + new_turn).strip()
    if len(merged) > SESSION_SUMMARY_MAX_CHARS:
        half = SESSION_SUMMARY_MAX_CHARS // 2
        merged = merged[:half] + "\n…[截断]…\n" + merged[-half:]
    payload = {"summary": merged, "turns": (json.loads(row["summary_json"]).get("turns", 0) + 1
                                            if row and row.get("summary_json") else 1),
               "updated_at": datetime.datetime.now().isoformat(timespec="seconds")}
    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_session(tenant_id,session_id,summary_json,turns,updated_at) "
            "VALUES (%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE summary_json=VALUES(summary_json),"
            "turns=VALUES(turns),updated_at=VALUES(updated_at)",
            (tenant_id, session_id, json.dumps(payload, ensure_ascii=False),
             payload["turns"], datetime.datetime.now()))
    return {"session_id": session_id, **payload}


# ---------------------------------------------------------------------------
# L3 事件记忆（只追加 + 检索）
# ---------------------------------------------------------------------------
def recall(tenant_id: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """事件记忆检索：诊断/告警/工单/知识库四源聚合。

    检索策略：SQLite 边缘可用 FTS5；TiDB 用 LIKE（两条后端行为一致）。
    只读，不改写任何历史（L3 只追加）。
    """
    from app import repository

    like = f"%{query}%"
    out: list[dict[str, Any]] = []
    qb = repository.TenantQueryBuilder
    out += list(qb(tenant_id).query(
        "SELECT alert_id AS id, 'alert' AS kind, message AS text, created_at "
        "FROM alert WHERE tenant_id=%s AND (message LIKE %s OR asset_id LIKE %s) "
        "ORDER BY created_at DESC LIMIT %s", like, like, limit).fetchall())
    out += list(qb(tenant_id).query(
        "SELECT case_id AS id, 'knowledge' AS kind, title AS text, created_at "
        "FROM knowledge_case WHERE tenant_id=%s AND (title LIKE %s OR root_cause LIKE %s "
        "OR tags LIKE %s) ORDER BY created_at DESC LIMIT %s",
        like, like, like, limit).fetchall())
    out.sort(key=lambda r: str(r.get("created_at")), reverse=True)
    return out[:limit]


def stats(tenant_id: str) -> dict[str, Any]:
    """记忆规模（系统运维视图）。"""
    from app import repository

    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM agent_session WHERE tenant_id=%s", (tenant_id,))
        sessions = int(cur.fetchone()["n"])
    return {"sessions": sessions, "session_summary_max_chars": SESSION_SUMMARY_MAX_CHARS}

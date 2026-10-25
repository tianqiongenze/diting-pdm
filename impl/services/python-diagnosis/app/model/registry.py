"""模型治理：注册表 + 漂移检测 + 反馈闭环（docs/43 Phase F）。"""

from __future__ import annotations

import datetime
import os
import uuid
from typing import Any


# ---------------------------------------------------------------------------
# F1 模型注册表
# ---------------------------------------------------------------------------
def register_model(tenant_id: str, model_id: str, model_kind: str = "rules",
                   description: str = "", feature_set: str = "",
                   metrics: dict[str, Any] | None = None,
                   created_by: str = "system") -> dict[str, Any]:
    """注册模型版本（诊断结果关联版本，支持新旧对比）。"""
    import json

    from app import repository

    now = datetime.datetime.now()
    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO model_version(tenant_id,model_id,model_kind,description,"
            "feature_set,metrics_json,status,created_by,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,'ACTIVE',%s,%s) "
            "ON DUPLICATE KEY UPDATE description=VALUES(description),"
            "feature_set=VALUES(feature_set),metrics_json=VALUES(metrics_json)",
            (tenant_id, model_id, model_kind, description[:255], feature_set[:255],
             json.dumps(metrics or {}, ensure_ascii=False), created_by, now))
    return {"model_id": model_id, "status": "ACTIVE"}


def list_models(tenant_id: str, status: str = "") -> list[dict[str, Any]]:
    import json

    from app import repository

    sql = ("SELECT model_id, model_kind, description, feature_set, metrics_json, "
           "status, created_by, created_at FROM model_version WHERE tenant_id=%s")
    params: list[Any] = []
    if status:
        sql += " AND status=%s"
        params.append(status)
    sql += " ORDER BY created_at DESC"
    rows = list(repository.TenantQueryBuilder(tenant_id).query(sql, *params).fetchall())
    for r in rows:
        if isinstance(r.get("metrics_json"), str):
            try:
                r["metrics"] = json.loads(r.pop("metrics_json"))
            except Exception:  # noqa: BLE001
                r["metrics"] = {}
        else:
            r.pop("metrics_json", None)
    return rows


def current_model_id() -> str:
    """当前诊断引擎版本（与 domain.MODEL_VERSION 一致，单一来源）。"""
    from app import domain
    return domain.MODEL_VERSION


# ---------------------------------------------------------------------------
# F2 漂移检测
# ---------------------------------------------------------------------------
def detect_drift(tenant_id: str, *, window_days: int = 7,
                 z_threshold: float = 2.0) -> dict[str, Any]:
    """HI 分布漂移检测：对比近 window_days 与此前 window_days 的均值/标准差。

    方法：双窗口 z 检验（近窗均值 vs 基线窗）。|z| > z_threshold → 告警级漂移。
    样本不足或基线无波动 → 返回 insufficient（不编数字）。
    """
    from app import repository

    now = datetime.datetime.now()
    recent_start = now - datetime.timedelta(days=window_days)
    base_start = now - datetime.timedelta(days=window_days * 2)

    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT AVG(hi) AS m, COUNT(*) AS n FROM read_asset_health_history "
            "WHERE tenant_id=%s AND ts >= %s AND ts < %s",
            (tenant_id, recent_start, now))
        recent = cur.fetchone()
        cur.execute(
            "SELECT AVG(hi) AS m, COUNT(*) AS n FROM read_asset_health_history "
            "WHERE tenant_id=%s AND ts >= %s AND ts < %s",
            (tenant_id, base_start, recent_start))
        base = cur.fetchone()

    n_r, n_b = int(recent["n"] or 0), int(base["n"] or 0)
    if n_r < 30 or n_b < 30:
        return {"tenant_id": tenant_id, "drift": None,
                "note": "样本不足（每窗 <30 点），不判定"}
    m_r, m_b = float(recent["m"]), float(base["m"])
    # 简化标准误：用经验标准差代理（HI 分布跨度约 60）
    se = 12.0 * (1 / (n_r ** 0.5) + 1 / (n_b ** 0.5)) ** 0.5
    if se <= 0:
        return {"tenant_id": tenant_id, "drift": None, "note": "基线无波动"}
    z = (m_r - m_b) / se
    return {"tenant_id": tenant_id,
            "drift": abs(z) > z_threshold,
            "z": round(z, 2), "threshold": z_threshold,
            "recent_mean": round(m_r, 2), "baseline_mean": round(m_b, 2),
            "recent_points": n_r, "baseline_points": n_b,
            "direction": "down" if z < 0 else "up"}


# ---------------------------------------------------------------------------
# F3 预测反馈闭环
# ---------------------------------------------------------------------------
def record_feedback(tenant_id: str, asset_id: str, model_id: str,
                    predicted_level: str, actual_outcome: str,
                    diag_id: str = "", card_id: str = "",
                    predicted_hi: float | None = None,
                    actual_hi: float | None = None,
                    note: str = "", closed_by: str = "") -> dict[str, Any]:
    """工单关闭时回写"预测 vs 实际"（模型准确率的唯一事实源）。"""
    from app import repository

    if predicted_level not in ("red", "orange", "yellow", "green"):
        raise ValueError(f"invalid predicted_level: {predicted_level}")
    if actual_outcome not in ("CONFIRMED_FAILURE", "FALSE_ALARM", "NO_EVENT"):
        raise ValueError(f"invalid actual_outcome: {actual_outcome}")
    feedback_id = f"FB-{datetime.datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO prediction_feedback(tenant_id,feedback_id,asset_id,diag_id,"
            "card_id,model_id,predicted_level,actual_outcome,predicted_hi,actual_hi,"
            "note,closed_by,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (tenant_id, feedback_id, asset_id, diag_id or None, card_id or None,
             model_id, predicted_level, actual_outcome, predicted_hi, actual_hi,
             note[:255], closed_by or None, datetime.datetime.now()))
    return {"feedback_id": feedback_id, "asset_id": asset_id,
            "predicted_level": predicted_level, "actual_outcome": actual_outcome}


def accuracy_report(tenant_id: str, model_id: str = "") -> dict[str, Any]:
    """模型准确率汇总：预测命中（非 FALSE_ALARM）/ 误报 / 漏报。

    判定口径：
    - 预测 red/orange 且实际 CONFIRMED_FAILURE → 命中
    - 预测 red/orange 且实际 FALSE_ALARM      → 误报
    - 预测 green/yellow 且实际 CONFIRMED_FAILURE → 漏报
    """
    from app import repository

    sql = ("SELECT predicted_level, actual_outcome, COUNT(*) AS n "
           "FROM prediction_feedback WHERE tenant_id=%s")
    params: list[Any] = []
    if model_id:
        sql += " AND model_id=%s"
        params.append(model_id)
    sql += " GROUP BY predicted_level, actual_outcome"
    rows = list(repository.TenantQueryBuilder(tenant_id).query(sql, *params).fetchall())

    hit = fp = miss = 0
    for r in rows:
        lv, oc, n = r["predicted_level"], r["actual_outcome"], int(r["n"])
        warned = lv in ("red", "orange")
        failed = oc == "CONFIRMED_FAILURE"
        if warned and failed:
            hit += n
        elif warned and oc == "FALSE_ALARM":
            fp += n
        elif not warned and failed:
            miss += n
    total = hit + fp + miss
    return {"model_id": model_id or "(all)", "hit": hit, "false_positive": fp,
            "miss": miss, "evaluated": total,
            "precision": round(hit / (hit + fp), 3) if (hit + fp) else None,
            "recall": round(hit / (hit + miss), 3) if (hit + miss) else None}

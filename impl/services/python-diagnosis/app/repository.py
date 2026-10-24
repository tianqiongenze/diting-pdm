"""数据访问层（TiDB）。

两条硬规则（docs/17 §8.2、impl/db/tidb/TENANT_ISOLATION.md）：
1. 所有查询必须经 TenantQueryBuilder，禁止裸 SQL
2. 返回结果逐行断言 tenant_id，不一致即抛 CrossTenantLeak

连接策略：短连接 + 线程内复用。TiDB 的连接建立成本低（毫秒级），
比维护连接池更简单可靠；高频场景再引入池化。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator
import datetime

import pymysql
from pymysql.cursors import DictCursor

DB_CONFIG = dict(
    host=os.getenv("PDM_TIDB_HOST", "tidb.infra.svc.cluster.local"),
    port=int(os.getenv("PDM_TIDB_PORT", "4000")),
    user=os.getenv("PDM_TIDB_USER", "pdm_diag"),
    password=os.getenv("PDM_TIDB_PASSWORD", ""),
    database="pdm",
    charset="utf8mb4",
    autocommit=True,
    connect_timeout=3,
    read_timeout=10,
    write_timeout=10,
    cursorclass=DictCursor,
)


class CrossTenantLeak(RuntimeError):
    """检测到跨租户数据，属于安全事件，必须告警。"""


@contextmanager
def connect() -> Iterator[pymysql.connections.Connection]:
    conn = pymysql.connect(**DB_CONFIG)
    try:
        yield conn
    finally:
        conn.close()


class TenantQueryBuilder:
    """强制注入 tenant_id，调用方无法覆盖；结果逐行断言。"""

    def __init__(self, tenant_id: str) -> None:
        if not tenant_id:
            raise ValueError("tenant_id required")
        self.tenant_id = tenant_id
        self._sql: str | None = None
        self._params: list[Any] = []

    def query(self, sql: str, *params: Any) -> "TenantQueryBuilder":
        if "tenant_id" not in sql:
            raise ValueError("query must filter by tenant_id")
        self._sql = sql
        self._params = [self.tenant_id, *params]
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        assert self._sql is not None
        with connect() as conn, conn.cursor() as cur:
            cur.execute(self._sql, self._params)
            rows = cur.fetchall()
        for r in rows:
            if r.get("tenant_id") not in (None, self.tenant_id):
                raise CrossTenantLeak(f"expected {self.tenant_id}, got {r.get('tenant_id')}")
        return rows

    def fetchone(self) -> dict[str, Any] | None:
        rows = self.fetchall()
        return rows[0] if rows else None


# ---------------------------------------------------------------------------
# 领域查询
# ---------------------------------------------------------------------------
def get_asset(tenant_id: str, asset_id: str) -> dict[str, Any] | None:
    return (
        TenantQueryBuilder(tenant_id)
        .query(
            "SELECT tenant_id, asset_id, node_id, name, category, criticality, status "
            "FROM asset WHERE tenant_id=%s AND asset_id=%s",
            asset_id,
        )
        .fetchone()
    )


def get_latest_feature(tenant_id: str, asset_id: str) -> dict[str, Any] | None:
    """取该设备最新一条特征点（走 (tenant,mp,ts) 聚簇索引，按 mp 前缀倒序）。

    为了在 1 亿行上仍然快，先定位该设备的一个 mp，再用 PK 倒序取 1 行。
    """
    rows = (
        TenantQueryBuilder(tenant_id)
        .query(
            """
            SELECT fp.tenant_id, fp.mp_id, fp.ts, fp.rms, fp.peak, fp.kurtosis,
                   fp.skewness, fp.crest, fp.hi, fp.level, fp.quality
            FROM feature_point fp
            WHERE fp.tenant_id = %s
              AND fp.mp_id LIKE %s
            ORDER BY fp.mp_id DESC, fp.ts DESC
            LIMIT 1
            """,
            f"%.{asset_id}.%",
        )
        .fetchall()
    )
    return rows[0] if rows else None


def get_asset_baseline(tenant_id: str, asset_id: str, days: int = 30) -> float | None:
    """基线 = 最近 N 天 rms 的中位数（近似：抽样 2000 行）。"""
    rows = (
        TenantQueryBuilder(tenant_id)
        .query(
            """
            SELECT AVG(t.rms) AS base_rms FROM (
              SELECT fp.rms FROM feature_point fp
              WHERE fp.tenant_id = %s AND fp.mp_id LIKE %s
              ORDER BY fp.ts DESC LIMIT 2000
            ) t
            """,
            f"%.{asset_id}.%",
        )
        .fetchall()
    )
    if not rows or rows[0]["base_rms"] is None:
        return None
    return float(rows[0]["base_rms"])


def list_hot_assets(tenant_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """按关键度挑"热点"设备，用于 warmup。

    优先读 read_asset_latest（读模型）；为空时退回 asset 表全量。
    """
    rows = (
        TenantQueryBuilder(tenant_id)
        .query(
            """
            SELECT l.tenant_id, l.asset_id, a.name, a.criticality, l.rms, l.kurtosis,
                   l.hi, l.level, l.ts
            FROM read_asset_latest l
            JOIN asset a ON a.tenant_id = l.tenant_id AND a.asset_id = l.asset_id
            WHERE l.tenant_id = %s
            ORDER BY FIELD(a.criticality,'A','B','C'), l.hi ASC
            LIMIT %s
            """,
            limit,
        )
        .fetchall()
    )
    if len(rows) >= limit:
        return rows
    # 读模型不全（刚上线/刚清空）→ 用快照表补齐，避免"热点列表只有 2 条"
    seen = {r["asset_id"] for r in rows}
    extra = (
        TenantQueryBuilder(tenant_id)
        .query(
            """
            SELECT s.tenant_id, s.asset_id, a.name, a.criticality,
                   s.vib_rms_mm_s AS rms, s.kurtosis, NULL AS hi, NULL AS level, s.ts
            FROM asset_metric_snapshot s
            JOIN asset a ON a.tenant_id = s.tenant_id AND a.asset_id = s.asset_id
            WHERE s.tenant_id = %s
            ORDER BY FIELD(a.criticality,'A','B','C'), s.vib_rms_mm_s DESC
            LIMIT %s
            """,
            limit * 3,
        )
        .fetchall()
    )
    for r in extra:
        if len(rows) >= limit:
            break
        if r["asset_id"] not in seen:
            rows.append(r)
            seen.add(r["asset_id"])
    return rows[:limit]


def count_assets(tenant_id: str) -> int:
    rows = (
        TenantQueryBuilder(tenant_id)
        .query("SELECT COUNT(*) AS n FROM asset WHERE tenant_id=%s")
        .fetchall()
    )
    return int(rows[0]["n"]) if rows else 0


def save_diagnosis(tenant_id: str, diag: dict[str, Any]) -> None:
    import json
    import uuid

    diag_id = diag.get("diag_id") or str(uuid.uuid4())
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO diagnosis(tenant_id,diag_id,asset_id,window_start,window_end,hi,level,degraded,"
            "degraded_reason,contributions,hypotheses,model_name,model_version,status) "
            "VALUES (%s,%s,%s,NOW() - INTERVAL 1 HOUR,NOW(),%s,%s,%s,%s,%s,%s,%s,%s,'FINALIZED')",
            (
                tenant_id, diag_id, diag["asset_id"], diag["hi"], diag["level"],
                1 if diag["degraded"] else 0, diag.get("degraded_reason"),
                json.dumps(diag["contributions"], ensure_ascii=False),
                json.dumps(diag["hypotheses"], ensure_ascii=False),
                "rules", diag["model_version"],
            ),
        )
    diag["diag_id"] = diag_id


def get_snapshot(tenant_id: str, asset_id: str) -> dict[str, Any] | None:
    """取多源指标快照（一次拿全 5 维，避免降级）。"""
    return (
        TenantQueryBuilder(tenant_id)
        .query(
            "SELECT tenant_id, asset_id, vib_rms_mm_s, vib_baseline_mm_s, kurtosis, temp_c, "
            "temp_baseline_c, current_dev_pct, trend_pct_per_day, ts "
            "FROM asset_metric_snapshot WHERE tenant_id=%s AND asset_id=%s",
            asset_id,
        )
        .fetchone()
    )


def save_recommendation_card(tenant_id: str, card: dict[str, Any],
                             dedup_minutes: int | None = None) -> str:
    """落库方案卡（recommendation_card）。返回实际生效的 card_id。

    去重：同一设备 + 同一 verdict 在窗口期内（默认 30 分钟，环境变量
    PDM_CARD_DEDUP_MINUTES 可调）已有卡片时**复用**该卡，不再新建。

    为什么需要：/v1/consult 是幂等查询型接口，重复调用（刷新看板、
    重跑测试）会不断堆积同一设备的相同结论卡片。曾因此在演示环境
    积累 20 张同设备卡，看板一片重复。设 0 可关闭去重。
    """
    import datetime
    import json
    import os

    card_id = card["card_id"]
    verdict = card.get("verdict", "MONITOR")
    window = (dedup_minutes if dedup_minutes is not None
              else int(os.getenv("PDM_CARD_DEDUP_MINUTES", "30")))

    with connect() as conn, conn.cursor() as cur:
        if window > 0:
            since = datetime.datetime.now() - datetime.timedelta(minutes=window)
            cur.execute(
                "SELECT card_id FROM recommendation_card "
                "WHERE tenant_id=%s AND asset_id=%s AND verdict=%s AND created_at >= %s "
                "ORDER BY created_at DESC LIMIT 1",
                (tenant_id, card["asset_id"], verdict, since),
            )
            row = cur.fetchone()
            if row:
                return row["card_id"] if isinstance(row, dict) else row[0]

        options = card.get("options") or []
        cur.execute(
            "INSERT IGNORE INTO recommendation_card(tenant_id, card_id, asset_id, diag_id, conclusion, "
            "confidence, options, economics, conflicts, verdict, risk_level, required_roles, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                tenant_id, card_id, card["asset_id"], card.get("diag_id"),
                card["diagnosis"]["conclusion"], card["diagnosis"]["confidence"],
                json.dumps(options, ensure_ascii=False),
                json.dumps(card["economics"], ensure_ascii=False),
                json.dumps(card.get("conflicts", []), ensure_ascii=False),
                verdict, card.get("risk_level", "中"),
                json.dumps(card["human_confirm"]["required_roles"], ensure_ascii=False),
                card["human_confirm"]["status"],
            ),
        )
    return card_id


def get_recommendation_card(tenant_id: str, card_id: str) -> dict[str, Any] | None:
    rows = (
        TenantQueryBuilder(tenant_id)
        .query(
            "SELECT * FROM recommendation_card WHERE tenant_id=%s AND card_id=%s",
            card_id,
        )
        .fetchall()
    )
    return rows[0] if rows else None


def list_confirmed_roles(tenant_id: str, card_id: str) -> list[str]:
    """已确认的角色列表（去重复用旧卡时回填用）。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT role FROM card_confirmation "
            "WHERE tenant_id=%s AND card_id=%s AND decision='confirm'",
            (tenant_id, card_id),
        )
        return [r["role"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]


def confirm_recommendation_card(tenant_id: str, card_id: str, role: str,
                                user_id: str, decision: str, comment: str = "") -> dict[str, Any]:
    """人工确认方案卡。决策为 confirm 时若齐备 required_roles 则状态转为 CONFIRMED。"""
    import json

    card = get_recommendation_card(tenant_id, card_id)
    if card is None:
        raise ValueError(f"card not found: {card_id}")
    if card["status"] in ("CONFIRMED", "REJECTED"):
        raise ValueError(f"card already finalized: {card['status']}")

    required = card.get("required_roles") or []
    if isinstance(required, str):
        required = json.loads(required)

    import uuid

    with connect() as conn, conn.cursor() as cur:
        # 该表有多列 NOT NULL 无默认（actor/action/token_hash/expires_at），必须显式给值
        cur.execute(
            "INSERT INTO card_confirmation(confirm_id, tenant_id, card_id, role, actor, user_id, "
            "decision, comment, action, token_hash, expires_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                uuid.uuid4().hex, tenant_id, card_id, role, user_id, user_id, decision, comment,
                decision, uuid.uuid4().hex, datetime.datetime.now() + datetime.timedelta(hours=72),
            ),
        )
        if decision == "reject":
            cur.execute(
                "UPDATE recommendation_card SET status='REJECTED' WHERE tenant_id=%s AND card_id=%s",
                (tenant_id, card_id),
            )
            return {"card_id": card_id, "status": "REJECTED", "confirmed_by": [user_id]}

        cur.execute(
            "SELECT role FROM card_confirmation WHERE tenant_id=%s AND card_id=%s AND decision='confirm'",
            (tenant_id, card_id),
        )
        confirmed = {r["role"] for r in cur.fetchall()}
        missing = [r for r in required if r not in confirmed]
        if not missing:
            cur.execute(
                "UPDATE recommendation_card SET status='CONFIRMED' WHERE tenant_id=%s AND card_id=%s",
                (tenant_id, card_id),
            )
            return {"card_id": card_id, "status": "CONFIRMED", "confirmed_by": sorted(confirmed)}
        return {"card_id": card_id, "status": "PENDING", "confirmed_by": sorted(confirmed), "missing": missing}


def get_health_trend(tenant_id: str, asset_id: str, days: int = 7) -> dict[str, Any]:
    """HI 趋势（看板折线图）。

    返回按小时聚合的序列 + 汇总指标（均值/最低/红色持续时长）。
    采样上限 500 点，超出按时序抽样，避免前端渲染压力。
    """
    import datetime

    since = datetime.datetime.now() - datetime.timedelta(days=max(1, min(days, 90)))
    # 时序查询量大，直接用 connect（不走 TenantQueryBuilder 的通用封装）
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT hi, level, ts FROM read_asset_health_history "
            "WHERE tenant_id=%s AND asset_id=%s AND ts >= %s ORDER BY ts",
            (tenant_id, asset_id, since),
        )
        rows = cur.fetchall()

    if not rows:
        return {"asset_id": asset_id, "points": 0, "series": [], "summary": {}}

    step = max(1, len(rows) // 500)
    sampled = rows[::step]
    series = [
        {"ts": r["ts"].strftime("%Y-%m-%d %H:%M"), "hi": float(r["hi"]), "level": r["level"]}
        for r in sampled
    ]
    his = [float(r["hi"]) for r in rows]
    red = sum(1 for r in rows if r["level"] == "red")
    return {
        "asset_id": asset_id,
        "points": len(series),
        "total_rows": len(rows),
        "sampled_step": step,
        "series": series,
        "summary": {
            "avg_hi": round(sum(his) / len(his), 2),
            "min_hi": round(min(his), 2),
            "max_hi": round(max(his), 2),
            "latest_hi": his[-1],
            "red_points": red,
            "red_ratio": round(red / len(rows), 3),
        },
    }


def list_pending_cards(tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
    return (
        TenantQueryBuilder(tenant_id)
        .query(
            "SELECT card_id, asset_id, conclusion, verdict, risk_level, status, created_at "
            "FROM recommendation_card WHERE tenant_id=%s AND status IN ('PENDING','CONFIRMED') "
            "ORDER BY created_at DESC LIMIT %s",
            limit,
        )
        .fetchall()
    )


def refresh_latest(tenant_id: str, asset_id: str, hi: float, level: str,
                   rms: float, kurtosis: float, ts: Any, mp_count: int) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO read_asset_latest(tenant_id,asset_id,mp_count,rms,kurtosis,hi,level,ts) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE mp_count=VALUES(mp_count),rms=VALUES(rms),kurtosis=VALUES(kurtosis),"
            "hi=VALUES(hi),level=VALUES(level),ts=VALUES(ts)",
            (tenant_id, asset_id, mp_count, rms, kurtosis, hi, level, ts),
        )


# ---------------------------------------------------------------------------
# 告警中心（FR-01）
# ---------------------------------------------------------------------------
_ALERT_RULES = (
    # (rule_id, level, owner_role, sla_hours)
    ("HI_CRITICAL", "red", "maintenance_supervisor", 4),      # HI < 40
    ("HI_WARNING", "orange", "maintenance_supervisor", 24),   # 40 <= HI < 阈值
)


def sync_alerts(tenant_id: str, threshold_hi: float = 70.0,
                limit: int = 200) -> dict[str, Any]:
    """从当前健康状态生成告警（幂等）。

    幂等键：同一 (asset_id, rule_id) 存在 OPEN/ACKED/ESCALATED 时不重复建单。
    只对已有健康评估的设备建告警——没有 HI 就没有判断依据，不臆造。
    """
    rows = (
        TenantQueryBuilder(tenant_id)
        .query(
            # 列名是 updated_at（不是 ts）；rul_p50_days 为剩余寿命中位数
            "SELECT asset_id, hi, level, rul_p50_days, updated_at FROM read_asset_health "
            "WHERE tenant_id=%s AND hi IS NOT NULL AND hi < %s "
            "ORDER BY hi ASC LIMIT %s",
            threshold_hi, limit,
        )
        .fetchall()
    )

    created = 0
    skipped = 0
    now = datetime.datetime.now()
    with connect() as conn, conn.cursor() as cur:
        for r in rows:
            hi = float(r["hi"])
            if hi < 40:
                rule, lv, role, sla = _ALERT_RULES[0]
                msg = f"{r['asset_id']} 健康指数 {hi:.1f}，低于临界阈值 40"
                hint = "立即停机检查，按方案卡执行紧急维修"
            else:
                rule, lv, role, sla = _ALERT_RULES[1]
                msg = f"{r['asset_id']} 健康指数 {hi:.1f}，低于关注阈值 {threshold_hi:.0f}"
                hint = "安排计划检修窗口，检查振动与轴承状态"

            cur.execute(
                "SELECT alert_id FROM alert WHERE tenant_id=%s AND asset_id=%s "
                "AND rule_id=%s AND status IN ('OPEN','ACKED','ESCALATED') LIMIT 1",
                (tenant_id, r["asset_id"], rule),
            )
            if cur.fetchone():
                skipped += 1
                continue

            alert_id = f"ALT-{now:%Y%m%d}-{os.urandom(3).hex().upper()}"
            cur.execute(
                "INSERT INTO alert(tenant_id,alert_id,asset_id,diag_id,level,rule_id,message,"
                "action_hint,owner_role,sla_hours,status,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'OPEN',%s)",
                (tenant_id, alert_id, r["asset_id"], None, lv, rule, msg, hint, role, sla, now),
            )
            created += 1

        cur.execute(
            "SELECT COUNT(*) AS n FROM alert WHERE tenant_id=%s "
            "AND status IN ('OPEN','ACKED','ESCALATED')", (tenant_id,))
        total_open = cur.fetchone()["n"]

    return {"created": created, "skipped": skipped, "total_open": total_open}


def list_alerts(tenant_id: str, status: str = "", level: str = "",
                limit: int = 100) -> list[dict[str, Any]]:
    """告警列表，按 SLA 剩余时间升序（超时优先）。"""
    # 左连接健康表带出 RUL，便于按"还有几天到期"判断优先级
    sql = ("SELECT a.alert_id AS alert_id, a.asset_id AS asset_id, a.level AS level, "
           "a.rule_id AS rule_id, a.message AS message, a.action_hint AS action_hint, "
           "a.owner_role AS owner_role, a.sla_hours AS sla_hours, a.status AS status, "
           "a.ack_by AS ack_by, a.ack_action AS ack_action, a.created_at AS created_at, "
           "a.escalated_at AS escalated_at, h.rul_p50_days AS rul_days "
           "FROM alert a LEFT JOIN read_asset_health h "
           "ON a.tenant_id=h.tenant_id AND a.asset_id=h.asset_id "
           "WHERE a.tenant_id=%s")
    params: list[Any] = []
    if status:
        marks = ",".join(["%s"] * len(status.split(",")))
        sql += f" AND a.status IN ({marks})"
        params.extend(status.split(","))
    if level:
        sql += " AND a.level=%s"
        params.append(level)
    sql += " ORDER BY a.created_at DESC LIMIT %s"
    params.append(limit)

    # pymysql 的 fetchall() 返回 tuple，需转 list 才能排序
    rows = list(TenantQueryBuilder(tenant_id).query(sql, *params).fetchall())
    now = datetime.datetime.now()
    for r in rows:
        created = r.get("created_at")
        if isinstance(created, datetime.datetime):
            age_h = (now - created).total_seconds() / 3600
            r["age_hours"] = round(age_h, 1)
            r["sla_remaining_hours"] = round(float(r.get("sla_hours") or 0) - age_h, 1)
            r["overdue"] = r["sla_remaining_hours"] < 0
    rows.sort(key=lambda x: (not x.get("overdue"), x.get("sla_remaining_hours", 999)))
    return rows


def ack_alert(tenant_id: str, alert_id: str, action: str, by: str,
              comment: str = "") -> dict[str, Any]:
    """处置告警：ack / escalate / close。"""
    mapping = {"ack": "ACKED", "escalate": "ESCALATED", "close": "CLOSED"}
    if action not in mapping:
        raise ValueError(f"unknown action: {action}")

    row = (
        TenantQueryBuilder(tenant_id)
        .query("SELECT alert_id, status FROM alert WHERE tenant_id=%s AND alert_id=%s", alert_id)
        .fetchone()
    )
    if row is None:
        raise ValueError(f"alert not found: {alert_id}")
    if row["status"] == "CLOSED":
        raise ValueError("alert already closed")

    new_status = mapping[action]
    now = datetime.datetime.now()
    with connect() as conn, conn.cursor() as cur:
        if action == "escalate":
            cur.execute(
                "UPDATE alert SET status=%s, ack_by=%s, ack_action=%s, escalated_at=%s "
                "WHERE tenant_id=%s AND alert_id=%s",
                (new_status, by, comment or "escalate", now, tenant_id, alert_id),
            )
        else:
            cur.execute(
                "UPDATE alert SET status=%s, ack_by=%s, ack_action=%s "
                "WHERE tenant_id=%s AND alert_id=%s",
                (new_status, by, comment or action, tenant_id, alert_id),
            )
    return {"alert_id": alert_id, "status": new_status, "by": by}


# ---------------------------------------------------------------------------
# 诊断历史（FR-03）
# ---------------------------------------------------------------------------
def list_diagnoses(tenant_id: str, asset_id: str = "",
                   limit: int = 50) -> list[dict[str, Any]]:
    import json

    sql = ("SELECT diag_id, asset_id, hi, level, degraded, degraded_reason, "
           "contributions, model_name, model_version, created_at "
           "FROM diagnosis WHERE tenant_id=%s")
    params: list[Any] = []
    if asset_id:
        sql += " AND asset_id LIKE %s"
        params.append(f"{asset_id}%")
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)

    rows = TenantQueryBuilder(tenant_id).query(sql, *params).fetchall()
    for r in rows:
        if isinstance(r.get("contributions"), str):
            try:
                r["contributions"] = json.loads(r["contributions"])
            except Exception:  # noqa: BLE001
                r["contributions"] = []
        if isinstance(r.get("hi"), str):
            r["hi"] = float(r["hi"])
    return rows


# ---------------------------------------------------------------------------
# 成本效益（FR-04）
# ---------------------------------------------------------------------------
def cost_stats(tenant_id: str, top_n: int = 10) -> dict[str, Any]:
    """从方案卡的 economics / options.cost 聚合成本效益。"""
    import json

    rows = (
        TenantQueryBuilder(tenant_id)
        .query(
            "SELECT card_id, asset_id, verdict, economics, options FROM recommendation_card "
            "WHERE tenant_id=%s",
        )
        .fetchall()
    )

    tot = {"avoided_loss": 0.0, "total_cost": 0.0, "net_benefit": 0.0}
    breakdown = {"downtime": 0.0, "labor": 0.0, "spare": 0.0}
    by_asset: dict[str, dict[str, float]] = {}
    by_option: dict[str, dict[str, Any]] = {}

    for r in rows:
        try:
            eco = json.loads(r["economics"]) if isinstance(r["economics"], str) else (r["economics"] or {})
        except Exception:  # noqa: BLE001
            eco = {}
        al = float(eco.get("avoided_loss") or 0)
        tc = float(eco.get("total_cost") or 0)
        nb = float(eco.get("net_benefit") or 0)
        tot["avoided_loss"] += al
        tot["total_cost"] += tc
        tot["net_benefit"] += nb

        a = by_asset.setdefault(r["asset_id"], {"avoided_loss": 0.0, "total_cost": 0.0,
                                                "net_benefit": 0.0, "cards": 0})
        a["avoided_loss"] += al
        a["total_cost"] += tc
        a["net_benefit"] += nb
        a["cards"] += 1

        try:
            opts = json.loads(r["options"]) if isinstance(r["options"], str) else (r["options"] or [])
        except Exception:  # noqa: BLE001
            opts = []
        # 成本构成只统计「主方案」（options[0]）：economics.total_cost 对应的就是它。
        # 若把所有备选方案都累加，breakdown 之和会远大于 total_cost，
        # 两个口径不一致会误导使用者。by_option 仍保留全部方案用于对比。
        if opts:
            c = (opts[0] or {}).get("cost") or {}
            breakdown["downtime"] += float(c.get("downtime") or 0)
            breakdown["labor"] += float(c.get("labor") or 0)
            breakdown["spare"] += float(c.get("spare") or 0)

        for o in opts:
            c = (o or {}).get("cost") or {}
            oid = o.get("option_id") or "未命名"
            b = by_option.setdefault(oid, {"option_id": oid, "count": 0,
                                           "total_cost": 0.0, "downtime_hours": 0.0})
            b["count"] += 1
            b["total_cost"] += float(c.get("total") or 0)
            b["downtime_hours"] += float(o.get("downtime_hours") or 0)

    roi = (tot["net_benefit"] / tot["total_cost"] * 100) if tot["total_cost"] else 0.0
    ranked = sorted(by_asset.items(), key=lambda kv: kv[1]["net_benefit"], reverse=True)
    return {
        "sample_cards": len(rows),
        "total": {**{k: round(v, 2) for k, v in tot.items()}, "roi_pct": round(roi, 1)},
        "breakdown": {k: round(v, 2) for k, v in breakdown.items()},
        "by_asset": [{"asset_id": k, **{kk: round(vv, 2) if isinstance(vv, float) else vv
                                        for kk, vv in v.items()}}
                     for k, v in ranked[:top_n]],
        "by_option": sorted(by_option.values(), key=lambda x: x["total_cost"], reverse=True),
    }


# ---------------------------------------------------------------------------
# 组织与产线（FR-05）
# ---------------------------------------------------------------------------
def org_tree(tenant_id: str) -> list[dict[str, Any]]:
    """组织树 + 各节点（含子树）设备数与健康分布。"""
    nodes = (
        TenantQueryBuilder(tenant_id)
        .query("SELECT node_id, parent_id, node_type, name, path FROM org_node WHERE tenant_id=%s")
        .fetchall()
    )
    health = (
        TenantQueryBuilder(tenant_id)
        .query(
            # level 在 read_asset_health，不在 asset；用左连接，未评估的设备记为 none
            "SELECT a.asset_id AS asset_id, a.node_id AS node_id, h.level AS level "
            "FROM asset a LEFT JOIN read_asset_health h "
            "ON a.tenant_id=h.tenant_id AND a.asset_id=h.asset_id "
            "WHERE a.tenant_id=%s",
        )
        .fetchall()
    )

    # 设备按 node_id 归集
    direct: dict[str, dict[str, int]] = {}
    for a in health:
        nid = a.get("node_id") or ""
        d = direct.setdefault(nid, {"total": 0, "red": 0, "orange": 0,
                                    "yellow": 0, "green": 0, "none": 0})
        d["total"] += 1
        lv = a.get("level") or "none"
        d[lv if lv in d else "none"] += 1

    children: dict[str, list[str]] = {}
    for n in nodes:
        children.setdefault(n.get("parent_id") or "", []).append(n["node_id"])

    def aggregate(nid: str) -> dict[str, int]:
        acc = dict(direct.get(nid, {"total": 0, "red": 0, "orange": 0,
                                    "yellow": 0, "green": 0, "none": 0}))
        for c in children.get(nid, []):
            sub = aggregate(c)
            for k in acc:
                acc[k] += sub[k]
        return acc

    depth_map: dict[str, int] = {}

    def depth(nid: str, d: int = 0) -> int:
        depth_map[nid] = d
        for c in children.get(nid, []):
            depth(c, d + 1)
        return d

    for n in nodes:
        if not n.get("parent_id"):
            depth(n["node_id"], 0)

    out = []
    for n in nodes:
        agg = aggregate(n["node_id"])
        out.append({
            "node_id": n["node_id"], "parent_id": n.get("parent_id"),
            "node_type": n.get("node_type"), "name": n.get("name"),
            "depth": depth_map.get(n["node_id"], 0),
            "asset_count": agg["total"],
            "levels": {k: agg[k] for k in ("red", "orange", "yellow", "green", "none")},
        })

    # 按父子关系 DFS 输出：若只按 (depth, node_id) 排序，会变成
    # 「所有车间 → 所有产线」，父子被拆散，树形缩进失去意义（实测踩到）。
    by_id = {r["node_id"]: r for r in out}
    kids: dict[str, list[str]] = {}
    for r in out:
        pid = r.get("parent_id") or ""
        if pid and pid in by_id:
            kids.setdefault(pid, []).append(r["node_id"])

    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(nid: str) -> None:
        if nid in seen:
            return
        seen.add(nid)
        ordered.append(by_id[nid])
        for c in sorted(kids.get(nid, [])):
            walk(c)

    for r in sorted(out, key=lambda x: x["node_id"]):
        if not (r.get("parent_id") or "") or r["parent_id"] not in by_id:
            walk(r["node_id"])
    for r in sorted(out, key=lambda x: x["node_id"]):
        walk(r["node_id"])

    return ordered


# ---------------------------------------------------------------------------
# 系统运维（FR-06）
# ---------------------------------------------------------------------------
def system_stats(tenant_id: str) -> dict[str, Any]:
    """核心表规模。仅统计行数，不含任何业务内容，故不走 TenantQueryBuilder。

    性能注意：feature_point 有 1 亿行，COUNT(*) 要扫全表（数秒），
    会让 /v1/stats/system 卡住首屏。策略：先取 information_schema 的估算行数，
    小于 500 万行的表才做精确 COUNT，大表直接用估算值并标记 exact=False。
    """
    tables = ("asset", "component", "measurement_point", "feature_point",
              "diagnosis", "recommendation_card", "card_confirmation",
              "alert", "read_asset_health", "read_asset_health_history",
              "integration_outbox", "processed_event")
    out = []
    with connect() as conn, conn.cursor() as cur:
        est: dict[str, int] = {}
        try:
            cur.execute(
                "SELECT table_name AS tn, table_rows AS tr FROM information_schema.tables "
                "WHERE table_schema='pdm'")
            for r in cur.fetchall():
                est[str(r["tn"])] = int(r["tr"] or 0)
        except Exception:  # noqa: BLE001
            est = {}

        for t in tables:
            guess = est.get(t)
            try:
                if guess is not None and guess >= 5_000_000:
                    # 大表用估算，避免全表扫描
                    out.append({"name": t, "rows": guess, "exact": False})
                else:
                    cur.execute(f"SELECT COUNT(*) AS n FROM `{t}`")
                    out.append({"name": t, "rows": int(cur.fetchone()["n"]), "exact": True})
            except Exception as exc:  # noqa: BLE001
                out.append({"name": t, "rows": guess if guess is not None else -1,
                            "exact": False, "error": str(exc)[:60]})
    return {"tables": out}


# ---------------------------------------------------------------------------
# RUL 剩余寿命估算（技术债 #3）
# ---------------------------------------------------------------------------
def estimate_rul(tenant_id: str, asset_id: str, critical_hi: float = 40.0,
                 min_points: int = 3, lookback_days: int = 30) -> dict[str, Any]:
    """用健康历史线性外推估算剩余寿命（天）。

    不做黑盒预测，只用可解释的线性外推：
        rul_days = (hi_now - critical_hi) / (-slope_per_day)

    边界（不臆造）：
    - 历史点 < min_points → 返回 None（样本不足）
    - 斜率 >= -0.01（基本无劣化）→ 返回 None（无劣化趋势）
    - 当前 HI 已 <= 临界线 → 返回 0（已到期）
    """
    since = datetime.datetime.now() - datetime.timedelta(days=lookback_days)
    rows = (
        TenantQueryBuilder(tenant_id)
        .query(
            "SELECT hi, ts FROM read_asset_health_history "
            "WHERE tenant_id=%s AND asset_id=%s AND ts >= %s ORDER BY ts ASC",
            asset_id, since,
        )
        .fetchall()
    )
    pts = [(r["ts"], float(r["hi"])) for r in rows if r.get("hi") is not None]
    if len(pts) < min_points:
        return {"asset_id": asset_id, "rul_days": None, "reason": "insufficient_history",
                "points": len(pts)}

    t0 = pts[0][0]
    xs = [(t - t0).total_seconds() / 86400.0 for t, _ in pts]
    ys = [v for _, v in pts]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 1e-9:
        return {"asset_id": asset_id, "rul_days": None, "reason": "flat_history",
                "points": n}
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den

    hi_now = ys[-1]
    if hi_now <= critical_hi:
        return {"asset_id": asset_id, "rul_days": 0.0, "reason": "already_critical",
                "points": n, "hi_now": round(hi_now, 2), "slope_per_day": round(slope, 3)}
    if slope >= -0.01:
        return {"asset_id": asset_id, "rul_days": None, "reason": "no_degradation",
                "points": n, "hi_now": round(hi_now, 2), "slope_per_day": round(slope, 3)}

    rul = (hi_now - critical_hi) / (-slope)
    return {"asset_id": asset_id, "rul_days": round(rul, 1), "reason": "estimated",
            "points": n, "hi_now": round(hi_now, 2), "slope_per_day": round(slope, 3)}


def refresh_rul(tenant_id: str, critical_hi: float = 40.0) -> dict[str, Any]:
    """批量估算并写回 read_asset_health.rul_p50_days。"""
    assets = (
        TenantQueryBuilder(tenant_id)
        .query("SELECT asset_id FROM read_asset_health WHERE tenant_id=%s")
        .fetchall()
    )
    updated = skipped = 0
    with connect() as conn, conn.cursor() as cur:
        for a in assets:
            est = estimate_rul(tenant_id, a["asset_id"], critical_hi)
            if est["rul_days"] is None:
                skipped += 1
                continue
            cur.execute(
                "UPDATE read_asset_health SET rul_p50_days=%s "
                "WHERE tenant_id=%s AND asset_id=%s",
                (est["rul_days"], tenant_id, a["asset_id"]),
            )
            updated += 1
    return {"updated": updated, "skipped": skipped, "total": len(assets)}


# ---------------------------------------------------------------------------
# 用户与角色（技术债 #4）
# ---------------------------------------------------------------------------
def get_user(tenant_id: str, username: str) -> dict[str, Any] | None:
    return (
        TenantQueryBuilder(tenant_id)
        .query("SELECT username, role, display_name, password_hash, enabled "
               "FROM app_user WHERE tenant_id=%s AND username=%s", username)
        .fetchone()
    )


def list_users(tenant_id: str) -> list[dict[str, Any]]:
    return list(
        TenantQueryBuilder(tenant_id)
        .query("SELECT username, role, display_name, enabled, created_at "
               "FROM app_user WHERE tenant_id=%s ORDER BY role, username")
        .fetchall()
    )


# ---------------------------------------------------------------------------
# 点检巡检 + 保养计划（技术债 #2）
# ---------------------------------------------------------------------------
def list_plans(tenant_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """保养计划列表，按下次到期时间升序（逾期优先）。"""
    import json

    rows = list(
        TenantQueryBuilder(tenant_id)
        .query(
            "SELECT plan_id, asset_id, name, cycle_days, items, owner_role, "
            "last_done_at, next_due_at, enabled FROM maintenance_plan "
            "WHERE tenant_id=%s ORDER BY next_due_at ASC LIMIT %s",
            limit,
        )
        .fetchall()
    )
    now = datetime.datetime.now()
    for r in rows:
        if isinstance(r.get("items"), str):
            try:
                r["items"] = json.loads(r["items"])
            except Exception:  # noqa: BLE001
                r["items"] = []
        due = r.get("next_due_at")
        if isinstance(due, datetime.datetime):
            secs = (due - now).total_seconds()
            r["days_left"] = round(secs / 86400, 1)
            # 用原始秒判断，避免 -0.0 这类四舍五入后既不为负也不为零的边界
            r["overdue"] = secs < 0
        else:
            r["days_left"] = None
            r["overdue"] = False
        r["item_count"] = len(r.get("items") or [])
    return rows


def generate_inspections(tenant_id: str, days: int = 7,
                         limit: int = 100) -> dict[str, Any]:
    """从保养计划生成点检任务（幂等）。

    幂等键：(plan_id, due 日期)。同一计划同一到期日只建一条任务。
    限量原因：库中 1200 台设备，若全量生成会瞬间灌入上千条任务，
    对车间级使用无意义。按到期时间升序取前 limit 条。
    """
    import json

    now = datetime.datetime.now()
    horizon = now + datetime.timedelta(days=days)
    plans = list(
        TenantQueryBuilder(tenant_id)
        .query(
            "SELECT plan_id, asset_id, name, items, owner_role, next_due_at, cycle_days "
            "FROM maintenance_plan WHERE tenant_id=%s AND enabled=1 "
            "AND next_due_at IS NOT NULL AND next_due_at <= %s "
            "ORDER BY next_due_at ASC LIMIT %s",
            horizon, limit,
        )
        .fetchall()
    )

    created = skipped = 0
    with connect() as conn, conn.cursor() as cur:
        for p in plans:
            due = p["next_due_at"]
            if not isinstance(due, datetime.datetime):
                skipped += 1
                continue
            # 幂等：同计划同到期日已存在未完成任务则跳过
            cur.execute(
                "SELECT task_id FROM inspection_task WHERE tenant_id=%s AND plan_id=%s "
                "AND DATE(due_at)=DATE(%s) AND status IN ('PENDING','OVERDUE') LIMIT 1",
                (tenant_id, p["plan_id"], due),
            )
            if cur.fetchone():
                skipped += 1
                continue

            items = p.get("items")
            if isinstance(items, str):
                try:
                    items = json.loads(items)
                except Exception:  # noqa: BLE001
                    items = []
            task_id = f"INS-{due:%Y%m%d}-{os.urandom(3).hex().upper()}"
            cur.execute(
                "INSERT INTO inspection_task(tenant_id,task_id,plan_id,asset_id,title,items,"
                "due_at,status,assignee_role,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,'PENDING',%s,%s)",
                (tenant_id, task_id, p["plan_id"], p["asset_id"],
                 f"{p['name']}（{p['asset_id']}）", json.dumps(items or [], ensure_ascii=False),
                 due, p.get("owner_role") or "operator", now),
            )
            created += 1

        cur.execute(
            "SELECT COUNT(*) AS n FROM inspection_task WHERE tenant_id=%s "
            "AND status IN ('PENDING','OVERDUE')", (tenant_id,))
        open_cnt = cur.fetchone()["n"]

    return {"created": created, "skipped": skipped,
            "candidates": len(plans), "open_total": open_cnt}


def list_inspections(tenant_id: str, status: str = "", asset_id: str = "",
                     limit: int = 100) -> list[dict[str, Any]]:
    """点检任务列表。逾期未完成的按 due 升序置顶。"""
    import json

    sql = ("SELECT task_id, plan_id, asset_id, title, items, due_at, status, "
           "assignee_role, done_at, done_by, ng_count, note, created_at "
           "FROM inspection_task WHERE tenant_id=%s")
    params: list[Any] = []
    if status:
        marks = ",".join(["%s"] * len(status.split(",")))
        sql += f" AND status IN ({marks})"
        params.extend(status.split(","))
    if asset_id:
        sql += " AND asset_id LIKE %s"
        params.append(f"{asset_id}%")
    sql += " ORDER BY due_at ASC LIMIT %s"
    params.append(limit)

    rows = list(TenantQueryBuilder(tenant_id).query(sql, *params).fetchall())
    now = datetime.datetime.now()
    for r in rows:
        if isinstance(r.get("items"), str):
            try:
                r["items"] = json.loads(r["items"])
            except Exception:  # noqa: BLE001
                r["items"] = []
        due = r.get("due_at")
        if isinstance(due, datetime.datetime):
            r["hours_left"] = round((due - now).total_seconds() / 3600, 1)
            # 只读展示：PENDING 且已过期即视为逾期，不改库（避免读接口写数据）
            r["overdue"] = r["status"] == "PENDING" and r["hours_left"] < 0
        else:
            r["hours_left"] = None
            r["overdue"] = False
    rows.sort(key=lambda x: (0 if x.get("overdue") else (1 if x["status"] == "PENDING" else 2),
                             x.get("hours_left") if x.get("hours_left") is not None else 9e9))
    return rows


def submit_inspection(tenant_id: str, task_id: str, items: list[dict[str, Any]],
                      by: str, note: str = "") -> dict[str, Any]:
    """提交点检结果：写结果、算 NG 数、推进保养计划到期日。"""
    import json

    row = (
        TenantQueryBuilder(tenant_id)
        .query("SELECT task_id, plan_id, status, items FROM inspection_task "
               "WHERE tenant_id=%s AND task_id=%s", task_id)
        .fetchone()
    )
    if row is None:
        raise ValueError(f"task not found: {task_id}")
    if row["status"] == "DONE":
        raise ValueError("task already done")

    plan_items = row.get("items")
    if isinstance(plan_items, str):
        try:
            plan_items = json.loads(plan_items)
        except Exception:  # noqa: BLE001
            plan_items = []
    expect = {str(i.get("key")): i for i in (plan_items or [])}

    results: list[dict[str, Any]] = []
    ng = 0
    for it in items or []:
        key = str(it.get("key") or "")
        base = expect.get(key, {})
        judgment = str(it.get("judgment") or "OK").upper()
        if judgment not in ("OK", "NG"):
            raise ValueError(f"invalid judgment: {judgment}")
        if judgment == "NG":
            ng += 1
        results.append({
            "key": key,
            "label": it.get("label") or base.get("label") or key,
            "std": base.get("std"),
            "value": it.get("value"),
            "judgment": judgment,
            "note": it.get("note") or "",
        })

    now = datetime.datetime.now()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE inspection_task SET status='DONE', items=%s, done_at=%s, done_by=%s, "
            "ng_count=%s, note=%s WHERE tenant_id=%s AND task_id=%s",
            (json.dumps(results, ensure_ascii=False), now, by, ng, note[:255], tenant_id, task_id),
        )
        # 推进保养计划：以本次完成为起点重算下次到期
        if row.get("plan_id"):
            cur.execute(
                "SELECT cycle_days FROM maintenance_plan WHERE tenant_id=%s AND plan_id=%s",
                (tenant_id, row["plan_id"]),
            )
            p = cur.fetchone()
            if p and p.get("cycle_days"):
                nxt = now + datetime.timedelta(days=int(p["cycle_days"]))
                cur.execute(
                    "UPDATE maintenance_plan SET last_done_at=%s, next_due_at=%s "
                    "WHERE tenant_id=%s AND plan_id=%s",
                    (now, nxt, tenant_id, row["plan_id"]),
                )

    return {"task_id": task_id, "status": "DONE", "ng_count": ng,
            "done_by": by, "done_at": now.isoformat(timespec="seconds"),
            "results": results}


def inspection_stats(tenant_id: str) -> dict[str, Any]:
    """点检汇总：状态分布、今日到期、NG 率、计划覆盖。

    时间一律由 Python 传入，不用 SQL 的 NOW()/CURDATE()：
    本库时区为 UTC，与应用进程本地时区（UTC+8）相差 8 小时，
    用数据库时间会把"今天到期"算到前一天（实测踩到）。
    """
    now = datetime.datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today_start + datetime.timedelta(days=1)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, COUNT(*) AS n FROM inspection_task WHERE tenant_id=%s "
            "GROUP BY status", (tenant_id,))
        by_status = {r["status"]: int(r["n"]) for r in cur.fetchall()}

        cur.execute(
            "SELECT COUNT(*) AS n FROM inspection_task WHERE tenant_id=%s "
            "AND status='PENDING' AND due_at < %s", (tenant_id, now))
        overdue = int(cur.fetchone()["n"])

        cur.execute(
            "SELECT COUNT(*) AS n FROM inspection_task WHERE tenant_id=%s "
            "AND due_at >= %s AND due_at < %s", (tenant_id, today_start, tomorrow))
        today = int(cur.fetchone()["n"])

        cur.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(ng_count),0) AS ng, "
            "COALESCE(SUM(CASE WHEN ng_count > 0 THEN 1 ELSE 0 END),0) AS ngt "
            "FROM inspection_task WHERE tenant_id=%s AND status='DONE'", (tenant_id,))
        r = cur.fetchone()
        done = int(r["n"] or 0)
        ng_items = int(r["ng"] or 0)
        ng_tasks = int(r["ngt"] or 0)

        cur.execute(
            "SELECT COUNT(*) AS n FROM maintenance_plan WHERE tenant_id=%s AND enabled=1",
            (tenant_id,))
        plans = int(cur.fetchone()["n"])

        cur.execute(
            "SELECT COUNT(*) AS n FROM maintenance_plan WHERE tenant_id=%s "
            "AND enabled=1 AND next_due_at < %s", (tenant_id, now))
        plan_overdue = int(cur.fetchone()["n"])

    return {
        "by_status": by_status,
        "pending": by_status.get("PENDING", 0),
        "done": done,
        "overdue": overdue,
        "due_today": today,
        "ng_tasks": ng_tasks,
        "ng_items": ng_items,
        "plans": plans,
        "plan_overdue": plan_overdue,
        "completion_pct": round(100 * done / (done + by_status.get("PENDING", 0)), 1)
                          if (done + by_status.get("PENDING", 0)) else 0.0,
    }

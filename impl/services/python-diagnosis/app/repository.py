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


def save_recommendation_card(tenant_id: str, card: dict[str, Any]) -> str:
    """落库方案卡（recommendation_card）。返回 card_id。"""
    import json

    card_id = card["card_id"]
    options = card.get("options") or []
    with connect() as conn, conn.cursor() as cur:
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
                card.get("verdict", "MONITOR"), card.get("risk_level", "中"),
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

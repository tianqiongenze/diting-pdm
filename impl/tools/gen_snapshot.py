"""造数阶段 3：多源指标快照（每台设备 1 行，供诊断与 warmup 使用）。

与 feature_point 的关系：
- feature_point：1 亿行历史明细，算趋势、查历史、做 RUL
- asset_metric_snapshot：2400 行最新快照，一次查询拿全 5 维，供诊断
"""

from __future__ import annotations

import os

import pymysql

DB = dict(
    host=os.getenv("PDM_DB_HOST", "10.167.2.175"),
    port=int(os.getenv("PDM_DB_PORT", "30210")),
    user=os.getenv("PDM_DB_USER", "pdm_diag"),
    password=os.getenv("PDM_DB_PASSWORD", ""),
    database="pdm",
    charset="utf8mb4",
    autocommit=True,
    cursorclass=pymysql.cursors.DictCursor,
)


def main() -> None:
    c = pymysql.connect(**DB)
    cur = c.cursor()
    cur.execute(
        """
        SELECT fp.tenant_id, fp.mp_id, a.asset_id, a.category, a.criticality
        FROM feature_point fp
        JOIN asset a ON a.tenant_id = fp.tenant_id AND a.asset_id = CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(fp.mp_id,'.',3),'.',-1) AS CHAR)
        """
    )
    # 上面的 JOIN 依赖命名规则，若为空则退化为按资产列表生成
    rows = cur.fetchall()
    if not rows:
        cur.execute("SELECT tenant_id, asset_id, category, criticality FROM asset")
        rows = cur.fetchall()

    # 直接从 feature_point 聚合出每台设备的最新值（mp 命名含 asset_id）
    cur.execute(
        """
        SELECT fp.tenant_id,
               CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(fp.mp_id,'.',3),'.',-1) AS CHAR) AS asset_id,
               MAX(fp.rms) AS rms, MAX(fp.kurtosis) AS k, MAX(fp.ts) AS ts
        FROM feature_point fp
        GROUP BY fp.tenant_id, CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(fp.mp_id,'.',3),'.',-1) AS CHAR)
        """
    )
    agg = {(r["tenant_id"], r["asset_id"]): r for r in cur.fetchall()}

    batch = []
    for r in rows:
        key = (r["tenant_id"], r["asset_id"])
        a = agg.get(key)
        if not a:
            continue
        rms = float(a["rms"] or 2.0)
        base = max(0.5, rms * 0.72)
        kurt = float(a["k"] or 3.5)
        crit = r.get("criticality", "B")
        # 关键度越高，温度/电流偏离越大（模拟重载设备）
        temp_base = 45.0
        temp = temp_base + (8.0 if crit == "A" else 5.0 if crit == "B" else 3.0) + (kurt - 3.0) * 0.8
        cur_dev = (kurt - 3.0) * 2.5 + (2.0 if crit == "A" else 0.0)
        trend = max(0.0, (rms / base - 1.0) * 0.9)
        batch.append((r["tenant_id"], r["asset_id"], rms, base, kurt, temp, temp_base, cur_dev, trend, a["ts"]))
        if len(batch) >= 500:
            _upsert(cur, batch)
            batch.clear()
    if batch:
        _upsert(cur, batch)

    cur.execute("SELECT COUNT(*) AS n FROM asset_metric_snapshot")
    print(f"[snapshot] rows={cur.fetchone()['n']}")
    c.close()


def _upsert(cur, batch) -> None:
    cur.executemany(
        "INSERT INTO asset_metric_snapshot(tenant_id,asset_id,vib_rms_mm_s,vib_baseline_mm_s,kurtosis,"
        "temp_c,temp_baseline_c,current_dev_pct,trend_pct_per_day,ts) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE vib_rms_mm_s=VALUES(vib_rms_mm_s),vib_baseline_mm_s=VALUES(vib_baseline_mm_s),"
        "kurtosis=VALUES(kurtosis),temp_c=VALUES(temp_c),temp_baseline_c=VALUES(temp_baseline_c),"
        "current_dev_pct=VALUES(current_dev_pct),trend_pct_per_day=VALUES(trend_pct_per_day),ts=VALUES(ts)",
        batch,
    )


if __name__ == "__main__":
    main()

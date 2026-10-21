"""造数：为雅迪场景生成 ~1 亿条时序特征点 + 配套元数据。

设计要点：
- **确定性生成**：给定 (asset_idx, mp_idx, ts_idx) 必然得到同一行，重复执行用 INSERT IGNORE 幂等
- **多进程并行**：按 mp 区间切分，每个 worker 独立连接，避免单连接成为瓶颈
- **批量插入**：默认 3000 行/批（TiDB 大事务代价高，3000 是吞吐与内存的折中）
- **贴近真实**：不同设备类型有不同的基线/波动/故障注入，不是纯随机噪声
- **可断点续跑**：已存在的行直接跳过，中断后重跑不会重复

用法：
    python tools/gen_data.py meta                    # 只生成租户/组织/资产/测点
    python tools/gen_data.py features --rows 100000000 --workers 8
    python tools/gen_data.py verify
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta
from multiprocessing import Process

import pymysql

DB = dict(
    host=os.getenv("PDM_DB_HOST", "10.167.2.175"),
    port=int(os.getenv("PDM_DB_PORT", "30210")),
    user=os.getenv("PDM_DB_USER", "pdm_diag"),
    password=os.getenv("PDM_DB_PASSWORD", ""),
    database="pdm",
    charset="utf8mb4",
    autocommit=True,
    connect_timeout=10,
)

# 场景参数（与 docs/14 对齐：集团 + 2 基地 + 供应商）
TENANTS = [
    ("yadea", "GROUP", "雅迪集团", None, "CN"),
    ("site-a", "SITE", "无锡总装基地", "yadea", "CN"),
    ("site-b", "SITE", "天津零部件基地", "yadea", "CN"),
    ("supplier-t1-001", "SUPPLIER", "T1-电池供应商", "yadea", "CN"),
]

ASSET_CATEGORIES = [
    # (category, 基线 rms mm/s, 波动, 每台测点数)
    ("PRESS", 2.2, 0.5, 8),
    ("ROBOT", 1.6, 0.4, 12),
    ("MOTOR", 1.9, 0.45, 10),
    ("PUMP", 2.6, 0.6, 8),
    ("FAN", 3.1, 0.8, 6),
    ("CONVEYOR", 1.4, 0.3, 10),
    ("AGV", 1.2, 0.3, 6),
    ("INJECTION", 2.8, 0.7, 8),
]

POINTS_PER_MP = 5000  # 每个测点 5000 个时间点 → 20000 mp × 5000 = 1 亿


def conn():
    return pymysql.connect(**DB)


def h(*parts) -> float:
    """确定性伪随机：同一输入永远同一输出，保证多进程结果一致且可复现。"""
    d = hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest()
    return int.from_bytes(d, "big") / 2**64


# ---------------------------------------------------------------------------
# 阶段 1：元数据
# ---------------------------------------------------------------------------
def gen_meta(assets: int = 2000) -> None:
    c = conn()
    cur = c.cursor()

    for t in TENANTS:
        cur.execute(
            "INSERT IGNORE INTO tenant(tenant_id,tenant_type,name,parent_id,region) VALUES (%s,%s,%s,%s,%s)", t
        )

    # 组织树：每基地 4 车间 × 3 产线
    nodes = []
    for site in ("site-a", "site-b"):
        for w in range(1, 5):
            wid = f"{site}-W{w}"
            nodes.append((site, wid, None, "WORKSHOP", f"{w}号车间", f"/{site}/w{w}"))
            for ln in range(1, 4):
                lid = f"{wid}-L{ln}"
                nodes.append((site, lid, wid, "LINE", f"{w}-{ln}产线", f"/{site}/w{w}/l{ln}"))
    cur.executemany(
        "INSERT IGNORE INTO org_node(tenant_id,node_id,parent_id,node_type,name,path) VALUES (%s,%s,%s,%s,%s,%s)",
        nodes,
    )

    asset_rows, comp_rows, mp_rows = [], [], []
    for i in range(assets):
        tenant = "site-a" if i % 2 == 0 else "site-b"
        cat, base_rms, jitter, n_mp = ASSET_CATEGORIES[i % len(ASSET_CATEGORIES)]
        asset_id = f"{cat[:3]}-{i:05d}"
        node_id = f"{tenant}-W{(i % 4) + 1}"
        criticality = "A" if i % 10 == 0 else ("B" if i % 3 == 0 else "C")
        asset_rows.append((tenant, asset_id, node_id, f"{cat}设备{i}", f"MDL-{cat}-{i % 7}", cat, criticality))

        comp_id = f"{asset_id}-C1"
        comp_rows.append((tenant, comp_id, asset_id, "BEARING", "DE", '{"n_balls":9,"ball_d":12.7,"pitch_d":65.0}'))

        for p in range(n_mp):
            direction = ["H", "V", "A"][p % 3]
            quantity = "vib_vel" if p % 4 else "vib_acc"
            mp_id = f"{tenant}.{node_id}.{asset_id}.{comp_id}.P{p}.{quantity}"
            mp_rows.append((tenant, mp_id, asset_id, comp_id, quantity, "mm/s" if quantity == "vib_vel" else "m/s2",
                            direction, 25600 if quantity == "vib_acc" else None))

    cur.executemany(
        "INSERT IGNORE INTO asset(tenant_id,asset_id,node_id,name,model,category,criticality) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        asset_rows,
    )
    cur.executemany(
        "INSERT IGNORE INTO component(tenant_id,comp_id,asset_id,comp_type,position,spec) VALUES (%s,%s,%s,%s,%s,%s)",
        comp_rows,
    )
    cur.executemany(
        "INSERT IGNORE INTO measurement_point"
        "(tenant_id,mp_id,asset_id,comp_id,quantity,unit,direction,sample_rate,calibrated_at,calibration_no) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'2026-08-01',%s)",
        [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], f"CAL-2026-{i:05d}") for i, r in enumerate(mp_rows)],
    )

    cur.execute("SELECT COUNT(*) FROM asset")
    a = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM measurement_point")
    m = cur.fetchone()[0]
    print(f"[meta] tenants={len(TENANTS)} assets={a} measurement_points={m}")
    c.close()


# ---------------------------------------------------------------------------
# 阶段 2：特征点（多进程）
# ---------------------------------------------------------------------------
def load_mps(limit: int | None = None) -> list[tuple[str, str, str, float, float]]:
    """返回 (tenant_id, mp_id, asset_id, base_rms, jitter)。"""
    c = conn()
    cur = c.cursor()
    sql = """
        SELECT mp.tenant_id, mp.mp_id, mp.asset_id, a.category
        FROM measurement_point mp JOIN asset a
          ON a.tenant_id = mp.tenant_id AND a.asset_id = mp.asset_id
        ORDER BY mp.mp_id
    """
    if limit:
        sql += f" LIMIT {limit}"
    cur.execute(sql)
    rows = cur.fetchall()
    c.close()

    base = {c[0]: (c[1], c[2]) for c in ASSET_CATEGORIES}
    out = []
    for tenant, mp_id, asset_id, cat in rows:
        b, j = base.get(cat, (2.0, 0.5))
        out.append((tenant, mp_id, asset_id, b, j))
    return out


def _worker(worker_id: int, mps: list, batch: int, start_ts: datetime) -> None:
    c = conn()
    cur = c.cursor()
    sql = (
        "INSERT IGNORE INTO feature_point"
        "(tenant_id,mp_id,ts,rms,peak,kurtosis,skewness,crest,hi,level,quality) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    )
    total = 0
    t0 = time.time()
    for tenant, mp_id, asset_id, base_rms, jitter in mps:
        rows = []
        # 每台设备有独立的劣化速率（部分设备故意注入故障趋势）
        seed = h(asset_id)
        degrade = 1.0 + (seed - 0.5) * 1.6  # 0.2 ~ 1.8 倍劣化速度
        for k in range(POINTS_PER_MP):
            ts = start_ts + timedelta(hours=k)
            # 季节/班次周期 + 个体噪声 + 缓慢劣化
            seasonal = 1.0 + 0.08 * math.sin(2 * math.pi * k / 24) + 0.05 * math.sin(2 * math.pi * k / 168)
            noise = 1.0 + (h(mp_id, k) - 0.5) * jitter * 0.6
            wear = 1.0 + degrade * 0.00012 * k
            rms = max(0.2, base_rms * seasonal * noise * wear)
            kurt = 3.0 + (h(mp_id, k, "k") - 0.5) * 2.4 + max(0.0, (wear - 1.0) * 6)
            hi = max(0.0, min(100.0, 100.0 - (rms / base_rms - 1.0) * 28.0 - max(0.0, kurt - 3.0) * 2.2))
            level = "green" if hi >= 80 else ("yellow" if hi >= 60 else ("orange" if hi >= 40 else "red"))
            rows.append((
                tenant, mp_id, ts, round(rms, 4), round(rms * 2.8, 4), round(kurt, 4),
                round((h(mp_id, k, "s") - 0.5) * 0.8, 4), round(2.8 + (h(mp_id, k, "c") - 0.5) * 0.6, 4),
                round(hi, 2), level, 95 if h(mp_id, k, "q") > 0.03 else 45,
            ))
            if len(rows) >= batch:
                cur.executemany(sql, rows)
                total += len(rows)
                rows.clear()
        if rows:
            cur.executemany(sql, rows)
            total += len(rows)

    dt = time.time() - t0
    print(f"[worker-{worker_id}] {total:,} rows in {dt:.1f}s = {total / dt:,.0f} rows/s", flush=True)
    c.close()


def gen_features(rows: int, workers: int, batch: int) -> None:
    all_mps = load_mps()
    need_mps = max(1, rows // POINTS_PER_MP)
    if need_mps > len(all_mps):
        print(f"WARN: 需要 {need_mps} 个测点但只有 {len(all_mps)}，实际行数将少于此值")
        need_mps = len(all_mps)
    all_mps = all_mps[:need_mps]

    start_ts = datetime(2026, 10, 21, 0, 0, 0) - timedelta(hours=POINTS_PER_MP - 1)
    chunk = math.ceil(len(all_mps) / workers)

    procs = []
    for w in range(workers):
        part = all_mps[w * chunk : (w + 1) * chunk]
        if not part:
            continue
        p = Process(target=_worker, args=(w, part, batch, start_ts))
        p.start()
        procs.append(p)
    for p in procs:
        p.join()

    print(f"[features] 完成：目标 {rows:,} 行，测点 {len(all_mps):,}，时间点 {POINTS_PER_MP}")


# ---------------------------------------------------------------------------
def verify() -> None:
    c = conn()
    cur = c.cursor()
    for t in ("tenant", "org_node", "asset", "measurement_point", "feature_point", "read_asset_latest"):
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        print(f"  {t:<20} {cur.fetchone()[0]:>12,}")
    cur.execute("SELECT level, COUNT(*) FROM feature_point GROUP BY level ORDER BY 2 DESC")
    print("  level 分布:", dict(cur.fetchall()))
    cur.execute("SELECT MIN(ts), MAX(ts) FROM feature_point")
    print("  时间范围:", cur.fetchone())
    cur.execute("SHOW TABLE STATS pdm.feature_point" if False else "SELECT 1")
    c.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["meta", "features", "verify"])
    ap.add_argument("--rows", type=int, default=100_000_000)
    ap.add_argument("--assets", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--batch", type=int, default=3000)
    a = ap.parse_args()

    if a.phase == "meta":
        gen_meta(a.assets)
    elif a.phase == "features":
        gen_features(a.rows, a.workers, a.batch)
    else:
        verify()

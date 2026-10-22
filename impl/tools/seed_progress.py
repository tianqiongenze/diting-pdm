#!/usr/bin/env python3
"""课程平台数据填充进度监控（一次性快照）。

用法:
    python tools/seed_progress.py            # 当前快照
    python tools/seed_progress.py --watch 60 # 每 60 秒刷新一次

目标总量由 SCALE=20 决定（101,580,420 行）。
分片日志位置 /tmp/seed_s*.log 由 tools/seed_all.sh 约定。
"""

from __future__ import annotations

import argparse
import os
import time

import pymysql

HOST = os.getenv("PDM_TIDB_HOST", "10.167.2.175")
PORT = int(os.getenv("PDM_TIDB_PORT", "30210"))

TABLES = (
    "tenant_db.tenants",
    "iam_db.roles",
    "iam_db.users",
    "course_db.courses",
    "course_db.chapters",
    "course_db.knowledge_points",
    "course_db.resources",
    "teaching_db.learning_paths",
    "teaching_db.learning_path_steps",
    "teaching_db.assignments",
    "teaching_db.exams",
    "teaching_db.discussions",
    "teaching_db.live_sessions",
    "tenant_db.credit_transfer_rules",
    "tenant_db.credit_transfer_records",
)

# scale=20 的期望行数。口径与 seed_course_platform.py 的 plan() 严格一致：
#   30 租户 × 800 课程/租户 × 8 章/课 = 192,000 章
#   kps/resources 按【每章】计（240/200 每章）；records 按【每规则】计
#   （每租户 6 规则 × 16,000 条 = 96,000/租户）
TARGET = {
    "tenant_db.tenants": 30,
    "iam_db.roles": 240,
    "iam_db.users": 300_000,
    "course_db.courses": 24_000,
    "course_db.chapters": 192_000,
    "course_db.knowledge_points": 46_080_000,
    "course_db.resources": 38_400_000,
    "teaching_db.learning_paths": 144_000,
    "teaching_db.learning_path_steps": 1_152_000,
    "teaching_db.assignments": 144_000,
    "teaching_db.exams": 96_000,
    "teaching_db.discussions": 12_000_000,
    "teaching_db.live_sessions": 192_000,
    "tenant_db.credit_transfer_rules": 180,
    "tenant_db.credit_transfer_records": 2_880_000,
}
TOTAL = sum(TARGET.values())


def counts() -> dict[str, int]:
    conn = pymysql.connect(host=HOST, port=PORT, user="root", password="",
                           cursorclass=pymysql.cursors.DictCursor)
    out = {}
    with conn.cursor() as cur:
        for t in TABLES:
            cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
            out[t] = cur.fetchone()["n"]
    conn.close()
    return out


def shard_status(shards: int = 8) -> list[str]:
    lines = []
    for i in range(shards):
        p = f"/tmp/seed_s{i}.log"
        if not os.path.exists(p):
            lines.append(f"s{i}: 无日志")
            continue
        with open(p, encoding="utf-8", errors="ignore") as fh:
            txt = fh.read().strip().splitlines()
        lines.append(f"s{i}: {txt[-1].strip() if txt else '(空)'}")
    return lines


def report(verbose: bool = False) -> int:
    c = counts()
    tot = sum(c.values())
    pct = tot / TOTAL * 100
    print(f"总计 {tot:,} / {TOTAL:,}  ({pct:.2f}%)   剩余 {max(0, TOTAL - tot):,} 行")
    if verbose:
        for t in TABLES:
            print(f"  {t:<42} {c[t]:>12,} / {TARGET[t]:>12,}")
    return tot


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=0, help="每 N 秒刷新（0=只查一次）")
    ap.add_argument("-v", "--verbose", action="store_true", help="逐表明细")
    a = ap.parse_args()

    if not a.watch:
        report(a.verbose)
        return

    prev = 0
    while True:
        tot = report(a.verbose)
        if prev:
            print(f"  较上次 +{tot - prev:,} 行")
        prev = tot
        if tot >= TOTAL:
            print("✅ 已达目标总量")
            break
        print("  分片: " + " | ".join(shard_status()))
        print()
        time.sleep(a.watch)


if __name__ == "__main__":
    main()

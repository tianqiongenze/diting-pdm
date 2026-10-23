#!/usr/bin/env python3
"""清理重复方案卡（同租户 + 同设备 + 同 verdict 只保留最新一张）。

背景：/v1/consult 是幂等查询型接口，去重逻辑上线前每次调用都会新建卡片，
测试与刷新看板累积了大量同设备重复卡（演示时看板一片重复）。

本脚本按 (tenant_id, asset_id, verdict) 分组，每组保留 created_at 最新的一张，
其余删除（先删其确认记录，避免外键残留）。

用法：
    python tools/cleanup_cards.py            # 只统计，不删除
    python tools/cleanup_cards.py --apply    # 实际删除
    python tools/cleanup_cards.py --apply --keep 2   # 每组保留 2 张
"""

from __future__ import annotations

import argparse
import os

import pymysql


def conn():
    return pymysql.connect(
        host=os.getenv("PDM_TIDB_HOST", "10.167.2.175"),
        port=int(os.getenv("PDM_TIDB_PORT", "30210")),
        user=os.getenv("PDM_TIDB_USER", "root"),
        password=os.getenv("PDM_TIDB_PASSWORD", ""),
        database="pdm",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际删除（默认只统计）")
    ap.add_argument("--keep", type=int, default=1, help="每组保留最新 N 张")
    ap.add_argument("--window-hours", type=int, default=0,
                    help="只清理 N 小时内创建的（0=不限）")
    a = ap.parse_args()

    c = conn()
    with c.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM recommendation_card")
        total = cur.fetchone()["n"]

        where = ""
        params: list = []
        if a.window_hours > 0:
            where = "WHERE created_at >= NOW() - INTERVAL %s HOUR"
            params = [a.window_hours]

        cur.execute(
            f"SELECT tenant_id, asset_id, verdict, COUNT(*) AS n "
            f"FROM recommendation_card {where} "
            f"GROUP BY tenant_id, asset_id, verdict "
            f"HAVING COUNT(*) > %s ORDER BY n DESC",
            params + [a.keep],
        )
        dup_groups = cur.fetchall()

        print(f"总卡片数: {total}")
        print(f"重复组数（每组 >{a.keep} 张）: {len(dup_groups)}")
        for g in dup_groups[:10]:
            print(f"  {g['tenant_id']}/{g['asset_id']} verdict={g['verdict']}: {g['n']} 张")

        if not dup_groups:
            print("✅ 无重复，无需清理")
            c.close()
            return

        # 每组保留最新 keep 张
        to_delete: list[str] = []
        for g in dup_groups:
            cur.execute(
                f"SELECT card_id FROM recommendation_card {where + ' AND ' if where else 'WHERE '}"
                f"tenant_id=%s AND asset_id=%s AND verdict=%s "
                f"ORDER BY created_at DESC LIMIT 18446744073709551615 OFFSET %s",
                params + [g["tenant_id"], g["asset_id"], g["verdict"], a.keep],
            )
            to_delete.extend(r["card_id"] for r in cur.fetchall())

        print(f"\n待删除: {len(to_delete)} 张（每组保留最新 {a.keep} 张）")
        if not a.apply:
            print("（未加 --apply，仅统计。确认后重跑加 --apply）")
            c.close()
            return

        # 先删确认记录，再删卡片
        deleted_conf = 0
        for i in range(0, len(to_delete), 200):
            chunk = to_delete[i:i + 200]
            ph = ",".join(["%s"] * len(chunk))
            cur.execute(f"DELETE FROM card_confirmation WHERE tenant_id=%s AND card_id IN ({ph})",
                        [dup_groups[0]["tenant_id"]] + chunk)
            deleted_conf += cur.rowcount
            cur.execute(f"DELETE FROM recommendation_card WHERE card_id IN ({ph})", chunk)
        cur.execute("SELECT COUNT(*) AS n FROM recommendation_card")
        left = cur.fetchone()["n"]
        print(f"✅ 已删除 {len(to_delete)} 张卡片、{deleted_conf} 条确认记录")
        print(f"   剩余卡片: {left} 张")
    c.close()


if __name__ == "__main__":
    main()

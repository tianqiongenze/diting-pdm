"""事件消费者 / 读模型投影器（CQRS 的 Read Side）。

职责：
- 消费 `pdm:events:<tenant>` 流
- 把 diagnosis.finalized 投影到 read_asset_health（读模型）
- 幂等：processed_event 表去重
- 支持断点续跑（消费者组 + ACK）

为什么独立进程而不是在服务里跑：
投影器是"读侧"，与写侧解耦；独立进程可单独扩缩、单独重启，不影响诊断服务。

用法（集群内）：
    python -m app.projector --once            # 跑一批就退出（测试/定时任务）
    python -m app.projector --interval 5      # 常驻，每 5 秒拉一次
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

import redis

from . import repository

STREAM_PREFIX = "pdm:events:"
GROUP = "projector-read-model"
# 幂等键：读模型是共享的，去重必须按事件而非按消费者
PROJECTION_ID = "read-model-v1"
CONSUMER = os.getenv("HOSTNAME", "projector-1")


def _client() -> redis.Redis:
    url = os.getenv("PDM_REDIS_URL", "redis://redis.infra.svc.cluster.local:6379/3")
    return redis.Redis.from_url(url, decode_responses=True, socket_timeout=2.0)


def _ensure_group(r: redis.Redis, stream: str) -> None:
    try:
        r.xgroup_create(stream, GROUP, id="0", mkstream=True)
    except redis.exceptions.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _already_processed(tenant: str, event_id: str) -> bool:
    """幂等：processed_event 主键 (consumer, event_id)。"""
    try:
        with repository.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO processed_event(consumer, event_id) VALUES (%s, %s)",
                (PROJECTION_ID, event_id),
            )
            return cur.rowcount == 0
    except Exception:  # noqa: BLE001 - 库不可用时宁可重复投影（幂等写），也不丢事件
        return False


def project(event: dict[str, Any]) -> str | None:
    """把事件投影到读模型。返回动作描述。"""
    etype = event.get("type", "")
    tenant = event.get("tenant_id", "")
    data = event.get("data") or {}

    if etype == "com.yadea.pdm.diagnosis.finalized.v1":
        asset = data.get("asset_id")
        if not asset:
            return None
        with repository.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO read_asset_health(tenant_id,asset_id,hi,level,rul_p50_days,open_alerts) "
                "VALUES (%s,%s,%s,%s,NULL,0) "
                "ON DUPLICATE KEY UPDATE hi=VALUES(hi),level=VALUES(level)",
                (tenant, asset, data.get("hi"), data.get("level")),
            )
        return f"read_asset_health <- {asset} hi={data.get('hi')}"

    if etype == "com.yadea.pdm.decision.cardCreated.v1":
        # 方案卡投影到 open_alerts 计数（示例：有待确认卡片即视为 open）
        asset = data.get("asset_id")
        if not asset:
            return None
        with repository.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE read_asset_health SET open_alerts = open_alerts + 1 "
                "WHERE tenant_id=%s AND asset_id=%s",
                (tenant, asset),
            )
        return f"open_alerts +1 for {asset}"

    return None


def run_once(tenants: list[str], count: int = 50) -> dict[str, int]:
    r = _client()
    stat = {"read": 0, "projected": 0, "skipped": 0, "errors": 0}
    for tenant in tenants:
        stream = f"{STREAM_PREFIX}{tenant}"
        _ensure_group(r, stream)
        resp = r.xreadgroup(GROUP, CONSUMER, {stream: ">"}, count=count, block=500)
        if not resp:
            continue
        for _stream, entries in resp:
            for entry_id, fields in entries:
                stat["read"] += 1
                try:
                    event = json.loads(fields.get("data", "{}"))
                except json.JSONDecodeError:
                    stat["errors"] += 1
                    r.xack(stream, GROUP, entry_id)
                    continue
                if _already_processed(tenant, event.get("id", entry_id)):
                    stat["skipped"] += 1
                    r.xack(stream, GROUP, entry_id)
                    continue
                try:
                    action = project(event)
                    if action:
                        stat["projected"] += 1
                        print(f"[project] {tenant} {event.get('type')} → {action}", flush=True)
                    else:
                        stat["skipped"] += 1
                except Exception as exc:  # noqa: BLE001
                    stat["errors"] += 1
                    print(f"[error] {tenant} {entry_id}: {exc}", flush=True)
                    continue  # 不 ACK，留待重试
                r.xack(stream, GROUP, entry_id)
    return stat


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="跑一批就退出")
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--tenants", default=os.getenv("PDM_WARMUP_TENANTS", "site-a,site-b"))
    a = ap.parse_args()
    tenants = [t.strip() for t in a.tenants.split(",") if t.strip()]

    if a.once:
        print(run_once(tenants))
        return

    print(f"projector started: tenants={tenants} interval={a.interval}s", flush=True)
    while True:
        try:
            stat = run_once(tenants)
            if stat["read"]:
                print(stat, flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[fatal] {exc}", flush=True)
        time.sleep(a.interval)


if __name__ == "__main__":
    main()

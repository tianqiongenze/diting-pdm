"""Pulsar 消费者 / 读模型投影器（CQRS 的 Read Side，Pulsar 路径）。

为什么有两个投影器：
| 投影器 | 事件源 | 用途 |
|---|---|---|
| `projector.py` | Redis Stream | 主路径（当前） |
| `pulsar_consumer.py`（本模块） | **Pulsar** | 生产路径，事件总线的正式入口 |
| `go-eventpipe` | Redis Stream | 降级路径 / 高吞吐场景 |

三者共用幂等键 `read-model-v1`，同一事件只会被投影一次，可同时运行不双写。

为什么 Go 投影器不直接读 Pulsar：
Go 官方 `pulsar-client` 依赖 C++ 库（CGO），需在镜像内编译 C++ 工具链，
成本远高于收益。因此 Go 侧仍读 Redis，Pulsar 侧由本模块消费。

用法：
    python -m app.pulsar_consumer --tenants site-a,site-b --interval 2
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

from . import repository

SUBSCRIPTION = "pdm-projector-pulsar"
PROJECTION_ID = "read-model-v1"  # 与 projector.py / go-eventpipe 一致，保证全局幂等


def _already_processed(event_id: str) -> bool:
    try:
        with repository.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO processed_event(consumer, event_id) VALUES (%s, %s)",
                (PROJECTION_ID, event_id),
            )
            return cur.rowcount == 0
    except Exception:  # noqa: BLE001 - 库不可用时宁可重复投影，不可丢事件
        return False


def project(event: dict[str, Any]) -> str | None:
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
        return f"health<-{asset} hi={data.get('hi')}"

    if etype == "com.yadea.pdm.decision.cardCreated.v1":
        asset = data.get("asset_id")
        if not asset:
            return None
        with repository.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE read_asset_health SET open_alerts = open_alerts + 1 "
                "WHERE tenant_id=%s AND asset_id=%s",
                (tenant, asset),
            )
        return f"alerts+1<-{asset}"

    return None


def run_forever(client: Any, tenants: list[str], timeout_ms: int = 1000) -> None:
    """常驻消费：每个 topic 建一个持久 consumer，循环 receive。

    为什么不能每轮新建 consumer：新建 consumer 会重新定位订阅游标，
    在关闭-重建的间隙里发布的消息可能被跳过。持久 consumer 由客户端
    维护内部游标，未 ACK 的消息会自动重投。
    """
    consumers = []
    for tenant in tenants:
        topic = f"persistent://pdm/{tenant}/diagnosis-events"
        try:
            consumers.append((tenant, topic, client.subscribe(topic, SUBSCRIPTION)))
            print(f"[sub] {topic} subscription={SUBSCRIPTION}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] subscribe {topic}: {exc}", flush=True)

    if not consumers:
        print("[fatal] no consumer created", flush=True)
        return

    stat = {"read": 0, "projected": 0, "skipped": 0, "errors": 0}
    print(f"[loop] consuming {len(consumers)} topics", flush=True)
    while True:
        progressed = False
        for tenant, topic, consumer in consumers:
            try:
                msg = consumer.receive(timeout_millis=timeout_ms)
            except Exception:  # noqa: BLE001 - 超时即无消息，正常
                continue
            if msg is None:
                continue
            progressed = True
            stat["read"] += 1
            try:
                event = json.loads(msg.data().decode())
            except Exception:  # noqa: BLE001
                stat["errors"] += 1
                consumer.acknowledge(msg)
                continue
            if _already_processed(event.get("id", str(msg.message_id()))):
                stat["skipped"] += 1
                consumer.acknowledge(msg)
                continue
            try:
                action = project(event)
                if action:
                    stat["projected"] += 1
                    print(f"[pulsar] {tenant} {event.get('type')} → {action}", flush=True)
                else:
                    stat["skipped"] += 1
            except Exception as exc:  # noqa: BLE001
                stat["errors"] += 1
                print(f"[error] {tenant}: {exc}", flush=True)
                consumer.negative_acknowledge(msg)
                continue
            consumer.acknowledge(msg)
        if progressed and stat["read"] % 20 == 0:
            print(stat, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenants", default=os.getenv("PDM_TENANTS", "site-a,site-b"))
    ap.add_argument("--interval", type=int, default=2, help="未使用（常驻模式保留）")
    a = ap.parse_args()
    tenants = [t.strip() for t in a.tenants.split(",") if t.strip()]

    import pulsar

    url = os.getenv("PDM_PULSAR_URL", "pulsar://pulsar-broker.infra.svc.cluster.local:6650")
    client = pulsar.Client(url)
    print(f"pulsar-consumer started: {url} tenants={tenants}", flush=True)
    try:
        run_forever(client, tenants)
    finally:
        client.close()


if __name__ == "__main__":
    main()

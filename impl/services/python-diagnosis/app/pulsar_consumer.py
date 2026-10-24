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
BATCH_SIZE = int(os.getenv("PDM_CONSUMER_BATCH", "200"))
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


def _write_history(cur, tenant: str, asset: str, hi, level) -> None:
    """写 HI 历史（看板趋势图）。失败不影响读模型，静默降级。"""
    import datetime

    try:
        cur.execute(
            "INSERT IGNORE INTO read_asset_health_history(tenant_id,asset_id,hi,level,ts) "
            "VALUES (%s,%s,%s,%s,%s)",
            (tenant, asset, hi, level, datetime.datetime.now()),
        )
    except Exception:  # noqa: BLE001
        pass


def _project_one(cur: Any, event: dict[str, Any]) -> str | None:
    """用调用方提供的游标做投影（不开新连接）。

    拆出来是为了让消费循环能在一批消息里复用同一个连接：
    原实现每条消息调两次 repository.connect()，1200 条消息就是 2400 次
    建连，实测吞吐只有 ~1.5 条/秒，15 分钟的批量诊断根本追不上。
    """
    etype = event.get("type", "")
    tenant = event.get("tenant_id", "")
    data = event.get("data") or {}

    if etype == "com.yadea.pdm.diagnosis.finalized.v1":
        asset = data.get("asset_id")
        if not asset:
            return None
        cur.execute(
            "INSERT INTO read_asset_health(tenant_id,asset_id,hi,level,rul_p50_days,open_alerts) "
            "VALUES (%s,%s,%s,%s,NULL,0) "
            "ON DUPLICATE KEY UPDATE hi=VALUES(hi),level=VALUES(level)",
            (tenant, asset, data.get("hi"), data.get("level")),
        )
        _write_history(cur, tenant, asset, data.get("hi"), data.get("level"))
        return f"health<-{asset} hi={data.get('hi')}"

    if etype == "com.yadea.pdm.decision.cardCreated.v1":
        asset = data.get("asset_id")
        if not asset:
            return None
        cur.execute(
            "UPDATE read_asset_health SET open_alerts = open_alerts + 1 "
            "WHERE tenant_id=%s AND asset_id=%s",
            (tenant, asset),
        )
        return f"alerts+1<-{asset}"

    return None


def project(event: dict[str, Any]) -> str | None:
    """单事件投影（保留给单元测试与一次性调用）。"""
    with repository.connect() as conn, conn.cursor() as cur:
        action = _project_one(cur, event)
        conn.commit()
        return action


def _dedupe(cur: Any, event_id: str) -> bool:
    """已处理过返回 True（同连接内完成，避免额外建连）。"""
    cur.execute(
        "INSERT IGNORE INTO processed_event(consumer, event_id) VALUES (%s, %s)",
        (PROJECTION_ID, event_id),
    )
    return cur.rowcount == 0


def run_forever(client: Any, tenants: list[str], timeout_ms: int = 1000) -> None:
    """常驻消费：每个 topic 建一个持久 consumer，循环 receive。

    为什么不能每轮新建 consumer：新建 consumer 会重新定位订阅游标，
    在关闭-重建的间隙里发布的消息可能被跳过。持久 consumer 由客户端
    维护内部游标，未 ACK 的消息会自动重投。
    """
    import pulsar  # 局部导入：避免模块级依赖，便于单元测试

    consumers = []
    for tenant in tenants:
        topic = f"persistent://pdm/{tenant}/diagnosis-events"
        try:
            # Shared 订阅：允许多个消费者实例共同消费同一订阅（可水平扩展）。
            # 默认 Exclusive 会在滚动更新时因旧实例未退出而 ConsumerBusy。
            consumers.append((tenant, topic, client.subscribe(
                topic, SUBSCRIPTION, consumer_type=pulsar.ConsumerType.Shared)))
            print(f"[sub] {topic} subscription={SUBSCRIPTION}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] subscribe {topic}: {exc}", flush=True)

    if not consumers:
        print("[fatal] no consumer created", flush=True)
        return

    stat = {"read": 0, "projected": 0, "skipped": 0, "errors": 0, "batches": 0}
    print(f"[loop] consuming {len(consumers)} topics (batch={BATCH_SIZE})", flush=True)
    while True:
        # ---- 1) 尽量一次多取：首条阻塞等待，其余非阻塞抽干 ----
        batch: list[tuple[Any, Any]] = []          # (consumer, msg)
        for tenant, topic, consumer in consumers:
            first = True
            while len(batch) < BATCH_SIZE:
                try:
                    msg = consumer.receive(
                        timeout_millis=timeout_ms if first else 0)
                except Exception:  # noqa: BLE001 - 超时即无消息，正常
                    break
                if msg is None:
                    break
                batch.append((consumer, msg))
                first = False
        if not batch:
            continue

        # ---- 2) 一批消息共用一个连接、一个事务 ----
        stat["read"] += len(batch)
        stat["batches"] += 1
        try:
            with repository.connect() as conn, conn.cursor() as cur:
                for consumer, msg in batch:
                    try:
                        event = json.loads(msg.data().decode())
                    except Exception:  # noqa: BLE001 - 脏数据：解析不了重投也没用
                        stat["errors"] += 1
                        continue
                    if _dedupe(cur, event.get("id", str(msg.message_id()))):
                        stat["skipped"] += 1
                        continue
                    try:
                        action = _project_one(cur, event)
                    except Exception as exc:  # noqa: BLE001
                        stat["errors"] += 1
                        print(f"[error] project failed: {exc}", flush=True)
                        continue
                    if action:
                        stat["projected"] += 1
                    else:
                        stat["skipped"] += 1
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - 整批失败：全部重投
            stat["errors"] += len(batch)
            print(f"[error] batch failed: {exc}", flush=True)
            for consumer, msg in batch:
                try:
                    consumer.negative_acknowledge(msg)
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(1)
            continue

        # 整批提交成功后统一 ACK（含脏数据：解析失败的重投无意义，直接放过）
        for consumer, msg in batch:
            try:
                consumer.acknowledge(msg)
            except Exception:  # noqa: BLE001
                pass
        print(stat, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenants", default=os.getenv("PDM_TENANTS", "site-a,site-b"))
    ap.add_argument("--interval", type=int, default=2, help="未使用（常驻模式保留）")
    a = ap.parse_args()
    tenants = [t.strip() for t in a.tenants.split(",") if t.strip()]

    import pulsar  # 局部导入：run_forever 内亦各自导入，避免模块级依赖

    url = os.getenv("PDM_PULSAR_URL", "pulsar://pulsar-broker.infra.svc.cluster.local:6650")
    client = pulsar.Client(url)
    print(f"pulsar-consumer started: {url} tenants={tenants}", flush=True)
    try:
        run_forever(client, tenants)
    finally:
        client.close()


if __name__ == "__main__":
    main()

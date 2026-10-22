"""EAM/MES 出站集成（Outbox 模式 + 异步重试 + 死信）。

为什么用 Outbox 而不是同步 webhook：
    EAM/MES 常在计划维护窗口不可用。若在确认工单时同步调用 EAM，
    一次超时就会让整个确认链路失败。Outbox 把"必须送达"和"立即响应"
    解耦：确认动作本地事务落库即返回，投递由后台 worker 负责。

契约（与 docs/06 §7 一致）：
    POST {EAM_BASE}/api/v1/work-orders
    Header: X-External-Id  → 幂等键，EAM 侧据此去重（重复提交不建重复工单）
    Header: X-Signature    → HMAC-SHA256(body, secret)，防伪造
    Body:   {card_id, asset_id, conclusion, options, economics, confirm_roles}

投递策略：
    - 指数退避：1m, 5m, 15m, 1h, 6h（最多 5 次）
    - 超过最大次数 → status=DEAD_LETTER，人工介入
    - 4xx（除 429）视为永久失败，立即 DEAD_LETTER，不浪费重试

用法：
    python -m app.integration_worker --once       # 跑一批
    python -m app.integration_worker              # 常驻
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from . import repository

MAX_ATTEMPTS = 5
# 退避序列（秒）
BACKOFF = [60, 300, 900, 3600, 21600]


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post_work_order(base: str, secret: str, external_id: str, payload: dict[str, Any]) -> tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(base.rstrip("/") + "/api/v1/work-orders", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-External-Id", external_id)
    req.add_header("X-Signature", _sign(body, secret))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:  # noqa: BLE001 - 网络错误统一按可重试处理
        return 0, str(e)[:200]


def enqueue(tenant_id: str, target: str, event_type: str, external_id: str, payload: dict[str, Any]) -> bool:
    """写入 Outbox（幂等：同一 external_id 重复入队被忽略）。"""
    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT IGNORE INTO integration_outbox(tenant_id,target,event_type,external_id,payload) "
            "VALUES (%s,%s,%s,%s,%s)",
            (tenant_id, target, event_type, external_id, json.dumps(payload, ensure_ascii=False)),
        )
        return cur.rowcount > 0


def work_order_payload_from_card(card: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    """方案卡 → EAM 工单报文。"""
    return {
        "source": "diting-pdm",
        "external_id": card["card_id"],
        "tenant_id": tenant_id,
        "asset_id": card["asset_id"],
        "title": f"预测性维修：{card['asset_id']}",
        "conclusion": card["diagnosis"]["conclusion"],
        "priority": "P1" if card.get("verdict") == "ACT_NOW" else "P3",
        "verdict": card.get("verdict"),
        "options": card.get("options", []),
        "economics": card.get("economics", {}),
        "confirm_roles": card["human_confirm"]["required_roles"],
        "disclaimer": card.get("disclaimer", ""),
    }


def dispatch_batch(limit: int = 20) -> dict[str, int]:
    """投递一批待发消息。返回统计。"""
    base = os.getenv("EAM_BASE", "http://mock-eam.pdm-prod.svc.cluster.local:8082")
    secret = os.getenv("EAM_HMAC_SECRET", "diting-eam-secret")

    stat = {"picked": 0, "sent": 0, "retry": 0, "dead": 0, "skipped": 0}
    now = datetime.datetime.now()

    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, tenant_id, target, external_id, payload, attempts FROM integration_outbox "
            "WHERE status='PENDING' AND next_retry_at<=%s ORDER BY id LIMIT %s",
            (now, limit),
        )
        rows = cur.fetchall()

    for r in rows:
        stat["picked"] += 1
        payload = r["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        attempts = int(r["attempts"]) + 1

        status, detail = _post_work_order(base, secret, r["external_id"], payload)

        if 200 <= status < 300:
            with repository.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE integration_outbox SET status='SENT', attempts=%s, last_error=NULL WHERE id=%s",
                    (attempts, r["id"]),
                )
            stat["sent"] += 1
            print(f"[sent] {r['external_id']} → {status}", flush=True)
            continue

        # 4xx（除 429）= 永久失败，不值得重试
        if 400 <= status < 500 and status != 429:
            with repository.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE integration_outbox SET status='DEAD_LETTER', attempts=%s, last_error=%s WHERE id=%s",
                    (attempts, f"HTTP {status}: {detail}"[:500], r["id"]),
                )
            stat["dead"] += 1
            print(f"[dead] {r['external_id']} HTTP {status}: {detail[:80]}", flush=True)
            continue

        # 可重试失败：指数退避
        if attempts >= MAX_ATTEMPTS:
            with repository.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE integration_outbox SET status='DEAD_LETTER', attempts=%s, last_error=%s WHERE id=%s",
                    (attempts, f"max attempts, last: HTTP {status}: {detail}"[:500], r["id"]),
                )
            stat["dead"] += 1
            print(f"[dead] {r['external_id']} max attempts ({attempts})", flush=True)
        else:
            wait = BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
            nxt = now + datetime.timedelta(seconds=wait)
            with repository.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE integration_outbox SET attempts=%s, next_retry_at=%s, last_error=%s WHERE id=%s",
                    (attempts, nxt, f"HTTP {status}: {detail}"[:500], r["id"]),
                )
            stat["retry"] += 1
            print(f"[retry] {r['external_id']} attempt={attempts} next={nxt:%H:%M:%S}", flush=True)

    return stat


def outbox_stats() -> dict[str, int]:
    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, COUNT(*) AS n FROM integration_outbox GROUP BY status")
        return {r["status"]: int(r["n"]) for r in cur.fetchall()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=15)
    a = ap.parse_args()

    if a.once:
        print(dispatch_batch())
        return

    print(f"integration-worker started (interval={a.interval}s)", flush=True)
    while True:
        try:
            stat = dispatch_batch()
            if stat["picked"]:
                print(stat, flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[fatal] {exc}", flush=True)
        time.sleep(a.interval)


if __name__ == "__main__":
    main()

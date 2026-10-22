"""领域事件发布（CloudEvents 1.0 信封 + 可切换传输层）。

为什么用 Redis Streams 而不是 Pulsar（当前）：
- `pulsar-client` 只提供 manylinux wheel，而服务镜像基于 musl（alpine），装不上
- 从 glibc 重建整个依赖层需重下 ~60MB @128KB/s，成本高于收益
- Redis Streams 提供 XADD/XREADGROUP/消费者组，语义足够支撑 CQRS 投影
- 传输层已抽象为 `Transport`，切换 Pulsar 只需实现 `PulsarTransport`

事件契约（docs/15 §4）：
    com.yadea.pdm.<context>.<entity>.<pastTenseVerb>.v1
    tenant_id 必须在一级字段，便于跨主题审计

可靠性策略：
- 发布失败不阻塞主请求（业务优先），但会计入 `emit_failures`
- 事件流保留 7 天（MAXLEN ~ 1e6），支持重放
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Protocol

import redis

STREAM_PREFIX = "pdm:events:"
MAXLEN = 1_000_000


class Transport(Protocol):
    def publish(self, tenant_id: str, event: dict[str, Any]) -> str | None: ...


class RedisStreamTransport:
    """Redis Streams 实现。key = pdm:events:<tenant>。"""

    def __init__(self, url: str) -> None:
        self.client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=1.0, socket_connect_timeout=1.0)
        self.client.ping()

    def publish(self, tenant_id: str, event: dict[str, Any]) -> str | None:
        key = f"{STREAM_PREFIX}{tenant_id}"
        return self.client.xadd(
            key,
            {"type": event["type"], "specversion": event["specversion"], "data": json.dumps(event, ensure_ascii=False)},
            maxlen=MAXLEN,
            approximate=True,
        )


class NullTransport:
    def publish(self, tenant_id: str, event: dict[str, Any]) -> str | None:
        return None


_transport: Transport = NullTransport()
_lock = threading.Lock()
stats = {"emitted": 0, "failures": 0, "last_error": None}


def init(url: str | None = None) -> bool:
    """初始化传输层。失败时降级为 NullTransport，不影响服务启动。"""
    global _transport
    url = url or os.getenv("PDM_REDIS_URL")
    if not url:
        return False
    try:
        _transport = RedisStreamTransport(url)
        return True
    except Exception as exc:  # noqa: BLE001
        stats["last_error"] = f"init failed: {exc}"
        return False


def enabled() -> bool:
    return not isinstance(_transport, NullTransport)


def emit(
    event_type: str,
    tenant_id: str,
    subject: str,
    data: dict[str, Any],
    causation_id: str | None = None,
    trace_id: str | None = None,
) -> str | None:
    """发布一条领域事件。返回 stream id（失败返回 None）。"""
    if not enabled():
        return None
    event = {
        "specversion": "1.0",
        "id": uuid.uuid4().hex,
        "type": event_type,
        "source": "/pdm/python-diagnosis",
        "subject": subject,
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tenant_id": tenant_id,
        "trace_id": trace_id or uuid.uuid4().hex,
        "causation_id": causation_id,
        "datacontenttype": "application/json",
        "data": data,
    }
    try:
        with _lock:
            sid = _transport.publish(tenant_id, event)
            stats["emitted"] += 1
            return sid
    except Exception as exc:  # noqa: BLE001 - 事件发布失败不得影响主流程
        stats["failures"] += 1
        stats["last_error"] = str(exc)[:200]
        return None


def stats_snapshot() -> dict[str, Any]:
    return {"enabled": enabled(), **stats}

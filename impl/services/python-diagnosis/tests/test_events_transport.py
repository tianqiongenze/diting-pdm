"""事件传输层降级可见性测试（宪法 4.2：降级必须显式标记）。

事故背景：0.9.20 纯净构建缺 pulsar-client，events.init 静默降级到 Redis，
/events/stats 只报 transport=redis 且 last_error=null——看起来一切正常，
实际换了总线。本测试锁定"降级必须可见"。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages", "pdm_signal"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app import events  # noqa: E402


def test_degraded_transport_is_visible(monkeypatch):
    """pulsar 初始化失败 → 降级 redis 时必须保留原因并标记 degraded。"""
    class _BadPulsar:
        def __init__(self, *a, **k):
            raise RuntimeError("no pulsar-client")

    class _OkRedis:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(events, "PulsarTransport", _BadPulsar)
    monkeypatch.setattr(events, "RedisStreamTransport", _OkRedis)
    monkeypatch.setenv("PDM_EVENT_TRANSPORT", "pulsar")
    ok = events.init("redis://fake:6379/0")
    assert ok is True
    snap = events.stats_snapshot()
    assert snap["transport"] == "redis"
    assert snap["transport_degraded"] is True
    assert "no pulsar-client" in (snap["last_error"] or "")


def test_no_degradation_when_pulsar_ok(monkeypatch):
    class _OkPulsar:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(events, "PulsarTransport", _OkPulsar)
    monkeypatch.setenv("PDM_EVENT_TRANSPORT", "pulsar")
    assert events.init(None) is True
    snap = events.stats_snapshot()
    assert snap["transport"] == "pulsar"
    assert snap["transport_degraded"] is False
    assert snap["last_error"] is None

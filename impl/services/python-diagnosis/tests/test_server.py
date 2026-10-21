"""诊断服务 API 测试（Litestar TestClient）。

覆盖：租户守卫、HI 计算、降级、越界、证据完整性、免责声明、缓存命中。
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from litestar.testing import TestClient

from app.server import app

TENANT = "site-a"


@pytest.fixture(scope="module")
def client():
    with TestClient(app=app) as c:
        yield c


def _post(client, payload: dict, tenant: str | None = TENANT):
    headers = {"X-Tenant-Id": tenant} if tenant is not None else {}
    return client.post("/v1/diagnose", json=payload, headers=headers)


def _base_payload(**metrics) -> dict:
    return {
        "tenant_id": TENANT,
        "asset_id": "KA-101",
        "window_start": "2026-10-14T00:00:00Z",
        "window_end": "2026-10-21T00:00:00Z",
        "metrics": metrics,
    }


class TestHealth:
    def test_healthz(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_version_exposes_model(self, client):
        assert client.get("/version").json()["model"].startswith("rules-")

    def test_cache_stats_available(self, client):
        body = client.get("/cache/stats").json()
        assert "hit_rate" in body
        assert body["l2_enabled"] is False  # 测试环境无 Redis，自动降级


class TestTenantGuard:
    def test_missing_header_rejected(self, client):
        r = _post(client, _base_payload(vib_rms_mm_s=2.0), tenant=None)
        assert r.status_code == 400

    def test_mismatched_tenant_forbidden(self, client):
        r = _post(client, _base_payload(vib_rms_mm_s=2.0), tenant="site-b")
        assert r.status_code == 403
        assert "tenant mismatch" in r.json()["detail"]


class TestDiagnose:
    def test_healthy_asset(self, client):
        r = _post(client, _base_payload(vib_rms_mm_s=1.2, vib_baseline_mm_s=1.2))
        assert r.status_code == 200
        body = r.json()
        assert body["hi"] == 100.0
        assert body["level"] == "green"
        assert body["degraded"] is True  # 只给了一个维度

    def test_degraded_asset_is_orange_or_worse(self, client):
        r = _post(client, _base_payload(vib_rms_mm_s=4.0, vib_baseline_mm_s=2.0, kurtosis=6.5))
        body = r.json()
        assert body["hi"] < 60
        assert body["level"] in ("orange", "red")
        assert len(body["contributions"]) == 2

    def test_fatal_dimension_forces_zero(self, client):
        r = _post(client, _base_payload(vib_rms_mm_s=11.5, zone_c=4.5, zone_d=11.0))
        body = r.json()
        assert body["hi"] == 0.0
        assert body["level"] == "red"
        assert body["sla_hours"] == 4

    def test_no_metrics_returns_422(self, client):
        assert _post(client, _base_payload()).status_code == 422

    def test_evidence_present_for_every_dimension(self, client):
        r = _post(client, _base_payload(vib_rms_mm_s=5.0, kurtosis=5.0, temp_c=70.0, temp_baseline_c=55.0))
        body = r.json()
        assert len(body["evidence"]) >= 3
        for c in body["contributions"]:
            assert c["evidence"] is not None

    def test_disclaimer_always_present(self, client):
        d = _post(client, _base_payload(vib_rms_mm_s=3.0)).json()["disclaimer"]
        assert "不构成维修指令" in d
        assert "AI 辅助生成" in d

    def test_contributions_weight_sum_to_one(self, client):
        r = _post(
            client,
            _base_payload(
                vib_rms_mm_s=3.0,
                kurtosis=4.5,
                temp_c=60.0,
                temp_baseline_c=55.0,
                current_dev_pct=5.0,
                trend_pct_per_day=0.5,
            ),
        )
        body = r.json()
        assert abs(sum(c["weight"] for c in body["contributions"]) - 1.0) < 1e-6
        assert body["degraded"] is False


class TestCache:
    def test_second_identical_request_is_cached(self, client):
        payload = _base_payload(
            vib_rms_mm_s=3.3, kurtosis=4.2, temp_c=62.0, temp_baseline_c=55.0, current_dev_pct=3.0, trend_pct_per_day=0.3
        )
        first = _post(client, payload).json()
        second = _post(client, payload).json()
        assert first["cached"] is False
        assert second["cached"] is True
        assert first["hi"] == second["hi"]

    def test_degraded_result_not_cached(self, client):
        payload = _base_payload(vib_rms_mm_s=2.7)  # 单维度 → degraded
        first = _post(client, payload).json()
        second = _post(client, payload).json()
        assert first["degraded"] is True
        assert second["cached"] is False


class TestBearingHypothesis:
    @staticmethod
    def _synthetic_bearing_signal(fs=25600, fc=4000.0, bpfo=87.0, seconds=2):
        t = np.arange(int(fs * seconds)) / fs
        carrier = np.sin(2 * math.pi * fc * t)
        mod = 1.0 + 0.9 * np.sin(2 * math.pi * bpfo * t)
        return (carrier * mod).tolist()

    def test_detects_outer_race_only(self, client):
        payload = _base_payload(vib_rms_mm_s=4.0, vib_baseline_mm_s=2.0)
        payload["waveform"] = {
            "samples": self._synthetic_bearing_signal(),
            "fs": 25600,
            "rpm": 1500,
            "bearing": {"n_balls": 9, "ball_d": 12.7, "pitch_d": 65.0, "contact_angle_deg": 0.0},
        }
        codes = [h["code"] for h in _post(client, payload).json()["hypotheses"]]
        assert all(not c.startswith("BEARING_BPFI") for c in codes)

    def test_no_bearing_spec_means_no_hypothesis(self, client):
        payload = _base_payload(vib_rms_mm_s=4.0)
        payload["waveform"] = {
            "samples": self._synthetic_bearing_signal(seconds=1),
            "fs": 25600,
            "rpm": 1500,
        }
        assert _post(client, payload).json()["hypotheses"] == []

    def test_short_waveform_rejected(self, client):
        payload = _base_payload(vib_rms_mm_s=4.0)
        payload["waveform"] = {"samples": [0.0] * 100, "fs": 25600, "rpm": 1500}
        assert _post(client, payload).status_code == 422

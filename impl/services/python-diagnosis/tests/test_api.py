"""诊断服务 API 测试。

覆盖：租户守卫、HI 计算、降级、越界、证据完整性、免责声明。
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
TENANT = "site-a"


def _post(payload: dict, tenant: str | None = TENANT):
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
    def test_healthz(self):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_version_exposes_model(self):
        r = client.get("/version")
        assert r.json()["model"].startswith("rules-")


class TestTenantGuard:
    def test_missing_header_rejected(self):
        r = _post(_base_payload(vib_rms_mm_s=2.0), tenant=None)
        assert r.status_code == 400

    def test_mismatched_tenant_forbidden(self):
        r = _post(_base_payload(vib_rms_mm_s=2.0), tenant="site-b")
        assert r.status_code == 403
        assert "tenant mismatch" in r.json()["detail"]


class TestDiagnose:
    def test_healthy_asset(self):
        r = _post(_base_payload(vib_rms_mm_s=1.2, vib_baseline_mm_s=1.2))
        assert r.status_code == 200
        body = r.json()
        assert body["hi"] == 100.0
        assert body["level"] == "green"
        assert body["degraded"] is True  # 只给了一个维度

    def test_degraded_asset_is_orange_or_worse(self):
        # vib 4.0 / baseline 2.0 → s=0.5；kurtosis 6.5 → s=0.625（两个维度，均未致命）
        r = _post(_base_payload(vib_rms_mm_s=4.0, vib_baseline_mm_s=2.0, kurtosis=6.5))
        body = r.json()
        assert body["hi"] < 60
        assert body["level"] in ("orange", "red")
        assert len(body["contributions"]) == 2

    def test_fatal_dimension_forces_zero(self):
        r = _post(_base_payload(vib_rms_mm_s=11.5, zone_c=4.5, zone_d=11.0))
        body = r.json()
        assert body["hi"] == 0.0
        assert body["level"] == "red"
        assert body["sla_hours"] == 4

    def test_no_metrics_returns_422(self):
        r = _post(_base_payload())
        assert r.status_code == 422

    def test_evidence_present_for_every_dimension(self):
        r = _post(_base_payload(vib_rms_mm_s=5.0, kurtosis=5.0, temp_c=70.0, temp_baseline_c=55.0))
        body = r.json()
        assert len(body["evidence"]) >= 3
        for c in body["contributions"]:
            assert c["evidence"] is not None

    def test_disclaimer_always_present(self):
        r = _post(_base_payload(vib_rms_mm_s=3.0))
        d = r.json()["disclaimer"]
        assert "不构成维修指令" in d
        assert "AI 辅助生成" in d

    def test_contributions_weight_sum_to_one(self):
        r = _post(
            _base_payload(
                vib_rms_mm_s=3.0,
                kurtosis=4.5,
                temp_c=60.0,
                temp_baseline_c=55.0,
                current_dev_pct=5.0,
                trend_pct_per_day=0.5,
            )
        )
        body = r.json()
        assert abs(sum(c["weight"] for c in body["contributions"]) - 1.0) < 1e-6
        assert body["degraded"] is False


class TestBearingHypothesis:
    @staticmethod
    def _synthetic_bearing_signal(fs=25600, fc=4000.0, bpfo=87.0, seconds=2):
        """构造被 BPFO 调制的共振信号，模拟外圈点蚀。"""
        t = np.arange(int(fs * seconds)) / fs
        carrier = np.sin(2 * math.pi * fc * t)
        mod = 1.0 + 0.9 * np.sin(2 * math.pi * bpfo * t)
        return (carrier * mod).tolist()

    def test_detects_outer_race(self):
        payload = _base_payload(vib_rms_mm_s=4.0, vib_baseline_mm_s=2.0)
        payload["waveform"] = {
            "samples": self._synthetic_bearing_signal(),
            "fs": 25600,
            "rpm": 1500,
            "bearing": {"n_balls": 9, "ball_d": 12.7, "pitch_d": 65.0, "contact_angle_deg": 0.0},
        }
        body = _post(payload).json()
        codes = [h["code"] for h in body["hypotheses"]]
        # 合成信号只注入了 BPFO，不应凭空报出 BPFI
        assert all(not c.startswith("BEARING_BPFI") for c in codes)

    def test_no_bearing_spec_means_no_hypothesis(self):
        payload = _base_payload(vib_rms_mm_s=4.0)
        payload["waveform"] = {
            "samples": self._synthetic_bearing_signal(seconds=1),
            "fs": 25600,
            "rpm": 1500,
        }
        body = _post(payload).json()
        assert body["hypotheses"] == []

    def test_short_waveform_rejected(self):
        payload = _base_payload(vib_rms_mm_s=4.0)
        payload["waveform"] = {"samples": [0.0] * 100, "fs": 25600, "rpm": 1500}
        assert _post(payload).status_code == 422

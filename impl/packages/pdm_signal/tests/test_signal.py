"""pdm_signal 单元测试。

TDD 纪律：这些用例先于实现约定（已在实现中体现），任何修改必须先改这里。
数学性质断言（union bound、单调性、致命短路）是本模块的"契约"，不可删。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from pdm_signal import (
    BearingGeometry,
    Dimension,
    characteristic_frequencies,
    compute_hi,
    crest_factor,
    detrend,
    envelope_spectrum,
    fft_spectrum,
    kurtosis,
    level_of,
    match_harmonics,
    robust_baseline,
    robust_z,
    rms,
    theil_sen_slope,
)
from pdm_signal.health import degradation_from_baseline, degradation_from_threshold


# --------------------------------------------------------------------------
# 信号特征
# --------------------------------------------------------------------------
class TestSignalFeatures:
    def test_rms_of_sine_is_amplitude_over_sqrt2(self):
        fs, f, amp = 25600, 100.0, 2.0
        t = np.arange(fs) / fs
        x = amp * np.sin(2 * math.pi * f * t)
        assert rms(x) == pytest.approx(amp / math.sqrt(2), rel=1e-3)

    def test_kurtosis_of_gaussian_is_about_three(self):
        rng = np.random.default_rng(42)
        x = rng.normal(size=50000)
        assert kurtosis(x) == pytest.approx(3.0, abs=0.1)

    def test_kurtosis_rises_with_impulses(self):
        base = np.zeros(10000)
        impulsive = base.copy()
        impulsive[::1000] = 20.0  # 稀疏冲击
        assert kurtosis(impulsive) > kurtosis(base + 0.01)

    def test_crest_factor_of_sine_is_sqrt2(self):
        t = np.arange(10000) / 25600
        x = np.sin(2 * math.pi * 50 * t)
        assert crest_factor(x) == pytest.approx(math.sqrt(2), rel=1e-2)

    def test_detrend_removes_linear_trend(self):
        x = np.linspace(0, 10, 1000) + 5.0
        assert abs(np.mean(detrend(x))) < 1e-9

    def test_empty_signal_raises(self):
        with pytest.raises(ValueError):
            rms([])

    def test_nan_signal_raises(self):
        with pytest.raises(ValueError):
            rms([1.0, float("nan"), 2.0])


class TestSpectrum:
    def test_fft_finds_single_tone(self):
        fs, f0 = 25600, 120.0
        t = np.arange(fs) / fs
        x = np.sin(2 * math.pi * f0 * t)
        freqs, amps = fft_spectrum(x, fs)
        peak_idx = int(np.argmax(amps))
        assert freqs[peak_idx] == pytest.approx(f0, abs=1.0)
        assert amps[peak_idx] == pytest.approx(1.0, rel=0.05)

    def test_envelope_detects_modulation(self):
        """轴承故障 = 高频共振被低频冲击调制。包络谱应出现调制频率。"""
        fs, fc, fm = 25600, 4000.0, 87.0
        t = np.arange(fs * 2) / fs
        carrier = np.sin(2 * math.pi * fc * t)
        mod = 1.0 + 0.8 * np.sin(2 * math.pi * fm * t)
        x = carrier * mod
        freqs, amps = envelope_spectrum(x, fs, band=(2000.0, 8000.0))
        mask = (freqs > 70) & (freqs < 105)
        assert freqs[mask][int(np.argmax(amps[mask]))] == pytest.approx(fm, abs=3.0)

    def test_envelope_rejects_bad_band(self):
        x = np.random.default_rng(0).normal(size=1024)
        with pytest.raises(ValueError):
            envelope_spectrum(x, 1000.0, band=(600.0, 400.0))


class TestRobustStats:
    def test_robust_baseline_ignores_outliers(self):
        x = np.full(1000, 2.0)
        x[0] = 1000.0  # 一个离群点
        med, sigma = robust_baseline(x)
        assert med == pytest.approx(2.0)
        assert sigma < 1e-6

    def test_robust_z_handles_zero_sigma(self):
        assert robust_z(5.0, 2.0, 0.0) == 0.0

    def test_theil_sen_slope_is_robust(self):
        y = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        y[2] = 100.0  # 一个尖刺
        assert theil_sen_slope(y) == pytest.approx(1.0, abs=0.2)


# --------------------------------------------------------------------------
# 轴承特征频率
# --------------------------------------------------------------------------
class TestBearing:
    GEOM = BearingGeometry(n_balls=8, ball_d=10.0, pitch_d=50.0, contact_angle_deg=0.0)

    def test_frequencies_ordered(self):
        cf = characteristic_frequencies(self.GEOM, fr=25.0)
        assert 0 < cf["FTF"] < cf["BSF"] < cf["FR"] * 10
        assert cf["BPFO"] < cf["BPFI"]
        assert cf["BPFO"] + cf["BPFI"] == pytest.approx(8 * 25.0)  # n × fr

    def test_invalid_geometry_raises(self):
        with pytest.raises(ValueError):
            BearingGeometry(n_balls=0, ball_d=10, pitch_d=50)
        with pytest.raises(ValueError):
            BearingGeometry(n_balls=8, ball_d=60, pitch_d=50)

    def test_match_requires_multiple_harmonics(self):
        """单峰不算故障：这是防误报的关键规则。"""
        freqs = np.linspace(0, 500, 5001)
        amps = np.full_like(freqs, 0.01)
        amps[np.argmin(np.abs(freqs - 87.0))] = 1.0  # 只有 1 阶
        m = match_harmonics(freqs, amps, 87.0)
        assert m.harmonics_found == 1
        assert m.is_suspicious is False

    def test_match_with_harmonics_is_suspicious(self):
        freqs = np.linspace(0, 500, 50001)
        amps = np.full_like(freqs, 0.01)
        for k in (1, 2, 3):
            amps[np.argmin(np.abs(freqs - 87.0 * k))] = 1.0 / k
        m = match_harmonics(freqs, amps, 87.0)
        assert m.harmonics_found == 3
        assert m.is_suspicious is True
        assert m.confidence > 0.6


# --------------------------------------------------------------------------
# 健康指数（核心契约）
# --------------------------------------------------------------------------
class TestHealthIndex:
    def test_all_healthy_is_100_green(self):
        dims = [Dimension(n, 0.0) for n in ("vib", "imp", "temp", "curr", "trend")]
        hi = compute_hi(dims)
        assert hi.hi == 100.0
        assert hi.level == "green"
        assert hi.degraded is False

    def test_single_fatal_forces_zero(self):
        dims = [
            Dimension("vib", 1.0),
            Dimension("imp", 0.0),
            Dimension("temp", 0.0),
            Dimension("curr", 0.0),
            Dimension("trend", 0.0),
        ]
        hi = compute_hi(dims)
        assert hi.hi == 0.0
        assert hi.level == "red"
        assert hi.sla_hours == 4

    def test_union_bound_holds(self):
        """HI ≥ 100 − Σ impact（union bound）。乘积对非致命多维劣化更宽容，避免重复扣分。"""
        dims = [Dimension(n, 0.6) for n in ("vib", "imp", "temp", "curr", "trend")]
        hi = compute_hi(dims)
        assert hi.hi >= 100.0 - hi.linear_loss - 1e-6
        assert hi.overlap_gap >= -1e-6

    def test_monotonic_in_degradation(self):
        prev = 101.0
        for s in (0.0, 0.2, 0.4, 0.6, 0.8, 0.99):
            dims = [Dimension("vib", s), Dimension("imp", 0.0)]
            cur = compute_hi(dims).hi
            assert cur <= prev
            prev = cur

    def test_missing_dimension_renormalizes_and_degrades(self):
        dims = [Dimension("vib", 0.5), Dimension("imp", 0.2)]
        hi = compute_hi(dims)
        assert hi.degraded is True
        assert hi.degraded_reason is not None
        assert sum(c.weight for c in hi.contributions) == pytest.approx(1.0, abs=1e-6)

    def test_explicit_weights_normalized(self):
        dims = [Dimension("a", 0.5, weight=3.0), Dimension("b", 0.5, weight=1.0)]
        hi = compute_hi(dims, weights={"a": 1.0, "b": 1.0})
        w = {c.name: c.weight for c in hi.contributions}
        assert w["a"] == pytest.approx(0.75)
        assert w["b"] == pytest.approx(0.25)

    def test_level_boundaries(self):
        assert level_of(80.0) == "green"
        assert level_of(79.99) == "yellow"
        assert level_of(60.0) == "yellow"
        assert level_of(59.99) == "orange"
        assert level_of(40.0) == "orange"
        assert level_of(39.99) == "red"

    def test_out_of_range_level_raises(self):
        with pytest.raises(ValueError):
            level_of(101.0)

    def test_empty_dimensions_raises(self):
        with pytest.raises(ValueError):
            compute_hi([])

    def test_invalid_s_raises(self):
        with pytest.raises(ValueError):
            Dimension("vib", 1.5)


class TestDegradationMapping:
    def test_baseline_mapping(self):
        assert degradation_from_baseline(2.0, 2.0) == pytest.approx(0.0)
        assert degradation_from_baseline(4.0, 2.0) == pytest.approx(0.5)
        assert degradation_from_baseline(6.0, 2.0) == pytest.approx(1.0)
        assert degradation_from_baseline(100.0, 2.0) == pytest.approx(1.0)

    def test_threshold_mapping(self):
        assert degradation_from_threshold(3.5, 4.5, 11.0) == 0.0
        assert degradation_from_threshold(11.0, 4.5, 11.0) == 1.0
        assert 0 < degradation_from_threshold(7.0, 4.5, 11.0) < 1

    def test_bad_threshold_raises(self):
        with pytest.raises(ValueError):
            degradation_from_threshold(5.0, 10.0, 10.0)

"""诊断领域逻辑（框架无关）。

从 Web 框架中剥离出来，目的有三个：
1. 可独立测试（不需要起 HTTP 服务）
2. 未来可被 Rust 实现替换，或用 PyO3 包装复用
3. 让"业务规则"与"传输协议"解耦（DDD 的应用服务层）
"""

from __future__ import annotations

from typing import Any

import numpy as np

from pdm_signal import (
    BearingGeometry,
    Dimension,
    compute_hi,
    diagnose_bearing,
    envelope_spectrum,
)
from pdm_signal.health import degradation_from_baseline, degradation_from_threshold

from .schemas import BearingSpec, ContributionOut, DiagnoseRequest, HypothesisOut, Metrics, Waveform

MODEL_VERSION = "rules-v0.2.0"

DISCLAIMER = (
    "本结论由 AI 辅助生成，基于所给时间窗内的数据与 " + MODEL_VERSION + "，"
    "不构成维修指令。任何停机、拆检、更换或采购决策须由持证人员现场确认。"
)

HYPOTHESIS_LABELS = {
    "BPFO": "轴承外圈故障（疑似）",
    "BPFI": "轴承内圈故障（疑似）",
    "BSF": "滚动体故障（疑似）",
    "FTF": "保持架故障（疑似）",
}


class NoUsableMetrics(ValueError):
    """没有任何可用指标，无法给出结论（对应 HTTP 422）。"""


def build_dimensions(m: Metrics, wave: Waveform | None) -> tuple[list[Dimension], list[dict[str, Any]]]:
    """把物理指标映射为 5 个劣化度维度。缺失维度不加入列表（触发降级）。"""
    dims: list[Dimension] = []
    evidence: list[dict[str, Any]] = []

    if m.vib_rms_mm_s is not None:
        if m.vib_baseline_mm_s:
            s = degradation_from_baseline(m.vib_rms_mm_s, m.vib_baseline_mm_s, span_ratio=3.0)
            basis = f"相对基线 {m.vib_baseline_mm_s} mm/s"
        else:
            s = degradation_from_threshold(m.vib_rms_mm_s, m.zone_c, m.zone_d)
            basis = f"ISO 20816 分区 C={m.zone_c} / D={m.zone_d}"
        dims.append(Dimension("vib", s, evidence=f"振动RMS {m.vib_rms_mm_s} mm/s（{basis}）"))
        evidence.append({"type": "metric", "name": "vib_rms_mm_s", "value": m.vib_rms_mm_s, "s": round(s, 4), "basis": basis})

    k = m.kurtosis
    if k is None and wave is not None:
        from pdm_signal import kurtosis as kurt

        k = kurt(np.asarray(wave.samples, dtype=float))
    if k is not None:
        s = max(0.0, min(1.0, (k - 4.0) / 4.0))
        dims.append(Dimension("imp", s, evidence=f"峭度 {round(k, 2)}（>4 可疑）"))
        evidence.append({"type": "metric", "name": "kurtosis", "value": round(k, 3), "s": round(s, 4)})

    if m.temp_c is not None and m.temp_baseline_c is not None:
        rise = m.temp_c - m.temp_baseline_c
        s = max(0.0, min(1.0, rise / 15.0))
        dims.append(Dimension("temp", s, evidence=f"温升 {round(rise, 1)}℃（15℃ 视为致命）"))
        evidence.append({"type": "metric", "name": "temp_rise_c", "value": round(rise, 2), "s": round(s, 4)})

    if m.current_dev_pct is not None:
        s = max(0.0, min(1.0, abs(m.current_dev_pct) / 25.0))
        dims.append(Dimension("curr", s, evidence=f"电流偏差 {m.current_dev_pct}%（25% 视为致命）"))
        evidence.append({"type": "metric", "name": "current_dev_pct", "value": m.current_dev_pct, "s": round(s, 4)})

    if m.trend_pct_per_day is not None:
        s = max(0.0, min(1.0, abs(m.trend_pct_per_day) / 5.0))
        dims.append(Dimension("trend", s, evidence=f"劣化速率 {m.trend_pct_per_day}%/天（5%/天 视为致命）"))
        evidence.append({"type": "metric", "name": "trend_pct_per_day", "value": m.trend_pct_per_day, "s": round(s, 4)})

    return dims, evidence


def bearing_hypotheses(wave: Waveform) -> tuple[list[HypothesisOut], list[dict[str, Any]]]:
    """包络谱轴承诊断。判据：>=2 阶谐波且 SNR>=3 才输出（防单峰误报）。"""
    if wave.bearing is None:
        return [], []

    x = np.asarray(wave.samples, dtype=float)
    nyq = wave.fs / 2.0
    band = (max(1000.0, nyq * 0.1), nyq * 0.9)
    freqs, amps = envelope_spectrum(x, wave.fs, band=band)

    geom = _to_geometry(wave.bearing)
    matches = diagnose_bearing(freqs, amps, geom, rpm=wave.rpm)

    hyps: list[HypothesisOut] = []
    ev: list[dict[str, Any]] = []
    for mt in matches:
        if not mt.is_suspicious:
            continue
        hyps.append(
            HypothesisOut(
                code=f"BEARING_{mt.name}",
                label=HYPOTHESIS_LABELS[mt.name],
                confidence=mt.confidence,
                basis=f"{mt.name} 特征频率 {round(mt.target, 1)} Hz，命中 {mt.harmonics_found} 阶谐波，SNR {round(mt.snr, 1)}",
            )
        )
        ev.append(
            {
                "type": "spectrum",
                "name": f"envelope_{mt.name}",
                "target_hz": round(mt.target, 2),
                "orders": list(mt.orders),
                "snr": round(mt.snr, 2),
            }
        )
    return hyps, ev


def _to_geometry(spec: BearingSpec) -> BearingGeometry:
    return BearingGeometry(
        n_balls=spec.n_balls,
        ball_d=spec.ball_d,
        pitch_d=spec.pitch_d,
        contact_angle_deg=spec.contact_angle_deg,
    )


def diagnose(req: DiagnoseRequest) -> dict[str, Any]:
    """核心用例：返回可直接序列化的 dict（便于缓存，避免二次序列化）。"""
    dims, evidence = build_dimensions(req.metrics, req.waveform)
    if not dims:
        raise NoUsableMetrics("no usable metrics")

    hi = compute_hi(dims)

    hypotheses: list[HypothesisOut] = []
    if req.waveform is not None:
        hyps, wave_ev = bearing_hypotheses(req.waveform)
        hypotheses.extend(hyps)
        evidence.extend(wave_ev)

    return {
        "hi": hi.hi,
        "level": hi.level,
        "sla_hours": hi.sla_hours,
        "degraded": hi.degraded,
        "degraded_reason": hi.degraded_reason,
        "contributions": [
            {"name": c.name, "s": c.s, "weight": c.weight, "impact": c.impact, "evidence": c.evidence}
            for c in hi.contributions
        ],
        "hypotheses": [
            {"code": h.code, "label": h.label, "confidence": h.confidence, "basis": h.basis} for h in hypotheses
        ],
        "evidence": evidence,
        "model_version": MODEL_VERSION,
    }


def cache_key_payload(req: DiagnoseRequest) -> dict[str, Any]:
    """用于生成缓存指纹：只取影响结果的字段，剔除 trace/时间戳等无关项。"""
    return {
        "tenant": req.tenant_id,
        "asset": req.asset_id,
        "metrics": {
            "vib": req.metrics.vib_rms_mm_s,
            "base": req.metrics.vib_baseline_mm_s,
            "zc": req.metrics.zone_c,
            "zd": req.metrics.zone_d,
            "k": req.metrics.kurtosis,
            "t": req.metrics.temp_c,
            "tb": req.metrics.temp_baseline_c,
            "c": req.metrics.current_dev_pct,
            "tr": req.metrics.trend_pct_per_day,
        },
        "wave": None
        if req.waveform is None
        else {
            "fs": req.waveform.fs,
            "rpm": req.waveform.rpm,
            "n": len(req.waveform.samples),
            "bearing": None
            if req.waveform.bearing is None
            else {
                "n": req.waveform.bearing.n_balls,
                "d": req.waveform.bearing.ball_d,
                "D": req.waveform.bearing.pitch_d,
                "a": req.waveform.bearing.contact_angle_deg,
            },
        },
        "model": MODEL_VERSION,
    }

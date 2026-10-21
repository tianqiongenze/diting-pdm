"""诊断服务（SD / HA）—— 垂直切片的最小可运行实现。

职责边界：
- 输入：一个设备的指标（可选波形）
- 输出：健康指数 HI + 分解 + 故障假设 + 证据
- 不做：决策、工单、采购（属 decision / ops 上下文）

多租户（D23 = 全用 TiDB，无 RLS）：
- 所有请求必须带 X-Tenant-Id，且与 body.tenant_id 一致
- 服务内所有数据访问必须经 TenantQueryBuilder（见 db/tidb/TENANT_ISOLATION.md）
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from pdm_signal import (
    BearingGeometry,
    Dimension,
    compute_hi,
    diagnose_bearing,
    envelope_spectrum,
    kurtosis as kurt,
    rms,
)
from pdm_signal.health import degradation_from_baseline, degradation_from_threshold

SERVICE_NAME = "python-diagnosis"
SERVICE_VERSION = "0.1.0"
MODEL_VERSION = "rules-v0.1.0"

app = FastAPI(
    title="Diting PdM · Diagnosis Service",
    version=SERVICE_VERSION,
    description="设备健康评估与故障假设（ISO 13374 SD/HA）",
)


# --------------------------------------------------------------------------
# 契约
# --------------------------------------------------------------------------
class BearingSpec(BaseModel):
    n_balls: int = Field(gt=0)
    ball_d: float = Field(gt=0)
    pitch_d: float = Field(gt=0)
    contact_angle_deg: float = 0.0


class Waveform(BaseModel):
    samples: list[float] = Field(min_length=256, max_length=1_000_000)
    fs: float = Field(gt=0)
    rpm: float = Field(gt=0)
    bearing: BearingSpec | None = None


class Metrics(BaseModel):
    vib_rms_mm_s: float | None = Field(default=None, ge=0)
    vib_baseline_mm_s: float | None = Field(default=None, gt=0)
    zone_c: float = Field(default=4.5, gt=0)
    zone_d: float = Field(default=11.0, gt=0)
    kurtosis: float | None = Field(default=None, ge=0)
    temp_c: float | None = None
    temp_baseline_c: float | None = None
    current_dev_pct: float | None = None
    trend_pct_per_day: float | None = None


class DiagnoseRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=32)
    asset_id: str = Field(min_length=1, max_length=64)
    window_start: str | None = None
    window_end: str | None = None
    metrics: Metrics = Field(default_factory=Metrics)
    waveform: Waveform | None = None


class ContributionOut(BaseModel):
    name: str
    s: float
    weight: float
    impact: float
    evidence: str | None = None


class Hypothesis(BaseModel):
    code: str
    label: str
    confidence: float
    basis: str


class DiagnoseResponse(BaseModel):
    trace_id: str
    tenant_id: str
    asset_id: str
    hi: float
    level: str
    sla_hours: int
    degraded: bool
    degraded_reason: str | None
    contributions: list[ContributionOut]
    hypotheses: list[Hypothesis]
    evidence: list[dict[str, Any]]
    model_version: str
    elapsed_ms: int
    disclaimer: str


DISCLAIMER = (
    "本结论由 AI 辅助生成，基于所给时间窗内的数据与 " + MODEL_VERSION + "，"
    "不构成维修指令。任何停机、拆检、更换或采购决策须由持证人员现场确认。"
)


# --------------------------------------------------------------------------
# 多租户守卫
# --------------------------------------------------------------------------
async def tenant_ctx(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> str:
    if not x_tenant_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="missing X-Tenant-Id header")
    return x_tenant_id


# --------------------------------------------------------------------------
# 诊断逻辑
# --------------------------------------------------------------------------
def _build_dimensions(m: Metrics, wave: Waveform | None) -> tuple[list[Dimension], list[dict[str, Any]]]:
    """把物理指标映射为 5 个劣化度维度。缺失的维度不加入列表（触发降级）。"""
    dims: list[Dimension] = []
    evidence: list[dict[str, Any]] = []

    # 1) 振动烈度：优先用相对基线，否则用绝对分区
    if m.vib_rms_mm_s is not None:
        if m.vib_baseline_mm_s:
            s = degradation_from_baseline(m.vib_rms_mm_s, m.vib_baseline_mm_s, span_ratio=3.0)
            basis = f"相对基线 {m.vib_baseline_mm_s} mm/s"
        else:
            s = degradation_from_threshold(m.vib_rms_mm_s, m.zone_c, m.zone_d)
            basis = f"ISO 20816 分区 C={m.zone_c} / D={m.zone_d}"
        dims.append(Dimension("vib", s, evidence=f"振动RMS {m.vib_rms_mm_s} mm/s（{basis}）"))
        evidence.append(
            {"type": "metric", "name": "vib_rms_mm_s", "value": m.vib_rms_mm_s, "s": round(s, 4), "basis": basis}
        )

    # 2) 冲击（峭度）：>4 开始可疑，>8 严重
    k = m.kurtosis
    if k is None and wave is not None:
        k = kurt(np.asarray(wave.samples, dtype=float))
    if k is not None:
        s = max(0.0, min(1.0, (k - 4.0) / 4.0))
        dims.append(Dimension("imp", s, evidence=f"峭度 {round(k, 2)}（>4 可疑）"))
        evidence.append({"type": "metric", "name": "kurtosis", "value": round(k, 3), "s": round(s, 4)})

    # 3) 温度：相对环境/基线升高 15℃ 视为致命
    if m.temp_c is not None and m.temp_baseline_c is not None:
        rise = m.temp_c - m.temp_baseline_c
        s = max(0.0, min(1.0, rise / 15.0))
        dims.append(Dimension("temp", s, evidence=f"温升 {round(rise, 1)}℃（15℃ 视为致命）"))
        evidence.append({"type": "metric", "name": "temp_rise_c", "value": round(rise, 2), "s": round(s, 4)})

    # 4) 电流偏差
    if m.current_dev_pct is not None:
        s = max(0.0, min(1.0, abs(m.current_dev_pct) / 25.0))
        dims.append(Dimension("curr", s, evidence=f"电流偏差 {m.current_dev_pct}%（25% 视为致命）"))
        evidence.append({"type": "metric", "name": "current_dev_pct", "value": m.current_dev_pct, "s": round(s, 4)})

    # 5) 趋势：5%/天 视为致命
    if m.trend_pct_per_day is not None:
        s = max(0.0, min(1.0, abs(m.trend_pct_per_day) / 5.0))
        dims.append(Dimension("trend", s, evidence=f"劣化速率 {m.trend_pct_per_day}%/天（5%/天 视为致命）"))
        evidence.append({"type": "metric", "name": "trend_pct_per_day", "value": m.trend_pct_per_day, "s": round(s, 4)})

    return dims, evidence


def _bearing_hypotheses(wave: Waveform) -> tuple[list[Hypothesis], list[dict[str, Any]]]:
    """有波形且给出轴承几何时，做包络谱轴承诊断。

    判据：>=2 阶谐波且 SNR>=3 才输出（防单峰误报）。
    """
    if wave.bearing is None:
        return [], []

    x = np.asarray(wave.samples, dtype=float)
    nyq = wave.fs / 2.0
    band = (max(1000.0, nyq * 0.1), nyq * 0.9)
    freqs, amps = envelope_spectrum(x, wave.fs, band=band)

    geom = BearingGeometry(
        n_balls=wave.bearing.n_balls,
        ball_d=wave.bearing.ball_d,
        pitch_d=wave.bearing.pitch_d,
        contact_angle_deg=wave.bearing.contact_angle_deg,
    )
    matches = diagnose_bearing(freqs, amps, geom, rpm=wave.rpm)

    labels = {
        "BPFO": "轴承外圈故障（疑似）",
        "BPFI": "轴承内圈故障（疑似）",
        "BSF": "滚动体故障（疑似）",
        "FTF": "保持架故障（疑似）",
    }
    hyps: list[Hypothesis] = []
    ev: list[dict[str, Any]] = []
    for m in matches:
        if not m.is_suspicious:
            continue
        hyps.append(
            Hypothesis(
                code=f"BEARING_{m.name}",
                label=labels[m.name],
                confidence=m.confidence,
                basis=f"{m.name} 特征频率 {round(m.target, 1)} Hz，命中 {m.harmonics_found} 阶谐波，SNR {round(m.snr, 1)}",
            )
        )
        ev.append(
            {
                "type": "spectrum",
                "name": f"envelope_{m.name}",
                "target_hz": round(m.target, 2),
                "orders": list(m.orders),
                "snr": round(m.snr, 2),
            }
        )
    return hyps, ev


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME, "version": SERVICE_VERSION}


@app.get("/version")
def version() -> dict[str, str]:
    return {"service": SERVICE_NAME, "version": SERVICE_VERSION, "model": MODEL_VERSION}


@app.post("/v1/diagnose", response_model=DiagnoseResponse)
def diagnose(req: DiagnoseRequest, tenant: str = Depends(tenant_ctx)) -> DiagnoseResponse:
    started = time.perf_counter()

    # 租户一致性：header 与 body 必须相同，否则视为越权尝试
    if req.tenant_id != tenant:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="tenant mismatch")

    dims, evidence = _build_dimensions(req.metrics, req.waveform)
    if not dims:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="no usable metrics; at least one of vib_rms/kurtosis/temp/current/trend is required",
        )

    hi = compute_hi(dims)

    hypotheses: list[Hypothesis] = []
    if req.waveform is not None:
        # 波形 RMS 可作为 vib 维度的兜底
        if req.metrics.vib_rms_mm_s is None:
            wave_rms = rms(np.asarray(req.waveform.samples, dtype=float))
            evidence.append({"type": "metric", "name": "waveform_rms_raw", "value": round(wave_rms, 4)})
        hyps, wave_ev = _bearing_hypotheses(req.waveform)
        hypotheses.extend(hyps)
        evidence.extend(wave_ev)

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    return DiagnoseResponse(
        trace_id=str(uuid.uuid4()),
        tenant_id=tenant,
        asset_id=req.asset_id,
        hi=hi.hi,
        level=hi.level,
        sla_hours=hi.sla_hours,
        degraded=hi.degraded,
        degraded_reason=hi.degraded_reason,
        contributions=[
            ContributionOut(name=c.name, s=c.s, weight=c.weight, impact=c.impact, evidence=c.evidence)
            for c in hi.contributions
        ],
        hypotheses=hypotheses,
        evidence=evidence,
        model_version=MODEL_VERSION,
        elapsed_ms=elapsed_ms,
        disclaimer=DISCLAIMER,
    )

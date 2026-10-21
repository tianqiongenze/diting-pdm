"""请求/响应契约（msgspec.Struct）。

选 msgspec 而非 pydantic 的理由：
- 序列化/反序列化在 Rust 侧完成，基准通常快 2-5 倍，内存更省
- 与 Litestar 原生集成，无需 DTO 包装层
- Struct 是 immutable 语义（frozen=False 但无 __dict__），减少运行时开销
"""

from __future__ import annotations

from typing import Any

import msgspec


class BearingSpec(msgspec.Struct):
    n_balls: int
    ball_d: float
    pitch_d: float
    contact_angle_deg: float = 0.0


class Waveform(msgspec.Struct):
    samples: list[float]
    fs: float
    rpm: float
    bearing: BearingSpec | None = None


class Metrics(msgspec.Struct):
    vib_rms_mm_s: float | None = None
    vib_baseline_mm_s: float | None = None
    zone_c: float = 4.5
    zone_d: float = 11.0
    kurtosis: float | None = None
    temp_c: float | None = None
    temp_baseline_c: float | None = None
    current_dev_pct: float | None = None
    trend_pct_per_day: float | None = None


class DiagnoseRequest(msgspec.Struct):
    tenant_id: str
    asset_id: str
    window_start: str | None = None
    window_end: str | None = None
    metrics: Metrics = msgspec.field(default_factory=Metrics)
    waveform: Waveform | None = None


class ContributionOut(msgspec.Struct):
    name: str
    s: float
    weight: float
    impact: float
    evidence: str | None = None


class HypothesisOut(msgspec.Struct):
    code: str
    label: str
    confidence: float
    basis: str


class DiagnoseResponse(msgspec.Struct):
    trace_id: str
    tenant_id: str
    asset_id: str
    hi: float
    level: str
    sla_hours: int
    degraded: bool
    degraded_reason: str | None
    contributions: list[ContributionOut]
    hypotheses: list[HypothesisOut]
    evidence: list[dict[str, Any]]
    model_version: str
    elapsed_ms: int
    cached: bool
    disclaimer: str

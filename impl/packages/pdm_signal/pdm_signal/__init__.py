"""pdm_signal —— 设备状态监测的信号处理与健康评估核心。

设计约束（来自 docs/04）：
- 纯函数、无 IO、无状态，便于 TDD 与 Rust 平移（后续可整体重写为 rust-dsp-core）
- 特征必须可解释：每个函数对应一个有物理含义的指标
- 数值稳健：使用中位数/MAD 而非均值/标准差，抵抗工业数据的离群点
"""

from .bearing import (
    BearingGeometry,
    HarmonicMatch,
    characteristic_frequencies,
    diagnose_bearing,
    electrical_frequency,
    gear_mesh_frequency,
    match_harmonics,
)
from .health import Dimension, HealthIndex, compute_hi, level_of
from .signal import (
    clearance_factor,
    crest_factor,
    detrend,
    envelope_spectrum,
    fft_spectrum,
    impulse_factor,
    kurtosis,
    peak,
    peak_to_peak,
    robust_baseline,
    robust_z,
    rms,
    skewness,
    theil_sen_slope,
    top_peaks,
)

__all__ = [
    "BearingGeometry",
    "Dimension",
    "HarmonicMatch",
    "HealthIndex",
    "characteristic_frequencies",
    "clearance_factor",
    "compute_hi",
    "crest_factor",
    "detrend",
    "diagnose_bearing",
    "electrical_frequency",
    "envelope_spectrum",
    "fft_spectrum",
    "gear_mesh_frequency",
    "impulse_factor",
    "kurtosis",
    "level_of",
    "match_harmonics",
    "peak",
    "peak_to_peak",
    "robust_baseline",
    "robust_z",
    "rms",
    "skewness",
    "theil_sen_slope",
    "top_peaks",
]

__version__ = "0.1.0"

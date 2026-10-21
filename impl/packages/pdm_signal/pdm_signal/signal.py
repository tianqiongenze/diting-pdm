"""时域 / 频域特征计算。所有函数均为纯函数，输入一维序列，输出标量或数组。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import signal as sps

EPS = 1e-12


def _as_array(x) -> np.ndarray:
    arr = np.asarray(x, dtype=float).ravel()
    if arr.size == 0:
        raise ValueError("empty signal")
    if not np.all(np.isfinite(arr)):
        raise ValueError("signal contains NaN or Inf")
    return arr


def detrend(x) -> np.ndarray:
    """去除线性趋势。加速度信号积分前必须做，否则低频漂移会淹没故障特征。"""
    return sps.detrend(_as_array(x))


def rms(x) -> float:
    """有效值。ISO 20816 判级的主指标（速度 RMS）。"""
    arr = _as_array(x)
    return float(np.sqrt(np.mean(arr**2)))


def peak(x) -> float:
    return float(np.max(np.abs(_as_array(x))))


def peak_to_peak(x) -> float:
    arr = _as_array(x)
    return float(np.max(arr) - np.min(arr))


def kurtosis(x) -> float:
    """峭度（Fisher，正态为 0）。工程惯例常报 Pearson（正态为 3），此处返回 Pearson。"""
    arr = _as_array(x)
    mu = np.mean(arr)
    sd = np.std(arr)
    if sd < EPS:
        return 3.0
    return float(np.mean(((arr - mu) / sd) ** 4))


def skewness(x) -> float:
    arr = _as_array(x)
    mu = np.mean(arr)
    sd = np.std(arr)
    if sd < EPS:
        return 0.0
    return float(np.mean(((arr - mu) / sd) ** 3))


def crest_factor(x) -> float:
    """峰值因子 = 峰值 / RMS。冲击性故障早期敏感。"""
    r = rms(x)
    return float(peak(x) / r) if r > EPS else 0.0


def impulse_factor(x) -> float:
    arr = _as_array(x)
    mean_abs = np.mean(np.abs(arr))
    return float(peak(arr) / mean_abs) if mean_abs > EPS else 0.0


def clearance_factor(x) -> float:
    """裕度因子 = 峰值 / (均值 sqrt(|x|))^2。磨损类故障敏感。"""
    arr = _as_array(x)
    denom = np.mean(np.sqrt(np.abs(arr))) ** 2
    return float(peak(arr) / denom) if denom > EPS else 0.0


def fft_spectrum(x, fs: float, window: str = "hann") -> tuple[np.ndarray, np.ndarray]:
    """单边幅值谱。返回 (freqs, amps)，amps 已按窗函数能量归一。"""
    arr = detrend(x)
    if fs <= 0:
        raise ValueError("fs must be positive")
    n = arr.size
    win = sps.get_window(window, n, fftbins=True)
    spec = np.fft.rfft(arr * win)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    amps = np.abs(spec) * 2.0 / np.sum(win)
    return freqs, amps


def envelope_spectrum(
    x,
    fs: float,
    band: tuple[float, float] = (1000.0, 10000.0),
    order: int = 4,
) -> tuple[np.ndarray, np.ndarray]:
    """包络谱（共振解调）。轴承早期点蚀在时域看不出，必须在包络谱上找 BPFO/BPFI。

    步骤：带通(共振带) -> Hilbert 取包络 -> 去直流 -> FFT
    """
    arr = detrend(x)
    nyq = fs / 2.0
    low, high = band
    high = min(high, nyq * 0.95)
    if low <= 0 or low >= high:
        raise ValueError(f"invalid band {band} for fs={fs}")
    sos = sps.butter(order, [low, high], btype="bandpass", fs=fs, output="sos")
    filtered = sps.sosfiltfilt(sos, arr)
    env = np.abs(sps.hilbert(filtered))
    env = env - np.mean(env)
    win = sps.get_window("hann", env.size, fftbins=True)
    spec = np.fft.rfft(env * win)
    freqs = np.fft.rfftfreq(env.size, d=1.0 / fs)
    amps = np.abs(spec) * 2.0 / np.sum(win)
    return freqs, amps


def top_peaks(
    freqs: np.ndarray,
    amps: np.ndarray,
    n: int = 5,
    min_freq: float = 5.0,
    min_prominence_ratio: float = 0.15,
) -> list[tuple[float, float]]:
    """提取显著谱峰，返回 [(freq, amp)]，按幅值降序。"""
    mask = freqs >= min_freq
    f, a = freqs[mask], amps[mask]
    if f.size == 0:
        return []
    floor = float(np.median(a)) + EPS
    idx, props = sps.find_peaks(a, prominence=floor * min_prominence_ratio)
    if idx.size == 0:
        return []
    order = np.argsort(a[idx])[::-1][:n]
    return [(float(f[i]), float(a[i])) for i in idx[order]]


def robust_baseline(x) -> tuple[float, float]:
    """鲁棒基线：返回 (中位数, 1.4826*MAD)。工业数据非高斯，禁用均值/标准差。"""
    arr = _as_array(x)
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med)))
    return med, 1.4826 * mad


def robust_z(value: float, median: float, sigma_robust: float) -> float:
    """鲁棒 z 分数。sigma 为 0（恒定信号）时返回 0，避免除零爆炸。"""
    if sigma_robust < EPS:
        return 0.0
    return float((value - median) / sigma_robust)


def theil_sen_slope(y, x=None) -> float:
    """Theil-Sen 斜率估计（对离群点稳健），用于劣化速率。"""
    arr = _as_array(y)
    if arr.size < 2:
        return 0.0
    xs = np.arange(arr.size, dtype=float) if x is None else _as_array(x)
    slopes = []
    for i in range(arr.size - 1):
        dx = xs[i + 1 :] - xs[i]
        valid = np.abs(dx) > EPS
        if not np.any(valid):
            continue
        slopes.extend(((arr[i + 1 :] - arr[i])[valid] / dx[valid]).tolist())
    return float(np.median(slopes)) if slopes else 0.0


@dataclass(frozen=True)
class FeatureSet:
    """一次特征计算的完整产出，可直接落库为 DWD 层宽表。"""

    rms: float
    peak: float
    peak_to_peak: float
    kurtosis: float
    skewness: float
    crest_factor: float
    impulse_factor: float
    clearance_factor: float

    @classmethod
    def from_signal(cls, x) -> "FeatureSet":
        return cls(
            rms=rms(x),
            peak=peak(x),
            peak_to_peak=peak_to_peak(x),
            kurtosis=kurtosis(x),
            skewness=skewness(x),
            crest_factor=crest_factor(x),
            impulse_factor=impulse_factor(x),
            clearance_factor=clearance_factor(x),
        )

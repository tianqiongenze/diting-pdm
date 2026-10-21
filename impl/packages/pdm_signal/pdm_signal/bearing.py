"""轴承/齿轮特征频率与谱峰匹配。

判据来源：ISO 13373-3 / GB/T 19873.3 的包络解调流程。
注意：特征频率只是"嫌疑"，必须结合排除条件（啮合频率、电磁 2×电源、结构共振）才能定论。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

EPS = 1e-9


@dataclass(frozen=True)
class BearingGeometry:
    """轴承几何参数。d=滚珠直径, D=节径, n=滚珠数, alpha=接触角(度)。"""

    n_balls: int
    ball_d: float
    pitch_d: float
    contact_angle_deg: float = 0.0

    def __post_init__(self) -> None:
        if self.n_balls <= 0:
            raise ValueError("n_balls must be positive")
        if self.ball_d <= 0 or self.pitch_d <= 0:
            raise ValueError("ball_d and pitch_d must be positive")
        if self.ball_d >= self.pitch_d:
            raise ValueError("ball_d must be smaller than pitch_d")


def characteristic_frequencies(geom: BearingGeometry, fr: float) -> dict[str, float]:
    """给定转频 fr(Hz)，返回 BPFO/BPFI/BSF/FTF。

    fr 由转速得到：fr = rpm / 60
    """
    if fr <= 0:
        raise ValueError("fr must be positive")
    ratio = (geom.ball_d / geom.pitch_d) * math.cos(math.radians(geom.contact_angle_deg))
    n = geom.n_balls
    return {
        "BPFO": (n / 2.0) * fr * (1.0 - ratio),
        "BPFI": (n / 2.0) * fr * (1.0 + ratio),
        "BSF": (geom.pitch_d / (2.0 * geom.ball_d)) * fr * (1.0 - ratio**2),
        "FTF": (fr / 2.0) * (1.0 - ratio),
        "FR": fr,
    }


@dataclass(frozen=True)
class HarmonicMatch:
    """单个特征频率的匹配结果。"""

    name: str
    target: float
    harmonics_found: int
    orders: tuple[int, ...]
    best_amp: float
    snr: float
    confidence: float

    @property
    def is_suspicious(self) -> bool:
        """>=2 阶谐波且信噪比 >=3 才认为可疑（单峰不足以定论）。"""
        return self.harmonics_found >= 2 and self.snr >= 3.0


def match_harmonics(
    freqs: np.ndarray,
    amps: np.ndarray,
    target: float,
    tol_pct: float = 2.0,
    max_order: int = 3,
) -> HarmonicMatch:
    """在谱中搜索 target 的 1..max_order 阶谐波。

    snr = 匹配到的峰值 / 全谱中位数（底噪）。
    confidence 为 0..1 的经验映射：谐波数(0.5) + 信噪比(0.5)。
    """
    if target <= 0:
        raise ValueError("target must be positive")
    floor = float(np.median(amps)) + EPS
    tol = target * tol_pct / 100.0
    orders: list[int] = []
    best_amp = 0.0
    for k in range(1, max_order + 1):
        f0 = target * k
        mask = np.abs(freqs - f0) <= tol
        if not np.any(mask):
            continue
        amp = float(np.max(amps[mask]))
        if amp >= floor * 2.0:  # 至少 2 倍底噪才算"存在"
            orders.append(k)
            best_amp = max(best_amp, amp)
    snr = best_amp / floor if floor > EPS else 0.0
    conf = 0.5 * min(len(orders) / max_order, 1.0) + 0.5 * min(snr / 8.0, 1.0)
    return HarmonicMatch(
        name="",
        target=target,
        harmonics_found=len(orders),
        orders=tuple(orders),
        best_amp=best_amp,
        snr=snr,
        confidence=round(conf, 3),
    )


def diagnose_bearing(
    freqs: np.ndarray,
    amps: np.ndarray,
    geom: BearingGeometry,
    rpm: float,
    tol_pct: float = 2.0,
) -> list[HarmonicMatch]:
    """对四种特征频率逐一匹配，按 confidence 降序返回。"""
    cf = characteristic_frequencies(geom, rpm / 60.0)
    out: list[HarmonicMatch] = []
    for name in ("BPFO", "BPFI", "BSF", "FTF"):
        m = match_harmonics(freqs, amps, cf[name], tol_pct=tol_pct)
        out.append(
            HarmonicMatch(
                name=name,
                target=m.target,
                harmonics_found=m.harmonics_found,
                orders=m.orders,
                best_amp=m.best_amp,
                snr=m.snr,
                confidence=m.confidence,
            )
        )
    return sorted(out, key=lambda m: m.confidence, reverse=True)


def gear_mesh_frequency(teeth: int, rpm: float) -> float:
    """齿轮啮合频率 GMF = 齿数 × 转频。用于排除轴承误判。"""
    if teeth <= 0:
        raise ValueError("teeth must be positive")
    return teeth * rpm / 60.0


def electrical_frequency(rpm: float, poles: int) -> float:
    """电机电磁频率 = 极对数 × 转频。2× 电源频率常被误判为故障。"""
    if poles <= 0:
        raise ValueError("poles must be positive")
    return (poles / 2.0) * rpm / 60.0

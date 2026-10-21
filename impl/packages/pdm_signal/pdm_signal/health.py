"""健康指数（HI）计算。

模型（docs/04 §5）：
    HI = 100 × Π (1 − w_i · s_i)

为什么用乘积而不是加权和：
1. **单维度致命**：s_i = 1 时该项为 0，整体归零。加权和在同样情况下只扣 w_i 分
   （例如 w=0.35 时仍有 65 分，落在"黄"区），这正是要避免的"短板被平均掉"。
2. **避免重复扣分**：多个维度往往由同一根因驱动（轴承坏了 → 振动大 + 温度高 + 峭度高），
   线性叠加会把同一故障罚三次；乘积形式天然收敛。

数学性质（测试会验证，勿凭直觉改）：
    由 union bound，Π(1−a_i) ≥ 1 − Σa_i
    故  HI ≥ 100 − Σ(w_i·s_i) = 100 − linear_loss
    即乘积形式的联合惩罚**弱于**线性叠加（对非致命的多维劣化更宽容）。

可解释性：
    impact_i = 100 · w_i · s_i              —— 该维度名义损失的分数
    linear_loss = Σ impact_i                —— 名义总损失
    overlap_gap = linear_loss − (100 − HI)  —— 联合惩罚的"折扣"，恒 ≥ 0
"""

from __future__ import annotations

from dataclasses import dataclass, field

EPS = 1e-9

# 默认权重（docs/04 §5.1）。顺序：振动、冲击、温度、电流、趋势
DEFAULT_WEIGHTS: dict[str, float] = {
    "vib": 0.35,
    "imp": 0.20,
    "temp": 0.15,
    "curr": 0.15,
    "trend": 0.15,
}

LEVEL_THRESHOLDS = (
    (80.0, "green"),
    (60.0, "yellow"),
    (40.0, "orange"),
    (0.0, "red"),
)

LEVEL_SLA_HOURS = {"green": 0, "yellow": 168, "orange": 72, "red": 4}


def level_of(hi: float) -> str:
    """按 docs/04 §5.2 的四级色判定。"""
    if not (0.0 <= hi <= 100.0):
        raise ValueError(f"hi out of range: {hi}")
    for threshold, level in LEVEL_THRESHOLDS:
        if hi >= threshold:
            return level
    return "red"


@dataclass(frozen=True)
class Dimension:
    """一个健康维度。s ∈ [0,1] 为归一化劣化度，1 表示致命。"""

    name: str
    s: float
    weight: float | None = None
    evidence: str | None = None

    def __post_init__(self) -> None:
        if not (0.0 <= self.s <= 1.0):
            raise ValueError(f"{self.name}: s must be in [0,1], got {self.s}")
        if self.weight is not None and self.weight < 0:
            raise ValueError(f"{self.name}: weight must be >= 0")


@dataclass(frozen=True)
class Contribution:
    name: str
    s: float
    weight: float
    impact: float  # 100 * w * s
    evidence: str | None = None


@dataclass(frozen=True)
class HealthIndex:
    hi: float
    level: str
    sla_hours: int
    contributions: list[Contribution] = field(default_factory=list)
    degraded: bool = False
    degraded_reason: str | None = None

    @property
    def linear_loss(self) -> float:
        return round(sum(c.impact for c in self.contributions), 6)

    @property
    def overlap_gap(self) -> float:
        """维度间的重叠惩罚。恒 ≥ 0，用于解释"为什么比线性叠加更低"。"""
        return round(self.linear_loss - (100.0 - self.hi), 6)

    def top(self, n: int = 3) -> list[Contribution]:
        return sorted(self.contributions, key=lambda c: c.impact, reverse=True)[:n]


def compute_hi(
    dimensions: list[Dimension],
    weights: dict[str, float] | None = None,
) -> HealthIndex:
    """计算健康指数。

    规则：
    1. 任一维度 s >= 1.0 → HI = 0（单维度致命即整体致命）
    2. 权重未显式给出时取 DEFAULT_WEIGHTS；未知维度按等权处理
    3. 维度缺失（None 或未提供）→ 按剩余维度的权重重归一，并标记 degraded
    4. 权重和为 0 → 抛错（无意义输入）
    """
    if not dimensions:
        raise ValueError("at least one dimension is required")

    base = dict(weights or DEFAULT_WEIGHTS)

    # 1) 单维度致命短路
    fatal = [d for d in dimensions if d.s >= 1.0 - EPS]
    if fatal:
        contribs = [
            Contribution(
                name=d.name,
                s=d.s,
                weight=d.weight if d.weight is not None else base.get(d.name, 0.0),
                impact=100.0,
                evidence=d.evidence,
            )
            for d in fatal[:1]
        ]
        return HealthIndex(hi=0.0, level="red", sla_hours=LEVEL_SLA_HOURS["red"], contributions=contribs)

    # 2) 权重归一
    raw = {d.name: (d.weight if d.weight is not None else base.get(d.name, 1.0 / len(dimensions))) for d in dimensions}
    total_w = sum(raw.values())
    if total_w <= EPS:
        raise ValueError("sum of weights must be positive")
    norm = {k: v / total_w for k, v in raw.items()}

    # 3) 乘积形式
    product = 1.0
    contribs: list[Contribution] = []
    for d in dimensions:
        w = norm[d.name]
        product *= 1.0 - w * d.s
        contribs.append(
            Contribution(name=d.name, s=d.s, weight=round(w, 6), impact=round(100.0 * w * d.s, 6), evidence=d.evidence)
        )
    hi = round(max(0.0, min(100.0, 100.0 * product)), 2)

    # 4) 降级标记：维度数少于默认权重维度数
    degraded = len(dimensions) < len(base)
    reason = None
    if degraded:
        missing = sorted(set(base) - {d.name for d in dimensions})
        reason = "缺少维度: " + ",".join(missing) + "；已按剩余权重重归一，置信度下调"

    lv = level_of(hi)
    return HealthIndex(
        hi=hi,
        level=lv,
        sla_hours=LEVEL_SLA_HOURS[lv],
        contributions=contribs,
        degraded=degraded,
        degraded_reason=reason,
    )


def clamp01(x: float) -> float:
    """把任意实数压到 [0,1]。用于把物理量映射为劣化度。"""
    return float(max(0.0, min(1.0, x)))


def degradation_from_baseline(value: float, baseline: float, span_ratio: float = 3.0) -> float:
    """相对基线劣化度：达到 baseline × span_ratio 时视为致命。

    例：baseline=2.0 mm/s, span_ratio=3 → 4.0 mm/s 时 s=0.5，6.0 时 s=1.0
    """
    if baseline <= EPS:
        raise ValueError("baseline must be positive")
    return clamp01((value / baseline - 1.0) / max(span_ratio - 1.0, EPS))


def degradation_from_threshold(value: float, warn: float, fatal: float) -> float:
    """绝对阈值劣化度：warn 以下为 0，fatal 以上为 1，中间线性。"""
    if fatal <= warn:
        raise ValueError("fatal must be greater than warn")
    return clamp01((value - warn) / (fatal - warn))

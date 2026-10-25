"""世界模型 L1：统计退化模型 + 反事实推演（docs/43 §10.3，Phase C）。

定位（为什么不是追热点）：
PdM 的瓶颈不在"预测更准"，而在**决策推演**——"如果推迟 2 周保养，
HI 会掉到多少？这期间非计划停机概率多大？"当前只有线性外推 RUL，
无法回答反事实问题，方案卡的经济性数字因此缺乏依据。

L1 统计世界模型（本期）：
- 输入：read_asset_health_history 的 (ts, hi) 序列
- 学习：加权线性回归（近期点权重更高，指数衰减）
- 输出：未来轨迹 + **不确定性区间**（残差标准差 → 95% 区间）
- 反事实：维护干预（HI 重置到基线）+ 推迟情景

工业红线（docs/43 §13.2）：
1. 输出必须带区间——无区间不输出
2. 样本不足返回 null——不编数字（与 RUL 三边界同纪律）
3. 斜率 >= -0.01（无劣化）→ 推演无意义，返回 null

L2 神经世界模型（预留）：学习型退化仿真 + 情景生成，
接口与本模块一致（fit/predict/counterfactual 签名稳定），可替换。
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass
from typing import Any

MIN_POINTS = 3              # 与 estimate_rul 一致：少于 3 个点不估
MIN_SLOPE = -0.01           # 每天劣化不足 0.01 HI 视为无趋势
Z95 = 1.96                  # 95% 置信区间


@dataclass(frozen=True)
class DegradationModel:
    """加权线性退化模型：hi(t) = intercept + slope * t_days。"""
    slope_per_day: float
    intercept: float
    resid_std: float
    n_points: int
    window_days: float

    def predict(self, day_offset: float) -> tuple[float, float, float]:
        """预测 day_offset 天后的 (mean, lo, hi)。

        区间 = 残差标准差 × Z95 × sqrt(1 + 1/n + 杠杆项)——
        简化为残差标准差 × Z95 × sqrt(1 + 1/n)，随外推距离保持保守。
        """
        mean = self.intercept + self.slope_per_day * day_offset
        spread = self.resid_std * Z95 * math.sqrt(1 + 1 / max(1, self.n_points))
        return mean, mean - spread, mean + spread

    def days_until(self, critical_hi: float) -> float | None:
        """预计多少天后 HI 跌破临界线（无劣化返回 None）。"""
        if self.slope_per_day >= MIN_SLOPE:
            return None
        return (critical_hi - self.intercept) / self.slope_per_day


def fit(history: list[dict[str, Any]], *, half_life_days: float = 30.0) -> DegradationModel | None:
    """从健康历史拟合退化模型。

    history: [{"ts": datetime, "hi": float}, ...] 按时间升序。
    权重：指数衰减，半衰期 half_life_days——近期行为更能代表当前状态。
    样本不足/无劣化 → None（调用方按 null 处理，不编数字）。
    """
    pts = [(r["ts"], float(r["hi"])) for r in history
           if r.get("ts") is not None and r.get("hi") is not None]
    if len(pts) < MIN_POINTS:
        return None
    pts.sort(key=lambda p: p[0])
    t0 = pts[0][0]
    xs = [(ts - t0).total_seconds() / 86400.0 for ts, _ in pts]
    ys = [hi for _, hi in pts]

    # 指数权重（以最新点为基准）
    t_end = xs[-1]
    lam = math.log(2) / max(1.0, half_life_days)
    ws = [math.exp(-lam * (t_end - x)) for x in xs]

    sw = sum(ws)
    if sw <= 0:
        return None
    mx = sum(w * x for w, x in zip(ws, xs)) / sw
    my = sum(w * y for w, y in zip(ws, ys)) / sw
    var_x = sum(w * (x - mx) ** 2 for w, x in zip(ws, xs))
    if var_x <= 1e-9:
        return None                      # 时间无跨度，无法拟合
    cov_xy = sum(w * (x - mx) * (y - my) for w, x, y in zip(ws, xs, ys))
    slope = cov_xy / var_x
    if slope >= MIN_SLOPE:
        return None                      # 无劣化趋势，推演无意义

    intercept = my - slope * mx
    # 残差标准差（加权）
    resid = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    resid_var = sum(w * r * r for w, r in zip(ws, resid)) / sw
    resid_std = math.sqrt(max(resid_var, 1e-6))
    return DegradationModel(slope_per_day=slope, intercept=intercept,
                            resid_std=resid_std, n_points=len(pts),
                            window_days=xs[-1] - xs[0])


def counterfactual(tenant_id: str, asset_id: str, horizon_days: int = 14,
                   maintain_at_day: int | None = None,
                   maintain_reset_hi: float | None = None,
                   critical_hi: float = 40.0,
                   history_days: int = 90) -> dict[str, Any]:
    """反事实推演：给定"第 N 天做保养（HI 重置）"或"不干预"，输出 HI 轨迹。

    输出（工业红线：必带区间；样本不足返回 null 字段而非编造）：
    {
      "asset_id": ..., "model": {...} | null,
      "trajectory": [{"day": d, "mean": m, "lo": l, "hi": h}, ...],
      "failure_day_estimate": int | null,     # 预计跌破 critical_hi 的天数
      "intervention": {"day": N, "reset_hi": R} | null
    }
    """
    from app import repository

    since = datetime.datetime.now() - datetime.timedelta(days=history_days)
    rows = (
        repository.TenantQueryBuilder(tenant_id)
        .query("SELECT ts, hi FROM read_asset_health_history "
               "WHERE tenant_id=%s AND asset_id=%s AND ts >= %s ORDER BY ts",
               asset_id, since)
        .fetchall()
    )
    model = fit(rows)
    out: dict[str, Any] = {"asset_id": asset_id, "horizon_days": horizon_days,
                           "critical_hi": critical_hi,
                           "intervention": ({"day": maintain_at_day,
                                             "reset_hi": maintain_reset_hi}
                                            if maintain_at_day is not None else None)}
    if model is None:
        out["model"] = None
        out["trajectory"] = []
        out["failure_day_estimate"] = None
        out["note"] = "样本不足或无劣化趋势，按纪律不推演（不编数字）"
        return out

    out["model"] = {"slope_per_day": round(model.slope_per_day, 4),
                    "resid_std": round(model.resid_std, 3),
                    "n_points": model.n_points,
                    "window_days": round(model.window_days, 1)}

    # 基线轨迹：以最新 HI 为截距重新锚定（intercept 是窗口起点的值）
    latest = float(rows[-1]["hi"])
    anchor_slope = model.slope_per_day
    traj = []
    for day in range(0, max(1, horizon_days) + 1):
        if maintain_at_day is not None and day >= maintain_at_day:
            # 干预后：HI 重置到指定基线，随后按原斜率继续劣化
            reset = maintain_reset_hi if maintain_reset_hi is not None else latest
            mean = reset + anchor_slope * (day - maintain_at_day)
        else:
            mean = latest + anchor_slope * day
        spread = model.resid_std * Z95 * math.sqrt(1 + 1 / max(1, model.n_points))
        traj.append({"day": day, "mean": round(mean, 2),
                     "lo": round(mean - spread, 2), "hi": round(mean + spread, 2)})
    out["trajectory"] = traj

    # 无干预情景的预计跌破天数
    fail_days = model.days_until(critical_hi)
    if fail_days is not None:
        # days_until 以窗口起点为基准，换算到"现在起还有几天"
        days_from_window_start = (rows[-1]["ts"] - rows[0]["ts"]).total_seconds() / 86400.0
        remaining = fail_days - days_from_window_start
        out["failure_day_estimate"] = max(0, int(remaining)) if remaining > 0 else 0
    else:
        out["failure_day_estimate"] = None
    return out

"""决策会诊（L4 / ISO 13374 AG 执行层）。

设计要点（docs/05 §9）：
- 四角色并行独立出意见，各自只拿授权数据，互不可见
- 独立 Evaluator 校验一致性与证据充分性，**不由生成方自评**
- 输出方案卡（RecommendationCard），含经济性、窗口、备件、冲突
- 规则化实现，确定性可测；LLM 可后续替换单角色，但 Evaluator 必须独立

为什么先做规则版而不是 LLM 版：
1. 决策输出要可复现、可审计，规则版天然满足
2. 会诊的关键不是"说得像人话"，而是"约束算得对"（备件到货、排产窗口、成本）
3. LLM 版可以后接，接口不变
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

# 角色权重：影响一致性评分的加权
ROLE_WEIGHTS = {"shopfloor": 0.2, "reliability": 0.3, "production": 0.25, "maintenance": 0.25}

# 结论严重度：数值越大越紧急，用于平票时的确定性裁决
VERDICT_SEVERITY = {"NO_ACTION": 0, "MONITOR": 1, "PLAN_WINDOW": 2, "ACT_NOW": 3}


def aggregate_verdict(opinions: list[RoleOpinion]) -> str:
    """确定性聚合：加权票数最高者胜；平票时取更紧急的结论。

    为什么不能直接用 max(set(...), key=count)：
    set 迭代无序，2:2 平票时结果随机 —— 红色设备可能被判成 MONITOR。
    决策系统不允许这种不确定性，必须显式定义平票规则。
    """
    if not opinions:
        return "NO_ACTION"
    score: dict[str, float] = {}
    for o in opinions:
        score[o.verdict] = score.get(o.verdict, 0.0) + ROLE_WEIGHTS.get(o.role, 0.25)
    # 先比加权票数，再比严重度（平票取更紧急）
    return max(score.items(), key=lambda kv: (round(kv[1], 6), VERDICT_SEVERITY.get(kv[0], 0)))[0]


@dataclass
class RoleOpinion:
    role: str
    verdict: str  # ACT_NOW / PLAN_WINDOW / MONITOR / NO_ACTION
    urgency_hours: int
    confidence: float
    basis: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    cost_estimate: float | None = None


@dataclass
class Evaluation:
    consistency: float
    conflicts: list[str]
    evidence_gaps: list[str]
    risk_note: str


def _fmt(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# 四个角色：每个只看自己该看的数据
# ---------------------------------------------------------------------------
def role_shopfloor(diag: dict, snapshot: dict) -> RoleOpinion:
    """车间经理：运行数据 + 现场观察。不评估成本、不排产。"""
    hi, level = diag["hi"], diag["level"]
    basis = [
        f"健康指数 {hi}（{level}）",
        f"振动 {snapshot['vib_rms_mm_s']:.2f} mm/s（基线 {snapshot['vib_baseline_mm_s']:.2f}）",
        f"温升 {snapshot['temp_c'] - snapshot['temp_baseline_c']:.1f}℃",
    ]
    if level == "red":
        return RoleOpinion("shopfloor", "ACT_NOW", 4, 0.85, basis, ["设备已处于危险区，操作工需现场确认"])
    if level == "orange":
        return RoleOpinion("shopfloor", "PLAN_WINDOW", 72, 0.78, basis, ["需在下次换班窗口安排检查"])
    if level == "yellow":
        return RoleOpinion("shopfloor", "MONITOR", 168, 0.6, basis, ["加密点检频次"])
    return RoleOpinion("shopfloor", "NO_ACTION", 720, 0.5, basis, [])


def role_reliability(diag: dict, asset: dict, history: dict | None) -> RoleOpinion:
    """可靠性工程师：关键度 + 历史故障 + 同类案例。不排产。"""
    crit = asset.get("criticality", "B")
    basis = [
        f"设备关键度 {crit}",
        f"故障假设 {len(diag.get('hypotheses', []))} 个",
    ]
    if history:
        basis.append(f"近 12 个月故障 {history.get('faults_12m', 0)} 次，MTBF {history.get('mtbf_days', 'NA')} 天")
    conf = 0.8 if diag.get("hypotheses") else 0.65
    if crit == "A" and diag["level"] in ("orange", "red"):
        return RoleOpinion("reliability", "ACT_NOW", 24, conf, basis, ["A 类设备失效影响面大，建议优先处置"])
    if diag["level"] == "orange":
        return RoleOpinion("reliability", "PLAN_WINDOW", 72, conf, basis, [])
    return RoleOpinion("reliability", "MONITOR", 168, conf, basis, [])


def role_production(diag: dict, plan: dict) -> RoleOpinion:
    """生产经理：排产 + 瓶颈 + 交付节点。不定维修方案。"""
    basis = [
        f"当前排产负荷 {plan.get('load_pct', 80)}%",
        f"瓶颈工序：{plan.get('bottleneck', '未知')}",
        f"最近交付节点 {plan.get('next_delivery', 'NA')}",
    ]
    constraints = []
    if plan.get("load_pct", 80) >= 90:
        constraints.append("高负荷期，停机窗口稀缺，建议等计划检修窗口")
        verdict, urgency = "PLAN_WINDOW", 168
    elif plan.get("window_available"):
        constraints.append(f"可用窗口 {plan['window_available']}")
        verdict, urgency = "PLAN_WINDOW", 72
    else:
        constraints.append("近期无可用窗口，需与计划科协商插单")
        verdict, urgency = "PLAN_WINDOW", 96
    if diag["level"] == "red":
        constraints.append("红色等级下生产需让位于安全")
        verdict, urgency = "ACT_NOW", 8
    return RoleOpinion("production", verdict, urgency, 0.7, basis, constraints)


def role_maintenance(diag: dict, spare: dict, crew: dict) -> RoleOpinion:
    """维修经理：RUL + 备件 + 工时 + 技能。不改排产。"""
    rul = diag.get("rul_p50_days")
    basis = [
        f"剩余寿命中位数 {rul if rul is not None else 'NA'} 天",
        f"备件 {spare.get('sku', 'NA')} 库存 {spare.get('qty', 0)}，到货 {spare.get('eta', 'NA')}",
        f"可派工 {crew.get('available', 0)} 人，工时 {crew.get('hours', 0)}h",
    ]
    constraints = []
    if spare.get("qty", 0) <= 0:
        constraints.append(f"备件缺货，最早到货 {spare.get('eta', '未知')}")
    if crew.get("available", 0) < crew.get("required", 2):
        constraints.append("人力不足，需跨班组调配")
    if rul is not None and rul < 7:
        return RoleOpinion("maintenance", "ACT_NOW", 24, 0.82, basis, constraints)
    if rul is not None and rul < 30:
        return RoleOpinion("maintenance", "PLAN_WINDOW", 72, 0.78, basis, constraints)
    return RoleOpinion("maintenance", "MONITOR", 168, 0.6, basis, constraints)


# ---------------------------------------------------------------------------
# 独立评估者（不由生成方自评）
# ---------------------------------------------------------------------------
def evaluate(opinions: list[RoleOpinion]) -> Evaluation:
    if not opinions:
        return Evaluation(0.0, ["无任何角色意见"], ["缺少全部角色输入"], "无法会诊")

    verdicts = [o.verdict for o in opinions]
    dominant = max(set(verdicts), key=verdicts.count)
    agree = verdicts.count(dominant)

    # 一致性 = 加权同意比例
    total_w = sum(ROLE_WEIGHTS.get(o.role, 0.25) for o in opinions)
    agree_w = sum(ROLE_WEIGHTS.get(o.role, 0.25) for o in opinions if o.verdict == dominant)
    consistency = round(agree_w / total_w, 3) if total_w else 0.0

    conflicts: list[str] = []
    urgencies = [o.urgency_hours for o in opinions]
    if max(urgencies) - min(urgencies) >= 96:
        fast = min(opinions, key=lambda o: o.urgency_hours)
        slow = max(opinions, key=lambda o: o.urgency_hours)
        conflicts.append(
            f"紧迫度分歧：{fast.role} 要求 {fast.urgency_hours}h 内，{slow.role} 认为可等 {slow.urgency_hours}h"
        )
    if "ACT_NOW" in verdicts and "MONITOR" in verdicts:
        conflicts.append("存在'立即处置'与'仅监测'的结论冲突，需人工裁决")

    gaps: list[str] = []
    for o in opinions:
        if len(o.basis) < 2:
            gaps.append(f"{o.role} 依据不足（{len(o.basis)} 条）")
    if not any(o.confidence >= 0.75 for o in opinions):
        gaps.append("所有角色置信度均低于 0.75，证据可能不充分")

    risk = "低"
    if consistency < 0.6 or conflicts:
        risk = "中"
    if "ACT_NOW" in verdicts and consistency < 0.6:
        risk = "高"

    return Evaluation(consistency, conflicts, gaps, risk)


# ---------------------------------------------------------------------------
# 方案卡
# ---------------------------------------------------------------------------
def build_card(
    tenant_id: str,
    asset_id: str,
    diag: dict,
    snapshot: dict,
    opinions: list[RoleOpinion],
    evaluation: Evaluation,
    spare: dict,
    crew: dict,
    plan: dict,
    downtime_cost_per_hour: float = 6000.0,
    spare_price: float = 4200.0,
    labor_rate: float = 300.0,
) -> dict[str, Any]:
    """生成维护方案卡：含窗口、备件、人力、成本、净收益、冲突、待确认项。"""
    now = datetime.now()
    eta_raw = spare.get("eta")
    eta = None
    if isinstance(eta_raw, str) and eta_raw:
        try:
            eta = datetime.strptime(eta_raw, "%Y-%m-%d")
        except ValueError:
            eta = None
    elif isinstance(eta_raw, datetime):
        eta = eta_raw

    downtime_h = crew.get("hours", 6)
    options: list[dict[str, Any]] = []

    def _opt(label: str, start: datetime, spare_ready: bool, impact: str) -> dict[str, Any]:
        labor = downtime_h * crew.get("required", 2) * labor_rate
        down = downtime_h * downtime_cost_per_hour
        total = spare_price + labor + down
        return {
            "option_id": label,
            "window_start": _fmt(start),
            "window_end": _fmt(start + timedelta(hours=downtime_h)),
            "downtime_hours": downtime_h,
            "production_impact": impact,
            "spare_ready": spare_ready,
            "crew": [f"机修 {crew.get('required', 2)} 人"],
            "cost": {
                "spare": spare_price,
                "labor": round(labor, 2),
                "downtime": round(down, 2),
                "total": round(total, 2),
                "currency": "CNY",
            },
            "risk": "低" if spare_ready else "中（备件未到）",
        }

    # 方案 A：备件到货后 + 最近可用窗口
    earliest = now + timedelta(days=1)
    if eta:
        earliest = max(earliest, eta + timedelta(days=1))
    # 对齐到周日（示例策略：周日计划检修）
    days_ahead = (6 - earliest.weekday()) % 7
    window_a = (earliest + timedelta(days=days_ahead)).replace(hour=8, minute=0, second=0, microsecond=0)
    options.append(_opt("A-计划窗口", window_a, spare.get("qty", 0) > 0 or bool(eta), "低"))

    # 方案 B：备件未到则给"调拨"备选，代价更高
    if not eta or eta > now + timedelta(days=3):
        window_b = now + timedelta(hours=24)
        opt_b = _opt("B-紧急调拨", window_b, False, "高")
        opt_b["cost"]["spare"] = round(spare_price * 1.35, 2)
        opt_b["cost"]["total"] = round(
            opt_b["cost"]["spare"] + opt_b["cost"]["labor"] + opt_b["cost"]["downtime"], 2
        )
        opt_b["risk"] = "高（调拨溢价 35%，且需插单）"
        options.append(opt_b)

    avoided = downtime_cost_per_hour * 8 * 3  # 假设避免 3 次 8 小时非计划停机
    total_cost = min(o["cost"]["total"] for o in options)
    net = round(avoided - total_cost, 2)

    dominant = aggregate_verdict(opinions)

    # 领域不变量：红色等级不得被判为"仅监测"。
    # 这是安全兜底——即使多数角色偏保守，红色也必须升级为立即处置。
    escalated = False
    if diag.get("level") == "red" and dominant in ("MONITOR", "NO_ACTION"):
        dominant = "ACT_NOW"
        escalated = True
    required_roles = ["maintenance_supervisor"]
    if any(o.verdict == "ACT_NOW" for o in opinions):
        required_roles.append("equipment_manager")

    return {
        "card_id": f"REC-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}",
        "tenant_id": tenant_id,
        "asset_id": asset_id,
        "diagnosis": {
            "conclusion": diag.get("conclusion") or f"健康指数 {diag['hi']}（{diag['level']}）",
            "confidence": round(sum(o.confidence for o in opinions) / len(opinions), 3),
            "evidence": diag.get("evidence", [])[:5],
        },
        "options": options,
        "recommendation": "A-计划窗口" if options[0]["spare_ready"] else "需先解决备件，暂不推荐立即停机",
        "verdict": dominant,
        "verdict_escalated": escalated,
        "economics": {
            "avoided_loss": avoided,
            "total_cost": total_cost,
            "net_benefit": net,
            "currency": "CNY",
        },
        "conflicts": evaluation.conflicts,
        "evidence_gaps": evaluation.evidence_gaps,
        "consistency": evaluation.consistency,
        "risk_level": evaluation.risk_note,
        "opinions": [
            {
                "role": o.role,
                "verdict": o.verdict,
                "urgency_hours": o.urgency_hours,
                "confidence": o.confidence,
                "basis": o.basis,
                "constraints": o.constraints,
            }
            for o in opinions
        ],
        "human_confirm": {
            "required_roles": required_roles,
            "deadline": _fmt(now + timedelta(hours=48)),
            "status": "PENDING",
        },
        "disclaimer": (
            "本方案卡由 AI 辅助生成，不构成维修指令。"
            "任何停机、拆检、更换或采购决策须由持证人员现场确认。"
        ),
    }

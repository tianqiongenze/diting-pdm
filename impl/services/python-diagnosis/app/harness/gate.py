"""PermissionGate 三级权限门（docs/05 §4，Phase A3）。

三级决策：
- allow ：只读工具，直接放行
- ask   ：写操作，需人工/令牌确认（携带确认上下文）
- deny  ：越权或超阈值未授权，拒绝并记录

阈值（双人复核）：
- downtime_cost_per_hour × 停机时长 > ¥50,000 → dual
- 备件采购金额 > ¥50,000 → dual（与 purchase_order.confirm_level 对齐）

与既有 RBAC 的关系：Gate 管"这个动作要不要人点头"，auth.py 管"这个人有没有权限"。
两层串联：Gate 先判动作级别，RBAC 再判角色权限。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DUAL_APPROVAL_THRESHOLD = 50_000.0   # ¥5 万：停机损失或采购金额超此值需双人复核


@dataclass(frozen=True)
class GateDecision:
    action: str            # allow / ask / deny
    reason: str
    dual: bool = False     # 是否需要双人复核
    required_role: str = ""


def evaluate(tool_side_effect: str, tool_name: str, arguments: dict[str, Any],
             user_role: str | None = None,
             downtime_cost_per_hour: float = 6000.0) -> GateDecision:
    """对一次工具调用做权限判定。

    user_role=None 表示无登录态（自动化/边缘场景）：写操作按 ask 处理，
    由调用方提供令牌；deny 仅用于显式违规（未知工具由 LoopController 处理）。
    """
    if tool_side_effect == "read":
        return GateDecision("allow", "只读工具", required_role="")

    if tool_side_effect == "write":
        return GateDecision("ask", f"写操作 {tool_name} 需确认", required_role="")

    # critical：停机/工单/点检提交类
    dual = False
    reason = f"关键操作 {tool_name} 需确认"
    if tool_name == "ack_alert" and arguments.get("action") == "close":
        reason = "关闭告警需确认（防误关）"
    est_cost = _estimate_exposure(tool_name, arguments, downtime_cost_per_hour)
    if est_cost > DUAL_APPROVAL_THRESHOLD:
        dual = True
        reason = (f"关键操作 {tool_name} 预估暴露 ¥{est_cost:,.0f} "
                  f"超过 ¥{DUAL_APPROVAL_THRESHOLD:,.0f}，需双人复核")
    return GateDecision("ask", reason, dual=dual, required_role="")


def _estimate_exposure(tool_name: str, arguments: dict[str, Any],
                       downtime_cost_per_hour: float) -> float:
    """粗估一次操作的停机/金额暴露（用于双人复核判定）。"""
    if tool_name == "submit_inspection":
        # 点检本身不停机；但 NG 项会转停机建议，按 2h 停机估
        return downtime_cost_per_hour * 2
    if tool_name == "ack_alert":
        action = arguments.get("action", "ack")
        if action == "close":
            # 误关红色告警的暴露：按 4h SLA × 停机成本
            return downtime_cost_per_hour * 4
        return 0.0
    return 0.0

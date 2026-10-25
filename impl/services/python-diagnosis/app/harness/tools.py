"""Harness 工具注册表（docs/05 §5，Phase A2）。

设计纪律（docs/05）：
- 宁少勿多：每个工具语义清晰、副作用明确
- 每个工具有 JSON Schema + 前置条件 + 副作用声明
- **一份定义两处暴露**：MCP tools 与 Harness 内部执行共用本注册表，
  避免两条路径行为漂移（单点/批量诊断共用核心的同一教训）

副作用分级（与 PermissionGate 联动）：
- read    ：只读，allow
- write   ：写业务数据，ask（需人工/令牌确认）
- critical：涉及工单/停机/金额，ask + 双人复核阈值
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    side_effect: str                      # read / write / critical
    preconditions: tuple[str, ...] = ()   # 人类可读的前置条件（用于 GATE 说明）
    fn: Callable[..., Any] | None = field(default=None, repr=False, compare=False)

    def public_spec(self) -> dict[str, Any]:
        """MCP/Agent Card 暴露的契约（不含可执行体）。"""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "sideEffect": self.side_effect,
            "preconditions": list(self.preconditions),
        }


# ---------------------------------------------------------------------------
# 工具实现：薄封装，核心逻辑仍走 repository / domain（不复制业务）
# ---------------------------------------------------------------------------
def _tool_get_asset(asset_id: str) -> dict[str, Any]:
    from app import repository
    return _safe(lambda: repository.get_asset("site-a", asset_id) or {})


def _tool_assess_health(asset_id: str) -> dict[str, Any]:
    from app import repository

    def _q():
        row = (
            repository.TenantQueryBuilder("site-a")
            .query("SELECT hi, level FROM read_asset_health WHERE tenant_id=%s AND asset_id=%s",
                   asset_id)
            .fetchone()
        )
        return row or {"asset_id": asset_id, "hi": None, "level": "unknown"}

    return _safe(_q)


def _tool_diagnose_asset(asset_id: str) -> dict[str, Any]:
    from app import domain
    from app import repository
    from app.schemas import DiagnoseRequest, Metrics

    snap = _safe(lambda: repository.get_snapshot("site-a", asset_id))
    if not snap or snap.get("degraded") or not snap.get("vib_rms_mm_s"):
        reason = (snap or {}).get("degraded_reason") or "no snapshot"
        return {"asset_id": asset_id, "degraded": True, "degraded_reason": reason}
    m = Metrics(
        vib_rms_mm_s=snap["vib_rms_mm_s"], vib_baseline_mm_s=snap["vib_baseline_mm_s"],
        kurtosis=snap["kurtosis"], temp_c=snap["temp_c"],
        temp_baseline_c=snap["temp_baseline_c"], current_dev_pct=snap["current_dev_pct"],
        trend_pct_per_day=snap["trend_pct_per_day"],
    )
    return domain.diagnose(DiagnoseRequest(tenant_id="site-a", asset_id=asset_id, metrics=m))


def _tool_consult_maintenance(asset_id: str, spare_eta: str | None = None,
                              load_pct: float | None = None) -> dict[str, Any]:
    from app import decision, repository
    from app import domain
    from app.schemas import DiagnoseRequest, Metrics

    snap = repository.get_snapshot("site-a", asset_id)
    if not snap:
        raise ValueError("no snapshot for asset")
    m = Metrics(
        vib_rms_mm_s=snap["vib_rms_mm_s"], vib_baseline_mm_s=snap["vib_baseline_mm_s"],
        kurtosis=snap["kurtosis"], temp_c=snap["temp_c"],
        temp_baseline_c=snap["temp_baseline_c"], current_dev_pct=snap["current_dev_pct"],
        trend_pct_per_day=snap["trend_pct_per_day"],
    )
    diag = domain.diagnose(DiagnoseRequest(tenant_id="site-a", asset_id=asset_id, metrics=m))
    asset = repository.get_asset("site-a", asset_id) or {"criticality": "B"}
    spare = {"sku": "SKF-6316-C3", "qty": 1, "eta": spare_eta}
    crew = {"available": 3, "required": 2, "hours": 6}
    plan = {"load_pct": load_pct if load_pct is not None else 82,
            "bottleneck": "总装线", "window_available": "周日 08:00"}
    opinions = [
        decision.role_shopfloor(diag, snap),
        decision.role_reliability(diag, asset, None),
        decision.role_production(diag, plan),
        decision.role_maintenance(diag, spare, crew),
    ]
    evaluation = decision.evaluate(opinions)
    card = decision.build_card("site-a", asset_id, diag, snap, opinions, evaluation,
                               spare, crew, plan)
    return {"card": card, "evaluation": {
        "consistency": evaluation.consistency,
        "conflicts": evaluation.conflicts,
        "evidence_gaps": evaluation.evidence_gaps,
        "risk_note": evaluation.risk_note,
    }}


def _tool_list_hot_assets(limit: int = 20) -> dict[str, Any]:
    from app import repository
    return _safe(lambda: {"assets": repository.list_hot_assets(
        "site-a", max(1, min(int(limit), 200)))})


def _tool_get_health_trend(asset_id: str, days: int = 7) -> dict[str, Any]:
    from app import repository
    return _safe(lambda: repository.get_health_trend(
        "site-a", asset_id, max(1, min(int(days), 90))))


def _tool_list_alerts(status: str = "", limit: int = 50) -> dict[str, Any]:
    from app import repository

    def _q():
        rows = repository.list_alerts("site-a", status, "", int(limit))
        if isinstance(rows, tuple):
            rows = rows[0]
        return {"alerts": rows, "count": len(rows)}

    return _safe(_q)


def _tool_ack_alert(alert_id: str, action: str = "ack", by: str = "agent",
                    comment: str = "") -> dict[str, Any]:
    from app import repository
    return repository.ack_alert("site-a", alert_id, action, by, comment)


def _tool_list_plans(limit: int = 50) -> dict[str, Any]:
    from app import repository
    return _safe(lambda: {"plans": repository.list_plans("site-a", int(limit))})


def _tool_submit_inspection(task_id: str, items: list[dict[str, Any]],
                            by: str = "agent", note: str = "") -> dict[str, Any]:
    from app import repository
    return repository.submit_inspection("site-a", task_id, items, by, note)


def _tool_list_spares(low_only: bool = False) -> dict[str, Any]:
    from app import repository
    return _safe(lambda: {"items": repository.list_spares("site-a", bool(low_only))})


def _tool_query_knowledge(q: str = "", limit: int = 20) -> dict[str, Any]:
    from app import repository
    return _safe(lambda: {"items": repository.list_cases("site-a", q, int(limit))})


def _safe(fn, *args, **kwargs):
    """读工具兜底：库/网络不可用时返回 degraded 结构而非崩溃。

    纪律与 server._run_single_diagnose 一致：失败可降级，但必须显式标注
    degraded + reason，调用方（LoopController/Eval）能识别。
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"degraded": True, "degraded_reason": f"{type(exc).__name__}: {exc}"[:160]}


_TOOLS: list[Tool] = [
    Tool("get_asset", "查询设备台账（关键度、类别、所属产线）。只读。",
         {"type": "object",
          "properties": {"asset_id": {"type": "string", "description": "设备位号，如 FAN-00004"}},
          "required": ["asset_id"]},
         "read", ("asset_id 必须存在于本租户台账",), _tool_get_asset),
    Tool("assess_health", "评估设备当前健康指数(HI)与四级色。只读，不产生工单。",
         {"type": "object",
          "properties": {"asset_id": {"type": "string"}},
          "required": ["asset_id"]},
         "read", ("设备已有健康评估（诊断覆盖）",), _tool_assess_health),
    Tool("diagnose_asset", "对设备做完整诊断：HI + 5 维分解 + 故障假设 + 证据链。只读。",
         {"type": "object",
          "properties": {"asset_id": {"type": "string"}},
          "required": ["asset_id"]},
         "read", ("设备有多源指标快照，否则降级并标注",), _tool_diagnose_asset),
    Tool("consult_maintenance",
         "触发四角色会诊，产出维护方案卡（窗口/备件/人力/成本/冲突）。生成待确认卡片，不创建工单。",
         {"type": "object",
          "properties": {
             "asset_id": {"type": "string"},
             "spare_eta": {"type": "string", "description": "备件到货日期 YYYY-MM-DD，可选"},
             "load_pct": {"type": "number", "description": "当前排产负荷百分比，可选"}},
          "required": ["asset_id"]},
         "write", ("设备有快照", "方案卡必须经人工确认后才可转工单"), _tool_consult_maintenance),
    Tool("list_hot_assets", "列出需优先关注的设备（按关键度 + HI 升序）。只读。",
         {"type": "object",
          "properties": {"limit": {"type": "integer", "default": 20, "maximum": 200}},
          "required": []},
         "read", (), _tool_list_hot_assets),
    Tool("get_health_trend", "设备 HI 趋势（看板折线图）。只读。",
         {"type": "object",
          "properties": {"asset_id": {"type": "string"},
                         "days": {"type": "integer", "default": 7, "maximum": 90}},
          "required": ["asset_id"]},
         "read", (), _tool_get_health_trend),
    Tool("list_alerts", "告警列表（超时优先）。只读。",
         {"type": "object",
          "properties": {"status": {"type": "string", "description": "OPEN/ACKED/...，可空"},
                         "limit": {"type": "integer", "default": 50}},
          "required": []},
         "read", (), _tool_list_alerts),
    Tool("ack_alert", "处置告警：ack/escalate/close。写操作，需 alert.ack 及以上权限。",
         {"type": "object",
          "properties": {"alert_id": {"type": "string"},
                         "action": {"type": "string", "enum": ["ack", "escalate", "close"]},
                         "by": {"type": "string"},
                         "comment": {"type": "string"}},
          "required": ["alert_id"]},
         "critical", ("alert_id 存在且未关闭", "操作者角色具备对应权限"), _tool_ack_alert),
    Tool("list_plans", "保养计划列表（逾期优先）。只读。",
         {"type": "object",
          "properties": {"limit": {"type": "integer", "default": 50}},
          "required": []},
         "read", (), _tool_list_plans),
    Tool("submit_inspection", "提交点检结果，推进保养计划下次到期。写操作，需 inspect.execute。",
         {"type": "object",
          "properties": {"task_id": {"type": "string"},
                         "items": {"type": "array", "items": {"type": "object"}},
                         "by": {"type": "string"},
                         "note": {"type": "string"}},
          "required": ["task_id"]},
         "critical", ("任务存在且为 PENDING", "操作者具备 inspect.execute"), _tool_submit_inspection),
    Tool("list_spares", "备件库存（可只看低库存）。只读。",
         {"type": "object",
          "properties": {"low_only": {"type": "boolean", "default": False}},
          "required": []},
         "read", (), _tool_list_spares),
    Tool("query_knowledge", "故障案例检索（标题/根因/标签/设备号）。只读。",
         {"type": "object",
          "properties": {"q": {"type": "string"},
                         "limit": {"type": "integer", "default": 20}},
          "required": []},
         "read", (), _tool_query_knowledge),
]

_REGISTRY: dict[str, Tool] = {t.name: t for t in _TOOLS}


def list_tools() -> list[Tool]:
    return list(_TOOLS)


def get_tool(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """执行工具（Harness ACT 阶段调用）。

    校验：工具存在 + 必填入参齐备。业务异常原样抛出，由 LoopController 归类。
    """
    tool = _REGISTRY.get(name)
    if tool is None:
        raise KeyError(f"unknown tool: {name}")
    if tool.fn is None:
        raise RuntimeError(f"tool {name} has no implementation")
    required = tool.input_schema.get("required", [])
    missing = [k for k in required if k not in (arguments or {})]
    if missing:
        raise ValueError(f"tool {name} missing required args: {missing}")
    return tool.fn(**(arguments or {}))

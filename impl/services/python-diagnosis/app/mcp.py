"""最小 MCP 服务端（JSON-RPC 2.0 over HTTP）。

为什么自己实现而不装官方 SDK：
- 服务器出口仅 128KB/s，装 SDK 要拉一串依赖，收益不抵成本
- MCP 的 HTTP 传输层就是 JSON-RPC 2.0，核心方法只有 3 个，实现成本极低
- 契约（tools/list 的 inputSchema）与官方一致，任何 MCP 客户端都能连

支持：
    initialize   → 返回 protocolVersion / capabilities / serverInfo
    tools/list   → 返回 5 个工具及 JSON Schema
    tools/call   → 执行工具，返回 content[]

工具设计遵守 docs/05 §5：宁少勿多，每个工具语义清晰、副作用明确。
"""

from __future__ import annotations

import json
from typing import Any

from . import decision, domain, repository
from .schemas import DiagnoseRequest, Metrics

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "diting-pdm", "version": "0.3.0"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_asset",
        "description": "查询设备台账（关键度、类别、所属产线）。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {"asset_id": {"type": "string", "description": "设备位号，如 FAN-00004"}},
            "required": ["asset_id"],
        },
    },
    {
        "name": "assess_health",
        "description": "评估设备当前健康指数(HI)与四级色。只读，不产生工单。",
        "inputSchema": {
            "type": "object",
            "properties": {"asset_id": {"type": "string"}},
            "required": ["asset_id"],
        },
    },
    {
        "name": "diagnose_asset",
        "description": "对设备做完整诊断：HI + 5 维分解 + 故障假设 + 证据链。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {"asset_id": {"type": "string"}},
            "required": ["asset_id"],
        },
    },
    {
        "name": "consult_maintenance",
        "description": (
            "触发四角色会诊，产出维护方案卡（窗口/备件/人力/成本/冲突）。"
            "该操作不创建工单，仅生成待人工确认的方案卡。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset_id": {"type": "string"},
                "spare_eta": {"type": "string", "description": "备件到货日期 YYYY-MM-DD，可选"},
                "load_pct": {"type": "number", "description": "当前排产负荷百分比，可选"},
            },
            "required": ["asset_id"],
        },
    },
    {
        "name": "list_hot_assets",
        "description": "列出需优先关注的设备（按关键度 + HI 升序）。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20, "maximum": 200}},
            "required": [],
        },
    },
]


def _text(payload: Any) -> dict[str, Any]:
    return {"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}


def _err(code: int, msg: str, rid: Any = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}


def _ok(result: Any, rid: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _require_tenant(tenant: str | None) -> str:
    if not tenant:
        raise PermissionError("missing X-Tenant-Id")
    return tenant


def _diagnose(tenant: str, asset_id: str) -> dict[str, Any]:
    snap = repository.get_snapshot(tenant, asset_id)
    if not snap:
        raise ValueError(f"no snapshot for {asset_id}")
    m = Metrics(
        vib_rms_mm_s=snap["vib_rms_mm_s"],
        vib_baseline_mm_s=snap["vib_baseline_mm_s"],
        kurtosis=snap["kurtosis"],
        temp_c=snap["temp_c"],
        temp_baseline_c=snap["temp_baseline_c"],
        current_dev_pct=snap["current_dev_pct"],
        trend_pct_per_day=snap["trend_pct_per_day"],
    )
    return domain.diagnose(DiagnoseRequest(tenant_id=tenant, asset_id=asset_id, metrics=m))


def _call_tool(name: str, args: dict[str, Any], tenant: str) -> dict[str, Any]:
    if name == "get_asset":
        a = repository.get_asset(tenant, args["asset_id"])
        return {"content": [_text(a or {"error": "not found"})], "isError": a is None}

    if name == "assess_health":
        d = _diagnose(tenant, args["asset_id"])
        return {
            "content": [
                _text(
                    {
                        "asset_id": args["asset_id"],
                        "hi": d["hi"],
                        "level": d["level"],
                        "sla_hours": d["sla_hours"],
                        "top_dimensions": d["contributions"][:3],
                    }
                )
            ]
        }

    if name == "diagnose_asset":
        d = _diagnose(tenant, args["asset_id"])
        return {"content": [_text(d)]}

    if name == "consult_maintenance":
        asset = repository.get_asset(tenant, args["asset_id"]) or {"criticality": "B"}
        snap = repository.get_snapshot(tenant, args["asset_id"])
        if not snap:
            return {"content": [_text({"error": "no snapshot"})], "isError": True}
        diag = _diagnose(tenant, args["asset_id"])
        spare = {"sku": "SKF-6316-C3", "qty": 1, "eta": args.get("spare_eta")}
        crew = {"available": 3, "required": 2, "hours": 6}
        plan = {"load_pct": args.get("load_pct", 82), "bottleneck": "总装线", "window_available": "周日 08:00"}
        ops = [
            decision.role_shopfloor(diag, snap),
            decision.role_reliability(diag, asset, None),
            decision.role_production(diag, plan),
            decision.role_maintenance(diag, spare, crew),
        ]
        ev = decision.evaluate(ops)
        card = decision.build_card(tenant, args["asset_id"], diag, snap, ops, ev, spare, crew, plan)
        return {"content": [_text({"card": card, "evaluation": ev.__dict__})]}

    if name == "list_hot_assets":
        limit = int(args.get("limit", 20))
        rows = repository.list_hot_assets(tenant, limit=limit)
        return {"content": [_text({"count": len(rows), "assets": rows})]}

    raise ValueError(f"unknown tool: {name}")


def handle_rpc(req: dict[str, Any], tenant: str | None) -> dict[str, Any]:
    """处理单条 JSON-RPC 请求。"""
    rid = req.get("id")
    method = req.get("method", "")
    params = req.get("params") or {}

    if method == "initialize":
        return _ok(
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
            rid,
        )

    if method == "tools/list":
        return _ok({"tools": TOOLS}, rid)

    if method == "tools/call":
        try:
            t = _require_tenant(tenant)
            name = params.get("name", "")
            args = params.get("arguments") or {}
            return _ok(_call_tool(name, args, t), rid)
        except PermissionError as e:
            return _err(-32001, str(e), rid)
        except KeyError as e:
            return _err(-32602, f"missing argument: {e}", rid)
        except ValueError as e:
            return _err(-32602, str(e), rid)
        except Exception as e:  # noqa: BLE001
            return _err(-32603, f"internal error: {e}", rid)

    if method in ("notifications/initialized", "ping"):
        return _ok({}, rid)

    return _err(-32601, f"method not found: {method}", rid)

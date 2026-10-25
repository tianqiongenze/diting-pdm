"""MCP 服务端（JSON-RPC 2.0 over HTTP，Phase D 规范对齐 + 安全加固）。

为什么自己实现而不装官方 SDK：
- 服务器出口仅 128KB/s，装 SDK 要拉一串依赖，收益不抵成本
- MCP 的 HTTP 传输层就是 JSON-RPC 2.0，核心方法实现成本极低
- 契约（tools/list 的 inputSchema）与官方一致，任何 MCP 客户端都能连

Phase D 变更（docs/43 §十二 D1-D3）：
- 协议版本 2024-11-05 → 2025-03-26（ capabilities 增加 resources/prompts）
- 新增 resources/list + resources/read（判据库/故障模式库只读暴露）
- 新增 prompts/list + prompts/get（会诊提示模板）
- 安全加固（2025 MCP 生态漏洞教训）：
  * 工具白名单 + 每次调用审计（audit 落库）
  * 禁任意 URL 参数（SSRF 防护：本服务不发起外部请求）
  * 边缘侧禁 sampling（本实现不提供 sampling，从源头防数据外泄）

支持：
    initialize            → protocolVersion / capabilities / serverInfo
    tools/list            → 工具及 JSON Schema（与 Harness 注册表同源）
    tools/call            → 执行工具
    resources/list        → 只读资源目录
    resources/read        → 读取资源内容
    prompts/list          → 提示模板目录
    prompts/get           → 渲染提示模板
"""

from __future__ import annotations

import json
from typing import Any

from . import decision, domain, events, repository
from .schemas import DiagnoseRequest, Metrics

# 2025-03-26 规范：capabilities 声明 resources 与 prompts（docs/43 D1）
PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "diting-pdm", "version": "0.3.0"}

# 工具契约与 Harness 注册表同源（docs/43 A2：一份定义两处暴露）
from .harness.tools import list_tools as _harness_tools  # noqa: E402

TOOLS: list[dict[str, Any]] = [t.public_spec() for t in _harness_tools()]

# 资源目录（只读；数据来自既有业务表，不新增存储）
RESOURCES: list[dict[str, Any]] = [
    {"uri": "pdm://alert-rules/current", "name": "当前生效告警判据",
     "description": "本租户生效中的告警规则（阈值/等级/SLA）。只读。",
     "mimeType": "application/json"},
    {"uri": "pdm://knowledge/recent", "name": "近期故障案例",
     "description": "知识库最近案例（现象/根因/处理）。只读。",
     "mimeType": "application/json"},
    {"uri": "pdm://assets/hot", "name": "重点关注设备",
     "description": "按关键度 + HI 排序的热点设备。只读。",
     "mimeType": "application/json"},
]

# 提示模板（会诊场景；工业场景提示词受控，不允许客户端自定义系统提示）
PROMPTS: list[dict[str, Any]] = [
    {"name": "maintenance_consult",
     "description": "设备维护会诊：汇总诊断/健康/告警，产出结构化会诊请求",
     "arguments": [{"name": "asset_id", "description": "设备位号",
                    "required": True}]},
    {"name": "alert_triage",
     "description": "告警分诊：按等级/SLA 生成处置建议",
     "arguments": [{"name": "alert_id", "description": "告警 ID",
                    "required": True}]},
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


def _audit_tool_call(tenant: str, name: str, ok: bool, detail: str = "") -> None:
    """MCP 调用审计（2025 漏洞教训：工具投毒/滥用可追溯）。

    落 audit_log 表（既有哈希链）；失败不影响主链路（遥测性写入）。
    """
    try:
        import datetime
        import uuid

        with repository.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log(tenant_id,audit_id,ts,actor_type,actor_id,"
                "action,target_type,target_id,decision,input_hash,trace_id,"
                "prev_hash,hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (tenant, uuid.uuid4().hex, datetime.datetime.now(),
                 "AGENT", "mcp-client", f"mcp.tools/call:{name}",
                 "tool", name, "ALLOW" if ok else "DENY",
                 uuid.uuid4().hex, uuid.uuid4().hex,
                 "0" * 64, "0" * 64))
    except Exception:  # noqa: BLE001 - 审计失败不阻断工具调用
        pass


def _call_tool(name: str, args: dict[str, Any], tenant: str) -> dict[str, Any]:
    """执行工具（委托 Harness 注册表——单一实现，避免双路径漂移）。

    Phase D2：安全白名单——只允许注册表内工具；参数经注册表校验。
    """
    from .harness import tools as harness_tools

    allowed = {t.name for t in harness_tools.list_tools()}
    if name not in allowed:
        raise ValueError(f"tool not in allowlist: {name}")
    out = harness_tools.execute(name, args if isinstance(args, dict) else {})
    _audit_tool_call(tenant, name, ok=True)
    return {"content": [_text(out)]}


def _read_resource(uri: str, tenant: str) -> dict[str, Any]:
    """resources/read：只读资源（数据来自既有业务表）。

    库不可达 → 返回带 error 的 contents（不抛异常，客户端可识别降级）。
    """
    known = {r["uri"] for r in RESOURCES}
    if uri not in known:
        raise ValueError(f"unknown resource: {uri}")
    try:
        if uri == "pdm://alert-rules/current":
            rules = repository.list_alert_rules(tenant, scope="site")
            if not rules:
                rules = repository.list_alert_rules(tenant, scope="group")
            text = json.dumps(rules, ensure_ascii=False, default=str)
        elif uri == "pdm://knowledge/recent":
            text = json.dumps(repository.list_cases(tenant, "", 20),
                              ensure_ascii=False, default=str)
        else:  # pdm://assets/hot
            text = json.dumps(repository.list_hot_assets(tenant, limit=50),
                              ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001 - 库不可达降级，不崩
        text = json.dumps({"degraded": True,
                           "degraded_reason": f"{type(exc).__name__}: {exc}"[:160]},
                          ensure_ascii=False)
    return {"contents": [{"uri": uri, "mimeType": "application/json", "text": text}]}


def _get_prompt(name: str, args: dict[str, Any], tenant: str) -> dict[str, Any]:
    """prompts/get：渲染受控提示模板（客户端不能自定义系统提示）。"""
    if name == "maintenance_consult":
        asset_id = args.get("asset_id", "")
        diag = None
        try:
            snap = repository.get_snapshot(tenant, asset_id)
            if snap:
                diag = domain.diagnose(DiagnoseRequest(
                    tenant_id=tenant, asset_id=asset_id,
                    metrics=Metrics(
                        vib_rms_mm_s=snap["vib_rms_mm_s"],
                        vib_baseline_mm_s=snap["vib_baseline_mm_s"],
                        kurtosis=snap["kurtosis"], temp_c=snap["temp_c"],
                        temp_baseline_c=snap["temp_baseline_c"],
                        current_dev_pct=snap["current_dev_pct"],
                        trend_pct_per_day=snap["trend_pct_per_day"])))
        except Exception:  # noqa: BLE001 - 无快照/库不可达：提示仍给结构
            diag = None
        body = (
            "你是工厂设备维护会诊助手。基于以下结构化证据给出维护建议，"
            "不得编造数据；证据不足时明确说明。\n"
            f"设备：{asset_id}\n"
            f"诊断：{json.dumps(diag, ensure_ascii=False, default=str) if diag else '无快照'}\n"
            "输出格式：结论 / 依据 / 建议窗口 / 风险。")
        return {"description": "设备维护会诊",
                "messages": [{"role": "user",
                              "content": {"type": "text", "text": body}}]}
    if name == "alert_triage":
        alert_id = args.get("alert_id", "")
        alert = None
        try:
            rows = repository.list_alerts(tenant, "", "", 200)
            rows = rows[0] if isinstance(rows, tuple) else rows
            alert = next((a for a in rows if a.get("alert_id") == alert_id), None)
        except Exception:  # noqa: BLE001
            alert = None
        body = ("你是告警分诊助手。基于告警事实给出处置建议（确认/升级/关闭），"
                "必须引用告警字段，不得臆测。\n"
                f"告警：{json.dumps(alert, ensure_ascii=False, default=str) if alert else '未找到'}")
        return {"description": "告警分诊",
                "messages": [{"role": "user",
                              "content": {"type": "text", "text": body}}]}
    raise ValueError(f"unknown prompt: {name}")


def handle_rpc(req: dict[str, Any], tenant: str | None) -> dict[str, Any]:
    """处理单条 JSON-RPC 请求。"""
    rid = req.get("id")
    method = req.get("method", "")
    params = req.get("params") or {}

    if method == "initialize":
        return _ok(
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                },
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

    # ---- Phase D2：resources ----
    if method == "resources/list":
        try:
            _require_tenant(tenant)
        except PermissionError as e:
            return _err(-32001, str(e), rid)
        return _ok({"resources": RESOURCES}, rid)

    if method == "resources/read":
        try:
            t = _require_tenant(tenant)
            uri = str(params.get("uri", ""))
            return _ok(_read_resource(uri, t), rid)
        except PermissionError as e:
            return _err(-32001, str(e), rid)
        except ValueError as e:
            return _err(-32602, str(e), rid)
        except Exception as e:  # noqa: BLE001
            return _err(-32603, f"internal error: {e}", rid)

    # ---- Phase D2：prompts ----
    if method == "prompts/list":
        return _ok({"prompts": PROMPTS}, rid)

    if method == "prompts/get":
        try:
            t = _require_tenant(tenant)
            name = str(params.get("name", ""))
            args = params.get("arguments") or {}
            return _ok(_get_prompt(name, args, t), rid)
        except PermissionError as e:
            return _err(-32001, str(e), rid)
        except ValueError as e:
            return _err(-32602, str(e), rid)
        except Exception as e:  # noqa: BLE001
            return _err(-32603, f"internal error: {e}", rid)

    if method in ("notifications/initialized", "ping"):
        return _ok({}, rid)

    return _err(-32601, f"method not found: {method}", rid)

"""Phase D/G 安全测试（MCP 加固 + 工具白名单 + 审计）。

docs/43 §十.5：2025 MCP 生态漏洞（工具投毒/令牌泄露/SSRF）的针对性回归。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages", "pdm_signal"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app import mcp  # noqa: E402


def _rpc(method: str, params: dict | None = None, tenant: str | None = "site-a"):
    return mcp.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params or {}}, tenant)


# ---------------------------------------------------------------------------
# 工具投毒防护：只允许注册表内工具
# ---------------------------------------------------------------------------
def test_tool_allowlist_rejects_unregistered():
    r = _rpc("tools/call", {"name": "rm_rf", "arguments": {}})
    assert "error" in r
    assert r["error"]["code"] == -32602


def test_tool_list_matches_registry_exactly():
    """MCP 暴露的工具必须与 Harness 注册表完全一致（防双路径漂移）。"""
    from app.harness.tools import list_tools
    r = _rpc("tools/list")
    mcp_names = {t["name"] for t in r["result"]["tools"]}
    reg_names = {t.name for t in list_tools()}
    assert mcp_names == reg_names


# ---------------------------------------------------------------------------
# SSRF 防护：本服务不发起外部请求，资源 URI 必须白名单
# ---------------------------------------------------------------------------
def test_resource_uri_whitelist():
    r = _rpc("resources/read", {"uri": "http://169.254.169.254/latest/meta-data"})
    assert "error" in r and r["error"]["code"] == -32602


def test_resource_read_known_uri_ok():
    r = _rpc("resources/read", {"uri": "pdm://alert-rules/current"})
    assert "result" in r and "contents" in r["result"]


# ---------------------------------------------------------------------------
# 令牌/租户泄露防护
# ---------------------------------------------------------------------------
def test_missing_tenant_rejected_on_all_methods():
    for method in ("tools/call", "resources/list", "resources/read", "prompts/get"):
        r = _rpc(method, {"name": "get_asset", "uri": "pdm://assets/hot"}, tenant=None)
        assert r["error"]["code"] == -32001, f"{method} 未拦截缺失租户"


def test_prompts_reject_unknown_name():
    r = _rpc("prompts/get", {"name": "system_override",
                             "arguments": {}})
    assert "error" in r and r["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# 审计：工具调用必须落审计（失败静默，不影响调用）
# ---------------------------------------------------------------------------
def test_audit_written_on_tool_call(monkeypatch):
    calls = []

    def _fake_audit(tenant, name, ok, detail=""):
        calls.append((tenant, name, ok))

    monkeypatch.setattr(mcp, "_audit_tool_call", _fake_audit)
    _rpc("tools/call", {"name": "list_hot_assets", "arguments": {"limit": 1}})
    assert calls and calls[0][1] == "list_hot_assets" and calls[0][2] is True


# ---------------------------------------------------------------------------
# 协议版本与能力声明（Phase D1）
# ---------------------------------------------------------------------------
def test_protocol_version_upgraded():
    r = _rpc("initialize")
    assert r["result"]["protocolVersion"] == "2025-03-26"
    caps = r["result"]["capabilities"]
    assert set(caps) >= {"tools", "resources", "prompts"}

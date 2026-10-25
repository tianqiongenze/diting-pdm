"""Harness 运行时测试（Phase A，不依赖数据库的工具用假执行体）。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages", "pdm_signal"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.harness import eval as eval_mod
from app.harness import gate as gate_mod
from app.harness import hooks as hooks_mod
from app.harness import loop as loop_mod
from app.harness import otel as otel_mod
from app.harness import sandbox as sandbox_mod
from app.harness import tools as tools_mod


# ---------------------------------------------------------------------------
# 工具注册表
# ---------------------------------------------------------------------------
def test_tool_registry_shape():
    tools = tools_mod.list_tools()
    names = {t.name for t in tools}
    assert len(tools) == 12, f"期望 12 工具，实际 {len(tools)}"
    assert {"get_asset", "diagnose_asset", "consult_maintenance",
            "ack_alert", "submit_inspection"} <= names
    for t in tools:
        assert t.side_effect in ("read", "write", "critical")
        assert "type" in t.input_schema
        spec = t.public_spec()
        assert spec["name"] == t.name and "sideEffect" in spec


def test_execute_unknown_and_missing_args():
    import pytest
    with pytest.raises(KeyError):
        tools_mod.execute("no_such_tool", {})
    with pytest.raises(ValueError):
        tools_mod.execute("get_asset", {})          # 缺 asset_id


def test_execute_read_tool_degraded_without_db():
    """无数据库环境下 diagnose_asset 应降级返回而非崩溃。"""
    out = tools_mod.execute("diagnose_asset", {"asset_id": "NOPE-0001"})
    assert out.get("degraded") is True
    assert "reason" in out.get("degraded_reason", "") or out.get("degraded_reason")


# ---------------------------------------------------------------------------
# 权限门
# ---------------------------------------------------------------------------
def test_gate_read_allow():
    d = gate_mod.evaluate("read", "get_asset", {"asset_id": "A"})
    assert d.action == "allow"


def test_gate_write_ask():
    d = gate_mod.evaluate("write", "consult_maintenance", {"asset_id": "A"})
    assert d.action == "ask"


def test_gate_critical_close_alert():
    d = gate_mod.evaluate("critical", "ack_alert",
                          {"alert_id": "X", "action": "close"})
    assert d.action == "ask"
    # 6000 元/h × 4h = 24000 < 50000，不触发双人
    assert d.dual is False


def test_gate_dual_approval_threshold():
    d = gate_mod.evaluate("critical", "ack_alert",
                          {"alert_id": "X", "action": "close"},
                          downtime_cost_per_hour=20000.0)   # ×4h = 80000 > 50000
    assert d.dual is True and "双人复核" in d.reason


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------
def test_hooks_verify_consult_requires_card_id():
    assert hooks_mod.verify("consult_maintenance", {"card": {"card_id": "C1"}})
    assert not hooks_mod.verify("consult_maintenance", {"card": {}})


def test_stop_hook_rejects_failed_step():
    assert not hooks_mod.run_stop([{"ok": False, "tool": "x"}], "site-a")
    assert hooks_mod.run_stop([{"ok": True}], "site-a")


def test_custom_stop_hook_registration():
    calls = []
    hooks_mod.register_stop(lambda results, tenant: calls.append(tenant) or True)
    hooks_mod.run_stop([{"ok": True}], "site-x")
    assert calls == ["site-x"]


# ---------------------------------------------------------------------------
# LoopController
# ---------------------------------------------------------------------------
def _read_only_plan():
    return [loop_mod.Step(tool="get_asset", arguments={"asset_id": "A"},
                          rationale="读台账")]


def test_loop_pending_approval_for_write():
    plan = [loop_mod.Step(tool="consult_maintenance", arguments={"asset_id": "A"})]
    ctl = loop_mod.LoopController("site-a", plan, approvals={})
    out = ctl.run()
    assert out["status"] == "PENDING_APPROVAL"
    assert out["pending_step"] == 0
    assert "GATE" in out["trace"]["states"]


def test_loop_rejects_empty_and_too_long():
    import pytest
    with pytest.raises(loop_mod.LoopError):
        loop_mod.LoopController("site-a", []).run()
    long_plan = [loop_mod.Step(tool="get_asset", arguments={"asset_id": "A"})] * 9
    with pytest.raises(loop_mod.LoopError):
        loop_mod.LoopController("site-a", long_plan).run()


def test_loop_rejects_unknown_tool():
    import pytest
    with pytest.raises(loop_mod.LoopError):
        loop_mod.LoopController("site-a", [loop_mod.Step(tool="nope")]).run()


def test_loop_read_plan_completes_without_db():
    """get_asset 无库时返回 {}，verify 通过（read 工具无强校验）。"""
    ctl = loop_mod.LoopController("site-a", _read_only_plan())
    out = ctl.run()
    assert out["status"] == "DONE"
    states = out["trace"]["states"]
    assert states[0] == "IDLE" and states[-1] == "DONE"
    assert out["trace"]["turns"] == 1


def test_loop_approved_write_plan_runs():
    """批准后的写计划：consult_maintenance 无快照会失败 → OBSERVE 记录 ok=False
    → Stop Hook 拒绝 → ESCALATE（这正是防假完成的设计）。"""
    plan = [loop_mod.Step(tool="consult_maintenance", arguments={"asset_id": "A"})]
    ctl = loop_mod.LoopController("site-a", plan,
                                  approvals={"consult_maintenance": True})
    import pytest
    with pytest.raises(loop_mod.LoopError, match="stop hook"):
        ctl.run()


# ---------------------------------------------------------------------------
# OTel
# ---------------------------------------------------------------------------
def test_otel_span_layers_and_buffer():
    s = otel_mod.invoke_agent("site-a", "diagnose")
    otel_mod.span_event(s, "plan_ready", steps=1)
    c = otel_mod.chat("site-a", "rules-v0.2.0", tokens_in=10)
    otel_mod.end_span(c, **{"gen_ai.usage.output_tokens": 5})
    otel_mod.end_span(s, status="DONE")
    recent = otel_mod.recent(10)
    assert any(x["name"] == "invoke_agent" and x["attrs"]["intent"] == "diagnose"
               for x in recent)
    chat_span = next(x for x in recent if x["name"] == "chat")
    assert chat_span["attrs"]["gen_ai.request.model"] == "rules-v0.2.0"
    assert chat_span["attrs"]["gen_ai.usage.input_tokens"] == 10
    st = otel_mod.stats()
    assert st["buffered"] >= 2 and "invoke_agent" in st["by_name"]


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------
def test_sandbox_pure_function():
    out = sandbox_mod.run_pure(lambda x: x * 2, 21, timeout_s=5)
    assert out == 42


def test_sandbox_timeout():
    import pytest
    import time

    def _slow():
        time.sleep(3)
        return "late"

    with pytest.raises(RuntimeError, match="timeout"):
        sandbox_mod.run_pure(_slow, timeout_s=0.5)


def test_sandbox_error_propagates():
    import pytest
    with pytest.raises(RuntimeError):
        sandbox_mod.run_pure(lambda: 1 / 0, timeout_s=5)


# ---------------------------------------------------------------------------
# Eval
# ---------------------------------------------------------------------------
def test_eval_golden_cases_pass():
    out = eval_mod.run_cases()
    assert out["total"] == 4 and out["failed"] == 0, out["failures"]


def test_eval_detects_wrong_level():
    def _bad_runner(inp):
        return {"level": "green"}      # 故意全错
    out = eval_mod.run_cases(runner=_bad_runner)
    assert out["failed"] >= 3 and out["pass_rate"] < 0.5


def test_eval_detects_missing_interval():
    def _bad_wm(inp):
        return {"trajectory": [{"day": 0, "mean": 50.0}]}   # 缺 lo/hi
    out = eval_mod.run_cases(runner=_bad_wm)
    assert out["failed"] >= 1

"""决策会诊与 MCP 单元测试（不依赖数据库）。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import decision, mcp

DIAG = {
    "hi": 47.0,
    "level": "orange",
    "sla_hours": 72,
    "contributions": [
        {"name": "vib", "s": 0.6, "weight": 0.35, "impact": 21.0, "evidence": "振动 4.8mm/s"},
        {"name": "imp", "s": 0.5, "weight": 0.2, "impact": 10.0, "evidence": "峭度 5.2"},
    ],
    "hypotheses": [{"code": "BEARING_BPFO", "label": "外圈", "confidence": 0.78, "basis": "BPFO 3 阶"}],
    "evidence": [{"type": "metric", "name": "vib_rms_mm_s", "value": 4.8}],
    "degraded": False,
    "model_version": "rules-v0.2.0",
    "rul_p50_days": 19,
}

SNAP = {
    "vib_rms_mm_s": 4.8,
    "vib_baseline_mm_s": 2.0,
    "kurtosis": 5.2,
    "temp_c": 68.0,
    "temp_baseline_c": 55.0,
    "current_dev_pct": 6.0,
    "trend_pct_per_day": 1.2,
}

ASSET = {"asset_id": "KA-101", "criticality": "A", "category": "MOTOR"}


class TestRoles:
    def test_shopfloor_act_now_on_red(self):
        d = {**DIAG, "level": "red", "hi": 30.0}
        o = decision.role_shopfloor(d, SNAP)
        assert o.verdict == "ACT_NOW"
        assert o.urgency_hours == 4

    def test_shopfloor_plan_window_on_orange(self):
        o = decision.role_shopfloor(DIAG, SNAP)
        assert o.verdict == "PLAN_WINDOW"
        assert len(o.basis) >= 2

    def test_reliability_prioritises_critical_asset(self):
        o = decision.role_reliability(DIAG, ASSET, None)
        assert o.verdict == "ACT_NOW"
        assert "A 类" in o.constraints[0]

    def test_production_blocks_when_high_load(self):
        o = decision.role_production(DIAG, {"load_pct": 95, "bottleneck": "总装"})
        assert o.urgency_hours >= 96
        assert any("高负荷" in c for c in o.constraints)

    def test_maintenance_flags_missing_spare(self):
        o = decision.role_maintenance(DIAG, {"sku": "X", "qty": 0, "eta": "2026-11-05"},
                                      {"available": 3, "required": 2, "hours": 6})
        assert any("备件缺货" in c for c in o.constraints)

    def test_maintenance_short_rul_is_urgent(self):
        d = {**DIAG, "rul_p50_days": 3}
        o = decision.role_maintenance(d, {"qty": 1, "eta": None}, {"available": 3, "required": 2, "hours": 6})
        assert o.verdict == "ACT_NOW"


class TestEvaluator:
    def test_consistent_opinions_score_high(self):
        ops = [
            decision.RoleOpinion("shopfloor", "PLAN_WINDOW", 72, 0.8, ["a", "b"]),
            decision.RoleOpinion("reliability", "PLAN_WINDOW", 72, 0.8, ["a", "b"]),
            decision.RoleOpinion("production", "PLAN_WINDOW", 72, 0.7, ["a", "b"]),
            decision.RoleOpinion("maintenance", "PLAN_WINDOW", 72, 0.7, ["a", "b"]),
        ]
        ev = decision.evaluate(ops)
        assert ev.consistency == 1.0
        assert ev.conflicts == []

    def test_conflict_detected_between_act_now_and_monitor(self):
        ops = [
            decision.RoleOpinion("shopfloor", "ACT_NOW", 4, 0.8, ["a", "b"]),
            decision.RoleOpinion("reliability", "MONITOR", 720, 0.6, ["a", "b"]),
            decision.RoleOpinion("production", "PLAN_WINDOW", 168, 0.7, ["a", "b"]),
            decision.RoleOpinion("maintenance", "MONITOR", 720, 0.6, ["a", "b"]),
        ]
        ev = decision.evaluate(ops)
        assert ev.consistency < 0.6
        assert any("冲突" in c for c in ev.conflicts)
        assert ev.risk_note == "高"

    def test_evidence_gap_detected(self):
        ops = [decision.RoleOpinion("shopfloor", "PLAN_WINDOW", 72, 0.5, ["only-one"])]
        ev = decision.evaluate(ops)
        assert any("依据不足" in g for g in ev.evidence_gaps)

    def test_empty_opinions(self):
        ev = decision.evaluate([])
        assert ev.consistency == 0.0
        assert ev.risk_note == "无法会诊"


class TestAggregateVerdict:
    def test_true_tie_breaks_toward_urgency(self):
        """等权角色 2:2 平票时必须取更紧急的结论，且结果确定。"""
        ops = [
            decision.RoleOpinion("r1", "ACT_NOW", 4, 0.8, ["a", "b"]),
            decision.RoleOpinion("r2", "MONITOR", 720, 0.6, ["a", "b"]),
            decision.RoleOpinion("r3", "ACT_NOW", 8, 0.7, ["a", "b"]),
            decision.RoleOpinion("r4", "MONITOR", 720, 0.6, ["a", "b"]),
        ]
        results = {decision.aggregate_verdict(ops) for _ in range(20)}
        assert results == {"ACT_NOW"}

    def test_weighted_majority_wins(self):
        ops = [
            decision.RoleOpinion("shopfloor", "MONITOR", 720, 0.6, ["a", "b"]),
            decision.RoleOpinion("reliability", "PLAN_WINDOW", 72, 0.8, ["a", "b"]),
            decision.RoleOpinion("production", "PLAN_WINDOW", 72, 0.7, ["a", "b"]),
            decision.RoleOpinion("maintenance", "PLAN_WINDOW", 72, 0.7, ["a", "b"]),
        ]
        assert decision.aggregate_verdict(ops) == "PLAN_WINDOW"

    def test_empty(self):
        assert decision.aggregate_verdict([]) == "NO_ACTION"

    def test_red_level_never_yields_monitor(self):
        """领域不变量：红色设备不得被判为仅监测，必须升级。"""
        red_diag = {**DIAG, "level": "red", "hi": 20.0, "rul_p50_days": None}
        # 构造多数角色偏保守的场景
        ops = [
            decision.RoleOpinion("shopfloor", "MONITOR", 720, 0.6, ["a", "b"]),
            decision.RoleOpinion("reliability", "MONITOR", 720, 0.6, ["a", "b"]),
            decision.RoleOpinion("production", "PLAN_WINDOW", 168, 0.7, ["a", "b"]),
            decision.RoleOpinion("maintenance", "MONITOR", 720, 0.6, ["a", "b"]),
        ]
        ev = decision.evaluate(ops)
        card = decision.build_card(
            "site-a", "KA-101", red_diag, SNAP, ops, ev,
            {"sku": "X", "qty": 1, "eta": None},
            {"available": 3, "required": 2, "hours": 6},
            {"load_pct": 82},
        )
        assert card["verdict"] == "ACT_NOW"
        assert card["verdict_escalated"] is True


class TestCard:
    @staticmethod
    def _build(spare_eta=None, qty=1):
        ops = [
            decision.role_shopfloor(DIAG, SNAP),
            decision.role_reliability(DIAG, ASSET, None),
            decision.role_production(DIAG, {"load_pct": 82, "window_available": "周日 08:00"}),
            decision.role_maintenance(DIAG, {"sku": "SKF-6316", "qty": qty, "eta": spare_eta},
                                      {"available": 3, "required": 2, "hours": 6}),
        ]
        ev = decision.evaluate(ops)
        return decision.build_card("site-a", "KA-101", DIAG, SNAP, ops, ev,
                                   {"sku": "SKF-6316", "qty": qty, "eta": spare_eta},
                                   {"available": 3, "required": 2, "hours": 6},
                                   {"load_pct": 82, "bottleneck": "总装"})

    def test_card_has_required_fields(self):
        c = self._build()
        for k in ("card_id", "tenant_id", "asset_id", "diagnosis", "options",
                  "economics", "human_confirm", "disclaimer"):
            assert k in c, k
        assert c["human_confirm"]["status"] == "PENDING"

    def test_every_option_has_cost_breakdown(self):
        c = self._build()
        for o in c["options"]:
            for f in ("spare", "labor", "downtime", "total"):
                assert f in o["cost"]
            assert o["cost"]["total"] == pytest.approx(
                o["cost"]["spare"] + o["cost"]["labor"] + o["cost"]["downtime"], rel=1e-6
            )

    def test_window_respects_spare_eta(self):
        eta = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
        c = self._build(spare_eta=eta)
        first = c["options"][0]
        assert first["window_start"][:10] >= eta, "窗口不得早于备件到货日"

    def test_emergency_option_when_spare_late(self):
        eta = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")
        c = self._build(spare_eta=eta)
        assert len(c["options"]) >= 2
        assert any(o["option_id"].startswith("B") for o in c["options"])

    def test_disclaimer_present(self):
        assert "不构成维修指令" in self._build()["disclaimer"]

    def test_net_benefit_arithmetic(self):
        c = self._build()
        e = c["economics"]
        assert e["net_benefit"] == pytest.approx(e["avoided_loss"] - e["total_cost"], rel=1e-6)


class TestMcp:
    def test_initialize(self):
        r = mcp.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, "site-a")
        assert r["result"]["protocolVersion"]
        assert r["result"]["serverInfo"]["name"] == "diting-pdm"

    def test_tools_list_has_five(self):
        r = mcp.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "site-a")
        names = [t["name"] for t in r["result"]["tools"]]
        assert len(names) == 5
        assert "consult_maintenance" in names
        for t in r["result"]["tools"]:
            assert "inputSchema" in t and t["inputSchema"]["type"] == "object"

    def test_missing_tenant_denied(self):
        r = mcp.handle_rpc(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "get_asset", "arguments": {"asset_id": "X"}}},
            None,
        )
        assert r["error"]["code"] == -32001

    def test_unknown_method(self):
        r = mcp.handle_rpc({"jsonrpc": "2.0", "id": 4, "method": "foo/bar"}, "site-a")
        assert r["error"]["code"] == -32601

    def test_unknown_tool(self):
        r = mcp.handle_rpc(
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "nope", "arguments": {}}},
            "site-a",
        )
        assert r["error"]["code"] == -32602

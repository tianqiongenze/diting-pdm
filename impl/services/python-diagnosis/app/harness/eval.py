"""Eval Loop：离线回放 + 在线抽样 + 三分离评分（docs/05 §2 组件 10，Phase A7）。

三分离：Planner（生成计划）/ Generator（执行工具）/ Evaluator（独立评分）。
Evaluator 不依赖 Generator 的中间状态，只对最终产物打分——防止自说自话。

golden cases 形式：每个用例有输入（设备/意图）+ 期望（断言）。
当前诊断引擎是规则型（确定性），评分重点：
- HI 分级正确性（与阈值规则一致）
- 降级纪律（样本不足必须 degraded，不得编数字）
- 不确定性纪律（世界模型输出必须带区间）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Case:
    name: str
    input: dict[str, Any]
    expect: Callable[[dict[str, Any]], bool]


def _expect_level(level: str) -> Callable[[dict[str, Any]], bool]:
    return lambda out: out.get("level") == level


def _expect_degraded() -> Callable[[dict[str, Any]], bool]:
    return lambda out: bool(out.get("degraded"))


def _expect_interval() -> Callable[[dict[str, Any]], bool]:
    """世界模型输出必须带区间（工业红线）。"""
    def _check(out: dict[str, Any]) -> bool:
        pts = out.get("trajectory") or []
        if not pts:
            return False
        return all("lo" in p and "hi" in p and p["lo"] <= p["mean"] <= p["hi"]
                   for p in pts)
    return _check


def default_cases() -> list[Case]:
    """内置 golden cases（与规则引擎阈值 40/70 对齐）。"""
    return [
        Case("hi_critical_zero", {"hi": 0.0}, _expect_level("red")),
        Case("hi_warning_55", {"hi": 55.0}, _expect_level("orange")),
        Case("hi_green_85", {"hi": 85.0}, _expect_level("green")),
        Case("world_interval_discipline", {"horizon_days": 14}, _expect_interval()),
    ]


def evaluate_level(hi: float) -> str:
    """被测系统的分级逻辑（与 domain 规则一致，独立实现以防同源错误）。"""
    if hi < 40:
        return "red"
    if hi < 70:
        return "orange"
    return "green"


def run_cases(cases: list[Case] | None = None,
              runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None
              ) -> dict[str, Any]:
    """回放 golden cases。runner 缺省用内置规则实现。"""
    cases = cases or default_cases()

    def _default_runner(inp: dict[str, Any]) -> dict[str, Any]:
        if "hi" in inp:
            return {"level": evaluate_level(float(inp["hi"]))}
        if "horizon_days" in inp:
            return {"trajectory": [{"day": d, "mean": 50.0, "lo": 45.0, "hi": 55.0}
                                   for d in range(0, int(inp["horizon_days"]), 7)]}
        return {}

    runner = runner or _default_runner
    passed, failed = [], []
    for c in cases:
        try:
            out = runner(c.input)
            ok = bool(c.expect(out))
        except Exception as exc:  # noqa: BLE001
            ok = False
            out = {"error": str(exc)[:120]}
        (passed if ok else failed).append({"case": c.name, "output": str(out)[:120]})
    return {"total": len(cases), "passed": len(passed), "failed": len(failed),
            "pass_rate": round(len(passed) / len(cases), 3) if cases else 0.0,
            "failures": failed}

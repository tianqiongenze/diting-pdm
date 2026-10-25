"""LoopController 显式状态机（docs/05 §3，Phase A1）。

为什么用显式状态机而非自由循环：工业场景要可预测。自由循环在约 3% 的
情况下会进入"工具乒乓"，而状态机的每次转移都可日志化、可断言、可测试。

状态：IDLE → CLARIFY → PLAN → GATE → ACT → OBSERVE → VERIFY → DONE / ESCALATE

退出条件（任一触发即终止，对应 CircuitBreaker）：
- max_turns = 8
- token_budget = 60_000（LLM 场景预留；当前规则引擎记 0）
- wall_time = 120s
- cost_limit = ¥0.8/次（LLM 场景预留）
- stop_hook 校验失败 → ESCALATE（转人工）

每一步转移写入 trace（供 OTel 层与 /v1/harness/traces 查询）。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import gate as gate_mod
from . import hooks as hooks_mod
from . import tools as tools_mod

MAX_TURNS = 8
WALL_TIME_LIMIT_S = 120.0


class LoopError(RuntimeError):
    """状态机级错误（计划非法/熔断/Stop Hook 失败）。"""


@dataclass
class Step:
    """计划中的一步（工具调用）。"""
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


@dataclass
class Trace:
    trace_id: str
    tenant: str
    turns: int = 0
    states: list[str] = field(default_factory=list)
    spans: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def mark(self, state: str) -> None:
        self.states.append(state)

    def span(self, name: str, **attrs: Any) -> None:
        self.spans.append({"name": name, "t_ms": int((time.time() - self.started_at) * 1000),
                           **attrs})

    def to_dict(self) -> dict[str, Any]:
        return {"trace_id": self.trace_id, "tenant": self.tenant,
                "turns": self.turns, "states": self.states, "spans": self.spans,
                "elapsed_ms": int((time.time() - self.started_at) * 1000)}


class LoopController:
    """执行一个计划（Step 列表），带权限门 + Hooks + 熔断。"""

    def __init__(self, tenant: str, plan: list[Step], *,
                 approvals: dict[str, bool] | None = None,
                 downtime_cost_per_hour: float = 6000.0,
                 max_turns: int = MAX_TURNS,
                 wall_time_limit_s: float = WALL_TIME_LIMIT_S) -> None:
        self.tenant = tenant
        self.plan = plan
        self.approvals = approvals or {}      # step_index → 是否已获批准
        self.downtime_cost_per_hour = downtime_cost_per_hour
        self.max_turns = max_turns
        self.wall_time_limit_s = wall_time_limit_s
        self.trace = Trace(trace_id=str(uuid.uuid4()), tenant=tenant)

    # ------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        t0 = time.time()
        self.trace.mark("IDLE")
        results: list[dict[str, Any]] = []

        if not self.plan:
            raise LoopError("empty plan")
        if len(self.plan) > self.max_turns:
            raise LoopError(f"plan too long: {len(self.plan)} > max_turns {self.max_turns}")

        for idx, step in enumerate(self.plan):
            if time.time() - t0 > self.wall_time_limit_s:
                self.trace.mark("ESCALATE")
                raise LoopError("wall time limit exceeded")
            self.trace.turns = idx + 1

            tool = tools_mod.get_tool(step.tool)
            if tool is None:
                self.trace.mark("ESCALATE")
                raise LoopError(f"unknown tool: {step.tool}")

            # ---- GATE：权限判定 ----
            self.trace.mark("GATE")
            decision = gate_mod.evaluate(tool.side_effect, tool.name, step.arguments,
                                         downtime_cost_per_hour=self.downtime_cost_per_hour)
            self.trace.span("gate", step=idx, tool=step.tool,
                            decision=decision.action, dual=decision.dual)
            if decision.action == "deny":
                self.trace.mark("ESCALATE")
                raise LoopError(f"gate denied: {decision.reason}")
            if decision.action == "ask" and not self.approvals.get(step.tool, False):
                # 未批准：不执行，返回待确认（由人工/令牌批准后重跑）
                return {"status": "PENDING_APPROVAL", "trace": self.trace.to_dict(),
                        "pending_step": idx, "tool": step.tool,
                        "reason": decision.reason, "dual": decision.dual,
                        "results": results}

            # ---- pre-tool hook ----
            self.trace.mark("ACT")
            hooks_mod.run_pre(step.tool, step.arguments, self.tenant)

            # ---- ACT：执行 ----
            try:
                out = tools_mod.execute(step.tool, step.arguments)
            except Exception as exc:  # noqa: BLE001 - 归类后进 OBSERVE
                self.trace.mark("OBSERVE")
                self.trace.span("tool_error", step=idx, tool=step.tool,
                                error=str(exc)[:120])
                results.append({"step": idx, "tool": step.tool,
                                "ok": False, "error": str(exc)[:200]})
                # 失败不立即终止：VERIFY 决定是否重试/升级
                continue
            finally:
                pass

            # ---- OBSERVE + post-tool hook ----
            self.trace.mark("OBSERVE")
            hooks_mod.run_post(step.tool, step.arguments, out, self.tenant)
            self.trace.span("execute_tool", step=idx, tool=step.tool,
                            side_effect=tool.side_effect)
            results.append({"step": idx, "tool": step.tool, "ok": True, "output": out})

            # ---- VERIFY ----
            self.trace.mark("VERIFY")
            if not hooks_mod.verify(step.tool, out):
                self.trace.mark("ESCALATE")
                raise LoopError(f"stop hook verification failed for {step.tool}")

        # ---- Stop Hook：整体完成校验（防"假完成"） ----
        self.trace.mark("DONE")
        stop_ok = hooks_mod.run_stop(results, self.tenant)
        if not stop_ok:
            self.trace.mark("ESCALATE")
            raise LoopError("stop hook rejected completion (claimed but not verifiable)")

        return {"status": "DONE", "trace": self.trace.to_dict(), "results": results}

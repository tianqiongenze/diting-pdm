"""Harness 运行时（docs/05，Phase A）。

组件总览（与 docs/05 §2 十组件对应）：
1. Agentic Loop   → loop.py   LoopController 显式状态机
2. Tool Registry  → tools.py  12 工具，一份定义两处暴露（MCP 共用）
3. Sandbox        → sandbox.py 纯函数限额执行
4. Permission     → gate.py   allow/ask/deny + 双人复核阈值
5. Memory         → app/memory/（Phase B）
6. Hooks          → hooks.py  pre/post/stop 三类
7. Sub-agents     → decision.py 四角色 + Evaluator（已有）
8. Skills         → app/memory/skills.py（Phase B）
9. Observability  → otel.py   OTel GenAI 语义约定三层 span
10. Eval Loop     → eval.py   golden cases 回放 + 三分离评分
"""

from .loop import LoopController, LoopError, Step, Trace  # noqa: F401
from .tools import Tool, execute, get_tool, list_tools  # noqa: F401
from .gate import DUAL_APPROVAL_THRESHOLD, GateDecision, evaluate  # noqa: F401
from . import hooks, otel, sandbox, eval  # noqa: F401

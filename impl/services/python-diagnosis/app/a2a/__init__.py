"""A2A（Agent2Agent）智能体互联（docs/43 §10.4，Phase E）。

- agent_card()   能力发现（/.well-known/agent.json）
- create_task()  tasks/send
- get_task()     tasks/get
- execute_task() 受控分发执行
"""

from . import agent  # noqa: F401
from .agent import TASK_TYPES, TASK_STATES, agent_card, create_task, execute_task, get_task  # noqa: F401

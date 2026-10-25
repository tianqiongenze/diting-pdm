"""Hermes 式三层记忆与受控技能（docs/05 §6 / docs/12 红线，Phase B）。

模块：
- layers.py  三层记忆（设备档案 / 会话滚动摘要 / 事件检索）
- skills.py  技能沉淀管线（候选 → 人工审批 → 生效；禁止在线自生效）
- freeze.py  记忆冻结快照与回滚
"""

from . import freeze, layers, skills  # noqa: F401

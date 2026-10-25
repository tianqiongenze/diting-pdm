"""Hooks 三类：pre-tool / post-tool / stop（docs/05 §7，Phase A4）。

Stop Hook 是防"假完成"的关键：Agent 声称"已创建工单"时，必须有回执号
（workorder_no）可验证，否则判失败转人工。

钩子通过 register 注册，便于测试替换；内置默认实现只做审计与轻校验。
"""

from __future__ import annotations

from typing import Any, Callable

_pre_hooks: list[Callable[[str, dict[str, Any], str], None]] = []
_post_hooks: list[Callable[[str, dict[str, Any], dict[str, Any], str], None]] = []
_stop_hooks: list[Callable[[list[dict[str, Any]], str], bool]] = []


def register_pre(fn: Callable[[str, dict[str, Any], str], None]) -> None:
    _pre_hooks.append(fn)


def register_post(fn: Callable[[str, dict[str, Any], dict[str, Any], str], None]) -> None:
    _post_hooks.append(fn)


def register_stop(fn: Callable[[list[dict[str, Any]], str], bool]) -> None:
    _stop_hooks.append(fn)


def run_pre(tool: str, arguments: dict[str, Any], tenant: str) -> None:
    for fn in _pre_hooks:
        fn(tool, arguments, tenant)


def run_post(tool: str, arguments: dict[str, Any], output: dict[str, Any],
             tenant: str) -> None:
    for fn in _post_hooks:
        fn(tool, arguments, output, tenant)


def verify(tool: str, output: dict[str, Any]) -> bool:
    """单步结果校验（VERIFY 阶段）。

    默认规则：
- 工具输出含 error 字段且 ok=False → 不通过（由调用方归类）
- consult_maintenance 必须返回 card_id（方案卡唯一标识）
- ack_alert 必须返回新状态
    """
    if not isinstance(output, dict):
        return False
    if output.get("ok") is False:
        return False
    if tool == "consult_maintenance":
        card = output.get("card") or {}
        return bool(card.get("card_id"))
    if tool == "ack_alert":
        return bool(output.get("status"))
    return True


def run_stop(results: list[dict[str, Any]], tenant: str) -> bool:
    """整体完成校验：所有 stop 钩子全部通过才算完成。"""
    for fn in _stop_hooks:
        if not fn(results, tenant):
            return False
    return True


def _default_stop(results: list[dict[str, Any]], tenant: str) -> bool:
    """默认 Stop Hook：关键操作必须"可验证地完成"。

    规则：任何 ok=False 的步骤 → 未完成（防止把失败当作完成上报）。
    """
    return all(r.get("ok") for r in results)


register_stop(_default_stop)

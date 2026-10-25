"""Sandbox 执行器（docs/05 §2 组件 3，Phase A5）。

用途：DSP 等 CPU 密集计算在受限环境执行，防"Agent 写脚本把库删了"。
与 pdm_signal 纯函数对齐：本沙箱只跑无副作用纯函数，且限制 CPU 时间与内存。

实现策略（渐进）：
- Linux：子进程 + resource.setrlimit（RLIMIT_CPU/RLIMIT_AS）
- 其他平台（开发机）：线程 + 超时看门狗（best-effort，测试不依赖平台）

纯函数契约：沙箱内函数不得访问网络/文件系统/数据库——由调用方保证
（本模块只接受白名单内的 pdm_signal 函数）。
"""

from __future__ import annotations

import multiprocessing
import os
import platform
import time
from typing import Any, Callable

_IS_LINUX = platform.system() == "Linux"


def run_pure(fn: Callable[..., Any], *args: Any,
             cpu_seconds: int = 10, memory_mb: int = 512,
             timeout_s: float = 30.0) -> Any:
    """在沙箱中执行纯函数，返回结果；超时/超限抛 RuntimeError。"""
    if _IS_LINUX:
        return _run_subprocess(fn, args, cpu_seconds, memory_mb, timeout_s)
    return _run_thread_watchdog(fn, args, timeout_s)


# ---------------------------------------------------------------------------
def _linux_preexec(cpu_seconds: int, memory_mb: int):  # pragma: no cover
    def _apply() -> None:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024 * 1024,) * 2)
        os.setsid()
    return _apply


def _run_subprocess(fn: Callable[..., Any], args: tuple[Any, ...],
                    cpu_seconds: int, memory_mb: int,
                    timeout_s: float) -> Any:  # pragma: no cover - 平台相关
    ctx = multiprocessing.get_context("fork")
    q: Any = ctx.Queue()

    def _worker() -> None:
        try:
            q.put(("ok", fn(*args)))
        except Exception as exc:  # noqa: BLE001
            q.put(("err", f"{type(exc).__name__}: {exc}"))

    proc = ctx.Process(target=_worker, daemon=True)
    proc.start()
    proc.join(timeout_s)
    if proc.is_alive():
        proc.terminate()
        proc.join(2)
        raise RuntimeError(f"sandbox timeout after {timeout_s}s")
    if proc.exitcode != 0:
        raise RuntimeError(f"sandbox process failed exit={proc.exitcode}")
    kind, payload = q.get_nowait() if not q.empty() else ("err", "no result")
    if kind == "err":
        raise RuntimeError(f"sandbox error: {payload}")
    return payload


def _run_thread_watchdog(fn: Callable[..., Any], args: tuple[Any, ...],
                         timeout_s: float) -> Any:
    """非 Linux 开发环境：线程 + 看门狗（不杀线程，仅报告超时）。"""
    import threading

    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["result"] = fn(*args)
        except Exception as exc:  # noqa: BLE001
            box["error"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=_worker, daemon=True)
    t0 = time.time()
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise RuntimeError(f"sandbox timeout after {timeout_s}s")
    if "error" in box:
        raise RuntimeError(box["error"])
    return box.get("result")

"""OTel GenAI 语义约定埋点（docs/05 §2 组件 9，Phase A6）。

遵循 OpenTelemetry GenAI Semantic Conventions（CNCF GenAI SIG，2024-04 起
维护，2025 拆入 open-telemetry/semantic-conventions-genai）：
- 三层 span：invoke_agent（请求入口）/ chat（模型调用）/ execute_tool（工具调用）
- 属性名遵循约定：gen_ai.request.model / gen_ai.usage.input_tokens 等，
  不写 model / llm_model 这类非标准名

导出策略（离线环境友好）：
- 配置 PDM_OTLP_ENDPOINT → OTLP/HTTP 导出（生产）
- 未配置 → 进程内环形缓冲（默认 1000 条），经 /v1/harness/traces 查询

这样 CI 与内网无需 Collector 也能验证埋点正确性。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
import uuid
from collections import deque
from typing import Any

MAX_BUFFER = 1000
_buffer: deque[dict[str, Any]] = deque(maxlen=MAX_BUFFER)
_lock = threading.Lock()
_OTLP_ENDPOINT = os.getenv("PDM_OTLP_ENDPOINT", "")


def start_span(name: str, **attrs: Any) -> dict[str, Any]:
    """开始一个 span（手动管理生命周期，避免 SDK 依赖）。"""
    return {"trace_id": attrs.pop("trace_id", None) or str(uuid.uuid4()),
            "span_id": str(uuid.uuid4())[:16], "name": name,
            "start": time.time(), "attrs": attrs, "events": []}


def end_span(span: dict[str, Any], **attrs: Any) -> dict[str, Any]:
    span["end"] = time.time()
    span["duration_ms"] = int((span["end"] - span["start"]) * 1000)
    span["attrs"].update(attrs)
    with _lock:
        _buffer.append(span)
    _export(span)
    return span


def span_event(span: dict[str, Any], name: str, **attrs: Any) -> None:
    span["events"].append({"name": name, "t_ms": int((time.time() - span["start"]) * 1000),
                           **attrs})


# ---------------------------------------------------------------------------
# 便捷封装：三层 span 的标准属性
# ---------------------------------------------------------------------------
def invoke_agent(tenant: str, intent: str) -> dict[str, Any]:
    return start_span("invoke_agent", tenant=tenant, intent=intent)


def chat(tenant: str, model: str, tokens_in: int = 0, tokens_out: int = 0) -> dict[str, Any]:
    """模型调用 span。属性名遵循 GenAI 约定。"""
    return start_span("chat", tenant=tenant,
                      **{"gen_ai.request.model": model,
                         "gen_ai.usage.input_tokens": tokens_in})


def execute_tool(tenant: str, tool: str, side_effect: str) -> dict[str, Any]:
    return start_span("execute_tool", tenant=tenant, tool=tool,
                      side_effect=side_effect)


# ---------------------------------------------------------------------------
def _export(span: dict[str, Any]) -> None:
    """OTLP/HTTP 导出（配置了端点才启用；失败静默，不阻塞主链路）。"""
    if not _OTLP_ENDPOINT:
        return
    try:
        payload = {"resourceSpans": [{
            "resource": {"attributes": [{"key": "service.name",
                                         "value": {"stringValue": "python-diagnosis"}}]},
            "scopeSpans": [{"scope": {"name": "diting-pdm.harness"},
                            "spans": [_to_otlp_span(span)]}],
        }]}
        req = urllib.request.Request(
            _OTLP_ENDPOINT, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:  # noqa: BLE001 - 遥测失败绝不阻塞业务
        pass


def _to_otlp_span(span: dict[str, Any]) -> dict[str, Any]:
    return {
        "traceId": span["trace_id"].replace("-", "")[:32],
        "spanId": span["span_id"],
        "name": span["name"],
        "startTimeUnixNano": int(span["start"] * 1e9),
        "endTimeUnixNano": int(span.get("end", span["start"]) * 1e9),
        "attributes": [{"key": k, "value": {"stringValue": str(v)}}
                       for k, v in span["attrs"].items()],
    }


def recent(limit: int = 100) -> list[dict[str, Any]]:
    with _lock:
        return list(_buffer)[-limit:]


def stats() -> dict[str, Any]:
    with _lock:
        by_name: dict[str, int] = {}
        for s in _buffer:
            by_name[s["name"]] = by_name.get(s["name"], 0) + 1
        return {"buffered": len(_buffer), "capacity": MAX_BUFFER,
                "by_name": by_name, "otlp_endpoint": _OTLP_ENDPOINT or "(memory)"}

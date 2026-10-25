"""Phase E A2A 测试（Agent Card / 任务状态机 / 供应商上报）。"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages", "pdm_signal"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

SQLITE_PATH = "/tmp/pdm_a2a_test.db"
SCHEMA = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                      "db", "sqlite", "001_schema.sql")

_SAVED_ENV = {k: os.environ.get(k) for k in ("PDM_DB_BACKEND", "PDM_SQLITE_PATH")}


@pytest.fixture(autouse=True)
def _isolate_backend():
    """切 sqlite 后端：设 env + 仅 reload app.repository（不删全部 app 模块，
    避免污染同进程其他测试文件——test_memory 事故教训）。"""
    import importlib

    from app import repository as _repo_mod
    for suffix in ("", "-wal", "-shm"):
        p = pathlib.Path(SQLITE_PATH + suffix)
        if p.exists():
            p.unlink()
    os.environ["PDM_DB_BACKEND"] = "sqlite"
    os.environ["PDM_SQLITE_PATH"] = SQLITE_PATH
    importlib.reload(_repo_mod)
    conn = _repo_mod._SQLiteConn(SQLITE_PATH)
    with open(SCHEMA, encoding="utf-8") as fh:
        conn._conn.executescript(fh.read())
    conn.close()
    yield
    for k, v in _SAVED_ENV.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    importlib.reload(_repo_mod)


def test_agent_card_shape():
    from app.a2a import agent_card
    card = agent_card(base_url="http://pdm.example:8000", tenant="site-a")
    assert card["name"] == "diting-pdm-agent"
    assert card["protocolVersion"]
    skill_ids = {s["id"] for s in card["skills"]}
    assert {"diagnose", "consult", "counterfactual"} <= skill_ids
    assert "diagnose_request" in card["taskTypes"]


def test_task_whitelist_rejects_unknown_type():
    from app.a2a import create_task
    with pytest.raises(ValueError, match="unsupported task_type"):
        create_task("site-a", "arbitrary_code_exec", {})


def test_diagnose_request_task_lifecycle():
    from app.a2a import create_task, execute_task, get_task
    t = create_task("site-a", "diagnose_request", {"asset_id": "FAN-00004"},
                    requester="agent:site-b")
    assert t["state"] == "SUBMITTED"
    got = get_task("site-a", t["task_id"])
    assert got is not None and got["state"] == "SUBMITTED"

    out = execute_task("site-a", t["task_id"])
    # 无快照时 diagnose_asset 降级返回 degraded（不抛）→ 任务 COMPLETED
    assert out["state"] == "COMPLETED"
    result = out["result"]
    assert result.get("degraded") is True

    # 重复执行被拒（状态机守卫）
    with pytest.raises(ValueError, match="cannot execute"):
        execute_task("site-a", t["task_id"])


def test_quality_report_supplier_domain():
    """FR-907：供应商上报 → 关联来料批次 → 通知 SQE。"""
    from app.a2a import create_task, execute_task
    from app import repository

    t = create_task("supplier-t1-001", "quality_report",
                    {"batch_no": "BATCH-2026-001", "supplier_code": "T1-BATT",
                     "asset_id": "PACK-0001", "abnormal_type": "电压波动",
                     "detail": {"cv_pct": 3.2}},
                    requester="agent:supplier-t1-001")
    out = execute_task("supplier-t1-001", t["task_id"])
    assert out["state"] == "COMPLETED"
    assert out["result"]["linked_batch"] == "BATCH-2026-001"
    assert out["result"]["notify"] == "SQE"

    # 落库可查
    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT report_id, batch_no FROM quality_report WHERE tenant_id=%s",
                    ("supplier-t1-001",))
        row = cur.fetchone()
    assert row is not None and row["batch_no"] == "BATCH-2026-001"


def test_alert_escalation_task():
    from app.a2a import create_task, execute_task
    t = create_task("site-a", "alert_escalation",
                    {"alert_id": "ALT-20261024-001"}, requester="agent:site-a")
    out = execute_task("site-a", t["task_id"])
    assert out["state"] == "COMPLETED"
    assert out["result"]["escalated"] is True


def test_task_tenant_isolation():
    """任务按租户隔离：site-a 读不到 supplier 的任务。"""
    from app.a2a import create_task, get_task
    t = create_task("supplier-t1-001", "alert_escalation", {"alert_id": "X"},
                    requester="agent:supplier")
    assert get_task("site-a", t["task_id"]) is None
    assert get_task("supplier-t1-001", t["task_id"]) is not None

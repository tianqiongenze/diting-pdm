"""Phase B 记忆与技能测试（SQLite 后端，验证工业红线）。

注意：本文件会把仓库层切到 sqlite 后端。为不污染同进程内的其他测试
（test_server 等依赖 mysql 路径的 monkeypatch 会在 fixture 内恢复），
_fresh_db 用 pytest 的 monkeypatch 语义：设置 env + 清理 app 模块缓存，
并在最后一个测试后由 _restore_backend 还原。
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages", "pdm_signal"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

SQLITE_PATH = "/tmp/pdm_mem_test.db"
SCHEMA = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                      "db", "sqlite", "001_schema.sql")

_SAVED_ENV = {k: os.environ.get(k) for k in ("PDM_DB_BACKEND", "PDM_SQLITE_PATH")}


@pytest.fixture(autouse=True)
def _isolate_backend():
    """切 sqlite 后端：设 env + 仅 reload app.repository（DB_BACKEND 在
    模块加载时求值），结束后还原 env 并再次 reload 回 mysql 路径。

    刻意不删全部 app 模块——那会破坏同进程内其他测试文件已绑定的
    app.server.app 对象引用（曾造成 11 个 server 测试 422 的污染事故）。
    """
    import importlib

    from app import repository as _repo_mod
    for suffix in ("", "-wal", "-shm"):
        p = pathlib.Path(SQLITE_PATH + suffix)
        if p.exists():
            p.unlink()
    os.environ["PDM_DB_BACKEND"] = "sqlite"
    os.environ["PDM_SQLITE_PATH"] = SQLITE_PATH
    importlib.reload(_repo_mod)
    assert _repo_mod.DB_BACKEND == "sqlite"
    yield
    # 还原 env 并 reload 回 mysql 路径
    for k, v in _SAVED_ENV.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    importlib.reload(_repo_mod)


def _fresh_db():
    from app import repository
    with open(SCHEMA, encoding="utf-8") as fh:
        ddl = fh.read()
    conn = repository._SQLiteConn(SQLITE_PATH)
    conn._conn.executescript(ddl)
    conn.close()
    return repository


def test_session_rolling_summary():
    repo = _fresh_db()
    from app import memory

    out = memory.layers.roll_summary("site-a", "S1", "第一轮：诊断 FAN-00004")
    assert out["turns"] == 1
    out2 = memory.layers.roll_summary("site-a", "S1", "第二轮：确认告警")
    assert out2["turns"] == 2
    assert "第一轮" in out2["summary"] and "第二轮" in out2["summary"]


def test_session_summary_truncation():
    repo = _fresh_db()
    from app import memory

    big = "x" * 5000
    out = memory.layers.roll_summary("site-a", "S2", big)
    assert len(out["summary"]) <= memory.layers.SESSION_SUMMARY_MAX_CHARS + 20
    assert "截断" in out["summary"]


def test_recall_without_crash():
    repo = _fresh_db()
    from app import memory

    # 空库检索不报错
    out = memory.layers.recall("site-a", "轴承")
    assert isinstance(out, list)


def test_skill_lifecycle_and_redline():
    """红线：候选技能不能直接生效，必须人工审批。"""
    repo = _fresh_db()
    from app import memory

    # 1) 提议 → PENDING
    sk = memory.skills.propose("site-a", "轴承异响处置", "先查润滑，再测振动",
                               source_case_id="KC-001", proposed_by="agent")
    assert sk["status"] == "PENDING"

    # 2) PENDING 技能不在可加载列表（禁止在线自生效）
    active = memory.skills.load_active("site-a")
    assert active == []

    # 3) 审批 → APPROVED → 可加载
    ok = memory.skills.approve("site-a", sk["skill_id"], "manager-zhang")
    assert ok["status"] == "APPROVED" and ok["approved_by"] == "manager-zhang"
    active = memory.skills.load_active("site-a")
    assert len(active) == 1 and active[0]["skill_id"] == sk["skill_id"]

    # 4) 重复审批被拒
    import pytest
    with pytest.raises(ValueError, match="only PENDING"):
        memory.skills.approve("site-a", sk["skill_id"], "manager-zhang")


def test_skill_reject_and_rollback():
    repo = _fresh_db()
    from app import memory

    import pytest
    sk = memory.skills.propose("site-a", "错误技能", "内容")
    memory.skills.reject("site-a", sk["skill_id"], "manager-zhang")
    with pytest.raises(ValueError, match="only PENDING"):
        memory.skills.approve("site-a", sk["skill_id"], "manager-zhang")

    sk2 = memory.skills.propose("site-a", "可回滚技能", "内容")
    memory.skills.approve("site-a", sk2["skill_id"], "manager-zhang")
    rb = memory.skills.rollback("site-a", sk2["skill_id"], "manager-zhang")
    assert rb["status"] == "SUPERSEDED"
    assert memory.skills.load_active("site-a") == []


def test_skill_invalid_status_filter():
    repo = _fresh_db()
    from app import memory
    import pytest
    with pytest.raises(ValueError, match="invalid status"):
        memory.skills.list_skills("site-a", status="BOGUS")


def test_freeze_snapshot_and_restore():
    repo = _fresh_db()
    from app import memory

    # 建两个技能：一个批准、一个待审
    sk1 = memory.skills.propose("site-a", "技能A", "A内容")
    memory.skills.approve("site-a", sk1["skill_id"], "mgr")
    memory.skills.propose("site-a", "技能B", "B内容")

    snap = memory.freeze.create_snapshot("site-a", note="变更前快照")
    assert snap["skills"] == 2

    # 快照后：批准 B、回滚 A
    sk2 = memory.skills.list_skills("site-a", status="PENDING")[0]
    memory.skills.approve("site-a", sk2["skill_id"], "mgr")
    memory.skills.rollback("site-a", sk1["skill_id"], "mgr")
    assert len(memory.skills.load_active("site-a")) == 1

    # 恢复快照 → 回到"1 批准 + 1 待审"
    out = memory.freeze.restore("site-a", snap["freeze_id"], "mgr")
    assert out["restored_skills"] == 2
    active = memory.skills.load_active("site-a")
    assert len(active) == 1 and active[0]["skill_id"] == sk1["skill_id"]

    snaps = memory.freeze.list_snapshots("site-a")
    assert len(snaps) == 1

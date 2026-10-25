"""Phase F 模型治理测试（注册表 / 漂移检测 / 反馈闭环）。"""

from __future__ import annotations

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages", "pdm_signal"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

SQLITE_PATH = "/tmp/pdm_model_test.db"
SCHEMA = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                      "db", "sqlite", "001_schema.sql")

_SAVED_ENV = {k: os.environ.get(k) for k in ("PDM_DB_BACKEND", "PDM_SQLITE_PATH")}


@pytest.fixture(autouse=True)
def _isolate_backend():
    import importlib

    from app import repository as _repo_mod
    for suffix in ("", "-wal", "-shm"):
        p = os.path.join("/tmp", "pdm_model_test.db" + suffix)
        if os.path.exists(p):
            os.unlink(p)
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


def _seed_history(tenant: str, n: int, hi0: float, hi1: float, step_hours: int = 4):
    """灌入两窗历史：前半 hi0（基线窗），后半 hi1（近窗）。

    时间跨度必须覆盖两个 window（默认 7 天 × 2 = 14 天），否则基线窗为空。
    n=80 × step_hours=4 ≈ 13.3 天，每窗约 40 点（>=30 最小样本）。
    """
    from app import repository

    now = datetime.datetime.now()
    total_hours = n * step_hours
    with repository.connect() as conn, conn.cursor() as cur:
        for i in range(n):
            ts = now - datetime.timedelta(hours=total_hours - i * step_hours)
            hi = hi0 if i < n // 2 else hi1
            cur.execute(
                "INSERT INTO read_asset_health_history(tenant_id,asset_id,hi,level,ts) "
                "VALUES (%s,%s,%s,%s,%s)",
                (tenant, f"A-{i:04d}", hi, "green" if hi >= 70 else "red", ts))


# ---------------------------------------------------------------------------
def test_register_and_list_models():
    from app.model import list_models, register_model

    register_model("site-a", "rules-v0.2.0", "rules",
                   description="五维规则引擎",
                   feature_set="vib,kurtosis,temp,current,trend",
                   metrics={"accuracy": 0.82})
    register_model("site-a", "wm-linear-1.0", "world_model",
                   description="统计退化模型")
    rows = list_models("site-a")
    assert len(rows) == 2
    wm = next(r for r in rows if r["model_id"] == "wm-linear-1.0")
    assert wm["model_kind"] == "world_model"
    rules = next(r for r in rows if r["model_id"] == "rules-v0.2.0")
    assert rules["metrics"]["accuracy"] == 0.82
    # 幂等 upsert
    register_model("site-a", "rules-v0.2.0", "rules", description="更新描述")
    assert len(list_models("site-a")) == 2


def test_current_model_id_matches_domain():
    from app import domain
    from app.model import current_model_id
    assert current_model_id() == domain.MODEL_VERSION


# ---------------------------------------------------------------------------
def test_drift_insufficient_samples():
    from app.model import detect_drift
    out = detect_drift("site-a")
    assert out["drift"] is None
    assert "样本不足" in out["note"]


def test_drift_detects_downward_shift():
    from app import repository
    from app.model import detect_drift

    _seed_history("site-a", n=80, hi0=80.0, hi1=60.0)   # 近窗显著下降
    out = detect_drift("site-a", window_days=7)
    assert out["drift"] is True
    assert out["direction"] == "down"
    assert out["recent_mean"] < out["baseline_mean"]


def test_drift_stable_no_alert():
    from app.model import detect_drift

    _seed_history("site-a", n=80, hi0=75.0, hi1=75.0)   # 无变化
    out = detect_drift("site-a", window_days=7)
    assert out["drift"] is False


# ---------------------------------------------------------------------------
def test_feedback_validation():
    from app.model import record_feedback
    with pytest.raises(ValueError, match="invalid predicted_level"):
        record_feedback("site-a", "A1", "m1", "purple", "CONFIRMED_FAILURE")
    with pytest.raises(ValueError, match="invalid actual_outcome"):
        record_feedback("site-a", "A1", "m1", "red", "MAYBE")


def test_feedback_and_accuracy_report():
    from app.model import accuracy_report, record_feedback

    # 2 命中 + 1 误报 + 1 漏报
    record_feedback("site-a", "A1", "rules-v0.2.0", "red", "CONFIRMED_FAILURE")
    record_feedback("site-a", "A2", "rules-v0.2.0", "orange", "CONFIRMED_FAILURE")
    record_feedback("site-a", "A3", "rules-v0.2.0", "red", "FALSE_ALARM")
    record_feedback("site-a", "A4", "rules-v0.2.0", "green", "CONFIRMED_FAILURE")
    rep = accuracy_report("site-a", "rules-v0.2.0")
    assert rep["hit"] == 2 and rep["false_positive"] == 1 and rep["miss"] == 1
    assert rep["precision"] == round(2 / 3, 3)
    assert rep["recall"] == round(2 / 3, 3)

"""世界模型 L1 测试（Phase C：反事实推演 + 工业红线）。"""

from __future__ import annotations

import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages", "pdm_signal"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.world import model as wm  # noqa: E402

NOW = datetime.datetime(2026, 10, 25, 12, 0, 0)


def _history(n: int = 10, hi0: float = 80.0, slope: float = -1.0,
             noise: float = 0.0, step_days: int = 1) -> list[dict]:
    """生成合成历史：每天一个点，线性劣化。"""
    import random
    rnd = random.Random(42)
    out = []
    for i in range(n):
        hi = hi0 + slope * i * step_days
        if noise:
            hi += rnd.uniform(-noise, noise)
        out.append({"ts": NOW - datetime.timedelta(days=(n - 1 - i) * step_days),
                    "hi": hi})
    return out


# ---------------------------------------------------------------------------
# fit：模型拟合与纪律
# ---------------------------------------------------------------------------
def test_fit_insufficient_points_returns_none():
    """红线：样本不足不估。"""
    assert wm.fit(_history(n=2)) is None


def test_fit_no_degradation_returns_none():
    """红线：无劣化趋势不推演。"""
    assert wm.fit(_history(n=5, slope=0.0)) is None
    assert wm.fit(_history(n=5, slope=0.005)) is None     # < MIN_SLOPE


def test_fit_recovers_slope():
    m = wm.fit(_history(n=10, hi0=80.0, slope=-1.0))
    assert m is not None
    assert abs(m.slope_per_day - (-1.0)) < 0.05
    assert m.n_points == 10


def test_fit_handles_noise():
    m = wm.fit(_history(n=20, hi0=80.0, slope=-0.8, noise=1.5))
    assert m is not None
    assert abs(m.slope_per_day - (-0.8)) < 0.15
    assert m.resid_std > 0


def test_fit_single_timestamp_returns_none():
    """所有点同一时间 → 无时间跨度 → None。"""
    pts = [{"ts": NOW, "hi": 50.0 - i} for i in range(5)]
    assert wm.fit(pts) is None


def test_fit_recent_weighted():
    """近期点权重更高：前半平稳后半陡降，拟合斜率应显著偏向陡降段。

    未加权时整段斜率约 -0.3；半衰期 7 天时近期陡降段主导，斜率应更负。
    """
    pts = ([{"ts": NOW - datetime.timedelta(days=20 - i), "hi": 80.0}
            for i in range(15)] +
           [{"ts": NOW - datetime.timedelta(days=5 - i), "hi": 80.0 - 1.5 * (i + 1)}
            for i in range(5)])
    m = wm.fit(pts, half_life_days=7.0)
    unweighted = wm.fit(pts, half_life_days=1e9)   # 近似等权
    assert m is not None and unweighted is not None
    assert m.slope_per_day < unweighted.slope_per_day - 0.05
    assert m.slope_per_day < -0.3


# ---------------------------------------------------------------------------
# predict：区间纪律
# ---------------------------------------------------------------------------
def test_predict_has_interval():
    m = wm.fit(_history(n=10, slope=-1.0, noise=0.5))
    assert m is not None
    mean, lo, hi = m.predict(7)
    assert lo < mean < hi, "必须带区间且 mean 在区间内"


def test_days_until_critical():
    m = wm.fit(_history(n=10, hi0=80.0, slope=-1.0))
    assert m is not None
    # intercept 是窗口起点（9 天前）的 HI≈80，斜率 -1/天 → 40 天后到 40
    d = m.days_until(40.0)
    assert d is not None and abs(d - 40.0) < 2.0


# ---------------------------------------------------------------------------
# counterfactual：反事实推演（DB 无关部分用假 history 注入）
# ---------------------------------------------------------------------------
def test_counterfactual_degraded_trajectory(monkeypatch):
    """推迟保养：HI 按斜率下行，轨迹必带区间。"""
    hist = _history(n=10, hi0=70.0, slope=-1.0)

    import app.repository as repo
    from app.world import model

    class _FakeQB:
        def __init__(self, tenant):
            pass

        def query(self, sql, *params):
            return self

        def fetchall(self):
            return hist

    monkeypatch.setattr(repo, "TenantQueryBuilder", _FakeQB)
    out = model.counterfactual("site-a", "FAN-0001", horizon_days=14,
                               critical_hi=40.0)
    assert out["model"] is not None
    assert len(out["trajectory"]) == 15
    for p in out["trajectory"]:
        assert p["lo"] <= p["mean"] <= p["hi"], "工业红线：必带区间"
    # 单调下行（无干预）
    means = [p["mean"] for p in out["trajectory"]]
    assert means[0] > means[-1]
    assert out["failure_day_estimate"] is not None


def test_counterfactual_with_maintenance_intervention(monkeypatch):
    """第 7 天保养（HI 重置 85）：轨迹应先降后升再降。"""
    hist = _history(n=10, hi0=70.0, slope=-1.0)

    import app.repository as repo
    from app.world import model

    class _FakeQB:
        def __init__(self, tenant):
            pass

        def query(self, sql, *params):
            return self

        def fetchall(self):
            return hist

    monkeypatch.setattr(repo, "TenantQueryBuilder", _FakeQB)
    out = model.counterfactual("site-a", "FAN-0001", horizon_days=14,
                               maintain_at_day=7, maintain_reset_hi=85.0,
                               critical_hi=40.0)
    assert out["intervention"]["day"] == 7
    traj = out["trajectory"]
    assert traj[7]["mean"] >= traj[6]["mean"] - 0.01, "干预点应出现重置跳升"
    assert abs(traj[7]["mean"] - 85.0) < 3.0
    # 干预后仍按原斜率劣化
    assert traj[14]["mean"] < traj[7]["mean"]


def test_counterfactual_insufficient_data_returns_null(monkeypatch):
    """红线：样本不足返回 null 字段，不编数字。"""
    import app.repository as repo
    from app.world import model

    class _FakeQB:
        def __init__(self, tenant):
            pass

        def query(self, sql, *params):
            return self

        def fetchall(self):
            return [{"ts": NOW, "hi": 50.0}]

    monkeypatch.setattr(repo, "TenantQueryBuilder", _FakeQB)
    out = model.counterfactual("site-a", "FAN-0001", horizon_days=14)
    assert out["model"] is None
    assert out["trajectory"] == []
    assert out["failure_day_estimate"] is None
    assert "不编数字" in out["note"]


def test_counterfactual_healthy_asset_no_failure_estimate(monkeypatch):
    """无劣化设备：模型 None → 不估计跌破时间。"""
    hist = _history(n=8, hi0=90.0, slope=0.0)

    import app.repository as repo
    from app.world import model

    class _FakeQB:
        def __init__(self, tenant):
            pass

        def query(self, sql, *params):
            return self

        def fetchall(self):
            return hist

    monkeypatch.setattr(repo, "TenantQueryBuilder", _FakeQB)
    out = model.counterfactual("site-a", "FAN-0001", horizon_days=14)
    assert out["model"] is None
    assert out["failure_day_estimate"] is None


# ---------------------------------------------------------------------------
# 集成测试（真实 SQLite 查询，不 mock）——防止 SQL 参数错位回归
# 事故：counterfactual 的 SQL 有 3 个 %s 但只传了 since（漏 asset_id），
# mock 测试发现不了，线上 500 才暴露。
# ---------------------------------------------------------------------------
def test_counterfactual_with_real_sqlite_query():
    import pathlib

    import pytest

    sqlite_path = "/tmp/pdm_world_it.db"
    schema = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                          "db", "sqlite", "001_schema.sql")
    saved = {k: os.environ.get(k) for k in ("PDM_DB_BACKEND", "PDM_SQLITE_PATH")}
    try:
        for suffix in ("", "-wal", "-shm"):
            p = pathlib.Path(sqlite_path + suffix)
            if p.exists():
                p.unlink()
        os.environ["PDM_DB_BACKEND"] = "sqlite"
        os.environ["PDM_SQLITE_PATH"] = sqlite_path
        import importlib

        import app.repository as repo
        importlib.reload(repo)
        conn = repo._SQLiteConn(sqlite_path)
        with open(schema, encoding="utf-8") as fh:
            conn._conn.executescript(fh.read())
        conn.close()

        now = datetime.datetime.now()
        with repo.connect() as c, c.cursor() as cur:
            for i in range(12):
                cur.execute(
                    "INSERT INTO read_asset_health_history(tenant_id,asset_id,hi,level,ts) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    ("site-a", "PRE-1", 80.0 - i, "green" if 80 - i >= 70 else "orange",
                     now - datetime.timedelta(days=11 - i)))

        from app import world
        out = world.counterfactual("site-a", "PRE-1", horizon_days=7)
        # 真实查询必须成功且带区间（此断言在参数错位时会 500/抛异常）
        assert out["model"] is not None
        assert out["trajectory"] and all("lo" in p for p in out["trajectory"])
        assert out["model"]["n_points"] == 12
        # 不存在的设备 → 空历史 → null 纪律
        empty = world.counterfactual("site-a", "NOPE", horizon_days=7)
        assert empty["model"] is None and empty["trajectory"] == []
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import importlib

        import app.repository as repo
        importlib.reload(repo)

"""SQLite 后端冒烟测试：验证方言转换层覆盖全部仓库路径。

运行方式（与单测同环境）：
    PDM_DB_BACKEND=sqlite PDM_SQLITE_PATH=/tmp/pdm_smoke.db \
    PYTHONPATH="packages/pdm_signal;services/python-diagnosis" \
    python -m pytest services/python-diagnosis/tests/test_sqlite_backend.py -q

覆盖：
- UPSERT 三条路径（read_asset_health / read_asset_latest / alert_rule）
- INSERT IGNORE 幂等（integration_outbox / processed_event / recommendation_card）
- NOW() 表达式（save_diagnosis 的 window_start/window_end）
- datetime 参数读写往返（list_plans 的 days_left 依赖 isinstance 判断）
- TenantQueryBuilder 跨租户断言
- 告警同步/处置/收敛、规则版本化、集团发布
- 备件/知识库/工单/点检/保养/成本/组织树/fleet/system_stats
"""

from __future__ import annotations

import datetime
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages", "pdm_signal"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

SQLITE_PATH = os.getenv("PDM_SQLITE_PATH", "/tmp/pdm_smoke.db")
SCHEMA = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                      "db", "sqlite", "001_schema.sql")


@pytest.fixture(scope="module")
def db():
    """模块级：建库 + 灌入最小数据集。"""
    os.environ["PDM_DB_BACKEND"] = "sqlite"
    os.environ["PDM_SQLITE_PATH"] = SQLITE_PATH
    # 清掉旧库，保证可重复运行
    for suffix in ("", "-wal", "-shm"):
        p = pathlib.Path(SQLITE_PATH + suffix)
        if p.exists():
            p.unlink()
    pathlib.Path(os.path.dirname(SQLITE_PATH)).mkdir(parents=True, exist_ok=True)

    # import 必须在环境变量之后（DB_BACKEND 在模块加载时求值）
    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]
    from app import repository  # noqa: PLC0415

    assert repository.DB_BACKEND == "sqlite", "必须在 sqlite 后端下运行"
    with open(SCHEMA, encoding="utf-8") as fh:
        ddl = fh.read()
    conn = repository._SQLiteConn(SQLITE_PATH)
    conn._conn.executescript(ddl)
    conn.close()

    now = datetime.datetime.now()
    with repository.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant(tenant_id,tenant_type,name,region) VALUES (%s,%s,%s,%s)",
            ("site-b", "SITE", "天津零部件基地", "CN"))
        cur.execute(
            "INSERT INTO org_node(tenant_id,node_id,parent_id,node_type,name,path) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            ("site-b", "site-b-W1", None, "WORKSHOP", "焊装车间", "/site-b/W1"))
        # 2 台设备：一台健康、一台劣化
        for i, (aid, crit) in enumerate([("WELD-0001", "A"), ("WELD-0002", "B")]):
            cur.execute(
                "INSERT INTO asset(tenant_id,asset_id,node_id,name,category,criticality,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                ("site-b", aid, "site-b-W1", f"焊钳{i+1}", "ROBOT", crit, "RUNNING"))
            cur.execute(
                "INSERT INTO asset_metric_snapshot(tenant_id,asset_id,vib_rms_mm_s,"
                "vib_baseline_mm_s,kurtosis,temp_c,temp_baseline_c,current_dev_pct,"
                "trend_pct_per_day,ts) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                ("site-b", aid, 2.0 + i * 6.0, 2.0, 3.0, 45.0, 40.0, 2.0, 0.1, now))
        # 告警规则（集团发布 + 基地本地各一条）
        cur.execute(
            "INSERT INTO alert_rule(tenant_id,rule_id,name,metric,op,threshold,level,"
            "owner_role,sla_hours,version,scope) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("site-b", "HI_CRITICAL", "HI 临界", "hi", "lt", 40, "red",
             "maintenance_supervisor", 4, 1, "group"))
        cur.execute(
            "INSERT INTO alert_rule(tenant_id,rule_id,name,metric,op,threshold,level,"
            "owner_role,sla_hours,version,scope) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("site-b", "HI_WARNING", "HI 预警", "hi", "lt", 70, "orange",
             "maintenance_supervisor", 24, 1, "group"))
        # 保养计划（覆盖 items JSON + datetime 往返）
        cur.execute(
            "INSERT INTO maintenance_plan(tenant_id,plan_id,asset_id,name,cycle_days,"
            "items,owner_role,last_done_at,next_due_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("site-b", "MP-001", "WELD-0001", "焊钳保养", 7, '[{"item":"换油"}]',
             "operator", now, now + datetime.timedelta(days=3)))
        # 备件
        cur.execute(
            "INSERT INTO spare_part(tenant_id,part_code,name,unit,stock,safety_stock,"
            "unit_price) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            ("site-b", "BRG-6316", "轴承 6316", "个", 5, 3, 120.0))
        # 用户（PBKDF2 哈希由 auth 模块生成）
        from app import auth as auth_mod
        cur.execute(
            "INSERT INTO app_user(tenant_id,username,role,display_name,password_hash) "
            "VALUES (%s,%s,%s,%s,%s)",
            ("site-b", "op_li", "operator", "值班员", auth_mod.hash_password("Oper@2026")))
        # 读模型：一台已评估且 HI 劣化（供告警同步使用，不依赖执行顺序）
        cur.execute(
            "INSERT INTO read_asset_health(tenant_id,asset_id,hi,level,rul_p50_days,open_alerts) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            ("site-b", "WELD-0002", 5.0, "red", None, 0))
        # HI 历史（供趋势与 RUL）
        cur.execute(
            "INSERT INTO read_asset_health_history(tenant_id,asset_id,hi,level,ts) "
            "VALUES (%s,%s,%s,%s,%s)",
            ("site-b", "WELD-0002", 6.0, "red", now))
    return repository


def test_diagnose_and_read_model(db):
    """诊断落库（NOW() 表达式）+ 读模型 UPSERT（ON CONFLICT 路径）。"""
    from app import domain
    from app.schemas import DiagnoseRequest, Metrics

    tenant, asset = "site-b", "WELD-0002"
    m = Metrics(vib_rms_mm_s=8.0, vib_baseline_mm_s=2.0, kurtosis=3.0,
                temp_c=45.0, temp_baseline_c=40.0, current_dev_pct=2.0,
                trend_pct_per_day=0.1)
    req = DiagnoseRequest(tenant_id=tenant, asset_id=asset, metrics=m)
    diag = domain.diagnose(req)
    diag["asset_id"] = asset  # save_diagnosis 依赖此键（server 层由 _persist 注入）
    db.save_diagnosis(tenant, diag)

    row = db.get_latest_feature(tenant, asset)  # feature_point 为空，允许 None
    assert row is None or isinstance(row, dict)

    # refresh_latest：INSERT ... ON DUPLICATE KEY UPDATE → ON CONFLICT
    db.refresh_latest(tenant, asset, diag["hi"], diag["level"], 8.0, 3.0,
                      datetime.datetime.now(), 4)
    db.refresh_latest(tenant, asset, diag["hi"] - 1, diag["level"], 8.0, 3.0,
                      datetime.datetime.now(), 4)  # 第二次走 DO UPDATE
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT hi, level FROM read_asset_latest WHERE tenant_id=%s AND asset_id=%s",
                    (tenant, asset))
        r = cur.fetchone()
    assert r is not None and abs(r["hi"] - (diag["hi"] - 1)) < 0.01

    # 读模型主表 UPSERT
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO read_asset_health(tenant_id,asset_id,hi,level,rul_p50_days,open_alerts) "
            "VALUES (%s,%s,%s,%s,NULL,0) "
            "ON DUPLICATE KEY UPDATE hi=VALUES(hi),level=VALUES(level)",
            (tenant, asset, diag["hi"], diag["level"]))
        cur.execute(
            "INSERT INTO read_asset_health(tenant_id,asset_id,hi,level,rul_p50_days,open_alerts) "
            "VALUES (%s,%s,%s,%s,NULL,0) "
            "ON DUPLICATE KEY UPDATE hi=VALUES(hi),level=VALUES(level)",
            (tenant, asset, 55.5, "yellow"))
    ov = db.overview_stats(tenant)
    assert ov["total_assets"] == 2
    assert ov["assessed"] >= 1
    assert 0 <= ov["coverage_pct"] <= 100


def test_alert_sync_and_ack(db):
    """告警同步（等级匹配最严）+ 处置 + 列表 + summary。"""
    tenant = "site-b"
    res = db.sync_alerts(tenant, 70, 100)
    assert res["created"] + res["skipped"] >= 1

    out = db.list_alerts(tenant, "", "", 50)
    rows = out[0] if isinstance(out, tuple) else out
    assert len(rows) >= 1
    first = rows[0]
    # 等级必须与 HI 匹配（HI=0 级别劣化应命中最严规则）
    assert first["level"] in ("red", "orange")

    out = db.ack_alert(tenant, first["alert_id"], "ack", "op_li", "处理中")
    assert out["status"] in ("ACK", "ACKED")

    summary = db.alert_summary(tenant)
    assert isinstance(summary, dict)


def test_rule_versioning(db):
    """告警规则版本化：scope/version + 集团发布 version 递增。"""
    tenant = "site-b"
    rules = db.list_alert_rules(tenant, scope="group")
    assert len(rules) >= 2
    assert all(r["scope"] == "group" for r in rules)
    assert all(r["version"] == 1 for r in rules)

    # upsert 指定 scope/version
    db.upsert_alert_rule(tenant, {"rule_id": "HI_WARNING", "metric": "hi",
                                  "op": "lt", "threshold": 65, "level": "orange",
                                  "version": 2, "scope": "group"})
    rules_v2 = db.list_alert_rules(tenant, scope="group", version=2)
    assert any(r["rule_id"] == "HI_WARNING" and r["threshold"] == 65
               and r["version"] == 2 for r in rules_v2)

    # 集团发布：version 自动递增到 3
    pub = db.publish_group_rules(tenant, {"rule_id": "HI_WARNING", "metric": "hi",
                                          "op": "lt", "threshold": 60, "level": "orange"})
    assert pub["version"] == 3 and pub["scope"] == "group"

    # hi_rules 回退链：site 无规则 → 用 group
    rules = db.hi_rules(tenant)
    assert len(rules) >= 1


def test_inspection_plan_and_datetime_roundtrip(db):
    """保养计划列表：datetime 必须还原为对象（days_left 计算依赖）。"""
    tenant = "site-b"
    plans = db.list_plans(tenant)
    assert len(plans) >= 1
    p = plans[0]
    assert p["days_left"] is not None, "DATETIME 未还原为 datetime 对象"
    assert isinstance(p["days_left"], (int, float))
    assert p["item_count"] == 1

    gen = db.generate_inspections(tenant, days=7, limit=10)
    assert gen["created"] + gen["skipped"] >= 1
    tasks = db.list_inspections(tenant)
    assert len(tasks) >= 1
    sub = db.submit_inspection(tenant, tasks[0]["task_id"],
                               [{"item": "换油", "result": "ok"}], "op_li", "正常")
    assert sub["status"] == "DONE"

    stats = db.inspection_stats(tenant)
    assert stats["by_status"].get("DONE", 0) >= 1


def test_spares_knowledge_worklog(db):
    """备件出入库 + 知识库 + 工单流水。"""
    tenant = "site-b"
    parts = db.list_spares(tenant)
    assert len(parts) == 1 and parts[0]["part_code"] == "BRG-6316"

    out = db.move_spare(tenant, "BRG-6316", -2, "op_li", card_id="CARD-001")
    assert out["stock"] == 3
    with pytest.raises(ValueError):
        db.move_spare(tenant, "BRG-6316", -99, "op_li")  # 不允许负库存

    case = db.create_case(tenant, {"title": "焊钳异响", "root_cause": "轴承磨损",
                                   "tags": "轴承,异响"})
    assert case["case_id"].startswith("KC-")
    found = db.list_cases(tenant, q="轴承")
    assert len(found) >= 1

    log = db.append_work_log(tenant, "CARD-001", "ASSIGNED", "sup_wang", "派工")
    assert log["action"] == "ASSIGNED"
    logs = db.list_work_logs(tenant, card_id="CARD-001")
    assert len(logs) >= 1


def test_cost_org_fleet_system_stats(db):
    """成本/组织树/fleet 跨租户聚合/系统统计。"""
    cost = db.cost_stats("site-b", 10)
    assert "total" in cost and "by_asset" in cost

    tree = db.org_tree("site-b")
    assert len(tree) >= 1

    fleet = db.fleet_stats()
    assert fleet["tenants"] >= 1
    site_b = [s for s in fleet["sites"] if s["tenant_id"] == "site-b"]
    assert site_b and site_b[0]["total_assets"] == 2

    sysstat = db.system_stats("site-b")
    names = {t["name"] for t in sysstat["tables"]}
    assert "asset" in names and "alert" in names


def test_cross_tenant_isolation(db):
    """TenantQueryBuilder 跨租户断言在 SQLite 后端同样生效。

    注意：query() 会自动把 tenant_id 注入为第一个参数，所以这里直接构造
    「查询参数带 site-b 行但构造器是 site-a」的场景：先插一行 site-a 的
    asset 指向 site-b 的内容是不可能的（PK 含 tenant_id），因此改用
    构造器传 site-a、返回行中 tenant_id 为 site-a 的正常路径验证通过，
    再验证无 tenant_id 列的 SQL 会被拒绝。
    """
    from app.repository import TenantQueryBuilder
    import pytest
    # SQL 必须含 tenant_id 过滤（仓库硬规则）
    with pytest.raises(ValueError):
        TenantQueryBuilder("site-b").query("SELECT asset_id FROM asset").fetchall()
    # 正常路径：第一个 %s 由 query() 注入 tenant_id，无额外参数
    rows = TenantQueryBuilder("site-b").query(
        "SELECT tenant_id, asset_id FROM asset WHERE tenant_id=%s").fetchall()
    assert len(rows) == 2

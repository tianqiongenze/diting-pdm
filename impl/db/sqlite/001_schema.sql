-- =============================================================================
-- Diting PdM · SQLite Schema v1.0（边缘站点本地库）
--
-- 用途：docs/44 边缘部署的本地数据库。边缘单节点 K3s 不跑 TiDB，
--       用 SQLite 承载"本地自治"所需的全部表（零运维、文件级存储）。
--
-- 与 TiDB schema 的对应关系：
--   - 001_schema.sql ~ 006_alert_rule.sql 的全部表，此处为 SQLite 方言
--   - VARCHAR→TEXT / DECIMAL→REAL / DATETIME(3)→DATETIME（注册了转换器，
--     读出来仍是 datetime 对象，与 pymysql 行为对齐）
--   - ON UPDATE CURRENT_TIMESTAMP 不支持，由应用层维护 updated_at
--
-- 方言转换由 repository.py _translate() 统一处理，仓库函数零改动：
--   %s→? / INSERT IGNORE→INSERT OR IGNORE /
--   ON DUPLICATE KEY UPDATE→ON CONFLICT DO UPDATE / NOW()→datetime('now')
-- =============================================================================

-- 1. 租户与组织 -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant (
  tenant_id    TEXT NOT NULL,
  tenant_type  TEXT NOT NULL,                       -- GROUP / SITE / SUPPLIER
  name         TEXT NOT NULL,
  parent_id    TEXT,
  region       TEXT NOT NULL DEFAULT 'CN',          -- CN / OVERSEAS
  status       TEXT NOT NULL DEFAULT 'ACTIVE',
  created_at   DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id)
);
CREATE INDEX IF NOT EXISTS idx_tenant_parent ON tenant(parent_id);

CREATE TABLE IF NOT EXISTS org_node (
  tenant_id   TEXT NOT NULL,
  node_id     TEXT NOT NULL,
  parent_id   TEXT,
  node_type   TEXT NOT NULL,                        -- WORKSHOP / LINE / STATION
  name        TEXT NOT NULL,
  path        TEXT NOT NULL,
  PRIMARY KEY (tenant_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_org_path ON org_node(tenant_id, path);

-- 2. 资产 -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset (
  tenant_id     TEXT NOT NULL,
  asset_id      TEXT NOT NULL,
  node_id       TEXT,
  name          TEXT NOT NULL,
  model         TEXT,
  category      TEXT NOT NULL,
  criticality   TEXT NOT NULL DEFAULT 'B',          -- A 致命 / B 重大 / C 一般
  commissioned  DATE,
  status        TEXT NOT NULL DEFAULT 'RUNNING',
  attrs         TEXT,                               -- JSON：行业包扩展字段
  created_at    DATETIME NOT NULL DEFAULT (datetime('now')),
  updated_at    DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, asset_id)
);
CREATE INDEX IF NOT EXISTS idx_asset_node ON asset(tenant_id, node_id);
CREATE INDEX IF NOT EXISTS idx_asset_crit ON asset(tenant_id, criticality, status);

CREATE TABLE IF NOT EXISTS component (
  tenant_id   TEXT NOT NULL,
  comp_id     TEXT NOT NULL,
  asset_id    TEXT NOT NULL,
  comp_type   TEXT NOT NULL,                        -- BEARING/GEAR/COUPLING/...
  position    TEXT,
  spec        TEXT,                                 -- JSON
  PRIMARY KEY (tenant_id, comp_id)
);
CREATE INDEX IF NOT EXISTS idx_comp_asset ON component(tenant_id, asset_id);

CREATE TABLE IF NOT EXISTS measurement_point (
  tenant_id     TEXT NOT NULL,
  mp_id         TEXT NOT NULL,
  asset_id      TEXT NOT NULL,
  comp_id       TEXT,
  quantity      TEXT NOT NULL,                      -- vib_acc / vib_vel / temp / ...
  unit          TEXT NOT NULL,
  direction     TEXT,
  sample_rate   INTEGER,
  range_min     REAL,
  range_max     REAL,
  calibrated_at DATE,
  calibration_no TEXT,
  PRIMARY KEY (tenant_id, mp_id)
);
CREATE INDEX IF NOT EXISTS idx_mp_asset ON measurement_point(tenant_id, asset_id);

-- 3. 诊断 -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS diagnosis (
  tenant_id       TEXT NOT NULL,
  diag_id         TEXT NOT NULL,
  asset_id        TEXT NOT NULL,
  window_start    DATETIME NOT NULL,
  window_end      DATETIME NOT NULL,
  hi              REAL NOT NULL,
  level           TEXT NOT NULL,
  degraded        INTEGER NOT NULL DEFAULT 0,
  degraded_reason TEXT,
  contributions   TEXT NOT NULL,                    -- JSON
  hypotheses      TEXT,                             -- JSON
  model_name      TEXT NOT NULL,
  model_version   TEXT NOT NULL,
  dataset_hash    TEXT,
  status          TEXT NOT NULL DEFAULT 'FINALIZED',
  created_at      DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, diag_id)
);
CREATE INDEX IF NOT EXISTS idx_diag_asset_time ON diagnosis(tenant_id, asset_id, created_at);
CREATE INDEX IF NOT EXISTS idx_diag_level_time ON diagnosis(tenant_id, level, created_at);

CREATE TABLE IF NOT EXISTS evidence (
  tenant_id  TEXT NOT NULL,
  ev_id      TEXT NOT NULL,
  diag_id    TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  ev_type    TEXT NOT NULL,
  payload    TEXT NOT NULL,                         -- JSON
  source     TEXT NOT NULL,
  prev_hash  TEXT NOT NULL,
  hash       TEXT NOT NULL,
  created_at DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, ev_id),
  UNIQUE (tenant_id, diag_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_ev_diag ON evidence(tenant_id, diag_id);

-- 4. 决策（方案卡）--------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recommendation_card (
  tenant_id      TEXT NOT NULL,
  card_id        TEXT NOT NULL,
  diag_id        TEXT,
  asset_id       TEXT NOT NULL,
  conclusion     TEXT,
  confidence     REAL,
  options        TEXT NOT NULL,                     -- JSON
  economics      TEXT,                              -- JSON
  conflicts      TEXT,                              -- JSON
  verdict        TEXT NOT NULL DEFAULT 'MONITOR',
  risk_level     TEXT NOT NULL DEFAULT '中',
  required_roles TEXT,                              -- JSON
  status         TEXT NOT NULL DEFAULT 'PENDING',
  confirm_roles  TEXT,                              -- JSON（001 原列，保留兼容）
  confirm_deadline DATETIME,
  reject_reason  TEXT,
  workorder_no   TEXT,
  avoided_loss   REAL,
  total_cost     REAL,
  net_benefit    REAL,
  created_at     DATETIME NOT NULL DEFAULT (datetime('now')),
  updated_at     DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, card_id)
);
CREATE INDEX IF NOT EXISTS idx_card_status ON recommendation_card(tenant_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_card_asset ON recommendation_card(tenant_id, asset_id);

CREATE TABLE IF NOT EXISTS card_confirmation (
  tenant_id   TEXT NOT NULL,
  confirm_id  TEXT NOT NULL,
  card_id     TEXT NOT NULL,
  role        TEXT NOT NULL,
  actor       TEXT NOT NULL,
  user_id     TEXT NOT NULL DEFAULT '',
  decision    TEXT NOT NULL DEFAULT 'confirm',
  comment     TEXT,
  action      TEXT NOT NULL,
  token_hash  TEXT NOT NULL,
  expires_at  DATETIME NOT NULL,
  created_at  DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, confirm_id)
);
CREATE INDEX IF NOT EXISTS idx_conf_card ON card_confirmation(tenant_id, card_id);

CREATE TABLE IF NOT EXISTS purchase_order (
  tenant_id       TEXT NOT NULL,
  po_id           TEXT NOT NULL,
  card_id         TEXT NOT NULL,
  supplier_code   TEXT NOT NULL,
  sku             TEXT NOT NULL,
  qty             INTEGER NOT NULL,
  unit_price      REAL NOT NULL,
  amount          REAL NOT NULL,
  idempotency_key TEXT NOT NULL,
  confirm_level   TEXT NOT NULL,
  confirmed_by    TEXT,                             -- JSON
  vendor_receipt  TEXT,
  status          TEXT NOT NULL DEFAULT 'DRAFT',
  cancellable_until DATETIME,
  created_at      DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, po_id),
  UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_po_card ON purchase_order(tenant_id, card_id);

-- 5. 告警 -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert (
  tenant_id   TEXT NOT NULL,
  alert_id    TEXT NOT NULL,
  asset_id    TEXT NOT NULL,
  diag_id     TEXT,
  level       TEXT NOT NULL,
  rule_id     TEXT NOT NULL,
  message     TEXT NOT NULL,
  action_hint TEXT NOT NULL,
  owner_role  TEXT NOT NULL,
  sla_hours   INTEGER NOT NULL,
  status      TEXT NOT NULL DEFAULT 'OPEN',         -- OPEN/ACK/ESCALATED/CLOSED
  ack_by      TEXT,
  ack_action  TEXT,
  escalated_at DATETIME,
  created_at  DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, alert_id)
);
CREATE INDEX IF NOT EXISTS idx_alert_status ON alert(tenant_id, status, level, created_at);
CREATE INDEX IF NOT EXISTS idx_alert_asset ON alert(tenant_id, asset_id, created_at);

-- 6. 审计链 ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
  tenant_id   TEXT NOT NULL,
  audit_id    TEXT NOT NULL,
  ts          DATETIME NOT NULL DEFAULT (datetime('now')),
  actor_type  TEXT NOT NULL,
  actor_id    TEXT NOT NULL,
  on_behalf   TEXT,
  action      TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id   TEXT NOT NULL,
  decision    TEXT,
  input_hash  TEXT NOT NULL,
  output_hash TEXT,
  trace_id    TEXT NOT NULL,
  prev_hash   TEXT NOT NULL,
  hash        TEXT NOT NULL,
  PRIMARY KEY (tenant_id, audit_id)
);
CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_log(trace_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

-- 7. 事件 Outbox -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbox (
  tenant_id    TEXT NOT NULL,
  event_id     TEXT NOT NULL,
  event_type   TEXT NOT NULL,
  aggregate_id TEXT NOT NULL,
  payload      TEXT NOT NULL,                       -- JSON
  published_at DATETIME,
  retry_count  INTEGER NOT NULL DEFAULT 0,
  created_at   DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_outbox_unpub ON outbox(published_at, created_at);

CREATE TABLE IF NOT EXISTS processed_event (
  consumer     TEXT NOT NULL,
  event_id     TEXT NOT NULL,
  processed_at DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (consumer, event_id)
);

-- 8. 读模型 ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS read_asset_health (
  tenant_id    TEXT NOT NULL,
  asset_id     TEXT NOT NULL,
  hi           REAL NOT NULL,
  level        TEXT NOT NULL,
  rul_p50_days INTEGER,
  open_alerts  INTEGER NOT NULL DEFAULT 0,
  updated_at   DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, asset_id)
);
CREATE INDEX IF NOT EXISTS idx_rah_level ON read_asset_health(level, updated_at);

CREATE TABLE IF NOT EXISTS read_group_benchmark (
  tenant_id   TEXT NOT NULL,
  model       TEXT NOT NULL,
  period      TEXT NOT NULL,                        -- YYYY-MM
  mtbf_hours  REAL,
  mttr_hours  REAL,
  alert_count INTEGER,
  updated_at  DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, model, period)
);

CREATE TABLE IF NOT EXISTS read_asset_health_history (
  tenant_id TEXT NOT NULL,
  asset_id  TEXT NOT NULL,
  hi        REAL NOT NULL,
  level     TEXT NOT NULL,
  ts        DATETIME NOT NULL,
  PRIMARY KEY (tenant_id, asset_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_rahh_asset_ts ON read_asset_health_history(tenant_id, asset_id, ts);

CREATE TABLE IF NOT EXISTS read_asset_latest (
  tenant_id  TEXT NOT NULL,
  asset_id   TEXT NOT NULL,
  mp_count   INTEGER NOT NULL DEFAULT 0,
  rms        REAL,
  kurtosis   REAL,
  hi         REAL,
  level      TEXT,
  ts         DATETIME,
  updated_at DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, asset_id)
);
CREATE INDEX IF NOT EXISTS idx_ral_level ON read_asset_latest(tenant_id, level);

-- 9. 时序特征（边缘本地保留窗口，不逐条上云）------------------------------------
CREATE TABLE IF NOT EXISTS feature_point (
  tenant_id TEXT NOT NULL,
  mp_id     TEXT NOT NULL,
  ts        DATETIME NOT NULL,
  rms       REAL NOT NULL,
  peak      REAL NOT NULL,
  kurtosis  REAL NOT NULL,
  skewness  REAL NOT NULL,
  crest     REAL NOT NULL,
  hi        REAL NOT NULL,
  level     TEXT NOT NULL,
  quality   INTEGER NOT NULL DEFAULT 95,
  PRIMARY KEY (tenant_id, mp_id, ts)
);

CREATE TABLE IF NOT EXISTS asset_metric_snapshot (
  tenant_id         TEXT NOT NULL,
  asset_id          TEXT NOT NULL,
  vib_rms_mm_s      REAL NOT NULL,
  vib_baseline_mm_s REAL NOT NULL,
  kurtosis          REAL NOT NULL,
  temp_c            REAL NOT NULL,
  temp_baseline_c   REAL NOT NULL,
  current_dev_pct   REAL NOT NULL,
  trend_pct_per_day REAL NOT NULL,
  ts                DATETIME NOT NULL,
  PRIMARY KEY (tenant_id, asset_id)
);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON asset_metric_snapshot(ts);

-- 10. 集成 Outbox ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS integration_outbox (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id    TEXT NOT NULL,
  target       TEXT NOT NULL,                       -- eam / mes
  event_type   TEXT NOT NULL,
  external_id  TEXT NOT NULL,
  payload      TEXT NOT NULL,                       -- JSON
  status       TEXT NOT NULL DEFAULT 'PENDING',
  attempts     INTEGER NOT NULL DEFAULT 0,
  next_retry_at DATETIME NOT NULL DEFAULT (datetime('now')),
  last_error   TEXT,
  created_at   DATETIME NOT NULL DEFAULT (datetime('now')),
  updated_at   DATETIME NOT NULL DEFAULT (datetime('now')),
  UNIQUE (tenant_id, target, external_id)
);
CREATE INDEX IF NOT EXISTS idx_ob_pending ON integration_outbox(status, next_retry_at);

-- 11. 告警规则（判据版本化，docs/43 P0-3）---------------------------------------
CREATE TABLE IF NOT EXISTS alert_rule (
  tenant_id  TEXT NOT NULL,
  rule_id    TEXT NOT NULL,
  name       TEXT NOT NULL DEFAULT '',
  metric     TEXT NOT NULL DEFAULT 'hi',
  op         TEXT NOT NULL DEFAULT 'lt',
  threshold  REAL NOT NULL,
  level      TEXT NOT NULL DEFAULT 'orange',
  owner_role TEXT NOT NULL DEFAULT 'maintenance_supervisor',
  sla_hours  INTEGER NOT NULL DEFAULT 24,
  enabled    INTEGER NOT NULL DEFAULT 1,
  note       TEXT,
  version    INTEGER NOT NULL DEFAULT 1,
  scope      TEXT NOT NULL DEFAULT 'site',          -- group=集团发布/site=基地本地
  created_at DATETIME NOT NULL DEFAULT (datetime('now')),
  updated_at DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, rule_id, version)
);
CREATE INDEX IF NOT EXISTS idx_rule_scope ON alert_rule(tenant_id, scope, version);
CREATE INDEX IF NOT EXISTS idx_rule_enabled ON alert_rule(tenant_id, enabled);

-- 12. 用户与角色（RBAC）---------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_user (
  tenant_id    TEXT NOT NULL,
  username     TEXT NOT NULL,
  role         TEXT NOT NULL,                       -- operator/supervisor/manager
  display_name TEXT,
  password_hash TEXT NOT NULL,                      -- pbkdf2$salt$hash
  enabled      INTEGER NOT NULL DEFAULT 1,
  created_at   DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, username)
);

-- 13. 点检巡检 + 保养计划 --------------------------------------------------------
CREATE TABLE IF NOT EXISTS maintenance_plan (
  tenant_id    TEXT NOT NULL,
  plan_id      TEXT NOT NULL,
  asset_id     TEXT NOT NULL,
  name         TEXT NOT NULL,
  cycle_days   INTEGER NOT NULL,
  items        TEXT,                                -- JSON
  owner_role   TEXT NOT NULL DEFAULT 'operator',
  last_done_at DATETIME,
  next_due_at  DATETIME,
  enabled      INTEGER NOT NULL DEFAULT 1,
  created_at   DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, plan_id)
);
CREATE INDEX IF NOT EXISTS idx_plan_due ON maintenance_plan(tenant_id, next_due_at);

CREATE TABLE IF NOT EXISTS inspection_task (
  tenant_id     TEXT NOT NULL,
  task_id       TEXT NOT NULL,
  plan_id       TEXT,
  asset_id      TEXT NOT NULL,
  title         TEXT NOT NULL,
  items         TEXT,                               -- JSON
  due_at        DATETIME NOT NULL,
  status        TEXT NOT NULL DEFAULT 'PENDING',    -- PENDING/DONE/OVERDUE
  assignee_role TEXT,
  done_at       DATETIME,
  done_by       TEXT,
  ng_count      INTEGER NOT NULL DEFAULT 0,
  note          TEXT,
  created_at    DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_ins_due ON inspection_task(tenant_id, due_at);
CREATE INDEX IF NOT EXISTS idx_ins_status ON inspection_task(tenant_id, status);

-- 14. 备件 ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS spare_part (
  tenant_id    TEXT NOT NULL,
  part_code    TEXT NOT NULL,
  name         TEXT NOT NULL,
  spec         TEXT,
  unit         TEXT,
  stock        INTEGER NOT NULL DEFAULT 0,
  safety_stock INTEGER NOT NULL DEFAULT 0,
  unit_price   REAL NOT NULL DEFAULT 0,
  location     TEXT,
  created_at   DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, part_code)
);

CREATE TABLE IF NOT EXISTS spare_issue (
  tenant_id  TEXT NOT NULL,
  issue_id   TEXT NOT NULL,
  part_code  TEXT NOT NULL,
  qty        INTEGER NOT NULL,
  card_id    TEXT,
  asset_id   TEXT,
  issued_by  TEXT,
  note       TEXT,
  created_at DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, issue_id)
);
CREATE INDEX IF NOT EXISTS idx_spare_issue_code ON spare_issue(tenant_id, part_code, created_at);

-- 15. 知识库 --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_case (
  tenant_id  TEXT NOT NULL,
  case_id    TEXT NOT NULL,
  asset_id   TEXT,
  title      TEXT NOT NULL,
  symptom    TEXT,
  root_cause TEXT,
  resolution TEXT,
  source     TEXT,
  tags       TEXT,
  created_by TEXT,
  created_at DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, case_id)
);
CREATE INDEX IF NOT EXISTS idx_kc_created ON knowledge_case(tenant_id, created_at);

-- 16. 工单执行流水 --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS work_order_log (
  tenant_id   TEXT NOT NULL,
  card_id     TEXT NOT NULL,
  action      TEXT NOT NULL,                        -- ASSIGNED/STARTED/COMPLETED/ACCEPTED/CLOSED
  actor       TEXT NOT NULL,
  note        TEXT,
  actual_cost REAL,
  created_at  DATETIME NOT NULL DEFAULT (datetime('now')),
  id          INTEGER PRIMARY KEY AUTOINCREMENT
);
CREATE INDEX IF NOT EXISTS idx_wol_card ON work_order_log(tenant_id, card_id, created_at);

-- 17. 集团站点注册与上行摘要（云端用；边缘库也建此表便于本地开发联调）----------
CREATE TABLE IF NOT EXISTS site_registry (
  site_id      TEXT NOT NULL,
  site_name    TEXT NOT NULL,
  region       TEXT NOT NULL DEFAULT 'CN',
  device_count INTEGER NOT NULL DEFAULT 0,
  edge_version TEXT,
  heartbeat    DATETIME NOT NULL,
  status       TEXT NOT NULL DEFAULT 'ACTIVE',
  created_at   DATETIME NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (site_id)
);

CREATE TABLE IF NOT EXISTS read_edge_summary (
  site_id      TEXT NOT NULL,
  total_assets INTEGER NOT NULL DEFAULT 0,
  assessed     INTEGER NOT NULL DEFAULT 0,
  coverage_pct REAL NOT NULL DEFAULT 0,
  by_level     TEXT,                                -- JSON
  avg_hi       REAL,
  open_alerts  INTEGER NOT NULL DEFAULT 0,
  summary_json TEXT,                                -- JSON
  reported_at  DATETIME NOT NULL,
  PRIMARY KEY (site_id)
);

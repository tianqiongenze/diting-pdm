-- =============================================================================
-- Diting PdM · TiDB Schema v0.1
-- 目标库：TiDB v8.5.8（MySQL 8.0 协议兼容）
-- 决策依据：DECISIONS.md D23 = 全用 TiDB；docs/17 ADR-017 §8
--
-- 三条硬约束：
-- 1. 所有业务表第一列固定 tenant_id，主键与索引一律以 tenant_id 打头
-- 2. TiDB 无 RLS → 隔离靠应用层 TenantQueryBuilder + 结果断言 + CI 越权测试
-- 3. TiDB 无 GIN 索引 → 高频 JSON 字段抽为独立列或生成列，禁止在 JSON 上做范围查询
-- =============================================================================

CREATE DATABASE IF NOT EXISTS pdm
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;

USE pdm;

-- -----------------------------------------------------------------------------
-- 1. 租户与组织（多基地 + 供应商）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant (
  tenant_id    VARCHAR(32)  NOT NULL COMMENT '租户标识，如 site-a / supplier-t1-001',
  tenant_type  VARCHAR(16)  NOT NULL COMMENT 'GROUP / SITE / SUPPLIER',
  name         VARCHAR(128) NOT NULL,
  parent_id    VARCHAR(32)  NULL COMMENT '上级租户，集团为 NULL',
  region       VARCHAR(16)  NOT NULL DEFAULT 'CN' COMMENT 'CN / OVERSEAS，决定数据主权策略',
  status       VARCHAR(16)  NOT NULL DEFAULT 'ACTIVE',
  created_at   DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id),
  KEY idx_parent (parent_id)
);

CREATE TABLE IF NOT EXISTS org_node (
  tenant_id   VARCHAR(32) NOT NULL,
  node_id     VARCHAR(64) NOT NULL COMMENT '车间/产线/工位，路径式 ID',
  parent_id   VARCHAR(64) NULL,
  node_type   VARCHAR(16) NOT NULL COMMENT 'WORKSHOP / LINE / STATION',
  name        VARCHAR(128) NOT NULL,
  path        VARCHAR(512) NOT NULL COMMENT '/site-a/总装/线1/工位3，便于子树查询',
  PRIMARY KEY (tenant_id, node_id),
  KEY idx_path (tenant_id, path)
);

-- -----------------------------------------------------------------------------
-- 2. 资产（统一命名空间）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS asset (
  tenant_id     VARCHAR(32) NOT NULL,
  asset_id      VARCHAR(64) NOT NULL COMMENT '位号，与 P&ID 一致，如 KA-101',
  node_id       VARCHAR(64) NULL COMMENT '所属产线/工位',
  name          VARCHAR(128) NOT NULL,
  model         VARCHAR(64)  NULL,
  category      VARCHAR(32)  NOT NULL COMMENT 'MOTOR/PUMP/FAN/PRESS/ROBOT/AGV/...',
  criticality   CHAR(1)      NOT NULL DEFAULT 'B' COMMENT 'A 致命 / B 重大 / C 一般',
  commissioned  DATE         NULL,
  status        VARCHAR(16)  NOT NULL DEFAULT 'RUNNING',
  attrs         JSON         NULL COMMENT '行业包扩展字段（电动车/汽车差异在此）',
  created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, asset_id),
  KEY idx_node (tenant_id, node_id),
  KEY idx_criticality (tenant_id, criticality, status)
);

CREATE TABLE IF NOT EXISTS component (
  tenant_id   VARCHAR(32) NOT NULL,
  comp_id     VARCHAR(64) NOT NULL,
  asset_id    VARCHAR(64) NOT NULL,
  comp_type   VARCHAR(32) NOT NULL COMMENT 'BEARING/GEAR/COUPLING/IMPELLER/BELT',
  position    VARCHAR(32) NULL COMMENT 'DE / NDE',
  spec        JSON NULL COMMENT '轴承几何参数等（n_balls/ball_d/pitch_d/angle）',
  PRIMARY KEY (tenant_id, comp_id),
  KEY idx_asset (tenant_id, asset_id)
);

CREATE TABLE IF NOT EXISTS measurement_point (
  tenant_id     VARCHAR(32) NOT NULL,
  mp_id         VARCHAR(96) NOT NULL COMMENT 'site.area.asset.component.point.quantity',
  asset_id      VARCHAR(64) NOT NULL,
  comp_id       VARCHAR(64) NULL,
  quantity      VARCHAR(32) NOT NULL COMMENT 'vib_acc / vib_vel / temp / current / speed',
  unit          VARCHAR(16) NOT NULL,
  direction     VARCHAR(8)  NULL COMMENT 'H / V / A',
  sample_rate   INT         NULL COMMENT '波形采样率 Hz，标量为 NULL',
  range_min     DOUBLE      NULL,
  range_max     DOUBLE      NULL,
  calibrated_at DATE        NULL,
  calibration_no VARCHAR(64) NULL COMMENT '校准证书号，验收 A 类项',
  PRIMARY KEY (tenant_id, mp_id),
  KEY idx_asset (tenant_id, asset_id)
);

-- -----------------------------------------------------------------------------
-- 3. 诊断（聚合根 Diagnosis）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS diagnosis (
  tenant_id       VARCHAR(32) NOT NULL,
  diag_id         VARCHAR(40) NOT NULL,
  asset_id        VARCHAR(64) NOT NULL,
  window_start    DATETIME(3) NOT NULL,
  window_end      DATETIME(3) NOT NULL,
  hi              DECIMAL(5,2) NOT NULL COMMENT '0-100',
  level           VARCHAR(8)  NOT NULL COMMENT 'green/yellow/orange/red（独立列，便于索引）',
  degraded        TINYINT(1)  NOT NULL DEFAULT 0,
  degraded_reason VARCHAR(255) NULL,
  contributions   JSON        NOT NULL COMMENT '维度分解，仅展示用，不参与查询过滤',
  hypotheses      JSON        NULL COMMENT '故障假设列表',
  model_name      VARCHAR(64) NOT NULL,
  model_version   VARCHAR(32) NOT NULL,
  dataset_hash    VARCHAR(64) NULL,
  status          VARCHAR(16) NOT NULL DEFAULT 'FINALIZED' COMMENT 'DRAFT/FINALIZED/SUPERSEDED',
  created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, diag_id),
  KEY idx_asset_time (tenant_id, asset_id, created_at),
  KEY idx_level_time (tenant_id, level, created_at)
);

CREATE TABLE IF NOT EXISTS evidence (
  tenant_id  VARCHAR(32) NOT NULL,
  ev_id      VARCHAR(40) NOT NULL,
  diag_id    VARCHAR(40) NOT NULL,
  seq        INT         NOT NULL COMMENT '链内序号，从 1 开始',
  ev_type    VARCHAR(32) NOT NULL COMMENT 'metric/spectrum/rule/knowledge',
  payload    JSON        NOT NULL,
  source     VARCHAR(64) NOT NULL COMMENT '工具名或规则 ID',
  prev_hash  CHAR(64)    NOT NULL,
  hash       CHAR(64)    NOT NULL COMMENT 'SHA256(prev_hash || canonical_json(payload))',
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, ev_id),
  UNIQUE KEY uk_diag_seq (tenant_id, diag_id, seq),
  KEY idx_diag (tenant_id, diag_id)
) COMMENT '证据链不可变：仅允许 INSERT，禁止 UPDATE/DELETE（由应用层与权限双重保证）';

-- -----------------------------------------------------------------------------
-- 4. 决策（聚合根 RecommendationCard）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recommendation_card (
  tenant_id      VARCHAR(32) NOT NULL,
  card_id        VARCHAR(40) NOT NULL,
  diag_id        VARCHAR(40) NOT NULL,
  asset_id       VARCHAR(64) NOT NULL,
  options        JSON        NOT NULL COMMENT '窗口方案列表（含成本分解）',
  avoided_loss   DECIMAL(14,2) NULL,
  total_cost     DECIMAL(14,2) NULL,
  net_benefit    DECIMAL(14,2) NULL,
  conflicts      JSON        NULL,
  status         VARCHAR(16) NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING/CONFIRMED/REJECTED/EXPIRED/CONVERTED',
  confirm_roles  JSON        NULL,
  confirm_deadline DATETIME(3) NULL,
  reject_reason  VARCHAR(32) NULL COMMENT 'FALSE_POSITIVE/TIMING/COST/OTHER',
  workorder_no   VARCHAR(64) NULL COMMENT 'EAM 回执号，Stop Hook 依赖此字段',
  created_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, card_id),
  KEY idx_status (tenant_id, status, created_at),
  KEY idx_asset (tenant_id, asset_id)
);

-- 人工确认记录（谁签的字，不可篡改）
CREATE TABLE IF NOT EXISTS card_confirmation (
  tenant_id   VARCHAR(32) NOT NULL,
  confirm_id  VARCHAR(40) NOT NULL,
  card_id     VARCHAR(40) NOT NULL,
  role        VARCHAR(32) NOT NULL,
  actor       VARCHAR(64) NOT NULL,
  action      VARCHAR(16) NOT NULL COMMENT 'CONFIRM / REJECT',
  token_hash  CHAR(64)    NOT NULL COMMENT '一次性令牌的哈希，原文不落库',
  expires_at  DATETIME(3) NOT NULL,
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, confirm_id),
  KEY idx_card (tenant_id, card_id)
);

-- 采购单（D6 = 可下备件采购，R1–R8 护栏的落地载体）
CREATE TABLE IF NOT EXISTS purchase_order (
  tenant_id      VARCHAR(32) NOT NULL,
  po_id          VARCHAR(40) NOT NULL,
  card_id        VARCHAR(40) NOT NULL,
  supplier_code  VARCHAR(64) NOT NULL,
  sku            VARCHAR(64) NOT NULL,
  qty            INT         NOT NULL,
  unit_price     DECIMAL(12,2) NOT NULL,
  amount         DECIMAL(14,2) NOT NULL,
  idempotency_key VARCHAR(128) NOT NULL COMMENT 'recommendation_id + sku',
  confirm_level  VARCHAR(16) NOT NULL COMMENT 'SINGLE / DUAL / BLOCKED（>5万阻断）',
  confirmed_by   JSON        NULL COMMENT '确认人列表（双人复核时为 2 个）',
  vendor_receipt VARCHAR(64) NULL COMMENT '供应商回执号，无回执不得置为 PLACED',
  status         VARCHAR(16) NOT NULL DEFAULT 'DRAFT' COMMENT 'DRAFT/PLACED/CANCELLED',
  cancellable_until DATETIME(3) NULL COMMENT 'R4：24h 内可撤销',
  created_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, po_id),
  UNIQUE KEY uk_idem (tenant_id, idempotency_key),
  KEY idx_card (tenant_id, card_id)
);

-- -----------------------------------------------------------------------------
-- 5. 告警
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert (
  tenant_id   VARCHAR(32) NOT NULL,
  alert_id    VARCHAR(40) NOT NULL,
  asset_id    VARCHAR(64) NOT NULL,
  diag_id     VARCHAR(40) NULL,
  level       VARCHAR(8)  NOT NULL,
  rule_id     VARCHAR(64) NOT NULL,
  message     VARCHAR(512) NOT NULL,
  action_hint VARCHAR(512) NOT NULL COMMENT '必须有建议动作，仅分数不得发送（FR-605）',
  owner_role  VARCHAR(32) NOT NULL,
  sla_hours   INT         NOT NULL,
  status      VARCHAR(16) NOT NULL DEFAULT 'OPEN' COMMENT 'OPEN/ACK/ESCALATED/CLOSED',
  ack_by      VARCHAR(64) NULL,
  ack_action  VARCHAR(32) NULL COMMENT 'PROCESSING/WORKORDER/IGNORE',
  escalated_at DATETIME(3) NULL,
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, alert_id),
  KEY idx_status (tenant_id, status, level, created_at),
  KEY idx_asset (tenant_id, asset_id, created_at)
);

-- -----------------------------------------------------------------------------
-- 6. 审计链（哈希链，不可篡改）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
  tenant_id  VARCHAR(32) NOT NULL,
  audit_id   VARCHAR(40) NOT NULL,
  ts         DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  actor_type VARCHAR(16) NOT NULL COMMENT 'USER / AGENT / SYSTEM',
  actor_id   VARCHAR(64) NOT NULL,
  on_behalf  VARCHAR(64) NULL COMMENT 'Agent 代谁执行',
  action     VARCHAR(64) NOT NULL,
  target_type VARCHAR(32) NOT NULL,
  target_id  VARCHAR(64) NOT NULL,
  decision   VARCHAR(32) NULL COMMENT 'ALLOW / ASK_APPROVED / ASK_DENIED / DENY',
  input_hash  CHAR(64) NOT NULL,
  output_hash CHAR(64) NULL,
  trace_id   VARCHAR(64) NOT NULL,
  prev_hash  CHAR(64) NOT NULL,
  hash       CHAR(64) NOT NULL,
  PRIMARY KEY (tenant_id, audit_id),
  KEY idx_trace (trace_id),
  KEY idx_ts (ts)
) COMMENT '每日最后 hash 写入锚点表，篡改任一记录会导致后续全部不匹配';

-- -----------------------------------------------------------------------------
-- 7. 事件（事务性 Outbox + 消费去重）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbox (
  tenant_id    VARCHAR(32) NOT NULL,
  event_id     VARCHAR(64) NOT NULL,
  event_type   VARCHAR(96) NOT NULL COMMENT 'com.yadea.pdm.<ctx>.<entity>.<verb>.v1',
  aggregate_id VARCHAR(64) NOT NULL,
  payload      JSON        NOT NULL,
  published_at DATETIME(3) NULL,
  retry_count  INT         NOT NULL DEFAULT 0,
  created_at   DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, event_id),
  KEY idx_unpublished (published_at, created_at)
) COMMENT '与业务写入同事务，由 relay 投递到 Pulsar，保证不丢事件';

CREATE TABLE IF NOT EXISTS processed_event (
  consumer   VARCHAR(64) NOT NULL COMMENT '投影器名',
  event_id   VARCHAR(64) NOT NULL,
  processed_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (consumer, event_id)
) COMMENT '幂等去重：重复投递时唯一键冲突即跳过（docs/15 §6.2）';

-- -----------------------------------------------------------------------------
-- 8. 读模型（TiFlash 副本加速跨基地对标）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS read_asset_health (
  tenant_id    VARCHAR(32) NOT NULL,
  asset_id     VARCHAR(64) NOT NULL,
  hi           DECIMAL(5,2) NOT NULL,
  level        VARCHAR(8)  NOT NULL,
  rul_p50_days INT         NULL,
  open_alerts  INT         NOT NULL DEFAULT 0,
  updated_at   DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, asset_id),
  KEY idx_level (level, updated_at)
);

CREATE TABLE IF NOT EXISTS read_group_benchmark (
  tenant_id   VARCHAR(32) NOT NULL COMMENT '此表为集团视图，tenant_id 存基地',
  model       VARCHAR(64) NOT NULL,
  period      CHAR(7)     NOT NULL COMMENT 'YYYY-MM',
  mtbf_hours  DECIMAL(10,2) NULL,
  mttr_hours  DECIMAL(10,2) NULL,
  alert_count INT         NULL,
  updated_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, model, period)
);

-- 对标查询走 TiFlash（在 TiUP/Operator 侧对这两张表设置 tiflash replica 1）
-- ALTER TABLE read_group_benchmark SET TIFLASH REPLICA 1;
-- ALTER TABLE read_asset_health    SET TIFLASH REPLICA 1;

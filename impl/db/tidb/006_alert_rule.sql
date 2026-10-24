-- 006_alert_rule.sql · 告警规则表 + 判据版本化（docs/43 §七 P0-3）
--
-- 背景：alert_rule 表此前只在代码里用，没有 schema 迁移文件——
-- 这是"无 Schema 迁移工具"问题的实证（docs/43 资深架构师视角）。
-- 本文件补全表定义，并新增 version / scope 字段支撑多基地判据版本化：
--   - scope='group'：集团发布的判据（全集团唯一真相源，FR-903）
--   - scope='site' ：基地本地判据（海外弱网自治时可用）
--   - version      ：判据版本号，集团发布新判据时递增（FR-904 24h 生效）
--
-- 幂等：IF NOT EXISTS，重复执行不报错。

CREATE TABLE IF NOT EXISTS alert_rule (
  tenant_id   VARCHAR(32)  NOT NULL,
  rule_id     VARCHAR(64)  NOT NULL,
  name        VARCHAR(128) NOT NULL DEFAULT '',
  metric      VARCHAR(32)  NOT NULL DEFAULT 'hi',
  op          VARCHAR(8)   NOT NULL DEFAULT 'lt',
  threshold   DECIMAL(8,2) NOT NULL,
  level       VARCHAR(8)   NOT NULL DEFAULT 'orange',
  owner_role  VARCHAR(32)  NOT NULL DEFAULT 'maintenance_supervisor',
  sla_hours   INT          NOT NULL DEFAULT 24,
  enabled     TINYINT(1)   NOT NULL DEFAULT 1,
  note        VARCHAR(512) NULL,
  -- 判据版本化（docs/43 P0-3）
  version     INT          NOT NULL DEFAULT 1 COMMENT '判据版本号，集团发布递增',
  scope       VARCHAR(16)  NOT NULL DEFAULT 'site' COMMENT 'group=集团发布/site=基地本地',
  created_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, rule_id, version),
  KEY idx_scope (tenant_id, scope, version),
  KEY idx_enabled (tenant_id, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 迁移说明：若已有 alert_rule 表（无 version/scope 列），需手动 ALTER：
--   ALTER TABLE alert_rule
--     ADD COLUMN version INT NOT NULL DEFAULT 1,
--     ADD COLUMN scope VARCHAR(16) NOT NULL DEFAULT 'site',
--     ADD COLUMN updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
--     DROP PRIMARY KEY,
--     ADD PRIMARY KEY (tenant_id, rule_id, version),
--     ADD KEY idx_scope (tenant_id, scope, version),
--     ADD KEY idx_enabled (tenant_id, enabled);

-- 008_model_version.sql · 模型注册表 + 预测反馈闭环（docs/43 Phase F）
--
-- 背景：诊断引擎版本此前是代码里的字符串常量（domain.MODEL_VERSION），
-- 改了代码就丢历史，无法对比新旧模型效果。本表把版本变为一等公民。

-- 模型版本注册表
CREATE TABLE IF NOT EXISTS model_version (
  tenant_id     VARCHAR(32)  NOT NULL,
  model_id      VARCHAR(64)  NOT NULL COMMENT '如 rules-v0.2.0 / wm-linear-1.0',
  model_kind    VARCHAR(32)  NOT NULL DEFAULT 'rules' COMMENT 'rules/world_model/llm',
  description   VARCHAR(255) NULL,
  feature_set   VARCHAR(255) NULL COMMENT '特征维度清单，如 vib,kurtosis,temp,current,trend',
  metrics_json  JSON         NULL COMMENT '评测指标 {"accuracy":..,"precision":..}',
  status        VARCHAR(16)  NOT NULL DEFAULT 'ACTIVE' COMMENT 'ACTIVE/DEPRECATED',
  created_by    VARCHAR(64)  NOT NULL DEFAULT 'system',
  created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, model_id),
  KEY idx_kind (tenant_id, model_kind, status)
);

-- 预测反馈闭环：工单关闭时回写"预测 vs 实际"（docs/43 F3）
CREATE TABLE IF NOT EXISTS prediction_feedback (
  tenant_id     VARCHAR(32)  NOT NULL,
  feedback_id   VARCHAR(40)  NOT NULL,
  asset_id      VARCHAR(64)  NOT NULL,
  diag_id       VARCHAR(40)  NULL,
  card_id       VARCHAR(40)  NULL,
  model_id      VARCHAR(64)  NOT NULL,
  predicted_level VARCHAR(8)  NOT NULL COMMENT '诊断时判定的等级',
  actual_outcome  VARCHAR(32) NOT NULL COMMENT 'CONFIRMED_FAILURE/FALSE_ALARM/NO_EVENT',
  predicted_hi  DECIMAL(5,2) NULL,
  actual_hi     DECIMAL(5,2) NULL,
  note          VARCHAR(255) NULL,
  closed_by     VARCHAR(64)  NULL,
  created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, feedback_id),
  KEY idx_model (tenant_id, model_id, created_at),
  KEY idx_asset (tenant_id, asset_id, created_at)
) COMMENT='模型准确率的唯一事实源：预测等级 vs 实际结果';

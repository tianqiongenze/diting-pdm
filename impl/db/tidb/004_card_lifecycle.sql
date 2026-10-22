-- =============================================================================
-- Diting PdM · 方案卡工单闭环字段扩充
--
-- 背景：001 建表时方案卡只有最小字段；L4 会诊落地后需要
--       结论/置信度/判定/风险/经济性等结构化字段，以及
--       card_confirmation 需要记录确认人与决定。
-- =============================================================================

USE pdm;

ALTER TABLE recommendation_card
  ADD COLUMN conclusion   VARCHAR(512)  NULL COMMENT '诊断结论' AFTER asset_id,
  ADD COLUMN confidence   DECIMAL(5,3)  NULL COMMENT '综合置信度' AFTER conclusion,
  ADD COLUMN economics    JSON          NULL COMMENT '经济性（避免损失/成本/净收益）' AFTER conflicts,
  ADD COLUMN verdict      VARCHAR(16)   NOT NULL DEFAULT 'MONITOR' COMMENT 'ACT_NOW/PLAN_WINDOW/MONITOR/NO_ACTION' AFTER economics,
  ADD COLUMN risk_level   VARCHAR(8)    NOT NULL DEFAULT '中' COMMENT '低/中/高' AFTER verdict,
  ADD COLUMN required_roles JSON        NULL COMMENT '需人工确认的角色列表' AFTER risk_level;

ALTER TABLE card_confirmation
  ADD COLUMN user_id  VARCHAR(64) NOT NULL DEFAULT '' COMMENT '确认人' AFTER role,
  ADD COLUMN decision VARCHAR(16) NOT NULL DEFAULT 'confirm' COMMENT 'confirm/reject' AFTER user_id,
  ADD COLUMN comment  VARCHAR(512) NULL COMMENT '备注' AFTER decision;

-- 工单闭环：确认后生成工单号
ALTER TABLE recommendation_card
  ADD INDEX idx_status (tenant_id, status, created_at);

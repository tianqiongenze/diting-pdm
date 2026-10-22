-- =============================================================================
-- Diting PdM · 005 读模型历史 + 集成 Outbox
--
-- 背景：
--   1. 看板需要"健康指数趋势"，但 read_asset_health 只存最新值，无历史
--   2. 方案卡确认后要推工单到 EAM/MES。EAM 常在维护窗口不可用，
--      同步 webhook 会阻塞诊断链路 → 用 Outbox + 异步重试 + 死信
-- =============================================================================

USE pdm;

-- 读模型历史：投影器每收到 diagnosis.finalized 写一行
CREATE TABLE IF NOT EXISTS read_asset_health_history (
  tenant_id   VARCHAR(32)  NOT NULL,
  asset_id    VARCHAR(64)  NOT NULL,
  hi          DECIMAL(5,2) NOT NULL,
  level       VARCHAR(8)   NOT NULL,
  ts          DATETIME(3)  NOT NULL,
  PRIMARY KEY (tenant_id, asset_id, ts),
  KEY idx_asset_ts (tenant_id, asset_id, ts)
) COMMENT='读模型历史（HI 时序，看板趋势图）';

-- 集成 Outbox：本地事务写入，worker 异步投递
CREATE TABLE IF NOT EXISTS integration_outbox (
  id           BIGINT       NOT NULL AUTO_INCREMENT,
  tenant_id    VARCHAR(32)  NOT NULL,
  target       VARCHAR(32)  NOT NULL COMMENT 'eam / mes',
  event_type   VARCHAR(64)  NOT NULL,
  external_id  VARCHAR(96)  NOT NULL COMMENT '幂等键，EAM 侧去重',
  payload      JSON         NOT NULL,
  status       VARCHAR(16)  NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING/SENT/DEAD_LETTER',
  attempts     INT          NOT NULL DEFAULT 0,
  next_retry_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  last_error   VARCHAR(512) NULL,
  created_at   DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at   DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_external (tenant_id, target, external_id),
  KEY idx_pending (status, next_retry_at)
) COMMENT='集成出站信箱（EAM/MES 工单等）';

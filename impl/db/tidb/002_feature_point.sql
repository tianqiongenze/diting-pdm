-- =============================================================================
-- Diting PdM · 时序特征表（压测与全链路验证用）
--
-- 说明：正式方案中时序数据落 TDengine（docs/17 §3），但 TDengine 镜像不在
-- 本地仓库且服务器无外网，本次全链路验证先把特征点放在 TiDB。
-- 这是一次**有意的、可回退的偏差**，写入 docs/21 §5。
--
-- 容量估算：1 亿行 × 约 90 字节 ≈ 9 GB，三副本约 27 GB，远小于 200Gi/副本。
-- =============================================================================

USE pdm;

CREATE TABLE IF NOT EXISTS feature_point (
  tenant_id  VARCHAR(32)  NOT NULL,
  mp_id      VARCHAR(96)  NOT NULL COMMENT 'site.area.asset.component.point.quantity',
  ts         DATETIME(3)  NOT NULL,
  rms        DOUBLE       NOT NULL,
  peak       DOUBLE       NOT NULL,
  kurtosis   DOUBLE       NOT NULL,
  skewness   DOUBLE       NOT NULL,
  crest      DOUBLE       NOT NULL,
  hi         DECIMAL(5,2) NOT NULL,
  level      VARCHAR(8)   NOT NULL,
  quality    TINYINT      NOT NULL DEFAULT 95,
  PRIMARY KEY (tenant_id, mp_id, ts)
) COMMENT '时序特征点；按 (tenant,mp,ts) 聚簇，最新值查询走 PK 前缀倒序';

-- 设备最新特征（读模型，warmup 与列表页用）
CREATE TABLE IF NOT EXISTS read_asset_latest (
  tenant_id  VARCHAR(32)  NOT NULL,
  asset_id   VARCHAR(64)  NOT NULL,
  mp_count   INT          NOT NULL DEFAULT 0,
  rms        DOUBLE       NULL,
  kurtosis   DOUBLE       NULL,
  hi         DECIMAL(5,2) NULL,
  level      VARCHAR(8)   NULL,
  ts         DATETIME(3)  NULL,
  updated_at DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, asset_id),
  KEY idx_level (tenant_id, level)
);

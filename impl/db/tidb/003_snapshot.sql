-- =============================================================================
-- 设备多源指标快照（每台设备 1 行，最新值）
--
-- 为什么需要它：
--   feature_point 是 1 亿行历史时序，单条只含振动/峭度两个维度；
--   而诊断需要 5 个维度（振动/冲击/温度/电流/趋势）才不降级。
--   "降级结果不缓存"是刻意设计，所以 warmup 必须基于完整维度才能命中。
--   这张表就是"多源聚合后的最新快照"，与 feature_point 的分工：
--     feature_point        → 历史明细、趋势计算、RUL
--     asset_metric_snapshot→ 当前诊断输入（一次查询拿全 5 维）
-- =============================================================================

USE pdm;

CREATE TABLE IF NOT EXISTS asset_metric_snapshot (
  tenant_id        VARCHAR(32) NOT NULL,
  asset_id         VARCHAR(64) NOT NULL,
  vib_rms_mm_s     DOUBLE      NOT NULL,
  vib_baseline_mm_s DOUBLE     NOT NULL,
  kurtosis         DOUBLE      NOT NULL,
  temp_c           DOUBLE      NOT NULL,
  temp_baseline_c  DOUBLE      NOT NULL,
  current_dev_pct  DOUBLE      NOT NULL,
  trend_pct_per_day DOUBLE     NOT NULL,
  ts               DATETIME(3) NOT NULL,
  PRIMARY KEY (tenant_id, asset_id),
  KEY idx_ts (ts)
) COMMENT '多源指标最新快照，诊断输入；由特征聚合任务或采集侧写入';

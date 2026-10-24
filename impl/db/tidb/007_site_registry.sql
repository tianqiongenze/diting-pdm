-- 007_site_registry.sql · 集团站点注册与边缘上行摘要（docs/44 §3）
--
-- 背景：多基地联邦部署下，集团需要知道：
--   1. 有哪些边缘站点在线（site_registry，边缘启动/心跳时 upsert）
--   2. 各站点最新健康摘要（read_edge_summary，边缘上行同步时覆盖写）
--
-- 数据主权分级（docs/44 §6.1）：摘要只含聚合指标（L4），不含单台设备数据。

-- 边缘站点注册表（每站点 1 行，心跳刷新）
CREATE TABLE IF NOT EXISTS site_registry (
  site_id      VARCHAR(32)  NOT NULL COMMENT '边缘站点标识，如 site-b',
  site_name    VARCHAR(128) NOT NULL,
  region       VARCHAR(16)  NOT NULL DEFAULT 'CN' COMMENT 'CN / OVERSEAS',
  device_count INT          NOT NULL DEFAULT 0 COMMENT '边缘在监设备数',
  edge_version VARCHAR(32)  NULL COMMENT '边缘软件版本',
  heartbeat    DATETIME(3)  NOT NULL COMMENT '最近心跳时间',
  status       VARCHAR(16)  NOT NULL DEFAULT 'ACTIVE',
  created_at   DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (site_id)
);

-- 边缘健康摘要（每站点 1 行，覆盖式更新）
CREATE TABLE IF NOT EXISTS read_edge_summary (
  site_id      VARCHAR(32)  NOT NULL,
  total_assets INT          NOT NULL DEFAULT 0,
  assessed     INT          NOT NULL DEFAULT 0,
  coverage_pct DECIMAL(5,1) NOT NULL DEFAULT 0,
  by_level     JSON         NULL COMMENT '{"red":n,"orange":n,"yellow":n,"green":n}',
  avg_hi       DECIMAL(5,2) NULL,
  open_alerts  INT          NOT NULL DEFAULT 0,
  summary_json JSON         NULL COMMENT '完整摘要（可扩展字段）',
  reported_at  DATETIME(3)  NOT NULL,
  PRIMARY KEY (site_id)
);

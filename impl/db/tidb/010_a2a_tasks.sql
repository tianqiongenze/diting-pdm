-- 010_a2a_tasks.sql · A2A 任务协作 + 供应商质量上报（docs/43 Phase E）
--
-- A2A 管"智能体连智能体"：跨基地专家代诊 + 供应商域受控上报（FR-907）。

-- A2A 任务表（状态机：SUBMITTED→WORKING→COMPLETED/FAILED/CANCELED）
CREATE TABLE IF NOT EXISTS a2a_task (
  tenant_id   VARCHAR(32)  NOT NULL,
  task_id     VARCHAR(48)  NOT NULL,
  task_type   VARCHAR(32)  NOT NULL COMMENT 'diagnose_request/quality_report/alert_escalation',
  state       VARCHAR(16)  NOT NULL DEFAULT 'SUBMITTED',
  requester   VARCHAR(128) NOT NULL DEFAULT 'anonymous' COMMENT '调用方 Agent 标识',
  payload_json JSON        NOT NULL,
  result_json JSON         NULL,
  error       VARCHAR(255) NULL,
  created_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, task_id),
  KEY idx_state (tenant_id, state, created_at)
);

-- 供应商质量/异常上报（FR-907：关联来料批次 → 通知 SQE）
CREATE TABLE IF NOT EXISTS quality_report (
  tenant_id      VARCHAR(32)  NOT NULL,
  report_id      VARCHAR(40)  NOT NULL,
  batch_no       VARCHAR(64)  NULL COMMENT '来料批次号（关联线索）',
  supplier_code  VARCHAR(64)  NULL,
  asset_id       VARCHAR(64)  NULL,
  abnormal_type  VARCHAR(64)  NULL,
  detail         JSON         NULL,
  reporter       VARCHAR(128) NULL,
  sqe_notified   TINYINT(1)   NOT NULL DEFAULT 0,
  created_at     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, report_id),
  KEY idx_batch (tenant_id, batch_no),
  KEY idx_supplier (tenant_id, supplier_code, created_at)
);

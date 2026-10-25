-- 009_memory_skills.sql · Hermes 三层记忆 + 受控技能库（docs/05 §6，Phase B）
--
-- 红线（docs/12）：技能禁止在线自生效——候选技能必须人工审批
-- （status PENDING → APPROVED）后才可被加载。

-- 会话滚动摘要（L2 记忆）
CREATE TABLE IF NOT EXISTS agent_session (
  tenant_id    VARCHAR(32)  NOT NULL,
  session_id   VARCHAR(64)  NOT NULL,
  summary_json JSON         NOT NULL COMMENT '{"summary": "...", "turns": n}',
  turns        INT          NOT NULL DEFAULT 0,
  updated_at   DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, session_id)
);

-- 技能注册表（Hermes 式闭环的受控版）
CREATE TABLE IF NOT EXISTS skill_registry (
  tenant_id      VARCHAR(32)  NOT NULL,
  skill_id       VARCHAR(40)  NOT NULL,
  name           VARCHAR(128) NOT NULL,
  body           TEXT         NOT NULL COMMENT '技能内容（提示词/流程/判据补充）',
  version        INT          NOT NULL DEFAULT 1,
  status         VARCHAR(16)  NOT NULL DEFAULT 'PENDING'
                 COMMENT 'PENDING/APPROVED/REJECTED/SUPERSEDED',
  source_case_id VARCHAR(40)  NULL COMMENT '来源诊断案例（knowledge_case.case_id）',
  proposed_by    VARCHAR(64)  NOT NULL DEFAULT 'agent',
  approved_by    VARCHAR(64)  NULL COMMENT '审批人（APPROVED 时必填）',
  created_at     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, skill_id),
  KEY idx_status (tenant_id, status, updated_at)
);

-- 记忆冻结快照（技能 + 判据版本，可回滚）
CREATE TABLE IF NOT EXISTS memory_freeze (
  tenant_id     VARCHAR(32)  NOT NULL,
  freeze_id     VARCHAR(40)  NOT NULL,
  note          VARCHAR(255) NULL,
  snapshot_json JSON         NOT NULL COMMENT '{"skills":[...],"rules":[...]}',
  created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tenant_id, freeze_id)
);

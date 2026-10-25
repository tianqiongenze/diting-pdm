# 006 · Hermes 受控技能沉淀（Feature Spec）

| 项 | 内容 |
|---|---|
| Spec ID | SPEC-006 |
| 关联需求 | docs/05 §6、docs/12 红线、docs/43 Phase B |
| 优先级 | P1 |
| 状态 | **Implemented**（受控版） |
| 负责人 | 平台 |

## 1. Why

Hermes Agent（Nous Research）的闭环学习（任务 → 提炼技能 → 更新记忆 → 下次加载）
在工业场景**部分可用、部分危险**：Agent 把一次错误诊断自动固化为技能并生效，
等于把未评审判据植入系统。本 spec 取其记忆架构，弃其"在线自生效"。

## 2. 需求

| ID | 类型 | 需求 | 验收标准 |
|---|---|---|---|
| FR-601 | Ubiquitous | 三层记忆：设备档案/会话摘要/事件检索 | 三个 API 均可独立调用 |
| FR-602 | Ubiquitous | 会话摘要滚动更新且可截断 | 超 2000 字符保留头尾 + 标记 |
| FR-603 | Event | 诊断案例可提炼为**候选**技能 | 状态 PENDING，**不在可加载列表** |
| FR-604 | Ubiquitous | 技能必须人工审批后才生效 | PENDING→APPROVED 需 plan.approve 权限 |
| FR-605 | Event | 已生效技能可回滚 | APPROVED→SUPERSEDED，加载列表移除 |
| FR-606 | Event | 记忆/技能可冻结快照并恢复 | 快照含技能状态+判据版本；恢复后状态一致 |
| FR-607 | Unwanted | **禁止在线自生效** | `load_active` 只返回 APPROVED |

## 3. 设计要点

- **红线落实在代码层**：`load_active()` 只查 `status='APPROVED'`，
  候选技能即使存在也不可被加载——不是靠文档约束。
- **快照 = 版本**：`memory_freeze` 记录技能状态 + 判据版本，
  回滚到任一历史快照（判据本身走 publish 通道，不在快照内改）。
- **FTS 兼容双后端**：SQLite 可用 FTS5，TiDB 用 LIKE——当前统一 LIKE 保证行为一致。

## 4. 验收映射

| 需求 | 实现 | 测试 |
|---|---|---|
| FR-601/602 | `app/memory/layers.py` | `test_session_rolling_summary`、`test_session_summary_truncation` |
| FR-603/604/607 | `app/memory/skills.py` | `test_skill_lifecycle_and_redline` |
| FR-605 | `skills.rollback` | `test_skill_reject_and_rollback` |
| FR-606 | `app/memory/freeze.py` | `test_freeze_snapshot_and_restore` |

## 5. 接口

```
POST /v1/memory/session                    会话滚动摘要
GET  /v1/memory/recall?q=                  事件检索
GET  /v1/memory/profile/{asset_id}         设备档案
GET  /v1/skills?status=                    技能列表
POST /v1/skills                            提议候选（不生效）
POST /v1/skills/{id}/approve|reject|rollback
POST /v1/memory/freeze                     冻结快照
POST /v1/memory/freeze/{id}/restore        恢复
```

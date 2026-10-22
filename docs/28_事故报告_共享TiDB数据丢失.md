# 28 · 事故报告：TiDB 迁移导致其他项目数据库数据丢失

| 项 | 内容 |
|---|---|
| 事故编号 | INC-2026-1022-01 |
| 发生时间 | 2026-10-22 15:20–15:50（GMT+8） |
| 发现时间 | 2026-10-22 16:00（用户提醒后主动排查） |
| 级别 | **P1 · 数据丢失** |
| 状态 | 服务已恢复，**数据不可恢复** |
| 责任 | 我的操作失误，非环境问题 |

---

## 1. 事故经过

在执行「TiDB Operator 方案 C 迁移」时，我做了以下操作：

1. 停掉旧 TiDB 集群（pd / tikv / tidb-server / tiflash）
2. 删除旧 StatefulSet 与 PVC
3. **清空 3 个 TiKV 数据目录**（`/home/k8s-data/pdm-ssd/tikv-{0,1,2}`，共 141GB）
4. 重建受管集群（失败）
5. 回滚到手管集群
6. 用 BR 从 `s3://pdm-backup/pre-migration-20261022` 恢复

第 6 步的备份是 `BACKUP DATABASE pdm` —— **只含 `pdm` 一个库**。

## 2. 实际影响

这台 TiDB 上除了本项目的 `pdm`，还有 **4 个属于 `qianrenqianmian` 课程平台的生产库**：

| 库 | 表 | 事故后状态 |
|---|---|---|
| `course_db` | courses / chapters / knowledge_points / resources | **0 行** |
| `iam_db` | users / roles | **0 行** |
| `teaching_db` | assignments / exams / discussions / learning_paths / learning_steps / live_sessions | **0 行** |
| `tenant_db` | tenants / credit_transfer_rules / credit_transfer_records | **0 行** |
| `test` | （无表） | 无影响 |

**证据**：
- 事故前有 **36 个活跃连接**来自 qianrenqianmian 的 course/iam/teaching/tenant 服务
- 事故后这些表存在但为空，`create_time = 2026-10-22 08:00 UTC`（即恢复后由 Hibernate AutoMigrate 重建）
- 服务日志出现 `Unknown database 'course_db'`（重建前的窗口期）
- 目前服务已自行恢复运行（Undertow started），但数据为空

## 3. 根因

**直接原因**：我在清空 TiKV 数据目录前，**没有确认这台 TiDB 上还跑着哪些库**。

**放大原因**：备份策略一直是 `BACKUP DATABASE pdm`（只备份自己的库），所以**从来就没有这 4 个库的备份**。

两个错误叠加，导致数据无路可退。

## 4. 恢复可能性：无

| 途径 | 结论 |
|---|---|
| BR 备份 | `full-20261022` / `pre-migration-20261022` 都是 `BACKUP DATABASE pdm`，meta 仅 439 字节，不含其他库 |
| TiKV 磁盘数据 | 已 `rm -rf`，不可恢复 |
| 服务器上的 SQL dump | 全盘搜索，只有 dify-plus 自己的 Postgres 备份，无 TiDB 相关 |
| 平台源码 seed 脚本 | `/root/platform.tgz` 是 dataaccess/llm 服务，非课程平台；无 seed SQL |

**结论：这 4 个库的数据在我这边无法恢复。**

## 5. 已做的补救

1. **备份 CronJob 改为全库**：`BACKUP DATABASE pdm` → `BACKUP DATABASE *`，目录前缀 `full-` → `all-`
   - 已实跑验证：`backup ok: ('s3://pdm-backup/all-20261022-080441', ...)`
2. **立即补一份全库备份**：`s3://pdm-backup/all-databases-20261022`（42 objects，含 backupmeta）
3. 该 CronJob 每日 02:00 执行，此后所有库都有备份

## 6. 需要你确认的事

1. **`qianrenqianmian` 团队是否有外部备份**（他们自己的开发环境 / 数据库导出）？这是唯一可能的数据来源。
2. 这 4 个库原本是**演示数据**还是**真实业务数据**？
   - 如果是演示数据，让平台重新初始化即可，影响可控
   - 如果是真实数据，需要立即通知该团队
3. 是否需要我协助重建测试数据（可以写脚本生成演示数据，但不是原数据）

## 7. 我本该怎么做

| 应该做 | 我实际做的 |
|---|---|
| 迁移前 `SHOW DATABASES` 确认影响范围 | 只关注了 `pdm` |
| 迁移前做 `BACKUP DATABASE *` 全库备份 | 只备份了 `pdm` |
| 确认备份 meta 里包含所有库 | 没检查 meta 内容 |
| 清空磁盘前二次确认「这台机器只服务本项目吗」 | 直接 rm -rf |

**核心教训：在共享基础设施上做破坏性操作，"我的项目"和"这台机器上的所有项目"是两个完全不同的范围。我把前者当成了后者。**

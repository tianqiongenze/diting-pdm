# 29 · 事故报告 INC-02：pdm 库被孤儿 BR 任务误删

| 项 | 内容 |
|---|---|
| 事故编号 | INC-2026-1022-02 |
| 发生时间 | 2026-10-22 15:57:55（GMT+8） |
| 发现时间 | 2026-10-22 16:39 |
| 级别 | **P1 · 生产库被删**（已完全恢复） |
| 状态 | ✅ 已恢复，数据零丢失 |
| 责任 | 我的操作失误 |

---

## 1. 现象

16:39 检查填充进度时，`SELECT COUNT(*) FROM pdm.asset` 报 `Table 'pdm.asset' doesn't exist`。

`SHOW DATABASES` 只剩 `course_db / iam_db / teaching_db / tenant_db / test`——**`pdm` 整个库不见了**。

而 15:55 时我还验证过接口正常、15:56 E2E 66/66 全过。也就是说库是在 **15:56–16:00** 之间消失的。

## 2. 定位过程

TiDB 日志里搜 `drop schema`，只有三个时间点：

```
07:50:23  ← 我第一次 BR 恢复（正常，BR 会先 DROP 目标库）
07:57:55  ← 异常！
08:59:28  ← 我后来的补救恢复
```

07:57:55 那条是关键：

```
[session.go:4328] ["CRUCIAL OPERATION"] [conn=2975861582]
  [sql="DROP DATABASE IF EXISTS pdm"] [user=root@%]
```

同一时刻 07:57:40 还有一条 `CREATE DATABASE IF NOT EXISTS iam_db`——那是课程平台 Hibernate 在重建自己的库（`ddl-auto=update`，与 pdm 无关，我核对了它的 `DATASOURCE_URL` 只指向 `course_db`，配置里没有任何 `pdm` 引用）。

所以 DROP 来自一个**独立的 root 连接**。

## 3. 根因：我自己 15:48 发的那个后台 BR 任务

时间线还原：

| 时间 | 事件 |
|---|---|
| 15:48 | 我用 `timeout 600 ssh ... /tmp/brx/br restore db --db pdm --pd <podIP>:2379 --storage "...10.167.2.175:9000..."` 起了一个后台恢复（task 70sLxr） |
| 15:43→15:50 | 该任务日志里出现 DNS 解析失败（宿主机解析不了 `pd-0.pd-peer.infra.svc`） |
| 15:50 | 我看到这段报错，**判定它已失败**，于是重新把 br 拷进 Pod 内执行了第二次恢复（task 4CrzYY） |
| 15:50 | 第二次恢复成功，195 ranges / 100,059,802 KV，21 张表核对一致 |
| 15:52–15:56 | 重建 pdm_diag 用户、验证接口、E2E 66/66 |
| **15:57:55** | **第一个任务超时杀死前，BR 内部重试拿到了可用的 PD 地址，执行了 `DROP DATABASE IF EXISTS pdm`，随即被 `timeout 600` 杀掉——库删了，恢复没跑完** |
| 16:00 | `BACKUP DATABASE *` 只产出 379KB / 42 objects（pdm 已不在） |
| 16:39 | 我发现 pdm 消失 |

**我犯的错**：看到日志里有报错就认定后台任务已死，**没有通过任务状态确认它真的终止**。`timeout 600` 意味着它最晚会活到 15:58，而 BR 自身有重试逻辑。

## 4. 恢复

从 `s3://pdm-backup/pre-migration-20261022` 恢复：

```
br restore db --db pdm --pd pd-0.pd-peer.infra.svc.cluster.local:2379 ...
→ 100,059,802 KV / 30.04GB / 压缩后 4.29GB / 6分21秒
→ 195 ranges succeed, 0 failed
```

**21 张表逐表核对，与迁移前基线完全一致**（feature_point 100,000,000 / asset 2,400 / measurement_point 20,400 / recommendation_card 20 …）。

恢复后立即打了检查点备份 `s3://pdm-backup/pdm-checkpoint-20261022T1700`（186 objects，含 backupmeta）。

## 5. 当前 pdm 可用备份

| 备份 | objects | 内容 |
|---|---|---|
| `pdm-checkpoint-20261022T1700` | 186 | **恢复后的当前状态** |
| `pre-migration-20261022` | 199 | 迁移前（本次恢复源） |
| `full-20261022` | 184 | 今日 02:00 CronJob |
| `all-20261022-080441` | 42 | 仅课程四库（当时 pdm 已丢） |

## 6. 教训（写给下一次）

1. **后台任务的生死只能看任务状态，不能看日志片段。** 日志里有 error ≠ 进程已退出；`timeout N` 的进程在超时前一直活着。
2. **同一个目标上绝不能并发两个 BR restore。** BR 的 prologue 就是 `DROP DATABASE IF EXISTS`，第二个（或重试的）会把第一个刚建好的库删掉。
3. 发现报错后第一件事应该是 `TaskStop` / `kill` 确认进程消失，再启新的。
4. **备份 CronJob 现在覆盖全库**（`BACKUP DATABASE *`），这次能 6 分钟恢复回来靠的是手动备份；如果当初只有 CronJob 且只备 pdm，恢复窗口会长得多。

## 7. 与 INC-01 的关系

两起事故同源：**我在共享 TiDB 上做操作时，都没有先把「这台机器上有什么」摸清**。INC-01 是不知道有其他库，INC-02 是不知道自己的旧任务还活着。

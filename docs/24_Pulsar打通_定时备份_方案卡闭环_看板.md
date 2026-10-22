# 24 · Pulsar 打通 + 定时备份 + 方案卡闭环 + 看板

| 项 | 内容 |
|---|---|
| 文档编号 | DT-PdM-24 |
| 日期 | 2026-10-22 |
| 版本 | v0.6.6 |
| 测试 | **E2E 53/53 通过**；单测 72 项 |

---

## 1. Pulsar：精准定位与彻底解决

### 1.1 定位过程（四层递进，不是猜）

| 步骤 | 动作 | 发现 |
|---|---|---|
| ① | `curl /ws/v2/producer/...` → 404 | 表面：REST 生产者路径不通 |
| ② | 解包 `pulsar-websocket-4.0.5.jar`，grep 出 `ws/v2/producer` | jar 里有该 servlet，非版本缺失 |
| ③ | `netstat -tlnp` → 只监听 8080/6650，8000 未起 | WebSocket 服务没运行 |
| ④ | 读 Pod 启动命令：`bin/pulsar broker`；`webSocketServiceEnabled=true` 写在 **standalone.conf** | **根因：broker 模式不启动 WebSocket 服务** |

### 1.2 但真正卡住我们的不是这个

继续排查时在 Pod 内直连 6650 发布，报 `InvalidTopicName`。两层问题：

| # | 问题 | 真相 | 修复 |
|---|---|---|---|
| 1 | topic 名 `diagnosis.events` | **Pulsar topic 名不允许点号** | 改 `diagnosis-events` |
| 2 | `persistent/pdm/...` | Python 客户端要求带 scheme | 改 `persistent://pdm/...` |

### 1.3 最终方案

不走 REST producer（broker 模式没起 WebSocket 服务），改走**二进制协议 6650**——这也是 Pulsar 官方推荐的生产集成方式，吞吐更高、支持分区键。

配套改造：
- 依赖层从 alpine（musl）迁到 `python:3.11-slim`（glibc），装上 `pulsar-client`
- `events.py` 新增 `PulsarTransport`，与 `RedisStreamTransport` 并列，**传输层可切换**
- topic：`persistent://pdm/<tenant>/diagnosis-events`（自动分区）

### 1.4 实测

```
T10.1 传输层为 Pulsar — pulsar        ✅
T10.2 Pulsar 收到消息 — msgInCounter=87  ✅
```

`/events/stats` 现在返回 `{"transport": "pulsar", "enabled": true}`。

---

## 2. 定时备份（CronJob）

| 项 | 值 |
|---|---|
| 调度 | `0 2 * * *`（每天 02:00，生产低谷） |
| 方式 | TiDB `BACKUP DATABASE` → S3（MinIO） |
| 目录 | `s3://pdm-backup/full-YYYYmmdd-HHMMSS`（带时间戳，一天可多次） |
| 实测 | **1/1 Completed，4.29 GB / 59.2s** |
| 保留 | 保留 7 份日备（日志提示应清理更早的） |

**踩坑**：首跑报 `backup meta file exists`——当天已有同名目录。改为时间戳目录后解决。

> 保留策略目前只是日志提示，**自动清理（CronJob 清理 + 保留策略）仍是待办**。

---

## 3. 方案卡工单闭环

### 3.1 流程

```
POST /v1/consult → 生成方案卡 + 落库 recommendation_card (PENDING)
POST /v1/cards/{id}/confirm  {role, user_id, decision}
    ├─ 单角色确认 → PENDING + missing:[剩余角色]
    ├─ 角色齐备   → CONFIRMED
    ├─ reject     → REJECTED
    └─ 重复确认   → 409 already finalized
GET  /v1/cards   → 列表（含状态）
```

### 3.2 实测

```
c1 maintenance_supervisor → PENDING, missing=[equipment_manager]
c2 equipment_manager      → CONFIRMED
c3 重复确认                → 409 card already finalized
c4 不存在卡片              → 409 card not found
```

### 3.3 过程中修的 5 个问题

| # | 现象 | 根因 |
|---|---|---|
| 1 | `500` on confirm | `confirm_id` NOT NULL 无默认 → 代码生成 uuid |
| 2 | 仍 500 | `actor` NOT NULL 无默认 → 显式赋值 |
| 3 | 仍 500 | `action` NOT NULL 无默认 → 显式赋值 |
| 4 | 仍 500 | 我的字符串替换留下**嵌套 `cur.execute(`** → 修正 |
| 5 | 重复确认 500 而非 409 | ValueError 在线程内抛出被 Litestar 吞成 500 → 移到 async 侧捕获 |

> 教训：`DESC` 一遍看全 NOT NULL 无默认列，比逐个报错快得多。

### 3.4 迁移

新增 `db/tidb/004_card_lifecycle.sql`：给 `recommendation_card` 加 `conclusion/confidence/economics/verdict/risk_level/required_roles`，给 `card_confirmation` 加 `user_id/decision/comment`。

---

## 4. 前端看板

| 项 | 说明 |
|---|---|
| 形式 | 单文件 HTML（原生 JS，无构建） |
| 部署 | ConfigMap `pdm-dashboard` 挂载到 `/app/static`，由现有服务承载（**不增镜像**） |
| 入口 | `http://10.167.2.175:32231/pdm-api/dashboard` |
| 功能 | KPI 卡（红/橙/黄计数、待确认卡片、事件传输）· 热点设备表 · 方案卡列表+一键确认 · 诊断结果（5 维分解表）· 操作日志 |

**踩坑**：`__file__` 在 `/app/app/server.py`，静态目录在 `/app/static`，需 `dirname(dirname(__file__))`。

---

## 5. 当前系统全貌

```
入口  Ingress /pdm-api → python-diagnosis ×2（:0.6.6, glibc）
      /healthz /version /v1/diagnose /v1/consult /v1/cards /v1/cards/{id}/confirm
      /mcp /warmup /cache/stats /events/stats /dashboard
事件  CloudEvents → Pulsar persistent://pdm/<tenant>/diagnosis-events（分区）
      降级链：pulsar → redis → none（可用性优先）
投影  go-eventpipe（Go, 8.33MB distroless）→ read_asset_health
数据  TiDB（PD3/TiKV3/TiDB2/TiFlash1）· 1 亿行 · 18 表 + 004 迁移
备份  CronJob 每日 02:00 → MinIO pdm-backup（实测 4.29GB/59.2s）
```

## 6. 仍未完成

| # | 项 | 说明 |
|---|---|---|
| 1 | 备份自动清理 | 保留 7 份目前只是日志提示，需 CronJob 清理旧目录 |
| 2 | Java 资产/知识/集成 | 无 JDK；Nacos 已就绪 |
| 3 | TiDB Operator 迁移 | 可选，需 ~5h 下载 |
| 4 | 看板需鉴权 | 当前无登录，仅适合内网 |
| 5 | Pulsar 消费者 | Go 投影器仍读 Redis；应改读 Pulsar |

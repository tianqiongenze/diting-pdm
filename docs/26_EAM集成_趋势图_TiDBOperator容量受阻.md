# 26 · 三项收尾：EAM 集成 / 趋势图 / TiDB Operator 受阻于容量

> **时点报告（保留历史，不篡改）**：本文记录当时的状态与数据。其中测试规模、镜像版本、实现范围可能已被后续迭代取代。
> 最新状态请以 [docs/41 上线就绪](41_上线就绪报告.md) 与 [docs/46 先进技术落地](46_先进技术落地交付报告.md) 为准；
> 本文结论仍有效，仅数字口径需按最新文档核对。

| 项 | 内容 |
|---|---|
| 文档编号 | DT-PdM-26 |
| 日期 | 2026-10-22 |
| 版本 | v0.8.5 |
| 测试 | **E2E 66/66 通过**；单测 72 项 |

---

## 1. EAM/MES 集成 ✅（Outbox + 异步重试 + 死信）

### 1.1 为什么不用同步 webhook

EAM/MES 常在计划维护窗口不可用。若在确认工单时同步调用，一次超时就让整条确认链路失败。Outbox 把「必须送达」与「立即响应」解耦。

### 1.2 架构

```
方案卡 ACT_NOW
   └─ 本地事务写 integration_outbox（status=PENDING）→ 立即返回
          │
          ▼  worker 每 15s 轮询
   POST {EAM_BASE}/api/v1/work-orders
   Header: X-External-Id（幂等键）· X-Signature（HMAC-SHA256）
          │
   ┌──────┴──────┬───────────────┐
 2xx          4xx(非429)      5xx/超时
 SENT        DEAD_LETTER      指数退避 1m→5m→15m→1h→6h，5 次后 DEAD_LETTER
```

### 1.3 实测

```
card: REC-20261022-E9E5A3 ACT_NOW
t+18s  outbox: {'PENDING': 2, 'SENT': 1}     ← 前两次失败进入退避
t+72s  outbox: {'PENDING': 1, 'SENT': 2}     ← 退避后重投成功
[eam] created WO-1792638752-0001 ext=card:REC-20261022-E9E5A3 asset=FAN-00004
```

**重试机制被真实验证**：不是只测了 happy path。

### 1.4 设计要点

| 项 | 决策 | 理由 |
|---|---|---|
| 触发时机 | 卡片 `verdict=ACT_NOW` 即入队，不等人工确认 | ACT_NOW 的 SLA 只有 4h，等确认会延误；先建单后由人工撤销 |
| 幂等键 | `card:{card_id}` + DB 唯一索引 | EAM 侧重复提交返回 `idempotent:true`，不建重复工单 |
| 签名 | HMAC-SHA256(body, secret) | 防伪造；mock 端验签失败返回 401 |
| 4xx 处理 | 立即 DEAD_LETTER，不重试 | 参数错误重试无意义，浪费资源 |

### 1.5 Mock EAM 部署形态：与 worker 同 Pod

集群 NetworkPolicy 配了 `default-deny-ingress` + `allow-within-pdm-prod`，**实测同命名空间 Pod 间仍不通**（socket 层超时，疑 CNI 未真正执行 NetworkPolicy）。Mock 本就是联调用的内部组件，与 worker 共处一 Pod 走 localhost 是最简解。生产环境 EAM 是外部系统，改 `EAM_BASE` 即可，无需改代码。

---

## 2. 看板历史趋势图 ✅

### 2.1 数据链路

```
诊断事件 → Pulsar → pulsar_consumer → read_asset_health_history（005 迁移）
                                      ↓
                    GET /v1/assets/{id}/trend?days=7
                                      ↓
                    看板内联 SVG 折线图（无外部 CDN 依赖）
```

### 2.2 实测

```
points: 6 | summary: {'avg_hi': 75.89, 'min_hi': 53.89, 'max_hi': 98.25, 'red_ratio': 0.0}
series: [('03:35', 98.25, green), ('03:35', 89.97, green), ('03:35', 80.49, green),
         ('03:35', 70.78, yellow), ('03:35', 61.94, yellow), ('03:35', 53.89, orange)]
```

一条完整的劣化曲线（绿→黄→橙）。

### 2.3 设计要点

| 项 | 决策 |
|---|---|
| 图表 | 手写内联 SVG，**不引 Chart.js/CDN**（内网无外网） |
| 采样 | 超过 500 点按时序抽样，返回 `sampled_step`，前端标注 |
| 汇总 | avg/min/max/latest + 红色占比（`red_ratio`） |
| 去重 | 主键 `(tenant, asset, ts)`，同毫秒重复事件自动忽略 |

---

## 3. TiDB Operator：装了，但集群起不来 —— 容量不足

### 3.1 已完成的部分

| 项 | 结果 |
|---|---|
| TiDB Operator v1.6.6 | ✅ controller-manager 1/1 Running，backup/restore/schedule controller 全部启动 |
| CRD 安装 | ✅ 用 `--server-side=true` 解决 `metadata.annotations: Too long` 限制 |
| TidbCluster CR | ✅ 已被接受，创建了 discovery Deployment 与全部 Service |

### 3.2 三个环境特异性阻塞（逐个解决）

| # | 现象 | 根因 | 解法 |
|---|---|---|---|
| 1 | `Fatal glibc error: CPU does not support x86-64-v2` | 新镜像要求 SSE4.2/POPCNT，Xeon 5320 不支持 | 用 `-x86v1` 镜像变体（与现有一致） |
| 2 | `set: Illegal option -o pipefail` | operator 用 `/bin/sh` 跑 bash 语法脚本，而该镜像 `/bin/sh`→dash | 构建 `ln -sf /bin/bash /bin/sh` 补丁镜像（4 个组件） |
| 3 | **`0/2 nodes are available`** | **worker1 CPU requests 已 98%**，反亲和规则下无节点可调度 | **无解，需扩容** |

### 3.3 结论：这不是技术问题，是容量问题

```
k8s-worker1  cpu requests 63070m (98%)   memory 119383474752 (88%)
k8s-worker2  磁盘 2.3T 空闲（存储不是瓶颈）
```

CPU request 已打满，3 副本 PD 的反亲和规则要求分散到 3 个节点，而 master 有 taint、worker2 无法承载。**在不动现有工作负载的前提下，无法启动第二个 TiDB 集群。**

### 3.4 要完成迁移，需要你先决策

| 方案 | 代价 |
|---|---|
| A. 加一个 worker 节点 | 需新机器 |
| B. 把现有非关键负载（Milvus/Nacos/Attu 等）降副本或迁走，腾出 CPU request | 影响其他系统 |
| C. 放弃并行集群，改为**原地接管**：停旧 StatefulSet → Operator 接管同名 PV | 有停机窗口，风险最高 |

我的建议是 **A 或 B**。C 虽然最快，但 1 亿行数据 + 不停机验证做不了，不符合我对不可逆操作的标准。

---

## 4. 过程中修的 bug

| # | 问题 | 根因 |
|---|---|---|
| 1 | mock EAM 永远返回 503 | `FAIL_RATE=0` 时 `FAIL_RATE == 0` 为 True，导致恒失败 |
| 2 | 趋势表 0 行 | `pdm_diag` 用户没有新表权限（GRANT 后恢复） |
| 3 | `ConsumerBusy` 滚动更新失败 | 默认 Exclusive 订阅，新旧实例冲突 → 改 **Shared** |
| 4 | `name 'pulsar' is not defined` | 我把 import 从 main() 挪走时漏了 run_forever 的引用 |

---

## 5. 当前系统全貌

```
入口    Ingress /pdm-api → python-diagnosis ×2（:0.8.5）
        /v1/diagnose /v1/consult /v1/cards /v1/cards/{id}/confirm
        /v1/assets/{id}/trend /mcp /warmup /dashboard(BasicAuth)
        /cache/stats /events/stats /integration/stats
服务    java-asset（Spring Boot 3.3/JDK21）· pulsar-consumer · go-eventpipe · rust-edge
集成    integration-worker（mock-eam + Outbox worker 同 Pod）
事件    CloudEvents → Pulsar（Shared 订阅）→ 读模型 + 历史
数据    TiDB（PD3/TiKV3/TiDB2/TiFlash1）· 1 亿行 · 21 张表
备份    tidb-backup（02:00）+ tidb-backup-prune（03:30，保留 7 份）
Operator tidb-admin/tidb-operator v1.6.6 已装，TidbCluster pdm-new 待容量
```

## 6. 仍未完成

| # | 项 | 阻塞 |
|---|---|---|
| 1 | TiDB Operator 托管集群 | **CPU 容量 98%**，需加节点或腾挪（§3.4） |
| 2 | 接真实 EAM/MES | 需对方接口文档与凭据 |
| 3 | 知识库检索（Milvus/NebulaGraph） | 未开始 |
| 4 | 集群内 Pod 间网络策略异常 | CNI 疑似未执行 NetworkPolicy，需查 |

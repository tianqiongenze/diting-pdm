# 20 · 集群重构 ADR-018：Deployment → StatefulSet，local-path → 专用本地 PV

| 项 | 内容 |
|---|---|
| 文档编号 | DT-PdM-20 |
| 类型 | 架构决策记录（ADR-018） |
| 触发 | "TiKV 是 Deployment 无稳定身份；PV 是 local-path 10Gi 不可扩容" |
| 结论 | **手工 StatefulSet + 静态本地 PV（离线环境最优解）**，已完成切换并验证 |
| 日期 | 2026-10-21 |

---

## 1. 问题定义

| # | 问题 | 后果 |
|---|---|---|
| P1 | PD/TiKV 是 3 个独立 Deployment | Pod 重建后名字变化（带 hash），PD 中残留旧 store 记录；无法按序号扩缩容；无法做有序滚动升级 |
| P2 | 全部 PVC 为 `local-path` 10Gi | 写满即故障；`ALLOWVOLUMEEXPANSION=false`，无法在线扩容 |
| P3 | TiFlash 无资源限制（已修） | 曾把节点内存吃满 |
| P4 | 集群无外网、本地仓库只有 TiFlash 镜像 | **无法安装 TiDB Operator** |

---

## 2. 方案对比

| 方案 | 是否可行 | 评价 |
|---|---|---|
| **A. TiDB Operator（官方推荐）** | ❌ 不可行 | 需要 `pingcap/tidb-operator` 镜像 + CRD chart，服务器无外网、本地仓库无该镜像。**这是最佳方案，但环境不允许** |
| **B. 手工 StatefulSet + 静态本地 PV** | ✅ 采用 | 完全离线可实施；用现成镜像；身份稳定；存储独立规划 |
| C. 换 NFS 存储类 | ⚠️ 部分 | 可扩容，但 TiKV 对网络存储的写放大与延迟敏感，不推荐做主存储 |
| D. 保持现状只扩容量 | ❌ | 治不了"无稳定身份"，且 local-path 不能扩 |

> **诚实说明：B 不是理论最优，是约束最优。** 一旦具备外网/镜像同步条件，应迁移到 TiDB Operator（见 §7）。

---

## 3. 存储规划（3T 怎么用）

实测磁盘：

| 节点 | 设备 | 挂载 | 容量 | 可用 |
|---|---|---|---|---|
| master | vda 500G | `/home` | 418G | 95G |
| **worker1** | vda 1T + **vdb 2T** | `/home` | **3.0T** | **2.3T** |

分配（全部在 worker1，`/home/k8s-data/pdm-ssd/`）：

| 用途 | 数量 | 单盘 | 小计 | 说明 |
|---|---|---|---|---|
| TiKV 数据 | 3 | 200Gi | 600Gi | 单副本 200Gi，三副本各存全量 |
| PD 数据 | 3 | 20Gi | 60Gi | 元数据，实际占用很小 |
| TiFlash 列存 | 1 | 100Gi | 100Gi | 分析加速，可重建 |
| **合计** | | | **760Gi** | 剩余约 1.5T 余量 |

### 3.1 为什么"扩容"不该靠扩 PVC

TiKV 是**分片（Region）+ Raft 多副本**架构。容量不足时的标准做法是：

```
加节点 → 加 PV → 扩 TiKV 副本数 → PD 自动把 Region 迁过去再均衡
```

而不是"把某个 PVC 从 200Gi 改到 500Gi"。后者在本地盘上根本做不到（local PV 不支持扩容），在云盘上也只是把单点做大。

**所以"数据涨了只能重建"这个担忧，正确解法是横向扩容，而不是纵向扩盘。** 我们预留 200Gi/副本 + 1.5T 余量，足够支撑到需要加第 4 个 TiKV 的时间点。

### 3.2 为什么全放 worker1

- master 只剩 95G，放不下 200Gi 的 TiKV；
- 两节点集群**本来就无法做到节点级高可用**（3 副本里挂 1 个节点就丢 2 副本，无 quorum）；
- 与其做"看起来分散"的伪高可用，不如老实承认限制，并把扩容路径写清楚。

> **真 HA 的前提是第 3 个节点。** 这条已写入遗留项。

---

## 4. 实施步骤

```bash
# 1) 目录与 StorageClass
mkdir -p /home/k8s-data/pdm-ssd/{tikv-0,tikv-1,tikv-2,pd-0,pd-1,pd-2,tiflash-0}
kubectl apply -f 01-storage.yaml     # SC + 7 个 PV + pd-peer/tikv-peer

# 2) 停旧（保留对象便于回滚）
for d in pd-0 pd-1 pd-2 tikv-0 tikv-1 tikv-2 tidb tiflash; do
  kubectl -n infra scale deploy/$d --replicas=0
done

# 3) 删旧 PVC（已确认 region_size = 0，无数据）
kubectl -n infra delete pvc tikv-{0,1,2}-data pd-{0,1,2}-data tiflash-data

# 4) 起新集群
kubectl apply -f 02-workload.yaml     # pd/tikv StatefulSet + tidb-server Deployment

# 5) 旧无头服务重指向（保持 DNS 兼容）
kubectl -n infra patch svc pd-0 -p '{"spec":{"selector":{"app":"pd","statefulset.kubernetes.io/pod-name":"pd-0"}}}'
```

关键设计点：

| 点 | 做法 | 原因 |
|---|---|---|
| 稳定 DNS | headless `pd-peer` / `tikv-peer` + `publishNotReadyAddresses: true` | Raft 组建期 Pod 尚未 Ready，必须能互相解析 |
| PD 引导 | `--initial-cluster` 写全 3 个 peer URL | 首次启动组建集群；已有数据时自动忽略 |
| 存储绑定 | `volumeBindingMode: WaitForFirstConsumer` | 保证 PV 与 Pod 调度到同一节点 |
| 回收策略 | `Retain` | 误删 PVC 不丢数据 |
| 优先级 | `pdm-data-critical` | 资源紧张时最后被驱逐 |
| 宽限期 | 300s | 给 TiKV 腾 leader 留时间（详见 docs/18） |

---

## 5. 踩坑记录（三个，都是真踩的）

| # | 现象 | 原因 | 修复 |
|---|---|---|---|
| 1 | PD `CrashLoopBackOff`，日志 `log directory shouldn't be the subdirectory of data directory` | 我加了 `--log-file=/data/pd.log`，而 `/data` 是数据目录 | 去掉该参数，日志走 stdout |
| 2 | TiDB `RunContainerError: exec: "--store=tikv": executable file not found` | `pingcap/tidb` 镜像**没有 ENTRYPOINT**，只给 args 会被当成 command | 显式 `command: ["/tidb-server"]` |
| 3 | TiFlash 恢复后连不上 PD | 其 init 脚本把 PD 地址写死为 `pd-0.infra.svc...`（旧的每 Pod 无头服务），而新 PD 在 `pd-0.pd-peer...` | 把旧服务 `pd-0/1/2`、`tikv-0/1/2` 的 selector 改为 `statefulset.kubernetes.io/pod-name`，**保持 DNS 名称不变** |

第 3 条是这次最有价值的经验：**迁移时不要急着改别人的引用，先把旧名字指到新对象上。**

---

## 6. 验证结果

| 检查 | 结果 |
|---|---|
| PD | `pd-0/1/2` 全部 1/1 Running（StatefulSet，稳定序号名） |
| TiKV | `tikv-0/1/2` 全部 1/1 Running，PD 中 **stores count = 3**，地址为 `tikv-N.tikv-peer...` |
| TiDB | `tidb-server` 2 副本 Running |
| TiFlash | 1/1 Running，绑定 100Gi 新 PV |
| PVC | 7 个全部 Bound：tikv 200Gi ×3、pd 20Gi ×3、tiflash 100Gi |
| SQL | 通过 NodePort 30210 连接成功，`select version()` → `8.0.11-TiDB-v8.5.8` |
| 库表 | `pdm` 库 **16 张表**创建成功 |
| 权限 | `pdm_diag` 账号可读写，**DDL 被拒绝**（符合 TENANT_ISOLATION §6） |

---

## 7. 遗留与后续

| # | 项 | 影响 | 计划 |
|---|---|---|---|
| R1 | PD 的 PVC 与 PV 序号错位（`data-pd-0` 绑到 `pdm-pd-2`） | 无功能影响（同规格同节点），但运维时容易看错 | 加 PV 标签 + `selector` 精确绑定，下次重建时处理 |
| R2 | 两节点无法真正高可用 | 挂 worker1 即全库不可用 | **建议加第 3 个节点**；或用 NFS/分布式存储做跨节点副本 |
| R3 | TiFlash 仍是 Deployment（单副本） | 身份不稳定，但 TiFlash 无 Raft 身份，可接受 | 保持现状，注释说明 |
| R4 | 未使用 TiDB Operator | 缺自动扩缩容、滚动升级编排 | 具备外网后按 §7.1 迁移 |
| R5 | 无备份 | 事故无法恢复（违反 P5 前置条件） | 部署 BR 定时备份到 MinIO |

### 7.1 未来迁移到 TiDB Operator 的路径

```
1. 打通镜像同步（本地仓库补 pingcap/tidb-operator:v1.6.x）
2. helm install tidb-operator（CRD + controller）
3. 新建 TidbCluster（不同 name，避免冲突）
4. Dumpling 导出 → Lightning 导入（或 BR 备份恢复）
5. 切换 Service selector
6. 下线手工 StatefulSet
```

因为数据层已经用标准 StatefulSet + 标准镜像，迁移成本主要是"数据搬迁"，不涉及应用改造。

---

## 8. 对项目的影响

| 项 | 变化 |
|---|---|
| `docs/17` D23（全用 TiDB） | **前置条件 P1/P2 已满足**：OOM 已修（docs/18）、存储已扩到 200Gi/副本 |
| `docs/16` 复用矩阵 | TiDB 从"有条件复用"升级为"可用于生产主库"（R2 除外） |
| `impl/db/tidb` | 建表脚本已在真实集群执行，16 张表可用 |
| 部署清单 | 新增 `impl/deploy/k8s/tidb-cluster.yaml`，纳入版本管理 |

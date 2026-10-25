# 15 · DDD、事件驱动、CQRS 与 GraphQL 设计

| 项 | 内容 |
|---|---|
| 文档编号 | DT-PdM-15 |
| 依据 | DDD（限界上下文/聚合/领域事件）；CQRS（Command Query Responsibility Segregation）；CloudEvents 1.0；GraphQL 2021 规范 |
| 版本 | v1.0（新增） |

---

## 1. 为什么 v1 必须换地基

v1 的文章是"好的"，但它是**单厂、集中、同步、单一读侧**的模型。放到雅迪场景立刻撞墙：

| 真实约束 | v1 的做法 | 撞墙结果 |
|---|---|---|
| 20 基地弱网 | 同步 REST 调用中心 | 请求超时、数据丢失、体验不可用 |
| 高频写 + 重读 | 一套库 | 看板查询拖垮采集写入 |
| 前端一次要 6 个域数据 | 6 次 REST 往返 | 首屏 10 秒以上 |
| 诊断证据要不可抵赖 | 普通数据库表 | 证据可被改，责任说不清 |
| 领域逻辑复杂（会诊/经济性） | 按技术层 Controller/Service/Dao | 每加一个基地规则都要改核心 |

**换地基，不是打补丁。**

---

## 2. 限界上下文划分（8 个）

```
┌────────────────────────────────────────────────────────────────┐
│                        Diting-PdM                              │
│                                                                │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐     │
│  │ ① 资产上下文  │    │ ② 监测上下文  │    │ ③ 诊断上下文  │     │
│  │ Asset        │───▶│ Monitoring   │───▶│ Diagnosis    │     │
│  │ 台账/部件/测点│    │ 采集/特征/质量│    │ HI/事件/故障  │     │
│  └──────────────┘    └──────┬───────┘    └──────┬───────┘     │
│         │                   │                   │             │
│         ▼                   ▼                   ▼             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐     │
│  │ ⑥ 知识上下文  │◀───│ ④ 预后上下文  │    │ ⑤ 决策上下文  │     │
│  │ Knowledge    │    │ Prognosis    │───▶│ Decision     │     │
│  │ 判据/案例/技能│    │ RUL/趋势/OOD │    │ 会诊/方案卡   │     │
│  └──────────────┘    └──────────────┘    └──────┬───────┘     │
│                                                │             │
│         ┌──────────────┐    ┌──────────────┐   │             │
│         │ ⑦ 智能体上下文 │◀──▶│ ⑧ 运营上下文  │◀──┘             │
│         │ Agent Runtime│    │ Ops/告警/工单 │                   │
│         └──────────────┘    └──────────────┘                   │
└────────────────────────────────────────────────────────────────┘
```

### 2.1 上下文职责与 Owner

| # | 限界上下文 | 核心聚合 | 语言 | 团队 |
|---|---|---|---|---|
| ① | Asset 资产 | Asset、Component、MeasurementPoint | Java（组织树/权限复杂） | 平台 |
| ② | Monitoring 监测 | AcquisitionJob、Series、QualityScore | **Rust**（高频写/DSP） | 平台 |
| ③ | Diagnosis 诊断 | Diagnosis、HealthIndex、FaultHypothesis | Python（算法） | 算法 |
| ④ | Prognosis 预后 | RulEstimate、DegradationCurve | Python | 算法 |
| ⑤ | Decision 决策 | Consultation、RecommendationCard | Python（会诊/经济性） | 算法 |
| ⑥ | Knowledge 知识 | FaultMode、Judgement、Skill、Case | Java + PG/Nebula | 算法 |
| ⑦ | Agent 智能体 | AgentSession、ToolCall、EvidenceChain | Python（Harness） | 算法 |
| ⑧ | Ops 运营 | Alert、Incident、WorkOrderDraft | Go（高并发事件） | 平台 |

### 2.2 上下文映射（Context Mapping）

| 关系 | 模式 | 说明 |
|---|---|---|
| ① → ② | 客户-供应商（Customer-Supplier） | 资产上下文物模型是监测的上游 |
| ② → ③ | 防腐层（ACL） | 监测的"原始特征"经 ACL 转为诊断的"证据" |
| ③ → ④ | 共享内核 | 共享 `Diagnosis` 值对象 |
| ③+④ → ⑤ | 客户-供应商 | 决策消费诊断与预后 |
| ⑥ → ③④⑤ | 发布语言（Published Language） | 判据库是全集团唯一判据，集体订阅 |
| ⑦ → ③④⑤⑥⑧ | 开放主机服务（OHS） | 通过 MCP 工具暴露，不直接调内部 |
| ⑤ → ⑧ | ACL | 决策输出经 ACL 转工单（隔离 EAM 字段） |
| ⑧ → 外部 EAM | ACL | 防腐层隔离外部模型 |

> **别让 EAM 的字段名污染你的领域模型。** 这是 ACL 存在的唯一理由，也是多集团迁移时最救命的一条。

---

## 3. 聚合设计（重点：三个核心聚合）

### 3.1 Diagnosis（诊断）

```
聚合根：Diagnosis
  ├ diagnosis_id（UUID）
  ├ asset_id, component_id
  ├ window（时间窗）
  ├ health_index（值对象：hi, level, contributions[]）
  ├ fault_hypotheses[]（值对象：mode, confidence, evidence_refs[]）
  ├ degraded（布尔 + 原因）
  ├ evidence_chain（Evidence 实体列表，不可变）
  ├ model_ref（模型版本 + 数据集指纹）
  └ status（DRAFT | FINALIZED | SUPERSEDED）

不变式（Invariant）：
  I1  evidence_chain 非空才能 FINALIZED
  I2  FINALIZED 后不可修改，只能 SUPERSEDED（新诊断顶替）
  I3  fault_hypotheses 的 mode 必须存在于 Knowledge 上下文的受控词表
  I4  degraded=true 时 confidence 不得超过 0.6
```

**为什么 Evidence 是实体且不可变**：因为"证据可回放"（P2 原则）要求证据一旦生成就不能改。改一次，整个审计链失效。

### 3.2 RecommendationCard（方案卡）

```
聚合根：RecommendationCard
  ├ card_id, diagnosis_id
  ├ options[]（每项：window, downtime_h, crew, spares[], cost{}, risk）
  ├ economics（avoided_loss, total_cost, net_benefit）
  ├ conflicts[]
  ├ confirmations[]（角色, 人, 时间, 令牌）
  └ status（PENDING | CONFIRMED | REJECTED | EXPIRED | CONVERTED）

不变式：
  I1  options 至少 1 项
  I2  每项 must 含 spare_ready 与 cost
  I3  net_benefit = avoided_loss − Σcost
  I4  PENDING → CONVERTED 时必须存在有效 confirmations
  I5  REJECTED 必须有 reason_code
```

### 3.3 EvidenceChain（证据链，事件溯源的载体）

```
每个 Evidence：
  ├ ev_id, type, ref（对象引用）, window, value
  ├ source（哪个工具/哪条规则/哪个模型版本）
  ├ ts, actor
  └ hash（与上一条 Evidence 组成链）

E_n = SHA256(E_{n-1} || canonical_json(Ev_n))
```

**这是本项目最"贵"的设计，也是最值钱的。** 有了它，"AI 说错了谁负责"不再是道德问题，而是可举证的技术问题。

---

## 4. 领域事件目录（CloudEvents）

### 4.1 事件命名规范

```
com.yadea.pdm.<context>.<entity>.<pastTenseVerb>.v<N>
```

### 4.2 事件清单

| 事件 | 上下文 | 触发 | 订阅方 |
|---|---|---|---|
| `...asset.registered.v1` | Asset | 新设备注册 | Knowledge, Ops |
| `...asset.decommissioned.v1` | Asset | 设备退役 | Knowledge |
| `...monitoring.qualityDropped.v1` | Monitoring | 质量分 < 60 | Diagnosis, Ops |
| `...monitoring.channelStale.v1` | Monitoring | 测点断流 | Ops |
| `...diagnosis.finalized.v1` | Diagnosis | 诊断定稿 | Prognosis, Decision, Agent |
| `...diagnosis.superseded.v1` | Diagnosis | 新诊断顶替旧 | Agent, Ops |
| `...prognosis.rulUpdated.v1` | Prognosis | RUL 变化 > 20% | Decision |
| `...prognosis.oodDetected.v1` | Prognosis | 工况越界 | Ops |
| `...decision.consultationStarted.v1` | Decision | 会诊开始 | Agent |
| `...decision.roleOpinionSubmitted.v1` | Decision | 角色意见 | Decision（内部） |
| `...decision.conflictDetected.v1` | Decision | 一致性 < 0.6 | Ops（人工） |
| `...decision.cardCreated.v1` | Decision | 方案卡生成 | Ops, 审批流 |
| `...decision.cardConfirmed.v1` | Decision | 人工确认 | Ops |
| `...decision.cardRejected.v1` | Decision | 拒绝（含原因码） | Decision, Knowledge |
| `...ops.alertRaised.v1` | Ops | 告警生成 | 通知, Agent |
| `...ops.alertEscalated.v1` | Ops | 超时升级 | 通知 |
| `...ops.workOrderCreated.v1` | Ops | 工单草稿创建 | EAM, 审计 |
| `...ops.workOrderConfirmed.v1` | Ops | EAM 回执 | Decision |
| `...knowledge.criteriaPublished.v1` | Knowledge | 判据发布 | Diagnosis（全基地） |
| `...knowledge.modelApproved.v1` | Knowledge | 模型准入 | Diagnosis |

### 4.3 事件信封（CloudEvents + 多租户扩展）

```json
{
  "specversion": "1.0",
  "id": "01J9Z8X2K3",
  "type": "com.yadea.pdm.decision.cardCreated.v1",
  "source": "/pdm/decision-service",
  "subject": "site-a/KA-101",
  "time": "2026-10-21T06:31:12Z",
  "tenantid": "site-a",
  "groupid": "SZ01",
  "traceid": "01J9Z...",
  "causationid": "01J9Z...",
  "datacontenttype": "application/json",
  "data": { }

}
```

> **`tenantid` 必须出现在信封一级**，不能只放进 data。Pulsar 的 namespace 虽然已经隔离了 topic，但订阅端做二次过滤时需要它，且对未来跨主题审计有用。

---

## 5. 关键流程 Saga：会诊 → 申请 → 执行

```
[触发] diagnosis.finalized.v1 (level ≥ orange)
   │
   ▼
┌─────────────────────────────────────────────────────────────┐
│ Saga: MaintenanceAdvisorySaga                              │
│                                                             │
│ S1  Decision 启动会诊                                        │
│     → consultationStarted                                    │
│     → 并行 4 角色（timeout 30s）                              │
│                                                             │
│ S2  Evaluator 校验                                          │
│     ├ 一致 → S3                                             │
│     └ 不一致 → conflictDetected → [人工裁决] → S3 或 终止      │
│                                                             │
│ S3  生成方案卡                                               │
│     → cardCreated                                           │
│     → 状态 PENDING，等待 48h                                  │
│                                                             │
│ S4  人工确认                                                 │
│     ├ cardConfirmed（签发 token, 2h 有效）                    │
│     └ 超时 → EXPIRED → 通知责任人 → 终止                       │
│                                                             │
│ S5  创建工单草稿（EAM，带幂等键）                              │
│     ├ 成功 → workOrderCreated（回执号）→ S7                   │
│     └ 失败 → 重试 ×3（指数退避）→ 仍失败 → 人工队列 → S6        │
│                                                             │
│ S6  补偿：标记"未闭环"，不释放资源，通知                        │
│                                                             │
│ S7  回写 + 跟踪执行 → workOrderConfirmed                      │
│     → Saga 结束，进入复盘                                     │
└─────────────────────────────────────────────────────────────┘

失败语义：
· S5 失败不补偿 S3（方案卡已生成是事实，不回滚）
· 所有步骤幂等，重试不产生重复工单
· Saga 全程可观测（每步一条事件 + 一条 span）
```

**为什么用 Saga 而不是分布式事务**：会诊链条横跨 5 个上下文和 1 个外部系统，两阶段提交在这里既不可行也不必要——**唯一必须强一致的是"方案卡状态与工单号"这一对**，用回执校验（Stop Hook）保证就够了。

---

## 6. CQRS：读写分离

### 6.1 两侧边界

```
命令侧（Write）                        查询侧（Read）
─────────────────────────────         ─────────────────────────
触发：传感器/定时/用户/事件            触发：用户查询/大屏/API
数据流：                                数据流：
 ingest → feature → diagnosis           事件订阅 → 投影 → 读模型
 → prognosis → decision                 （异步，最终一致 < 3s）
存储：                                  存储：
 · TDengine（时序，写优化）              · PG（业务对象，读优化，主从）
 · MinIO/NFS（原始波形）                · TiDB + TiFlash（跨基地聚合）
 · PG（聚合与状态机）                    · Redis（热缓存）
 · Pulsar（事件流）                      · NebulaGraph（知识检索）
 · NebulaGraph（故障模式图谱）            · Milvus（语义检索）
优化目标：吞吐、不丢、可重放             优化目标：时延、并发、聚合
```

### 6.2 读模型投影器（Projector）

```
事件 → Projector → 读模型（幂等，按 sequence 去重）

投影器类型：
1. 一对一投影：asset.registered → read_mm_assets
2. 聚合投影：diagnosis.finalized × N → read_mm_asset_health（每设备当前 HI）
3. 时序聚合投影：rulUpdated × N → read_mm_trend（TiDB，按月/季分区）
4. 跨租户投影：× 20 基地 → read_mm_group_benchmark（TiFlash 加速）

幂等保证：
· 每条事件带 sequence（Pulsar messageId）
· 读模型表带 processed_events(event_id) 唯一索引
· 重复投递时唯一索引冲突即跳过
```

### 6.3 延迟预算（最终一致的可接受边界）

| 读模型 | 允许延迟 | 超期后果 |
|---|---|---|
| 设备当前 HI | ≤ 5 s | 报告可能不新 |
| 告警列表 | ≤ 3 s | 用户体验下降 |
| 跨基地对标 | ≤ 60 s | 不敏感 |
| 趋势图 | ≤ 5 min | 不敏感 |
| 审计链 | 实时（同步写） | **不可延迟** |

> **审计链路同步写，不走投影。** 这是唯一例外——因为它不是"读模型"，它是"证据"。

---

## 7. GraphQL BFF 层

### 7.1 为什么需要

前端看板一次需要：设备信息 + 当前 HI + 最近告警 + RUL + 方案卡 + 趋势。
v1 的做法是 6 次 REST 调用 + 前端拼装，首屏慢且弱网下极易失败。

### 7.2 Schema 设计（节选）

```graphql
type Query {
  # 集团/基地看板
  assetHealthList(
    tenantId: ID!
    filter: AssetFilter
    page: Page          # 注意：分页由 BFF 控制，不透传数据库分页
  ): AssetHealthConnection!

  # 单设备全景（一次拉全，避免 N+1）
  assetDetail(assetId: ID!): AssetDetail

  # 跨基地对标（走 TiFlash 加速读模型）
  groupBenchmark(model: String!, metric: Metric!, from: DateTime!): [BenchmarkPoint!]!
}

type AssetDetail {
  id: ID!
  profile: AssetProfile
  health: HealthIndex
  rul: RulEstimate
  alerts(last: Int = 10): [Alert!]!
  recommendations(status: [CardStatus!]): [RecommendationCard!]!
  trend(from: DateTime!, to: DateTime!): TrendSeries
}

# 多租户与权限：声明式，网关统一执行
type AssetHealth @authz(require: "asset:read", scope: TENANT)
type RecommendationCard @authz(require: "card:read", scope: TENANT)
```

### 7.3 三条硬规则

| 规则 | 原因 |
|---|---|
| **禁止客户端拼任意深度查询** | 查询成本攻击；BFF 需深度限制（≤ 10）+ 复杂度评分 |
| **禁止 N+1** | 必须用 DataLoader 批量化，否则一次查询打爆下游 |
| **禁止把数据库字段直接暴露** | GraphQL schema 是"发布语言"，不能随数据库 schema 漂移 |

### 7.4 与 REST / MCP 的分工

| 入口 | 用途 | 消费者 |
|---|---|---|
| **GraphQL** | 前端看板、复杂聚合查询 | Web / App / 大屏 |
| **REST** | 简单资源操作、运维接口、外部系统 | 运维脚本 / EAM / 集成 |
| **MCP** | 智能体工具调用 | Agent Harness |

**三者并存，各有明确消费者，不要把某个入口强行统一成唯一入口。**

---

## 8. 事件总线选型：复用 Pulsar（已实测存在）

### 8.1 实测结论

```
10.167.2.175/176 集群 infra 命名空间已运行：
  pulsar            10.110.176.161:6650（broker）
  pulsar-broker     6650/8080
  pulsar-bookie-0/1 3181（ledgers + journal 双 bookie）
  pulsar-zookeeper  2181
```

### 8.2 为什么选它而不是 Kafka

| 维度 | Pulsar（存量） | Kafka（需新建） |
|---|---|---|
| 多租户 | **原生 tenant/namespace/topic 三级**，权限可精细授权 | 仅 topic 前缀约定，靠 ACL |
| 分层存储 | bookkeeper 自动分层 | 需自建 tiered storage |
| 弱网友好 | 有 geo-replication、可延迟确认 | 弱 |
| 运维现状 | **已在跑，零部署成本** | 新增一套 broker + ZK |
| 团队熟悉度 | 已有运维经验 | 需要学习 |

> **Pulsar 的 tenant/namespace 模型对"20 个基地"是天然匹配**：每个基地一个 tenant，报警/诊断/工单各占 namespace，供应商一个独立 tenant 且授权最小。这是选它的决定性理由，不是因为它更流行。

### 8.3 接入规范

```
Pulsar URL: pulsar://pulsar-broker.infra:6650
Admin URL: http://pulsar-broker.infra:8080

租户规划：
  tenant/pdm                              # PdM 根
    namespace/pdm.site-a                  # 每基地一个
    namespace/pdm.site-b
    namespace/pdm.group                   # 集团级（判据发布、对标投影）
    namespace/pdm.supplier                # 供应商域（权限最小）
    namespace/pdm.audit                   # 审计专用（保留策略最长）

Topic：
  persistent://pdm/site-a/diagnosis.finalized
  persistent://pdm/site-a/alert.raised
  persistent://pdm/group/criteria.published

订阅：
  每个读模型投影器独立订阅名（如 projector-asset-health）
  类型：Shared（可横向扩展）或 Failover（需顺序，如审计链）
```

---

## 9. 反模式清单（这些错我见过人犯）

| 反模式 | 症状 | 正确做法 |
|---|---|---|
| **把领域事件当日志用** | 事件里塞调试信息，订阅方被迫解析字符串 | 事件是契约，schema 版本化，字段只增不改 |
| **投影器里做业务判断** | 读模型开始"算结论"，与命令侧逻辑分叉 | 投影器只做搬运，判断留给领域服务 |
| **GraphQL 直接透传数据库** | 一个 schema 改动穿三层到 DBA | schema 是发布语言，加投影层 |
| **Saga 没有幂等** | 重试产生 3 张工单 | 幂等键 + 去重表 + 回执校验 |
| **最终一致当借口** | 数据不一致就说"最终一致" | 定义每类数据的延迟预算，超期告警 |
| **DDD 全量铺开** | 8 个上下文写 8 套完整分层，工期翻倍 | 只在诊断/决策域做厚模型，监测域薄层 |
| **聚合做太大** | Diagnosis 聚合包含整个设备历史 | 聚合边界按"一致性需求"划，不按"方便"划 |
| **事件风暴** | 一个动作发 20 个事件，订阅方雪崩 | 一个业务动作原则上一个事件 |

---

## 10. 落地顺序建议

| 阶段 | 先做什么 | 后做什么 |
|---|---|---|
| Ph1 | Pulsar 接入 + 事件信封 + 审计事件流 | 读模型投影 |
| Ph2 | 判据发布事件 + diagnosis.finalized | 跨基地对标投影 |
| Ph3 | 会诊 Saga + GraphQL BFF | 供应商域 |

**不要一上来就全量 DDD + 全量 CQRS。** 先打通"事件能可靠送达 + 幂等消费"，这是地基；地基不牢，上面都是沙。



---

## 11. 新增限界上下文与事件（2026-10-25 对齐）

### 11.1 新增四个上下文

| 上下文 | 职责 | 聚合根 | 表 |
|---|---|---|---|
| **Harness**（通用域） | 计划执行、权限门、Hooks、遥测 | 计划执行（trace） | 无独立表（span 在内存/OTLP） |
| **Memory/Skill**（支撑域） | 三层记忆、技能沉淀审批、冻结回滚 | 技能（skill_id） | agent_session / skill_registry / memory_freeze |
| **WorldModel**（核心域） | 退化拟合、反事实推演 | 推演请求（临时） | 复用 read_asset_health_history |
| **Federation/A2A**（通用域） | 站点注册、摘要上行、跨域任务 | 任务（task_id） | site_registry / read_edge_summary / a2a_task / quality_report |

### 11.2 新增领域事件（命名遵循 `com.yadea.pdm.<ctx>.<entity>.<verb>.v1`）

| 事件 | 触发 | 消费者 |
|---|---|---|
| `com.yadea.pdm.skill.approved.v1` | 技能审批通过 | 记忆加载、审计 |
| `com.yadea.pdm.skill.rolledback.v1` | 技能回滚 | 审计 |
| `com.yadea.pdm.model.driftDetected.v1` | 漂移超阈值 | 告警、模型迭代流程 |
| `com.yadea.pdm.a2a.taskCompleted.v1` | A2A 任务完成 | 请求方 Agent |
| `com.yadea.pdm.federation.summaryUploaded.v1` | 边缘摘要上行 | 集团对标看板 |

### 11.3 CQRS 一致性说明

- 世界模型/漂移检测读的是**读模型**（`read_asset_health_history`），属最终一致；
  因此推演结果必须带区间与样本量，不能当作强一致事实。
- 技能审批与判据发布是**写模型**命令，必须走版本化通道（宪法第十一/十二条）。

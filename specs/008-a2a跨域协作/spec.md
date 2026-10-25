# 008 · A2A 跨域协作（Feature Spec）

| 项 | 内容 |
|---|---|
| Spec ID | SPEC-008 |
| 关联需求 | FR-907、docs/43 §10.4、Phase E |
| 优先级 | P1 |
| 状态 | **Implemented** |
| 负责人 | 平台 + 供应链 |

## 1. Why

MCP 管"智能体连工具"，A2A 管"智能体连智能体"。本项目需要两类跨域协作：
1. **跨基地专家代诊**：集团可靠性工程师远程会诊小基地设备
2. **供应商域受控上报**（FR-907）：Tier1 供应商上报质量/设备异常 →
   关联来料批次 → 通知 SQE

A2A 于 2025-04 由 Google 发布，2025-06 捐 Linux Foundation，
2026-08 加入 AAIF 与 MCP 同治理——采用它可获得跨厂商互操作能力。

## 2. 需求

| ID | 类型 | 需求 | 验收标准 |
|---|---|---|---|
| FR-801 | Ubiquitous | 提供 Agent Card 能力发现 | `/.well-known/agent.json` 含 skills + taskTypes |
| FR-802 | Event | 支持创建任务（受控类型白名单） | 未知类型 400；已知类型返回 SUBMITTED |
| FR-803 | Event | 任务状态机可查询 | SUBMITTED→WORKING→COMPLETED/FAILED |
| FR-804 | Event | diagnose_request 可被跨域执行 | 返回诊断或降级结果 |
| FR-805 | Event | quality_report 落库并关联来料批次 | quality_report 表可查，notify=SQE |
| FR-806 | Event | alert_escalation 支持跨域升级 | 返回 escalated=True |
| FR-807 | Unwanted | 任务必须租户隔离 | site-a 读不到 supplier 任务 |
| FR-808 | Unwanted | 不得执行任意代码 | 无 eval/exec 路径，分发为白名单映射 |

## 3. 设计要点

- **受控分发**：`_dispatch` 是 if/elif 白名单映射，不执行客户端传入的代码
- **状态机守卫**：非 SUBMITTED/WORKING 状态不可再执行（防重复副作用）
- **失败落状态**：任务异常记 FAILED + error，不抛给客户端
- **审计**：任务创建/执行留痕（a2a_task 表本身即审计）

## 4. 验收映射

| 需求 | 测试 |
|---|---|
| FR-801 | `test_agent_card_shape` |
| FR-802/808 | `test_task_whitelist_rejects_unknown_type` |
| FR-803/804 | `test_diagnose_request_task_lifecycle` |
| FR-805 | `test_quality_report_supplier_domain` |
| FR-806 | `test_alert_escalation_task` |
| FR-807 | `test_task_tenant_isolation` |

## 5. 接口

```
GET  /.well-known/agent.json           Agent Card
POST /a2a/tasks/send                   {task_type, payload, requester}
GET  /a2a/tasks/{task_id}?execute=true 查询/同步执行
```

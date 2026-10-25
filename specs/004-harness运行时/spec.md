# 004 · Harness 运行时（Feature Spec）

| 项 | 内容 |
|---|---|
| Spec ID | SPEC-004 |
| 关联需求 | docs/05 §2 十组件、docs/43 Phase A |
| 优先级 | P0（智能体工程地基） |
| 状态 | **Implemented**（2026-10-25） |
| 负责人 | 平台 + 算法 |

## 1. Why

docs/05 设计了十组件，但代码只有四角色会诊与事件发射——LoopController /
Sandbox / PermissionGate / Hooks / OTel / Eval 六个组件零实现。
没有 Harness，所谓"智能体"只是"规则引擎 + 一个 MCP 端点"，
无法保证可追责、可控制、可复现。

## 2. 需求

| ID | 类型 | 需求 | 验收标准 |
|---|---|---|---|
| FR-401 | Ubiquitous | 计划执行必须是显式状态机，每态可日志化 | 状态序列含 IDLE→GATE→ACT→OBSERVE→VERIFY→DONE；非法转移抛 LoopError |
| FR-402 | Ubiquitous | 工具必须有 JSON Schema + 副作用分级 + 前置条件 | 12 工具全部具备；`sideEffect ∈ {read,write,critical}` |
| FR-403 | Unwanted | 写操作不得无确认执行 | 未批准的 write/critical 返回 PENDING_APPROVAL 而非执行 |
| FR-404 | Unwanted | 金额/停机暴露超阈值必须双人复核 | >¥50,000 → `dual=True` |
| FR-405 | Ubiquitous | Stop Hook 必须能识别"假完成" | ok=False 的步骤导致 ESCALATE |
| FR-406 | Ubiquitous | 每步必须可观测（OTel GenAI 语义约定） | span 含 `gen_ai.request.model` 等标准属性 |
| FR-407 | Ubiquitous | 必须有 golden cases 评测 | `GET /v1/harness/eval` 返回 pass_rate |
| NFR-401 | Ubiquitous | 单次计划 max_turns ≤ 8、wall_time ≤ 120s | 超限熔断抛 LoopError |

## 3. 设计要点

- **一份定义两处暴露**：`app/harness/tools.py` 是唯一工具契约源，MCP `tools/list`
  直接派生——消除双路径漂移（单点/批量诊断共用核心的同一教训）。
- **降级纪律**：读工具在库不可达时返回 `degraded=True + reason`，不崩溃。
- **OTel 无 Collector 可用**：未配 `PDM_OTLP_ENDPOINT` 时退回进程内环形缓冲，
  经 `/v1/harness/traces` 查询——内网/CI 也能验证埋点。

## 4. 验收映射

| 需求 | 实现 | 测试 |
|---|---|---|
| FR-401 | `app/harness/loop.py` | `test_loop_*` |
| FR-402 | `app/harness/tools.py` | `test_tool_registry_shape` |
| FR-403/404 | `app/harness/gate.py` | `test_gate_*` |
| FR-405 | `app/harness/hooks.py` | `test_stop_hook_*` |
| FR-406 | `app/harness/otel.py` | `test_otel_span_layers_and_buffer` |
| FR-407 | `app/harness/eval.py` | `test_eval_*` |
| NFR-401 | `loop.py` 熔断 | `test_loop_rejects_empty_and_too_long` |

## 5. 不做（边界）

- 不引入 LLM 规划器：当前计划由调用方给出（规则型），LLM 接入后 LoopController 不变。
- 不做跨进程分布式执行：单实例内状态机，跨基地协作走 A2A（spec 008）。

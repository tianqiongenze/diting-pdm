# SPEC-001 技术方案（Plan）

| 项 | 内容 |
|---|---|
| 关联 | `spec.md` |
| 状态 | Draft |
| 预计工作量 | 8 人日（平台 3 / 算法 3 / 前端 2） |

---

## 1. 架构落点

```
用户输入
   │
   ▼
[Web 对话组件] ── REST ──► [agent-runtime / Harness]
                              │
                    ┌─────────┼─────────┬──────────┐
                    ▼         ▼         ▼          ▼
              list_assets  get_profile  query_series  compute_features
                    │         │         │          │
                    └─────────┴────┬────┴──────────┘
                                   ▼
                            assess_health
                                   │
                                   ▼
                             detect_faults
                                   │
                                   ▼
                        [证据组装 + 报告渲染]
                                   │
                                   ▼
                              HTML 报告
```

**只读工具链，零写操作。** 权限模型里全部 ALLOW。

---

## 2. 关键技术决策

| 决策 | 选择 | 理由 | 备选 |
|---|---|---|---|
| 意图解析 | LLM 结构化输出（JSON Schema 约束） | 灵活、可扩展 | 正则/规则（太脆） |
| 设备消歧 | 先精确匹配位号 → 再模糊匹配 → 最后候选列表 | 避免"3号电机"匹配错 | 纯向量检索（不准） |
| 工具编排 | 固定 DAG（非自由循环） | 可预测、可测试 | 自由 ReAct（不稳定） |
| 证据组装 | 工具返回的 `evidence` 字段汇总去重 | 强制契约 | 事后提取（易漏） |
| 报告 | 服务端渲染 HTML（Jinja + ECharts CDN 内联） | 可离线、可归档 | 前端渲染（不可归档） |
| 缓存 | 设备档案 10 min；健康结果 5 min | 减少重复计算 | 不缓存（成本高） |

### 2.1 为什么用固定 DAG 而不是自由 ReAct

首期只有 6 个只读工具，路径高度确定：

```
解析 → 取档案 → 取数据 → 算特征 → 评估健康 → 检测故障 → 组装
```

固定 DAG 的好处：
- 每步可单独测试（TDD 友好）
- 时延可预测（≤ 8 s）
- 出错点明确
- token 消耗稳定（≈ 8k）

**代价：** 灵活性低。当工具增加到 10+ 或需要条件分支时，再引入受控的 ReAct（带 max_turns=6）。这个演进点写在 ADR-009。

---

## 3. 接口契约

### 3.1 主入口

```
POST /api/v1/qa
{
  "question": "3号电机现在怎么样",
  "session_id": "s-123",
  "context": {"asset_hint": null, "window": null}
}
→ 200
{
  "answer": "...",
  "level": "orange",
  "hi": 47,
  "evidence": [{"id":"EV-001","type":"series","desc":"...","value":"..."}],
  "actions": ["..."],
  "report_url": "/reports/...",
  "meta": {"elapsed_ms": 3200, "degraded": false, "trace_id": "..."}
}
```

### 3.2 澄清响应

```
→ 200
{
  "need_clarify": true,
  "question": "请问是哪台设备？",
  "candidates": [{"id":"KA-101","name":"3号压缩机"}, ...],
  "round": 1
}
```

**`round` 字段必须返回**，前端据此在第 2 轮后强制给出候选列表（对应 FR-001-02）。

---

## 4. 数据模型（本 spec 用到）

| 表 | 用途 |
|---|---|
| `asset` | 设备台账 |
| `component` | 部件 |
| `measurement_point` | 测点 |
| `series_*`（TDengine 超级表） | 时序 |
| `fault_mode` | 故障模式 |
| `diagnosis` | 诊断记录（本 spec 只写这一张） |
| `audit_log` | 审计 |

---

## 5. 实现步骤（Task 分解见 tasks.md）

| 步 | 内容 | 产出 |
|---|---|---|
| 1 | 定义 JSON Schema（意图/设备/时间窗） | `schemas/intent.json` |
| 2 | 实现设备消歧（3 级匹配） | `resolver.py` + 单测 |
| 3 | 实现 DAG 编排器 | `pipeline.py` + 单测 |
| 4 | 实现证据组装器 | `evidence.py` + 单测 |
| 5 | 接入 6 个只读工具 | `tools/*.py` |
| 6 | 报告渲染 | `report/template.html.j2` |
| 7 | 对话 UI | React 组件 |
| 8 | BDD 场景 | `features/qa.feature` |
| 9 | 注入防护 | `guard/` |
| 10 | 端到端联调 + 性能 | 测试报告 |

---

## 6. 风险

| 风险 | 影响 | 对策 |
|---|---|---|
| 设备名口语化（"三号机"、"3#"） | 消歧失败 | 别名表 + 拼音/数字归一化 |
| 时序数据不足 30 天 | 基线学不出来 | 用"绝对阈值模式"降级，标注"基线未建立" |
| LLM 输出 JSON 不稳定 | 解析失败 | JSON Schema 约束 + 重试 1 次 + 规则兜底 |
| 报告渲染慢（大图） | 超时 | 图片服务端预渲染为 PNG，HTML 内联 base64 |

---

## 7. 完成定义（DoD）

- [ ] 8 条 AC 全部通过
- [ ] 单测覆盖率 ≥ 85%
- [ ] BDD 场景 100% 通过
- [ ] 100 条测试集意图识别 ≥ 95%
- [ ] P95 ≤ 8 s
- [ ] 单次成本 ≤ ¥0.3
- [ ] 注入红队 10 例全过
- [ ] 报告可离线打开（无外网依赖）

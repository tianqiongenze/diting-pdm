# SPEC-001 任务分解（Tasks）

> 规则：**每个任务必须先写测试再写实现**（TDD）。`[P]` 表示可并行。

## 阶段 0 · 准备（0.5 人日）

- [ ] T0.1 确认 3 台试点设备位号与别名表
- [ ] T0.2 收集 100 条真实提问（业务方）
- [ ] T0.3 确认默认时间窗 = 7 天（Q1 拍板）
- [ ] T0.4 搭建 test 环境（TDengine + PG + stub LLM）

## 阶段 1 · 意图解析（1.5 人日）

- [ ] T1.1 写 `test_intent_schema_*`（5 例：完整/缺设备/缺时间/歧义/越界）
- [ ] T1.2 实现 `schemas/intent.json`
- [ ] T1.3 [P] 写 `test_resolver_exact_match`、`test_resolver_fuzzy`、`test_resolver_candidates`
- [ ] T1.4 实现设备消歧三级匹配（位号 → 别名/拼音 → 模糊）
- [ ] T1.5 实现时间窗解析（"最近一周"/"上周"/"昨天"）
- [ ] T1.6 实现澄清轮次控制（max_round=2）
- [ ] T1.7 跑 100 条测试集，记录准确率

**验收：** 意图识别 ≥ 95%，澄清 ≤ 2 轮

## 阶段 2 · 工具层（2 人日）

- [ ] T2.1 写 `test_query_series_downsample`、`test_query_series_window_limit`
- [ ] T2.2 实现 `query_series`（降采样 ≤ 2000 点，> 90 天拒绝）
- [ ] T2.3 [P] 写 `test_compute_features_rms/kurtosis/envelope`
- [ ] T2.4 实现 `compute_features`（时域 + FFT + 包络）
- [ ] T2.5 [P] 写 `test_assess_health_*`（见 docs/08 示例）
- [ ] T2.6 实现 `assess_health`（HI + 分解 + 降级）
- [ ] T2.7 [P] 写 `test_detect_faults_bpfo_harmonics`
- [ ] T2.8 实现 `detect_faults`（故障模式匹配 + 排除条件）
- [ ] T2.9 [P] 实现 `list_assets`、`get_asset_profile`
- [ ] T2.10 为每个工具补 `evidence` 返回字段

**验收：** 单测覆盖 ≥ 85%，工具契约测试通过

## 阶段 3 · 编排与证据（1.5 人日）

- [ ] T3.1 写 `test_pipeline_happy_path`、`test_pipeline_low_quality`
- [ ] T3.2 实现 DAG 编排器（6 步，含超时/短路）
- [ ] T3.3 [P] 写 `test_evidence_dedup`、`test_evidence_missing_blocks_answer`
- [ ] T3.4 实现证据组装器（去重、编号 EV-xxx）
- [ ] T3.5 实现降级分支（质量分 < 60 → 拒答）
- [ ] T3.6 实现停机状态分支
- [ ] T3.7 接入 OTel 埋点（每步一个 span）

**验收：** 质量分 45 时 100% 拒答且说明缺失项

## 阶段 4 · 报告与 UI（2 人日）

- [ ] T4.1 [P] 实现 HTML 报告模板（结论/等级/证据/频谱/趋势/建议/免责）
- [ ] T4.2 频谱图服务端渲染为 PNG 内联（离线可看）
- [ ] T4.3 [P] 实现对话组件（含澄清交互、候选选择）
- [ ] T4.4 实现"为什么"展开推理链
- [ ] T4.5 实现 AI 标识与免责声明（不可关闭）

**验收：** 报告断网可打开；免责声明 100% 出现

## 阶段 5 · 护栏（0.5 人日）

- [ ] T5.1 写 `test_injection_*`（10 例）
- [ ] T5.2 实现数据隔离区标记与指令检测
- [ ] T5.3 实现越界问题拒答 + 引导话术
- [ ] T5.4 实现行级权限过滤（按车间）

**验收：** 红队 10 例 100% 拦截；越权 0 例

## 阶段 6 · 联调与验收（0.5 人日）

- [ ] T6.1 BDD 8 条场景全绿
- [ ] T6.2 性能测试（P95 ≤ 8 s，50 并发）
- [ ] T6.3 成本核算（≤ ¥0.3/次）
- [ ] T6.4 演示给设备科，收集反馈
- [ ] T6.5 更新文档与 RTM

---

## 依赖关系

```
T0 ──► T1 ──► T3 ──► T6
        │      ▲
        └► T2 ─┘
               │
        T4 ────┘（可与 T3 并行）
        T5 ────┘
```

**关键路径：T0 → T1 → T2 → T3 → T6**

## 风险缓冲

| 任务 | 缓冲 | 原因 |
|---|---|---|
| T1.4 设备消歧 | +0.5 人日 | 口语化别名难以穷举 |
| T2.6 HI 实现 | +0.5 人日 | 权重需专家确认 |
| T4.2 频谱渲染 | +0.5 人日 | 服务端渲染调试耗时 |

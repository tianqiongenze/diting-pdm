# 005 · 世界模型反事实推演（Feature Spec）

| 项 | 内容 |
|---|---|
| Spec ID | SPEC-005 |
| 关联需求 | docs/43 §10.3、Phase C |
| 优先级 | P0（决策会诊的理论根基） |
| 状态 | **Implemented**（L1 统计世界模型） |
| 负责人 | 算法 |

## 1. Why

PdM 的瓶颈不是"预测更准"，而是**决策推演**："如果推迟 2 周保养，HI 会掉到多少？
这期间非计划停机概率多大？"此前只有线性外推 RUL，无法回答反事实问题，
方案卡的经济性数字（`avoided_loss = 成本 × 8h × 3 次`）是拍脑袋系数。

## 2. 需求

| ID | 类型 | 需求 | 验收标准 |
|---|---|---|---|
| FR-501 | Ubiquitous | 从健康历史学习退化动态 | 加权线性回归，近期点权重更高（指数衰减） |
| FR-502 | Event | 给定 horizon 输出 HI 轨迹 + 区间 | 每点含 mean/lo/hi 且 lo≤mean≤hi |
| FR-503 | Event | 支持反事实：指定第 N 天保养（HI 重置） | 干预点出现重置跳升，之后按原斜率劣化 |
| FR-504 | Ubiquitous | 输出预计跌破临界线天数 | `failure_day_estimate`（无劣化时为 null） |
| FR-505 | Unwanted | **样本不足必须返回 null，不得编数字** | <3 点或无斜率 → model=null + note |
| FR-506 | Ubiquitous | 方案卡 avoided_loss 必须引用推演依据 | economics.avoided_loss_basis 标注来源 |

## 3. 设计要点（工业红线）

1. **无区间不输出**：`trajectory` 每点必带 lo/hi（残差标准差 × Z95）
2. **不编数字**：样本 <3、时间无跨度、斜率 ≥ -0.01/天 → 返回 null + 说明
3. **可解释**：模型是加权线性回归，参数（slope/resid_std/n_points）全部暴露
4. **L2 预留**：接口 `fit/predict/counterfactual` 签名稳定，神经世界模型可替换实现

## 4. 验收映射

| 需求 | 实现 | 测试 |
|---|---|---|
| FR-501 | `app/world/model.py::fit` | `test_fit_recovers_slope`、`test_fit_recent_weighted` |
| FR-502 | `predict()` / `counterfactual()` | `test_predict_has_interval`、`test_counterfactual_degraded_trajectory` |
| FR-503 | `counterfactual(maintain_at_day=...)` | `test_counterfactual_with_maintenance_intervention` |
| FR-504 | `days_until()` | `test_days_until_critical` |
| FR-505 | null 纪律 | `test_counterfactual_insufficient_data_returns_null` |
| FR-506 | `decision._avoided_loss_with_world_model` | 方案卡 economics 断言 |

## 5. 接口

```
POST /v1/world/counterfactual
  {asset_id, horizon_days, maintain_at_day?, maintain_reset_hi?, critical_hi?}
  → {model|null, trajectory[{day,mean,lo,hi}], failure_day_estimate|null, note?}

GET /v1/world/model/{asset_id}
  → {model: {slope_per_day, resid_std, n_points, window_days} | null}
```

## 6. 不做

- 不做神经世界模型（L2）：需 LLM 预算与训练数据，接口已预留。
- 不做多设备耦合推演：当前按单设备建模。

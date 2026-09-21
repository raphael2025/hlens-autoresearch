# Factor Library

| 字段 | 值 |
|---|---|
| 类型 | `factor` |
| 状态 | 空（Architecture Bootstrap） |
| 首次填充 | Phase 0.5 / 5 |

横截面或时间序列因子（通常用于排序或打分的 Feature 组合）。

## 条目字段（草案，以 `core/contracts` 为准）

| 字段 | 说明 |
|---|---|
| `name@version` | 唯一标识 |
| `definition` | 数学定义 |
| `inputs` | Feature 引用 |
| `universe` | 适用标的池规则 |
| `rebalance` | 调仓频率 |
| `source` | 出处 |
| `known_decay` | 已知衰减或拥挤证据 |
| `lifecycle_state` | 状态 |
| `validation_reports` | 报告引用 |

## 通用规则

- 条目登记 ≠ 条目有效。所有条目默认状态为 `IDEA` 或 `UNVERIFIED`，只有 Lifecycle 状态能说明其是否经过验证。
- 每个条目必须有出处（`source`）；来源为本系统实验时引用 ExperimentRun ID。
- 已发布版本不可修改；修改 = 新版本。
- 被拒绝 / 失败的条目**不删除**，其状态更新并在 [failure-registry.md](failure-registry.md) 中登记。
- 本 Markdown 是人类可读索引；Phase 0 之后，权威数据存放在 Control Plane Registry 中，本文件由其生成或与其同步。

## 条目索引

| name@version | 摘要 | 出处 | 状态 |
|---|---|---|---|
| — | 暂无条目 | — | — |

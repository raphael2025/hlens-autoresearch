# Factor Library

| 字段 | 值 |
|---|---|
| 类型 | `factor` |
| 状态 | 1 条未验证的文档草稿（未登记为 `KnowledgeItem`；Phase 0.5 未验收） |
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

以下仅为研究条目草稿，不是已发布的 Registry / `KnowledgeItem` 数据，也不表示该主张已在本项目复现。来源、许可与证据等级按 Phase 0.5 的记录形状给出；主张保持未验证。

| name@version（草稿） | 定义与可检验主张 | 条件 / 检验方法 | 出处与许可 | 证据等级 / 状态 |
|---|---|---|---|---|
| `factor_equity_cross_sectional_momentum@0.1.0-draft` | 在每个形成时点按过去一段时间的资产收益排序，并构造高收益组减低收益组的多空组合。可检验主张：在预先固定的样本、形成期、持有期与交易成本下，该组合的样本外平均净收益是否大于 0。 | 原始文献研究美国股票，报告过往赢家与输家在后续持有期的收益差异；本项目尚无对应数据复现。检验时须先固定 point-in-time universe、收益形成窗口、持有窗口、成本和样本切分；不能将文献结论外推为当前 BTC/ETH 现货结论。 | Narasimhan Jegadeesh & Sheridan Titman (1993), “Returns to Buying Winners and Selling Losers: Implications for Stock Market Efficiency,” *The Journal of Finance* 48(1), 65–91, [DOI / Wiley 记录](https://doi.org/10.1111/j.1540-6261.1993.tb04702.x)。许可：Wiley 页面未标示开放许可；此处仅列书目与自写摘要，不复制论文正文，原文使用受出版方条款约束。 | E2（文献中的历史实证；不是本项目证据） / `unverified` |

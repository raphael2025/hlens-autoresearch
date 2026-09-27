# Feature Library

| 字段 | 值 |
|---|---|
| 类型 | `feature` |
| 状态 | 1 条未验证的文档草稿（未登记为 `KnowledgeItem`；Phase 0.5 未验收） |
| 首次填充 | Phase 1 |

从 Representation 计算的时间序列特征（FeatureSpec）。

## 条目字段（草案，以 `core/contracts` 为准）

| 字段 | 说明 |
|---|---|
| `name@version` | 唯一标识 |
| `definition` | 计算定义 |
| `inputs` | Canonical / Representation 引用 |
| `params` | 参数与默认值 |
| `available_lag` | 可用延迟（防未来函数） |
| `output_schema` | Arrow schema |
| `provider` | 实现插件 name@version |
| `deterministic` | 是否确定性 |
| `source` | 出处 |

## 通用规则

- 条目登记 ≠ 条目有效。所有条目默认状态为 `IDEA` 或 `UNVERIFIED`，只有 Lifecycle 状态能说明其是否经过验证。
- 每个条目必须有出处（`source`）；来源为本系统实验时引用 ExperimentRun ID。
- 已发布版本不可修改；修改 = 新版本。
- 被拒绝 / 失败的条目**不删除**，其状态更新并在 [failure-registry.md](failure-registry.md) 中登记。
- 本 Markdown 是人类可读索引；Phase 0 之后，权威数据存放在 Control Plane Registry 中，本文件由其生成或与其同步。

## 条目索引

以下仅为研究条目草稿，不是已发布的 FeatureSpec、Registry 或 `KnowledgeItem` 数据，也不表示本项目已实现或验证该特征。来源、许可与证据等级按 Phase 0.5 的记录形状给出；主张保持未验证。

| name@version（草稿） | 定义与可检验主张 | 条件 / 检验方法 | 出处与许可 | 证据等级 / 状态 |
|---|---|---|---|---|
| `feature_amihud_illiquidity_daily_mean@0.1.0-draft` | 对资产 `i` 在窗口内的有效日 bar 计算 `ILLIQ_i = mean_d(|r_i,d| / dollar_volume_i,d)`；价格收益与美元成交额须使用一致且明确的日界线。可检验实现主张：输出严格等于仅由截止时点可用的日 bar 计算出的窗口均值；零成交额或缺失输入须显式处理，不能静默产生有限值。 | 原始定义和经验分析来自美国股票研究；本项目尚未验证该度量在加密现货上的数据口径或预测含义。实现测试应覆盖手算向量、零成交额 / 缺失日、窗口边界，以及 `available_time ≤ feature_time` 的因果约束。 | Yakov Amihud (2002), “Illiquidity and stock returns: cross-section and time-series effects,” *Journal of Financial Markets* 5(1), 31–56, [DOI / Elsevier 记录](https://doi.org/10.1016/S1386-4181(01)00024-6)。许可：出版方页面未标示开放许可；此处仅列书目与自写公式摘要，不复制论文正文，原文使用受出版方条款约束。 | E2（文献中的历史实证；不是本项目证据） / `unverified` |

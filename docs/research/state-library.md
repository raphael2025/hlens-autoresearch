# State Library

| 字段 | 值 |
|---|---|
| 类型 | `state` |
| 状态 | Phase 2 框架：首批条目已登记（FRAMEWORK_IMPLEMENTED / NOT_VALIDATED） |
| 首次填充 | Phase 2（[ADR-0035](../adr/0035-state-provider-contract.md)） |

市场状态定义（StateSpec）。契约见 `core/contracts/state.py`，执行器 `infrastructure/state/`，实现 `plugins/states/`，
诊断 `research/states/diagnostics.py`。

## 条目字段（以 `core/contracts` 为准）

| 字段 | 说明 |
|---|---|
| `name@version` | 唯一标识 |
| `state_space` | 离散状态集合（`StateSpec.state_space`） |
| `inputs` | Feature 引用（`StateSpec.features`；只能是 `kind=feature`，Outcome 永不作为输入） |
| `method` | 规则 / 阈值 / 聚类 / HMM / ...；参数以 `<method>:<canonical JSON>` 编码，受 spec hash 绑定 |
| `training_window` | 若为训练型：固定尾随窗口（执行器只给出 `(t - window, t]` 的输入）与固定 `seed` |
| `stability` | 平均持续时间、转移矩阵、闪烁摘要（由 `research/states/diagnostics.py` 生成，来源为实验时引用 ExperimentRun ID） |
| `provider` | 实现插件 |
| `source` | 出处 |

## 通用规则

- 条目登记 ≠ 条目有效。所有条目默认状态为 `IDEA` 或 `UNVERIFIED`，只有 Lifecycle 状态能说明其是否经过验证。
- 每个条目必须有出处（`source`）；来源为本系统实验时引用 ExperimentRun ID。
- 已发布版本不可修改；修改 = 新版本。
- 被拒绝 / 失败的条目**不删除**，其状态更新并在 [failure-registry.md](failure-registry.md) 中登记。
- 模型参数（分位切点、最少历史、阈值、窗口）是**规格参数**，由每个规格声明，不是验证阈值；首批 Provider 不设默认值。
- 本 Markdown 是人类可读索引；Phase 0 之后，权威数据存放在 Control Plane Registry 中，本文件由其生成或与其同步。

## 条目索引

条目是**模型族**：具体版本（参数、窗口、种子）由研究方在规格中声明；下表不登记任何参数取值。

| name@version | 摘要 | inputs | method | training_window / seed | provider | 出处 | 状态 |
|---|---|---|---|---|---|---|---|
| `volatility_regime@*` | 波动率体制：最新已实现波动率在固定尾随窗口内的经验分位分桶（`low_vol` / `mid_vol` / `high_vol`） | `bar_realized_vol_<n>` | `trailing_quantile_buckets`（`cuts`、`min_history`） | 必须；固定 seed | `plugins.states.VolatilityRegimeProvider@1.0.0` | ADR-0035（框架） | UNVERIFIED |
| `liquidity_regime@*` | 流动性体制：最新成交量合计在固定尾随窗口内的经验分位分桶（`thin` / `normal` / `deep`） | `bar_volume_sum_<n>` | `trailing_quantile_buckets`（`cuts`、`min_history`） | 必须；固定 seed | `plugins.states.LiquidityRegimeProvider@1.0.0` | ADR-0035（框架） | UNVERIFIED |
| `trend_range@*` | 趋势 / 震荡：最近 `window` 个对数收益的效率比 `\|Σr\| / Σ\|r\|` 与阈值比较（`trend_down` / `range` / `trend_up`） | `bar_log_return` | `efficiency_ratio`（`window`、`threshold`） | 规则型：无 | `plugins.states.TrendRangeProvider@1.0.0` | ADR-0035（框架） | UNVERIFIED |
| `funding_regime` | 资金费率体制 | 资金费率（**未采集**，ADR-0022 范围） | — | — | 无（declared-unavailable，`plugins.states.DECLARED_UNAVAILABLE`） | roadmap Phase 2 | IDEA（输入不可用） |

## 已知失败模式（roadmap Phase 2）

- 状态事后看起来完美但实时不可识别 → 执行器结构性截断 + contract suite 因果扰动检查。
- 状态过多导致样本稀薄 → 诊断报告的分布与 `not_computable` 计数。
- 状态标签闪烁 → 诊断报告的短 run 占比与切换率（`min_run` 由调用方给出）。

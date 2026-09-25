# Strategy Library

| 字段 | 值 |
|---|---|
| 类型 | `strategy` |
| 状态 | 框架已实现（Phase 5，ADR-0038）；条目均为 NOT_VALIDATED |
| 首次填充 | Phase 5 |

从知识库与研究中登记的交易策略（StrategySpec）。

## 条目字段（草案，以 `core/contracts` 为准）

| 字段 | 说明 |
|---|---|
| `name@version` | 唯一标识 |
| `family` | trend / mean-reversion / carry / microstructure / event-driven / ... |
| `source` | 出处（KnowledgeItem ID / 论文 / 实验） |
| `signals` | 依赖的 Feature / State / Event 引用 |
| `param_space` | 参数及搜索空间（用于 trial count） |
| `applicable_scope` | 标的、频率、状态范围 |
| `risk_policy` | 默认 RiskPolicy 引用 |
| `lifecycle_state` | IDEA ... RETIRED / REJECTED |
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
| `tsmom_bars@1.0.0` | trend：各标的最近 `lookback` 个可见 `bar_log_return` 之和的符号 → 多 / 空（`long_only` 时空头改为空仓），每标的等分 `1 / N` 总敞口；历史不足或窗口内有缺失 → 空仓。信号 `feature:bar_log_return@1.0.0`；参数空间 `lookback ∈ {60, 240, 1440}`（bar 数）、`long_only ∈ {false, true}`；无风控 | `knowledge:strategy_time_series_momentum@1.0.0`、`knowledge:strategy_crypto_time_series_momentum@1.0.0` | NOT_VALIDATED（等待 Phase 4 验证接入） |
| `tsmom_bars_vol_scaled@1.0.0` | 同一信号与参数空间，`risk_policy = risk:vol_target_bars@1.0.0`（见 [risk-library.md](risk-library.md)） | 同上 + 风控条目出处 | NOT_VALIDATED |

实现：`research/strategies/time_series_momentum.py`（研究代码，未晋升；`strategies/` 为空）。参数空间外的请求被 Provider 拒绝；
回测为 `plugins/backtest/BarBacktester`（下一根开盘成交，费率 + 滑点）。验证报告在 Phase 4 流水线接入后生成；被拒绝或运行失败的
试验经 `research/strategies/failure_registry.py` 追加到 Failure Registry。

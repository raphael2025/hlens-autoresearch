# research/strategies/

Phase 5 研究策略库（[ADR-0038](../../docs/adr/0038-strategy-risk-backtest-providers.md)）。**研究代码，永不直接成为生产代码**
（CLAUDE.md H5、[ADR-0005](../../docs/adr/0005-research-production-boundary.md)）；晋升只经 Promotion 流程进入 `strategies/`、`risk/`。

状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。

| 模块 | 内容 |
|---|---|
| `time_series_momentum.py` | `TimeSeriesMomentumProvider`（StrategyProvider）；`tsmom_bars@1.0.0`、`tsmom_bars_vol_scaled@1.0.0`；lineage 为知识库条目 |
| `volatility_target.py` | `VolatilityTargetRiskProvider`（RiskProvider）；`vol_target_bars@1.0.0` 与其声明的参数空间 |
| `signals.py` | 从 `PriceBar` 派生 `bar_log_return` / `bar_realized_vol_<n>` 信号观察（探索用；生产路径走 FeatureProvider runner） |
| `pipeline.py` | 策略 → 风控 → 回测 → 验证接缝 → Failure Registry |
| `validation.py` | `BacktestValidator` 窄接口；`TODO(phase4-wiring)`：接入 `research/validation`（ADR-0037） |
| `failure_registry.py` | `FailureRecord` 的追加式 JSON Lines 写路径（无删除 / 改写） |
| `library.py` | 策略条目、出处（KnowledgeItem 引用）解析、候选装配 |

条目索引见 [strategy-library.md](../../docs/research/strategy-library.md) 与 [risk-library.md](../../docs/research/risk-library.md)。

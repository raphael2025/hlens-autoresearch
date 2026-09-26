# research/strategies/

Phase 5 研究策略库（[ADR-0038](../../docs/adr/0038-strategy-risk-backtest-providers.md)）。**研究代码，永不直接成为生产代码**
（CLAUDE.md H5、[ADR-0005](../../docs/adr/0005-research-production-boundary.md)）；晋升只经 Promotion 流程进入 `strategies/`、`risk/`。

状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。验证接线见 [ADR-0041](../../docs/adr/0041-validation-robustness.md)；
`validator=None` 时评估结果为 `NOT_VALIDATED`，永远不是通过。

| 模块 | 内容 |
|---|---|
| `time_series_momentum.py` | `TimeSeriesMomentumProvider`（StrategyProvider）；`tsmom_bars@1.0.0`、`tsmom_bars_vol_scaled@1.0.0`；lineage 为知识库条目 |
| `volatility_target.py` | `VolatilityTargetRiskProvider`（RiskProvider）；`vol_target_bars@1.0.0` 与其声明的参数空间 |
| `signals.py` | 从 `PriceBar` 派生 `bar_log_return` / `bar_realized_vol_<n>` 信号观察（探索用；生产路径走 FeatureProvider runner） |
| `pipeline.py` | 策略 → 风控 → 回测 → 验证 → Failure Registry；`CandidateTrialRunner`（任一声明参数点重跑，可延迟 k bar、平移决策网格、限定标的） |
| `validation.py` | `BacktestValidator` 窄接口与其实现 `PipelineBacktestValidator`：由 `research/validation` 跑 G0 – G3（ADR-0037）与 G4（ADR-0041），附 JSON 报告视图；不可复现记为 FAILED |
| `failure_registry.py` | `FailureRecord` 的追加式 JSON Lines 写路径（无删除 / 改写） |
| `library.py` | 策略条目、出处（KnowledgeItem 引用）解析、候选装配 |

条目索引见 [strategy-library.md](../../docs/research/strategy-library.md) 与 [risk-library.md](../../docs/research/risk-library.md)。

**回测执行模型**：流水线与 `CandidateTrialRunner` 接受任一 `BacktestProvider`。默认 `BarBacktester()` 是 v1（下一根开盘、只有费率 +
滑点）；要在试验中计入容量与融资，显式传入 `BarBacktester(execution=ExecutionModel(...))`（`plugins/backtest/execution.py`）。其平方根
冲击与 G4 容量检查（`research/validation/robustness.py` 的 `capacity_check`）同一公式，`bar_volume` 与 `ValidatorSetup.bar_volume` 同形；
参与上限只在执行 bar 截断（剩余量报告、不结转，D-PARTIAL），策略每个决策时刻重发目标即逐 bar 收敛。变体的参数由 `provider_hash` 绑定，
因此同一候选在两种执行模型下的回测结果可区分（ADR-0038 实施说明 execution realism）。

**验证器接线**（ADR-0041 实施说明 execution model in validation, 2026-09-26）：用变体回测的候选，验证时应把同一执行模型显式声明给
`validation.ValidatorSetup.backtester`（任一 `BacktestProvider`）或 `.execution`（只给 `ExecutionModel`，验证器自行包一层
`BarBacktester`），二者互斥、都可选、默认 `None`。给出时，`PipelineBacktestValidator` 新增适配器门 `G0.execution_model`：直接核对
`validate` 收到的 `backtest.provider_hash` 就是声明模型的 descriptor 哈希，不符在 G0 判 FAIL（`REJECTED` / `CONTRACT_VIOLATION`），
比先重跑再靠 `G0.reproducibility` 间接发现更早、更具体；`robustness_input` 同样先做这个核对再重跑参数网格。声明的模型带
`impact_coefficient` 时，G4 容量检查读它而不是只读显式的 `RobustnessParams.impact_coefficient`——两者都给出且不同时判
`INCONCLUSIVE`（`impact_coefficient_mismatch`），不静默择一。两个字段都不给时（每个既有调用方）不加任何门，报告逐字节不变。

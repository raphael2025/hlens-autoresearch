# research/strategies/

Phase 5 研究策略库（[ADR-0038](../../docs/adr/0038-strategy-risk-backtest-providers.md)）。**研究代码，永不直接成为生产代码**
（CLAUDE.md H5、[ADR-0005](../../docs/adr/0005-research-production-boundary.md)）；晋升只经 Promotion 流程进入 `strategies/`、`risk/`。

状态：FRAMEWORK_IMPLEMENTED / NOT_VALIDATED。验证接线见 [ADR-0041](../../docs/adr/0041-validation-robustness.md)；
`validator=None` 时评估结果为 `NOT_VALIDATED`，永远不是通过。

| 模块 | 内容 |
|---|---|
| `time_series_momentum.py` | `TimeSeriesMomentumProvider`（StrategyProvider）；`tsmom_bars@1.0.0`、`tsmom_bars_vol_scaled@1.0.0`；lineage 为知识库条目 |
| `cross_sectional_momentum.py` | `CrossSectionalMomentumProvider`（StrategyProvider）；`xsmom_bars@1.0.0`；lineage 为 `factor_crypto_market_size_momentum@1.0.0` |
| `cross_section.py` | 横截面策略的**声明**（ADR-0059）：静态集合 `CROSS_SECTIONAL_STRATEGIES`（按 `StrategySpec.name`），`is_cross_sectional(spec)` 只读规格名，从不由结果推断 |
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

**多标的验证**（Phase 8 实施说明，2026-09-26；状态 **CODE_COMPLETE / DEBUG_PENDING**；未改 core / 契约 / Schema / Profile 数值 / 门的放行条件）：
`ValidatorSetup.instruments`（默认 `None`）给出回测交易的确切标的集合（至少两个，须包含 `instrument`）。`None` 时仍是单标的路径，报告逐字节不变
（改动前固定的报告哈希见 `tests/research/strategies/test_multi_instrument_validation.py`）。给出时：`G0.instrument_scope` 取代
`G0.single_instrument_adapter`（重跑交易的标的与声明集合不一致 → `INCONCLUSIVE`，不计算标签）；数据集路径的 `G0.manifest_binding` 对每个标的
单独核对（`instrument_bars[<名>]` / `bars_in_manifest[<名>]` / `price_cutoff[<名>]`），任一标的不绑定仍在 G0 判 FAIL；`OutcomeRequest` 仍是单标的，
**每个标的一份请求**，标签的 `event_key`（`<标的>|<时间>`）自带标的；合并与判定见 `research/validation/README.md`「多标的验证」。每个试验仍只计一次：
逐标的证据复用同一次重跑、不新增 `TrialRunner` 调用，G3 调整用不变的 `family_trial_count`。G4 跨资产检查对每个声明标的使用它自己的单独重跑，
多标的基准回测永不被当作某一个标的的收益。

**截面动量**（Phase 5，2026-09-26；状态 **CODE_COMPLETE / DEBUG_PENDING / NOT_VALIDATED**；未改 core / 契约 / Schema）：`xsmom_bars@1.0.0`
在请求的标的集合上按 `lookback` 根 `bar_log_return` 之和（尾随对数收益）降序排名（同值按标的名升序），做多前 `k`、做空后 `k`
（`long_only` 时只做多），`k = min(top_n, 可计算标的数 // 2)`；绝对权重合计为声明的 `gross_exposure`；不足两个可计算标的或无截面离散时全部空仓。
参数空间 `lookback ∈ {60, 240, 1440}`、`long_only ∈ {False, True}`、`top_n ∈ {1, 2}`、`gross_exposure ∈ {1}`（int：规格不存 Decimal、请求拒收数值文本），
试验数 12；这些是策略参数，不是验证阈值。库条目 `hypothesis_family_id = "xsmom_bars"`（独立于 `tsmom_bars`）。它至少需要两个标的，
端到端测试走多标的验证路径（`ValidatorSetup.instruments`）。注意：G4 跨资产检查对每个标的单独重跑，截面策略在单标的上按定义空仓，
因此其跨资产证据对此类策略结构上无信息量——由 ADR-0059 解决（见下）。

**横截面策略的 G4 跨资产检查**（[ADR-0059](../../docs/adr/0059-cross-asset-check-for-cross-sectional-strategies.md)，Accepted 2026-09-26；
状态 **CODE_COMPLETE / DEBUG_PENDING**；未改 core / 契约 / Schema）：`validation.py` 构建 G4 输入时为每个声明标的的单标的重跑记录是否有敞口
（`_exposed`：有非零执行目标或成交；只看持仓，不看收益），全部无敞口时跨资产比例门判 `INCONCLUSIVE`（C）。`cross_section.is_cross_sectional(spec)`
为真的策略（目前只有 `xsmom_bars`）在逐标的重跑之后，再对 `subuniverse_partition(declared_instruments)` 的每个子宇宙各重跑一次，按同一
`cross_asset.min_positive_fraction` 判定子宇宙正收益比例（A）；少于 4 个声明标的（不足两个子宇宙）判 `INCONCLUSIVE`。这些都是所选试验的稳健性重跑，
试验数不变。其它策略的 `TrialRunner` 调用与报告逐字节不变（改动前固定的哈希见 `tests/research/strategies/test_cross_sectional_g4.py`）。

**未实现**：`state_cross_exchange_price_deviations`（Makarov & Schoar 2020，跨交易所价差）需要多交易所数据，超出已批准的数据范围
（ADR-0022：仅 Binance），因此不在本库实现；数据范围扩大需先有 ADR。

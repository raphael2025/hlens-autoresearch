# plugins/

通用 / 基础设施类 Provider 与 Adapter：Collector、Backtest、Knowledge、LLM、SyntheticMarket、Storage、Catalog 等（05-plugin.md）。

## 当前内容

| 模块 | 用途 |
|---|---|
| `features/` | Phase 1 F4（ADR-0030）首批 `FeatureProvider`：`BarLogReturnProvider`、`BarRealizedVolatilityProvider`、`BarVolumeSumProvider`——确定性、精确 `Decimal`、无浮点；窗口、输出精度、`available_lag` 与输入 representation 都是 `FeatureSpec` 参数；只依赖 `core`，经 `tests/contract_suites/feature.py` 检查 |
| `states/` | Phase 2（ADR-0035；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）首批 `StateProvider`：`VolatilityRegimeProvider`、`LiquidityRegimeProvider`（固定尾随窗口经验分位分桶，训练型、固定 seed）、`TrendRangeProvider`（效率比，规则型）——确定性、精确 `Decimal`；参数编码在 `StateSpec.method`、无默认值；资金费率体制为 declared-unavailable；只依赖 `core`，经 `tests/contract_suites/state.py` 检查 |

# plugins/

通用 / 基础设施类 Provider 与 Adapter：Collector、Backtest、Knowledge、LLM、SyntheticMarket、Storage、Catalog 等（05-plugin.md）。

## 当前内容

| 模块 | 用途 |
|---|---|
| `backtest/` | Phase 5（ADR-0038）`BarBacktester`：确定性、`Decimal`、bar 级回测 v1；下一根 bar 开盘成交，成本模型 = 费率 + 不利滑点；**只是模拟**（无下单、无密钥、无网络）；经 `tests/contract_suites/backtest.py` 的基准一致性检查 |
| `events/` | Phase 3（ADR-0036）首批 `EventProvider`：`FeatureThresholdCrossProvider`、`VolatilityBreakoutProvider`、`StateSwitchProvider` 与交互算子 `EventSequenceProvider`、`EventCoOccurrenceProvider`——确定性、精确 `Decimal`；参数写入 `EventSpec.trigger` 的规范 JSON 并由 spec hash 绑定；交互输出引用上游 `event_id`；只依赖 `core`，经 `tests/contract_suites/event.py` 检查 |
| `features/` | Phase 1 F4（ADR-0030）首批 `FeatureProvider`：`BarLogReturnProvider`、`BarRealizedVolatilityProvider`、`BarVolumeSumProvider`——确定性、精确 `Decimal`、无浮点；窗口、输出精度、`available_lag` 与输入 representation 都是 `FeatureSpec` 参数；只依赖 `core`，经 `tests/contract_suites/feature.py` 检查 |
| `states/` | Phase 2（ADR-0035；FRAMEWORK_IMPLEMENTED / NOT_VALIDATED）首批 `StateProvider`：`VolatilityRegimeProvider`、`LiquidityRegimeProvider`（固定尾随窗口经验分位分桶，训练型、固定 seed）、`TrendRangeProvider`（效率比，规则型）——确定性、精确 `Decimal`；参数编码在 `StateSpec.method`、无默认值；资金费率体制为 declared-unavailable；只依赖 `core`，经 `tests/contract_suites/state.py` 检查 |

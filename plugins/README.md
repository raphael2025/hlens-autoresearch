# plugins/

通用 / 基础设施类 Provider 与 Adapter：Collector、Backtest、Knowledge、LLM、SyntheticMarket、Storage、Catalog 等（05-plugin.md）。

## 当前内容

| 模块 | 用途 |
|---|---|
| `features/` | Phase 1 F4（ADR-0030）首批 `FeatureProvider`：`BarLogReturnProvider`、`BarRealizedVolatilityProvider`、`BarVolumeSumProvider`——确定性、精确 `Decimal`、无浮点；窗口、输出精度、`available_lag` 与输入 representation 都是 `FeatureSpec` 参数；只依赖 `core`，经 `tests/contract_suites/feature.py` 检查 |

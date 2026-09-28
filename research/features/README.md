# research/features

FeatureProvider 实现（探索期）。必须声明 available_lag。登记于 docs/research/feature-library.md。

> 状态：Phase 1 已开启，F4（[ADR-0030](../../docs/adr/0030-feature-provider-contract.md)）首批 3 个 FeatureProvider
> （`bar_log_return`、`bar_realized_vol_<n>`、`bar_volume_sum_<n>`）已实现，但直接进入 `plugins/features/bars.py`
> （执行器 `infrastructure/feature/`），因为它们的定义与参数已由 ADR-0030 完整规格化，不需要先在此探索。
>
> 本目录仍**无代码**：它是 `docs/research/feature-library.md`「候选方法」表中尚未规格化的方法（区间波动率
> Parkinson / Garman–Klass / Yang–Zhang、经典指标 ATR / RSI / MACD / 布林带 / VWAP、Taker 流量、Amihud 非流动性、
> Corwin–Schultz 价差、分数阶差分等，均标记 `DOCUMENTED · UNSPECIFIED`）的实现位置。这些方法的窗口、会话边界
> （24/7 市场无"隔夜"）、异常值处理与参数均未被任何已接受 ADR 或现有契约定义；在提出并批准新的 `FeatureSpec`
> 规格之前不得实现（CLAUDE.md H1 / H3 / H5；见 feature-library.md 缺口 F-2）。aggTrades → Feature 的输入路径
> （feature-library.md 缺口 F-1，例如成交量 bar / tick bar 等 Representation）同样未被任何 ADR 定义，不在此目录
> 预先实现。

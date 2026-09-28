"""FeatureProvider plugins (Phase 1 F4; ADR-0030; ADR-0085 batch 2).

- ``bars``: ``BarLogReturnProvider``, ``BarRealizedVolatilityProvider``, ``BarVolumeSumProvider`` —
  deterministic, exact-``Decimal`` features over bar observations; every parameter is in the spec.
- ``indicators``: ``AtrProvider`` (IND-ATR-001), ``RsiProvider`` (IND-RSI-001),
  ``MacdLineProvider`` / ``MacdSignalProvider`` (IND-MACD-001), ``BbandsPercentBProvider`` /
  ``BbandsBandwidthProvider`` (IND-BBANDS-001), ``VwapProvider`` (IND-VWAP-001), ``AdxProvider``
  (FEA-TREND-STRENGTH-001) — classic technical indicators over the same bar observations; no
  parameter has a default value (ADR-0085 §"通用规则" #2). Also ``BarCloseProvider`` /
  ``BarHighProvider`` / ``BarLowProvider`` (``bar_close`` / ``bar_high`` / ``bar_low``, no
  parameters): the latest visible bar's close / high / low, exact — added at PM's direction for
  ``donchian_breakout`` / ``zscore_reversion`` to read a price signal from.
- ``range_volatility``: ``ParkinsonVolatilityProvider`` (FEA-PARKINSON-001),
  ``GarmanKlassVolatilityProvider`` (FEA-GK-001), ``YangZhangVolatilityProvider`` (FEA-YZ-001),
  ``JumpVarianceProvider`` (FEA-JUMP-QV-001) — range- and jump-based realized-volatility estimators
  over bar observations; no parameter has a default value.
- ``microstructure``: ``TakerFlowImbalanceProvider`` (FEA-TAKER-FLOW-001),
  ``AmihudIlliquidityProvider`` (MSTX-AMIHUD-001), ``CorwinSchultzSpreadProvider``
  (FEA-CS-SPREAD-001) — kline-derived microstructure features; no parameter has a default value.
"""

from plugins.features.bars import (
    BAR_1M_INPUT,
    BarLogReturnProvider,
    BarRealizedVolatilityProvider,
    BarVolumeSumProvider,
)
from plugins.features.indicators import (
    AdxProvider,
    AtrProvider,
    BarCloseProvider,
    BarHighProvider,
    BarLowProvider,
    BbandsBandwidthProvider,
    BbandsPercentBProvider,
    MacdLineProvider,
    MacdSignalProvider,
    RsiProvider,
    VwapProvider,
)
from plugins.features.microstructure import (
    AmihudIlliquidityProvider,
    CorwinSchultzSpreadProvider,
    TakerFlowImbalanceProvider,
)
from plugins.features.range_volatility import (
    GarmanKlassVolatilityProvider,
    JumpVarianceProvider,
    ParkinsonVolatilityProvider,
    YangZhangVolatilityProvider,
)

__all__ = [
    "BAR_1M_INPUT",
    "AdxProvider",
    "AmihudIlliquidityProvider",
    "AtrProvider",
    "BarCloseProvider",
    "BarHighProvider",
    "BarLogReturnProvider",
    "BarLowProvider",
    "BarRealizedVolatilityProvider",
    "BarVolumeSumProvider",
    "BbandsBandwidthProvider",
    "BbandsPercentBProvider",
    "CorwinSchultzSpreadProvider",
    "GarmanKlassVolatilityProvider",
    "JumpVarianceProvider",
    "MacdLineProvider",
    "MacdSignalProvider",
    "ParkinsonVolatilityProvider",
    "RsiProvider",
    "TakerFlowImbalanceProvider",
    "VwapProvider",
    "YangZhangVolatilityProvider",
]

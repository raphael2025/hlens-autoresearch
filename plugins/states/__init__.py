"""StateProvider plugins (Phase 2; ADR-0035).

- ``regimes``: ``VolatilityRegimeProvider`` / ``LiquidityRegimeProvider`` (empirical-quantile
  buckets over a fixed trailing window) and ``TrendRangeProvider`` (efficiency ratio) —
  deterministic, exact-``Decimal`` states over feature values; every parameter is in the spec.
  A funding-rate regime is declared unavailable (``DECLARED_UNAVAILABLE``).
"""

from plugins.states.regimes import (
    DECLARED_UNAVAILABLE,
    LIQUIDITY_LABELS,
    TREND_LABELS,
    VOLATILITY_LABELS,
    LiquidityRegimeProvider,
    TrendRangeProvider,
    VolatilityRegimeProvider,
)

__all__ = [
    "DECLARED_UNAVAILABLE",
    "LIQUIDITY_LABELS",
    "TREND_LABELS",
    "VOLATILITY_LABELS",
    "LiquidityRegimeProvider",
    "TrendRangeProvider",
    "VolatilityRegimeProvider",
]

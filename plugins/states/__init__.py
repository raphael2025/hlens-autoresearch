"""StateProvider plugins (Phase 2; ADR-0035, ADR-0085).

- ``regimes``: ``VolatilityRegimeProvider`` / ``LiquidityRegimeProvider`` (empirical-quantile
  buckets over a fixed trailing window) and ``TrendRangeProvider`` (efficiency ratio) —
  deterministic, exact-``Decimal`` states over feature values; every parameter is in the spec.
  A funding-rate regime is declared unavailable (``DECLARED_UNAVAILABLE``).
- ``volatility_events`` (ADR-0085): ``VolatilitySqueezeProvider`` (short/long volatility ratio vs.
  explicit bands: ``squeeze`` / ``normal`` / ``expansion``) and ``ReturnShockProvider`` (``|return|
  > k * vol``: ``calm`` / ``shock``) — rule-based, two-feature states; no defaults.
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
from plugins.states.volatility_events import (
    SHOCK_LABELS,
    SQUEEZE_LABELS,
    ReturnShockProvider,
    VolatilitySqueezeProvider,
)

__all__ = [
    "DECLARED_UNAVAILABLE",
    "LIQUIDITY_LABELS",
    "SHOCK_LABELS",
    "SQUEEZE_LABELS",
    "TREND_LABELS",
    "VOLATILITY_LABELS",
    "LiquidityRegimeProvider",
    "ReturnShockProvider",
    "TrendRangeProvider",
    "VolatilityRegimeProvider",
    "VolatilitySqueezeProvider",
]

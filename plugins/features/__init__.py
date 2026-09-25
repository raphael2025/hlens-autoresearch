"""FeatureProvider plugins (Phase 1 F4; ADR-0030).

- ``bars``: ``BarLogReturnProvider``, ``BarRealizedVolatilityProvider``, ``BarVolumeSumProvider`` —
  deterministic, exact-``Decimal`` features over bar observations; every parameter is in the spec.
"""

from plugins.features.bars import (
    BAR_1M_INPUT,
    BarLogReturnProvider,
    BarRealizedVolatilityProvider,
    BarVolumeSumProvider,
)

__all__ = [
    "BAR_1M_INPUT",
    "BarLogReturnProvider",
    "BarRealizedVolatilityProvider",
    "BarVolumeSumProvider",
]

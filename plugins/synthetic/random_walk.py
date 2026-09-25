"""A seeded random-walk market with optional planted return autocorrelation (Phase 9).

Per minute ``t``: ``r_t = drift + e_t + sum(strength_k * r_{t - lag_k})`` with ``e_t ~ N(0,
volatility)`` from ``random.Random(seed)``; ``close_t = close_{t-1} * (1 + r_t)``. Every value is
quantized to a fixed ``Decimal`` scale right after it is drawn, so the output is a pure function
of the spec (the same seed gives the same ``market_hash``). ``truth`` lists exactly the effects
with non-zero strength; with none, the market is pure noise (the null a validation pipeline must
not reject too often).
"""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

from core.contracts.synthetic import (
    SyntheticBar,
    SyntheticMarket,
    SyntheticMarketSpec,
    SyntheticProviderDescriptor,
    market_hash,
)

__all__ = ["RandomWalkMarket"]

_RETURN_SCALE: Final = Decimal("1e-12")
_PRICE_SCALE: Final = Decimal("1e-8")
_MINUTE: Final = timedelta(minutes=1)


def _q(value: Decimal, scale: Decimal) -> Decimal:
    return value.quantize(scale, rounding=ROUND_HALF_EVEN)


class RandomWalkMarket:
    """Deterministic given the spec's seed."""

    def __init__(self) -> None:
        self._descriptor = SyntheticProviderDescriptor(
            name="hlens_synthetic_random_walk", version="1.0.0", deterministic=True
        )

    @property
    def descriptor(self) -> SyntheticProviderDescriptor:
        return self._descriptor

    def generate(self, spec: SyntheticMarketSpec) -> SyntheticMarket:
        rng = random.Random(spec.seed)
        effects = tuple(effect for effect in spec.effects if effect.strength != 0)
        returns: list[Decimal] = []
        bars: list[SyntheticBar] = []
        close = spec.initial_price
        for minute in range(spec.minutes):
            noise = _q(Decimal(repr(rng.gauss(0.0, 1.0))) * spec.volatility, _RETURN_SCALE)
            carried = sum(
                (
                    effect.strength * returns[minute - effect.lag_minutes]
                    for effect in effects
                    if minute - effect.lag_minutes >= 0
                ),
                Decimal(0),
            )
            r = _q(spec.drift + noise + carried, _RETURN_SCALE)
            returns.append(r)
            opened = close
            close = _q(max(opened * (1 + r), _PRICE_SCALE), _PRICE_SCALE)
            spread = _q(Decimal(repr(rng.random())) * spec.volatility / 2, _RETURN_SCALE)
            high = _q(max(opened, close) * (1 + spread), _PRICE_SCALE)
            low = _q(min(opened, close) * (1 - spread), _PRICE_SCALE)
            volume = _q(Decimal(repr(rng.uniform(1.0, 100.0))), _PRICE_SCALE)
            start = spec.start + minute * _MINUTE
            bars.append(
                SyntheticBar(
                    interval_start=start,
                    interval_end=start + _MINUTE,
                    open=opened,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    trade_count=rng.randint(1, 500),
                )
            )
        spec_hash = spec.content_hash()
        provider = self._descriptor.plugin_key
        frozen = tuple(bars)
        return SyntheticMarket(
            spec_hash=spec_hash,
            provider=provider,
            bars=frozen,
            truth=effects,
            market_hash=market_hash(spec_hash, provider, frozen, effects),
        )

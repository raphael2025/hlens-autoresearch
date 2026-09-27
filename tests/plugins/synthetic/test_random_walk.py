"""Phase 9: the seeded synthetic market (ADR-0042)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.contracts.synthetic import PlantedEffect, SyntheticBar, SyntheticMarketSpec
from plugins.synthetic import RandomWalkMarket


def _spec(**overrides: object) -> SyntheticMarketSpec:
    fields: dict[str, object] = {
        "name": "noise",
        "version": "1.0.0",
        "symbol": "SYN-USDT",
        "start": datetime(2024, 1, 1, tzinfo=UTC),
        "minutes": 500,
        "seed": 7,
        "initial_price": Decimal("100"),
        "volatility": Decimal("0.001"),
    }
    fields.update(overrides)
    return SyntheticMarketSpec(**fields)  # type: ignore[arg-type]


def _returns(bars: tuple[SyntheticBar, ...]) -> list[float]:
    return [float(bar.close / bar.open - 1) for bar in bars]


def _autocorrelation(values: list[float], lag: int) -> float:
    mean = sum(values) / len(values)
    num = sum((values[i] - mean) * (values[i - lag] - mean) for i in range(lag, len(values)))
    den = sum((v - mean) ** 2 for v in values)
    return num / den


def test_the_same_seed_gives_the_same_market() -> None:
    a = RandomWalkMarket().generate(_spec())
    b = RandomWalkMarket().generate(_spec())
    c = RandomWalkMarket().generate(_spec(seed=8))
    assert a.market_hash == b.market_hash != c.market_hash
    assert a.truth == ()


def test_bars_are_lawful_minutes() -> None:
    market = RandomWalkMarket().generate(_spec())
    for bar in market.bars:
        assert bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
    assert market.bars[1].open == market.bars[0].close


def test_a_planted_autocorrelation_is_recorded_and_visible() -> None:
    effect = PlantedEffect(lag_minutes=1, strength=Decimal("0.6"))
    planted = RandomWalkMarket().generate(_spec(minutes=3000, effects=(effect,)))
    noise = RandomWalkMarket().generate(_spec(minutes=3000))
    assert planted.truth == (effect,)
    assert _autocorrelation(_returns(planted.bars), 1) > 0.4
    assert abs(_autocorrelation(_returns(noise.bars), 1)) < 0.1


def test_a_non_stationary_effect_is_refused() -> None:
    with pytest.raises(ValidationError, match="strength"):
        PlantedEffect(lag_minutes=1, strength=Decimal("1"))

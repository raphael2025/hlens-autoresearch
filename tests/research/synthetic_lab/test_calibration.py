"""Phase 9 framework: false-positive rate and power of a detector on known truth."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.contracts.synthetic import PlantedEffect, SyntheticMarket, SyntheticMarketSpec
from plugins.synthetic import RandomWalkMarket
from research.synthetic_lab import calibrate

BASE = SyntheticMarketSpec(
    name="noise",
    version="1.0.0",
    symbol="SYN-USDT",
    start=datetime(2024, 1, 1, tzinfo=UTC),
    minutes=400,
    seed=0,
    initial_price=Decimal("100"),
    volatility=Decimal("0.001"),
)
EFFECT = PlantedEffect(lag_minutes=1, strength=Decimal("0.5"))


def _lag1_autocorrelation_detector(market: SyntheticMarket) -> bool:
    """A toy detector (test-only cut-off): |lag-1 autocorrelation| of returns > 0.2."""
    returns = [float(bar.close / bar.open - 1) for bar in market.bars]
    mean = sum(returns) / len(returns)
    num = sum((returns[i] - mean) * (returns[i - 1] - mean) for i in range(1, len(returns)))
    den = sum((r - mean) ** 2 for r in returns)
    return abs(num / den) > 0.2


def test_a_sound_detector_has_low_false_positives_and_high_power() -> None:
    report = calibrate(
        RandomWalkMarket(), BASE, EFFECT, _lag1_autocorrelation_detector,
        detector_name="toy_lag1", trials=10,
    )  # fmt: skip
    assert report.false_positive_rate <= Decimal("0.1")
    assert report.power >= Decimal("0.9")


def test_a_detector_that_always_fires_is_exposed() -> None:
    report = calibrate(
        RandomWalkMarket(), BASE, EFFECT, lambda _: True, detector_name="always", trials=3
    )
    assert report.false_positive_rate == Decimal(1)


def test_the_base_must_be_noise() -> None:
    with pytest.raises(ValueError, match="pure noise"):
        calibrate(
            RandomWalkMarket(), BASE.model_copy(update={"effects": (EFFECT,)}), EFFECT,
            lambda _: False, detector_name="x", trials=1,
        )  # fmt: skip


def test_a_raising_detector_is_counted_as_an_error_never_as_a_detection() -> None:
    def flaky(market: SyntheticMarket) -> bool:
        if market.truth:
            raise RuntimeError("detector crashed on this market")
        return True

    report = calibrate(RandomWalkMarket(), BASE, EFFECT, flaky, detector_name="flaky", trials=3)
    assert report.planted_errors == 3 and report.detections == 0 and report.power == 0
    assert report.noise_errors == 0 and report.false_positive_rate == Decimal(1)


def test_a_detector_without_errors_reports_zero_errors() -> None:
    report = calibrate(
        RandomWalkMarket(), BASE, EFFECT, lambda _: False, detector_name="never", trials=2
    )
    assert (report.noise_errors, report.planted_errors) == (0, 0)

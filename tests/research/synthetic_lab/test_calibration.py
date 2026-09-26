"""Phase 9 framework: false-positive rate and power of a detector on known truth."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.contracts.synthetic import PlantedEffect, SyntheticMarket, SyntheticMarketSpec
from plugins.synthetic import RandomWalkMarket
from research.synthetic_lab import calibrate
from research.synthetic_lab.calibration import PROPAGATED_ERRORS
from research.synthetic_lab.gate_calibration import DetectorConfigurationError
from research.validation.gates import ProfileFieldMissing, profile_value
from research.validation.stats import UnsupportedMethod
from tests.research.synthetic_lab import gate_fixtures as fx

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


def test_a_runtime_failure_reports_bounded_rates() -> None:
    def flaky(market: SyntheticMarket) -> bool:
        if market.truth:
            raise RuntimeError("detector crashed on this market")
        return market.spec_hash < "8"  # a deterministic mix of detections on noise

    report = calibrate(RandomWalkMarket(), BASE, EFFECT, flaky, detector_name="flaky", trials=4)
    assert report.power_bounds == (Decimal(0), Decimal(1))
    fp = Decimal(report.false_positives) / 4
    assert report.false_positive_rate_bounds == (fp, fp)  # no noise errors: a point


def _needs_a_missing_profile_field(market: SyntheticMarket) -> bool:
    """TEST ONLY: a detector whose validator reads a field the candidate does not carry."""
    profile_value(fx.LAX_TEST_ONLY_PROFILE, "significance.negative_control_threshold")
    return True


def test_a_profile_missing_a_field_the_validator_needs_raises() -> None:
    with pytest.raises(ProfileFieldMissing, match="negative_control_threshold"):
        calibrate(
            RandomWalkMarket(), BASE, EFFECT, _needs_a_missing_profile_field,
            detector_name="missing_field", trials=2,
        )  # fmt: skip


@pytest.mark.parametrize(
    "error",
    [
        UnsupportedMethod("null_model 'x' is not implemented"),
        DetectorConfigurationError("setup_for must use the trial runner it is given"),
        ValueError("a deliberate input refusal"),
        TypeError("wrong argument"),
        MemoryError(),
    ],
    ids=lambda error: type(error).__name__,
)
def test_configuration_errors_propagate_unchanged(error: BaseException) -> None:
    def refusing(market: SyntheticMarket) -> bool:
        raise error

    with pytest.raises(type(error)) as caught:
        calibrate(RandomWalkMarket(), BASE, EFFECT, refusing, detector_name="x", trials=1)
    assert caught.value is error


def test_the_propagated_errors_are_the_pipelines_own_classification() -> None:
    from research.validation import g4

    assert PROPAGATED_ERRORS == g4._PROPAGATED  # drift guard (module docs)

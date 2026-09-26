"""Phase 9 framework: false-positive rate and power of a detector on known truth."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext
from fractions import Fraction

import pytest

from core.contracts.synthetic import PlantedEffect, SyntheticMarket, SyntheticMarketSpec
from plugins.synthetic import RandomWalkMarket
from research.synthetic_lab import calibrate
from research.synthetic_lab.calibration import PROPAGATED_ERRORS, CalibrationReport
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


# (trials, hits, errors): repeating decimals, trials = 1, zero errors and integer divisions.
BOUND_CASES = [
    (3, 2, 0),  # B45 repro: a half-even 2 / 3 rounds up, above the exact rate
    (3, 1, 0),  # B45 repro: a half-even 1 / 3 rounds down, below the exact rate
    (3, 1, 1),
    (3, 0, 0),
    (3, 3, 0),
    (6, 1, 0),
    (7, 3, 2),
    (997, 1, 0),  # a small repeating rate: 28 significant digits, not 28 places
    (997, 996, 1),
    (1, 0, 0),
    (1, 1, 0),
    (1, 0, 1),
    (4, 1, 2),
    (4, 2, 0),
    (128, 1, 0),  # 0.0078125: exact at 28 digits, not at intervals.PLACES
    (10, 3, 4),
]
BOUND_IDS = [f"{hits}+{errors}of{trials}" for trials, hits, errors in BOUND_CASES]
_NEIGHBOUR = Context(prec=28)


def _fpr_report(trials: int, hits: int, errors: int) -> CalibrationReport:
    return CalibrationReport(
        detector="direct", trials=trials, false_positives=hits, detections=0,
        false_positive_rate=Decimal(hits) / trials, power=Decimal(0), planted=EFFECT,
        noise_errors=errors,
    )  # fmt: skip


def _power_report(trials: int, hits: int, errors: int) -> CalibrationReport:
    return CalibrationReport(
        detector="direct", trials=trials, false_positives=0, detections=hits,
        false_positive_rate=Decimal(0), power=Decimal(hits) / trials, planted=EFFECT,
        planted_errors=errors,
    )  # fmt: skip


BOUNDS: list[Callable[[int, int, int], tuple[Decimal, Decimal]]] = [
    lambda trials, hits, errors: _fpr_report(trials, hits, errors).false_positive_rate_bounds,
    lambda trials, hits, errors: _power_report(trials, hits, errors).power_bounds,
]
BOUND_NAMES = ["false_positive_rate_bounds", "power_bounds"]


def _assert_tight_outward(bounds: tuple[Decimal, Decimal], low: Fraction, high: Fraction) -> None:
    lower, upper = bounds
    assert Fraction(lower) <= low and Fraction(upper) >= high  # the exact rates are enclosed
    if Fraction(lower) != low:  # inexact: the greatest 28-digit Decimal below the exact rate
        assert Fraction(_NEIGHBOUR.next_plus(lower)) > low
    if Fraction(upper) != high:  # inexact: the least 28-digit Decimal above the exact rate
        assert Fraction(_NEIGHBOUR.next_minus(upper)) < high


@pytest.mark.parametrize("bounds_of", BOUNDS, ids=BOUND_NAMES)
@pytest.mark.parametrize(("trials", "hits", "errors"), BOUND_CASES, ids=BOUND_IDS)
def test_rate_bounds_enclose_the_exact_ratios(
    bounds_of: Callable[[int, int, int], tuple[Decimal, Decimal]],
    trials: int,
    hits: int,
    errors: int,
) -> None:
    low, high = Fraction(hits, trials), Fraction(hits + errors, trials)
    bounds = bounds_of(trials, hits, errors)
    assert all(type(endpoint) is Decimal for endpoint in bounds)
    _assert_tight_outward(bounds, low, high)


@pytest.mark.parametrize("bounds_of", BOUNDS, ids=BOUND_NAMES)
@pytest.mark.parametrize(("trials", "hits", "errors"), BOUND_CASES, ids=BOUND_IDS)
def test_rate_bounds_ignore_the_ambient_decimal_context(
    bounds_of: Callable[[int, int, int], tuple[Decimal, Decimal]],
    trials: int,
    hits: int,
    errors: int,
) -> None:
    expected = bounds_of(trials, hits, errors)
    with localcontext(prec=5, rounding=ROUND_HALF_UP):
        coarse = bounds_of(trials, hits, errors)
    with localcontext(prec=60):
        fine = bounds_of(trials, hits, errors)
    assert coarse == expected and fine == expected
    assert [str(endpoint) for endpoint in coarse] == [str(endpoint) for endpoint in expected]


@pytest.mark.parametrize("bounds_of", BOUNDS, ids=BOUND_NAMES)
@pytest.mark.parametrize(
    ("trials", "hits", "rate"),
    [(1, 0, "0"), (1, 1, "1"), (3, 0, "0"), (3, 3, "1"), (4, 2, "0.5"), (128, 1, "0.0078125")],
)
def test_exact_rates_without_errors_are_a_point(
    bounds_of: Callable[[int, int, int], tuple[Decimal, Decimal]],
    trials: int,
    hits: int,
    rate: str,
) -> None:
    assert bounds_of(trials, hits, 0) == (Decimal(rate), Decimal(rate))


def test_a_repeating_rate_without_errors_is_enclosed_one_digit_wide() -> None:
    seen: list[str] = []

    def fires_once_on_noise(market: SyntheticMarket) -> bool:
        if market.truth:
            return False
        seen.append(market.spec_hash)
        return len(seen) == 1

    report = calibrate(
        RandomWalkMarket(), BASE, EFFECT, fires_once_on_noise, detector_name="once", trials=3
    )
    lower, upper = report.false_positive_rate_bounds
    assert lower == Decimal("0." + "3" * 28) and upper == Decimal("0." + "3" * 27 + "4")
    assert lower <= report.false_positive_rate <= upper
    assert report.power_bounds == (Decimal(0), Decimal(0))


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

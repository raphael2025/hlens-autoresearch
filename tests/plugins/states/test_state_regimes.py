"""First StateProviders (Phase 2; ADR-0035): contract suite, hand-checked states, spec params.

All cut points / windows / thresholds here are test-only fixture parameters (tests/fake_states.py).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from core.contracts.state import StateInputError, StateRequest, parse_state_method, state_method
from core.domain.specs import StateSpec
from plugins.states import (
    DECLARED_UNAVAILABLE,
    LiquidityRegimeProvider,
    TrendRangeProvider,
    VolatilityRegimeProvider,
)
from tests.contract_suites.state import StateProviderContract, StateSubject
from tests.fake_states import (
    RETURN_FEATURE,
    TEST_CUTS,
    TEST_MIN_HISTORY,
    TEST_SEED,
    TEST_TREND_THRESHOLD,
    TEST_TREND_WINDOW,
    TEST_WINDOW,
    VOL_FEATURE,
    VOLUME_FEATURE,
    at,
    level_inputs,
    negate,
    return_inputs,
    value_input,
)

TIMES = tuple(at(i) for i in range(26))  # inputs run to minute 30: later inputs exist


def vol_spec(**overrides: Any) -> StateSpec:
    fields: dict[str, Any] = {
        "cuts": TEST_CUTS,
        "min_history": TEST_MIN_HISTORY,
        "training_window": TEST_WINDOW,
        "seed": TEST_SEED,
    }
    fields.update(overrides)
    return VolatilityRegimeProvider.spec(VOL_FEATURE, **fields)


def liquidity_spec() -> StateSpec:
    return LiquidityRegimeProvider.spec(
        VOLUME_FEATURE,
        cuts=TEST_CUTS,
        min_history=TEST_MIN_HISTORY,
        training_window=TEST_WINDOW,
        seed=TEST_SEED,
    )


def trend_spec() -> StateSpec:
    return TrendRangeProvider.spec(
        RETURN_FEATURE, window=TEST_TREND_WINDOW, threshold=TEST_TREND_THRESHOLD
    )


class TestVolatilityRegimeContract(StateProviderContract):
    @pytest.fixture
    def state_subject(self) -> StateSubject:
        spec = vol_spec()
        return StateSubject(
            open=lambda: VolatilityRegimeProvider((spec,)),
            spec=spec,
            inputs=level_inputs(VOL_FEATURE),
            evaluation_times=TIMES,
            perturb=negate,
        )


class TestLiquidityRegimeContract(StateProviderContract):
    @pytest.fixture
    def state_subject(self) -> StateSubject:
        spec = liquidity_spec()
        return StateSubject(
            open=lambda: LiquidityRegimeProvider((spec,)),
            spec=spec,
            inputs=level_inputs(VOLUME_FEATURE),
            evaluation_times=TIMES,
            perturb=negate,
        )


class TestTrendRangeContract(StateProviderContract):
    @pytest.fixture
    def state_subject(self) -> StateSubject:
        spec = trend_spec()
        return StateSubject(
            open=lambda: TrendRangeProvider((spec,)),
            spec=spec,
            inputs=return_inputs(),
            evaluation_times=TIMES,
            perturb=negate,
        )


# ======================================================================================
# states, checked by hand
# ======================================================================================


def _one(provider: Any, spec: StateSpec, inputs: Any, minute: int) -> Any:
    request = StateRequest(
        state=spec.ref, spec_hash=spec.content_hash(), evaluation_times=(at(minute),), inputs=inputs
    )
    [value] = provider.compute(request).values
    return value


def test_quantile_cuts_are_exact_order_statistics() -> None:
    """Cuts 0.3 / 0.7 over n = 10 values → the order statistics h_3 and h_7 (no interpolation)."""
    spec = vol_spec(cuts=("0.3", "0.7"), min_history=10, training_window=TEST_WINDOW * 2)
    provider = VolatilityRegimeProvider((spec,))
    history = [value_input(VOL_FEATURE, i, Decimal(i + 1)) for i in range(9)]
    for last, expected in (
        ("3", "low_vol"),
        ("3.5", "mid_vol"),
        ("7", "mid_vol"),
        ("8", "high_vol"),
    ):
        value = _one(provider, spec, (*history, value_input(VOL_FEATURE, 9, Decimal(last))), 9)
        assert value.state == expected, last
        assert (value.inputs_used, value.latest_input_time) == (10, at(9))


def test_too_little_history_or_a_none_latest_value_is_none() -> None:
    spec = vol_spec()
    provider = VolatilityRegimeProvider((spec,))
    short = [value_input(VOL_FEATURE, i, Decimal(i + 1)) for i in range(TEST_MIN_HISTORY - 1)]
    assert _one(provider, spec, short, TEST_MIN_HISTORY).state is None
    enough = [value_input(VOL_FEATURE, i, Decimal(i + 1)) for i in range(TEST_MIN_HISTORY)]
    gap = (*enough, value_input(VOL_FEATURE, TEST_MIN_HISTORY, None))
    assert _one(provider, spec, gap, TEST_MIN_HISTORY).state is None
    assert _one(provider, spec, enough, TEST_MIN_HISTORY).state is not None


def test_the_window_bounds_the_fit_even_when_called_directly() -> None:
    """Inputs older than the training window are never part of the fit (no full-sample fit)."""
    spec = vol_spec()
    provider = VolatilityRegimeProvider((spec,))
    recent = [value_input(VOL_FEATURE, 20 + i, Decimal(10 + i)) for i in range(10)]
    ancient = [value_input(VOL_FEATURE, i, Decimal(1000)) for i in range(10)]
    assert _one(provider, spec, recent, 29) == _one(provider, spec, [*ancient, *recent], 29)


def test_trend_range_labels() -> None:
    spec = trend_spec()
    provider = TrendRangeProvider((spec,))
    cases = {
        "trend_up": ["0.01"] * TEST_TREND_WINDOW,
        "trend_down": ["-0.01"] * TEST_TREND_WINDOW,
        "range": ["0.01", "-0.01", "0.01", "-0.01", "0.01"],
    }
    for expected, returns in cases.items():
        inputs = [value_input(RETURN_FEATURE, i, Decimal(r)) for i, r in enumerate(returns)]
        value = _one(provider, spec, inputs, TEST_TREND_WINDOW)
        assert value.state == expected
        assert value.inputs_used == TEST_TREND_WINDOW
    flat = [value_input(RETURN_FEATURE, i, Decimal(0)) for i in range(TEST_TREND_WINDOW)]
    assert _one(provider, spec, flat, TEST_TREND_WINDOW).state == "range"
    holed = [*flat[:-1], value_input(RETURN_FEATURE, TEST_TREND_WINDOW - 1, None)]
    assert _one(provider, spec, holed, TEST_TREND_WINDOW).state is None


def test_non_numeric_inputs_fail_closed() -> None:
    spec = trend_spec()
    inputs = [value_input(RETURN_FEATURE, i, True) for i in range(TEST_TREND_WINDOW)]  # type: ignore[arg-type]
    with pytest.raises(StateInputError):
        _one(TrendRangeProvider((spec,)), spec, inputs, TEST_TREND_WINDOW)


def test_inputs_of_another_feature_fail_closed() -> None:
    spec = vol_spec()
    with pytest.raises(StateInputError):
        _one(VolatilityRegimeProvider((spec,)), spec, level_inputs(VOLUME_FEATURE), 20)


# ======================================================================================
# spec parameters
# ======================================================================================


def test_parameters_live_in_the_spec_method_and_are_hash_bound() -> None:
    spec = vol_spec()
    name, params = parse_state_method(spec.method)
    assert name == "trailing_quantile_buckets"
    assert params == {"cuts": "0.3333,0.6667", "min_history": TEST_MIN_HISTORY}
    assert vol_spec(cuts=("0.25", "0.75")).content_hash() != spec.content_hash()
    assert vol_spec(training_window=TEST_WINDOW * 2).content_hash() != spec.content_hash()
    assert vol_spec(seed=TEST_SEED + 1).content_hash() != spec.content_hash()


@pytest.mark.parametrize(
    "overrides",
    [
        {"cuts": ("0.6", "0.4")},
        {"cuts": ("0", "0.5")},
        {"cuts": ("0.5",)},  # two cuts need three labels; one cut with three labels is refused
        {"min_history": 0},
        {"training_window": None},
    ],
)
def test_invalid_quantile_specs_are_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TypeError)):
        vol_spec(**overrides)


def test_a_provider_refuses_specs_it_did_not_build() -> None:
    good = vol_spec()
    untrained = good.model_copy(update={"seed": None})
    other_method = good.model_copy(
        update={"method": state_method("something_else", {"cuts": "0.5", "min_history": 3})}
    )
    loose = good.model_copy(update={"method": good.method.replace(":", ": ", 1)})
    for spec in (untrained, other_method, loose):
        with pytest.raises(ValueError):
            VolatilityRegimeProvider((spec,))
    with pytest.raises(ValueError):
        TrendRangeProvider((good,))


def test_funding_regime_is_declared_unavailable() -> None:
    assert "funding_regime" in DECLARED_UNAVAILABLE

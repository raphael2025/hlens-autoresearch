"""Two-feature rule states (ADR-0085): contract suite, hand-checked states, spec params.

All cut points / multipliers here are test-only fixture parameters (not validation thresholds
and not recommended model settings), following the convention of ``tests/fake_states.py`` and
``tests/plugins/states/test_state_regimes.py``.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from core.contracts.state import StateInputError, StateRequest, parse_state_method, state_method
from core.domain.base import Kind, Ref
from core.domain.specs import StateSpec
from plugins.states import ReturnShockProvider, VolatilitySqueezeProvider
from tests.contract_suites.state import StateProviderContract, StateSubject
from tests.fake_states import RETURN_FEATURE, VOL_FEATURE, at, negate, value_input

#: A second volatility feature distinct from ``VOL_FEATURE`` (short window); this state needs two.
LONG_VOL_FEATURE = Ref(kind=Kind.FEATURE, name="bar_realized_vol_20", version="1.0.0")
SHORT_VOL_FEATURE = VOL_FEATURE

TEST_SQUEEZE_BELOW = "0.5"
TEST_EXPANSION_ABOVE = "1.5"
TEST_SHOCK_K = "2"

TIMES = tuple(at(i) for i in range(16))  # inputs run to minute 20: later inputs exist


def squeeze_spec(**overrides: Any) -> StateSpec:
    fields: dict[str, Any] = {
        "squeeze_below": TEST_SQUEEZE_BELOW,
        "expansion_above": TEST_EXPANSION_ABOVE,
    }
    fields.update(overrides)
    return VolatilitySqueezeProvider.spec(SHORT_VOL_FEATURE, LONG_VOL_FEATURE, **fields)


def shock_spec(**overrides: Any) -> StateSpec:
    fields: dict[str, Any] = {"k": TEST_SHOCK_K}
    fields.update(overrides)
    return ReturnShockProvider.spec(RETURN_FEATURE, VOL_FEATURE, **fields)


def squeeze_inputs(count: int = 20, *, missing: int = 2) -> tuple[Any, ...]:
    """Deterministic, non-monotone, strictly positive short/long streams; the first are missing."""
    short = [
        value_input(SHORT_VOL_FEATURE, i, None if i < missing else Decimal(1 + (i % 5)))
        for i in range(count)
    ]
    long = [value_input(LONG_VOL_FEATURE, i, Decimal(2 + ((i * 3) % 6))) for i in range(count)]
    return (*short, *long)


def shock_inputs(count: int = 20, *, missing: int = 4) -> tuple[Any, ...]:
    """Return magnitude cycles above / below ``k * vol``; index ``missing`` is a gap."""
    returns = [
        value_input(
            RETURN_FEATURE,
            i,
            None if i == missing else (Decimal("0.05") if i % 6 == 0 else Decimal("0.005")),
        )
        for i in range(count)
    ]
    vols = [value_input(VOL_FEATURE, i, Decimal("0.01")) for i in range(count)]
    return (*returns, *vols)


def _squeeze_perturb(item: Any) -> Any:
    """Scales the two features by different factors: ratio-invariant scaling (e.g. negation of
    both) would not change any label, so this must NOT scale both sides by the same factor."""
    if item.value is None:
        return item
    factor = Decimal(3) if item.feature == SHORT_VOL_FEATURE else Decimal(7)
    return item.model_copy(update={"value": Decimal(item.value) * factor})


class TestVolatilitySqueezeContract(StateProviderContract):
    @pytest.fixture
    def state_subject(self) -> StateSubject:
        spec = squeeze_spec()
        return StateSubject(
            open=lambda: VolatilitySqueezeProvider((spec,)),
            spec=spec,
            inputs=squeeze_inputs(),
            evaluation_times=TIMES,
            perturb=_squeeze_perturb,
        )


class TestReturnShockContract(StateProviderContract):
    @pytest.fixture
    def state_subject(self) -> StateSubject:
        spec = shock_spec()
        return StateSubject(
            open=lambda: ReturnShockProvider((spec,)),
            spec=spec,
            inputs=shock_inputs(),
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


def _pair(short: Decimal | None, long: Decimal | None, minute: int = 0) -> tuple[Any, ...]:
    return (
        value_input(SHORT_VOL_FEATURE, minute, short),
        value_input(LONG_VOL_FEATURE, minute, long),
    )


def test_volatility_squeeze_labels_exact_at_the_band_edges() -> None:
    """squeeze_below = 0.5, expansion_above = 1.5; edges are exclusive (equal → normal)."""
    spec = squeeze_spec()
    provider = VolatilitySqueezeProvider((spec,))
    cases = {
        "squeeze": (Decimal("2"), Decimal("5")),  # 0.4 < 0.5
        "normal (== squeeze_below)": (Decimal("1"), Decimal("2")),  # 0.5, not < 0.5
        "normal (== expansion_above)": (Decimal("3"), Decimal("2")),  # 1.5, not > 1.5
        "normal (midpoint)": (Decimal("3"), Decimal("3")),  # 1.0
        "expansion": (Decimal("4"), Decimal("2")),  # 2.0 > 1.5
    }
    expected = ["squeeze", "normal", "normal", "normal", "expansion"]
    for (label, (short, long)), want in zip(cases.items(), expected, strict=True):
        value = _one(provider, spec, _pair(short, long), 0)
        assert value.state == want, label
        assert (value.inputs_used, value.latest_input_time) == (2, at(0))


def test_volatility_squeeze_missing_or_zero_denominator_is_none() -> None:
    spec = squeeze_spec()
    provider = VolatilitySqueezeProvider((spec,))
    assert _one(provider, spec, _pair(None, Decimal("2")), 0).state is None
    assert _one(provider, spec, _pair(Decimal("2"), None), 0).state is None
    assert _one(provider, spec, (), 0).state is None
    # Division by zero (zero long-window volatility) is treated as not computable, never raised.
    zero_long = _one(provider, spec, _pair(Decimal("2"), Decimal("0")), 0)
    assert zero_long.state is None
    assert zero_long.inputs_used == 0


def test_volatility_squeeze_only_uses_the_latest_visible_value_per_feature() -> None:
    """Later inputs (even for the same feature) never leak into an earlier evaluation time."""
    spec = squeeze_spec()
    provider = VolatilitySqueezeProvider((spec,))
    past = (
        value_input(SHORT_VOL_FEATURE, 0, Decimal("2")),
        value_input(LONG_VOL_FEATURE, 0, Decimal("5")),
    )
    future = (
        value_input(SHORT_VOL_FEATURE, 1, Decimal("999")),
        value_input(LONG_VOL_FEATURE, 1, Decimal("1")),
    )
    assert _one(provider, spec, past, 0) == _one(provider, spec, (*past, *future), 0)


def test_volatility_squeeze_non_numeric_inputs_fail_closed() -> None:
    spec = squeeze_spec()
    inputs = (
        value_input(SHORT_VOL_FEATURE, 0, True),  # type: ignore[arg-type]
        value_input(LONG_VOL_FEATURE, 0, Decimal("2")),
    )
    with pytest.raises(StateInputError):
        _one(VolatilitySqueezeProvider((spec,)), spec, inputs, 0)


def test_volatility_squeeze_inputs_of_another_feature_fail_closed() -> None:
    spec = squeeze_spec()
    stray = (
        value_input(SHORT_VOL_FEATURE, 0, Decimal("2")),
        value_input(LONG_VOL_FEATURE, 0, Decimal("5")),
        value_input(RETURN_FEATURE, 0, Decimal("0.01")),
    )
    with pytest.raises(StateInputError):
        _one(VolatilitySqueezeProvider((spec,)), spec, stray, 0)


def test_return_shock_labels_exact_at_the_threshold() -> None:
    """k = 2; threshold is k * vol; the boundary (== threshold) is calm, not shock."""
    spec = shock_spec()
    provider = ReturnShockProvider((spec,))
    cases = {
        "shock": (Decimal("0.05"), Decimal("0.01")),  # 0.05 > 0.02
        "calm (== threshold)": (Decimal("0.02"), Decimal("0.01")),  # 0.02, not > 0.02
        "calm (small)": (Decimal("0.005"), Decimal("0.01")),
        "shock (negative return)": (Decimal("-0.05"), Decimal("0.01")),  # |−0.05| > 0.02
    }
    expected = ["shock", "calm", "calm", "shock"]
    for (label, (r, vol)), want in zip(cases.items(), expected, strict=True):
        inputs = (
            value_input(RETURN_FEATURE, 0, r),
            value_input(VOL_FEATURE, 0, vol),
        )
        value = _one(provider, spec, inputs, 0)
        assert value.state == want, label
        assert (value.inputs_used, value.latest_input_time) == (2, at(0))


def test_return_shock_missing_input_is_none() -> None:
    spec = shock_spec()
    provider = ReturnShockProvider((spec,))
    missing_return = (value_input(VOL_FEATURE, 0, Decimal("0.01")),)
    missing_vol = (value_input(RETURN_FEATURE, 0, Decimal("0.05")),)
    assert _one(provider, spec, missing_return, 0).state is None
    assert _one(provider, spec, missing_vol, 0).state is None
    assert _one(provider, spec, (), 0).state is None


def test_return_shock_only_uses_the_latest_visible_value_per_feature() -> None:
    spec = shock_spec()
    provider = ReturnShockProvider((spec,))
    past = (
        value_input(RETURN_FEATURE, 0, Decimal("0.005")),
        value_input(VOL_FEATURE, 0, Decimal("0.01")),
    )
    future = (
        value_input(RETURN_FEATURE, 1, Decimal("5")),
        value_input(VOL_FEATURE, 1, Decimal("0.0001")),
    )
    assert _one(provider, spec, past, 0) == _one(provider, spec, (*past, *future), 0)


def test_return_shock_non_numeric_inputs_fail_closed() -> None:
    spec = shock_spec()
    inputs = (
        value_input(RETURN_FEATURE, 0, True),  # type: ignore[arg-type]
        value_input(VOL_FEATURE, 0, Decimal("0.01")),
    )
    with pytest.raises(StateInputError):
        _one(ReturnShockProvider((spec,)), spec, inputs, 0)


def test_return_shock_inputs_of_another_feature_fail_closed() -> None:
    spec = shock_spec()
    stray = (
        value_input(RETURN_FEATURE, 0, Decimal("0.05")),
        value_input(VOL_FEATURE, 0, Decimal("0.01")),
        value_input(LONG_VOL_FEATURE, 0, Decimal("0.01")),
    )
    with pytest.raises(StateInputError):
        _one(ReturnShockProvider((spec,)), spec, stray, 0)


# ======================================================================================
# spec parameters
# ======================================================================================


def test_squeeze_parameters_live_in_the_spec_method_and_are_hash_bound() -> None:
    spec = squeeze_spec()
    name, params = parse_state_method(spec.method)
    assert name == "vol_ratio_bands"
    assert params == {"squeeze_below": TEST_SQUEEZE_BELOW, "expansion_above": TEST_EXPANSION_ABOVE}
    assert squeeze_spec(squeeze_below="0.4").content_hash() != spec.content_hash()
    assert squeeze_spec(expansion_above="1.6").content_hash() != spec.content_hash()


@pytest.mark.parametrize(
    "overrides",
    [
        {"squeeze_below": "0", "expansion_above": "1.5"},
        {"squeeze_below": "-0.1", "expansion_above": "1.5"},
        {"squeeze_below": "1.5", "expansion_above": "0.5"},  # not squeeze_below < expansion_above
        {"squeeze_below": "1", "expansion_above": "1"},  # equal is refused too
    ],
)
def test_invalid_squeeze_specs_are_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        squeeze_spec(**overrides)


def test_squeeze_spec_needs_two_distinct_features() -> None:
    with pytest.raises(ValueError):
        VolatilitySqueezeProvider.spec(
            SHORT_VOL_FEATURE,
            SHORT_VOL_FEATURE,
            squeeze_below=TEST_SQUEEZE_BELOW,
            expansion_above=TEST_EXPANSION_ABOVE,
        )


def test_a_squeeze_provider_refuses_specs_it_did_not_build() -> None:
    good = squeeze_spec()
    trained = good.model_copy(update={"training_window": timedelta(minutes=5), "seed": 1})
    other_method = good.model_copy(
        update={"method": state_method("something_else", {"squeeze_below": "0.5"})}
    )
    loose = good.model_copy(update={"method": good.method.replace(":", ": ", 1)})
    for spec in (trained, other_method, loose):
        with pytest.raises(ValueError):
            VolatilitySqueezeProvider((spec,))
    with pytest.raises(ValueError):
        ReturnShockProvider((good,))


def test_shock_parameters_live_in_the_spec_method_and_are_hash_bound() -> None:
    spec = shock_spec()
    name, params = parse_state_method(spec.method)
    assert name == "abs_return_vol_multiple"
    assert params == {"k": TEST_SHOCK_K}
    assert shock_spec(k="3").content_hash() != spec.content_hash()


@pytest.mark.parametrize("overrides", [{"k": "0"}, {"k": "-1"}])
def test_invalid_shock_specs_are_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        shock_spec(**overrides)


def test_shock_spec_needs_two_distinct_features() -> None:
    with pytest.raises(ValueError):
        ReturnShockProvider.spec(RETURN_FEATURE, RETURN_FEATURE, k=TEST_SHOCK_K)


def test_a_shock_provider_refuses_specs_it_did_not_build() -> None:
    good = shock_spec()
    trained = good.model_copy(update={"training_window": timedelta(minutes=5), "seed": 1})
    other_method = good.model_copy(update={"method": state_method("something_else", {"k": "2"})})
    for spec in (trained, other_method):
        with pytest.raises(ValueError):
            ReturnShockProvider((spec,))
    with pytest.raises(ValueError):
        VolatilitySqueezeProvider((good,))

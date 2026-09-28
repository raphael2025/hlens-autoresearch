"""Range-volatility FeatureProviders (ADR-0085): contract suite, hand-checked values, edge cases."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from core.contracts.feature import (
    FeatureInputError,
    FeatureObservation,
    FeatureProvider,
    FeatureRequest,
    UnsupportedFeature,
)
from core.contracts.universe import SelectedRevisionLineage
from core.domain.base import FrozenMapping, content_hash
from core.domain.specs import FeatureSpec
from plugins.features.range_volatility import (
    GarmanKlassVolatilityProvider,
    JumpVarianceProvider,
    ParkinsonVolatilityProvider,
    YangZhangVolatilityProvider,
)
from tests.contract_suites.feature import FeatureProviderContract, FeatureSubject

T0 = datetime(2024, 3, 1, 12, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
LAG = timedelta(minutes=1)
CUTOFF = datetime(2024, 3, 2, tzinfo=UTC)
MANIFEST = content_hash({"manifest": "range-volatility-features"})


def bar(
    minute: int,
    close: str,
    *,
    high: str | None = None,
    low: str | None = None,
    open_: str | None = None,
    delay: int = 0,
    revision: str | None = None,
    symbol: str = "BTC-USDT",
) -> FeatureObservation:
    """A 1m OHLC bar starting at ``T0 + minute``, available ``delay`` minutes after it closes."""
    start = T0 + minute * MINUTE
    close_d = Decimal(close)
    values: dict[str, Decimal | int | bool | str] = {
        "symbol": symbol,
        "open": Decimal(open_) if open_ is not None else close_d - 1,
        "high": Decimal(high) if high is not None else close_d + 2,
        "low": Decimal(low) if low is not None else close_d - 2,
        "close": close_d,
        "volume": Decimal("10"),
        "trade_count": 3,
    }
    return FeatureObservation(
        observation_key=f"bar:{symbol}:{minute}",
        event_time=start,
        event_end_time=start + MINUTE,
        available_time=start + MINUTE + delay * MINUTE,
        knowledge_time=start + MINUTE + (delay + 1) * MINUTE,
        values=FrozenMapping(values),
        lineage=SelectedRevisionLineage(
            canonical_table="canonical.bars_1m",
            canonical_revision_id=revision or f"crev-{minute}",
            raw_table="raw.binance_spot_klines_1m",
            raw_revision_id=f"raw-{minute}",
            source_table="raw.binance_spot_archives",
            source_revision_id="archive-1",
        ),
    )


#: Minutes 0..9 without minute 6 (a gap), uneven availability, and a later replacement of minute 3.
SUITE_BARS = (
    bar(0, "100"),
    bar(1, "101"),
    bar(2, "103", delay=2),
    bar(3, "102"),
    bar(3, "102.5", delay=5, revision="crev-3b"),
    bar(4, "104"),
    bar(5, "103", delay=1),
    bar(7, "105"),
    bar(8, "106", delay=3),
    bar(9, "107"),
)
SUITE_TIMES = tuple(T0 + minute * MINUTE for minute in range(0, 17))


def _perturb(item: FeatureObservation) -> FeatureObservation:
    values = dict(item.values)
    for key in ("open", "high", "low", "close"):
        values[key] = values[key] * 2 + 1  # type: ignore[operator]
    return item.model_copy(update={"values": values})


def _subject(provider: type[Any], spec: FeatureSpec) -> FeatureSubject:
    return FeatureSubject(
        open=lambda: provider((spec,)),
        spec=spec,
        observations=SUITE_BARS,
        evaluation_times=SUITE_TIMES,
        knowledge_cutoff=CUTOFF,
        manifest_content_hash=MANIFEST,
        perturb=_perturb,
    )


class TestParkinsonContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        spec = ParkinsonVolatilityProvider.spec(2, available_lag=LAG)
        return _subject(ParkinsonVolatilityProvider, spec)


class TestGarmanKlassContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        spec = GarmanKlassVolatilityProvider.spec(2, available_lag=LAG)
        return _subject(GarmanKlassVolatilityProvider, spec)


class TestYangZhangContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        spec = YangZhangVolatilityProvider.spec(2, available_lag=LAG)
        return _subject(YangZhangVolatilityProvider, spec)


class TestJumpVarianceContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        return _subject(JumpVarianceProvider, JumpVarianceProvider.spec(2, available_lag=LAG))


# ======================================================================================
# values, checked by hand (independent math.* computation, never the implementation's own path)
# ======================================================================================


def _request(spec: FeatureSpec, bars: tuple[FeatureObservation, ...], *times: datetime) -> Any:
    return FeatureRequest(
        feature=spec.ref,
        spec_hash=spec.content_hash(),
        manifest_content_hash=MANIFEST,
        knowledge_cutoff=CUTOFF,
        evaluation_times=times,
        observations=bars,
    )


LATE = T0 + 30 * MINUTE

# Two independent, cleanly-specified OHLC bars for the range-only providers.
RANGE_PAIR = (
    bar(0, "102", high="105", low="95", open_="100"),
    bar(1, "104", high="110", low="90", open_="102"),
)


def test_parkinson_is_the_sqrt_trailing_mean_of_range_variance() -> None:
    spec = ParkinsonVolatilityProvider.spec(2)
    [value] = ParkinsonVolatilityProvider((spec,)).compute(_request(spec, RANGE_PAIR, LATE)).values
    ln2 = math.log(2)
    terms = [math.log(105 / 95) ** 2 / (4 * ln2), math.log(110 / 90) ** 2 / (4 * ln2)]
    expected = math.sqrt(sum(terms) / 2)
    assert value.value is not None and value.inputs_used == 2
    assert math.isclose(float(value.value), expected, rel_tol=1e-12)
    assert value.value == value.value.quantize(Decimal("1e-18"))


def test_garman_klass_is_the_sqrt_trailing_mean_of_its_variance() -> None:
    spec = GarmanKlassVolatilityProvider.spec(2)
    result = GarmanKlassVolatilityProvider((spec,)).compute(_request(spec, RANGE_PAIR, LATE))
    [value] = result.values

    def term(high: float, low: float, close: float, open_: float) -> float:
        hl2 = math.log(high / low) ** 2
        co2 = math.log(close / open_) ** 2
        return 0.5 * hl2 - (2 * math.log(2) - 1) * co2

    expected = math.sqrt((term(105, 95, 102, 100) + term(110, 90, 104, 102)) / 2)
    assert value.value is not None and value.inputs_used == 2
    assert math.isclose(float(value.value), expected, rel_tol=1e-12)
    assert value.value >= 0  # proven non-negative given valid OHLC; never NaN from a negative sqrt


def test_yang_zhang_is_the_sqrt_of_its_three_part_variance() -> None:
    triple = (
        bar(0, "102", high="104", low="96", open_="100"),
        bar(1, "105", high="107", low="99", open_="103"),
        bar(2, "107", high="109", low="101", open_="104"),
    )
    spec = YangZhangVolatilityProvider.spec(2)
    [value] = YangZhangVolatilityProvider((spec,)).compute(_request(spec, triple, LATE)).values

    o = [math.log(103 / 102), math.log(104 / 105)]
    c = [math.log(105 / 103), math.log(107 / 104)]
    rs = [
        math.log(107 / 105) * math.log(107 / 103) + math.log(99 / 105) * math.log(99 / 103),
        math.log(109 / 107) * math.log(109 / 104) + math.log(101 / 107) * math.log(101 / 104),
    ]
    o_mean, c_mean = sum(o) / 2, sum(c) / 2
    v_o = sum((x - o_mean) ** 2 for x in o) / 1
    v_c = sum((x - c_mean) ** 2 for x in c) / 1
    v_rs = sum(rs) / 2
    k = 0.34 / (1.34 + 3 / 1)
    expected = math.sqrt(v_o + k * v_c + (1 - k) * v_rs)

    assert value.value is not None and value.inputs_used == 3
    assert math.isclose(float(value.value), expected, rel_tol=1e-9)
    assert value.value >= 0


def test_jump_variance_is_rv_minus_bipower_variation_floored_at_zero() -> None:
    quad = (bar(0, "100"), bar(1, "101"), bar(2, "99"), bar(3, "103"))
    spec = JumpVarianceProvider.spec(3)
    [value] = JumpVarianceProvider((spec,)).compute(_request(spec, quad, LATE)).values

    returns = [math.log(101 / 100), math.log(99 / 101), math.log(103 / 99)]
    rv = sum(r * r for r in returns)
    bpv = (math.pi / 2) * sum(abs(returns[i]) * abs(returns[i - 1]) for i in range(1, len(returns)))
    expected = max(rv - bpv, 0.0)

    assert value.value is not None and value.inputs_used == 4
    assert math.isclose(float(value.value), expected, rel_tol=1e-9, abs_tol=1e-15)
    assert value.value >= 0


def test_jump_variance_is_never_negative_even_when_rv_below_bpv() -> None:
    # A single big jump followed by tiny returns pushes BPV (product of neighbours) low relative to
    # RV in general, but a pure oscillation pattern can push BPV above RV for the sample; either
    # way the floor must hold.
    alternating = (bar(0, "100"), bar(1, "100.01"), bar(2, "100"), bar(3, "100.01"), bar(4, "100"))
    spec = JumpVarianceProvider.spec(4)
    [value] = JumpVarianceProvider((spec,)).compute(_request(spec, alternating, LATE)).values
    assert value.value is not None and value.value >= 0


# ======================================================================================
# insufficient history / gaps => None, never filled
# ======================================================================================


def test_too_little_history_is_none() -> None:
    spec = ParkinsonVolatilityProvider.spec(5)
    values = ParkinsonVolatilityProvider((spec,)).compute(_request(spec, RANGE_PAIR, LATE)).values
    assert [item.value for item in values] == [None]


def test_a_gap_inside_the_window_is_none_never_filled() -> None:
    bars = (bar(0, "100"), bar(1, "101"), bar(3, "103"))  # minute 2 missing
    for provider, spec in (
        (ParkinsonVolatilityProvider, ParkinsonVolatilityProvider.spec(2)),
        (GarmanKlassVolatilityProvider, GarmanKlassVolatilityProvider.spec(2)),
        (YangZhangVolatilityProvider, YangZhangVolatilityProvider.spec(2)),
        (JumpVarianceProvider, JumpVarianceProvider.spec(2)),
    ):
        [value] = provider((spec,)).compute(_request(spec, bars, LATE)).values
        assert (value.value, value.inputs_used) == (None, 0), spec.name


def test_yang_zhang_window_below_two_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="window must be >= 2"):
        YangZhangVolatilityProvider.spec(1)
    with pytest.raises(ValueError, match="positive int"):
        YangZhangVolatilityProvider.spec(0)


# ======================================================================================
# malformed OHLC bars fail closed, never clamped
# ======================================================================================


@pytest.mark.parametrize(
    ("bars", "match"),
    [
        ((bar(0, "100", high="105", low="95"), bar(1, "101", symbol="ETH-USDT")), "one symbol"),
        (
            (
                bar(0, "100"),
                bar(0, "101", revision="x").model_copy(update={"observation_key": "dup"}),
            ),
            "overlap",
        ),
        ((bar(0, "100", high="105", low="0"),), "non-positive high/low"),
        ((bar(0, "100", high="90", low="95"),), "high < low"),
        ((bar(0, "100", high="105", low="95", open_="106"),), "open outside"),
    ],
)
def test_malformed_bars_fail_closed(bars: tuple[FeatureObservation, ...], match: str) -> None:
    spec = ParkinsonVolatilityProvider.spec(1)
    with pytest.raises(FeatureInputError, match=match):
        ParkinsonVolatilityProvider((spec,)).compute(_request(spec, bars, LATE))


def test_close_outside_low_high_fails_closed() -> None:
    spec = GarmanKlassVolatilityProvider.spec(1)
    bad = (bar(0, "100", high="99", low="90", open_="95"),)  # close (100) > high (99)
    with pytest.raises(FeatureInputError, match="close outside"):
        GarmanKlassVolatilityProvider((spec,)).compute(_request(spec, bad, LATE))


def test_a_point_observation_is_not_a_bar() -> None:
    point = bar(0, "100").model_copy(update={"event_end_time": None})
    spec = ParkinsonVolatilityProvider.spec(1)
    with pytest.raises(FeatureInputError, match="not a bar"):
        ParkinsonVolatilityProvider((spec,)).compute(_request(spec, (point,), LATE))


# ======================================================================================
# parameters live in the spec; no library defaults for window
# ======================================================================================


def test_window_and_scale_are_spec_parameters_bound_by_the_hash() -> None:
    two, three = ParkinsonVolatilityProvider.spec(2), ParkinsonVolatilityProvider.spec(3)
    assert (two.name, dict(two.params)) == ("parkinson_vol_2", {"window": 2, "scale": 18})
    assert two.content_hash() != three.content_hash()
    provider = ParkinsonVolatilityProvider((two, three))
    assert provider.descriptor.supported_features == {
        str(two.ref): two.content_hash(),
        str(three.ref): three.content_hash(),
    }


def test_a_spec_whose_params_disagree_with_its_definition_is_refused() -> None:
    honest = GarmanKlassVolatilityProvider.spec(3)
    forged = honest.model_copy(update={"params": {"window": 4, "scale": 18}})
    with pytest.raises(ValueError, match="not a garman_klass_vol spec"):
        GarmanKlassVolatilityProvider((forged,))
    with pytest.raises(ValueError, match="not a garman_klass_vol spec"):
        GarmanKlassVolatilityProvider((ParkinsonVolatilityProvider.spec(3),))
    with pytest.raises(ValueError, match="positive int"):
        GarmanKlassVolatilityProvider.spec(0)


def test_another_providers_spec_is_unsupported() -> None:
    spec = ParkinsonVolatilityProvider.spec(2)
    other = GarmanKlassVolatilityProvider.spec(2)
    with pytest.raises(UnsupportedFeature):
        ParkinsonVolatilityProvider((spec,)).compute(_request(other, RANGE_PAIR, LATE))


def test_providers_are_statically_substitutable() -> None:
    providers: list[FeatureProvider] = [
        ParkinsonVolatilityProvider((ParkinsonVolatilityProvider.spec(2),)),
        GarmanKlassVolatilityProvider((GarmanKlassVolatilityProvider.spec(2),)),
        YangZhangVolatilityProvider((YangZhangVolatilityProvider.spec(2),)),
        JumpVarianceProvider((JumpVarianceProvider.spec(2),)),
    ]
    assert all(provider.descriptor.deterministic for provider in providers)

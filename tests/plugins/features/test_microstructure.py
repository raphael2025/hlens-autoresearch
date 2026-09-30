"""Microstructure FeatureProviders (ADR-0085): contract suite, hand-checked values, edge cases."""

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
from plugins.features.microstructure import (
    AmihudIlliquidityProvider,
    CorwinSchultzSpreadProvider,
    TakerFlowImbalanceProvider,
)
from tests.contract_suites.feature import FeatureProviderContract, FeatureSubject

T0 = datetime(2024, 3, 1, 12, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
LAG = timedelta(minutes=1)
CUTOFF = datetime(2024, 3, 2, tzinfo=UTC)
MANIFEST = content_hash({"manifest": "microstructure-features"})


def bar(
    minute: int,
    close: str,
    *,
    high: str | None = None,
    low: str | None = None,
    open_: str | None = None,
    volume: str = "10",
    quote_volume: str | None = None,
    taker: str = "4",
    delay: int = 0,
    revision: str | None = None,
    symbol: str = "BTC-USDT",
) -> FeatureObservation:
    """A 1m bar with every field the three microstructure providers can read."""
    start = T0 + minute * MINUTE
    close_d = Decimal(close)
    volume_d = Decimal(volume)
    values: dict[str, Decimal | int | bool | str] = {
        "symbol": symbol,
        "open": Decimal(open_) if open_ is not None else close_d - Decimal("0.5"),
        "high": Decimal(high) if high is not None else close_d + 2,
        "low": Decimal(low) if low is not None else close_d - 2,
        "close": close_d,
        "volume": volume_d,
        "quote_volume": Decimal(quote_volume) if quote_volume is not None else volume_d * close_d,
        "taker_buy_base_volume": Decimal(taker),
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
    for key in ("open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base_volume"):
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


class TestTakerFlowContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        spec = TakerFlowImbalanceProvider.spec(3, available_lag=LAG)
        return _subject(TakerFlowImbalanceProvider, spec)


class TestAmihudContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        spec = AmihudIlliquidityProvider.spec(2, available_lag=LAG)
        return _subject(AmihudIlliquidityProvider, spec)


class TestCorwinSchultzContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        spec = CorwinSchultzSpreadProvider.spec(2, available_lag=LAG)
        return _subject(CorwinSchultzSpreadProvider, spec)


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


def test_taker_flow_is_volume_weighted_buy_share_rescaled_to_minus_one_one() -> None:
    bars = (
        bar(0, "100", volume="10", taker="6"),
        bar(1, "101", volume="20", taker="5"),
        bar(2, "102", volume="30", taker="9"),
    )
    spec = TakerFlowImbalanceProvider.spec(3)
    [value] = TakerFlowImbalanceProvider((spec,)).compute(_request(spec, bars, LATE)).values
    total_volume, total_taker = 60, 20
    expected = 2 * total_taker / total_volume - 1
    assert value.value is not None and value.inputs_used == 3
    assert math.isclose(float(value.value), expected, rel_tol=1e-12)
    assert isinstance(value.value, Decimal)
    assert value.value == value.value.quantize(Decimal("1e-18"))
    assert Decimal("-1") <= value.value <= Decimal("1")


def test_taker_flow_zero_volume_window_is_none_not_arbitrary() -> None:
    bars = (bar(0, "100", volume="0", taker="0"), bar(1, "101", volume="0", taker="0"))
    spec = TakerFlowImbalanceProvider.spec(2)
    [value] = TakerFlowImbalanceProvider((spec,)).compute(_request(spec, bars, LATE)).values
    assert (value.value, value.inputs_used) == (None, 0)


def test_amihud_is_the_mean_of_absolute_return_over_quote_volume() -> None:
    bars = (
        bar(0, "100", quote_volume="1000"),
        bar(1, "102", quote_volume="1500"),
        bar(2, "101", quote_volume="900"),
    )
    spec = AmihudIlliquidityProvider.spec(2)
    [value] = AmihudIlliquidityProvider((spec,)).compute(_request(spec, bars, LATE)).values
    r1, r2 = math.log(102 / 100), math.log(101 / 102)
    expected = (abs(r1) / 1500 + abs(r2) / 900) / 2
    assert value.value is not None and value.inputs_used == 3
    assert math.isclose(float(value.value), expected, rel_tol=1e-12)
    assert value.value >= 0


def test_amihud_zero_quote_volume_in_window_is_none_not_skipped() -> None:
    bars = (
        bar(0, "100", quote_volume="1000"),
        bar(1, "102", quote_volume="0"),
        bar(2, "101", quote_volume="900"),
    )
    spec = AmihudIlliquidityProvider.spec(2)
    [value] = AmihudIlliquidityProvider((spec,)).compute(_request(spec, bars, LATE)).values
    assert (value.value, value.inputs_used) == (None, 0)


def test_corwin_schultz_is_the_mean_of_truncated_two_bar_estimates() -> None:
    bars = (
        bar(0, "100", high="104", low="97"),
        bar(1, "101", high="106", low="98"),
        bar(2, "103", high="108", low="100"),
    )
    spec = CorwinSchultzSpreadProvider.spec(2)
    [value] = CorwinSchultzSpreadProvider((spec,)).compute(_request(spec, bars, LATE)).values

    sqrt2 = math.sqrt(2)
    denom = 3 - 2 * sqrt2

    def estimate(hi_a: float, lo_a: float, hi_b: float, lo_b: float) -> float:
        beta = math.log(hi_b / lo_b) ** 2 + math.log(hi_a / lo_a) ** 2
        gamma = math.log(max(hi_a, hi_b) / min(lo_a, lo_b)) ** 2
        alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / denom - math.sqrt(gamma / denom)
        e_alpha = math.exp(alpha)
        spread = 2 * (e_alpha - 1) / (1 + e_alpha)
        return max(spread, 0.0)

    expected = (estimate(104, 97, 106, 98) + estimate(106, 98, 108, 100)) / 2
    assert value.value is not None and value.inputs_used == 3
    assert math.isclose(float(value.value), expected, rel_tol=1e-9)
    assert value.value >= 0


def test_corwin_schultz_never_negative_even_when_the_raw_estimate_is() -> None:
    # A near-zero-range bar next to a wide one drives alpha very negative for that pair; the
    # truncation to >= 0 (ADR-0085) must still hold for the trailing mean.
    bars = (
        bar(0, "100", high="100.01", low="99.99"),
        bar(1, "100", high="130", low="70"),
        bar(2, "100", high="100.02", low="99.98"),
    )
    spec = CorwinSchultzSpreadProvider.spec(2)
    [value] = CorwinSchultzSpreadProvider((spec,)).compute(_request(spec, bars, LATE)).values
    assert value.value is not None and value.value >= 0


# ======================================================================================
# insufficient history / gaps => None, never filled
# ======================================================================================


def test_too_little_history_is_none() -> None:
    spec = AmihudIlliquidityProvider.spec(5)
    bars = (bar(0, "100"), bar(1, "101"))
    values = AmihudIlliquidityProvider((spec,)).compute(_request(spec, bars, LATE)).values
    assert [item.value for item in values] == [None]


def test_a_gap_inside_the_window_is_none_never_filled() -> None:
    bars = (bar(0, "100"), bar(1, "101"), bar(3, "103"))  # minute 2 missing
    for provider, spec in (
        (TakerFlowImbalanceProvider, TakerFlowImbalanceProvider.spec(2)),
        (AmihudIlliquidityProvider, AmihudIlliquidityProvider.spec(2)),
        (CorwinSchultzSpreadProvider, CorwinSchultzSpreadProvider.spec(2)),
    ):
        [value] = provider((spec,)).compute(_request(spec, bars, LATE)).values
        assert (value.value, value.inputs_used) == (None, 0), spec.name


# ======================================================================================
# malformed bars fail closed, never clamped
# ======================================================================================


def test_taker_above_volume_fails_closed() -> None:
    spec = TakerFlowImbalanceProvider.spec(1)
    bad = (bar(0, "100", volume="5", taker="6"),)
    with pytest.raises(FeatureInputError, match=r"outside \[0, volume\]"):
        TakerFlowImbalanceProvider((spec,)).compute(_request(spec, bad, LATE))


def test_negative_volume_fails_closed() -> None:
    spec = TakerFlowImbalanceProvider.spec(1)
    bad = (bar(0, "100", volume="-1", taker="0"),)
    with pytest.raises(FeatureInputError, match="negative volume"):
        TakerFlowImbalanceProvider((spec,)).compute(_request(spec, bad, LATE))


def test_negative_quote_volume_fails_closed() -> None:
    spec = AmihudIlliquidityProvider.spec(1)
    bad = (bar(0, "100", quote_volume="-1"), bar(1, "101"))
    with pytest.raises(FeatureInputError, match="negative quote_volume"):
        AmihudIlliquidityProvider((spec,)).compute(_request(spec, bad, LATE))


def test_non_positive_close_fails_closed() -> None:
    spec = AmihudIlliquidityProvider.spec(1)
    bad = (bar(0, "0"), bar(1, "101"))
    with pytest.raises(FeatureInputError, match="non-positive close"):
        AmihudIlliquidityProvider((spec,)).compute(_request(spec, bad, LATE))


def test_non_positive_or_inverted_high_low_fails_closed() -> None:
    spec = CorwinSchultzSpreadProvider.spec(1)
    for bad, match in (
        ((bar(0, "100", high="105", low="0"), bar(1, "101")), "non-positive high/low"),
        ((bar(0, "100", high="90", low="95"), bar(1, "101")), "high < low"),
    ):
        with pytest.raises(FeatureInputError, match=match):
            CorwinSchultzSpreadProvider((spec,)).compute(_request(spec, bad, LATE))


def test_mixed_symbols_and_overlapping_bars_fail_closed() -> None:
    spec = TakerFlowImbalanceProvider.spec(1)
    with pytest.raises(FeatureInputError, match="one symbol"):
        bars = (bar(0, "100"), bar(1, "101", symbol="ETH-USDT"))
        TakerFlowImbalanceProvider((spec,)).compute(_request(spec, bars, LATE))
    with pytest.raises(FeatureInputError, match="overlap"):
        dup = bar(0, "101", revision="x").model_copy(update={"observation_key": "dup"})
        bars = (bar(0, "100"), dup)
        TakerFlowImbalanceProvider((spec,)).compute(_request(spec, bars, LATE))


def test_a_point_observation_is_not_a_bar() -> None:
    point = bar(0, "100").model_copy(update={"event_end_time": None})
    spec = TakerFlowImbalanceProvider.spec(1)
    with pytest.raises(FeatureInputError, match="not a bar"):
        TakerFlowImbalanceProvider((spec,)).compute(_request(spec, (point,), LATE))


# ======================================================================================
# parameters live in the spec; no library defaults for window
# ======================================================================================


def test_window_and_scale_are_spec_parameters_bound_by_the_hash() -> None:
    two, three = TakerFlowImbalanceProvider.spec(2), TakerFlowImbalanceProvider.spec(3)
    assert (two.name, dict(two.params)) == ("taker_flow_2", {"window": 2, "scale": 18})
    assert two.content_hash() != three.content_hash()
    provider = TakerFlowImbalanceProvider((two, three))
    assert provider.descriptor.supported_features == {
        str(two.ref): two.content_hash(),
        str(three.ref): three.content_hash(),
    }


def test_a_spec_whose_params_disagree_with_its_definition_is_refused() -> None:
    honest = AmihudIlliquidityProvider.spec(3)
    forged = honest.model_copy(update={"params": {"window": 4, "scale": 18}})
    with pytest.raises(ValueError, match="not a amihud_illiq spec"):
        AmihudIlliquidityProvider((forged,))
    with pytest.raises(ValueError, match="not a amihud_illiq spec"):
        AmihudIlliquidityProvider((TakerFlowImbalanceProvider.spec(3),))
    with pytest.raises(ValueError, match="positive int"):
        AmihudIlliquidityProvider.spec(0)


def test_another_providers_spec_is_unsupported() -> None:
    spec = TakerFlowImbalanceProvider.spec(2)
    other = AmihudIlliquidityProvider.spec(2)
    with pytest.raises(UnsupportedFeature):
        TakerFlowImbalanceProvider((spec,)).compute(_request(other, SUITE_BARS, LATE))


def test_providers_are_statically_substitutable() -> None:
    providers: list[FeatureProvider] = [
        TakerFlowImbalanceProvider((TakerFlowImbalanceProvider.spec(2),)),
        AmihudIlliquidityProvider((AmihudIlliquidityProvider.spec(2),)),
        CorwinSchultzSpreadProvider((CorwinSchultzSpreadProvider.spec(2),)),
    ]
    assert all(provider.descriptor.deterministic for provider in providers)

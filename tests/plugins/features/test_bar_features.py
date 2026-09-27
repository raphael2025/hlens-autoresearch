"""First FeatureProviders (Phase 1 F4; ADR-0030): contract suite, hand-checked values, params."""

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
from core.domain.base import FrozenMapping, Kind, Ref, content_hash
from core.domain.specs import FeatureSpec
from plugins.features import (
    BAR_1M_INPUT,
    BarLogReturnProvider,
    BarRealizedVolatilityProvider,
    BarVolumeSumProvider,
)
from tests.contract_suites.feature import FeatureProviderContract, FeatureSubject

T0 = datetime(2024, 3, 1, 12, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
LAG = timedelta(minutes=1)
CUTOFF = datetime(2024, 3, 2, tzinfo=UTC)
MANIFEST = content_hash({"manifest": "bar-features"})


def bar(
    minute: int,
    close: str,
    *,
    volume: str = "10",
    delay: int = 0,
    revision: str | None = None,
    symbol: str = "BTC-USDT",
) -> FeatureObservation:
    """The 1m bar starting at ``T0 + minute``, available ``delay`` minutes after it closes."""
    start = T0 + minute * MINUTE
    values: dict[str, Decimal | int | bool | str] = {
        "symbol": symbol,
        "open": Decimal(close) - 1,
        "high": Decimal(close) + 1,
        "low": Decimal(close) - 2,
        "close": Decimal(close),
        "volume": Decimal(volume),
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
    bar(0, "100", volume="1"),
    bar(1, "101", volume="2"),
    bar(2, "103", volume="3", delay=2),
    bar(3, "102", volume="4"),
    bar(3, "102.5", volume="4.5", delay=5, revision="crev-3b"),
    bar(4, "104", volume="5"),
    bar(5, "103", volume="6", delay=1),
    bar(7, "105", volume="7"),
    bar(8, "106", volume="8", delay=3),
    bar(9, "107", volume="9"),
)
SUITE_TIMES = tuple(T0 + minute * MINUTE for minute in range(0, 17))


def _perturb(item: FeatureObservation) -> FeatureObservation:
    values = dict(item.values)
    values["close"] = values["close"] * 2 + 1  # type: ignore[operator]
    values["volume"] = values["volume"] * 2 + 1  # type: ignore[operator]
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


class TestBarLogReturnContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        return _subject(BarLogReturnProvider, BarLogReturnProvider.spec(available_lag=LAG))


class TestBarRealizedVolatilityContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        return _subject(
            BarRealizedVolatilityProvider, BarRealizedVolatilityProvider.spec(2, available_lag=LAG)
        )


class TestBarVolumeSumContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        return _subject(BarVolumeSumProvider, BarVolumeSumProvider.spec(3, available_lag=LAG))


# ======================================================================================
# values, checked by hand
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
STRAIGHT = tuple(bar(i, close) for i, close in enumerate(("100", "101", "102.01", "103")))


def test_log_return_is_the_last_contiguous_pair() -> None:
    spec = BarLogReturnProvider.spec()
    [value] = BarLogReturnProvider((spec,)).compute(_request(spec, STRAIGHT, LATE)).values
    assert value.value == Decimal("0.009658140535208237")  # ln(103 / 102.01), 18 places
    assert value.value is not None
    assert math.isclose(float(value.value), math.log(103 / 102.01), rel_tol=1e-12)
    assert value.inputs_used == 2
    assert value.latest_input_available_time == STRAIGHT[-1].available_time


def test_realized_volatility_is_the_root_sum_of_squared_log_returns() -> None:
    spec = BarRealizedVolatilityProvider.spec(3)
    [value] = BarRealizedVolatilityProvider((spec,)).compute(_request(spec, STRAIGHT, LATE)).values
    returns = [math.log(b / a) for a, b in ((100, 101), (101, 102.01), (102.01, 103))]
    assert value.value is not None and value.inputs_used == 4
    assert math.isclose(float(value.value), math.sqrt(sum(r * r for r in returns)), rel_tol=1e-12)
    assert isinstance(value.value, Decimal)
    assert value.value == value.value.quantize(Decimal("1e-18"))  # fixed scale, no float noise


def test_volume_sum_is_exact() -> None:
    bars = tuple(
        bar(i, "100", volume=v) for i, v in enumerate(("0.1", "0.2", "0.000000000000000001"))
    )
    spec = BarVolumeSumProvider.spec(3)
    [value] = BarVolumeSumProvider((spec,)).compute(_request(spec, bars, LATE)).values
    assert value.value == Decimal("0.300000000000000001")
    assert value.inputs_used == 3


def test_a_gap_inside_the_window_is_none_never_filled() -> None:
    bars = (bar(0, "100"), bar(1, "101"), bar(3, "103"))  # minute 2 missing
    for provider, spec in (
        (BarLogReturnProvider, BarLogReturnProvider.spec()),
        (BarRealizedVolatilityProvider, BarRealizedVolatilityProvider.spec(2)),
        (BarVolumeSumProvider, BarVolumeSumProvider.spec(2)),
    ):
        [value] = provider((spec,)).compute(_request(spec, bars, LATE)).values
        assert (value.value, value.inputs_used) == (None, 0), spec.name


def test_too_little_history_is_none() -> None:
    spec = BarRealizedVolatilityProvider.spec(5)
    values = BarRealizedVolatilityProvider((spec,)).compute(_request(spec, STRAIGHT, LATE)).values
    assert [item.value for item in values] == [None]


def test_the_lag_is_the_spec_parameter() -> None:
    """The same bars answer differently under a different declared lag (a different spec)."""
    at = STRAIGHT[-1].available_time
    now = BarVolumeSumProvider.spec(2)
    lagged = BarVolumeSumProvider.spec(2, available_lag=MINUTE, version="1.1.0")
    provider = BarVolumeSumProvider((now, lagged))
    [immediate] = provider.compute(_request(now, STRAIGHT, at)).values
    [delayed] = provider.compute(_request(lagged, STRAIGHT, at)).values
    assert immediate.latest_input_available_time == at
    assert delayed.latest_input_available_time == STRAIGHT[-2].available_time


# ======================================================================================
# parameters live in the spec
# ======================================================================================


def test_window_and_scale_are_spec_parameters_bound_by_the_hash() -> None:
    three, five = BarVolumeSumProvider.spec(3), BarVolumeSumProvider.spec(5)
    assert (three.name, dict(three.params)) == ("bar_volume_sum_3", {"window": 3})
    assert three.content_hash() != five.content_hash()
    rv = BarRealizedVolatilityProvider.spec(30, scale=12)
    assert dict(rv.params) == {"window": 30, "scale": 12}
    provider = BarVolumeSumProvider((three, five))
    assert provider.descriptor.supported_features == {
        str(three.ref): three.content_hash(),
        str(five.ref): five.content_hash(),
    }


def test_a_spec_whose_params_disagree_with_its_definition_is_refused() -> None:
    honest = BarVolumeSumProvider.spec(3)
    forged = honest.model_copy(update={"params": {"window": 4}})  # name still says 3
    with pytest.raises(ValueError, match="not a bar_volume_sum spec"):
        BarVolumeSumProvider((forged,))
    with pytest.raises(ValueError, match="not a bar_volume_sum spec"):
        BarVolumeSumProvider((BarLogReturnProvider.spec(),))
    with pytest.raises(ValueError, match="positive int"):
        BarVolumeSumProvider.spec(0)


def test_another_providers_spec_is_unsupported() -> None:
    spec = BarLogReturnProvider.spec()
    other = BarVolumeSumProvider.spec(2)
    with pytest.raises(UnsupportedFeature):
        BarLogReturnProvider((spec,)).compute(_request(other, STRAIGHT, LATE))


# ======================================================================================
# inputs that are not bars fail closed
# ======================================================================================


@pytest.mark.parametrize(
    ("bars", "match"),
    [
        ((bar(0, "100"), bar(1, "101", symbol="ETH-USDT")), "one symbol"),
        (
            (
                bar(0, "100"),
                bar(0, "101", revision="x").model_copy(update={"observation_key": "dup"}),
            ),
            "overlap",
        ),
        ((bar(0, "0"), bar(1, "1")), "non-positive close"),
    ],
)
def test_malformed_bars_fail_closed(bars: tuple[FeatureObservation, ...], match: str) -> None:
    spec = BarLogReturnProvider.spec()
    with pytest.raises(FeatureInputError, match=match):
        BarLogReturnProvider((spec,)).compute(_request(spec, bars, LATE))


def test_a_point_observation_is_not_a_bar() -> None:
    point = bar(0, "100").model_copy(update={"event_end_time": None})
    spec = BarVolumeSumProvider.spec(1)
    with pytest.raises(FeatureInputError, match="not a bar"):
        BarVolumeSumProvider((spec,)).compute(_request(spec, (point,), LATE))


def test_providers_are_statically_substitutable() -> None:
    providers: list[FeatureProvider] = [
        BarLogReturnProvider((BarLogReturnProvider.spec(),)),
        BarRealizedVolatilityProvider((BarRealizedVolatilityProvider.spec(2),)),
        BarVolumeSumProvider((BarVolumeSumProvider.spec(2),)),
    ]
    assert all(provider.descriptor.deterministic for provider in providers)
    assert BAR_1M_INPUT == Ref(kind=Kind.REPRESENTATION, name="canonical_bar_1m", version="1.0.0")

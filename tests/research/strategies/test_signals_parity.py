"""The research signal helper equals the Phase 1 feature providers (ADR-0030 / ADR-0038).

``research.strategies.signals.bar_signals`` computes ``bar_log_return@1.0.0`` and
``bar_realized_vol_<n>@1.0.0`` without the data plane. Under the same identity its values must be
the providers' values digit for digit. The crafted series below differs in the last place when the
helper squares log returns already quantized to 18 places (the defect fixed with this test).
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.contracts.feature import FeatureObservation, FeatureProvider, FeatureRequest
from core.contracts.strategy import PriceBar
from core.contracts.universe import SelectedRevisionLineage
from core.domain.base import FrozenMapping, content_hash
from core.domain.specs import FeatureSpec
from plugins.features import BarLogReturnProvider, BarRealizedVolatilityProvider
from research.strategies.signals import LOG_RETURN_SIGNAL, bar_signals, realized_vol_signal

T0 = datetime(2024, 3, 1, 12, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
CUTOFF = datetime(2024, 3, 2, tzinfo=UTC)
MANIFEST = content_hash({"manifest": "signals-parity"})
SYMBOL = "BTC-USDT"

#: Found by search: the old helper gives ...311 at the last bar, the provider ...312.
CRAFTED = ("864.05", "507.56", "285.19", "133.02")

#: ``(minute, close)`` rows of one instrument, in time order.
Series = list[tuple[int, Decimal]]


def _price_bar(minute: int, close: Decimal, delay: timedelta) -> PriceBar:
    start = T0 + minute * MINUTE
    return PriceBar(
        instrument=SYMBOL,
        interval_start=start,
        interval_end=start + MINUTE,
        available_time=start + MINUTE + delay,
        open=close,
        high=close,
        low=close,
        close=close,
    )


def _observation(minute: int, close: Decimal, delay: timedelta) -> FeatureObservation:
    start = T0 + minute * MINUTE
    values: dict[str, Decimal | int | bool | str] = {
        "symbol": SYMBOL,
        "close": close,
        "volume": Decimal(1),
        "trade_count": 1,
    }
    return FeatureObservation(
        observation_key=f"bar:{SYMBOL}:{minute}",
        event_time=start,
        event_end_time=start + MINUTE,
        available_time=start + MINUTE + delay,
        knowledge_time=start + MINUTE + delay,
        values=FrozenMapping(values),
        lineage=SelectedRevisionLineage(
            canonical_table="canonical.bars_1m",
            canonical_revision_id=f"crev-{minute}",
            raw_table="raw.binance_spot_klines_1m",
            raw_revision_id=f"raw-{minute}",
            source_table="raw.binance_spot_archives",
            source_revision_id="archive-1",
        ),
    )


def _provider_values(
    provider: FeatureProvider, spec: FeatureSpec, series: Series, delay: timedelta
) -> list[Decimal | None]:
    """The provider's value at each bar's availability time (the helper's observation time)."""
    observations = tuple(_observation(minute, close, delay) for minute, close in series)
    times = tuple(T0 + (minute + 1) * MINUTE + delay for minute, _ in series)
    request = FeatureRequest(
        feature=spec.ref,
        spec_hash=spec.content_hash(),
        manifest_content_hash=MANIFEST,
        knowledge_cutoff=CUTOFF,
        evaluation_times=times,
        observations=observations,
    )
    values = []
    for item in provider.compute(request).values:
        assert item.value is None or isinstance(item.value, Decimal)
        values.append(item.value)
    return values


def _helper_values(
    series: Series, window: int, delay: timedelta
) -> tuple[list[object], list[object]]:
    bars = [_price_bar(minute, close, delay) for minute, close in series]
    signals = bar_signals(bars, vol_windows=(window,))
    by_ref: dict[object, list[object]] = {LOG_RETURN_SIGNAL: [], realized_vol_signal(window): []}
    for item in sorted(signals, key=lambda obs: obs.event_time):
        by_ref[item.signal].append(item.value)
    return by_ref[LOG_RETURN_SIGNAL], by_ref[realized_vol_signal(window)]


def _assert_parity(series: Series, window: int, delay: timedelta = timedelta(0)) -> None:
    log_returns, vols = _helper_values(series, window, delay)
    log_spec = BarLogReturnProvider.spec()
    vol_spec = BarRealizedVolatilityProvider.spec(window)
    log_provider = BarLogReturnProvider((log_spec,))
    vol_provider = BarRealizedVolatilityProvider((vol_spec,))
    # The helper has no log-return observation for the first bar (nothing completes one).
    assert log_returns == _provider_values(log_provider, log_spec, series, delay)[1:]
    assert vols == _provider_values(vol_provider, vol_spec, series, delay)


def _walk(seed: int, minutes: list[int]) -> Series:
    rng = random.Random(seed)
    close = Decimal("100")
    series = []
    for minute in minutes:
        series.append((minute, close))
        step = Decimal(rng.randint(-300, 300)) / Decimal(10_000)
        close = (close * (1 + step)).quantize(Decimal("1e-8"))
    return series


def test_realized_volatility_squares_unquantized_log_returns() -> None:
    series = [(minute, Decimal(close)) for minute, close in enumerate(CRAFTED)]
    _, vols = _helper_values(series, 3, timedelta(0))
    assert vols[-1] == Decimal("1.094070573236092312")
    _assert_parity(series, 3)


@pytest.mark.parametrize("window", [1, 2, 5])
def test_helper_matches_the_providers_on_a_random_walk(window: int) -> None:
    _assert_parity(_walk(20260927 + window, list(range(41))), window)


@pytest.mark.parametrize("window", [2, 3])
def test_helper_matches_the_providers_across_a_gap_with_delayed_availability(window: int) -> None:
    # Minutes 12 and 25..26 are missing: the run breaks and no value is filled on either side.
    minutes = [m for m in range(40) if m not in {12, 25, 26}]
    series = _walk(7 + window, minutes)
    _, vols = _helper_values(series, window, timedelta(minutes=2))
    assert None in vols[13:16]  # just after the gap nothing is computable yet
    _assert_parity(series, window, delay=timedelta(minutes=2))

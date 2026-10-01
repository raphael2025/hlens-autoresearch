"""Classic technical indicator FeatureProviders (ADR-0085 batch; ADR-0030 contract).

Contract suite, hand-checked values, insufficient-history / division-by-zero behavior, and
parameter validation for ``plugins/features/indicators.py``: ``AtrProvider`` (IND-ATR-001),
``RsiProvider`` (IND-RSI-001), ``MacdLineProvider`` / ``MacdSignalProvider`` (IND-MACD-001),
``BbandsPercentBProvider`` / ``BbandsBandwidthProvider`` (IND-BBANDS-001), ``VwapProvider``
(IND-VWAP-001), ``AdxProvider`` (FEA-TREND-STRENGTH-001).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from core.contracts.feature import (
    FeatureInputError,
    FeatureObservation,
    FeatureProvider,
    FeatureRequest,
)
from core.contracts.universe import SelectedRevisionLineage
from core.domain.base import FrozenMapping, content_hash
from core.domain.specs import FeatureSpec
from plugins.features import (
    AdxProvider,
    AtrProvider,
    BarCloseProvider,
    BarHighProvider,
    BarLowProvider,
    BbandsBandwidthProvider,
    BbandsPercentBProvider,
    MacdLineProvider,
    MacdSignalProvider,
    RsiProvider,
    VwapProvider,
)
from tests.contract_suites.feature import FeatureProviderContract, FeatureSubject

T0 = datetime(2024, 3, 1, 12, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
LAG = timedelta(minutes=1)
CUTOFF = datetime(2024, 3, 2, tzinfo=UTC)
MANIFEST = content_hash({"manifest": "indicator-features"})


def bar(
    minute: int,
    close: str,
    *,
    high: str | None = None,
    low: str | None = None,
    open_: str | None = None,
    volume: str = "10",
    delay: int = 0,
    revision: str | None = None,
    symbol: str = "BTC-USDT",
) -> FeatureObservation:
    """The 1m bar starting at ``T0 + minute``, available ``delay`` minutes after it closes.

    Defaults ``high = close + 1`` / ``low = close - 1`` / ``open = close`` when a hand-checked
    test does not care about the exact H/L shape (RSI, MACD, Bollinger, VWAP's volume weighting).
    """
    start = T0 + minute * MINUTE
    c = Decimal(close)
    values: dict[str, Decimal | int | bool | str] = {
        "symbol": symbol,
        "open": Decimal(open_) if open_ is not None else c,
        "high": Decimal(high) if high is not None else c + 1,
        "low": Decimal(low) if low is not None else c - 1,
        "close": c,
        "volume": Decimal(volume),
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


def _request(spec: FeatureSpec, bars: tuple[FeatureObservation, ...], *times: datetime) -> Any:
    return FeatureRequest(
        feature=spec.ref,
        spec_hash=spec.content_hash(),
        manifest_content_hash=MANIFEST,
        knowledge_cutoff=CUTOFF,
        evaluation_times=times,
        observations=bars,
    )


# ======================================================================================
# contract suite: one shared bar fixture (mirrors bars.py's), 8 subjects
# ======================================================================================

#: Minutes 0..9 without minute 6 (a gap), uneven availability, a later replacement of minute 3 —
#: same shape as ``tests/plugins/features/test_bar_features.py``'s fixture, with OHLC added.
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
    # Neither affine nor monotone: RSI, %B and ADX are invariant under ``x -> a*x + b`` price
    # maps, and period-2 %B depends only on the direction of each move, so an order-preserving
    # map would leave the causal-perturbation check without teeth. One offset per bar (by its
    # minute) flips directions while keeping each bar's OHLC ordering and positive prices; every
    # third bar is also widened so that the next bar is inside it, which ADX(1) — 100 whenever
    # exactly one directional move is positive — can see.
    values = dict(item.values)
    phase = item.event_time.minute % 3
    offset = Decimal(37) * phase
    for name in ("open", "high", "low", "close"):
        values[name] = values[name] * 2 + 1 + offset  # type: ignore[operator]
    if phase == 1:
        values["high"] = values["high"] * 10
        values["low"] = Decimal(1)
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


class TestAtrContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        return _subject(AtrProvider, AtrProvider.spec(2, scale=6, available_lag=LAG))


class TestRsiContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        return _subject(RsiProvider, RsiProvider.spec(2, scale=6, available_lag=LAG))


class TestMacdLineContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        return _subject(
            MacdLineProvider, MacdLineProvider.spec(1, 3, 2, scale=6, available_lag=LAG)
        )


class TestMacdSignalContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        return _subject(
            MacdSignalProvider, MacdSignalProvider.spec(1, 3, 2, scale=6, available_lag=LAG)
        )


class TestBbandsPercentBContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        return _subject(
            BbandsPercentBProvider,
            BbandsPercentBProvider.spec(2, Decimal("2"), scale=6, available_lag=LAG),
        )


class TestBbandsBandwidthContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        return _subject(
            BbandsBandwidthProvider,
            BbandsBandwidthProvider.spec(2, Decimal("2"), scale=6, available_lag=LAG),
        )


class TestVwapContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        return _subject(VwapProvider, VwapProvider.spec(2, scale=6, available_lag=LAG))


class TestAdxContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        return _subject(AdxProvider, AdxProvider.spec(1, scale=6, available_lag=LAG))


class TestBarCloseContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        return _subject(BarCloseProvider, BarCloseProvider.spec(available_lag=LAG))


class TestBarHighContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        return _subject(BarHighProvider, BarHighProvider.spec(available_lag=LAG))


class TestBarLowContract(FeatureProviderContract):
    @pytest.fixture
    def feature_subject(self) -> FeatureSubject:
        return _subject(BarLowProvider, BarLowProvider.spec(available_lag=LAG))


# ======================================================================================
# values, checked by hand
# ======================================================================================

LATE = T0 + 30 * MINUTE


def test_atr_is_the_seeded_wilder_mean_of_true_ranges() -> None:
    """3 bars, period=2: trs = [TR1, TR2], seeded (no further smoothing needed).

    bar0 H=10 L=8 C=9; bar1 H=11 L=9 C=10 -> TR1 = max(2, |11-9|=2, |9-9|=0) = 2;
    bar2 H=13 L=10 C=12 -> TR2 = max(3, |13-10|=3, |10-10|=0) = 3. ATR = (2+3)/2 = 2.5.
    """
    bars = (
        bar(0, "9", high="10", low="8"),
        bar(1, "10", high="11", low="9"),
        bar(2, "12", high="13", low="10"),
    )
    spec = AtrProvider.spec(2, scale=2)
    [value] = AtrProvider((spec,)).compute(_request(spec, bars, LATE)).values
    assert value.value == Decimal("2.50")
    assert value.inputs_used == 3


def test_rsi_is_the_seeded_wilder_mean_of_gains_and_losses() -> None:
    """3 bars, period=2, closes 100/101/100: avg_gain=avg_loss=0.5 -> RSI = 100-100/2 = 50."""
    bars = (bar(0, "100"), bar(1, "101"), bar(2, "100"))
    spec = RsiProvider.spec(2, scale=2)
    [value] = RsiProvider((spec,)).compute(_request(spec, bars, LATE)).values
    assert value.value == Decimal("50.00")
    assert value.inputs_used == 3


def test_rsi_zero_average_loss_is_100() -> None:
    """A strictly increasing run has avg_loss == 0 -> RSI = 100 (ADR-0085), not a raised error."""
    bars = (bar(0, "100"), bar(1, "101"), bar(2, "102"))
    spec = RsiProvider.spec(2, scale=2)
    [value] = RsiProvider((spec,)).compute(_request(spec, bars, LATE)).values
    assert value.value == Decimal("100.00")


#: 7 contiguous closes, fast=1 (alpha=1, exact), slow=4 (alpha=0.4, exact), signal=4 (alpha=0.4).
#: Chosen so every division terminates in decimal (denominators 1, 4, 5): no 50-digit rounding
#: is needed to check the result by hand.
MACD_CLOSES = ("100", "104", "108", "100", "113", "97", "105")
MACD_BARS = tuple(bar(i, c) for i, c in enumerate(MACD_CLOSES))


def test_macd_line_is_ema_fast_minus_ema_slow_seeded_by_the_same_sma() -> None:
    """seed = mean(100,104,108,100) = 103 (both EMAs). Then, bar by bar:

    c4=113: ema_fast=113, ema_slow=113*0.4+103*0.6=107.0  -> macd=6.0
    c5=97:  ema_fast=97,  ema_slow=97*0.4+107*0.6=103.0    -> macd=-6.0
    c6=105: ema_fast=105, ema_slow=105*0.4+103*0.6=103.8   -> macd=1.2
    """
    spec = MacdLineProvider.spec(1, 4, 4, scale=1)
    [value] = MacdLineProvider((spec,)).compute(_request(spec, MACD_BARS, LATE)).values
    assert value.value == Decimal("1.2")
    assert value.inputs_used == 7


def test_macd_signal_is_the_sma_seed_when_exactly_signal_points_exist() -> None:
    """With run length == slow + signal - 1 (7 bars), the MACD series has exactly `signal` (4)
    points [0, 6.0, -6.0, 1.2]; the signal line is their plain mean, 1.2/4 = 0.3.
    """
    spec = MacdSignalProvider.spec(1, 4, 4, scale=1)
    [value] = MacdSignalProvider((spec,)).compute(_request(spec, MACD_BARS, LATE)).values
    assert value.value == Decimal("0.3")
    assert value.inputs_used == 7


def test_bbands_percent_b_and_bandwidth() -> None:
    """closes 96, 104, window=2, k=1: mean=100, population std=4 (variance=16, sqrt exact).

    lower=96, upper=104, band_range=8. %b=(104-96)/8=1. bandwidth=8/100=0.08.
    """
    bars = (bar(0, "96"), bar(1, "104"))
    pb_spec = BbandsPercentBProvider.spec(2, Decimal("1"), scale=2)
    bw_spec = BbandsBandwidthProvider.spec(2, Decimal("1"), scale=2)
    [pb] = BbandsPercentBProvider((pb_spec,)).compute(_request(pb_spec, bars, LATE)).values
    [bw] = BbandsBandwidthProvider((bw_spec,)).compute(_request(bw_spec, bars, LATE)).values
    assert pb.value == Decimal("1.00")
    assert bw.value == Decimal("0.08")


def test_vwap_is_the_volume_weighted_mean_of_typical_price() -> None:
    """bar0 typical=(10+8+9)/3=9 vol=2; bar1 typical=(13+11+12)/3=12 vol=1.

    VWAP = (9*2 + 12*1) / (2+1) = 30/3 = 10.
    """
    bars = (
        bar(0, "9", high="10", low="8", volume="2"),
        bar(1, "12", high="13", low="11", volume="1"),
    )
    spec = VwapProvider.spec(2, scale=2)
    [value] = VwapProvider((spec,)).compute(_request(spec, bars, LATE)).values
    assert value.value == Decimal("10.00")


def test_bar_close_high_low_are_the_latest_visible_bars_own_values() -> None:
    """No smoothing, no window: the value is exactly the latest visible bar's own field."""
    bars = (
        bar(0, "100", high="101", low="99"),
        bar(1, "103", high="105", low="102"),
    )
    close_spec = BarCloseProvider.spec()
    high_spec = BarHighProvider.spec()
    low_spec = BarLowProvider.spec()
    [close] = BarCloseProvider((close_spec,)).compute(_request(close_spec, bars, LATE)).values
    [high] = BarHighProvider((high_spec,)).compute(_request(high_spec, bars, LATE)).values
    [low] = BarLowProvider((low_spec,)).compute(_request(low_spec, bars, LATE)).values
    assert (close.value, high.value, low.value) == (Decimal("103"), Decimal("105"), Decimal("102"))
    assert (close.inputs_used, high.inputs_used, low.inputs_used) == (1, 1, 1)


def test_bar_close_high_low_have_no_parameters() -> None:
    for spec in (BarCloseProvider.spec(), BarHighProvider.spec(), BarLowProvider.spec()):
        assert dict(spec.params) == {}


def test_adx_period_1_is_the_directional_index_of_the_latest_bar_pair() -> None:
    """period=1 has no memory (smoothed_x - smoothed_x/1 + x == x): ADX collapses to the plain
    DX of the single latest bar pair.

    bar0 H=10 L=8 C=9; bar1 H=13 L=7 C=10. up_move=3, down_move=1 -> +DM=3, -DM=0.
    TR = max(6, |13-9|=4, |7-9|=2) = 6. +DI=100*3/6=50, -DI=0. DX = 100*50/50 = 100.
    """
    bars = (
        bar(0, "9", high="10", low="8"),
        bar(1, "10", high="13", low="7"),
    )
    spec = AdxProvider.spec(1, scale=2)
    [value] = AdxProvider((spec,)).compute(_request(spec, bars, LATE)).values
    assert value.value == Decimal("100.00")


# ======================================================================================
# insufficient history is None, never filled
# ======================================================================================


def test_atr_with_too_few_bars_is_none() -> None:
    bars = (bar(0, "100"), bar(1, "101"))  # period=2 needs 3 bars
    spec = AtrProvider.spec(2, scale=2)
    [value] = AtrProvider((spec,)).compute(_request(spec, bars, LATE)).values
    assert (value.value, value.inputs_used) == (None, 0)


def test_rsi_with_too_few_bars_is_none() -> None:
    bars = (bar(0, "100"), bar(1, "101"))  # period=2 needs 3 bars
    spec = RsiProvider.spec(2, scale=2)
    [value] = RsiProvider((spec,)).compute(_request(spec, bars, LATE)).values
    assert (value.value, value.inputs_used) == (None, 0)


def test_macd_line_with_too_few_bars_is_none() -> None:
    bars = tuple(bar(i, c) for i, c in enumerate(("100", "101")))  # slow=4 needs 4 bars
    spec = MacdLineProvider.spec(1, 4, 4, scale=1)
    [value] = MacdLineProvider((spec,)).compute(_request(spec, bars, LATE)).values
    assert (value.value, value.inputs_used) == (None, 0)


def test_macd_signal_needs_slow_plus_signal_minus_one_bars() -> None:
    """6 bars gives a real MACD line (needs >= slow=4) but not yet a signal (needs >= 7)."""
    bars = tuple(bar(i, c) for i, c in enumerate(("100", "104", "108", "100", "113", "97")))
    line_spec = MacdLineProvider.spec(1, 4, 4, scale=1)
    signal_spec = MacdSignalProvider.spec(1, 4, 4, scale=1)
    [line] = MacdLineProvider((line_spec,)).compute(_request(line_spec, bars, LATE)).values
    [signal] = MacdSignalProvider((signal_spec,)).compute(_request(signal_spec, bars, LATE)).values
    assert line.value is not None
    assert (signal.value, signal.inputs_used) == (None, 0)


def test_bbands_with_too_few_bars_is_none() -> None:
    bars = (bar(0, "100"),)  # window=2 needs 2 bars
    pb_spec = BbandsPercentBProvider.spec(2, Decimal("1"), scale=2)
    bw_spec = BbandsBandwidthProvider.spec(2, Decimal("1"), scale=2)
    [pb] = BbandsPercentBProvider((pb_spec,)).compute(_request(pb_spec, bars, LATE)).values
    [bw] = BbandsBandwidthProvider((bw_spec,)).compute(_request(bw_spec, bars, LATE)).values
    assert (pb.value, bw.value) == (None, None)


def test_vwap_with_too_few_bars_is_none() -> None:
    bars = (bar(0, "100"),)  # window=2 needs 2 bars
    spec = VwapProvider.spec(2, scale=2)
    [value] = VwapProvider((spec,)).compute(_request(spec, bars, LATE)).values
    assert (value.value, value.inputs_used) == (None, 0)


def test_adx_with_too_few_bars_is_none() -> None:
    bars = (bar(0, "100"),)  # period=1 needs 2 bars
    spec = AdxProvider.spec(1, scale=2)
    [value] = AdxProvider((spec,)).compute(_request(spec, bars, LATE)).values
    assert (value.value, value.inputs_used) == (None, 0)


def test_bar_close_high_low_with_no_visible_bar_is_none() -> None:
    for provider_cls, spec in (
        (BarCloseProvider, BarCloseProvider.spec()),
        (BarHighProvider, BarHighProvider.spec()),
        (BarLowProvider, BarLowProvider.spec()),
    ):
        [value] = provider_cls((spec,)).compute(_request(spec, (), LATE)).values
        assert (value.value, value.inputs_used) == (None, 0), spec.name


def test_a_gap_inside_the_growing_run_resets_it_to_none() -> None:
    """ATR/RSI/MACD/ADX use the maximal *contiguous* trailing run: a gap resets it, it is never
    bridged (ADR-0085 §"通用规则" #3)."""
    bars = (bar(0, "100"), bar(1, "101"), bar(3, "103"))  # minute 2 missing
    for provider_cls, spec in (
        (AtrProvider, AtrProvider.spec(2, scale=2)),
        (RsiProvider, RsiProvider.spec(2, scale=2)),
    ):
        [value] = provider_cls((spec,)).compute(_request(spec, bars, LATE)).values
        assert (value.value, value.inputs_used) == (None, 0), spec.name


# ======================================================================================
# division by zero is missing, never raised and never an arbitrary value
# ======================================================================================


def test_bbands_zero_variance_is_none_for_percent_b_but_defined_for_bandwidth() -> None:
    """Constant closes give std=0: %b divides by zero (band width) -> None; bandwidth divides by
    the (non-zero) mean -> 0, a perfectly defined answer."""
    bars = (bar(0, "100"), bar(1, "100"))
    pb_spec = BbandsPercentBProvider.spec(2, Decimal("2"), scale=2)
    bw_spec = BbandsBandwidthProvider.spec(2, Decimal("2"), scale=2)
    [pb] = BbandsPercentBProvider((pb_spec,)).compute(_request(pb_spec, bars, LATE)).values
    [bw] = BbandsBandwidthProvider((bw_spec,)).compute(_request(bw_spec, bars, LATE)).values
    assert pb.value is None
    assert bw.value == Decimal("0.00")


def test_vwap_zero_volume_is_none() -> None:
    bars = (bar(0, "100", volume="0"), bar(1, "101", volume="0"))
    spec = VwapProvider.spec(2, scale=2)
    [value] = VwapProvider((spec,)).compute(_request(spec, bars, LATE)).values
    assert value.value is None


def test_adx_flat_market_is_none() -> None:
    """H == L == C on every bar: zero true range, zero directional movement -> DX is 0/0."""
    bars = (
        bar(0, "100", high="100", low="100"),
        bar(1, "100", high="100", low="100"),
    )
    spec = AdxProvider.spec(1, scale=2)
    [value] = AdxProvider((spec,)).compute(_request(spec, bars, LATE)).values
    assert value.value is None


# ======================================================================================
# parameters live in the spec; invalid combinations are refused at construction
# ======================================================================================


def test_macd_requires_fast_strictly_less_than_slow() -> None:
    with pytest.raises(ValueError, match="fast must be < slow"):
        MacdLineProvider.spec(3, 3, 2, scale=1)
    with pytest.raises(ValueError, match="fast must be < slow"):
        MacdSignalProvider.spec(4, 3, 2, scale=1)


def test_bbands_requires_a_positive_k() -> None:
    with pytest.raises(ValueError, match="k must be a positive"):
        BbandsPercentBProvider.spec(2, Decimal("0"), scale=2)
    with pytest.raises(ValueError, match="k must be a positive"):
        BbandsBandwidthProvider.spec(2, Decimal("-1"), scale=2)


@pytest.mark.parametrize(
    "build",
    [
        lambda: AtrProvider.spec(0, scale=2),
        lambda: RsiProvider.spec(0, scale=2),
        lambda: VwapProvider.spec(0, scale=2),
        lambda: AdxProvider.spec(0, scale=2),
        lambda: BbandsPercentBProvider.spec(0, Decimal("1"), scale=2),
    ],
)
def test_period_or_window_must_be_a_positive_int(build: Any) -> None:
    with pytest.raises(ValueError, match="positive int"):
        build()


def test_a_spec_whose_params_disagree_with_its_definition_is_refused() -> None:
    honest = AtrProvider.spec(3, scale=2)
    forged = honest.model_copy(update={"params": {"period": 4, "scale": 2}})  # name still says 3
    with pytest.raises(ValueError, match="not a atr spec"):
        AtrProvider((forged,))
    with pytest.raises(ValueError, match="not a atr spec"):
        AtrProvider((RsiProvider.spec(3, scale=2),))


# ======================================================================================
# inputs that are not well-formed bars fail closed (shared `_bars` / `_Bar` validation)
# ======================================================================================


def test_high_below_low_fails_closed() -> None:
    bars = (bar(0, "100", high="99", low="101"),)
    spec = AtrProvider.spec(1, scale=2)
    with pytest.raises(FeatureInputError, match="high < low"):
        AtrProvider((spec,)).compute(_request(spec, bars, LATE))


def test_mixed_symbols_fail_closed() -> None:
    bars = (bar(0, "100"), bar(1, "101", symbol="ETH-USDT"))
    spec = AtrProvider.spec(1, scale=2)
    with pytest.raises(FeatureInputError, match="one symbol"):
        AtrProvider((spec,)).compute(_request(spec, bars, LATE))


def test_overlapping_bars_fail_closed() -> None:
    bars = (
        bar(0, "100"),
        bar(0, "101", revision="x").model_copy(update={"observation_key": "dup"}),
    )
    spec = AtrProvider.spec(1, scale=2)
    with pytest.raises(FeatureInputError, match="overlap"):
        AtrProvider((spec,)).compute(_request(spec, bars, LATE))


def test_a_point_observation_is_not_a_bar() -> None:
    point = bar(0, "100").model_copy(update={"event_end_time": None})
    spec = VwapProvider.spec(1, scale=2)
    with pytest.raises(FeatureInputError, match="not a bar"):
        VwapProvider((spec,)).compute(_request(spec, (point,), LATE))


# ======================================================================================
# providers are statically substitutable FeatureProviders
# ======================================================================================


def test_providers_are_statically_substitutable() -> None:
    providers: list[FeatureProvider] = [
        AtrProvider((AtrProvider.spec(2, scale=2),)),
        RsiProvider((RsiProvider.spec(2, scale=2),)),
        MacdLineProvider((MacdLineProvider.spec(1, 4, 4, scale=1),)),
        MacdSignalProvider((MacdSignalProvider.spec(1, 4, 4, scale=1),)),
        BbandsPercentBProvider((BbandsPercentBProvider.spec(2, Decimal("2"), scale=2),)),
        BbandsBandwidthProvider((BbandsBandwidthProvider.spec(2, Decimal("2"), scale=2),)),
        VwapProvider((VwapProvider.spec(2, scale=2),)),
        AdxProvider((AdxProvider.spec(1, scale=2),)),
        BarCloseProvider((BarCloseProvider.spec(),)),
        BarHighProvider((BarHighProvider.spec(),)),
        BarLowProvider((BarLowProvider.spec(),)),
    ]
    assert all(provider.descriptor.deterministic for provider in providers)
    assert len({provider.descriptor.name for provider in providers}) == 11


def test_bar_price_feature_identities_match_pms_specification() -> None:
    """PM asked for these exact identities (``research/strategies/price_signals.py`` — the module
    said to define ``BAR_CLOSE_SIGNAL`` / ``BAR_HIGH_SIGNAL`` / ``BAR_LOW_SIGNAL`` — does not exist
    in this worktree, so only the identities PM gave directly could be pinned down here)."""
    assert str(BarCloseProvider.spec().ref) == "feature:bar_close@1.0.0"
    assert str(BarHighProvider.spec().ref) == "feature:bar_high@1.0.0"
    assert str(BarLowProvider.spec().ref) == "feature:bar_low@1.0.0"

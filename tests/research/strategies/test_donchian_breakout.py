"""``donchian_breakout@1.0.0`` (ADR-0085 ``STR-TF-DONCHIAN-001``): contract suite, hand-computed
positions, insufficient history, missing values, no look-ahead, explicit parameter points."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from core.contracts.strategy import (
    SignalObservation,
    StrategyInputError,
    StrategyRequest,
    StrategyResult,
    UnsupportedStrategy,
)
from core.domain.base import FrozenMapping
from core.domain.specs import StrategySpec
from research.strategies.donchian_breakout import (
    DONCHIAN_PARAM_SPACE,
    DONCHIAN_SIGNALS,
    DonchianBreakoutProvider,
    donchian_spec,
)
from research.strategies.price_signals import (
    BAR_CLOSE_SIGNAL,
    BAR_HIGH_SIGNAL,
    BAR_LOW_SIGNAL,
    bar_price_signals,
)
from research.strategies.signals import bar_signals
from tests.contract_suites.strategy import StrategyProviderContract, StrategySubject
from tests.strategy_fixtures import MINUTE, T0, make_bars, wave_closes

CUTOFF = T0 + timedelta(days=1)
SPEC = donchian_spec(entry_window=20, exit_window=10)
PAIR = ("BTCUSDT", "ETHUSDT")
WAVE_BARS = make_bars("BTCUSDT", wave_closes(160)) + make_bars(
    "ETHUSDT", wave_closes(160, phase=13)
)
WAVE_SIGNALS = bar_price_signals(WAVE_BARS, DONCHIAN_SIGNALS)
WAVE_DECISIONS = tuple(T0 + minute * MINUTE for minute in range(20, 160, 6))

#: 20 flat bars at 100, then a breakout (101), a pullback that holds (100.5) and a break (99).
HAND_CLOSES = (*(Decimal(100) for _ in range(20)), Decimal(101), Decimal("100.5"), Decimal(99))
HAND_BARS = make_bars("BTCUSDT", HAND_CLOSES)
HAND_SIGNALS = bar_price_signals(HAND_BARS, DONCHIAN_SIGNALS)
HAND_DECISIONS = tuple(T0 + minute * MINUTE for minute in (20, 21, 22, 23))


def _request(
    signals: Sequence[SignalObservation],
    decisions: Sequence[datetime],
    *,
    instruments: tuple[str, ...] = ("BTCUSDT",),
    params: dict[str, int] | None = None,
    spec: StrategySpec = SPEC,
) -> StrategyRequest:
    return StrategyRequest(
        strategy=spec.ref,
        spec_hash=spec.content_hash(),
        params=FrozenMapping(params or {}),
        instruments=instruments,
        knowledge_cutoff=CUTOFF,
        decision_times=tuple(decisions),
        signals=tuple(signals),
    )


def _run(request: StrategyRequest, spec: StrategySpec = SPEC) -> StrategyResult:
    provider = DonchianBreakoutProvider((spec,))
    result = provider.target_positions(request)
    result.check_answers(request, provider.descriptor)
    return result


def _perturb(item: SignalObservation) -> SignalObservation:
    value = item.value if isinstance(item.value, Decimal) else Decimal(100)
    return item.model_copy(update={"value": value + Decimal(7)})


class TestDonchianBreakoutContract(StrategyProviderContract):
    @pytest.fixture
    def strategy_subject(self) -> StrategySubject:
        return StrategySubject(
            open=lambda: DonchianBreakoutProvider((SPEC,)),
            request=_request(WAVE_SIGNALS, WAVE_DECISIONS, instruments=PAIR),
            unsupported=_request(
                WAVE_SIGNALS, WAVE_DECISIONS, instruments=PAIR, params={"entry_window": 21}
            ),
            perturb=_perturb,
        )


def test_hand_computed_breakout_hold_and_exit() -> None:
    result = _run(_request(HAND_SIGNALS, HAND_DECISIONS))
    before, entry, hold, exit_ = result.positions
    # T0+20m: bar 19 has only 19 bars before it (< max(20, 10)): no channel yet.
    assert (before.target_weight, before.inputs_used) == (Decimal(0), 0)
    assert before.latest_input_available_time is None
    # T0+21m: close 101 > max high of bars 0..19 (100) -> long, the whole slice of one instrument.
    assert entry.target_weight == Decimal(1)
    assert entry.inputs_used == 21 * 3
    assert entry.latest_input_available_time == T0 + 21 * MINUTE
    # T0+22m: close 100.5 is not below min low of bars 11..20 (100) -> still long.
    assert hold.target_weight == Decimal(1)
    assert hold.inputs_used == 22 * 3
    # T0+23m: close 99 < min low of bars 12..21 (100) -> flat, computed from 23 bars.
    assert exit_.target_weight == Decimal(0)
    assert exit_.inputs_used == 23 * 3
    assert exit_.latest_input_available_time == T0 + 23 * MINUTE


def test_touching_the_channel_does_not_enter() -> None:
    closes = tuple(Decimal(100) for _ in range(21))
    signals = bar_price_signals(make_bars("BTCUSDT", closes), DONCHIAN_SIGNALS)
    (position,) = _run(_request(signals, (T0 + 21 * MINUTE,))).positions
    assert position.target_weight == Decimal(0)
    assert position.inputs_used == 21 * 3  # computable, just not a breakout


def test_equal_slices_over_the_requested_instruments() -> None:
    eth = bar_price_signals(make_bars("ETHUSDT", HAND_CLOSES[:20] * 2), DONCHIAN_SIGNALS)
    request = _request(HAND_SIGNALS + eth, (T0 + 21 * MINUTE,), instruments=PAIR)
    btc, flat = _run(request).positions
    assert btc.target_weight == Decimal("0.5")
    assert flat.target_weight == Decimal(0) and flat.inputs_used == 21 * 3


def test_insufficient_history_is_flat_without_inputs() -> None:
    short = tuple(item for item in HAND_SIGNALS if item.available_time <= T0 + 20 * MINUTE)
    result = _run(_request(short, (T0 + 20 * MINUTE, T0 + 21 * MINUTE)))
    for position in result.positions:
        assert (position.target_weight, position.inputs_used) == (Decimal(0), 0)


def test_an_explicit_none_resets_to_flat_and_restarts_the_run() -> None:
    breakout_high = next(
        item
        for item in HAND_SIGNALS
        if item.signal == BAR_HIGH_SIGNAL and item.event_time == T0 + 21 * MINUTE
    )
    signals = tuple(
        item.model_copy(update={"value": None}) if item is breakout_high else item
        for item in HAND_SIGNALS
    )
    result = _run(_request(signals, HAND_DECISIONS[1:]))
    for position in result.positions:  # the run restarts at bar 21: no channel for 20 bars
        assert (position.target_weight, position.inputs_used) == (Decimal(0), 0)


def test_a_missing_observation_is_not_filled_in() -> None:
    signals = tuple(
        item
        for item in HAND_SIGNALS
        if not (item.signal == BAR_LOW_SIGNAL and item.event_time == T0 + 21 * MINUTE)
    )
    (position,) = _run(_request(signals, (T0 + 21 * MINUTE,))).positions
    assert (position.target_weight, position.inputs_used) == (Decimal(0), 0)


def test_future_signals_do_not_move_past_positions() -> None:
    cut = HAND_DECISIONS[1]
    base = _run(_request(HAND_SIGNALS, HAND_DECISIONS))
    changed = tuple(_perturb(item) if item.available_time > cut else item for item in HAND_SIGNALS)
    truncated = tuple(item for item in HAND_SIGNALS if item.available_time <= cut)
    for signals in (changed, truncated):
        other = _run(_request(signals, HAND_DECISIONS))
        assert [p for p in base.positions if p.decision_time <= cut] == [
            p for p in other.positions if p.decision_time <= cut
        ]


def test_long_or_flat_only() -> None:
    result = _run(_request(WAVE_SIGNALS, WAVE_DECISIONS, instruments=PAIR))
    weights = {position.target_weight for position in result.positions}
    assert weights <= {Decimal(0), Decimal("0.5")}
    assert Decimal("0.5") in weights, "the wave fixture must break out"


def test_every_parameter_is_explicit_and_declared() -> None:
    assert DONCHIAN_PARAM_SPACE == {"entry_window": (20, 55), "exit_window": (10, 20)}
    with pytest.raises(TypeError):
        donchian_spec(entry_window=20)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="declared point"):
        donchian_spec(entry_window=21, exit_window=10)
    with pytest.raises(ValueError, match="declared point"):
        donchian_spec(entry_window=True, exit_window=10)
    for entry in (20, 55):
        for exit_window in (10, 20):
            spec = donchian_spec(entry_window=entry, exit_window=exit_window)
            assert dict(spec.params) == {"entry_window": entry, "exit_window": exit_window}


def test_the_provider_refuses_points_and_specs_outside_the_space() -> None:
    provider = DonchianBreakoutProvider((SPEC,))
    for params in ({"exit_window": 11}, {"window": 20}, {"entry_window": True}):
        with pytest.raises(UnsupportedStrategy):
            provider.target_positions(_request(HAND_SIGNALS, HAND_DECISIONS, params=dict(params)))
    wider = FrozenMapping({"entry_window": (5, 20, 55), "exit_window": (10, 20)})
    widened = SPEC.model_copy(update={"param_search_space": wider})
    with pytest.raises(ValueError, match="declared space"):
        DonchianBreakoutProvider((widened,))
    with pytest.raises(ValueError):
        DonchianBreakoutProvider(())


def test_foreign_signals_are_refused() -> None:
    foreign = bar_signals(HAND_BARS)
    with pytest.raises(StrategyInputError):
        DonchianBreakoutProvider((SPEC,)).target_positions(
            _request(HAND_SIGNALS + foreign, HAND_DECISIONS)
        )


def test_price_signals_carry_the_bar_values_exactly() -> None:
    bar = HAND_BARS[21]
    values = {
        item.signal: item.value for item in HAND_SIGNALS if item.event_time == bar.interval_end
    }
    assert values == {
        BAR_CLOSE_SIGNAL: Decimal("100.5"),
        BAR_HIGH_SIGNAL: Decimal(101),
        BAR_LOW_SIGNAL: Decimal("100.5"),
    }
    assert all(item.available_time == item.event_time for item in HAND_SIGNALS)

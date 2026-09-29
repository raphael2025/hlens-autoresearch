"""``dual_momentum@1.0.0`` (ADR-0085 ``STR-DUAL-MOM-001``): contract suite, hand-computed
selection, absolute-momentum filter, ties, insufficient history, missing values, no look-ahead,
explicit parameter points."""

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
from research.strategies.dual_momentum import (
    DUAL_MOMENTUM_PARAM_SPACE,
    DualMomentumProvider,
    dual_momentum_spec,
)
from research.strategies.signals import LOG_RETURN_SIGNAL, bar_signals, realized_vol_signal
from tests.contract_suites.strategy import StrategyProviderContract, StrategySubject
from tests.strategy_fixtures import MINUTE, T0, make_bars, wave_closes

CUTOFF = T0 + timedelta(days=1)
SPEC = dual_momentum_spec(lookback=60)
PAIR = ("BTCUSDT", "ETHUSDT")
WAVE_BARS = make_bars("BTCUSDT", wave_closes(160)) + make_bars(
    "ETHUSDT", wave_closes(160, phase=13)
)
WAVE_SIGNALS = tuple(item for item in bar_signals(WAVE_BARS) if item.signal == LOG_RETURN_SIGNAL)
WAVE_DECISIONS = tuple(T0 + minute * MINUTE for minute in range(62, 160, 6))
AT_60 = T0 + 60 * MINUTE


def _returns(
    instrument: str, values: Sequence[Decimal | None], *, first_minute: int = 1
) -> tuple[SignalObservation, ...]:
    """One ``bar_log_return`` per minute, available when it is described."""
    return tuple(
        SignalObservation(
            signal=LOG_RETURN_SIGNAL,
            instrument=instrument,
            event_time=T0 + (first_minute + index) * MINUTE,
            available_time=T0 + (first_minute + index) * MINUTE,
            knowledge_time=T0 + (first_minute + index) * MINUTE,
            value=value,
        )
        for index, value in enumerate(values)
    )


def _flat(value: str, count: int = 60) -> list[Decimal | None]:
    values: list[Decimal | None] = [Decimal(value)] * count
    return values


def _request(
    signals: Sequence[SignalObservation],
    decisions: Sequence[datetime],
    *,
    params: dict[str, int] | None = None,
    spec: StrategySpec = SPEC,
) -> StrategyRequest:
    return StrategyRequest(
        strategy=spec.ref,
        spec_hash=spec.content_hash(),
        params=FrozenMapping(params or {}),
        instruments=PAIR,
        knowledge_cutoff=CUTOFF,
        decision_times=tuple(decisions),
        signals=tuple(signals),
    )


def _run(request: StrategyRequest) -> StrategyResult:
    provider = DualMomentumProvider((SPEC,))
    result = provider.target_positions(request)
    result.check_answers(request, provider.descriptor)
    return result


def _weights(signals: Sequence[SignalObservation], at: datetime = AT_60) -> list[Decimal]:
    return [position.target_weight for position in _run(_request(signals, (at,))).positions]


def _perturb(item: SignalObservation) -> SignalObservation:
    value = item.value if isinstance(item.value, Decimal) else Decimal(0)
    return item.model_copy(update={"value": -value - Decimal("0.01")})


class TestDualMomentumContract(StrategyProviderContract):
    @pytest.fixture
    def strategy_subject(self) -> StrategySubject:
        return StrategySubject(
            open=lambda: DualMomentumProvider((SPEC,)),
            request=_request(WAVE_SIGNALS, WAVE_DECISIONS),
            unsupported=_request(WAVE_SIGNALS, WAVE_DECISIONS, params={"lookback": 61}),
            perturb=_perturb,
        )


def test_hand_computed_selection_holds_the_single_best_instrument() -> None:
    # Trailing sums over 60 returns: BTC 60 x 0.001 = 0.06, ETH 60 x 0.002 = 0.12 > 0.
    signals = _returns("BTCUSDT", _flat("0.001")) + _returns("ETHUSDT", _flat("0.002"))
    btc, eth = _run(_request(signals, (AT_60,))).positions
    assert (btc.target_weight, eth.target_weight) == (Decimal(0), Decimal(1))
    for position in (btc, eth):  # every position depends on both windows
        assert position.inputs_used == 120
        assert position.latest_input_available_time == AT_60


def test_absolute_momentum_at_or_below_zero_is_flat() -> None:
    negative = _returns("BTCUSDT", _flat("-0.002")) + _returns("ETHUSDT", _flat("-0.001"))
    zero = _returns("BTCUSDT", _flat("-0.002")) + _returns("ETHUSDT", _flat("0"))
    for signals in (negative, zero):
        result = _run(_request(signals, (AT_60,)))
        assert [p.target_weight for p in result.positions] == [Decimal(0), Decimal(0)]
        assert all(p.inputs_used == 120 for p in result.positions)  # computable, just flat


def test_a_tie_at_the_top_is_flat() -> None:
    tie = _returns("BTCUSDT", _flat("0.001")) + _returns("ETHUSDT", _flat("0.001"))
    assert _weights(tie) == [Decimal(0), Decimal(0)]


def test_insufficient_history_is_flat_without_inputs() -> None:
    signals = _returns("BTCUSDT", _flat("0.001")) + _returns("ETHUSDT", _flat("0.002"))
    early = _run(_request(signals, (T0 + 59 * MINUTE,))).positions
    assert all((p.target_weight, p.inputs_used) == (Decimal(0), 0) for p in early)
    one_short = _returns("BTCUSDT", _flat("0.001")) + _returns("ETHUSDT", _flat("0.002", 59))
    result = _run(_request(one_short, (AT_60,)))
    assert all((p.target_weight, p.inputs_used) == (Decimal(0), 0) for p in result.positions)


def test_an_explicit_none_is_not_filled_in() -> None:
    eth: list[Decimal | None] = [*_flat("0.002", 59), None]
    signals = _returns("BTCUSDT", _flat("0.001")) + _returns("ETHUSDT", eth)
    result = _run(_request(signals, (AT_60,)))
    assert all((p.target_weight, p.inputs_used) == (Decimal(0), 0) for p in result.positions)


def test_future_signals_do_not_move_past_positions() -> None:
    past = _returns("BTCUSDT", _flat("0.001")) + _returns("ETHUSDT", _flat("0.002"))
    later = _returns("BTCUSDT", _flat("0.01", 10), first_minute=61) + _returns(
        "ETHUSDT", _flat("-0.01", 10), first_minute=61
    )
    decisions = (AT_60, T0 + 70 * MINUTE)
    full = _run(_request(past + later, decisions))
    truncated = _run(_request(past, decisions))
    assert full.at(AT_60) == truncated.at(AT_60)
    # The later data does move the later decision: BTC now leads (0.05 + 0.1 > 0.1 - 0.1).
    assert [p.target_weight for p in full.at(T0 + 70 * MINUTE)] == [Decimal(1), Decimal(0)]


def test_long_or_flat_and_at_most_one_holding() -> None:
    result = _run(_request(WAVE_SIGNALS, WAVE_DECISIONS))
    for decision in WAVE_DECISIONS:
        weights = [p.target_weight for p in result.at(decision)]
        assert set(weights) <= {Decimal(0), Decimal(1)}
        assert sum(weights) <= 1
    assert any(p.target_weight == 1 for p in result.positions), "the wave fixture must hold"


def test_every_parameter_is_explicit_and_declared() -> None:
    assert DUAL_MOMENTUM_PARAM_SPACE == {"lookback": (60, 240, 1440)}
    with pytest.raises(TypeError):
        dual_momentum_spec()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="declared point"):
        dual_momentum_spec(lookback=61)
    for lookback in (60, 240, 1440):
        assert dict(dual_momentum_spec(lookback=lookback).params) == {"lookback": lookback}


def test_the_provider_refuses_points_and_specs_outside_the_space() -> None:
    provider = DualMomentumProvider((SPEC,))
    signals = _returns("BTCUSDT", _flat("0.001")) + _returns("ETHUSDT", _flat("0.002"))
    for params in ({"lookback": 120}, {"top_n": 1}, {"lookback": True}):
        with pytest.raises(UnsupportedStrategy):
            provider.target_positions(_request(signals, (AT_60,), params=dict(params)))
    wider = FrozenMapping({"lookback": (10, 60, 240, 1440)})
    with pytest.raises(ValueError, match="declared space"):
        DualMomentumProvider((SPEC.model_copy(update={"param_search_space": wider}),))
    with pytest.raises(ValueError):
        DualMomentumProvider(())


def test_foreign_signals_are_refused() -> None:
    everything = bar_signals(WAVE_BARS, vol_windows=(5,))
    vol = tuple(item for item in everything if item.signal != LOG_RETURN_SIGNAL)
    assert vol and all(item.signal == realized_vol_signal(5) for item in vol)
    with pytest.raises(StrategyInputError):
        DualMomentumProvider((SPEC,)).target_positions(_request(WAVE_SIGNALS + vol, WAVE_DECISIONS))

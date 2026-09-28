"""``zscore_reversion@1.0.0`` (ADR-0085 ``STR-MR-ZSCORE-001``): contract suite, hand-computed
z-score and positions, insufficient history, zero deviation, missing values, no look-ahead,
explicit parameter points."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

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
from research.strategies.price_signals import BAR_CLOSE_SIGNAL, BAR_HIGH_SIGNAL, bar_price_signals
from research.strategies.zscore_reversion import (
    ZSCORE_PARAM_SPACE,
    ZSCORE_SIGNALS,
    ZScoreReversionProvider,
    _zscore,
    zscore_spec,
)
from tests.contract_suites.strategy import StrategyProviderContract, StrategySubject
from tests.strategy_fixtures import MINUTE, T0, make_bars, wave_closes

CUTOFF = T0 + timedelta(days=1)
SPEC = zscore_spec(window=20, entry_z=2, exit_z=0)
PAIR = ("BTCUSDT", "ETHUSDT")
WAVE_BARS = make_bars("BTCUSDT", wave_closes(160)) + make_bars(
    "ETHUSDT", wave_closes(160, phase=13)
)
WAVE_SIGNALS = bar_price_signals(WAVE_BARS, ZSCORE_SIGNALS)
WAVE_DECISIONS = tuple(T0 + minute * MINUTE for minute in range(19, 160, 6))
WAVE_SPEC = zscore_spec(window=20, entry_z=1, exit_z=0)

#: 19 closes at 10, then 5 (z = -sqrt(19)), 6 (z ~ -2.61) and 10 (z > 0).
HAND_CLOSES = (*(Decimal(10) for _ in range(19)), Decimal(5), Decimal(6), Decimal(10))
HAND_SIGNALS = bar_price_signals(make_bars("BTCUSDT", HAND_CLOSES), ZSCORE_SIGNALS)
HAND_DECISIONS = tuple(T0 + minute * MINUTE for minute in (19, 20, 21, 22))


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
    provider = ZScoreReversionProvider((spec,))
    result = provider.target_positions(request)
    result.check_answers(request, provider.descriptor)
    return result


def _perturb(item: SignalObservation) -> SignalObservation:
    value = item.value if isinstance(item.value, Decimal) else Decimal(100)
    return item.model_copy(update={"value": value + Decimal(7)})


class TestZScoreReversionContract(StrategyProviderContract):
    @pytest.fixture
    def strategy_subject(self) -> StrategySubject:
        return StrategySubject(
            open=lambda: ZScoreReversionProvider((WAVE_SPEC,)),
            request=_request(WAVE_SIGNALS, WAVE_DECISIONS, instruments=PAIR, spec=WAVE_SPEC),
            unsupported=_request(
                WAVE_SIGNALS,
                WAVE_DECISIONS,
                instruments=PAIR,
                params={"window": 21},
                spec=WAVE_SPEC,
            ),
            perturb=_perturb,
        )


def test_hand_computed_zscore() -> None:
    # 19 x 10 and one 5: mean 9.75, population variance 23.75 / 20 = 1.1875,
    # z = -4.75 / sqrt(1.1875) = -sqrt(19) (since 4.75^2 / 1.1875 = 19).
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        z = _zscore(HAND_CLOSES[:20])
        expected = -Decimal(19).sqrt()
    assert z is not None
    assert abs(z - expected) < Decimal("1e-45")
    assert _zscore(HAND_CLOSES[1:21]) is not None


def test_hand_computed_entry_hold_and_exit() -> None:
    result = _run(_request(HAND_SIGNALS, HAND_DECISIONS))
    before, entry, hold, exit_ = result.positions
    # T0+19m: 19 closes < window 20 -> z missing.
    assert (before.target_weight, before.inputs_used) == (Decimal(0), 0)
    # T0+20m: z = -sqrt(19) < -2 -> long, one instrument: weight 1.
    assert entry.target_weight == Decimal(1)
    assert entry.inputs_used == 20
    assert entry.latest_input_available_time == T0 + 20 * MINUTE
    # T0+21m: 18 x 10, 5, 6: mean 9.55, variance 36.95 / 20, z = -3.55 / 1.3592... ~ -2.61 <= 0.
    assert hold.target_weight == Decimal(1)
    assert hold.inputs_used == 21
    # T0+22m: 17 x 10, 5, 6, 10: mean 9.55, last deviation +0.45 -> z > -exit_z = 0 -> flat.
    assert exit_.target_weight == Decimal(0)
    assert exit_.inputs_used == 22
    assert exit_.latest_input_available_time == T0 + 22 * MINUTE


def test_insufficient_history_is_flat_without_inputs() -> None:
    short = tuple(item for item in HAND_SIGNALS if item.available_time <= T0 + 19 * MINUTE)
    for position in _run(_request(short, HAND_DECISIONS)).positions:
        assert (position.target_weight, position.inputs_used) == (Decimal(0), 0)


def test_zero_deviation_is_missing_not_a_division_error() -> None:
    constant = bar_price_signals(
        make_bars("BTCUSDT", tuple(Decimal(10) for _ in range(30))), ZSCORE_SIGNALS
    )
    decisions = tuple(T0 + minute * MINUTE for minute in range(20, 31))
    for position in _run(_request(constant, decisions)).positions:
        assert (position.target_weight, position.inputs_used) == (Decimal(0), 0)


def test_an_explicit_none_resets_to_flat_and_restarts_the_run() -> None:
    signals = tuple(
        item.model_copy(update={"value": None}) if item.event_time == T0 + 20 * MINUTE else item
        for item in HAND_SIGNALS
    )
    for position in _run(_request(signals, HAND_DECISIONS[1:])).positions:
        assert (position.target_weight, position.inputs_used) == (Decimal(0), 0)


def test_future_signals_do_not_move_past_positions() -> None:
    cut = HAND_DECISIONS[1]
    base = _run(_request(HAND_SIGNALS, HAND_DECISIONS))
    changed = tuple(
        _perturb(item) if item.available_time > cut else item for item in HAND_SIGNALS
    )
    truncated = tuple(item for item in HAND_SIGNALS if item.available_time <= cut)
    for signals in (changed, truncated):
        other = _run(_request(signals, HAND_DECISIONS))
        assert [p for p in base.positions if p.decision_time <= cut] == [
            p for p in other.positions if p.decision_time <= cut
        ]


def test_long_or_flat_only() -> None:
    result = _run(
        _request(WAVE_SIGNALS, WAVE_DECISIONS, instruments=PAIR, spec=WAVE_SPEC), WAVE_SPEC
    )
    weights = {position.target_weight for position in result.positions}
    assert weights <= {Decimal(0), Decimal("0.5")}
    assert Decimal("0.5") in weights, "the wave fixture must dip below -entry_z"


def test_every_parameter_is_explicit_and_declared() -> None:
    assert ZSCORE_PARAM_SPACE == {"window": (20, 60, 240), "entry_z": (1, 2), "exit_z": (0,)}
    with pytest.raises(TypeError):
        zscore_spec(window=20, entry_z=2)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="declared point"):
        zscore_spec(window=21, entry_z=2, exit_z=0)
    with pytest.raises(ValueError, match="exit_z < entry_z"):
        zscore_spec(window=20, entry_z=1, exit_z=1)
    for window in (20, 60, 240):
        for entry_z in (1, 2):
            spec = zscore_spec(window=window, entry_z=entry_z, exit_z=0)
            assert dict(spec.params) == {"window": window, "entry_z": entry_z, "exit_z": 0}
            assert spec.lineage == ()


def test_the_provider_refuses_points_and_specs_outside_the_space() -> None:
    provider = ZScoreReversionProvider((SPEC,))
    for params in ({"entry_z": 3}, {"exit_z": 1}, {"lookback": 20}, {"window": True}):
        with pytest.raises(UnsupportedStrategy):
            provider.target_positions(_request(HAND_SIGNALS, HAND_DECISIONS, params=params))
    wider = FrozenMapping({"window": (5, 20, 60, 240), "entry_z": (1, 2), "exit_z": (0,)})
    with pytest.raises(ValueError, match="declared space"):
        ZScoreReversionProvider((SPEC.model_copy(update={"param_search_space": wider}),))
    with pytest.raises(ValueError):
        ZScoreReversionProvider(())


def test_foreign_signals_are_refused() -> None:
    highs = bar_price_signals(make_bars("BTCUSDT", HAND_CLOSES), (BAR_HIGH_SIGNAL,))
    with pytest.raises(StrategyInputError):
        ZScoreReversionProvider((SPEC,)).target_positions(
            _request(HAND_SIGNALS + highs, HAND_DECISIONS)
        )
    assert ZSCORE_SIGNALS == (BAR_CLOSE_SIGNAL,)

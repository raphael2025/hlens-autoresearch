"""Phase 6 framework: State x Strategy decomposition, conditional trial counting, P5 wiring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.contracts.state import (
    StateInput,
    StateProviderDescriptor,
    StateRequest,
    StateResult,
    StateValue,
)
from core.contracts.strategy import (
    BacktestCostModel,
    BacktestRequest,
    BacktestResult,
    TargetPosition,
)
from core.domain.base import FrozenMapping, Kind, Ref, content_hash
from plugins.backtest import BarBacktester
from research.experiments import (
    backtest_returns,
    matrix_from_backtest,
    register_conditionals,
    state_strategy_matrix,
)
from research.hypotheses import TrialLedger
from tests.strategy_fixtures import make_bars

S = Ref(kind=Kind.STRATEGY, name="tsmom", version="1.0.0")
ST = Ref(kind=Kind.STATE, name="vol_regime", version="1.0.0")
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def _t(i: int) -> datetime:
    return T0 + timedelta(minutes=i)


def test_returns_are_decomposed_by_the_state_known_at_t() -> None:
    returns = {
        _t(0): Decimal("0.01"),
        _t(1): Decimal("-0.02"),
        _t(2): Decimal("0.03"),
        _t(3): Decimal("0.005"),
    }
    states = {_t(0): "high", _t(1): "high", _t(2): "low", _t(3): None}
    matrix = state_strategy_matrix(S, ST, returns, states, top_k=1)
    cells = {cell.state: cell for cell in matrix.cells}
    assert cells["high"].count == 2 and cells["high"].total == Decimal("-0.01")
    assert cells["high"].hit_rate == Decimal("0.5")
    assert cells["low"].mean == Decimal("0.03") and cells[None].count == 1
    # gains: high 0.01, low 0.03, unknown 0.005 -> best state is low with 0.03 / 0.045
    assert matrix.best_state_share == Decimal("0.03") / Decimal("0.045")
    assert matrix.top_k_share_in_best_state == Decimal(1)
    assert "no thresholds applied" in matrix.report()


def test_a_return_without_a_state_evaluation_is_refused() -> None:
    with pytest.raises(ValueError, match="no state was evaluated"):
        state_strategy_matrix(S, ST, {_t(0): Decimal(1)}, {})


def test_explicit_max_state_age_uses_latest_prior_evaluation_and_keeps_unknowns() -> None:
    returns = {
        _t(-1): Decimal("0.01"),  # no prior state yet
        _t(1): Decimal("0.02"),
        _t(2): Decimal("0.03"),  # exact evaluation time is visible
        _t(4): Decimal("0.04"),  # latest evaluation is explicitly unknown
        _t(6): Decimal("0.05"),  # last known state is stale
    }
    states = {_t(0): "high", _t(2): "low", _t(3): None}
    matrix = state_strategy_matrix(S, ST, returns, states, max_state_age=timedelta(minutes=2))
    cells = {cell.state: cell for cell in matrix.cells}
    assert cells["high"].count == 1
    assert cells["low"].count == 1
    assert cells[None].count == 3
    assert matrix.max_state_age == timedelta(minutes=2)
    assert matrix.alignment_mode == "as_of"
    assert "state alignment: as_of (maximum age 0:02:00)" in matrix.report()


def test_asof_matrix_hash_binds_the_explicit_age_but_exact_matrix_identity_is_unchanged() -> None:
    returns = {_t(0): Decimal("0.01"), _t(1): Decimal("0.02")}
    states = {_t(0): "high", _t(1): "low"}
    exact = state_strategy_matrix(S, ST, returns, states)
    exact_explicit_none = state_strategy_matrix(S, ST, returns, states, max_state_age=None)
    asof = state_strategy_matrix(S, ST, returns, states, max_state_age=timedelta(minutes=1))
    assert exact == exact_explicit_none
    assert exact.alignment_mode == "exact"
    assert exact.matrix_hash == exact_explicit_none.matrix_hash
    assert exact.matrix_hash != asof.matrix_hash


def test_max_state_age_must_be_a_positive_timedelta() -> None:
    for invalid in (timedelta(0), timedelta(seconds=-1), 60):
        with pytest.raises(ValueError, match="max_state_age must be a positive timedelta"):
            state_strategy_matrix(
                S,
                ST,
                {_t(0): Decimal("0.01")},
                {_t(0): "high"},
                max_state_age=invalid,  # type: ignore[arg-type]
            )


def test_asof_state_mapping_requires_utc_datetimes() -> None:
    naive = datetime(2024, 1, 1)
    with pytest.raises(ValueError, match="must be UTC datetimes"):
        state_strategy_matrix(
            S,
            ST,
            {_t(0): Decimal("0.01")},
            {naive: "high"},
            max_state_age=timedelta(minutes=1),
        )


def test_every_conditional_attempt_counts_as_a_trial() -> None:
    ledger = TrialLedger()
    trials = register_conditionals(
        ledger,
        strategy=S,
        state=ST,
        labels=("low", "mid", "high"),
        family_id="s_x_st",
        minimum_effect="0.1",
    )
    assert trials == 3


# --------------------------------------------------------------------- W1: P5 backtest wiring


def _state_result(labels: dict[datetime, str | None]) -> StateResult:
    feature = Ref(kind=Kind.FEATURE, name="bar_realized_vol_5", version="1.0.0")
    source = content_hash({"fixture": "vol"})
    times = tuple(sorted(labels))
    request = StateRequest(
        state=ST,
        spec_hash=content_hash({"fixture": "state-spec"}),
        evaluation_times=times,
        inputs=tuple(
            StateInput(
                feature=feature, evaluation_time=t, value=Decimal(1), source_result_hash=source
            )
            for t in times
        ),
    )
    descriptor = StateProviderDescriptor(
        name="fixture_states",
        version="1.0.0",
        deterministic=True,
        supported_states=FrozenMapping({str(ST): request.spec_hash}),
    )
    values = [
        StateValue(evaluation_time=t, state=None, inputs_used=0)
        if labels[t] is None
        else StateValue(evaluation_time=t, state=labels[t], inputs_used=1, latest_input_time=t)
        for t in times
    ]
    return StateResult.build(request, descriptor, values)


def _buy_and_hold() -> BacktestResult:
    # bar k spans [T0 + k, T0 + k + 1); each opens at the previous close.
    bars = make_bars("BTC", [Decimal(c) for c in ("100", "110", "99", "99", "108.9")], start=T0)
    zero = BacktestCostModel(
        name="zero_cost", version="1.0.0", fee_rate=Decimal(0), slippage_rate=Decimal(0)
    )
    target = TargetPosition(
        decision_time=_t(1),
        instrument="BTC",
        target_weight=Decimal(1),
        inputs_used=1,
        latest_input_available_time=_t(1),
    )
    request = BacktestRequest(
        cost_model=zero, initial_equity=Decimal(1000), bars=bars, targets=(target,)
    )
    return BarBacktester().run(request)


def test_backtest_returns_are_keyed_by_the_start_of_each_period() -> None:
    returns = backtest_returns(_buy_and_hold())
    # marks: 1000 (t1, flat), 1100 (t2), 990 (t3), 990 (t4), 1089 (t5)
    assert returns == {
        _t(1): Decimal("0.1"),
        _t(2): Decimal("-0.1"),
        _t(3): Decimal(0),
        _t(4): Decimal("0.1"),
    }


def test_matrix_from_backtest_attributes_to_the_state_known_at_t_and_links_inputs() -> None:
    backtest = _buy_and_hold()
    states = _state_result({_t(1): "high", _t(2): "low", _t(3): "low", _t(4): None, _t(5): "low"})
    matrix = matrix_from_backtest(S, ST, backtest, states, top_k=1)
    cells = {cell.state: cell for cell in matrix.cells}
    assert cells["high"].count == 1 and cells["high"].total == Decimal("0.1")
    assert cells["low"].count == 2 and cells["low"].total == Decimal("-0.1")
    assert cells[None].count == 1 and cells[None].total == Decimal("0.1")
    assert matrix.backtest_result_hash == backtest.result_hash
    assert matrix.state_result_hash == states.result_hash
    again = matrix_from_backtest(S, ST, _buy_and_hold(), states, top_k=1)
    assert again == matrix and again.matrix_hash == matrix.matrix_hash
    assert backtest.result_hash in matrix.report()


def test_matrix_from_backtest_passes_explicit_asof_age_and_binds_it() -> None:
    backtest = _buy_and_hold()
    states = _state_result({_t(2): "high", _t(4): "low"})
    matrix = matrix_from_backtest(S, ST, backtest, states, max_state_age=timedelta(minutes=2))
    cells = {cell.state: cell for cell in matrix.cells}
    assert cells[None].count == 1  # the first return starts before any state evaluation
    assert cells["high"].count == 2
    assert cells["low"].count == 1
    assert matrix.max_state_age == timedelta(minutes=2)
    assert matrix.backtest_result_hash == backtest.result_hash
    assert matrix.state_result_hash == states.result_hash


def test_matrix_from_backtest_refuses_a_period_without_a_state() -> None:
    states = _state_result({_t(1): "high", _t(2): "low", _t(4): "low"})
    with pytest.raises(ValueError, match="no state was evaluated"):
        matrix_from_backtest(S, ST, _buy_and_hold(), states)

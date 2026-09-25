"""Phase 6 framework: State x Strategy decomposition and conditional trial counting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.domain.base import Kind, Ref
from research.experiments import register_conditionals, state_strategy_matrix
from research.hypotheses import TrialLedger

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

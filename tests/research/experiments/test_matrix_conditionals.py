"""Phase 6: every matrix cell is a pre-committed trial (declared state space + unknown cell)."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.domain.base import Kind, Ref
from core.domain.specs import StateSpec
from research.experiments import (
    UNKNOWN_STATE_VALUE,
    StateStrategyMatrix,
    conditional_hypotheses,
    register_conditionals,
    register_matrix_conditionals,
    state_strategy_matrix,
)
from research.hypotheses import LedgerError, TrialLedger

S = Ref(kind=Kind.STRATEGY, name="tsmom", version="1.0.0")
F = Ref(kind=Kind.FEATURE, name="realized_vol", version="1.0.0")
T0 = datetime(2024, 1, 1, tzinfo=UTC)
FAMILY = "tsmom_x_vol_regime"


def _spec(state_space: tuple[str, ...] = ("low", "mid", "high")) -> StateSpec:
    return StateSpec(
        name="vol_regime",
        version="1.0.0",
        features=(F,),
        state_space=state_space,
        method="test_only",
    )


SPEC = _spec()


def _matrix(labels: list[str | None], returns: list[str] | None = None) -> StateStrategyMatrix:
    times = [T0 + timedelta(minutes=i) for i in range(len(labels))]
    values = returns or ["0.01"] * len(labels)
    return state_strategy_matrix(
        S,
        SPEC.ref,
        {t: Decimal(v) for t, v in zip(times, values, strict=True)},
        dict(zip(times, labels, strict=True)),
    )


# low x3, unknown x1; mid and high are never visited
VISITED = _matrix(["low", "low", None, "low"])


def test_every_declared_cell_and_the_unknown_cell_are_counted_as_trials() -> None:
    ledger = TrialLedger()
    reg = register_matrix_conditionals(
        ledger, VISITED, state_spec=SPEC, family_id=FAMILY, minimum_effect="0.1", min_support=2
    )
    assert [c.state for c in reg.cells] == ["low", "mid", "high", None]
    assert [c.count for c in reg.cells] == [3, 0, 0, 1]
    assert reg.newly_registered == 4 and reg.family_trials == 4 == ledger.trials(FAMILY)
    assert [c.trial_index for c in reg.cells] == [1, 2, 3, 4]
    assert reg.matrix_hash == VISITED.matrix_hash and reg.min_support == 2
    unknown = ledger.hypotheses[-1]
    assert unknown.name == "h_tsmom_given_vol_regime_unknown_state"
    assert unknown.conditions == (f"{SPEC.ref} = {UNKNOWN_STATE_VALUE}",)
    assert [str(h.ref) for h in ledger.hypotheses] == [c.hypothesis for c in reg.cells]


def test_re_registration_is_idempotent_and_independent_of_results() -> None:
    ledger = TrialLedger()
    first = register_matrix_conditionals(
        ledger, VISITED, state_spec=SPEC, family_id=FAMILY, minimum_effect="0.1", min_support=2
    )
    again = register_matrix_conditionals(
        ledger, VISITED, state_spec=SPEC, family_id=FAMILY, minimum_effect="0.1", min_support=2
    )
    assert again.newly_registered == 0 and again.family_trials == 4
    assert again.cells == first.cells and again.matrix_hash == first.matrix_hash
    # other returns, other visited labels: the same four hypotheses, no new trial
    other = _matrix(["high", "mid", "mid"], ["-0.5", "0.2", "0.3"])
    third = register_matrix_conditionals(
        ledger, other, state_spec=SPEC, family_id=FAMILY, minimum_effect="0.1", min_support=2
    )
    assert third.newly_registered == 0 and ledger.trials(FAMILY) == 4
    assert [c.hypothesis for c in third.cells] == [c.hypothesis for c in first.cells]
    assert [c.count for c in third.cells] == [0, 2, 1, 0]


def test_a_conflicting_minimum_effect_registers_nothing() -> None:
    ledger = TrialLedger()
    # the conflict sits on the LAST planned cell: a naive loop would register three first
    unknown_cell = conditional_hypotheses(
        strategy=S, state_spec=SPEC, family_id=FAMILY, minimum_effect="0.1"
    )[-1][1]
    ledger.register(unknown_cell)
    with pytest.raises(LedgerError, match="other content"):
        register_matrix_conditionals(
            ledger, VISITED, state_spec=SPEC, family_id=FAMILY, minimum_effect="0.2", min_support=2
        )
    assert ledger.trials(FAMILY) == 1 and ledger.hypotheses == (unknown_cell,)


def test_a_label_outside_the_declared_state_space_is_refused() -> None:
    ledger = TrialLedger()
    stray = _matrix(["low", "extreme"])
    with pytest.raises(ValueError, match=r"outside the declared state space: \['extreme'\]"):
        register_matrix_conditionals(
            ledger, stray, state_spec=SPEC, family_id=FAMILY, minimum_effect="0.1", min_support=1
        )
    other_state = StateSpec(
        name="liq_regime", version="1.0.0", features=(F,), state_space=("low",), method="x"
    )
    with pytest.raises(ValueError, match="must declare the matrix's state"):
        register_matrix_conditionals(
            ledger,
            VISITED,
            state_spec=other_state,
            family_id=FAMILY,
            minimum_effect="0.1",
            min_support=1,
        )
    assert ledger.trials(FAMILY) == 0


def test_an_unnameable_declared_label_fails_before_any_registration() -> None:
    ledger = TrialLedger()
    spec = _spec(("low", "High"))  # "High" cannot form a hypothesis name (NAME_PATTERN)
    matrix = state_strategy_matrix(S, spec.ref, {T0: Decimal("0.01")}, {T0: "low"})
    with pytest.raises(ValueError):
        register_matrix_conditionals(
            ledger, matrix, state_spec=spec, family_id=FAMILY, minimum_effect="0.1", min_support=1
        )
    assert ledger.trials(FAMILY) == 0


def test_min_support_is_required_and_must_be_a_positive_int() -> None:
    ledger = TrialLedger()
    with pytest.raises(TypeError, match="min_support"):
        register_matrix_conditionals(  # type: ignore[call-arg]
            ledger, VISITED, state_spec=SPEC, family_id=FAMILY, minimum_effect="0.1"
        )
    for bad in (0, -1, True, "2", 1.5):
        with pytest.raises(ValueError, match="min_support"):
            register_matrix_conditionals(
                ledger,
                VISITED,
                state_spec=SPEC,
                family_id=FAMILY,
                minimum_effect="0.1",
                min_support=bad,  # type: ignore[arg-type]
            )
    assert ledger.trials(FAMILY) == 0


def test_cells_below_min_support_are_kept_and_reported_unsupported() -> None:
    reg = register_matrix_conditionals(
        TrialLedger(),
        VISITED,
        state_spec=SPEC,
        family_id=FAMILY,
        minimum_effect="0.1",
        min_support=2,
    )
    assert [(c.state, c.supported, c.reason) for c in reg.cells] == [
        ("low", True, "meets_min_support"),
        ("mid", False, "below_min_support"),
        ("high", False, "below_min_support"),
        (None, False, "below_min_support"),
    ]


def test_no_stated_threshold_means_every_cell_is_unsupported() -> None:
    reg = register_matrix_conditionals(
        TrialLedger(),
        VISITED,
        state_spec=SPEC,
        family_id=FAMILY,
        minimum_effect="0.1",
        min_support=None,
    )
    assert len(reg.cells) == 4 and reg.family_trials == 4
    assert all(not c.supported and c.reason == "no_support_threshold" for c in reg.cells)


def test_no_post_hoc_selection_of_cells_is_possible() -> None:
    params = inspect.signature(register_matrix_conditionals).parameters
    assert set(params) == {
        "ledger",
        "matrix",
        "state_spec",
        "family_id",
        "minimum_effect",
        "min_support",
    }
    assert params["min_support"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        register_matrix_conditionals(  # type: ignore[call-arg]
            TrialLedger(),
            VISITED,
            state_spec=SPEC,
            family_id=FAMILY,
            minimum_effect="0.1",
            min_support=1,
            labels=("low",),
        )
    # a caller who registered only the winning label earlier still gets every cell counted,
    # and the label hypotheses are exactly those register_conditionals produces
    ledger = TrialLedger()
    assert (
        register_conditionals(
            ledger,
            strategy=S,
            state=SPEC.ref,
            labels=("low",),
            family_id=FAMILY,
            minimum_effect="0.1",
        )
        == 1
    )
    reg = register_matrix_conditionals(
        ledger, VISITED, state_spec=SPEC, family_id=FAMILY, minimum_effect="0.1", min_support=1
    )
    assert reg.newly_registered == 3 and reg.family_trials == 4
    assert reg.cells[0].trial_index == 1

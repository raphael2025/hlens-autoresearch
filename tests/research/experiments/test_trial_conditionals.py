"""Phase 6 in the loop: every cell of one trial's matrix is a counted trial of the trial's family.

``register_trial_conditionals`` is the form the continuous loop uses (opt-in ``ConditionalPlan``):
cells keyed by the trial's hypothesis, one trial per cell and look (a re-evaluation of the parent
registers one re-evaluation of every cell under the same attempt key).
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.domain.base import Kind, Ref
from core.domain.specs import StateSpec
from research.experiments import (
    UNKNOWN_STATE_VALUE,
    ConditionalRegistration,
    StateStrategyMatrix,
    conditional_hypotheses,
    register_trial_conditionals,
    state_strategy_matrix,
    trial_conditional_hypotheses,
)
from research.hypotheses import LedgerError, TrialLedger, conditioning

S = Ref(kind=Kind.STRATEGY, name="tsmom", version="1.0.0")
F = Ref(kind=Kind.FEATURE, name="realized_vol", version="1.0.0")
T0 = datetime(2024, 1, 1, tzinfo=UTC)
FAMILY = "tsmom_family"
SPEC = StateSpec(
    name="vol_regime",
    version="1.0.0",
    features=(F,),
    state_space=("low", "mid", "high"),
    method="test_only",
)
PARENT = conditioning("h_tsmom_60", FAMILY, S, SPEC.ref, "low", "0.1")
OTHER_PARENT = conditioning("h_tsmom_240", FAMILY, S, SPEC.ref, "low", "0.1")


def _matrix(labels: list[str | None], returns: list[str] | None = None) -> StateStrategyMatrix:
    times = [T0 + timedelta(minutes=i) for i in range(len(labels))]
    values = returns or ["0.01"] * len(labels)
    return state_strategy_matrix(
        S,
        SPEC.ref,
        {t: Decimal(v) for t, v in zip(times, values, strict=True)},
        dict(zip(times, labels, strict=True)),
    )


VISITED = _matrix(["low", "low", None, "low"])


def _register(
    ledger: TrialLedger,
    matrix: StateStrategyMatrix = VISITED,
    *,
    parent: object = PARENT,
    attempt: str | None = None,
    minimum_effect: str = "0.1",
) -> ConditionalRegistration:
    return register_trial_conditionals(
        ledger,
        matrix,
        parent=parent,  # type: ignore[arg-type]
        attempt=attempt,
        state_spec=SPEC,
        minimum_effect=minimum_effect,
        min_support=2,
    )


def test_every_declared_cell_and_the_unknown_cell_are_trials_of_the_parents_family() -> None:
    ledger = TrialLedger()
    ledger.register(PARENT)
    reg = register_trial_conditionals(
        ledger,
        VISITED,
        parent=PARENT,
        attempt=None,
        state_spec=SPEC,
        minimum_effect="0.1",
        min_support=2,
    )
    assert [c.state for c in reg.cells] == ["low", "mid", "high", None]
    assert [c.count for c in reg.cells] == [3, 0, 0, 1]  # zero-support cells are trials too
    assert reg.newly_registered == 4 and reg.family_trials == 5 == ledger.trials(FAMILY)
    assert [c.trial_index for c in reg.cells] == [2, 3, 4, 5]
    assert reg.parent == str(PARENT.ref) and reg.attempt is None and reg.family_id == FAMILY
    names = [h.name for h in ledger.hypotheses[1:]]
    assert names == [
        "h_tsmom_60_given_vol_regime_low",
        "h_tsmom_60_given_vol_regime_mid",
        "h_tsmom_60_given_vol_regime_high",
        "h_tsmom_60_given_vol_regime_unknown_state",
    ]
    for (label, planned), registered in zip(
        conditional_hypotheses(strategy=S, state_spec=SPEC, family_id=FAMILY, minimum_effect="0.1"),
        ledger.hypotheses[1:],
        strict=True,
    ):  # same statement and condition as the strategy-keyed form; traceable to the parent
        assert registered.statement == planned.statement
        assert registered.conditions == planned.conditions
        assert registered.origin_refs == (*planned.origin_refs, PARENT.ref)
        assert registered.version == PARENT.version and registered.family_id == FAMILY
        if label is None:
            assert registered.conditions == (f"{SPEC.ref} = {UNKNOWN_STATE_VALUE}",)


def test_the_registrations_do_not_depend_on_the_matrix_results() -> None:
    first, second = TrialLedger(), TrialLedger()
    a = _register(first, VISITED)
    b = _register(second, _matrix(["high", "mid", "mid"], ["-0.5", "0.2", "0.3"]))
    assert [h.content_hash() for h in first.hypotheses] == [
        h.content_hash() for h in second.hypotheses
    ]
    assert [c.hypothesis for c in a.cells] == [c.hypothesis for c in b.cells]
    assert [c.count for c in b.cells] == [0, 2, 1, 0]


def test_the_same_look_again_adds_no_trial() -> None:
    ledger = TrialLedger()
    first = _register(ledger)
    again = _register(ledger)
    assert again.newly_registered == 0 and ledger.trials(FAMILY) == 4
    assert again.cells == first.cells
    look = _register(ledger, attempt="loop_round:x:1")
    assert look.newly_registered == 4 and ledger.trials(FAMILY) == 8
    repeat = _register(ledger, attempt="loop_round:x:1")
    assert repeat.newly_registered == 0 and ledger.trials(FAMILY) == 8
    assert repeat.cells == look.cells


def test_a_re_evaluation_look_is_one_more_trial_per_cell() -> None:
    ledger = TrialLedger()
    _register(ledger)
    reg = register_trial_conditionals(
        ledger,
        VISITED,
        parent=PARENT,
        attempt="loop_round:x:1",
        state_spec=SPEC,
        minimum_effect="0.1",
        min_support=2,
    )
    assert reg.attempt == "loop_round:x:1" and reg.newly_registered == 4
    assert [c.trial_index for c in reg.cells] == [5, 6, 7, 8]
    assert [e.attempt for e in ledger.trial_log] == [None] * 4 + ["loop_round:x:1"] * 4
    assert len(ledger.hypotheses) == 4  # the same four cell hypotheses, looked at again


def test_each_parent_has_its_own_cells() -> None:
    ledger = TrialLedger()
    _register(ledger, parent=PARENT)
    other = _register(ledger, parent=OTHER_PARENT)  # same strategy, another hypothesis
    assert other.newly_registered == 4 and ledger.trials(FAMILY) == 8
    assert all("h_tsmom_240_given_" in c.hypothesis for c in other.cells)


def test_a_re_evaluation_look_without_its_first_look_registers_nothing() -> None:
    ledger = TrialLedger()
    with pytest.raises(LedgerError, match="needs the cell's first look"):
        _register(ledger, attempt="loop_round:x:1")
    assert ledger.trials(FAMILY) == 0 and ledger.hypotheses == ()


def test_a_conflicting_minimum_effect_registers_nothing() -> None:
    ledger = TrialLedger()
    unknown_cell = trial_conditional_hypotheses(
        parent=PARENT, strategy=S, state_spec=SPEC, minimum_effect="0.1"
    )[-1][1]
    ledger.register(unknown_cell)  # the conflict sits on the LAST planned cell
    with pytest.raises(LedgerError, match="other content"):
        _register(ledger, minimum_effect="0.2")
    assert ledger.trials(FAMILY) == 1 and ledger.hypotheses == (unknown_cell,)
    with pytest.raises(LedgerError):  # a look at cells that are not (all) registered as planned
        _register(ledger, minimum_effect="0.2", attempt="loop_round:x:1")
    assert ledger.trials(FAMILY) == 1


def test_inputs_are_checked_before_any_registration() -> None:
    ledger = TrialLedger()
    with pytest.raises(ValueError, match="needs the trial's Hypothesis"):
        _register(ledger, parent=str(PARENT.ref))
    with pytest.raises(ValueError, match="attempt"):
        _register(ledger, attempt="  ")
    with pytest.raises(ValueError, match=r"outside the declared state space: \['extreme'\]"):
        _register(ledger, _matrix(["low", "extreme"]))
    for bad in (0, -1, True, "2"):
        with pytest.raises(ValueError, match="min_support"):
            register_trial_conditionals(
                ledger,
                VISITED,
                parent=PARENT,
                attempt=None,
                state_spec=SPEC,
                minimum_effect="0.1",
                min_support=bad,  # type: ignore[arg-type]
            )
    assert ledger.trials(FAMILY) == 0


def test_min_support_and_attempt_are_required_and_no_cell_can_be_chosen() -> None:
    params = inspect.signature(register_trial_conditionals).parameters
    assert set(params) == {
        "ledger",
        "matrix",
        "parent",
        "attempt",
        "state_spec",
        "minimum_effect",
        "min_support",
    }
    for name in ("parent", "attempt", "state_spec", "minimum_effect", "min_support"):
        assert params[name].default is inspect.Parameter.empty
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError, match="min_support"):
        register_trial_conditionals(  # type: ignore[call-arg]
            TrialLedger(),
            VISITED,
            parent=PARENT,
            attempt=None,
            state_spec=SPEC,
            minimum_effect="0.1",
        )

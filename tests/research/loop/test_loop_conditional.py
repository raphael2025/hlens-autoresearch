"""P6 conditional hypotheses in the continuous loop (opt-in ``ConditionalPlan``, 2026-09-26).

With ``LoopWiring.conditional`` set, every cell of each completed trial's State × Strategy matrix
(declared state space + unknown cell) is pre-registered as a trial of the trial's family right
after the matrix is computed, so G3's family trial count includes them; ``None`` changes nothing
(pinned hashes: ``test_loop_e2e.test_records_without_a_conditional_plan_are_pinned``).

Scenario (every number TEST ONLY, arbitrary and uncalibrated; see ``loop_fixtures``): the default
three-round planted loop with a larger trial budget (the conditional trials are charged to it),
and the LLM draft ``h_llm_0`` (no strategy condition) approved after round 0 so round 1 has an
errored trial. ``min_support`` = 5 is an arbitrary TEST ONLY reporting threshold.
"""

from __future__ import annotations

import dataclasses
import gc
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from apps.worker import LoopBudget, ResearchLoop
from core.domain.research import RunState
from core.domain.specs import StateSpec
from core.lifecycle.strategy import LifecycleState
from infrastructure.event_bus import InMemoryEventBus
from plugins.llm import ScriptedLLMProvider
from plugins.synthetic import RandomWalkMarket
from research.experiments import register_trial_conditionals, state_strategy_matrix
from research.hypotheses import TrialLedger
from research.loop import (
    ConditionalPlan,
    ExperimentStage,
    LoopStateInconsistent,
    LoopWiring,
    ResearchMemory,
    SyntheticLoopConfig,
    loop_fingerprint,
    open_synthetic_loop,
)
from research.loop import durable as loop_durable
from research.loop.durable import LEDGER_FILE
from research.loop.trials import PER_CELL_VALIDATION
from research.persistence import AppendOnlyJournal
from research.strategies.failure_registry import FailureRegistry
from tests.research.loop import loop_fixtures as fx

ROUNDS = 3
DRAFT = "h_llm_0@1.0.0"
REVIEWER = "test-human"
LLM_LOOKBACKS = (None, 240, 1440)
#: TEST ONLY plan numbers (arbitrary; a reporting threshold, not a Profile number).
PLAN = ConditionalPlan(minimum_effect="net mean return above costs (test only)", min_support=5)
#: TEST ONLY budget: room for 4 conditional trials per trial of a round.
BUDGET = LoopBudget(
    max_trials_per_round=30,
    max_trials_total=80,
    max_llm_cost_units=Decimal(10),
    max_compute_seconds=Decimal(1000),
)
CELLS = [*fx.TREND.state_space, None]
COMPLETED = RunState.COMPLETED.value


def _config(plan: ConditionalPlan | None = PLAN) -> SyntheticLoopConfig:
    return fx.config(budget=BUDGET, loop_wiring=replace(fx.wiring(), conditional=plan))


def _llm(consumed: int = 0) -> ScriptedLLMProvider:
    outputs = [fx.llm_output(i, lookback) for i, lookback in enumerate(LLM_LOOKBACKS)]
    return ScriptedLLMProvider(outputs[consumed:], clock=lambda: fx.T0)


@dataclass
class Run:
    loop: ResearchLoop
    memory: ResearchMemory


def _stage(record: Any, name: str) -> Any:
    return next(stage for stage in record.stages if stage.name == name)


@pytest.fixture(scope="module")
def planned(tmp_path_factory: pytest.TempPathFactory) -> Run:
    tmp = tmp_path_factory.mktemp("planned")
    loop, memory, _ = fx.build(tmp, _config(), llm_lookbacks=LLM_LOOKBACKS)
    loop.run_unattended(1)
    memory.reviews.approve(DRAFT, reviewer=REVIEWER)
    loop.run_unattended(ROUNDS - 1)
    return Run(loop, memory)


# ------------------------------------------------------------------------------------ the plan


def test_both_plan_fields_are_required_and_checked() -> None:
    assert all(f.default is dataclasses.MISSING for f in dataclasses.fields(ConditionalPlan))
    with pytest.raises(TypeError):
        ConditionalPlan()  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="min_support"):
        ConditionalPlan(minimum_effect="x")  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="minimum_effect"):
        ConditionalPlan(min_support=5)  # type: ignore[call-arg]
    for effect in ("", "  ", None, 1):
        with pytest.raises(ValueError, match="minimum_effect"):
            ConditionalPlan(minimum_effect=effect, min_support=5)  # type: ignore[arg-type]
    for support in (0, -1, True, "5", 2.0):
        with pytest.raises(ValueError, match="min_support"):
            ConditionalPlan(minimum_effect="x", min_support=support)  # type: ignore[arg-type]
    assert ConditionalPlan(minimum_effect="x", min_support=None).payload() == {
        "minimum_effect": "x",
        "min_support": None,
    }
    wiring_default = {f.name: f.default for f in dataclasses.fields(LoopWiring)}["conditional"]
    assert wiring_default is None and fx.wiring().conditional is None


def test_the_stage_refuses_a_plan_it_cannot_name_cells_for(tmp_path: Path) -> None:
    memory = ResearchMemory(failures=FailureRegistry(tmp_path / "failures.jsonl"))
    components = cast(Any, None)  # never reached: the plan is refused first
    with pytest.raises(ValueError, match="needs the StateSpec"):
        ExperimentStage(memory, components, compute_seconds_per_trial=Decimal(1), conditional=PLAN)
    with pytest.raises(ValueError, match="must be a ConditionalPlan"):
        ExperimentStage(
            memory,
            components,
            compute_seconds_per_trial=Decimal(1),
            conditional=cast(Any, {"minimum_effect": "x", "min_support": 5}),
            state_spec=fx.TREND,
        )
    upper = StateSpec.model_validate(
        {**fx.TREND.model_dump(), "state_space": ("Trend_Down", "range", "trend_up")}
    )
    with pytest.raises(ValueError, match=r"\['Trend_Down'\] cannot name a cell hypothesis"):
        ExperimentStage(
            memory,
            components,
            compute_seconds_per_trial=Decimal(1),
            conditional=PLAN,
            state_spec=upper,
        )


def test_the_plan_is_fingerprinted_only_when_set() -> None:
    plain = loop_fingerprint(fx.config())
    assert "conditional" not in plain
    assert loop_fingerprint(_config(None)) == {**plain, "budget": BUDGET.payload()}
    with_plan = loop_fingerprint(_config())
    assert with_plan["conditional"] == PLAN.payload()
    other = loop_fingerprint(
        _config(ConditionalPlan(minimum_effect=PLAN.minimum_effect, min_support=6))
    )
    assert other != with_plan


# ----------------------------------------------------------------------------- the registrations


def _rows(run: Run) -> list[tuple[int, dict[str, Any]]]:
    return [
        (record.round_index, row)
        for record in run.loop.audit.records
        for row in _stage(record, "experiment").summary["experiments"]
    ]


def test_every_cell_of_every_completed_trial_is_a_registered_trial(planned: Run) -> None:
    ledger = planned.memory.ledger
    hypotheses = {str(h.ref): h for h in ledger.hypotheses}
    looks = {(e.name, e.version, e.attempt) for e in ledger.trial_log}
    completed = [(i, row) for i, row in _rows(planned) if row["run_state"] == COMPLETED]
    assert len(completed) >= 4 and any(row["attempt"] for _, row in completed)
    for _, row in completed:
        conditional = row["conditional"]
        assert conditional["parent"] == row["hypothesis"]
        assert conditional["attempt"] == row["attempt"]
        assert conditional["matrix_hash"] == row["state_strategy_matrix_hash"]
        assert conditional["minimum_effect"] == PLAN.minimum_effect
        assert conditional["min_support"] == PLAN.min_support
        assert conditional["validation"] == PER_CELL_VALIDATION
        assert conditional["newly_registered"] == len(CELLS)
        # every declared cell and the unknown cell, in declared order, whatever the counts
        assert [c["state"] for c in conditional["cells"]] == CELLS
        parent = row["hypothesis"].split(":", 1)[1].split("@", 1)[0]
        for cell in conditional["cells"]:
            hypothesis = hypotheses[cell["hypothesis"]]
            assert hypothesis.name.startswith(f"{parent}_given_{fx.TREND.name}_")
            assert hypothesis.family_id == fx.FAMILY
            assert (hypothesis.name, hypothesis.version, row["attempt"]) in looks
            assert ledger.trial_index(hypothesis, row["attempt"]) == cell["trial_index"]
            expected = "meets_min_support" if cell["count"] >= 5 else "below_min_support"
            assert cell["support"] == expected and cell["supported"] is (cell["count"] >= 5)
        # the first look registers the cells, a re-evaluation looks at them again (same attempt)
        indices = [c["trial_index"] for c in conditional["cells"]]
        assert indices == list(range(indices[0], indices[0] + len(CELLS)))
        assert conditional["family_trials"] == indices[-1]


def test_an_errored_trial_registers_no_conditional(planned: Run) -> None:
    errored = [row for _, row in _rows(planned) if row["run_state"] != COMPLETED]
    assert [row["hypothesis"] for row in errored] == [f"hypothesis:{DRAFT}"]
    assert errored[0]["conditional"] is None
    assert not any(h.name.startswith("h_llm_0_given_") for h in planned.memory.ledger.hypotheses)


def test_the_family_count_g3_sees_includes_every_conditional_trial(planned: Run) -> None:
    ledger = planned.memory.ledger
    conditional = [e for e in ledger.trial_log if "_given_" in e.name]
    own = [e for e in ledger.trial_log if "_given_" not in e.name]
    completed = [row for _, row in _rows(planned) if row["run_state"] == COMPLETED]
    assert len(conditional) == len(CELLS) * len(completed)
    assert ledger.trials(fx.FAMILY) == len(own) + len(conditional)
    for record in planned.loop.audit.records:
        rows = _stage(record, "experiment").summary["experiments"]
        registered = [r["conditional"]["family_trials"] for r in rows if r["conditional"]]
        reports = _stage(record, "validation").summary["reports"]
        assert reports and registered
        # the experiment stage registered the round's cells before the validation stage ran
        assert {r["family_trial_count"] for r in reports} == {max(registered)}
        assert all(r["trial_index"] <= r["family_trial_count"] - len(CELLS) for r in reports)


def test_the_conditional_trials_are_charged_to_the_loop_budget(planned: Run) -> None:
    for record in planned.loop.audit.records:
        stage = _stage(record, "experiment")
        rows = stage.summary["experiments"]
        newly = sum(r["conditional"]["newly_registered"] for r in rows if r["conditional"])
        assert stage.usage.trials == newly
        assert stage.estimate.trials == len(CELLS) * len(rows) >= newly
    total = planned.loop.total_usage.trials
    assert total >= len(planned.memory.ledger.trial_log)


def test_conditional_hypotheses_never_enter_the_lifecycle(planned: Run) -> None:
    """Registered as trials only: no lifecycle subject, never re-evaluated or evolved as such."""
    subjects = {str(h.subject) for h in planned.loop.guard.histories}
    assert subjects and not any("_given_" in subject for subject in subjects)
    assert all(
        "_given_" not in str(t.hypothesis.ref) for t in planned.memory.trials
    )  # no trial of a cell hypothesis ran (per-cell validation is a follow-up)
    states = {h.transitions[-1].to_state for h in planned.loop.guard.histories}
    assert LifecycleState.FAILED in states  # the errored draft: the lifecycle still works


# ----------------------------------------------------------------------------------- durability


def _open(state_dir: Path, *, consumed: int = 0, plan: ConditionalPlan | None = PLAN) -> Any:
    return open_synthetic_loop(
        _config(plan),
        state_dir=state_dir,
        provider=RandomWalkMarket(),
        bus=InMemoryEventBus(),
        llm=_llm(consumed),
    )


@pytest.fixture(scope="module")
def restarted(tmp_path_factory: pytest.TempPathFactory) -> Path:
    state_dir = tmp_path_factory.mktemp("restarted") / "state"
    first = _open(state_dir)
    first.loop.run_unattended(1)
    first.memory.reviews.approve(DRAFT, reviewer=REVIEWER)
    del first
    gc.collect()
    second = _open(state_dir, consumed=1)
    second.loop.run_unattended(ROUNDS - 1)
    del second
    gc.collect()
    return state_dir


def test_a_restarted_loop_replays_the_registrations_exactly(restarted: Path, planned: Run) -> None:
    reopened = _open(restarted, consumed=ROUNDS)
    assert [r.record_hash for r in reopened.loop.audit.records] == [
        r.record_hash for r in planned.loop.audit.records
    ]
    assert list(reopened.memory.ledger.trial_log) == list(planned.memory.ledger.trial_log)
    assert reopened.memory.ledger.trials(fx.FAMILY) == planned.memory.ledger.trials(fx.FAMILY)


def test_registering_a_recorded_look_again_on_reopening_adds_nothing(restarted: Path) -> None:
    reopened = _open(restarted, consumed=ROUNDS)
    ledger = reopened.memory.ledger
    lines = len(AppendOnlyJournal(restarted / LEDGER_FILE).entries)
    before = list(ledger.trial_log)
    looks = [t for t in reopened.memory.trials if t.summary.get("conditional")]
    assert looks
    for trial in looks:
        assert trial.candidate is not None
        empty = state_strategy_matrix(trial.candidate.spec.ref, fx.TREND.ref, {}, {})
        again = register_trial_conditionals(
            ledger,
            empty,
            parent=trial.hypothesis,
            attempt=trial.attempt,
            state_spec=fx.TREND,
            minimum_effect=PLAN.minimum_effect,
            min_support=PLAN.min_support,
        )
        assert again.newly_registered == 0
        recorded = trial.summary["conditional"]["cells"]
        assert [c.hypothesis for c in again.cells] == [c["hypothesis"] for c in recorded]
        assert [c.trial_index for c in again.cells] == [c["trial_index"] for c in recorded]
    assert list(ledger.trial_log) == before
    assert len(AppendOnlyJournal(restarted / LEDGER_FILE).entries) == lines


def test_reopening_with_another_plan_or_without_one_is_refused(restarted: Path) -> None:
    other = ConditionalPlan(minimum_effect=PLAN.minimum_effect, min_support=6)
    for plan in (other, None):
        with pytest.raises(LoopStateInconsistent, match="conditional"):
            _open(restarted, consumed=ROUNDS, plan=plan)


def test_a_ledger_without_the_recorded_cells_is_refused(restarted: Path) -> None:
    """Cross-check 6: every cell an experiment row registered must be in the trial ledger."""
    reopened = _open(restarted, consumed=ROUNDS)
    memory = reopened.memory
    stripped = TrialLedger()
    for hypothesis in memory.ledger.hypotheses:
        if "_given_" not in hypothesis.name:
            if hypothesis.origin.value == "llm":
                stripped._register(hypothesis)  # test-only: bypasses the review requirement
            else:
                stripped.register(hypothesis)
    for entry in memory.ledger.trial_log:
        if entry.attempt is not None and "_given_" not in entry.name:
            match = next(h for h in stripped.hypotheses if h.name == entry.name)
            stripped.register_reevaluation(match, entry.attempt)
    memory.ledger = stripped
    with pytest.raises(LoopStateInconsistent, match="_given_trend_range_"):
        loop_durable._check_ledgers(memory, reopened.loop.audit.records)

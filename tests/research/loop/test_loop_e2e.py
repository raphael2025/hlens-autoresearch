"""ADR-0049 W2 e2e: unattended rounds of the loop on the real Phase 1 / 2 / 4 / 5 / 6 / 7 / 8 / 12
components (F4 features, P2 state provider via ``run_state``, P5 TSMOM + ``BarBacktester``,
P6 State × Strategy matrix, P7 ``TrialLedger``, P4 / P8 ``PipelineBacktestValidator`` G0 – G4,
P12 evolution operators) on a synthetic market with a planted effect, versus pure noise.

Every round evaluates on the accumulated research data (ADR-0049 accumulated-window note); see
``loop_fixtures`` for the calendar (research window = rounds 0 and 1, round 2 after it).

TEST ONLY: every budget, cost unit, model parameter and Profile number comes from
``loop_fixtures`` and is arbitrary and uncalibrated (see its docstring). Synthetic results support
no claim about real markets (roadmap Phase 9).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast, get_protocol_members

import pytest

from apps.worker import AUTOMATABLE_TARGETS, LoopBudget, ResearchLoop, RoundStatus, StageStatus
from apps.worker.loop import EXTENDED_STAGE_ORDER, FORBIDDEN_TARGETS, ROUND_TOPIC, STAGE_TOPIC
from core.domain.base import Ref, content_hash
from core.domain.research import FailureRecord, HypothesisOrigin, RunState, Verdict
from core.domain.specs import StrategySpec
from core.errors import ReasonCode
from core.lifecycle.strategy import LifecycleState
from infrastructure.event_bus import InMemoryEventBus
from plugins.llm import ScriptedLLMProvider
from plugins.synthetic import RandomWalkMarket
from research.evolution import LineageGraph, require_new_version
from research.experiments.run_inputs import (
    RUN_INPUTS_KEY,
    STATE_LABELLER_FORMAT,
    recorded_run_inputs,
    strategy_params,
)
from research.loop import (
    OosUnsealBudget,
    ResearchMemory,
    RoundData,
    build_synthetic_loop,
    loop_fingerprint,
    open_synthetic_loop,
)
from research.loop import trials as loop_trials
from research.loop.compose import _synthetic_ingest, compose_loop
from research.loop.stages import reevaluation_candidates
from research.loop.trials import EPHEMERAL_UNSEAL_MARK
from research.strategies.failure_registry import FailureRegistry
from research.strategies.pipeline import CandidateTrialRunner
from research.validation.sealed_oos import (
    DurableUnsealingLedger,
    InMemoryUnsealingLedger,
    OosAlreadyUnsealed,
    SealedOosAlreadyEvaluated,
    SealedOosVault,
)
from tests.research.loop import loop_fixtures as fx


@dataclass
class Run:
    loop: ResearchLoop
    memory: ResearchMemory
    bus: InMemoryEventBus
    records: tuple[Any, ...]


def _run(tmp: Path, rounds: int = 3, **config: Any) -> Run:
    loop, memory, bus = fx.build(tmp, fx.config(**config))
    return Run(loop, memory, bus, loop.run_unattended(rounds))


@pytest.fixture(scope="module")
def planted(tmp_path_factory: pytest.TempPathFactory) -> Run:
    return _run(tmp_path_factory.mktemp("planted"))


@pytest.fixture(scope="module")
def noise(tmp_path_factory: pytest.TempPathFactory) -> Run:
    return _run(tmp_path_factory.mktemp("noise"), planted=False)


def _stage(record: Any, name: str) -> Any:
    return next(stage for stage in record.stages if stage.name == name)


def _floats(value: object) -> Iterator[float]:
    if isinstance(value, float):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _floats(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _floats(item)


def _transitions(run: Run) -> list[Any]:
    return [t for h in run.loop.guard.histories for t in h.transitions]


# ---------------------------------------------------------------------------------------- rounds


H60 = "hypothesis:h_k_tsmom_lookback_60@1.0.0"
H240 = "hypothesis:h_k_tsmom_lookback_240@1.0.0"
H1440 = "hypothesis:h_k_tsmom_lookback_1440@1.0.0"
CHILD = "hypothesis:h_tsmom_bars_v1_1_0@1.0.0"
BOUNDARY = fx.T0 + timedelta(days=6)  # the default TEST ONLY Profile's sealed OOS boundary


def _reports(record: Any) -> list[tuple[str, str, str]]:
    return [
        (r["hypothesis"], r["origin"], r["verdict"])
        for r in _stage(record, "validation").summary["reports"]
    ]


def test_three_unattended_rounds_on_real_components(planted: Run) -> None:
    """The planted 60-bar effect on the accumulated research window.

    Round 0 sees 3 of the Profile's 6 research days: every G0 - G4 gate passes except the
    walk-forward coverage — the Profile's windows over days 3 - 6 have no returns yet, which is
    INCONCLUSIVE (R23) — so the hypothesis stays in VALIDATION. Round 1's accumulated data covers
    the whole research window and the walk-forward: the hypothesis, re-evaluated as a new
    registered trial, passes G0 - G4 and moves to OOS. Round 2 brings no research data (it lies
    after the window), so nothing is re-evaluated; its fresh hypothesis is refuted.
    """
    run = planted
    assert [r.status for r in run.records] == [RoundStatus.COMPLETED] * 3
    assert run.loop.audit.verify()
    assert all(s.status is StageStatus.COMPLETED for r in run.records for s in r.stages)
    assert [s.name for s in run.records[0].stages] == list(EXTENDED_STAGE_ORDER)
    assert len(run.bus.poll("audit", STAGE_TOPIC, 100)) == 3 * len(EXTENDED_STAGE_ORDER)
    assert len(run.bus.poll("audit", ROUND_TOPIC, 100)) == 3
    # the accumulated research window: 3 days, then 6, then still 6 (round 2 is after it)
    ingest = [_stage(r, "ingest").summary for r in run.records]
    assert [i["research_bars"] for i in ingest] == [3 * 1440, 3 * 1440, 0]
    assert [i["accumulated_research_bars"] for i in ingest] == [3 * 1440, 6 * 1440, 6 * 1440]
    assert [i["sealed_bars_withheld"] for i in ingest] == [0, 0, 1440]
    assert [i["unused_bars"] for i in ingest] == [0, 0, 2 * 1440]
    # P2 state through run_state over F4 feature values, every round
    for record in run.records:
        state = _stage(record, "state").summary
        assert state["state"] == str(fx.TREND.ref) and state["evaluated"] > 0
        assert state["state_result_hash"] and state["feature_result_hashes"]
    # round 0: INCONCLUSIVE on the walk-forward coverage only
    first = _stage(run.records[0], "validation").summary["reports"]
    assert _reports(run.records[0]) == [(H60, "hypothesis", "INCONCLUSIVE")]
    assert {g["gate_id"].split(".")[0] for g in first[0]["gates"]} == {"G0", "G1", "G2", "G3", "G4"}
    not_pass = [g for g in first[0]["gates"] if g["verdict"] != "PASS"]
    assert [(g["gate_id"], g["verdict"]) for g in not_pass] == [
        ("G4.walk_forward.positive_fraction", "INCONCLUSIVE")
    ]
    [result] = [v for v in run.memory.validations if v.round_index == 0]
    assert result.report is not None
    [gate] = [g for g in result.report.gates if g.verdict is Verdict.INCONCLUSIVE]
    assert gate.metric == "walk_forward_windows_without_returns" and gate.value > 0
    assert first[0]["sealed_oos"]["status"] == "not_run"  # G5 needs an in-sample PASS
    assert [(t.subject.name, t.to_state) for t in run.records[0].transitions] == [
        ("h_k_tsmom_lookback_60", LifecycleState.CANDIDATE),
        ("h_k_tsmom_lookback_60", LifecycleState.VALIDATION),
    ]
    # round 1: the whole research window; the re-evaluation passes every G0 - G4 gate -> OOS
    assert _stage(run.records[1], "hypothesis").summary["reevaluations"] == [H60]
    assert _reports(run.records[1]) == [
        (H240, "hypothesis", "PASS"),
        (H60, "reevaluation", "PASS"),
        (CHILD, "evolution", "FAIL"),
    ]
    [again] = [v for v in run.memory.validations if v.round_index == 1 and v.outcome.attempt]
    assert again.outcome.hypothesis.ref == result.outcome.hypothesis.ref
    assert again.outcome.attempt == "loop_round:synthetic_loop:1"
    assert again.report is not None and again.report.verdict is Verdict.PASS
    assert {g.verdict for g in again.report.gates} == {Verdict.PASS}
    assert _stage(run.records[1], "memory").summary["moved_to_oos"] == [H240, H60]
    [history] = [h for h in run.loop.guard.histories if str(h.subject) == H60]
    assert [(t.from_state, t.to_state) for t in history.transitions] == [
        (LifecycleState.IDEA, LifecycleState.CANDIDATE),
        (LifecycleState.CANDIDATE, LifecycleState.VALIDATION),
        (LifecycleState.VALIDATION, LifecycleState.OOS),
    ]
    assert f"validation_report:{again.report.report_id}" in history.transitions[-1].evidence
    # round 2: no new research data -> no re-evaluation; the fresh hypothesis is refuted
    assert _stage(run.records[2], "hypothesis").summary["reevaluations"] == []
    assert _reports(run.records[2]) == [(H1440, "hypothesis", "FAIL")]
    states = {str(h.subject): h.current_state for h in run.loop.guard.histories}
    assert states == {
        H60: LifecycleState.OOS,
        H240: LifecycleState.OOS,
        CHILD: LifecycleState.REJECTED,
        H1440: LifecycleState.REJECTED,
    }
    # LLM drafts only wait for a human
    assert run.memory.reviews.pending == ("h_llm_0@1.0.0", "h_llm_1@1.0.0", "h_llm_2@1.0.0")
    assert run.loop.total_usage.llm_cost_units == Decimal(3)


def test_every_look_at_the_accumulated_data_is_a_registered_trial(planted: Run) -> None:
    """Re-evaluating the same (growing) data is paid in trials: the family count G3 corrects for
    grows with every (hypothesis, round) evaluation, and each report binds its own trial index."""
    run = planted
    log = run.memory.ledger.trial_log
    assert [(f"hypothesis:{e.name}@{e.version}", e.attempt) for e in log] == [
        (H60, None),
        (H240, None),
        (H60, "loop_round:synthetic_loop:1"),
        (CHILD, None),
        (H1440, None),
    ]
    assert run.memory.ledger.trials(fx.FAMILY) == 5 == run.loop.total_usage.trials
    counts = [_stage(r, "hypothesis").summary["family_trials"] for r in run.records]
    assert counts == [1, 3, 5]  # after the hypothesis stage (round 1's offspring comes later)
    reports = [r for record in run.records for r in _stage(record, "validation").summary["reports"]]
    assert [(r["trial_index"], r["family_trial_count"]) for r in reports] == [
        (1, 1),
        (2, 4),
        (3, 4),
        (4, 4),
        (5, 5),
    ]
    for result in run.memory.validations:  # the report's metadata carries the same numbers
        assert result.report is not None
        index = run.memory.ledger.trial_index(result.outcome.hypothesis, result.outcome.attempt)
        assert result.summary["trial_index"] == index


def test_refuted_and_oos_hypotheses_are_never_evaluated_again(planted: Run, noise: Run) -> None:
    for run in (planted, noise):
        looks: dict[str, list[tuple[int, Verdict | None]]] = {}
        for result in run.memory.validations:
            key = str(result.outcome.hypothesis.ref)
            looks.setdefault(key, []).append((result.round_index, result.verdict))
        for key, seen in looks.items():
            rounds = [r for r, _ in seen]
            assert rounds == sorted(set(rounds)), key  # at most one evaluation per round
            # only an INCONCLUSIVE look may be followed by another one
            assert all(verdict is Verdict.INCONCLUSIVE for _, verdict in seen[:-1]), key
        rejected = {
            str(r.subject_ref)
            for r in run.memory.failures.records()
            if r.terminal_state == "REJECTED"
        }
        for key in rejected:
            assert looks[key][-1][1] is Verdict.FAIL
    assert [r.outcome.attempt for r in noise.memory.validations] == [None, None, None]


def test_only_open_hypotheses_with_new_data_are_reevaluated(tmp_path: Path) -> None:
    """``reevaluation_candidates``: VALIDATION + INCONCLUSIVE + grown data; never REJECTED /
    FAILED / OOS / CANDIDATE subjects, never a subject with a REJECTED failure record."""
    loop, memory, _ = fx.build(
        tmp_path, fx.config(lookbacks=(60,), loop_wiring=fx.wiring(evolution=False))
    )
    [record] = loop.run_unattended(1)
    [result] = memory.validations
    assert result.verdict is Verdict.INCONCLUSIVE
    hypothesis, trial = result.outcome.hypothesis, result.outcome
    assert trial.inputs is not None
    seen_until = trial.inputs.knowledge_cutoff
    later = seen_until + timedelta(days=3)
    assert loop.guard.state_of(hypothesis.ref) is LifecycleState.VALIDATION
    assert reevaluation_candidates(memory, loop.guard.state_of, later, 1) == (hypothesis,)
    assert reevaluation_candidates(memory, loop.guard.state_of, later, 0) == ()
    assert reevaluation_candidates(memory, loop.guard.state_of, seen_until, 1) == ()
    assert reevaluation_candidates(memory, loop.guard.state_of, None, 1) == ()
    for state in (
        LifecycleState.CANDIDATE,
        LifecycleState.OOS,
        LifecycleState.REJECTED,
        LifecycleState.FAILED,
    ):

        def pinned(_ref: Ref, pinned_state: LifecycleState = state) -> LifecycleState:
            return pinned_state

        assert reevaluation_candidates(memory, pinned, later, 1) == ()
    memory.failures.append(
        FailureRecord(
            subject_ref=hypothesis.ref,
            terminal_state="REJECTED",
            reason_code=ReasonCode.BENCHMARK_NOT_BEATEN,
            gate_id="G2.null_model_percentile",
            evidence=("test-only",),
            hypothesis_family_id=hypothesis.family_id,
            recorded_at=fx.T0,
        )
    )
    assert reevaluation_candidates(memory, loop.guard.state_of, later, 1) == ()
    assert record.status is RoundStatus.COMPLETED


def test_sealed_bars_never_enter_the_research_data(planted: Run) -> None:
    run = planted
    for piece in run.memory.research_data:
        assert all(bar.interval_end <= BOUNDARY for bar in piece.bars)
    assert [p.round_index for p in run.memory.research_data] == [0, 1]
    sealed_starts = {
        bar.interval_start
        for bar in run.memory.markets[2].bars
        if BOUNDARY <= bar.interval_start < BOUNDARY + timedelta(days=1)
    }
    assert len(sealed_starts) == 1440
    for outcome in run.memory.trials:
        assert outcome.inputs is not None
        assert outcome.inputs.knowledge_cutoff <= BOUNDARY
        assert all(bar.available_time <= BOUNDARY for bar in outcome.inputs.bars)
        assert all(s.available_time <= BOUNDARY for s in outcome.inputs.signals)
        assert not sealed_starts & {bar.interval_start for bar in outcome.inputs.bars}
        assert all(t + fx.LABEL_SPEC.horizon < BOUNDARY for t in outcome.inputs.decision_times)
        for snapshot in outcome.run.repro.dataset_snapshots:
            assert snapshot.time_range_end is not None and snapshot.time_range_end <= BOUNDARY
    assert run.memory.oos_ledger.count() == 0  # nothing was ever unsealed


def test_pure_noise_passes_nothing(noise: Run) -> None:
    run = noise
    assert [_reports(r) for r in run.records] == [
        [(H60, "hypothesis", "FAIL")],
        [(H240, "hypothesis", "FAIL")],
        [(H1440, "hypothesis", "FAIL")],
    ]
    assert [r.status for r in run.records] == [RoundStatus.COMPLETED] * 3
    verdicts = [v.verdict for v in run.memory.validations]
    assert verdicts and Verdict.PASS not in verdicts
    assert LifecycleState.OOS not in {t.to_state for t in _transitions(run)}
    assert run.memory.offspring == []  # nothing un-refuted to evolve from
    rejected = [r for r in run.memory.failures.records() if r.terminal_state == "REJECTED"]
    assert len(rejected) == sum(1 for v in verdicts if v is Verdict.FAIL)


def test_same_seed_gives_identical_audit_hashes(planted: Run, tmp_path: Path) -> None:
    again = _run(tmp_path / "again")
    assert [r.record_hash for r in again.records] == [r.record_hash for r in planted.records]
    other_loop, _, _ = fx.build(tmp_path / "other", fx.config(seed=12))
    [other] = other_loop.run_unattended(1)
    assert other.record_hash != planted.records[0].record_hash


#: Pinned at 1fb7918 (before the opt-in ``ConditionalPlan`` existed): the default planted run's
#: record hashes and its configuration fingerprint. A loop without the plan must reproduce them
#: byte for byte. A deliberate change elsewhere that alters these records (another stage's
#: summary, a component's result) re-pins them in the same commit, stating why.
#: Re-pinned for ADR-0060 enforcement (2026-09-26): ``ValidationStage`` now validates with
#: ``market_benchmark=True`` and the TEST ONLY Profile names ``buy_and_hold_equal_weight`` with the
#: inverse control reported (was the unregistered placeholder ``"test-only"``), so every report
#: carries the reported-only ``G2.market_benchmark.*`` / ``G2.inverse_control`` items and the
#: configuration fingerprint (which binds the Profile) changed.
#: Re-pinned for contract 2.1.0 (ADR-0052 §4, 2026-09-26): the intended envelope change only —
#: every newly built contract object is 2.1.0 and the envelope is part of each content hash.
#: The previous values still hold when the same test builds every object at 2.0.0
#: (verified by running it inside ``contract_schema_version_scope("2.0.0")``).
#: 2.0.0 values (evidence, git history): 9b5e9e8c…, 96e58aff…, 1df0bc1c…; fingerprint f7a137b2…
#: Re-pinned for contract 2.2.0 (ADR-0055, 2026-09-26): envelope change only; the 2.1.0
#: values still hold when the test builds every object at 2.1.0 (verified: the unmodified
#: test passes inside ``contract_schema_version_scope("2.1.0")``).
#: 2.1.0 values (evidence, git history): e241ceb2…, 86b2adda…, a0dc0b91…; fingerprint 175c1a47…
#: Re-pinned for ADR-0100 修订 2 (``ca8bd57``, 2026-10-01): each new run records its
#: same-source inputs (``hlens.p11.inputs@1.0.0``) in the hashed ``repro.params`` and the
#: experiment row's ``run_inputs`` — the ADR states the record enters the run's content hash —
#: and that row renders its floats as decimal text (W1 fix: no float in a hashed record). The
#: 2.2.0 values below held up to ``ca8bd57^`` (verified by bisect, 2026-10-01).
#: 2.2.0 values (evidence, git history): cd8e1512…, c94401f4…, 9acaa76b…
#: Re-pinned for contract 2.6.0 (ADR-0109, 2026-10-02): envelope change only; the 2.5.0 values
#: still hold when the unmodified test runs with every object (imports included) built inside
#: ``contract_schema_version_scope("2.5.0")`` (verified).
#: 2.5.0 values (evidence, git history): 6dca257d…, eef9f1d7…, 503116d5…; fingerprint 196485af…
PINNED_RECORD_HASHES = [
    "7f82a54ea805788e3fcb791fa39c17d2c99994540773123a5f0f494ff9ba5da4",
    "9dfd9ed61a33d1c1c8dd819143950d6777024c51a2cd263a9b6867a5cad29c38",
    "9cd2c06322ac7beec5414709d22a58e420993a90f43e79ae313270e1abfdadff",
]
#: Fingerprint re-pinned for contracts 2.3.0 – 2.5.0: envelope change only — built entirely at
#: 2.2.0 (scope entered before any import) it is still ``fbbd152b…`` (verified 2026-10-01).
#: Re-pinned for 2.6.0 with the record hashes above (was ``196485af…``, verified at 2.5.0).
PINNED_FINGERPRINT_HASH = "2d3f585449647c9ea715591119a1f9f38fd43b699cd0b837351db7d8a435dd42"


def test_records_without_a_conditional_plan_are_pinned(planted: Run) -> None:
    assert fx.wiring().conditional is None
    assert [r.record_hash for r in planted.records] == PINNED_RECORD_HASHES
    assert content_hash(loop_fingerprint(fx.config())) == PINNED_FINGERPRINT_HASH
    for record in planted.records:  # no conditional key, no conditional trial, no charge
        experiment = _stage(record, "experiment")
        assert all("conditional" not in row for row in experiment.summary["experiments"])
        assert experiment.estimate.trials == 0 == experiment.usage.trials
    assert not any("_given_" in h.name for h in planted.memory.ledger.hypotheses)


def test_every_trial_report_carries_the_market_benchmark(planted: Run) -> None:
    """ADR-0060 enforced in the loop: the TEST ONLY Profile's registered rule and inverse control
    are computed for every validated trial that reached G2 (reported only, ``PASS`` = computed);
    the unregistered-rule gap ``G2.market_benchmark`` never appears."""
    items = {"G2.market_benchmark.buy_and_hold_equal_weight", "G2.inverse_control"}
    reached = 0
    for result in planted.memory.validations:
        assert result.report is not None
        by_id = {g.gate_id: g for g in result.report.gates}
        assert "G2.market_benchmark" not in by_id
        if not any(gate_id.startswith("G3.") for gate_id in by_id):
            continue  # stopped before G2 completed: nothing is computed
        reached += 1
        assert items <= set(by_id)
        assert all(by_id[gate_id].verdict is Verdict.PASS for gate_id in items)
        assert by_id["G2.market_benchmark.buy_and_hold_equal_weight"].threshold is None
    assert reached > 0


def test_hashed_records_hold_no_floats(planted: Run, noise: Run) -> None:
    for run in (planted, noise):
        for record in run.records:
            assert list(_floats(record.payload())) == []


def test_every_run_with_a_strategy_records_its_run_inputs(planted: Run) -> None:
    """ADR-0100 修订 2 (tested per ADR-0105 §7): each run records, in its hashed
    ``repro.params``, exactly the decision grid, equity and validation inputs the round used —
    the ``hlens.p11.inputs@1.0.0`` record the P11 authority compares — and the experiment row
    carries the same record (floats as decimal text); the strategy params stay apart."""
    wiring = fx.wiring()
    rows = {row["run_id"]: row for row in planted.memory.experiments}
    checked = 0
    for trial in planted.memory.trials:
        params = trial.run.repro.params
        recorded = recorded_run_inputs(params)
        if trial.candidate is None:
            assert recorded is None and RUN_INPUTS_KEY not in params
            continue
        assert recorded is not None
        checked += 1
        assert (recorded.decision_step, recorded.decision_warmup) == (
            wiring.decision_step,
            wiring.decision_warmup,
        )
        assert str(recorded.initial_equity) == str(wiring.initial_equity)
        assert recorded.validation_seed == trial.validation_seed
        assert recorded.validation_seed in trial.run.repro.seeds
        assert recorded.control_seeds == loop_trials.VALIDATION_CONTROL_SEEDS
        assert recorded.cscv_partitions == wiring.robustness.cscv_partitions
        assert recorded.impact_coefficient == wiring.robustness.impact_coefficient
        assert recorded.state_labeller is not None
        assert recorded.state_labeller["format"] == STATE_LABELLER_FORMAT
        family = planted.memory.ledger.trials(trial.hypothesis.family_id)
        assert 1 <= recorded.family_trial_count <= family
        # the record is bound into the run's content hash; the strategy params are apart
        unrecorded = trial.run.repro.model_copy(update={"params": strategy_params(params)})
        assert unrecorded.experiment_hash != trial.run.experiment_hash
        row = rows[trial.run.run_id]
        assert row["run_inputs"]["execution"] == recorded.execution_payload()
        assert row["run_inputs"]["format"] == recorded.payload()["format"]
        assert set(row["params"]) == set(strategy_params(params))
        assert RUN_INPUTS_KEY not in row["params"]
    assert checked > 0


# ---------------------------------------------------------------------------------------- budget


def test_budget_exhaustion_stops_the_loop_before_evolution(tmp_path: Path) -> None:
    # round 0: 1 trial; round 1: a new hypothesis + the re-evaluation of round 0's INCONCLUSIVE
    # one (2 trials: 3 in total), then the offspring would be the 4th
    budget = LoopBudget(
        max_trials_per_round=3,
        max_trials_total=3,
        max_llm_cost_units=Decimal(10),
        max_compute_seconds=Decimal(1000),
    )
    run = _run(tmp_path, rounds=5, budget=budget)
    assert [r.status for r in run.records] == [RoundStatus.COMPLETED, RoundStatus.BUDGET_EXHAUSTED]
    last = run.records[-1]
    assert _stage(last, "hypothesis").status is StageStatus.COMPLETED
    assert _stage(last, "hypothesis").summary["registered"] == [H240]
    assert _stage(last, "hypothesis").summary["reevaluations"] == [H60]
    refused = _stage(last, "evolution")
    assert refused.status is StageStatus.REFUSED_BUDGET and refused.refused == ("max_trials_total",)
    assert _stage(last, "experiment").status is StageStatus.SKIPPED
    assert run.loop.halted is RoundStatus.BUDGET_EXHAUSTED
    assert run.memory.ledger.trials(fx.FAMILY) == 3 == run.loop.total_usage.trials
    assert run.memory.offspring == []  # the refused stage registered nothing


# ------------------------------------------------------------------------------------- lifecycle


def test_no_paper_or_active_transition_ever(planted: Run, noise: Run) -> None:
    for run in (planted, noise):
        transitions = _transitions(run)
        assert transitions
        states = {t.to_state for t in transitions}
        assert states <= AUTOMATABLE_TARGETS and states.isdisjoint(FORBIDDEN_TARGETS)
        assert all(t.approved_by is None for t in transitions)
        assert {t.triggered_by for t in transitions} == {run.loop.guard.actor}


# ------------------------------------------------------------------------------ trials / records


def test_every_trial_is_preregistered_recorded_and_reproducible(planted: Run) -> None:
    memory = planted.memory
    registered = {(h.name, h.version): h.content_hash() for h in memory.ledger.hypotheses}
    log = memory.ledger.trial_log
    assert len(memory.trials) == len(log) == len(memory.experiments)
    assert [(o.hypothesis.name, o.attempt) for o in memory.trials] == [
        (e.name, e.attempt) for e in log
    ]
    for outcome in memory.trials:
        hypothesis, repro = outcome.hypothesis, outcome.run.repro
        assert registered[(hypothesis.name, hypothesis.version)] == hypothesis.content_hash()
        assert memory.ledger.is_registered(hypothesis, outcome.attempt)
        assert outcome.run.experiment_hash == repro.content_hash()
        assert outcome.experiment.experiment_hash == repro.content_hash()
        assert outcome.run.state is RunState.COMPLETED and outcome.trial is not None
        # the reproducibility tuple of 06-experiment.md §2
        assert repro.hypothesis_ref == hypothesis.ref
        # the accumulated research data: one snapshot per contributing market, oldest first
        pieces = [p for p in memory.research_data if p.round_index <= outcome.round_index]
        assert [
            (s.snapshot_id, s.time_range_start, s.time_range_end) for s in repro.dataset_snapshots
        ] == [
            (p.market.market_hash, p.bars[0].interval_start, p.bars[-1].interval_end)
            for p in pieces
        ]
        assert outcome.inputs is not None
        assert len(outcome.inputs.bars) == sum(len(p.bars) for p in pieces)
        assert outcome.summary["market_hash"] == memory.markets[outcome.round_index].market_hash
        assert repro.code_commit == fx.CODE_COMMIT
        assert repro.validation_profile_hash == fx.loop_profile().content_hash()
        assert {"bar_log_return@1.0.0", "trend_range@1.0.0", "research_tsmom@0.1.0"} <= set(
            repro.plugin_versions
        )
        assert repro.params["lookback"] in (60, 240, 1440)
        assert len(repro.seeds) == 2
        # re-running the recorded point reproduces the recorded backtest exactly
        assert outcome.candidate is not None and outcome.inputs is not None
        rerun = CandidateTrialRunner(outcome.candidate, outcome.inputs, fx.wiring().backtester)
        again = rerun.run(dict(outcome.request_params))
        assert again.backtest.result_hash == outcome.trial.backtest.result_hash
        assert outcome.summary["state_strategy_matrix_hash"]
    # every refutation was filed, failures included, nothing dropped
    fails = [v for v in memory.validations if v.verdict is Verdict.FAIL]
    assert len(memory.failures.records()) == len(fails)


def test_offspring_are_new_versions_registered_and_revalidated(planted: Run) -> None:
    memory = planted.memory
    assert memory.offspring, "the planted run must evolve at least one candidate"
    specs: dict[str, StrategySpec] = {str(s.ref): s for s in memory.lineage}
    graph = LineageGraph(memory.lineage)
    reports = {str(v.outcome.hypothesis.ref): v for v in memory.validations}
    for row in memory.offspring:
        parent, child = specs[row["parent"]], specs[row["child"]]
        require_new_version(parent, child)  # accepted: a new version with lineage
        assert child.version != parent.version and parent.ref in child.lineage
        assert parent.ref in graph.ancestors(child.ref)
        hypothesis = next(h for h in memory.ledger.hypotheses if str(h.ref) == row["hypothesis"])
        assert hypothesis.origin is HypothesisOrigin.COMBINATION
        # validated afresh: its own report about the child spec, never the parent's verdict
        own = reports[row["hypothesis"]]
        assert own.round_index == row["round"] and own.report is not None
        assert own.report.subject == child.ref
        assert own.report.report_id != row["parent_report_id"]
        history = next(
            h for h in planted.loop.guard.histories if str(h.subject) == row["hypothesis"]
        )
        assert history.transitions[0].from_state is LifecycleState.IDEA
        assert history.transitions[0].to_state is LifecycleState.CANDIDATE


# -------------------------------------------------------------------------------------- sealed


def _sealed_config(unseal: OosUnsealBudget | None) -> Any:
    # one 4-day round: research days 0-3, then the sealed OOS day 3-4 of the Profile. These loops
    # are in memory: the unseal budget carries the TEST-ONLY ephemeral-ledger flag (review fixes 4)
    return fx.config(
        lookbacks=(60,),
        days_per_round=4,
        profile=fx.loop_profile(boundary_day=3),
        loop_wiring=fx.wiring(
            evolution=False,
            oos_unseal=None if unseal is None else replace(unseal, ephemeral_unseal_for_tests=True),
        ),
    )


def test_sealed_oos_stays_sealed_without_an_unseal_budget(tmp_path: Path) -> None:
    loop, memory, _ = fx.build(tmp_path, _sealed_config(None))
    [record] = loop.run_unattended(1)
    assert _stage(record, "ingest").summary["sealed_bars_withheld"] == 1440
    [result] = memory.validations
    assert result.verdict is Verdict.PASS and result.sealed_report is None
    assert result.sealed_status["status"] == "sealed"
    assert result.sealed_status["reason"] == "no unseal budget configured"
    assert memory.oos_ledger.count() == 0
    # R19: OOS = eligible for / undergoing the sealed evaluation. Without a G5 PASS nothing
    # beyond OOS happens, and the move into OOS cites the in-sample report.
    _assert_ends_in_oos_on_in_sample_evidence(loop, result, record)
    assert not memory.failures.records()


def _assert_ends_in_oos_on_in_sample_evidence(loop: Any, result: Any, record: Any) -> None:
    [history] = [h for h in loop.guard.histories if h.subject == result.outcome.hypothesis.ref]
    assert [(t.from_state, t.to_state) for t in history.transitions] == [
        (LifecycleState.IDEA, LifecycleState.CANDIDATE),
        (LifecycleState.CANDIDATE, LifecycleState.VALIDATION),
        (LifecycleState.VALIDATION, LifecycleState.OOS),
    ]
    move = history.transitions[-1]
    assert f"validation_report:{result.report.report_id}" in move.evidence
    assert "in-sample" in move.reason and "eligible for the sealed OOS evaluation" in move.reason
    if result.sealed_report is not None:
        assert f"validation_report:{result.sealed_report.report_id}" not in move.evidence
    assert _stage(record, "memory").summary["moved_to_oos"] == [str(result.outcome.hypothesis.ref)]


def test_a_family_not_on_the_approved_list_is_never_unsealed(tmp_path: Path) -> None:
    unseal = OosUnsealBudget(max_unsealings=5, approved_families={"another_family": "test-human"})
    loop, memory, _ = fx.build(tmp_path, _sealed_config(unseal))
    [record] = loop.run_unattended(1)
    [result] = memory.validations
    assert result.verdict is Verdict.PASS and result.sealed_report is None
    assert result.sealed_status == {
        "status": "sealed",
        "reason": "the family is not on the unseal budget's approved list",
    }
    assert memory.oos_ledger.count() == 0 and memory.oos_ledger.get(fx.FAMILY) is None
    _assert_ends_in_oos_on_in_sample_evidence(loop, result, record)


def _assert_consumed_without_result(loop: Any, memory: ResearchMemory, reason: str) -> None:
    [result] = memory.validations
    assert result.verdict is Verdict.PASS
    sealed = result.sealed_report
    assert sealed is not None and sealed.verdict is Verdict.INCONCLUSIVE
    assert [(g.gate_id, g.verdict, g.metric) for g in sealed.gates] == [
        ("G5.unsealing_recorded", Verdict.PASS, "oos_unsealing_recorded"),
        (
            "G5.oos_evaluation",
            Verdict.INCONCLUSIVE,
            f"consumed_without_result:{reason.replace(' ', '_')}",
        ),
    ]
    assert result.sealed_status["status"] == "consumed_without_result"
    assert result.sealed_status["reason"] == reason
    assert result.sealed_status["approved_by"] == "test-human"
    assert result.sealed_status["unseal_ledger"] == EPHEMERAL_UNSEAL_MARK  # never real use
    # the single evaluation is consumed: nobody can unseal or read the window again
    assert memory.oos_ledger.count() == 1 and memory.oos_ledger.is_evaluated(fx.FAMILY)
    vault = SealedOosVault(fx.loop_profile(boundary_day=3), memory.oos_ledger, max_unsealings=5)
    with pytest.raises(SealedOosAlreadyEvaluated):
        vault.sealed_view(fx.FAMILY, [])
    with pytest.raises(SealedOosAlreadyEvaluated):
        vault.claim_evaluation(fx.FAMILY)
    with pytest.raises(OosAlreadyUnsealed):
        vault.unseal(fx.FAMILY, "test-human", fx.T0)
    # INCONCLUSIVE G5: the subject stays in OOS, no failure record
    assert loop.guard.state_of(result.outcome.hypothesis.ref) is LifecycleState.OOS
    assert not memory.failures.records()


def test_no_sealed_decision_time_still_consumes_the_single_evaluation(tmp_path: Path) -> None:
    unseal = OosUnsealBudget(max_unsealings=1, approved_families={fx.FAMILY: "test-human"})
    config = _sealed_config(unseal)
    # a sealed decision step longer than the 1-day sealed window: no sealed decision time
    wiring = replace(config.wiring, sealed_decision_step=timedelta(days=2))
    loop, memory, _ = fx.build(tmp_path, replace(config, wiring=wiring))
    [record] = loop.run_unattended(1)
    assert record.status is RoundStatus.COMPLETED
    _assert_consumed_without_result(loop, memory, "no sealed decision time")
    assert memory.validations[0].sealed_status["decisions"] == 0


class _FlatInTheSealedWindow(CandidateTrialRunner):
    """TEST ONLY collaborator stub: the strategy stays flat in the sealed run (the one whose
    knowledge cutoff lies after the sealed boundary); in-sample runs are untouched."""

    def run(self, params: Any, **kwargs: Any) -> Any:
        trial = super().run(params, **kwargs)
        boundary = fx.T0 + timedelta(days=3)
        if self.inputs.knowledge_cutoff <= boundary:
            return trial
        flat = tuple(t.model_copy(update={"target_weight": Decimal(0)}) for t in trial.targets)
        return replace(trial, targets=flat)


def test_no_non_flat_sealed_target_still_consumes_the_single_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loop_trials, "CandidateTrialRunner", _FlatInTheSealedWindow)
    unseal = OosUnsealBudget(max_unsealings=1, approved_families={fx.FAMILY: "test-human"})
    loop, memory, _ = fx.build(tmp_path, _sealed_config(unseal))
    [record] = loop.run_unattended(1)
    assert record.status is RoundStatus.COMPLETED
    _assert_consumed_without_result(loop, memory, "no non-flat target in the sealed window")
    assert memory.validations[0].sealed_status["decisions"] > 0


def test_an_explicit_unseal_budget_runs_g5_once_per_family(tmp_path: Path) -> None:
    unseal = OosUnsealBudget(max_unsealings=1, approved_families={fx.FAMILY: "test-human"})
    loop, memory, _ = fx.build(tmp_path, _sealed_config(unseal))
    [record] = loop.run_unattended(1)
    [result] = memory.validations
    assert result.verdict is Verdict.PASS and result.sealed_report is not None
    assert [g.gate_id for g in result.sealed_report.gates] == [
        "G5.unsealing_recorded",
        "G5.oos_effective_sample_size",
        "G5.oos_breakeven_cost_multiple",
    ]
    assert memory.oos_ledger.count() == 1 and memory.oos_ledger.is_evaluated(fx.FAMILY)
    unsealing = memory.oos_ledger.get(fx.FAMILY)
    assert unsealing is not None and unsealing.approved_by == "test-human"
    # the TEST-ONLY ephemeral ledger is visible in the audit: the G5 status and the stage summary
    assert result.sealed_status["unseal_ledger"] == EPHEMERAL_UNSEAL_MARK
    assert _stage(record, "validation").summary["unseal_ledger"] == EPHEMERAL_UNSEAL_MARK
    [row] = _stage(record, "validation").summary["reports"]
    assert row["sealed_oos"]["unseal_ledger"] == EPHEMERAL_UNSEAL_MARK
    vault = SealedOosVault(fx.loop_profile(boundary_day=3), memory.oos_ledger, max_unsealings=1)
    with pytest.raises(OosAlreadyUnsealed):
        vault.unseal(fx.FAMILY, "test-human", fx.T0)
    # at most OOS: a G5 pass stays in OOS (OOS -> PAPER needs a human)
    assert result.sealed_report.verdict is Verdict.PASS
    assert loop.guard.state_of(result.outcome.hypothesis.ref) is LifecycleState.OOS
    _assert_ends_in_oos_on_in_sample_evidence(loop, result, record)
    with pytest.raises(SealedOosAlreadyEvaluated):  # the one evaluation was used
        vault.sealed_view(fx.FAMILY, [])
    with pytest.raises(ValueError, match="human"):
        OosUnsealBudget(max_unsealings=1, approved_families={fx.FAMILY: loop.guard.actor})
    assert record.status is RoundStatus.COMPLETED


# --------------------------------------- durable unsealing ledger (review fixes 4, 2026-09-26)


def _unseal_budget(*, ephemeral: bool = False) -> OosUnsealBudget:
    return OosUnsealBudget(
        max_unsealings=1,
        approved_families={fx.FAMILY: "test-human"},
        ephemeral_unseal_for_tests=ephemeral,
    )


def _budget_config(budget: OosUnsealBudget) -> Any:
    """``_sealed_config`` with ``budget`` exactly as given (no flag added)."""
    config = _sealed_config(None)
    wiring = replace(config.wiring, oos_unseal=budget, sealed_decision_step=fx.HOUR)
    return replace(config, wiring=wiring)


def _llm() -> ScriptedLLMProvider:
    return ScriptedLLMProvider(
        [fx.llm_output(i, lookback) for i, lookback in enumerate((240, 1440, None))],
        clock=lambda: fx.T0,
    )


def _memory(tmp_path: Path, ledger: Any = None) -> ResearchMemory:
    failures = FailureRegistry(tmp_path / "failures.jsonl")
    if ledger is None:
        return ResearchMemory(failures=failures)
    return ResearchMemory(failures=failures, oos_ledger=ledger)


def test_an_unseal_budget_needs_a_durable_unsealing_ledger(tmp_path: Path) -> None:
    """An in-memory ledger forgets its unsealings on a restart: refused without the flag."""
    with pytest.raises(ValueError, match="durable unsealing ledger"):
        fx.build(tmp_path, _budget_config(_unseal_budget()))
    # an explicitly passed durable ledger: G5 runs, and nothing marks the run as test-only
    memory = _memory(tmp_path, DurableUnsealingLedger(tmp_path / "sealed_oos.jsonl"))
    loop = build_synthetic_loop(
        _budget_config(_unseal_budget()),
        provider=RandomWalkMarket(),
        bus=InMemoryEventBus(),
        memory=memory,
        llm=_llm(),
    )
    [record] = loop.run_unattended(1)
    [result] = memory.validations
    assert result.sealed_report is not None and result.sealed_status["status"] == "unsealed"
    assert "unseal_ledger" not in result.sealed_status
    assert "unseal_ledger" not in _stage(record, "validation").summary
    assert memory.oos_ledger.count() == 1 and memory.oos_ledger.is_evaluated(fx.FAMILY)


def test_the_ledger_is_rechecked_before_every_unsealing(tmp_path: Path) -> None:
    memory = _memory(tmp_path, DurableUnsealingLedger(tmp_path / "sealed_oos.jsonl"))
    loop = build_synthetic_loop(
        _budget_config(_unseal_budget()),
        provider=RandomWalkMarket(),
        bus=InMemoryEventBus(),
        memory=memory,
        llm=_llm(),
    )
    memory.oos_ledger = InMemoryUnsealingLedger()  # swapped after composition
    [record] = loop.run_unattended(1)
    validation = _stage(record, "validation")
    assert validation.status is StageStatus.FAILED
    assert "durable unsealing ledger" in (validation.error or "")
    assert memory.oos_ledger.count() == 0 and not memory.oos_ledger.is_evaluated(fx.FAMILY)


def test_the_test_only_ephemeral_flag_is_explicit_and_visible(tmp_path: Path) -> None:
    flagged = _budget_config(_unseal_budget(ephemeral=True))
    # visible in the fingerprint (only when set: every durable fingerprint is unchanged)
    assert loop_fingerprint(flagged)["oos_unseal"]["ephemeral_unseal_for_tests"] is True
    plain = loop_fingerprint(_budget_config(_unseal_budget()))["oos_unseal"]
    assert "ephemeral_unseal_for_tests" not in plain
    # refused with a durable ledger, and with a state directory (before anything is written)
    with pytest.raises(ValueError, match="drop the flag"):
        build_synthetic_loop(
            flagged,
            provider=RandomWalkMarket(),
            bus=InMemoryEventBus(),
            memory=_memory(tmp_path, DurableUnsealingLedger(tmp_path / "sealed_oos.jsonl")),
            llm=_llm(),
        )
    state_dir = tmp_path / "state"
    with pytest.raises(ValueError, match="TEST-ONLY"):
        open_synthetic_loop(
            flagged, state_dir=state_dir, provider=RandomWalkMarket(), bus=InMemoryEventBus()
        )
    assert not state_dir.exists()
    with pytest.raises(ValueError, match="bool"):
        OosUnsealBudget(
            max_unsealings=1,
            approved_families={fx.FAMILY: "test-human"},
            ephemeral_unseal_for_tests=cast(Any, "yes"),
        )
    # in memory it works, and every record it touches says so (see also the G5 test above)
    loop, memory, _ = fx.build(tmp_path / "flagged", flagged)
    [record] = loop.run_unattended(1)
    [result] = memory.validations
    assert result.sealed_status["status"] == "unsealed"
    assert result.sealed_status["unseal_ledger"] == EPHEMERAL_UNSEAL_MARK
    assert _stage(record, "validation").summary["unseal_ledger"] == EPHEMERAL_UNSEAL_MARK


# ------------------------------- synthetic sealed bars stay with the ingest (review fixes 4)


class _RecordingSealed:
    """A round's ``SealedSource``, recording whether each release came after the claim."""

    def __init__(self, sealed: Any, memory: ResearchMemory, releases: list[bool]) -> None:
        self._sealed, self._memory, self._releases = sealed, memory, releases

    @property
    def window(self) -> Any:
        return self._sealed.window

    def evaluable(self, as_of: Any) -> str | None:
        result: str | None = self._sealed.evaluable(as_of)
        return result

    def release(self, evaluation: Any) -> Any:
        self._releases.append(self._memory.oos_ledger.is_evaluated(evaluation.family_id))
        return self._sealed.release(evaluation)


class _RoundDataOnly:
    """The ingest's segment with nothing but the ``RoundData`` protocol's members: any other
    attribute (the generated market, the research pieces, ...) raises."""

    MEMBERS = frozenset(get_protocol_members(RoundData))

    def __init__(self, segment: Any, sealed: _RecordingSealed) -> None:
        self._segment, self._sealed = segment, sealed

    def __getattr__(self, name: str) -> Any:
        if name not in self.MEMBERS:
            raise AttributeError(f"a stage read {name!r}, which is not part of RoundData")
        return self._sealed if name == "sealed" else getattr(self._segment, name)


class _ProtocolOnlyIngest:
    name = "ingest"

    def __init__(self, inner: Any, memory: ResearchMemory, releases: list[bool]) -> None:
        self._inner, self._memory, self._releases = inner, memory, releases

    def estimate(self, ctx: Any) -> Any:
        return self._inner.estimate(ctx)

    def run(self, ctx: Any) -> Any:
        result = self._inner.run(ctx)
        segment = result.artifacts["segment"]
        sealed = _RecordingSealed(segment.sealed, self._memory, self._releases)
        artifacts = {**result.artifacts, "segment": _RoundDataOnly(segment, sealed)}
        return replace(result, artifacts=artifacts)


def test_stages_after_the_ingest_see_only_the_round_data_protocol(tmp_path: Path) -> None:
    """The synthetic ingest generates the sealed bars with its market (not a read of real sealed
    data); every later stage — G5 included — runs over the RoundData protocol alone, and the
    sealed bars leave the segment once, after the family's evaluation was claimed."""
    config = _sealed_config(_unseal_budget())
    assert set(RoundData.__dict__) >= {"sealed", "research_bars", "feature_runs"}
    assert not {"market", "pieces", "research"} & _RoundDataOnly.MEMBERS
    loop, memory, _ = fx.build(tmp_path / "plain", config)
    [plain] = loop.run_unattended(1)
    releases: list[bool] = []
    guarded = _memory(tmp_path / "guarded")
    ingest = _ProtocolOnlyIngest(
        _synthetic_ingest(config, RandomWalkMarket(), guarded), guarded, releases
    )
    loop = compose_loop(config, ingest, InMemoryEventBus(), guarded, _llm(), None)
    [record] = loop.run_unattended(1)
    assert record.status is RoundStatus.COMPLETED
    assert all(stage.status is StageStatus.COMPLETED for stage in record.stages)
    assert releases == [True]  # one release, after the claim
    [result] = guarded.validations
    assert result.sealed_report is not None and result.sealed_status["status"] == "unsealed"
    assert record.record_hash == plain.record_hash  # the proxy changes nothing that is recorded


def test_no_stage_after_the_ingest_touches_the_generated_market() -> None:
    """Source scan: outside ``IngestStage`` the stage modules never access ``market`` /
    ``markets`` (the generated market, sealed bars included, stays with the ingest)."""
    root = Path(loop_trials.__file__).parent
    offenders: list[str] = []
    for module in ("stages.py", "trials.py", "evolution.py"):
        tree = ast.parse((root / module).read_text())
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "IngestStage":
                continue
            for item in ast.walk(node):
                if isinstance(item, ast.Attribute) and item.attr in {"market", "markets"}:
                    offenders.append(f"{module}:{item.lineno}")
    assert offenders == []


# ---------------------------------------------------------------------------------- LLM reviews


def test_reviewed_llm_drafts_are_registered_later_and_unrunnable_ones_fail(tmp_path: Path) -> None:
    loop, memory, _ = fx.build(
        tmp_path,
        fx.config(loop_wiring=fx.wiring(evolution=False), days_per_round=2),
        llm_lookbacks=(None, 240),
    )
    loop.run_unattended(1)
    with pytest.raises(ValueError, match="automation"):
        memory.reviews.approve("h_llm_0@1.0.0", reviewer=loop.guard.actor)
    with pytest.raises(ValueError, match="identity"):
        memory.reviews.approve("h_llm_0@1.0.0", reviewer="  ")
    memory.reviews.approve("h_llm_0@1.0.0", reviewer="test-human")  # outside the loop
    [approval] = memory.reviews.approvals
    assert approval.reviewer == "test-human" and approval.key == "h_llm_0@1.0.0"
    [record] = loop.run_unattended(1)
    assert record.status is RoundStatus.COMPLETED
    assert "hypothesis:h_llm_0@1.0.0" in _stage(record, "hypothesis").summary["registered"]
    assert "human_review:test-human" in [e for t in record.transitions for e in t.evidence]
    llm_trial = next(o for o in memory.trials if o.hypothesis.origin is HypothesisOrigin.LLM)
    assert llm_trial.run.state is RunState.ERRORED and len(llm_trial.run.repro.llm_calls) == 1
    failed = [r for r in memory.failures.records() if r.terminal_state == "FAILED"]
    assert [str(r.subject_ref) for r in failed] == ["hypothesis:h_llm_0@1.0.0"]
    assert loop.guard.state_of(llm_trial.hypothesis.ref) is LifecycleState.FAILED

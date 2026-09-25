"""ADR-0049 W2 e2e: unattended rounds of the loop on the real Phase 1 / 2 / 4 / 5 / 6 / 7 / 8 / 12
components (F4 features, P2 state provider via ``run_state``, P5 TSMOM + ``BarBacktester``,
P6 State × Strategy matrix, P7 ``TrialLedger``, P4 / P8 ``PipelineBacktestValidator`` G0 – G4,
P12 evolution operators) on a synthetic market with a planted effect, versus pure noise.

TEST ONLY: every budget, cost unit, model parameter and Profile number comes from
``loop_fixtures`` and is arbitrary and uncalibrated (see its docstring). Synthetic results support
no claim about real markets (roadmap Phase 9).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from apps.worker import AUTOMATABLE_TARGETS, LoopBudget, ResearchLoop, RoundStatus, StageStatus
from apps.worker.loop import EXTENDED_STAGE_ORDER, FORBIDDEN_TARGETS, ROUND_TOPIC, STAGE_TOPIC
from core.domain.research import HypothesisOrigin, RunState, Verdict
from core.domain.specs import StrategySpec
from core.lifecycle.strategy import LifecycleState
from infrastructure.event_bus import InMemoryEventBus
from research.evolution import LineageGraph, require_new_version
from research.loop import OosUnsealBudget, ResearchMemory
from research.loop import trials as loop_trials
from research.strategies.pipeline import CandidateTrialRunner
from research.validation.sealed_oos import (
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


def test_three_unattended_rounds_on_real_components(planted: Run) -> None:
    run = planted
    assert [r.status for r in run.records] == [RoundStatus.COMPLETED] * 3
    assert run.loop.audit.verify()
    assert all(s.status is StageStatus.COMPLETED for r in run.records for s in r.stages)
    assert [s.name for s in run.records[0].stages] == list(EXTENDED_STAGE_ORDER)
    assert len(run.bus.poll("audit", STAGE_TOPIC, 100)) == 3 * len(EXTENDED_STAGE_ORDER)
    assert len(run.bus.poll("audit", ROUND_TOPIC, 100)) == 3
    # P2 state through run_state over F4 feature values, every round
    for record in run.records:
        state = _stage(record, "state").summary
        assert state["state"] == str(fx.TREND.ref) and state["evaluated"] > 0
        assert state["state_result_hash"] and state["feature_result_hashes"]
    # The planted 60-bar effect passes every G0 - G4 gate in round 0 except the walk-forward
    # coverage: this round's 3 days leave most of the Profile's (10-day) walk-forward windows
    # without returns, which is INCONCLUSIVE since review fixes 2 (R23; it used to PASS on the
    # shrunken denominator). It therefore stays in VALIDATION; PASS -> OOS is covered below by the
    # sealed-OOS tests, whose single round covers the whole research window.
    first = _stage(run.records[0], "validation").summary["reports"]
    assert [(r["hypothesis"], r["verdict"]) for r in first] == [
        ("hypothesis:h_k_tsmom_lookback_60@1.0.0", "INCONCLUSIVE")
    ]
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
    assert run.loop.guard.state_of(run.memory.trials[0].hypothesis.ref) is (
        LifecycleState.VALIDATION
    )
    # later hypotheses are refuted and filed; LLM drafts only wait for a human
    assert run.memory.reviews.pending == ("h_llm_0@1.0.0", "h_llm_1@1.0.0", "h_llm_2@1.0.0")
    assert run.loop.total_usage.llm_cost_units == Decimal(3)


def test_pure_noise_passes_nothing(noise: Run) -> None:
    run = noise
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


def test_hashed_records_hold_no_floats(planted: Run, noise: Run) -> None:
    for run in (planted, noise):
        for record in run.records:
            assert list(_floats(record.payload())) == []


# ---------------------------------------------------------------------------------------- budget


def test_budget_exhaustion_stops_the_loop_before_evolution(tmp_path: Path) -> None:
    budget = LoopBudget(
        max_trials_per_round=3,
        max_trials_total=2,
        max_llm_cost_units=Decimal(10),
        max_compute_seconds=Decimal(1000),
    )
    run = _run(tmp_path, rounds=5, budget=budget)
    assert [r.status for r in run.records] == [RoundStatus.COMPLETED, RoundStatus.BUDGET_EXHAUSTED]
    last = run.records[-1]
    assert _stage(last, "hypothesis").status is StageStatus.COMPLETED
    refused = _stage(last, "evolution")
    assert refused.status is StageStatus.REFUSED_BUDGET and refused.refused == ("max_trials_total",)
    assert _stage(last, "experiment").status is StageStatus.SKIPPED
    assert run.loop.halted is RoundStatus.BUDGET_EXHAUSTED
    assert run.memory.ledger.trials(fx.FAMILY) == 2 == run.loop.total_usage.trials
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
    assert len(memory.trials) == len(registered) == len(memory.experiments)
    for outcome in memory.trials:
        hypothesis, repro = outcome.hypothesis, outcome.run.repro
        assert registered[(hypothesis.name, hypothesis.version)] == hypothesis.content_hash()
        assert outcome.run.experiment_hash == repro.content_hash()
        assert outcome.experiment.experiment_hash == repro.content_hash()
        assert outcome.run.state is RunState.COMPLETED and outcome.trial is not None
        # the reproducibility tuple of 06-experiment.md §2
        assert repro.hypothesis_ref == hypothesis.ref
        assert repro.dataset_snapshots[0].snapshot_id == outcome.summary["market_hash"]
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
    # one 4-day round: research days 0-3, then the sealed OOS day 3-4 of the Profile
    return fx.config(
        lookbacks=(60,),
        days_per_round=4,
        profile=fx.loop_profile(boundary_day=3),
        loop_wiring=fx.wiring(evolution=False, oos_unseal=unseal),
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

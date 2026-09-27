"""P6 per-cell validation in the continuous loop (``ConditionalPlan.validate_cells``, 2026-09-26).

With ``validate_cells=True`` the validation stage runs in-sample G0 – G3 on every supported cell
of each validated trial (the cells the experiment stage pre-registered), under the same family
trial count the trial's own report used; unsupported cells are never validated, G4 / G5 are not
run for a cell, and no cell verdict moves any lifecycle state (``research.loop.trials``, module
docs, **Per-cell validation**). ``validate_cells=False`` keeps the registration-only behaviour
(``test_loop_conditional``); no plan at all keeps the pinned records
(``test_loop_e2e.test_records_without_a_conditional_plan_are_pinned``).

Scenario (every number TEST ONLY, arbitrary and uncalibrated; see ``loop_fixtures``): the planted
three-round loop of ``test_loop_conditional`` (the LLM draft ``h_llm_0`` approved after round 0,
so round 1 has an errored trial), with a larger compute budget (each validated cell is charged one
validation). ``min_support`` = 5 is an arbitrary TEST ONLY eligibility threshold.
"""

from __future__ import annotations

import gc
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from apps.worker import LoopBudget, ResearchLoop
from core.domain.research import GateResult, RunState, Verdict
from infrastructure.event_bus import InMemoryEventBus
from plugins.llm import ScriptedLLMProvider
from plugins.synthetic import RandomWalkMarket
from research.loop import (
    ConditionalPlan,
    LoopStateInconsistent,
    ResearchMemory,
    SyntheticLoopConfig,
    ValidationStage,
    loop_fingerprint,
    open_synthetic_loop,
)
from research.loop import trials as loop_trials
from research.loop.trials import (
    CELL_G4_NOT_RUN,
    CELL_G5_NOT_RUN,
    CELL_LIFECYCLE,
    PER_CELL_VALIDATION,
    PER_CELL_VALIDATION_RUN,
)
from research.strategies.failure_registry import FailureRegistry
from research.validation import InSampleInput
from tests.research.loop import loop_fixtures as fx

ROUNDS = 3
DRAFT = "h_llm_0@1.0.0"
REVIEWER = "test-human"
LLM_LOOKBACKS = (None, 240, 1440)
EFFECT = "net mean return above costs (test only)"
#: TEST ONLY plans (arbitrary numbers; ``min_support`` is an eligibility threshold, no Profile).
VALIDATE = ConditionalPlan(minimum_effect=EFFECT, min_support=5, validate_cells=True)
REGISTER_ONLY = replace(VALIDATE, validate_cells=False)
#: TEST ONLY budget: room for the conditional trials and one validation per supported cell.
BUDGET = LoopBudget(
    max_trials_per_round=30,
    max_trials_total=80,
    max_llm_cost_units=Decimal(10),
    max_compute_seconds=Decimal(5000),
)
CELLS = [*fx.TREND.state_space, None]
COMPLETED = RunState.COMPLETED.value
IN_SAMPLE_STAGES = ("G0.", "G1.", "G2.", "G3.")


def _config(plan: ConditionalPlan | None = VALIDATE) -> SyntheticLoopConfig:
    return fx.config(budget=BUDGET, loop_wiring=replace(fx.wiring(), conditional=plan))


def _llm(consumed: int = 0) -> ScriptedLLMProvider:
    outputs = [fx.llm_output(i, lookback) for i, lookback in enumerate(LLM_LOOKBACKS)]
    return ScriptedLLMProvider(outputs[consumed:], clock=lambda: fx.T0)


@dataclass
class Run:
    loop: ResearchLoop
    memory: ResearchMemory


def _run(tmp: Path, plan: ConditionalPlan | None, rounds: int = ROUNDS) -> Run:
    loop, memory, _ = fx.build(tmp, _config(plan), llm_lookbacks=LLM_LOOKBACKS)
    loop.run_unattended(1)
    if rounds > 1:
        memory.reviews.approve(DRAFT, reviewer=REVIEWER)
        loop.run_unattended(rounds - 1)
    return Run(loop, memory)


def _stage(record: Any, name: str) -> Any:
    return next(stage for stage in record.stages if stage.name == name)


def _reports(run: Run) -> list[tuple[int, dict[str, Any]]]:
    return [
        (record.round_index, row)
        for record in run.loop.audit.records
        for row in _stage(record, "validation").summary["reports"]
    ]


def _experiments(run: Run) -> dict[tuple[int, str, str | None], dict[str, Any]]:
    return {
        (record.round_index, row["hypothesis"], row["attempt"]): row
        for record in run.loop.audit.records
        for row in _stage(record, "experiment").summary["experiments"]
    }


@pytest.fixture(scope="module")
def validated(tmp_path_factory: pytest.TempPathFactory) -> Run:
    return _run(tmp_path_factory.mktemp("validated"), VALIDATE)


@pytest.fixture(scope="module")
def registered(tmp_path_factory: pytest.TempPathFactory) -> Run:
    return _run(tmp_path_factory.mktemp("registered"), REGISTER_ONLY)


# ------------------------------------------------------------------------------------ the plan


def test_the_plan_selects_validation_and_is_fingerprinted() -> None:
    assert REGISTER_ONLY.payload() == {"minimum_effect": EFFECT, "min_support": 5}
    assert VALIDATE.payload() == {**REGISTER_ONLY.payload(), "validate_cells": True}
    assert loop_fingerprint(_config())["conditional"] == VALIDATE.payload()
    assert loop_fingerprint(_config()) != loop_fingerprint(_config(REGISTER_ONLY))


def test_the_validation_stage_refuses_anything_but_a_plan(tmp_path: Path) -> None:
    memory = ResearchMemory(failures=FailureRegistry(tmp_path / "failures.jsonl"))
    with pytest.raises(ValueError, match="must be a ConditionalPlan"):
        ValidationStage(
            memory,
            cast(Any, None),  # never reached: the plan is refused first
            compute_seconds_per_validation=Decimal(1),
            conditional=cast(Any, {"validate_cells": True}),
        )


# ----------------------------------------------------------------------------- the cell reports


def test_every_supported_cell_gets_in_sample_gates(validated: Run) -> None:
    ledger = validated.memory.ledger
    hypotheses = {str(h.ref): h for h in ledger.hypotheses}
    experiments = _experiments(validated)
    rows = [(i, row) for i, row in _reports(validated) if row.get("conditional_cells")]
    assert len(rows) >= 4 and any(row["attempt"] for _, row in rows)
    report_ids: set[str] = set()
    validated_cells = 0
    for index, row in rows:
        experiment = experiments[(index, row["hypothesis"], row["attempt"])]
        assert experiment["conditional"]["validation"] == PER_CELL_VALIDATION_RUN
        block = row["conditional_cells"]
        assert block["family_trial_count"] == row["family_trial_count"]
        assert (block["g4"], block["g5"], block["lifecycle"]) == (
            CELL_G4_NOT_RUN,
            CELL_G5_NOT_RUN,
            CELL_LIFECYCLE,
        )
        registrations = experiment["conditional"]["cells"]
        assert [c["hypothesis"] for c in block["cells"]] == [c["hypothesis"] for c in registrations]
        assert [c["state"] for c in block["cells"]] == CELLS
        for cell, registration in zip(block["cells"], registrations, strict=True):
            assert cell["trial_index"] == registration["trial_index"]
            hypothesis = hypotheses[cell["hypothesis"]]
            assert ledger.trial_index(hypothesis, row["attempt"]) == cell["trial_index"]
            assert cell["trial_index"] <= row["family_trial_count"]
            if not registration["supported"]:
                continue
            validated_cells += 1
            assert cell["status"] == "validated", cell
            gate_ids = [g["gate_id"] for g in cell["gates"]]
            assert gate_ids and all(g.startswith(IN_SAMPLE_STAGES) for g in gate_ids)
            assert not any(g.startswith(("G4.", "G5.")) for g in gate_ids)
            verdicts = {g["verdict"] for g in cell["gates"]}
            expected = (
                "FAIL"
                if "FAIL" in verdicts
                else "INCONCLUSIVE"
                if "INCONCLUSIVE" in verdicts
                else "PASS"
            )
            assert cell["verdict"] == expected
            if cell["traded_decisions"]:  # evidence: G0 - G3 on the cell's own labels
                assert "G0.bindings" in gate_ids and "G0.reproducibility" in gate_ids
            assert cell["report_id"] not in report_ids
            report_ids.add(cell["report_id"])
    assert validated_cells >= 4


def test_unsupported_cells_are_never_validated(validated: Run) -> None:
    seen = 0
    for _, row in _reports(validated):
        for cell in (row.get("conditional_cells") or {}).get("cells", ()):
            if cell["support"] == "meets_min_support":
                continue
            seen += 1
            assert cell["status"] == "unsupported" and cell["reason"] == cell["support"]
            assert cell["verdict"] is None
            assert "gates" not in cell and "report_id" not in cell
    assert seen >= 1


def test_the_cells_partition_the_traded_decisions(validated: Run) -> None:
    """Each validated cell reads only its own traded decisions (one attribution, no overlap)."""
    for _, row in _reports(validated):
        block = row.get("conditional_cells")
        if not block:
            continue
        assert block["rerun_result_hash"] is not None
        assert block["unattributed_traded"] >= 0
        for cell in block["cells"]:
            if cell["status"] == "validated":
                assert 0 <= cell["traded_decisions"] <= cell["count"]


# --------------------------------------------------- nothing else changes (trials, lifecycle)


def test_validation_adds_no_trial(validated: Run, registered: Run) -> None:
    assert list(validated.memory.ledger.trial_log) == list(registered.memory.ledger.trial_log)
    for a, b in zip(validated.loop.audit.records, registered.loop.audit.records, strict=True):
        assert _stage(a, "experiment").usage.trials == _stage(b, "experiment").usage.trials
    counts = [row["family_trial_count"] for _, row in _reports(validated)]
    assert counts == [row["family_trial_count"] for _, row in _reports(registered)]


def test_the_trial_reports_are_those_of_the_registration_only_plan(
    validated: Run, registered: Run
) -> None:
    mine = [
        {k: v for k, v in row.items() if k != "conditional_cells"} for _, row in _reports(validated)
    ]
    theirs = [row for _, row in _reports(registered)]
    assert mine == theirs
    assert all("conditional_cells" not in row for row in theirs)
    for key, row in _experiments(registered).items():
        other = _experiments(validated)[key]
        if row["conditional"] is None:
            assert other == row
            continue
        assert row["conditional"]["validation"] == PER_CELL_VALIDATION
        expected = {
            **row["conditional"],
            "validation": PER_CELL_VALIDATION_RUN,
            "validate_cells": True,
        }
        assert other == {**row, "conditional": expected}


def test_a_cell_verdict_moves_no_lifecycle_state(validated: Run, registered: Run) -> None:
    for a, b in zip(validated.loop.audit.records, registered.loop.audit.records, strict=True):
        assert a.transitions == b.transitions
        assert _stage(a, "memory").summary == _stage(b, "memory").summary
    subjects = {str(h.subject) for h in validated.loop.guard.histories}
    assert subjects and not any("_given_" in subject for subject in subjects)
    assert len(validated.memory.failures.records()) == len(registered.memory.failures.records())


def test_a_forced_cell_pass_moves_no_lifecycle_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registered: Run
) -> None:
    """Every cell's G0 – G3 forced to PASS (the trial's own validator is untouched): the round's
    lifecycle transitions and failure records are exactly the registration-only run's."""

    def passing(inp: InSampleInput) -> tuple[GateResult, ...]:
        assert inp.context.metadata.family_trial_count >= inp.context.metadata.trial_index
        forced = GateResult(
            gate_id="G3.adjusted_p_value",
            metric="forced_test_only",
            value=0.0,
            verdict=Verdict.PASS,
        )
        return (forced,)

    monkeypatch.setattr(loop_trials, "run_in_sample", passing)
    forced = _run(tmp_path, VALIDATE, rounds=1)
    record = forced.loop.audit.records[0]
    passed = [
        cell
        for row in _stage(record, "validation").summary["reports"]
        for cell in (row.get("conditional_cells") or {}).get("cells", ())
        if cell["verdict"] == "PASS"
    ]
    assert passed  # at least one supported cell with a PASS verdict
    first = registered.loop.audit.records[0]
    assert record.transitions == first.transitions
    assert _stage(record, "memory").summary == _stage(first, "memory").summary
    assert not any("_given_" in str(h.subject) for h in forced.loop.guard.histories)


def test_no_support_threshold_validates_no_cell(tmp_path: Path) -> None:
    plan = ConditionalPlan(minimum_effect=EFFECT, min_support=None, validate_cells=True)
    run = _run(tmp_path, plan, rounds=1)
    cells = [
        cell
        for _, row in _reports(run)
        for cell in (row.get("conditional_cells") or {}).get("cells", ())
    ]
    assert len(cells) >= len(CELLS)
    assert all(
        c["status"] == "unsupported" and c["support"] == "no_support_threshold" for c in cells
    )
    assert all(c["verdict"] is None for c in cells)
    for _, row in _reports(run):
        assert row["conditional_cells"]["rerun_result_hash"] is None  # nothing re-run
    stage = _stage(run.loop.audit.records[0], "validation")
    assert stage.usage.compute_seconds == Decimal(5) * len(stage.summary["reports"])


def test_the_cell_validations_are_charged(validated: Run) -> None:
    for record in validated.loop.audit.records:
        stage = _stage(record, "validation")
        rows = stage.summary["reports"]
        cells = sum(
            1
            for row in rows
            for cell in (row.get("conditional_cells") or {}).get("cells", ())
            if cell["status"] == "validated"
        )
        assert stage.usage.compute_seconds == Decimal(5) * (len(rows) + cells)
        assert stage.estimate.compute_seconds >= stage.usage.compute_seconds


# ----------------------------------------------------------------------------------- durability


def _open(state_dir: Path, *, consumed: int = 0, plan: ConditionalPlan | None = VALIDATE) -> Any:
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


def test_a_restarted_loop_reproduces_the_cell_reports(restarted: Path, validated: Run) -> None:
    reopened = _open(restarted, consumed=ROUNDS)
    assert [r.record_hash for r in reopened.loop.audit.records] == [
        r.record_hash for r in validated.loop.audit.records
    ]
    assert [dict(v.summary) for v in reopened.memory.validations] == [
        row for _, row in _reports(validated)
    ]
    assert list(reopened.memory.ledger.trial_log) == list(validated.memory.ledger.trial_log)


def test_reopening_with_a_registration_only_plan_is_refused(restarted: Path) -> None:
    with pytest.raises(LoopStateInconsistent, match="conditional"):
        _open(restarted, consumed=ROUNDS, plan=REGISTER_ONLY)


def test_the_run_is_deterministic(tmp_path: Path, validated: Run) -> None:
    again = _run(tmp_path, VALIDATE, rounds=1)
    assert again.loop.audit.records[0].record_hash == validated.loop.audit.records[0].record_hash

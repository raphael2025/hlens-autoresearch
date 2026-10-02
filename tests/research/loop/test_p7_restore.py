"""ADR-0110: a durable loop that admitted a P7 plan reopens — the candidate is rebuilt only
through ``LoopWiring.p7_plans`` under its plan's checkpointed COMMIT — and every inconsistency
refuses the directory.

Every number is TEST ONLY (``loop_fixtures``). The plan is ``negation(tsmom_bars@1.0.0)``
(``p7_loop_fixtures``), admitted in round 0.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from apps.worker import LoopAuditLog
from core.domain.specs import InstrumentType
from plugins.synthetic import RandomWalkMarket
from research.hypotheses.p7_binding import P7PlanRecord, p7_plan_condition
from research.hypotheses.typed_plan_audit import PlanAdmissionJournal
from research.hypotheses.typed_plan_compiler import P7ExecutionSwitch
from research.loop import LoopStateInconsistent
from research.loop.compose import llm_content_fingerprint, loop_fingerprint
from research.loop.durable import (
    AUDIT_FILE,
    MEMORY_FILE,
    PLAN_ADMISSION_FILE,
    ROUND_MEMORY,
    P7Rebuild,
    _P7Proofs,
    open_state,
)
from research.loop.p7_admission import (
    P7CandidateRebuild,
    P7PlanRequest,
    P7RestoreRefused,
    p7_rebuild,
)
from research.persistence import AppendOnlyJournal
from research.strategies.pipeline import StrategyCandidate
from tests.research.hypotheses.p7_fixtures import (
    compile_nodes as compile_fixture_nodes,
)
from tests.research.hypotheses.p7_fixtures import cross_sectional_nodes, experiment_for
from tests.research.loop import loop_fixtures as fx
from tests.research.loop.p7_loop_fixtures import (
    CREATED,
    TSMOM,
    compiled_plan,
    negation_plan,
    plan_hypothesis,
    root_strategy,
)
from tests.research.loop.test_p7_admission import _config, _open, _request, _source
from tests.test_universe_contracts import manifest

ROUNDS = 3
LOCK = "state.lock"


def _files(state_dir: Path) -> dict[str, bytes]:
    """Every file of the directory but the lock (byte-identity evidence)."""
    return {
        path.name: path.read_bytes()
        for path in sorted(state_dir.iterdir())
        if path.is_file() and path.name != LOCK
    }


def _copy(state_dir: Path, tmp_path: Path) -> Path:
    target = tmp_path / "state"
    shutil.copytree(state_dir, target, ignore=shutil.ignore_patterns(LOCK))
    return target


def _rechain(path: Path, edit: Any) -> None:
    """Rewrite a journal with ``edit(type, payload)`` applied and a valid new hash chain."""
    entries = AppendOnlyJournal(path).entries
    path.unlink()
    rewritten = AppendOnlyJournal(path)
    for entry in entries:
        rewritten.append(entry.type, edit(entry.type, json.loads(json.dumps(entry.payload))))


def _edit_p7_row(**changes: Any) -> Any:
    """An edit of round 0's P7 strategy row (``provenance`` fields, or ``parent``)."""

    def edit(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind == ROUND_MEMORY and payload["round_index"] == 0:
            (row,) = payload["delta"]["strategies"]
            for key, value in changes.items():
                if key == "parent":
                    row["parent"] = value
                else:
                    row["provenance"][key] = value
        return payload

    return edit


def _open_state(state_dir: Path, rebuild: P7Rebuild | None) -> Any:
    """``open_state`` with the admitting configuration's own fingerprint (so only the P7 rebuild
    differs from what the composition passes)."""
    config = _config(_source(_request()))
    return open_state(
        state_dir,
        fingerprint={**loop_fingerprint(config), **llm_content_fingerprint(None)},
        strategies=config.wiring.strategies,
        provider=RandomWalkMarket(),
        provider_for=None,
        p7_rebuild=rebuild,
    )


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[str], dict[str, bytes]]:
    """One process runs every round, never restarted; the directory as it stood after round 0
    (the plan admitted — PREPARE / COMMIT — and its hypothesis run) is copied aside first.

    The copy is taken from the same run, not from a second one: round 0's knowledge hypothesis
    has a wall-clock ``created_at`` (``Hypothesis`` default), so two separate runs never agree
    byte for byte — the restart is compared against what the same round 0 went on to write.
    """
    base = tmp_path_factory.mktemp("p7_restore")
    state_dir = base / "uninterrupted"
    durable = _open(state_dir, _source(_request()))
    try:
        durable.loop.run_unattended(1)
        shutil.copytree(state_dir, base / "admitted", ignore=shutil.ignore_patterns(LOCK))
        durable.loop.run_unattended(ROUNDS - 1)
        hashes = [record.record_hash for record in durable.loop.audit.records]
    finally:
        durable.close()
    return base / "admitted", hashes, _files(state_dir)


@pytest.fixture(scope="module")
def admitted(runs: tuple[Path, list[str], dict[str, bytes]]) -> Path:
    """Round 0 admitted the plan and ran its hypothesis; the process ended there."""
    return runs[0]


# ------------------------------------------------------------------------------- restore


def test_the_p7_row_records_its_provenance_and_no_parent(admitted: Path) -> None:
    compiled = compiled_plan(negation_plan())
    entries = AppendOnlyJournal(admitted / MEMORY_FILE).entries
    (checkpoint,) = [entry for entry in entries if entry.type == ROUND_MEMORY]
    (row,) = checkpoint.payload["delta"]["strategies"]
    assert row["provenance"] == {
        "origin": "p7",
        "plan_hash": compiled.plan_hash,
        "round_index": 0,
    }
    assert row["parent"] is None  # the lowered spec's lineage is not an evolution parent
    assert compiled.root.spec.lineage  # (the spec itself does have one)
    assert row["spec_hash"] == compiled.root.spec.content_hash()


def test_a_restarted_loop_after_an_admission_ends_like_an_uninterrupted_one(
    runs: tuple[Path, list[str], dict[str, bytes]], tmp_path: Path
) -> None:
    admitted, expected_hashes, expected_files = runs
    state_dir = _copy(admitted, tmp_path)
    compiled = compiled_plan(negation_plan())
    durable = _open(state_dir, _source(_request()))
    try:
        assert len(durable.loop.audit.records) == 1
        restored = durable.memory.strategies[str(compiled.root.spec.ref)]
        assert restored.plan_record == P7PlanRecord.from_compiled(compiled)
        assert restored.spec == compiled.root.spec
        assert list(durable.memory.strategies) == [
            str(TSMOM.spec.ref),
            str(compiled.root.spec.ref),
        ]
        records = durable.loop.run_unattended(ROUNDS - 1)
        assert [record.round_index for record in records] == [1, 2]
        hashes = [record.record_hash for record in durable.loop.audit.records]
    finally:
        durable.close()
    assert hashes == expected_hashes
    assert _files(state_dir) == expected_files  # every file, byte for byte


# ------------------------------------------------------------------------------- refusals


def test_reopening_without_p7_plans_is_refused(admitted: Path, tmp_path: Path) -> None:
    state_dir = _copy(admitted, tmp_path)
    before = _files(state_dir)
    with pytest.raises(LoopStateInconsistent, match=r"LoopWiring\.p7_plans is not configured"):
        _open_state(state_dir, None)
    # through the composition the missing source is already a configuration change
    with pytest.raises(LoopStateInconsistent, match="p7_plans"):
        _open(state_dir, None)
    assert _files(state_dir) == before


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"plan_hash": "f" * 64}, "no checkpointed COMMIT of plan " + "f" * 64),
        ({"plan_hash": "not-a-hash"}, "names no P7 plan hash"),
        ({"round_index": 1}, "was not admitted in round 0"),
        ({"origin": "evolution"}, "malformed P7 provenance"),
        ({"parent": str(TSMOM.spec.ref)}, "with an evolution parent"),
    ],
)
def test_a_tampered_p7_row_is_refused(
    admitted: Path, tmp_path: Path, changes: dict[str, Any], message: str
) -> None:
    state_dir = _copy(admitted, tmp_path)
    _rechain(state_dir / MEMORY_FILE, _edit_p7_row(**changes))
    with pytest.raises(LoopStateInconsistent, match=f"round 0 is inconsistent: .*{message}"):
        _open(state_dir, _source(_request()))


def test_a_missing_commit_is_refused(admitted: Path, tmp_path: Path) -> None:
    compiled = compiled_plan(negation_plan())
    spec = root_strategy(compiled)
    entries = AppendOnlyJournal(admitted / MEMORY_FILE).entries
    (checkpoint,) = [entry for entry in entries if entry.type == ROUND_MEMORY]
    (row,) = checkpoint.payload["delta"]["strategies"]
    marks = [(entry.type, entry) for entry in entries[1:]]
    journal = PlanAdmissionJournal(admitted / PLAN_ADMISSION_FILE, loop_id="synthetic_loop")
    assert _P7Proofs(journal, marks, None).proof(spec, row, 0) == journal.committed[0]
    # no COMMIT in the journal (a journal holding only its header)
    empty = PlanAdmissionJournal(tmp_path / "empty.jsonl", loop_id="synthetic_loop", create=True)
    with pytest.raises(ValueError, match="no checkpointed COMMIT"):
        _P7Proofs(empty, marks, None).proof(spec, row, 0)
    # a COMMIT without its admission checkpoint
    with pytest.raises(ValueError, match="no checkpointed COMMIT"):
        _P7Proofs(journal, [], None).proof(spec, row, 0)
    with pytest.raises(ValueError, match="without a plan admission journal"):
        _P7Proofs(None, marks, None).proof(spec, row, 0)
    # a directory whose plan journal lost the COMMIT is refused before anything is restored
    state_dir = _copy(admitted, tmp_path)
    lines = (state_dir / PLAN_ADMISSION_FILE).read_text(encoding="utf-8").splitlines(keepends=True)
    assert json.loads(lines[-1])["type"] == "plan_admission_commit"
    (state_dir / PLAN_ADMISSION_FILE).write_text("".join(lines[:-1]), encoding="utf-8")
    with pytest.raises(LoopStateInconsistent):
        _open(state_dir, _source(_request()))


def _other_hypothesis_request() -> P7PlanRequest:
    """The same plan with another hypothesis: not what its PREPARE recorded."""
    compiled = compiled_plan(negation_plan())
    hypothesis = plan_hypothesis(compiled, name="h_p7_other")
    return replace(
        _request(),
        hypotheses=(hypothesis,),
        experiment_specs=(
            experiment_for(compiled, hypothesis, with_record=False, bind_outputs=False),
        ),
    )


def test_a_changed_plan_source_is_refused(admitted: Path, tmp_path: Path) -> None:
    state_dir = _copy(admitted, tmp_path)
    before = _files(state_dir)
    family = fx.FAMILY
    changed = _source(_other_hypothesis_request())
    with pytest.raises(LoopStateInconsistent, match="admission_evidence_mismatch"):
        _open_state(state_dir, p7_rebuild(changed, family_id=family))
    with pytest.raises(LoopStateInconsistent, match="p7_plans"):  # the composition's fingerprint
        _open(state_dir, changed)
    xs = _cross_sectional_request()
    with pytest.raises(LoopStateInconsistent, match="plan_not_declared"):
        _open_state(state_dir, p7_rebuild(_source(xs), family_id=family))
    disabled = _source(_request(), switch=P7ExecutionSwitch())
    with pytest.raises(LoopStateInconsistent, match="execution_disabled"):
        _open_state(state_dir, p7_rebuild(disabled, family_id=family))
    assert _files(state_dir) == before


def test_a_rebuild_with_other_content_is_refused(admitted: Path, tmp_path: Path) -> None:
    state_dir = _copy(admitted, tmp_path)

    def another_spec(*_: Any) -> StrategyCandidate:
        return TSMOM

    with pytest.raises(LoopStateInconsistent, match="does not rebuild from plan"):
        _open_state(state_dir, another_spec)
    with pytest.raises(LoopStateInconsistent, match="does not rebuild with its family"):
        _open_state(state_dir, p7_rebuild(_source(_request()), family_id="another_family"))


def _cross_sectional_request() -> P7PlanRequest:
    universe = manifest()
    compiled, resolution = compile_fixture_nodes(
        cross_sectional_nodes(universe), "xs", universes=(universe,)
    )
    hypothesis = plan_hypothesis(compiled, conditions=(p7_plan_condition(compiled.plan_hash),))
    return P7PlanRequest(
        plan=compiled.plan,
        resolution=resolution,
        created_at=CREATED,
        hypotheses=(hypothesis,),
        experiment_specs=(experiment_for(compiled, hypothesis, with_record=False),),
        universes=(universe,),
        instrument_type=InstrumentType.PERPETUAL,
    )


def test_a_cross_sectional_plan_is_never_rebuilt(admitted: Path) -> None:
    journal = PlanAdmissionJournal(admitted / PLAN_ADMISSION_FILE, loop_id="synthetic_loop")
    (committed,) = journal.committed
    request = _cross_sectional_request()
    rebuild = P7CandidateRebuild(_source(request), fx.FAMILY)
    with pytest.raises(P7RestoreRefused) as caught:
        rebuild(request.plan_hash, committed, 0)
    assert caught.value.code == "cross_sectional_loop_unsupported"
    assert isinstance(caught.value, ValueError)  # the durable state reports it as inconsistent
    assert p7_rebuild(None, family_id=fx.FAMILY) is None


# --------------------------------------------------------------- offline check (ADR-0073 §4)


def _interrupt_round_1(state_dir: Path) -> None:
    """A round 1 that started and was never recorded (the process died inside it)."""
    LoopAuditLog(state_dir / AUDIT_FILE).begin_round("synthetic_loop", 1)


def test_the_offline_check_accepts_a_committed_p7_row(admitted: Path, tmp_path: Path) -> None:
    """A reopening that may recover verifies rounds without compiling anything: the P7 row passes
    (structure, plan hash, COMMIT) and the reopening ends refused for the open round itself."""
    state_dir = _copy(admitted, tmp_path)
    _interrupt_round_1(state_dir)
    with pytest.raises(LoopStateInconsistent, match="round 1 was interrupted"):
        _open(state_dir, _source(_request()))
    with pytest.raises(LoopStateInconsistent, match="round 1 was interrupted"):
        _open_state(state_dir, None)  # no plan source needed on this path


def test_the_offline_check_refuses_a_p7_row_without_its_commit(
    admitted: Path, tmp_path: Path
) -> None:
    state_dir = _copy(admitted, tmp_path)
    _rechain(state_dir / MEMORY_FILE, _edit_p7_row(plan_hash="f" * 64))
    _interrupt_round_1(state_dir)
    with pytest.raises(LoopStateInconsistent, match="no checkpointed COMMIT of plan"):
        _open(state_dir, _source(_request()))

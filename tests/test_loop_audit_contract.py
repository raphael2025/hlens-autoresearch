"""ADR-0050: the research-loop audit record is a versioned contract that describes existing bytes.

Covers: the eight models are appended to the registry and exported byte-identically; every record
the worker writes (each stage / round outcome, transitions, the optional stage, a durable
journal's lines) and the committed console fixture validate and round-trip hash-identically; the
stage-order and status invariants; field-level refusals; and fail-closed refusals in the worker
(write and replay). ``ReportStore`` refusals are in ``tests/apps/test_reports.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from apps.worker import LoopAuditLog, LoopBudget, ResearchLoop, RoundContext, StageUsage
from apps.worker import loop as worker
from apps.worker.journal import AppendOnlyJournal
from apps.worker.loop import (
    ROUND_RECORDED,
    ROUND_STARTED,
    LoopAuditCorrupted,
    LoopHalted,
    LoopRecord,
    StageFailed,
)
from core.contracts import loop_audit
from core.contracts.loop_audit import (
    BUDGET_LIMITS,
    EXTENDED_STAGE_ORDER,
    STAGE_ORDER,
    LoopBudgetLimits,
    LoopBudgetUsage,
    LoopOverrun,
    LoopRoundRecord,
    LoopRoundRecorded,
    LoopRoundStarted,
    LoopRoundStatus,
    LoopStageRecord,
    LoopStageStatus,
    LoopTransitionRecord,
    check_stage_order,
)
from core.contracts.registry import CONTRACT_MODELS, export_json_schemas
from core.domain.base import canonical_json, content_hash
from core.lifecycle.strategy import LifecycleState
from infrastructure.event_bus import InMemoryEventBus
from tests.apps.test_research_loop import (
    EPOCH,
    SUBJECT,
    TEST_ONLY_BUDGET,
    FakeStage,
    _admit,
    _stages,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO / "apps" / "web" / "fixtures" / "research_loop_round"
LOOP_MODELS = (
    LoopBudgetUsage,
    LoopBudgetLimits,
    LoopStageRecord,
    LoopTransitionRecord,
    LoopOverrun,
    LoopRoundRecord,
    LoopRoundStarted,
    LoopRoundRecorded,
)


def _run(stages: list[FakeStage], rounds: int = 3, budget: LoopBudget = TEST_ONLY_BUDGET) -> Any:
    loop = ResearchLoop(
        loop_id="fake_loop",
        stages=stages,
        budget=budget,
        bus=InMemoryEventBus(),
        seed=7,
        epoch=EPOCH,
        cadence=timedelta(hours=1),
    )
    return loop.run_unattended(rounds)


def _raise(exc: Exception) -> Callable[[RoundContext], None]:
    def action(ctx: RoundContext) -> None:
        raise exc

    return action


class _EstimateFails(FakeStage):
    def estimate(self, ctx: RoundContext) -> StageUsage:
        raise RuntimeError("cannot estimate")


def _guard_violation(ctx: RoundContext) -> None:
    _admit(ctx)
    ctx.advance(SUBJECT, LifecycleState.PAPER, reason="test", evidence=("e",))


def _scenarios() -> dict[str, tuple[LoopRecord, ...]]:
    """Worker-written records covering every stage and round outcome the loop produces."""
    declared = StageUsage(trials=2, llm_cost_units=Decimal("1.5"), compute_seconds=Decimal(5))
    tight = LoopBudget(
        max_trials_per_round=5,
        max_trials_total=2,
        max_llm_cost_units=Decimal(100),
        max_compute_seconds=Decimal(1000),
    )
    evolution = _stages(hypothesis=FakeStage("hypothesis", action=_admit))
    evolution.insert(3, FakeStage("evolution", StageUsage(trials=1)))
    return {
        "completed_with_transition": _run(evolution),
        "under_reported_is_charged": _run(
            _stages(hypothesis=FakeStage("hypothesis", declared, actual=StageUsage()))
        ),
        "refused_budget": _run(
            _stages(hypothesis=FakeStage("hypothesis", StageUsage(trials=1))), budget=tight
        ),
        "overrun_completed": _run(
            _stages(
                experiment=FakeStage(
                    "experiment",
                    StageUsage(trials=1, compute_seconds=Decimal(2)),
                    actual=StageUsage(trials=3, compute_seconds=Decimal(1)),
                )
            )
        ),
        "overrun_failed": _run(
            _stages(
                validation=FakeStage(
                    "validation",
                    StageUsage(compute_seconds=Decimal(4)),
                    action=_raise(
                        StageFailed("over", usage=StageUsage(compute_seconds=Decimal(9)))
                    ),
                )
            )
        ),
        "failed_reported": _run(
            _stages(
                experiment=FakeStage(
                    "experiment",
                    declared,
                    action=_raise(StageFailed("half", usage=StageUsage(trials=1))),
                )
            )
        ),
        "failed_unreported": _run(
            _stages(state=FakeStage("state", declared, action=_raise(RuntimeError("down"))))
        ),
        "failed_estimate": _run(_stages(ingest=_EstimateFails("ingest"))),
        "guard_violation": _run(
            _stages(memory=FakeStage("memory", StageUsage(trials=1), action=_guard_violation))
        ),
    }


# --------------------------------------------------------------------------- registry and schemas


def test_the_loop_audit_models_are_appended_and_exported_byte_identically(tmp_path: Path) -> None:
    names = tuple(model.__name__ for model in CONTRACT_MODELS)
    assert len(names) == 146
    assert names[126:134] == tuple(model.__name__ for model in LOOP_MODELS)  # appended block
    written = export_json_schemas(tmp_path)
    for model in LOOP_MODELS:
        committed = (REPO / "schemas" / f"{model.__name__}.schema.json").read_bytes()
        assert committed == written[model.__name__].read_bytes(), model.__name__


def test_the_worker_uses_the_contract_stage_order_and_statuses() -> None:
    assert worker.STAGE_ORDER is loop_audit.STAGE_ORDER
    assert worker.EXTENDED_STAGE_ORDER is loop_audit.EXTENDED_STAGE_ORDER
    assert worker.OPTIONAL_STAGES is loop_audit.OPTIONAL_STAGES
    assert worker.check_stage_order is loop_audit.check_stage_order
    assert [s.value for s in worker.StageStatus] == [s.value for s in LoopStageStatus]
    assert [s.value for s in worker.RoundStatus] == [s.value for s in LoopRoundStatus]


def test_the_budget_payload_is_a_loop_budget_limits_with_the_same_hash() -> None:
    limits = LoopBudgetLimits.from_audit_payload(TEST_ONLY_BUDGET.payload())
    assert limits.budget_hash == TEST_ONLY_BUDGET.budget_hash
    nothing_fits = StageUsage(
        trials=10**6, llm_cost_units=Decimal(10**6), compute_seconds=Decimal(10**6)
    )
    assert (
        tuple(TEST_ONLY_BUDGET.refusals(StageUsage(), StageUsage(), nothing_fits)) == BUDGET_LIMITS
    )


# --------------------------------------------------------------------------- existing bytes


@pytest.mark.parametrize("scenario", sorted(_scenarios()))
def test_every_record_the_worker_writes_validates_and_hashes_identically(scenario: str) -> None:
    records = _scenarios()[scenario]
    assert records
    for record in records:
        payload = record.payload()
        contract = LoopRoundRecord.from_audit_payload(payload)
        assert contract.record_hash == record.record_hash
        assert canonical_json(contract.audit_payload()) == canonical_json(payload)
        recorded = {"record": payload, "record_hash": record.record_hash}
        LoopRoundRecorded.from_audit_payload(recorded)
        # the JSON bytes a journal / report holds validate the same way
        reread = json.loads(canonical_json(payload))
        assert LoopRoundRecord.from_audit_payload(reread).record_hash == record.record_hash


def test_the_scenarios_cover_every_stage_and_round_status() -> None:
    records = [r for rs in _scenarios().values() for r in rs]
    assert {s.status.value for r in records for s in r.stages} == {s.value for s in LoopStageStatus}
    assert {r.status.value for r in records} == {s.value for s in LoopRoundStatus}
    assert any(r.transitions for r in records)
    assert any(s.charged is not None for r in records for s in r.stages)
    assert any([s.name for s in r.stages] == list(EXTENDED_STAGE_ORDER) for r in records)


def test_the_committed_console_fixture_validates_and_is_named_by_its_hash() -> None:
    files = sorted(FIXTURE_DIR.glob("*.json"))
    assert files
    for path in files:
        raw = path.read_text(encoding="utf-8")
        record = LoopRoundRecord.from_audit_payload(json.loads(raw))
        assert record.record_hash == path.stem == content_hash(json.loads(raw))
        assert canonical_json(record.audit_payload()) == raw  # byte-identical round trip


def test_every_line_of_a_durable_audit_validates(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    stages = _stages(hypothesis=FakeStage("hypothesis", StageUsage(trials=1), action=_admit))
    ResearchLoop(
        loop_id="fake_loop",
        stages=stages,
        budget=TEST_ONLY_BUDGET,
        bus=InMemoryEventBus(),
        seed=7,
        epoch=EPOCH,
        cadence=timedelta(hours=1),
        audit=LoopAuditLog(path),
    ).run_unattended(1)  # round 0 admits the subject; the next round would re-open it
    entries = AppendOnlyJournal(path).entries
    assert [e.type for e in entries] == [ROUND_STARTED, ROUND_RECORDED]
    LoopRoundStarted.from_audit_payload(dict(entries[0].payload))
    recorded = LoopRoundRecorded.from_audit_payload(dict(entries[1].payload))
    assert recorded.record_hash == LoopAuditLog(path).head


# --------------------------------------------------------------------------- invariants


def _good() -> dict[str, Any]:
    (record,) = _scenarios()["completed_with_transition"][:1]
    payload: dict[str, Any] = json.loads(canonical_json(record.payload()))
    return payload


def _refused(payload: Any, match: str | None = None) -> None:
    with pytest.raises(ValueError, match=match):
        LoopRoundRecord.from_audit_payload(payload)


def test_the_stage_order_invariant_holds() -> None:
    good = _good()
    LoopRoundRecord.from_audit_payload(good)
    stages = good["stages"]
    assert [s["name"] for s in stages] == list(EXTENDED_STAGE_ORDER)
    without_evolution = [s for s in stages if s["name"] != "evolution"]
    assert [s["name"] for s in without_evolution] == list(STAGE_ORDER)
    swapped = list(stages)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    misplaced = [s for s in stages if s["name"] != "evolution"]
    misplaced.insert(5, next(s for s in stages if s["name"] == "evolution"))
    unknown = [*stages, {**stages[0], "name": "reporting"}]
    for bad in ([], stages[:-1], swapped, misplaced, unknown, [*stages, stages[-1]]):
        _refused({**good, "stages": bad}, "stages must be exactly")
    with pytest.raises(ValueError, match="exactly"):
        check_stage_order(list(reversed(STAGE_ORDER)))


def test_the_round_status_and_totals_must_follow_from_the_stages() -> None:
    good = _good()
    _refused({**good, "status": "FAILED"}, "round status")
    _refused({**good, "status": "PROMOTED"})
    skipped = json.loads(json.dumps(good))
    nothing = {"estimate": None, "usage": None, "summary": None, "overrun": None, "charged": None}
    skipped["stages"][2] = {**skipped["stages"][2], **nothing, "status": "SKIPPED"}
    LoopStageRecord.from_audit_payload(skipped["stages"][2])  # a well-formed skipped stage
    _refused(skipped, "SKIPPED, yet nothing stopped the round")
    usage = {"trials": 0, "llm_cost_units": "0", "compute_seconds": "0"}
    _refused({**good, "round_usage": usage}, "round_usage")
    _refused({**good, "total_usage": usage}, "total_usage")
    fake_overrun = {"stage": "memory", "amount": {**usage, "trials": 1}}
    _refused({**good, "overrun": fake_overrun}, "overrun")


def test_the_chain_starts_at_round_zero_only() -> None:
    good = _good()
    _refused({**good, "previous_hash": content_hash("x")}, "round 0")
    _refused({**good, "round_index": 1}, "round 0")
    with pytest.raises(ValueError, match="round 0"):
        LoopRoundStarted.from_audit_payload(
            {"loop_id": "l", "round_index": 1, "previous_hash": None}
        )


def test_the_recorded_line_hash_is_recomputed() -> None:
    good = _good()
    LoopRoundRecorded.from_audit_payload({"record": good, "record_hash": content_hash(good)})
    with pytest.raises(ValidationError, match="record_hash"):
        LoopRoundRecorded.from_audit_payload({"record": good, "record_hash": content_hash("x")})


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("as_of",), "2026-01-01T01:00:00+01:00"),  # not UTC
        (("as_of",), "2026-01-01T00:00:00Z"),  # not the isoformat the worker writes
        (("as_of",), "2026-01-01T00:00:00"),  # naive
        (("seed",), -1),
        (("round_index",), True),
        (("budget_hash",), "not-a-hash"),
        (("round_usage", "trials"), "1"),
        (("round_usage", "llm_cost_units"), "1e3"),  # not canonical str(Decimal)
        (("round_usage", "llm_cost_units"), "-1"),
        (("round_usage", "llm_cost_units"), "NaN"),
        (("round_usage", "compute_seconds"), 1),
        (("stages", 0, "refused"), ["max_trials_total"]),  # only a refused stage names limits
        (("stages", 0, "error"), "an error on a completed stage"),
        (("stages", 0, "summary"), None),
        (("stages", 0, "status"), "DONE"),
        (("transitions", 0, "to"), "PAPER"),  # not an edge from IDEA
        (("transitions", 0, "to"), "VALIDATION"),  # not an edge from IDEA
        (("transitions", 0, "from"), "OOS"),  # OOS -> CANDIDATE is no edge either
        (("transitions", 0, "reason"), " test"),  # whitespace is never normalized
        (("transitions", 0, "evidence"), []),
        (("transitions", 0, "subject"), "hypothesis:h_fake@1"),
        (("schema_version",), "2.0.0"),  # the persisted bytes carry no envelope
        (("extra",), 1),
    ],
)
def test_ill_formed_fields_are_refused(path: tuple[Any, ...], value: Any) -> None:
    payload = _good()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    _refused(payload)


def test_a_transition_through_a_human_gate_is_refused() -> None:
    transition = _good()["transitions"][0]
    LoopTransitionRecord.from_audit_payload(transition)
    with pytest.raises(ValidationError, match="human approval"):
        LoopTransitionRecord.from_audit_payload({**transition, "from": "OOS", "to": "PAPER"})


def test_a_stage_carries_exactly_what_its_status_implies() -> None:
    records = [r for rs in _scenarios().values() for r in rs]
    by_status = {s.status.value: s.payload() for r in records for s in r.stages}
    zero = {"trials": 0, "llm_cost_units": "0", "compute_seconds": "0"}
    bad = [
        {**by_status["SKIPPED"], "estimate": zero},
        {**by_status["REFUSED_BUDGET"], "refused": []},
        {**by_status["REFUSED_BUDGET"], "refused": list(reversed(BUDGET_LIMITS))},
        {**by_status["GUARD_VIOLATION"], "error": None},
        {**by_status["FAILED"], "error": None},
        {**by_status["FAILED"], "estimate": None, "usage": zero},
        {**by_status["COMPLETED"], "usage": None},
        {**by_status["BUDGET_OVERRUN"], "overrun": zero},
        {**by_status["BUDGET_OVERRUN"], "status": "COMPLETED", "overrun": None},
    ]
    for payload in bad:
        with pytest.raises(ValueError):
            LoopStageRecord.from_audit_payload(payload)
    for payload in by_status.values():
        LoopStageRecord.from_audit_payload(payload)


# --------------------------------------------------------------------------- fail closed


def test_the_worker_refuses_to_write_a_record_that_breaks_the_contract() -> None:
    (record,) = _scenarios()["completed_with_transition"][:1]
    audit = LoopAuditLog()
    for bad in (
        replace(record, stages=()),
        replace(record, stages=tuple(reversed(record.stages))),
        replace(record, status=worker.RoundStatus.FAILED),
    ):
        with pytest.raises(ValueError, match="LoopRoundRecorded contract"):
            audit.append(bad)
    assert audit.records == ()
    audit.append(record)
    assert audit.head == record.record_hash


def test_a_loop_whose_record_breaks_the_contract_stops_fail_closed(tmp_path: Path) -> None:
    """A summary with non-string keys hashes (JSON stringifies them) but is not a JSON object
    the contract accepts: the round is not recorded and the loop stops."""

    class IntKeys(FakeStage):
        def run(self, ctx: RoundContext) -> worker.StageResult:
            summary: dict[Any, Any] = {1: "one"}
            return worker.StageResult(summary)

    path = tmp_path / "audit.jsonl"
    loop = ResearchLoop(
        loop_id="fake_loop",
        stages=_stages(memory=IntKeys("memory")),
        budget=TEST_ONLY_BUDGET,
        bus=InMemoryEventBus(),
        seed=7,
        epoch=EPOCH,
        cadence=timedelta(hours=1),
        audit=LoopAuditLog(path),
    )
    with pytest.raises(RuntimeError, match="LoopRoundRecorded contract"):
        loop.run_unattended(1)
    assert loop.audit.records == () and loop.stopped is not None
    with pytest.raises(LoopHalted):
        loop.submit_round(0)
    assert [e.type for e in AppendOnlyJournal(path).entries] == [ROUND_STARTED]


def _rewrite(source: Path, target: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    """Re-chain ``source`` with the recorded line's record edited and its record_hash recomputed
    (an attacker who knows every hash rule: only the contract can refuse it)."""
    journal = AppendOnlyJournal(target)
    for entry in AppendOnlyJournal(source).entries:
        payload = json.loads(json.dumps(dict(entry.payload)))
        if entry.type == ROUND_RECORDED:
            edit(payload["record"])
            payload["record_hash"] = content_hash(payload["record"])
        journal.append(entry.type, payload)


@pytest.mark.parametrize(
    "edit",
    [
        lambda r: r.__setitem__("stages", list(reversed(r["stages"]))),
        lambda r: r.__setitem__("status", "FAILED"),
        lambda r: r["round_usage"].__setitem__("trials", 1),
        lambda r: r["stages"][0].__setitem__("summary", None),
    ],
    ids=["shuffled-stages", "status", "round-usage", "stage-shape"],
)
def test_a_rehashed_record_that_breaks_the_contract_is_refused_on_replay(
    tmp_path: Path, edit: Callable[[dict[str, Any]], None]
) -> None:
    path = tmp_path / "audit.jsonl"
    ResearchLoop(
        loop_id="fake_loop",
        stages=_stages(),
        budget=TEST_ONLY_BUDGET,
        bus=InMemoryEventBus(),
        seed=7,
        epoch=EPOCH,
        cadence=timedelta(hours=1),
        audit=LoopAuditLog(path),
    ).run_unattended(1)
    rewritten = tmp_path / "rewritten.jsonl"
    _rewrite(path, rewritten, edit)
    with pytest.raises(LoopAuditCorrupted, match="LoopRoundRecorded contract"):
        LoopAuditLog(rewritten)
    identity = tmp_path / "identity.jsonl"
    _rewrite(path, identity, lambda r: None)  # the unedited rewrite replays fine
    assert LoopAuditLog(identity).head == LoopAuditLog(path).head

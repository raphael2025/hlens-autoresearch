"""ADR-0049 implementation note (durable audit and measured compute, 2026-09-26).

Backlog C / P11: the loop's audit survives a restart (hash-chained, fsync'd, fail closed), and
stage time is measured on a monotonic clock outside the hashed record. Budgets, tolerances and
clock steps below are TEST ONLY numbers chosen for readability, not proposals.
"""

from __future__ import annotations

import ast
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from apps.worker import (
    AppendOnlyJournal,
    JournalCorrupted,
    LoopAuditCorrupted,
    LoopAuditLog,
    LoopBudget,
    LoopHalted,
    LoopRecord,
    ResearchLoop,
    RoundContext,
    RoundStatus,
    StageStatus,
    StageUsage,
)
from apps.worker.loop import METRICS_TOPIC, ROUND_RECORDED, ROUND_STARTED
from apps.worker.metrics import Clock
from core.lifecycle.strategy import LifecycleState
from infrastructure.event_bus import InMemoryEventBus
from tests.apps.test_research_loop import (
    EPOCH,
    REPO,
    SUBJECT,
    TEST_ONLY_BUDGET,
    FakeStage,
    _admit,
    _stages,
)

SECOND = 1_000_000_000


def _lifecycle(ctx: RoundContext) -> None:
    """Round 0 admits the subject; round 3 advances it (needs the guard to survive a restart)."""
    if ctx.round_index == 0:
        _admit(ctx)
    if ctx.round_index == 3:
        ctx.advance(SUBJECT, LifecycleState.VALIDATION, reason="test", evidence=("e",))


def _durable_stages() -> list[FakeStage]:
    return _stages(
        hypothesis=FakeStage("hypothesis", StageUsage(trials=1)),
        memory=FakeStage("memory", action=_lifecycle),
    )


def _loop(
    stages: list[FakeStage],
    audit: LoopAuditLog | None = None,
    *,
    budget: LoopBudget = TEST_ONLY_BUDGET,
    seed: int = 7,
    loop_id: str = "fake_loop",
    clock: Clock | None = None,
    tolerance: Decimal | None = None,
) -> tuple[ResearchLoop, InMemoryEventBus]:
    bus = InMemoryEventBus()
    loop = ResearchLoop(
        loop_id=loop_id,
        stages=stages,
        budget=budget,
        bus=bus,
        seed=seed,
        epoch=EPOCH,
        cadence=timedelta(hours=1),
        audit=audit,
        clock=clock,
        compute_tolerance_seconds=tolerance,
    )
    return loop, bus


# --------------------------------------------------------------------------- durable audit


def test_a_restart_continues_from_the_last_round_with_the_same_hashes(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    first, _ = _loop(_durable_stages(), LoopAuditLog(path))
    before = first.run_unattended(2)

    stages = _durable_stages()
    restarted, _ = _loop(stages, LoopAuditLog(path))  # a new process: nothing shared but the file
    assert [r.record_hash for r in restarted.audit.records] == [r.record_hash for r in before]
    assert restarted.guard.state_of(SUBJECT) is LifecycleState.CANDIDATE  # guard rebuilt
    after = restarted.run_unattended(2)
    assert [r.round_index for r in after] == [2, 3]
    assert all(stage.runs == 2 for stage in stages)  # recorded rounds 0 and 1 never ran again
    assert restarted.guard.state_of(SUBJECT) is LifecycleState.VALIDATION
    assert restarted.audit.verify() and after[0].previous_hash == before[-1].record_hash

    uninterrupted, _ = _loop(_durable_stages())
    expected = [r.record_hash for r in uninterrupted.run_unattended(4)]
    assert [r.record_hash for r in (*before, *after)] == expected
    reopened = LoopAuditLog(path)
    assert [r.record_hash for r in reopened.records] == expected
    assert [r.payload() for r in reopened.records] == [
        r.payload() for r in uninterrupted.audit.records
    ]


def test_a_restarted_loop_never_reruns_a_recorded_round(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _loop(_durable_stages(), LoopAuditLog(path))[0].run_unattended(2)
    stages = _durable_stages()
    restarted, _ = _loop(stages, LoopAuditLog(path))
    restarted.submit_round(0)
    [outcome] = restarted.run_pending()
    assert not outcome.succeeded and "out of order" in (outcome.error or "")
    assert all(stage.runs == 0 for stage in stages)
    assert len(LoopAuditLog(path).records) == 2


def test_cumulative_budget_totals_survive_a_restart(tmp_path: Path) -> None:
    budget = LoopBudget(
        max_trials_per_round=5,
        max_trials_total=3,
        max_llm_cost_units=Decimal(0),
        max_compute_seconds=Decimal(1000),
    )
    path = tmp_path / "audit.jsonl"
    first, _ = _loop(_durable_stages(), LoopAuditLog(path), budget=budget)
    first.run_unattended(2)
    assert first.total_usage.trials == 2

    restarted, _ = _loop(_durable_stages(), LoopAuditLog(path), budget=budget)
    assert restarted.total_usage == first.total_usage  # a restart does not reset the budget
    records = restarted.run_unattended(5)
    assert [r.status for r in records] == [RoundStatus.COMPLETED, RoundStatus.BUDGET_EXHAUSTED]
    assert restarted.total_usage.trials == 3 and records[-1].total_usage.trials == 3

    again, _ = _loop(_durable_stages(), LoopAuditLog(path), budget=budget)
    assert again.halted is RoundStatus.BUDGET_EXHAUSTED  # the halt survives the restart too
    with pytest.raises(LoopHalted):
        again.submit_round(4)


def test_the_durable_audit_is_deterministic(tmp_path: Path) -> None:
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    hashes_a = [
        r.record_hash for r in _loop(_durable_stages(), LoopAuditLog(a))[0].run_unattended(3)
    ]
    hashes_b = [
        r.record_hash for r in _loop(_durable_stages(), LoopAuditLog(b))[0].run_unattended(3)
    ]
    in_memory = [r.record_hash for r in _loop(_durable_stages())[0].run_unattended(3)]
    assert hashes_a == hashes_b == in_memory
    assert a.read_bytes() == b.read_bytes()  # no wall-clock time or measurement in the file
    types = [entry.type for entry in AppendOnlyJournal(a).entries]
    assert types == [ROUND_STARTED, ROUND_RECORDED] * 3


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _rechain(source: Path, target: Path, edit_line: int, payload: dict[str, object]) -> None:
    """Copy ``source`` into a fresh, validly chained journal with one line's payload replaced
    (the attacker recomputes every journal hash; only the records' own hashes can catch it)."""
    journal = AppendOnlyJournal(target)
    for number, entry in enumerate(AppendOnlyJournal(source).entries, 1):
        journal.append(entry.type, payload if number == edit_line else dict(entry.payload))


def test_a_tampered_audit_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _loop(_durable_stages(), LoopAuditLog(path))[0].run_unattended(2)

    edited = tmp_path / "edited.jsonl"
    lines = _lines(path)
    lines[1]["payload"]["record"]["total_usage"]["trials"] = 0  # type: ignore[index]
    edited.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    with pytest.raises(JournalCorrupted):
        LoopAuditLog(edited)

    record_line = AppendOnlyJournal(path).entries[1]
    forged = json.loads(json.dumps(dict(record_line.payload)))
    forged["record"]["round_usage"]["trials"] = 0  # the record_hash is left stale
    rechained = tmp_path / "rechained.jsonl"
    _rechain(path, rechained, 2, forged)
    with pytest.raises(LoopAuditCorrupted, match="record_hash"):
        LoopAuditLog(rechained)

    unknown = tmp_path / "unknown.jsonl"
    unknown.write_bytes(path.read_bytes())
    AppendOnlyJournal(unknown).append("loop_round_deleted", {"round_index": 0})
    with pytest.raises(LoopAuditCorrupted, match="unknown"):
        LoopAuditLog(unknown)

    partial = tmp_path / "partial.jsonl"
    partial.write_bytes(path.read_bytes()[:-20])  # cut mid-line
    with pytest.raises(JournalCorrupted):
        LoopAuditLog(partial)


def test_an_interrupted_round_stops_the_restarted_loop(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _loop(_durable_stages(), LoopAuditLog(path))[0].run_unattended(2)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    truncated = tmp_path / "interrupted.jsonl"
    truncated.write_text("".join(lines[:-1]), encoding="utf-8")  # round 1 started, never recorded

    audit = LoopAuditLog(truncated)
    assert audit.open_round == 1 and len(audit.records) == 1
    stages = _durable_stages()
    loop, _ = _loop(stages, audit)
    assert loop.stopped is not None and "interrupted" in loop.stopped
    with pytest.raises(LoopHalted, match="interrupted"):
        loop.run_unattended(1)
    assert all(stage.runs == 0 for stage in stages)


def test_an_audit_write_failure_stops_the_loop(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    loop, _ = _loop(_durable_stages(), LoopAuditLog(path))
    loop.run_unattended(1)
    path.write_text("", encoding="utf-8")  # history removed underneath the running loop
    with pytest.raises(RuntimeError):
        loop.run_unattended(1)
    assert loop.stopped is not None and "could not be recorded" in loop.stopped
    with pytest.raises(LoopHalted):
        loop.submit_round(1)


def test_an_audit_of_another_loop_or_schedule_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _loop(_durable_stages(), LoopAuditLog(path))[0].run_unattended(1)
    with pytest.raises(ValueError, match="belongs to loop"):
        _loop(_durable_stages(), LoopAuditLog(path), loop_id="other_loop")
    with pytest.raises(ValueError, match="another seed"):
        _loop(_durable_stages(), LoopAuditLog(path), seed=8)


def test_the_in_memory_audit_is_unchanged_and_durable_needs_a_started_round(
    tmp_path: Path,
) -> None:
    loop, _ = _loop(_durable_stages())
    [record] = loop.run_unattended(1)
    assert not loop.audit.durable
    durable = LoopAuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(ValueError, match="begin_round"):
        durable.append(record)
    assert durable.records == () and not (tmp_path / "audit.jsonl").exists()


# --------------------------------------------------------------------------- measured time


class FakeClock:
    """A TEST ONLY clock that moves only when a stage says it worked."""

    def __init__(self) -> None:
        self.wall = 0
        self.cpu = 0

    def __call__(self) -> tuple[int, int]:
        return self.wall, self.cpu

    def work(self, wall_seconds: int, cpu_seconds: int) -> None:
        self.wall += wall_seconds * SECOND
        self.cpu += cpu_seconds * SECOND


def _timed_stages(clock: FakeClock) -> list[FakeStage]:
    # declares 1 compute second, "takes" 3 s wall / 2 s CPU on the fake clock
    slow = FakeStage(
        "experiment",
        StageUsage(trials=1, compute_seconds=Decimal(1)),
        action=lambda ctx: clock.work(3, 2),
    )
    return _stages(experiment=slow)


@pytest.mark.parametrize(
    ("tolerance", "flagged"),
    [(None, None), (Decimal(1), True), (Decimal(2), False), (Decimal(5), False)],
)
def test_measured_time_is_reported_and_flagged_only_against_a_configured_tolerance(
    tolerance: Decimal | None, flagged: bool | None
) -> None:
    clock = FakeClock()
    loop, bus = _loop(_timed_stages(clock), clock=clock, tolerance=tolerance)
    [record] = loop.run_unattended(1)
    [metrics] = loop.metrics
    assert metrics.record_hash == record.record_hash and metrics.tolerance_seconds == tolerance
    by_name = {stage.name: stage for stage in metrics.stages}
    slow = by_name["experiment"]
    assert (slow.wall_seconds, slow.cpu_seconds) == (Decimal(3), Decimal(2))
    assert slow.declared_compute_seconds == Decimal(1) and slow.excess_seconds == Decimal(2)
    assert slow.flagged is flagged
    assert metrics.flagged == (("experiment",) if flagged else ())
    assert by_name["ingest"].wall_seconds == 0 and by_name["ingest"].excess_seconds == 0
    # the budget still charges max(declared, reported), never the measurement
    assert loop.total_usage.compute_seconds == Decimal(1)
    [message] = bus.poll("ops", METRICS_TOPIC, 10)
    assert message.payload["record_hash"] == record.record_hash
    assert message.payload["stages"][3]["excess_seconds"] == "2"


def test_measured_time_never_enters_the_hashed_record() -> None:
    fake = FakeClock()
    measured_slow, _ = _loop(_timed_stages(fake), clock=fake, tolerance=Decimal(0))
    real, _ = _loop(_timed_stages(FakeClock()))  # same declarations, timed on the real clock
    slow_records = measured_slow.run_unattended(2)
    assert [r.record_hash for r in slow_records] == [r.record_hash for r in real.run_unattended(2)]
    assert all(m.flagged == ("experiment",) for m in measured_slow.metrics)
    assert "wall_seconds" not in json.dumps([r.payload() for r in slow_records])


def test_the_real_clock_measures_non_negative_time_and_skipped_stages_are_not_timed() -> None:
    budget = LoopBudget(
        max_trials_per_round=0,
        max_trials_total=0,
        max_llm_cost_units=Decimal(0),
        max_compute_seconds=Decimal(1000),
    )
    loop, _ = _loop(
        _stages(hypothesis=FakeStage("hypothesis", StageUsage(trials=1))), budget=budget
    )
    [record] = loop.run_unattended(1)
    assert record.status is RoundStatus.BUDGET_EXHAUSTED
    [metrics] = loop.metrics
    # refused and skipped stages never ran, so they have no measurement
    assert [stage.name for stage in metrics.stages] == ["ingest", "state"]
    assert {s.name: s.status for s in record.stages}["hypothesis"] is StageStatus.REFUSED_BUDGET
    assert all(s.wall_seconds >= 0 and s.cpu_seconds >= 0 for s in metrics.stages)
    assert all(s.flagged is None for s in metrics.stages)  # no tolerance configured: report only


def test_a_negative_tolerance_is_refused() -> None:
    with pytest.raises(ValueError, match="compute_tolerance_seconds"):
        _loop(_stages(), tolerance=Decimal(-1))


def test_the_worker_journal_and_metrics_modules_use_only_core_and_the_stdlib() -> None:
    allowed = {
        "journal.py": {"__future__", "core", "collections", "dataclasses", "hashlib", "json"}
        | {"os", "pathlib", "typing"},
        "metrics.py": {"__future__", "collections", "dataclasses", "decimal", "time", "typing"},
    }
    for name, roots_allowed in allowed.items():
        tree = ast.parse((REPO / "apps" / "worker" / name).read_text(encoding="utf-8"))
        roots = {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert roots <= roots_allowed, f"apps/worker/{name} imports {sorted(roots - roots_allowed)}"


# --------------------------------------------------------------------------- round checkpoint


def test_the_checkpoint_sees_each_round_before_the_audit_records_it(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    audit = LoopAuditLog(path)
    seen: list[tuple[int, str, int]] = []

    def checkpoint(record: LoopRecord) -> None:
        seen.append((record.round_index, record.record_hash, len(audit.records)))

    loop = ResearchLoop(
        loop_id="fake_loop",
        stages=_durable_stages(),
        budget=TEST_ONLY_BUDGET,
        bus=InMemoryEventBus(),
        seed=7,
        epoch=EPOCH,
        cadence=timedelta(hours=1),
        audit=audit,
        checkpoint=checkpoint,
    )
    records = loop.run_unattended(2)
    # called once per round, with the final record, while the audit did not yet hold it
    assert seen == [(r.round_index, r.record_hash, r.round_index) for r in records]


def test_a_failing_checkpoint_leaves_the_round_unrecorded_and_stops_the_loop(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit.jsonl"

    def checkpoint(record: LoopRecord) -> None:
        if record.round_index == 1:
            raise OSError("disk full")

    loop = ResearchLoop(
        loop_id="fake_loop",
        stages=_durable_stages(),
        budget=TEST_ONLY_BUDGET,
        bus=InMemoryEventBus(),
        seed=7,
        epoch=EPOCH,
        cadence=timedelta(hours=1),
        audit=LoopAuditLog(path),
        checkpoint=checkpoint,
    )
    with pytest.raises(RuntimeError, match="disk full"):
        loop.run_unattended(3)
    assert loop.stopped is not None and "round 1 could not be recorded" in loop.stopped
    reopened = LoopAuditLog(path)
    assert len(reopened.records) == 1 and reopened.open_round == 1  # interrupted, not recorded
    with pytest.raises(LoopHalted):
        loop.submit_round(1)

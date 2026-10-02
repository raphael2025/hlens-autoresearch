"""Phase 14 migration drill, partner exercise (ADR-0106 decision 3): ``InMemoryEventBus`` ->
``FileEventBus``.

The source is the in-memory bus; the target is the file-backed bus
(``infrastructure/event_bus/file.py``). The drill checks conformance (``BUS_CHECKS``) on both and
reruns a deterministic research-loop scenario (``tests/research/loop/loop_fixtures.py``, three
rounds) on each bus. Only observables that do not depend on persistence are golden outputs:

- ``round.<i>.record_hash``: every round's audit ``record_hash`` (the loop result itself);
- ``bus.<topic>.count`` / ``bus.<topic>.delivery_hash``: how many messages each loop topic
  delivered to a consumer and the content hash of that delivery sequence (``message_id`` in
  delivery order, so order and content are both pinned);
- ``bus.pending_after_ack``: what a consumer still sees after acknowledging every delivery.

Tolerance 0, nothing excluded. Rollback evidence: switching back to the in-memory bus reproduces
the golden bit-exactly. Counterexamples show the comparison is falsifiable (another market seed, a
bus that drops a message, a bus that reorders deliveries).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from apps.worker.loop import ROUND_TOPIC, STAGE_TOPIC
from core.contracts.event_bus import BusMessage, EventBusAdapter
from core.domain.base import content_hash
from infrastructure.event_bus import FileEventBus, InMemoryEventBus
from infrastructure.migration import (
    ConformanceReport,
    GoldenRecord,
    MigrationReport,
    MigrationTarget,
    RollbackVerdict,
    compare_golden,
    load_migration_report,
    record_golden,
    rollback_evidence,
    run_conformance,
    save_migration_report,
)
from plugins.llm import ScriptedLLMProvider
from plugins.synthetic import RandomWalkMarket
from research.loop import ResearchMemory, build_synthetic_loop
from research.strategies.failure_registry import FailureRegistry
from tests.contract_suites import event_bus as bus_suite
from tests.research.loop import loop_fixtures as fx

ZERO = Decimal(0)
SOURCE = "hlens_event_bus_memory"
TARGET = "hlens_event_bus_file"
ROUNDS = 3
CONSUMER = "migration-drill"
TOPICS = (ROUND_TOPIC, STAGE_TOPIC)

MIGRATION = MigrationTarget(
    migration_id="p14-event-bus-memory-to-file",
    source=SOURCE,
    target=TARGET,
    tolerance=ZERO,
    scope=(
        "EventBusAdapter: at-least-once delivery, publish order within a topic, independent "
        "consumers",
        "a deterministic three-round research-loop scenario (TEST ONLY synthetic market)",
    ),
    out_of_scope=(
        "crash recovery, cross-process delivery, locking and tamper detection of the file bus",
        "an external bus (NATS): not introduced (ADR-0021)",
    ),
    limitations=(
        "golden outputs are only persistence-independent observables (record hashes, delivery "
        "sequence hashes)",
    ),
)


def _outputs(bus: EventBusAdapter, records: tuple[object, ...]) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {"rounds": Decimal(len(records))}
    for index, record in enumerate(records):
        values[f"round.{index}.record_hash"] = Decimal(int(record.record_hash, 16))  # type: ignore[attr-defined]
    delivered: dict[str, tuple[BusMessage, ...]] = {}
    for topic in TOPICS:
        delivered[topic] = bus.poll(CONSUMER, topic, 10_000)
        ids = [message.message_id for message in delivered[topic]]
        values[f"bus.{topic}.count"] = Decimal(len(ids))
        values[f"bus.{topic}.delivery_hash"] = Decimal(int(content_hash(ids), 16))
    round_hashes = [m.payload["record_hash"] for m in delivered[ROUND_TOPIC]]
    assert round_hashes == [record.record_hash for record in records]  # type: ignore[attr-defined]
    for topic in TOPICS:
        for message in delivered[topic]:
            bus.ack(CONSUMER, topic, message.message_id)
    values["bus.pending_after_ack"] = Decimal(
        sum(len(bus.poll(CONSUMER, t, 10_000)) for t in TOPICS)
    )
    return values


def _run_loop(tmp: Path, bus: EventBusAdapter, seed: int = 11) -> dict[str, Decimal]:
    memory = ResearchMemory(failures=FailureRegistry(tmp / "failures.jsonl"))
    llm = ScriptedLLMProvider(
        [fx.llm_output(i, lookback) for i, lookback in enumerate((240, 1440, None))],
        clock=lambda: fx.T0,
    )
    loop = build_synthetic_loop(
        fx.config(seed=seed), provider=RandomWalkMarket(), bus=bus, memory=memory, llm=llm
    )
    return _outputs(bus, loop.run_unattended(ROUNDS))


class _File:
    """Opens file buses under ``root`` and closes every one at teardown."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._count = itertools.count()
        self.opened: list[FileEventBus] = []

    def __call__(self) -> FileEventBus:
        bus = FileEventBus(self._root / f"bus-{next(self._count)}")
        self.opened.append(bus)
        return bus


@pytest.fixture
def file_bus(tmp_path: Path) -> Iterator[_File]:
    opener = _File(tmp_path)
    yield opener
    for bus in opener.opened:
        bus.close()


@pytest.fixture(scope="module")
def golden(tmp_path_factory: pytest.TempPathFactory) -> GoldenRecord:
    """The source (in-memory bus) run, recorded before the migration."""
    scratch = tmp_path_factory.mktemp("golden")
    return record_golden(
        "event_bus_loop_three_rounds", lambda: _run_loop(scratch, InMemoryEventBus())
    )


@pytest.fixture(scope="module")
def migrated(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Decimal]:
    """The same scenario on the target (file bus)."""
    root = tmp_path_factory.mktemp("migrated")
    with FileEventBus(root / "bus") as bus:
        return _run_loop(root, bus)


@pytest.fixture(scope="module")
def rolled_back(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Decimal]:
    """The scenario after switching back to the in-memory bus (a genuine rerun)."""
    return _run_loop(tmp_path_factory.mktemp("rolled_back"), InMemoryEventBus())


# --- conformance -------------------------------------------------------------------------------


def _conformance(file_bus: _File) -> tuple[ConformanceReport, ConformanceReport]:
    return (
        run_conformance(SOURCE, InMemoryEventBus, bus_suite.BUS_CHECKS),
        run_conformance(TARGET, file_bus, bus_suite.BUS_CHECKS),
    )


def test_source_and_target_pass_the_event_bus_suite(file_bus: _File) -> None:
    for report in _conformance(file_bus):
        assert report.passed, report.failures
        assert report.checks == tuple(check.__name__ for check in bus_suite.BUS_CHECKS)
    assert len(file_bus.opened) == len(bus_suite.BUS_CHECKS), "a fresh target bus per check"


class _Forgetful(InMemoryEventBus):
    """Faulty target: never redelivers an unacknowledged message (not at-least-once)."""

    def __init__(self) -> None:
        super().__init__()
        self._seen: set[tuple[str, str]] = set()

    def poll(self, consumer: str, topic: str, limit: int) -> tuple[BusMessage, ...]:
        fresh = tuple(
            m
            for m in super().poll(consumer, topic, limit)
            if (consumer, m.message_id) not in self._seen
        )
        self._seen.update((consumer, m.message_id) for m in fresh)
        return fresh


def test_a_faulty_target_fails_conformance() -> None:
    report = run_conformance("faulty_bus", _Forgetful, bus_suite.BUS_CHECKS)
    assert not report.passed
    assert [name for name, _ in report.failures] == [
        "check_at_least_once_until_ack",
        "check_publish_order_within_a_topic",
    ]


# --- golden loop scenario ----------------------------------------------------------------------


def test_the_golden_records_what_the_loop_and_the_bus_delivered(golden: GoldenRecord) -> None:
    outputs = golden.outputs
    assert outputs["rounds"] == ROUNDS
    assert [key for key in outputs if key.startswith("round.")] == [
        f"round.{i}.record_hash" for i in range(ROUNDS)
    ]
    assert outputs[f"bus.{ROUND_TOPIC}.count"] == ROUNDS
    assert outputs[f"bus.{STAGE_TOPIC}.count"] > 0
    assert outputs["bus.pending_after_ack"] == 0
    assert len({outputs[f"round.{i}.record_hash"] for i in range(ROUNDS)}) == ROUNDS


def test_the_file_bus_reproduces_the_golden_at_tolerance_zero(
    golden: GoldenRecord, migrated: dict[str, Decimal]
) -> None:
    diff = MIGRATION.compare(golden, lambda: migrated)
    assert diff.passed and diff.bit_identical and diff.differences == {}
    assert MIGRATION.excluded_outputs == () and diff.tolerance == ZERO
    assert diff.rerun_hash == diff.golden_hash == golden.outputs_hash


def test_a_different_market_seed_is_reported(golden: GoldenRecord, tmp_path: Path) -> None:
    with FileEventBus(tmp_path / "bus") as bus:
        other = _run_loop(tmp_path, bus, seed=12)
    diff = MIGRATION.compare(golden, lambda: other)
    assert not diff.passed
    assert "round.0.record_hash" in diff.differences or "round.1.record_hash" in diff.differences


class _Dropping(InMemoryEventBus):
    """Faulty target: loses the first stage message."""

    def __init__(self) -> None:
        super().__init__()
        self._dropped = False

    def publish(self, message: BusMessage) -> None:
        if message.topic == STAGE_TOPIC and not self._dropped:
            self._dropped = True
            return
        super().publish(message)


class _Reordering(InMemoryEventBus):
    """Faulty target: delivers each poll newest first."""

    def poll(self, consumer: str, topic: str, limit: int) -> tuple[BusMessage, ...]:
        return tuple(reversed(super().poll(consumer, topic, limit)))


@pytest.mark.parametrize(
    ("bus_class", "expected"),
    [
        (_Dropping, {f"bus.{STAGE_TOPIC}.count", f"bus.{STAGE_TOPIC}.delivery_hash"}),
        (_Reordering, {f"bus.{ROUND_TOPIC}.delivery_hash", f"bus.{STAGE_TOPIC}.delivery_hash"}),
    ],
)
def test_a_lossy_or_reordering_bus_is_caught_by_the_delivery_outputs(
    golden: GoldenRecord,
    tmp_path: Path,
    bus_class: Callable[[], EventBusAdapter],
    expected: set[str],
) -> None:
    bus = bus_class()
    if bus_class is _Reordering:  # the loop itself needs ordered delivery of its own topics
        loop_bus = InMemoryEventBus()
        outputs = _run_loop(tmp_path, loop_bus)
        records_view = _outputs_view_reordered(loop_bus)
        outputs.update(records_view)
    else:
        outputs = _run_loop(tmp_path, bus)
    diff = MIGRATION.compare(golden, lambda: outputs)
    assert expected <= set(diff.differences)


def _outputs_view_reordered(source: InMemoryEventBus) -> dict[str, Decimal]:
    """Delivery hashes as a reordering consumer would compute them (newest first)."""
    values: dict[str, Decimal] = {}
    for topic in TOPICS:
        ids = [m.message_id for m in reversed(source.poll("other-consumer", topic, 10_000))]
        values[f"bus.{topic}.delivery_hash"] = Decimal(int(content_hash(ids), 16))
    return values


# --- rollback and report -----------------------------------------------------------------------


def test_switching_back_to_the_memory_bus_restores_the_golden_bit_exactly(
    golden: GoldenRecord, migrated: dict[str, Decimal], rolled_back: dict[str, Decimal]
) -> None:
    migrated_diff = compare_golden(golden, lambda: migrated, MIGRATION.tolerance)
    back = compare_golden(golden, lambda: rolled_back, ZERO)
    assert back.passed and back.bit_identical
    evidence = rollback_evidence(MIGRATION.migration_id, golden, migrated_diff, back)
    assert evidence.verdict is RollbackVerdict.RESTORED and evidence.residual_differences == ()
    assert evidence.migrated_passed


def test_the_migration_report_passes_and_round_trips(
    tmp_path: Path,
    file_bus: _File,
    golden: GoldenRecord,
    migrated: dict[str, Decimal],
    rolled_back: dict[str, Decimal],
) -> None:
    evidence = rollback_evidence(
        MIGRATION.migration_id,
        golden,
        compare_golden(golden, lambda: migrated, MIGRATION.tolerance),
        compare_golden(golden, lambda: rolled_back, ZERO),
    )
    report = MigrationReport(
        target=MIGRATION,
        golden_name=golden.name,
        golden_record_hash=golden.record_hash,
        conformance=_conformance(file_bus),
        golden_diff=MIGRATION.compare(golden, lambda: migrated),
        rollback=evidence,
    )
    assert report.passed and report.verdict == "passed" and report.failures == ()
    path = save_migration_report(report, tmp_path / "reports")
    loaded = load_migration_report(path.parent, report.report_hash)
    assert loaded == report and loaded.passed and "verdict: PASSED" in loaded.render()

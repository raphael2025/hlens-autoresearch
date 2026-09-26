"""Phase 14: saved golden records, rerun reports, rollback evidence, event-bus conformance bridge.

ADR-0047. Everything writes into ``tmp_path`` only.
"""

from __future__ import annotations

import itertools
import json
import os
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from core.contracts.event_bus import BusMessage
from core.domain.base import canonical_json, content_hash
from infrastructure.event_bus import FileEventBus, InMemoryEventBus
from infrastructure.migration import (
    GoldenDiff,
    GoldenError,
    RollbackVerdict,
    compare_golden,
    load_golden,
    record_golden,
    rollback_evidence,
    run_conformance,
    save_golden,
)
from tests.contract_suites import event_bus as bus_suite

OUTPUTS = {"sharpe": Decimal("1.25"), "trades": Decimal(40)}
DRIFT = {"sharpe": Decimal("1.2501"), "trades": Decimal(40)}

#: ``outputs_hash`` of OUTPUTS, captured with the pre-change golden.py (1843ff5); must not move.
OUTPUTS_HASH = "9dbcbf3ca5079a03c7d17f8ef231c2749c112f8b8f8d87b500c7f30452ef7803"
#: ``record_hash`` (saved file name) of record_golden("exp", OUTPUTS).
RECORD_HASH = "0aaca75a975cb21e12dc09676d8048ccb60f3d45a7bdc7405930504b3dd01a5c"


def test_hashes_are_pinned() -> None:
    golden = record_golden("exp", lambda: dict(OUTPUTS))
    assert golden.outputs_hash == OUTPUTS_HASH
    assert golden.record_hash == RECORD_HASH


# --------------------------------------------------------------------------------------
# save / load
# --------------------------------------------------------------------------------------


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    golden = record_golden("exp", lambda: dict(OUTPUTS))
    path = save_golden(golden, tmp_path)
    assert path == tmp_path / f"{golden.record_hash}.json"
    loaded = load_golden(tmp_path, golden.record_hash)
    assert loaded == golden
    assert compare_golden(loaded, lambda: dict(OUTPUTS), Decimal(0)).bit_identical


def test_an_identical_re_save_is_idempotent(tmp_path: Path) -> None:
    golden = record_golden("exp", lambda: dict(OUTPUTS))
    first = save_golden(golden, tmp_path)
    before = first.read_bytes()
    assert save_golden(golden, tmp_path) == first
    assert first.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [first.name]


def test_trailing_zeros_are_part_of_the_record(tmp_path: Path) -> None:
    a = record_golden("exp", lambda: {"x": Decimal("1.0")})
    b = record_golden("exp", lambda: {"x": Decimal("1.00")})
    assert a.record_hash != b.record_hash
    save_golden(a, tmp_path)
    assert load_golden(tmp_path, a.record_hash).outputs["x"].as_tuple() == Decimal("1.0").as_tuple()


def test_a_record_whose_hash_disagrees_with_its_outputs_is_not_saved(tmp_path: Path) -> None:
    golden = record_golden("exp", lambda: dict(OUTPUTS))
    forged = type(golden)(name="exp", outputs=dict(DRIFT), outputs_hash=golden.outputs_hash)
    with pytest.raises(GoldenError, match="outputs_hash does not match"):
        save_golden(forged, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_an_occupied_address_with_different_bytes_is_refused(tmp_path: Path) -> None:
    golden = record_golden("exp", lambda: dict(OUTPUTS))
    (tmp_path / f"{golden.record_hash}.json").write_bytes(b"something else")
    with pytest.raises(GoldenError, match="different file"):
        save_golden(golden, tmp_path)


def _tamper(path: Path, edit: object) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(edit(raw)), encoding="utf-8")  # type: ignore[operator]


@pytest.mark.parametrize(
    "edit",
    [
        lambda raw: dict(raw, outputs=dict(raw["outputs"], sharpe="1.2501")),
        lambda raw: dict(raw, name="other"),
        lambda raw: raw,  # re-serialised (non-canonical spacing) with the same content
    ],
)
def test_a_tampered_file_is_refused(tmp_path: Path, edit: object) -> None:
    golden = record_golden("exp", lambda: dict(OUTPUTS))
    path = save_golden(golden, tmp_path)
    _tamper(path, edit)
    with pytest.raises(GoldenError, match="does not hash to its name"):
        load_golden(tmp_path, golden.record_hash)


def _plant(tmp_path: Path, payload: object) -> str:
    """Write ``payload`` under its own hash (a self-consistent but invalid file)."""
    digest = content_hash(payload)
    (tmp_path / f"{digest}.json").write_text(canonical_json(payload), encoding="utf-8")
    return digest


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"outputs_hash": "0" * 64}, "outputs_hash does not match"),
        ({"format": "other"}, "not a golden record"),
        ({"extra": 1}, "not a golden record"),
        ({"outputs": {"sharpe": "NaN"}}, "finite Decimal"),
        ({"outputs": {"sharpe": "abc"}}, "not a decimal"),
        ({"outputs": {"sharpe": 1.25}}, "not a golden record"),
        ({"outputs": {"sharpe": " 1.25", "trades": "40"}}, "not in canonical form"),
    ],
)
def test_self_consistent_but_invalid_files_are_refused(
    tmp_path: Path, change: dict[str, object], match: str
) -> None:
    golden = record_golden("exp", lambda: dict(OUTPUTS))
    payload: dict[str, object] = {
        "format": "hlens.golden_record",
        "schema_version": "1.0.0",
        "name": "exp",
        "outputs": {"sharpe": "1.25", "trades": "40"},
        "outputs_hash": golden.outputs_hash,
    }
    payload.update(change)
    with pytest.raises(GoldenError, match=match):
        load_golden(tmp_path, _plant(tmp_path, payload))


def test_missing_records_and_bad_hashes_are_refused(tmp_path: Path) -> None:
    with pytest.raises(GoldenError, match="unreadable"):
        load_golden(tmp_path, "a" * 64)
    with pytest.raises(GoldenError, match="not a golden record hash"):
        load_golden(tmp_path, "../x")


# --------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------


def test_the_rerun_report_is_deterministic() -> None:
    golden = record_golden("exp", lambda: {**OUTPUTS, "gone": Decimal(1)})
    diff = compare_golden(golden, lambda: {**DRIFT, "new": Decimal(2)}, Decimal(0))
    report = diff.report()
    assert (
        report == compare_golden(golden, lambda: {**DRIFT, "new": Decimal(2)}, Decimal(0)).report()
    )
    assert report.splitlines() == [
        "golden rerun report: exp",
        "tolerance: 0",
        f"golden outputs_hash: {golden.outputs_hash}",
        f"rerun outputs_hash: {diff.rerun_hash}",
        "bit_identical: no",
        "verdict: FAIL",
        "differences: 3",
        "  gone: golden=1 rerun=<missing>",
        "  new: golden=<missing> rerun=2",
        "  sharpe: golden=1.25 rerun=1.2501 delta=0.0001",
    ]


def test_a_passing_report() -> None:
    golden = record_golden("exp", lambda: dict(OUTPUTS))
    diff = compare_golden(golden, lambda: dict(OUTPUTS), Decimal(0))
    assert diff.golden_hash == diff.rerun_hash == golden.outputs_hash
    assert "verdict: PASS" in diff.report() and "bit_identical: yes" in diff.report()


# --------------------------------------------------------------------------------------
# rollback evidence
# --------------------------------------------------------------------------------------


def test_a_bit_identical_rollback_is_restored() -> None:
    golden = record_golden("exp", lambda: dict(OUTPUTS))
    migrated = compare_golden(golden, lambda: dict(DRIFT), Decimal("0.001"))
    rolled_back = compare_golden(golden, lambda: dict(OUTPUTS), Decimal(0))
    evidence = rollback_evidence("mig-0001", golden, migrated, rolled_back)
    assert evidence.verdict is RollbackVerdict.RESTORED
    assert evidence.migrated_passed and evidence.migrated_hash != evidence.golden_hash
    assert evidence.rolled_back_hash == evidence.golden_hash == golden.outputs_hash
    assert evidence.golden_record_hash == golden.record_hash
    assert evidence.residual_differences == ()
    again = rollback_evidence("mig-0001", golden, migrated, rolled_back)
    assert again.evidence_hash == evidence.evidence_hash


def test_a_rollback_that_does_not_restore_is_reported() -> None:
    golden = record_golden("exp", lambda: dict(OUTPUTS))
    migrated = compare_golden(golden, lambda: dict(DRIFT), Decimal(0))
    rolled_back = compare_golden(golden, lambda: dict(DRIFT), Decimal(0))
    evidence = rollback_evidence("mig-0001", golden, migrated, rolled_back)
    assert evidence.verdict is RollbackVerdict.NOT_RESTORED
    assert evidence.residual_differences == ("sharpe",)
    assert not evidence.migrated_passed


def test_rollback_evidence_refuses_inconsistent_inputs() -> None:
    golden = record_golden("exp", lambda: dict(OUTPUTS))
    other = record_golden("other", lambda: dict(OUTPUTS))
    ok = compare_golden(golden, lambda: dict(OUTPUTS), Decimal(0))
    with pytest.raises(GoldenError, match="migration id"):
        rollback_evidence(" ", golden, ok, ok)
    foreign = compare_golden(other, lambda: dict(OUTPUTS), Decimal(0))
    with pytest.raises(GoldenError, match="not compared against"):
        rollback_evidence("m", golden, foreign, ok)
    tolerant = compare_golden(golden, lambda: dict(DRIFT), Decimal("0.001"))
    with pytest.raises(GoldenError, match="tolerance 0"):
        rollback_evidence("m", golden, ok, tolerant)
    legacy = GoldenDiff(name="exp", tolerance=Decimal(0), bit_identical=True, differences={})
    with pytest.raises(GoldenError, match="not compared against"):
        rollback_evidence("m", golden, ok, legacy)
    forged = GoldenDiff(
        name="exp",
        tolerance=Decimal(0),
        bit_identical=True,
        differences={},
        golden_hash=golden.outputs_hash,
        rerun_hash="f" * 64,
    )
    with pytest.raises(GoldenError, match="inconsistent"):
        rollback_evidence("m", golden, ok, forged)


# --------------------------------------------------------------------------------------
# event-bus contract suite through run_conformance (10-migration.md §2: 事件总线)
# --------------------------------------------------------------------------------------


@pytest.fixture
def file_buses(tmp_path: Path) -> Iterator[list[FileEventBus]]:
    opened: list[FileEventBus] = []
    yield opened
    for bus in opened:
        bus.close()


def test_the_event_bus_suite_runs_as_a_conformance_check(
    tmp_path: Path, file_buses: list[FileEventBus]
) -> None:
    counter = itertools.count()

    def make_file_bus() -> FileEventBus:
        bus = FileEventBus(tmp_path / f"bus-{next(counter)}")
        file_buses.append(bus)
        return bus

    for candidate, make in (
        ("hlens_event_bus_memory", InMemoryEventBus),
        ("hlens_event_bus_file", make_file_bus),
    ):
        report = run_conformance(candidate, make, bus_suite.BUS_CHECKS)
        assert report.passed, report.failures
        assert report.checks == tuple(check.__name__ for check in bus_suite.BUS_CHECKS)
    assert len(file_buses) == len(bus_suite.BUS_CHECKS), "a fresh bus per check"
    assert len(os.listdir(tmp_path)) == len(bus_suite.BUS_CHECKS)


class _ForgetfulBus(InMemoryEventBus):
    """A non-conforming candidate: never redelivers an unacknowledged message."""

    def __init__(self) -> None:
        super().__init__()
        self._seen: set[tuple[str, str]] = set()

    def poll(self, consumer: str, topic: str, limit: int) -> tuple[BusMessage, ...]:
        fresh = tuple(
            message
            for message in super().poll(consumer, topic, limit)
            if (consumer, message.message_id) not in self._seen
        )
        self._seen.update((consumer, message.message_id) for message in fresh)
        return fresh


def test_a_non_conforming_bus_fails_the_conformance_run() -> None:
    report = run_conformance("forgetful", _ForgetfulBus, bus_suite.BUS_CHECKS)
    assert not report.passed
    assert [name for name, _ in report.failures] == [
        "check_at_least_once_until_ack",
        "check_publish_order_within_a_topic",
    ]

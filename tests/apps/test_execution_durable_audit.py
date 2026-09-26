"""Phase 13 (ADR-0046): durable execution audit, fail-closed reopen, and read-only replay.

Simulation only. The limit numbers come from the ``test_execution`` fixtures and are not budgets.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from apps.execution import (
    RESTORE_TRIPPED_BY,
    AuditCorrupted,
    AuditTrail,
    ExecutionService,
    KillSwitch,
    KillSwitchTrip,
    Monitor,
    RejectionSource,
    SecondLineRisk,
    SimulatedVenue,
    replay_audit,
)
from core.domain.execution import ExecutionMode
from infrastructure.event_bus import InMemoryEventBus
from tests.apps.test_execution import COSTS, KB, KE, T0, Rig, _clock, _lifecycle, _limits, _scenario


class DurableRig(Rig):
    """``Rig`` whose service writes a durable audit at ``path``."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.service = _service(path, self.kill_switch, self.venue, self.bus)
        self.ladder = self.service.admit(
            self.deployment, self.artifact, _lifecycle(self.artifact.strategy_spec)
        )


def _service(
    path: Path,
    kill_switch: KillSwitch | None = None,
    venue: SimulatedVenue | None = None,
    bus: InMemoryEventBus | None = None,
) -> ExecutionService:
    risk = SecondLineRisk(_limits())
    return ExecutionService(
        mode=ExecutionMode.SIMULATED,
        venue=venue or SimulatedVenue(venue_id="sim-1", cost_model=COSTS),
        kill_switch=kill_switch or KillSwitch(),
        risk=risk,
        monitor=Monitor(capital=risk.limits.capital, max_drawdown=Decimal(5_000)),
        bus=bus or InMemoryEventBus(),
        clock=_clock(),
        audit=AuditTrail(path),
        record_marks=False,  # explicit for a durable audit; False keeps the pinned heads
    )


def _run(tmp_path: Path) -> DurableRig:
    rig = DurableRig(tmp_path / "audit.jsonl")
    _scenario(rig)
    return rig


def test_a_durable_audit_holds_exactly_what_the_in_memory_audit_holds(tmp_path: Path) -> None:
    durable, plain = _run(tmp_path), Rig()
    _scenario(plain)
    assert durable.service.audit.durable and not plain.service.audit.durable
    assert durable.service.audit.head() == plain.service.audit.head()
    reopened = AuditTrail(durable.path)
    assert reopened.entries == durable.service.audit.entries
    assert reopened.head_hash == durable.service.audit.head_hash is not None


def test_replay_rebuilds_positions_and_fees_from_the_fills(tmp_path: Path) -> None:
    rig = _run(tmp_path)
    replay = replay_audit(rig.path)
    deployment = rig.deployment.deployment_id
    assert dict(replay.positions[deployment]) == dict(
        (k, q) for k, q in rig.venue.positions(deployment).items() if q != 0
    )
    assert replay.fees[deployment] == sum((f.fee for f in rig.venue.fills), Decimal(0))
    assert replay.incomplete_orders == () and replay.kill_switch_tripped
    assert replay.trail.head() == rig.service.audit.head()


def test_reopening_a_non_empty_audit_starts_halted_and_sends_no_order(tmp_path: Path) -> None:
    rig = DurableRig(tmp_path / "audit.jsonl")
    rig.service.submit_targets(rig.targets(btc=10), {KB: Decimal(100)})  # no trip before restart
    history = AuditTrail(rig.path).entries
    prior_max_sequence = max(o.sequence for o in rig.service.audit.orders)

    restarted = DurableRig(rig.path)
    trips = restarted.service.audit.trips
    assert restarted.kill_switch.tripped and trips[-1].tripped_by == RESTORE_TRIPPED_BY
    prices = {KB: Decimal(100), KE: Decimal(50)}
    report = restarted.service.submit_targets(restarted.targets(btc=10, eth=1), prices)
    assert report.orders and report.fills == () and restarted.venue.fills == ()
    assert {r.source for r in report.rejections} == {RejectionSource.KILL_SWITCH}
    # the old history is intact, followed by the restore trip and the new (rejected) records
    reopened = AuditTrail(rig.path)
    assert reopened.entries[: len(history)] == history
    assert isinstance(reopened.entries[len(history)], KillSwitchTrip)
    assert reopened.incomplete_orders() == ()
    # order sequence numbers continue instead of restarting at zero
    assert all(o.sequence > prior_max_sequence for o in report.orders)


def test_an_empty_durable_audit_does_not_trip(tmp_path: Path) -> None:
    service = _service(tmp_path / "fresh.jsonl")
    assert not service.kill_switch.tripped and service.audit.entries == ()


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_tampered_record_is_refused(tmp_path: Path) -> None:
    rig = _run(tmp_path)
    text = rig.path.read_text(encoding="utf-8")
    rig.path.write_text(text.replace('"BUY"', '"SELL"', 1), encoding="utf-8")
    with pytest.raises(AuditCorrupted):
        AuditTrail(rig.path)
    with pytest.raises(AuditCorrupted):
        replay_audit(rig.path)


def test_a_partial_trailing_line_is_refused(tmp_path: Path) -> None:
    rig = _run(tmp_path)
    text = rig.path.read_text(encoding="utf-8")
    rig.path.write_text(text[:-10], encoding="utf-8")
    with pytest.raises(AuditCorrupted):
        AuditTrail(rig.path)


def _rewrite(path: Path, lines: list[dict[str, object]]) -> None:
    """Re-chain edited lines with the worker journal (a *well-formed* but lying file)."""
    from apps.worker.journal import AppendOnlyJournal

    path.unlink()
    journal = AppendOnlyJournal(path)
    for line in lines:
        journal.append(str(line["type"]), line["payload"])  # type: ignore[arg-type]


def test_a_record_whose_content_does_not_match_its_id_is_refused(tmp_path: Path) -> None:
    rig = _run(tmp_path)
    lines = _lines(rig.path)
    payload = lines[0]["payload"]
    assert isinstance(payload, dict)
    payload["record"]["quantity"] = "999"
    _rewrite(rig.path, lines)
    with pytest.raises(AuditCorrupted, match="record_id"):
        AuditTrail(rig.path)


def test_an_unknown_record_type_and_a_duplicate_are_refused(tmp_path: Path) -> None:
    rig = _run(tmp_path)
    lines = _lines(rig.path)
    _rewrite(rig.path, [{**lines[0], "type": "WireTransfer"}, *lines[1:]])
    with pytest.raises(AuditCorrupted, match="unknown record type"):
        AuditTrail(rig.path)
    _rewrite(rig.path, [lines[0], lines[0], *lines[1:]])
    with pytest.raises(AuditCorrupted, match="repeats"):
        AuditTrail(rig.path)


def test_a_fill_without_its_order_is_refused_by_replay(tmp_path: Path) -> None:
    rig = _run(tmp_path)
    lines = [line for line in _lines(rig.path) if line["type"] != "OrderRecord"]
    _rewrite(rig.path, lines)
    AuditTrail(rig.path)  # structurally fine ...
    with pytest.raises(AuditCorrupted, match="does not answer a recorded order"):
        replay_audit(rig.path)  # ... but it does not prove what was traded


def test_the_in_memory_default_is_unchanged() -> None:
    rig = Rig()
    _scenario(rig)
    assert not rig.service.audit.durable and rig.service.audit.head_hash is None
    assert T0 <= rig.service.audit.orders[0].submitted_at

"""Phase 13 (风险与事件可重放): MarkRecord + ``replay_risk`` over an execution audit.

Simulation only. The limit numbers come from the ``test_execution`` fixtures and are not budgets.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from apps.execution import (
    Alert,
    AlertKind,
    AuditCorrupted,
    AuditTrail,
    ExecutionService,
    KillSwitch,
    MarkPrice,
    MarkRecord,
    Monitor,
    RiskLimits,
    RiskReplayDiverged,
    SecondLineRisk,
    SimulatedVenue,
    replay_risk,
)
from apps.worker.journal import AppendOnlyJournal
from core.domain.execution import ExecutionMode
from infrastructure.event_bus import InMemoryEventBus
from tests import factories
from tests.apps.test_execution import COSTS, KB, KE, T0, Rig, _clock, _lifecycle, _limits, _scenario


class MarkRig(Rig):
    """``Rig`` whose service records marks (optionally into a durable audit at ``path``)."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        limits: RiskLimits | None = None,
        max_drawdown: Decimal = Decimal(5_000),
        record_marks: bool = True,
    ) -> None:
        self._path = path
        self.bus = InMemoryEventBus()
        self.kill_switch = KillSwitch()
        self.risk = SecondLineRisk(limits or _limits())
        self.monitor = Monitor(capital=self.risk.limits.capital, max_drawdown=max_drawdown)
        self.venue = SimulatedVenue(venue_id="sim-1", cost_model=COSTS)
        self.service = ExecutionService(
            mode=ExecutionMode.SIMULATED,
            venue=self.venue,
            kill_switch=self.kill_switch,
            risk=self.risk,
            monitor=self.monitor,
            bus=self.bus,
            clock=_clock(),
            audit=AuditTrail(path),
            record_marks=record_marks,
        )
        self.artifact = factories.strategy_artifact()
        self.deployment = factories.deployment_record(
            equivalence=factories.equivalence_check(artifact_id=self.artifact.artifact_id)
        )
        self.ladder = self.service.admit(
            self.deployment, self.artifact, _lifecycle(self.artifact.strategy_spec)
        )

    @property
    def path(self) -> Path:
        assert self._path is not None, "this rig keeps its audit in memory"
        return self._path


def _loss_scenario(rig: Rig) -> None:
    """Exposure rejection, fills, a max_loss rejection, then de-risking that passes."""
    rig.service.submit_targets(rig.targets(btc=10, eth=4), {KB: Decimal(100), KE: Decimal(50)})
    rig.service.submit_targets(rig.targets(btc=70, eth=4), {KB: Decimal(101), KE: Decimal(49)})
    rig.service.submit_targets(rig.targets(btc=20, eth=4), {KB: Decimal(100), KE: Decimal(50)})
    rig.service.submit_targets(rig.targets(btc=20, eth=8), {KB: Decimal(40), KE: Decimal(50)})
    rig.service.submit_targets(rig.targets(btc=0, eth=4), {KB: Decimal(40), KE: Decimal(50)})


def _drawdown_scenario(rig: Rig) -> None:
    """A drawdown alert whose hook trips the kill switch, then kill-switch rejections."""

    def hook(alert: Alert) -> None:
        if alert.kind is AlertKind.DRAWDOWN_BREACH:
            rig.service.trip_kill_switch(reason=alert.message, tripped_by="monitor")

    rig.monitor.add_alert_hook(hook)
    rig.service.submit_targets(rig.targets(btc=20), {KB: Decimal(100)})
    rig.service.submit_targets(rig.targets(btc=20), {KB: Decimal(70)})  # mark only: breach
    rig.service.submit_targets(rig.targets(btc=0, eth=1), {KB: Decimal(70), KE: Decimal(50)})


_DRAWDOWN = Decimal(500)
_SCENARIOS: dict[str, tuple[Callable[[Rig], None], Decimal]] = {
    "base": (_scenario, Decimal(5_000)),
    "loss": (_loss_scenario, Decimal(5_000)),
    "drawdown": (_drawdown_scenario, _DRAWDOWN),
}


def _run(tmp_path: Path, name: str) -> MarkRig:
    scenario, max_drawdown = _SCENARIOS[name]
    rig = MarkRig(tmp_path / "audit.jsonl", max_drawdown=max_drawdown)
    scenario(rig)
    return rig


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_replay_reproduces_every_rejection_acceptance_and_alert(tmp_path: Path, name: str) -> None:
    rig = _run(tmp_path, name)
    audit = rig.service.audit
    assert audit.marks and audit.rejections and audit.alerts
    result = replay_risk(rig.path, _limits(), max_drawdown=_SCENARIOS[name][1])
    assert result.verified_rejections == tuple(r.record_id for r in audit.rejections)
    assert result.verified_alerts == tuple(a.record_id for a in audit.alerts)
    assert result.verified_acceptances == tuple(f.order_id for f in audit.fills)
    assert result.unverified_orders == () and result.sessions == 1
    # the in-memory trail replays to the same result
    same = replay_risk(audit, _limits(), max_drawdown=_SCENARIOS[name][1])
    assert same.verified_alerts == result.verified_alerts


def test_the_drawdown_scenario_really_raises_the_alert_chain(tmp_path: Path) -> None:
    rig = _run(tmp_path, "drawdown")
    kinds = [a.kind for a in rig.service.audit.alerts]
    assert kinds[:2] == [AlertKind.DRAWDOWN_BREACH, AlertKind.KILL_SWITCH_TRIPPED]
    assert AlertKind.RISK_REJECTION in kinds


def test_marks_are_opt_in_and_the_default_audit_head_is_unchanged(tmp_path: Path) -> None:
    """Pin old vs new: without ``record_marks`` the audit is exactly what it was before."""
    default, marked, plain = MarkRig(record_marks=False), MarkRig(tmp_path / "a.jsonl"), Rig()
    for rig in (default, marked, plain):
        _scenario(rig)
    assert default.service.audit.marks == () and marked.service.audit.marks
    assert default.service.audit.head() == plain.service.audit.head()
    assert marked.service.audit.head() != plain.service.audit.head()
    # recording marks adds MarkRecords and changes nothing else (same clock ticks, same records)
    without_marks = [r for r in marked.service.audit.entries if not isinstance(r, MarkRecord)]
    assert without_marks == list(plain.service.audit.entries)
    assert len(marked.service.audit.marks) == 4  # one per submit_targets batch
    assert marked.bus_ids(MarkRecord) == [m.record_id for m in marked.service.audit.marks]
    reopened = AuditTrail(marked.path)
    assert reopened.entries == marked.service.audit.entries


def test_a_mark_record_is_sorted_unique_and_positive() -> None:
    with pytest.raises(ValueError, match="sorted"):
        MarkRecord(
            sequence=0,
            deployment_id="d",
            prices=(MarkPrice(key="b", price=Decimal(1)), MarkPrice(key="a", price=Decimal(1))),
            marked_at=T0,
        )
    with pytest.raises(ValueError):
        MarkPrice(key="a", price=Decimal(0))


def test_an_invalid_price_records_no_mark() -> None:
    rig = MarkRig()
    with pytest.raises(ValueError, match="finite, positive"):
        rig.service.submit_targets(rig.targets(btc=1), {KB: Decimal("-1")})
    assert rig.service.audit.entries == ()


def test_an_audit_without_marks_is_refused() -> None:
    rig = MarkRig(record_marks=False)
    _scenario(rig)
    with pytest.raises(RiskReplayDiverged, match="record_marks"):
        replay_risk(rig.service.audit, _limits(), max_drawdown=Decimal(5_000))


def test_other_limits_diverge_at_the_first_differing_decision(tmp_path: Path) -> None:
    rig = _run(tmp_path, "base")
    stricter = _limits(max_gross_exposure=Decimal(900))
    with pytest.raises(RiskReplayDiverged, match="replay rejects it") as caught:
        replay_risk(rig.path, stricter, max_drawdown=Decimal(5_000))
    assert caught.value.record == rig.service.audit.orders[0]
    looser = _limits(max_gross_exposure=Decimal(1_000_000), max_leverage=Decimal(100))
    with pytest.raises(RiskReplayDiverged, match="replay accepts it"):
        replay_risk(rig.path, looser, max_drawdown=Decimal(5_000))


def test_another_drawdown_threshold_diverges_on_the_alerts(tmp_path: Path) -> None:
    rig = _run(tmp_path, "drawdown")
    with pytest.raises(RiskReplayDiverged, match="Alert") as caught:
        replay_risk(rig.path, _limits(), max_drawdown=Decimal(5_000))
    assert isinstance(caught.value, AuditCorrupted)
    assert caught.value.record == rig.service.audit.alerts[0]
    with pytest.raises(RiskReplayDiverged, match="does not hold"):
        replay_risk(rig.path, _limits(), max_drawdown=Decimal(1))


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _rewrite(path: Path, lines: list[dict[str, object]]) -> None:
    """Re-chain edited lines with the worker journal (a *well-formed* but lying file)."""
    path.unlink()
    journal = AppendOnlyJournal(path)
    for line in lines:
        journal.append(str(line["type"]), line["payload"])  # type: ignore[arg-type]


def test_a_well_formed_audit_missing_an_alert_is_refused(tmp_path: Path) -> None:
    rig = _run(tmp_path, "drawdown")
    lines = _lines(rig.path)
    first_alert = next(i for i, line in enumerate(lines) if line["type"] == "Alert")
    _rewrite(rig.path, lines[:first_alert] + lines[first_alert + 1 :])
    AuditTrail(rig.path)  # the chain itself is fine ...
    with pytest.raises(RiskReplayDiverged, match="does not hold"):
        replay_risk(rig.path, _limits(), max_drawdown=_DRAWDOWN)  # ... the alert is missing


def test_a_rehashed_mark_that_lies_about_the_price_is_refused(tmp_path: Path) -> None:
    rig = _run(tmp_path, "drawdown")
    lines = _lines(rig.path)
    index = [i for i, line in enumerate(lines) if line["type"] == "MarkRecord"][1]
    payload = lines[index]["payload"]
    assert isinstance(payload, dict)
    forged = MarkRecord.model_validate(payload["record"]).model_copy(
        update={"prices": (MarkPrice(key=KB, price=Decimal(99)),)}
    )
    lines[index] = {
        **lines[index],
        "payload": {"record_id": forged.record_id, "record": forged.model_dump(mode="json")},
    }
    _rewrite(rig.path, lines)
    with pytest.raises(RiskReplayDiverged, match="not reproduced"):
        replay_risk(rig.path, _limits(), max_drawdown=_DRAWDOWN)


def test_a_dropped_rejection_is_refused(tmp_path: Path) -> None:
    rig = _run(tmp_path, "base")
    lines = _lines(rig.path)
    rejection = next(r for r in rig.service.audit.rejections if r.limit_name != "kill_switch")
    kept = [line for line in lines if line["payload"]["record_id"] != rejection.record_id]  # type: ignore[index]
    _rewrite(rig.path, kept)
    with pytest.raises(RiskReplayDiverged, match="Alert is not reproduced"):
        replay_risk(rig.path, _limits(), max_drawdown=Decimal(5_000))


def test_a_restart_starts_a_new_risk_session(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    first = MarkRig(path)
    first.service.submit_targets(first.targets(btc=10), {KB: Decimal(100)})
    restarted = MarkRig(path)
    restarted.service.submit_targets(restarted.targets(btc=5), {KB: Decimal(100)})
    result = replay_risk(path, _limits(), max_drawdown=Decimal(5_000))
    assert result.sessions == 2
    assert len(result.verified_rejections) == 1 and len(result.verified_acceptances) == 1
    assert result.verified_alerts == tuple(a.record_id for a in AuditTrail(path).alerts)

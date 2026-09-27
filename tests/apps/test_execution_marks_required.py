"""A durable execution audit needs an explicit ``record_marks`` choice (audit finding F9).

Without marks ``replay_risk`` refuses an audit that holds orders, so a durable audit built with the
old default (``False``) could never be risk-replayed. A durable ``AuditTrail(path)`` without an
explicit choice is now refused; the in-memory default and the pinned heads of an explicit
``record_marks=False`` are unchanged. Simulation only; the limit numbers are the
``test_execution`` fixtures, not budgets.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from apps.execution import (
    AuditTrail,
    ExecutionService,
    KillSwitch,
    MarksChoiceRequired,
    Monitor,
    RiskReplayDiverged,
    SecondLineRisk,
    SimulatedVenue,
    replay_risk,
)
from core.domain.execution import ExecutionMode
from infrastructure.event_bus import InMemoryEventBus
from tests.apps.test_execution import COSTS, Rig, _clock, _limits, _scenario
from tests.apps.test_execution_risk_replay import MarkRig

MAX_DRAWDOWN = Decimal(5_000)


def _service(audit: AuditTrail | None, **kwargs: object) -> ExecutionService:
    risk = SecondLineRisk(_limits())
    return ExecutionService(
        mode=ExecutionMode.SIMULATED,
        venue=SimulatedVenue(venue_id="sim-1", cost_model=COSTS),
        kill_switch=KillSwitch(),
        risk=risk,
        monitor=Monitor(capital=risk.limits.capital, max_drawdown=MAX_DRAWDOWN),
        bus=InMemoryEventBus(),
        clock=_clock(),
        audit=audit,
        **kwargs,  # type: ignore[arg-type]
    )


def test_a_durable_audit_without_an_explicit_marks_choice_is_refused(tmp_path: Path) -> None:
    audit = AuditTrail(tmp_path / "audit.jsonl")
    with pytest.raises(MarksChoiceRequired, match="explicit record_marks") as caught:
        _service(audit)
    assert isinstance(caught.value, ValueError)
    assert audit.entries == ()  # refused before anything was recorded


@pytest.mark.parametrize("value", [None, 0, 1, "yes"])
def test_record_marks_must_be_a_bool_for_a_durable_audit(tmp_path: Path, value: object) -> None:
    with pytest.raises(MarksChoiceRequired):
        _service(AuditTrail(tmp_path / "audit.jsonl"), record_marks=value)


@pytest.mark.parametrize("choice", [True, False])
def test_an_explicit_choice_is_accepted_for_a_durable_audit(tmp_path: Path, choice: bool) -> None:
    service = _service(AuditTrail(tmp_path / "audit.jsonl"), record_marks=choice)
    assert service.audit.durable


def test_the_in_memory_default_is_unchanged() -> None:
    default, explicit = Rig(), MarkRig(record_marks=False)
    for rig in (default, explicit):
        _scenario(rig)
    assert not default.service.audit.durable
    assert default.service.audit.marks == ()
    assert default.service.audit.head() == explicit.service.audit.head()


def test_an_explicit_false_on_a_durable_audit_keeps_the_pinned_head(tmp_path: Path) -> None:
    durable, plain = MarkRig(tmp_path / "audit.jsonl", record_marks=False), Rig()
    for rig in (durable, plain):
        _scenario(rig)
    assert durable.service.audit.durable and durable.service.audit.marks == ()
    assert durable.service.audit.head() == plain.service.audit.head()
    # the choice is recorded in effect: such a trail is not risk-replayable
    with pytest.raises(RiskReplayDiverged, match="record_marks"):
        replay_risk(durable.path, _limits(), max_drawdown=MAX_DRAWDOWN)


def test_a_durable_audit_with_marks_can_be_risk_replayed(tmp_path: Path) -> None:
    rig = MarkRig(tmp_path / "audit.jsonl", record_marks=True)
    _scenario(rig)
    audit = rig.service.audit
    assert audit.durable and audit.marks
    result = replay_risk(rig.path, _limits(), max_drawdown=MAX_DRAWDOWN)
    assert result.unverified_orders == ()
    assert result.verified_acceptances == tuple(f.order_id for f in audit.fills)

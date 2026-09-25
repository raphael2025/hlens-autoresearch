"""ADR-0046 smoke tests: simulated execution only; LIVE refused; kill switch; second-line risk.

The limit numbers below are test fixtures, not proposed risk budgets.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from apps.execution import (
    TOPICS,
    Alert,
    AlertKind,
    DeploymentNotAdmitted,
    ExecutionService,
    ExecutionStage,
    KillSwitch,
    KillSwitchEngaged,
    LadderGateRecord,
    LadderGateRefused,
    LinearCostModel,
    LiveExecutionRefused,
    Monitor,
    OrderRecord,
    RejectionSource,
    RiskLimits,
    SecondLineRisk,
    Side,
    SimulatedVenue,
    TargetPosition,
    TargetPositions,
    instrument_key,
    run_kill_switch_drill,
)
from core.domain.artifact import StrategyArtifact
from core.domain.base import Kind, Ref
from core.domain.execution import ExecutionMode
from core.domain.specs import Instrument, InstrumentType, RiskPolicy
from core.lifecycle.strategy import (
    AuthorizationRecord,
    LifecycleHistory,
    LifecycleState,
    LifecycleTransition,
)
from infrastructure.event_bus import InMemoryEventBus
from tests import factories

S = LifecycleState
T0 = datetime(2026, 1, 1, tzinfo=UTC)
BTC = Instrument(
    venue="sim", symbol="BTCUSDT", instrument_type=InstrumentType.SPOT, base="BTC", quote="USDT"
)
ETH = Instrument(
    venue="sim", symbol="ETHUSDT", instrument_type=InstrumentType.SPOT, base="ETH", quote="USDT"
)
KB, KE = instrument_key(BTC), instrument_key(ETH)
COSTS = LinearCostModel(fee_rate=Decimal("0.001"), slippage_rate=Decimal("0.0005"))


def _clock() -> Callable[[], datetime]:
    ticks = iter(range(10_000))
    return lambda: T0 + timedelta(seconds=next(ticks))


def _lifecycle(subject: Ref, until: LifecycleState = S.PAPER) -> LifecycleHistory:
    path = [S.IDEA, S.CANDIDATE, S.VALIDATION, S.OOS, S.PAPER]
    history = LifecycleHistory(subject=subject)
    for i, (a, b) in enumerate(zip(path, path[1:], strict=False)):
        if a is until:
            break
        history = history.append(
            LifecycleTransition(
                subject=subject,
                from_state=a,
                to_state=b,
                reason="test",
                evidence=("report:test",),
                triggered_by="test",
                approved_by="raphael" if (a, b) == (S.OOS, S.PAPER) else None,
                occurred_at=T0 + timedelta(minutes=i),
            )
        )
    return history


def _limits(**overrides: Decimal) -> RiskLimits:
    values = {
        "capital": Decimal(10_000),
        "max_gross_exposure": Decimal(5_000),
        "max_leverage": Decimal(2),
        "max_loss": Decimal(1_000),
    }
    values.update(overrides)
    return RiskLimits.model_validate(values)


class Rig:
    def __init__(self, limits: RiskLimits | None = None, max_drawdown: Decimal = Decimal(5_000)):
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
        )
        self.artifact: StrategyArtifact = factories.strategy_artifact()
        self.deployment = factories.deployment_record(
            equivalence=factories.equivalence_check(artifact_id=self.artifact.artifact_id)
        )
        self.ladder = self.service.admit(
            self.deployment, self.artifact, _lifecycle(self.artifact.strategy_spec)
        )

    def targets(self, **quantities: int) -> TargetPositions:
        inst = {"btc": BTC, "eth": ETH}
        return TargetPositions(
            deployment_id=self.deployment.deployment_id,
            as_of=T0,
            targets=tuple(
                TargetPosition(instrument=inst[name], quantity=Decimal(q))
                for name, q in quantities.items()
            ),
        )

    def bus_ids(self, record_type: type) -> list[str]:
        return [m.key for m in self.bus.poll("audit", TOPICS[record_type], 10_000)]


# -- LIVE is refused ----------------------------------------------------------------------------


def test_live_mode_is_refused() -> None:
    rig = Rig()
    with pytest.raises(LiveExecutionRefused, match="SIMULATED only"):
        ExecutionService(
            mode=ExecutionMode.LIVE,
            venue=rig.venue,
            kill_switch=KillSwitch(),
            risk=rig.risk,
            monitor=rig.monitor,
            bus=rig.bus,
            clock=_clock(),
        )


def test_a_venue_other_than_the_simulated_one_is_refused() -> None:
    class OtherVenue(SimulatedVenue):
        pass

    rig = Rig()
    with pytest.raises(LiveExecutionRefused, match="SimulatedVenue"):
        ExecutionService(
            mode=ExecutionMode.SIMULATED,
            venue=OtherVenue(venue_id="x", cost_model=COSTS),
            kill_switch=KillSwitch(),
            risk=rig.risk,
            monitor=rig.monitor,
            bus=rig.bus,
            clock=_clock(),
        )


def test_a_live_order_record_cannot_exist() -> None:
    with pytest.raises(ValidationError, match="SIMULATED orders only"):
        OrderRecord(
            sequence=0,
            deployment_id="d",
            artifact_id="a" * 64,
            stage=ExecutionStage.SIMULATED,
            mode=ExecutionMode.LIVE,
            instrument=BTC,
            side=Side.BUY,
            quantity=Decimal(1),
            reference_price=Decimal(1),
            submitted_at=T0,
        )


def test_ladder_grants_paper_and_refuses_every_live_rung() -> None:
    rig = Rig()
    ladder = rig.ladder
    with pytest.raises(ValidationError, match="evidence"):
        ladder.promote_to_paper(evidence=(), decided_by="raphael")
    gate = ladder.promote_to_paper(evidence=("review:sim-run-1",), decided_by="raphael")
    assert gate.granted and ladder.stage is ExecutionStage.PAPER
    with pytest.raises(LadderGateRefused):
        ladder.promote_to_paper(evidence=("again",), decided_by="raphael")

    with pytest.raises(LiveExecutionRefused, match="requires an AuthorizationRecord"):
        ladder.request_live(ExecutionStage.SMALL_LIVE, requested_by="operator")
    authorization = AuthorizationRecord(
        subject=rig.artifact.strategy_spec,
        authorized_by="raphael",
        risk_budget="operator-supplied",
        authorized_at=T0,
        valid_until=T0 + timedelta(days=1),
    )
    with pytest.raises(LiveExecutionRefused, match="not available in this build"):
        ladder.request_live(
            ExecutionStage.SMALL_LIVE, requested_by="operator", authorization=authorization
        )
    with pytest.raises(LiveExecutionRefused):
        ladder.request_live(
            ExecutionStage.SCALED_LIVE, requested_by="operator", authorization=authorization
        )
    assert ladder.stage is ExecutionStage.PAPER
    refusals = [g for g in ladder.history if not g.granted]
    assert len(refusals) == 3 and all(g.to_stage is not ExecutionStage.PAPER for g in refusals)
    assert rig.service.audit.ladder_gates == ladder.history
    assert rig.bus_ids(LadderGateRecord) == [g.record_id for g in ladder.history]
    with pytest.raises(ValidationError, match="cannot be granted"):
        refusals[1].model_copy(update={"granted": True, "evidence": ("x",)})


# -- kill switch ----------------------------------------------------------------------------------


def test_kill_switch_drill_stops_all_order_flow() -> None:
    rig = Rig()
    rig.service.submit_targets(rig.targets(btc=10), {KB: Decimal(100), KE: Decimal(50)})
    assert len(rig.venue.fills) == 1

    report = run_kill_switch_drill(
        rig.service,
        rig.targets(btc=0, eth=5),
        {KB: Decimal(100), KE: Decimal(50)},
        operator="drill-operator",
    )
    assert report.passed, report
    assert report.orders_attempted == 2 and report.fills_after_trip == 0
    assert len(rig.venue.fills) == 1 and rig.venue.position("deploy-1", KB) == 10
    assert rig.kill_switch.tripped
    assert rig.bus_ids(type(report.trip)) == [report.trip.record_id]
    assert AlertKind.KILL_SWITCH_TRIPPED in {a.kind for a in rig.monitor.alerts}


def test_the_venue_itself_refuses_fills_once_the_kill_switch_trips() -> None:
    """Holding the venue directly must not bypass a tripped kill switch (review finding)."""
    rig = Rig()
    rig.service.submit_targets(rig.targets(btc=10), {KB: Decimal(100)})
    order = rig.venue.orders[-1]
    rig.service.trip_kill_switch(reason="bypass probe", tripped_by="test")
    with pytest.raises(KillSwitchEngaged, match="tripped"):
        rig.service.venue.execute(order, Decimal(100), T0)
    assert len(rig.venue.fills) == 1


def test_an_unbound_venue_never_fills_and_cannot_be_rebound() -> None:
    rig = Rig()
    rig.service.submit_targets(rig.targets(btc=10), {KB: Decimal(100)})
    loose = SimulatedVenue(venue_id="sim-2", cost_model=COSTS)
    with pytest.raises(KillSwitchEngaged, match="not bound"):
        loose.execute(rig.venue.orders[-1], Decimal(100), T0)
    assert loose.fills == ()
    with pytest.raises(ValueError, match="different kill switch"):
        rig.venue.bind_kill_switch(KillSwitch())


def test_monitor_alert_hook_can_trip_the_kill_switch_on_drawdown() -> None:
    rig = Rig(max_drawdown=Decimal(500))

    def hook(alert: Alert) -> None:
        if alert.kind is AlertKind.DRAWDOWN_BREACH:
            rig.service.trip_kill_switch(reason=alert.message, tripped_by="monitor")

    rig.monitor.add_alert_hook(hook)
    rig.service.submit_targets(rig.targets(btc=20), {KB: Decimal(100)})
    assert not rig.kill_switch.tripped
    rig.service.submit_targets(rig.targets(btc=20), {KB: Decimal(70)})  # no order, just a mark
    assert rig.kill_switch.tripped
    snapshot = rig.monitor.snapshot()
    assert snapshot.drawdown >= 500 and snapshot.positions == {KB: Decimal(20)}
    report = rig.service.submit_targets(rig.targets(btc=0), {KB: Decimal(70)})
    assert [r.source for r in report.rejections] == [RejectionSource.KILL_SWITCH]


# -- second-line risk -----------------------------------------------------------------------------


def test_second_line_rejects_exposure_leverage_and_loss_and_records_each() -> None:
    exposure = Rig()
    report = exposure.service.submit_targets(exposure.targets(btc=60), {KB: Decimal(100)})
    assert [r.limit_name for r in report.rejections] == ["max_gross_exposure"]

    leverage = Rig(_limits(max_gross_exposure=Decimal(1_000_000)))
    report = leverage.service.submit_targets(leverage.targets(btc=300), {KB: Decimal(100)})
    assert [r.limit_name for r in report.rejections] == ["max_leverage"]

    loss = Rig()
    loss.service.submit_targets(loss.targets(btc=20), {KB: Decimal(100)})
    report = loss.service.submit_targets(
        loss.targets(btc=20, eth=1), {KB: Decimal(40), KE: Decimal(50)}
    )
    assert [r.limit_name for r in report.rejections] == ["max_loss"]
    # de-risking still passes after the breach
    report = loss.service.submit_targets(loss.targets(btc=0), {KB: Decimal(40)})
    assert not report.rejections and len(report.fills) == 1

    for rig in (exposure, leverage, loss):
        assert rig.risk.rejections == rig.service.audit.rejections
        assert rig.bus_ids(type(rig.risk.rejections[0])) == [
            r.record_id for r in rig.risk.rejections
        ]
        assert all(r.source is RejectionSource.SECOND_LINE_RISK for r in rig.risk.rejections)
        assert AlertKind.RISK_REJECTION in {a.kind for a in rig.monitor.alerts}


def test_limits_come_only_from_injected_parameters() -> None:
    policy = RiskPolicy.model_validate(
        {
            "name": "second_line",
            "version": "1.0.0",
            "rules": ("second_line",),
            "params": {"max_gross_exposure": 5000, "max_leverage": 2},
        }
    )
    with pytest.raises(ValueError, match="max_loss"):
        RiskLimits.from_risk_policy(policy, capital=Decimal(10_000))
    full = policy.model_copy(update={"params": {**policy.params, "max_loss": "1000"}})
    limits = RiskLimits.from_risk_policy(full, capital=Decimal(10_000))
    assert limits.max_loss == 1000 and limits.policy == Ref(
        kind=Kind.RISK, name="second_line", version="1.0.0"
    )
    with pytest.raises(ValidationError):
        _limits(max_leverage=Decimal("Infinity"))
    with pytest.raises(ValidationError, match="capital"):
        RiskLimits()  # type: ignore[call-arg]  # no default numbers


# -- admission, audit and determinism ------------------------------------------------------------


def test_only_admitted_runnable_deployments_may_send_targets() -> None:
    rig = Rig()
    other = rig.targets(btc=1).model_copy(update={"deployment_id": "unknown"})
    with pytest.raises(DeploymentNotAdmitted):
        rig.service.submit_targets(other, {KB: Decimal(100)})
    deployment = factories.deployment_record(
        deployment_id="deploy-2",
        equivalence=factories.equivalence_check(artifact_id=rig.artifact.artifact_id),
    )
    with pytest.raises(DeploymentNotAdmitted, match="may not run"):
        rig.service.admit(
            deployment, rig.artifact, _lifecycle(rig.artifact.strategy_spec, S.VALIDATION)
        )
    with pytest.raises(DeploymentNotAdmitted, match="artifact"):
        rig.service.admit(
            factories.deployment_record(deployment_id="deploy-3"),
            rig.artifact,
            _lifecycle(rig.artifact.strategy_spec),
        )
    assert not rig.venue.orders


def _scenario(rig: Rig) -> None:
    rig.service.submit_targets(rig.targets(btc=10, eth=4), {KB: Decimal(100), KE: Decimal(50)})
    rig.service.submit_targets(rig.targets(btc=70, eth=4), {KB: Decimal(101), KE: Decimal(49)})
    rig.service.submit_targets(rig.targets(btc=-5, eth=0), {KB: Decimal(99), KE: Decimal(52)})
    rig.service.trip_kill_switch(reason="end of scenario", tripped_by="test")
    rig.service.submit_targets(rig.targets(btc=0), {KB: Decimal(98)})


def test_every_order_has_exactly_one_terminal_record_and_every_record_is_published() -> None:
    rig = Rig()
    _scenario(rig)
    audit = rig.service.audit
    assert audit.orders and audit.fills and audit.rejections
    assert audit.incomplete_orders() == ()
    order_ids = {o.record_id for o in audit.orders}
    assert {f.order_id for f in rig.venue.fills} <= order_ids
    assert [o.record_id for o in rig.venue.orders] == [f.order_id for f in rig.venue.fills]
    published = sum(len(rig.bus_ids(t)) for t in TOPICS)
    assert published == len(audit.entries)
    for record_type in TOPICS:
        expected = [r.record_id for r in audit.entries if isinstance(r, record_type)]
        assert rig.bus_ids(record_type) == expected


def test_replay_is_deterministic_and_fills_follow_the_cost_model() -> None:
    first, second = Rig(), Rig()
    _scenario(first)
    _scenario(second)
    assert first.service.audit.head() == second.service.audit.head()
    assert first.venue.fills == second.venue.fills
    buy = first.venue.fills[0]
    assert buy.side is Side.BUY and buy.price == Decimal(100) * Decimal("1.0005")
    assert buy.fee == buy.quantity * buy.price * Decimal("0.001")
    sell = next(f for f in first.venue.fills if f.side is Side.SELL)
    assert sell.price == sell.reference_price * Decimal("0.9995")

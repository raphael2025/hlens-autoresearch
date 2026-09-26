"""The independent execution service (roadmap Phase 13; 09-security.md §5; ADR-0046).

Flow for one batch of target positions from an admitted deployment::

    targets -> per-instrument delta vs. venue position -> OrderRecord (always recorded)
            -> Kill Switch  -> RejectionRecord   (stop)
            -> SecondLineRisk -> RejectionRecord (stop)
            -> SimulatedVenue -> FillRecord -> risk book, monitor

Every record goes to the append-only ``AuditTrail`` and is published on the event bus. With a
durable ``audit`` (``AuditTrail(path)``) the records survive the process. Reopening a service on a
**non-empty** durable audit is fail closed: venue positions, the risk book, the monitor and the
admitted deployments are *not* rebuilt from it, so the new instance trips its kill switch at once
(a recorded ``KillSwitchTrip`` by ``RESTORE_TRIPPED_BY``) and never sends an order. Resuming order
flow needs a fresh audit path chosen by a human; ``replay_audit`` rebuilds what the old trail
proves for inspection. With ``record_marks=True`` every batch's prices are also recorded (a
``MarkRecord``, before they are applied) so that ``apps.execution.risk_replay.replay_risk`` can
re-derive every rejection, acceptance and alert; the default (``False``) writes exactly the records
it wrote before, so existing audit heads are unchanged. The service
refuses ``ExecutionMode.LIVE`` and any venue that is not exactly ``SimulatedVenue``; it performs no
network I/O and holds no credentials. It never imports ``research/`` (tests/test_architecture_
boundaries.py): the research plane cannot reach it, only a ``TargetPositionSource`` can.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from apps.execution.audit import AuditRecord, AuditTrail
from apps.execution.errors import DeploymentNotAdmitted, ExecutionRefused, LiveExecutionRefused
from apps.execution.kill_switch import KillSwitch
from apps.execution.ladder import ExecutionLadder
from apps.execution.monitor import Monitor
from apps.execution.records import (
    LIVE_STAGES,
    Alert,
    FillRecord,
    KillSwitchTrip,
    LadderGateRecord,
    MarkPrice,
    MarkRecord,
    OrderRecord,
    RejectionRecord,
    RejectionSource,
    Side,
    TargetPositions,
    instrument_key,
)
from apps.execution.risk import SecondLineRisk
from apps.execution.venue import SimulatedVenue
from core.contracts.event_bus import BusMessage, EventBusAdapter
from core.domain.artifact import DeploymentRecord, StrategyArtifact
from core.domain.execution import ExecutionMode
from core.domain.specs import Instrument
from core.lifecycle.strategy import LifecycleHistory, LifecycleState

__all__ = [
    "RESTORE_TRIPPED_BY",
    "RUNNABLE_LIFECYCLE_STATES",
    "TOPICS",
    "ExecutionReport",
    "ExecutionService",
    "TargetPositionSource",
]

#: Lifecycle states whose deployments may receive simulated orders (ADR-0005 §4, ADR-0006 §2).
RUNNABLE_LIFECYCLE_STATES: frozenset[LifecycleState] = frozenset(
    {LifecycleState.PAPER, LifecycleState.ACTIVE}
)

#: ``tripped_by`` of the trip recorded when a service is reopened on a non-empty durable audit.
RESTORE_TRIPPED_BY = "execution_service:restore"

TOPICS: Mapping[type, str] = {
    OrderRecord: "execution.order",
    FillRecord: "execution.fill",
    RejectionRecord: "execution.rejection",
    KillSwitchTrip: "execution.kill_switch",
    Alert: "execution.alert",
    LadderGateRecord: "execution.ladder",
    MarkRecord: "execution.mark",
}


class TargetPositionSource(Protocol):
    """The narrow seam to Phase 5: the only way strategy output enters the execution plane.

    A production strategy runtime (Phase 5, ADR-0038) implements this for a promoted artifact's
    deployment; it returns target positions, never orders. ``apps.execution.strategy_source.
    StrategyProviderTargetSource`` is the wiring adapter: it wraps a
    ``core.contracts.strategy.StrategyProvider`` (+ optional ``RiskProvider``) and a signal source
    to answer this Protocol.
    """

    def target_positions(self, deployment_id: str, as_of: datetime) -> TargetPositions: ...


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    orders: tuple[OrderRecord, ...]
    fills: tuple[FillRecord, ...]
    rejections: tuple[RejectionRecord, ...]


@dataclass(frozen=True, slots=True)
class _Admitted:
    deployment: DeploymentRecord
    artifact: StrategyArtifact
    ladder: ExecutionLadder


class ExecutionService:
    def __init__(
        self,
        *,
        mode: ExecutionMode,
        venue: SimulatedVenue,
        kill_switch: KillSwitch,
        risk: SecondLineRisk,
        monitor: Monitor,
        bus: EventBusAdapter,
        clock: Callable[[], datetime],
        audit: AuditTrail | None = None,
        record_marks: bool = False,
    ) -> None:
        if mode is not ExecutionMode.SIMULATED:
            raise LiveExecutionRefused(
                f"execution mode {mode} refused: this build is SIMULATED only (ADR-0046, H10)"
            )
        if type(venue) is not SimulatedVenue:
            raise LiveExecutionRefused("only the in-process SimulatedVenue is accepted (ADR-0046)")
        self._mode = mode
        venue.bind_kill_switch(kill_switch)
        self._venue = venue
        self._kill_switch = kill_switch
        self._risk = risk
        self._monitor = monitor
        self._bus = bus
        self._clock = clock
        self._audit = AuditTrail() if audit is None else audit
        self._deployments: dict[str, _Admitted] = {}
        prior = self._audit.entries
        self._sequence = 1 + max((o.sequence for o in self._audit.orders), default=-1)
        self._record_marks = record_marks
        self._mark_sequence = 1 + max((m.sequence for m in self._audit.marks), default=-1)
        kill_switch.subscribe(self._on_trip)
        monitor.add_alert_hook(self._record)
        if prior:
            # Fail closed (module docs): state is not rebuilt from the audit, so no order may flow.
            kill_switch.trip(
                reason=(
                    f"reopened on a durable audit with {len(prior)} prior records "
                    f"({len(self._audit.trips)} prior kill switch trips); execution state is not "
                    "rebuilt — resuming needs a fresh audit"
                ),
                tripped_by=RESTORE_TRIPPED_BY,
                at=clock(),
            )

    @property
    def mode(self) -> ExecutionMode:
        return self._mode

    @property
    def audit(self) -> AuditTrail:
        return self._audit

    @property
    def kill_switch(self) -> KillSwitch:
        return self._kill_switch

    @property
    def venue(self) -> SimulatedVenue:
        return self._venue

    def ladder(self, deployment_id: str) -> ExecutionLadder:
        return self._admitted(deployment_id).ladder

    def admit(
        self,
        deployment: DeploymentRecord,
        artifact: StrategyArtifact,
        lifecycle: LifecycleHistory,
    ) -> ExecutionLadder:
        """Admit a deployment of a promoted artifact (ADR-0005 §4); returns its ladder.

        ``DeploymentRecord`` itself guarantees a passed Equivalence Gate bound to the same
        ``artifact_id`` and production code. Here we also require the artifact to be the deployed
        one and its lifecycle to be in a runnable state. Registry presence is the Control Plane's
        job and is not verified by this framework.
        """
        if deployment.deployment_id in self._deployments:
            raise DeploymentNotAdmitted(f"deployment {deployment.deployment_id} already admitted")
        if artifact.artifact_id != deployment.artifact_id:
            raise DeploymentNotAdmitted("the artifact is not the one this deployment records")
        subjects = {artifact.ref.target_identity(), artifact.strategy_spec.target_identity()}
        if lifecycle.subject.target_identity() not in subjects:
            raise DeploymentNotAdmitted("the lifecycle history belongs to another subject")
        if lifecycle.current_state not in RUNNABLE_LIFECYCLE_STATES:
            raise DeploymentNotAdmitted(
                f"lifecycle state {lifecycle.current_state} may not run; "
                f"needs one of {sorted(RUNNABLE_LIFECYCLE_STATES)}"
            )
        ladder = ExecutionLadder(
            deployment_id=deployment.deployment_id,
            subject=lifecycle.subject,
            clock=self._clock,
            on_record=self._record,
        )
        self._deployments[deployment.deployment_id] = _Admitted(deployment, artifact, ladder)
        return ladder

    def run_once(
        self,
        source: TargetPositionSource,
        deployment_id: str,
        prices: Mapping[str, Decimal],
    ) -> ExecutionReport:
        targets = source.target_positions(deployment_id, self._clock())
        if targets.deployment_id != deployment_id:
            raise DeploymentNotAdmitted("the source returned targets for another deployment")
        return self.submit_targets(targets, prices)

    def submit_targets(
        self, targets: TargetPositions, prices: Mapping[str, Decimal]
    ) -> ExecutionReport:
        admitted = self._admitted(targets.deployment_id)
        applicable = {instrument_key(i) for i in admitted.artifact.applicable_instruments}
        for target in targets.targets:
            key = instrument_key(target.instrument)
            if applicable and key not in applicable:
                raise ExecutionRefused(f"{key} is outside the artifact's applicable instruments")
            if key not in prices:
                raise ExecutionRefused(f"no price supplied for {key}")
        now = self._clock()
        self._risk.mark(prices)  # validates every price before anything is recorded
        if self._record_marks:
            self._record(
                MarkRecord(
                    sequence=self._mark_sequence,
                    deployment_id=targets.deployment_id,
                    prices=tuple(MarkPrice(key=k, price=p) for k, p in sorted(prices.items())),
                    marked_at=now,
                )
            )
            self._mark_sequence += 1
        self._monitor.mark(prices, now)

        orders: list[OrderRecord] = []
        fills: list[FillRecord] = []
        rejections: list[RejectionRecord] = []
        for target in sorted(targets.targets, key=lambda t: instrument_key(t.instrument)):
            key = instrument_key(target.instrument)
            delta = target.quantity - self._venue.position(targets.deployment_id, key)
            if delta == 0:
                continue
            order = self._new_order(admitted, target.instrument, delta, prices[key])
            orders.append(order)
            rejection = self._pre_trade(order)
            if rejection is not None:
                rejections.append(rejection)
                continue
            fill = self._venue.execute(order, prices[key], self._clock())
            self._risk.on_fill(fill)
            self._record(fill)
            fills.append(fill)
            self._monitor.on_fill(fill)
        return ExecutionReport(tuple(orders), tuple(fills), tuple(rejections))

    def trip_kill_switch(self, *, reason: str, tripped_by: str) -> KillSwitchTrip:
        return self._kill_switch.trip(reason=reason, tripped_by=tripped_by, at=self._clock())

    def _admitted(self, deployment_id: str) -> _Admitted:
        admitted = self._deployments.get(deployment_id)
        if admitted is None:
            raise DeploymentNotAdmitted(f"deployment {deployment_id} was not admitted")
        return admitted

    def _new_order(
        self, admitted: _Admitted, instrument: Instrument, delta: Decimal, price: Decimal
    ) -> OrderRecord:
        stage = admitted.ladder.stage
        if stage in LIVE_STAGES:  # unreachable: the ladder never grants a live rung
            raise LiveExecutionRefused(f"stage {stage} refused (ADR-0046)")
        order = OrderRecord(
            sequence=self._sequence,
            deployment_id=admitted.deployment.deployment_id,
            artifact_id=admitted.deployment.artifact_id,
            stage=stage,
            mode=self._mode,
            instrument=instrument,
            side=Side.BUY if delta > 0 else Side.SELL,
            quantity=abs(delta),
            reference_price=price,
            submitted_at=self._clock(),
        )
        self._sequence += 1
        self._record(order)
        return order

    def _pre_trade(self, order: OrderRecord) -> RejectionRecord | None:
        if self._kill_switch.tripped:
            rejection: RejectionRecord | None = RejectionRecord(
                order_id=order.record_id,
                source=RejectionSource.KILL_SWITCH,
                limit_name="kill_switch",
                reason="kill switch is tripped; all order flow is stopped",
                rejected_at=self._clock(),
            )
        else:
            rejection = self._risk.check(order, self._clock())
        if rejection is not None:
            self._record(rejection)
            self._monitor.on_rejection(rejection)
        return rejection

    def _on_trip(self, trip: KillSwitchTrip) -> None:
        self._record(trip)
        self._monitor.on_kill_switch(trip)

    def _record(self, record: AuditRecord) -> None:
        self._audit.append(record)
        self._bus.publish(
            BusMessage.build(TOPICS[type(record)], record.record_id, record.model_dump(mode="json"))
        )

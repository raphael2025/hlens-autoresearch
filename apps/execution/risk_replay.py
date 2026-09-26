"""Risk / alert replay of an execution audit (roadmap Phase 13: 风险与事件可重放; ADR-0046).

``replay_risk`` re-runs a fresh ``SecondLineRisk`` and ``Monitor`` — built from the limits and
monitor parameters the caller states the service ran with — over the audited records **in order**
and requires the audit to hold exactly what they produce:

* ``MarkRecord`` -> ``risk.mark`` and ``monitor.mark`` (drawdown alerts may follow);
* ``OrderRecord`` -> the kill-switch / second-line decision is recomputed. A recorded rejection
  must equal the replayed one field for field (its ``rejected_at`` is taken from the audit: time is
  an input, not a decision); an order the audit filled must be accepted by the replay;
* ``FillRecord`` -> must answer an order the replay accepted; then ``risk.on_fill`` /
  ``monitor.on_fill`` (drawdown alerts may follow);
* ``KillSwitchTrip`` -> kill switch tripped from here on, ``monitor.on_kill_switch``. A trip by
  ``RESTORE_TRIPPED_BY`` starts a new service session: risk and monitor restart from scratch (the
  reopened service is built with fresh ones, and the same parameters are assumed);
* ``RejectionRecord`` / ``Alert`` -> must be one the replay is currently expecting (then a
  rejection feeds ``monitor.on_rejection``); ``LadderGateRecord`` has no risk effect.

A record the replay did not produce, a replayed record the audit does not hold, or a decision that
differs is ``RiskReplayDiverged`` (an ``AuditCorrupted``) naming the first diverging record —
refused, never repaired. The trail must hold ``MarkRecord``s (a service built with
``record_marks=True``) whenever it holds orders. An order without exactly one terminal record (a
crash between order and outcome) is re-checked to keep state identical but listed as unverified.

Evidence only: nothing is sent to a venue, no network I/O, no live code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from apps.execution.audit import AuditCorrupted, AuditRecord, AuditTrail
from apps.execution.monitor import Monitor
from apps.execution.records import (
    Alert,
    FillRecord,
    KillSwitchTrip,
    LadderGateRecord,
    MarkRecord,
    OrderRecord,
    RejectionRecord,
    RejectionSource,
)
from apps.execution.risk import RiskLimits, SecondLineRisk
from apps.execution.service import RESTORE_TRIPPED_BY
from apps.worker.journal import JournalPath

__all__ = ["RiskReplay", "RiskReplayDiverged", "replay_risk"]


class RiskReplayDiverged(AuditCorrupted):
    """The audited decisions / alerts are not what the stated risk and monitor produce."""

    def __init__(self, message: str, record: AuditRecord | None, index: int | None) -> None:
        super().__init__(message)
        #: The first diverging record (an audit record, or the replayed one the audit lacks).
        self.record = record
        #: Its position in the audit (``None`` when the audit lacks it).
        self.index = index


@dataclass(frozen=True, slots=True)
class RiskReplay:
    """What the replay reproduced (see module docs)."""

    trail: AuditTrail
    #: Recorded ``RejectionRecord`` ids, all reproduced exactly.
    verified_rejections: tuple[str, ...]
    #: Order ids the audit filled and the replay accepted.
    verified_acceptances: tuple[str, ...]
    #: Recorded ``Alert`` ids, all reproduced exactly.
    verified_alerts: tuple[str, ...]
    #: Order ids without exactly one terminal record; their decision could not be confirmed.
    unverified_orders: tuple[str, ...]
    #: Service sessions seen (1 + restore trips).
    sessions: int


class _Engines:
    def __init__(self, limits: RiskLimits, capital: Decimal, max_drawdown: Decimal) -> None:
        self.risk = SecondLineRisk(limits)
        self.monitor = Monitor(capital=capital, max_drawdown=max_drawdown)
        self.produced: list[AuditRecord] = []
        self.monitor.add_alert_hook(self.produced.append)


def replay_risk(
    source: JournalPath | AuditTrail,
    limits: RiskLimits,
    *,
    max_drawdown: Decimal,
    monitor_capital: Decimal | None = None,
) -> RiskReplay:
    """Re-run second-line risk and the monitor over an audit and verify it (module docs).

    ``source`` is a durable audit path (reopened and chain-verified) or an ``AuditTrail``.
    ``monitor_capital`` defaults to ``limits.capital`` (as the service's callers wire it).
    """
    trail = source if isinstance(source, AuditTrail) else AuditTrail(source)
    capital = limits.capital if monitor_capital is None else monitor_capital
    entries = trail.entries
    if trail.orders and not trail.marks:
        raise RiskReplayDiverged(
            "the audit holds orders but no MarkRecord (service not built with record_marks=True);"
            " its risk decisions cannot be replayed",
            None,
            None,
        )

    terminals: dict[str, list[FillRecord | RejectionRecord]] = {}
    for record in entries:
        if isinstance(record, FillRecord | RejectionRecord):
            terminals.setdefault(record.order_id, []).append(record)

    engines = _Engines(limits, capital, max_drawdown)
    pending: list[AuditRecord] = []  # replayed records the audit must hold next
    awaiting_fill: OrderRecord | None = None  # an accepted order's fill must come next
    tripped = False
    sessions = 1
    rejections: list[str] = []
    acceptances: list[str] = []
    alerts: list[str] = []
    unverified: list[str] = []

    def drain() -> None:
        pending.extend(engines.produced)
        engines.produced.clear()

    def diverge(message: str, record: AuditRecord | None, index: int | None) -> RiskReplayDiverged:
        where = f"audit record {index}" if index is not None else "end of audit"
        rid = record.record_id if record is not None else "-"
        return RiskReplayDiverged(f"{where} ({rid}): {message}", record, index)

    def expect_settled(record: AuditRecord, index: int) -> None:
        if pending:
            missing = pending[0]
            raise diverge(
                f"replay produced {type(missing).__name__} {missing.record_id} that the audit does"
                f" not hold before this {type(record).__name__}",
                record,
                index,
            )

    def take(record: AuditRecord, index: int) -> None:
        for i, expected in enumerate(pending):
            if expected.record_id == record.record_id:
                del pending[i]
                return
        raise diverge(
            f"recorded {type(record).__name__} is not reproduced by the replay", record, index
        )

    def decide(order: OrderRecord, at: datetime) -> RejectionRecord | None:
        if tripped:
            return RejectionRecord(
                order_id=order.record_id,
                source=RejectionSource.KILL_SWITCH,
                limit_name="kill_switch",
                reason="kill switch is tripped; all order flow is stopped",
                rejected_at=at,
            )
        return engines.risk.check(order, at)

    for index, record in enumerate(entries):
        if awaiting_fill is not None and not (
            isinstance(record, FillRecord) and record.order_id == awaiting_fill.record_id
        ):
            raise diverge(
                f"the replay accepted order {awaiting_fill.record_id}, so its fill must come next",
                record,
                index,
            )
        if isinstance(record, MarkRecord):
            expect_settled(record, index)
            prices: Mapping[str, Decimal] = record.price_map()
            engines.risk.mark(prices)
            engines.monitor.mark(prices, record.marked_at)
            drain()
        elif isinstance(record, OrderRecord):
            expect_settled(record, index)
            outcome = terminals.get(record.record_id, [])
            if len(outcome) > 1:
                raise diverge("order has more than one terminal record", record, index)
            if not outcome:
                decide(record, record.submitted_at)  # same state change, decision unconfirmed
                unverified.append(record.record_id)
                continue
            terminal = outcome[0]
            if isinstance(terminal, RejectionRecord):
                replayed = decide(record, terminal.rejected_at)
                if replayed is None:
                    raise diverge(
                        f"the audit rejected this order ({terminal.limit_name}) but the replay"
                        " accepts it",
                        record,
                        index,
                    )
                pending.append(replayed)
            else:
                replayed = decide(record, terminal.filled_at)
                if replayed is not None:
                    raise diverge(
                        f"the audit filled this order but the replay rejects it"
                        f" ({replayed.limit_name}: {replayed.reason})",
                        record,
                        index,
                    )
                awaiting_fill = record
        elif isinstance(record, FillRecord):
            expect_settled(record, index)
            if awaiting_fill is None:
                raise diverge("fill does not answer an order the replay accepted", record, index)
            awaiting_fill = None
            acceptances.append(record.order_id)
            engines.risk.on_fill(record)
            engines.monitor.on_fill(record)
            drain()
        elif isinstance(record, RejectionRecord):
            take(record, index)
            rejections.append(record.record_id)
            engines.monitor.on_rejection(record)
            drain()
        elif isinstance(record, Alert):
            take(record, index)
            alerts.append(record.record_id)
        elif isinstance(record, KillSwitchTrip):
            if record.tripped_by == RESTORE_TRIPPED_BY:
                expect_settled(record, index)
                engines = _Engines(limits, capital, max_drawdown)
                sessions += 1
            tripped = True
            engines.monitor.on_kill_switch(record)
            drain()
        else:
            assert isinstance(record, LadderGateRecord)  # no risk effect
    if awaiting_fill is not None:
        raise diverge(
            "the replay accepted this order but its fill is not after it", awaiting_fill, None
        )
    if pending:
        raise diverge(
            f"replay produced {type(pending[0]).__name__} that the audit does not hold",
            pending[0],
            None,
        )
    return RiskReplay(
        trail=trail,
        verified_rejections=tuple(rejections),
        verified_acceptances=tuple(acceptances),
        verified_alerts=tuple(alerts),
        unverified_orders=tuple(unverified),
        sessions=sessions,
    )

"""Kill Switch drill (roadmap Phase 13 acceptance: "Kill Switch 演练通过"; ADR-0046).

Run it on a dedicated service instance: the switch stays tripped afterwards by design.
The drill trips the switch, pushes a batch of targets through the service, and checks that
no order reached the venue, every order produced during the drill was rejected by the kill switch,
and the trip itself is in the audit trail.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from apps.execution.records import KillSwitchTrip, RejectionSource, TargetPositions
from apps.execution.service import ExecutionService

__all__ = ["DrillReport", "run_kill_switch_drill"]


@dataclass(frozen=True, slots=True)
class DrillReport:
    trip: KillSwitchTrip
    orders_attempted: int
    orders_rejected_by_kill_switch: int
    fills_after_trip: int
    trip_audited: bool
    audit_complete: bool

    @property
    def passed(self) -> bool:
        return (
            self.orders_attempted > 0
            and self.orders_rejected_by_kill_switch == self.orders_attempted
            and self.fills_after_trip == 0
            and self.trip_audited
            and self.audit_complete
        )


def run_kill_switch_drill(
    service: ExecutionService,
    targets: TargetPositions,
    prices: Mapping[str, Decimal],
    *,
    operator: str,
) -> DrillReport:
    fills_before = len(service.venue.fills)
    trip = service.trip_kill_switch(reason="kill switch drill", tripped_by=operator)
    report = service.submit_targets(targets, prices)
    blocked = sum(1 for r in report.rejections if r.source is RejectionSource.KILL_SWITCH)
    return DrillReport(
        trip=trip,
        orders_attempted=len(report.orders),
        orders_rejected_by_kill_switch=blocked,
        fills_after_trip=len(service.venue.fills) - fills_before,
        trip_audited=trip in service.audit.trips,
        audit_complete=not service.audit.incomplete_orders(),
    )

"""Append-only audit trail of every execution record (roadmap Phase 13: every order auditable)."""

from __future__ import annotations

from apps.execution.records import (
    Alert,
    FillRecord,
    KillSwitchTrip,
    LadderGateRecord,
    OrderRecord,
    RejectionRecord,
)
from core.domain.base import content_hash

__all__ = ["AuditRecord", "AuditTrail"]

AuditRecord = OrderRecord | FillRecord | RejectionRecord | KillSwitchTrip | Alert | LadderGateRecord


class AuditTrail:
    """Records in arrival order, each at most once (by ``record_id``); never edited or removed."""

    def __init__(self) -> None:
        self._entries: list[AuditRecord] = []
        self._ids: set[str] = set()

    def append(self, record: AuditRecord) -> None:
        if record.record_id in self._ids:
            raise ValueError(f"audit record {record.record_id} is already recorded")
        self._entries.append(record)
        self._ids.add(record.record_id)

    @property
    def entries(self) -> tuple[AuditRecord, ...]:
        return tuple(self._entries)

    def head(self) -> str:
        """Hash of the ordered (type, record_id) sequence: identical replays give equal heads."""
        return content_hash([[type(r).__name__, r.record_id] for r in self._entries])

    def _of[R](self, kind: type[R]) -> tuple[R, ...]:
        return tuple(r for r in self._entries if isinstance(r, kind))

    @property
    def orders(self) -> tuple[OrderRecord, ...]:
        return self._of(OrderRecord)

    @property
    def fills(self) -> tuple[FillRecord, ...]:
        return self._of(FillRecord)

    @property
    def rejections(self) -> tuple[RejectionRecord, ...]:
        return self._of(RejectionRecord)

    @property
    def trips(self) -> tuple[KillSwitchTrip, ...]:
        return self._of(KillSwitchTrip)

    @property
    def alerts(self) -> tuple[Alert, ...]:
        return self._of(Alert)

    @property
    def ladder_gates(self) -> tuple[LadderGateRecord, ...]:
        return self._of(LadderGateRecord)

    def incomplete_orders(self) -> tuple[str, ...]:
        """Order ids without exactly one terminal record (a fill or a rejection)."""
        terminal: dict[str, int] = {}
        for record in self._entries:
            if isinstance(record, FillRecord | RejectionRecord):
                terminal[record.order_id] = terminal.get(record.order_id, 0) + 1
        known = {o.record_id for o in self.orders}
        orphans = tuple(sorted(set(terminal) - known))
        return tuple(o.record_id for o in self.orders if terminal.get(o.record_id) != 1) + orphans

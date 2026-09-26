"""Append-only audit trail of every execution record (roadmap Phase 13: every order auditable).

In memory by default. Given a ``path`` the trail is **durable**: every record is appended (fsync'd)
to a hash-chained JSON-lines journal (``apps.worker.journal.AppendOnlyJournal``, the worker's
on-disk contract) before it is kept in memory, and reopening the path replays and verifies the
whole chain. Each line is ``{"record_id", "record"}`` under the record's class name; on replay the
record is re-validated and its content hash must equal the stored ``record_id``. A bad chain, an
unknown record type, a mismatched id or a duplicate is ``AuditCorrupted`` — refused, never skipped
or repaired. Dropping whole lines from the end of the file leaves a valid shorter chain; only an
externally kept ``head_hash`` detects that (same honest boundary as the worker journal).

``replay_audit`` rebuilds, from a durable trail alone, what the service did: per-deployment
positions and fees from the fills, orders without exactly one terminal record, kill switch trips
and ladder decisions. It is read-only evidence; it never re-executes anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from apps.execution.records import (
    Alert,
    FillRecord,
    KillSwitchTrip,
    LadderGateRecord,
    OrderRecord,
    RejectionRecord,
    instrument_key,
)
from apps.worker.journal import AppendOnlyJournal, JournalCorrupted, JournalPath
from core.domain.base import content_hash

__all__ = ["AuditCorrupted", "AuditRecord", "AuditReplay", "AuditTrail", "replay_audit"]

AuditRecord = OrderRecord | FillRecord | RejectionRecord | KillSwitchTrip | Alert | LadderGateRecord

_KINDS: Final[Mapping[str, type[AuditRecord]]] = {
    kind.__name__: kind
    for kind in (OrderRecord, FillRecord, RejectionRecord, KillSwitchTrip, Alert, LadderGateRecord)
}


class AuditCorrupted(RuntimeError):
    """A durable audit file cannot be trusted (broken chain, unknown type, id mismatch, dup)."""


class AuditTrail:
    """Records in arrival order, each at most once (by ``record_id``); never edited or removed."""

    def __init__(self, path: JournalPath | None = None) -> None:
        self._entries: list[AuditRecord] = []
        self._ids: set[str] = set()
        self._journal: AppendOnlyJournal | None = None
        if path is not None:
            try:
                self._journal = AppendOnlyJournal(path)
            except JournalCorrupted as exc:
                raise AuditCorrupted(str(exc)) from exc
            for entry in self._journal.entries:
                record = _decode(entry.seq, entry.type, entry.payload)
                if record.record_id in self._ids:
                    raise AuditCorrupted(f"audit line {entry.seq} repeats {record.record_id}")
                self._entries.append(record)
                self._ids.add(record.record_id)

    @property
    def durable(self) -> bool:
        return self._journal is not None

    @property
    def head_hash(self) -> str | None:
        """The durable chain's tip (keep it outside the file to detect tail deletion)."""
        return self._journal.head_hash if self._journal is not None else None

    def append(self, record: AuditRecord) -> None:
        if record.record_id in self._ids:
            raise ValueError(f"audit record {record.record_id} is already recorded")
        if self._journal is not None:
            try:
                self._journal.append(
                    type(record).__name__,
                    {"record_id": record.record_id, "record": record.model_dump(mode="json")},
                )
            except JournalCorrupted as exc:
                raise AuditCorrupted(str(exc)) from exc
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


def _decode(seq: int, kind_name: str, payload: Mapping[str, Any]) -> AuditRecord:
    kind = _KINDS.get(kind_name)
    if kind is None:
        raise AuditCorrupted(f"audit line {seq} has unknown record type {kind_name!r}")
    if set(payload) != {"record_id", "record"}:
        raise AuditCorrupted(f"audit line {seq} does not hold exactly record_id and record")
    try:
        record = kind.model_validate(payload["record"])
    except ValueError as exc:
        raise AuditCorrupted(f"audit line {seq} is not a valid {kind_name}") from exc
    if record.record_id != payload["record_id"]:
        raise AuditCorrupted(f"audit line {seq}: content does not hash to its record_id")
    return record


@dataclass(frozen=True, slots=True)
class AuditReplay:
    """What a durable trail proves the service did (see module docs)."""

    trail: AuditTrail
    #: ``deployment_id -> instrument key -> signed quantity`` from the fills (zeros dropped).
    positions: Mapping[str, Mapping[str, Decimal]]
    #: ``deployment_id -> total fees`` from the fills.
    fees: Mapping[str, Decimal]
    incomplete_orders: tuple[str, ...]
    kill_switch_tripped: bool
    head_hash: str | None


def replay_audit(path: JournalPath) -> AuditReplay:
    """Reopen a durable audit read-only-in-effect and rebuild positions / fees (evidence only).

    Every fill must answer a recorded order of the same deployment, instrument, side and quantity;
    otherwise the trail is ``AuditCorrupted``.
    """
    trail = AuditTrail(path)
    orders = {order.record_id: order for order in trail.orders}
    positions: dict[str, dict[str, Decimal]] = {}
    fees: dict[str, Decimal] = {}
    for fill in trail.fills:
        order = orders.get(fill.order_id)
        if order is None or (
            order.deployment_id,
            instrument_key(order.instrument),
            order.side,
            order.quantity,
        ) != (fill.deployment_id, instrument_key(fill.instrument), fill.side, fill.quantity):
            raise AuditCorrupted(f"fill {fill.record_id} does not answer a recorded order")
        book = positions.setdefault(fill.deployment_id, {})
        key = instrument_key(fill.instrument)
        book[key] = book.get(key, Decimal(0)) + fill.signed_quantity
        fees[fill.deployment_id] = fees.get(fill.deployment_id, Decimal(0)) + fill.fee
    return AuditReplay(
        trail=trail,
        positions={
            d: {k: q for k, q in sorted(book.items()) if q != 0}
            for d, book in sorted(positions.items())
        },
        fees=dict(sorted(fees.items())),
        incomplete_orders=trail.incomplete_orders(),
        kill_switch_tripped=bool(trail.trips),
        head_hash=trail.head_hash,
    )

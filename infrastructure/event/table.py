"""Event table materialization (Phase 3; ADR-0036 §4).

``event_table(result)`` flattens an ``EventResult`` into rows of the logical Event table — one row
per event, with its definition (``event`` + ``spec_hash``), observable ``event_time``, attributes
(canonical JSON), lineage (``input_ids`` / ``upstream_event_ids``) and the producing run
(``provider`` / ``result_hash``). Rows are plain, immutable values in the result's canonical order.

This is the in-memory materialization only: registering a physical ``event.*`` Iceberg table (like
the Phase 1 production tables) is a separate, later decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from core.contracts.event import EventResult
from core.domain.base import canonical_json

__all__ = ["EVENT_TABLE_COLUMNS", "EventTableRow", "event_table"]


@dataclass(frozen=True, slots=True)
class EventTableRow:
    event_id: str
    event: str
    spec_hash: str
    event_time: datetime
    attributes_json: str
    input_ids: tuple[str, ...]
    upstream_event_ids: tuple[str, ...]
    provider: str
    result_hash: str


#: Column order of the logical Event table.
EVENT_TABLE_COLUMNS: Final = tuple(EventTableRow.__dataclass_fields__)


def event_table(result: EventResult) -> tuple[EventTableRow, ...]:
    """One row per event of ``result`` (canonical ``(event_time, event_id)`` order)."""
    return tuple(
        EventTableRow(
            event_id=item.event_id,
            event=str(item.event),
            spec_hash=item.spec_hash,
            event_time=item.event_time,
            attributes_json=canonical_json(item.model_dump(mode="json")["attributes"]),
            input_ids=item.input_ids,
            upstream_event_ids=item.upstream_event_ids,
            provider=result.provider,
            result_hash=result.result_hash,
        )
        for item in result.events
    )

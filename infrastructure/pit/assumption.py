"""Archive event-time availability assumption (ADR-0032, D-HIST; decided by Raphael 2026-09-25).

Stored revisions stay conservative (``available_time = ingest_time`` + evidence gap, ADR-0023
§2). A ``PointInTimeSpec`` that **binds** ``hlens.availability.archive-event-time-assumption@1.0.0``
in its ``availability_bindings`` lets the PIT selector use, for exactly the revisions this rule
names, an effective ``available_time = min(stored, observable_time + 5 s)``:

- named: Canonical revisions whose Raw lineage is an archive element table and whose evidence gap
  is the archive trade / kline "publication bound not stated" gap of
  ``binance.spot.publication@1.0.0`` (inherited verbatim by the Canonical availability policy);
- basis: the official streams are real-time (trades) / pushed every 2000 ms (klines); the
  **stated assumption** is that an archive holds what the stream pushed; 5 s is the latency;
- never later than the stored value, never for REST tails, listings or any other gap; the
  knowledge axis is untouched; evidence gaps are still listed and bound.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

from core.contracts.revision import PointInTimeSpec, PolicyBinding, PolicyRole
from core.domain.base import canonical_json
from infrastructure.catalog.phase1_tables import BINANCE_SPOT_AGG_TRADES, BINANCE_SPOT_KLINES_1M
from infrastructure.contract_version import PHASE1_PUBLICATION_VERSION
from infrastructure.revision.availability import AVAILABILITY_POLICY_ID, AVAILABILITY_POLICY_VERSION

__all__ = [
    "ASSUMPTION_BINDING",
    "ASSUMPTION_ID",
    "ASSUMPTION_LATENCY",
    "ASSUMPTION_SPEC",
    "ASSUMPTION_VERSION",
    "AssumptionSpecError",
    "assumption_bound",
    "effective_available_times",
]

ASSUMPTION_ID: Final = "hlens.availability.archive-event-time-assumption"
ASSUMPTION_VERSION: Final = "1.0.0"
ASSUMPTION_LATENCY: Final = timedelta(seconds=5)
_ARCHIVE_TABLES: Final = (BINANCE_SPOT_AGG_TRADES.table, BINANCE_SPOT_KLINES_1M.table)
_GAP_TOKENS: Final = tuple(
    f"{AVAILABILITY_POLICY_ID}@{AVAILABILITY_POLICY_VERSION}:{token}"
    for token in ("agg_trade_publication_bound_not_stated", "kline_1m_publication_bound_not_stated")
)
ASSUMPTION_SPEC: Final[dict[str, Any]] = {
    "rule": ASSUMPTION_ID,
    "version": ASSUMPTION_VERSION,
    "adr": "ADR-0032 (D-HIST), decided by Raphael 2026-09-25",
    "binding": "only when bound in PointInTimeSpec.availability_bindings (exact version + hash)",
    "applies_to": {
        "lineage_raw_table": list(_ARCHIVE_TABLES),
        "evidence_gap_contains": list(_GAP_TOKENS),
    },
    "effective_available_time": "min(stored available_time, observable_time + latency)",
    "observable_time": "event_end_time for interval events (kline close), else event_time",
    "latency_microseconds": ASSUMPTION_LATENCY // timedelta(microseconds=1),
    "basis": "official streams: aggTrade 'Real-time', kline pushed every 2000ms",
    "assumption": "an archive holds exactly what the stream pushed at the time",
    "unchanged": "stored revisions, evidence gaps (still listed and bound), knowledge axis",
}
ASSUMPTION_BINDING: Final = PolicyBinding(
    schema_version=PHASE1_PUBLICATION_VERSION,
    role=PolicyRole.AVAILABILITY,
    policy_id=ASSUMPTION_ID,
    version=ASSUMPTION_VERSION,
    policy_hash=hashlib.sha256(canonical_json(ASSUMPTION_SPEC).encode("utf-8")).hexdigest(),
)


class AssumptionSpecError(ValueError):
    """The spec names the assumption policy with another version or hash (fail closed)."""


def assumption_bound(spec: PointInTimeSpec) -> bool:
    """Whether ``spec`` binds exactly this assumption; another version / hash is refused."""
    named = [b for b in spec.availability_bindings if b.policy_id == ASSUMPTION_ID]
    if not named:
        return False
    if named != [ASSUMPTION_BINDING]:
        raise AssumptionSpecError(
            f"{ASSUMPTION_ID} must be bound exactly once as {ASSUMPTION_VERSION} with its hash"
        )
    return True


def _named(row: Mapping[str, Any]) -> bool:
    gap = row["availability_evidence_gap"]
    return (
        row["lineage_raw_table"] in _ARCHIVE_TABLES
        and gap is not None
        and any(token in gap for token in _GAP_TOKENS)
    )


def effective_available_times(
    rows: Sequence[Mapping[str, Any]], *, bound: bool
) -> dict[str, tuple[datetime, datetime]]:
    """``revision_id -> (stored, effective)`` for the rows whose time the assumption moves."""
    if not bound:
        return {}
    moved: dict[str, tuple[datetime, datetime]] = {}
    for row in rows:
        if not _named(row):
            continue
        stored = row["available_time"]
        observable = row.get("interval_end") or row.get("event_time")
        if observable is None:
            continue
        effective = min(stored, observable + ASSUMPTION_LATENCY)
        if effective < stored:
            moved[row["revision_id"]] = (stored, effective)
    return moved

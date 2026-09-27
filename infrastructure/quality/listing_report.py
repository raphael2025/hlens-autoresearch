"""The listing-history quality report (Phase 1 F2 / F3; ADR-0023 §2, ADR-0024, ADR-0029 §2).

``ListingQualityReporter.report()`` writes one row of ``quality.data_quality_reports`` about the
listing history — ``canonical.instrument_listings`` proven against
``raw.binance_spot_exchange_info`` — under ``hlens.quality.listing-history@1.0.0``. It exists
because every listing revision carries an availability evidence gap
(``binance.spot.exchange-info-publication@1.0.0``: observed-from, ``available_time =
ingest_time``) and a Research Dataset manifest may bind a gap only through a quality report that
records it (``AvailabilityEvidenceGap.quality_report_id``). The partition
reports of E3 cover Canonical trades / bars only.

1. **inputs** — the current heads of the two tables, read twice until they agree, then everything
   is read through a ``PinnedCatalogView`` of exactly those snapshots;
2. **proof** — ``ListingDeriver.verify()`` on that view (every snapshot row rebuilt from its
   checkpoint, every listing batch re-derived); an unprovable history gets no report;
3. **events** (deterministic ids, no threshold) — ``report_inputs`` (the bound snapshots) and one
   event per derivation finding (``listing_status_unknown``, ``listing_symbol_missing``,
   ``listing_instrument_mismatch``, ``listing_observation_tie``,
   ``listing_suspended_before_first_trading``, ``listing_history_diverged``): the quality events
   ADR-0029 §2 asks for, persisted;
4. **evidence gaps** — every committed listing revision with an availability evidence gap;
5. **idempotency** — ``report_id`` derives from the rule set, the listing rules it relies on and
   the bound snapshots; a committed report is re-derived and reused (its first
   ``knowledge_time``); only a missing report reads the injected clock, never before a
   ``knowledge_time`` it describes. ``existing_only`` never writes (a dataset build's check).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.expressions import EqualTo

from core.contracts.catalog import BatchConflict, CommitConflict, CommitRequest, TableNotFound
from core.contracts.storage import StorageAdapter
from core.domain.base import canonical_json
from infrastructure.canonical import listing_rules as lr
from infrastructure.canonical.listings import FINDING_HISTORY_DIVERGED, ListingDeriver
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_EXCHANGE_INFO,
    CANONICAL_INSTRUMENT_LISTINGS,
    DATA_QUALITY_REPORTS,
)
from infrastructure.parser.binance_exchange_info import EXCHANGE_INFO_DECODER_BINDING
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.quality.reporter import (
    QualityReported,
    QualityReportError,
    QualityReportMissing,
)
from infrastructure.revision.exchange_info_availability import EXCHANGE_INFO_AVAILABILITY_BINDING
from infrastructure.revision.store import BatchCommit, RevisionCatalog

__all__ = [
    "LISTING_QUALITY_RULE_HASH",
    "LISTING_QUALITY_RULE_ID",
    "LISTING_QUALITY_RULE_SPEC",
    "LISTING_QUALITY_RULE_VERSION",
    "ListingQualityReporter",
    "listing_quality_report_id",
]

LISTING_QUALITY_RULE_ID: Final = "hlens.quality.listing-history"
LISTING_QUALITY_RULE_VERSION: Final = "1.0.0"
LISTING_QUALITY_RULE_SPEC: Final[dict[str, Any]] = {
    "rule": LISTING_QUALITY_RULE_ID,
    "version": LISTING_QUALITY_RULE_VERSION,
    "subject": "the listing history: canonical.instrument_listings proven against "
    "raw.binance_spot_exchange_info",
    "inputs": "current heads of both tables, pinned; the report is computed on exactly those "
    "snapshots",
    "proof": "ListingDeriver.verify (snapshot rows rebuilt from checkpoints, listing batches "
    "re-derived); an unprovable history gets no report",
    "events": {
        "report_inputs": "the bound snapshots (canonical JSON)",
        "<finding code>": "each binance.spot.listing-status@1.0.0 derivation finding and each "
        "listing_history_diverged revision, at its observation instant",
    },
    "thresholds": "none",
    "evidence_gaps": "every committed listing revision with an availability evidence gap",
    "report_id": "<rule>@<version>.canonical.instrument_listings.<sha256 of the rule hashes used "
    "(this set, listing-status, listing-observation, exchange-info availability and decoder) and "
    "the bound snapshots>",
    "knowledge_time": "first commit's clock reading, not before any listing or snapshot "
    "knowledge_time it describes; reused by every replay",
}
LISTING_QUALITY_RULE_HASH: Final = hashlib.sha256(
    canonical_json(LISTING_QUALITY_RULE_SPEC).encode("utf-8")
).hexdigest()

_LISTINGS: Final = CANONICAL_INSTRUMENT_LISTINGS.table
_RAW: Final = BINANCE_SPOT_EXCHANGE_INFO.table
_INPUT_TABLES: Final = (_LISTINGS, _RAW)
_ATTEMPTS: Final = 8
_ZERO: Final = timedelta(0)


def _digest(document: Any) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def listing_quality_report_id(bindings: Mapping[str, str]) -> str:
    """The report's identity: rule set, the listing rules it relies on, the bound snapshots."""
    rules_used = {
        "quality": LISTING_QUALITY_RULE_HASH,
        **{
            f"{binding.policy_id}@{binding.version}": binding.policy_hash
            for binding in (
                lr.LISTING_STATUS_BINDING,
                lr.LISTING_OBSERVATION_BINDING,
                EXCHANGE_INFO_AVAILABILITY_BINDING,
                EXCHANGE_INFO_DECODER_BINDING,
            )
        },
    }
    inputs = {table: bindings[table] for table in _INPUT_TABLES if table in bindings}
    return (
        f"{LISTING_QUALITY_RULE_ID}@{LISTING_QUALITY_RULE_VERSION}.{_LISTINGS}."
        f"{_digest({'rules': rules_used, 'bindings': inputs})}"
    )


def _event(
    event_type: str,
    *,
    table: str | None,
    revisions: Iterable[str],
    start: datetime | None,
    detail: str,
) -> dict[str, Any]:
    revision_ids = sorted(set(revisions))
    identity = {
        "rule": f"{LISTING_QUALITY_RULE_ID}@{LISTING_QUALITY_RULE_VERSION}",
        "type": event_type,
        "table": table,
        "revisions": revision_ids,
        "start": None if start is None else start.isoformat(),
        "detail": detail,
    }
    return {
        "event_id": f"{event_type}.{_digest(identity)[:32]}",
        "event_type": event_type,
        "table": table,
        "observation_key": None,
        "revision_ids": revision_ids,
        "event_start": start,
        "event_end": None,
        "detail": detail,
    }


class ListingQualityReporter:
    """Writes the deterministic, reproducible listing-history report; one writer per table."""

    def __init__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        *,
        market_data_base_url: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapter = adapter
        self._storage = storage
        self._origin = market_data_base_url
        self._clock = clock or (lambda: datetime.now(UTC))

    def report(self, *, existing_only: bool = False) -> QualityReported:
        """Write (or reuse) the report at the current heads; ``existing_only`` never writes."""
        last: Exception | None = None
        for _ in range(_ATTEMPTS):
            bindings = self._pinned_heads()
            if _LISTINGS not in bindings:
                raise QualityReportError(f"{_LISTINGS} has no snapshot: nothing to report on")
            report_id = listing_quality_report_id(bindings)
            body, floor = self._survey(bindings, report_id)
            committed = self._committed(report_id)
            if committed is not None:
                if committed["knowledge_time"] < floor:
                    raise CatalogIntegrityError(
                        f"quality report {report_id} is committed with a knowledge_time before a "
                        "revision it describes"
                    )
                expected = _row(body, committed["knowledge_time"])
                mismatched = sorted(k for k, v in expected.items() if committed[k] != v)
                if mismatched:
                    raise CatalogIntegrityError(
                        f"quality report {report_id} disagrees with its re-derivation: {mismatched}"
                    )
                return QualityReported(report_id, committed, None, True)
            if existing_only:
                raise QualityReportMissing(
                    f"no committed quality report {report_id} for these listing snapshots"
                )
            now = self._clock()
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != _ZERO:
                raise QualityReportError("the clock must return timezone-aware UTC")
            if now < floor:
                raise QualityReportError(
                    "the report clock precedes a listing or snapshot revision it describes"
                )
            row = _row(body, now)
            try:
                commit = self._commit(report_id, row)
            except (CommitConflict, BatchConflict) as exc:
                last = exc
                continue
            return QualityReported(report_id, row, commit, False)
        raise QualityReportError("the listing report lost races") from last

    # ------------------------------------------------------------------ inputs and proof

    def _pinned_heads(self) -> dict[str, str]:
        for _ in range(_ATTEMPTS):
            first = {table: self._head(table) for table in _INPUT_TABLES}
            second = {table: self._head(table) for table in _INPUT_TABLES}
            if first == second:
                return {table: head for table, head in sorted(first.items()) if head is not None}
        raise QualityReportError("the listing tables kept moving")

    def _survey(
        self, bindings: Mapping[str, str], report_id: str
    ) -> tuple[dict[str, Any], datetime]:
        view = PinnedCatalogView(self._adapter, bindings)
        deriver = ListingDeriver(view, self._storage, market_data_base_url=self._origin)
        try:
            verified = deriver.verify()
        finally:
            deriver.close()
        raw_times = view.scan_columns(_RAW, columns=("knowledge_time",)).column("knowledge_time")
        floor = max(
            [
                *(row["knowledge_time"] for row in verified.rows.values()),
                *raw_times.to_pylist(),
            ],
            default=datetime.min.replace(tzinfo=UTC),
        )
        events = [
            _event(
                "report_inputs",
                table=None,
                revisions=(),
                start=None,
                detail=canonical_json(dict(bindings)),
            )
        ]
        for finding in verified.findings:
            events.append(
                _event(
                    finding.code,
                    table=_LISTINGS if finding.code == FINDING_HISTORY_DIVERGED else _RAW,
                    revisions=finding.snapshot_revision_ids,
                    start=finding.observed_at,
                    detail=f"{finding.venue_symbol}: {finding.detail}",
                )
            )
        gaps = [
            {"table": _LISTINGS, "revision_id": revision, "gap": row["availability_evidence_gap"]}
            for revision, row in sorted(verified.rows.items())
            if row["availability_evidence_gap"] is not None
        ]
        body = {
            "report_id": report_id,
            "quality_rule_id": LISTING_QUALITY_RULE_ID,
            "quality_rule_version": LISTING_QUALITY_RULE_VERSION,
            "quality_rule_hash": LISTING_QUALITY_RULE_HASH,
            "subject_table": _LISTINGS,
            "subject_snapshot_id": bindings[_LISTINGS],
            "subject_symbol": None,
            "subject_start": None,
            "subject_end": None,
            "events": events,
            "evidence_gaps": gaps,
        }
        return body, floor

    # ------------------------------------------------------------------ catalog

    def _committed(self, report_id: str) -> Mapping[str, Any] | None:
        columns = tuple(field.name for field in DATA_QUALITY_REPORTS.arrow_schema)
        rows = self._adapter.scan_columns(
            DATA_QUALITY_REPORTS.table,
            columns=columns,
            row_filter=EqualTo("report_id", report_id),  # type: ignore[call-arg, arg-type]
        ).to_pylist()
        if len(rows) > 1:
            raise CatalogIntegrityError(f"quality report {report_id} is committed twice")
        return rows[0] if rows else None

    def _commit(self, report_id: str, row: Mapping[str, Any]) -> BatchCommit:
        definition = DATA_QUALITY_REPORTS
        table = pa.Table.from_pylist([dict(row)], schema=definition.arrow_schema)
        request = CommitRequest(
            table=definition.table,
            batch_id=report_id,
            batch_fingerprint=definition.fingerprint_rule.fingerprint(table),
            row_count=1,
            expected_parent_snapshot_id=self._head(definition.table),
        )
        result = self._adapter.commit_batch(request, table)
        if self._committed(report_id) != table.to_pylist()[0]:
            raise CatalogIntegrityError(f"quality report {report_id} reads back differently")
        return BatchCommit(
            table=definition.table,
            batch_id=report_id,
            snapshot_id=result.snapshot.snapshot_id,
            outcome=result.outcome,
            row_count=1,
        )

    def _head(self, table: str) -> str | None:
        info = self._adapter.load_table(table)
        if info is None:
            raise TableNotFound(f"table {table} does not exist; create the Phase 1 tables first")
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id


def _row(body: Mapping[str, Any], knowledge_time: datetime) -> dict[str, Any]:
    row = dict(body, knowledge_time=knowledge_time)
    normalised: list[dict[str, Any]] = pa.Table.from_pylist(
        [row], schema=DATA_QUALITY_REPORTS.arrow_schema
    ).to_pylist()
    return normalised[0]

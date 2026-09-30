"""Bounded ADR-0093 listing-history Quality reporter (rule version 2.0.0).

The legacy listing report remains in ``listing_report.py`` with its inline v1 payload. This
reporter binds caller-supplied PIT Listing and Raw snapshots, uses the bounded Listing replay
verifier, and commits its complete events, revision identities, and availability gaps through
the fixed-size Quality report manifest.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import zip_longest
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]

from core.contracts.catalog import CommitConflict
from core.contracts.storage import StorageAdapter
from core.domain.base import canonical_json
from infrastructure.canonical import listing_rules as lr
from infrastructure.canonical.listings import FINDING_HISTORY_DIVERGED, ListingDeriver
from infrastructure.catalog.bounded_metadata import BoundedMetadataLimits
from infrastructure.catalog.fingerprint import PYARROW_BATCH_FINGERPRINT_RULE_ID
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_EXCHANGE_INFO,
    CANONICAL_INSTRUMENT_LISTINGS,
)
from infrastructure.parser.binance_exchange_info import EXCHANGE_INFO_DECODER_BINDING
from infrastructure.quality.manifest_store import (
    QualityReportManifestStore,
    derive_quality_report_id,
)
from infrastructure.quality.report_streams import (
    QualityReportStreamLimits,
    QualityReportStreamRef,
    QualityReportStreamWriter,
    iter_quality_report_stream,
)
from infrastructure.revision.exchange_info_availability import EXCHANGE_INFO_AVAILABILITY_BINDING
from infrastructure.revision.store import RevisionCatalog
from infrastructure.streaming.runs import RunLimits, RunRef, RunSetBuilder, iter_run

__all__ = [
    "LISTING_HISTORY_V2_RULE_HASH",
    "LISTING_HISTORY_V2_RULE_ID",
    "LISTING_HISTORY_V2_RULE_SPEC",
    "LISTING_HISTORY_V2_RULE_VERSION",
    "ListingHistoryQualityReportError",
    "ListingHistoryQualityReportMissing",
    "ListingHistoryQualityReporterV2",
    "ListingHistoryQualityReportedV2",
]

LISTING_HISTORY_V2_RULE_ID: Final = "hlens.quality.listing-history"
LISTING_HISTORY_V2_RULE_VERSION: Final = "2.0.0"
LISTING_HISTORY_V2_RULE_SPEC: Final[dict[str, Any]] = {
    "rule": LISTING_HISTORY_V2_RULE_ID,
    "version": LISTING_HISTORY_V2_RULE_VERSION,
    "subject": "the exact PIT-bound canonical.instrument_listings history, proven against the "
    "exact PIT-bound raw.binance_spot_exchange_info history",
    "snapshot_bindings": "exactly one positive snapshot ID for each Listing and Raw table; "
    "IDs are supplied by the caller's PIT manifest and resolved under one bounded metadata pin",
    "proof": "ListingDeriver.verify_bounded; all Listing batches are replayed against the "
    "bound Raw prefixes and C3 fingerprints; findings are synchronously streamed",
    "event_order": "report_inputs first; then findings in bounded verifier emission order "
    "(venue symbol and observation time order, followed by diverged revision ID order); "
    "evidence_gaps summary last",
    "event_revision_order": "event ordinal ascending, then revision_id ascending; duplicate "
    "revision IDs within one event fail closed",
    "event_fields": [
        "event_id",
        "event_type",
        "table",
        "observation_key",
        "revision_first_ordinal",
        "revision_count",
        "event_start",
        "event_end",
        "detail",
    ],
    "event_id": {
        "algorithm": "SHA-256 over domain separator, length-prefixed canonical fixed event "
        "fields, then every sorted unique length-prefixed revision ID",
        "domain_separator": "hlens.quality.listing-history@2.0.0/event-id/v1\\0",
        "length": "unsigned 8-byte big-endian UTF-8 byte length",
    },
    "evidence_gaps": "every non-null availability_evidence_gap from the bounded committed Listing "
    "row run; records ordered by revision_id and include quality_report_id, table, revision_id, "
    "and exact gap text",
    "knowledge_time": "first commit's UTC clock reading, not before any Raw or Listing row "
    "knowledge_time represented; reused by every replay",
    "legacy_compatibility": "listing-history@1.0.0 inline reports remain unchanged and are never "
    "used as fallback",
    "thresholds": "none",
}
LISTING_HISTORY_V2_RULE_HASH: Final = hashlib.sha256(
    canonical_json(LISTING_HISTORY_V2_RULE_SPEC).encode("utf-8")
).hexdigest()

_LISTINGS: Final = CANONICAL_INSTRUMENT_LISTINGS.table
_RAW: Final = BINANCE_SPOT_EXCHANGE_INFO.table
_TABLES: Final = (_LISTINGS, _RAW)
_SNAPSHOT_ID: Final = re.compile(r"^[1-9][0-9]*$")
_EVENT_DOMAIN: Final = b"hlens.quality.listing-history@2.0.0/event-id/v1\x00"
_UTC_ZERO: Final = timedelta(0)
_KNOWLEDGE_SCAN_BATCH_ROWS: Final = 65_536
_DEPENDENCY_HASHES: Final = {
    "listing_status": lr.LISTING_STATUS_BINDING.policy_hash,
    "listing_observation": lr.LISTING_OBSERVATION_BINDING.policy_hash,
    "exchange_info_availability": EXCHANGE_INFO_AVAILABILITY_BINDING.policy_hash,
    "exchange_info_decoder": EXCHANGE_INFO_DECODER_BINDING.policy_hash,
    # C3 identifies its versioned fingerprint algorithm by rule ID, not a separate rule hash.
    "listing_fingerprint_rule_id": hashlib.sha256(
        PYARROW_BATCH_FINGERPRINT_RULE_ID.encode("ascii")
    ).hexdigest(),
}
_IDENTITY_HASHES: Final = {
    "quality": LISTING_HISTORY_V2_RULE_HASH,
    **{
        f"{name}@{version}": digest
        for name, version, digest in (
            (
                lr.LISTING_STATUS_BINDING.policy_id,
                lr.LISTING_STATUS_BINDING.version,
                _DEPENDENCY_HASHES["listing_status"],
            ),
            (
                lr.LISTING_OBSERVATION_BINDING.policy_id,
                lr.LISTING_OBSERVATION_BINDING.version,
                _DEPENDENCY_HASHES["listing_observation"],
            ),
            (
                EXCHANGE_INFO_AVAILABILITY_BINDING.policy_id,
                EXCHANGE_INFO_AVAILABILITY_BINDING.version,
                _DEPENDENCY_HASHES["exchange_info_availability"],
            ),
            (
                EXCHANGE_INFO_DECODER_BINDING.policy_id,
                EXCHANGE_INFO_DECODER_BINDING.version,
                _DEPENDENCY_HASHES["exchange_info_decoder"],
            ),
        )
    },
    PYARROW_BATCH_FINGERPRINT_RULE_ID: _DEPENDENCY_HASHES["listing_fingerprint_rule_id"],
}


class ListingHistoryQualityReportError(Exception):
    """A bounded Listing History report could not be derived or committed."""


class ListingHistoryQualityReportMissing(ListingHistoryQualityReportError):
    """No manifest exists for these exact PIT snapshot bindings."""


@dataclass(frozen=True, slots=True)
class ListingHistoryQualityReportedV2:
    """Fixed-size report summary and commit/reuse state; stream rows require explicit readers."""

    report_id: str
    manifest: Mapping[str, Any]
    reused: bool


def _jsonl_size(row: Mapping[str, Any]) -> int:
    return len(canonical_json(dict(row)).encode("utf-8")) + 1


def _snapshot_bindings(
    snapshot_ids: Mapping[str, str],
) -> tuple[list[dict[str, str]], dict[str, str]]:
    if not isinstance(snapshot_ids, Mapping) or set(snapshot_ids) != set(_TABLES):
        raise ListingHistoryQualityReportError(
            "snapshot_ids must bind exactly the PIT Listing and Raw tables"
        )
    normalized: dict[str, str] = {}
    for table in _TABLES:
        snapshot_id = snapshot_ids[table]
        if not isinstance(snapshot_id, str) or _SNAPSHOT_ID.fullmatch(snapshot_id) is None:
            raise ListingHistoryQualityReportError(f"{table} PIT snapshot ID is invalid")
        normalized[table] = snapshot_id
    return (
        [{"table": table, "snapshot_id": normalized[table]} for table in sorted(normalized)],
        normalized,
    )


def _stream_ref_row(ref: QualityReportStreamRef) -> dict[str, Any]:
    return {
        "format_id": ref.format,
        "record_count": ref.record_count,
        "leaf_count": ref.leaf_count,
        "depth": ref.depth,
        "root_key": ref.root_key,
        "root_sha256": ref.root_sha256,
        "root_size": ref.root_size,
    }


def _quality_ref(name: str, value: Mapping[str, Any]) -> QualityReportStreamRef:
    return QualityReportStreamRef(
        name,
        value["format_id"],
        value["record_count"],
        value["leaf_count"],
        value["depth"],
        value["root_key"],
        value["root_sha256"],
        value["root_size"],
    )


def _event_id(
    storage: StorageAdapter,
    revision_run: RunRef | None,
    fixed_fields: Mapping[str, Any],
    *,
    max_run_object_bytes: int,
) -> str:
    fixed_bytes = canonical_json(dict(fixed_fields)).encode("utf-8")
    hasher = hashlib.sha256()
    hasher.update(_EVENT_DOMAIN)
    hasher.update(len(fixed_bytes).to_bytes(8, "big"))
    hasher.update(fixed_bytes)
    if revision_run is not None:
        with iter_run(storage, revision_run, max_object_bytes=max_run_object_bytes) as rows:
            previous: str | None = None
            for row in rows:
                revision_id = row.get("revision_id")
                if not isinstance(revision_id, str) or not revision_id:
                    raise CatalogIntegrityError("Listing event revision ID is invalid")
                if previous is not None and revision_id <= previous:
                    raise CatalogIntegrityError(
                        "Listing event revision IDs are duplicated or out of order"
                    )
                previous = revision_id
                encoded = revision_id.encode("utf-8")
                hasher.update(len(encoded).to_bytes(8, "big"))
                hasher.update(encoded)
    return f"qevt2-{hasher.hexdigest()}"


class _EventWriter:
    def __init__(
        self,
        *,
        scratch_storage: StorageAdapter,
        events: QualityReportStreamWriter,
        revisions: QualityReportStreamWriter,
        capacity: int,
        merge_fanout: int,
        run_limits: RunLimits,
        max_event_record_bytes: int,
        max_revision_record_bytes: int,
        max_run_object_bytes: int,
    ) -> None:
        self._scratch = scratch_storage
        self._events = events
        self._revisions = revisions
        self._capacity = capacity
        self._fanout = merge_fanout
        self._run_limits = run_limits
        self._max_event_bytes = max_event_record_bytes
        self._max_revision_bytes = max_revision_record_bytes
        self._max_run_object_bytes = max_run_object_bytes
        self._event_ordinal = 0
        self._revision_ordinal = 0

    def append(
        self,
        event_type: str,
        *,
        table: str | None,
        instant: datetime | None,
        revision_ids: Iterable[str],
        detail: str | Callable[[int], str],
    ) -> None:
        builder = RunSetBuilder(
            self._scratch,
            key=lambda row: row["revision_id"],
            capacity=self._capacity,
            merge_fanout=self._fanout,
            limits=self._run_limits,
        )
        with builder:
            for revision_id in revision_ids:
                if not isinstance(revision_id, str) or not revision_id:
                    raise CatalogIntegrityError("Listing finding contains an invalid revision ID")
                run_row = {"revision_id": revision_id}
                if _jsonl_size(run_row) > self._max_revision_bytes:
                    raise ListingHistoryQualityReportError(
                        "Listing finding revision exceeds max_revision_record_bytes"
                    )
                builder.add(run_row)
            revision_run = builder.finish()
        revision_count = 0 if revision_run is None else revision_run.record_count
        detail_text = detail(revision_count) if callable(detail) else detail
        if not isinstance(detail_text, str) or not detail_text:
            raise CatalogIntegrityError("Listing event detail is invalid")
        event_fields = {
            "event_type": event_type,
            "table": table,
            "observation_key": None,
            "revision_first_ordinal": self._revision_ordinal,
            "revision_count": revision_count,
            "event_start": None if instant is None else _utc_text(instant),
            "event_end": None,
            "detail": detail_text,
        }
        event_record = {"event_id": "qevt2-" + "0" * 64, **event_fields}
        if _jsonl_size(event_record) > self._max_event_bytes:
            raise ListingHistoryQualityReportError("Listing event exceeds max_event_record_bytes")
        event_record["event_id"] = _event_id(
            self._scratch,
            revision_run,
            {
                "quality_rule_id": LISTING_HISTORY_V2_RULE_ID,
                "quality_rule_version": LISTING_HISTORY_V2_RULE_VERSION,
                "quality_rule_hash": LISTING_HISTORY_V2_RULE_HASH,
                **event_fields,
            },
            max_run_object_bytes=self._max_run_object_bytes,
        )
        if _jsonl_size(event_record) > self._max_event_bytes:
            raise ListingHistoryQualityReportError("Listing event exceeds max_event_record_bytes")
        if revision_run is not None:
            with iter_run(
                self._scratch,
                revision_run,
                max_object_bytes=self._max_run_object_bytes,
            ) as rows:
                previous: str | None = None
                for row in rows:
                    revision_id = row["revision_id"]
                    if previous == revision_id:
                        raise CatalogIntegrityError(f"Listing event repeats revision {revision_id}")
                    previous = revision_id
                    revision_record = {
                        "event_ordinal": self._event_ordinal,
                        "revision_id": revision_id,
                    }
                    if _jsonl_size(revision_record) > self._max_revision_bytes:
                        raise ListingHistoryQualityReportError(
                            "event revision exceeds max_revision_record_bytes"
                        )
                    self._revisions.append(revision_record)
                    self._revision_ordinal += 1
        self._events.append(event_record)
        self._event_ordinal += 1


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != _UTC_ZERO:
        raise CatalogIntegrityError("Listing finding time must be aware UTC")
    return value.astimezone(UTC).isoformat()


class ListingHistoryQualityReporterV2:
    """Append or verify one bounded ``listing-history@2.0.0`` manifest."""

    def __init__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        *,
        scratch_storage: StorageAdapter,
        market_data_base_url: str,
        clock: Callable[[], datetime],
        metadata_limits: BoundedMetadataLimits,
        capacity: int,
        merge_fanout: int,
        run_limits: RunLimits,
        stream_limits: QualityReportStreamLimits,
        max_record_bytes: int,
        max_run_object_bytes: int,
        prefix_leaf_max_records: int,
        prefix_fanout: int,
        prefix_max_node_bytes: int,
        prefix_max_record_bytes: int,
        row_chunk_capacity: int,
        max_hash_chunk_bytes: int,
        max_event_record_bytes: int,
        max_revision_record_bytes: int,
        max_gap_record_bytes: int,
        max_manifest_record_bytes: int,
        max_identity_bytes: int,
        retries: int,
    ) -> None:
        for name, value, minimum in (
            ("capacity", capacity, 1),
            ("merge_fanout", merge_fanout, 2),
            ("max_record_bytes", max_record_bytes, 1),
            ("max_run_object_bytes", max_run_object_bytes, 1),
            ("prefix_leaf_max_records", prefix_leaf_max_records, 1),
            ("prefix_fanout", prefix_fanout, 2),
            ("prefix_max_node_bytes", prefix_max_node_bytes, 1),
            ("prefix_max_record_bytes", prefix_max_record_bytes, 1),
            ("row_chunk_capacity", row_chunk_capacity, 1),
            ("max_hash_chunk_bytes", max_hash_chunk_bytes, 16),
            ("max_event_record_bytes", max_event_record_bytes, 1),
            ("max_revision_record_bytes", max_revision_record_bytes, 1),
            ("max_gap_record_bytes", max_gap_record_bytes, 1),
            ("max_manifest_record_bytes", max_manifest_record_bytes, 1),
            ("max_identity_bytes", max_identity_bytes, 1),
            ("retries", retries, 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if not isinstance(metadata_limits, BoundedMetadataLimits):
            raise ValueError("metadata_limits must be BoundedMetadataLimits")
        if not isinstance(run_limits, RunLimits):
            raise ValueError("run_limits must be RunLimits")
        if not isinstance(stream_limits, QualityReportStreamLimits):
            raise ValueError("stream_limits must be QualityReportStreamLimits")
        if scratch_storage is storage:
            raise ValueError(
                "scratch_storage must be a distinct adapter; caller must isolate its namespace"
            )
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._adapter = adapter
        self._storage = storage
        self._scratch = scratch_storage
        self._origin = market_data_base_url
        self._clock = clock
        self._metadata_limits = metadata_limits
        self._capacity = capacity
        self._fanout = merge_fanout
        self._run_limits = run_limits
        self._stream_limits = stream_limits
        self._limits = {
            "max_record": max_record_bytes,
            "max_run_object": max_run_object_bytes,
            "prefix_leaf_records": prefix_leaf_max_records,
            "prefix_fanout": prefix_fanout,
            "prefix_node_bytes": prefix_max_node_bytes,
            "prefix_record_bytes": prefix_max_record_bytes,
            "row_chunk": row_chunk_capacity,
            "hash_chunk": max_hash_chunk_bytes,
            "event": max_event_record_bytes,
            "revision": max_revision_record_bytes,
            "gap": max_gap_record_bytes,
            "manifest": max_manifest_record_bytes,
            "identity": max_identity_bytes,
            "retries": retries,
        }
        self._store = QualityReportManifestStore(
            adapter,
            quality_rule_id=LISTING_HISTORY_V2_RULE_ID,
            quality_rule_version=LISTING_HISTORY_V2_RULE_VERSION,
            quality_rule_hash=LISTING_HISTORY_V2_RULE_HASH,
            allowed_snapshot_tables=_TABLES,
            required_snapshot_tables=_TABLES,
            identity_rule_hashes=_IDENTITY_HASHES,
            max_identity_rule_hashes=len(_IDENTITY_HASHES),
            stream_limits=stream_limits,
            max_manifest_record_bytes=max_manifest_record_bytes,
        )

    def report(
        self, snapshot_ids: Mapping[str, str], *, existing_only: bool = False
    ) -> ListingHistoryQualityReportedV2:
        bindings, normalized = _snapshot_bindings(snapshot_ids)
        report_id = derive_quality_report_id(
            quality_rule_id=LISTING_HISTORY_V2_RULE_ID,
            quality_rule_version=LISTING_HISTORY_V2_RULE_VERSION,
            quality_rule_hash=LISTING_HISTORY_V2_RULE_HASH,
            identity_rule_hashes=_IDENTITY_HASHES,
            max_identity_rule_hashes=len(_IDENTITY_HASHES),
            subject_table=_LISTINGS,
            subject_snapshot_id=normalized[_LISTINGS],
            subject_symbol=None,
            subject_start=None,
            subject_end=None,
            snapshot_bindings=bindings,
            max_identity_bytes=self._limits["identity"],
        )
        existing = self._store.lookup(report_id)
        if existing is None and existing_only:
            raise ListingHistoryQualityReportMissing(
                f"no committed Listing History v2 manifest for {report_id}"
            )
        target = self._scratch if existing is not None else self._storage
        writers = self._writers(target)
        event_writer = _EventWriter(
            scratch_storage=self._scratch,
            events=writers[0],
            revisions=writers[1],
            capacity=self._capacity,
            merge_fanout=self._fanout,
            run_limits=self._run_limits,
            max_event_record_bytes=self._limits["event"],
            max_revision_record_bytes=self._limits["revision"],
            max_run_object_bytes=self._limits["max_run_object"],
        )
        try:
            event_writer.append(
                "report_inputs",
                table=None,
                instant=None,
                revision_ids=(),
                detail=canonical_json(normalized),
            )
            proof = self._verify(normalized, event_writer)
            floor = self._knowledge_floor(proof.committed_rows, normalized[_RAW])
            gap_count = self._project_gaps(
                proof.committed_rows,
                writer=writers[2],
                report_id=report_id,
                max_run_object_bytes=self._limits["max_run_object"],
            )
            event_writer.append(
                "evidence_gaps",
                table="quality.availability_evidence_gaps",
                instant=None,
                revision_ids=(),
                detail=f"{gap_count} listing evidence gap record(s) in the evidence_gaps stream",
            )
            refs = tuple(writer.finish() for writer in writers)
        except BaseException:
            for writer in writers:
                writer.close()
            raise
        names = ("events", "event_revisions", "evidence_gaps")
        expected = dict(zip(names, refs, strict=True))
        if existing is not None:
            if existing["knowledge_time"] < floor:
                raise CatalogIntegrityError(
                    "stored listing report knowledge_time predates a represented Raw or Listing row"
                )
            self._compare_streams(existing, expected)
            return ListingHistoryQualityReportedV2(report_id, existing, True)

        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != _UTC_ZERO:
            raise ListingHistoryQualityReportError("clock must return aware UTC")
        if now < floor:
            raise ListingHistoryQualityReportError(
                "report clock precedes a Raw or Listing row it describes"
            )
        row = {
            "report_id": report_id,
            "quality_rule_id": LISTING_HISTORY_V2_RULE_ID,
            "quality_rule_version": LISTING_HISTORY_V2_RULE_VERSION,
            "quality_rule_hash": LISTING_HISTORY_V2_RULE_HASH,
            "subject_table": _LISTINGS,
            "subject_snapshot_id": normalized[_LISTINGS],
            "subject_symbol": None,
            "subject_start": None,
            "subject_end": None,
            "snapshot_bindings": bindings,
            "knowledge_time": now,
            **expected,
        }
        last: Exception | None = None
        for _ in range(self._limits["retries"]):
            try:
                committed = self._store.commit(row)
            except CommitConflict as exc:
                last = exc
                continue
            return ListingHistoryQualityReportedV2(report_id, committed, False)
        raise ListingHistoryQualityReportError(
            "manifest commit lost repeated catalog races"
        ) from last

    def _writers(self, target: StorageAdapter) -> tuple[QualityReportStreamWriter, ...]:
        return tuple(
            QualityReportStreamWriter(target, name, limits=self._stream_limits)
            for name in ("events", "event_revisions", "evidence_gaps")
        )

    def _verify(
        self,
        snapshot_ids: Mapping[str, str],
        event_writer: _EventWriter,
    ) -> Any:
        def finding_sink(
            code: str,
            symbol: str,
            instant: Any,
            revision_ids: Iterable[str],
            detail: Callable[[int], str],
        ) -> None:
            if not isinstance(instant, datetime):
                raise CatalogIntegrityError("Listing finding instant is not a datetime")
            table = _LISTINGS if code == FINDING_HISTORY_DIVERGED else _RAW
            event_writer.append(
                code,
                table=table,
                instant=instant,
                revision_ids=revision_ids,
                detail=lambda count: f"{symbol}: {detail(count)}",
            )

        deriver = ListingDeriver(
            self._adapter,
            self._storage,
            market_data_base_url=self._origin,
            clock=lambda: datetime.min.replace(tzinfo=UTC),
        )
        try:
            return deriver.verify_bounded(
                snapshot_ids=snapshot_ids,
                scratch_storage=self._scratch,
                metadata_limits=self._metadata_limits,
                capacity=self._capacity,
                merge_fanout=self._fanout,
                limits=self._run_limits,
                max_record_bytes=self._limits["max_record"],
                max_run_object_bytes=self._limits["max_run_object"],
                prefix_leaf_max_records=self._limits["prefix_leaf_records"],
                prefix_fanout=self._limits["prefix_fanout"],
                prefix_max_node_bytes=self._limits["prefix_node_bytes"],
                prefix_max_record_bytes=self._limits["prefix_record_bytes"],
                row_chunk_capacity=self._limits["row_chunk"],
                max_hash_chunk_bytes=self._limits["hash_chunk"],
                finding_sink=finding_sink,
            )
        finally:
            deriver.close()

    def _knowledge_floor(self, committed_rows: RunRef | None, raw_snapshot_id: str) -> datetime:
        """Stream one Raw knowledge-time column at the exact PIT ID.

        The adapter yields one file-backed RecordBatch at a time, at most 65,536 rows, with one
        batch and one fragment readahead. The loop retains only the scalar maximum; no Arrow table
        or Python row list is built.
        """
        floor = datetime.min.replace(tzinfo=UTC)
        if committed_rows is not None:
            with iter_run(
                self._scratch,
                committed_rows,
                max_object_bytes=self._limits["max_run_object"],
            ) as rows:
                for row in rows:
                    value = row.get("knowledge_time")
                    floor = max(floor, _utc_datetime(value, "Listing knowledge_time"))
        scan = getattr(self._adapter, "scan_column_batches", None)
        if not callable(scan):
            raise ListingHistoryQualityReportError(
                "bounded Listing report requires exact-snapshot streaming Raw reads"
            )
        reader = scan(_RAW, columns=("knowledge_time",), snapshot_id=raw_snapshot_id)
        try:
            for batch in reader:
                if (
                    not isinstance(batch, pa.RecordBatch)
                    or batch.num_columns != 1
                    or batch.num_rows > _KNOWLEDGE_SCAN_BATCH_ROWS
                ):
                    raise CatalogIntegrityError("Raw knowledge-time scan returned an invalid batch")
                for scalar in batch.column(0):
                    floor = max(floor, _utc_datetime(scalar.as_py(), "Raw knowledge_time"))
        finally:
            close = getattr(reader, "close", None)
            if callable(close):
                close()
        return floor

    def _project_gaps(
        self,
        committed_rows: RunRef | None,
        *,
        writer: QualityReportStreamWriter,
        report_id: str,
        max_run_object_bytes: int,
    ) -> int:
        count = 0
        try:
            if committed_rows is not None:
                with iter_run(
                    self._scratch,
                    committed_rows,
                    max_object_bytes=max_run_object_bytes,
                ) as rows:
                    previous: str | None = None
                    for row in rows:
                        revision_id = row.get("revision_id")
                        gap = row.get("availability_evidence_gap")
                        if not isinstance(revision_id, str) or not revision_id:
                            raise CatalogIntegrityError("Listing committed row has no revision ID")
                        if previous is not None and revision_id <= previous:
                            raise CatalogIntegrityError(
                                "Listing committed rows are duplicated or out of revision order"
                            )
                        previous = revision_id
                        if gap is None:
                            continue
                        if not isinstance(gap, str) or not gap:
                            raise CatalogIntegrityError("Listing evidence gap is invalid")
                        record = {
                            "quality_report_id": report_id,
                            "table": _LISTINGS,
                            "revision_id": revision_id,
                            "gap": gap,
                        }
                        if _jsonl_size(record) > self._limits["gap"]:
                            raise ListingHistoryQualityReportError(
                                "Listing evidence gap exceeds max_gap_record_bytes"
                            )
                        writer.append(record)
                        count += 1
            return count
        except BaseException:
            writer.close()
            raise

    def _compare_streams(
        self, existing: Mapping[str, Any], expected: Mapping[str, QualityReportStreamRef]
    ) -> None:
        for name, ref in expected.items():
            if existing[name] != _stream_ref_row(ref):
                raise CatalogIntegrityError(
                    f"stored listing report {name} root differs from replay"
                )
            with (
                iter_quality_report_stream(
                    self._storage,
                    _quality_ref(name, existing[name]),
                    limits=self._stream_limits,
                ) as stored,
                iter_quality_report_stream(
                    self._scratch,
                    ref,
                    limits=self._stream_limits,
                ) as derived,
            ):
                sentinel = object()
                for old, new in zip_longest(stored, derived, fillvalue=sentinel):
                    if old is sentinel or new is sentinel or old != new:
                        raise CatalogIntegrityError(
                            f"stored listing report {name} stream differs from re-derivation"
                        )


def _utc_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != _UTC_ZERO:
        raise CatalogIntegrityError(f"{label} must be aware UTC")
    return value.astimezone(UTC)

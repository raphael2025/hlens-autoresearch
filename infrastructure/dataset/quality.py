"""Dataset v3's bounded, exact-snapshot Quality report consumer (ADR-0093 / ADR-0077)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final, Protocol

from core.contracts.revision import PointInTimeSpec
from core.contracts.storage import StorageAdapter
from infrastructure.canonical import rules
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_EXCHANGE_INFO,
    CANONICAL_INSTRUMENT_LISTINGS,
    DATA_QUALITY_REPORT_MANIFESTS,
)
from infrastructure.dataset.builder import DatasetQualityError
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.quality.listing_report_v2 import (
    LISTING_HISTORY_V2_RULE_HASH,
    LISTING_HISTORY_V2_RULE_ID,
    LISTING_HISTORY_V2_RULE_VERSION,
    ListingHistoryQualityReporterV2,
)
from infrastructure.quality.report_projection import (
    CANONICAL_PARTITION_V3_RULE_HASH,
    CANONICAL_PARTITION_V3_RULE_ID,
    CANONICAL_PARTITION_V3_RULE_VERSION,
)
from infrastructure.quality.report_streams import (
    QualityReportStreamLimits,
    QualityReportStreamRef,
    iter_quality_report_stream,
)
from infrastructure.quality.report_v3 import QualityReporterV3
from infrastructure.revision.store import RevisionCatalog
from infrastructure.settings import local_file_uri_to_path
from infrastructure.storage.local import LocalFileStorageAdapter
from infrastructure.streaming.runs import RunLimits, RunRef, RunSetBuilder, iter_run

__all__ = ["BoundedQualityEvidence", "QualityReporterFactory", "ListingReporterFactory"]

_MANIFEST_TABLE: Final = DATA_QUALITY_REPORT_MANIFESTS.table
_LISTINGS: Final = CANONICAL_INSTRUMENT_LISTINGS.table
_RAW: Final = BINANCE_SPOT_EXCHANGE_INFO.table


def _reject_local_storage_aliases(
    evidence_storage: StorageAdapter, scratch_storage: StorageAdapter
) -> None:
    """Reject overlapping Local adapter roots, including distinct aliases of one namespace."""
    evidence_adapters = _local_storage_adapters(evidence_storage)
    scratch_adapters = _local_storage_adapters(scratch_storage)
    if not evidence_adapters or not scratch_adapters:
        return
    evidence_roots = tuple(
        local_file_uri_to_path(adapter.warehouse_uri, field_name="warehouse_uri").resolve()
        for adapter in evidence_adapters
    ) + tuple(
        local_file_uri_to_path(adapter.staging_uri, field_name="staging_uri").resolve()
        for adapter in evidence_adapters
    )
    scratch_roots = tuple(
        local_file_uri_to_path(adapter.warehouse_uri, field_name="warehouse_uri").resolve()
        for adapter in scratch_adapters
    ) + tuple(
        local_file_uri_to_path(adapter.staging_uri, field_name="staging_uri").resolve()
        for adapter in scratch_adapters
    )
    for evidence_root in evidence_roots:
        for scratch_root in scratch_roots:
            if (
                evidence_root == scratch_root
                or evidence_root in scratch_root.parents
                or scratch_root in evidence_root.parents
            ):
                raise DatasetQualityError(
                    "Quality gap join scratch roots must not overlap evidence storage roots"
                )


def _local_storage_adapters(storage: StorageAdapter) -> tuple[LocalFileStorageAdapter, ...]:
    """Find Local adapters behind small delegating wrappers used for instrumentation/tests."""
    found: list[LocalFileStorageAdapter] = []
    pending: list[object] = [storage]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(current, LocalFileStorageAdapter):
            found.append(current)
            continue
        inner = getattr(current, "inner", None)
        if inner is not None:
            pending.append(inner)
    return tuple(found)


class QualityReporterFactory(Protocol):
    def __call__(self, view: PinnedCatalogView) -> QualityReporterV3: ...


class ListingReporterFactory(Protocol):
    def __call__(self, view: PinnedCatalogView) -> ListingHistoryQualityReporterV2: ...


def _manifest_ref(name: str, row: Mapping[str, Any]) -> QualityReportStreamRef:
    try:
        return QualityReportStreamRef(
            name,
            row["format_id"],
            row["record_count"],
            row["leaf_count"],
            row["depth"],
            row["root_key"],
            row["root_sha256"],
            row["root_size"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CatalogIntegrityError("Quality manifest has a malformed evidence-gap root") from exc


def _bindings(pit: PointInTimeSpec, value: object) -> dict[str, str]:
    if not isinstance(value, list):
        raise CatalogIntegrityError("Quality manifest snapshot_bindings is not a list")
    result: dict[str, str] = {}
    previous: str | None = None
    for item in value:
        if not isinstance(item, Mapping):
            raise CatalogIntegrityError("Quality manifest has a malformed snapshot binding")
        table, snapshot_id = item.get("table"), item.get("snapshot_id")
        if not isinstance(table, str) or not isinstance(snapshot_id, str):
            raise CatalogIntegrityError("Quality manifest has a malformed snapshot binding")
        if previous is not None and table <= previous:
            raise CatalogIntegrityError(
                "Quality manifest snapshot bindings are duplicate or unordered"
            )
        if pit.snapshot_bindings.get(table) != snapshot_id:
            raise CatalogIntegrityError(
                f"Quality report snapshot for {table} differs from Dataset PIT binding"
            )
        result[table] = snapshot_id
        previous = table
    return result


def _check_manifest_identity(
    manifest: Mapping[str, Any],
    *,
    report_id: str,
    rule_id: str,
    rule_version: str,
    rule_hash: str,
    subject_table: str,
    subject_snapshot_id: str,
    pit: PointInTimeSpec,
) -> None:
    if (
        manifest.get("report_id") != report_id
        or manifest.get("quality_rule_id") != rule_id
        or manifest.get("quality_rule_version") != rule_version
        or manifest.get("quality_rule_hash") != rule_hash
        or manifest.get("subject_table") != subject_table
        or manifest.get("subject_snapshot_id") != subject_snapshot_id
    ):
        raise DatasetQualityError("Quality report identity or subject differs from Dataset request")
    _bindings(pit, manifest.get("snapshot_bindings"))
    if manifest.get("events") is None or manifest.get("event_revisions") is None:
        raise CatalogIntegrityError("bounded Quality manifest is missing an event stream reference")


class BoundedQualityEvidence:
    """Re-derive only ADR-0093 reports and stream their verified gap roots on demand.

    Reporter factories are mandatory and receive this source's PIT-pinned catalog view. They
    supply the project's explicit run/byte limits; no limits or legacy fallback are selected here.
    """

    def __init__(
        self,
        adapter: RevisionCatalog,
        evidence_storage: StorageAdapter,
        pit: PointInTimeSpec,
        data_type: str,
        *,
        canonical_reporter: QualityReporterFactory,
        listing_reporter: ListingReporterFactory,
        stream_limits: QualityReportStreamLimits,
        scratch_storage: StorageAdapter,
        run_capacity: int,
        merge_fanout: int,
        run_limits: RunLimits,
        max_run_object_bytes: int,
    ) -> None:
        if not isinstance(pit, PointInTimeSpec):
            raise DatasetQualityError("bounded Quality source requires a PointInTimeSpec")
        if _MANIFEST_TABLE not in pit.snapshot_bindings:
            raise DatasetQualityError(f"the PIT spec does not bind {_MANIFEST_TABLE}")
        if _LISTINGS not in pit.snapshot_bindings or _RAW not in pit.snapshot_bindings:
            raise DatasetQualityError("the PIT spec must bind exact Listing and Raw snapshots")
        if data_type not in rules.CANONICAL_TABLES:
            raise DatasetQualityError(f"unsupported Dataset data type {data_type!r}")
        if not callable(canonical_reporter) or not callable(listing_reporter):
            raise DatasetQualityError("bounded Quality reporter factories must be callable")
        if not isinstance(stream_limits, QualityReportStreamLimits):
            raise DatasetQualityError("stream_limits must be QualityReportStreamLimits")
        if not isinstance(run_limits, RunLimits):
            raise DatasetQualityError("run_limits must be RunLimits")
        if scratch_storage is evidence_storage:
            raise DatasetQualityError(
                "Quality gap join scratch must be isolated from evidence storage"
            )
        _reject_local_storage_aliases(evidence_storage, scratch_storage)
        if (
            isinstance(run_capacity, bool)
            or not isinstance(run_capacity, int)
            or run_capacity < 1
            or isinstance(merge_fanout, bool)
            or not isinstance(merge_fanout, int)
            or merge_fanout < 2
            or isinstance(max_run_object_bytes, bool)
            or not isinstance(max_run_object_bytes, int)
            or max_run_object_bytes < 1
        ):
            raise DatasetQualityError("Quality gap join limits are invalid")
        self._pit = pit
        self._data_type = data_type
        self._storage = evidence_storage
        self._scratch = scratch_storage
        self._stream_limits = stream_limits
        self._run_capacity = run_capacity
        self._merge_fanout = merge_fanout
        self._run_limits = run_limits
        self._max_run_object_bytes = max_run_object_bytes
        self._view = PinnedCatalogView(adapter, pit.snapshot_bindings)
        self._canonical_factory = canonical_reporter
        self._listing_factory = listing_reporter
        self._active_report_id: str | None = None
        self._active_manifest: Mapping[str, Any] | None = None
        self._claims: RunSetBuilder | None = None

    def listing_report(self) -> str:
        reporter = self._listing_factory(self._view)
        self._validate_reporter_storage(reporter)
        report = reporter.report(
            {
                _LISTINGS: self._pit.snapshot_bindings[_LISTINGS],
                _RAW: self._pit.snapshot_bindings[_RAW],
            },
            existing_only=True,
        )
        _check_manifest_identity(
            report.manifest,
            report_id=report.report_id,
            rule_id=LISTING_HISTORY_V2_RULE_ID,
            rule_version=LISTING_HISTORY_V2_RULE_VERSION,
            rule_hash=LISTING_HISTORY_V2_RULE_HASH,
            subject_table=_LISTINGS,
            subject_snapshot_id=self._pit.snapshot_bindings[_LISTINGS],
            pit=self._pit,
        )
        if (
            report.manifest.get("subject_symbol") is not None
            or report.manifest.get("subject_start") is not None
        ):
            raise DatasetQualityError("listing-history report has a partition subject")
        self._activate(report.report_id, report.manifest)
        return report.report_id

    def partition_report(self, venue_symbol: str, day: date) -> str:
        if not isinstance(day, date) or isinstance(day, datetime):
            raise DatasetQualityError("Quality partition day must be a date")
        reporter = self._canonical_factory(self._view)
        self._validate_reporter_storage(reporter)
        report = reporter.report(self._data_type, venue_symbol, day, existing_only=True)
        canonical = rules.CANONICAL_TABLES[self._data_type].table
        subject_start = datetime.combine(day, time.min, tzinfo=UTC)
        subject_end = subject_start + timedelta(days=1)
        _check_manifest_identity(
            report.manifest,
            report_id=report.report_id,
            rule_id=CANONICAL_PARTITION_V3_RULE_ID,
            rule_version=CANONICAL_PARTITION_V3_RULE_VERSION,
            rule_hash=CANONICAL_PARTITION_V3_RULE_HASH,
            subject_table=canonical,
            subject_snapshot_id=self._pit.snapshot_bindings.get(canonical, ""),
            pit=self._pit,
        )
        if (
            report.manifest.get("subject_symbol") != venue_symbol
            or report.manifest.get("subject_start") != subject_start
            or report.manifest.get("subject_end") != subject_end
        ):
            raise DatasetQualityError("canonical-partition report subject differs from Dataset day")
        self._activate(report.report_id, report.manifest)
        return report.report_id

    def _validate_reporter_storage(self, reporter: object) -> None:
        reporter_storage = getattr(reporter, "_storage", None)
        reporter_scratch = getattr(reporter, "_scratch", None)
        if (
            reporter_storage is not self._storage
            or reporter_scratch is None
            or any(
                not callable(getattr(reporter_scratch, name, None))
                for name in ("stage", "publish", "lookup", "open_read")
            )
        ):
            raise DatasetQualityError(
                "bounded Quality reporter must use the Dataset evidence adapter and expose its "
                "scratch adapter"
            )
        _reject_local_storage_aliases(self._storage, reporter_scratch)
        _reject_local_storage_aliases(self._scratch, reporter_scratch)

    def recorded_gap(self, report_id: str, table: str, revision_id: str) -> str | None:
        raise DatasetQualityError("bounded Quality gaps must use the external ordered-join path")

    def claim_gap(self, report_id: str, table: str, revision_id: str, gap: str) -> None:
        manifest = self._active_manifest
        if report_id != self._active_report_id or manifest is None:
            raise DatasetQualityError("gap claim does not name the active verified report")
        if (
            not isinstance(table, str)
            or not table
            or not isinstance(revision_id, str)
            or not revision_id
            or not isinstance(gap, str)
            or not gap
        ):
            raise DatasetQualityError("gap claim is malformed")
        if self._claims is None:
            self._claims = RunSetBuilder(
                self._scratch,
                key=lambda row: (row["table"], row["revision_id"]),
                capacity=self._run_capacity,
                merge_fanout=self._merge_fanout,
                limits=self._run_limits,
            )
        self._claims.add({"table": table, "revision_id": revision_id, "gap": gap})

    def finish_reports(self) -> None:
        self._finish_active()

    def close(self) -> None:
        if self._claims is not None:
            self._claims.close()
            self._claims = None
        self._active_report_id = None
        self._active_manifest = None

    def _activate(self, report_id: str, manifest: Mapping[str, Any]) -> None:
        if self._active_report_id is not None and self._active_report_id != report_id:
            self._finish_active()
        self._active_report_id = report_id
        self._active_manifest = manifest

    def _finish_active(self) -> None:
        report_id, manifest = self._active_report_id, self._active_manifest
        if report_id is None or manifest is None:
            return
        builder, self._claims = self._claims, None
        claims: RunRef | None = None
        try:
            if builder is not None:
                claims = builder.finish()
            ref = _manifest_ref("evidence_gaps", manifest.get("evidence_gaps", {}))
            previous: tuple[str, str] | None = None
            with iter_quality_report_stream(
                self._storage, ref, limits=self._stream_limits
            ) as actual:
                if claims is None:
                    for row in actual:
                        self._check_stored_gap_row(row, report_id, previous)
                        previous = (row["table"], row["revision_id"])
                else:
                    with iter_run(
                        self._scratch,
                        claims,
                        max_object_bytes=self._max_run_object_bytes,
                    ) as wanted:
                        claim = next(wanted, None)
                        for row in actual:
                            self._check_stored_gap_row(row, report_id, previous)
                            key = (row["table"], row["revision_id"])
                            previous = key
                            while (
                                claim is not None and (claim["table"], claim["revision_id"]) < key
                            ):
                                raise DatasetQualityError(
                                    f"report {report_id} omits Dataset evidence gap "
                                    f"{claim['table']} revision {claim['revision_id']}"
                                )
                            if claim is not None and (claim["table"], claim["revision_id"]) == key:
                                if claim["gap"] != row["gap"]:
                                    raise DatasetQualityError(
                                        f"report {report_id} records a different gap for "
                                        f"{row['table']} revision {row['revision_id']}"
                                    )
                                claim = next(wanted, None)
                        if claim is not None:
                            raise DatasetQualityError(
                                f"report {report_id} omits Dataset evidence gap "
                                f"{claim['table']} revision {claim['revision_id']}"
                            )
        finally:
            if builder is not None:
                builder.close()

    @staticmethod
    def _check_stored_gap_row(
        row: Mapping[str, Any], report_id: str, previous: tuple[str, str] | None
    ) -> None:
        if not isinstance(row, Mapping):
            raise CatalogIntegrityError("Quality evidence-gap stream has a malformed row")
        quality_report_id = row.get("quality_report_id")
        table, revision_id, gap = row.get("table"), row.get("revision_id"), row.get("gap")
        if (
            quality_report_id != report_id
            or not isinstance(table, str)
            or not isinstance(revision_id, str)
            or not isinstance(gap, str)
            or not gap
        ):
            raise CatalogIntegrityError("Quality evidence-gap row has invalid identity")
        key = (table, revision_id)
        if previous is not None and key <= previous:
            raise CatalogIntegrityError("Quality evidence-gap stream is duplicate or unordered")

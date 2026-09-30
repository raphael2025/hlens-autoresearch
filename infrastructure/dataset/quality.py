"""Bounded, fixed-snapshot Quality consumer for Dataset schema 2.5+ (ADR-0093 §3.5)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final, Protocol, cast

from pyiceberg.expressions import EqualTo

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
    _IDENTITY_HASHES as _LISTING_V2_IDENTITY_HASHES,
)
from infrastructure.quality.listing_report_v2 import (
    LISTING_HISTORY_V2_RULE_HASH,
    LISTING_HISTORY_V2_RULE_ID,
    LISTING_HISTORY_V2_RULE_VERSION,
)
from infrastructure.quality.manifest_store import derive_quality_report_id
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
from infrastructure.quality.report_v3 import _INPUT_TABLES as _CANONICAL_V3_INPUTS
from infrastructure.revision.store import RevisionCatalog
from infrastructure.settings import local_file_uri_to_path
from infrastructure.storage.local import LocalFileStorageAdapter
from infrastructure.streaming.runs import RunLimits, RunRef, RunSetBuilder, iter_run

__all__ = ["BoundedQualityEvidence", "BoundedQualityEvidenceFactory", "BoundedQualitySourceParams"]

_MANIFEST_TABLE: Final = DATA_QUALITY_REPORT_MANIFESTS.table
_LISTINGS: Final = CANONICAL_INSTRUMENT_LISTINGS.table
_RAW: Final = BINANCE_SPOT_EXCHANGE_INFO.table


class QualityIdentityRegistry(Protocol):
    """Caller-owned, finite identity hashes for the registered canonical v3 rule."""

    @property
    def canonical_v3_hashes(self) -> Mapping[str, str]: ...

    @property
    def max_identity_rule_hashes(self) -> int: ...

    @property
    def max_identity_bytes(self) -> int: ...


@dataclass(frozen=True, slots=True)
class BoundedQualitySourceParams:
    """All Quality join/read bounds and identity hashes, with no DQ-9-selected defaults."""

    scratch_storage: StorageAdapter
    stream_limits: QualityReportStreamLimits
    run_limits: RunLimits
    run_capacity: int
    merge_fanout: int
    max_run_object_bytes: int
    identity_registry: QualityIdentityRegistry


class BoundedQualityEvidenceFactory(Protocol):
    """Build a bounded source using the already pinned view and explicit caller parameters."""

    def __call__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        pit: PointInTimeSpec,
        data_type: str,
        *,
        view: PinnedCatalogView,
        canonical_scratch_directory: Path,
        params: BoundedQualitySourceParams,
    ) -> BoundedQualityEvidence: ...


class _DatasetQualityReader(Protocol):
    def __call__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        pit: PointInTimeSpec,
        data_type: str,
        *,
        view: PinnedCatalogView,
        canonical_scratch_directory: Path,
        params: BoundedQualitySourceParams,
    ) -> BoundedQualityEvidence: ...


def _local_adapters(storage: StorageAdapter) -> tuple[LocalFileStorageAdapter, ...]:
    found: list[LocalFileStorageAdapter] = []
    pending: list[object] = [storage]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, LocalFileStorageAdapter):
            found.append(current)
        else:
            inner = getattr(current, "inner", None)
            if inner is not None:
                pending.append(inner)
    return tuple(found)


def _ensure_isolated(evidence: StorageAdapter, scratch: StorageAdapter) -> None:
    if evidence is scratch:
        raise DatasetQualityError("Quality gap scratch must use a distinct storage adapter")
    left = _local_adapters(evidence)
    right = _local_adapters(scratch)
    for a in left:
        for b in right:
            roots_a = (
                local_file_uri_to_path(a.warehouse_uri, field_name="warehouse_uri").resolve(),
                local_file_uri_to_path(a.staging_uri, field_name="staging_uri").resolve(),
            )
            roots_b = (
                local_file_uri_to_path(b.warehouse_uri, field_name="warehouse_uri").resolve(),
                local_file_uri_to_path(b.staging_uri, field_name="staging_uri").resolve(),
            )
            if any(x == y or x in y.parents or y in x.parents for x in roots_a for y in roots_b):
                raise DatasetQualityError(
                    "Quality gap join scratch roots must not overlap evidence storage roots"
                )


_reject_local_storage_aliases = _ensure_isolated


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


def _check_manifest(
    manifest: Mapping[str, Any],
    *,
    report_id: str,
    rule_id: str,
    rule_version: str,
    rule_hash: str,
    subject_table: str,
    subject_snapshot: str,
    expected_bindings: Mapping[str, str],
    pit: PointInTimeSpec,
) -> None:
    if (
        manifest.get("report_id") != report_id
        or manifest.get("quality_rule_id") != rule_id
        or manifest.get("quality_rule_version") != rule_version
        or manifest.get("quality_rule_hash") != rule_hash
        or manifest.get("subject_table") != subject_table
        or manifest.get("subject_snapshot_id") != subject_snapshot
    ):
        raise DatasetQualityError("Quality report identity/subject differs from Dataset inputs")
    bindings = manifest.get("snapshot_bindings")
    if not isinstance(bindings, list):
        raise CatalogIntegrityError("Quality manifest snapshot_bindings is malformed")
    previous: str | None = None
    observed: dict[str, str] = {}
    for item in bindings:
        if not isinstance(item, Mapping):
            raise CatalogIntegrityError("Quality manifest has a malformed snapshot binding")
        table, snapshot = item.get("table"), item.get("snapshot_id")
        if not isinstance(table, str) or not isinstance(snapshot, str):
            raise CatalogIntegrityError("Quality manifest has a malformed snapshot binding")
        if previous is not None and table <= previous:
            raise CatalogIntegrityError("Quality manifest snapshot bindings duplicate or unordered")
        if pit.snapshot_bindings.get(table) != snapshot:
            raise DatasetQualityError(f"Quality report snapshot for {table} differs from Dataset")
        observed[table] = snapshot
        previous = table
    if observed != dict(expected_bindings):
        raise DatasetQualityError("Quality report bindings differ from its registered inputs")
    if not all(manifest.get(name) for name in ("events", "event_revisions", "evidence_gaps")):
        raise CatalogIntegrityError("Quality manifest is missing an evidence stream root")


class BoundedQualityEvidence:
    """Validate v2 listing/v3 partition reports and merge their gap roots against Dataset claims.

    All limits and identity hashes are required inputs. This object reads only committed rows at
    the fixed PIT ``PinnedCatalogView``; it never invokes a reporter or chooses a snapshot.
    """

    def __init__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        pit: PointInTimeSpec,
        data_type: str,
        *,
        view: PinnedCatalogView,
        params: BoundedQualitySourceParams,
        canonical_scratch_directory: Path,
    ) -> None:
        if not isinstance(pit, PointInTimeSpec):
            raise DatasetQualityError("bounded Quality source requires a PointInTimeSpec")
        if pit.snapshot_bindings.get(_MANIFEST_TABLE) is None:
            raise DatasetQualityError(f"the PIT spec does not bind {_MANIFEST_TABLE}")
        if _LISTINGS not in pit.snapshot_bindings or _RAW not in pit.snapshot_bindings:
            raise DatasetQualityError("Dataset PIT must bind exact Listing and Raw snapshots")
        if not isinstance(params, BoundedQualitySourceParams):
            raise DatasetQualityError("explicit bounded Quality source parameters are required")
        if not isinstance(params.stream_limits, QualityReportStreamLimits) or not isinstance(
            params.run_limits, RunLimits
        ):
            raise DatasetQualityError("Quality stream and run limits are required")
        for name, value, minimum in (
            ("run_capacity", params.run_capacity, 1),
            ("merge_fanout", params.merge_fanout, 2),
            ("max_run_object_bytes", params.max_run_object_bytes, 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise DatasetQualityError(f"{name} must be an integer >= {minimum}")
        _ensure_isolated(storage, params.scratch_storage)
        self._adapter = adapter
        self._storage = storage
        self._scratch = params.scratch_storage
        self._pit = pit
        self._data_type = data_type
        self._view = view
        self._canonical_scratch_directory = canonical_scratch_directory
        self._params = params
        self._stream_limits = params.stream_limits
        self._capacity = params.run_capacity
        self._fanout = params.merge_fanout
        self._run_limits = params.run_limits
        self._max_run_object_bytes = params.max_run_object_bytes
        self._active_report: str | None = None
        self._active_manifest: Mapping[str, Any] | None = None
        self._claims: RunSetBuilder | None = None

    @property
    def view(self) -> PinnedCatalogView:
        """The exact Dataset PIT view supplied by the source factory."""
        return self._view

    def listing_report(self) -> str:
        bindings = {
            _LISTINGS: self._pit.snapshot_bindings[_LISTINGS],
            _RAW: self._pit.snapshot_bindings[_RAW],
        }
        manifest_id = derive_quality_report_id(
            quality_rule_id=LISTING_HISTORY_V2_RULE_ID,
            quality_rule_version=LISTING_HISTORY_V2_RULE_VERSION,
            quality_rule_hash=LISTING_HISTORY_V2_RULE_HASH,
            identity_rule_hashes=_LISTING_V2_IDENTITY_HASHES,
            max_identity_rule_hashes=self._params.identity_registry.max_identity_rule_hashes,
            subject_table=_LISTINGS,
            subject_snapshot_id=bindings[_LISTINGS],
            subject_symbol=None,
            subject_start=None,
            subject_end=None,
            snapshot_bindings=[
                {"table": table, "snapshot_id": sid} for table, sid in sorted(bindings.items())
            ],
            max_identity_bytes=self._params.identity_registry.max_identity_bytes,
        )
        manifest = self._read_one(
            DATA_QUALITY_REPORT_MANIFESTS.table, manifest_id, self._manifest_columns()
        )
        return self._select_report(
            manifest_id,
            manifest,
            identity=(
                LISTING_HISTORY_V2_RULE_ID,
                LISTING_HISTORY_V2_RULE_VERSION,
                LISTING_HISTORY_V2_RULE_HASH,
                _LISTINGS,
                bindings[_LISTINGS],
                None,
                None,
                None,
                bindings,
            ),
        )

    def partition_report(self, venue_symbol: str, day: date) -> str:
        if not isinstance(day, date) or isinstance(day, datetime):
            raise DatasetQualityError("Quality partition day must be a date")
        canonical = rules.CANONICAL_TABLES[self._data_type].table
        manifest_bindings = {
            table: sid
            for table in _CANONICAL_V3_INPUTS[self._data_type]
            if (sid := self._pit.snapshot_bindings.get(table)) is not None
        }
        start = datetime.combine(day, time(), tzinfo=UTC)
        end = start + timedelta(days=1)
        manifest_id = derive_quality_report_id(
            quality_rule_id=CANONICAL_PARTITION_V3_RULE_ID,
            quality_rule_version=CANONICAL_PARTITION_V3_RULE_VERSION,
            quality_rule_hash=CANONICAL_PARTITION_V3_RULE_HASH,
            identity_rule_hashes=self._params.identity_registry.canonical_v3_hashes,
            max_identity_rule_hashes=self._params.identity_registry.max_identity_rule_hashes,
            subject_table=canonical,
            subject_snapshot_id=self._pit.snapshot_bindings.get(canonical),
            subject_symbol=venue_symbol,
            subject_start=start,
            subject_end=end,
            snapshot_bindings=[
                {"table": table, "snapshot_id": sid}
                for table, sid in sorted(manifest_bindings.items())
            ],
            max_identity_bytes=self._params.identity_registry.max_identity_bytes,
        )
        manifest = self._read_one(
            DATA_QUALITY_REPORT_MANIFESTS.table, manifest_id, self._manifest_columns()
        )
        return self._select_report(
            manifest_id,
            manifest,
            identity=(
                CANONICAL_PARTITION_V3_RULE_ID,
                CANONICAL_PARTITION_V3_RULE_VERSION,
                CANONICAL_PARTITION_V3_RULE_HASH,
                canonical,
                self._pit.snapshot_bindings.get(canonical),
                venue_symbol,
                start,
                end,
                manifest_bindings,
            ),
        )

    @staticmethod
    def _manifest_columns() -> tuple[str, ...]:
        return (
            "report_id",
            "quality_rule_id",
            "quality_rule_version",
            "quality_rule_hash",
            "subject_table",
            "subject_snapshot_id",
            "subject_symbol",
            "subject_start",
            "subject_end",
            "knowledge_time",
            "snapshot_bindings",
            "events",
            "event_revisions",
            "evidence_gaps",
        )

    def _read_one(
        self, table: str, report_id: str, columns: tuple[str, ...]
    ) -> Mapping[str, Any] | None:
        rows = self._view.scan_columns(
            table,
            columns=columns,
            row_filter=EqualTo("report_id", report_id),  # type: ignore[call-arg, arg-type]
            limit=2,
        ).to_pylist()
        if len(rows) > 1:
            raise DatasetQualityError(f"Quality report {report_id} is committed more than once")
        return None if not rows else rows[0]

    def _select_report(
        self,
        manifest_id: str,
        manifest: Mapping[str, Any] | None,
        *,
        identity: tuple[Any, ...],
    ) -> str:
        if manifest is None:
            raise DatasetQualityError("no bounded v3 Quality manifest matches the Dataset subject")
        ident = identity
        self._validate_row(manifest, manifest_id, ident[:8])
        expected = ident[8]
        _check_manifest(
            manifest,
            report_id=manifest_id,
            rule_id=ident[0],
            rule_version=ident[1],
            rule_hash=ident[2],
            subject_table=ident[3],
            subject_snapshot=ident[4],
            expected_bindings=expected,
            pit=self._pit,
        )
        self._validate_manifest_streams(manifest, manifest_id)
        self._activate(manifest_id, manifest)
        return manifest_id

    @staticmethod
    def _validate_row(row: Mapping[str, Any], report_id: str, identity: tuple[Any, ...]) -> None:
        rule_id, version, rule_hash, subject_table, subject_snapshot, symbol, start, end = identity
        if (
            row.get("report_id"),
            row.get("quality_rule_id"),
            row.get("quality_rule_version"),
            row.get("quality_rule_hash"),
            row.get("subject_table"),
            row.get("subject_snapshot_id"),
            row.get("subject_symbol"),
            row.get("subject_start"),
            row.get("subject_end"),
        ) != (
            report_id,
            rule_id,
            version,
            rule_hash,
            subject_table,
            subject_snapshot,
            symbol,
            start,
            end,
        ):
            raise DatasetQualityError(
                "committed Quality report identity/subject differs from Dataset"
            )

    def _validate_manifest_streams(self, manifest: Mapping[str, Any], report_id: str) -> None:
        for name in ("events", "event_revisions", "evidence_gaps"):
            ref = _manifest_ref(name, manifest[name])
            with iter_quality_report_stream(self._storage, ref, limits=self._stream_limits) as rows:
                for row in rows:
                    if not isinstance(row, Mapping):
                        raise CatalogIntegrityError(f"Quality {name} stream contains a non-object")
                    if name == "evidence_gaps" and row.get("quality_report_id") != report_id:
                        raise CatalogIntegrityError(
                            "Quality evidence-gap row has invalid report identity"
                        )

    def recorded_gap(self, report_id: str, table: str, revision_id: str) -> str | None:
        raise DatasetQualityError("bounded Quality gaps require the external ordered-join path")

    def claim_gap(self, report_id: str, table: str, revision_id: str, gap: str) -> None:
        if report_id != self._active_report or self._active_manifest is None:
            raise DatasetQualityError("gap claim does not name the active verified report")
        if not all(isinstance(value, str) and value for value in (table, revision_id, gap)):
            raise DatasetQualityError("Dataset Quality gap claim is malformed")
        if self._claims is None:
            self._claims = RunSetBuilder(
                self._scratch,
                key=lambda row: (row["table"], row["revision_id"]),
                capacity=self._capacity,
                merge_fanout=self._fanout,
                limits=self._run_limits,
            )
        self._claims.add({"table": table, "revision_id": revision_id, "gap": gap})

    def finish_reports(self) -> None:
        self._finish_active()

    def close(self) -> None:
        # Error/early-exit cleanup is abortive: never publish an unverified claim prefix.
        if self._claims is not None:
            self._claims.close()
            self._claims = None
        self._active_report = None
        self._active_manifest = None

    def _activate(self, report_id: str, manifest: Mapping[str, Any]) -> None:
        if self._active_report is not None and self._active_report != report_id:
            self._finish_active()
        self._active_report, self._active_manifest = report_id, manifest

    def _finish_active(self) -> None:
        report_id, manifest = self._active_report, self._active_manifest
        if report_id is None or manifest is None:
            return
        builder, self._claims = self._claims, None
        claims: RunRef | None = None
        try:
            if builder is not None:
                claims = builder.finish()
            actual_ref = _manifest_ref("evidence_gaps", manifest["evidence_gaps"])
            actual_cm = cast(
                Any,
                iter_quality_report_stream(self._storage, actual_ref, limits=self._stream_limits),
            )
            with actual_cm as actual_rows:
                claim_cm = cast(
                    Any,
                    (
                        iter_run(self._scratch, claims, max_object_bytes=self._max_run_object_bytes)
                        if claims is not None
                        else nullcontext(iter(()))
                    ),
                )
                with claim_cm as claim_rows:
                    self._compare_gaps(report_id, actual_rows, claim_rows)
        finally:
            if builder is not None:
                builder.close()
        self._active_report = None
        self._active_manifest = None

    def _compare_gaps(
        self,
        report_id: str,
        actual: Iterator[Mapping[str, Any]],
        claims: Iterator[Mapping[str, Any]],
    ) -> None:
        previous: tuple[str, str] | None = None
        actual_row = next(actual, None)
        claim_row = next(claims, None)
        while claim_row is not None:
            while actual_row is not None:
                actual_key = self._validate_gap(actual_row, report_id, previous)
                previous = actual_key
                claim_key = (claim_row.get("table"), claim_row.get("revision_id"))
                if actual_key >= claim_key:
                    break
                actual_row = next(actual, None)
            if actual_row is None:
                raise DatasetQualityError(f"Quality report {report_id} omits a Dataset gap")
            claim_key = (claim_row.get("table"), claim_row.get("revision_id"))
            if claim_key != actual_key or claim_row.get("gap") != actual_row.get("gap"):
                raise DatasetQualityError(f"Quality report {report_id} gap differs from Dataset")
            actual_row = next(actual, None)
            claim_row = next(claims, None)
        while actual_row is not None:
            previous = self._validate_gap(actual_row, report_id, previous)
            actual_row = next(actual, None)

    @staticmethod
    def _validate_gap(
        row: Mapping[str, Any], report_id: str, previous: tuple[str, str] | None
    ) -> tuple[str, str]:
        if not isinstance(row, Mapping) or row.get("quality_report_id") != report_id:
            raise CatalogIntegrityError("Quality evidence-gap row has invalid report identity")
        table, revision, gap = row.get("table"), row.get("revision_id"), row.get("gap")
        if not all(isinstance(value, str) and value for value in (table, revision, gap)):
            raise CatalogIntegrityError("Quality evidence-gap row is malformed")
        key = (table, revision)
        if previous is not None and key <= previous:
            raise CatalogIntegrityError("Quality evidence-gap stream is duplicate or unordered")
        return cast(str, key[0]), cast(str, key[1])

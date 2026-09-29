"""Idempotent persistence of ``ResearchDatasetManifest`` rows (Phase 1 F3; ADR-0023 §6).

``research.dataset_manifests`` (frozen, ``phase1_tables.py``) holds one row per manifest: the
queryable identity columns plus ``manifest_json``, the contract canonical JSON whose SHA-256 is
``manifest_content_hash`` = ``ResearchDatasetManifest.content_hash()``. The content hash is the
identity: persisting the same manifest again finds its row, proves it equal and commits nothing;
a different row under that hash, or two rows, is ``CatalogIntegrityError``. ``load`` re-proves a
persisted row (hash of the JSON, the contract re-validated, every column re-derived).

A hash-consistent, contract-valid manifest can still be false: the genuine one minus an exclusion
(survivorship) binds the same genuine dataset snapshot (G2 finding RT-4). So the store never
persists or returns a manifest its ``ManifestVerifier`` has not proven to be exactly what a build
of its own inputs produces — ``DatasetBuilder`` is that verifier: the dataset snapshot must commit
the batch ``selection_id_for`` those inputs, and ``DatasetBuilder.select`` at the bound snapshots
(read-only, deterministic) must re-derive every member, exclusion, lineage entry, report id, gap
and the dataset rows themselves.

**v3 (ADR-0077 §7 / §8.2).** ``research.dataset_evidence_manifests`` holds the fixed-size
``ResearchDatasetEvidenceManifest`` rows; ``DatasetEvidenceManifestStore`` is its one writer (the
``EvidenceManifestStore`` of ``DatasetEvidenceBuilder.build``) and is verified by an
``EvidenceManifestVerifier`` (the streaming verifier, ``infrastructure.dataset.verify_v3``) on
persist and load, exactly as the v2 store is. A content hash names at most one form: a hash in
both manifest tables is ``CatalogIntegrityError``. ``ManifestStore.load`` keeps its v2 type and
path unchanged and refuses a v3-only hash with ``ManifestFormError``; ``ManifestStore.load_any``
dispatches to whichever table holds the hash. The v2 path is materializing (its verification
grows with N) and is not part of the ADR-0077 bounded claim (§8.3).

**Assumptions (ADR-0051 §3).** A manifest binds the listing backfill assumption, like ADR-0032's
archive event-time assumption, only through its PIT spec's ``availability_bindings`` (id, version
and hash, so the content hash covers it); ``manifest_assumptions`` is the read-only view a report
groups results by.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

import pyarrow as pa  # type: ignore[import-untyped]
from pydantic import ValidationError
from pyiceberg.expressions import EqualTo

from core.contracts.catalog import BatchConflict, CommitConflict, CommitRequest, TableNotFound
from core.contracts.revision import PolicyBinding
from core.contracts.universe import ResearchDatasetEvidenceManifest, ResearchDatasetManifest
from core.domain.base import canonical_json
from infrastructure import contract_version
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import DATASET_EVIDENCE_MANIFESTS, DATASET_MANIFESTS
from infrastructure.pit import assumption as archive_assumption
from infrastructure.revision.store import BatchCommit, RevisionCatalog
from infrastructure.universe import listing_assumption

__all__ = [
    "DatasetEvidenceManifestStore",
    "EvidenceManifestVerifier",
    "ManifestAssumptions",
    "ManifestFormError",
    "ManifestPersisted",
    "ManifestStore",
    "ManifestVerifier",
    "evidence_manifest_batch_id",
    "evidence_manifest_row",
    "manifest_assumptions",
    "manifest_batch_id",
    "manifest_row",
]

_TABLE: Final = DATASET_MANIFESTS.table
_EVIDENCE_TABLE: Final = DATASET_EVIDENCE_MANIFESTS.table
_ATTEMPTS: Final = 8


def manifest_batch_id(content_hash: str) -> str:
    return f"manifest.{content_hash}"


def manifest_row(manifest: ResearchDatasetManifest) -> dict[str, Any]:
    """The frozen row of one manifest, normalised through the table's Arrow schema."""
    pit = manifest.point_in_time
    document = canonical_json(manifest.model_dump(mode="json"))
    content_hash = manifest.content_hash()
    if hashlib.sha256(document.encode("utf-8")).hexdigest() != content_hash:
        raise CatalogIntegrityError("the manifest JSON does not hash to its content hash")
    row = {
        "manifest_content_hash": content_hash,
        "contract_schema_version": manifest.schema_version,
        "dataset_zone": manifest.dataset.zone.value,
        "dataset_table": manifest.dataset.table,
        "dataset_snapshot_id": manifest.dataset.snapshot_id,
        "dataset_time_range_start": manifest.dataset.time_range_start,
        "dataset_time_range_end": manifest.dataset.time_range_end,
        "point_in_time_name": pit.name,
        "point_in_time_version": pit.version,
        "point_in_time_hash": pit.content_hash(),
        "simulation_time": pit.simulation_time,
        "simulation_start": pit.simulation_start,
        "simulation_end": pit.simulation_end,
        "knowledge_cutoff": pit.knowledge_cutoff,
        "universe_spec_name": manifest.universe_spec.name,
        "universe_spec_version": manifest.universe_spec.version,
        "universe_spec_hash": manifest.universe_spec.spec_hash,
        "snapshot_bindings": [
            {"table": table, "snapshot_id": snapshot}
            for table, snapshot in sorted(pit.snapshot_bindings.items())
        ],
        "quality_report_ids": list(manifest.quality_report_ids),
        "manifest_json": document,
    }
    normalised: list[dict[str, Any]] = pa.Table.from_pylist(
        [row], schema=DATASET_MANIFESTS.arrow_schema
    ).to_pylist()
    return normalised[0]


@dataclass(frozen=True, slots=True)
class ManifestAssumptions:
    """The stated assumption policies a manifest binds through its PIT spec (report grouping).

    ADR-0051 §3: results must be groupable by whether the listing backfill assumption is bound,
    together with ADR-0032's archive event-time assumption. Each field is the bound policy
    (exact id, version and hash) or ``None``; ``key`` is the grouping key.
    """

    archive_event_time: PolicyBinding | None
    listing_backfill: PolicyBinding | None

    @property
    def key(self) -> tuple[bool, bool]:
        """``(archive event-time assumed, listing backfill assumed)``."""
        return (self.archive_event_time is not None, self.listing_backfill is not None)


def manifest_assumptions(
    manifest: ResearchDatasetManifest | ResearchDatasetEvidenceManifest,
) -> ManifestAssumptions:
    """Which assumption policies ``manifest`` binds (ADR-0032, ADR-0051); nothing is read.

    A PIT spec naming either policy with another version or hash is refused (fail closed), as a
    build of it would be.
    """
    if not isinstance(manifest, ResearchDatasetManifest | ResearchDatasetEvidenceManifest):
        raise TypeError("manifest must be a ResearchDatasetManifest or its v3 evidence form")
    pit = manifest.point_in_time
    try:
        archive = archive_assumption.assumption_bound(pit)
        listing = listing_assumption.assumption_bound(pit)
    except (
        archive_assumption.AssumptionSpecError,
        listing_assumption.AssumptionSpecError,
    ) as exc:
        raise CatalogIntegrityError(
            f"manifest {manifest.content_hash()} binds an assumption policy wrongly: {exc}"
        ) from exc
    return ManifestAssumptions(
        archive_event_time=archive_assumption.ASSUMPTION_BINDING if archive else None,
        listing_backfill=listing_assumption.ASSUMPTION_BINDING if listing else None,
    )


@dataclass(frozen=True, slots=True)
class ManifestPersisted:
    content_hash: str
    row: Mapping[str, Any]
    #: ``None`` when the row was already persisted (a replay: nothing committed).
    commit: BatchCommit | None

    @property
    def replayed(self) -> bool:
        return self.commit is None


class ManifestVerifier(Protocol):
    """Proves a manifest is what a build of its own inputs produces, or raises (fail closed)."""

    def verify_manifest(self, manifest: ResearchDatasetManifest) -> None: ...


class ManifestFormError(Exception):
    """The content hash names a persisted manifest of the other form (v2 / v3, ADR-0077 §8.2).

    Not an integrity failure: the typed entry point asked for cannot return that form (a v2
    consumer cannot consume a v3 evidence manifest, and vice versa), so it refuses instead of
    answering "absent". ``ManifestStore.load_any`` dispatches by form.
    """


class EvidenceManifestVerifier(Protocol):
    """Proves a v3 manifest is what a bounded re-derivation of its own inputs produces (§6).

    ``manifested``: whether a manifest row with this content hash is already persisted (a load,
    or the persist of a replay); it exempts the replay from the unbound-table refusal as in v2.
    """

    def verify_evidence_manifest(
        self, manifest: ResearchDatasetEvidenceManifest, *, manifested: bool
    ) -> None: ...


class ManifestStore:
    """The one writer of ``research.dataset_manifests``; content-hash idempotent.

    Every manifest is proven by ``verifier`` before it is persisted (a replay included) and after
    it is loaded; nothing is committed for a manifest that does not verify.

    ADR-0077 §8.2: ``load`` looks the hash up in both manifest tables (v2 path unchanged);
    ``load_any`` returns the manifest of whichever form holds it, verifying a v3 one with
    ``evidence_verifier`` (none given: a v3 hash is refused, never returned unverified).
    """

    def __init__(
        self,
        adapter: RevisionCatalog,
        verifier: ManifestVerifier,
        *,
        evidence_verifier: EvidenceManifestVerifier | None = None,
    ) -> None:
        self._adapter = adapter
        self._verifier = verifier
        self._evidence_verifier = evidence_verifier

    def persist(self, manifest: ResearchDatasetManifest) -> ManifestPersisted:
        expected = manifest_row(manifest)
        self._verifier.verify_manifest(manifest)
        content_hash = expected["manifest_content_hash"]
        last: Exception | None = None
        for _ in range(_ATTEMPTS):
            existing = self._row(content_hash)
            if existing is not None:
                if existing != expected:
                    raise CatalogIntegrityError(
                        f"manifest {content_hash} is persisted with other content"
                    )
                return ManifestPersisted(content_hash, existing, None)
            table = pa.Table.from_pylist([expected], schema=DATASET_MANIFESTS.arrow_schema)
            request = CommitRequest(
                table=_TABLE,
                batch_id=manifest_batch_id(content_hash),
                batch_fingerprint=DATASET_MANIFESTS.fingerprint_rule.fingerprint(table),
                row_count=1,
                expected_parent_snapshot_id=self._head(),
            )
            try:
                result = self._adapter.commit_batch(request, table)
            except CommitConflict as exc:
                last = exc  # another manifest landed first: re-read, then retry
                continue
            except BatchConflict as exc:
                raise CatalogIntegrityError(
                    f"manifest batch {request.batch_id} is committed with other content"
                ) from exc
            if self._row(content_hash) != expected:
                raise CatalogIntegrityError(f"manifest {content_hash} reads back differently")
            return ManifestPersisted(
                content_hash,
                expected,
                BatchCommit(
                    table=_TABLE,
                    batch_id=request.batch_id,
                    snapshot_id=result.snapshot.snapshot_id,
                    outcome=result.outcome,
                    row_count=1,
                ),
            )
        raise CatalogIntegrityError(f"manifest {content_hash} lost {_ATTEMPTS} races") from last

    def load(self, content_hash: str) -> ResearchDatasetManifest | None:
        """The persisted manifest, re-proven and re-derived; ``None`` if no row has this hash.

        ADR-0077 §8.2: a hash also persisted in the v3 table is ``CatalogIntegrityError``; a hash
        persisted only there is ``ManifestFormError`` (use ``load_any``). Otherwise the v2 path,
        unchanged: JSON hash, contract re-validation, ``manifest_row`` equality, verifier.
        """
        row = self._row(content_hash)
        _check_one_form(self._adapter, content_hash, found=row is not None, v3=False)
        if row is None:
            return None
        document: str = row["manifest_json"]
        if hashlib.sha256(document.encode("utf-8")).hexdigest() != content_hash:
            raise CatalogIntegrityError(f"manifest {content_hash}: JSON does not hash to its id")
        manifest = ResearchDatasetManifest.model_validate_json(document)
        if manifest_row(manifest) != row:
            raise CatalogIntegrityError(f"manifest {content_hash}: columns disagree with its JSON")
        self._verifier.verify_manifest(manifest)
        return manifest

    def load_any(
        self, content_hash: str
    ) -> ResearchDatasetManifest | ResearchDatasetEvidenceManifest | None:
        """The persisted manifest of either form (ADR-0077 §8.2), re-proven by its verifier.

        Only in the v2 table: ``load`` (the v2 path). Only in the v3 table: the v3 path
        (``DatasetEvidenceManifestStore.load`` with ``evidence_verifier``). In both:
        ``CatalogIntegrityError``. In neither: ``None``.
        """
        if not _present(self._adapter, _EVIDENCE_TABLE, content_hash):
            return self.load(content_hash)
        if self._row(content_hash) is not None:
            raise CatalogIntegrityError(
                f"manifest {content_hash} is persisted in both {_TABLE} and {_EVIDENCE_TABLE}"
            )
        if self._evidence_verifier is None:
            raise ManifestFormError(
                f"manifest {content_hash} is a v3 evidence manifest and this store has no "
                "evidence verifier: it is never returned unverified"
            )
        return DatasetEvidenceManifestStore(self._adapter, self._evidence_verifier).load(
            content_hash
        )

    def _row(self, content_hash: str) -> Mapping[str, Any] | None:
        columns = tuple(field.name for field in DATASET_MANIFESTS.arrow_schema)
        rows = self._adapter.scan_columns(
            _TABLE,
            columns=columns,
            row_filter=EqualTo("manifest_content_hash", content_hash),  # type: ignore[call-arg, arg-type]
        ).to_pylist()
        if len(rows) > 1:
            raise CatalogIntegrityError(f"manifest {content_hash} is persisted twice")
        return rows[0] if rows else None

    def _head(self) -> str | None:
        info = self._adapter.load_table(_TABLE)
        if info is None:
            raise TableNotFound(f"table {_TABLE} does not exist; create the Phase 1 tables first")
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id


# =========================================================================================
# v3: ``research.dataset_evidence_manifests`` (ADR-0077 §7 / §8.2)
# =========================================================================================


def _present(adapter: RevisionCatalog, table: str, content_hash: str) -> bool:
    """Whether ``table`` has a row under ``content_hash`` (at its head).

    A catalog without ``table`` (created before ADR-0077 added the v3 table) has no such row:
    no manifest of that form can exist there.
    """
    try:
        found = adapter.scan_columns(
            table,
            columns=("manifest_content_hash",),
            row_filter=EqualTo("manifest_content_hash", content_hash),  # type: ignore[call-arg, arg-type]
        )
    except TableNotFound:
        return False
    return bool(found.num_rows)


def _check_one_form(adapter: RevisionCatalog, content_hash: str, *, found: bool, v3: bool) -> None:
    """A hash names one form (§8.2): in both tables is an integrity error; only in the other
    table is ``ManifestFormError`` for the typed entry point asking for this form."""
    other = _TABLE if v3 else _EVIDENCE_TABLE
    if not _present(adapter, other, content_hash):
        return
    if found:
        raise CatalogIntegrityError(
            f"manifest {content_hash} is persisted in both {_TABLE} and {_EVIDENCE_TABLE}"
        )
    raise ManifestFormError(
        f"manifest {content_hash} is persisted in {other}, not as a "
        f"{'v3 evidence' if v3 else 'v2'} manifest"
    )


def evidence_manifest_batch_id(content_hash: str) -> str:
    return f"evidence-manifest.{content_hash}"


def evidence_manifest_row(manifest: ResearchDatasetEvidenceManifest) -> dict[str, Any]:
    """The frozen row of one v3 manifest, normalised through the table's Arrow schema.

    Every column is fixed-size (§7): the ``evidence`` list has six stream groups for recorded
    2.3.0 / 2.4.0 manifests and seven for 2.5.0+, in canonical stream-name order. The row schema's
    list element shape remains unchanged, so legacy flattened rows retain their exact values.
    """
    if not isinstance(manifest, ResearchDatasetEvidenceManifest):
        raise TypeError("manifest must be a ResearchDatasetEvidenceManifest")
    pit = manifest.point_in_time
    document = canonical_json(manifest.model_dump(mode="json"))
    content_hash = manifest.content_hash()
    if hashlib.sha256(document.encode("utf-8")).hexdigest() != content_hash:
        raise CatalogIntegrityError("the evidence manifest JSON does not hash to its content hash")
    row = {
        "manifest_content_hash": content_hash,
        "contract_schema_version": manifest.schema_version,
        "dataset_zone": manifest.dataset.zone.value,
        "dataset_table": manifest.dataset.table,
        "dataset_snapshot_id": manifest.dataset.snapshot_id,
        "dataset_time_range_start": manifest.dataset.time_range_start,
        "dataset_time_range_end": manifest.dataset.time_range_end,
        "point_in_time_name": pit.name,
        "point_in_time_version": pit.version,
        "point_in_time_hash": pit.content_hash(),
        "simulation_time": pit.simulation_time,
        "simulation_start": pit.simulation_start,
        "simulation_end": pit.simulation_end,
        "knowledge_cutoff": pit.knowledge_cutoff,
        "universe_spec_name": manifest.universe_spec.name,
        "universe_spec_version": manifest.universe_spec.version,
        "universe_spec_hash": manifest.universe_spec.spec_hash,
        "snapshot_bindings": [
            {"table": table, "snapshot_id": snapshot}
            for table, snapshot in sorted(pit.snapshot_bindings.items())
        ],
        "rule_id": manifest.rule.rule_id,
        "rule_version": manifest.rule.version,
        "rule_hash": manifest.rule.rule_hash,
        "data_type": manifest.data_type,
        "selection_id": manifest.selection_id,
        "row_count": manifest.row_count,
        "chunk_rows": manifest.chunk_rows,
        "chunk_count": manifest.chunk_count,
        "evidence": [
            {
                "stream": ref.stream.value,
                "record_count": ref.record_count,
                "leaf_count": ref.leaf_count,
                "depth": ref.depth,
                "root_key": ref.root.key,
                "root_sha256": ref.root.sha256,
                "root_size": ref.root.size,
            }
            for ref in manifest.evidence
        ],
        "manifest_json": document,
    }
    normalised: list[dict[str, Any]] = pa.Table.from_pylist(
        [row], schema=DATASET_EVIDENCE_MANIFESTS.arrow_schema
    ).to_pylist()
    return normalised[0]


class DatasetEvidenceManifestStore:
    """The one writer of ``research.dataset_evidence_manifests``; content-hash idempotent.

    Implements ``infrastructure.dataset.builder.EvidenceManifestStore`` (B4). As the v2 store:
    every manifest is proven by ``verifier`` (the streaming verifier) before it is persisted, a
    replay included, and after it is loaded; nothing is committed for a manifest that does not
    verify; a different row under the same hash, or two rows, is ``CatalogIntegrityError``. In
    addition a ``selection_id`` has at most one manifest (same inputs, same manifest: a second
    one differing in anything, its envelope included, is an integrity error, ADR-0052 V7), and a
    hash persisted in the v2 table is never persisted here (§8.2).
    """

    def __init__(self, adapter: RevisionCatalog, verifier: EvidenceManifestVerifier) -> None:
        self._adapter = adapter
        self._verifier = verifier

    def recorded_version(self, selection_id: str) -> str | None:
        """The contract version of the persisted manifest of ``selection_id``, else ``None``."""
        rows = self._adapter.scan_columns(
            _EVIDENCE_TABLE,
            columns=("manifest_content_hash", "contract_schema_version"),
            row_filter=EqualTo("selection_id", selection_id),  # type: ignore[call-arg, arg-type]
        ).to_pylist()
        if not rows:
            return None
        if len(rows) > 1:
            raise CatalogIntegrityError(
                f"selection {selection_id} has {len(rows)} persisted evidence manifests"
            )
        return contract_version.replay_version(
            rows[0]["contract_schema_version"],
            what=f"the evidence manifest of selection {selection_id}",
        )

    def persist(self, manifest: ResearchDatasetEvidenceManifest) -> bool:
        """Verify and persist ``manifest``; ``True`` when it was already persisted (a replay)."""
        expected = evidence_manifest_row(manifest)
        content_hash: str = expected["manifest_content_hash"]
        if _present(self._adapter, _TABLE, content_hash):
            raise CatalogIntegrityError(
                f"manifest {content_hash} is already persisted in {_TABLE} (ADR-0077 §8.2)"
            )
        self._check_selection(manifest.selection_id, content_hash)
        self._verifier.verify_evidence_manifest(
            manifest, manifested=self._row(content_hash) is not None
        )
        last: Exception | None = None
        for _ in range(_ATTEMPTS):
            existing = self._row(content_hash)
            if existing is not None:
                if existing != expected:
                    raise CatalogIntegrityError(
                        f"evidence manifest {content_hash} is persisted with other content"
                    )
                return True
            table = pa.Table.from_pylist([expected], schema=DATASET_EVIDENCE_MANIFESTS.arrow_schema)
            request = CommitRequest(
                table=_EVIDENCE_TABLE,
                batch_id=evidence_manifest_batch_id(content_hash),
                batch_fingerprint=DATASET_EVIDENCE_MANIFESTS.fingerprint_rule.fingerprint(table),
                row_count=1,
                expected_parent_snapshot_id=self._head(),
            )
            try:
                self._adapter.commit_batch(request, table)
            except CommitConflict as exc:
                last = exc  # another manifest landed first: re-read, then retry
                continue
            except BatchConflict as exc:
                raise CatalogIntegrityError(
                    f"evidence manifest batch {request.batch_id} is committed with other content"
                ) from exc
            if self._row(content_hash) != expected:
                raise CatalogIntegrityError(
                    f"evidence manifest {content_hash} reads back differently"
                )
            # A racing writer may have landed another manifest of this selection meanwhile.
            self._check_selection(manifest.selection_id, content_hash)
            return False
        raise CatalogIntegrityError(
            f"evidence manifest {content_hash} lost {_ATTEMPTS} races"
        ) from last

    def load(self, content_hash: str) -> ResearchDatasetEvidenceManifest | None:
        """The persisted v3 manifest, re-proven and re-derived; ``None`` if no row has this hash.

        A hash also in the v2 table is ``CatalogIntegrityError``; only there,
        ``ManifestFormError``.
        """
        row = self._row(content_hash)
        _check_one_form(self._adapter, content_hash, found=row is not None, v3=True)
        if row is None:
            return None
        document: str = row["manifest_json"]
        if hashlib.sha256(document.encode("utf-8")).hexdigest() != content_hash:
            raise CatalogIntegrityError(
                f"evidence manifest {content_hash}: JSON does not hash to its id"
            )
        try:
            manifest = ResearchDatasetEvidenceManifest.model_validate_json(document)
        except ValidationError as exc:
            raise CatalogIntegrityError(
                f"evidence manifest {content_hash}: JSON is not a valid manifest"
            ) from exc
        if evidence_manifest_row(manifest) != row:
            raise CatalogIntegrityError(
                f"evidence manifest {content_hash}: columns disagree with its JSON"
            )
        self._verifier.verify_evidence_manifest(manifest, manifested=True)
        return manifest

    # ------------------------------------------------------------------ internals

    def _check_selection(self, selection_id: str, content_hash: str) -> None:
        found = self._adapter.scan_columns(
            _EVIDENCE_TABLE,
            columns=("manifest_content_hash",),
            row_filter=EqualTo("selection_id", selection_id),  # type: ignore[call-arg, arg-type]
        ).column("manifest_content_hash")
        others = sorted({value for value in found.to_pylist() if value != content_hash})
        if others:
            raise CatalogIntegrityError(
                f"selection {selection_id} already has another evidence manifest {others[0]}"
            )

    def _row(self, content_hash: str) -> Mapping[str, Any] | None:
        columns = tuple(field.name for field in DATASET_EVIDENCE_MANIFESTS.arrow_schema)
        rows = self._adapter.scan_columns(
            _EVIDENCE_TABLE,
            columns=columns,
            row_filter=EqualTo("manifest_content_hash", content_hash),  # type: ignore[call-arg, arg-type]
        ).to_pylist()
        if len(rows) > 1:
            raise CatalogIntegrityError(f"evidence manifest {content_hash} is persisted twice")
        return rows[0] if rows else None

    def _head(self) -> str | None:
        info = self._adapter.load_table(_EVIDENCE_TABLE)
        if info is None:
            raise TableNotFound(
                f"table {_EVIDENCE_TABLE} does not exist; create the Phase 1 tables first"
            )
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id

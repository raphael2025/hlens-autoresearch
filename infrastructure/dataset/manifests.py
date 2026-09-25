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
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.expressions import EqualTo

from core.contracts.catalog import BatchConflict, CommitConflict, CommitRequest, TableNotFound
from core.contracts.universe import ResearchDatasetManifest
from core.domain.base import canonical_json
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import DATASET_MANIFESTS
from infrastructure.revision.store import BatchCommit, RevisionCatalog

__all__ = [
    "ManifestPersisted",
    "ManifestStore",
    "ManifestVerifier",
    "manifest_batch_id",
    "manifest_row",
]

_TABLE: Final = DATASET_MANIFESTS.table
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


class ManifestStore:
    """The one writer of ``research.dataset_manifests``; content-hash idempotent.

    Every manifest is proven by ``verifier`` before it is persisted (a replay included) and after
    it is loaded; nothing is committed for a manifest that does not verify.
    """

    def __init__(self, adapter: RevisionCatalog, verifier: ManifestVerifier) -> None:
        self._adapter = adapter
        self._verifier = verifier

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
        """The persisted manifest, re-proven and re-derived; ``None`` if no row has this hash."""
        row = self._row(content_hash)
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

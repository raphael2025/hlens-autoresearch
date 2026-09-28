"""Opt-in, process-local cache of verified Research Dataset manifest loads (debugging pass, perf).

``load_manifest(builder, hash)`` (F4 / G2 RT-6) loads a manifest through ``builder``'s own
``ManifestStore``, whose verifier (``DatasetBuilder.verify_manifest``, G2 RT-4) re-derives the
whole build: seconds per load, paid again by every consumer of the same manifest in one round.
``VerifiedManifestCache`` reuses a **successful** proof, and only while nothing it depended on can
have changed. A caller opts in by passing one explicitly; ``load_verified_manifest(builder, hash,
None)`` is exactly ``load_manifest`` (the default everywhere, byte-identical behaviour).

What a proof depends on, and how the cache pins each input:

1. **the manifest** — its content hash. The hash binds the dataset's own snapshot
   (``DatasetRef.snapshot_id``) and every upstream snapshot the verifier reads pinned
   (``point_in_time.snapshot_bindings``: ``PinnedCatalogView`` time-travels there, and explicit
   historical snapshot ids are immutable), so one hash is one set of pinned inputs;
2. **the verifier** — the ``DatasetBuilder`` object itself (its catalog, storage, market-data
   origin and dataset table are fixed at construction), its class and ``DATASET_RULE_HASH``.
   A proof is never served to another builder object, even one over the same catalog;
3. **the catalog** — the builder's own ``RevisionCatalog`` object (identity; the cache holds it, so
   an identity is never reused while an entry lives). Another catalog, or the same catalog
   reopened (a new adapter), is a miss;
4. **the heads the verifier reads unpinned** — ``verification_head_tables``: the manifests table
   (the store's row read and the replay check's manifest scan), the manifest's own dataset table
   (the replay check's batch history) and the two bound-if-present tables (ADR-0027 §13 evidence,
   ADR-0031 evidence gaps; ``_check_unbound``). An Iceberg snapshot id names one immutable table
   state, so equal heads mean identical reads. The test
   ``test_the_verifier_reads_no_other_table_at_its_head`` runs the real verifier over a recording
   catalog and fails if it ever reads another table at its head (the list must then grow);
5. **Raw objects** — the ``StorageAdapter`` contract has no overwrite or delete, and ``open_read``
   re-checks every object's SHA-256: an object a proof read reads the same again.

**Store**: on a miss the heads are read before and after the full verified load; the proof is
stored only when they are equal (the verifier saw exactly them), the manifest hashes to the
requested hash and its dataset table is the one whose head was read. **Hit**: the same builder and
catalog objects, the same rule, and every recorded head unchanged; anything else is a miss that
runs the full verified load (a stale entry is replaced only by a new success). A failed load
(unknown hash, forged row, a verification that does not prove) is never stored, and a forged row
under a cached hash moves the manifests head, so it misses and the store refuses it again.

**Hit-path time of check / time of use** (review fixes 4, 2026-09-26): a hit compares the recorded
heads once and then returns the stored manifest; a head that advances concurrently **after** that
comparison is not observed by that hit (the next load sees it and misses). This does not make the
returned manifest wrong: its content is fixed by its hash, and every snapshot the proof read
through it is pinned (``snapshot_bindings``, the dataset's own ``snapshot_id``: immutable table
states), so the hit hands out exactly the manifest a full verified load would have proven at the
moment of the head comparison — the same answer a plain ``load_manifest`` gives when a writer
commits just after it returns. What such a late head move could change (a forged manifest row, a
new evidence or gap row) is judged by the next load, never retroactively by one already served.

**Limits**: process-local and unbounded (one entry per distinct builder x manifest; the caller
drops the cache to free it); the builder's catalog is read through its private ``_adapter``
attribute — a builder without it is never cached (plain verified loads, counted as
``uncacheable``); the unpinned-head list mirrors ``DatasetBuilder.verify_manifest`` from outside
Phase 1 (guarded by the test above; a public ``DatasetBuilder`` accessor for both is a follow-up
for Codex review); a catalog whose head is moved away and back to the same snapshot **during** one
verified load (a rollback, which no project writer performs) would not be noticed.

**v3 evidence manifests (ADR-0077 §8.2; C1-CONSUMERS).** ``load_verified_any(builder, hash, cache,
evidence_verifier)`` loads either form (``infrastructure.feature.dataset.load_any_manifest``);
``evidence_verifier`` None is exactly ``load_verified_manifest``. ``VerifiedManifestCache.load_any``
keeps the two forms apart:

- the unverified manifest rows choose the path: a hash with a row in
  ``research.dataset_manifests`` only is ``load`` above (the v2 entry, key and proof unchanged); a
  hash in neither table or in both is never cached (the full ``load_any`` runs and refuses);
- a hash with a row in ``research.dataset_evidence_manifests`` only is a v3 proof, keyed by the
  builder, the ``StreamingEvidenceVerifier`` object (its rule, chunk writer, sources and listing
  lookup are fixed at construction) and the content hash, and bound to the manifest's dataset
  snapshot (the chunk snapshot the hash binds: the persisted row's ``dataset_snapshot_id`` must be
  the verified manifest's for the proof to be stored). The streaming verifier reads unpinned only
  the two manifest tables (the store's row reads and the one-form check) and the chunk table
  (``DatasetChunkWriter.seal``); ``verification_head_tables(<chunk table>)`` covers them (and the
  two bound-if-present tables, read only by a first persist, never by a load: a superset, so at
  worst a needless miss). Everything else it reads is pinned (the spec's bound snapshots, the
  dataset snapshot, snapshot ids, content-addressed evidence objects). A hit needs the same
  builder, verifier and catalog objects (the verifier must prove manifests of the builder's own
  catalog) and every recorded head unchanged, as for v2;
- a v2 proof is never served for a v3 request or the reverse: the two live in separate maps under
  separate keys, and a hash names one form (a second row in the other table moves that table's
  head, so the stored proof misses, and the full load refuses the hash).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from threading import Lock
from typing import Final

from pyiceberg.expressions import EqualTo

from core.contracts.catalog import TableNotFound
from core.contracts.universe import ResearchDatasetEvidenceManifest, ResearchDatasetManifest
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    DATASET_EVIDENCE_MANIFESTS,
    DATASET_MANIFESTS,
    QUALITY_EVIDENCE_GAPS,
)
from infrastructure.dataset.builder import DATASET_RULE_HASH, DatasetBuilder
from infrastructure.dataset.verify_v3 import StreamingEvidenceVerifier
from infrastructure.feature.dataset import (
    AnyDatasetManifest,
    DatasetBindingError,
    evidence_catalog,
    load_any_manifest,
    load_manifest,
)
from infrastructure.revision.store import RevisionCatalog

__all__ = [
    "HEAD_READ_TABLES",
    "ManifestCacheStats",
    "VerifiedManifestCache",
    "load_verified_any",
    "load_verified_manifest",
    "verification_head_tables",
]

#: Tables ``DatasetBuilder.verify_manifest`` / ``ManifestStore.load`` read at their current head
#: for every manifest (plus the manifest's own dataset table, see ``verification_head_tables``).
HEAD_READ_TABLES: Final = (
    DATASET_MANIFESTS.table,
    DATASET_EVIDENCE_MANIFESTS.table,
    BINANCE_SPOT_PRECEDENCE_EVIDENCE.table,
    QUALITY_EVIDENCE_GAPS.table,
)
_ABSENT: Final = "<no table>"
_EMPTY: Final = "<no snapshot>"


def verification_head_tables(dataset_table: str) -> tuple[str, ...]:
    """Every table a verified load of a manifest of ``dataset_table`` reads at its head."""
    return tuple(sorted({*HEAD_READ_TABLES, dataset_table}))


def load_verified_manifest(
    builder: DatasetBuilder,
    manifest_content_hash: str,
    cache: VerifiedManifestCache | None = None,
) -> ResearchDatasetManifest:
    """``load_manifest`` (``cache`` None: exactly it) or the cache's proven entry (module docs)."""
    if cache is None:
        return load_manifest(builder, manifest_content_hash)
    return cache.load(builder, manifest_content_hash)


def load_verified_any(
    builder: DatasetBuilder,
    manifest_content_hash: str,
    cache: VerifiedManifestCache | None = None,
    evidence_verifier: StreamingEvidenceVerifier | None = None,
) -> AnyDatasetManifest:
    """A verified manifest of either form (module docs, v3); ``evidence_verifier`` None is exactly
    ``load_verified_manifest``."""
    if evidence_verifier is None:
        return load_verified_manifest(builder, manifest_content_hash, cache)
    if cache is None:
        return load_any_manifest(builder, manifest_content_hash, evidence_verifier)
    return cache.load_any(builder, manifest_content_hash, evidence_verifier)


@dataclass(frozen=True, slots=True)
class ManifestCacheStats:
    hits: int
    misses: int
    stored: int
    uncacheable: int


@dataclass(frozen=True, slots=True)
class _Proof:
    builder: DatasetBuilder
    catalog: RevisionCatalog
    verifier: type[DatasetBuilder]
    rule_hash: str
    manifest: ResearchDatasetManifest
    heads: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _EvidenceProof:
    """A successful v3 verified load (module docs, v3)."""

    builder: DatasetBuilder
    verifier: StreamingEvidenceVerifier
    verifier_type: type[StreamingEvidenceVerifier]
    catalog: RevisionCatalog
    manifest: ResearchDatasetEvidenceManifest
    #: the chunk snapshot the manifest binds (``dataset.snapshot_id``)
    dataset_snapshot: str
    heads: tuple[tuple[str, str], ...]


class VerifiedManifestCache:
    """Successful verified manifest loads, reused only while every input is unchanged."""

    def __init__(self) -> None:
        self._proofs: dict[tuple[int, str], _Proof] = {}
        self._evidence_proofs: dict[tuple[int, int, str], _EvidenceProof] = {}
        self._lock = Lock()
        self._hits = self._misses = self._stored = self._uncacheable = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._proofs) + len(self._evidence_proofs)

    @property
    def stats(self) -> ManifestCacheStats:
        with self._lock:
            return ManifestCacheStats(self._hits, self._misses, self._stored, self._uncacheable)

    def load(self, builder: DatasetBuilder, manifest_content_hash: str) -> ResearchDatasetManifest:
        """The verified manifest: a proven entry when every input is unchanged, else a full
        verified load (stored only on success, module docs)."""
        catalog = _catalog_of(builder)
        if catalog is None or not isinstance(manifest_content_hash, str):
            with self._lock:
                self._uncacheable += 1
            return load_manifest(builder, manifest_content_hash)  # its own refusals, unchanged
        key = (id(builder), manifest_content_hash)
        with self._lock:
            proof = self._proofs.get(key)
        if proof is not None and self._valid(proof, builder, catalog):
            with self._lock:
                self._hits += 1
            return proof.manifest

        with self._lock:
            self._misses += 1
        dataset_table = _dataset_table_of(catalog, manifest_content_hash)
        tables = verification_head_tables(dataset_table) if dataset_table else ()
        before = _heads(catalog, tables)
        manifest = load_manifest(builder, manifest_content_hash)  # raises: nothing is stored
        if (
            dataset_table
            and manifest.content_hash() == manifest_content_hash
            and manifest.dataset.table == dataset_table
            and _heads(catalog, tables) == before
        ):
            with self._lock:
                self._proofs[key] = _Proof(
                    builder=builder,
                    catalog=catalog,
                    verifier=type(builder),
                    rule_hash=DATASET_RULE_HASH,
                    manifest=manifest,
                    heads=before,
                )
                self._stored += 1
        return manifest

    @staticmethod
    def _valid(proof: _Proof, builder: DatasetBuilder, catalog: RevisionCatalog) -> bool:
        return (
            proof.builder is builder
            and proof.catalog is catalog
            and proof.verifier is type(builder)
            and proof.rule_hash == DATASET_RULE_HASH
            and _heads(catalog, tuple(table for table, _ in proof.heads)) == proof.heads
        )

    # ------------------------------------------------------------------ either form (v3)

    def load_any(
        self,
        builder: DatasetBuilder,
        manifest_content_hash: str,
        evidence_verifier: StreamingEvidenceVerifier,
    ) -> AnyDatasetManifest:
        """The verified manifest of either form (module docs, v3): a v2 hash is ``load``; a v3
        one a proven entry when every input is unchanged, else a full verified load."""
        catalog = _catalog_of(builder)
        verifier_catalog = _verifier_catalog_of(evidence_verifier)
        if (
            catalog is None
            or verifier_catalog is not catalog
            or not isinstance(manifest_content_hash, str)
        ):
            with self._lock:
                self._uncacheable += 1
            return load_any_manifest(builder, manifest_content_hash, evidence_verifier)
        in_v2 = _dataset_table_of(catalog, manifest_content_hash) is not None
        persisted = _evidence_row_of(catalog, manifest_content_hash)
        if in_v2 and persisted is None:
            return self.load(builder, manifest_content_hash)  # the v2 entry, unchanged
        if persisted is None or in_v2:
            with self._lock:
                self._misses += 1
            # absent, in both tables or not one row: never cached (the full load refuses)
            return load_any_manifest(builder, manifest_content_hash, evidence_verifier)

        key = (id(builder), id(evidence_verifier), manifest_content_hash)
        with self._lock:
            proof = self._evidence_proofs.get(key)
        if proof is not None and self._valid_evidence(proof, builder, evidence_verifier, catalog):
            with self._lock:
                self._hits += 1
            return proof.manifest

        with self._lock:
            self._misses += 1
        dataset_table, dataset_snapshot = persisted
        tables = verification_head_tables(dataset_table)
        before = _heads(catalog, tables)
        manifest = load_any_manifest(builder, manifest_content_hash, evidence_verifier)
        if (
            isinstance(manifest, ResearchDatasetEvidenceManifest)
            and manifest.content_hash() == manifest_content_hash
            and manifest.dataset.table == dataset_table
            and manifest.dataset.snapshot_id == dataset_snapshot
            and _heads(catalog, tables) == before
        ):
            with self._lock:
                self._evidence_proofs[key] = _EvidenceProof(
                    builder=builder,
                    verifier=evidence_verifier,
                    verifier_type=type(evidence_verifier),
                    catalog=catalog,
                    manifest=manifest,
                    dataset_snapshot=dataset_snapshot,
                    heads=before,
                )
                self._stored += 1
        return manifest

    @staticmethod
    def _valid_evidence(
        proof: _EvidenceProof,
        builder: DatasetBuilder,
        verifier: StreamingEvidenceVerifier,
        catalog: RevisionCatalog,
    ) -> bool:
        return (
            proof.builder is builder
            and proof.verifier is verifier
            and proof.verifier_type is type(verifier)
            and proof.catalog is catalog
            and proof.manifest.dataset.snapshot_id == proof.dataset_snapshot
            and _heads(catalog, tuple(table for table, _ in proof.heads)) == proof.heads
        )


def _verifier_catalog_of(verifier: StreamingEvidenceVerifier) -> RevisionCatalog | None:
    """The catalog ``verifier`` proves manifests of, or None (never cached)."""
    try:
        return evidence_catalog(verifier)
    except DatasetBindingError:
        return None


def _evidence_row_of(
    catalog: RevisionCatalog, manifest_content_hash: str
) -> tuple[str, str] | None:
    """``(dataset_table, dataset_snapshot_id)`` of the one v3 row under the hash (unverified: it
    only chooses the path and the heads; a stored proof requires the verified manifest to bind
    the same), or None (no row, several, or no v3 table)."""
    try:
        rows = catalog.scan_columns(
            DATASET_EVIDENCE_MANIFESTS.table,
            columns=("dataset_table", "dataset_snapshot_id"),
            row_filter=EqualTo("manifest_content_hash", manifest_content_hash),  # type: ignore[call-arg, arg-type]
        ).to_pylist()
    except TableNotFound:
        return None
    if len(rows) != 1:
        return None
    return rows[0]["dataset_table"], rows[0]["dataset_snapshot_id"]


def _catalog_of(builder: DatasetBuilder) -> RevisionCatalog | None:
    """The builder's own catalog (the one its verifier reads), or None: never cached."""
    if not isinstance(builder, DatasetBuilder):
        return None
    catalog: RevisionCatalog | None = getattr(builder, "_adapter", None)
    return catalog


def _dataset_table_of(catalog: RevisionCatalog, manifest_content_hash: str) -> str | None:
    """The dataset table the persisted row names (unverified: it only chooses which heads to
    read; a stored proof requires the verified manifest to name the same table)."""
    rows = catalog.scan_columns(
        DATASET_MANIFESTS.table,
        columns=("dataset_table",),
        row_filter=EqualTo("manifest_content_hash", manifest_content_hash),  # type: ignore[call-arg, arg-type]
    ).to_pylist()
    tables = {row["dataset_table"] for row in rows}
    return tables.pop() if len(tables) == 1 else None


def _heads(catalog: RevisionCatalog, tables: Sequence[str]) -> tuple[tuple[str, str], ...]:
    heads: list[tuple[str, str]] = []
    for table in tables:
        info = catalog.load_table(table)
        if info is None:
            heads.append((table, _ABSENT))
        elif info.current_snapshot is None:
            heads.append((table, _EMPTY))
        else:
            heads.append((table, info.current_snapshot.snapshot_id))
    return tuple(heads)

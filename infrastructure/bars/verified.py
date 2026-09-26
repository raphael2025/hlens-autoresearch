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

**Limits**: process-local and unbounded (one entry per distinct builder x manifest; the caller
drops the cache to free it); the builder's catalog is read through its private ``_adapter``
attribute — a builder without it is never cached (plain verified loads, counted as
``uncacheable``); the unpinned-head list mirrors ``DatasetBuilder.verify_manifest`` from outside
Phase 1 (guarded by the test above; a public ``DatasetBuilder`` accessor for both is a follow-up
for Codex review); a catalog whose head is moved away and back to the same snapshot **during** one
verified load (a rollback, which no project writer performs) would not be noticed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from threading import Lock
from typing import Final

from pyiceberg.expressions import EqualTo

from core.contracts.universe import ResearchDatasetManifest
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    DATASET_MANIFESTS,
    QUALITY_EVIDENCE_GAPS,
)
from infrastructure.dataset.builder import DATASET_RULE_HASH, DatasetBuilder
from infrastructure.feature.dataset import load_manifest
from infrastructure.revision.store import RevisionCatalog

__all__ = [
    "HEAD_READ_TABLES",
    "ManifestCacheStats",
    "VerifiedManifestCache",
    "load_verified_manifest",
    "verification_head_tables",
]

#: Tables ``DatasetBuilder.verify_manifest`` / ``ManifestStore.load`` read at their current head
#: for every manifest (plus the manifest's own dataset table, see ``verification_head_tables``).
HEAD_READ_TABLES: Final = (
    DATASET_MANIFESTS.table,
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


class VerifiedManifestCache:
    """Successful verified manifest loads, reused only while every input is unchanged."""

    def __init__(self) -> None:
        self._proofs: dict[tuple[int, str], _Proof] = {}
        self._lock = Lock()
        self._hits = self._misses = self._stored = self._uncacheable = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._proofs)

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

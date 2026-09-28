"""v2 manifests stay readable and verifiable bit for bit after ADR-0077 (§8; acceptance 1).

ADR-0077 §8.2 adds the v3 manifest table and a v2 / v3 dispatch to ``ManifestStore``; the v2
path itself must not move. The golden reference here is ``legacy_load``, a verbatim copy of
``ManifestStore.load`` as it was before ADR-0077 (commit 7259ebf): for v2 manifests written at
every published contract envelope (2.0.0 ... current), the new store returns exactly what the
legacy code returns — the same object, canonical JSON, content hash and row — refuses exactly
what it refuses, commits nothing, and never touches the v3 verifier. Only a hash persisted in
**both** manifest tables, which could not exist before ADR-0077, is newly refused.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import Any, cast

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from pyiceberg.expressions import BooleanExpression, EqualTo

from core.contracts.catalog import TableNotFound
from core.contracts.universe import ResearchDatasetEvidenceManifest, ResearchDatasetManifest
from core.domain.base import PUBLISHED_CONTRACT_SCHEMA_VERSIONS, canonical_json
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    DATASET_EVIDENCE_MANIFESTS,
    DATASET_MANIFESTS,
    PHASE1_TABLES,
)
from infrastructure.dataset.builder import DATASET_RULE_HASH, DATASET_RULE_VERSION
from infrastructure.dataset.manifests import (
    DatasetEvidenceManifestStore,
    ManifestFormError,
    ManifestStore,
    ManifestVerifier,
    manifest_batch_id,
    manifest_row,
)
from infrastructure.revision.store import RevisionCatalog
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE
from tests.infrastructure.contract_era import written_at
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import END, START, World


def legacy_load(
    adapter: RevisionCatalog, verifier: ManifestVerifier, content_hash: str
) -> ResearchDatasetManifest | None:
    """``ManifestStore.load`` before ADR-0077, verbatim (``_row`` inlined): the golden path."""
    columns = tuple(field.name for field in DATASET_MANIFESTS.arrow_schema)
    rows = adapter.scan_columns(
        DATASET_MANIFESTS.table,
        columns=columns,
        row_filter=EqualTo("manifest_content_hash", content_hash),  # type: ignore[call-arg, arg-type]
    ).to_pylist()
    if len(rows) > 1:
        raise CatalogIntegrityError(f"manifest {content_hash} is persisted twice")
    row = rows[0] if rows else None
    if row is None:
        return None
    document: str = row["manifest_json"]
    if hashlib.sha256(document.encode("utf-8")).hexdigest() != content_hash:
        raise CatalogIntegrityError(f"manifest {content_hash}: JSON does not hash to its id")
    manifest = ResearchDatasetManifest.model_validate_json(document)
    if manifest_row(manifest) != row:
        raise CatalogIntegrityError(f"manifest {content_hash}: columns disagree with its JSON")
    verifier.verify_manifest(manifest)
    return manifest


class RefusingEvidenceVerifier:
    """A v3 verifier a v2 manifest must never reach."""

    def __init__(self) -> None:
        self.calls = 0

    def verify_evidence_manifest(
        self, manifest: ResearchDatasetEvidenceManifest, *, manifested: bool
    ) -> None:
        self.calls += 1
        raise AssertionError("a v2 manifest never reaches the v3 verifier")


def _ready(w: World) -> None:
    w.listed()
    w.trades()
    w.report("agg_trades")


def _build(w: World, window: tuple[Any, Any] = (START, END)) -> Any:
    return w.builder().build(FIRST_SLICE_UNIVERSE, w.spec(), "agg_trades", *window)


def _heads(w: World) -> dict[str, str | None]:
    return {table.table: w.h.head(table.table) for table in PHASE1_TABLES}


def _outcome(call: Callable[[], Any]) -> tuple[str, Any]:
    try:
        return ("returned", call())
    except Exception as exc:  # the outcome itself is compared
        return ("raised", type(exc))


def _v3_row(content_hash: str) -> dict[str, Any]:
    """A row of the v3 table under ``content_hash`` (its content is irrelevant here)."""
    return {
        "manifest_content_hash": content_hash,
        "contract_schema_version": "2.3.0",
        "dataset_zone": "research_dataset",
        "dataset_table": "research.dataset_selection_chunks",
        "dataset_snapshot_id": "1",
        "dataset_time_range_start": START,
        "dataset_time_range_end": END,
        "point_in_time_name": "planted",
        "point_in_time_version": "1.0.0",
        "point_in_time_hash": "0" * 64,
        "simulation_time": ds.SIM,
        "simulation_start": None,
        "simulation_end": None,
        "knowledge_cutoff": ds.SIM,
        "universe_spec_name": "planted",
        "universe_spec_version": "1.0.0",
        "universe_spec_hash": "0" * 64,
        "snapshot_bindings": [],
        "rule_id": "hlens.dataset.pit-selection",
        "rule_version": "2.0.0",
        "rule_hash": "0" * 64,
        "data_type": "agg_trades",
        "selection_id": "planted",
        "row_count": 1,
        "chunk_rows": 1,
        "chunk_count": 1,
        "evidence": [],
        "manifest_json": "{}",
    }


# =========================================================================================
# acceptance 1: every published envelope, read and verified as before
# =========================================================================================


@pytest.mark.parametrize("version", PUBLISHED_CONTRACT_SCHEMA_VERSIONS)
def test_a_v2_manifest_loads_and_verifies_exactly_as_before(w: World, version: str) -> None:
    with written_at(version):
        _ready(w)
        built = _build(w)
    manifest = built.manifest
    assert manifest.schema_version == version
    content_hash = manifest.content_hash()
    heads = _heads(w)

    refusing = RefusingEvidenceVerifier()
    store = ManifestStore(w.h.adapter, w.builder(), evidence_verifier=refusing)
    legacy = legacy_load(w.h.adapter, w.builder(), content_hash)
    loaded = store.load(content_hash)
    assert loaded is not None and legacy is not None
    assert loaded == legacy == manifest
    assert canonical_json(loaded.model_dump(mode="json")) == canonical_json(
        legacy.model_dump(mode="json")
    )
    assert loaded.content_hash() == legacy.content_hash() == content_hash
    assert loaded.schema_version == version
    [row] = w.h.rows(DATASET_MANIFESTS)
    assert row == manifest_row(loaded) == manifest_row(manifest)

    # The dispatcher takes the v2 path for a v2 hash; the builder verifies as before.
    assert store.load_any(content_hash) == manifest
    assert w.builder().manifests().load_any(content_hash) == manifest
    w.builder().verify_manifest(manifest)
    assert store.persist(manifest).replayed
    assert refusing.calls == 0

    # Nothing was committed anywhere; the v3 table was never written.
    assert _heads(w) == heads
    assert w.h.head(DATASET_EVIDENCE_MANIFESTS.table) is None


def test_v2_refusals_are_exactly_the_legacy_ones(w: World) -> None:
    _ready(w)
    built = _build(w)
    genuine: ResearchDatasetManifest = built.manifest
    store = w.builder().manifests()

    dropped = ResearchDatasetManifest.model_validate_json(
        genuine.model_copy(update={"members": genuine.members[1:]}).model_dump_json()
    )
    w.h.forge_rows(DATASET_MANIFESTS, [manifest_row(dropped)], batch_id="forged-dropped")
    extra = genuine.model_copy(
        update={"quality_report_ids": (*genuine.quality_report_ids, "qr-extra")}
    )
    w.h.forge_rows(
        DATASET_MANIFESTS,
        [dict(manifest_row(extra), manifest_content_hash="f" * 64)],
        batch_id="forged-hash",
    )

    cases = {
        "genuine": genuine.content_hash(),
        "absent": "0" * 64,
        "dropped-member": dropped.content_hash(),
        "json-hash-mismatch": "f" * 64,
    }
    outcomes: dict[str, tuple[str, Any]] = {}
    for name, content_hash in cases.items():
        new = _outcome(partial(store.load, content_hash))
        old = _outcome(partial(legacy_load, w.h.adapter, w.builder(), content_hash))
        assert new == old, name
        assert _outcome(partial(store.load_any, content_hash)) == old, name
        outcomes[name] = new
    assert outcomes["genuine"] == ("returned", genuine)
    assert outcomes["absent"] == ("returned", None)
    assert outcomes["dropped-member"] == ("raised", CatalogIntegrityError)
    assert outcomes["json-hash-mismatch"] == ("raised", CatalogIntegrityError)


# =========================================================================================
# §8.2: one form per hash (newly refused: impossible before ADR-0077)
# =========================================================================================


def test_a_v2_hash_also_in_the_v3_table_is_refused_by_every_entry(w: World) -> None:
    _ready(w)
    built = _build(w)
    content_hash = built.manifest.content_hash()
    w.h.forge_rows(DATASET_EVIDENCE_MANIFESTS, [_v3_row(content_hash)], batch_id="planted-v3")
    refusing = RefusingEvidenceVerifier()
    store = ManifestStore(w.h.adapter, w.builder(), evidence_verifier=refusing)
    with pytest.raises(CatalogIntegrityError, match="both"):
        store.load(content_hash)
    with pytest.raises(CatalogIntegrityError, match="both"):
        store.load_any(content_hash)
    with pytest.raises(CatalogIntegrityError, match="both"):
        DatasetEvidenceManifestStore(w.h.adapter, refusing).load(content_hash)
    assert refusing.calls == 0


def test_the_v3_store_never_returns_a_v2_manifest(w: World) -> None:
    _ready(w)
    built = _build(w)
    refusing = RefusingEvidenceVerifier()
    v3 = DatasetEvidenceManifestStore(w.h.adapter, refusing)
    with pytest.raises(ManifestFormError):
        v3.load(built.manifest.content_hash())
    assert v3.load("0" * 64) is None
    assert v3.recorded_version(built.selection.selection_id) is None
    assert refusing.calls == 0


class _WithoutV3Table:
    """A catalog created before ADR-0077: the v3 manifest table does not exist."""

    def __init__(self, inner: RevisionCatalog) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def load_table(self, table: str) -> Any:
        if table == DATASET_EVIDENCE_MANIFESTS.table:
            return None
        return self._inner.load_table(table)

    def scan_columns(
        self,
        table: str,
        *,
        columns: Sequence[str],
        row_filter: BooleanExpression | None = None,
        limit: int | None = None,
        snapshot_id: str | None = None,
    ) -> pa.Table:
        if table == DATASET_EVIDENCE_MANIFESTS.table:
            raise TableNotFound(f"table {table} does not exist")
        kwargs: Mapping[str, Any] = {} if row_filter is None else {"row_filter": row_filter}
        return self._inner.scan_columns(
            table, columns=columns, limit=limit, snapshot_id=snapshot_id, **kwargs
        )


def test_a_catalog_without_the_v3_table_reads_v2_unchanged(w: World) -> None:
    _ready(w)
    built = _build(w)
    content_hash = built.manifest.content_hash()
    legacy_catalog = cast(RevisionCatalog, _WithoutV3Table(w.h.adapter))
    store = ManifestStore(legacy_catalog, w.builder())
    assert store.load(content_hash) == built.manifest
    assert store.load_any(content_hash) == built.manifest
    assert store.load("0" * 64) is None


def test_the_v2_surface_is_unchanged() -> None:
    assert manifest_batch_id("abc") == "manifest.abc"
    assert DATASET_RULE_VERSION == "1.0.0"
    assert len(DATASET_RULE_HASH) == 64
    assert DATASET_MANIFESTS.table == "research.dataset_manifests"
    assert DATASET_MANIFESTS in PHASE1_TABLES

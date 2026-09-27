"""G2 / forged and deleted rows, rewritten snapshots and objects, forged manifests.

Every attack happens *after* a dataset was built. Two questions each time: does the old manifest
still reproduce (catalog snapshots are immutable, so appends and deletes must not reach it, while
storage rewritten under a bound snapshot must be caught), and does a new build over the tampered
heads fail closed without writing anything?
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pyiceberg.expressions import EqualTo

from core.contracts.universe import ResearchDatasetManifest
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    CANONICAL_INSTRUMENT_LISTINGS,
    DATA_QUALITY_REPORTS,
    DATASET_MANIFESTS,
    DATASET_SELECTIONS,
    QUALITY_EVIDENCE_GAPS,
)
from infrastructure.dataset.builder import DatasetBuilt
from infrastructure.dataset.manifests import ManifestStore
from infrastructure.quality.reporter import QualityReportMissing
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import L1, L2, World
from tests.infrastructure.redteam import redteam_support as rt


def _built(w: World) -> DatasetBuilt:
    w.listed()
    w.trades()
    w.report()
    return rt.build(w)


def _equals(column: str, value: object) -> Any:
    return EqualTo(column, value)  # type: ignore[call-arg, arg-type]


def _replays(w: World, built: DatasetBuilt) -> None:
    replay = rt.build(w, built.manifest.point_in_time)
    assert replay.replayed and replay.manifest == built.manifest


def _archive_trade(w: World) -> dict[str, Any]:
    rows = [r for r in w.h.rows(c.TRADES) if r["lineage_raw_table"] == c.ARCHIVE_AGGS.table]
    return min(rows, key=lambda row: row["arrival_seq"])


# =========================================================================================
# catalog rows forged or deleted after the build (new snapshots: the old manifest is immune)
# =========================================================================================


def test_a_forged_canonical_row(w: World) -> None:
    built = _built(w)
    row = _archive_trade(w)
    forged = dict(row, price=row["price"] + Decimal(1))
    w.h.forge_rows(c.TRADES, [forged], batch_id="forged-canonical")
    _replays(w, built)
    before = rt.outputs(w)
    with pytest.raises(CatalogIntegrityError):
        w.report()
        rt.build(w)
    assert rt.outputs(w) == before


def test_a_deleted_canonical_row(w: World) -> None:
    built = _built(w)
    w.h.delete_rows(c.TRADES, _equals("revision_id", _archive_trade(w)["revision_id"]))
    _replays(w, built)
    before = rt.outputs(w)
    with pytest.raises(CatalogIntegrityError):
        w.report()
        rt.build(w)
    assert rt.outputs(w) == before


def test_a_deleted_evidence_gap_row(w: World) -> None:
    built = _built(w)
    [gap, *_] = w.h.rows(QUALITY_EVIDENCE_GAPS)
    w.h.delete_rows(QUALITY_EVIDENCE_GAPS, _equals("revision_id", gap["revision_id"]))
    _replays(w, built)
    before = rt.outputs(w)
    with pytest.raises(CatalogIntegrityError):
        rt.build(w)  # the same reports, their gap batches no longer whole
    assert rt.outputs(w) == before


def test_a_deleted_quality_report(w: World) -> None:
    built = _built(w)
    report_id = built.manifest.quality_report_ids[0]
    w.h.delete_rows(DATA_QUALITY_REPORTS, _equals("report_id", report_id))
    _replays(w, built)
    before = rt.outputs(w)
    with pytest.raises((QualityReportMissing, CatalogIntegrityError)):
        rt.build(w)
    assert rt.outputs(w) == before


def test_a_deleted_listing_row(w: World) -> None:
    w.listed(ds.TRADING, L1)
    w.listed(ds.ETH_HALT, L2)
    w.trades()
    w.report(symbols=("BTCUSDT",))
    built = rt.build(w)
    [excluded] = built.manifest.exclusions
    w.h.delete_rows(
        CANONICAL_INSTRUMENT_LISTINGS, _equals("revision_id", excluded.listing_revision_id)
    )
    _replays(w, built)
    before = rt.outputs(w)
    with pytest.raises(CatalogIntegrityError, match="not a listing derivation batch"):
        w.report(symbols=("BTCUSDT",))
        rt.build(w)
    assert rt.outputs(w) == before


def test_forged_rows_appended_under_a_dataset_selection_id_never_reach_its_manifest(
    w: World,
) -> None:
    built = _built(w)
    rows = w.h.rows_at(DATASET_SELECTIONS.table, built.dataset_commit.snapshot_id)
    forged = dict(rows[0], observation_key="binance:spot:agg_trade:BTCUSDT:999")
    w.h.forge_rows(DATASET_SELECTIONS, [forged], batch_id="forged-selection")
    _replays(w, built)
    # The manifest binds its own snapshot: the forged row is not there.
    assert w.h.rows_at(DATASET_SELECTIONS.table, built.manifest.dataset.snapshot_id) == rows


# =========================================================================================
# storage rewritten under a bound snapshot (same path): the old manifest must not replay
# =========================================================================================


def test_a_canonical_data_file_rewritten_in_place(w: World) -> None:
    built = _built(w)
    snapshot = built.manifest.point_in_time.snapshot_bindings[c.TRADES.table]
    price = _archive_trade(w)["price"]
    assert rt.rewrite_in_place(w, c.TRADES, snapshot, "price", {price: price + 1}) > 0
    w.h.reopen()
    before = rt.outputs(w)
    with pytest.raises(CatalogIntegrityError):
        rt.build(w, built.manifest.point_in_time)
    assert rt.outputs(w) == before


def test_the_dataset_data_file_rewritten_in_place(w: World) -> None:
    built = _built(w)
    snapshot = built.manifest.dataset.snapshot_id
    [first, *_] = sorted(row["revision_id"] for row in built.selection.rows)
    changed = rt.rewrite_in_place(
        w, DATASET_SELECTIONS, snapshot, "revision_id", {first: "crev1-" + "0" * 64}
    )
    assert changed == 1
    w.h.reopen()
    with pytest.raises(CatalogIntegrityError, match="reads back differently"):
        rt.build(w, built.manifest.point_in_time)


def test_an_archive_object_rewritten_in_place(w: World) -> None:
    built = _built(w)
    [archive] = w.h.rows(c.ARCHIVES)
    path = w.h.object_path(archive["object_key"])
    data = bytearray(path.read_bytes())
    data[-30] ^= 0x01
    path.write_bytes(bytes(data))
    before = rt.outputs(w)
    with pytest.raises(CatalogIntegrityError, match="not lawful"):
        rt.build(w, built.manifest.point_in_time)
    with pytest.raises(CatalogIntegrityError):
        rt.build(w)
    assert rt.outputs(w) == before


# =========================================================================================
# manifests
# =========================================================================================


def _survivorship(built: DatasetBuilt) -> ResearchDatasetManifest:
    """The genuine manifest minus its exclusion: hash-consistent, contract-valid, false."""
    forged = built.manifest.model_copy(update={"exclusions": ()})
    return ResearchDatasetManifest.model_validate_json(forged.model_dump_json())


def _halted(w: World) -> DatasetBuilt:
    w.listed(ds.TRADING, L1)
    w.listed(ds.ETH_HALT, L2)
    w.trades()
    w.report(symbols=("BTCUSDT",))
    built = rt.build(w)
    assert len(built.manifest.exclusions) == 1
    return built


def test_a_forged_row_under_a_genuine_manifest_hash_is_refused(w: World) -> None:
    built = _halted(w)
    forged = _survivorship(built)
    row = dict(rt.manifest_row(forged), manifest_content_hash=built.manifest.content_hash())
    w.h.forge_rows(DATASET_MANIFESTS, [row], batch_id="forged-manifest")
    with pytest.raises(CatalogIntegrityError):
        ManifestStore(w.h.adapter, w.builder()).load(built.manifest.content_hash())
    with pytest.raises(CatalogIntegrityError):
        rt.build(w, built.manifest.point_in_time)


def test_a_self_consistent_manifest_no_build_produced_is_refused(w: World) -> None:
    """G2 RT-4 (fixed): the store re-derives every manifest it persists or loads."""
    built = _halted(w)
    forged = _survivorship(built)
    assert forged.dataset == built.manifest.dataset  # binds the genuine dataset snapshot
    store = ManifestStore(w.h.adapter, w.builder())
    with pytest.raises(CatalogIntegrityError):
        store.persist(forged)
        store.load(forged.content_hash())

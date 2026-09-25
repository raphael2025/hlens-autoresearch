"""F3 Research Dataset + manifest (roadmap #18 / #20; ADR-0023 §5 / §6, ADR-0024 §6).

Archive → Raw → Canonical → PIT → universe → dataset + manifest over the real stores, the real
normalizer / reconciler, the real exchangeInfo snapshots and listing derivation, and the real
quality reporters, on one SQLite catalog (PostgreSQL variant in ``test_dataset_postgres.py``).
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from core.contracts.universe import ExclusionReason, ResearchDatasetManifest
from core.domain.base import canonical_json
from core.domain.specs import Zone
from infrastructure.canonical.listings import UnconstructibleReason
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_EXCHANGE_INFO,
    CANONICAL_INSTRUMENT_LISTINGS,
    DATA_QUALITY_REPORTS,
    DATASET_MANIFESTS,
    DATASET_SELECTIONS,
)
from infrastructure.dataset import builder as b
from infrastructure.dataset.builder import (
    DatasetEmpty,
    DatasetSpecError,
    selection_id_for,
)
from infrastructure.dataset.manifests import ManifestStore, manifest_row
from infrastructure.pit.selector import PitConflictError
from infrastructure.quality.reporter import (
    QualityReportMissing,
    evidence_gaps_of,
    quality_report_id,
)
from infrastructure.universe.builder import (
    FIRST_SLICE_UNIVERSE,
    UniverseSpecError,
    UniverseUnconstructible,
)
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import END, L1, L2, SIM, START, World
from tests.infrastructure.revision.rest_store_support import DAY, SYMBOL, utc

MANIFESTS = DATASET_MANIFESTS


def _ready(w: World, *, statuses: Any = ds.TRADING, data: str = "trades") -> None:
    w.listed(statuses)
    if data == "trades":
        w.trades()
    else:
        w.bars()
    w.report("agg_trades" if data == "trades" else "klines_1m")


def _build(w: World, spec: Any = None, data_type: str = "agg_trades", **kwargs: Any) -> Any:
    window = kwargs.pop("window", (START, END))
    return w.builder().build(
        FIRST_SLICE_UNIVERSE, spec or w.spec(**kwargs), data_type, window[0], window[1]
    )


# =========================================================================================
# end to end (roadmap #20) and determinism
# =========================================================================================


def test_archive_to_manifest_end_to_end(w: World) -> None:
    _ready(w)
    spec = w.spec()
    built = w.builder().build(FIRST_SLICE_UNIVERSE, spec, "agg_trades", START, END)
    manifest = built.manifest

    # Universe: both first-slice episodes observed TRADING before the simulation time.
    assert [ds.symbol_of(m) for m in manifest.members] == ["BTC-USDT", "ETH-USDT"]
    assert manifest.exclusions == ()
    assert manifest.universe_spec == FIRST_SLICE_UNIVERSE.binding()
    # Rows: the three archive trades (the D-33 edges make the archive copy the only head).
    archive_rows = [r for r in w.h.rows(c.TRADES) if r["lineage_raw_table"] == c.ARCHIVE_AGGS.table]
    assert len(archive_rows) == 3
    rows = w.h.rows_at(DATASET_SELECTIONS.table, built.dataset_commit.snapshot_id)
    assert sorted(r["revision_id"] for r in rows) == sorted(r["revision_id"] for r in archive_rows)
    assert {r["selection_id"] for r in rows} == {built.selection.selection_id}
    assert {(r["effective_from"], r["effective_until"]) for r in rows} == {(None, None)}
    # Own DatasetRef: the materialized snapshot, the event window.
    assert manifest.dataset.zone is Zone.RESEARCH_DATASET
    assert manifest.dataset.table == DATASET_SELECTIONS.table
    assert manifest.dataset.snapshot_id == built.dataset_commit.snapshot_id
    assert (manifest.dataset.time_range_start, manifest.dataset.time_range_end) == (START, END)
    # Lineage: listings (both hops = the observing snapshot) + trades (three hops to the archive).
    tables = {
        (item.canonical_table, item.raw_table, item.source_table) for item in manifest.lineage
    }
    assert tables == {
        (
            CANONICAL_INSTRUMENT_LISTINGS.table,
            BINANCE_SPOT_EXCHANGE_INFO.table,
            BINANCE_SPOT_EXCHANGE_INFO.table,
        ),
        (c.TRADES.table, c.ARCHIVE_AGGS.table, c.ARCHIVES.table),
    }
    # Quality: BTC + ETH partition of DAY and the listing report, each re-derived.
    reports = {r["report_id"]: r for r in w.h.rows(DATA_QUALITY_REPORTS)}
    assert set(manifest.quality_report_ids) == set(reports)
    assert len(manifest.quality_report_ids) == 3
    # Evidence gaps: every trade and listing revision bound, each to the report recording it.
    gap_tables = sorted({gap.table for gap in manifest.evidence_gaps})
    assert gap_tables == [CANONICAL_INSTRUMENT_LISTINGS.table, c.TRADES.table]
    for gap in manifest.evidence_gaps:
        recorded = {
            (g["table"], g["revision_id"]): g["gap"]
            for g in evidence_gaps_of(w.h.adapter, gap.quality_report_id)
        }
        assert recorded[(gap.table, gap.revision_id)] == gap.gap
    # Persisted: one row, re-proven on load.
    [row] = w.h.rows(MANIFESTS)
    assert row == manifest_row(manifest)
    assert ManifestStore(w.h.adapter).load(manifest.content_hash()) == manifest
    assert not built.replayed


@pytest.mark.parametrize("data_type", ["klines_1m"])
def test_bars_are_selected_a_day_at_a_time(w: World, data_type: str) -> None:
    _ready(w, data="bars")
    window = (utc(2023, 11, 14), utc(2023, 11, 15))
    built = _build(w, data_type=data_type, window=window)
    bars = [r for r in w.h.rows(c.BARS) if r["lineage_raw_table"] == c.ARCHIVE_KLINES.table]
    assert sorted(r["revision_id"] for r in built.selection.rows) == sorted(
        r["revision_id"] for r in bars
    )
    assert b._slices(data_type, *window) == [window]


def test_a_rebuild_is_bit_identical_and_replays(w: World) -> None:
    _ready(w)
    spec = w.spec()
    first = _build(w, spec)
    w.h.reopen()  # a fresh process
    second = _build(w, spec)
    assert canonical_json(second.manifest.model_dump(mode="json")) == canonical_json(
        first.manifest.model_dump(mode="json")
    )
    assert second.replayed and second.dataset_commit.snapshot_id == first.dataset_commit.snapshot_id
    assert len(w.h.rows(MANIFESTS)) == 1
    assert len(w.h.rows_at(DATASET_SELECTIONS.table, first.dataset_commit.snapshot_id)) == 3
    # The same inputs select the same rows without writing anything.
    again = w.builder().select(FIRST_SLICE_UNIVERSE, spec, "agg_trades", START, END)
    assert again.rows == first.selection.rows


def test_later_data_never_changes_an_earlier_manifest(w: World) -> None:
    _ready(w)
    old_spec = w.spec()
    first = _build(w, old_spec)
    w.more_trades()  # more data arrives: a new REST page, normalized
    replay = _build(w, old_spec)
    assert replay.manifest == first.manifest and replay.replayed


# =========================================================================================
# no survivorship (ADR-0024 §3 / §4, ADR-0029 §3)
# =========================================================================================


def test_a_halted_symbol_is_kept_as_an_exclusion_not_dropped(w: World) -> None:
    w.listed(ds.TRADING, L1)
    w.listed(ds.ETH_HALT, L2)
    w.trades()
    w.report(symbols=("BTCUSDT",))
    manifest = _build(w).manifest
    [member] = manifest.members
    [excluded] = manifest.exclusions
    assert ds.symbol_of(member) == "BTC-USDT"
    assert (ds.symbol_of(excluded), excluded.reason) == ("ETH-USDT", ExclusionReason.NOT_TRADABLE)
    # The halted episode's history is intact: listed from L1, closed at the halt observation.
    lineage = {item.canonical_revision_id for item in manifest.lineage}
    assert excluded.listing_revision_id in lineage


def test_a_symbol_is_never_a_member_before_it_is_observed(w: World) -> None:
    w.listed(ds.TRADING, L2)
    w.trades()
    w.report()
    with pytest.raises(UniverseUnconstructible) as caught:
        _build(w, at=L1)  # simulation before the first local observation
    assert caught.value.reason == UnconstructibleReason.NO_VISIBLE_LISTING
    assert w.h.rows(MANIFESTS) == []
    assert w.h.head(DATASET_SELECTIONS.table) is None


def test_an_interval_gates_rows_by_membership_spans(w: World) -> None:
    w.listed(ds.TRADING, L1)
    w.trades()
    w.listed(ds.ETH_HALT, utc(2023, 11, 18))
    w.report()
    interval = (utc(2023, 11, 15), SIM)
    built = _build(w, interval=interval)
    manifest = built.manifest
    [btc] = manifest.members[:1]
    assert (ds.symbol_of(btc), btc.effective_from, btc.effective_until) == ("BTC-USDT", *interval)
    eth_member = [m for m in manifest.members if ds.symbol_of(m) == "ETH-USDT"]
    [eth_excluded] = manifest.exclusions
    assert [(m.effective_from, m.effective_until) for m in eth_member] == [
        (interval[0], utc(2023, 11, 18))
    ]
    assert (eth_excluded.effective_from, eth_excluded.effective_until) == (
        utc(2023, 11, 18),
        SIM,
    )
    # Trades are selected from their availability on and never outside the simulation interval.
    archive = {
        r["revision_id"]: r
        for r in w.h.rows(c.TRADES)
        if r["lineage_raw_table"] == c.ARCHIVE_AGGS.table
    }
    for row in built.selection.rows:
        assert row["effective_from"] == max(
            interval[0], archive[row["revision_id"]]["available_time"]
        )
        assert row["effective_until"] == SIM


# =========================================================================================
# fail closed
# =========================================================================================


def test_missing_listing_history_is_refused(w: World) -> None:
    w.trades()
    w.report(listing=False)
    with pytest.raises(UniverseSpecError, match="listing history is missing"):
        _build(w)


def test_a_listing_not_yet_derived_fails_closed(w: World) -> None:
    w.listed(ds.TRADING, L1)
    w.observe("snap-late", ds.ETH_HALT, L2)  # known to Raw, not derived
    w.trades()
    w.report()
    with pytest.raises(UniverseUnconstructible) as caught:
        _build(w)
    assert caught.value.reason == UnconstructibleReason.LISTING_NOT_DERIVED


def test_competing_listing_heads_fail_closed(w: World) -> None:
    halt = {"BTCUSDT": "HALT", "ETHUSDT": "TRADING"}
    w.x.collect("snap-1", ds.TRADING, L1, server_time=1)
    w.x.collect("snap-2", halt, L2, server_time=2)
    w.x.collect("snap-3", halt, ds.L3, server_time=3)
    w.x.ingest("snap-1")
    w.x.ingest("snap-3")
    w.derive()
    w.x.ingest("snap-2")  # moves the change point: the committed revision diverges
    w.derive()
    w.trades()
    w.report()
    with pytest.raises(UniverseUnconstructible) as caught:
        _build(w)
    assert caught.value.reason == UnconstructibleReason.COMPETING_HEADS


def test_competing_trade_heads_fail_closed(w: World) -> None:
    w.listed()
    w.trades(reconcile=False)  # archive and REST copies, no edge: two heads per key
    w.report()
    with pytest.raises(PitConflictError):
        _build(w)
    assert w.h.rows(MANIFESTS) == []


def test_an_unbound_evidence_table_with_a_snapshot_is_refused(w: World) -> None:
    _ready(w)
    with pytest.raises(DatasetSpecError, match="ADR-0027"):
        _build(w, skip=(c.EVIDENCE.table,))


def test_a_missing_partition_report_fails_closed(w: World) -> None:
    w.listed()
    w.trades()
    w.report(symbols=("BTCUSDT",))  # no ETH report although ETH is a member
    with pytest.raises(QualityReportMissing):
        _build(w)


def test_a_missing_listing_report_fails_closed(w: World) -> None:
    w.listed()
    w.trades()
    w.report(listing=False)
    with pytest.raises(QualityReportMissing):
        _build(w)


def test_a_report_of_other_snapshots_is_not_bound(w: World) -> None:
    _ready(w)
    w.more_trades()  # the Canonical head moves after the reports were written
    with pytest.raises(QualityReportMissing):
        _build(w)  # the spec pins the new heads: the old reports describe other inputs


def test_a_forged_report_under_the_expected_id_is_refused(w: World) -> None:
    w.listed()
    w.trades()
    [eth_id, _] = w.report(symbols=("ETHUSDT",))
    [eth] = [r for r in w.h.rows(DATA_QUALITY_REPORTS) if r["report_id"] == eth_id]
    [inputs] = [e for e in eth["events"] if e["event_type"] == "report_inputs"]
    btc_id = quality_report_id(c.TRADES.table, SYMBOL, DAY, json.loads(inputs["detail"]))
    forged = dict(eth, report_id=btc_id, subject_symbol=SYMBOL)  # ETH's content, BTC's id
    w.h.forge_rows(DATA_QUALITY_REPORTS, [forged], batch_id=btc_id)
    with pytest.raises(
        CatalogIntegrityError, match="disagrees with its re-derivation|evidence-gap batch"
    ):
        _build(w)


def test_empty_selection_is_refused(w: World) -> None:
    w.listed()
    w.trades()
    w.report()
    with pytest.raises(DatasetEmpty):
        _build(w, window=(utc(2023, 11, 14, 1), utc(2023, 11, 14, 2)))


def test_filters_unregistered_specs_and_bindings_are_refused(w: World) -> None:
    _ready(w)
    spec = w.spec()
    other = FIRST_SLICE_UNIVERSE.model_copy(update={"version": "1.0.1"})
    with pytest.raises(UniverseSpecError, match="not registered"):
        w.builder().build(other, spec, "agg_trades", START, END)
    unknown = spec.model_copy(
        update={
            "parser_bindings": (
                *spec.parser_bindings,
                spec.parser_bindings[0].model_copy(update={"policy_id": "x.parser"}),
            )
        }
    )
    with pytest.raises(DatasetSpecError, match="not registered"):
        _build(w, unknown)
    with pytest.raises(DatasetSpecError, match="data_type"):
        _build(w, data_type="order_book")
    with pytest.raises(DatasetSpecError, match="window"):
        _build(w, window=(END, START))


def test_slices_follow_the_utc_grid() -> None:
    assert b._slices("agg_trades", utc(2023, 11, 14, 21, 30), utc(2023, 11, 14, 23, 10)) == [
        (utc(2023, 11, 14, 21, 30), utc(2023, 11, 14, 22)),
        (utc(2023, 11, 14, 22), utc(2023, 11, 14, 23)),
        (utc(2023, 11, 14, 23), utc(2023, 11, 14, 23, 10)),
    ]
    assert b._slices("klines_1m", utc(2023, 11, 14, 12), utc(2023, 11, 16)) == [
        (utc(2023, 11, 14, 12), utc(2023, 11, 15)),
        (utc(2023, 11, 15), utc(2023, 11, 16)),
    ]
    assert b._slices("klines_1m", START, END) == [(START, END)]
    assert timedelta(0) == timedelta(0)


def test_manifest_store_is_content_hash_idempotent(w: World) -> None:
    _ready(w)
    built = _build(w)
    store = ManifestStore(w.h.adapter)
    assert store.persist(built.manifest).replayed
    other: ResearchDatasetManifest = built.manifest.model_copy(
        update={"quality_report_ids": (*built.manifest.quality_report_ids, "qr-extra")}
    )
    forged = dict(manifest_row(other), manifest_content_hash=built.manifest.content_hash())
    w.h.forge_rows(MANIFESTS, [forged], batch_id="forged-manifest")
    with pytest.raises(CatalogIntegrityError):
        store.load(built.manifest.content_hash())
    with pytest.raises(CatalogIntegrityError):
        store.persist(built.manifest)
    assert (
        selection_id_for(
            FIRST_SLICE_UNIVERSE, built.manifest.point_in_time, "agg_trades", START, END
        )
        == built.selection.selection_id
    )

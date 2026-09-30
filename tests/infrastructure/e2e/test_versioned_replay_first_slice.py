"""ADR-0052 versioned replay over the whole Phase 1 first slice at current contract version 2.5.0.

The first slice (exchangeInfo -> listings; archive + REST aggTrades / klines for two symbols;
reconciliation edges; Canonical normalization; quality reports; a Research Dataset + manifest) is
committed by an earlier published code version (``written_at("2.0.0")`` and ``written_at("2.1.0")``,
one run each), then every write step is re-run and every read repeated by the current 2.5.0 code on
the same catalog:

- nothing is committed: every table head is unchanged, every row reads back as recorded;
- the manifest loads and re-verifies with its recorded content hash; a rebuild from its own PIT
  spec replays the dataset snapshot and the manifest (no second manifest);
- PIT selections of the earlier records are the same revisions and rows as when first read;
- new data written afterwards lands at 2.2.0 in the same tables, beside the earlier rows, and a
  PIT read / dataset build over both versions is defined (each record keeps its own version).

These are real committed Iceberg snapshots (temporary SQLite catalog), not in-memory models.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.contracts.revision import PointInTimeSpec
from core.domain.base import CONTRACT_SCHEMA_VERSION, canonical_json
from infrastructure.canonical import rules
from infrastructure.catalog.phase1_tables import (
    DATASET_MANIFESTS,
    DATASET_SELECTIONS,
    PHASE1_TABLES,
)
from infrastructure.dataset.manifests import ManifestStore
from infrastructure.pit.assumption import ASSUMPTION_BINDING
from infrastructure.pit.selector import PitSelection, PitSelector
from infrastructure.revision.exchange_info_availability import EXCHANGE_INFO_AVAILABILITY_BINDING
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.collector import rest_support as cs
from tests.infrastructure.contract_era import written_at
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.e2e import first_slice_support as fs
from tests.infrastructure.revision import rest_store_support as ss

#: Every published version older than the current one (ADR-0052 2.1.0, ADR-0055 2.2.0 bumps).
PRIOR = ("2.0.0", "2.1.0")
prior_versions = pytest.mark.parametrize("old", PRIOR)
_ASSUMED = (rules.AVAILABILITY_BINDING, EXCHANGE_INFO_AVAILABILITY_BINDING, ASSUMPTION_BINDING)


def _walk(w: ds.World) -> None:
    """The first slice's writes (roadmap #20 steps 1 - 3), as the e2e acceptance runs them."""
    w.listed(ds.TRADING, ds.L1)
    w.trades()
    fs.ingest_trades_for(w, fs.ETH, tag="eth")
    fs.ingest_bars_for(w, fs.BTC, tag="btc", base="100")
    fs.ingest_bars_for(w, fs.ETH, tag="eth", base="200")
    w.report("agg_trades", symbols=(fs.BTC, fs.ETH), listing=False)
    w.report("klines_1m", symbols=(fs.BTC, fs.ETH), listing=True)


def _text(row: Any) -> str:
    return json.dumps(row, default=repr, sort_keys=True)


def _heads(w: ds.World) -> dict[str, str | None]:
    return {table.table: w.h.head(table.table) for table in PHASE1_TABLES}


def _contents(w: ds.World) -> dict[str, list[dict[str, Any]]]:
    return {
        table.table: sorted(w.h.rows(table), key=_text)
        for table in PHASE1_TABLES
        if w.h.head(table.table) is not None
    }


def _versions(w: ds.World) -> dict[str, set[str]]:
    return {
        table.table: {row["contract_schema_version"] for row in w.h.rows(table)}
        for table in PHASE1_TABLES
        if "contract_schema_version" in table.arrow_schema.names
        and w.h.head(table.table) is not None
    }


def _picture(selection: PitSelection) -> Any:
    """What a PIT read decided, independent of the objects' own (read-time) envelopes."""
    return (
        [
            (
                item.observation_key,
                item.simulation_time,
                item.status,
                item.selected_revision_id,
                item.maximal_heads,
            )
            for item in selection.selections
        ],
        sorted(_text(row) for row in selection.selected_rows.values()),
        selection.conflicts,
    )


def _select(w: ds.World, spec: PointInTimeSpec, data_type: str, symbol: str) -> PitSelection:
    start, end = (fs.DAY_START, fs.DAY_END) if data_type == "klines_1m" else (ds.START, ds.END)
    return PitSelector(
        w.h.adapter, w.h.storage, canonical_scratch_directory=w.h.canonical_scratch_directory
    ).select(spec, data_type, symbol, start, end)


def _replay_every_step(w: ds.World) -> None:
    """Re-run each committed write through its own writer (stores, normalizer, reconciler,
    listing derivation, quality reports) exactly as first called."""
    h = w.h
    w.x.ingest(f"snap-{ds.L1.isoformat()}")
    w.derive()
    c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(ss.agg_items(3)), knowledge=ds.K_A)
    h.store(clock=ss.StepClock(start=ds.K_R)).ingest_collection(ss.agg_request("req-rest"))
    items = ss.agg_items(3, first_id=100, first_ms=ss.T0)
    fs.ingest_archive_for(
        h,
        "agg_trades",
        fs.ETH,
        ss.archive_agg_lines(items),
        knowledge=ds.K_A,
        request_id="archive-agg-eth",
    )
    h.store(clock=ss.StepClock(start=ds.K_R)).ingest_collection(
        cs.agg_request(
            request_id="rest-agg-eth",
            symbols=(fs.ETH,),
            start_ms=ss.T0,
            end_ms=ss.T0 + 5 * ss.MINUTE_MS,
        )
    )
    for symbol, tag, base in ((fs.BTC, "btc", "100"), (fs.ETH, "eth", "200")):
        bars = fs.klines(fs.KLINE_COUNT, fs.KLINE_START_MS, base=base)
        fs.ingest_archive_for(
            h,
            "klines_1m",
            symbol,
            ss.archive_kline_lines(bars),
            knowledge=ds.K_A,
            request_id=f"archive-klines-{tag}",
        )
        h.store(clock=ss.StepClock(start=ds.K_R)).ingest_collection(
            cs.kline_request(
                request_id=f"rest-klines-{tag}",
                symbols=(symbol,),
                start_ms=bars[0][0],
                end_ms=bars[0][0] + len(bars) * ss.MINUTE_MS,
            )
        )
    for raw_table, lineage in (
        (c.ARCHIVE_AGGS.table, "archive_revision_id"),
        (c.ARCHIVE_KLINES.table, "archive_revision_id"),
        (c.REST_AGGS.table, "response_revision_id"),
        (c.REST_KLINES.table, "response_revision_id"),
    ):
        units = h.adapter.scan_columns(raw_table, columns=(lineage,)).column(lineage).to_pylist()
        clock = ss.StepClock(start=ds.N_A)
        for unit in sorted(set(units)):
            assert c.normalizer(h, clock=clock).normalize_unit(raw_table, unit).replayed
        assert clock.calls == 0
    for symbol in (fs.BTC, fs.ETH):
        for data_type in ("agg_trades", "klines_1m"):
            h.reconciler(clock=ss.StepClock(start=ds.K_E)).reconcile(data_type, symbol, ss.DAY)
    w.report("agg_trades", symbols=(fs.BTC, fs.ETH), listing=False)
    w.report("klines_1m", symbols=(fs.BTC, fs.ETH), listing=True)


@prior_versions
def test_an_earlier_first_slice_is_read_and_replayed_unchanged_now(w: ds.World, old: str) -> None:
    assert CONTRACT_SCHEMA_VERSION == "2.5.0"
    with written_at(old):
        _walk(w)
        spec = w.spec()
        built = w.builder().build(FIRST_SLICE_UNIVERSE, spec, "klines_1m", fs.DAY_START, fs.DAY_END)
        assumed = w.spec(availability_bindings=_ASSUMED)
        first_reads = {
            (data_type, symbol): _picture(_select(w, assumed, data_type, symbol))
            for data_type in ("agg_trades", "klines_1m")
            for symbol in (fs.BTC, fs.ETH)
        }
    manifest = built.manifest
    assert manifest.schema_version == old
    assert set().union(*_versions(w).values()) == {old}
    heads, contents = _heads(w), _contents(w)

    w.h.reopen()  # a fresh process of the current code
    _replay_every_step(w)
    assert _heads(w) == heads  # nothing committed anywhere
    assert _contents(w) == contents  # every row as recorded

    loaded = w.builder().manifests().load(manifest.content_hash())
    assert loaded is not None
    assert canonical_json(loaded.model_dump(mode="json")) == canonical_json(
        manifest.model_dump(mode="json")
    )
    assert loaded.content_hash() == manifest.content_hash()
    rebuilt = w.builder().build(
        FIRST_SLICE_UNIVERSE, manifest.point_in_time, "klines_1m", fs.DAY_START, fs.DAY_END
    )
    assert rebuilt.replayed
    assert rebuilt.manifest.content_hash() == manifest.content_hash()
    assert rebuilt.dataset_commit.snapshot_id == built.dataset_commit.snapshot_id
    assert len(w.h.rows(DATASET_MANIFESTS)) == 1
    for (data_type, symbol), picture in first_reads.items():
        again = _select(w, assumed, data_type, symbol)
        assert _picture(again) == picture
        assert {r.schema_version for rs in again.records.values() for r in rs} == {old}
    assert _heads(w) == heads


@prior_versions
def test_earlier_and_current_groups_are_read_side_by_side(w: ds.World, old: str) -> None:
    """V6: the current code appends beside earlier data in the same tables; reads stay defined."""
    with written_at(old):
        w.listed(ds.TRADING, ds.L1)
        w.trades()  # BTCUSDT aggTrades: archive + REST + edge + Canonical, all at old
        fs.ingest_bars_for(w, fs.BTC, tag="btc", base="100")
        w.report("klines_1m", symbols=(fs.BTC, fs.ETH), listing=True)
        old_manifest = (
            w.builder()
            .build(FIRST_SLICE_UNIVERSE, w.spec(), "klines_1m", fs.DAY_START, fs.DAY_END)
            .manifest
        )
    fs.ingest_trades_for(w, fs.ETH, tag="eth")  # ETHUSDT aggTrades at the current version
    fs.ingest_bars_for(w, fs.ETH, tag="eth", base="200")
    w.report("klines_1m", symbols=(fs.BTC, fs.ETH), listing=True)
    both = {old, CONTRACT_SCHEMA_VERSION}
    versions = _versions(w)
    for name in (
        c.ARCHIVES.table,
        c.TRADES.table,
        c.BARS.table,
        c.RESPONSES.table,
        c.EVIDENCE.table,
    ):
        assert versions[name] == both, name
    for definition in (c.TRADES, c.BARS):
        for row in w.h.rows(definition):
            expected = old if row["symbol"] == "BTC-USDT" else CONTRACT_SCHEMA_VERSION
            assert row["contract_schema_version"] == expected

    assumed = w.spec(availability_bindings=_ASSUMED, skip=(DATASET_SELECTIONS.table,))
    for symbol, version in ((fs.BTC, old), (fs.ETH, CONTRACT_SCHEMA_VERSION)):
        out = _select(w, assumed, "agg_trades", symbol)
        out.require_no_conflict()
        assert {item.status.value for item in out.selections} == {"selected"}
        assert {r.schema_version for rs in out.records.values() for r in rs} == {version}
        assert {row["contract_schema_version"] for row in out.selected_rows.values()} == {version}

    # The earlier manifest still loads; a new dataset over both symbols is a current manifest.
    assert w.builder().manifests().load(old_manifest.content_hash()) == old_manifest
    spec = w.spec(skip=(DATASET_SELECTIONS.table,))  # the dataset table is never an input
    built = w.builder().build(FIRST_SLICE_UNIVERSE, spec, "klines_1m", fs.DAY_START, fs.DAY_END)
    assert not built.replayed and built.manifest.schema_version == CONTRACT_SCHEMA_VERSION
    assert {m.schema_version for m in built.manifest.members} == {CONTRACT_SCHEMA_VERSION}
    assert {row["revision_id"] for row in built.selection.rows} >= {
        row["revision_id"]
        for row in w.h.rows(c.BARS)
        if row["lineage_raw_table"] == (c.ARCHIVE_KLINES.table)
    }
    store = ManifestStore(w.h.adapter, w.builder())
    assert store.load(built.manifest.content_hash()) == built.manifest
    assert store.load(old_manifest.content_hash()) == old_manifest

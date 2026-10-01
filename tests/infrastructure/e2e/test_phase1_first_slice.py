"""Phase 1 G1 end-to-end acceptance (roadmap #20): archive -> Raw -> Canonical -> PIT ->
Research Dataset + manifest -> Representation, for BTCUSDT and ETHUSDT on one small fixture.

Nothing here fakes a pipeline stage; every step runs the real, already-accepted machinery through
the existing harnesses (``tests/infrastructure/dataset/dataset_support.py`` joins the D3E archive
/ REST harness with the E2 exchangeInfo harness on one catalog; ``canonical_support`` runs the real
normalizer; ``infrastructure.pit`` / ``infrastructure.dataset`` / ``infrastructure.feature`` are the
real F1 / F3 / F4 modules). The only new code is the thin, symbol-parametrized ingestion helpers
below: every reusable wrapper in the existing support modules (``World.trades`` /
``World.bars`` / ``RestHarness.ingest_archive`` / ``canonical_support.ingest_rest``) hard-codes
``BTCUSDT`` and a handful of rows, so a *second* symbol and a klines run long enough to resample
need the same building blocks (``revision_support.archive``, ``rest_support.queue_*_chain`` /
``*_request``, ``RestHarness.collect`` / ``store``) called with an explicit symbol instead.

Two tests:

- ``test_phase1_first_slice_archive_to_representation``: the full walk (1-6 in the roadmap #20
  batch brief) ending in a materialized, manifested Research Dataset and a reproducible F4 feature
  over E4-resampled bars, bound to the persisted manifest by ``feature_request_from_derived_bars``
  (F4-R2).
- ``test_pit_selection_with_and_without_the_archive_assumption``: the same archive-only revision
  selected under a conservative spec (ABSENT before the store's ingest time) and under a spec that
  binds ``infrastructure.pit.assumption.ASSUMPTION_BINDING`` (ADR-0032; SELECTED shortly after the
  trade's event time), proving the two PIT behaviours side by side.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from core.contracts.feature import FeatureRequest
from core.contracts.revision import PointInTimeStatus
from core.domain.base import Kind, Ref, canonical_json
from infrastructure.canonical import rules
from infrastructure.canonical.resample import resample_bars
from infrastructure.catalog.phase1_tables import (
    DATA_QUALITY_REPORTS,
    DATASET_MANIFESTS,
    DATASET_SELECTIONS,
    QUALITY_EVIDENCE_GAPS,
)
from infrastructure.dataset.manifests import ManifestStore, manifest_row
from infrastructure.feature.dataset import DatasetBindingError, feature_request_from_derived_bars
from infrastructure.feature.observations import FeatureInputBuildError, derived_bar_observations
from infrastructure.feature.runner import run_feature
from infrastructure.pit.assumption import ASSUMPTION_BINDING, ASSUMPTION_LATENCY
from infrastructure.pit.selector import PitSelector
from infrastructure.quality.reporter import evidence_gaps_of
from infrastructure.revision.exchange_info_availability import EXCHANGE_INFO_AVAILABILITY_BINDING
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE
from plugins.features import BarLogReturnProvider
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.e2e.first_slice_support import (
    BTC,
    DAY_END,
    DAY_START,
    ETH,
    KLINE_COUNT,
    ingest_archive_for,
    ingest_bars_for,
    ingest_trades_for,
)
from tests.infrastructure.revision import exchange_info_support as xs
from tests.infrastructure.revision import rest_store_support as ss

FIVE_MINUTE_BAR: Ref = Ref(kind=Kind.REPRESENTATION, name="canonical_resample_5m", version="1.0.0")


# =========================================================================================
# the end-to-end walk (roadmap #20)
# =========================================================================================


def test_phase1_first_slice_archive_to_representation(w: ds.World) -> None:
    # ---- step 2: exchangeInfo snapshots (mock transport) -> E2 listing derivation ----
    w.listed(ds.TRADING, ds.L1)

    # ---- step 1: archive ingest (D2) -> REST tail ingest (D3E) -> channel reconciliation
    # (D-33 edges) -> Canonical normalization (E1) of every unit, both symbols, both data types ----
    w.trades()  # BTCUSDT aggTrades (archive + REST + normalize + reconcile)
    ingest_trades_for(w, ETH, tag="eth")  # ETHUSDT aggTrades
    ingest_bars_for(w, BTC, tag="btc", base="100")  # BTCUSDT 1m klines
    ingest_bars_for(w, ETH, tag="eth", base="200")  # ETHUSDT 1m klines

    # The archive copy wins every D-33 edge (equal content, archive supersedes REST): the
    # Canonical head of every key is the archive-lineage revision.
    def _archive_rows(table: Any, symbol: str, raw_table: str) -> list[dict[str, Any]]:
        return [
            row
            for row in w.h.rows(table)
            if row["symbol"] == symbol and row["lineage_raw_table"] == raw_table
        ]

    btc_archive_bars = _archive_rows(c.BARS, "BTC-USDT", c.ARCHIVE_KLINES.table)
    eth_archive_bars = _archive_rows(c.BARS, "ETH-USDT", c.ARCHIVE_KLINES.table)
    assert len(btc_archive_bars) == len(eth_archive_bars) == KLINE_COUNT
    btc_archive_trades = _archive_rows(c.TRADES, "BTC-USDT", c.ARCHIVE_AGGS.table)
    eth_archive_trades = _archive_rows(c.TRADES, "ETH-USDT", c.ARCHIVE_AGGS.table)
    assert len(btc_archive_trades) == len(eth_archive_trades) == 3

    # ---- step 3: quality reports (E3; rule 2.0.0 with the gap table) for every covered
    # partition, and the listing-history report ----
    trade_report_ids = w.report("agg_trades", symbols=(BTC, ETH), listing=False)
    bar_report_ids = w.report("klines_1m", symbols=(BTC, ETH), listing=True)
    assert len(trade_report_ids) == 2  # BTC/DAY, ETH/DAY
    assert len(bar_report_ids) == 3  # BTC/DAY, ETH/DAY, the listing-history report
    reported = {row["report_id"] for row in w.h.rows(DATA_QUALITY_REPORTS)}
    assert set(trade_report_ids) | set(bar_report_ids) <= reported

    # ---- step 5: F2 universe -> an F3 v2 dataset in the production research.dataset_selections,
    # with the manifest persisted to research.dataset_manifests. ADR-0077 DQ-10 refuses a new v2
    # build, so the first build is seeded as a pre-cutoff one; the rebuild below is the public
    # DatasetBuilder.build replay ----
    expected_bindings = w.bindings()  # every upstream table currently holding a snapshot
    spec = w.spec()  # SIM point simulation; binds exactly `expected_bindings`
    assert spec.snapshot_bindings == expected_bindings
    built = ds.seed_historical_v2(
        w.builder(), FIRST_SLICE_UNIVERSE, spec, "klines_1m", DAY_START, DAY_END
    )
    manifest = built.manifest

    assert [ds.symbol_of(m) for m in manifest.members] == ["BTC-USDT", "ETH-USDT"]
    assert manifest.exclusions == ()
    assert manifest.universe_spec == FIRST_SLICE_UNIVERSE.binding()
    dataset_rows = w.h.rows_at(DATASET_SELECTIONS.table, built.dataset_commit.snapshot_id)
    assert sorted(r["revision_id"] for r in dataset_rows) == sorted(
        r["revision_id"] for r in (*btc_archive_bars, *eth_archive_bars)
    )
    assert {r["selection_id"] for r in dataset_rows} == {built.selection.selection_id}
    assert manifest.dataset.table == DATASET_SELECTIONS.table
    assert manifest.dataset.snapshot_id == built.dataset_commit.snapshot_id
    assert (manifest.dataset.time_range_start, manifest.dataset.time_range_end) == (
        DAY_START,
        DAY_END,
    )

    # The manifest binds every upstream snapshot the spec bound (ADR-0023 §6 / ADR-0024 §6).
    assert manifest.point_in_time.snapshot_bindings == expected_bindings
    assert manifest.point_in_time == spec

    # Lineage: listings (the observing snapshot) + bars (archive -> archive -> archive).
    lineage_tables = {
        (item.canonical_table, item.raw_table, item.source_table) for item in manifest.lineage
    }
    assert lineage_tables == {
        (xs.LISTINGS.table, xs.EXCHANGE_INFO.table, xs.EXCHANGE_INFO.table),
        (c.BARS.table, c.ARCHIVE_KLINES.table, c.ARCHIVES.table),
    }

    # Quality: BTC + ETH klines partitions of DAY and the listing report, each re-derived.
    assert len(manifest.quality_report_ids) == 3
    assert set(manifest.quality_report_ids) <= reported

    # Evidence gaps: every bound gap is recorded, with the same text, by the report it cites.
    gap_tables = sorted({gap.table for gap in manifest.evidence_gaps})
    assert gap_tables and set(gap_tables) <= {xs.LISTINGS.table, c.BARS.table}
    for gap in manifest.evidence_gaps:
        recorded = {
            (g["table"], g["revision_id"]): g["gap"]
            for g in evidence_gaps_of(w.h.adapter, gap.quality_report_id)
        }
        assert recorded[(gap.table, gap.revision_id)] == gap.gap
    assert w.h.head(QUALITY_EVIDENCE_GAPS.table) is not None

    # Persisted: one row, re-proven on load.
    [manifest_db_row] = w.h.rows(DATASET_MANIFESTS)
    assert manifest_db_row == manifest_row(manifest)
    assert ManifestStore(w.h.adapter, w.builder()).load(manifest.content_hash()) == manifest
    assert not built.replayed

    # A rebuild (fresh process: a reopened catalog adapter) is bit-identical and replays.
    w.h.reopen()
    rebuilt = w.builder().build(FIRST_SLICE_UNIVERSE, spec, "klines_1m", DAY_START, DAY_END)
    assert canonical_json(rebuilt.manifest.model_dump(mode="json")) == canonical_json(
        manifest.model_dump(mode="json")
    )
    assert rebuilt.replayed
    assert rebuilt.dataset_commit.snapshot_id == built.dataset_commit.snapshot_id
    assert len(w.h.rows(DATASET_MANIFESTS)) == 1

    # ---- step 6: E4 resample 5-minute bars from the selection, and an F4 feature run
    # through run_feature (a stable result_hash across re-runs; no value beyond its eval time) ----
    selection = PitSelector(
        w.h.adapter, w.h.storage, canonical_scratch_directory=w.h.canonical_scratch_directory
    ).select(spec, "klines_1m", BTC, DAY_START, DAY_END)
    selection.require_no_conflict()
    bars5 = resample_bars(selection, 5, DAY_START, DAY_END)
    assert [bar.complete for bar in bars5] == [True, True, False]  # the 22:15 bucket is a gap
    observations = derived_bar_observations(bars5, selection, spec)
    assert len(observations) == 2  # only the complete buckets: nothing is filled or interpolated

    feature = BarLogReturnProvider.spec(
        available_lag=timedelta(minutes=1), bar_input=FIVE_MINUTE_BAR
    )
    assert spec.simulation_time is not None
    sim_time = spec.simulation_time
    eval_time = sim_time + feature.available_lag

    # The run is bound to the persisted manifest (F4-R2): the manifest is loaded and re-derived
    # through the builder's ManifestStore, and the 5-minute bars are proven to be exactly
    # resample_bars of the manifest's own dataset selection.
    def dataset_request(
        items: Any, evaluation_times: tuple[Any, ...] = (eval_time,)
    ) -> FeatureRequest:
        return feature_request_from_derived_bars(
            w.h.adapter,
            w.h.storage,
            builder=w.builder(),
            manifest_content_hash=manifest.content_hash(),
            pit_spec=spec,
            minutes=5,
            observations=items,
            feature=feature,
            evaluation_times=evaluation_times,
        )

    request = dataset_request(observations)
    assert request.manifest_content_hash == manifest.content_hash()
    # Mechanical no-lookahead: the visible set never includes an observation later than the
    # evaluation time allows, and every value's latest input honours the lag (F4 runner + contract).
    for item in request.visible_at(eval_time, feature.available_lag):
        assert item.available_time + feature.available_lag <= eval_time
    result_first = run_feature(BarLogReturnProvider((feature,)), feature, request)
    [value] = result_first.values
    assert value.value is not None  # two contiguous complete 5-minute bars: a real log return
    assert value.latest_input_available_time is not None
    assert value.latest_input_available_time + feature.available_lag <= value.evaluation_time

    # A point spec proves visibility only as of its own simulation_time: an earlier evaluation
    # time is refused outright rather than silently answered from a later view.
    with pytest.raises(FeatureInputBuildError, match="does not cover"):
        dataset_request(observations, (sim_time - timedelta(days=1),))
    # Derived bars that are not exactly resample_bars of the dataset never make a request.
    with pytest.raises(DatasetBindingError, match="not exactly"):
        dataset_request(observations[:1])

    # Stable result_hash across re-runs, including across a fresh process (reopened catalog).
    result_second = run_feature(BarLogReturnProvider((feature,)), feature, request)
    assert result_second == result_first and result_second.result_hash == result_first.result_hash

    w.h.reopen()
    selection_again = PitSelector(
        w.h.adapter, w.h.storage, canonical_scratch_directory=w.h.canonical_scratch_directory
    ).select(spec, "klines_1m", BTC, DAY_START, DAY_END)
    bars5_again = resample_bars(selection_again, 5, DAY_START, DAY_END)
    observations_again = derived_bar_observations(bars5_again, selection_again, spec)
    request_again = dataset_request(observations_again)
    assert request_again.model_dump_json() == request.model_dump_json()
    result_third = run_feature(BarLogReturnProvider((feature,)), feature, request_again)
    assert result_third.result_hash == result_first.result_hash


# =========================================================================================
# PIT selection two ways (ADR-0032, roadmap #20 step 4)
# =========================================================================================


def test_pit_selection_with_and_without_the_archive_assumption(w: ds.World) -> None:
    """An archive-only trade: invisible before the store's ingest time under the conservative
    spec, visible shortly after its event time once the spec binds ``ASSUMPTION_BINDING``."""
    items = ss.agg_items(1, first_id=900, first_ms=ss.T0)
    archive_revision = ingest_archive_for(
        w.h,
        "agg_trades",
        BTC,
        ss.archive_agg_lines(items),
        knowledge=ds.K_A,
        request_id="archive-assume",
    )
    c.normalizer(w.h, clock=ss.StepClock(start=ds.N_A)).normalize_unit(
        c.ARCHIVE_AGGS.table, archive_revision
    )
    trade_at = ss.utc(2023, 11, 14, 22, 14)  # ss.T0
    selector = PitSelector(
        w.h.adapter, w.h.storage, canonical_scratch_directory=w.h.canonical_scratch_directory
    )

    conservative = w.spec(at=trade_at + timedelta(hours=1))
    conservative_out = selector.select(conservative, "agg_trades", BTC, ds.START, ds.END)
    [conservative_selection] = conservative_out.selections
    assert conservative_selection.status is PointInTimeStatus.ABSENT
    assert conservative_out.assumed == {}

    with_assumption = (
        rules.AVAILABILITY_BINDING,
        EXCHANGE_INFO_AVAILABILITY_BINDING,
        ASSUMPTION_BINDING,
    )
    before_latency = w.spec(
        at=trade_at + ASSUMPTION_LATENCY - timedelta(microseconds=1),
        availability_bindings=with_assumption,
    )
    before_out = selector.select(before_latency, "agg_trades", BTC, ds.START, ds.END)
    [before_selection] = before_out.selections
    assert before_selection.status is PointInTimeStatus.ABSENT

    after_latency = w.spec(at=trade_at + ASSUMPTION_LATENCY, availability_bindings=with_assumption)
    after_out = selector.select(after_latency, "agg_trades", BTC, ds.START, ds.END)
    [after_selection] = after_out.selections
    assert after_selection.status is PointInTimeStatus.SELECTED
    [row] = w.h.rows(c.TRADES)
    effective = trade_at + ASSUMPTION_LATENCY
    assert after_out.assumed == {row["revision_id"]: (row["available_time"], effective)}
    # The stored row is unchanged; only the PIT view's effective time moved (ADR-0032).
    assert w.h.rows(c.TRADES)[0]["available_time"] == row["available_time"]

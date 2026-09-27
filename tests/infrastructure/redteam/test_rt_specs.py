"""G2 / PIT specs that lie: stale snapshots, snapshots of another table, unknown policy hashes.

Each spec is built from a genuine one by changing one binding. The build must refuse it before
writing anything: the dataset's own tables keep their heads.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from core.contracts.catalog import SnapshotNotFound
from core.contracts.revision import PointInTimeSpec, PolicyBinding
from infrastructure.canonical.listings import UnconstructibleReason
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_EXCHANGE_INFO,
    CANONICAL_INSTRUMENT_LISTINGS,
    QUALITY_EVIDENCE_GAPS,
)
from infrastructure.dataset.builder import DatasetSpecError
from infrastructure.pit.assumption import ASSUMPTION_BINDING
from infrastructure.pit.selector import PitConflictError, PitSpecError
from infrastructure.quality.reporter import QualityReportMissing
from infrastructure.universe.builder import UniverseSpecError, UniverseUnconstructible
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import L1, L2, World
from tests.infrastructure.redteam import redteam_support as rt
from tests.infrastructure.revision import rest_store_support as ss


def _refused(w: World, spec: PointInTimeSpec, error: Any, match: str | None = None) -> None:
    before = rt.outputs(w)
    with pytest.raises(error, match=match):
        rt.build(w, spec)
    assert rt.outputs(w) == before


def _rebind(spec: PointInTimeSpec, **tables: str) -> PointInTimeSpec:
    bindings = {**spec.snapshot_bindings, **tables}
    return spec.model_copy(update={"snapshot_bindings": bindings})


def _first(w: World, table: str) -> str:
    return w.h.history(table)[0].snapshot_id


# =========================================================================================
# snapshots of another table / that do not exist
# =========================================================================================


def test_a_snapshot_of_another_table_is_not_found(w: World) -> None:
    w.listed()
    w.trades()
    w.report()
    spec = w.spec()
    foreign = spec.snapshot_bindings[CANONICAL_INSTRUMENT_LISTINGS.table]
    _refused(w, _rebind(spec, **{c.TRADES.table: foreign}), SnapshotNotFound)
    _refused(w, _rebind(spec, **{c.EVIDENCE.table: foreign}), SnapshotNotFound)
    _refused(w, _rebind(spec, **{c.TRADES.table: "not-a-snapshot"}), SnapshotNotFound)


# =========================================================================================
# stale snapshots (each one genuinely existed, but not together with the others)
# =========================================================================================


def _bars(w: World) -> None:
    """Archive + REST klines of DAY, normalized and reconciled (their own request ids)."""
    items = ss.kline_items(2)
    archive = c.ingest_archive(
        w.h, "klines_1m", ss.archive_kline_lines(items), knowledge=ds.K_A, request_id="arc-bars"
    )
    [response] = c.ingest_rest(w.h, "klines_1m", items, knowledge=ds.K_R, request_id="req-bars")
    rt.normalize(w, c.ARCHIVE_KLINES.table, archive, at=ds.N_A)
    rt.normalize(w, c.REST_KLINES.table, response, at=ds.N_R)
    w.h.reconciler(clock=ss.StepClock(start=ds.K_E)).reconcile("klines_1m", ss.SYMBOL, ss.DAY)


def test_a_stale_evidence_snapshot_turns_every_trade_into_a_conflict(w: World) -> None:
    w.listed()
    _bars(w)  # the first evidence snapshot: kline edges only
    stale = w.h.head(c.EVIDENCE.table)
    w.trades()  # the trade edges land in a later evidence snapshot
    w.report()
    assert stale is not None and stale != w.h.head(c.EVIDENCE.table)
    _refused(w, _rebind(w.spec(), **{c.EVIDENCE.table: stale}), PitConflictError)


def test_a_stale_listing_snapshot_behind_its_exchange_info_snapshot(w: World) -> None:
    w.listed(ds.TRADING, L1)
    stale = w.h.head(CANONICAL_INSTRUMENT_LISTINGS.table)
    w.listed(ds.ETH_HALT, L2)
    w.trades()
    w.report()
    spec = _rebind(w.spec(), **{CANONICAL_INSTRUMENT_LISTINGS.table: str(stale)})
    before = rt.outputs(w)
    with pytest.raises(UniverseUnconstructible) as caught:
        rt.build(w, spec)
    assert caught.value.reason == UnconstructibleReason.LISTING_NOT_DERIVED
    assert rt.outputs(w) == before


def test_an_exchange_info_snapshot_behind_its_listing_snapshot(w: World) -> None:
    w.listed(ds.TRADING, L1)
    stale = _first(w, BINANCE_SPOT_EXCHANGE_INFO.table)
    w.listed(ds.ETH_HALT, L2)
    w.trades()
    w.report()
    spec = _rebind(w.spec(), **{BINANCE_SPOT_EXCHANGE_INFO.table: stale})
    _refused(w, spec, CatalogIntegrityError, "not in the proven history")


def test_a_stale_evidence_gap_snapshot_behind_its_reports(w: World) -> None:
    w.listed()
    w.trades()
    w.report()
    stale = w.h.head(QUALITY_EVIDENCE_GAPS.table)
    w.more_trades()
    w.report()  # the new BTC report's gaps land in a later snapshot
    assert stale is not None and stale != w.h.head(QUALITY_EVIDENCE_GAPS.table)
    spec = _rebind(w.spec(), **{QUALITY_EVIDENCE_GAPS.table: stale})
    _refused(w, spec, CatalogIntegrityError, "evidence-gap batch")


def test_a_stale_raw_element_snapshot_under_a_newer_canonical_snapshot(w: World) -> None:
    w.listed()
    items = ss.agg_items(3)
    archive = rt.archived(rt.archive_trades(w, ss.archive_agg_lines(items), knowledge=ds.K_A))
    [response] = rt.rest_trades(w, items, knowledge=ds.K_R, element_microbatch_rows=1)
    rt.normalize(w, c.ARCHIVE_AGGS.table, archive, at=ds.N_A)
    rt.normalize(w, c.REST_AGGS.table, response, at=ds.N_R)
    rt.reconcile(w, at=ds.K_E)
    w.report()
    stale = _first(w, c.REST_AGGS.table)  # the first REST element only
    _refused(w, _rebind(w.spec(), **{c.REST_AGGS.table: stale}), CatalogIntegrityError)


def test_a_stale_canonical_snapshot_has_no_report_of_its_own(w: World) -> None:
    w.listed()
    w.trades()
    stale = _first(w, c.TRADES.table)  # archive rows only, before the REST unit
    w.report()
    _refused(w, _rebind(w.spec(), **{c.TRADES.table: stale}), QualityReportMissing)


# =========================================================================================
# unknown policy versions / hashes
# =========================================================================================


def _forge(binding: PolicyBinding) -> PolicyBinding:
    return binding.model_copy(update={"policy_hash": "f" * 64})


def _swap(field: str, policy_id: str) -> Callable[[PointInTimeSpec], PointInTimeSpec]:
    def change(spec: PointInTimeSpec) -> PointInTimeSpec:
        bindings = tuple(
            _forge(item) if item.policy_id == policy_id else item for item in getattr(spec, field)
        )
        assert bindings != getattr(spec, field), policy_id
        return spec.model_copy(update={field: bindings})

    return change


def _pit(spec: PointInTimeSpec) -> PointInTimeSpec:
    return spec.model_copy(update={"point_in_time_binding": _forge(spec.point_in_time_binding)})


def _assumption(spec: PointInTimeSpec) -> PointInTimeSpec:
    forged = _forge(ASSUMPTION_BINDING)
    return spec.model_copy(update={"availability_bindings": (*spec.availability_bindings, forged)})


def _dropped_normalizer(spec: PointInTimeSpec) -> PointInTimeSpec:
    parsers = tuple(item for item in spec.parser_bindings if "normalizer" not in item.policy_id)
    return spec.model_copy(update={"parser_bindings": parsers})


FORGERIES: dict[str, Callable[[PointInTimeSpec], PointInTimeSpec]] = {
    "pit-rule": _pit,
    "canonical-availability": _swap("availability_bindings", "hlens.canonical.availability"),
    "exchange-info-availability": _swap(
        "availability_bindings", "binance.spot.exchange-info-publication"
    ),
    "assumption": _assumption,
    "delivery-channel": _swap("precedence_bindings", "binance.spot.delivery-channel"),
    "precedence-map": _swap("precedence_bindings", "hlens.canonical.precedence-map"),
    "listing-observation": _swap("precedence_bindings", "binance.spot.listing-observation"),
    "normalizer": _swap("parser_bindings", "hlens.canonical.binance-spot.normalizer"),
    "listing-status": _swap("parser_bindings", "binance.spot.listing-status"),
    "normalizer-missing": _dropped_normalizer,
}


@pytest.mark.parametrize("name", sorted(FORGERIES))
def test_an_unknown_policy_hash_or_a_missing_policy_is_refused(w: World, name: str) -> None:
    w.listed()
    w.trades()
    w.report()
    genuine = w.spec()
    forged = FORGERIES[name](genuine)
    assert forged.content_hash() != genuine.content_hash()
    _refused(w, forged, (DatasetSpecError, PitSpecError, UniverseSpecError))
    assert rt.build(w, genuine).manifest.point_in_time == genuine  # the genuine one still builds

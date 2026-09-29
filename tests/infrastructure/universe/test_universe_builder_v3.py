"""``UniverseBuilder``'s v3 cursor entry (ADR-0077 §6.1.1; B0a of the R2 slicing).

Mirrors ``tests/infrastructure/dataset/test_universe.py``'s scenarios but drives
``UniverseBuilder.cursor()`` instead of ``build()``, and checks:

1. **parity** -- the v3 cursor's members / exclusions / listing lineage / member spans / evidence
   gaps are exactly the same *content* as v2's fully-materialized ``UniverseBuilt`` (set equality;
   ``Contract`` models are frozen and hashable) for the same ``(spec, pit)``;
2. **fail-closed timing** -- a structurally invalid spec / pit is refused eagerly, at
   ``cursor()``, exactly like v2's ``build()``; an unconstructible point-in-time listing is only
   discovered lazily, once a view is actually iterated (the cursor cannot know this without
   walking the pinned view);
3. **explicit close** -- each view is independently closable, on exhaustion, an early stop, or an
   exception, without disturbing the other views of the same cursor;
4. **no whole-table read** -- the v3 path never calls ``PinnedCatalogView.scan_columns`` (the
   whole-table read ADR-0077 §6.1 item 1 forbids); only bounded ``scan_column_batches``.

``build()`` / ``UniverseBuilt`` themselves are untouched by this slice and stay covered by
``test_universe.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.contracts.universe import (
    ExclusionReason,
    SelectedRevisionLineage,
    UniverseExclusion,
    UniverseMember,
)
from infrastructure.canonical.listings import ListingDeriver, UnconstructibleReason
from infrastructure.catalog.phase1_tables import CANONICAL_INSTRUMENT_LISTINGS
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.universe import builder as ub
from infrastructure.universe.builder import (
    FIRST_SLICE_UNIVERSE,
    UniverseSpanCursor,
    UniverseSpecError,
    UniverseUnconstructible,
)
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import L1, L2, L3, SIM, World

LISTINGS = CANONICAL_INSTRUMENT_LISTINGS.table

BTC_HALT = {"BTCUSDT": "HALT", "ETHUSDT": "TRADING"}


@pytest.fixture
def w(tmp_path: Path) -> Iterator[World]:
    with ds.sqlite_world(tmp_path) as opened:
        yield opened


def _members(cursor: UniverseSpanCursor) -> tuple[UniverseMember, ...]:
    with cursor.members() as members:
        return tuple(members)


def _exclusions(cursor: UniverseSpanCursor) -> tuple[UniverseExclusion, ...]:
    with cursor.exclusions() as exclusions:
        return tuple(exclusions)


def _lineage(cursor: UniverseSpanCursor) -> tuple[SelectedRevisionLineage, ...]:
    with cursor.listing_lineage() as lineage:
        return tuple(lineage)


def _member_spans(cursor: UniverseSpanCursor) -> tuple[tuple[str, object, object], ...]:
    with cursor.member_spans() as spans:
        return tuple(spans)


def _gaps(cursor: UniverseSpanCursor) -> tuple[tuple[str, str], ...]:
    with cursor.evidence_gaps() as gaps:
        return tuple(gaps)


# ============================================================================ parity with v2


def test_members_at_a_point_matches_v2(w: World) -> None:
    w.listed()
    spec, pit = FIRST_SLICE_UNIVERSE, w.spec()
    built = w.universe().build(spec, pit)
    cursor = w.universe().cursor(spec, pit)

    assert set(_members(cursor)) == set(built.members)
    assert set(_exclusions(cursor)) == set(built.exclusions) == set()
    assert set(_lineage(cursor)) == set(built.lineage)
    assert set(_gaps(cursor)) == set(built.evidence_gaps)

    v3_spans: dict[str, list[tuple[object, object]]] = {}
    for symbol, start, end in _member_spans(cursor):
        v3_spans.setdefault(symbol, []).append((start, end))
    assert {symbol: tuple(spans) for symbol, spans in v3_spans.items()} == built.member_spans


def test_halt_and_resume_interval_matches_v2(w: World) -> None:
    """ADR-0024 #3, ADR-0077 §6.1.1: suspended = excluded, resumed = the same episode again."""
    w.listed(ds.TRADING, L1)
    w.listed(BTC_HALT, L2)
    w.listed(ds.TRADING, L3)
    spec, pit = FIRST_SLICE_UNIVERSE, w.spec(interval=(L1, SIM))
    built = w.universe().build(spec, pit)
    cursor = w.universe().cursor(spec, pit)

    v3_members = _members(cursor)
    assert set(v3_members) == set(built.members)
    btc = [m for m in v3_members if ds.symbol_of(m) == "BTC-USDT"]
    assert sorted((m.effective_from, m.effective_until) for m in btc) == [(L1, L2), (L3, SIM)]

    v3_exclusions = _exclusions(cursor)
    assert set(v3_exclusions) == set(built.exclusions)
    [halted] = v3_exclusions
    assert (halted.effective_from, halted.effective_until) == (L2, L3)
    assert halted.reason is ExclusionReason.NOT_TRADABLE

    assert set(_lineage(cursor)) == set(built.lineage)
    # BTC: listed / halted / resumed (3 distinct revisions) + ETH: unchanged throughout (1).
    assert len(_lineage(cursor)) == 4

    v3_spans: dict[str, list[tuple[object, object]]] = {}
    for symbol, start, end in _member_spans(cursor):
        v3_spans.setdefault(symbol, []).append((start, end))
    assert v3_spans["BTCUSDT"] == [(L1, L2), (L3, SIM)]
    assert {symbol: tuple(spans) for symbol, spans in v3_spans.items()} == built.member_spans
    # Exclusion spans never leak into member_spans (mirrors v2's UniverseBuilt.member_spans).
    assert (L2, L3) not in v3_spans["BTCUSDT"]


def test_the_knowledge_axis_hides_later_derivations_v3(w: World) -> None:
    """ADR-0024 #5: an early cutoff does not see a later halt, even once the snapshot binding
    moves past it -- the knowledge axis, not which snapshot is bound, is what hides it."""
    w.listed(ds.TRADING, L1)
    early_cutoff = w.x.clock.now - timedelta(microseconds=1)
    spec = FIRST_SLICE_UNIVERSE
    early = _members(w.universe().cursor(spec, w.spec(cutoff=early_cutoff)))
    w.listed(BTC_HALT, L2)
    again = _members(w.universe().cursor(spec, w.spec(cutoff=early_cutoff)))
    assert set(again) == set(early) and len(early) == 2
    late = w.universe().cursor(spec, w.spec())
    assert [ds.symbol_of(e) for e in _exclusions(late)] == ["BTC-USDT"]


# ============================================================================ fail-closed timing


def test_unregistered_specs_and_missing_bindings_fail_closed_eagerly(w: World) -> None:
    """Structural refusals (ADR-0024 §5) happen at ``cursor()``, exactly like v2's ``build()``:
    no view needs to be opened for these to raise."""
    w.listed()
    renamed = FIRST_SLICE_UNIVERSE.model_copy(update={"symbols": ("BTCUSDT",)})
    with pytest.raises(UniverseSpecError, match="not registered"):
        w.universe().cursor(renamed, w.spec())
    with pytest.raises(UniverseSpecError, match="listing history is missing"):
        w.universe().cursor(FIRST_SLICE_UNIVERSE, w.spec(skip=(LISTINGS,)))


def test_before_the_first_observation_fails_closed_lazily(w: World) -> None:
    """ADR-0024 #6 / ADR-0029 #6: the cursor itself is constructed fine (it is structurally
    valid); only walking a view discovers the unconstructible listing, never today's list."""
    w.listed(ds.TRADING, L2)
    cursor = w.universe().cursor(FIRST_SLICE_UNIVERSE, w.spec(at=L2 - timedelta(microseconds=1)))
    with pytest.raises(UniverseUnconstructible) as caught:
        _members(cursor)
    assert caught.value.reason == UnconstructibleReason.NO_VISIBLE_LISTING
    # A second, independent view over the same cursor re-walks and fails the same way.
    with pytest.raises(UniverseUnconstructible):
        _exclusions(cursor)


# ============================================================================ explicit close


def test_each_view_is_independent_and_closable_early(w: World) -> None:
    w.listed(ds.TRADING, L1)
    w.listed(BTC_HALT, L2)
    w.listed(ds.TRADING, L3)
    cursor = w.universe().cursor(FIRST_SLICE_UNIVERSE, w.spec(interval=(L1, SIM)))

    with cursor.members() as members:
        first = next(members)
        assert isinstance(first, UniverseMember)
        # Stops here without exhausting `members`: __exit__ must still close it cleanly.

    # A fresh view from the same cursor is unaffected by the earlier early stop.
    assert len(_members(cursor)) == 3
    assert len(_exclusions(cursor)) == 1
    assert len(_lineage(cursor)) == 4  # BTC: 3 revisions + ETH: 1 unchanged revision


def test_close_releases_the_deriver_exactly_once(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    w.listed(ds.TRADING, L1)
    w.listed(BTC_HALT, L2)
    cursor = w.universe().cursor(FIRST_SLICE_UNIVERSE, w.spec(interval=(L1, SIM)))

    closes: list[ListingDeriver] = []
    original = ListingDeriver.close

    def counted(self: ListingDeriver) -> None:
        closes.append(self)
        original(self)

    monkeypatch.setattr(ListingDeriver, "close", counted)

    with cursor.members() as members:
        next(members)  # stop early, mid-walk
    assert len(closes) == 1

    closes.clear()
    with cursor.exclusions() as exclusions:
        list(exclusions)  # drain to exhaustion
    assert len(closes) == 1

    closes.clear()
    with pytest.raises(UniverseUnconstructible):
        with (
            w.universe()
            .cursor(FIRST_SLICE_UNIVERSE, w.spec(at=L1 - timedelta(days=1)))
            .members() as members
        ):
            list(members)
    assert len(closes) == 1


# ============================================================================ no whole-table read


def test_instants_v3_never_calls_whole_table_scan_columns(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0077 §6.1 item 1: ``_instants_v3`` must read via bounded ``scan_column_batches``,
    never a whole-table ``scan_columns(...).to_pylist()`` (what v2's ``_instants`` still does).

    White-box, scoped to ``_instants_v3`` itself: the wider cursor walk also drives
    ``ListingDeriver`` / ``ExchangeInfoRowVerifier``, existing v2-shared machinery outside this
    slice's boundary (``infrastructure/universe/builder.py`` only) that legitimately still calls
    ``scan_columns`` for its own proving logic, so asserting it is never called across a whole
    ``cursor()`` walk would be both untrue and out of scope.
    """
    w.listed(ds.TRADING, L1)
    w.listed(BTC_HALT, L2)
    w.listed(ds.TRADING, L3)
    pit = w.spec(interval=(L1, SIM))
    view = PinnedCatalogView(w.h.adapter, pit.snapshot_bindings)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("_instants_v3 must not call the whole-table scan_columns")

    monkeypatch.setattr(PinnedCatalogView, "scan_columns", forbidden)

    instants = ub._instants_v3(view, pit)
    assert instants[0] == L1
    assert len(instants) >= 3  # L1, the halt at L2, the resume at L3 are all change points


def test_instants_v3_closes_its_batch_readers_on_normal_completion(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-FIX: ``_fold_exchange_info_changes`` / ``_fold_listing_changes`` used to close their
    ``scan_column_batches`` reader only on an exception (``except BaseException: ...close();
    raise``), never after an ordinary, fully-exhausted loop -- leaking the reader on every
    successful call. Both folds must close their reader exactly once on that ordinary path too
    (a ``try/finally``, not a bare ``except``).
    """
    w.listed()
    pit = w.spec(interval=(L1, SIM))
    view = PinnedCatalogView(w.h.adapter, pit.snapshot_bindings)
    closed: list[str] = []

    class _FakeReader:
        """One empty batch, then exhausted; records whether ``close`` was ever called."""

        def __init__(self, table: str) -> None:
            self._table = table
            self._batches = iter([SimpleNamespace(to_pylist=lambda: [])])

        def __iter__(self) -> _FakeReader:
            return self

        def __next__(self) -> SimpleNamespace:
            return next(self._batches)

        def close(self) -> None:
            closed.append(self._table)

    def fake_scan_column_batches(self: PinnedCatalogView, table: str, **kwargs: Any) -> _FakeReader:
        return _FakeReader(table)

    monkeypatch.setattr(PinnedCatalogView, "scan_column_batches", fake_scan_column_batches)
    ub._instants_v3(view, pit)
    assert closed.count(ub.EXCHANGE_INFO_TABLE) == 1
    assert closed.count(ub.LISTINGS_TABLE) == 1

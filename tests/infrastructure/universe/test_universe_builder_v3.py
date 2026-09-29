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
from infrastructure.dataset.sources import UniverseRunParams
from infrastructure.pit.runs import RunLimits
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.universe import builder as ub
from infrastructure.universe.builder import (
    FIRST_SLICE_UNIVERSE,
    UniverseSpanCursor,
    UniverseSpecError,
    UniverseUnconstructible,
)
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import L1, L2, L3, ORIGIN, SIM, World

LISTINGS = CANONICAL_INSTRUMENT_LISTINGS.table
RUN_PARAMS = UniverseRunParams(
    capacity=1,
    merge_fanout=2,
    limits=RunLimits(leaf_max_records=1, leaf_max_bytes=4096, fanout=2),
)

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


def _cursor(w: World, spec: Any, pit: Any) -> UniverseSpanCursor:
    return w.universe().cursor(spec, pit, run_params=RUN_PARAMS)


# ============================================================================ parity with v2


def test_members_at_a_point_matches_v2(w: World) -> None:
    w.listed()
    spec, pit = FIRST_SLICE_UNIVERSE, w.spec()
    built = w.universe().build(spec, pit)
    cursor = _cursor(w, spec, pit)

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
    cursor = _cursor(w, spec, pit)

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
    early = _members(_cursor(w, spec, w.spec(cutoff=early_cutoff)))
    w.listed(BTC_HALT, L2)
    again = _members(_cursor(w, spec, w.spec(cutoff=early_cutoff)))
    assert set(again) == set(early) and len(early) == 2
    late = _cursor(w, spec, w.spec())
    assert [ds.symbol_of(e) for e in _exclusions(late)] == ["BTC-USDT"]


# ============================================================================ fail-closed timing


def test_unregistered_specs_and_missing_bindings_fail_closed_eagerly(w: World) -> None:
    """Structural refusals (ADR-0024 §5) happen at ``cursor()``, exactly like v2's ``build()``:
    no view needs to be opened for these to raise."""
    w.listed()
    renamed = FIRST_SLICE_UNIVERSE.model_copy(update={"symbols": ("BTCUSDT",)})
    with pytest.raises(UniverseSpecError, match="not registered"):
        _cursor(w, renamed, w.spec())
    with pytest.raises(UniverseSpecError, match="listing history is missing"):
        _cursor(w, FIRST_SLICE_UNIVERSE, w.spec(skip=(LISTINGS,)))


def test_before_the_first_observation_fails_closed_lazily(w: World) -> None:
    """ADR-0024 #6 / ADR-0029 #6: the cursor itself is constructed fine (it is structurally
    valid); only walking a view discovers the unconstructible listing, never today's list."""
    w.listed(ds.TRADING, L2)
    cursor = _cursor(w, FIRST_SLICE_UNIVERSE, w.spec(at=L2 - timedelta(microseconds=1)))
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
    cursor = _cursor(w, FIRST_SLICE_UNIVERSE, w.spec(interval=(L1, SIM)))

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
    cursor = _cursor(w, FIRST_SLICE_UNIVERSE, w.spec(interval=(L1, SIM)))

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
        with _cursor(
            w, FIRST_SLICE_UNIVERSE, w.spec(at=L1 - timedelta(days=1))
        ).members() as members:
            list(members)
    assert len(closes) == 1


def test_lineage_and_gap_dedup_is_disk_backed_and_closes_on_early_exit(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w.listed(ds.TRADING, L1)
    cursor = _cursor(w, FIRST_SLICE_UNIVERSE, w.spec())
    unique_count = 128
    ids = [f"revision-{index:04d}" for index in range(unique_count)]
    event_sources_closed: list[bool] = []
    stores: list[ub._FirstSeenStore] = []
    store_type = ub._FirstSeenStore

    class TrackingStore(store_type):
        def __init__(self) -> None:
            super().__init__()
            stores.append(self)

    def events() -> Iterator[ub._SpanEvent]:
        try:
            for revision_id in (*ids, *reversed(ids)):
                lineage = ds.listing_lineage(revision_id)
                yield ub._SpanEvent(
                    venue_symbol="BTCUSDT",
                    member=None,
                    exclusion=None,
                    listing_revision_id=revision_id,
                    lineage=lineage,
                    gap=ds.GAP_TEXT,
                )
        finally:
            event_sources_closed.append(True)

    monkeypatch.setattr(ub, "_FirstSeenStore", TrackingStore)
    monkeypatch.setattr(cursor, "_events", events)

    with cursor.listing_lineage() as lineage:
        assert [item.canonical_revision_id for item in lineage] == ids
    first_store = stores[-1]
    assert first_store.count == unique_count
    assert first_store.CACHE_KIB == 1024
    assert first_store.closed and not first_store.path.exists()

    with cursor.evidence_gaps() as gaps:
        assert list(gaps) == [(revision_id, ds.GAP_TEXT) for revision_id in ids]
    second_store = stores[-1]
    assert second_store.count == unique_count
    assert second_store.closed and not second_store.path.exists()

    with cursor.listing_lineage() as lineage:
        assert next(lineage).canonical_revision_id == ids[0]
    early_store = stores[-1]
    assert early_store.closed and not early_store.path.exists()

    def failing_events() -> Iterator[ub._SpanEvent]:
        try:
            yield ub._SpanEvent(
                venue_symbol="BTCUSDT",
                member=None,
                exclusion=None,
                listing_revision_id=ids[0],
                lineage=ds.listing_lineage(ids[0]),
                gap=ds.GAP_TEXT,
            )
            raise RuntimeError("event source failed")
        finally:
            event_sources_closed.append(True)

    monkeypatch.setattr(cursor, "_events", failing_events)
    with pytest.raises(RuntimeError, match="event source failed"):
        with cursor.evidence_gaps() as gaps:
            list(gaps)
    failed_store = stores[-1]
    assert failed_store.closed and not failed_store.path.exists()
    assert len(event_sources_closed) == 4


def test_repeated_selection_of_one_listing_revision_keeps_its_gap(w: World) -> None:
    """A listing revision's availability gap is stored in its immutable RevisionRecord.

    Repeated exchange-info snapshots can produce multiple timeline evaluation points while
    selecting the same canonical listing revision. The PIT deriver reads that revision's one
    canonical row each time, so its gap cannot change from absent to present (or vice versa).
    """
    w.listed(ds.TRADING, L1)
    w.listed(ds.TRADING, L2)  # unchanged status: no new canonical listing revision
    pit = w.spec(interval=(L1, SIM))
    view = PinnedCatalogView(w.h.adapter, pit.snapshot_bindings)
    deriver = ListingDeriver(view, w.h.storage, market_data_base_url=ORIGIN)
    try:
        first = deriver.listing_at("BTCUSDT", L1, pit.knowledge_cutoff, pit=pit)
        repeated = deriver.listing_at("BTCUSDT", L2, pit.knowledge_cutoff, pit=pit)
    finally:
        deriver.close()

    assert first.listing is not None and repeated.listing is not None
    assert first.listing.revision.revision_id == repeated.listing.revision.revision_id
    assert first.evidence_gap is not None
    assert first.evidence_gap == repeated.evidence_gap
    assert first.evidence_gap == first.listing.revision.availability.evidence_gap
    assert repeated.evidence_gap == repeated.listing.revision.availability.evidence_gap


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

    instants = ub._instants_v3(view, pit, w.h.storage, RUN_PARAMS)
    with instants.open() as values:
        materialized = tuple(values)
    assert materialized[0] == L1
    assert len(materialized) >= 3  # L1, the halt at L2, the resume at L3 are all change points


def test_point_in_time_instants_remain_a_singleton_without_run_parameters(w: World) -> None:
    w.listed(ds.TRADING, L1)
    pit = w.spec(at=SIM)
    replay = ub._instants_v3(
        PinnedCatalogView(w.h.adapter, pit.snapshot_bindings), pit, w.h.storage, None
    )
    with replay.open() as instants:
        assert tuple(instants) == (SIM,)


def test_interval_cursor_requires_explicit_run_parameters(w: World) -> None:
    w.listed(ds.TRADING, L1)
    with pytest.raises(UniverseSpecError, match="explicit UniverseRunParams"):
        w.universe().cursor(FIRST_SLICE_UNIVERSE, w.spec(interval=(L1, SIM)))


def test_instants_v3_closes_its_batch_readers_on_normal_completion(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both snapshot readers close once after their ordered event scans are drained."""
    w.listed(ds.TRADING, L1)
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
    ub._instants_v3(view, pit, w.h.storage, RUN_PARAMS)
    assert closed.count(ub.EXCHANGE_INFO_TABLE) == 1
    assert closed.count(ub.LISTINGS_TABLE) == 1


def test_instants_v3_sorts_batches_filters_cutoff_and_treats_end_as_right_open(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two pinned source scans may be unordered; event time is externally sorted once."""
    w.listed(ds.TRADING, L1)
    t1, t2, t3 = (L1 + timedelta(minutes=i) for i in (1, 2, 3))
    too_late = SIM + timedelta(microseconds=1)
    cutoff = SIM
    pit = w.spec(interval=(L1, SIM), cutoff=cutoff)
    batches = {
        ub.EXCHANGE_INFO_TABLE: [
            [{"retrieved_at": t3, "knowledge_time": cutoff}],
            [
                {"retrieved_at": t1, "knowledge_time": too_late},
                {"retrieved_at": t2, "knowledge_time": cutoff},
                {"retrieved_at": L1, "knowledge_time": cutoff},
                {"retrieved_at": SIM, "knowledge_time": cutoff},
            ],
        ],
        ub.LISTINGS_TABLE: [
            [
                {
                    "available_time": t2,
                    "knowledge_time": cutoff,
                    "tradable_intervals": [{"tradable_from": t2, "tradable_until": t3}],
                }
            ],
            [],
        ],
    }
    readers: list[Any] = []

    class _Reader:
        def __init__(self, table: str) -> None:
            self._batches = iter(
                [SimpleNamespace(to_pylist=lambda rows=rows: rows) for rows in batches[table]]
            )
            self.closed = 0
            readers.append(self)

        def __iter__(self) -> _Reader:
            return self

        def __next__(self) -> SimpleNamespace:
            return next(self._batches)

        def close(self) -> None:
            self.closed += 1

    monkeypatch.setattr(
        PinnedCatalogView,
        "scan_column_batches",
        lambda self, table, **kwargs: _Reader(table),
    )
    replay = ub._instants_v3(
        PinnedCatalogView(w.h.adapter, pit.snapshot_bindings), pit, w.h.storage, RUN_PARAMS
    )
    with replay.open() as instants:
        result = tuple(instants)
    assert result == (L1, t2, t3)
    assert SIM not in result  # Interval end is right-open, even when a source reports it.
    assert len(readers) == 2
    assert all(reader.closed == 1 for reader in readers)


def test_instants_v3_replay_closes_the_run_reader_early(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each symbol's event pass owns a closable run reader even when the consumer stops early."""
    from contextlib import contextmanager

    w.listed(ds.TRADING, L1)
    pit = w.spec(interval=(L1, SIM))
    event_times = [L1 + timedelta(minutes=i) for i in range(1, 5)]

    class _Reader:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self._batches = iter([SimpleNamespace(to_pylist=lambda: rows)])
            self.closed = 0

        def __iter__(self) -> _Reader:
            return self

        def __next__(self) -> SimpleNamespace:
            return next(self._batches)

        def close(self) -> None:
            self.closed += 1

    exchange_reader = _Reader(
        [{"retrieved_at": at, "knowledge_time": SIM} for at in reversed(event_times)]
    )
    listing_reader = _Reader([])
    monkeypatch.setattr(
        PinnedCatalogView,
        "scan_column_batches",
        lambda self, table, **kwargs: (
            exchange_reader if table == ub.EXCHANGE_INFO_TABLE else listing_reader
        ),
    )
    closed_runs: list[bool] = []
    original_iter_run = ub.iter_run

    @contextmanager
    def tracked_iter_run(storage: Any, root: Any) -> Iterator[Iterator[Any]]:
        with original_iter_run(storage, root) as records:
            try:
                yield records
            finally:
                closed_runs.append(True)

    monkeypatch.setattr(ub, "iter_run", tracked_iter_run)
    replay = ub._instants_v3(
        PinnedCatalogView(w.h.adapter, pit.snapshot_bindings), pit, w.h.storage, RUN_PARAMS
    )
    assert exchange_reader.closed == listing_reader.closed == 1
    with replay.open() as instants:
        assert next(instants) == L1
        assert next(instants) == event_times[0]
    assert closed_runs == [True]


def test_instants_v3_closes_the_active_batch_reader_on_error(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w.listed(ds.TRADING, L1)
    pit = w.spec(interval=(L1, SIM))
    closed: list[str] = []

    class _Batch:
        def to_pylist(self) -> list[dict[str, Any]]:
            raise RuntimeError("broken batch")

    class _Reader:
        def __iter__(self) -> _Reader:
            return self

        def __next__(self) -> _Batch:
            return _Batch()

        def close(self) -> None:
            closed.append("exchange")

    monkeypatch.setattr(
        PinnedCatalogView,
        "scan_column_batches",
        lambda self, table, **kwargs: _Reader(),
    )
    with pytest.raises(RuntimeError, match="broken batch"):
        ub._instants_v3(
            PinnedCatalogView(w.h.adapter, pit.snapshot_bindings), pit, w.h.storage, RUN_PARAMS
        )
    assert closed == ["exchange"]


def test_instants_v3_run_buffer_never_exceeds_explicit_capacity(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A structural bound check: flushes never hold more than the caller's row capacity."""
    w.listed(ds.TRADING, L1)
    pit = w.spec(interval=(L1, SIM))
    event_times = [L1 + timedelta(minutes=i) for i in range(1, 33)]
    batches = iter(
        [
            SimpleNamespace(
                to_pylist=lambda rows=[{"retrieved_at": at, "knowledge_time": SIM}]: rows
            )
            for at in event_times
        ]
    )

    class _Reader:
        def __init__(self, first: Any | None) -> None:
            self._first = first

        def __iter__(self) -> _Reader:
            return self

        def __next__(self) -> Any:
            if self._first is not None:
                first, self._first = self._first, None
                return first
            return next(batches)

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        PinnedCatalogView,
        "scan_column_batches",
        lambda self, table, **kwargs: (
            _Reader(None) if table == ub.LISTINGS_TABLE else _Reader(next(batches))
        ),
    )
    capacities: list[int] = []
    original_builder = ub.RunSetBuilder

    class _TrackingBuilder(original_builder):
        def _flush(self) -> None:
            capacities.append(len(self._rows))
            super()._flush()

    monkeypatch.setattr(ub, "RunSetBuilder", _TrackingBuilder)
    params = UniverseRunParams(
        capacity=3,
        merge_fanout=2,
        limits=RunLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2),
    )
    replay = ub._instants_v3(
        PinnedCatalogView(w.h.adapter, pit.snapshot_bindings), pit, w.h.storage, params
    )
    with replay.open() as instants:
        result = tuple(instants)
    assert result == (L1, *event_times)
    assert capacities and max(capacities) <= params.capacity
    assert sum(capacities) == len(event_times)

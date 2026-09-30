"""The v3 dataset builder over its real upstreams (ADR-0077 §2 / §6.1; B-ADAPT / B-FIX).

``infrastructure.dataset.sources`` adapts B-UNIV's ``UniverseSpanCursor`` and B-PIT's
``PitSelector.iter_bounded`` to the builder's ordered Protocols. End to end, over the v2 fixture
world (real stores, normalizer, reconciler, listing derivation and reports), the v3 build selects
exactly what v2's ``DatasetBuilder.select`` selects, and is proven by a real
``build -> persist -> load_any -> verify`` round trip: the real ``IcebergChunkWriter`` (B3) commits
chunks to ``research.dataset_selection_chunks`` and the real ``DatasetEvidenceManifestStore`` /
``StreamingEvidenceVerifier`` (B4 / B-VERIFY) persist and re-verify the manifest against
``research.dataset_evidence_manifests``; evidence objects and sorted runs go to the world's real
object store. The pure universe-reordering / fail-closed tests further below still build over
``dataset_support``'s in-memory ``FakeChunkWriter`` / ``FakeManifests`` stand-ins, since they drive
the builder's own validation over synthetic sources, not the real chunk / manifest tables. Every
run / rule size is an arbitrary small value (DQ-9 OPEN), chosen so that every stream spills into
several runs and merges in more than one pass.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from core.contracts.revision import PointInTimeSpec, PointInTimeStatus
from core.contracts.universe import (
    AvailabilityEvidenceGap,
    DatasetQualityReportRef,
    EvidenceStream,
    PitConflictHeadEvidence,
    SelectedRevisionLineage,
    UniverseExclusion,
    UniverseMember,
)
from core.domain.base import Contract
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    CANONICAL_INSTRUMENT_LISTINGS,
    DATASET_SELECTION_CHUNKS,
)
from infrastructure.dataset.builder import (
    DatasetEvidenceBuilder,
    DatasetEvidenceRequest,
    DatasetEvidenceSources,
    PinnedQualityEvidence,
    dataset_evidence_rule,
)
from infrastructure.dataset.chunks import IcebergChunkWriter
from infrastructure.dataset.evidence import iter_pit_conflict_heads
from infrastructure.dataset.manifests import ManifestStore
from infrastructure.dataset.sources import (
    OrderedUniverseSource,
    PitSelectorKeySource,
    UniverseRunParams,
    dataset_evidence_sources,
    pit_key_groups,
)
from infrastructure.dataset.verify_v3 import StreamingEvidenceVerifier
from infrastructure.pit.runs import RunLimits
from infrastructure.pit.selector import (
    PIT_BINDING,
    EvidenceGap,
    PitBoundedRecord,
    PitBoundedSelection,
    PitConflictError,
    PitRunParams,
    PitSelector,
)
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE
from infrastructure.universe.run_params import UniverseRunParams as SharedUniverseRunParams
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import END, L1, SIM, START, World
from tests.infrastructure.revision.rest_store_support import utc

MEMBERS = EvidenceStream.MEMBERS
EXCLUSIONS = EvidenceStream.EXCLUSIONS
LINEAGE = EvidenceStream.LINEAGE
GAPS = EvidenceStream.EVIDENCE_GAPS
REPORTS = EvidenceStream.QUALITY_REPORTS
LISTINGS = CANONICAL_INSTRUMENT_LISTINGS.table
NO_SPAN = datetime.min.replace(tzinfo=UTC)
SLICE_22 = utc(2023, 11, 14, 22)

#: Arbitrary small sizes (not a DQ-9 choice): one row per run, pairwise merges, one-row leaves.
RUN_LIMITS = RunLimits(leaf_max_records=1, leaf_max_bytes=1 << 16, fanout=2)
PIT_PARAMS = PitRunParams(
    row_batch_rows=1,
    edge_batch_rows=1,
    merge_fanout=2,
    key_history_buffer=1,
    limits=RUN_LIMITS,
)
UNIVERSE_PARAMS = UniverseRunParams(capacity=1, merge_fanout=2, limits=RUN_LIMITS)
RULE: dict[str, int] = {
    "chunk_rows": 2,
    "leaf_max_records": 2,
    "leaf_max_bytes": 1 << 14,
    "fanout": 2,
}


# ------------------------------------------------------------------ helpers


def _builder(storage: Any, adapter: Any = None) -> DatasetEvidenceBuilder:
    return DatasetEvidenceBuilder(
        ds.fake_heads() if adapter is None else adapter,
        storage,
        rule=dataset_evidence_rule(**RULE),
    )


def _request(spec: PointInTimeSpec, data_type: str = "agg_trades") -> DatasetEvidenceRequest:
    return DatasetEvidenceRequest(
        universe=FIRST_SLICE_UNIVERSE, pit=spec, data_type=data_type, start=START, end=END
    )


def _stream(b: DatasetEvidenceBuilder, manifest: Any, which: EvidenceStream) -> list[Contract]:
    with b.iter_evidence(manifest, which) as records:
        return list(records)


def _entry_order(entry: UniverseMember | UniverseExclusion) -> tuple[str, datetime]:
    start = NO_SPAN if entry.effective_from is None else entry.effective_from
    return entry.episode.observation_key(), start


def _row_content(row: Any) -> tuple[Any, ...]:
    """A dataset row without its selection id and ordinals (v2 and v3 ids differ by rule)."""
    return (
        row["canonical_table"],
        row["symbol"],
        row["observation_key"],
        row["revision_id"],
        row["event_time"],
        row["effective_from"],
        row["effective_until"],
    )


def _sources_factory(w: World) -> Any:
    """A ``DatasetEvidenceSourcesFactory``: the real upstreams, freshly built per request (as
    ``StreamingEvidenceVerifier`` needs for its own re-derivation pass)."""

    def factory(request: DatasetEvidenceRequest) -> DatasetEvidenceSources:
        return dataset_evidence_sources(
            w.h.adapter,
            w.h.storage,
            request,
            market_data_base_url=ds.ORIGIN,
            pit_params=PIT_PARAMS,
            universe_params=UNIVERSE_PARAMS,
        )

    return factory


def _chunk_rows(w: World, selection_id: str) -> list[dict[str, Any]]:
    """The selection's committed chunk rows, from the real ``DATASET_SELECTION_CHUNKS`` table."""
    prefix_rows = [
        row for row in w.h.rows(DATASET_SELECTION_CHUNKS) if row["selection_id"] == selection_id
    ]
    return sorted(prefix_rows, key=lambda row: row["row_ordinal"])


def _v3_build(
    w: World, spec: PointInTimeSpec
) -> tuple[DatasetEvidenceBuilder, Any, IcebergChunkWriter]:
    """Build over the real B-UNIV / B-PIT upstreams, and the real B3 chunk table / B4 manifest
    table + B-VERIFY streaming verifier: ``build`` (which itself persists and verifies) followed
    by an explicit ``load_any`` -- re-verifying -- through the dispatching v2/v3 ``ManifestStore``
    (ADR-0077 §8.2), exactly as a real caller would round-trip a v3 manifest.
    """
    request = _request(spec)
    b = _builder(w.h.storage, w.h.adapter)
    sources_factory = _sources_factory(w)
    chunks = IcebergChunkWriter(w.h.adapter, DATASET_SELECTION_CHUNKS)
    verifier = StreamingEvidenceVerifier(
        w.h.adapter, builder=b, chunks=chunks, sources=sources_factory
    )
    manifests = verifier.store()

    summary = b.build(request, sources=sources_factory(request), chunks=chunks, manifests=manifests)

    both = ManifestStore(w.h.adapter, w.builder(), evidence_verifier=verifier)
    assert both.load_any(summary.manifest_hash) == summary.manifest
    return b, summary, chunks


def _point_world(w: World) -> PointInTimeSpec:
    w.listed()
    w.trades()
    w.report()
    return w.spec()


def _interval_world(w: World) -> PointInTimeSpec:
    """BTC a member throughout; ETH a member until its halt on 11-18, then excluded."""
    w.listed(ds.TRADING, L1)
    w.trades()
    w.listed(ds.ETH_HALT, utc(2023, 11, 18))
    w.report()
    return w.spec(interval=(utc(2023, 11, 15), SIM))


# ------------------------------------------------------------------ end to end vs v2


@pytest.mark.parametrize("world", [_point_world, _interval_world], ids=["point", "interval"])
def test_v3_build_over_the_real_upstreams_selects_what_v2_selects(w: World, world: Any) -> None:
    spec = world(w)
    v2 = w.builder().select(FIRST_SLICE_UNIVERSE, spec, "agg_trades", START, END)
    b, summary, _chunks = _v3_build(w, spec)
    manifest = summary.manifest

    # Members and exclusions: v2's, in the ADR-0077 §2 order.
    assert _stream(b, manifest, MEMBERS) == sorted(v2.universe.members, key=_entry_order)
    assert _stream(b, manifest, EXCLUSIONS) == sorted(v2.universe.exclusions, key=_entry_order)

    # Rows: the same rows (v3 adds contiguous ordinals), read back from the real chunk table.
    rows = _chunk_rows(w, summary.selection_id)
    assert summary.row_count == len(rows) == len(v2.rows) > 0
    assert sorted(map(_row_content, rows)) == sorted(map(_row_content, v2.rows))
    assert [row["row_ordinal"] for row in rows] == list(range(len(rows)))

    # Lineage: listing lineage first, by canonical_revision_id (v2 sorts it the same way), then
    # the data lineage of exactly v2's selected revisions.
    lineage = cast(list[SelectedRevisionLineage], _stream(b, manifest, LINEAGE))
    listing = [item for item in lineage if item.canonical_table == LISTINGS]
    assert lineage[: len(listing)] == listing == list(v2.universe.lineage)
    ids = [item.canonical_revision_id for item in listing]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    v2_data = [item for item in v2.lineage if item.canonical_table != LISTINGS]
    assert sorted(lineage[len(listing) :], key=lambda item: item.canonical_revision_id) == v2_data

    # Gaps and reports: the same bindings.
    gaps = cast(list[AvailabilityEvidenceGap], _stream(b, manifest, GAPS))

    def gap_order(gap: AvailabilityEvidenceGap) -> tuple[str, str]:
        return gap.table, gap.revision_id

    assert sorted(gaps, key=gap_order) == sorted(v2.evidence_gaps, key=gap_order)
    reports = cast(list[DatasetQualityReportRef], _stream(b, manifest, REPORTS))
    assert sorted(item.report_id for item in reports) == sorted(v2.quality_report_ids)


def test_listing_lineage_and_gaps_of_the_real_cursor_are_reordered(w: World) -> None:
    spec = _interval_world(w)  # three listing revisions over two symbols
    cursor = w.universe().cursor(FIRST_SLICE_UNIVERSE, spec, run_params=UNIVERSE_PARAMS)
    with cursor.listing_lineage() as raw_lineage, cursor.evidence_gaps() as raw_gaps:
        generated, generated_gaps = list(raw_lineage), list(raw_gaps)
    source = OrderedUniverseSource(cursor, storage=w.h.storage, params=UNIVERSE_PARAMS)
    with source.listing_lineage() as ordered, source.evidence_gaps() as ordered_gaps:
        lineage, gaps = list(ordered), list(ordered_gaps)
    assert len(generated) >= 3
    assert lineage == sorted(generated, key=lambda item: item.canonical_revision_id)
    assert gaps == sorted(generated_gaps)
    with cursor.member_spans() as raw_spans, source.member_spans() as spans:
        assert list(spans) == list(raw_spans)  # passed through unchanged


def test_owner_and_event_times_are_the_canonical_rows_times(w: World) -> None:
    spec = _point_world(w)
    selector = PitSelector(w.h.adapter, w.h.storage)
    source = PitSelectorKeySource(selector, storage=w.h.storage, params=PIT_PARAMS)
    with source.keys(spec, "agg_trades", "BTCUSDT", SLICE_22, END) as groups:
        got = [(g.observation_key, g.owner_event_time, tuple(g.evaluations)) for g in groups]
    earliest: dict[str, datetime] = {}
    for row in w.h.rows(c.TRADES):
        key = row["observation_key"]
        earliest[key] = min(earliest.get(key, row["event_time"]), row["event_time"])
    assert [key for key, _, _ in got] == sorted(earliest)  # every key of 22:14 owned here
    legacy = selector.select(spec, "agg_trades", "BTCUSDT", SLICE_22, END)
    for key, owner, evaluations in got:
        assert owner == earliest[key] and SLICE_22 <= owner < END
        [evaluation] = evaluations  # a point simulation
        assert evaluation.status is PointInTimeStatus.SELECTED and evaluation.selected is not None
        revision = evaluation.selected.revision_id
        assert evaluation.selected.event_time == legacy.selected_rows[revision]["event_time"]
        assert evaluation.selected.lineage in legacy.lineage
    with source.keys(spec, "agg_trades", "BTCUSDT", START, SLICE_22) as groups:
        assert list(groups) == []  # the 21:00 slice owns nothing


class _KeysReversed(PitSelector):
    """A selector whose bounded stream yields its keys in descending order."""

    def iter_bounded(
        self,
        spec: PointInTimeSpec,
        data_type: str,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        params: PitRunParams,
        touching: bool = False,
        conflict_sink: Any = None,
    ) -> Any:
        return self._reversed(spec, data_type, symbol, start, end, params, touching, conflict_sink)

    @contextmanager
    def _reversed(
        self,
        spec: PointInTimeSpec,
        data_type: str,
        symbol: str,
        start: datetime,
        end: datetime,
        params: PitRunParams,
        touching: bool,
        conflict_sink: Any,
    ) -> Iterator[Iterator[PitBoundedRecord]]:
        with super().iter_bounded(
            spec,
            data_type,
            symbol,
            start,
            end,
            params=params,
            touching=touching,
            conflict_sink=conflict_sink,
        ) as records:
            held = list(records)
        keys = sorted({record.observation_key for record in held}, reverse=True)
        yield iter([record for key in keys for record in held if record.observation_key == key])


def test_pit_keys_out_of_order_fail_the_real_build_closed(w: World) -> None:
    spec = _point_world(w)
    request = _request(spec)
    sources = DatasetEvidenceSources(
        universe=OrderedUniverseSource(
            w.universe().cursor(FIRST_SLICE_UNIVERSE, spec, run_params=UNIVERSE_PARAMS),
            storage=w.h.storage,
            params=UNIVERSE_PARAMS,
        ),
        pit=PitSelectorKeySource(
            _KeysReversed(w.h.adapter, w.h.storage), storage=w.h.storage, params=PIT_PARAMS
        ),
        quality=PinnedQualityEvidence(
            w.h.adapter, w.h.storage, spec, "agg_trades", market_data_base_url=ds.ORIGIN
        ),
    )
    chunks = ds.FakeChunkWriter()
    with pytest.raises(CatalogIntegrityError, match="out of order"):
        _builder(w.h.storage, w.h.adapter).build(
            request, sources=sources, chunks=chunks, manifests=ds.FakeManifests()
        )
    assert chunks.sealed == []


# ------------------------------------------------------------------ universe reordering (fakes)


def _scrambled_universe() -> ds.FakeUniverse:
    """Generation order (BTC's revision, then ETH's) that is not revision-id order."""
    return ds.FakeUniverse(
        members_=(ds.v3_member("BTCUSDT", "z-btc"),),
        exclusions_=(ds.v3_exclusion("ETHUSDT", "a-eth"),),
        lineage=(ds.listing_lineage("z-btc"), ds.listing_lineage("a-eth")),
        gaps=(("z-btc", ds.GAP_TEXT), ("a-eth", ds.GAP_TEXT)),
        spans=(("BTCUSDT", None, None),),
    )


def _fake_quality() -> ds.FakeQuality:
    return ds.FakeQuality(
        gaps={
            (ds.V3_LISTING_REPORT, ds.V3_LISTINGS, "z-btc"): ds.GAP_TEXT,
            (ds.V3_LISTING_REPORT, ds.V3_LISTINGS, "a-eth"): ds.GAP_TEXT,
        }
    )


def _fake_pit() -> ds.FakePit:
    return ds.FakePit(groups={("BTCUSDT", ds.V3_SLICE_22): (ds.point_key("k1", "r1"),)})


def test_multi_symbol_listing_lineage_out_of_order_is_reordered(
    evidence_store: LocalFileStorageAdapter,
) -> None:
    raw = _scrambled_universe()
    with pytest.raises(CatalogIntegrityError, match="canonical order"):  # the builder's own check
        _builder(evidence_store).build(
            ds.v3_request(),
            sources=ds.v3_sources(raw, _fake_pit(), _fake_quality()),
            chunks=ds.FakeChunkWriter(),
            manifests=ds.FakeManifests(),
        )
    ordered = OrderedUniverseSource(
        _scrambled_universe(), storage=evidence_store, params=UNIVERSE_PARAMS
    )
    b = _builder(evidence_store)
    summary = b.build(
        ds.v3_request(),
        sources=DatasetEvidenceSources(universe=ordered, pit=_fake_pit(), quality=_fake_quality()),
        chunks=ds.FakeChunkWriter(),
        manifests=ds.FakeManifests(),
    )
    lineage = cast(list[SelectedRevisionLineage], _stream(b, summary.manifest, LINEAGE))
    assert [item.canonical_revision_id for item in lineage] == ["a-eth", "z-btc", "r1"]
    gaps = cast(list[AvailabilityEvidenceGap], _stream(b, summary.manifest, GAPS))
    assert [(gap.table, gap.revision_id) for gap in gaps] == [
        (ds.V3_LISTINGS, "a-eth"),
        (ds.V3_LISTINGS, "z-btc"),
    ]


def test_members_and_exclusions_are_reordered_to_the_episode_key(
    evidence_store: LocalFileStorageAdapter,
) -> None:
    universe = ds.FakeUniverse(
        members_=(ds.v3_member("ETHUSDT", "a-eth"), ds.v3_member("BTCUSDT", "z-btc")),
        exclusions_=(),
        lineage=(ds.listing_lineage("a-eth"), ds.listing_lineage("z-btc")),
        gaps=(),
        spans=(("BTCUSDT", None, None), ("ETHUSDT", None, None)),
    )
    ordered = OrderedUniverseSource(universe, storage=evidence_store, params=UNIVERSE_PARAMS)
    with ordered.members() as members:
        assert [ds.episode_of(item).symbol for item in members] == ["BTCUSDT", "ETHUSDT"]
    b = _builder(evidence_store)
    summary = b.build(
        ds.v3_request(),
        sources=DatasetEvidenceSources(universe=ordered, pit=_fake_pit(), quality=ds.FakeQuality()),
        chunks=ds.FakeChunkWriter(),
        manifests=ds.FakeManifests(),
    )
    members_stream = _stream(b, summary.manifest, MEMBERS)
    assert members_stream == sorted(universe.members_, key=_entry_order)
    assert universe.open_now == 0


def test_duplicates_still_reach_the_builder_and_fail_closed(
    evidence_store: LocalFileStorageAdapter,
) -> None:
    universe = _scrambled_universe()
    universe.lineage = (*universe.lineage, ds.listing_lineage("z-btc"))
    ordered = OrderedUniverseSource(universe, storage=evidence_store, params=UNIVERSE_PARAMS)
    with pytest.raises(CatalogIntegrityError, match="duplicated or out of canonical order"):
        _builder(evidence_store).build(
            ds.v3_request(),
            sources=DatasetEvidenceSources(
                universe=ordered, pit=_fake_pit(), quality=_fake_quality()
            ),
            chunks=ds.FakeChunkWriter(),
            manifests=ds.FakeManifests(),
        )


def test_malformed_universe_items_fail_closed(evidence_store: LocalFileStorageAdapter) -> None:
    universe = _scrambled_universe()
    universe.gaps = (("z-btc", ""),)
    ordered = OrderedUniverseSource(universe, storage=evidence_store, params=UNIVERSE_PARAMS)
    with pytest.raises(CatalogIntegrityError, match="malformed"), ordered.evidence_gaps() as gaps:
        list(gaps)
    universe.exclusions_ = cast(Any, (ds.v3_member("ETHUSDT", "a-eth"),))
    with pytest.raises(CatalogIntegrityError, match="not a UniverseExclusion"):
        with ordered.exclusions() as exclusions:
            list(exclusions)
    assert universe.open_now == 0


def test_universe_run_params_are_required_and_checked() -> None:
    assert UniverseRunParams is SharedUniverseRunParams
    parameters = inspect.signature(UniverseRunParams).parameters.values()
    assert {item.name for item in parameters} == {"capacity", "merge_fanout", "limits"}
    assert all(item.default is inspect.Parameter.empty for item in parameters)
    for bad in (
        {"capacity": 0},
        {"capacity": True},
        {"merge_fanout": 1},
        {"limits": cast(Any, None)},
    ):
        fields: dict[str, Any] = {
            "capacity": 1,
            "merge_fanout": 2,
            "limits": RUN_LIMITS,
            **bad,
        }
        with pytest.raises(ValueError):
            UniverseRunParams(**fields)


# ------------------------------------------------------------------ PIT grouping (pure)


T0, T1, T2 = utc(2023, 11, 14, 22, 1), utc(2023, 11, 14, 22, 2), utc(2023, 11, 14, 22, 3)
H0, H1 = utc(2023, 12, 1), utc(2023, 12, 2)

#: Sentinel: "not given" for ``_record``'s ``event_time`` (distinct from an explicit ``None``,
#: which forces a malformed record that carries no event_time at all).
_UNSET: Any = object()


def _record(
    key: str,
    status: PointInTimeStatus,
    revision: str | None = None,
    *,
    at: datetime = SIM,
    owner: datetime = T0,
    event_time: Any = _UNSET,
    lineage: bool = False,
    gap: str | None = None,
) -> PitBoundedRecord:
    """A ``PitBoundedRecord`` fixture matching what ``PitSelector.iter_bounded`` itself attaches.

    ``owner`` is the key's ``owner_event_time`` (the real generator repeats the same value across
    every record of one key: pass the same ``owner`` for records built as one key's group).
    ``event_time`` defaults to ``at`` for a ``SELECTED`` record (the real generator always
    attaches one) and to ``None`` otherwise; pass it explicitly -- ``None`` included -- to build a
    malformed record.
    """
    heads: tuple[str, ...] = ()
    if status is PointInTimeStatus.SELECTED:
        assert revision is not None
        heads = (revision,)
    elif status is PointInTimeStatus.CONFLICT:
        heads = ("x-1", "x-2")
    attached: SelectedRevisionLineage | None = None
    if lineage and revision is not None:
        attached = ds.trade_lineage(revision)
    evidence_gap: EvidenceGap | None = None
    if gap is not None and revision is not None:
        evidence_gap = EvidenceGap(ds.V3_TRADES, revision, gap)
    resolved_event_time: datetime | None
    if event_time is _UNSET:
        resolved_event_time = at if status is PointInTimeStatus.SELECTED else None
    else:
        resolved_event_time = cast(Any, event_time)
    return PitBoundedRecord(
        observation_key=key,
        selection=PitBoundedSelection(
            observation_key=key,
            simulation_time=at,
            knowledge_cutoff=SIM,
            status=status,
            selected_revision_id=revision if status is PointInTimeStatus.SELECTED else None,
            head_count=len(heads),
        ),
        lineage=attached,
        evidence_gap=evidence_gap,
        owner_event_time=owner,
        event_time=resolved_event_time,
    )


SELECTED = PointInTimeStatus.SELECTED
ABSENT = PointInTimeStatus.ABSENT


def test_groups_carry_owner_event_times_and_lineage_per_key() -> None:
    records = [
        _record(
            "k1", SELECTED, "r2", at=H0, owner=T0, event_time=T1, lineage=True, gap=ds.GAP_TEXT
        ),
        _record("k1", ABSENT, at=H0.replace(hour=6), owner=T0),
        _record("k1", SELECTED, "r2", at=H0.replace(hour=12), owner=T0, event_time=T1),
        _record("k2", SELECTED, "r3", at=H0, owner=T2, event_time=T2, lineage=True),
    ]
    groups = []
    evaluations_by_key = {}
    for group in pit_key_groups(records, knowledge_cutoff=SIM):
        groups.append(group)
        evaluations_by_key[group.observation_key] = list(group.evaluations)
    assert [(g.observation_key, g.owner_event_time) for g in groups] == [("k1", T0), ("k2", T2)]
    first = evaluations_by_key["k1"]
    assert [e.status for e in first] == [SELECTED, ABSENT, SELECTED]
    assert first[0].selected == first[2].selected
    assert first[2].selected is not None
    assert first[2].selected.event_time == T1  # the selected revision's time, not the owner's
    assert first[2].selected.lineage == ds.trade_lineage("r2")
    assert first[2].selected.evidence_gap == ds.GAP_TEXT


@pytest.mark.parametrize(
    ("keys", "match"),
    [
        (("k2", "k1"), "out of order"),
        (("k1", "k2", "k1"), "out of order"),
    ],
)
def test_pit_keys_out_of_order_are_refused(keys: tuple[str, ...], match: str) -> None:
    records = [_record(key, SELECTED, f"r-{key}", owner=T0, lineage=True) for key in keys]
    with pytest.raises(CatalogIntegrityError, match=match):
        for group in pit_key_groups(records, knowledge_cutoff=SIM):
            list(group.evaluations)


@pytest.mark.parametrize(
    ("records", "match"),
    [
        # No lineage attached anywhere for the selected revision.
        ([_record("k1", SELECTED, "r1", owner=T0)], "has no lineage"),
        # The generator always attaches an event_time to a SELECTED record; a malformed stream
        # that omits it must fail closed instead of silently losing the row's time.
        (
            [_record("k1", SELECTED, "r1", owner=T0, lineage=True, event_time=None)],
            "has no event_time",
        ),
        # Every record of one key must agree on owner_event_time (it is repeated, not carried).
        (
            [
                _record("k1", SELECTED, "r1", owner=T0, lineage=True, event_time=T0),
                _record("k1", ABSENT, owner=T1),
            ],
            "disagree on owner_event_time",
        ),
        # owner_event_time / event_time must both be UTC-aware.
        (
            [_record("k1", SELECTED, "r1", owner=T0.replace(tzinfo=None), lineage=True)],
            "UTC",
        ),
        (
            [
                _record(
                    "k1", SELECTED, "r1", owner=T0, lineage=True, event_time=T0.replace(tzinfo=None)
                )
            ],
            "UTC",
        ),
        # A non-SELECTED record must carry neither lineage, a gap, nor an event_time.
        (
            [_record("k1", ABSENT, "r1", owner=T0, lineage=True)],
            "carries lineage",
        ),
        (
            [_record("k1", ABSENT, "r1", owner=T0, event_time=T0)],
            "carries lineage",
        ),
    ],
)
def test_malformed_pit_streams_fail_closed(records: list[PitBoundedRecord], match: str) -> None:
    with pytest.raises(CatalogIntegrityError, match=match):
        for group in pit_key_groups(records, knowledge_cutoff=SIM):
            list(group.evaluations)


def test_a_selection_at_another_cutoff_fails_closed() -> None:
    records = [_record("k1", SELECTED, "r1", owner=T0, lineage=True)]
    with pytest.raises(CatalogIntegrityError, match="another knowledge cutoff"):
        for group in pit_key_groups(records, knowledge_cutoff=H1):
            list(group.evaluations)


def test_one_large_key_streams_evaluations_with_only_one_record_lookahead() -> None:
    count = 10_000
    consumed = 0

    def records() -> Iterator[PitBoundedRecord]:
        nonlocal consumed
        for minute in range(count):
            consumed += 1
            yield _record("large-key", ABSENT, at=SIM + timedelta(minutes=minute), owner=T0)

    groups = pit_key_groups(records(), knowledge_cutoff=SIM)
    group = next(groups)
    assert group.observation_key == "large-key"
    assert consumed == 1

    evaluations = iter(group.evaluations)
    first = next(evaluations)
    assert first.status is ABSENT
    assert consumed == 1  # yielding an evaluation does not prefetch its successor
    assert sum(1 for _ in evaluations) == count - 1
    assert consumed == count


def test_first_interval_conflict_does_not_pull_the_next_conflict_instant() -> None:
    pulled: list[datetime] = []

    def records() -> Iterator[PitBoundedRecord]:
        for at in (H0, H1):
            pulled.append(at)
            yield _record("conflicted-key", PointInTimeStatus.CONFLICT, at=at, owner=T0)

    groups = pit_key_groups(records(), knowledge_cutoff=SIM)
    group = next(groups)
    evaluations = iter(group.evaluations)
    first = next(evaluations)
    assert first.status is PointInTimeStatus.CONFLICT
    assert first.simulation_time == H0
    assert pulled == [H0]

    # A Dataset conflict is terminal. Closing the group stream must not advance it to H1.
    groups.close()
    assert pulled == [H0]


def test_dataset_conflict_seals_only_first_interval_evaluation_and_closes_selector(
    evidence_store: LocalFileStorageAdapter,
) -> None:
    start, middle, end = utc(2023, 12, 1), utc(2023, 12, 2), utc(2023, 12, 3)
    spec = ds.v3_pit(interval=(start, end))
    emitted: list[datetime] = []

    class ConflictSelector(PitSelector):
        active = 0

        def iter_bounded(
            self,
            pit: PointInTimeSpec,
            data_type: str,
            symbol: str,
            low: datetime,
            high: datetime,
            *,
            params: PitRunParams,
            touching: bool = False,
            conflict_sink: Any = None,
        ) -> Any:
            @contextmanager
            def records() -> Iterator[Iterator[PitBoundedRecord]]:
                self.active += 1

                def values() -> Iterator[PitBoundedRecord]:
                    if not low <= T0 < high:
                        return
                    for at in (start, middle):
                        heads = ("head-a", "head-b")
                        assert conflict_sink is not None
                        for ordinal, revision in enumerate(heads):
                            conflict_sink(
                                PitConflictHeadEvidence(
                                    rule_id=PIT_BINDING.policy_id,
                                    rule_version=PIT_BINDING.version,
                                    rule_hash=PIT_BINDING.policy_hash,
                                    observation_key="conflicted-key",
                                    simulation_time=at,
                                    knowledge_cutoff=pit.knowledge_cutoff,
                                    head_count=len(heads),
                                    ordinal=ordinal,
                                    revision_id=revision,
                                )
                            )
                        emitted.append(at)
                        yield _record("conflicted-key", PointInTimeStatus.CONFLICT, at=at, owner=T0)

                try:
                    yield values()
                finally:
                    self.active -= 1

            return records()

    universe = ds.FakeUniverse(
        members_=(ds.v3_member("BTCUSDT", "listing-btc", (start, middle)),),
        exclusions_=(
            ds.v3_exclusion("BTCUSDT", "listing-btc-halt", (middle, end)),
            ds.v3_exclusion("ETHUSDT", "listing-eth", (start, end)),
        ),
        lineage=(
            ds.listing_lineage("listing-btc"),
            ds.listing_lineage("listing-btc-halt"),
            ds.listing_lineage("listing-eth"),
        ),
        gaps=(),
        spans=(("BTCUSDT", start, middle),),
    )
    selector = ConflictSelector(ds.fake_heads(), evidence_store)
    source = PitSelectorKeySource(selector, storage=evidence_store, params=PIT_PARAMS)
    builder = _builder(evidence_store)
    with pytest.raises(PitConflictError) as caught:
        builder.build(
            ds.v3_request(pit=spec),
            sources=DatasetEvidenceSources(universe=universe, pit=source, quality=ds.FakeQuality()),
            chunks=ds.FakeChunkWriter(),
            manifests=ds.FakeManifests(),
        )

    result = caught.value.result
    assert result is not None and result.simulation_time == start
    assert emitted == [start]
    assert selector.active == 0
    with iter_pit_conflict_heads(evidence_store, result, limits=builder.rule.limits) as heads:
        records = list(heads)
    assert [record.revision_id for record in records] == ["head-a", "head-b"]
    assert [record.ordinal for record in records] == [0, 1]

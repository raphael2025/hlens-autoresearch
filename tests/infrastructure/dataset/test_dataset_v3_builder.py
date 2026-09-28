"""The bounded v3 dataset builder (ADR-0077 §1 / §2 / §5): generator, build, fail-closed paths.

The upstream generators (B-UNIV's ``UniverseSpanCursor``, B-PIT's key source) and the chunk /
manifest stores (B3 / B4) are in-memory stand-ins from ``dataset_support``; evidence objects are
written to a real ``LocalFileStorageAdapter``. Limits are arbitrary small values (DQ-9 OPEN).
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from core.contracts.revision import PointInTimeStatus
from core.contracts.universe import (
    AvailabilityEvidenceGap,
    DatasetChunkProof,
    DatasetQualityReportRef,
    EvidenceStream,
    SelectedRevisionLineage,
    UniverseSelectionSpec,
)
from core.domain.base import Contract
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.dataset.builder import (
    DATASET_RULE_VERSION,
    DatasetBuilder,
    DatasetEmpty,
    DatasetEvidenceBuilder,
    DatasetEvidenceRule,
    DatasetQualityError,
    DatasetSpecError,
    PitKeyEvaluation,
    PitKeyGroup,
    PitSelectedRevision,
    dataset_evidence_rule,
    selection_id_for,
)
from infrastructure.dataset.evidence import EvidenceRecordTooLarge
from infrastructure.pit.selector import PitConflictError
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE
from tests.infrastructure.dataset import dataset_support as ds

MEMBERS = EvidenceStream.MEMBERS
EXCLUSIONS = EvidenceStream.EXCLUSIONS
LINEAGE = EvidenceStream.LINEAGE
GAPS = EvidenceStream.EVIDENCE_GAPS
REPORTS = EvidenceStream.QUALITY_REPORTS
PROOFS = EvidenceStream.CHUNK_PROOFS
DERIVED = (EXCLUSIONS, GAPS, LINEAGE, MEMBERS, REPORTS)
#: Arbitrary small parameters (not a DQ-9 choice).
PARAMS: dict[str, int] = {
    "chunk_rows": 2,
    "leaf_max_records": 2,
    "leaf_max_bytes": 4096,
    "fanout": 2,
}


@pytest.fixture
def storage(evidence_store: LocalFileStorageAdapter) -> LocalFileStorageAdapter:
    return evidence_store


def builder(
    storage: LocalFileStorageAdapter,
    heads: dict[str, str | None] | None = None,
    **params: int,
) -> DatasetEvidenceBuilder:
    rule = dataset_evidence_rule(**{**PARAMS, **params})
    return DatasetEvidenceBuilder(ds.fake_heads(heads), storage, rule=rule)


def build(
    storage: LocalFileStorageAdapter,
    universe: ds.FakeUniverse,
    pit: ds.FakePit,
    quality: ds.FakeQuality,
    *,
    request: Any = None,
    chunks: ds.FakeChunkWriter | None = None,
    manifests: ds.FakeManifests | None = None,
    **params: int,
) -> Any:
    return builder(storage, **params).build(
        ds.v3_request() if request is None else request,
        sources=ds.v3_sources(universe, pit, quality),
        chunks=ds.FakeChunkWriter() if chunks is None else chunks,
        manifests=ds.FakeManifests() if manifests is None else manifests,
    )


def stream(b: DatasetEvidenceBuilder, manifest: Any, which: EvidenceStream) -> list[Contract]:
    with b.iter_evidence(manifest, which) as records:
        return list(records)


def report_ids(b: DatasetEvidenceBuilder, manifest: Any) -> list[str]:
    reports = cast(list[DatasetQualityReportRef], stream(b, manifest, REPORTS))
    return [item.report_id for item in reports]


# ------------------------------------------------------------------ build


def test_build_commits_chunks_evidence_and_a_fixed_size_manifest(
    storage: LocalFileStorageAdapter,
) -> None:
    universe, pit, quality = ds.point_scenario()
    chunks, manifests = ds.FakeChunkWriter(), ds.FakeManifests()
    b = builder(storage)
    summary = b.build(
        ds.v3_request(),
        sources=ds.v3_sources(universe, pit, quality),
        chunks=chunks,
        manifests=manifests,
    )
    manifest = summary.manifest
    assert (summary.row_count, summary.chunk_count, summary.replayed_chunk_count) == (3, 2, 0)
    assert not summary.replayed and not summary.manifest_replayed
    assert summary.manifest_hash == manifest.content_hash()
    assert manifest.dataset.table == ds.V3_CHUNK_TABLE
    assert manifest.dataset.snapshot_id == "snap-2"  # the last chunk's snapshot (§4.3)
    assert b.rule.binds(manifest.rule) and manifest.chunk_rows == 2
    assert manifests.stored[summary.selection_id] == manifest
    assert chunks.sealed == [(summary.selection_id, 2)]
    assert {ref.stream: ref.record_count for ref in manifest.evidence} == {
        MEMBERS: 1,
        EXCLUSIONS: 1,
        LINEAGE: 5,
        GAPS: 2,
        REPORTS: 2,
        PROOFS: 2,
    }

    rows = chunks.rows(summary.selection_id)
    assert [row["revision_id"] for row in rows] == ["r1", "r2", "r3"]
    assert [(row["chunk_index"], row["row_ordinal"]) for row in rows] == [(0, 0), (0, 1), (1, 2)]
    assert {row["symbol"] for row in rows} == {"BTC-USDT"}
    assert {(row["effective_from"], row["effective_until"]) for row in rows} == {(None, None)}

    assert stream(b, manifest, MEMBERS) == list(universe.members_)
    assert stream(b, manifest, EXCLUSIONS) == list(universe.exclusions_)
    assert stream(b, manifest, LINEAGE) == [
        ds.listing_lineage("listing-btc"),
        ds.listing_lineage("listing-eth"),
        *(ds.trade_lineage(revision) for revision in ("r1", "r2", "r3")),
    ]
    day_report = ds.partition_report("BTCUSDT", ds.DAY)
    gaps = cast(list[AvailabilityEvidenceGap], stream(b, manifest, GAPS))
    assert [(g.table, g.revision_id, g.quality_report_id, g.gap) for g in gaps] == [
        (ds.V3_LISTINGS, "listing-eth", ds.V3_LISTING_REPORT, ds.GAP_TEXT),
        (ds.V3_TRADES, "r2", day_report, ds.GAP_TEXT),
    ]
    assert report_ids(b, manifest) == [ds.V3_LISTING_REPORT, day_report]
    proofs = cast(list[DatasetChunkProof], stream(b, manifest, PROOFS))
    assert [(p.chunk_index, p.first_row_ordinal, p.row_count) for p in proofs] == [
        (0, 0, 2),
        (1, 2, 1),
    ]
    assert universe.open_now == 0 and pit.open_now == 0


def test_rebuild_replays_and_equal_inputs_give_the_same_manifest(
    storage: LocalFileStorageAdapter, tmp_path: Path
) -> None:
    chunks, manifests = ds.FakeChunkWriter(), ds.FakeManifests()
    first = build(storage, *ds.point_scenario(), chunks=chunks, manifests=manifests)
    again = build(storage, *ds.point_scenario(), chunks=chunks, manifests=manifests)
    assert again.manifest_hash == first.manifest_hash
    assert again.replayed and again.replayed_chunk_count == 2
    elsewhere = build(ds.evidence_storage(tmp_path / "other"), *ds.point_scenario())
    assert elsewhere.manifest_hash == first.manifest_hash


def test_select_derives_exactly_what_build_commits(storage: LocalFileStorageAdapter) -> None:
    chunks = ds.FakeChunkWriter()
    summary = build(storage, *ds.point_scenario(), chunks=chunks)
    b = builder(storage)
    sink = ds.RecordingSink()
    derived = b.select(
        ds.v3_request(), sources=ds.v3_sources(*ds.point_scenario()), sink=sink, manifested=True
    )
    assert (derived.selection_id, derived.row_count) == (summary.selection_id, 3)
    assert dict(derived.record_counts) == {item: len(sink.records[item]) for item in DERIVED}
    assert PROOFS not in sink.records
    for item in DERIVED:
        assert stream(b, summary.manifest, item) == sink.records[item]
    assert sink.rows == chunks.rows(summary.selection_id)


def test_interval_rows_are_selected_spans_gated_by_member_spans(
    storage: LocalFileStorageAdapter,
) -> None:
    t0, t2 = ds.utc(2023, 12, 1), ds.utc(2023, 12, 3)
    t1, half = ds.utc(2023, 12, 2), t0 + timedelta(hours=12)
    universe = ds.FakeUniverse(
        members_=(ds.v3_member("BTCUSDT", "listing-btc", (t0, t1)),),
        exclusions_=(
            ds.v3_exclusion("BTCUSDT", "listing-btc-halt", (t1, t2)),
            ds.v3_exclusion("ETHUSDT", "listing-eth", (t0, t2)),
        ),
        lineage=(
            ds.listing_lineage("listing-btc"),
            ds.listing_lineage("listing-btc-halt"),
            ds.listing_lineage("listing-eth"),
        ),
        gaps=(),
        spans=(("BTCUSDT", t0, t1),),
    )
    k1 = PitKeyGroup(
        observation_key="k1",
        owner_event_time=ds.V3_EVENT,
        evaluations=(
            PitKeyEvaluation(t0, PointInTimeStatus.SELECTED, ds.selected("r1")),
            PitKeyEvaluation(half, PointInTimeStatus.SELECTED, ds.selected("r1b")),
        ),
    )
    k2 = PitKeyGroup(
        observation_key="k2",
        owner_event_time=ds.V3_EVENT,
        evaluations=(
            PitKeyEvaluation(t0, PointInTimeStatus.ABSENT, None),
            PitKeyEvaluation(
                t1 + timedelta(hours=1), PointInTimeStatus.SELECTED, ds.selected("r2")
            ),
        ),
    )
    pit = ds.FakePit(groups={("BTCUSDT", ds.V3_SLICE_22): (k1, k2)})
    chunks = ds.FakeChunkWriter()
    request = ds.v3_request(ds.v3_pit(interval=(t0, t2)))
    summary = build(storage, universe, pit, ds.FakeQuality(), request=request, chunks=chunks)
    rows = chunks.rows(summary.selection_id)
    assert [
        (row["observation_key"], row["revision_id"], row["effective_from"], row["effective_until"])
        for row in rows
    ] == [("k1", "r1", t0, half), ("k1", "r1b", half, t1)]  # k2 selected only while excluded
    lineage = cast(
        list[SelectedRevisionLineage], stream(builder(storage), summary.manifest, LINEAGE)
    )
    assert [item.canonical_revision_id for item in lineage] == [
        "listing-btc",
        "listing-btc-halt",
        "listing-eth",
        "r1",
        "r1b",
    ]


# ------------------------------------------------------------------ fail closed


def test_an_empty_selection_is_refused(storage: LocalFileStorageAdapter) -> None:
    universe, _, quality = ds.point_scenario()
    with pytest.raises(DatasetEmpty):
        build(storage, universe, ds.FakePit(groups={}), quality)


def test_competing_heads_fail_closed(storage: LocalFileStorageAdapter) -> None:
    universe, _, quality = ds.point_scenario()
    pit = ds.FakePit(
        groups={
            ("BTCUSDT", ds.V3_SLICE_22): (
                ds.point_key("k1", "r1"),
                ds.point_key("k2", "r2", status=PointInTimeStatus.CONFLICT),
            )
        }
    )
    with pytest.raises(PitConflictError):
        build(storage, universe, pit, quality)
    assert universe.open_now == 0 and pit.open_now == 0


def test_a_conflict_outside_every_member_span_still_fails_closed(
    storage: LocalFileStorageAdapter,
) -> None:
    t0, t1, t2 = ds.utc(2023, 12, 1), ds.utc(2023, 12, 2), ds.utc(2023, 12, 3)
    universe = ds.FakeUniverse(
        members_=(ds.v3_member("BTCUSDT", "listing-btc", (t0, t1)),),
        exclusions_=(
            ds.v3_exclusion("BTCUSDT", "listing-btc-halt", (t1, t2)),
            ds.v3_exclusion("ETHUSDT", "listing-eth", (t0, t2)),
        ),
        lineage=(
            ds.listing_lineage("listing-btc"),
            ds.listing_lineage("listing-btc-halt"),
            ds.listing_lineage("listing-eth"),
        ),
        gaps=(),
        spans=(("BTCUSDT", t0, t1),),
    )
    late = PitKeyGroup(
        observation_key="k1",
        owner_event_time=ds.V3_EVENT,
        evaluations=(
            PitKeyEvaluation(t0, PointInTimeStatus.SELECTED, ds.selected("r1")),
            PitKeyEvaluation(
                t1 + timedelta(hours=1), PointInTimeStatus.SELECTED, ds.selected("r2")
            ),
            # Reached only after the member spans are exhausted: must still be read.
            PitKeyEvaluation(t1 + timedelta(hours=2), PointInTimeStatus.CONFLICT, None),
        ),
    )
    pit = ds.FakePit(groups={("BTCUSDT", ds.V3_SLICE_22): (late,)})
    request = ds.v3_request(ds.v3_pit(interval=(t0, t2)))
    with pytest.raises(PitConflictError):  # v2's require_no_conflict: membership does not matter
        build(storage, universe, pit, ds.FakeQuality(), request=request)


@pytest.mark.parametrize("keys", [("k2", "k1"), ("k1", "k1")])
def test_a_duplicate_or_unordered_key_in_a_slice_fails_closed(
    storage: LocalFileStorageAdapter, keys: tuple[str, str]
) -> None:
    universe, _, quality = ds.point_scenario()
    groups = tuple(ds.point_key(key, f"r-{i}") for i, key in enumerate(keys))
    pit = ds.FakePit(groups={("BTCUSDT", ds.V3_SLICE_22): groups})
    with pytest.raises(CatalogIntegrityError, match="duplicated or out of order"):
        build(storage, universe, pit, quality)


def test_a_key_not_owned_by_its_slice_fails_closed(storage: LocalFileStorageAdapter) -> None:
    universe, _, quality = ds.point_scenario()
    stray = ds.point_key("k1", "r1", owner=ds.utc(2023, 11, 14, 21, 30))
    pit = ds.FakePit(groups={("BTCUSDT", ds.V3_SLICE_22): (stray,)})
    with pytest.raises(CatalogIntegrityError, match="not owned by the slice"):
        build(storage, universe, pit, quality)


def test_missing_or_foreign_lineage_fails_closed(storage: LocalFileStorageAdapter) -> None:
    universe, _, quality = ds.point_scenario()
    for lineage in (ds.trade_lineage("other"), ds.trade_lineage("r1", table="canonical.bars_1m")):
        wrong = PitSelectedRevision(
            revision_id="r1", event_time=ds.V3_EVENT, lineage=lineage, evidence_gap=None
        )
        group = PitKeyGroup(
            observation_key="k1",
            owner_event_time=ds.V3_EVENT,
            evaluations=(PitKeyEvaluation(ds.SIM, PointInTimeStatus.SELECTED, wrong),),
        )
        pit = ds.FakePit(groups={("BTCUSDT", ds.V3_SLICE_22): (group,)})
        with pytest.raises(CatalogIntegrityError, match="has no lineage"):
            build(storage, universe, pit, quality)


def test_lineage_citing_an_unbound_table_fails_closed(storage: LocalFileStorageAdapter) -> None:
    request = ds.v3_request(ds.v3_pit(skip=("raw.binance_spot_archives",)))
    with pytest.raises(CatalogIntegrityError, match="does not bind"):
        build(storage, *ds.point_scenario(), request=request)


def test_an_unrecorded_evidence_gap_fails_closed(storage: LocalFileStorageAdapter) -> None:
    _, _, quality = ds.point_scenario()
    day_report = ds.partition_report("BTCUSDT", ds.DAY)
    for gaps in (
        {key: gap for key, gap in quality.gaps.items() if key[0] != day_report},
        {**quality.gaps, (ds.V3_LISTING_REPORT, ds.V3_LISTINGS, "listing-eth"): "other text"},
    ):
        with pytest.raises(DatasetQualityError):
            build(storage, *ds.point_scenario()[:2], ds.FakeQuality(gaps=gaps))


def test_an_episode_both_member_and_excluded_fails_closed(
    storage: LocalFileStorageAdapter,
) -> None:
    universe, pit, quality = ds.point_scenario()
    universe.exclusions_ = (
        ds.v3_exclusion("BTCUSDT", "listing-btc"),
        ds.v3_exclusion("ETHUSDT", "listing-eth"),
    )
    with pytest.raises(CatalogIntegrityError, match="both a member and excluded"):
        build(storage, universe, pit, quality)


def test_overlapping_member_spans_of_one_episode_fail_closed(
    storage: LocalFileStorageAdapter,
) -> None:
    t0, t2 = ds.utc(2023, 12, 1), ds.utc(2023, 12, 3)
    t1 = ds.utc(2023, 12, 2)
    universe, pit, quality = ds.point_scenario()
    universe.members_ = (
        ds.v3_member("BTCUSDT", "listing-btc", (t0, t1 + timedelta(hours=1))),
        ds.v3_member("BTCUSDT", "listing-btc", (t1, t2)),
    )
    universe.exclusions_ = (ds.v3_exclusion("ETHUSDT", "listing-eth", (t0, t2)),)
    universe.spans = (("BTCUSDT", t0, t2),)
    request = ds.v3_request(ds.v3_pit(interval=(t0, t2)))
    with pytest.raises(CatalogIntegrityError, match="overlap"):
        build(storage, universe, pit, quality, request=request)


def test_universe_streams_out_of_canonical_order_fail_closed(
    storage: LocalFileStorageAdapter,
) -> None:
    universe, pit, quality = ds.point_scenario()
    universe.members_ = (ds.v3_member("ETHUSDT", "e"), ds.v3_member("BTCUSDT", "b"))
    universe.exclusions_ = ()
    with pytest.raises(CatalogIntegrityError, match="canonical order"):
        build(storage, universe, pit, quality)

    universe, pit, quality = ds.point_scenario()
    universe.lineage = tuple(reversed(universe.lineage))  # ADR-0077 §2: by revision id
    with pytest.raises(CatalogIntegrityError, match="canonical order"):
        build(storage, universe, pit, quality)

    universe, pit, quality = ds.point_scenario()
    universe.gaps = (("listing-zzz", ds.GAP_TEXT),)
    with pytest.raises(CatalogIntegrityError, match="has no listing lineage"):
        build(storage, universe, pit, quality)

    universe, pit, quality = ds.point_scenario()
    universe.spans = (("BTCUSDT", None, None), ("XRPUSDT", None, None))
    with pytest.raises(CatalogIntegrityError, match="not in the spec"):
        build(storage, universe, pit, quality)


def test_event_days_outside_the_window_must_arrive_in_order(
    storage: LocalFileStorageAdapter,
) -> None:
    universe, _, quality = ds.point_scenario()
    later = ds.V3_EVENT + timedelta(days=1)
    pit = ds.FakePit(
        groups={
            ("BTCUSDT", ds.V3_SLICE_22): (
                ds.point_key("k1", "r1"),
                ds.point_key("k2", "r2", event=later, owner=ds.V3_EVENT),
            )
        }
    )
    b = builder(storage)
    summary = b.build(
        ds.v3_request(),
        sources=ds.v3_sources(universe, pit, quality),
        chunks=ds.FakeChunkWriter(),
        manifests=ds.FakeManifests(),
    )
    assert report_ids(b, summary.manifest) == [
        ds.V3_LISTING_REPORT,
        ds.partition_report("BTCUSDT", ds.DAY),
        ds.partition_report("BTCUSDT", later.date()),
    ]
    earlier = ds.V3_EVENT - timedelta(days=1)
    pit = ds.FakePit(
        groups={
            ("BTCUSDT", ds.V3_SLICE_22): (
                ds.point_key("k1", "r1"),
                ds.point_key("k2", "r2", event=earlier, owner=ds.V3_EVENT),
            )
        }
    )
    with pytest.raises(CatalogIntegrityError, match="report order"):
        build(storage, universe, pit, quality)


def test_a_chunk_proof_for_other_rows_fails_closed(storage: LocalFileStorageAdapter) -> None:
    chunks = ds.FakeChunkWriter()
    chunks.lie = True
    with pytest.raises(CatalogIntegrityError, match="proves other rows"):
        build(storage, *ds.point_scenario(), chunks=chunks)


def test_a_record_longer_than_a_leaf_fails_the_build(storage: LocalFileStorageAdapter) -> None:
    with pytest.raises(EvidenceRecordTooLarge):
        build(storage, *ds.point_scenario(), leaf_max_bytes=64)


def test_unbound_evidence_tables_with_a_snapshot_are_refused_unless_manifested(
    storage: LocalFileStorageAdapter,
) -> None:
    table = "raw.binance_spot_precedence_evidence"
    request = ds.v3_request(ds.v3_pit(skip=(table,)))
    b = builder(storage, heads={table: "7"})
    sources = ds.v3_sources(*ds.point_scenario())
    with pytest.raises(DatasetSpecError, match=table):
        b.select(request, sources=sources, sink=ds.RecordingSink(), manifested=False)
    derived = b.select(request, sources=sources, sink=ds.RecordingSink(), manifested=True)
    assert derived.row_count == 3
    with pytest.raises(DatasetSpecError, match=table):
        b.build(request, sources=sources, chunks=ds.FakeChunkWriter(), manifests=ds.FakeManifests())


def test_requests_are_checked_like_v2(storage: LocalFileStorageAdapter) -> None:
    b = builder(storage)
    sources = ds.v3_sources(*ds.point_scenario())
    unregistered = UniverseSelectionSpec(
        **{**FIRST_SLICE_UNIVERSE.model_dump(exclude={"schema_version"}), "symbols": ("BTCUSDT",)}
    )
    for request in (
        ds.v3_request(universe=unregistered),
        ds.v3_request(data_type="trades"),
        ds.v3_request(start=ds.START.replace(tzinfo=None)),
        ds.v3_request(start=ds.END, end=ds.START),
        ds.v3_request(ds.v3_pit(skip=(ds.V3_TRADES,))),
    ):
        with pytest.raises(DatasetSpecError):
            b.select(request, sources=sources, sink=ds.RecordingSink(), manifested=False)
    with pytest.raises(DatasetSpecError, match="namespace"):
        b.build(
            ds.v3_request(),
            sources=sources,
            chunks=ds.FakeChunkWriter("canonical.selection_chunks"),
            manifests=ds.FakeManifests(),
        )


# ------------------------------------------------------------------ the rule (DQ-9 OPEN)


def test_rule_parameters_are_required_and_part_of_the_identity(
    storage: LocalFileStorageAdapter,
) -> None:
    parameters = inspect.signature(dataset_evidence_rule).parameters.values()
    assert {item.name for item in parameters} == set(PARAMS)
    assert all(item.kind is inspect.Parameter.KEYWORD_ONLY for item in parameters)
    assert all(item.default is inspect.Parameter.empty for item in parameters)
    rule = dataset_evidence_rule(**PARAMS)
    assert rule.spec["chunks"]["chunk_rows"] == 2
    assert rule.spec["evidence"]["fanout"] == 2
    for name in PARAMS:
        other = dataset_evidence_rule(**{**PARAMS, name: PARAMS[name] + 1})
        assert other.rule_hash != rule.rule_hash
        assert builder(storage, **{name: PARAMS[name] + 1}).selection_id(
            ds.v3_request()
        ) != builder(storage).selection_id(ds.v3_request())
    for bad in ({"chunk_rows": 0}, {"fanout": 1}, {"leaf_max_bytes": 0}, {"chunk_rows": True}):
        with pytest.raises(DatasetSpecError):
            dataset_evidence_rule(**{**PARAMS, **bad})
    with pytest.raises(DatasetSpecError):
        DatasetEvidenceRule(rule.chunk_rows, rule.limits, "0" * 64)


def test_iter_evidence_needs_the_manifest_s_own_rule(storage: LocalFileStorageAdapter) -> None:
    summary = build(storage, *ds.point_scenario())
    with pytest.raises(CatalogIntegrityError, match="not this builder's rule"):
        stream(builder(storage, fanout=3), summary.manifest, MEMBERS)


def test_v3_is_additive_to_the_v2_api(storage: LocalFileStorageAdapter) -> None:
    assert DATASET_RULE_VERSION == "1.0.0"
    for method in (DatasetBuilder.select, DatasetBuilder.build):
        names = list(inspect.signature(method).parameters)
        assert names == ["self", "universe", "pit", "data_type", "start", "end"]
    request = ds.v3_request()
    v2 = selection_id_for(
        request.universe, request.pit, request.data_type, request.start, request.end
    )
    v3 = builder(storage).selection_id(request)
    assert v2.startswith("hlens.dataset.pit-selection@1.0.0.")
    assert v3.startswith("hlens.dataset.pit-selection@2.1.0.")

"""The v3 streaming verifier and manifest store (ADR-0077 §6 / §6.3 / §8.2; acceptance 3 / 4).

A real SQLite catalog holds the v3 manifest table and the chunk table; evidence objects go to the
world's real ``LocalFileStorageAdapter``. The upstream cursors (B-UNIV / B-PIT / quality) are the
in-memory stand-ins of ``dataset_support``; chunks are committed to the real chunk table by a
small test ``DatasetChunkWriter`` (B3's ``chunks.py`` is a parallel slice). Limits are arbitrary
small values (DQ-9 OPEN).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

import pyarrow as pa  # type: ignore[import-untyped]
import pytest

from core.contracts.catalog import CommitOutcome, CommitRequest
from core.contracts.revision import PointInTimeSpec
from core.contracts.universe import (
    DatasetChunkProof,
    DatasetQualityReportRef,
    DatasetQualitySubject,
    DegradedEpisodeKey,
    EpisodeIdentityBasis,
    EvidenceStream,
    ResearchDatasetEvidenceManifest,
    SelectedRevisionLineage,
    UniverseMember,
    dataset_chunk_batch_id,
)
from core.domain.base import CONTRACT_SCHEMA_VERSION, Contract
from core.domain.specs import InstrumentType
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    DATASET_EVIDENCE_MANIFESTS,
    DATASET_MANIFESTS,
    DATASET_SELECTION_CHUNKS,
)
from infrastructure.dataset import verify_v3 as v
from infrastructure.dataset.builder import (
    ChunkCommitted,
    DatasetBuildSummary,
    DatasetEvidenceBuilder,
    DatasetQualityError,
    dataset_evidence_rule,
)
from infrastructure.dataset.evidence import (
    EvidenceTreeLimits,
    EvidenceTreeWriter,
    iter_evidence_stream,
)
from infrastructure.dataset.manifests import (
    DatasetEvidenceManifestStore,
    ManifestFormError,
    ManifestStore,
    evidence_manifest_row,
)
from infrastructure.dataset.verify_v3 import StreamingEvidenceVerifier
from infrastructure.storage import LocalFileStorageAdapter
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World

MEMBERS = EvidenceStream.MEMBERS
LINEAGE = EvidenceStream.LINEAGE
GAPS = EvidenceStream.EVIDENCE_GAPS
REPORTS = EvidenceStream.QUALITY_REPORTS
PROOFS = EvidenceStream.CHUNK_PROOFS
CHUNKS = DATASET_SELECTION_CHUNKS
V3_TABLE = DATASET_EVIDENCE_MANIFESTS
#: Arbitrary small parameters (not a DQ-9 choice).
PARAMS: dict[str, int] = {
    "chunk_rows": 2,
    "leaf_max_records": 2,
    "leaf_max_bytes": 4096,
    "fanout": 2,
}

Scenario = tuple[ds.FakeUniverse, ds.FakePit, ds.FakeQuality]


# =========================================================================================
# stand-ins: a chunk writer over the real chunk table, a listing-episode lookup
# =========================================================================================


class CatalogChunks:
    """``DatasetChunkWriter`` over the real ``research.dataset_selection_chunks`` (B3 stand-in).

    ``tamper(chunk_index, rows)`` rewrites the rows actually committed (the proof then proves
    those); ``before(chunk_index, selection_id)`` runs before a chunk is committed.
    """

    def __init__(
        self,
        w: World,
        *,
        tamper: Callable[[int, list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
        before: Callable[[int, str], None] | None = None,
    ) -> None:
        self._w = w
        self._tamper = tamper
        self._before = before
        self.proofs: list[DatasetChunkProof] = []

    @property
    def table(self) -> str:
        return CHUNKS.table

    def commit_chunk(
        self, selection_id: str, chunk_index: int, rows: Sequence[Mapping[str, Any]]
    ) -> ChunkCommitted:
        if self._before is not None:
            self._before(chunk_index, selection_id)
        content = sorted((dict(row) for row in rows), key=lambda row: row["row_ordinal"])
        if self._tamper is not None:
            content = self._tamper(chunk_index, content)
        batch = pa.Table.from_pylist(content, schema=CHUNKS.arrow_schema)
        fingerprint = CHUNKS.fingerprint_rule.fingerprint(batch)
        batch_id = dataset_chunk_batch_id(selection_id, chunk_index)
        result = self._w.h.adapter.commit_batch(
            CommitRequest(
                table=CHUNKS.table,
                batch_id=batch_id,
                batch_fingerprint=fingerprint,
                row_count=len(content),
                expected_parent_snapshot_id=self._w.h.head(CHUNKS.table),
            ),
            batch,
        )
        proof = DatasetChunkProof(
            chunk_index=chunk_index,
            batch_id=batch_id,
            snapshot_id=result.snapshot.snapshot_id,
            first_row_ordinal=content[0]["row_ordinal"],
            row_count=len(content),
            batch_fingerprint=fingerprint,
        )
        self.proofs.append(proof)
        return ChunkCommitted(proof, result.outcome is CommitOutcome.ALREADY_COMMITTED)

    def seal(self, selection_id: str, chunk_count: int) -> None:
        prefix = f"{selection_id}.chunk-"
        found = sorted(
            item.batch_id
            for item in self._w.h.history(CHUNKS.table)
            if item.batch_id is not None and item.batch_id.startswith(prefix)
        )
        if found != [dataset_chunk_batch_id(selection_id, i) for i in range(chunk_count)]:
            raise CatalogIntegrityError(f"{selection_id} chunks are not exactly 0..{chunk_count}")


@dataclass
class FakeListingEpisodes:
    """``ListingEpisodes`` over a fixed revision -> observation key map; records each call."""

    keys: dict[str, str]
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def observation_keys(
        self, pit: PointInTimeSpec, revision_ids: Sequence[str]
    ) -> Iterator[tuple[str, str]]:
        self.calls.append(tuple(revision_ids))
        return iter([(rid, self.keys[rid]) for rid in revision_ids if rid in self.keys])


def episodes_of(universe: ds.FakeUniverse) -> FakeListingEpisodes:
    """Each cited listing revision belongs to the episode citing it (the genuine case)."""
    keys = {
        cast(Any, entry).listing_revision_id: cast(Any, entry).episode.observation_key()
        for entry in (*universe.members_, *universe.exclusions_)
    }
    return FakeListingEpisodes(keys)


# =========================================================================================
# setup
# =========================================================================================


@dataclass
class Setup:
    w: World
    builder: DatasetEvidenceBuilder
    chunks: CatalogChunks
    episodes: FakeListingEpisodes
    verifier: StreamingEvidenceVerifier
    store: DatasetEvidenceManifestStore
    #: What the verifier's sources factory serves (a test may swap it after a build).
    current: list[Scenario]

    def sources(self) -> Any:
        return ds.v3_sources(*self.current[0])

    def build(self, manifests: Any = None) -> DatasetBuildSummary:
        return self.builder.build(
            ds.v3_request(),
            sources=self.sources(),
            chunks=self.chunks,
            manifests=self.store if manifests is None else manifests,
        )

    def stream(self, manifest: ResearchDatasetEvidenceManifest, which: EvidenceStream) -> list[Any]:
        with self.builder.iter_evidence(manifest, which) as records:
            return list(records)


def setup(
    w: World,
    scenario: Scenario | None = None,
    *,
    chunks: CatalogChunks | None = None,
    episodes: FakeListingEpisodes | None = None,
    **params: int,
) -> Setup:
    chosen = ds.point_scenario() if scenario is None else scenario
    rule = dataset_evidence_rule(**{**PARAMS, **params})
    builder = DatasetEvidenceBuilder(w.h.adapter, w.h.storage, rule=rule)
    writer = CatalogChunks(w) if chunks is None else chunks
    lookup = episodes_of(chosen[0]) if episodes is None else episodes
    current = [chosen]
    verifier = StreamingEvidenceVerifier(
        w.h.adapter,
        builder=builder,
        chunks=writer,
        sources=lambda request: ds.v3_sources(*current[0]),
        listing_episodes=lookup,
    )
    return Setup(w, builder, writer, lookup, verifier, verifier.store(), current)


def revalidated(
    manifest: ResearchDatasetEvidenceManifest, **update: Any
) -> ResearchDatasetEvidenceManifest:
    """A contract-valid, hash-consistent variant of ``manifest``."""
    forged = manifest.model_copy(update=update)
    return ResearchDatasetEvidenceManifest.model_validate_json(forged.model_dump_json())


def with_stream(
    s: Setup,
    manifest: ResearchDatasetEvidenceManifest,
    stream: EvidenceStream,
    records: Sequence[Contract],
    **update: Any,
) -> ResearchDatasetEvidenceManifest:
    """``manifest`` with ``stream`` replaced by a real, well-formed tree of ``records`` (and any
    other field ``update``d in the same re-validation)."""
    writer = EvidenceTreeWriter(
        s.w.h.storage, stream, limits=s.builder.rule.limits, schema_version=manifest.schema_version
    )
    for record in records:
        writer.append(record)
    ref = writer.finish()
    evidence = tuple(ref if item.stream is stream else item for item in manifest.evidence)
    return revalidated(manifest, evidence=evidence, **update)


def v3_rows(w: World) -> list[dict[str, Any]]:
    return w.h.rows(V3_TABLE) if w.h.head(V3_TABLE.table) is not None else []


# =========================================================================================
# the genuine path
# =========================================================================================


def test_a_built_manifest_is_verified_persisted_and_loaded(w: World) -> None:
    s = setup(w)
    summary = s.build()
    manifest = summary.manifest
    assert not summary.manifest_replayed
    assert (summary.row_count, summary.chunk_count) == (3, 2)
    [row] = v3_rows(w)
    assert row == evidence_manifest_row(manifest)
    assert s.store.recorded_version(summary.selection_id) == manifest.schema_version
    assert s.store.load(summary.manifest_hash) == manifest
    assert s.store.load("0" * 64) is None

    again = s.build()  # a replay: chunks replayed, the manifest re-verified and found
    assert again.manifest_replayed and again.replayed
    assert again.manifest_hash == summary.manifest_hash
    assert len(v3_rows(w)) == 1

    # ADR-0077 §8.2 through the v2 entry points.
    both = ManifestStore(w.h.adapter, w.builder(), evidence_verifier=s.verifier)
    assert both.load_any(summary.manifest_hash) == manifest
    assert both.load_any("0" * 64) is None
    with pytest.raises(ManifestFormError):
        both.load(summary.manifest_hash)  # the typed v2 entry never returns a v3 manifest
    with pytest.raises(ManifestFormError):
        w.builder().manifests().load_any(summary.manifest_hash)  # no v3 verifier: refused
    assert w.h.head(DATASET_MANIFESTS.table) is None

    universe, pit, _ = s.current[0]
    assert universe.open_now == 0 and pit.open_now == 0


def test_listing_claims_are_resolved_in_leaf_bounded_batches(w: World) -> None:
    """Five members cite five listing revisions: with ``leaf_max_records = 2`` the claims are
    resolved in three sorted batches of at most two, never as one whole-universe set."""
    episodes = [
        DegradedEpisodeKey(
            basis=EpisodeIdentityBasis.DEGRADED_SYMBOL_START,
            venue="binance",
            instrument_type=InstrumentType.SPOT,
            symbol="BTCUSDT",
            tradable_from=ds.utc(2023, 1, day),
        )
        for day in range(1, 6)
    ]
    members = sorted(
        (
            UniverseMember(episode=episode, listing_revision_id=f"listing-{index}")
            for index, episode in enumerate(episodes)
        ),
        key=lambda member: member.episode.observation_key(),
    )
    universe = ds.FakeUniverse(
        members_=members,
        exclusions_=(),
        lineage=[ds.listing_lineage(f"listing-{index}") for index in range(5)],
        gaps=(),
        spans=(("BTCUSDT", None, None),),
    )
    _, pit, quality = ds.point_scenario()
    s = setup(w, (universe, pit, quality))
    summary = s.build()
    assert len(s.episodes.calls) == 3
    assert all(len(call) <= 2 for call in s.episodes.calls)
    assert sorted(rid for call in s.episodes.calls for rid in call) == [
        f"listing-{index}" for index in range(5)
    ]
    assert s.store.load(summary.manifest_hash) == summary.manifest


# =========================================================================================
# semantic checks the generator does not make (ADR-0077 §6.3; acceptance 4)
# =========================================================================================


def _universe(
    members: Sequence[tuple[str, str]],
    exclusions: Sequence[tuple[str, str]],
    lineage: Sequence[str],
) -> ds.FakeUniverse:
    return ds.FakeUniverse(
        members_=[ds.v3_member(symbol, revision) for symbol, revision in members],
        exclusions_=[ds.v3_exclusion(symbol, revision) for symbol, revision in exclusions],
        lineage=[ds.listing_lineage(revision) for revision in lineage],
        gaps=(),
        spans=(("BTCUSDT", None, None),),
    )


def _refused(s: Setup) -> None:
    with pytest.raises(CatalogIntegrityError):
        s.build()
    assert v3_rows(s.w) == []  # nothing persisted for a manifest that does not verify


def test_a_listing_revision_without_listing_lineage_is_refused(w: World) -> None:
    universe = _universe(
        [("BTCUSDT", "listing-btc")], [("ETHUSDT", "listing-eth")], ["listing-eth"]
    )
    _, pit, quality = ds.point_scenario()
    s = setup(w, (universe, pit, ds.FakeQuality(gaps=dict(quality.gaps))))
    with pytest.raises(CatalogIntegrityError, match="no listing lineage"):
        s.build()
    assert v3_rows(w) == []


@pytest.mark.parametrize("leaf_max_records", [1, 2])
def test_a_listing_revision_claimed_by_two_episodes_is_refused(
    w: World, leaf_max_records: int
) -> None:
    """Same buffer (two claims disagree) or two buffers (the second disagrees with the listing
    table): either way no two episodes can claim one listing revision."""
    universe = _universe(
        [("BTCUSDT", "listing-shared")], [("ETHUSDT", "listing-shared")], ["listing-shared"]
    )
    _, pit, quality = ds.point_scenario()
    episodes = FakeListingEpisodes({"listing-shared": ds.v3_episode("BTCUSDT").observation_key()})
    s = setup(
        w,
        (universe, pit, ds.FakeQuality(gaps=dict(quality.gaps))),
        episodes=episodes,
        leaf_max_records=leaf_max_records,
    )
    _refused(s)


def test_a_listing_revision_of_another_episode_is_refused(w: World) -> None:
    universe, pit, quality = ds.point_scenario()
    episodes = episodes_of(universe)
    episodes.keys["listing-btc"] = ds.v3_episode("ETHUSDT").observation_key()
    s = setup(w, (universe, pit, quality), episodes=episodes)
    with pytest.raises(CatalogIntegrityError, match="another episode"):
        s.build()
    assert v3_rows(w) == []


def test_a_listing_revision_absent_from_the_listing_table_is_refused(w: World) -> None:
    universe, pit, quality = ds.point_scenario()
    episodes = episodes_of(universe)
    del episodes.keys["listing-eth"]
    s = setup(w, (universe, pit, quality), episodes=episodes)
    with pytest.raises(CatalogIntegrityError, match="not in canonical.instrument_listings"):
        s.build()
    assert v3_rows(w) == []


# ------------------------------------------------------------------ re-derivation on load


def test_a_key_owned_by_two_slices_fails_the_load(w: World) -> None:
    s = setup(w)
    summary = s.build()
    universe, pit, quality = ds.point_scenario()
    pit.groups = {**pit.groups, ("BTCUSDT", ds.V3_SLICE_21): (ds.point_key("k1", "r1"),)}
    s.current[0] = (universe, pit, quality)
    with pytest.raises(CatalogIntegrityError, match="not owned by the slice"):
        s.store.load(summary.manifest_hash)


def test_a_member_also_excluded_fails_the_load(w: World) -> None:
    s = setup(w)
    summary = s.build()
    universe, pit, quality = ds.point_scenario()
    universe.exclusions_ = (ds.v3_exclusion("BTCUSDT", "listing-btc"),)
    s.current[0] = (universe, pit, quality)
    with pytest.raises(CatalogIntegrityError, match="both a member and excluded"):
        s.store.load(summary.manifest_hash)


def test_a_gap_its_report_does_not_record_fails_the_load(w: World) -> None:
    s = setup(w)
    summary = s.build()
    universe, pit, quality = ds.point_scenario()
    quality.gaps = {key: gap for key, gap in quality.gaps.items() if key[2] != "r2"}
    s.current[0] = (universe, pit, quality)
    with pytest.raises(DatasetQualityError):
        s.store.load(summary.manifest_hash)


# =========================================================================================
# identity (ADR-0077 §6.1)
# =========================================================================================


def test_a_manifest_of_other_inputs_or_another_rule_is_refused(w: World) -> None:
    s = setup(w)
    genuine = s.build(manifests=ds.FakeManifests()).manifest
    dataset = genuine.dataset
    forgeries = {
        "other-window": revalidated(
            genuine,
            dataset=dataset.model_copy(update={"time_range_end": ds.END + timedelta(hours=1)}),
        ),
        "other-data-type": revalidated(genuine, data_type="klines_1m"),
        "other-table": revalidated(
            genuine, dataset=dataset.model_copy(update={"table": "research.elsewhere"})
        ),
        "unregistered-universe": revalidated(
            genuine,
            universe_spec=genuine.universe_spec.model_copy(update={"spec_hash": "0" * 64}),
        ),
    }
    for name, forged in forgeries.items():
        assert forged != genuine, name
        with pytest.raises(CatalogIntegrityError):
            s.store.persist(forged)
        assert v3_rows(w) == [], name
    other_rule = setup(w, chunk_rows=3)
    with pytest.raises(CatalogIntegrityError, match="not the verifier's rule"):
        other_rule.verifier.verify_evidence_manifest(genuine, manifested=False)
    assert s.store.persist(genuine) is False  # the genuine one still verifies


# =========================================================================================
# tampering (acceptance 3)
# =========================================================================================


@pytest.mark.parametrize("how", ["delete", "alter"])
def test_a_deleted_or_altered_evidence_object_fails_the_load(w: World, how: str) -> None:
    s = setup(w)
    summary = s.build()
    key = summary.manifest.evidence_for(LINEAGE).root.key
    path = ds.evidence_object_path(w.h.storage, key)
    if how == "delete":
        path.unlink()
    else:
        data = path.read_bytes()
        path.write_bytes(data.replace(b'"level":', b'"level" :', 1))
    with pytest.raises(CatalogIntegrityError):
        s.store.load(summary.manifest_hash)


def _dropped_last(records: list[Any]) -> list[Any]:
    return records[:-1]


def _swapped_data_lineage(records: list[Any]) -> list[Any]:
    listing = [item for item in records if item.canonical_table == ds.V3_LISTINGS]
    data = [item for item in records if item.canonical_table != ds.V3_LISTINGS]
    return [*listing, data[1], data[0], *data[2:]]


def _extra_report(records: list[Any]) -> list[Any]:
    last = records[-1]
    assert last.day is not None
    return [
        *records,
        DatasetQualityReportRef(
            report_id="report-extra",
            subject=DatasetQualitySubject.SYMBOL_DAY,
            symbol=last.symbol,
            day=last.day + timedelta(days=1),
        ),
    ]


STREAM_FORGERIES: dict[str, tuple[EvidenceStream, Callable[[list[Any]], list[Any]]]] = {
    "member-dropped": (MEMBERS, _dropped_last),
    "lineage-reordered": (LINEAGE, _swapped_data_lineage),
    "gap-dropped": (GAPS, _dropped_last),
    "report-added": (REPORTS, _extra_report),
    "proof-dropped": (PROOFS, _dropped_last),
}


@pytest.mark.parametrize("case", sorted(STREAM_FORGERIES))
def test_a_stream_other_than_the_derivation_is_refused(w: World, case: str) -> None:
    """Well-formed trees (every object genuine, the root hash consistent) committing another
    record sequence: the lockstep merge rejects each; nothing is persisted; a planted row fails
    the load."""
    stream, change = STREAM_FORGERIES[case]
    s = setup(w)
    genuine = s.build(manifests=ds.FakeManifests()).manifest
    records = change(s.stream(genuine, stream))
    update: dict[str, Any] = {}
    if stream is PROOFS:  # one chunk fewer: counts and the dataset snapshot follow the proofs
        last = records[-1].snapshot_id
        update = {
            "row_count": 2,
            "chunk_count": 1,
            "dataset": genuine.dataset.model_copy(update={"snapshot_id": last}),
        }
    forged = with_stream(s, genuine, stream, records, **update)
    assert forged.content_hash() != genuine.content_hash()
    with pytest.raises(CatalogIntegrityError):
        s.store.persist(forged)
    assert v3_rows(w) == []
    w.h.forge_rows(V3_TABLE, [evidence_manifest_row(forged)], batch_id=f"forged-{case}")
    with pytest.raises(CatalogIntegrityError):
        s.store.load(forged.content_hash())


def test_a_chunk_committed_with_other_rows_is_refused(w: World) -> None:
    def tamper(index: int, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if index == 1:
            rows[0]["revision_id"] = "r-forged"
        return rows

    s = setup(w, chunks=CatalogChunks(w, tamper=tamper))
    with pytest.raises(CatalogIntegrityError, match="does not prove the derived chunk"):
        s.build()
    assert v3_rows(w) == []


@pytest.mark.parametrize("chunk_index", [0, 7])
def test_a_stray_row_in_the_dataset_snapshot_is_refused(w: World, chunk_index: int) -> None:
    """A row of the selection committed under another batch id before the last chunk: inside a
    proven chunk it breaks that chunk's read-back; outside every chunk, the row count."""

    def before(index: int, selection_id: str) -> None:
        if index != 1:
            return
        stray = {
            "selection_id": selection_id,
            "canonical_table": ds.V3_TRADES,
            "symbol": "BTC-USDT",
            "observation_key": "k-stray",
            "revision_id": "r-stray",
            "event_time": ds.V3_EVENT,
            "effective_from": None,
            "effective_until": None,
            "chunk_index": chunk_index,
            "row_ordinal": 99,
        }
        w.h.forge_rows(CHUNKS, [stray], batch_id=f"stray-{chunk_index}")

    s = setup(w, chunks=CatalogChunks(w, before=before))
    with pytest.raises(CatalogIntegrityError):
        s.build()
    assert v3_rows(w) == []


def test_an_extra_chunk_batch_fails_the_load(w: World) -> None:
    s = setup(w)
    summary = s.build()
    [*_, last] = w.h.rows_at(CHUNKS.table, summary.dataset.snapshot_id)
    extra = dict(last, chunk_index=2, row_ordinal=4)
    w.h.forge_rows(CHUNKS, [extra], batch_id=dataset_chunk_batch_id(summary.selection_id, 2))
    with pytest.raises(CatalogIntegrityError, match="chunks are not exactly"):
        s.store.load(summary.manifest_hash)


def test_a_manifest_bound_to_an_earlier_chunk_snapshot_is_refused(w: World) -> None:
    s = setup(w)
    genuine = s.build(manifests=ds.FakeManifests()).manifest
    first = s.chunks.proofs[0].snapshot_id
    forged = revalidated(genuine, dataset=genuine.dataset.model_copy(update={"snapshot_id": first}))
    with pytest.raises(CatalogIntegrityError):
        s.store.persist(forged)
    assert v3_rows(w) == []


def test_a_chunk_proof_naming_another_snapshot_is_refused(w: World) -> None:
    """A proof whose snapshot does not commit its batch (a hole papered over)."""
    s = setup(w)
    genuine = s.build(manifests=ds.FakeManifests()).manifest
    proofs = cast(list[DatasetChunkProof], s.stream(genuine, PROOFS))
    moved = proofs[0].model_copy(update={"snapshot_id": proofs[1].snapshot_id})
    forged = with_stream(s, genuine, PROOFS, [moved, proofs[1]])
    with pytest.raises(CatalogIntegrityError, match="does not commit chunk 0"):
        s.store.persist(forged)


def test_a_hash_in_both_manifest_tables_is_refused(w: World) -> None:
    s = setup(w)
    summary = s.build()
    content_hash = summary.manifest_hash
    planted = {
        "manifest_content_hash": content_hash,
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "dataset_zone": "research_dataset",
        "dataset_table": "research.dataset_selections",
        "dataset_snapshot_id": "1",
        "dataset_time_range_start": ds.START,
        "dataset_time_range_end": ds.END,
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
        "quality_report_ids": ["planted"],
        "manifest_json": "{}",
    }
    w.h.forge_rows(DATASET_MANIFESTS, [planted], batch_id="planted-v2")
    with pytest.raises(CatalogIntegrityError, match="both"):
        s.store.load(content_hash)
    both = ManifestStore(w.h.adapter, w.builder(), evidence_verifier=s.verifier)
    with pytest.raises(CatalogIntegrityError, match="both"):
        both.load(content_hash)
    with pytest.raises(CatalogIntegrityError, match="both"):
        both.load_any(content_hash)
    with pytest.raises(CatalogIntegrityError):
        s.store.persist(summary.manifest)


def test_another_manifest_of_the_same_selection_is_refused(w: World) -> None:
    s = setup(w)
    summary = s.build()
    genuine = summary.manifest
    records = s.stream(genuine, REPORTS)
    other = with_stream(s, genuine, REPORTS, _extra_report(records))
    w.h.forge_rows(V3_TABLE, [evidence_manifest_row(other)], batch_id="second-manifest")
    with pytest.raises(CatalogIntegrityError):
        s.store.recorded_version(summary.selection_id)
    with pytest.raises(CatalogIntegrityError, match="another evidence manifest"):
        s.store.persist(genuine)


# =========================================================================================
# the bounded claim buffers, in isolation
# =========================================================================================


def _tree(
    storage: LocalFileStorageAdapter, stream: EvidenceStream, records: Sequence[Contract]
) -> Callable[[EvidenceStream], Any]:
    limits = EvidenceTreeLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2)
    writer = EvidenceTreeWriter(
        storage, stream, limits=limits, schema_version=CONTRACT_SCHEMA_VERSION
    )
    for record in records:
        writer.append(record)
    ref = writer.finish()

    def reopen(which: EvidenceStream) -> Any:
        assert which is stream
        return iter_evidence_stream(
            storage, ref, limits=limits, schema_version=CONTRACT_SCHEMA_VERSION
        )

    return reopen


def _report(report_id: str, symbol: str | None = None, day: int | None = None) -> Any:
    if symbol is None:
        return DatasetQualityReportRef(report_id=report_id, subject=DatasetQualitySubject.LISTING)
    assert day is not None
    return DatasetQualityReportRef(
        report_id=report_id,
        subject=DatasetQualitySubject.SYMBOL_DAY,
        symbol=symbol,
        day=ds.DAY + timedelta(days=day),
    )


def _key(symbol: str, day: int) -> tuple[int, str, str]:
    return (1, symbol, (ds.DAY + timedelta(days=day)).isoformat())


def test_report_claims_merge_sorted_batches_against_the_stream(
    evidence_store: LocalFileStorageAdapter,
) -> None:
    reopen = _tree(
        evidence_store,
        REPORTS,
        [
            _report("rl"),
            _report("b0", "BTCUSDT", 0),
            _report("b1", "BTCUSDT", 1),
            _report("e0", "ETHUSDT", 0),
        ],
    )
    limits = EvidenceTreeLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2)
    claims = v._ReportClaims(limits, reopen)
    for report_id, key in (
        ("e0", _key("ETHUSDT", 0)),
        ("b1", _key("BTCUSDT", 1)),
        ("b0", _key("BTCUSDT", 0)),  # out of order: a gap's day may go back within the window
        ("rl", (0, "", "")),
        ("b0", _key("BTCUSDT", 0)),
    ):
        claims.claim(report_id, key)
    claims.close()
    assert claims.passes == 3

    for report_id, key, message in (
        ("b9", _key("BTCUSDT", 1), "binds b1"),
        ("b2", _key("BTCUSDT", 2), "does not bind"),
    ):
        bad = v._ReportClaims(limits, reopen)
        bad.claim(report_id, key)
        with pytest.raises(CatalogIntegrityError, match=message):
            bad.close()
    conflict = v._ReportClaims(limits, reopen)
    conflict.claim("b0", _key("BTCUSDT", 0))
    conflict.claim("bX", _key("BTCUSDT", 0))
    with pytest.raises(CatalogIntegrityError, match="cite 2 reports"):
        conflict.close()


def test_listing_claims_merge_sorted_batches_against_the_lineage_prefix(
    evidence_store: LocalFileStorageAdapter,
) -> None:
    lineage: list[SelectedRevisionLineage] = [
        ds.listing_lineage("la"),
        ds.listing_lineage("lc"),
        ds.trade_lineage("lb"),  # data lineage after the listing prefix never counts
    ]
    reopen = _tree(evidence_store, LINEAGE, lineage)
    limits = EvidenceTreeLimits(leaf_max_records=1, leaf_max_bytes=4096, fanout=2)
    keys = {rid: f"episode-{rid}" for rid in ("la", "lb", "lc")}
    pit = ds.v3_pit()

    ok = v._ListingClaims(limits, pit, FakeListingEpisodes(dict(keys)), reopen)
    ok.claim("lc", keys["lc"])
    ok.claim("la", keys["la"])
    ok.close()
    assert ok.passes == 2

    missing = v._ListingClaims(limits, pit, FakeListingEpisodes(dict(keys)), reopen)
    missing.claim("lb", keys["lb"])
    with pytest.raises(CatalogIntegrityError, match="no listing lineage"):
        missing.close()

    two = v._ListingClaims(
        EvidenceTreeLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2),
        pit,
        FakeListingEpisodes(dict(keys)),
        reopen,
    )
    two.claim("la", "episode-x")
    two.claim("la", keys["la"])
    with pytest.raises(CatalogIntegrityError, match="claimed by 2 episodes"):
        two.close()


def test_pinned_listing_episodes_read_the_bound_listing_table(w: World) -> None:
    w.listed()
    rows = w.h.rows_at("canonical.instrument_listings", cast(str, w.h.head(ds.V3_LISTINGS)))
    assert rows
    pit = ds.v3_pit().model_copy(
        update={
            "snapshot_bindings": {
                **ds.V3_BINDINGS,
                ds.V3_LISTINGS: cast(str, w.h.head(ds.V3_LISTINGS)),
            }
        }
    )
    wanted = [row["revision_id"] for row in rows]
    found = dict(v.PinnedListingEpisodes(w.h.adapter).observation_keys(pit, [*wanted, "absent"]))
    assert found == {row["revision_id"]: row["observation_key"] for row in rows}

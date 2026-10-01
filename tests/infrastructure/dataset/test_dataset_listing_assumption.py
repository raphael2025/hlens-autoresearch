"""ADR-0051 second phase through F3: manifests bind the listing backfill assumption through their
PIT spec, carry assumed members, and v2 / v3 verification re-derives them (D-LIST).

Real stores end to end (``dataset_support.World``); v3 uses the real B-UNIV / B-PIT upstreams, the
real chunk table, the real v3 manifest store and the streaming verifier. The current version's
``POLICY_TABLE`` (1.1.0) is monkeypatched with an arbitrary fixture floor; the bound
``ASSUMPTION_BINDING`` is the real module constant. Every run / rule size is an arbitrary small
value (DQ-9 OPEN).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from core.contracts.revision import PointInTimeSpec, PolicyBinding
from core.contracts.storage import StorageAdapter
from core.contracts.universe import (
    EvidenceStream,
    ResearchDatasetManifest,
    UniverseExclusion,
    UniverseMember,
)
from core.domain.base import Contract
from infrastructure.canonical import rules
from infrastructure.catalog.phase1_tables import (
    CANONICAL_INSTRUMENT_LISTINGS,
    DATASET_SELECTION_CHUNKS,
)
from infrastructure.contract_version import PHASE1_PUBLICATION_VERSION
from infrastructure.dataset.builder import (
    ACCEPTED_BINDINGS,
    KNOWN_BINDINGS,
    DatasetEvidenceBuilder,
    DatasetEvidenceRequest,
    DatasetEvidenceSources,
    DatasetSpecError,
    dataset_evidence_rule,
)
from infrastructure.dataset.chunks import IcebergChunkWriter
from infrastructure.dataset.evidence import evidence_record_bytes
from infrastructure.dataset.manifests import ManifestStore, manifest_assumptions
from infrastructure.dataset.quality import BoundedQualityEvidence, BoundedQualitySourceParams
from infrastructure.dataset.sources import UniverseRunParams, dataset_evidence_sources
from infrastructure.dataset.verify_v3 import StreamingEvidenceVerifier
from infrastructure.pit.runs import RunLimits
from infrastructure.pit.selector import PitRunParams
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision.exchange_info_availability import EXCHANGE_INFO_AVAILABILITY_BINDING
from infrastructure.revision.store import RevisionCatalog
from infrastructure.universe import listing_assumption as backfill
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE, UniverseUnconstructible
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import END, L1, SIM, START, World
from tests.infrastructure.revision.rest_store_support import DAY, utc

FLOOR = utc(2023, 11, 1)
TABLE = {"BTCUSDT": FLOOR, "ETHUSDT": FLOOR}
LISTINGS = CANONICAL_INSTRUMENT_LISTINGS.table
MEMBERS = EvidenceStream.MEMBERS
NO_SPAN = datetime.min.replace(tzinfo=UTC)

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


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backfill, "POLICY_TABLE", TABLE)


def bound(w: World, **kwargs: Any) -> PointInTimeSpec:
    return w.spec(
        availability_bindings=(
            rules.AVAILABILITY_BINDING,
            EXCHANGE_INFO_AVAILABILITY_BINDING,
            backfill.ASSUMPTION_BINDING,
        ),
        **kwargs,
    )


def ready(w: World) -> None:
    """BTC and ETH first observed TRADING at L1; three BTC trades on DAY; every report."""
    w.listed(ds.TRADING, L1)
    w.trades()
    w.report()


def row_content(row: Any) -> tuple[Any, ...]:
    return (
        row["canonical_table"],
        row["symbol"],
        row["observation_key"],
        row["revision_id"],
        row["event_time"],
        row["effective_from"],
        row["effective_until"],
    )


def entry_order(entry: UniverseMember | UniverseExclusion) -> tuple[str, datetime]:
    start = NO_SPAN if entry.effective_from is None else entry.effective_from
    return entry.episode.observation_key(), start


def the_binding(spec: PointInTimeSpec) -> PolicyBinding:
    [binding] = [b for b in spec.availability_bindings if b.policy_id == backfill.ASSUMPTION_ID]
    return binding


def assumed_spans(members: Any) -> list[tuple[str, Any, Any]]:
    return sorted(
        (ds.symbol_of(m), m.effective_from, m.effective_until)
        for m in members
        if m.assumption is not None
    )


# ============================================================================ registry


def test_the_policy_is_accepted_not_required_and_only_with_its_exact_hash() -> None:
    assert backfill.ASSUMPTION_BINDING in ACCEPTED_BINDINGS
    assert backfill.ASSUMPTION_BINDING not in KNOWN_BINDINGS  # the pinned Phase 1 registry
    assert ACCEPTED_BINDINGS - KNOWN_BINDINGS == {
        backfill.ASSUMPTION_BINDING_1_0_0,
        backfill.ASSUMPTION_BINDING,
    }
    assert backfill.ASSUMPTION_BINDING_1_0_0 not in KNOWN_BINDINGS


def test_a_wrong_hash_of_the_policy_is_refused_by_the_dataset(w: World, table: None) -> None:
    ready(w)
    wrong = backfill.ASSUMPTION_BINDING.model_copy(update={"policy_hash": "e" * 64})
    spec = w.spec(
        interval=(FLOOR, SIM),
        availability_bindings=(
            rules.AVAILABILITY_BINDING,
            EXCHANGE_INFO_AVAILABILITY_BINDING,
            wrong,
        ),
    )
    with pytest.raises(DatasetSpecError, match="not registered"):
        w.builder().build(FIRST_SLICE_UNIVERSE, spec, "agg_trades", START, END)


# ============================================================================ v2


def test_v2_manifest_binds_the_policy_and_verifies_its_assumed_members(
    w: World, table: None
) -> None:
    ready(w)
    # Every spec binds the heads before any build (a build moves the dataset table's own head).
    unbound = w.spec(interval=(FLOOR, SIM))
    observed_window = w.spec(interval=(L1, SIM))
    spec = bound(w, interval=(FLOOR, SIM))
    with pytest.raises(UniverseUnconstructible):  # unbound: unchanged, still refused
        w.historical_v2_build(FIRST_SLICE_UNIVERSE, unbound, "agg_trades", (START, END))

    built = w.historical_v2_build(FIRST_SLICE_UNIVERSE, spec, "agg_trades", (START, END))
    manifest = built.manifest

    # The manifest binds the policy through its PIT spec: name, version and hash.
    binding = the_binding(manifest.point_in_time)
    assert (binding.policy_id, binding.version, binding.policy_hash) == (
        backfill.ASSUMPTION_ID,
        backfill.ASSUMPTION_VERSION,
        backfill.ASSUMPTION_BINDING.policy_hash,
    )
    assert manifest_assumptions(manifest).listing_backfill == backfill.ASSUMPTION_BINDING
    assert manifest_assumptions(manifest).key == (False, True)

    # Assumed members: one per symbol, [floor, first observation), carrying the 2.0.0 binding.
    assert assumed_spans(manifest.members) == [("BTC-USDT", FLOOR, L1), ("ETH-USDT", FLOOR, L1)]
    for member in manifest.members:
        if member.assumption is not None:
            assert member.assumption == backfill.ASSUMPTION_BINDING
            assert member.assumption.schema_version == PHASE1_PUBLICATION_VERSION
    # The observed-from gap of every cited listing revision is bound as usual.
    cited = {m.listing_revision_id for m in manifest.members}
    assert {g.revision_id for g in manifest.evidence_gaps if g.table == LISTINGS} == cited

    # The assumption changes membership only: the rows are those of the observed window.
    observed = w.builder().select(FIRST_SLICE_UNIVERSE, observed_window, "agg_trades", START, END)
    assert sorted(map(row_content, built.selection.rows)) == sorted(map(row_content, observed.rows))

    # The stored JSON keeps the nested envelope, and verification re-derives everything.
    again = ResearchDatasetManifest.model_validate_json(manifest.model_dump_json())
    assert again == manifest and again.content_hash() == manifest.content_hash()
    builder = w.builder()
    builder.verify_manifest(manifest)
    store = builder.manifests()
    assert store.load(manifest.content_hash()) == manifest
    assert store.persist(manifest).replayed


def test_an_unbound_manifest_reports_no_listing_assumption(w: World, table: None) -> None:
    ready(w)
    built = w.historical_v2_build(FIRST_SLICE_UNIVERSE, w.spec(), "agg_trades", (START, END))
    assumptions = manifest_assumptions(built.manifest)
    assert assumptions.listing_backfill is None
    assert assumptions.key == (False, False)
    assert all(m.assumption is None for m in built.manifest.members)


# ============================================================================ v3


def _stream(b: DatasetEvidenceBuilder, manifest: Any, which: EvidenceStream) -> list[Contract]:
    with b.iter_evidence(manifest, which) as records:
        return list(records)


def _v3_build(w: World, spec: PointInTimeSpec) -> tuple[DatasetEvidenceBuilder, Any]:
    """``build`` (persists and verifies) then ``load_any`` (verifies again), as in
    ``test_dataset_v3_sources``."""
    request = DatasetEvidenceRequest(
        universe=FIRST_SLICE_UNIVERSE, pit=spec, data_type="agg_trades", start=START, end=END
    )
    b = DatasetEvidenceBuilder(w.h.adapter, w.h.storage, rule=dataset_evidence_rule(**RULE))
    from tests.infrastructure.dataset.test_quality_v3_dataset_integration import (
        _quality_params,
        _scratch,
        _source,
    )

    quality_scratch = _scratch(w.h.tmp_path, "dataset-listing-quality-join")
    quality_params = _quality_params(quality_scratch)

    def factory(item: DatasetEvidenceRequest) -> DatasetEvidenceSources:
        def quality_factory(
            adapter: RevisionCatalog,
            storage: StorageAdapter,
            request_pit: PointInTimeSpec,
            data_type: str,
            *,
            view: PinnedCatalogView,
            canonical_scratch_directory: Path,
            params: BoundedQualitySourceParams,
        ) -> BoundedQualityEvidence:
            assert adapter is w.h.adapter
            assert canonical_scratch_directory == w.h.canonical_scratch_directory
            return _source(w, request_pit, storage, params, view)

        return dataset_evidence_sources(
            w.h.adapter,
            w.h.storage,
            item,
            canonical_scratch_directory=w.h.canonical_scratch_directory,
            market_data_base_url=ds.ORIGIN,
            pit_params=PIT_PARAMS,
            universe_params=UNIVERSE_PARAMS,
            quality_factory=quality_factory,
            quality_params=quality_params,
        )

    chunks = IcebergChunkWriter(w.h.adapter, DATASET_SELECTION_CHUNKS)
    verifier = StreamingEvidenceVerifier(w.h.adapter, builder=b, chunks=chunks, sources=factory)
    summary = b.build(request, sources=factory(request), chunks=chunks, manifests=verifier.store())
    both = ManifestStore(w.h.adapter, w.builder(), evidence_verifier=verifier)
    assert both.load_any(summary.manifest_hash) == summary.manifest
    return b, summary


def test_v3_evidence_keeps_the_nested_binding_and_verifies(
    w: World, table: None, tmp_path: Path
) -> None:
    ready(w)
    from infrastructure.catalog.phase1_tables import (
        BINANCE_SPOT_EXCHANGE_INFO,
        CANONICAL_INSTRUMENT_LISTINGS,
    )
    from tests.infrastructure.dataset.test_quality_v3_dataset_integration import (
        _canonical_reporter,
        _scratch,
    )
    from tests.infrastructure.quality.test_listing_report_v2 import _reporter as listing_reporter

    quality_scratch = _scratch(tmp_path, "dataset-listing-quality-reports")
    reporter = _canonical_reporter(
        w.h.adapter,
        w.h.storage,
        quality_scratch,
        lambda: ds.K_Q,
        w.h.canonical_scratch_directory,
    )
    for symbol in ("BTCUSDT", "ETHUSDT"):
        reporter.report("agg_trades", symbol, DAY)
    listing_scratch = _scratch(tmp_path, "dataset-listing-history-quality")
    listing_snapshot = w.h.head(CANONICAL_INSTRUMENT_LISTINGS.table)
    raw_snapshot = w.h.head(BINANCE_SPOT_EXCHANGE_INFO.table)
    assert listing_snapshot is not None and raw_snapshot is not None
    listing_reporter(
        w.x,
        listing_scratch,
        lambda: ds.K_Q,
        adapter=w.h.adapter,
        evidence=w.h.storage,
    ).report(
        {
            CANONICAL_INSTRUMENT_LISTINGS.table: listing_snapshot,
            BINANCE_SPOT_EXCHANGE_INFO.table: raw_snapshot,
        }
    )
    spec = bound(w, interval=(FLOOR, SIM))
    v2 = w.builder().select(FIRST_SLICE_UNIVERSE, spec, "agg_trades", START, END)
    b, summary = _v3_build(w, spec)
    manifest = summary.manifest

    assert manifest.point_in_time == spec
    assert the_binding(manifest.point_in_time) == backfill.ASSUMPTION_BINDING
    assert manifest_assumptions(manifest).key == (False, True)

    members = cast(list[UniverseMember], _stream(b, manifest, MEMBERS))
    assert members == sorted(v2.universe.members, key=entry_order)
    assert assumed_spans(members) == [("BTC-USDT", FLOOR, L1), ("ETH-USDT", FLOOR, L1)]
    nested = f'"schema_version":"{PHASE1_PUBLICATION_VERSION}"'.encode()
    for member in members:
        assert member.schema_version == manifest.schema_version
        line = evidence_record_bytes(member)
        if member.assumption is not None:
            # Rebuilt bit-identically: the registered 2.0.0 constant, not a manifest-version twin.
            assert member.assumption == backfill.ASSUMPTION_BINDING
            assert member.assumption.schema_version == PHASE1_PUBLICATION_VERSION
            assert line.count(b'"schema_version"') == 1 and nested in line
        else:
            assert b"schema_version" not in line

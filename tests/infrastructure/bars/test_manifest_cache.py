"""Opt-in cache of verified manifest loads (``infrastructure.bars.verified``; debugging pass, perf).

A proof (``DatasetBuilder.verify_manifest`` via the builder's ``ManifestStore``) is reused only by
the same builder over the same catalog, for the same manifest hash, while every head the verifier
reads unpinned is unchanged. Verifications are counted by wrapping the real
``DatasetBuilder.verify_manifest`` (it still runs; nothing under test is faked). Same SQLite
``World`` fixtures as ``test_dataset_bars.py``; the PostgreSQL module imports these functions.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from pyiceberg.expressions import AlwaysFalse, AlwaysTrue, BooleanExpression

from core.contracts.catalog import CommitRequest, CommitResult, SnapshotInfo, TableInfo
from core.contracts.universe import ResearchDatasetManifest
from infrastructure.bars import (
    HEAD_READ_TABLES,
    VerifiedManifestCache,
    backtest_bars_from_dataset,
    load_verified_manifest,
    outcome_request_from_dataset,
    pair_manifests,
    verification_head_tables,
)
from infrastructure.catalog.definitions import RegisteredTableDefinition
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    DATASET_MANIFESTS,
    DATASET_SELECTIONS,
    QUALITY_EVIDENCE_GAPS,
)
from infrastructure.dataset.builder import DatasetBuilder, DatasetBuilt
from infrastructure.feature.dataset import DatasetBindingError, load_manifest
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision.store import RevisionCatalog
from tests.infrastructure.bars.test_dataset_bars import (
    BTC,
    DAY_WINDOW,
    EVENTS,
    FORWARD,
    TAMPERED,
    _assumed,
    _dataset,
)
from tests.infrastructure.bars.test_manifest_pair import _feature, _ingest, _price
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.redteam import redteam_support as rt
from tests.infrastructure.redteam.test_rt_arrival_orders import Arrivals
from tests.infrastructure.revision.rest_store_support import utc


@pytest.fixture
def w(tmp_path: Path) -> Iterator[World]:
    with ds.sqlite_world(tmp_path / "w") as opened:
        yield opened


@pytest.fixture
def other(tmp_path: Path) -> Iterator[World]:
    """A second, independent catalog."""
    with ds.sqlite_world(tmp_path / "other") as opened:
        yield opened


@pytest.fixture
def verified(monkeypatch: pytest.MonkeyPatch) -> list[DatasetBuilder]:
    """The builder of every real ``verify_manifest`` call, in order."""
    calls: list[DatasetBuilder] = []
    real = DatasetBuilder.verify_manifest

    def counting(self: DatasetBuilder, manifest: ResearchDatasetManifest) -> None:
        calls.append(self)
        real(self, manifest)

    monkeypatch.setattr(DatasetBuilder, "verify_manifest", counting)
    return calls


def _bars(w: World, builder: DatasetBuilder, manifest_hash: str, cache: Any) -> Any:
    return backtest_bars_from_dataset(
        w.h.adapter,
        w.h.storage,
        builder=builder,
        manifest_content_hash=manifest_hash,
        manifest_cache=cache,
    )


def _outcome(w: World, builder: DatasetBuilder, manifest_hash: str, cache: Any) -> Any:
    return outcome_request_from_dataset(
        w.h.adapter,
        w.h.storage,
        builder=builder,
        manifest_content_hash=manifest_hash,
        symbol=BTC,
        label_spec=FORWARD,
        events=EVENTS,
        manifest_cache=cache,
    )


def _copy_head_row(w: World, definition: RegisteredTableDefinition, batch_id: str) -> None:
    """A new head of ``definition`` (one existing row committed again, bypassing the stores)."""
    before = w.h.head(definition.table)
    w.h.forge_rows(definition, w.h.rows(definition)[:1], batch_id=batch_id)
    assert w.h.head(definition.table) != before


def _another_manifest_row(w: World, built: DatasetBuilt) -> None:
    ids = (*built.manifest.quality_report_ids, "qr-another")
    another = built.manifest.model_copy(update={"quality_report_ids": ids})
    assert another.content_hash() != built.manifest.content_hash()
    w.h.forge_rows(DATASET_MANIFESTS, [rt.manifest_row(another)], batch_id="another-manifest")


MOVES: dict[str, Callable[[World, DatasetBuilt], None]] = {
    "manifests": _another_manifest_row,
    "dataset-table": lambda w, _: _copy_head_row(w, DATASET_SELECTIONS, "moved-dataset"),
    "precedence-evidence": lambda w, _: _copy_head_row(
        w, BINANCE_SPOT_PRECEDENCE_EVIDENCE, "moved-evidence"
    ),
    "evidence-gaps": lambda w, _: _copy_head_row(w, QUALITY_EVIDENCE_GAPS, "moved-gaps"),
}


# ---------------------------------------------------------------------------------------------
# hits


def test_a_hit_returns_the_same_manifest_without_a_second_verification(
    w: World, verified: list[DatasetBuilder]
) -> None:
    built = _dataset(w)
    genuine = built.manifest.content_hash()
    builder, cache = w.builder(), VerifiedManifestCache()
    first = cache.load(builder, genuine)
    assert first == built.manifest and verified == [builder]
    assert cache.load(builder, genuine) is first
    assert load_verified_manifest(builder, genuine, cache) is first
    assert len(verified) == 1
    stats = cache.stats
    assert (stats.hits, stats.misses, stats.stored, stats.uncacheable) == (2, 1, 1, 0)
    # the bar entry points reuse the proof: same bars, still one verification
    with_cache = _bars(w, builder, genuine, cache)
    request = _outcome(w, builder, genuine, cache)
    assert len(verified) == 1
    assert with_cache == _bars(w, builder, genuine, None)
    assert request == _outcome(w, builder, genuine, None)
    assert len(verified) == 3  # no cache: every load verifies


def test_without_a_cache_every_load_verifies(w: World, verified: list[DatasetBuilder]) -> None:
    built = _dataset(w)
    genuine = built.manifest.content_hash()
    builder = w.builder()
    assert load_verified_manifest(builder, genuine) == load_manifest(builder, genuine)
    assert load_verified_manifest(builder, genuine, None) == built.manifest
    assert len(verified) == 3


def test_a_pair_reuses_both_proofs(w: World, verified: list[DatasetBuilder]) -> None:
    _ingest(w)
    feature, price = _feature(w), _price(w)
    hashes = (feature.manifest.content_hash(), price.manifest.content_hash())
    builder, cache = w.builder(), VerifiedManifestCache()
    pair = pair_manifests(builder, *hashes, manifest_cache=cache)
    assert len(verified) == 2
    assert pair_manifests(builder, *hashes, manifest_cache=cache) == pair
    assert len(verified) == 2
    assert pair_manifests(builder, *hashes) == pair
    assert len(verified) == 4


# ---------------------------------------------------------------------------------------------
# misses: new snapshots, another manifest


@pytest.mark.parametrize("move", sorted(MOVES))
def test_a_new_snapshot_of_a_table_read_at_its_head_misses(
    w: World, verified: list[DatasetBuilder], move: str
) -> None:
    built = _dataset(w)
    genuine = built.manifest.content_hash()
    builder, cache = w.builder(), VerifiedManifestCache()
    assert cache.load(builder, genuine) == built.manifest
    MOVES[move](w, built)
    again = cache.load(builder, genuine)  # a miss: the full verified load runs again
    assert again == built.manifest and len(verified) == 2
    assert cache.load(builder, genuine) is again  # re-proven at the new head: a hit
    assert len(verified) == 2
    assert (cache.stats.misses, cache.stats.stored) == (2, 2)


def test_another_manifest_with_other_pinned_snapshots_is_its_own_proof(
    w: World, verified: list[DatasetBuilder]
) -> None:
    built = _dataset(w)
    builder, cache = w.builder(), VerifiedManifestCache()
    cache.load(builder, built.manifest.content_hash())
    w.more_trades()  # canonical.trades and the shared Raw REST tables move on: new heads
    w.report("klines_1m")  # the bar reports of the new heads (a build needs them)
    newer = rt.build(w, _assumed(w.spec(skip=rt.OWN)), data_type="klines_1m", window=DAY_WINDOW)
    assert newer.manifest.point_in_time.snapshot_bindings != (
        built.manifest.point_in_time.snapshot_bindings
    )
    assert cache.load(builder, newer.manifest.content_hash()) == newer.manifest
    assert len(verified) == 2 and len(cache) == 2


def test_the_replay_path_is_re_proven_when_its_bound_if_present_table_appears(
    w: World, verified: list[DatasetBuilder]
) -> None:
    """G2 RT-5's replay: the evidence table's first snapshot is read at its head, so it misses."""
    w.listed()
    arrivals = Arrivals(w)
    for name in ("IR", "NR"):
        arrivals.step(name)
    rt.report(w, at=utc(2023, 12, 15))
    rest_only = rt.build(w)
    evidence = BINANCE_SPOT_PRECEDENCE_EVIDENCE.table
    assert evidence not in rest_only.manifest.point_in_time.snapshot_bindings
    genuine = rest_only.manifest.content_hash()
    builder, cache = w.builder(), VerifiedManifestCache()
    assert cache.load(builder, genuine) == rest_only.manifest
    for name in ("IA", "NA", "RC"):
        arrivals.step(name)
    assert w.h.head(evidence) is not None  # the first edge: judged by the replay path now
    assert cache.load(builder, genuine) == rest_only.manifest
    assert len(verified) == 2


# ---------------------------------------------------------------------------------------------
# refusals stay refusals


def _forge_under(w: World, built: DatasetBuilt) -> str:
    genuine = built.manifest.content_hash()
    ids = (*built.manifest.quality_report_ids, "qr-forged")
    forged = built.manifest.model_copy(update={"quality_report_ids": ids})
    row = dict(rt.manifest_row(forged), manifest_content_hash=genuine)
    w.h.forge_rows(DATASET_MANIFESTS, [row], batch_id="forged-manifest")
    return genuine


def test_a_forged_manifest_is_still_refused_with_the_cache_on(
    w: World, verified: list[DatasetBuilder]
) -> None:
    built = _dataset(w)
    builder, cache = w.builder(), VerifiedManifestCache()
    genuine = cache.load(builder, built.manifest.content_hash()).content_hash()
    _forge_under(w, built)  # a second row under the cached hash: the manifests head moves
    for _ in range(2):
        with pytest.raises(CatalogIntegrityError):
            cache.load(builder, genuine)
        with pytest.raises(CatalogIntegrityError):
            _bars(w, builder, genuine, cache)
        with pytest.raises(CatalogIntegrityError):
            _outcome(w, builder, genuine, cache)
    assert cache.stats.hits == 0 and cache.stats.stored == 1


@pytest.mark.parametrize("attack", sorted(TAMPERED))
def test_a_manifest_that_does_not_load_is_never_stored(
    w: World, verified: list[DatasetBuilder], attack: str
) -> None:
    built = _dataset(w)
    manifest_hash = TAMPERED[attack](w, built)
    builder, cache = w.builder(), VerifiedManifestCache()
    for _ in range(2):
        with pytest.raises((DatasetBindingError, CatalogIntegrityError)):
            cache.load(builder, manifest_hash)
        with pytest.raises((DatasetBindingError, CatalogIntegrityError)):
            _bars(w, builder, manifest_hash, cache)
    assert len(cache) == 0 and cache.stats.hits == 0


def test_a_verification_that_fails_is_not_stored(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    built = _dataset(w)
    genuine = built.manifest.content_hash()
    builder, cache = w.builder(), VerifiedManifestCache()

    def refuses(self: DatasetBuilder, manifest: ResearchDatasetManifest) -> None:
        raise CatalogIntegrityError("does not prove")

    with monkeypatch.context() as patched:
        patched.setattr(DatasetBuilder, "verify_manifest", refuses)
        with pytest.raises(CatalogIntegrityError, match="does not prove"):
            cache.load(builder, genuine)
    assert len(cache) == 0
    assert cache.load(builder, genuine) == built.manifest  # proven now, by the real verifier
    assert len(cache) == 1


def test_a_non_builder_verifier_is_refused_with_the_cache_on(w: World) -> None:
    built = _dataset(w)

    class _Trusting:
        def manifests(self) -> Any:
            return self

        def load(self, _: str) -> Any:
            return built.manifest

    cache = VerifiedManifestCache()
    with pytest.raises(DatasetBindingError, match="DatasetBuilder"):
        cache.load(_Trusting(), built.manifest.content_hash())  # type: ignore[arg-type]
    assert len(cache) == 0 and cache.stats.uncacheable == 1


# ---------------------------------------------------------------------------------------------
# catalog / verifier identity


def test_a_cache_shared_across_two_catalogs_misses(
    w: World, other: World, verified: list[DatasetBuilder]
) -> None:
    here, there = _dataset(w), _dataset(other)
    ours, theirs = w.builder(), other.builder()
    cache = VerifiedManifestCache()
    here_hash, there_hash = here.manifest.content_hash(), there.manifest.content_hash()
    assert here_hash != there_hash  # the two catalogs' own dataset snapshots differ
    cache.load(ours, here_hash)
    # the proof made on our catalog is never served for theirs: their store has no such row
    with pytest.raises(DatasetBindingError, match="no Research Dataset manifest"):
        cache.load(theirs, here_hash)
    assert cache.load(theirs, there_hash) == there.manifest
    assert verified == [ours, theirs]
    assert cache.load(ours, here_hash) == here.manifest  # each catalog's proof is its own
    assert cache.load(theirs, there_hash) == there.manifest
    assert len(verified) == 2 and len(cache) == 2


def test_another_builder_or_a_reopened_catalog_misses(
    w: World, verified: list[DatasetBuilder]
) -> None:
    built = _dataset(w)
    genuine = built.manifest.content_hash()
    first, cache = w.builder(), VerifiedManifestCache()
    cache.load(first, genuine)
    second = w.builder()  # the same catalog object, another verifier object
    assert cache.load(second, genuine) == built.manifest
    w.h.reopen()  # a fresh adapter on the same catalog: what a process restart sees
    third = w.builder()
    assert cache.load(third, genuine) == built.manifest
    assert verified == [first, second, third]


# ---------------------------------------------------------------------------------------------
# the key covers every head the verifier reads


class _Recording:
    """A ``RevisionCatalog`` proxy recording every table read at its current head."""

    def __init__(self, inner: RevisionCatalog) -> None:
        self._inner = inner
        self.head_reads: set[str] = set()

    def load_table(self, table: str) -> TableInfo | None:
        # A pinned view replaces the head with the bound snapshot (or none): not a head read.
        if sys._getframe(1).f_code is not PinnedCatalogView.load_table.__code__:
            self.head_reads.add(table)
        return self._inner.load_table(table)

    def get_snapshot(self, table: str, snapshot_id: str) -> SnapshotInfo:
        return self._inner.get_snapshot(table, snapshot_id)  # an id: immutable

    def scan_columns(
        self,
        table: str,
        *,
        columns: Sequence[str],
        row_filter: BooleanExpression = AlwaysTrue(),  # noqa: B008 - immutable singleton
        limit: int | None = None,
        snapshot_id: str | None = None,
    ) -> pa.Table:
        if snapshot_id is None and not isinstance(row_filter, AlwaysFalse):
            self.head_reads.add(table)
        return self._inner.scan_columns(
            table, columns=columns, row_filter=row_filter, limit=limit, snapshot_id=snapshot_id
        )

    def scan_column_batches(
        self,
        table: str,
        *,
        columns: Sequence[str],
        row_filter: BooleanExpression = AlwaysTrue(),  # noqa: B008 - immutable singleton
        snapshot_id: str | None = None,
    ) -> Iterator[pa.RecordBatch]:
        if snapshot_id is None and not isinstance(row_filter, AlwaysFalse):
            self.head_reads.add(table)
        return self._inner.scan_column_batches(
            table, columns=columns, row_filter=row_filter, snapshot_id=snapshot_id
        )

    def commit_batch(self, request: CommitRequest, batch: pa.Table) -> CommitResult:
        raise AssertionError("a verified load never writes")

    def max_int64(self, *args: Any, **kwargs: Any) -> int | None:
        raise AssertionError("a verified load never allocates")


def _recorded_load(w: World, manifest_hash: str) -> set[str]:
    recording = _Recording(w.h.adapter)
    builder = DatasetBuilder(
        recording,
        w.h.storage,
        market_data_base_url=ds.ORIGIN,
        dataset_table=DATASET_SELECTIONS,
    )
    load_manifest(builder, manifest_hash)
    return recording.head_reads


def test_the_verifier_reads_no_other_table_at_its_head(w: World) -> None:
    """If this fails, ``verify_manifest`` gained an unpinned read: ``HEAD_READ_TABLES`` (the
    cache key) must grow with it, or the cache could serve a stale proof."""
    built = _dataset(w)
    reads = _recorded_load(w, built.manifest.content_hash())
    assert DATASET_MANIFESTS.table in reads  # the recording sees the store's own row read
    assert reads <= set(verification_head_tables(built.manifest.dataset.table))
    assert set(HEAD_READ_TABLES) <= set(verification_head_tables(DATASET_SELECTIONS.table))


def test_the_replay_path_reads_no_other_table_at_its_head(w: World) -> None:
    w.listed()
    arrivals = Arrivals(w)
    for name in ("IR", "NR"):
        arrivals.step(name)
    rt.report(w, at=utc(2023, 12, 15))
    rest_only = rt.build(w)
    for name in ("IA", "NA", "RC"):
        arrivals.step(name)
    reads = _recorded_load(w, rest_only.manifest.content_hash())
    # the replay path ran: the bound-if-present table and the dataset history were read at head
    assert {BINANCE_SPOT_PRECEDENCE_EVIDENCE.table, DATASET_SELECTIONS.table} <= reads
    assert reads <= set(verification_head_tables(rest_only.manifest.dataset.table))

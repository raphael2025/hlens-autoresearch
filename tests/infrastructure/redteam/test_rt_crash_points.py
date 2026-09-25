"""G2 / a crash between every commit of a whole Phase 1 run, then a rerun.

One run = exchangeInfo snapshot + listing derivation (E2), a two-trade archive (D2, one row per
batch), the same trades over REST (D3D / D3E, one element per batch), both normalized one row per
batch (E1), the D-33 reconcile, three quality reports with their gap batches (E3) and the build
(F3: selection batch, then manifest) — 19 catalog commits. The process dies right after commit
``k``; a fresh process reruns everything from the top. The result must equal an uninterrupted
run (same rows, same manifest up to catalog-local ids, no duplicate row anywhere), and while the
run is dead no reader may find a dataset.

The last three tests are the orphan states themselves: a unit left half-written by a crash, read
before any rerun completes it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    DATASET_MANIFESTS,
    DATASET_SELECTIONS,
    PHASE1_TABLES,
)
from infrastructure.dataset.builder import DatasetBuilt
from infrastructure.dataset.manifests import ManifestStore
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.redteam import redteam_support as rt
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import (
    Crash,
    ProxyCatalog,
    crash_after_commits,
)

COMMITS: Final = 19
#: Primary identity column of each table (absent: the table's rows are compared by count only).
_IDENTITY: Final = ("revision_id", "report_id", "edge_id", "manifest_content_hash")


def _pipeline(w: World) -> DatasetBuilt:
    """Every stage once, with fixed clocks; each stage is its own idempotent step."""
    w.listed()
    items = ss.agg_items(2)
    archive = rt.archived(
        rt.archive_trades(w, ss.archive_agg_lines(items), knowledge=ds.K_A, microbatch_rows=1)
    )
    [response] = rt.rest_trades(w, items, knowledge=ds.K_R, element_microbatch_rows=1)
    rt.normalize(w, c.ARCHIVE_AGGS.table, archive, at=ds.N_A, microbatch_rows=1)
    rt.normalize(w, c.REST_AGGS.table, response, at=ds.N_R, microbatch_rows=1)
    rt.reconcile(w, at=ds.K_E)
    w.report()
    return rt.build(w)


def _use(w: World, adapter: Any) -> None:
    w.h.adapter = adapter
    w.x.adapter = adapter


def _counts(w: World) -> dict[str, int]:
    return {table.table: len(w.h.rows(table)) for table in PHASE1_TABLES}


def _no_duplicates(w: World) -> None:
    for table in PHASE1_TABLES:
        rows = w.h.rows(table)
        for column in _IDENTITY:
            if rows and column in rows[0]:
                values = [row[column] for row in rows]
                assert len(values) == len(set(values)), (table.table, column)
                break


@pytest.fixture(scope="module")
def reference(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[DatasetBuilt, Any]]:
    with ds.sqlite_world(tmp_path_factory.mktemp("uninterrupted")) as w:
        proxy = ProxyCatalog(w.h.adapter)
        _use(w, proxy)
        built = _pipeline(w)
        assert len(proxy.commits) == COMMITS
        yield built, _counts(w)


@pytest.mark.parametrize("k", range(1, COMMITS + 1))
def test_a_crash_after_any_commit_then_a_rerun_equals_an_uninterrupted_run(
    tmp_path: Path, k: int, reference: tuple[DatasetBuilt, dict[str, int]]
) -> None:
    expected, expected_counts = reference
    with ds.sqlite_world(tmp_path) as w:
        real = w.h.adapter
        proxy = ProxyCatalog(real, after=crash_after_commits(k))
        _use(w, proxy)
        with pytest.raises(Crash):
            _pipeline(w)
        _use(w, w.h.reopen())  # a fresh process

        # While the run is dead no reader can find a dataset: the manifest is the last commit.
        orphan = w.h.head(DATASET_SELECTIONS.table)
        assert (orphan is not None) == (k >= COMMITS - 1)
        assert w.h.rows(DATASET_MANIFESTS) == [] or k == COMMITS
        assert ManifestStore(w.h.adapter, w.builder()).load(
            expected.manifest.content_hash()
        ) is None or (k == COMMITS)

        rerun = _pipeline(w)
        assert rt.dataset_rows(rerun) == rt.dataset_rows(expected)
        assert rt.shape(rerun.manifest) == rt.shape(expected.manifest)
        assert _counts(w) == expected_counts  # nothing was written twice
        _no_duplicates(w)
        if orphan is not None:
            # The orphan batch is adopted (replayed), never re-materialized next to itself.
            assert rerun.dataset_commit.snapshot_id == orphan
            assert rerun.dataset_commit.replayed
        # One more rerun commits nothing and returns the very same manifest.
        again = rt.build(w, rerun.manifest.point_in_time)
        assert again.replayed and again.manifest == rerun.manifest
        assert len(w.h.rows(DATASET_MANIFESTS)) == 1


def test_a_crash_inside_the_archive_store_leaves_a_unit_no_reader_accepts(w: World) -> None:
    """Contrast (refused today): an archive whose rows stopped half-way is truncated for E1."""
    w.listed()
    proxy = ProxyCatalog(w.h.adapter, after=crash_after_commits(2, table=c.ARCHIVE_AGGS.table))
    with pytest.raises(Crash):
        rt.archive_trades(
            w, ss.archive_agg_lines(ss.agg_items(3)), knowledge=ds.K_A, adapter=proxy,
            microbatch_rows=1,
        )  # fmt: skip
    [archive] = w.h.rows(c.ARCHIVES)
    assert len(w.h.rows(c.ARCHIVE_AGGS)) == 2
    with pytest.raises(CatalogIntegrityError, match="not exactly the 3 lines"):
        rt.normalize(w, c.ARCHIVE_AGGS.table, archive["revision_id"], at=ds.N_A)
    assert w.h.rows(c.TRADES) == []


def test_a_crash_inside_the_normalizer_leaves_no_dataset_behind(w: World) -> None:
    w.listed()
    archive = rt.archived(
        rt.archive_trades(w, ss.archive_agg_lines(ss.agg_items(3)), knowledge=ds.K_A)
    )
    proxy = ProxyCatalog(w.h.adapter, after=crash_after_commits(1, table=c.TRADES.table))
    with pytest.raises(Crash):
        rt.normalize(w, c.ARCHIVE_AGGS.table, archive, at=ds.N_A, adapter=proxy, microbatch_rows=1)
    assert len(w.h.rows(c.TRADES)) == 1 < len(w.h.rows(c.ARCHIVE_AGGS)) == 3
    before = rt.outputs(w)
    with pytest.raises(rt.REFUSALS):
        w.report()
        rt.build(w)
    assert rt.outputs(w) == before


def test_a_crash_inside_the_rest_store_leaves_no_dataset_behind(w: World) -> None:
    w.listed()
    items = ss.agg_items(2)
    proxy = ProxyCatalog(w.h.adapter, after=crash_after_commits(2, table=None))
    with pytest.raises(Crash):
        rt.rest_trades(w, items, knowledge=ds.K_R, adapter=proxy, element_microbatch_rows=1)
    [response] = w.h.rows(c.RESPONSES)
    assert len(w.h.rows(c.REST_AGGS)) == 1 < len(items)  # one element of the page is missing
    before = rt.outputs(w)
    with pytest.raises(rt.REFUSALS):
        rt.normalize(w, c.REST_AGGS.table, response["revision_id"], at=ds.N_R)
        w.report()
        rt.build(w)
    assert rt.outputs(w) == before

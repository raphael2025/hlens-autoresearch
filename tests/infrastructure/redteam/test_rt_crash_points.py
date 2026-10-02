"""G2 / a crash between every commit of a whole Phase 1 run, then a rerun.

One run = exchangeInfo snapshot + listing derivation (E2), a two-trade archive (D2, one row per
batch), the same trades over REST (D3D / D3E, one element per batch), both normalized one row per
batch (E1), the D-33 reconcile, three quality reports with their gap batches (E3) and the build
(F3: selection batch, then manifest). Under ADR-0108 each archive / normalized unit is one
snapshot (16 catalog commits); the pre-ADR-0108 per-batch layout, which old history keeps and
which must still recover (ADR-0108 §7), is 19. The process dies right after commit ``k``; a
fresh process reruns everything from the top. The result must equal an uninterrupted
run (same rows, same manifest up to catalog-local ids, no duplicate row anywhere), and while the
run is dead no reader may find a dataset.

The tests after the matrix are the orphan states themselves: a unit left half-written (per-batch
layout), staged but never committed (unit layout), or, for a REST page, not written at all by a
crash, read before any rerun completes it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from core.contracts.catalog import CommitRequest
from infrastructure.canonical.normalizer import CanonicalUnitIncomplete
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    DATA_QUALITY_REPORTS,
    DATASET_MANIFESTS,
    DATASET_SELECTIONS,
    PHASE1_TABLES,
)
from infrastructure.dataset.builder import DatasetBuilt
from infrastructure.dataset.manifests import ManifestStore
from infrastructure.quality.reporter import QualityReporter
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

#: Catalog commits of one whole run per layout: ADR-0108 unit commits, and the per-batch layout.
COMMITS: Final = {"unit": 16, "legacy": 19}
#: Primary identity column of each table (absent: the table's rows are compared by count only).
_IDENTITY: Final = ("revision_id", "report_id", "edge_id", "manifest_content_hash")


def _pipeline(w: World, layout: str) -> DatasetBuilt:
    """Every stage once, with fixed clocks; each stage is its own idempotent step."""
    legacy = layout == "legacy"
    w.listed()
    items = ss.agg_items(2)
    archive = rt.archived(
        rt.archive_trades(
            w, ss.archive_agg_lines(items), knowledge=ds.K_A, microbatch_rows=1, legacy=legacy
        )
    )
    [response] = rt.rest_trades(w, items, knowledge=ds.K_R, element_microbatch_rows=1)
    rt.normalize(w, c.ARCHIVE_AGGS.table, archive, at=ds.N_A, microbatch_rows=1, legacy=legacy)
    rt.normalize(w, c.REST_AGGS.table, response, at=ds.N_R, microbatch_rows=1, legacy=legacy)
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
def references(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, tuple[DatasetBuilt, dict[str, int]]]]:
    built: dict[str, tuple[DatasetBuilt, dict[str, int]]] = {}
    for layout, commits in COMMITS.items():
        with ds.sqlite_world(tmp_path_factory.mktemp(f"uninterrupted-{layout}")) as w:
            proxy = ProxyCatalog(w.h.adapter)
            _use(w, proxy)
            built[layout] = (_pipeline(w, layout), _counts(w))
            assert len(proxy.commits) == commits, layout
    yield built


@pytest.mark.parametrize(
    ("layout", "k"),
    [(layout, k) for layout, commits in COMMITS.items() for k in range(1, commits + 1)],
)
def test_a_crash_after_any_commit_then_a_rerun_equals_an_uninterrupted_run(
    tmp_path: Path,
    layout: str,
    k: int,
    references: dict[str, tuple[DatasetBuilt, dict[str, int]]],
) -> None:
    expected, expected_counts = references[layout]
    commits = COMMITS[layout]
    with ds.sqlite_world(tmp_path) as w:
        real = w.h.adapter
        proxy = ProxyCatalog(real, after=crash_after_commits(k))
        _use(w, proxy)
        with pytest.raises(Crash):
            _pipeline(w, layout)
        _use(w, w.h.reopen())  # a fresh process

        # While the run is dead no reader can find a dataset: the manifest is the last commit.
        orphan = w.h.head(DATASET_SELECTIONS.table)
        assert (orphan is not None) == (k >= commits - 1)
        assert w.h.rows(DATASET_MANIFESTS) == [] or k == commits
        assert ManifestStore(w.h.adapter, w.builder()).load(
            expected.manifest.content_hash()
        ) is None or (k == commits)

        rerun = _pipeline(w, layout)
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


def _crash_before(table: str) -> Any:
    """A ``before`` hook that dies before ``table``'s commit: staged, nothing committed."""

    def hook(request: CommitRequest) -> None:
        if request.table == table:
            raise Crash(f"crash before the commit of {request.batch_id}")

    return hook


def test_a_crash_inside_the_archive_store_leaves_a_unit_no_reader_accepts(w: World) -> None:
    """Contrast (refused today): an archive whose rows stopped half-way is truncated for E1.

    Per-batch layout (old history, ADR-0108 §7): the store wrote 2 of 3 element batches.
    """
    w.listed()
    proxy = ProxyCatalog(w.h.adapter, after=crash_after_commits(2, table=c.ARCHIVE_AGGS.table))
    with pytest.raises(Crash):
        rt.archive_trades(
            w, ss.archive_agg_lines(ss.agg_items(3)), knowledge=ds.K_A, adapter=proxy,
            microbatch_rows=1, legacy=True,
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
        rt.normalize(
            w, c.ARCHIVE_AGGS.table, archive, at=ds.N_A, adapter=proxy, microbatch_rows=1,
            legacy=True,
        )  # fmt: skip
    assert len(w.h.rows(c.TRADES)) == 1 < len(w.h.rows(c.ARCHIVE_AGGS)) == 3
    before = rt.outputs(w)
    with pytest.raises(rt.REFUSALS):
        w.report()
        rt.build(w)
    assert rt.outputs(w) == before


def test_a_crash_before_the_archive_unit_commit_leaves_no_element_row(w: World) -> None:
    """ADR-0108 unit layout: a crash after staging, before the one commit, shows no element row;
    E1 refuses the archive until the store's rerun commits the whole unit."""
    w.listed()
    lines = ss.archive_agg_lines(ss.agg_items(3))
    proxy = ProxyCatalog(w.h.adapter, before=_crash_before(c.ARCHIVE_AGGS.table))
    with pytest.raises(Crash):
        rt.archive_trades(w, lines, knowledge=ds.K_A, adapter=proxy, microbatch_rows=1)
    [archive] = w.h.rows(c.ARCHIVES)
    assert w.h.rows(c.ARCHIVE_AGGS) == []
    with pytest.raises(CanonicalUnitIncomplete, match="none is committed"):
        rt.normalize(w, c.ARCHIVE_AGGS.table, archive["revision_id"], at=ds.N_A)
    assert w.h.rows(c.TRADES) == []
    rerun = rt.archived(rt.archive_trades(w, lines, knowledge=ds.K_A, microbatch_rows=1))
    assert rerun == archive["revision_id"] and len(w.h.rows(c.ARCHIVE_AGGS)) == 3


def test_an_archive_that_died_before_its_first_element_batch_is_incomplete(w: World) -> None:
    """Per-batch layout (old history): the source row is committed, no element batch is."""
    w.listed()
    proxy = ProxyCatalog(w.h.adapter, before=_crash_before(c.ARCHIVE_AGGS.table))
    with pytest.raises(Crash):
        rt.archive_trades(
            w, ss.archive_agg_lines(ss.agg_items(3)), knowledge=ds.K_A, adapter=proxy,
            microbatch_rows=1, legacy=True,
        )  # fmt: skip
    [archive] = w.h.rows(c.ARCHIVES)
    assert w.h.rows(c.ARCHIVE_AGGS) == []
    with pytest.raises(CanonicalUnitIncomplete, match="none is committed"):
        rt.normalize(w, c.ARCHIVE_AGGS.table, archive["revision_id"], at=ds.N_A, legacy=True)
    assert w.h.rows(c.TRADES) == []


def test_a_crash_before_the_normalizer_unit_commit_leaves_no_dataset_behind(w: World) -> None:
    """ADR-0108 unit layout: staged Canonical files are orphans; nothing is readable or built."""
    w.listed()
    archive = rt.archived(
        rt.archive_trades(w, ss.archive_agg_lines(ss.agg_items(3)), knowledge=ds.K_A)
    )
    proxy = ProxyCatalog(w.h.adapter, before=_crash_before(c.TRADES.table))
    with pytest.raises(Crash):
        rt.normalize(w, c.ARCHIVE_AGGS.table, archive, at=ds.N_A, adapter=proxy, microbatch_rows=1)
    assert w.h.rows(c.TRADES) == [] and len(w.h.rows(c.ARCHIVE_AGGS)) == 3
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


def test_a_rest_page_that_died_before_its_first_element_is_refused_until_rerun(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RT-3 residual (G2-R3a): the response row is committed, none of its elements is.

    With no Raw element row there is nothing for E3's derivation check to miss, so before
    G2-R3a the partition was reported (and built) from the archive alone while a committed page
    of the same trades was incomplete. Now E3 judges every bound page of the day with the
    normalizer's completeness rule, and F3 re-derives that verdict for a committed report —
    even one an older reporter wrote without the check.
    """
    w.listed()
    items = ss.agg_items(2)
    archive = rt.archived(rt.archive_trades(w, ss.archive_agg_lines(items), knowledge=ds.K_A))
    rt.normalize(w, c.ARCHIVE_AGGS.table, archive, at=ds.N_A)
    proxy = ProxyCatalog(w.h.adapter, after=crash_after_commits(1, table=c.RESPONSES.table))
    with pytest.raises(Crash):
        rt.rest_trades(w, items, knowledge=ds.K_R, adapter=proxy)
    [response] = w.h.rows(c.RESPONSES)
    assert w.h.rows(c.REST_AGGS) == []  # died before the first element batch
    before = rt.outputs(w)
    with pytest.raises(CanonicalUnitIncomplete, match=r"element\(s\) \[0, 1\] are committed"):
        w.report()
    with pytest.raises(rt.REFUSALS):
        rt.build(w)
    assert w.h.rows(DATA_QUALITY_REPORTS) == [] and rt.outputs(w) == before
    # A report written without the page check (an older reporter) does not let F3 through.
    with monkeypatch.context() as patched:
        patched.setattr(QualityReporter, "_check_rest_pages", lambda *_: None)
        w.report()
    with pytest.raises(CanonicalUnitIncomplete, match=response["revision_id"]):
        rt.build(w)
    assert rt.outputs(w) == before

    # The store's rerun completes the page; normalized and reconciled, the partition builds.
    w.h.store(clock=ss.StepClock(start=ds.K_R)).ingest_collection(
        ss.agg_request("req-rest", start_ms=ss.T0)
    )
    assert len(w.h.rows(c.REST_AGGS)) == len(items)
    rt.normalize(w, c.REST_AGGS.table, response["revision_id"], at=ds.N_R)
    rt.reconcile(w, at=ds.K_E)
    w.report()
    built = rt.build(w)
    assert len(built.selection.rows) == len(items)
    assert len(rt.manifests(w)) == 1

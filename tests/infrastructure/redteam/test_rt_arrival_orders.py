"""G2 / out-of-order arrivals: archive and REST in every order, builds in between.

Five steps — ingest archive (IA), ingest REST (IR), normalize each (NA, NR), reconcile (RC) —
run in every order the dependencies allow, each step one day after the previous (knowledge follows
arrival). Once everything is known, every order must yield the same dataset: the same rows, and
the same manifest up to the catalog-local snapshot / report ids. Builds taken *between* arrivals
must fail closed while two unreconciled heads exist and keep reproducing afterwards.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pyiceberg.expressions import EqualTo

from infrastructure.catalog.phase1_tables import DATASET_MANIFESTS
from infrastructure.dataset.builder import DatasetSpecError
from infrastructure.dataset.manifests import ManifestStore
from infrastructure.pit.selector import PitConflictError
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import START, World
from tests.infrastructure.redteam import redteam_support as rt
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import utc

STEPS = ("IA", "IR", "NA", "NR", "RC")
BASE = utc(2023, 11, 25)
INTERVAL = (START, utc(2024, 1, 1))


def _lawful(order: tuple[str, ...]) -> bool:
    at = {step: index for index, step in enumerate(order)}
    return at["IA"] < at["NA"] and at["IR"] < at["NR"] and at["RC"] > max(at["IA"], at["IR"])


ORDERS = [order for order in itertools.permutations(STEPS) if _lawful(order)]


class Arrivals:
    """Runs the steps against one world; step ``i`` knows ``BASE + i`` days."""

    def __init__(self, w: World) -> None:
        self.w = w
        self.items = ss.agg_items(3)
        self.units: dict[str, str] = {}
        self.index = 0

    def at(self) -> Any:
        return BASE + timedelta(days=self.index)

    def step(self, name: str) -> None:
        actions: dict[str, Callable[[], None]] = {
            "IA": self._ingest_archive,
            "IR": self._ingest_rest,
            "NA": lambda: self._normalize(c.ARCHIVE_AGGS.table, "IA"),
            "NR": lambda: self._normalize(c.REST_AGGS.table, "IR"),
            "RC": lambda: rt.reconcile(self.w, at=self.at()),
        }
        actions[name]()
        self.index += 1

    def _ingest_archive(self) -> None:
        lines = ss.archive_agg_lines(self.items)
        self.units["IA"] = rt.archived(rt.archive_trades(self.w, lines, knowledge=self.at()))

    def _ingest_rest(self) -> None:
        [response] = rt.rest_trades(self.w, self.items, knowledge=self.at())
        self.units["IR"] = response

    def _normalize(self, table: str, unit: str) -> None:
        rt.normalize(self.w, table, self.units[unit], at=self.at())


def _final(w: World) -> tuple[Any, Any]:
    rt.report(w, at=utc(2023, 12, 15))
    sim = utc(2023, 12, 20)
    return rt.build(w, at=sim, cutoff=sim), rt.build(w, interval=(INTERVAL[0], sim), cutoff=sim)


@pytest.fixture(scope="module")
def reference(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Any, Any]]:
    """Archive first, normalized, REST, normalized, reconciled: the textbook order."""
    with ds.sqlite_world(tmp_path_factory.mktemp("reference")) as w:
        w.listed()
        arrivals = Arrivals(w)
        for name in ("IA", "NA", "IR", "NR", "RC"):
            arrivals.step(name)
        point, interval = _final(w)
        yield point, interval


def test_the_orders_are_all_lawful_arrival_orders() -> None:
    assert len(ORDERS) == len(set(ORDERS)) == 16
    assert ("IR", "NR", "IA", "NA", "RC") in ORDERS and ("IA", "IR", "RC", "NR", "NA") in ORDERS


@pytest.mark.parametrize("order", ORDERS, ids=["-".join(order) for order in ORDERS])
def test_every_arrival_order_builds_the_same_dataset(
    tmp_path: Path, order: tuple[str, ...], reference: tuple[Any, Any]
) -> None:
    with ds.sqlite_world(tmp_path) as w:
        w.listed()
        arrivals = Arrivals(w)
        for name in order:
            arrivals.step(name)
        point, interval = _final(w)
    for built, expected in zip((point, interval), reference, strict=True):
        assert rt.dataset_rows(built) == rt.dataset_rows(expected), order
        assert rt.shape(built.manifest) == rt.shape(expected.manifest), order
    # Every selected revision is the archive copy (the D-33 edge), in every order.
    lineage = {item.canonical_revision_id: item.raw_table for item in point.manifest.lineage}
    assert {lineage[row["revision_id"]] for row in point.selection.rows} == {c.ARCHIVE_AGGS.table}


def test_rest_first_then_archive_conflicts_until_reconciled(w: World) -> None:
    w.listed()
    arrivals = Arrivals(w)
    for name in ("IR", "NR"):
        arrivals.step(name)
    rt.report(w, at=utc(2023, 12, 15))
    rest_only = rt.build(w)  # REST pages are all that is known: a REST-only dataset
    for name in ("IA", "NA"):
        arrivals.step(name)
    rt.report(w, at=utc(2023, 12, 16))
    before = rt.outputs(w)
    with pytest.raises(PitConflictError, match="3 observation key"):
        rt.build(w)  # archive and REST heads, no edge yet: no dataset
    assert rt.outputs(w) == before
    arrivals.step("RC")
    rt.report(w, at=utc(2023, 12, 17))
    final = rt.build(w)
    lineage = {item.canonical_revision_id: item.raw_table for item in final.manifest.lineage}
    assert {lineage[row["revision_id"]] for row in final.selection.rows} == {c.ARCHIVE_AGGS.table}
    assert (
        ManifestStore(w.h.adapter, w.builder()).load(rest_only.manifest.content_hash())
        == rest_only.manifest
    )


def test_a_manifest_built_before_the_first_edge_still_reproduces_after_it(w: World) -> None:
    """G2 RT-5 (fixed): the binding requirement is judged at the build, not at today's heads."""
    w.listed()
    arrivals = Arrivals(w)
    for name in ("IR", "NR"):
        arrivals.step(name)
    rt.report(w, at=utc(2023, 12, 15))
    rest_only = rt.build(w)
    assert c.EVIDENCE.table not in rest_only.manifest.point_in_time.snapshot_bindings
    for name in ("IA", "NA", "RC"):
        arrivals.step(name)
    assert w.h.head(c.EVIDENCE.table) is not None  # the first edge now exists
    replay = rt.build(w, rest_only.manifest.point_in_time)
    assert replay.replayed and replay.manifest == rest_only.manifest


def test_after_the_first_edge_the_old_manifest_loads_but_no_new_build_omits_the_edges(
    w: World,
) -> None:
    """RT-5's boundary: only the materialized selection is judged at its own build."""
    w.listed()
    arrivals = Arrivals(w)
    for name in ("IR", "NR"):
        arrivals.step(name)
    rt.report(w, at=utc(2023, 12, 15))
    rest_only = rt.build(w)
    stale = rest_only.manifest.point_in_time
    for name in ("IA", "NA", "RC"):
        arrivals.step(name)
    store = ManifestStore(w.h.adapter, w.builder())
    assert store.load(rest_only.manifest.content_hash()) == rest_only.manifest
    assert store.persist(rest_only.manifest).replayed
    before = rt.outputs(w)
    # The same stale spec over another window is a new build: it runs now, when the edges exist.
    with pytest.raises(DatasetSpecError, match="does not bind it"):
        rt.build(w, stale, window=(START, START + timedelta(hours=1)))
    # So is a new spec that simply leaves the evidence table out.
    with pytest.raises(DatasetSpecError, match="ADR-0027"):
        rt.build(w, skip=(*rt.OWN, c.EVIDENCE.table))
    assert rt.outputs(w) == before


def test_archive_first_then_rest_conflicts_until_reconciled_then_selects_the_same_rows(
    w: World,
) -> None:
    w.listed()
    arrivals = Arrivals(w)
    for name in ("IA", "NA"):
        arrivals.step(name)
    rt.report(w, at=utc(2023, 12, 15))
    archive_only = rt.build(w)
    for name in ("IR", "NR"):
        arrivals.step(name)
    rt.report(w, at=utc(2023, 12, 16))
    with pytest.raises(PitConflictError):
        rt.build(w)
    arrivals.step("RC")
    rt.report(w, at=utc(2023, 12, 17))
    final = rt.build(w)
    # Where they should match they do: the same archive revisions, the same rows.
    assert rt.dataset_rows(final) == rt.dataset_rows(archive_only)
    assert final.manifest.members == archive_only.manifest.members
    assert final.manifest.content_hash() != archive_only.manifest.content_hash()  # new bindings


def test_a_selection_batch_without_its_manifest_is_no_replay(w: World) -> None:
    """G2-R2 (cursor review): only a completed build is replayed past the binding check. A batch
    whose manifest never persisted (planted, or a build that died in between) is a new build."""
    w.listed()
    arrivals = Arrivals(w)
    for name in ("IR", "NR"):
        arrivals.step(name)
    rt.report(w, at=utc(2023, 12, 15))
    rest_only = rt.build(w)
    stale = rest_only.manifest.point_in_time
    w.h.delete_rows(  # the manifest row is gone; the selection batch stays
        DATASET_MANIFESTS,
        EqualTo("manifest_content_hash", rest_only.manifest.content_hash()),  # type: ignore[call-arg, arg-type]
    )
    for name in ("IA", "NA", "RC"):
        arrivals.step(name)
    before = rt.outputs(w)
    with pytest.raises(DatasetSpecError, match="does not bind it"):
        rt.build(w, stale)
    assert rt.outputs(w) == before

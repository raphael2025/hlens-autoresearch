"""G2 / replaced archives: same official path, new checksum, after (or before) a dataset exists.

A replacement is a second archive revision of one observation key (D2 appends, never overwrites).
An old manifest must keep reproducing from its bound snapshots; any build that can see both
archive revisions must fail closed on competing heads.
"""

from __future__ import annotations

import pytest

from infrastructure.dataset.manifests import ManifestStore
from infrastructure.pit.assumption import ASSUMPTION_BINDING
from infrastructure.pit.selector import PitConflictError
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.redteam import redteam_support as rt
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import utc

K_REPLACED, N_REPLACED, K_EDGE2 = utc(2023, 12, 11), utc(2023, 12, 12), utc(2023, 12, 13)


def _replace(w: World, *, normalize: bool = True, reconcile: bool = True) -> str:
    """The replacement archive of DAY (same trades, one price differs), through the chain."""
    replacement = rt.archived(
        rt.archive_trades(
            w,
            rt.replacement_lines(ss.agg_items(3)),
            knowledge=K_REPLACED,
            retrieved_at=utc(2023, 11, 30),
            request_id="archive-2",
        )
    )
    if normalize:
        rt.normalize(w, c.ARCHIVE_AGGS.table, replacement, at=N_REPLACED)
    if reconcile:
        rt.reconcile(w, at=K_EDGE2)
    return replacement


def test_after_a_replacement_the_old_manifest_reproduces_and_a_new_build_conflicts(
    w: World,
) -> None:
    w.listed()
    w.trades()
    w.report()
    old_spec = w.spec()
    first = rt.build(w, old_spec)

    _replace(w)
    archives = w.h.rows(c.ARCHIVES)
    assert len({row["source_uri"] for row in archives}) == 1  # same official path
    assert len({row["source_sha256"] for row in archives}) == 2  # new checksum
    w.report()

    # The old manifest is untouched by later knowledge: bit-identical replay, still loadable.
    replay = rt.build(w, old_spec)
    assert replay.replayed and replay.manifest == first.manifest
    assert (
        ManifestStore(w.h.adapter, w.builder()).load(first.manifest.content_hash())
        == first.manifest
    )

    # Any spec that sees both archive revisions fails closed; nothing is written.
    before = rt.outputs(w)
    with pytest.raises(PitConflictError, match="3 observation key"):
        rt.build(w)
    assert rt.outputs(w) == before and len(rt.manifests(w)) == 1


@pytest.mark.parametrize("order", ["original_first", "replacement_first"])
def test_competing_archives_conflict_whatever_order_they_arrive_in(w: World, order: str) -> None:
    w.listed()
    items = ss.agg_items(3)
    pairs = [
        (ss.archive_agg_lines(items), "archive-1", utc(2023, 11, 16)),
        (rt.replacement_lines(items), "archive-2", utc(2023, 11, 30)),
    ]
    if order == "replacement_first":
        pairs.reverse()
    for index, (lines, request_id, retrieved) in enumerate(pairs):
        unit = rt.archived(
            rt.archive_trades(
                w,
                lines,
                knowledge=utc(2023, 12, 1 + index),
                retrieved_at=retrieved,
                request_id=request_id,
            )
        )
        rt.normalize(w, c.ARCHIVE_AGGS.table, unit, at=utc(2023, 12, 5 + index))
    w.report()
    before = rt.outputs(w)
    with pytest.raises(PitConflictError, match="3 observation key"):
        rt.build(w)
    assert rt.outputs(w) == before


def test_the_bound_assumption_does_not_hide_a_replacement(w: World) -> None:
    """ADR-0032 moves both archive revisions to event time + 5 s: both are candidates."""
    w.listed()
    w.trades()
    _replace(w)
    w.report()
    spec = w.spec(skip=rt.OWN)
    assumed = spec.model_copy(
        update={"availability_bindings": (*spec.availability_bindings, ASSUMPTION_BINDING)}
    )
    before = rt.outputs(w)
    with pytest.raises(PitConflictError):
        rt.build(w, assumed)
    # Even a simulation time before the replacement's own ingest sees it under the assumption
    # (knowledge cutoff after it): the assumption never selects one archive over the other.
    early = assumed.model_copy(update={"simulation_time": utc(2023, 11, 20)})
    with pytest.raises(PitConflictError):
        rt.build(w, early)
    assert rt.outputs(w) == before


def test_a_replacement_known_to_raw_but_not_normalized_fails_closed(w: World) -> None:
    w.listed()
    w.trades()
    _replace(w, normalize=False, reconcile=False)
    assert ds.K_A < K_REPLACED <= ds.SIM  # the replacement is known to the spec below
    before = rt.outputs(w)
    with pytest.raises(rt.REFUSALS):
        w.report()
        rt.build(w)
    assert rt.outputs(w) == before

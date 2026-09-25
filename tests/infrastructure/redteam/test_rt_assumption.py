"""G2 / the ADR-0032 archive event-time assumption, bound where it must not act.

Bound in a spec, the assumption moves only archive revisions carrying the archive publication
gap (to event time + 5 s, never later). REST pages must not move, whatever else is bound; the
evidence gaps stay listed; and a replacement archive is still a conflict (``test_rt_replacement``).
"""

from __future__ import annotations

from datetime import datetime

from core.contracts.revision import PointInTimeSpec
from infrastructure.pit.assumption import ASSUMPTION_BINDING, ASSUMPTION_LATENCY
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import START, World
from tests.infrastructure.redteam import redteam_support as rt
from tests.infrastructure.revision import rest_store_support as ss

INTERVAL = (START, ds.SIM)


def _assumed(spec: PointInTimeSpec) -> PointInTimeSpec:
    return spec.model_copy(
        update={"availability_bindings": (*spec.availability_bindings, ASSUMPTION_BINDING)}
    )


def test_rest_only_data_does_not_move_under_the_assumption(w: World) -> None:
    w.listed()
    [response] = rt.rest_trades(w, ss.agg_items(3), knowledge=ds.K_R)
    rt.normalize(w, c.REST_AGGS.table, response, at=ds.N_R)
    w.report()
    plain_spec = w.spec(interval=INTERVAL)
    plain = rt.build(w, plain_spec)
    assumed = rt.build(w, _assumed(plain_spec))
    stored = {row["revision_id"]: row["available_time"] for row in w.h.rows(c.TRADES)}
    # Identical rows: every REST trade enters at its stored availability, not event + 5 s.
    assert rt.dataset_rows(assumed) == rt.dataset_rows(plain)
    for row in assumed.selection.rows:
        assert row["effective_from"] == max(INTERVAL[0], stored[row["revision_id"]])
        assert row["effective_from"] > row["event_time"] + ASSUMPTION_LATENCY
    assert assumed.manifest.evidence_gaps == plain.manifest.evidence_gaps
    assert assumed.manifest.lineage == plain.manifest.lineage
    assert assumed.manifest.content_hash() != plain.manifest.content_hash()  # the spec differs


def test_with_both_channels_only_the_archive_revisions_move(w: World) -> None:
    """Positive control: the same spec shape does move the archive copy once it is known."""
    w.listed()
    w.trades()
    w.report()
    assumed = rt.build(w, _assumed(w.spec(interval=INTERVAL)))
    lineage = {item.canonical_revision_id: item.raw_table for item in assumed.manifest.lineage}
    stored = {row["revision_id"]: row["available_time"] for row in w.h.rows(c.TRADES)}
    for row in assumed.selection.rows:
        assert lineage[row["revision_id"]] == c.ARCHIVE_AGGS.table
        effective: datetime = row["effective_from"]
        assert effective == max(INTERVAL[0], row["event_time"] + ASSUMPTION_LATENCY)
        assert effective < stored[row["revision_id"]]  # moved earlier, never later
    # The stored revisions and their evidence gaps are untouched and still bound.
    gaps = {gap.revision_id for gap in assumed.manifest.evidence_gaps}
    assert {row["revision_id"] for row in assumed.selection.rows} <= gaps

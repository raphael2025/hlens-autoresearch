"""v3 sorted-run PIT generator (ADR-0077 §6.1.2 / §6.1.3; ``PitSelector.iter_bounded``).

Reuses the real Raw / Canonical harness and helpers from ``test_selector.py`` (``_spec``,
``_chain``, the four-cutoff fixture data) so every case is checked against the *same* real
normalizer / reconciler data v2's ``select()`` uses — the point is that ``iter_bounded`` answers
identically to ``select()``, just through the sorted-run pipeline (``infrastructure/pit/runs.py``)
instead of ``select()``'s whole-window dicts. Deliberately tiny
``PitRunParams`` (batches / merge fanout / key-history buffer of 1-2) are used throughout so every
test exercises spilling, multi-run merging and (where applicable) ``KeyHistoryBuffer`` overflow,
not just the trivial single-run path.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest

from core.contracts.revision import PointInTimeSpec, PointInTimeStatus
from infrastructure.pit.runs import RunLimits
from infrastructure.pit.selector import (
    PitBoundedRecord,
    PitRunParams,
    PitSelector,
    PitSpecError,
)
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.pit.test_selector import (
    END,
    K_A,
    K_E,
    K_R,
    N_A,
    N_R,
    START,
    _chain,
    _spec,
    _WRONG,
)
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import RestHarness, SYMBOL, StepClock, utc

FAR = utc(2030, 1, 1)

#: Deliberately tiny: every test below spills into several runs and does a multi-pass merge.
#: leaf_max_records=1 alone already forces one leaf per row; leaf_max_bytes is left generous
#: (real Canonical rows have ~30 columns) so it never interacts with that fragmentation.
TINY_PARAMS = PitRunParams(
    row_batch_rows=1,
    edge_batch_rows=1,
    merge_fanout=2,
    key_history_buffer=1,
    limits=RunLimits(leaf_max_records=1, leaf_max_bytes=1 << 16, fanout=2),
)


def _bounded(h: RestHarness, spec: PointInTimeSpec, **kwargs: Any) -> list[PitBoundedRecord]:
    selector = PitSelector(h.adapter, h.storage)
    with selector.iter_bounded(
        spec, "agg_trades", SYMBOL, START, END, params=TINY_PARAMS, **kwargs
    ) as records:
        return list(records)


def _selections(records: list[PitBoundedRecord]) -> list[Any]:
    return [r.selection for r in records]


def _lineage_by_revision(records: list[PitBoundedRecord]) -> dict[str, Any]:
    return {r.lineage.canonical_revision_id: r.lineage for r in records if r.lineage is not None}


def _gaps_by_revision(records: list[PitBoundedRecord]) -> dict[str, Any]:
    return {
        r.evidence_gap.revision_id: r.evidence_gap for r in records if r.evidence_gap is not None
    }


# =========================================================================================
# equivalence with v2 select()
# =========================================================================================


def test_iter_bounded_matches_select_selections_lineage_and_gaps(h: RestHarness) -> None:
    _chain(h)
    spec = _spec(h, cutoff=K_E)
    legacy = PitSelector(h.adapter, h.storage).select(spec, "agg_trades", SYMBOL, START, END)
    records = _bounded(h, spec)

    got = sorted(_selections(records), key=lambda s: (s.observation_key, s.simulation_time))
    expected = sorted(legacy.selections, key=lambda s: (s.observation_key, s.simulation_time))
    assert got == expected
    assert _lineage_by_revision(records) == {
        item.canonical_revision_id: item for item in legacy.lineage
    }
    assert _gaps_by_revision(records) == {gap.revision_id: gap for gap in legacy.evidence_gaps}


def test_iter_bounded_matches_select_across_the_four_cutoffs(h: RestHarness) -> None:
    _chain(h)
    for cutoff in (N_A, N_R, K_E, K_A):
        spec = _spec(h, cutoff=cutoff)
        legacy = PitSelector(h.adapter, h.storage).select(spec, "agg_trades", SYMBOL, START, END)
        records = _bounded(h, spec)
        got = sorted(_selections(records), key=lambda s: (s.observation_key, s.simulation_time))
        expected = sorted(
            legacy.selections, key=lambda s: (s.observation_key, s.simulation_time)
        )
        assert got == expected, cutoff


def test_iter_bounded_reports_a_conflict_inline_like_select_reports_it_out_of_band(
    h: RestHarness,
) -> None:
    _chain(h)  # reconciled: N_R is a genuine competing-heads instant (ADR-0028 §4)
    spec = _spec(h, cutoff=N_R)
    legacy = PitSelector(h.adapter, h.storage).select(spec, "agg_trades", SYMBOL, START, END)
    assert legacy.conflicts  # sanity: v2 does see a conflict at this cutoff
    records = _bounded(h, spec)
    conflicted = [r for r in records if r.selection.status is PointInTimeStatus.CONFLICT]
    assert conflicted and all(r.lineage is None and r.evidence_gap is None for r in conflicted)
    assert {r.observation_key for r in conflicted} == set(legacy.conflicts)


def test_iter_bounded_deduplicates_lineage_across_repeated_selections_of_one_revision(
    h: RestHarness,
) -> None:
    """An interval re-selecting the same revision at several instants still yields its lineage
    exactly once (mirrors select()'s dict-keyed-by-revision dedup, ADR-0077 §6.1.4)."""
    _chain(h)
    spec = _spec(h, cutoff=FAR, interval=(utc(2023, 11, 15), utc(2023, 12, 31)))
    records = _bounded(h, spec)
    selected_revisions = [
        r.selection.selected_revision_id
        for r in records
        if r.selection.status is PointInTimeStatus.SELECTED
    ]
    assert len(selected_revisions) >= 1
    lineage_hits = [r for r in records if r.lineage is not None]
    assert len(lineage_hits) == len({item.lineage.canonical_revision_id for item in lineage_hits})


def test_iter_bounded_handles_several_keys_and_a_key_history_longer_than_the_buffer(
    h: RestHarness,
) -> None:
    """4 chained (archive + REST, 2 revisions each) keys under ``key_history_buffer=1`` force
    :class:`KeyHistoryBuffer` to spill for every one of them (ADR-0077 §6.1.2 item 2), plus a
    fifth, single-revision REST-only key exercising ordinary (non-spilling) grouping."""
    _chain(h, count=4)
    [other_item] = ss.agg_items(1, first_id=900)
    [other_response] = c.ingest_rest(
        h, "agg_trades", [other_item], knowledge=K_R, request_id="req-rest-lone-key"
    )
    c.normalizer(h, clock=StepClock(start=N_R)).normalize_unit(c.REST_AGGS.table, other_response)

    spec = _spec(h, cutoff=FAR)
    legacy = PitSelector(h.adapter, h.storage).select(spec, "agg_trades", SYMBOL, START, END)
    records = _bounded(h, spec)
    assert _lineage_by_revision(records) == {
        item.canonical_revision_id: item for item in legacy.lineage
    }
    assert len(legacy.lineage) == 5  # 4 from the chained key + 1 from the lone REST key


# =========================================================================================
# owner_event_time / event_time (B-FIX: PitBoundedRecord carries them directly)
# =========================================================================================


def test_iter_bounded_records_carry_owner_and_selected_event_times(h: RestHarness) -> None:
    """Every record's ``owner_event_time`` is its key's chain-earliest proven row time (the same
    ``earliest`` value ``_key_closure`` filters ownership on); a ``SELECTED`` record's
    ``event_time`` is exactly its selected revision's own proven row time (v2's row time column);
    every other record carries no ``event_time``."""
    _chain(h, count=4)
    spec = _spec(h, cutoff=FAR)
    rows = {row["revision_id"]: row for row in h.rows(c.TRADES)}
    owners: dict[str, datetime] = {}
    for row in rows.values():
        key = row["observation_key"]
        owners[key] = min(owners.get(key, row["event_time"]), row["event_time"])

    records = _bounded(h, spec)
    assert records  # sanity: the chained keys did produce records
    for record in records:
        assert record.owner_event_time == owners[record.observation_key]
        if record.selection.status is PointInTimeStatus.SELECTED:
            revision = record.selection.selected_revision_id
            assert revision is not None
            assert record.event_time == rows[revision]["event_time"]
        else:
            assert record.event_time is None
    # Every record of one key agrees with the others on owner_event_time (asserted per-record
    # above against the same precomputed value; restated here key by key for clarity).
    by_key: dict[str, set[datetime]] = {}
    for record in records:
        by_key.setdefault(record.observation_key, set()).add(record.owner_event_time)
    assert all(len(values) == 1 for values in by_key.values())


# =========================================================================================
# fail closed (shares select()'s validation, called unchanged)
# =========================================================================================


def test_iter_bounded_wrong_bindings_are_refused(h: RestHarness) -> None:
    _chain(h)
    spec = _spec(h, cutoff=FAR, parser_bindings=(_WRONG,))
    selector = PitSelector(h.adapter, h.storage)
    with pytest.raises(PitSpecError, match="parser_bindings"):
        with selector.iter_bounded(spec, "agg_trades", SYMBOL, START, END, params=TINY_PARAMS):
            pass


def test_iter_bounded_an_unbound_canonical_table_is_refused(h: RestHarness) -> None:
    _chain(h)
    spec = _spec(h, cutoff=FAR, skip=(c.TRADES.table,))
    selector = PitSelector(h.adapter, h.storage)
    with pytest.raises(PitSpecError, match="does not bind canonical.trades"):
        with selector.iter_bounded(spec, "agg_trades", SYMBOL, START, END, params=TINY_PARAMS):
            pass


def test_iter_bounded_an_unbound_evidence_table_still_yields_only_conflicts(
    h: RestHarness,
) -> None:
    _chain(h)
    spec = _spec(h, cutoff=FAR, skip=(c.EVIDENCE.table,))
    records = _bounded(h, spec)
    assert all(r.selection.status is PointInTimeStatus.CONFLICT for r in records)
    assert all(r.lineage is None for r in records)


# =========================================================================================
# explicit close of the internal generator and its merge readers (B-FIX)
# =========================================================================================


def test_iter_bounded_closes_its_generator_on_an_early_context_exit(h: RestHarness) -> None:
    """The context manager used to leave the inner generator (and the merge readers it holds
    open, up to ``merge_fanout`` per stream) to whenever it happened to be garbage collected: the
    ``with`` block's own exit never explicitly closed it. Consuming only part of the stream and
    then leaving the ``with`` block must now close it immediately -- proven here by the fact that
    the still-referenced generator object raises ``StopIteration`` on the next ``next()``, rather
    than producing another record."""
    _chain(h, count=4)  # several keys: more than one record is available past the first
    spec = _spec(h, cutoff=FAR)
    selector = PitSelector(h.adapter, h.storage)
    with selector.iter_bounded(
        spec, "agg_trades", SYMBOL, START, END, params=TINY_PARAMS
    ) as records:
        first = next(records)
        assert isinstance(first, PitBoundedRecord)
        # Stop here, well short of exhaustion: __exit__ must still close it.
    with pytest.raises(StopIteration):
        next(records)


def test_iter_bounded_closes_cleanly_after_full_iteration(h: RestHarness) -> None:
    """The normal (fully-exhausted) path must still close without error: closing an already-
    exhausted generator is a documented no-op, exercised here so a regression that makes the
    explicit close raise on the common path is caught."""
    _chain(h, count=2)
    spec = _spec(h, cutoff=FAR)
    selector = PitSelector(h.adapter, h.storage)
    with selector.iter_bounded(
        spec, "agg_trades", SYMBOL, START, END, params=TINY_PARAMS
    ) as records:
        drained = list(records)
    assert drained  # sanity: something was actually produced and consumed


def test_iter_bounded_closes_its_generator_when_the_context_body_raises(h: RestHarness) -> None:
    """An exception inside the ``with`` block must close the generator too, not just a clean
    early exit (``@contextmanager``'s ``finally`` covers both)."""
    _chain(h, count=4)
    spec = _spec(h, cutoff=FAR)
    selector = PitSelector(h.adapter, h.storage)
    captured: Iterator[PitBoundedRecord] | None = None
    with pytest.raises(RuntimeError, match="boom"):
        with selector.iter_bounded(
            spec, "agg_trades", SYMBOL, START, END, params=TINY_PARAMS
        ) as records:
            captured = records
            next(records)
            raise RuntimeError("boom")
    assert captured is not None
    with pytest.raises(StopIteration):
        next(captured)


# =========================================================================================
# PitRunParams parameter validation (ADR-0077 DQ-9: no defaults, all explicit)
# =========================================================================================


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("row_batch_rows", 0, "row_batch_rows"),
        ("edge_batch_rows", 0, "edge_batch_rows"),
        ("merge_fanout", 1, "merge_fanout"),
        ("key_history_buffer", 0, "key_history_buffer"),
    ],
)
def test_pit_run_params_rejects_non_positive_values(field: str, value: int, match: str) -> None:
    kwargs: dict[str, Any] = {
        "row_batch_rows": 1,
        "edge_batch_rows": 1,
        "merge_fanout": 2,
        "key_history_buffer": 1,
        "limits": RunLimits(leaf_max_records=1, leaf_max_bytes=4096, fanout=2),
    }
    kwargs[field] = value
    with pytest.raises(ValueError, match=match):
        PitRunParams(**kwargs)

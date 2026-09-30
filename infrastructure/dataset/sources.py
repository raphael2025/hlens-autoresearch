"""Real upstream sources for the v3 ``DatasetEvidenceBuilder`` (ADR-0077 §2 / §6.1; B-ADAPT).

``DatasetEvidenceBuilder`` reads its inputs through three read-only, ordered Protocols
(``UniverseEvidenceSource``, ``PitKeySource``, ``QualityEvidenceSource``). This module adapts the
real bounded generators to the first two (the third is ``PinnedQualityEvidence``, in the builder):

- ``OrderedUniverseSource`` wraps B-UNIV's ``UniverseSpanCursor`` (``UniverseBuilder.cursor``).
  The cursor yields in *generation* order (spec symbol, then time). ADR-0077 §2 requires
  ``listing_lineage`` by ``canonical_revision_id`` (PM decision on B-BUILD item 1) and the
  builder requires the listing ``evidence_gaps`` in that same order; ``members`` / ``exclusions``
  must be by ``(episode.observation_key(), effective_from)``, which coincides with generation order
  only while every symbol keeps one episode and every episode key sorts like its symbol (not
  provable for every registered spec: a stable-id key sorts before any degraded one, a rename
  opens a new episode). All four are therefore re-sorted through content-addressed sorted runs
  using an online hierarchical run set (every size explicit, with no all-runs ref list).
  ``member_spans`` is passed through: its generation order (symbol, then start) *is*
  the builder's required order, and the builder proves it.
- ``PitSelectorKeySource`` wraps B-PIT's ``PitSelector.iter_bounded`` and folds its per-instant
  ``PitBoundedRecord`` stream into one ``PitKeyGroup`` per observation key (``pit_key_groups``).
  Observation keys must be strictly increasing (a duplicate or reordered key fails closed); the
  lineage / gap ``iter_bounded`` attaches only to a revision's first selection inside its key is
  carried to every later selection of that revision in the key. Each record already carries its
  key's ``owner_event_time`` (the chain-earliest event, the slice-ownership witness
  ``PitKeyGroup.owner_event_time`` needs) and, when selected, the revision's own proven row time
  column (``event_time`` / ``interval_start``): ``pit_key_groups`` reads both straight off the
  stream, with no second read of the Canonical rows.

Neither adapter decides anything the builder re-proves: order, uniqueness, ownership, lineage and
report bindings are all checked again, fail closed, by ``DatasetEvidenceBuilder``.

**Held state.** One sorted-run batch (``capacity`` / ``row_batch_rows`` items) while spilling, the
run references of one stream (one per batch, as B-PIT's own spill), at most ``merge_fanout`` open
run readers, and one observation key's evaluations and carried lineage (the per-key bound the task
and ADR-0077 §2 "dedupe inside the key group" allow). Nothing spans keys, symbols or the window.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from core.contracts.revision import PointInTimeSpec, PointInTimeStatus
from core.contracts.storage import StorageAdapter
from core.contracts.universe import (
    PitConflictHeadEvidence,
    SelectedRevisionLineage,
    UniverseExclusion,
    UniverseMember,
)
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.dataset.builder import (
    DatasetEvidenceRequest,
    DatasetEvidenceSources,
    DatasetSpecError,
    PinnedQualityEvidence,
    PitKeyEvaluation,
    PitKeyGroup,
    PitSelectedRevision,
    UniverseEvidenceSource,
)
from infrastructure.pit.runs import RunSetBuilder, iter_run
from infrastructure.pit.selector import EvidenceGap, PitBoundedRecord, PitRunParams, PitSelector
from infrastructure.revision.store import RevisionCatalog
from infrastructure.universe.builder import UniverseBuilder
from infrastructure.universe.run_params import UniverseRunParams

__all__ = [
    "OrderedUniverseSource",
    "PitSelectorKeySource",
    "UniverseRunParams",
    "dataset_evidence_sources",
    "pit_key_groups",
]

_ZERO: Final = timedelta(0)
#: The sort value of a point entry's absent span start (as the builder's ``_NO_SPAN``).
_NO_SPAN: Final = datetime.min.replace(tzinfo=UTC)


# =========================================================================================
# universe: B-UNIV's cursor in the ADR-0077 §2 orders
# =========================================================================================


#: A run row: ``{"order": [...sort values], "record": <JSON-safe record>}``.
_RunRow = dict[str, Any]


def _run_order(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(row["order"])


def _entry_row(entry: UniverseMember | UniverseExclusion) -> _RunRow:
    start = _NO_SPAN if entry.effective_from is None else entry.effective_from
    return {
        "order": [entry.episode.observation_key(), start],
        "record": entry.model_dump(mode="json"),
    }


def _member_row(item: object) -> _RunRow:
    if not isinstance(item, UniverseMember) or type(item) is not UniverseMember:
        raise CatalogIntegrityError("a members item is not a UniverseMember")
    return _entry_row(item)


def _exclusion_row(item: object) -> _RunRow:
    if not isinstance(item, UniverseExclusion) or type(item) is not UniverseExclusion:
        raise CatalogIntegrityError("an exclusions item is not a UniverseExclusion")
    return _entry_row(item)


def _lineage_row(item: object) -> _RunRow:
    if not isinstance(item, SelectedRevisionLineage):
        raise CatalogIntegrityError("a listing lineage item is not a SelectedRevisionLineage")
    return {"order": [item.canonical_revision_id], "record": item.model_dump(mode="json")}


def _gap_row(item: object) -> _RunRow:
    if (
        not isinstance(item, tuple)
        or len(item) != 2
        or not all(isinstance(part, str) and part for part in item)
    ):
        raise CatalogIntegrityError(f"a listing evidence gap is malformed: {item!r}")
    revision, gap = item
    return {"order": [revision, gap], "record": {"revision_id": revision, "gap": gap}}


def _gap_of(record: Mapping[str, Any]) -> tuple[str, str]:
    return record["revision_id"], record["gap"]


@contextmanager
def _reordered[T](
    source: Callable[[], AbstractContextManager[Iterator[Any]]],
    encode: Callable[[object], _RunRow],
    decode: Callable[[Any], T],
    storage: StorageAdapter,
    params: UniverseRunParams,
) -> Iterator[Iterator[T]]:
    """One upstream view, drained into a hierarchical sorted run set, then streamed in order.

    Run references are compacted online with finite merge fanout. Sorting is by the encoded
    ``order`` only; ties keep an arbitrary order, and duplicates reach the builder, which rejects
    them (adjacent comparison), exactly as it would have unsorted.
    """
    run_set = RunSetBuilder(
        storage,
        key=_run_order,
        capacity=params.capacity,
        merge_fanout=params.merge_fanout,
        limits=params.limits,
    )
    with source() as items:
        with run_set:
            for item in items:
                run_set.add(encode(item))
            root = run_set.finish()
    if root is None:
        yield iter(())
        return
    with iter_run(storage, root) as merged:
        decoded = (decode(row["record"]) for row in merged)
        try:
            yield decoded
        finally:
            decoded.close()


class OrderedUniverseSource:
    """``UniverseEvidenceSource`` over a generation-ordered universe cursor, in ADR-0077 §2 order.

    ``cursor`` is B-UNIV's ``UniverseSpanCursor`` (``UniverseBuilder.cursor(spec, pit)`` for the
    request's own spec and PIT spec) or anything shaped like it. Every view is an independent,
    explicitly closed pass: re-sorted views drain their upstream pass into runs, close it, then
    stream the merge; ``member_spans`` is the cursor's own view.
    """

    def __init__(
        self,
        cursor: UniverseEvidenceSource,
        *,
        storage: StorageAdapter,
        params: UniverseRunParams,
    ) -> None:
        if not isinstance(params, UniverseRunParams):
            raise DatasetSpecError("params must be UniverseRunParams")
        self._cursor = cursor
        self._storage = storage
        self._params = params

    def members(self) -> AbstractContextManager[Iterator[UniverseMember]]:
        """By ``(episode.observation_key(), effective_from)``."""
        return _reordered(
            self._cursor.members,
            _member_row,
            UniverseMember.model_validate,
            self._storage,
            self._params,
        )

    def exclusions(self) -> AbstractContextManager[Iterator[UniverseExclusion]]:
        """By ``(episode.observation_key(), effective_from)``."""
        return _reordered(
            self._cursor.exclusions,
            _exclusion_row,
            UniverseExclusion.model_validate,
            self._storage,
            self._params,
        )

    def listing_lineage(self) -> AbstractContextManager[Iterator[SelectedRevisionLineage]]:
        """By ``canonical_revision_id`` (ADR-0077 §2; PM decision on B-BUILD item 1)."""
        return _reordered(
            self._cursor.listing_lineage,
            _lineage_row,
            SelectedRevisionLineage.model_validate,
            self._storage,
            self._params,
        )

    def evidence_gaps(self) -> AbstractContextManager[Iterator[tuple[str, str]]]:
        """``(listing revision id, gap)`` by revision id: the order of ``listing_lineage``."""
        return _reordered(
            self._cursor.evidence_gaps, _gap_row, _gap_of, self._storage, self._params
        )

    def member_spans(
        self,
    ) -> AbstractContextManager[Iterator[tuple[str, datetime | None, datetime | None]]]:
        """The cursor's own order (venue symbol, then start), proven by the builder."""
        return self._cursor.member_spans()


# =========================================================================================
# PIT: B-PIT's per-instant stream folded into per-key groups
# =========================================================================================


def _check_utc(value: object, what: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != _ZERO:
        raise CatalogIntegrityError(f"{what} is not a timezone-aware UTC datetime")
    return value


def _next_record(records: Iterator[object]) -> PitBoundedRecord | None:
    record = next(records, None)
    if record is None:
        return None
    if not isinstance(record, PitBoundedRecord):
        raise CatalogIntegrityError("a PIT stream item is not a PitBoundedRecord")
    if not isinstance(record.observation_key, str) or not record.observation_key:
        raise CatalogIntegrityError("a PIT stream record has no observation key")
    return record


def _next_run_row(rows: Iterator[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    try:
        return next(rows)
    except StopIteration:
        return None


def _evaluations(
    key: str,
    held: Iterable[PitBoundedRecord],
    knowledge_cutoff: datetime,
    *,
    storage: StorageAdapter,
    params: PitRunParams,
) -> Iterator[PitKeyEvaluation]:
    """Stream no-lineage prefixes, then spool selected history for a bounded revision join."""
    records = iter(held)
    try:
        for record in records:
            selection = record.selection
            if selection.observation_key != key:
                raise CatalogIntegrityError(
                    f"a selection of {selection.observation_key} is in {key}"
                )
            if selection.knowledge_cutoff != knowledge_cutoff:
                raise CatalogIntegrityError(f"key {key} is evaluated at another knowledge cutoff")
            if selection.status is PointInTimeStatus.SELECTED:

                def selected_records(
                    first: PitBoundedRecord = record,
                    rest: Iterator[PitBoundedRecord] = records,
                ) -> Iterator[PitBoundedRecord]:
                    yield first
                    yield from rest

                yield from _selected_evaluations(
                    key,
                    selected_records(),
                    knowledge_cutoff,
                    storage=storage,
                    params=params,
                )
                return
            if (
                record.event_time is not None
                or record.lineage is not None
                or record.evidence_gap is not None
            ):
                raise CatalogIntegrityError(
                    f"key {key}: a {selection.status.value} evaluation carries lineage"
                )
            yield PitKeyEvaluation(
                simulation_time=selection.simulation_time,
                status=selection.status,
                selected=None,
                head_count=selection.head_count,
            )
            if selection.status is PointInTimeStatus.CONFLICT:
                return
    finally:
        _close(records)


def _selected_evaluations(
    key: str,
    held: Iterable[PitBoundedRecord],
    knowledge_cutoff: datetime,
    *,
    storage: StorageAdapter,
    params: PitRunParams,
) -> Iterator[PitKeyEvaluation]:
    """Stage one key into bounded runs, validate lineage by revision, then replay by ordinal.

    Revision IDs are not ordered by simulation time, so an online dictionary lookup would grow
    with the key history. The two external sorts make both lookup and evaluation-order replay
    bounded: occurrences are grouped by revision, joined to their unique lineage, and finally
    written back by input ordinal. No evaluation is exposed until the complete key is validated.
    """

    evaluation_root = None
    occurrence_root = None
    with RunSetBuilder(
        storage,
        key=lambda row: row["ordinal"],
        capacity=params.key_history_buffer,
        merge_fanout=params.merge_fanout,
        limits=params.limits,
    ) as evaluations:
        with RunSetBuilder(
            storage,
            key=lambda row: (row["revision_id"], row["ordinal"]),
            capacity=params.key_history_buffer,
            merge_fanout=params.merge_fanout,
            limits=params.limits,
        ) as occurrences:
            count = 0
            for record in held:
                selection = record.selection
                if selection.observation_key != key:
                    raise CatalogIntegrityError(
                        f"a selection of {selection.observation_key} is in {key}"
                    )
                if selection.knowledge_cutoff != knowledge_cutoff:
                    raise CatalogIntegrityError(
                        f"key {key} is evaluated at another knowledge cutoff"
                    )
                row: dict[str, Any] = {
                    "ordinal": count,
                    "simulation_time": selection.simulation_time,
                    "status": selection.status.value,
                    "revision_id": selection.selected_revision_id,
                    "event_time": record.event_time,
                    "head_count": selection.head_count,
                }
                if selection.status is PointInTimeStatus.SELECTED:
                    revision = selection.selected_revision_id
                    if not revision:
                        raise CatalogIntegrityError(f"key {key}: a selection names no revision")
                    if record.event_time is None:
                        raise CatalogIntegrityError(
                            f"selected revision {revision} of {key} has no event_time"
                        )
                    _check_utc(record.event_time, f"revision {revision} of {key} event_time")
                    lineage = record.lineage
                    gap = record.evidence_gap
                    if lineage is None:
                        if gap is not None:
                            raise CatalogIntegrityError(
                                f"key {key}: revision {revision} has a gap but no lineage"
                            )
                        lineage_value = None
                    else:
                        if (
                            not isinstance(lineage, SelectedRevisionLineage)
                            or lineage.canonical_revision_id != revision
                        ):
                            raise CatalogIntegrityError(
                                f"selected revision {revision} carries another's lineage"
                            )
                        if gap is not None and (
                            not isinstance(gap, EvidenceGap)
                            or (gap.table, gap.revision_id) != (lineage.canonical_table, revision)
                            or not gap.gap
                        ):
                            raise CatalogIntegrityError(
                                f"selected revision {revision} carries another's gap"
                            )
                        lineage_value = lineage.model_dump(mode="json")
                    occurrences.add(
                        {
                            "revision_id": revision,
                            "ordinal": count,
                            "lineage": lineage_value,
                            "gap": None if gap is None else gap.gap,
                            "event_time": record.event_time,
                        }
                    )
                elif (
                    record.event_time is not None
                    or record.lineage is not None
                    or record.evidence_gap is not None
                ):
                    raise CatalogIntegrityError(
                        f"key {key}: a {selection.status.value} evaluation carries lineage"
                    )
                evaluations.add(row)
                count += 1
            evaluation_root = evaluations.finish()
            occurrence_root = occurrences.finish()

    if evaluation_root is None:
        raise CatalogIntegrityError(f"key {key} has no evaluation")

    lineage_root = None
    if occurrence_root is not None:
        with iter_run(storage, occurrence_root) as rows:
            with RunSetBuilder(
                storage,
                key=lambda row: row["revision_id"],
                capacity=params.key_history_buffer,
                merge_fanout=params.merge_fanout,
                limits=params.limits,
            ) as lineages:
                current_revision: str | None = None
                current_lineage: Mapping[str, Any] | None = None
                current_gap: str | None = None
                for occurrence in rows:
                    revision = occurrence["revision_id"]
                    attached = occurrence["lineage"]
                    gap = occurrence["gap"]
                    if revision != current_revision:
                        if current_revision is not None:
                            if current_lineage is None:
                                raise CatalogIntegrityError(
                                    f"selected revision {current_revision} has no lineage"
                                )
                            lineages.add(
                                {
                                    "revision_id": current_revision,
                                    "lineage": dict(current_lineage),
                                    "gap": current_gap,
                                }
                            )
                        current_revision = revision
                        current_lineage = attached
                        current_gap = gap
                        if current_lineage is None:
                            raise CatalogIntegrityError(
                                f"selected revision {revision} has no lineage"
                            )
                    elif attached is not None and (
                        attached != current_lineage or gap != current_gap
                    ):
                        raise CatalogIntegrityError(
                            f"selected revision {revision} carries two lineages"
                        )
                if current_revision is not None:
                    if current_lineage is None:
                        raise CatalogIntegrityError(
                            f"selected revision {current_revision} has no lineage"
                        )
                    lineages.add(
                        {
                            "revision_id": current_revision,
                            "lineage": dict(current_lineage),
                            "gap": current_gap,
                        }
                    )
                lineage_root = lineages.finish()

    selected_root = None
    if occurrence_root is not None:
        if lineage_root is None:
            raise CatalogIntegrityError(f"key {key} has selected evaluations but no lineage")
        with iter_run(storage, occurrence_root) as occurrences:
            with iter_run(storage, lineage_root) as lineage_rows:
                with RunSetBuilder(
                    storage,
                    key=lambda row: row["ordinal"],
                    capacity=params.key_history_buffer,
                    merge_fanout=params.merge_fanout,
                    limits=params.limits,
                ) as selected_builder:
                    occurrence_row = _next_run_row(occurrences)
                    lineage_row = _next_run_row(lineage_rows)
                    while occurrence_row is not None:
                        revision = occurrence_row["revision_id"]
                        while lineage_row is not None and lineage_row["revision_id"] < revision:
                            lineage_row = _next_run_row(lineage_rows)
                        if lineage_row is None or lineage_row["revision_id"] != revision:
                            raise CatalogIntegrityError(
                                f"selected revision {revision} has no lineage"
                            )
                        selected_builder.add(
                            {
                                "ordinal": occurrence_row["ordinal"],
                                "revision_id": revision,
                                "event_time": occurrence_row["event_time"],
                                "lineage": lineage_row["lineage"],
                                "gap": lineage_row["gap"],
                            }
                        )
                        occurrence_row = _next_run_row(occurrences)
                    selected_root = selected_builder.finish()

    final_root = None
    with iter_run(storage, evaluation_root) as evaluations, ExitStack() as stack:
        selected_rows: Iterator[Mapping[str, Any]] = (
            iter(())
            if selected_root is None
            else stack.enter_context(iter_run(storage, selected_root))
        )
        with RunSetBuilder(
            storage,
            key=lambda row: row["ordinal"],
            capacity=params.key_history_buffer,
            merge_fanout=params.merge_fanout,
            limits=params.limits,
        ) as output:
            selected_entry = _next_run_row(selected_rows)
            for eval_row in evaluations:
                status = PointInTimeStatus(eval_row["status"])
                picked: Mapping[str, Any] | None = None
                if status is PointInTimeStatus.SELECTED:
                    if selected_entry is None or selected_entry["ordinal"] != eval_row["ordinal"]:
                        raise CatalogIntegrityError(
                            f"selected revision {eval_row['revision_id']} has no lineage"
                        )
                    picked = selected_entry
                    selected_entry = _next_run_row(selected_rows)
                if status is not PointInTimeStatus.SELECTED and (
                    selected_entry is not None and selected_entry["ordinal"] == eval_row["ordinal"]
                ):
                    raise CatalogIntegrityError(
                        f"key {key}: a {status.value} evaluation carries lineage"
                    )
                output.add(
                    {
                        "ordinal": eval_row["ordinal"],
                        "simulation_time": eval_row["simulation_time"],
                        "status": eval_row["status"],
                        "head_count": eval_row["head_count"],
                        "selected": None
                        if picked is None
                        else {
                            "revision_id": picked["revision_id"],
                            "event_time": picked["event_time"],
                            "lineage": picked["lineage"],
                            "gap": picked["gap"],
                        },
                    }
                )
            if selected_entry is not None:
                raise CatalogIntegrityError(
                    f"selected revision {selected_entry['revision_id']} has no matching evaluation"
                )
            final_root = output.finish()

    if final_root is None:
        raise CatalogIntegrityError(f"key {key} has no evaluation")

    with iter_run(storage, final_root) as rows:
        for final_row in rows:
            selected_payload = final_row["selected"]
            output_selected: PitSelectedRevision | None = None
            if selected_payload is not None:
                lineage = SelectedRevisionLineage.model_validate(selected_payload["lineage"])
                output_selected = PitSelectedRevision(
                    revision_id=selected_payload["revision_id"],
                    event_time=selected_payload["event_time"],
                    lineage=lineage,
                    evidence_gap=selected_payload["gap"],
                )
            yield PitKeyEvaluation(
                simulation_time=final_row["simulation_time"],
                status=PointInTimeStatus(final_row["status"]),
                selected=output_selected,
                head_count=final_row["head_count"],
            )


def pit_key_groups(
    records: Iterable[PitBoundedRecord],
    *,
    knowledge_cutoff: datetime,
    storage: StorageAdapter,
    params: PitRunParams,
) -> Iterator[PitKeyGroup]:
    """Fold PIT records into lazy per-key groups.

    The consumer must exhaust a group's evaluations before requesting the next key. This lets
    Dataset process a key at a time and, on a conflict, finish its complete evidence stream
    before the PIT cursor advances. Successful keys use at most one-record lookahead to detect
    the next key; a conflict is terminal and never pulls another PIT record.
    """
    source = iter(records)
    pending = _next_record(source)
    previous: str | None = None
    while pending is not None:
        key = pending.observation_key
        if previous is not None and key <= previous:
            raise CatalogIntegrityError(
                f"observation key {key} is duplicated or out of order in the PIT stream (after "
                f"{previous})"
            )
        owner = _check_utc(pending.owner_event_time, f"key {key} owner_event_time")
        evaluations_complete = False
        conflicted = False

        def key_records(key: str = key, owner: datetime = owner) -> Iterator[PitBoundedRecord]:
            nonlocal evaluations_complete, pending, conflicted
            while pending is not None and pending.observation_key == key:
                current = pending
                this_owner = _check_utc(current.owner_event_time, f"key {key} owner_event_time")
                if this_owner != owner:
                    raise CatalogIntegrityError(f"key {key} records disagree on owner_event_time")
                # Suspend before advancing the PIT source. The Dataset consumer sees the complete
                # conflict evaluation and seals its heads root before another next() can compute
                # or emit a later evaluation.
                yield current
                if current.selection.status is PointInTimeStatus.CONFLICT:
                    conflicted = True
                    evaluations_complete = True
                    return
                pending = _next_record(source)
            evaluations_complete = True

        yield PitKeyGroup(
            observation_key=key,
            owner_event_time=owner,
            evaluations=_evaluations(
                key,
                key_records(),
                knowledge_cutoff,
                storage=storage,
                params=params,
            ),
        )
        if not evaluations_complete:
            raise CatalogIntegrityError(
                "a PIT key's evaluations must be consumed before requesting the next key"
            )
        previous = key
        if conflicted:
            return


def _close(iterator: object) -> None:
    close = getattr(iterator, "close", None)
    if callable(close):
        close()


class PitSelectorKeySource:
    """``PitKeySource`` over ``PitSelector.iter_bounded`` (owned keys only, ``touching=False``).

    ``params`` are B-PIT's explicit run sizes (DQ-9 OPEN), passed straight through to
    ``iter_bounded``. ``storage`` is accepted for symmetry with ``OrderedUniverseSource`` (this
    adapter does no sorted-run spilling of its own: ``iter_bounded``'s own runs already carry
    everything a key group needs, ``owner_event_time`` and each selected revision's ``event_time``
    included).
    """

    def __init__(
        self, selector: PitSelector, *, storage: StorageAdapter, params: PitRunParams
    ) -> None:
        if not isinstance(selector, PitSelector):
            raise DatasetSpecError("selector must be a PitSelector")
        if not isinstance(params, PitRunParams):
            raise DatasetSpecError("params must be PitRunParams")
        self._selector = selector
        self._storage = storage
        self._params = params

    def keys(
        self,
        pit: PointInTimeSpec,
        data_type: str,
        venue_symbol: str,
        start: datetime,
        end: datetime,
        *,
        conflict_sink: Callable[[PitConflictHeadEvidence], None] | None = None,
    ) -> AbstractContextManager[Iterator[PitKeyGroup]]:
        return self._keys(pit, data_type, venue_symbol, start, end, conflict_sink=conflict_sink)

    @contextmanager
    def _keys(
        self,
        pit: PointInTimeSpec,
        data_type: str,
        venue_symbol: str,
        start: datetime,
        end: datetime,
        *,
        conflict_sink: Callable[[PitConflictHeadEvidence], None] | None,
    ) -> Iterator[Iterator[PitKeyGroup]]:
        with ExitStack() as stack:
            # iter_bounded first: it checks the spec, data type and symbol before anything reads.
            records = stack.enter_context(
                self._selector.iter_bounded(
                    pit,
                    data_type,
                    venue_symbol,
                    start,
                    end,
                    params=self._params,
                    touching=False,
                    conflict_sink=conflict_sink,
                )
            )
            # The inner generator holds the merge readers: close it on any exit, not at GC.
            stack.callback(_close, records)
            groups = pit_key_groups(
                records,
                knowledge_cutoff=pit.knowledge_cutoff,
                storage=self._storage,
                params=self._params,
            )
            stack.callback(_close, groups)
            yield groups


# =========================================================================================
# assembly
# =========================================================================================


def dataset_evidence_sources(
    adapter: RevisionCatalog,
    storage: StorageAdapter,
    request: DatasetEvidenceRequest,
    *,
    canonical_scratch_directory: Path,
    market_data_base_url: str,
    pit_params: PitRunParams,
    universe_params: UniverseRunParams,
) -> DatasetEvidenceSources:
    """The real upstreams of ``request`` for ``DatasetEvidenceBuilder.select`` / ``build``.

    Universe: ``UniverseBuilder.cursor(request.universe, request.pit,
    run_params=universe_params)`` in ADR-0077 §2 order;
    PIT: ``PitSelector.iter_bounded`` grouped by key; quality: ``PinnedQualityEvidence`` at the
    PIT spec's bound snapshots. Every run size is the caller's (no defaults).
    """
    if not isinstance(request, DatasetEvidenceRequest):
        raise DatasetSpecError("request must be a DatasetEvidenceRequest")
    cursor = UniverseBuilder(adapter, storage, market_data_base_url=market_data_base_url).cursor(
        request.universe, request.pit, run_params=universe_params
    )
    return DatasetEvidenceSources(
        universe=OrderedUniverseSource(cursor, storage=storage, params=universe_params),
        pit=PitSelectorKeySource(
            PitSelector(
                adapter,
                storage,
                canonical_scratch_directory=canonical_scratch_directory,
            ),
            storage=storage,
            params=pit_params,
        ),
        quality=PinnedQualityEvidence(
            adapter,
            storage,
            request.pit,
            request.data_type,
            canonical_scratch_directory=canonical_scratch_directory,
            market_data_base_url=market_data_base_url,
        ),
    )

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
  opens a new episode). All four are therefore re-sorted through the content-addressed sorted
  runs of ``infrastructure.pit.runs`` (``RunSetBuilder``, every size explicit); run refs are
  folded online into one root instead of retained in a list. ``member_spans`` is passed through:
  its generation order (symbol, then start) *is* the builder's required order, and the builder
  proves it.
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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from core.contracts.revision import PointInTimeSpec, PointInTimeStatus
from core.contracts.storage import StorageAdapter
from core.contracts.universe import SelectedRevisionLineage, UniverseExclusion, UniverseMember
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
from infrastructure.pit.runs import RunLimits, RunSetBuilder, iter_run
from infrastructure.pit.selector import PitBoundedRecord, PitRunParams, PitSelector
from infrastructure.revision.store import RevisionCatalog
from infrastructure.universe.builder import UniverseBuilder

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


@dataclass(frozen=True, slots=True)
class UniverseRunParams:
    """The sorted-run sizes of B-UNIV and ``OrderedUniverseSource`` (DQ-9 OPEN: no defaults).

    - ``capacity``: items of one stream held before they are sorted and spilled as one run;
    - ``merge_fanout``: run readers open at once while merging (more runs merge in passes);
    - ``limits``: the leaf / index shape of every run written.
    """

    capacity: int
    merge_fanout: int
    limits: RunLimits

    def __post_init__(self) -> None:
        for name, value, minimum in (
            ("capacity", self.capacity, 1),
            ("merge_fanout", self.merge_fanout, 2),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise DatasetSpecError(f"{name} must be an integer >= {minimum}, got {value!r}")
        if not isinstance(self.limits, RunLimits):
            raise DatasetSpecError("limits must be RunLimits")


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
    """Drain one upstream view into an online compacted root, then stream it in order.

    Sorting is by the encoded ``order`` only; ties keep an arbitrary order, and duplicates reach
    the builder, which rejects them (adjacent comparison), exactly as it would have unsorted. The
    source closes before the run is read, and the accumulator retains only its fanout-bounded
    levels rather than one reference per capacity batch.
    """
    with source() as items:
        with RunSetBuilder(
            storage,
            key=_run_order,
            capacity=params.capacity,
            merge_fanout=params.merge_fanout,
            limits=params.limits,
        ) as runs:
            runs.extend(encode(item) for item in items)
            root = runs.finish()
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


def _carry(
    record: PitBoundedRecord,
    key: str,
    revision: str,
    carried: dict[str, tuple[SelectedRevisionLineage, str | None]],
) -> tuple[SelectedRevisionLineage, str | None]:
    """The lineage / gap of ``revision``: attached here (its first selection) or carried."""
    lineage, gap = record.lineage, record.evidence_gap
    if lineage is None:
        if gap is not None:
            raise CatalogIntegrityError(f"key {key}: revision {revision} has a gap but no lineage")
        known = carried.get(revision)
        if known is None:
            raise CatalogIntegrityError(f"selected revision {revision} has no lineage")
        return known
    if (
        not isinstance(lineage, SelectedRevisionLineage)
        or lineage.canonical_revision_id != revision
    ):
        raise CatalogIntegrityError(f"selected revision {revision} carries another's lineage")
    text: str | None = None
    if gap is not None:
        if (gap.table, gap.revision_id) != (lineage.canonical_table, revision) or not gap.gap:
            raise CatalogIntegrityError(f"selected revision {revision} carries another's gap")
        text = gap.gap
    attached = (lineage, text)
    if carried.setdefault(revision, attached) != attached:
        raise CatalogIntegrityError(f"selected revision {revision} carries two lineages")
    return attached


def _evaluations(
    key: str, held: Iterable[PitBoundedRecord], knowledge_cutoff: datetime
) -> tuple[PitKeyEvaluation, ...]:
    carried: dict[str, tuple[SelectedRevisionLineage, str | None]] = {}
    evaluations: list[PitKeyEvaluation] = []
    for record in held:
        selection = record.selection
        if selection.observation_key != key:
            raise CatalogIntegrityError(f"a selection of {selection.observation_key} is in {key}")
        if selection.knowledge_cutoff != knowledge_cutoff:
            raise CatalogIntegrityError(f"key {key} is evaluated at another knowledge cutoff")
        selected: PitSelectedRevision | None = None
        if selection.status is PointInTimeStatus.SELECTED:
            revision = selection.selected_revision_id
            if not revision:
                raise CatalogIntegrityError(f"key {key}: a selection names no revision")
            lineage, gap = _carry(record, key, revision, carried)
            if record.event_time is None:
                raise CatalogIntegrityError(
                    f"selected revision {revision} of {key} has no event_time"
                )
            at = _check_utc(record.event_time, f"revision {revision} of {key} event_time")
            selected = PitSelectedRevision(
                revision_id=revision, event_time=at, lineage=lineage, evidence_gap=gap
            )
        elif (
            record.event_time is not None
            or record.lineage is not None
            or record.evidence_gap is not None
        ):
            raise CatalogIntegrityError(
                f"key {key}: a {selection.status.value} evaluation carries lineage"
            )
        evaluations.append(
            PitKeyEvaluation(
                simulation_time=selection.simulation_time,
                status=selection.status,
                selected=selected,
            )
        )
    return tuple(evaluations)


def pit_key_groups(
    records: Iterable[PitBoundedRecord], *, knowledge_cutoff: datetime
) -> Iterator[PitKeyGroup]:
    """Fold ``iter_bounded``'s records into one ``PitKeyGroup`` per observation key.

    ``records``: key then instant order; one key's records are adjacent and keys strictly
    increase (a duplicate, split or reordered key fails closed). Every record already carries its
    key's ``owner_event_time`` (the chain-earliest event) and, when selected, the revision's own
    proven row time (``PitBoundedRecord.event_time``) -- both attached by
    ``PitSelector.iter_bounded`` -- so no second read of the Canonical rows is needed here. Every
    record of one key must agree on ``owner_event_time``. Holds one key's records.
    """
    source: Iterator[object] = iter(records)
    pending = _next_record(source)
    previous: str | None = None
    while pending is not None:
        key = pending.observation_key
        if previous is not None and key <= previous:
            raise CatalogIntegrityError(
                f"observation key {key} is duplicated or out of order in the PIT stream (after "
                f"{previous})"
            )
        held: list[PitBoundedRecord] = []
        owner: datetime | None = None
        while pending is not None and pending.observation_key == key:
            this_owner = _check_utc(pending.owner_event_time, f"key {key} owner_event_time")
            if owner is None:
                owner = this_owner
            elif this_owner != owner:
                raise CatalogIntegrityError(f"key {key} records disagree on owner_event_time")
            held.append(pending)
            pending = _next_record(source)
        assert owner is not None  # the inner while loop above ran at least once
        yield PitKeyGroup(
            observation_key=key,
            owner_event_time=owner,
            evaluations=_evaluations(key, held, knowledge_cutoff),
        )
        previous = key


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
    ) -> AbstractContextManager[Iterator[PitKeyGroup]]:
        return self._keys(pit, data_type, venue_symbol, start, end)

    @contextmanager
    def _keys(
        self,
        pit: PointInTimeSpec,
        data_type: str,
        venue_symbol: str,
        start: datetime,
        end: datetime,
    ) -> Iterator[Iterator[PitKeyGroup]]:
        with ExitStack() as stack:
            # iter_bounded first: it checks the spec, data type and symbol before anything reads.
            records = stack.enter_context(
                self._selector.iter_bounded(
                    pit, data_type, venue_symbol, start, end, params=self._params, touching=False
                )
            )
            # The inner generator holds the merge readers: close it on any exit, not at GC.
            stack.callback(_close, records)
            groups = pit_key_groups(records, knowledge_cutoff=pit.knowledge_cutoff)
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
            PitSelector(adapter, storage, canonical_scratch_directory=canonical_scratch_directory),
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

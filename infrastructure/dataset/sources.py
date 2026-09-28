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
  runs of ``infrastructure.pit.runs`` (``spill_sorted_runs`` + ``merge_sorted_runs``, every size
  explicit). ``member_spans`` is passed through: its generation order (symbol, then start) *is*
  the builder's required order, and the builder proves it.
- ``PitSelectorKeySource`` wraps B-PIT's ``PitSelector.iter_bounded`` and folds its per-instant
  ``PitBoundedRecord`` stream into one ``PitKeyGroup`` per observation key (``pit_key_groups``).
  Observation keys must be strictly increasing (a duplicate or reordered key fails closed); the
  lineage / gap ``iter_bounded`` attaches only to a revision's first selection inside its key is
  carried to every later selection of that revision in the key.

Neither adapter decides anything the builder re-proves: order, uniqueness, ownership, lineage and
report bindings are all checked again, fail closed, by ``DatasetEvidenceBuilder``.

**Held state.** One sorted-run batch (``capacity`` / ``row_batch_rows`` items) while spilling, the
run references of one stream (one per batch, as B-PIT's own spill), at most ``merge_fanout`` open
run readers, and one observation key's evaluations, revision times and carried lineage (the
per-key bound the task and ADR-0077 §2 "dedupe inside the key group" allow). Nothing spans keys,
symbols or the window.

**OPEN (B-PIT, reported, not changed here).** ``PitBoundedRecord`` carries neither the selected
revision's time column (``event_time`` / ``interval_start``) nor the key's chain-earliest event
(the slice-ownership witness ``PitKeyGroup.owner_event_time``). Until it does,
``PitSelectorKeySource`` re-reads the slice's key closure through the selector's own
``_canonical_rows`` at the same pinned snapshot (the rows ``iter_bounded`` itself evaluates) and
spills ``(observation_key, revision_id, time)`` into sorted runs: the owner is the minimum time of
the key's rows (exactly the ``earliest`` ``_key_closure`` filters ownership on), a selected
revision's time is its row's. The two streams must name the same keys in the same order, and every
selected revision must have a row, or the slice fails closed. The re-read costs one more scan of
the slice's closure (no second proof: ``_verify_canonical`` is not repeated). Once B-PIT adds the
two fields to ``PitBoundedRecord``, ``_revision_times`` is deleted and ``pit_key_groups`` reads them
from the records.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from core.contracts.revision import PointInTimeSpec, PointInTimeStatus
from core.contracts.storage import StorageAdapter
from core.contracts.universe import SelectedRevisionLineage, UniverseExclusion, UniverseMember
from infrastructure.canonical import rules
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
from infrastructure.pit.runs import RunLimits, merge_sorted_runs, spill_sorted_runs
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
    """The sorted-run sizes of ``OrderedUniverseSource`` (DQ-9 OPEN: no defaults).

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
    """One upstream view, drained into sorted runs (upstream closed first), merged back in order.

    Sorting is by the encoded ``order`` only; ties keep an arbitrary order, and duplicates reach
    the builder, which rejects them (adjacent comparison), exactly as it would have unsorted.
    """
    with source() as items:
        refs = list(
            spill_sorted_runs(
                (encode(item) for item in items),
                key=_run_order,
                capacity=params.capacity,
                storage=storage,
                limits=params.limits,
            )
        )
    with merge_sorted_runs(
        storage, refs, key=_run_order, merge_fanout=params.merge_fanout, limits=params.limits
    ) as merged:
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


def _time_column(data_type: str) -> str:
    return "event_time" if data_type == "agg_trades" else "interval_start"


def _revision_time_order(row: Mapping[str, Any]) -> tuple[str, str]:
    return (row["observation_key"], row["revision_id"])


def _check_utc(value: object, what: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != _ZERO:
        raise CatalogIntegrityError(f"{what} is not a timezone-aware UTC datetime")
    return value


class _RevisionTimes:
    """``(observation_key, revision_id, time)`` rows by key then revision, taken one key at a time.

    Holds one key's revision times. Every key read must be taken in key order (a key read but
    never evaluated, or evaluated but never read, fails closed); a repeated or unordered row fails
    closed too.
    """

    def __init__(self, rows: Iterator[Mapping[str, Any]]) -> None:
        self._rows = rows
        self._last: tuple[str, str] | None = None
        self._pending = self._next()

    def _next(self) -> tuple[str, str, datetime] | None:
        row = next(self._rows, None)
        if row is None:
            return None
        key, revision = row.get("observation_key"), row.get("revision_id")
        if not isinstance(key, str) or not key or not isinstance(revision, str) or not revision:
            raise CatalogIntegrityError(f"a Canonical revision time row is malformed: {row!r}")
        at = _check_utc(row.get("event_time"), f"the time of Canonical revision {revision}")
        if self._last is not None and (key, revision) <= self._last:
            raise CatalogIntegrityError(
                f"Canonical revision {revision} of {key} is read twice or out of order"
            )
        self._last = (key, revision)
        return key, revision, at

    def take(self, key: str) -> dict[str, datetime]:
        if self._pending is not None and self._pending[0] < key:
            raise CatalogIntegrityError(
                f"observation keys are out of order or unevaluated: {self._pending[0]} was read "
                f"for the slice but the PIT stream is already at {key}"
            )
        found: dict[str, datetime] = {}
        while self._pending is not None and self._pending[0] == key:
            found[self._pending[1]] = self._pending[2]
            self._pending = self._next()
        if not found:
            raise CatalogIntegrityError(
                f"observation key {key} is evaluated without a Canonical row in the slice's read"
            )
        return found

    def close(self) -> None:
        if self._pending is not None:
            raise CatalogIntegrityError(
                f"observation key {self._pending[0]} was read for the slice but never evaluated"
            )


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
    key: str,
    held: Iterable[PitBoundedRecord],
    times: Mapping[str, datetime],
    knowledge_cutoff: datetime,
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
            at = times.get(revision)
            if at is None:
                raise CatalogIntegrityError(
                    f"selected revision {revision} of {key} has no Canonical row in the slice's "
                    "read"
                )
            selected = PitSelectedRevision(
                revision_id=revision, event_time=at, lineage=lineage, evidence_gap=gap
            )
        elif record.lineage is not None or record.evidence_gap is not None:
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
    records: Iterable[PitBoundedRecord],
    revision_times: Iterable[Mapping[str, Any]],
    *,
    knowledge_cutoff: datetime,
) -> Iterator[PitKeyGroup]:
    """Fold ``iter_bounded``'s records into one ``PitKeyGroup`` per observation key.

    ``records``: key then instant order; one key's records are adjacent and keys strictly
    increase (a duplicate, split or reordered key fails closed). ``revision_times``: the slice's
    ``{"observation_key", "revision_id", "event_time"}`` rows by ``(key, revision)``, the same keys
    as ``records``. A group's ``owner_event_time`` is the earliest time of its key's rows; each
    selected revision's ``event_time`` is its row's time. Holds one key's records and times.
    """
    times = _RevisionTimes(iter(revision_times))
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
        while pending is not None and pending.observation_key == key:
            held.append(pending)
            pending = _next_record(source)
        key_times = times.take(key)
        yield PitKeyGroup(
            observation_key=key,
            owner_event_time=min(key_times.values()),
            evaluations=_evaluations(key, held, key_times, knowledge_cutoff),
        )
        previous = key
    times.close()


@contextmanager
def _revision_times(
    selector: PitSelector,
    storage: StorageAdapter,
    params: PitRunParams,
    pit: PointInTimeSpec,
    data_type: str,
    venue_symbol: str,
    start: datetime,
    end: datetime,
) -> Iterator[Iterator[Mapping[str, Any]]]:
    """The slice's owned keys' revision times by ``(key, revision)`` (see the module's OPEN).

    The same key-closure read ``iter_bounded`` evaluates (same selector, same pinned snapshot,
    ``touching=False``), reduced to three columns and spilled into sorted runs; the read list is
    released before the merge streams, as in ``iter_bounded``.
    """
    canonical = rules.CANONICAL_TABLES[data_type]
    instrument = rules.SYMBOLS[venue_symbol]
    column = _time_column(data_type)
    view = selector._pinned(pit)
    rows = selector._canonical_rows(
        view, canonical.table, data_type, instrument.symbol, start, end, False
    )
    refs = list(
        spill_sorted_runs(
            (
                {
                    "observation_key": row["observation_key"],
                    "revision_id": row["revision_id"],
                    "event_time": row[column],
                }
                for row in rows
            ),
            key=_revision_time_order,
            capacity=params.row_batch_rows,
            storage=storage,
            limits=params.limits,
        )
    )
    del rows
    with merge_sorted_runs(
        storage,
        refs,
        key=_revision_time_order,
        merge_fanout=params.merge_fanout,
        limits=params.limits,
    ) as merged:
        yield merged


def _close(iterator: object) -> None:
    close = getattr(iterator, "close", None)
    if callable(close):
        close()


class PitSelectorKeySource:
    """``PitKeySource`` over ``PitSelector.iter_bounded`` (owned keys only, ``touching=False``).

    ``params`` are B-PIT's explicit run sizes (DQ-9 OPEN); the revision-time runs use its
    ``row_batch_rows``, ``merge_fanout`` and ``limits``. ``storage`` receives those runs.
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
            times = stack.enter_context(
                _revision_times(
                    self._selector,
                    self._storage,
                    self._params,
                    pit,
                    data_type,
                    venue_symbol,
                    start,
                    end,
                )
            )
            groups = pit_key_groups(records, times, knowledge_cutoff=pit.knowledge_cutoff)
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
    market_data_base_url: str,
    pit_params: PitRunParams,
    universe_params: UniverseRunParams,
) -> DatasetEvidenceSources:
    """The real upstreams of ``request`` for ``DatasetEvidenceBuilder.select`` / ``build``.

    Universe: ``UniverseBuilder.cursor(request.universe, request.pit)`` in ADR-0077 §2 order;
    PIT: ``PitSelector.iter_bounded`` grouped by key; quality: ``PinnedQualityEvidence`` at the
    PIT spec's bound snapshots. Every run size is the caller's (no defaults).
    """
    if not isinstance(request, DatasetEvidenceRequest):
        raise DatasetSpecError("request must be a DatasetEvidenceRequest")
    cursor = UniverseBuilder(adapter, storage, market_data_base_url=market_data_base_url).cursor(
        request.universe, request.pit
    )
    return DatasetEvidenceSources(
        universe=OrderedUniverseSource(cursor, storage=storage, params=universe_params),
        pit=PitSelectorKeySource(
            PitSelector(adapter, storage), storage=storage, params=pit_params
        ),
        quality=PinnedQualityEvidence(
            adapter,
            storage,
            request.pit,
            request.data_type,
            market_data_base_url=market_data_base_url,
        ),
    )

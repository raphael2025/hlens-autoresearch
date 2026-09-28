"""The historical tradable universe at a PIT spec's bound snapshots (Phase 1 F2; ADR-0024).

``UniverseBuilder.build(spec, pit)`` answers which listing episodes of a registered
``UniverseSelectionSpec`` are members, and which are excluded and why, at the spec's simulation
time (or over its simulation interval) and ``knowledge_cutoff``:

1. **spec** — the spec must be registered here with its exact content hash (first slice:
   ``binance.spot.btc-eth@1.0.0``, BTCUSDT + ETHUSDT spot, no filter). A spec with filters is
   refused: no filter metric can be evaluated point-in-time yet, and no threshold is invented;
2. **bindings** — the PIT spec must bind ``canonical.instrument_listings`` and
   ``raw.binance_spot_exchange_info`` and carry the exact listing bindings (exchange-info
   availability, listing-status derivation, listing-observation precedence). Missing listing
   history is refused, never replaced by today's symbol list;
3. **read** — every read goes through a ``PinnedCatalogView`` of the bound snapshots and E2's
   ``ListingDeriver.listing_at`` (which proves the snapshot rows and the listing batches). Any
   unconstructible answer — no visible listing (e.g. a simulation time before the first local
   observation, ADR-0029 §3), several episodes, competing heads, an unresolved latest observation,
   a tie, a derivation that lags or diverged — fails the whole build closed with its reason;
4. **members / exclusions** — an episode is a member while its selected listing revision is
   tradable at the simulation time, otherwise it is excluded as ``not_tradable`` (suspended). For
   an interval the answer is evaluated at the interval start and at every instant inside it where
   it can change (a snapshot's ``retrieved_at``, a listing revision's ``available_time``, an
   interval boundary); consecutive identical answers are merged into one span.

Spec symbols are venue-native (``BTCUSDT``); an episode is a candidate when it was derived from
that venue symbol's observations and its instrument is the frozen Canonical form of it
(``BTC-USDT``, ``infrastructure.canonical.rules.SYMBOLS``). ``arrival_seq``, wall clocks and
payload hashes are never read for a decision.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from core.contracts.revision import PointInTimeSpec, PolicyBinding
from core.contracts.storage import StorageAdapter
from core.contracts.universe import (
    ExclusionReason,
    ListingEpisodeKey,
    SelectedRevisionLineage,
    UniverseCandidateSource,
    UniverseExclusion,
    UniverseMember,
    UniverseSelectionSpec,
)
from core.domain.specs import InstrumentType
from infrastructure.canonical import listing_rules as lr
from infrastructure.canonical.listings import ListingDeriver, ListingPointInTime
from infrastructure.canonical.rules import SYMBOLS
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_EXCHANGE_INFO,
    CANONICAL_INSTRUMENT_LISTINGS,
)
from infrastructure.contract_version import PHASE1_PUBLICATION_VERSION
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision.exchange_info_availability import EXCHANGE_INFO_AVAILABILITY_BINDING
from infrastructure.revision.store import RevisionCatalog

__all__ = [
    "FIRST_SLICE_UNIVERSE",
    "LISTING_REQUIRED_BINDINGS",
    "REGISTERED_UNIVERSES",
    "UniverseBuildError",
    "UniverseBuilder",
    "UniverseBuilt",
    "UniverseFilterUnavailable",
    "UniverseSpanCursor",
    "UniverseSpecError",
    "UniverseUnconstructible",
    "check_listing_bindings",
]

LISTINGS_TABLE: Final = CANONICAL_INSTRUMENT_LISTINGS.table
EXCHANGE_INFO_TABLE: Final = BINANCE_SPOT_EXCHANGE_INFO.table

#: The first-slice universe (03-data.md §7.3): BTCUSDT and ETHUSDT spot listing episodes.
FIRST_SLICE_UNIVERSE: Final = UniverseSelectionSpec(
    schema_version=PHASE1_PUBLICATION_VERSION,
    name="binance.spot.btc-eth",
    version="1.0.0",
    candidate_source=UniverseCandidateSource.POINT_IN_TIME_LISTINGS,
    venue="binance",
    instrument_type=InstrumentType.SPOT,
    symbols=("BTCUSDT", "ETHUSDT"),
    filters=(),
)
#: Registered specs by ``(name, version)``; a request must match the content hash exactly.
REGISTERED_UNIVERSES: Final[Mapping[tuple[str, str], UniverseSelectionSpec]] = {
    (FIRST_SLICE_UNIVERSE.name, FIRST_SLICE_UNIVERSE.version): FIRST_SLICE_UNIVERSE,
}
#: Bindings a PIT spec must carry (exact id + version + hash) to read the listing history.
LISTING_REQUIRED_BINDINGS: Final[Mapping[str, tuple[PolicyBinding, ...]]] = {
    "availability_bindings": (EXCHANGE_INFO_AVAILABILITY_BINDING,),
    "precedence_bindings": (lr.LISTING_OBSERVATION_BINDING,),
    "parser_bindings": (lr.LISTING_STATUS_BINDING,),
}


class UniverseBuildError(Exception):
    """Base class: no universe (and so no dataset) can be built from these inputs."""


class UniverseSpecError(UniverseBuildError, ValueError):
    """The selection spec or the PIT spec cannot drive a universe build (fail closed)."""


class UniverseFilterUnavailable(UniverseSpecError):
    """The spec has filters whose point-in-time metric inputs are not available (refused)."""


class UniverseUnconstructible(UniverseBuildError):
    """A candidate's point-in-time listing is unconstructible (ADR-0024 / ADR-0029 fail closed)."""

    def __init__(self, point: ListingPointInTime) -> None:
        self.venue_symbol = point.venue_symbol
        self.simulation_time = point.simulation_time
        self.reason = point.reason or "unconstructible"
        super().__init__(
            f"universe unconstructible: {point.venue_symbol} at simulation "
            f"{point.simulation_time.isoformat()} / knowledge {point.knowledge_cutoff.isoformat()}"
            f": {self.reason}; {point.detail}"
        )


@dataclass(frozen=True, slots=True)
class _Span:
    """One merged stretch of one candidate's answer (``start``/``end`` None = a point)."""

    start: datetime | None
    end: datetime | None
    point: ListingPointInTime


@dataclass(frozen=True, slots=True)
class UniverseBuilt:
    """The universe of one spec under one PIT spec, with what the manifest must bind."""

    spec: UniverseSelectionSpec
    members: tuple[UniverseMember, ...]
    exclusions: tuple[UniverseExclusion, ...]
    #: ``canonical.instrument_listings`` lineage of every listing revision an entry cites.
    lineage: tuple[SelectedRevisionLineage, ...]
    #: ``(revision_id, gap)`` of every cited listing revision with an availability evidence gap.
    evidence_gaps: tuple[tuple[str, str], ...]
    #: Member spans per venue symbol: ``(None, None)`` for a point simulation.
    member_spans: Mapping[str, tuple[tuple[datetime | None, datetime | None], ...]]

    def is_member(self, venue_symbol: str) -> bool:
        return bool(self.member_spans.get(venue_symbol))


def _check_spec(spec: UniverseSelectionSpec) -> None:
    if not isinstance(spec, UniverseSelectionSpec):
        raise UniverseSpecError("spec must be a UniverseSelectionSpec")
    if spec.filters:
        raise UniverseFilterUnavailable(
            f"{spec.name}@{spec.version} has filters "
            f"{[item.filter_id for item in spec.filters]}: no point-in-time metric input is "
            "available to evaluate them, so the universe is refused (no threshold is invented)"
        )
    registered = REGISTERED_UNIVERSES.get((spec.name, spec.version))
    if registered is None or registered.content_hash() != spec.content_hash():
        raise UniverseSpecError(
            f"universe spec {spec.name}@{spec.version} with this content hash is not registered"
        )
    if spec.candidate_source is not UniverseCandidateSource.POINT_IN_TIME_LISTINGS:
        raise UniverseSpecError("only point-in-time listing candidates exist")  # pragma: no cover
    for symbol in spec.symbols:
        if symbol not in lr.FIRST_SLICE_ASSETS or symbol not in SYMBOLS:
            raise UniverseSpecError(f"no listing history source covers {symbol!r}")


def check_listing_bindings(pit: PointInTimeSpec) -> None:
    """The PIT spec binds the listing tables and the exact listing policies (or refuse)."""
    if not isinstance(pit, PointInTimeSpec):
        raise UniverseSpecError("pit must be a PointInTimeSpec")
    for table in (LISTINGS_TABLE, EXCHANGE_INFO_TABLE):
        if table not in pit.snapshot_bindings:
            raise UniverseSpecError(
                f"the PIT spec does not bind {table}: the listing history is missing and the "
                "universe cannot be built (never replaced by today's symbol list)"
            )
    for field, required in LISTING_REQUIRED_BINDINGS.items():
        bound = {binding.policy_id: binding for binding in getattr(pit, field)}
        for binding in required:
            if bound.get(binding.policy_id) != binding:
                raise UniverseSpecError(
                    f"{field} must bind {binding.policy_id}@{binding.version} with its exact hash"
                )


class UniverseBuilder:
    """Builds members and exclusions at a PIT spec's bound snapshots; never writes."""

    def __init__(
        self, adapter: RevisionCatalog, storage: StorageAdapter, *, market_data_base_url: str
    ) -> None:
        self._adapter = adapter
        self._storage = storage
        self._origin = market_data_base_url

    def build(self, spec: UniverseSelectionSpec, pit: PointInTimeSpec) -> UniverseBuilt:
        _check_spec(spec)
        check_listing_bindings(pit)
        view = PinnedCatalogView(self._adapter, pit.snapshot_bindings)
        deriver = ListingDeriver(view, self._storage, market_data_base_url=self._origin)
        try:
            instants = _instants(view, pit)
            timelines = {
                symbol: _timeline(deriver, symbol, instants, pit) for symbol in spec.symbols
            }
        finally:
            deriver.close()
        return _assemble(spec, pit, timelines)

    def cursor(self, spec: UniverseSelectionSpec, pit: PointInTimeSpec) -> UniverseSpanCursor:
        """The v3 entry (ADR-0077 §6.1.1): a cursor of explicitly-closed, ordered iterators over
        one build's members, exclusions, listing lineage, member spans and evidence gaps --
        without materializing :class:`UniverseBuilt`'s full tuples or its ``timelines`` /
        ``member_spans`` working maps. ``build()`` and ``UniverseBuilt`` are unchanged and remain
        the v2 legacy, fully-materialized path; see :class:`UniverseSpanCursor` for the v3 shape.
        """
        return UniverseSpanCursor(self._adapter, self._storage, self._origin, spec, pit)


def _instants(view: PinnedCatalogView, pit: PointInTimeSpec) -> list[datetime]:
    """The evaluation instants: the point, or the interval start + every possible change."""
    if pit.simulation_time is not None:
        return [pit.simulation_time]
    start, end = pit.simulation_start, pit.simulation_end
    if start is None or end is None:  # pragma: no cover - the contract forbids it
        raise UniverseSpecError("the PIT spec has neither a simulation time nor an interval")
    cutoff = pit.knowledge_cutoff
    changes: set[datetime] = set()
    for row in view.scan_columns(
        EXCHANGE_INFO_TABLE, columns=("retrieved_at", "knowledge_time")
    ).to_pylist():
        if row["knowledge_time"] <= cutoff:
            changes.add(row["retrieved_at"])
    for row in view.scan_columns(
        LISTINGS_TABLE, columns=("available_time", "knowledge_time", "tradable_intervals")
    ).to_pylist():
        if row["knowledge_time"] <= cutoff:
            changes.add(row["available_time"])
            for interval in row["tradable_intervals"]:
                changes.add(interval["tradable_from"])
                if interval["tradable_until"] is not None:
                    changes.add(interval["tradable_until"])
    return [start, *sorted(item for item in changes if start < item < end)]


def _timeline(
    deriver: ListingDeriver, venue_symbol: str, instants: list[datetime], pit: PointInTimeSpec
) -> list[ListingPointInTime]:
    answers = []
    expected = SYMBOLS[venue_symbol]
    for instant in instants:
        point = deriver.listing_at(venue_symbol, instant, pit.knowledge_cutoff)
        if not point.constructible or point.listing is None or point.tradable is None:
            raise UniverseUnconstructible(point)
        instrument = point.listing.instrument
        if (instrument.venue, instrument.instrument_type.value, instrument.symbol) != (
            expected.venue,
            expected.instrument_type,
            expected.symbol,
        ):  # pragma: no cover - E2 derives exactly this instrument
            raise CatalogIntegrityError(
                f"listing {point.listing.revision.revision_id} is not {venue_symbol}'s instrument"
            )
        answers.append(point)
    return answers


def _spans(points: list[ListingPointInTime], pit: PointInTimeSpec) -> list[_Span]:
    if pit.simulation_time is not None:
        [point] = points
        return [_Span(None, None, point)]
    end = pit.simulation_end
    assert end is not None  # the contract: an interval has both ends
    spans: list[_Span] = []
    for index, point in enumerate(points):
        until = points[index + 1].simulation_time if index + 1 < len(points) else end
        previous = spans[-1] if spans else None
        if previous is not None and _same(previous.point, point):
            spans[-1] = _Span(previous.start, until, previous.point)
        else:
            spans.append(_Span(point.simulation_time, until, point))
    return spans


def _same(a: ListingPointInTime, b: ListingPointInTime) -> bool:
    assert a.listing is not None and b.listing is not None
    return (a.listing.revision.revision_id, a.tradable) == (
        b.listing.revision.revision_id,
        b.tradable,
    )


def _assemble(
    spec: UniverseSelectionSpec,
    pit: PointInTimeSpec,
    timelines: Mapping[str, list[ListingPointInTime]],
) -> UniverseBuilt:
    members: list[UniverseMember] = []
    exclusions: list[UniverseExclusion] = []
    lineage: dict[str, SelectedRevisionLineage] = {}
    gaps: dict[str, str] = {}
    member_spans: dict[str, tuple[tuple[datetime | None, datetime | None], ...]] = {}
    for venue_symbol, points in sorted(timelines.items()):
        mine: list[tuple[datetime | None, datetime | None]] = []
        for span in _spans(points, pit):
            listing = span.point.listing
            assert listing is not None and span.point.lineage is not None
            revision = listing.revision.revision_id
            episode: ListingEpisodeKey = listing.episode
            if span.point.tradable:
                members.append(
                    UniverseMember(
                        episode=episode,
                        listing_revision_id=revision,
                        effective_from=span.start,
                        effective_until=span.end,
                    )
                )
                mine.append((span.start, span.end))
            else:
                exclusions.append(
                    UniverseExclusion(
                        episode=episode,
                        listing_revision_id=revision,
                        reason=ExclusionReason.NOT_TRADABLE,
                        effective_from=span.start,
                        effective_until=span.end,
                    )
                )
            lineage[revision] = span.point.lineage
            if span.point.evidence_gap is not None:
                gaps[revision] = span.point.evidence_gap
        member_spans[venue_symbol] = tuple(mine)
    return UniverseBuilt(
        spec=spec,
        members=tuple(members),
        exclusions=tuple(exclusions),
        lineage=tuple(lineage[revision] for revision in sorted(lineage)),
        evidence_gaps=tuple(sorted(gaps.items())),
        member_spans=member_spans,
    )


# ==========================================================================================
# v3: bounded cursor / generator entry (ADR-0077 §6.1.1)
#
# ``build()`` / ``UniverseBuilt`` above are untouched and remain the v2 legacy, fully
# materialized replay path. Everything below is additive: a new ``UniverseBuilder.cursor()``
# entry that produces members, exclusions, listing lineage, member spans and evidence gaps as
# explicitly-closed, ordered iterators, without ever holding v2's full ``timelines`` dict,
# ``members`` / ``exclusions`` lists, ``lineage`` / ``gaps`` dicts or ``member_spans`` mapping
# (ADR-0077 §6.1 item 1 / item 4). Instants are read from bounded ``scan_column_batches``
# batches instead of ``scan_columns(...).to_pylist()`` on the whole table (ADR-0077 §6.1 item 1);
# the accepted-instant working set is bounded by the number of distinct listing / exchange-info
# change events inside the simulation window and before the knowledge cutoff -- not by the size
# of the underlying tables -- which is the concrete violation this replaces. It is *not* a
# general external sorted-run merge (that machinery is ``PitSelector``'s, ADR-0077 §6.1 item 2/3
# / DQ-9); a genuinely unbounded change-event count within one window is out of this slice's
# scope and would need its own Decision Packet, per ADR-0077 §6.1's closing paragraph.
# ==========================================================================================


def _instants_v3(view: PinnedCatalogView, pit: PointInTimeSpec) -> tuple[datetime, ...]:
    """The same evaluation instants as v2's ``_instants`` (the point, or the interval start plus
    every possible change), read via bounded ``scan_column_batches`` batches instead of a
    whole-table ``scan_columns(...).to_pylist()`` (ADR-0077 §6.1 item 1). Each batch is folded
    into the window-filtered ``changes`` set and discarded; nothing outside the simulation window
    or after the knowledge cutoff is kept.
    """
    if pit.simulation_time is not None:
        return (pit.simulation_time,)
    start, end = pit.simulation_start, pit.simulation_end
    if start is None or end is None:  # pragma: no cover - the contract forbids it
        raise UniverseSpecError("the PIT spec has neither a simulation time nor an interval")
    cutoff = pit.knowledge_cutoff
    changes: set[datetime] = set()
    _fold_exchange_info_changes(view, cutoff, changes)
    _fold_listing_changes(view, cutoff, changes)
    return (start, *sorted(item for item in changes if start < item < end))


def _close_reader(reader: object) -> None:
    close = getattr(reader, "close", None)
    if callable(close):
        close()


def _fold_exchange_info_changes(
    view: PinnedCatalogView, cutoff: datetime, changes: set[datetime]
) -> None:
    """Fold every exchange-info ``retrieved_at`` visible by ``cutoff`` into ``changes``."""
    reader = view.scan_column_batches(
        EXCHANGE_INFO_TABLE, columns=("retrieved_at", "knowledge_time")
    )
    try:
        for record_batch in reader:
            for row in record_batch.to_pylist():
                if row["knowledge_time"] <= cutoff:
                    changes.add(row["retrieved_at"])
    finally:
        _close_reader(reader)


def _fold_listing_changes(
    view: PinnedCatalogView, cutoff: datetime, changes: set[datetime]
) -> None:
    """Fold every listing ``available_time`` and tradable-interval boundary visible by ``cutoff``
    into ``changes``."""
    reader = view.scan_column_batches(
        LISTINGS_TABLE, columns=("available_time", "knowledge_time", "tradable_intervals")
    )
    try:
        for record_batch in reader:
            for row in record_batch.to_pylist():
                if row["knowledge_time"] <= cutoff:
                    changes.add(row["available_time"])
                    for interval in row["tradable_intervals"]:
                        changes.add(interval["tradable_from"])
                        if interval["tradable_until"] is not None:
                            changes.add(interval["tradable_until"])
    finally:
        _close_reader(reader)


def _timeline_stream(
    deriver: ListingDeriver, venue_symbol: str, instants: Iterable[datetime], pit: PointInTimeSpec
) -> Iterator[ListingPointInTime]:
    """The v3 analogue of ``_timeline``: validates and yields one point per instant lazily; no
    ``list`` of every instant's answer is held once it has been consumed by :func:`_spans_stream`.
    """
    expected = SYMBOLS[venue_symbol]
    for instant in instants:
        point = deriver.listing_at(venue_symbol, instant, pit.knowledge_cutoff)
        if not point.constructible or point.listing is None or point.tradable is None:
            raise UniverseUnconstructible(point)
        instrument = point.listing.instrument
        if (instrument.venue, instrument.instrument_type.value, instrument.symbol) != (
            expected.venue,
            expected.instrument_type,
            expected.symbol,
        ):  # pragma: no cover - E2 derives exactly this instrument
            raise CatalogIntegrityError(
                f"listing {point.listing.revision.revision_id} is not {venue_symbol}'s instrument"
            )
        yield point


def _spans_stream(points: Iterator[ListingPointInTime], pit: PointInTimeSpec) -> Iterator[_Span]:
    """The v3 analogue of ``_spans``: merges consecutive identical answers with a single-item
    lookahead (``current`` / the next ``point``) instead of indexing a materialized ``points``
    list -- O(1) extra state per symbol, not O(instants).
    """
    if pit.simulation_time is not None:
        yield _Span(None, None, next(points))
        return
    end = pit.simulation_end
    assert end is not None  # the contract: an interval has both ends
    try:
        current = next(points)
    except StopIteration:  # pragma: no cover - _instants_v3 always yields at least `start`
        return
    current_start = current.simulation_time
    for point in points:
        if _same(current, point):
            continue
        yield _Span(current_start, point.simulation_time, current)
        current = point
        current_start = point.simulation_time
    yield _Span(current_start, end, current)


@dataclass(frozen=True, slots=True)
class _SpanEvent:
    """One finalized v3 span, already projected for each of :class:`UniverseSpanCursor`'s views.

    Exactly one of ``member`` / ``exclusion`` is set (mirrors the v2 ``if span.point.tradable``
    branch). ``lineage`` / ``gap`` are set only the first time this walk sees
    ``listing_revision_id`` for this symbol (the same revision can recur non-adjacently, e.g. a
    halt/resume returning to it); later spans citing it carry ``None`` for both.
    """

    venue_symbol: str
    member: UniverseMember | None
    exclusion: UniverseExclusion | None
    listing_revision_id: str
    lineage: SelectedRevisionLineage | None
    gap: str | None


def _events_v3(
    adapter: RevisionCatalog,
    storage: StorageAdapter,
    origin: str,
    spec: UniverseSelectionSpec,
    pit: PointInTimeSpec,
) -> Iterator[_SpanEvent]:
    """The bounded walk behind every :class:`UniverseSpanCursor` view (ADR-0077 §6.1.1).

    One ``PinnedCatalogView`` + one ``ListingDeriver`` for the whole walk, released on
    exhaustion, an early ``.close()`` (thrown in as ``GeneratorExit`` at the current ``yield``)
    or an exception -- the same ``try/finally`` shape as v2's ``build()``. ``spec.symbols`` is
    already canonically sorted (``UniverseSelectionSpec._canonical_symbols``); each symbol is
    walked in full, one span at a time, before the next. No ``timelines``, ``members``,
    ``exclusions`` or ``member_spans`` collection spans more than the current symbol: the
    per-symbol ``seen`` set is bounded by that one symbol's own distinct listing revisions inside
    the window, not by row count or window length. The window-filtered ``instants`` tuple
    (:func:`_instants_v3`) is computed once and shared by every symbol's walk -- itself bounded
    the same way, and unrelated to the ``.to_pylist()`` whole-table read it replaces.
    """
    view = PinnedCatalogView(adapter, pit.snapshot_bindings)
    deriver = ListingDeriver(view, storage, market_data_base_url=origin)
    try:
        instants = _instants_v3(view, pit)
        for venue_symbol in spec.symbols:
            seen: set[str] = set()
            points = _timeline_stream(deriver, venue_symbol, instants, pit)
            for span in _spans_stream(points, pit):
                listing = span.point.listing
                assert listing is not None and span.point.lineage is not None
                revision = listing.revision.revision_id
                episode = listing.episode
                member: UniverseMember | None = None
                exclusion: UniverseExclusion | None = None
                if span.point.tradable:
                    member = UniverseMember(
                        episode=episode,
                        listing_revision_id=revision,
                        effective_from=span.start,
                        effective_until=span.end,
                    )
                else:
                    exclusion = UniverseExclusion(
                        episode=episode,
                        listing_revision_id=revision,
                        reason=ExclusionReason.NOT_TRADABLE,
                        effective_from=span.start,
                        effective_until=span.end,
                    )
                lineage: SelectedRevisionLineage | None = None
                gap: str | None = None
                if revision not in seen:
                    seen.add(revision)
                    lineage = span.point.lineage
                    gap = span.point.evidence_gap
                yield _SpanEvent(
                    venue_symbol=venue_symbol,
                    member=member,
                    exclusion=exclusion,
                    listing_revision_id=revision,
                    lineage=lineage,
                    gap=gap,
                )
    finally:
        deriver.close()


def _member_of(event: _SpanEvent) -> UniverseMember | None:
    return event.member


def _exclusion_of(event: _SpanEvent) -> UniverseExclusion | None:
    return event.exclusion


def _lineage_of(event: _SpanEvent) -> SelectedRevisionLineage | None:
    return event.lineage


def _member_span_of(event: _SpanEvent) -> tuple[str, datetime | None, datetime | None] | None:
    if event.member is None:
        return None
    return (event.venue_symbol, event.member.effective_from, event.member.effective_until)


def _gap_of(event: _SpanEvent) -> tuple[str, str] | None:
    if event.gap is None:
        return None
    return (event.listing_revision_id, event.gap)


@contextmanager
def _project[T](
    events: Iterator[_SpanEvent], extract: Callable[[_SpanEvent], T | None]
) -> Iterator[Iterator[T]]:
    """Wrap the shared walk as one explicitly-closed, filtered view (ADR-0077 §5's
    ``ContextManager[Iterator[record]]`` shape). ``events.close()`` always runs on ``__exit__``
    (exhaustion, an early ``break`` out of the ``with`` block, or an exception propagating
    through it); closing an already-exhausted generator is a no-op, so this is safe whichever way
    the caller stops.
    """

    def items() -> Iterator[T]:
        for event in events:
            value = extract(event)
            if value is not None:
                yield value

    try:
        yield items()
    finally:
        events.close()


class UniverseSpanCursor:
    """A v3, explicitly-closed cursor over one ``(spec, pit)`` build (ADR-0077 §6.1.1).

    Unlike :meth:`UniverseBuilder.build`, no member, exclusion, listing-lineage, member-span or
    gap tuple is held in full. Each view below is its own bounded, closable pass over the pinned
    view and the listing deriver -- ``AbstractContextManager[Iterator[T]]`` (ADR-0077 §5's
    ``iter_evidence`` / ``iter_rows`` shape): entering opens nothing eagerly (construction already
    validated ``spec`` / ``pit``, ADR-0024 fail-closed), exiting -- by exhaustion, an early
    ``break``, or an exception -- always releases the ``PinnedCatalogView`` + ``ListingDeriver``
    behind it. Consuming more than one view re-walks the spec's symbols and the pinned change
    instants once per view (no cursor state -- file handles, an open deriver -- is shared or kept
    alive between calls); callers that need several streams from one build open several views,
    each under its own ``with``.

    **Read-only iterator Protocol B1 (``DatasetBuilder``'s v3 entry) consumes**: each of the five
    methods below has the signature ``() -> AbstractContextManager[Iterator[T]]`` for its stated
    ``T``; a caller does ``with cursor.members() as members:\\n    for member in members: ...``.
    No method takes the ``spec`` / ``pit`` again -- both are fixed at cursor construction
    (:meth:`UniverseBuilder.cursor`).

    ``build()`` / ``UniverseBuilt`` are unchanged and remain the v2 legacy, fully-materialized
    path; this class does not wrap or call them.
    """

    def __init__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        origin: str,
        spec: UniverseSelectionSpec,
        pit: PointInTimeSpec,
    ) -> None:
        _check_spec(spec)
        check_listing_bindings(pit)
        self._adapter = adapter
        self._storage = storage
        self._origin = origin
        self._spec = spec
        self._pit = pit

    def _events(self) -> Iterator[_SpanEvent]:
        return _events_v3(self._adapter, self._storage, self._origin, self._spec, self._pit)

    def members(self) -> AbstractContextManager[Iterator[UniverseMember]]:
        """Members in generation order: ``spec.symbols`` (already canonical) ascending, then
        time ascending within each symbol.

        **Not** necessarily the v3 evidence stream's canonical
        ``(episode.observation_key(), effective_from)`` order (ADR-0077 §2) when a symbol's own
        episode identity changes mid-timeline (a degraded-key rename, ADR-0024 §2): generation
        order and that global order coincide whenever every symbol keeps one episode for the
        whole build (true of the first-slice universe today). Reconciling the two -- or proving
        they always coincide for every registered spec -- is B1's job when it assembles the
        ``members`` evidence stream from this cursor's output; open item for that slice.
        """
        return _project(self._events(), _member_of)

    def exclusions(self) -> AbstractContextManager[Iterator[UniverseExclusion]]:
        """Exclusions in the same generation order as :meth:`members` (see its docstring for the
        canonical-order caveat)."""
        return _project(self._events(), _exclusion_of)

    def listing_lineage(self) -> AbstractContextManager[Iterator[SelectedRevisionLineage]]:
        """One entry per distinct ``(venue_symbol, listing_revision_id)`` touched by the walk,
        first occurrence only: the same revision selected again later by the same symbol's
        timeline (e.g. a halt/resume returning to it) is not repeated. A revision cannot in fact
        belong to two symbols (ADR-0024 episode scoping; ``ListingHistory`` enforces one
        ``observation_key`` per ``revision_id``), so this per-symbol de-duplication is also the
        walk's full de-duplication -- no cross-symbol working set is needed. The dataset's other
        lineage hops (raw / source) are B1's responsibility, not this cursor's.
        """
        return _project(self._events(), _lineage_of)

    def member_spans(
        self,
    ) -> AbstractContextManager[Iterator[tuple[str, datetime | None, datetime | None]]]:
        """``(venue_symbol, effective_from, effective_until)`` for every **member** span only
        (exclusion spans are not included, matching v2 ``UniverseBuilt.member_spans``): one row
        per span instead of one ``Mapping`` entry per symbol holding a tuple of all of its spans.
        A single point simulation yields ``(symbol, None, None)``.
        """
        return _project(self._events(), _member_span_of)

    def evidence_gaps(self) -> AbstractContextManager[Iterator[tuple[str, str]]]:
        """``(listing_revision_id, gap)`` for every gap-bearing revision, de-duplicated exactly
        like :meth:`listing_lineage`. Not one of this task's four named streams, but required by
        ADR-0077 §6.1 item 1 ("...逐项产生成员 / 排除 / lineage / gap") and by B1's
        ``evidence_gaps`` evidence stream, so it is provided alongside the other three.
        """
        return _project(self._events(), _gap_of)

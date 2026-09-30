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

**ADR-0051 listing backfill assumption (D-LIST, second phase).** Every read passes the PIT spec
to ``listing_at``; only a spec that binds ``hlens.listing.observed-state-backfill-assumption``
(exact version and hash, checked eagerly by ``check_listing_bindings``) can get an assumed
answer, and only for ``[backfill_floor, first observed tradable_from)`` of an episode's first
revision. Such a span is its own member span (never merged with the observed span after it), its
``UniverseMember.assumption`` is the bound policy, and it is listed in ``UniverseBuilt.assumed`` /
``UniverseSpanCursor.assumed()``. The cited revision's lineage and its ADR-0029 "observed-from"
evidence gap are listed exactly as for an observed span. A spec that does not bind the assumption
gets exactly the answers it got before this assumption existed.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

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
from infrastructure.pit.runs import RunLimits, RunRef, RunSetBuilder, iter_run
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision.exchange_info_availability import EXCHANGE_INFO_AVAILABILITY_BINDING
from infrastructure.revision.store import RevisionCatalog
from infrastructure.universe import listing_assumption as backfill
from infrastructure.universe.run_params import UniverseRunParams

__all__ = [
    "FIRST_SLICE_UNIVERSE",
    "LISTING_REQUIRED_BINDINGS",
    "REGISTERED_UNIVERSES",
    "AssumedMembership",
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
class AssumedMembership:
    """One member span that exists only under the ADR-0051 listing backfill assumption (§3).

    Not a contract: the manifest carries the same fact as ``UniverseMember.assumption`` (ADR-0088
    decision 6). ``effective_from`` / ``effective_until`` are the member span (``None`` for a point
    simulation); ``backfill_floor`` / ``first_observed_from`` are the assumed interval
    ``[backfill_floor, first observed tradable_from)`` and ``effective_available_time`` the first
    revision's effective availability (never later than stored); ``binding`` is the bound policy.
    """

    venue_symbol: str
    listing_revision_id: str
    effective_from: datetime | None
    effective_until: datetime | None
    backfill_floor: datetime
    first_observed_from: datetime
    effective_available_time: datetime
    binding: PolicyBinding


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
    #: Venue symbol -> its one member span that exists only under the ADR-0051 listing backfill
    #: assumption (§3); empty when the PIT spec does not bind it (or it applies to no one).
    assumed: Mapping[str, AssumedMembership] = field(default_factory=dict)

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
    for name, required in LISTING_REQUIRED_BINDINGS.items():
        bound = {binding.policy_id: binding for binding in getattr(pit, name)}
        for binding in required:
            if bound.get(binding.policy_id) != binding:
                raise UniverseSpecError(
                    f"{name} must bind {binding.policy_id}@{binding.version} with its exact hash"
                )
    # ADR-0051 §2: the listing backfill assumption is allowed, never required; naming it with
    # another version or hash is refused here, before any read.
    try:
        backfill.assumption_bound(pit)
    except backfill.AssumptionSpecError as exc:
        raise UniverseSpecError(str(exc)) from exc


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

    def cursor(
        self,
        spec: UniverseSelectionSpec,
        pit: PointInTimeSpec,
        *,
        run_params: UniverseRunParams,
    ) -> UniverseSpanCursor:
        """The v3 entry (ADR-0077 §6.1.1): a cursor of explicitly-closed, ordered iterators over
        one build's members, exclusions, listing lineage, member spans and evidence gaps --
        without materializing :class:`UniverseBuilt`'s full tuples or its ``timelines`` /
        ``member_spans`` working maps. ``build()`` and ``UniverseBuilt`` are unchanged and remain
        the v2 legacy, fully-materialized path; see :class:`UniverseSpanCursor` for the v3 shape.
        ``run_params`` is mandatory and carries the caller's explicit DQ-9 resource values.
        """
        _check_spec(spec)
        check_listing_bindings(pit)
        _validate_run_params(run_params)
        return UniverseSpanCursor(
            self._adapter, self._storage, self._origin, spec, pit, run_params=run_params
        )


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
        point = deriver.listing_at(venue_symbol, instant, pit.knowledge_cutoff, pit=pit)
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
    """Same answer: same revision, same tradability, and both assumed or both observed (an
    ADR-0051 assumed span never merges with the observed span of the same first revision)."""
    assert a.listing is not None and b.listing is not None
    return (a.listing.revision.revision_id, a.tradable, a.assumed) == (
        b.listing.revision.revision_id,
        b.tradable,
        b.assumed,
    )


def _assumed_of(venue_symbol: str, span: _Span, pit: PointInTimeSpec) -> AssumedMembership | None:
    """The ADR-0051 membership of a span whose answer came from the assumption, else ``None``."""
    point = span.point
    if not point.assumed:
        return None
    interval = point.assumption
    listing = point.listing
    if (
        interval is None
        or listing is None
        or point.tradable is not True
        or backfill.bound_binding(pit) != interval.binding
    ):
        raise CatalogIntegrityError(
            f"{venue_symbol}: an assumed listing answer without the bound "
            f"{backfill.ASSUMPTION_ID} policy, its interval or a tradable listing"
        )
    return AssumedMembership(
        venue_symbol=venue_symbol,
        listing_revision_id=listing.revision.revision_id,
        effective_from=span.start,
        effective_until=span.end,
        backfill_floor=interval.backfill_floor,
        first_observed_from=interval.first_observed_from,
        effective_available_time=interval.effective_available_time,
        binding=interval.binding,
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
    assumed: dict[str, AssumedMembership] = {}
    for venue_symbol, points in sorted(timelines.items()):
        mine: list[tuple[datetime | None, datetime | None]] = []
        for span in _spans(points, pit):
            listing = span.point.listing
            assert listing is not None and span.point.lineage is not None
            revision = listing.revision.revision_id
            episode: ListingEpisodeKey = listing.episode
            membership = _assumed_of(venue_symbol, span, pit)
            if membership is not None:
                if venue_symbol in assumed:  # pragma: no cover - the assumed window is one span
                    raise CatalogIntegrityError(f"{venue_symbol} has two assumed member spans")
                assumed[venue_symbol] = membership
            if span.point.tradable:
                members.append(
                    UniverseMember(
                        episode=episode,
                        listing_revision_id=revision,
                        effective_from=span.start,
                        effective_until=span.end,
                        assumption=None if membership is None else membership.binding,
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
        assumed=assumed,
    )


# ==========================================================================================
# v3: bounded cursor / generator entry (ADR-0077 §6.1.1)
#
# ``build()`` / ``UniverseBuilt`` above are untouched and remain the v2 legacy, fully
# materialized replay path. Everything below is additive: a new ``UniverseBuilder.cursor()``
# entry that produces members, exclusions, listing lineage, member spans and evidence gaps as
# explicitly-closed, ordered iterators, without ever holding v2's full ``timelines`` dict,
# ``members`` / ``exclusions`` lists, ``lineage`` / ``gaps`` dicts or ``member_spans`` mapping
# (ADR-0077 §6.1 item 1 / item 4). Every accepted change event is written to a content-addressed
# bounded run set; only its fixed row buffer and fanout-bounded index levels remain resident. A
# sorted root is replayed for each symbol and adjacent duplicate timestamps are removed as they
# stream.
# ==========================================================================================


@dataclass(frozen=True, slots=True)
class _InstantReplay:
    storage: StorageAdapter
    start: datetime
    root: RunRef | None

    @contextmanager
    def open(self) -> Iterator[Iterator[datetime]]:
        """Open one independently closable, sorted, adjacent-deduplicated pass."""
        if self.root is None:
            yield iter((self.start,))
            return
        with iter_run(self.storage, self.root) as rows:

            def points() -> Generator[datetime]:
                yield self.start
                previous: datetime | None = None
                for row in rows:
                    instant = row["instant"]
                    if instant != previous:
                        yield instant
                    previous = instant

            iterator = points()
            try:
                yield iterator
            finally:
                iterator.close()


def _instants_v3(
    view: PinnedCatalogView,
    pit: PointInTimeSpec,
    storage: StorageAdapter,
    run_params: UniverseRunParams,
) -> _InstantReplay:
    """Spill cutoff-visible in-window change events to a bounded sorted run-set root."""
    if pit.simulation_time is not None:
        return _InstantReplay(storage, pit.simulation_time, None)
    start, end = pit.simulation_start, pit.simulation_end
    if start is None or end is None:  # pragma: no cover - the contract forbids it
        raise UniverseSpecError("the PIT spec has neither a simulation time nor an interval")
    _validate_run_params(run_params)
    run_set = RunSetBuilder(
        storage,
        key=lambda row: row["instant"],
        capacity=run_params.capacity,
        merge_fanout=run_params.merge_fanout,
        limits=run_params.limits,
    )
    events = _change_events(view, pit.knowledge_cutoff, start, end)
    try:
        with run_set:
            for instant in events:
                instant = instant.astimezone(UTC)
                if start < instant < end:
                    run_set.add({"instant": instant})
            root = run_set.finish()
    finally:
        events.close()
    return _InstantReplay(storage, start, root)


def _validate_run_params(params: UniverseRunParams) -> None:
    if not isinstance(params, UniverseRunParams):
        raise UniverseSpecError("run_params must be UniverseRunParams")
    if (
        isinstance(params.capacity, bool)
        or not isinstance(params.capacity, int)
        or params.capacity < 1
    ):
        raise UniverseSpecError("run_params.capacity must be an integer >= 1")
    if (
        isinstance(params.merge_fanout, bool)
        or not isinstance(params.merge_fanout, int)
        or params.merge_fanout < 2
    ):
        raise UniverseSpecError("run_params.merge_fanout must be an integer >= 2")
    if not isinstance(params.limits, RunLimits):
        raise UniverseSpecError("run_params.limits must be RunLimits")


def _close_reader(reader: object) -> None:
    close = getattr(reader, "close", None)
    if callable(close):
        close()


def _change_events(
    view: PinnedCatalogView, cutoff: datetime, start: datetime, end: datetime
) -> Generator[datetime]:
    """Yield only in-window, cutoff-visible events from bounded batches."""

    def in_window(instant: datetime) -> bool:
        return start < instant < end

    reader = view.scan_column_batches(
        EXCHANGE_INFO_TABLE, columns=("retrieved_at", "knowledge_time")
    )
    try:
        for record_batch in reader:
            for row in record_batch.to_pylist():
                if row["knowledge_time"] <= cutoff:
                    instant = row["retrieved_at"]
                    if in_window(instant):
                        yield instant
    finally:
        _close_reader(reader)

    reader = view.scan_column_batches(
        LISTINGS_TABLE, columns=("available_time", "knowledge_time", "tradable_intervals")
    )
    try:
        for record_batch in reader:
            for row in record_batch.to_pylist():
                if row["knowledge_time"] <= cutoff:
                    instant = row["available_time"]
                    if in_window(instant):
                        yield instant
                    for interval in row["tradable_intervals"]:
                        instant = interval["tradable_from"]
                        if in_window(instant):
                            yield instant
                        instant = interval["tradable_until"]
                        if instant is not None and in_window(instant):
                            yield instant
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
        point = deriver.listing_at(venue_symbol, instant, pit.knowledge_cutoff, pit=pit)
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
    branch). ``lineage`` / ``gap`` are candidates for every finalized span. Their projections
    externally sort and adjacent-dedupe records, failing closed if a revision maps to conflicting
    evidence.
    ``assumed`` is set only for a member span that exists under the ADR-0051 assumption.
    """

    venue_symbol: str
    member: UniverseMember | None
    exclusion: UniverseExclusion | None
    listing_revision_id: str
    lineage: SelectedRevisionLineage
    gap: str | None
    assumed: AssumedMembership | None = None


def _events_v3(
    adapter: RevisionCatalog,
    storage: StorageAdapter,
    origin: str,
    spec: UniverseSelectionSpec,
    pit: PointInTimeSpec,
    run_params: UniverseRunParams,
) -> Generator[_SpanEvent]:
    """The bounded walk behind every :class:`UniverseSpanCursor` view (ADR-0077 §6.1.1).

    One ``PinnedCatalogView`` + one ``ListingDeriver`` for the whole walk, released on
    exhaustion, an early ``.close()`` (thrown in as ``GeneratorExit`` at the current ``yield``)
    or an exception -- the same ``try/finally`` shape as v2's ``build()``. ``spec.symbols`` is
    already canonically sorted (``UniverseSelectionSpec._canonical_symbols``); each symbol is
    walked in full, one span at a time, before the next. No ``timelines``, ``members``,
    ``exclusions`` or ``member_spans`` collection spans more than the current symbol: the
    per-symbol state contains only the current point/span. The window-filtered event stream is
    sorted into one content-addressed run-set root and replayed for each symbol. Lineage and gap
    projections use their own run sets to sort by revision and collapse adjacent duplicates.
    """
    view = PinnedCatalogView(adapter, pit.snapshot_bindings)
    deriver = ListingDeriver(view, storage, market_data_base_url=origin)
    try:
        instants = _instants_v3(view, pit, storage, run_params)
        for venue_symbol in spec.symbols:
            assumed_seen = False
            with instants.open() as instant_stream:
                points = _timeline_stream(deriver, venue_symbol, instant_stream, pit)
                for span in _spans_stream(points, pit):
                    listing = span.point.listing
                    assert listing is not None and span.point.lineage is not None
                    revision = listing.revision.revision_id
                    episode = listing.episode
                    membership = _assumed_of(venue_symbol, span, pit)
                    if membership is not None:
                        if assumed_seen:  # pragma: no cover - the assumed window is one span
                            raise CatalogIntegrityError(
                                f"{venue_symbol} has two assumed member spans"
                            )
                        assumed_seen = True
                    member: UniverseMember | None = None
                    exclusion: UniverseExclusion | None = None
                    if span.point.tradable:
                        member = UniverseMember(
                            episode=episode,
                            listing_revision_id=revision,
                            effective_from=span.start,
                            effective_until=span.end,
                            assumption=None if membership is None else membership.binding,
                        )
                    else:
                        exclusion = UniverseExclusion(
                            episode=episode,
                            listing_revision_id=revision,
                            reason=ExclusionReason.NOT_TRADABLE,
                            effective_from=span.start,
                            effective_until=span.end,
                        )
                    yield _SpanEvent(
                        venue_symbol=venue_symbol,
                        member=member,
                        exclusion=exclusion,
                        listing_revision_id=revision,
                        lineage=span.point.lineage,
                        gap=span.point.evidence_gap,
                        assumed=membership,
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


def _gap_candidate_of(event: _SpanEvent) -> tuple[str, str | None]:
    """Include gap absence so repeated-revision disagreement is still detected during merge."""
    return (event.listing_revision_id, event.gap)


def _assumed_membership_of(event: _SpanEvent) -> AssumedMembership | None:
    return event.assumed


@contextmanager
def _project[T](
    events: Generator[_SpanEvent], extract: Callable[[_SpanEvent], T | None]
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


@contextmanager
def _unique_projection[T](
    events: Generator[_SpanEvent],
    extract: Callable[[_SpanEvent], T | None],
    *,
    revision_key: Callable[[T], str],
    encode: Callable[[T], Mapping[str, Any]],
    decode: Callable[[Mapping[str, Any]], T],
    storage: StorageAdapter,
    run_params: UniverseRunParams,
) -> Iterator[Iterator[T]]:
    """Sort projection candidates by revision and first ordinal, then collapse adjacent copies.

    Every candidate is content-addressed in a bounded run set. Repeated revisions must carry
    identical evidence; conflicts fail closed. The first ordinal selects the historical first
    occurrence deterministically without retaining a revision-sized in-memory index.
    """
    run_set = RunSetBuilder(
        storage,
        key=lambda row: (row["revision"], row["ordinal"]),
        capacity=run_params.capacity,
        merge_fanout=run_params.merge_fanout,
        limits=run_params.limits,
    )
    ordinal = 0
    try:
        with run_set:
            for event in events:
                value = extract(event)
                if value is None:
                    continue
                run_set.add(
                    {
                        "revision": revision_key(value),
                        "ordinal": ordinal,
                        "record": dict(encode(value)),
                    }
                )
                ordinal += 1
            root = run_set.finish()
        if root is None:
            yield iter(())
            return
        with iter_run(storage, root) as rows:

            def unique() -> Generator[T]:
                previous_revision: str | None = None
                previous_record: Mapping[str, Any] | None = None
                for row in rows:
                    revision = row["revision"]
                    record = row["record"]
                    if revision == previous_revision:
                        if record != previous_record:
                            raise CatalogIntegrityError(
                                f"listing revision {revision} has conflicting projected evidence"
                            )
                        continue
                    previous_revision = revision
                    previous_record = record
                    yield decode(record)

            iterator = unique()
            try:
                yield iterator
            finally:
                iterator.close()
    finally:
        events.close()


@contextmanager
def _evidence_gap_projection(
    events: Generator[_SpanEvent],
    *,
    storage: StorageAdapter,
    run_params: UniverseRunParams,
) -> Iterator[Iterator[tuple[str, str]]]:
    """Dedupe both gap-bearing and gap-free revisions, then expose only actual gaps."""
    with _unique_projection(
        events,
        _gap_candidate_of,
        revision_key=lambda item: item[0],
        encode=lambda item: {"revision_id": item[0], "gap": item[1]},
        decode=lambda item: (item["revision_id"], item["gap"]),
        storage=storage,
        run_params=run_params,
    ) as candidates:

        def gaps() -> Generator[tuple[str, str]]:
            for revision, gap in candidates:
                if gap is not None:
                    yield revision, gap

        iterator = gaps()
        try:
            yield iterator
        finally:
            iterator.close()


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
    stream methods below (and ``assumed``, ADR-0051 §3, which B1 does not consume) has the
    signature ``() -> AbstractContextManager[Iterator[T]]`` for its stated ``T``; a caller does
    ``with cursor.members() as members:\\n    for member in members: ...``.
    No method takes the ``spec`` / ``pit`` again -- both are fixed at cursor construction
    (:meth:`UniverseBuilder.cursor`), along with the required explicit ``UniverseRunParams``.

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
        run_params: UniverseRunParams,
    ) -> None:
        _check_spec(spec)
        check_listing_bindings(pit)
        _validate_run_params(run_params)
        self._adapter = adapter
        self._storage = storage
        self._origin = origin
        self._spec = spec
        self._pit = pit
        self._run_params = run_params

    def _events(self) -> Generator[_SpanEvent]:
        return _events_v3(
            self._adapter,
            self._storage,
            self._origin,
            self._spec,
            self._pit,
            self._run_params,
        )

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
        using the first generated candidate for each revision: the same revision selected again
        later by the same symbol's timeline (e.g. a halt/resume returning to it) is not repeated.
        Results are sorted by revision id. A revision cannot in fact
        belong to two symbols (ADR-0024 episode scoping; ``ListingHistory`` enforces one
        ``observation_key`` per ``revision_id``), so this per-symbol de-duplication is also the
        walk's full de-duplication -- no cross-symbol working set is needed. The dataset's other
        lineage hops (raw / source) are B1's responsibility, not this cursor's.
        """
        return _unique_projection(
            self._events(),
            _lineage_of,
            revision_key=lambda item: item.canonical_revision_id,
            encode=lambda item: item.model_dump(mode="json"),
            decode=SelectedRevisionLineage.model_validate,
            storage=self._storage,
            run_params=self._run_params,
        )

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
        return _evidence_gap_projection(
            self._events(),
            storage=self._storage,
            run_params=self._run_params,
        )

    def assumed(self) -> AbstractContextManager[Iterator[AssumedMembership]]:
        """The v3 form of ``UniverseBuilt.assumed`` (ADR-0051 §3): each member span that exists
        only under the listing backfill assumption, in generation order (at most one per venue
        symbol); empty when the PIT spec does not bind it."""
        return _project(self._events(), _assumed_membership_of)

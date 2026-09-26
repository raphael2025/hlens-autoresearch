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

from collections.abc import Mapping
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

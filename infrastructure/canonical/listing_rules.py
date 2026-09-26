"""Listing derivation rules of ADR-0029 §2 (Phase 1 E2) — pure functions, no I/O, no clock.

Two versioned identifiers (``03-data.md`` §7.3), each a spec whose canonical-JSON SHA-256 is its
hash (derived, never written down):

- ``binance.spot.listing-status@1.0.0`` (parser role: the derivation) — one exchangeInfo snapshot
  observation of one venue symbol → at most one ``canonical.instrument_listings`` revision:

  * status mapping: ``TRADING`` → ``listed``; ``HALT`` / ``BREAK`` / ``END_OF_DAY`` /
    ``CANCEL_ONLY`` → ``suspended`` (closes the open interval **at the observation instant**);
    any other value, a requested symbol absent from the snapshot, or base / quote assets other
    than the frozen first-slice ones → **unresolved**: no inference, a finding, and the episode's
    universe fails closed until the next resolved observation. ``delisted`` is never produced;
  * observed-from: ``tradable_from`` is the ``retrieved_at`` of the first snapshot observing
    ``TRADING``; the episode key is the ADR-0024 degraded key
    ``(binance, spot, <canonical symbol>, tradable_from)`` (no stable product id, L4); the
    ``status_reason`` states the observation lower bound;
  * a new revision **only** when the mapped status changes against the previous resolved
    observation; an unchanged status only exists in Raw;
  * identity: ``source_id = <policy>@<version>|raw.binance_spot_exchange_info|<snapshot revision>``
    (lineage is identity), payload document ``hlens.canonical.listing/1``, ``lrev1-`` ids;
- ``binance.spot.listing-observation@1.0.0`` (precedence) — within one episode of this source a
  revision supersedes the previous revision of the derived chain, whose observation
  (``retrieved_at``) is strictly earlier. Evidence: both instants and both snapshot revision ids.
  Two different snapshots observed at the same instant cannot be ordered: the chain stops there
  (fail closed). ``arrival_seq``, clocks and hashes are never evidence.

The chain is a pure function of the **set** of observations: arrival order changes nothing.
Every builder goes through the contracts (``ListingRevision`` / ``RevisionRecord`` /
``PrecedenceEvidence`` / ``RevisionGraph``), so whatever they refuse is never written.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import groupby
from typing import Any, Final

from core.contracts.revision import (
    AvailabilityDecision,
    ObservationTimes,
    PolicyBinding,
    PolicyRole,
    PrecedenceEvidence,
    RevisionGraph,
    RevisionRecord,
)
from core.contracts.universe import (
    DegradedEpisodeKey,
    EpisodeIdentityBasis,
    ListingRevision,
    ListingStatus,
    TradableInterval,
)
from core.domain.base import canonical_json
from core.domain.specs import Instrument, InstrumentType
from infrastructure import contract_version
from infrastructure.canonical.rules import SYMBOLS
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_EXCHANGE_INFO,
    CANONICAL_INSTRUMENT_LISTINGS,
)
from infrastructure.contract_version import PHASE1_PUBLICATION_VERSION
from infrastructure.revision.exchange_info_availability import (
    ExchangeInfoAvailabilitySubject,
    decide_exchange_info_availability,
)
from infrastructure.revision.row_integrity import batch, batch_rows

__all__ = [
    "FIRST_SLICE_ASSETS",
    "FINDING_INSTRUMENT_MISMATCH",
    "FINDING_OBSERVATION_TIE",
    "FINDING_STATUS_UNKNOWN",
    "FINDING_SUSPENDED_BEFORE_TRADING",
    "FINDING_SYMBOL_MISSING",
    "LISTED_STATUSES",
    "LISTING_OBSERVATION_BINDING",
    "LISTING_OBSERVATION_HASH",
    "LISTING_OBSERVATION_ID",
    "LISTING_OBSERVATION_SPEC",
    "LISTING_OBSERVATION_VERSION",
    "LISTING_STATUS_BINDING",
    "LISTING_STATUS_HASH",
    "LISTING_STATUS_ID",
    "LISTING_STATUS_SPEC",
    "LISTING_STATUS_VERSION",
    "SUSPENDED_STATUSES",
    "ListingChain",
    "ListingFinding",
    "ListingRuleViolation",
    "Observation",
    "ObservationClass",
    "PlannedListing",
    "batch_id_for",
    "classify",
    "derive_chain",
    "listing_columns",
    "listing_record_from_row",
    "listing_revision_from_row",
    "observations_from_rows",
    "parse_batch_id",
]

LISTING_STATUS_ID: Final = "binance.spot.listing-status"
LISTING_STATUS_VERSION: Final = "1.0.0"
LISTING_OBSERVATION_ID: Final = "binance.spot.listing-observation"
LISTING_OBSERVATION_VERSION: Final = "1.0.0"

LISTED_STATUSES: Final[frozenset[str]] = frozenset({"TRADING"})
SUSPENDED_STATUSES: Final[frozenset[str]] = frozenset(
    {"HALT", "BREAK", "END_OF_DAY", "CANCEL_ONLY"}
)
#: venue symbol → (base, quote) frozen with this policy version (ADR-0022 §1 first slice).
FIRST_SLICE_ASSETS: Final[Mapping[str, tuple[str, str]]] = {
    "BTCUSDT": ("BTC", "USDT"),
    "ETHUSDT": ("ETH", "USDT"),
}

FINDING_STATUS_UNKNOWN: Final = "listing_status_unknown"
FINDING_SYMBOL_MISSING: Final = "listing_symbol_missing"
FINDING_INSTRUMENT_MISMATCH: Final = "listing_instrument_mismatch"
FINDING_OBSERVATION_TIE: Final = "listing_observation_tie"
FINDING_SUSPENDED_BEFORE_TRADING: Final = "listing_suspended_before_first_trading"

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MICRO: Final = timedelta(microseconds=1)
_RAW_TABLE: Final = BINANCE_SPOT_EXCHANGE_INFO.table
_LISTINGS: Final = CANONICAL_INSTRUMENT_LISTINGS
_VENUE: Final = "binance"


class ListingRuleViolation(ValueError):
    """The inputs cannot lawfully become a listing revision (fail closed)."""


def _digest(document: Any) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def _micros(value: datetime) -> int:
    return (value - _EPOCH) // _MICRO


for _venue_symbol, (_base, _quote) in FIRST_SLICE_ASSETS.items():
    _instrument = SYMBOLS[_venue_symbol]
    if (_instrument.symbol, _instrument.venue, _instrument.instrument_type) != (
        f"{_base}-{_quote}",
        _VENUE,
        "spot",
    ):  # pragma: no cover - a frozen-table mismatch is a code defect
        raise ListingRuleViolation(f"{_venue_symbol}: listing assets disagree with the normalizer")


# =========================================================================================
# the two specs
# =========================================================================================

LISTING_STATUS_SPEC: Final[dict[str, Any]] = {
    "policy_id": LISTING_STATUS_ID,
    "version": LISTING_STATUS_VERSION,
    "role": PolicyRole.PARSER.value,
    "adr": "ADR-0029 §2",
    "input": f"proven {_RAW_TABLE} snapshot revisions (source binance.public.spot.exchange-info)",
    "output": _LISTINGS.table,
    "status_mapping": {
        "listed": sorted(LISTED_STATUSES),
        "suspended": sorted(SUSPENDED_STATUSES),
        "delisted": "never produced (no official delisting semantics)",
        "unresolved": [
            "any other status value",
            "a requested symbol absent from the snapshot",
            "baseAsset / quoteAsset other than the frozen first-slice assets",
        ],
        "unresolved_effect": "no inference, a finding, universe fails closed until the next "
        "resolved observation",
    },
    "first_slice_assets": {symbol: list(pair) for symbol, pair in FIRST_SLICE_ASSETS.items()},
    "episode": {
        "key": "ADR-0024 degraded key (venue, instrument_type, canonical symbol, tradable_from)",
        "instrument": "hlens.canonical.binance-spot.normalizer@1.0.0 symbol map",
        "tradable_from": "retrieved_at of the first snapshot observing TRADING (observed-from)",
        "one_episode_per_symbol": True,
    },
    "intervals": {
        "suspension": "closes the open interval at the suspending snapshot's retrieved_at",
        "relisting": "opens a new interval at the relisting snapshot's retrieved_at",
    },
    "new_revision": "only when the mapped status changes against the previous resolved "
    "observation; unchanged statuses exist only in Raw",
    "status_reason": "states the observed-from lower bound and the observing snapshot",
    "identity": {
        "source_id": f"<policy>@<version>|{_RAW_TABLE}|<snapshot revision id>",
        "payload": "hlens.canonical.listing/1: episode, instrument, intervals (epoch us), status, "
        "source_status, status_reason, renamed_from",
        "revision_id": "lrev1-<sha256 of {rule, rule_version, rule_hash, observation_key, "
        "source_id, payload_hash}>",
    },
    "lineage": f"raw and source hop both {_RAW_TABLE} / the observing snapshot revision",
    "availability": "binance.spot.exchange-info-publication@1.0.0 (listing_observation)",
    "precedence": f"{LISTING_OBSERVATION_ID}@{LISTING_OBSERVATION_VERSION}",
    "batch": {
        "one_batch_per_derivation": True,
        "batch_id": "<policy>@<version>.from.<raw snapshot id the derivation read>",
        "rows": "the derived revisions not yet committed, symbols sorted, chain order",
        "arrival_seq": "largest committed + 1, consecutive",
        "knowledge_time": "one clock reading, never before any Raw knowledge_time read",
    },
}
LISTING_STATUS_HASH: Final = _digest(LISTING_STATUS_SPEC)
LISTING_STATUS_BINDING: Final = PolicyBinding(
    schema_version=PHASE1_PUBLICATION_VERSION,
    role=PolicyRole.PARSER,
    policy_id=LISTING_STATUS_ID,
    version=LISTING_STATUS_VERSION,
    policy_hash=LISTING_STATUS_HASH,
)

LISTING_OBSERVATION_SPEC: Final[dict[str, Any]] = {
    "policy_id": LISTING_OBSERVATION_ID,
    "version": LISTING_OBSERVATION_VERSION,
    "role": PolicyRole.PRECEDENCE.value,
    "adr": "ADR-0029 §2",
    "scope": "listing revisions of one episode derived from one source "
    "(binance.public.spot.exchange-info@1.0.0)",
    "rule": "a revision supersedes the previous revision of its derived chain; its observation "
    "(snapshot retrieved_at) is strictly later",
    "evidence": ["both observation instants (retrieved_at)", "both snapshot revision ids"],
    "ties": "two different snapshots at one instant are unordered: the chain stops (fail closed)",
    "never_evidence": ["arrival_seq", "ingest order", "local wall clock", "serverTime", "hashes"],
    "edge_knowledge_time": "the superseding revision's knowledge_time",
}
LISTING_OBSERVATION_HASH: Final = _digest(LISTING_OBSERVATION_SPEC)
LISTING_OBSERVATION_BINDING: Final = PolicyBinding(
    schema_version=PHASE1_PUBLICATION_VERSION,
    role=PolicyRole.PRECEDENCE,
    policy_id=LISTING_OBSERVATION_ID,
    version=LISTING_OBSERVATION_VERSION,
    policy_hash=LISTING_OBSERVATION_HASH,
)

_POLICY: Final = f"{LISTING_STATUS_ID}@{LISTING_STATUS_VERSION}"
_BATCH_PREFIX: Final = f"{_POLICY}.from."


def batch_id_for(raw_snapshot_id: str) -> str:
    """The one listing batch derived from ``raw_snapshot_id`` of the snapshot table."""
    if not isinstance(raw_snapshot_id, str) or not raw_snapshot_id:
        raise ListingRuleViolation("a derivation needs the Raw snapshot id it read")
    return f"{_BATCH_PREFIX}{raw_snapshot_id}"


def parse_batch_id(batch_id: str | None) -> str | None:
    """The Raw snapshot id a listing batch id names, or ``None`` if it is not one."""
    if batch_id is None or not batch_id.startswith(_BATCH_PREFIX):
        return None
    raw = batch_id[len(_BATCH_PREFIX) :]
    return raw or None


# =========================================================================================
# observations
# =========================================================================================


class ObservationClass(StrEnum):
    LISTED = "listed"
    SUSPENDED = "suspended"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class Observation:
    """One proven snapshot's view of one venue symbol (``status is None`` = absent)."""

    venue_symbol: str
    snapshot_revision_id: str
    requested_at: datetime
    retrieved_at: datetime
    raw_knowledge_time: datetime
    status: str | None
    base_asset: str | None
    quote_asset: str | None


def observations_from_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[Observation]]:
    """Per first-slice venue symbol: every proven snapshot row's observation of it."""
    found: dict[str, list[Observation]] = {symbol: [] for symbol in FIRST_SLICE_ASSETS}
    for row in rows:
        present = {item["symbol"]: item for item in row["symbols"]}
        for symbol in row["requested_symbols"]:
            if symbol not in found:
                raise ListingRuleViolation(f"snapshot requests a symbol {symbol!r} out of scope")
            item = present.get(symbol)
            found[symbol].append(
                Observation(
                    venue_symbol=symbol,
                    snapshot_revision_id=row["revision_id"],
                    requested_at=row["requested_at"],
                    retrieved_at=row["retrieved_at"],
                    raw_knowledge_time=row["knowledge_time"],
                    status=None if item is None else item["status"],
                    base_asset=None if item is None else item["base_asset"],
                    quote_asset=None if item is None else item["quote_asset"],
                )
            )
    return found


def classify(observation: Observation) -> tuple[ObservationClass, str | None]:
    """The mapped class of one observation and, if unresolved, the finding code."""
    if observation.status is None:
        return ObservationClass.UNRESOLVED, FINDING_SYMBOL_MISSING
    expected = FIRST_SLICE_ASSETS.get(observation.venue_symbol)
    if expected is None or (observation.base_asset, observation.quote_asset) != expected:
        return ObservationClass.UNRESOLVED, FINDING_INSTRUMENT_MISMATCH
    if observation.status in LISTED_STATUSES:
        return ObservationClass.LISTED, None
    if observation.status in SUSPENDED_STATUSES:
        return ObservationClass.SUSPENDED, None
    return ObservationClass.UNRESOLVED, FINDING_STATUS_UNKNOWN


# =========================================================================================
# the chain
# =========================================================================================


@dataclass(frozen=True, slots=True)
class ListingFinding:
    """One internal, deterministic finding (persisting it is the quality writer's job)."""

    code: str
    venue_symbol: str
    observed_at: datetime
    snapshot_revision_ids: tuple[str, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class PlannedListing:
    """A derived listing revision without its ``arrival_seq`` / ``knowledge_time``."""

    venue_symbol: str
    episode: DegradedEpisodeKey
    instrument: Instrument
    intervals: tuple[TradableInterval, ...]
    status: ListingStatus
    source_status: str
    status_reason: str
    observation: Observation
    #: The superseded chain predecessor (``None`` for the first revision of the episode).
    previous: PlannedListing | None
    observation_key: str
    source_id: str
    payload_hash: str
    revision_id: str

    @property
    def supersedes(self) -> tuple[str, ...]:
        return () if self.previous is None else (self.previous.revision_id,)


@dataclass(frozen=True, slots=True)
class ListingChain:
    """The derived chain of one venue symbol, its findings and where it stopped (a tie)."""

    venue_symbol: str
    revisions: tuple[PlannedListing, ...]
    findings: tuple[ListingFinding, ...]
    stopped_at: datetime | None


def _source_id(snapshot_revision_id: str) -> str:
    if not isinstance(snapshot_revision_id, str) or not snapshot_revision_id:
        raise ListingRuleViolation("a listing revision needs its snapshot revision id")
    if "|" in snapshot_revision_id:
        raise ListingRuleViolation("a snapshot revision id must not contain '|'")
    return f"{_POLICY}|{_RAW_TABLE}|{snapshot_revision_id}"


def _payload_hash(
    episode: DegradedEpisodeKey,
    instrument: Instrument,
    intervals: Sequence[TradableInterval],
    status: ListingStatus,
    source_status: str,
    status_reason: str,
) -> str:
    document = {
        "kind": "hlens.canonical.listing/1",
        "episode": {
            "basis": episode.basis.value,
            "venue": episode.venue,
            "instrument_type": episode.instrument_type.value,
            "symbol": episode.symbol,
            "tradable_from_us": _micros(episode.tradable_from),
        },
        "instrument": {
            "venue": instrument.venue,
            "symbol": instrument.symbol,
            "instrument_type": instrument.instrument_type.value,
            "base": instrument.base,
            "quote": instrument.quote,
        },
        "tradable_intervals": [
            [
                _micros(item.tradable_from),
                None if item.tradable_until is None else _micros(item.tradable_until),
            ]
            for item in intervals
        ],
        "status": status.value,
        "source_status": source_status,
        "status_reason": status_reason,
        "renamed_from": None,
    }
    return _digest(document)


def _revision_id(observation_key: str, source_id: str, payload_hash: str) -> str:
    document = {
        "rule": LISTING_STATUS_ID,
        "rule_version": LISTING_STATUS_VERSION,
        "rule_hash": LISTING_STATUS_HASH,
        "observation_key": observation_key,
        "source_id": source_id,
        "payload_hash": payload_hash,
    }
    return f"lrev1-{_digest(document)}"


def _reason(kind: str, observation: Observation, tradable_from: datetime) -> str:
    at = observation.retrieved_at.isoformat()
    snapshot = observation.snapshot_revision_id
    bound = (
        f"tradable_from {tradable_from.isoformat()} is this installation's first observation of "
        "TRADING, an observed-from lower bound, not an exchange-declared listing time"
    )
    if kind == "first":
        return f"{_POLICY}: observed-from — TRADING observed at {at} (snapshot {snapshot}); {bound}"
    if kind == "suspended":
        return (
            f"{_POLICY}: observed-from — {observation.status} observed at {at} (snapshot "
            f"{snapshot}) closes the open interval at the observation instant; {bound}"
        )
    return (
        f"{_POLICY}: observed-from — TRADING observed again at {at} (snapshot {snapshot}) opens "
        f"a new interval at the observation instant; {bound}"
    )


def _plan(
    observation: Observation,
    episode: DegradedEpisodeKey,
    instrument: Instrument,
    intervals: tuple[TradableInterval, ...],
    status: ListingStatus,
    reason: str,
    previous: PlannedListing | None,
) -> PlannedListing:
    assert observation.status is not None
    key = episode.observation_key()
    source = _source_id(observation.snapshot_revision_id)
    payload = _payload_hash(episode, instrument, intervals, status, observation.status, reason)
    return PlannedListing(
        venue_symbol=observation.venue_symbol,
        episode=episode,
        instrument=instrument,
        intervals=intervals,
        status=status,
        source_status=observation.status,
        status_reason=reason,
        observation=observation,
        previous=previous,
        observation_key=key,
        source_id=source,
        payload_hash=payload,
        revision_id=_revision_id(key, source, payload),
    )


def _finding(code: str, observations: Sequence[Observation], detail: str) -> ListingFinding:
    return ListingFinding(
        code=code,
        venue_symbol=observations[0].venue_symbol,
        observed_at=observations[0].retrieved_at,
        snapshot_revision_ids=tuple(sorted(item.snapshot_revision_id for item in observations)),
        detail=detail,
    )


def derive_chain(venue_symbol: str, observations: Iterable[Observation]) -> ListingChain:
    """The listing chain of ``venue_symbol`` from a **set** of observations (order-free)."""
    if venue_symbol not in FIRST_SLICE_ASSETS:
        raise ListingRuleViolation(f"{venue_symbol!r} is not a first-slice venue symbol")
    items = list(observations)
    ids = [item.snapshot_revision_id for item in items]
    if len(set(ids)) != len(ids):
        raise ListingRuleViolation("one snapshot observes a symbol at most once")
    if any(item.venue_symbol != venue_symbol for item in items):
        raise ListingRuleViolation("every observation must be of the chain's symbol")
    base, quote = FIRST_SLICE_ASSETS[venue_symbol]
    canonical = SYMBOLS[venue_symbol].symbol
    instrument = Instrument(
        venue=_VENUE,
        symbol=canonical,
        instrument_type=InstrumentType.SPOT,
        base=base,
        quote=quote,
    )
    revisions: list[PlannedListing] = []
    findings: list[ListingFinding] = []
    stopped_at: datetime | None = None
    current: PlannedListing | None = None
    ordered = sorted(items, key=lambda item: item.retrieved_at)
    for instant, grouped in groupby(ordered, key=lambda item: item.retrieved_at):
        group = list(grouped)
        if len(group) > 1:
            findings.append(
                _finding(
                    FINDING_OBSERVATION_TIE,
                    group,
                    f"{len(group)} snapshots observed {venue_symbol} at {instant.isoformat()}: "
                    f"{LISTING_OBSERVATION_ID}@{LISTING_OBSERVATION_VERSION} cannot order them; "
                    "the chain stops here (fail closed)",
                )
            )
            stopped_at = instant
            break
        observation = group[0]
        kind, code = classify(observation)
        if kind is ObservationClass.UNRESOLVED:
            assert code is not None
            findings.append(
                _finding(
                    code,
                    group,
                    f"{venue_symbol} at {instant.isoformat()}: "
                    + {
                        FINDING_SYMBOL_MISSING: "absent from the snapshot",
                        FINDING_INSTRUMENT_MISMATCH: "base / quote assets are not the frozen ones",
                        FINDING_STATUS_UNKNOWN: f"status {observation.status!r} has no mapping",
                    }[code]
                    + "; no inference, universe fails closed until the next resolved observation",
                )
            )
            continue
        status = (
            ListingStatus.LISTED if kind is ObservationClass.LISTED else ListingStatus.SUSPENDED
        )
        if current is None:
            if status is ListingStatus.SUSPENDED:
                findings.append(
                    _finding(
                        FINDING_SUSPENDED_BEFORE_TRADING,
                        group,
                        f"{venue_symbol} observed {observation.status} at {instant.isoformat()} "
                        "before any TRADING observation: no episode exists yet",
                    )
                )
                continue
            episode = DegradedEpisodeKey(
                basis=EpisodeIdentityBasis.DEGRADED_SYMBOL_START,
                venue=_VENUE,
                instrument_type=InstrumentType.SPOT,
                symbol=canonical,
                tradable_from=instant,
            )
            intervals: tuple[TradableInterval, ...] = (
                TradableInterval(tradable_from=instant, tradable_until=None),
            )
            current = _plan(
                observation,
                episode,
                instrument,
                intervals,
                status,
                _reason("first", observation, instant),
                None,
            )
            revisions.append(current)
            continue
        if status is current.status:
            continue
        episode = current.episode
        if status is ListingStatus.SUSPENDED:
            last = current.intervals[-1]
            intervals = (
                *current.intervals[:-1],
                TradableInterval(tradable_from=last.tradable_from, tradable_until=instant),
            )
            reason = _reason("suspended", observation, episode.tradable_from)
        else:
            intervals = (
                *current.intervals,
                TradableInterval(tradable_from=instant, tradable_until=None),
            )
            reason = _reason("relisted", observation, episode.tradable_from)
        current = _plan(observation, episode, instrument, intervals, status, reason, current)
        revisions.append(current)
    return ListingChain(
        venue_symbol=venue_symbol,
        revisions=tuple(revisions),
        findings=tuple(findings),
        stopped_at=stopped_at,
    )


# =========================================================================================
# rows
# =========================================================================================


def _evidence(planned: PlannedListing, knowledge_time: datetime) -> PrecedenceEvidence:
    previous = planned.previous
    assert previous is not None
    return PrecedenceEvidence(
        observation_key=planned.observation_key,
        revision_id=planned.revision_id,
        superseded_revision_id=previous.revision_id,
        policy=LISTING_OBSERVATION_BINDING,
        evidence=(
            f"{LISTING_OBSERVATION_ID}@{LISTING_OBSERVATION_VERSION}: same source "
            f"binance.public.spot.exchange-info@1.0.0, same episode",
            f"observation {planned.observation.retrieved_at.isoformat()} of snapshot "
            f"{planned.observation.snapshot_revision_id} is strictly later than observation "
            f"{previous.observation.retrieved_at.isoformat()} of snapshot "
            f"{previous.observation.snapshot_revision_id}",
        ),
        knowledge_time=knowledge_time,
    )


def listing_columns(
    planned: PlannedListing,
    *,
    arrival_seq: int,
    knowledge_time: datetime,
    contract_schema_version: str | None = None,
) -> dict[str, Any]:
    """Every column of one ``canonical.instrument_listings`` revision (the single builder).

    ``contract_schema_version``: the version a committed batch records when it is re-derived,
    ``None`` for a new batch (the current version; ADR-0052 versioned replay, V1 / V2). It is
    the envelope of both the ``RevisionRecord`` and the ``ListingRevision`` built here, and the
    row's column is that envelope.
    """
    version = (
        contract_version.new_group_version()
        if contract_schema_version is None
        else contract_version.replay_version(contract_schema_version, what="a listing batch")
    )
    observation = planned.observation
    if planned.previous is not None and not (
        planned.previous.observation.retrieved_at < observation.retrieved_at
    ):
        raise ListingRuleViolation("a revision may only supersede a strictly earlier observation")
    if knowledge_time < observation.raw_knowledge_time:
        raise ListingRuleViolation("a listing revision cannot be known before its snapshot")
    try:
        decision = decide_exchange_info_availability(
            ExchangeInfoAvailabilitySubject.LISTING_OBSERVATION,
            requested_at=observation.requested_at,
            ingest_time=observation.retrieved_at,
            knowledge_time=knowledge_time,
        )
        record = RevisionRecord(
            schema_version=version,
            observation_key=planned.observation_key,
            revision_id=planned.revision_id,
            source_id=planned.source_id,
            payload_hash=planned.payload_hash,
            arrival_seq=arrival_seq,
            supersedes=planned.supersedes,
            availability=decision,
        )
        evidence = () if planned.previous is None else (_evidence(planned, knowledge_time),)
        RevisionGraph(revisions=(record,), precedence_evidence=evidence)
        listing = ListingRevision(
            schema_version=version,
            revision=record,
            episode=planned.episode,
            instrument=planned.instrument,
            tradable_intervals=planned.intervals,
            status=planned.status,
            source_status=planned.source_status,
            status_reason=planned.status_reason,
        )
    except ValueError as exc:
        raise ListingRuleViolation(f"no lawful listing revision: {exc}") from None
    times = decision.times
    episode = planned.episode
    row: dict[str, Any] = {
        "observation_key": record.observation_key,
        "revision_id": record.revision_id,
        "source_id": record.source_id,
        "payload_hash": record.payload_hash,
        "arrival_seq": record.arrival_seq,
        "supersedes": list(record.supersedes),
        "source_revision_id": None,
        "source_revision_time": None,
        "event_time": times.event_time,
        "event_end_time": times.event_end_time,
        "source_time": None,
        "available_time": times.available_time,
        "ingest_time": times.ingest_time,
        "knowledge_time": times.knowledge_time,
        "declared_latency_us": times.declared_latency // _MICRO,
        "availability_policy_id": decision.policy.policy_id,
        "availability_policy_version": decision.policy.version,
        "availability_policy_hash": decision.policy.policy_hash,
        "availability_evidence": list(decision.evidence),
        "availability_evidence_gap": decision.evidence_gap,
        "precedence_evidence": [
            {
                "superseded_revision_id": item.superseded_revision_id,
                "policy_id": item.policy.policy_id,
                "policy_version": item.policy.version,
                "policy_hash": item.policy.policy_hash,
                "evidence": list(item.evidence),
                "knowledge_time": item.knowledge_time,
            }
            for item in evidence
        ],
        "contract_schema_version": listing.schema_version,
        "episode_basis": episode.basis.value,
        "episode_venue": episode.venue,
        "episode_instrument_type": episode.instrument_type.value,
        "episode_venue_product_id": None,
        "episode_symbol": episode.symbol,
        "episode_tradable_from": episode.tradable_from,
        "venue": listing.instrument.venue,
        "symbol": listing.instrument.symbol,
        "instrument_type": listing.instrument.instrument_type.value,
        "base": listing.instrument.base,
        "quote": listing.instrument.quote,
        "tradable_intervals": [
            {"tradable_from": item.tradable_from, "tradable_until": item.tradable_until}
            for item in listing.tradable_intervals
        ],
        "status": listing.status.value,
        "source_status": listing.source_status,
        "status_reason": listing.status_reason,
        "renamed_from": None,
        "lineage_raw_table": _RAW_TABLE,
        "lineage_raw_revision_id": observation.snapshot_revision_id,
        "lineage_source_table": _RAW_TABLE,
        "lineage_source_revision_id": observation.snapshot_revision_id,
    }
    return dict(batch_rows(batch(_LISTINGS, [row]))[0])


def listing_record_from_row(
    row: Mapping[str, Any],
) -> tuple[RevisionRecord, tuple[PrecedenceEvidence, ...]]:
    """The ``RevisionRecord`` and in-row evidence of a (proven) listing row."""
    version = row["contract_schema_version"]
    times = ObservationTimes(
        schema_version=version,
        event_time=row["event_time"],
        event_end_time=row["event_end_time"],
        source_time=row["source_time"],
        available_time=row["available_time"],
        ingest_time=row["ingest_time"],
        knowledge_time=row["knowledge_time"],
        declared_latency=row["declared_latency_us"] * _MICRO,
    )
    record = RevisionRecord(
        schema_version=version,
        observation_key=row["observation_key"],
        revision_id=row["revision_id"],
        source_id=row["source_id"],
        payload_hash=row["payload_hash"],
        arrival_seq=row["arrival_seq"],
        supersedes=tuple(row["supersedes"]),
        source_revision_id=row["source_revision_id"],
        source_revision_time=row["source_revision_time"],
        availability=AvailabilityDecision(
            schema_version=version,
            times=times,
            policy=PolicyBinding(
                schema_version=version,
                role=PolicyRole.AVAILABILITY,
                policy_id=row["availability_policy_id"],
                version=row["availability_policy_version"],
                policy_hash=row["availability_policy_hash"],
            ),
            evidence=tuple(row["availability_evidence"]),
            evidence_gap=row["availability_evidence_gap"],
        ),
    )
    evidence = tuple(
        PrecedenceEvidence(
            schema_version=version,
            observation_key=record.observation_key,
            revision_id=record.revision_id,
            superseded_revision_id=item["superseded_revision_id"],
            policy=PolicyBinding(
                schema_version=version,
                role=PolicyRole.PRECEDENCE,
                policy_id=item["policy_id"],
                version=item["policy_version"],
                policy_hash=item["policy_hash"],
            ),
            evidence=tuple(item["evidence"]),
            knowledge_time=item["knowledge_time"],
        )
        for item in row["precedence_evidence"]
    )
    return record, evidence


def listing_revision_from_row(row: Mapping[str, Any]) -> ListingRevision:
    """The ``ListingRevision`` of a (proven) listing row."""
    record, _ = listing_record_from_row(row)
    version = row["contract_schema_version"]
    return ListingRevision(
        schema_version=version,
        revision=record,
        episode=DegradedEpisodeKey(
            schema_version=version,
            basis=row["episode_basis"],
            venue=row["episode_venue"],
            instrument_type=row["episode_instrument_type"],
            symbol=row["episode_symbol"],
            tradable_from=row["episode_tradable_from"],
        ),
        instrument=Instrument(
            schema_version=version,
            venue=row["venue"],
            symbol=row["symbol"],
            instrument_type=row["instrument_type"],
            base=row["base"],
            quote=row["quote"],
        ),
        tradable_intervals=tuple(
            TradableInterval(schema_version=version, **item) for item in row["tradable_intervals"]
        ),
        status=row["status"],
        source_status=row["source_status"],
        status_reason=row["status_reason"],
        renamed_from=None,
    )

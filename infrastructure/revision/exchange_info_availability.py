"""Availability policy ``binance.spot.exchange-info-publication@1.0.0`` (Phase 1 E2; ADR-0029).

The listing evidence (``docs/architecture/evidence/binance-spot-listing.md``) finds no publication
time, revision time, listing date or status-change time anywhere in the official sources (L1, L4):
``GET /api/v3/exchangeInfo`` is a **current** snapshot and its ``serverTime`` is the response
instant, not the time any status took effect. Every subject of this version therefore falls back
to ``available_time = ingest_time`` and records an explicit evidence gap (ADR-0023 §2). It is its
own ``policy_id``: the archive and REST policies are not touched.

Subjects and times:

- **exchange_info_snapshot** — the Raw source revision: the observation is the HTTP exchange
  ``[requested_at, ingest_time)`` (``event_time = requested_at``, ``event_end_time = ingest_time``,
  the last body byte);
- **listing_observation** — a ``canonical.instrument_listings`` revision derived from one snapshot:
  the same exchange and the snapshot's ``ingest_time``; ``knowledge_time`` is the derivation's own
  (never earlier than the snapshot's). Its gap states the ADR-0029 §2 "observed-from" boundary:
  every ``tradable_from`` and every status change is this installation's observation instant, a
  lower bound, not an exchange-declared time.

``source_time`` is always empty (``serverTime``, ``Date`` and friends are not source declarations of
any status) and ``declared_latency`` is always zero. Nothing here reads a clock.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from core.contracts.revision import (
    AvailabilityDecision,
    ObservationTimes,
    PolicyBinding,
    PolicyRole,
)
from core.domain.base import canonical_json
from infrastructure.contract_version import PHASE1_PUBLICATION_VERSION

__all__ = [
    "EXCHANGE_INFO_AVAILABILITY_BINDING",
    "EXCHANGE_INFO_AVAILABILITY_HASH",
    "EXCHANGE_INFO_AVAILABILITY_POLICY_ID",
    "EXCHANGE_INFO_AVAILABILITY_POLICY_VERSION",
    "EXCHANGE_INFO_AVAILABILITY_SPEC",
    "ExchangeInfoAvailabilitySubject",
    "ExchangeInfoAvailabilityViolation",
    "decide_exchange_info_availability",
    "exchange_info_gap_for",
]

EXCHANGE_INFO_AVAILABILITY_POLICY_ID: Final = "binance.spot.exchange-info-publication"
EXCHANGE_INFO_AVAILABILITY_POLICY_VERSION: Final = "1.0.0"

_GAP_PREFIX: Final = (
    f"{EXCHANGE_INFO_AVAILABILITY_POLICY_ID}@{EXCHANGE_INFO_AVAILABILITY_POLICY_VERSION}"
)


class ExchangeInfoAvailabilityViolation(ValueError):
    """The inputs cannot yield a lawful availability decision; nothing may be written."""


class ExchangeInfoAvailabilitySubject(StrEnum):
    """What the decision is about; each subject has exactly one rule in this version."""

    SNAPSHOT = "exchange_info_snapshot"
    LISTING_OBSERVATION = "listing_observation"


_GAPS: Final[dict[ExchangeInfoAvailabilitySubject, str]] = {
    ExchangeInfoAvailabilitySubject.SNAPSHOT: (
        f"{_GAP_PREFIX}:exchange_info_publication_time_not_stated — the official documentation "
        "gives no publication, revision or status-change time for exchangeInfo; serverTime is "
        "the response instant only (evidence L1, L4)"
    ),
    ExchangeInfoAvailabilitySubject.LISTING_OBSERVATION: (
        f"{_GAP_PREFIX}:listing_observed_from — tradable_from and every status change are this "
        "installation's exchangeInfo observation instants (observed-from lower bounds), not "
        "exchange-declared listing or status times; no official listing history exists "
        "(evidence L1 ~ L7)"
    ),
}


def exchange_info_gap_for(subject: ExchangeInfoAvailabilitySubject) -> str:
    """The stable evidence-gap token persisted for ``subject``."""
    return _GAPS[subject]


def _check_utc(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ExchangeInfoAvailabilityViolation(f"{label} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ExchangeInfoAvailabilityViolation(f"{label} must be UTC")
    return value


def decide_exchange_info_availability(
    subject: ExchangeInfoAvailabilitySubject,
    *,
    requested_at: datetime,
    ingest_time: datetime,
    knowledge_time: datetime,
) -> AvailabilityDecision:
    """Times and availability decision of one snapshot or listing revision.

    Always ``available_time = ingest_time`` with this subject's gap. A request that does not
    precede its last byte, or a knowledge time before the ingest time, fails closed.
    """
    if not isinstance(subject, ExchangeInfoAvailabilitySubject):
        raise ExchangeInfoAvailabilityViolation(
            "subject must be an ExchangeInfoAvailabilitySubject"
        )
    for label, value in (
        ("requested_at", requested_at),
        ("ingest_time", ingest_time),
        ("knowledge_time", knowledge_time),
    ):
        _check_utc(value, label)
    if requested_at >= ingest_time:
        raise ExchangeInfoAvailabilityViolation(
            "requested_at must precede ingest_time (a wall-clock regression fails closed)"
        )
    if knowledge_time < ingest_time:
        raise ExchangeInfoAvailabilityViolation("knowledge_time must not precede ingest_time")
    times = ObservationTimes(
        event_time=requested_at,
        event_end_time=ingest_time,
        source_time=None,
        available_time=ingest_time,
        ingest_time=ingest_time,
        knowledge_time=knowledge_time,
        declared_latency=timedelta(0),
    )
    return AvailabilityDecision(
        times=times,
        policy=EXCHANGE_INFO_AVAILABILITY_BINDING,
        evidence=(),
        evidence_gap=_GAPS[subject],
    )


#: The complete, versioned policy document; its canonical-JSON SHA-256 is the policy hash.
EXCHANGE_INFO_AVAILABILITY_SPEC: Final[dict[str, Any]] = {
    "policy_id": EXCHANGE_INFO_AVAILABILITY_POLICY_ID,
    "version": EXCHANGE_INFO_AVAILABILITY_POLICY_VERSION,
    "role": PolicyRole.AVAILABILITY.value,
    "adr": "ADR-0029",
    "scope": {
        "venue": "binance",
        "market": "spot",
        "source": "binance.public.spot.exchange-info@1.0.0",
        "subjects": [subject.value for subject in ExchangeInfoAvailabilitySubject],
    },
    "evidence_document": "docs/architecture/evidence/binance-spot-listing.md",
    "rules": [
        {"subject": subject.value, "kind": "ingest_fallback", "gap": _GAPS[subject]}
        for subject in ExchangeInfoAvailabilitySubject
    ],
    "times": {
        "exchange_info_snapshot": "event_time = requested_at; event_end_time = ingest_time",
        "listing_observation": "the observing snapshot's requested_at / ingest_time; "
        "knowledge_time = the derivation's own, never before the snapshot's",
    },
    "source_time": {
        "set_from": [],
        "rejected_inputs": ["serverTime", "http date", "http last-modified", "http etag"],
    },
    "declared_latency_microseconds": 0,
    "consequence": "no listing revision is visible to a simulation_time before its ingest_time; "
    "a universe before the first local observation cannot be built (fail closed)",
    "fail_closed": [
        "knowledge_time earlier than ingest_time",
        "requested_at not before ingest_time",
    ],
    "never_evidence": ["local wall clock", "serverTime", "http headers", "arrival_seq"],
}
EXCHANGE_INFO_AVAILABILITY_HASH: Final = hashlib.sha256(
    canonical_json(EXCHANGE_INFO_AVAILABILITY_SPEC).encode("utf-8")
).hexdigest()
EXCHANGE_INFO_AVAILABILITY_BINDING: Final = PolicyBinding(
    schema_version=PHASE1_PUBLICATION_VERSION,
    role=PolicyRole.AVAILABILITY,
    policy_id=EXCHANGE_INFO_AVAILABILITY_POLICY_ID,
    version=EXCHANGE_INFO_AVAILABILITY_POLICY_VERSION,
    policy_hash=EXCHANGE_INFO_AVAILABILITY_HASH,
)

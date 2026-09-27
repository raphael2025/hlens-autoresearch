"""Availability policy ``binance.spot.rest-publication@1.0.0`` (Phase 1 D3B; ADR-0027 §8).

The REST evidence (``docs/architecture/evidence/binance-spot-rest-market-data.md``) is the same
conclusion as the archive one: the official documentation states parameters, ordering, units and
rate limits, but **no** publication time for any response or element (evidence N1). So every
subject of this version falls back to ``available_time = ingest_time`` and records an explicit
evidence gap (ADR-0023 §2). It is a separate ``policy_id`` from ``binance.spot.publication`` so a
point-in-time spec can bind both (ADR-0027 §11 trap 2); the archive policy is not touched.

Times per subject (ADR-0027 §8):

- **response** — the observation is one HTTP exchange ``[requested_at, ingest_time)``:
  ``event_time = requested_at``, ``event_end_time = ingest_time`` (the last body byte);
- **agg_trade** — ``event_time = T``; **kline_1m** — ``[interval_start, interval_start + 1m)``;
  both inherit the delivering response's ``ingest_time`` and ``knowledge_time``.

``source_time`` is always empty (``Date`` / ``Last-Modified`` / ``ETag`` are not source
declarations, evidence N10) and ``declared_latency`` is always zero. Nothing here reads a clock.
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
    "REST_AVAILABILITY_BINDING",
    "REST_AVAILABILITY_HASH",
    "REST_AVAILABILITY_POLICY_ID",
    "REST_AVAILABILITY_POLICY_VERSION",
    "REST_AVAILABILITY_SPEC",
    "RestAvailabilitySubject",
    "RestAvailabilityViolation",
    "decide_rest_availability",
    "rest_gap_for",
]

REST_AVAILABILITY_POLICY_ID: Final = "binance.spot.rest-publication"
REST_AVAILABILITY_POLICY_VERSION: Final = "1.0.0"

_ONE_MINUTE: Final = timedelta(minutes=1)
_GAP_PREFIX: Final = f"{REST_AVAILABILITY_POLICY_ID}@{REST_AVAILABILITY_POLICY_VERSION}"


class RestAvailabilityViolation(ValueError):
    """The inputs cannot yield a lawful availability decision; nothing may be written."""


class RestAvailabilitySubject(StrEnum):
    """What the decision is about; each subject has exactly one rule in this version."""

    RESPONSE = "rest_response"
    AGG_TRADE = "agg_trade"
    KLINE_1M = "kline_1m"


_GAPS: Final[dict[RestAvailabilitySubject, str]] = {
    RestAvailabilitySubject.RESPONSE: (
        f"{_GAP_PREFIX}:rest_response_publication_time_not_stated — the official REST "
        "documentation gives no publication time, revision id or revision time for any response"
    ),
    RestAvailabilitySubject.AGG_TRADE: (
        f"{_GAP_PREFIX}:agg_trade_publication_bound_not_stated — the REST endpoint is served "
        "from a database with no documented bound between execution and availability"
    ),
    RestAvailabilitySubject.KLINE_1M: (
        f"{_GAP_PREFIX}:kline_1m_publication_bound_not_stated — no documented bound between the "
        "end of a 1m interval and the availability of its final candle over REST"
    ),
}


def rest_gap_for(subject: RestAvailabilitySubject) -> str:
    """The stable evidence-gap token persisted for ``subject``."""
    return _GAPS[subject]


def _check_utc(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RestAvailabilityViolation(f"{label} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise RestAvailabilityViolation(f"{label} must be UTC")
    return value


def decide_rest_availability(
    subject: RestAvailabilitySubject,
    *,
    event_time: datetime,
    ingest_time: datetime,
    knowledge_time: datetime,
    event_end_time: datetime | None = None,
) -> AvailabilityDecision:
    """The two-axis times and availability decision of one REST revision.

    Always ``available_time = ingest_time`` with this subject's evidence gap. Every input that
    would need a guess fails closed: a response whose exchange interval does not end at its
    ingest time, an element that claims to describe the future, a kline that is not exactly one
    minute, a knowledge time before the ingest time.
    """
    if not isinstance(subject, RestAvailabilitySubject):
        raise RestAvailabilityViolation("subject must be a RestAvailabilitySubject")
    for label, value in (
        ("event_time", event_time),
        ("ingest_time", ingest_time),
        ("knowledge_time", knowledge_time),
    ):
        _check_utc(value, label)
    if event_end_time is not None:
        _check_utc(event_end_time, "event_end_time")
    if knowledge_time < ingest_time:
        raise RestAvailabilityViolation("knowledge_time must not precede ingest_time")

    if subject is RestAvailabilitySubject.RESPONSE:
        if event_end_time != ingest_time:
            raise RestAvailabilityViolation(
                "a response observation is the exchange [requested_at, ingest_time): its "
                "event_end_time must equal ingest_time"
            )
        if event_time >= ingest_time:
            raise RestAvailabilityViolation(
                "requested_at must precede ingest_time (a wall-clock regression fails closed)"
            )
    elif subject is RestAvailabilitySubject.AGG_TRADE:
        if event_end_time is not None:
            raise RestAvailabilityViolation("an aggTrade is an instantaneous observation")
    elif event_end_time is None or event_end_time - event_time != _ONE_MINUTE:
        raise RestAvailabilityViolation("a 1m kline covers exactly [interval_start, +1 minute)")

    observable = event_time if event_end_time is None else event_end_time
    if ingest_time < observable:
        raise RestAvailabilityViolation(
            "ingest_time precedes the observable time: the payload claims to describe the future"
        )
    times = ObservationTimes(
        event_time=event_time,
        event_end_time=event_end_time,
        source_time=None,
        available_time=ingest_time,
        ingest_time=ingest_time,
        knowledge_time=knowledge_time,
        declared_latency=timedelta(0),
    )
    return AvailabilityDecision(
        times=times, policy=REST_AVAILABILITY_BINDING, evidence=(), evidence_gap=_GAPS[subject]
    )


#: The complete, versioned policy document; ``REST_AVAILABILITY_HASH`` is its JSON SHA-256.
REST_AVAILABILITY_SPEC: Final[dict[str, Any]] = {
    "policy_id": REST_AVAILABILITY_POLICY_ID,
    "version": REST_AVAILABILITY_POLICY_VERSION,
    "role": PolicyRole.AVAILABILITY.value,
    "scope": {
        "venue": "binance",
        "market": "spot",
        "source": "binance.public.spot.rest@1.0.0",
        "subjects": [subject.value for subject in RestAvailabilitySubject],
    },
    "evidence_document": "docs/architecture/evidence/binance-spot-rest-market-data.md",
    "rules": [
        {"subject": subject.value, "kind": "ingest_fallback", "gap": _GAPS[subject]}
        for subject in RestAvailabilitySubject
    ],
    "times": {
        "rest_response": "event_time = requested_at; event_end_time = ingest_time (last byte)",
        "agg_trade": "event_time = T in the declared unit; inherits the response axis times",
        "kline_1m": "[interval_start, interval_start + 1m); inherits the response axis times",
    },
    "source_time": {
        "set_from": [],
        "rejected_inputs": ["http date", "http last-modified", "http etag"],
    },
    "declared_latency_microseconds": 0,
    "fail_closed": [
        "knowledge_time earlier than ingest_time",
        "ingest_time earlier than the observable time",
        "a response whose event_end_time is not its ingest_time",
        "a response whose requested_at is not before its ingest_time",
        "a kline that is not exactly one minute",
    ],
    "never_evidence": ["local wall clock", "http headers", "arrival order", "arrival_seq"],
}
REST_AVAILABILITY_HASH: Final = hashlib.sha256(
    canonical_json(REST_AVAILABILITY_SPEC).encode("utf-8")
).hexdigest()
REST_AVAILABILITY_BINDING: Final = PolicyBinding(
    schema_version=PHASE1_PUBLICATION_VERSION,
    role=PolicyRole.AVAILABILITY,
    policy_id=REST_AVAILABILITY_POLICY_ID,
    version=REST_AVAILABILITY_POLICY_VERSION,
    policy_hash=REST_AVAILABILITY_HASH,
)

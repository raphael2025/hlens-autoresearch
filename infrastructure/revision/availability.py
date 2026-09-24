"""Availability policy ``binance.spot.publication@1.0.0`` (Phase 1 D2; ADR-0023 §2).

The historical axis answers one question: *from when on could a strategy running at that time
have used this observation?* ADR-0023 §2 allows an ``available_time`` earlier than the local
``ingest_time`` **only** when a versioned policy can point at source evidence for it; otherwise
the policy must fall back to ``available_time = ingest_time`` and persist a structured evidence
gap. 03-data.md §7.3 repeats it: "标识符冻结 ≠ 数据可信".

The audited evidence for this version is ``docs/architecture/evidence/binance-spot-publication.md``.
Its outcome for 1.0.0, from official Binance sources only:

- the official repository states that daily archives become "available the next day" and that
  "Archived files may be updated at a later date as a result of recently discovered issues" —
  a cadence and a revision *possibility*, not the publication time of a specific revision;
- the official spot WebSocket documentation states "Update Speed: Real-time" for aggregate trade
  streams — a qualitative claim with no bound between trade execution and publication;
- the same document states "Update Speed: 1000ms for 1s, 2000ms for the other intervals" for
  kline streams — the push cadence of a stream, not a documented bound between the end of an
  interval and the publication of its final (``x = true``) candle.

None of these proves the publication *time* of a concrete revision, so every rule in 1.0.0
resolves to the conservative fallback and every revision carries an evidence gap. The mechanism
for an evidence-bearing rule (``OBSERVABLE_PLUS_BOUND``) is implemented and tested, so a future
policy version that can cite a documented upper bound only has to add a rule — it must never
relax this one (ADR-0023 §2, H3).

``source_time`` stays empty: an HTTP ``Last-Modified`` or ``ETag`` on a CDN object is not a
source declaration of a revision's publication time (ADR-0023 §1 "没有就为空，不得伪造"). The raw
headers are still persisted in ``raw.binance_spot_archives.source_metadata`` for later audit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
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

__all__ = [
    "AVAILABILITY_BINDING",
    "AVAILABILITY_HASH",
    "AVAILABILITY_POLICY_ID",
    "AVAILABILITY_POLICY_VERSION",
    "AVAILABILITY_SPEC",
    "AvailabilityRule",
    "AvailabilityRuleKind",
    "AvailabilitySubject",
    "AvailabilityViolation",
    "decide_availability",
    "rule_for",
]

AVAILABILITY_POLICY_ID: Final = "binance.spot.publication"
AVAILABILITY_POLICY_VERSION: Final = "1.0.0"


class AvailabilityViolation(ValueError):
    """The inputs cannot yield a lawful availability decision; the revision is not written."""


class AvailabilitySubject(StrEnum):
    """What the decision is about; each subject has exactly one rule per policy version."""

    ARCHIVE = "archive_revision"
    AGG_TRADE = "agg_trade"
    KLINE_1M = "kline_1m"


class AvailabilityRuleKind(StrEnum):
    """How ``available_time`` is computed."""

    #: No source evidence bounds the publication time: ``available_time = ingest_time`` + gap.
    INGEST_FALLBACK = "ingest_fallback"
    #: Evidence-bearing: ``available_time = max(observable_time, source_time) + bound``.
    OBSERVABLE_PLUS_BOUND = "observable_plus_bound"


@dataclass(frozen=True, slots=True)
class AvailabilityRule:
    """One subject's rule inside a policy version.

    ``evidence`` items are short claims with their official source; ``gap`` is the stable token
    persisted in ``availability_evidence_gap`` when the rule falls back. Exactly one of the two
    is used, matching ``AvailabilityDecision``'s "evidence xor gap".
    """

    subject: AvailabilitySubject
    kind: AvailabilityRuleKind
    gap: str | None = None
    evidence: tuple[str, ...] = ()
    bound: timedelta | None = None

    def __post_init__(self) -> None:
        if self.kind is AvailabilityRuleKind.INGEST_FALLBACK:
            if not self.gap or self.evidence or self.bound is not None:
                raise AvailabilityViolation("a fallback rule carries exactly a gap token")
        else:
            if self.gap or not self.evidence or self.bound is None:
                raise AvailabilityViolation("an evidence rule carries evidence and a bound")
            if self.bound < timedelta(0):
                raise AvailabilityViolation("an evidence bound must not be negative")

    def document(self) -> dict[str, Any]:
        """The rule as it is hashed into the policy document."""
        return {
            "subject": self.subject.value,
            "kind": self.kind.value,
            "gap": self.gap,
            "evidence": list(self.evidence),
            "bound_microseconds": None if self.bound is None else self.bound // _MICROSECOND,
        }


_MICROSECOND: Final = timedelta(microseconds=1)
_GAP_PREFIX: Final = f"{AVAILABILITY_POLICY_ID}@{AVAILABILITY_POLICY_VERSION}"

_RULES: Final[dict[AvailabilitySubject, AvailabilityRule]] = {
    AvailabilitySubject.ARCHIVE: AvailabilityRule(
        subject=AvailabilitySubject.ARCHIVE,
        kind=AvailabilityRuleKind.INGEST_FALLBACK,
        gap=(
            f"{_GAP_PREFIX}:archive_publication_time_not_stated — the official source states a "
            "daily cadence and that archives may be updated later, but never the publication "
            "time of a specific archive revision"
        ),
    ),
    AvailabilitySubject.AGG_TRADE: AvailabilityRule(
        subject=AvailabilitySubject.AGG_TRADE,
        kind=AvailabilityRuleKind.INGEST_FALLBACK,
        gap=(
            f"{_GAP_PREFIX}:agg_trade_publication_bound_not_stated — the official stream "
            "documentation says 'Real-time' without any documented bound between trade "
            "execution and public availability"
        ),
    ),
    AvailabilitySubject.KLINE_1M: AvailabilityRule(
        subject=AvailabilitySubject.KLINE_1M,
        kind=AvailabilityRuleKind.INGEST_FALLBACK,
        gap=(
            f"{_GAP_PREFIX}:kline_1m_publication_bound_not_stated — the documented 2000ms stream "
            "update speed is a push cadence, not a bound between interval end and the final "
            "(x = true) candle"
        ),
    ),
}


def rule_for(subject: AvailabilitySubject) -> AvailabilityRule:
    """The rule this policy version uses for ``subject``."""
    return _RULES[subject]


def decide_availability(
    subject: AvailabilitySubject,
    *,
    event_time: datetime,
    ingest_time: datetime,
    knowledge_time: datetime,
    event_end_time: datetime | None = None,
    source_time: datetime | None = None,
    declared_latency: timedelta = timedelta(0),
    rule: AvailabilityRule | None = None,
    binding: PolicyBinding | None = None,
) -> AvailabilityDecision:
    """Compute one revision's two-axis times and its availability decision.

    ``rule`` / ``binding`` default to this policy version; tests may pass another registered
    version to exercise the evidence-bearing branch. Anything that would need a guess — an
    ``ingest_time`` before the observation could exist, a non-monotone knowledge axis, a negative
    latency — fails closed instead of being silently repaired.
    """
    applied = _RULES[subject] if rule is None else rule
    if applied.subject is not subject:
        raise AvailabilityViolation(f"rule {applied.subject.value} does not serve {subject.value}")
    policy = AVAILABILITY_BINDING if binding is None else binding
    if policy.role is not PolicyRole.AVAILABILITY:
        raise AvailabilityViolation("an availability decision needs an availability binding")
    if declared_latency < timedelta(0):
        raise AvailabilityViolation("declared_latency must not be negative")
    if knowledge_time < ingest_time:
        raise AvailabilityViolation("knowledge_time must not precede ingest_time")

    observable = event_time if event_end_time is None else event_end_time
    if applied.kind is AvailabilityRuleKind.INGEST_FALLBACK:
        if ingest_time < observable:
            raise AvailabilityViolation(
                "ingest_time precedes the observable time: the payload claims to describe the "
                "future, which no conservative fallback may paper over"
            )
        if source_time is not None and ingest_time < source_time:
            raise AvailabilityViolation("ingest_time precedes the source-declared source_time")
        available = ingest_time
        evidence: tuple[str, ...] = ()
        gap: str | None = applied.gap
    else:
        assert applied.bound is not None  # guaranteed by AvailabilityRule.__post_init__
        floor = observable if source_time is None else max(observable, source_time)
        available = floor + applied.bound + declared_latency
        evidence = applied.evidence
        gap = None

    times = ObservationTimes(
        event_time=event_time,
        event_end_time=event_end_time,
        source_time=source_time,
        available_time=available,
        ingest_time=ingest_time,
        knowledge_time=knowledge_time,
        declared_latency=declared_latency,
    )
    return AvailabilityDecision(times=times, policy=policy, evidence=evidence, evidence_gap=gap)


#: The complete, versioned policy document; ``AVAILABILITY_HASH`` is its canonical-JSON SHA-256.
AVAILABILITY_SPEC: Final[dict[str, Any]] = {
    "policy_id": AVAILABILITY_POLICY_ID,
    "version": AVAILABILITY_POLICY_VERSION,
    "role": PolicyRole.AVAILABILITY.value,
    "scope": {
        "venue": "binance",
        "market": "spot",
        "source": "binance.public.spot.archive@1.0.0",
        "data_types": ["archive_revision", "agg_trade", "kline_1m"],
    },
    "evidence_document": "docs/architecture/evidence/binance-spot-publication.md",
    "source_time": {
        "set_from": [],
        "rejected_inputs": ["http last-modified", "http etag", "object storage metadata"],
        "reason": "a CDN object header is not a source declaration of a revision publication time",
    },
    "rules": [_RULES[subject].document() for subject in AvailabilitySubject],
    "fail_closed": [
        "ingest_time earlier than the observable time",
        "knowledge_time earlier than ingest_time",
        "negative declared_latency",
        "a rule applied to another subject",
        "a binding whose role is not availability",
    ],
    "invariants": [
        "available_time >= observable time (interval observations use the interval end)",
        "available_time >= source_time when a source_time is given",
        "a recorded evidence gap forces available_time == ingest_time",
        "available_time earlier than ingest_time requires evidence",
    ],
}
AVAILABILITY_HASH: Final = hashlib.sha256(
    canonical_json(AVAILABILITY_SPEC).encode("utf-8")
).hexdigest()
AVAILABILITY_BINDING: Final = PolicyBinding(
    role=PolicyRole.AVAILABILITY,
    policy_id=AVAILABILITY_POLICY_ID,
    version=AVAILABILITY_POLICY_VERSION,
    policy_hash=AVAILABILITY_HASH,
)

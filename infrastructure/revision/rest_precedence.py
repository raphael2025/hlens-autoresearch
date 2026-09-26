"""Precedence policy ``binance.spot.rest-revision@1.0.0`` (Phase 1 D3B; ADR-0027 §2 / §3 / §10).

Scope: two revisions of the **same** observation key that both come from the REST channel —
two response revisions of one page identity, or two element revisions of one aggTrade / 1m
kline. Exactly two outcomes exist:

- **replay** — same (channel-level) source identity and same payload hash: no new revision,
  no new ``arrival_seq`` (ADR-0023 §4 "重复"). Paging overlap and refetches land here;
- **competing heads** — a different payload: both revisions are kept, **no edge is ever
  produced**, and any "latest" answer must fail closed. Binance declares no revision id or
  revision time for REST responses (evidence N1 / N4), so nothing can order them, and this
  policy never invents an order from arrival, clocks, headers or hashes.

Relations to archive revisions are out of scope: they belong to
``binance.spot.delivery-channel@1.0.0`` (``channel_precedence``). The outcome vocabulary and
``RevisionFacts`` are shared with D2's ``precedence`` module (read-only reuse).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any, Final

from core.contracts.revision import PolicyBinding, PolicyRole
from core.domain.base import canonical_json
from infrastructure.contract_version import PHASE1_PUBLICATION_VERSION
from infrastructure.revision.precedence import PrecedenceOutcome, RevisionFacts
from infrastructure.revision.rest_identity import rest_source_identity

__all__ = [
    "REST_PRECEDENCE_BINDING",
    "REST_PRECEDENCE_HASH",
    "REST_PRECEDENCE_POLICY_ID",
    "REST_PRECEDENCE_POLICY_VERSION",
    "REST_PRECEDENCE_SPEC",
    "RestPrecedenceViolation",
    "decide_rest_precedence",
    "rest_competing_revisions",
]

REST_PRECEDENCE_POLICY_ID: Final = "binance.spot.rest-revision"
REST_PRECEDENCE_POLICY_VERSION: Final = "1.0.0"


class RestPrecedenceViolation(ValueError):
    """The inputs cannot yield a lawful decision under this policy (fail closed)."""


def _check_in_scope(fact: RevisionFacts, label: str) -> None:
    if not isinstance(fact, RevisionFacts):
        raise RestPrecedenceViolation(f"{label} must be RevisionFacts")
    if fact.source_identity != rest_source_identity():
        raise RestPrecedenceViolation(
            f"{label} is not a REST revision: cross-channel relations belong to "
            "binance.spot.delivery-channel@1.0.0"
        )
    if fact.source_revision_id is not None or fact.source_revision_time is not None:
        raise RestPrecedenceViolation(
            f"{label} carries a source revision declaration the REST source never makes"
        )


def decide_rest_precedence(candidate: RevisionFacts, other: RevisionFacts) -> PrecedenceOutcome:
    """``REPLAY`` or ``UNORDERED`` — never an ordering — for two REST revisions of one key."""
    _check_in_scope(candidate, "candidate")
    _check_in_scope(other, "other")
    if candidate.observation_key != other.observation_key:
        raise RestPrecedenceViolation("precedence is only defined within one observation_key")
    if candidate.payload_hash == other.payload_hash:
        if candidate.revision_id != other.revision_id:
            raise RestPrecedenceViolation(
                "identical source identity and payload must yield the same revision_id"
            )
        return PrecedenceOutcome.REPLAY
    if candidate.revision_id == other.revision_id:
        raise RestPrecedenceViolation("two different payloads must not share a revision_id")
    return PrecedenceOutcome.UNORDERED


def rest_competing_revisions(
    candidate: RevisionFacts, known: Iterable[RevisionFacts]
) -> tuple[str, ...]:
    """Revision ids ``candidate`` competes with; raises on a replay (append nothing then).

    The candidate never supersedes anything under this policy, so there is no ``supersedes`` and
    no ``PrecedenceEvidence`` to persist: the caller appends the revision with empty edges and
    reports the returned ids as competing heads.
    """
    competing: list[str] = []
    for fact in known:
        outcome = decide_rest_precedence(candidate, fact)
        if outcome is PrecedenceOutcome.REPLAY:
            raise RestPrecedenceViolation(
                f"{candidate.revision_id} is a replay of {fact.revision_id}: no new revision"
            )
        competing.append(fact.revision_id)
    return tuple(sorted(competing))


#: The complete, versioned policy document; ``REST_PRECEDENCE_HASH`` is its JSON SHA-256.
REST_PRECEDENCE_SPEC: Final[Mapping[str, Any]] = {
    "policy_id": REST_PRECEDENCE_POLICY_ID,
    "version": REST_PRECEDENCE_POLICY_VERSION,
    "role": PolicyRole.PRECEDENCE.value,
    "scope": {
        "venue": "binance",
        "market": "spot",
        "source": "binance.public.spot.rest@1.0.0",
        "subjects": [
            "response revisions of one page identity",
            "element revisions of one aggTrade or 1m kline observation key",
        ],
        "out_of_scope": "archive <-> REST relations (binance.spot.delivery-channel@1.0.0)",
    },
    "evidence_document": "docs/architecture/evidence/binance-spot-rest-market-data.md",
    "rules": {
        "replay": "same source identity and same payload hash: no revision, no arrival_seq",
        "append": "same observation key, different payload: always a new appended revision",
        "ordering": "never: the REST source declares no revision id and no revision time",
        "edges": "none; supersedes and precedence evidence stay empty",
    },
    "never_evidence": [
        "retrieved_at",
        "requested_at",
        "ingest_time",
        "knowledge_time",
        "http arrival order",
        "http date",
        "http last-modified",
        "http etag",
        "payload hash",
        "arrival_seq",
        "local wall clock",
    ],
    "competing_heads": "fail closed at dataset level for element keys; response revisions are "
    "never selected by point-in-time queries",
}
REST_PRECEDENCE_HASH: Final = hashlib.sha256(
    canonical_json(REST_PRECEDENCE_SPEC).encode("utf-8")
).hexdigest()
REST_PRECEDENCE_BINDING: Final = PolicyBinding(
    schema_version=PHASE1_PUBLICATION_VERSION,
    role=PolicyRole.PRECEDENCE,
    policy_id=REST_PRECEDENCE_POLICY_ID,
    version=REST_PRECEDENCE_POLICY_VERSION,
    policy_hash=REST_PRECEDENCE_HASH,
)

"""Precedence policy ``binance.spot.archive-revision@1.0.0`` (Phase 1 D2; ADR-0023 §4).

Append order is not revision priority. This module decides, for two revisions of the **same**
observation key, exactly one of three things:

- **replay** — identical source identity and payload hash: no new revision, no new arrival
  sequence number (ADR-0023 §4 "重复");
- **proven precedence** — the source itself declares a revision identity *and* a revision time on
  both sides and they order strictly: the edge is persisted at ingest as ``supersedes`` plus a
  ``PrecedenceEvidence`` record;
- **competing heads** — anything else: both revisions stay, neither supersedes the other, and any
  "latest" answer must fail closed until a new, evidence-bearing precedence record is appended.

What is explicitly *not* evidence (ADR-0023 §4, 03-data.md §4.4): ``retrieved_at``, ingest or
knowledge time, HTTP arrival order, ``ETag`` lexical order, checksums or payload hashes,
``arrival_seq``, and the local wall clock. This module never reads them — the frozen test in
``tests/infrastructure/revision/test_precedence.py`` checks the module's source for them.

Binance's public archives declare no revision id and no revision time: the official repository
only says "Archived files may be updated at a later date as a result of recently discovered
issues". So under 1.0.0 two archives at the same official path with different checksums are
always competing heads — both sets of bytes and both sets of parsed rows are kept, and nothing
picks one. The ordering branch is implemented for sources that do declare a revision time and is
tested with an explicitly test-only policy binding.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from core.contracts.revision import (
    PolicyBinding,
    PolicyRole,
    PrecedenceEvidence,
    RevisionRecord,
)
from core.domain.base import canonical_json

__all__ = [
    "PRECEDENCE_BINDING",
    "PRECEDENCE_HASH",
    "PRECEDENCE_POLICY_ID",
    "PRECEDENCE_POLICY_VERSION",
    "PRECEDENCE_SPEC",
    "PrecedenceOutcome",
    "PrecedenceViolation",
    "RevisionFacts",
    "decide_precedence",
    "maximal_heads",
    "supersedes_for",
]

PRECEDENCE_POLICY_ID: Final = "binance.spot.archive-revision"
PRECEDENCE_POLICY_VERSION: Final = "1.0.0"


class PrecedenceViolation(ValueError):
    """The inputs cannot yield a lawful precedence decision (fail closed; never pick a head)."""


class PrecedenceOutcome(StrEnum):
    """Relation between a candidate revision and one already-known revision of the same key."""

    #: Same source identity and payload: a replay, not a revision.
    REPLAY = "replay"
    #: The candidate provably supersedes the other one.
    SUPERSEDES = "supersedes"
    #: The other one provably supersedes the candidate.
    SUPERSEDED_BY = "superseded_by"
    #: Nothing in the source orders them: competing heads.
    UNORDERED = "unordered"


@dataclass(frozen=True, slots=True)
class RevisionFacts:
    """The only facts a precedence decision may look at.

    ``source_revision_id`` / ``source_revision_time`` are **source-declared** fields (ADR-0023
    §4). Binance public archives declare neither, so the Binance path always passes ``None``.
    """

    observation_key: str
    revision_id: str
    source_identity: str
    payload_hash: str
    source_revision_id: str | None = None
    source_revision_time: datetime | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("observation_key", self.observation_key),
            ("revision_id", self.revision_id),
            ("source_identity", self.source_identity),
            ("payload_hash", self.payload_hash),
        ):
            if not isinstance(value, str) or not value:
                raise PrecedenceViolation(f"{label} must be a non-empty string")
        time = self.source_revision_time
        if time is not None:
            if not isinstance(time, datetime) or time.tzinfo is None:
                raise PrecedenceViolation("source_revision_time must be timezone-aware UTC")
            if time.utcoffset() != timedelta(0):
                raise PrecedenceViolation("source_revision_time must be UTC")
        if (self.source_revision_id is None) != (time is None):
            raise PrecedenceViolation(
                "a source revision is declared by id and time together or not at all"
            )


def decide_precedence(candidate: RevisionFacts, other: RevisionFacts) -> PrecedenceOutcome:
    """Relate ``candidate`` to ``other``; both must belong to the same observation key."""
    if candidate.observation_key != other.observation_key:
        raise PrecedenceViolation("precedence is only defined within one observation_key")
    if candidate.source_identity == other.source_identity:
        if candidate.payload_hash == other.payload_hash:
            if candidate.revision_id != other.revision_id:
                raise PrecedenceViolation(
                    "identical source identity and payload must yield the same revision_id"
                )
            return PrecedenceOutcome.REPLAY
    if candidate.revision_id == other.revision_id:
        raise PrecedenceViolation("two different payloads must not share a revision_id")
    mine, theirs = candidate.source_revision_time, other.source_revision_time
    if mine is None or theirs is None:
        return PrecedenceOutcome.UNORDERED
    if candidate.source_revision_id == other.source_revision_id:
        # The source claims one revision but delivered two payloads: contradictory, never order.
        return PrecedenceOutcome.UNORDERED
    if mine == theirs:
        return PrecedenceOutcome.UNORDERED
    return PrecedenceOutcome.SUPERSEDES if mine > theirs else PrecedenceOutcome.SUPERSEDED_BY


def supersedes_for(
    candidate: RevisionFacts,
    known: Iterable[RevisionFacts],
    *,
    knowledge_time: datetime,
    binding: PolicyBinding | None = None,
) -> tuple[tuple[str, ...], tuple[PrecedenceEvidence, ...], tuple[str, ...]]:
    """Edges to persist with ``candidate``: ``(supersedes, evidence, unordered_revision_ids)``.

    A replay is reported by raising: callers must not append a revision for it. Every returned
    ``supersedes`` id comes with its ``PrecedenceEvidence``, so the pair satisfies
    ``RevisionGraph``'s "every declared edge has evidence not later than this revision".
    """
    policy = PRECEDENCE_BINDING if binding is None else binding
    if policy.role is not PolicyRole.PRECEDENCE:
        raise PrecedenceViolation("a precedence edge needs a precedence binding")
    superseded: list[str] = []
    evidence: list[PrecedenceEvidence] = []
    unordered: list[str] = []
    for fact in known:
        outcome = decide_precedence(candidate, fact)
        if outcome is PrecedenceOutcome.REPLAY:
            raise PrecedenceViolation(
                f"{candidate.revision_id} is a replay of {fact.revision_id}: no new revision"
            )
        if outcome is PrecedenceOutcome.SUPERSEDES:
            superseded.append(fact.revision_id)
            evidence.append(
                PrecedenceEvidence(
                    observation_key=candidate.observation_key,
                    revision_id=candidate.revision_id,
                    superseded_revision_id=fact.revision_id,
                    policy=policy,
                    evidence=(
                        f"source revision {candidate.source_revision_id} declared at "
                        f"{_iso(candidate.source_revision_time)} is later than source revision "
                        f"{fact.source_revision_id} declared at {_iso(fact.source_revision_time)}",
                    ),
                    knowledge_time=knowledge_time,
                )
            )
        elif outcome is PrecedenceOutcome.UNORDERED:
            unordered.append(fact.revision_id)
    return tuple(sorted(superseded)), tuple(evidence), tuple(sorted(unordered))


def _iso(value: datetime | None) -> str:
    if value is None:  # pragma: no cover - only reachable through a decided ordering
        raise PrecedenceViolation("an ordered pair always has both source revision times")
    return value.isoformat().replace("+00:00", "Z")


def maximal_heads(
    records: Iterable[RevisionRecord], evidence: Iterable[PrecedenceEvidence] = ()
) -> tuple[str, ...]:
    """Revision ids of one observation key that nothing directly or transitively supersedes.

    Only persisted ``supersedes`` edges and ``PrecedenceEvidence`` are followed; the order of the
    inputs, ``arrival_seq`` and every time field are irrelevant to the result. Two or more heads
    mean competing heads — the caller must fail closed, not choose.
    """
    known = {record.revision_id: record for record in records}
    if not known:
        return ()
    keys = {record.observation_key for record in known.values()}
    if len(keys) > 1:
        raise PrecedenceViolation("maximal heads are computed per observation_key")
    edges: set[tuple[str, str]] = set()
    for record in known.values():
        edges.update((record.revision_id, older) for older in record.supersedes)
    for item in evidence:
        if item.observation_key not in keys:
            raise PrecedenceViolation("precedence evidence belongs to another observation_key")
        edges.add((item.revision_id, item.superseded_revision_id))
    reachable: dict[str, set[str]] = {revision: set() for revision in known}
    for newer, older in edges:
        reachable.setdefault(newer, set()).add(older)
    superseded: set[str] = set()
    for start in known:
        stack = list(reachable.get(start, ()))
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(reachable.get(node, ()))
        superseded.update(seen)
    return tuple(sorted(revision for revision in known if revision not in superseded))


#: The complete, versioned policy document; ``PRECEDENCE_HASH`` is its canonical-JSON SHA-256.
PRECEDENCE_SPEC: Final[Mapping[str, Any]] = {
    "policy_id": PRECEDENCE_POLICY_ID,
    "version": PRECEDENCE_POLICY_VERSION,
    "role": PolicyRole.PRECEDENCE.value,
    "scope": {
        "venue": "binance",
        "market": "spot",
        "source": "binance.public.spot.archive@1.0.0",
        "subject": "archive revisions at the same official path and the rows parsed from them",
    },
    "evidence_document": "docs/architecture/evidence/binance-spot-publication.md",
    "rules": {
        "replay": "same source identity and same payload hash: no revision, no arrival_seq",
        "append": "same observation key, different payload: always a new appended revision",
        "ordering": (
            "an edge is persisted only when both revisions carry a source-declared revision id "
            "and revision time and those times order strictly"
        ),
        "contradiction": (
            "equal source revision times, one source revision id with two payloads, or a missing "
            "declaration on either side: unordered"
        ),
        "outcome_for_binance_archives": (
            "the official source declares neither a revision id nor a revision time, so archive "
            "replacements are always competing heads under 1.0.0"
        ),
    },
    "never_evidence": [
        "retrieved_at",
        "ingest_time",
        "knowledge_time",
        "http arrival order",
        "http last-modified",
        "http etag",
        "checksum or payload hash",
        "arrival_seq",
        "local wall clock",
    ],
    "graph": {
        "self_reference": "rejected",
        "cycles": "rejected",
        "cross_observation_key_edges": "rejected",
        "dangling_predecessor": "allowed and recorded",
        "edge_without_evidence": "rejected",
    },
    "competing_heads": "fail closed; only a new evidence-bearing precedence record resolves them",
}
PRECEDENCE_HASH: Final = hashlib.sha256(canonical_json(PRECEDENCE_SPEC).encode("utf-8")).hexdigest()
PRECEDENCE_BINDING: Final = PolicyBinding(
    role=PolicyRole.PRECEDENCE,
    policy_id=PRECEDENCE_POLICY_ID,
    version=PRECEDENCE_POLICY_VERSION,
    policy_hash=PRECEDENCE_HASH,
)

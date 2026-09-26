"""Replacement proposals (roadmap Phase 12; ADR-0045): data for a human, never an action.

Evolution may *propose* that a re-validated descendant replace a running strategy; it never
approves, promotes or swaps anything. ``propose_replacement`` checks, and only then records:

- the incumbent is running: its lifecycle history is about the incumbent and is ``ACTIVE`` or
  ``DEGRADED`` (the states from which a replacement is meaningful);
- the candidate is a **new version that descends from the incumbent** (``require_new_version``)
  and the lineage graph traces it back to the incumbent with no missing ancestor on the way;
- the candidate went through validation again on its own history: it is ``PAPER`` or
  ``PRODUCTION_CANDIDATE`` (in-sample gates and the sealed OOS passed; ADR-0006), never a state
  reached by inheritance from the parent;
- at least one evidence reference (e.g. validation report hashes) and a non-empty proposer.

The result is a ``ReplacementProposal`` whose ``status`` is always ``PENDING_HUMAN_APPROVAL``;
nothing in this module can change it. Acting on a proposal is a lifecycle transition made by a
human through the normal Promotion path (ADR-0005 / ADR-0006), outside the research plane.

``ProposalLedger(path)`` keeps proposals in a hash-chained append-only journal
(``research.persistence.AppendOnlyJournal``); reopening replays and re-verifies every proposal
hash. Re-recording an identical proposal is a no-op; nothing is ever edited or removed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from core.domain.base import content_hash
from core.domain.specs import StrategySpec
from core.lifecycle.strategy import LifecycleHistory, LifecycleState
from research.evolution.lineage import LineageGraph
from research.evolution.operators import EvolutionError, require_new_version
from research.persistence import AppendOnlyJournal, JournalCorrupted

__all__ = [
    "CANDIDATE_STATES",
    "INCUMBENT_STATES",
    "PENDING_HUMAN_APPROVAL",
    "ProposalLedger",
    "ReplacementProposal",
    "propose_replacement",
]

PENDING_HUMAN_APPROVAL: Final = "PENDING_HUMAN_APPROVAL"
SCHEMA_VERSION: Final = "1.0.0"
#: A replacement is proposed for a strategy that is running (or degrading) in production.
INCUMBENT_STATES: Final = frozenset({LifecycleState.ACTIVE, LifecycleState.DEGRADED})
#: The candidate passed the in-sample gates and the sealed OOS on its own history.
CANDIDATE_STATES: Final = frozenset({LifecycleState.PAPER, LifecycleState.PRODUCTION_CANDIDATE})
_RECORD_TYPE: Final = "replacement_proposal"


@dataclass(frozen=True)
class ReplacementProposal:
    """A proposal only (see module docs); ``proposal_hash`` covers the whole payload."""

    incumbent: str
    incumbent_hash: str
    incumbent_state: LifecycleState
    candidate: str
    candidate_hash: str
    candidate_state: LifecycleState
    lineage_path: tuple[str, ...]
    evidence: tuple[str, ...]
    reason: str
    proposed_by: str
    proposed_at: datetime
    status: str = field(default=PENDING_HUMAN_APPROVAL, init=False)
    proposal_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.proposed_at.tzinfo is None or self.proposed_at.utcoffset() is None:
            raise ValueError("proposed_at must be timezone-aware (UTC)")
        object.__setattr__(self, "proposal_hash", content_hash(self._body()))

    def _body(self) -> dict[str, Any]:
        return {
            "kind": "replacement_proposal",
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "incumbent": self.incumbent,
            "incumbent_hash": self.incumbent_hash,
            "incumbent_state": self.incumbent_state.value,
            "candidate": self.candidate,
            "candidate_hash": self.candidate_hash,
            "candidate_state": self.candidate_state.value,
            "lineage_path": list(self.lineage_path),
            "evidence": list(self.evidence),
            "reason": self.reason,
            "proposed_by": self.proposed_by,
            "proposed_at": self.proposed_at.astimezone(UTC).isoformat(),
        }

    def to_payload(self) -> dict[str, Any]:
        return {**self._body(), "proposal_hash": self.proposal_hash}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ReplacementProposal:
        """Rebuild and re-verify (hash and the fixed pending status)."""
        expected = {*cls._fields(), "kind", "schema_version", "status", "proposal_hash"}
        if set(payload) != expected:
            raise ValueError("a replacement proposal payload has exactly the recorded fields")
        if payload["kind"] != "replacement_proposal" or payload["schema_version"] != SCHEMA_VERSION:
            raise ValueError("not a replacement proposal of this schema version")
        if payload["status"] != PENDING_HUMAN_APPROVAL:
            raise ValueError("a recorded proposal is always pending human approval")
        proposal = cls(
            incumbent=payload["incumbent"],
            incumbent_hash=payload["incumbent_hash"],
            incumbent_state=LifecycleState(payload["incumbent_state"]),
            candidate=payload["candidate"],
            candidate_hash=payload["candidate_hash"],
            candidate_state=LifecycleState(payload["candidate_state"]),
            lineage_path=tuple(payload["lineage_path"]),
            evidence=tuple(payload["evidence"]),
            reason=payload["reason"],
            proposed_by=payload["proposed_by"],
            proposed_at=datetime.fromisoformat(payload["proposed_at"]),
        )
        if proposal.proposal_hash != payload["proposal_hash"]:
            raise ValueError("the proposal content does not hash to its proposal_hash")
        return proposal

    @staticmethod
    def _fields() -> tuple[str, ...]:
        return (
            "incumbent",
            "incumbent_hash",
            "incumbent_state",
            "candidate",
            "candidate_hash",
            "candidate_state",
            "lineage_path",
            "evidence",
            "reason",
            "proposed_by",
            "proposed_at",
        )


def _lineage_path(
    lineage: LineageGraph, incumbent: StrategySpec, candidate: StrategySpec
) -> tuple[str, ...]:
    """The chain candidate → … → incumbent through recorded parents (shortest, deterministic)."""
    known = {spec.ref for spec in lineage.specs}
    for spec in (incumbent, candidate):
        if spec.ref not in known:
            raise EvolutionError(f"{spec.ref} is not recorded in the lineage graph")
    recorded = next(s for s in lineage.specs if s.ref == candidate.ref)
    if recorded.content_hash() != candidate.content_hash():
        raise EvolutionError(f"{candidate.ref} differs from the spec recorded in the lineage")
    unrecorded = sorted((a for a in lineage.ancestors(candidate.ref) if a not in known), key=str)
    if unrecorded:
        raise EvolutionError(f"ancestors {unrecorded} of {candidate.ref} are not recorded")
    frontier: list[tuple[Any, tuple[Any, ...]]] = [(candidate.ref, (candidate.ref,))]
    seen = {candidate.ref}
    while frontier:
        nxt: list[tuple[Any, tuple[Any, ...]]] = []
        for ref, path in frontier:
            for parent in sorted(lineage.parents(ref), key=str):
                if parent == incumbent.ref:
                    return tuple(str(r) for r in (*path, parent))
                if parent not in seen:
                    seen.add(parent)
                    nxt.append((parent, (*path, parent)))
        frontier = nxt
    raise EvolutionError(f"{candidate.ref} does not descend from {incumbent.ref} in the lineage")


def _state_of(history: LifecycleHistory, spec: StrategySpec, role: str) -> LifecycleState:
    if history.subject.target_identity() != spec.ref.target_identity():
        raise EvolutionError(f"the {role} lifecycle history belongs to {history.subject}")
    return history.current_state


def propose_replacement(
    *,
    incumbent: StrategySpec,
    incumbent_history: LifecycleHistory,
    candidate: StrategySpec,
    candidate_history: LifecycleHistory,
    lineage: LineageGraph,
    evidence: tuple[str, ...],
    reason: str,
    proposed_by: str,
    proposed_at: datetime,
) -> ReplacementProposal:
    """Check the preconditions (module docs) and return a pending proposal; never acts on it."""
    incumbent_state = _state_of(incumbent_history, incumbent, "incumbent")
    if incumbent_state not in INCUMBENT_STATES:
        raise EvolutionError(
            f"{incumbent.ref} is {incumbent_state}; a replacement needs one of "
            f"{sorted(s.value for s in INCUMBENT_STATES)}"
        )
    candidate_state = _state_of(candidate_history, candidate, "candidate")
    if candidate_state not in CANDIDATE_STATES:
        raise EvolutionError(
            f"{candidate.ref} is {candidate_state}; it must pass validation again on its own "
            f"history (one of {sorted(s.value for s in CANDIDATE_STATES)})"
        )
    if candidate.content_hash() == incumbent.content_hash():
        raise EvolutionError("a replacement must be a different strategy version")
    require_new_version(incumbent, candidate)
    path = _lineage_path(lineage, incumbent, candidate)
    if not evidence or any(not item.strip() for item in evidence):
        raise EvolutionError("a replacement proposal needs non-empty evidence references")
    if not reason.strip() or not proposed_by.strip():
        raise EvolutionError("a replacement proposal needs a reason and a proposer")
    return ReplacementProposal(
        incumbent=str(incumbent.ref),
        incumbent_hash=incumbent.content_hash(),
        incumbent_state=incumbent_state,
        candidate=str(candidate.ref),
        candidate_hash=candidate.content_hash(),
        candidate_state=candidate_state,
        lineage_path=path,
        evidence=tuple(evidence),
        reason=reason,
        proposed_by=proposed_by,
        proposed_at=proposed_at,
    )


class ProposalLedger:
    """Append-only record of proposals (module docs); there is no approve / edit / delete."""

    def __init__(self, path: Path) -> None:
        self._journal = AppendOnlyJournal(path)
        self._proposals: dict[str, ReplacementProposal] = {}
        for entry in self._journal.entries:
            if entry.type != _RECORD_TYPE:
                raise JournalCorrupted(f"{path}: unknown record type {entry.type!r}")
            try:
                proposal = ReplacementProposal.from_payload(dict(entry.payload))
            except (ValueError, KeyError, TypeError) as exc:
                raise JournalCorrupted(f"{path}: line {entry.seq} is not a valid proposal") from exc
            if proposal.proposal_hash in self._proposals:
                raise JournalCorrupted(f"{path}: line {entry.seq} repeats a proposal")
            self._proposals[proposal.proposal_hash] = proposal

    def record(self, proposal: ReplacementProposal) -> None:
        if proposal.proposal_hash in self._proposals:
            return
        self._journal.append(_RECORD_TYPE, proposal.to_payload())
        self._proposals[proposal.proposal_hash] = proposal

    @property
    def proposals(self) -> tuple[ReplacementProposal, ...]:
        return tuple(self._proposals.values())

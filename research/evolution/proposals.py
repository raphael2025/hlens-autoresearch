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
The ledger has a single writer (an ``flock`` on ``<path>.lock``, ``ProposalLedgerLocked``) and an
optional external anchor (``anchor=``, a ``ProposalAnchor`` outside the ledger's directory) that
turns a truncated, deleted or rolled-back ledger into a refusal on reopening
(``ProposalLedgerInconsistent``); see ``ProposalLedger``. The research job that feeds it from a
loop's durable lineage and verified validation reports is ``research.evolution.replacement_job``.
"""

from __future__ import annotations

import fcntl
import os
import weakref
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from core.domain.base import Kind, content_hash
from core.domain.specs import StrategySpec
from core.lifecycle.strategy import LifecycleHistory, LifecycleState
from research.evolution.lineage import LineageGraph
from research.evolution.operators import EvolutionError, require_new_version
from research.persistence import AppendOnlyJournal, JournalCorrupted

__all__ = [
    "CANDIDATE_STATES",
    "INCUMBENT_STATES",
    "PENDING_HUMAN_APPROVAL",
    "ProposalAnchor",
    "ProposalLedger",
    "ProposalLedgerInconsistent",
    "ProposalLedgerLocked",
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
_ANCHOR_TYPE: Final = "proposal_ledger_head"


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
    # Strategy ancestry must be complete; a library spec's lineage also cites non-strategy
    # provenance (e.g. the knowledge items it was derived from), which is not a strategy version
    # and is never in a strategy lineage graph (the loop's evolution stage checks the same).
    unrecorded = sorted(
        (
            str(a)
            for a in lineage.ancestors(candidate.ref)
            if a not in known and a.kind is Kind.STRATEGY
        ),
    )
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


class ProposalLedgerLocked(RuntimeError):
    """Another live ledger object (this or another process) holds the ledger (single writer)."""


class ProposalLedgerInconsistent(JournalCorrupted):
    """The ledger is behind or diverged from its external anchor (rolled back / truncated)."""


class _Flock:
    """An exclusive, non-blocking ``fcntl.flock`` on a lock file; ``release`` is idempotent."""

    def __init__(self, path: Path) -> None:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise ProposalLedgerLocked(f"{path} is held by another ledger (single writer)") from exc
        except BaseException:
            os.close(fd)
            raise
        self._fd: int | None = fd

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


class ProposalAnchor:
    """The ledger's head kept **outside** the ledger's directory (a hash-chained journal).

    Every publish is one ``proposal_ledger_head`` line ``{"count", "head"}`` (number of ledger
    lines and the ledger's chain head). Publishing the current head again is a no-op; a head with
    fewer lines, or another head at the same count, is refused: the anchor never moves back or
    sideways.
    """

    def __init__(self, path: Path) -> None:
        self._journal = AppendOnlyJournal(Path(path))

    @property
    def path(self) -> Path:
        return self._journal.path

    def load(self) -> tuple[int, str] | None:
        entries = self._journal.entries
        if not entries:
            return None
        last = entries[-1]
        payload = last.payload
        if (
            last.type != _ANCHOR_TYPE
            or set(payload) != {"count", "head"}
            or isinstance(payload["count"], bool)
            or not isinstance(payload["count"], int)
            or payload["count"] < 1
            or not isinstance(payload["head"], str)
        ):
            raise ProposalLedgerInconsistent(f"{self.path}:{last.seq} is not a ledger head")
        return payload["count"], payload["head"]

    def publish(self, count: int, head: str) -> None:
        last = self.load()
        if last == (count, head) or (last is None and count == 0):
            return  # the same head again, or an empty ledger: nothing to keep
        if last is not None and count <= last[0]:
            raise ProposalLedgerInconsistent(
                f"the anchor {self.path} is at ledger line {last[0]}; it never moves back or "
                f"sideways (asked to keep line {count})"
            )
        self._journal.append(_ANCHOR_TYPE, {"count": count, "head": head})


class ProposalLedger:
    """Append-only record of proposals (module docs); there is no approve / edit / delete.

    **Single writer**: the constructor takes an exclusive ``flock`` on ``<path>.lock`` before it
    reads the file (``ProposalLedgerLocked`` while another live ledger object — in this or another
    process — holds it); ``close()`` / leaving a ``with`` block / dropping the object releases it
    (the kernel drops it when a process dies: no stale lock).

    **External anchor** (optional ``anchor=``, a path outside the ledger's directory): after every
    new line and after a verified opening, the anchor keeps the ledger's line count and chain head
    (``ProposalAnchor``). On opening, a ledger with fewer lines than the anchor (lines dropped from
    its end, the file deleted or replaced by an older copy), another line at the anchored position,
    or any line while the anchor is empty (lost, or attached to a ledger that already had lines) is
    refused (``ProposalLedgerInconsistent``). A ledger ahead of its anchor (e.g. the process died
    between appending and anchoring) is accepted and the anchor moves up: an anchor detects lost
    lines, it does not authenticate added ones (the journal is a hash chain, not a signature).
    **Without** an anchor a whole-line truncation of the end of the file is a valid shorter chain
    and cannot be told from an older ledger (a rewritten or partially cut line is
    ``JournalCorrupted`` either way).
    """

    def __init__(self, path: Path, *, anchor: Path | None = None) -> None:
        path = Path(path)
        if anchor is not None and Path(anchor).resolve().is_relative_to(path.parent.resolve()):
            raise ValueError(
                f"the anchor {anchor} lies in the ledger's directory {path.parent}: it must live "
                "outside it (it has to survive a rollback of that directory)"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = _Flock(path.with_name(f"{path.name}.lock"))
        try:
            self._journal = AppendOnlyJournal(path)
            self._anchor = None if anchor is None else ProposalAnchor(anchor)
            self._proposals: dict[str, ReplacementProposal] = {}
            self._replay(path)
            self._check_anchor(path)
        except BaseException:
            self._lock.release()
            raise
        weakref.finalize(self, self._lock.release)

    def _replay(self, path: Path) -> None:
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

    def _check_anchor(self, path: Path) -> None:
        if self._anchor is None:
            return
        entries = self._journal.entries
        anchored = self._anchor.load()
        if anchored is None:
            if entries:
                raise ProposalLedgerInconsistent(
                    f"the anchor {self._anchor.path} holds no head but {path} holds "
                    f"{len(entries)} line(s): the anchor was lost or attached late (a human "
                    "checks the ledger and anchors it deliberately)"
                )
            return
        count, head = anchored
        if count > len(entries):
            raise ProposalLedgerInconsistent(
                f"{path} holds {len(entries)} line(s) but its anchor recorded {count}: the ledger "
                "was truncated, deleted or rolled back (proposals would be lost)"
            )
        if entries[count - 1].hash != head:
            raise ProposalLedgerInconsistent(f"{path} has diverged from its anchor at line {count}")
        self._publish()

    def _publish(self) -> None:
        if self._anchor is not None:
            self._anchor.publish(len(self._journal.entries), self._journal.head_hash)

    def record(self, proposal: ReplacementProposal) -> None:
        if not self._lock.held:
            raise ProposalLedgerLocked("this ledger was closed: open it again to record")
        if proposal.proposal_hash in self._proposals:
            return
        self._journal.append(_RECORD_TYPE, proposal.to_payload())
        self._proposals[proposal.proposal_hash] = proposal
        self._publish()

    def close(self) -> None:
        """Release the single-writer lock (idempotent); a closed ledger can still be read."""
        self._lock.release()

    def __enter__(self) -> ProposalLedger:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def path(self) -> Path:
        return self._journal.path

    @property
    def proposals(self) -> tuple[ReplacementProposal, ...]:
        return tuple(self._proposals.values())

    def proposal_for(self, incumbent: str, candidate: str) -> ReplacementProposal | None:
        """The recorded proposal for this (incumbent, candidate) pair, if any."""
        return next(
            (
                p
                for p in self._proposals.values()
                if (p.incumbent, p.candidate) == (incumbent, candidate)
            ),
            None,
        )

"""Replacement proposal job (Phase 12, ADR-0045 implementation note 2026-09-26): an explicit
research job, run by a caller, **outside** the continuous loop.

Why not inside the loop: the loop's lifecycle guard only reaches ``OOS`` (``apps.worker.loop``
``AUTOMATABLE_TARGETS``; ``OOS → PAPER`` is a human approval, ADR-0006), so no evolution offspring
is ever ``PAPER`` / ``PRODUCTION_CANDIDATE`` inside the loop and there is no in-loop trigger for a
proposal. The loop stays unchanged; its durable lineage (``lineage.jsonl`` in a loop state
directory) is read here, read-only.

``propose_replacements(...)`` takes everything that lives outside the loop from the caller:

- ``incumbents``: ``Incumbent(spec, history)`` — strategy versions running in production, their
  lifecycle ``ACTIVE`` or ``DEGRADED`` (refused at construction otherwise); they come from the
  Strategy Registry / production side, never from the loop;
- ``candidates``: ``ReplacementCandidate(spec, history, report_hashes)`` — evolution offspring with
  their lifecycle as recorded by the human Promotion path and the content hashes of the
  validation reports that back it;
- ``reports``: a resolver ``report hash -> ValidationReport | None`` (e.g.
  ``research.router.evidence.report_store_resolver(root)``, or ``reports_by_hash(...)``);
- ``profiles``: the ``ValidationProfile`` objects the reports were produced under (required, no
  default: ``check_report`` refuses a report whose Profile is not among them, and one missing the
  ADR-0060 ``G2.market_benchmark`` item its Profile's rule requires or the ``G2.inverse_control``
  item its Profile's ``inverse_control_reported`` requires);
- ``lineage``: the loop's lineage (``read_lineage(state_dir / LINEAGE_FILE)``: a verified,
  in-memory copy of the hash-chained journal; nothing is written to the loop's directory);
- ``ledger``: the durable ``ProposalLedger`` (single writer, optional external anchor);
- ``reason`` (a ``str.format`` template over ``{incumbent}``, ``{incumbent_state}``,
  ``{candidate}``, ``{candidate_state}``), ``proposed_by`` and ``proposed_at`` — no defaults.

For every (candidate, incumbent) pair: a candidate that does not descend from the incumbent in the
lineage is ``not_descendant`` (no proposal); a pair already in the ledger is ``already_proposed``
(never proposed twice: idempotent across runs and restarts); a candidate not ``PAPER`` /
``PRODUCTION_CANDIDATE`` on the Promotion path is refused (no proposal); otherwise every claimed
report must pass ``research.router.evidence.check_report`` — found, well-formed, hashing to the
claimed hash, ``subject`` = the candidate, verdict PASS **including G5** (sealed OOS), its Profile
given and the ADR-0060 items it calls for present — and then
``propose_replacement`` must accept the pair (incumbent ACTIVE / DEGRADED, candidate PAPER /
PRODUCTION_CANDIDATE on its own history, new version, traceable lineage); the proposal (evidence
``validation_report:<hash>`` per verified report) is recorded. Any refusal is returned in
``refused`` with its reason, never raised. A proposal is always ``PENDING_HUMAN_APPROVAL``:
nothing here approves, promotes, swaps or changes any lifecycle state.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from core.contracts.validation_profile import ValidationProfile
from core.domain.research import ValidationReport
from core.domain.specs import StrategySpec
from core.lifecycle.strategy import LifecycleHistory
from research.evolution.lineage import LineageGraph
from research.evolution.operators import EvolutionError
from research.evolution.proposals import (
    CANDIDATE_STATES,
    INCUMBENT_STATES,
    PENDING_HUMAN_APPROVAL,
    ProposalLedger,
    ReplacementProposal,
    propose_replacement,
)
from research.router.evidence import ReportResolver, check_report

__all__ = [
    "LINEAGE_FILE",
    "Incumbent",
    "ReplacementCandidate",
    "ReplacementJobResult",
    "propose_replacements",
    "read_lineage",
    "reports_by_hash",
]

#: The lineage journal of a loop state directory (``research.loop.durable.LINEAGE_FILE``; not
#: imported: ``research.loop`` depends on this package; a test pins the equality).
LINEAGE_FILE: Final = "lineage.jsonl"
_REASON_FIELDS: Final = ("incumbent", "incumbent_state", "candidate", "candidate_state")


@dataclass(frozen=True)
class Incumbent:
    """A strategy version running in production, declared by the caller (module docs)."""

    spec: StrategySpec
    history: LifecycleHistory

    def __post_init__(self) -> None:
        if self.history.subject.target_identity() != self.spec.ref.target_identity():
            raise ValueError(
                f"the incumbent lifecycle history belongs to {self.history.subject}, "
                f"not {self.spec.ref}"
            )
        state = self.history.current_state
        if state not in INCUMBENT_STATES:
            raise ValueError(
                f"{self.spec.ref} is {state.value}: an incumbent is running in production "
                f"(one of {sorted(s.value for s in INCUMBENT_STATES)})"
            )


@dataclass(frozen=True)
class ReplacementCandidate:
    """An offspring, its Promotion-path lifecycle and the reports that back it (module docs)."""

    spec: StrategySpec
    history: LifecycleHistory
    report_hashes: tuple[str, ...]


@dataclass(frozen=True)
class ReplacementJobResult:
    """What one run did; ``payload()`` is JSON-ready."""

    recorded: tuple[ReplacementProposal, ...]
    already_proposed: tuple[tuple[str, str, str], ...]  # incumbent, candidate, proposal hash
    not_descendant: tuple[tuple[str, str], ...]  # incumbent, candidate
    refused: tuple[tuple[str, str, str], ...]  # incumbent, candidate, reason

    def payload(self) -> dict[str, Any]:
        return {
            "status": PENDING_HUMAN_APPROVAL,
            "recorded": [p.to_payload() for p in self.recorded],
            "already_proposed": [
                {"incumbent": i, "candidate": c, "proposal_hash": h}
                for i, c, h in self.already_proposed
            ],
            "not_descendant": [{"incumbent": i, "candidate": c} for i, c in self.not_descendant],
            "refused": [{"incumbent": i, "candidate": c, "reason": r} for i, c, r in self.refused],
        }


def read_lineage(path: Path) -> LineageGraph:
    """A verified in-memory copy of a durable lineage journal (e.g. a loop state directory's
    ``lineage.jsonl``). The hash chain and every record are checked (``JournalCorrupted``); a
    missing file is refused (``FileNotFoundError``: no lineage is not an empty lineage). Nothing
    is ever written to ``path``."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"{path}: no lineage journal")
    return LineageGraph(LineageGraph((), path=path).specs)


def reports_by_hash(reports: Iterable[ValidationReport]) -> ReportResolver:
    """A resolver over report objects, keyed by their content hash."""
    table = {report.content_hash(): report for report in reports}
    return table.get


def _check_template(reason: str, proposed_by: str) -> None:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a replacement proposal needs a non-empty reason template")
    if not isinstance(proposed_by, str) or not proposed_by.strip():
        raise ValueError("a replacement proposal needs a non-empty proposer")
    try:
        reason.format(**{name: name for name in _REASON_FIELDS})
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(
            f"the reason template may only use {{{'}, {'.join(_REASON_FIELDS)}}}: {exc!r}"
        ) from exc


def _evidence(
    candidate: ReplacementCandidate,
    reports: ReportResolver,
    profiles: Sequence[ValidationProfile],
) -> tuple[tuple[str, ...], str | None]:
    """The verified evidence references, or the refusal reason."""
    if not candidate.report_hashes:
        return (), "no validation report claimed for the candidate"
    strategy = str(candidate.spec.ref)
    state = candidate.history.current_state.value
    for report_hash in candidate.report_hashes:
        check = check_report(strategy, state, report_hash, reports, profiles=profiles)
        if not check.verified:
            return (), f"report {report_hash}: {check.refusal} ({check.detail})"
    return tuple(f"validation_report:{h}" for h in candidate.report_hashes), None


def propose_replacements(
    *,
    incumbents: Sequence[Incumbent],
    candidates: Sequence[ReplacementCandidate],
    reports: ReportResolver,
    profiles: Sequence[ValidationProfile],
    lineage: LineageGraph,
    ledger: ProposalLedger,
    reason: str,
    proposed_by: str,
    proposed_at: datetime,
) -> ReplacementJobResult:
    """Propose and record (module docs); refusals are returned, never raised."""
    _check_template(reason, proposed_by)
    if not all(isinstance(i, Incumbent) for i in incumbents):
        raise TypeError("every incumbent must be an Incumbent (spec + ACTIVE/DEGRADED history)")
    recorded: list[ReplacementProposal] = []
    already: list[tuple[str, str, str]] = []
    unrelated: list[tuple[str, str]] = []
    refused: list[tuple[str, str, str]] = []
    for candidate in candidates:
        child = str(candidate.spec.ref)
        ancestors = {str(ref) for ref in lineage.ancestors(candidate.spec.ref)}
        for incumbent in incumbents:
            ref = str(incumbent.spec.ref)
            if ref not in ancestors:
                unrelated.append((ref, child))
                continue
            known = ledger.proposal_for(ref, child)
            if known is not None:
                already.append((ref, child, known.proposal_hash))
                continue
            state = candidate.history.current_state
            if state not in CANDIDATE_STATES:
                refused.append((ref, child, f"not re-validated through the human gate: {state}"))
                continue
            evidence, refusal = _evidence(candidate, reports, profiles)
            if refusal is not None:
                refused.append((ref, child, refusal))
                continue
            try:
                proposal = propose_replacement(
                    incumbent=incumbent.spec,
                    incumbent_history=incumbent.history,
                    candidate=candidate.spec,
                    candidate_history=candidate.history,
                    lineage=lineage,
                    evidence=evidence,
                    reason=reason.format(
                        incumbent=ref,
                        incumbent_state=incumbent.history.current_state.value,
                        candidate=child,
                        candidate_state=candidate.history.current_state.value,
                    ),
                    proposed_by=proposed_by,
                    proposed_at=proposed_at,
                )
            except EvolutionError as exc:
                refused.append((ref, child, str(exc)))
                continue
            ledger.record(proposal)
            recorded.append(proposal)
    return ReplacementJobResult(tuple(recorded), tuple(already), tuple(unrelated), tuple(refused))

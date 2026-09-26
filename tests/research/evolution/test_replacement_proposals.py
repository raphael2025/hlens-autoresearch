"""Phase 12 (ADR-0045): replacement proposals are data for a human, never an action."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.domain.base import Kind, Ref
from core.domain.specs import StrategySpec
from core.lifecycle.strategy import HUMAN_APPROVAL_TRANSITIONS, LifecycleHistory, LifecycleState
from core.lifecycle.strategy import LifecycleTransition as Transition
from research.evolution import (
    PENDING_HUMAN_APPROVAL,
    EvolutionError,
    LineageGraph,
    ProposalLedger,
    ReplacementProposal,
    combine,
    mutate,
    propose_replacement,
)
from research.persistence import JournalCorrupted

S = LifecycleState
T0 = datetime(2026, 9, 1, tzinfo=UTC)
SIGNAL = Ref(kind=Kind.FEATURE, name="bar_log_return", version="1.0.0")
PATH_TO_ACTIVE = (
    S.IDEA,
    S.CANDIDATE,
    S.VALIDATION,
    S.OOS,
    S.PAPER,
    S.PRODUCTION_CANDIDATE,
    S.ACTIVE,
)


def _spec(name: str = "tsmom", lookback: int = 20) -> StrategySpec:
    return StrategySpec.model_validate(
        {
            "name": name,
            "version": "1.0.0",
            "signals": (SIGNAL,),
            "params": {"lookback": lookback},
            "param_search_space": {"lookback": (10, 20, 40)},
        }
    )


def _history(spec: StrategySpec, until: LifecycleState) -> LifecycleHistory:
    history = LifecycleHistory(subject=spec.ref)
    for i, (a, b) in enumerate(zip(PATH_TO_ACTIVE, PATH_TO_ACTIVE[1:], strict=False)):
        if a is until:
            break
        history = history.append(
            Transition(
                subject=spec.ref,
                from_state=a,
                to_state=b,
                reason="test",
                evidence=(f"report:{spec.name}:{i}",),
                triggered_by="test",
                approved_by="raphael" if (a, b) in HUMAN_APPROVAL_TRANSITIONS else None,
                occurred_at=T0 + timedelta(minutes=i),
            )
        )
    return history


def _setup() -> tuple[StrategySpec, StrategySpec, LineageGraph]:
    parent = _spec()
    child = mutate(parent, "lookback", 40).spec
    grandchild = mutate(child, "lookback", 10).spec
    return parent, grandchild, LineageGraph((parent, child, grandchild))


def _propose(**overrides: object) -> ReplacementProposal:
    parent, grandchild, lineage = _setup()
    kwargs: dict[str, object] = {
        "incumbent": parent,
        "incumbent_history": _history(parent, S.ACTIVE),
        "candidate": grandchild,
        "candidate_history": _history(grandchild, S.PAPER),
        "lineage": lineage,
        "evidence": ("validation_report:abc", "g5_report:def"),
        "reason": "incumbent degrading; descendant passed G0-G5",
        "proposed_by": "research_loop:evolution",
        "proposed_at": T0,
    }
    kwargs.update(overrides)
    return propose_replacement(**kwargs)  # type: ignore[arg-type]


def test_a_valid_descendant_yields_a_pending_proposal_with_a_traced_lineage() -> None:
    proposal = _propose()
    parent, grandchild, _ = _setup()
    assert proposal.status == PENDING_HUMAN_APPROVAL
    assert proposal.lineage_path[0] == str(grandchild.ref)
    # ``mutate`` records the full ancestry, so the grandchild links to the incumbent directly
    assert proposal.lineage_path == (str(grandchild.ref), str(parent.ref))
    assert proposal.candidate_state is S.PAPER and proposal.incumbent_state is S.ACTIVE
    assert _propose().proposal_hash == proposal.proposal_hash  # deterministic


def test_the_status_cannot_be_set_to_approved() -> None:
    with pytest.raises(TypeError):
        ReplacementProposal(status="APPROVED", **{})  # type: ignore[call-arg]
    payload = _propose().to_payload()
    with pytest.raises(ValueError, match="pending"):
        ReplacementProposal.from_payload({**payload, "status": "APPROVED"})


def test_a_degraded_incumbent_may_be_replaced_but_a_paper_one_may_not() -> None:
    parent, _, _ = _setup()
    degraded = _history(parent, S.ACTIVE).append(
        Transition(
            subject=parent.ref, from_state=S.ACTIVE, to_state=S.DEGRADED, reason="drift",
            evidence=("monitor:x",), triggered_by="monitor", occurred_at=T0 + timedelta(hours=1),
        )
    )  # fmt: skip
    assert _propose(incumbent_history=degraded).incumbent_state is S.DEGRADED
    with pytest.raises(EvolutionError, match="replacement needs"):
        _propose(incumbent_history=_history(parent, S.PAPER))


@pytest.mark.parametrize("state", [S.IDEA, S.CANDIDATE, S.VALIDATION, S.OOS])
def test_a_candidate_that_has_not_passed_validation_again_is_refused(state: LifecycleState) -> None:
    _, grandchild, _ = _setup()
    with pytest.raises(EvolutionError, match="pass validation again"):
        _propose(candidate_history=_history(grandchild, state))


def test_histories_must_belong_to_their_specs() -> None:
    parent, grandchild, _ = _setup()
    with pytest.raises(EvolutionError, match="incumbent lifecycle history"):
        _propose(incumbent_history=_history(grandchild, S.ACTIVE))
    with pytest.raises(EvolutionError, match="candidate lifecycle history"):
        _propose(candidate_history=_history(parent, S.PAPER))


def test_a_candidate_outside_the_incumbents_lineage_is_refused() -> None:
    parent, grandchild, lineage = _setup()
    stranger = _spec("other_strategy")
    with pytest.raises(EvolutionError):
        _propose(
            candidate=stranger,
            candidate_history=_history(stranger, S.PAPER),
            lineage=LineageGraph((*lineage.specs, stranger)),
        )
    # a combination descends from both parents and is a legitimate proposal for either ...
    incumbent = mutate(parent, "lookback", 40).spec  # 1.1.0
    other = _spec("vol_target", lookback=40)
    combo = combine(incumbent, other, "tsmom_vol").spec  # 1.0.0
    proposal = _propose(
        incumbent=incumbent,
        incumbent_history=_history(incumbent, S.ACTIVE),
        candidate=combo,
        candidate_history=_history(combo, S.PRODUCTION_CANDIDATE),
        lineage=LineageGraph((parent, incumbent, other, combo)),
    )
    assert proposal.lineage_path == (str(combo.ref), str(incumbent.ref))
    # ... but the existing in-place guard (``require_new_version``) refuses any candidate whose
    # version equals the incumbent's, even under another name — conservative, kept as is
    partner = _spec("vol_target", lookback=20)
    same_version = combine(parent, partner, "tsmom_vol2").spec
    with pytest.raises(EvolutionError, match="in place"):
        _propose(
            candidate=same_version,
            candidate_history=_history(same_version, S.PAPER),
            lineage=LineageGraph((parent, partner, same_version)),
        )


def test_a_missing_ancestor_or_an_unrecorded_candidate_is_refused() -> None:
    parent, grandchild, lineage = _setup()
    child = next(s for s in lineage.specs if s.ref not in {parent.ref, grandchild.ref})
    with pytest.raises(EvolutionError, match="not recorded"):
        _propose(lineage=LineageGraph((parent, grandchild)))  # the middle generation is missing
    with pytest.raises(EvolutionError, match="not recorded"):
        _propose(lineage=LineageGraph((parent, child)))
    tampered = grandchild.model_copy(update={"params": {"lookback": 20}})
    with pytest.raises(EvolutionError):
        _propose(candidate=tampered, candidate_history=_history(tampered, S.PAPER))


def test_the_incumbent_itself_or_an_in_place_edit_is_refused() -> None:
    parent, _, lineage = _setup()
    with pytest.raises(EvolutionError):
        _propose(candidate=parent, candidate_history=_history(parent, S.PAPER))


@pytest.mark.parametrize(
    "override",
    [{"evidence": ()}, {"evidence": ("  ",)}, {"reason": " "}, {"proposed_by": ""}],
)
def test_evidence_reason_and_proposer_are_required(override: dict[str, object]) -> None:
    with pytest.raises(EvolutionError):
        _propose(**override)


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        _propose(proposed_at=datetime(2026, 9, 1))


def test_the_payload_round_trips_and_tampering_is_detected() -> None:
    proposal = _propose()
    payload = json.loads(json.dumps(proposal.to_payload()))
    assert ReplacementProposal.from_payload(payload) == proposal
    with pytest.raises(ValueError, match="hash"):
        ReplacementProposal.from_payload({**payload, "reason": "something else"})
    with pytest.raises(ValueError, match="exactly"):
        ReplacementProposal.from_payload({**payload, "approved_by": "raphael"})


def test_the_ledger_is_append_only_idempotent_and_verified_on_reopen(tmp_path: Path) -> None:
    path = tmp_path / "proposals.jsonl"
    ledger = ProposalLedger(path)
    proposal = _propose()
    ledger.record(proposal)
    ledger.record(proposal)  # identical re-record: no new line
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert ProposalLedger(path).proposals == (proposal,)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("incumbent degrading", "incumbent excellent"), encoding="utf-8")
    with pytest.raises(JournalCorrupted):
        ProposalLedger(path)


def test_a_ledger_line_approving_a_proposal_is_refused(tmp_path: Path) -> None:
    from research.persistence import AppendOnlyJournal

    path = tmp_path / "proposals.jsonl"
    AppendOnlyJournal(path).append(
        "replacement_proposal", {**_propose().to_payload(), "status": "APPROVED"}
    )
    with pytest.raises(JournalCorrupted, match="not a valid proposal"):
        ProposalLedger(path)
    other = tmp_path / "other.jsonl"
    AppendOnlyJournal(other).append("approve", {"proposal_hash": "x"})
    with pytest.raises(JournalCorrupted, match="unknown record type"):
        ProposalLedger(other)

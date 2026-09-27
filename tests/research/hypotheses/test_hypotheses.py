"""Phase 7 framework: operators, generation, pre-registration and trial counting (ADR-0040)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from core.domain.base import Kind, Ref
from core.domain.research import HypothesisOrigin
from plugins.knowledge import LocalKnowledgeProvider
from plugins.llm import ScriptedLLMProvider
from research.hypotheses import (
    LedgerError,
    TrialLedger,
    conditioning,
    ensemble,
    from_knowledge,
    from_llm,
    interaction,
    negation,
    temporal,
    transformation,
)

S = Ref(kind=Kind.STRATEGY, name="tsmom", version="1.0.0")
ST = Ref(kind=Kind.STATE, name="vol_regime", version="1.0.0")
F1 = Ref(kind=Kind.FEATURE, name="bar_log_return", version="1.0.0")
F2 = Ref(kind=Kind.FEATURE, name="bar_realized_vol_30", version="1.0.0")
E1 = Ref(kind=Kind.EVENT, name="vol_breakout", version="1.0.0")
NOW = datetime(2026, 9, 25, tzinfo=UTC)


def test_every_operator_traces_its_inputs_and_counts_as_a_trial() -> None:
    made = [
        conditioning("h_cond", "fam", S, ST, "high", "0.1 sharpe"),
        interaction("h_inter", "fam", F1, F2, "0.01 ic"),
        temporal("h_temp", "fam", E1, E1, 5, "10 bp"),
        transformation("h_rank", "fam", F1, "rank", "0.01 ic"),
        ensemble("h_ens", "fam", (F1, F2), "0.1 sharpe"),
        negation("h_neg", "fam", S, "0.1 sharpe"),
    ]
    ledger = TrialLedger()
    for hypothesis in made:
        assert hypothesis.origin is HypothesisOrigin.COMBINATION and hypothesis.origin_refs
        assert ledger.register(hypothesis)
    assert ledger.trials("fam") == 6
    assert not ledger.register(made[0])  # idempotent re-registration is not a new trial
    assert ledger.trials("fam") == 6


def test_a_registered_hypothesis_is_immutable() -> None:
    ledger = TrialLedger()
    original = negation("h", "fam", S, "0.1")
    ledger.register(original)
    changed = original.model_copy(update={"statement": "something else"})
    with pytest.raises(LedgerError, match="new version"):
        ledger.register(changed)


def test_a_reevaluation_is_its_own_pre_registered_trial() -> None:
    """ADR-0049 accumulated-window note: every further look at a hypothesis counts as a trial."""
    ledger = TrialLedger()
    first, second = negation("h_a", "fam", S, "0.1"), negation("h_b", "fam", S, "0.1")
    with pytest.raises(LedgerError, match="not registered"):
        ledger.register_reevaluation(first, "round:1")
    ledger.register(first)
    ledger.register(second)
    assert ledger.trials("fam") == 2 and ledger.is_registered(first)
    assert not ledger.is_registered(first, "round:1")
    assert ledger.register_reevaluation(first, "round:1")
    assert not ledger.register_reevaluation(first, " round:1 ")  # the same attempt: no new trial
    assert ledger.register_reevaluation(first, "round:2")
    assert ledger.trials("fam") == 4 and ledger.trials("other") == 0
    assert ledger.is_registered(first, "round:1") and not ledger.is_registered(second, "round:1")
    assert [(e.name, e.attempt) for e in ledger.trial_log] == [
        ("h_a", None),
        ("h_b", None),
        ("h_a", "round:1"),
        ("h_a", "round:2"),
    ]
    assert ledger.trial_index(first) == 1 and ledger.trial_index(first, "round:2") == 4
    assert ledger.trial_index(second) == 2
    with pytest.raises(LedgerError):
        ledger.trial_index(second, "round:1")
    with pytest.raises(LedgerError, match="non-empty"):
        ledger.register_reevaluation(first, "  ")
    changed = first.model_copy(update={"statement": "something else"})
    with pytest.raises(LedgerError, match="new version"):
        ledger.register_reevaluation(changed, "round:3")
    assert not ledger.is_registered(changed)
    assert len(ledger.hypotheses) == 2  # a re-evaluation never adds a hypothesis


def test_knowledge_claims_become_traceable_hypotheses() -> None:
    items = LocalKnowledgeProvider().items
    hypotheses = from_knowledge(items, "kb")
    assert len(hypotheses) == len(items)
    assert all(
        h.origin is HypothesisOrigin.KNOWLEDGE and len(h.origin_refs) == 1 for h in hypotheses
    )


def test_llm_drafts_are_recorded_validated_and_need_review() -> None:
    output = {
        "name": "h_llm_1",
        "statement": "volume spikes precede reversals",
        "expected_direction": "negative",
        "minimum_meaningful_effect": "5 bp",
    }
    provider = ScriptedLLMProvider([output], clock=lambda: NOW)
    draft = from_llm(provider, "propose one hypothesis", {"topic": "volume"}, "llm_fam")
    assert draft.call.called_at == NOW and len(provider.calls) == 1
    ledger = TrialLedger()
    with pytest.raises(LedgerError, match="reviewed"):
        ledger.register(draft.hypothesis)
    with pytest.raises(LedgerError, match="not been reviewed"):
        ledger.register_draft(draft)
    assert ledger.register_draft(replace(draft, reviewed=True))


def test_a_malformed_llm_output_is_refused() -> None:
    provider = ScriptedLLMProvider([{"name": "x"}], clock=lambda: NOW)
    with pytest.raises(ValueError, match="not a hypothesis draft"):
        from_llm(provider, "p", {}, "fam")

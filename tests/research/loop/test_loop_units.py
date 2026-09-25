"""Unit checks of the loop's research helpers (ADR-0049 W2): review identities, record numbers,
pre-registered trial points. No market data, no thresholds."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.domain.research import Hypothesis, HypothesisOrigin
from plugins.llm import ScriptedLLMProvider
from research.hypotheses import HypothesisDraft, from_llm
from research.loop import OosUnsealBudget, ReviewQueue
from research.loop.segment import RECORD_QUANTUM, decimal_text, trial_point
from tests.research.loop.loop_fixtures import T0, llm_output


def _draft(index: int = 0) -> HypothesisDraft:
    provider = ScriptedLLMProvider([llm_output(index, 60)], clock=lambda: T0)
    return from_llm(provider, "propose", {"round": 0}, "family")


def test_review_queue_refuses_automation_identities_and_records_the_human() -> None:
    queue = ReviewQueue()
    queue.bind_loop_actor("research_loop:my_loop")
    draft = _draft()
    assert queue.enqueue(draft)
    key = "h_llm_0@1.0.0"
    for bad in ("", "   ", "research_loop:my_loop", "research_loop:other_loop"):
        with pytest.raises(ValueError):
            queue.approve(key, reviewer=bad)
    assert queue.pending == (key,) and not queue.approvals
    queue.approve(key, reviewer="  alice  ")
    [approval] = queue.approvals
    assert approval.reviewer == "alice" and approval.draft_hash == draft.hypothesis.content_hash()
    assert approval.call_hash == draft.call.content_hash()
    with pytest.raises(ValueError, match="already approved"):
        queue.approve(key, reviewer="bob")
    [reviewed] = queue.reviewed_untaken()
    assert queue.reviewer_of(reviewed) == "alice"


def test_record_numbers_are_quantized_decimal_text() -> None:
    assert decimal_text(0.1) == "0.100000000000"
    assert decimal_text(1 / 3) == str(Decimal("0.333333333333"))
    assert decimal_text(Decimal("2.5")) == "2.500000000000"
    assert decimal_text(float("inf")) == "inf" and decimal_text(float("nan")) == "nan"
    assert decimal_text(None) is None
    assert Decimal(decimal_text(123456789.123456789) or "0").as_tuple().exponent == (
        RECORD_QUANTUM.as_tuple().exponent
    )
    with pytest.raises(TypeError):
        decimal_text(True)


def _hypothesis(*conditions: str) -> Hypothesis:
    return Hypothesis(
        name="h_point",
        version="1.0.0",
        family_id="f",
        statement="s",
        conditions=conditions,
        expected_direction="higher",
        minimum_meaningful_effect="m",
        origin=HypothesisOrigin.HUMAN,
    )


def test_a_trial_point_is_exactly_what_the_hypothesis_registered() -> None:
    point = trial_point(
        _hypothesis("strategy = tsmom_bars@1.0.0", "param lookback = 60", "param long_only = true")
    )
    assert point.strategy == "tsmom_bars@1.0.0"
    assert dict(point.overrides) == {"lookback": 60, "long_only": True}
    for conditions in (
        (),
        ("param lookback = 60",),
        ("strategy = a@1.0.0", "strategy = b@1.0.0"),
        ("strategy = a@1.0.0", "lag_minutes = 1"),
        ("strategy = a@1.0.0", "param x = 1", "param x = 2"),
    ):
        with pytest.raises(ValueError):
            trial_point(_hypothesis(*conditions))


def test_an_unseal_budget_lists_each_approved_family_with_its_human() -> None:
    """R20: the loop may only unseal a family a human approved, recorded per family."""
    budget = OosUnsealBudget(
        max_unsealings=2, approved_families={" fam_a ": " alice ", "fam_b": "bob"}
    )
    assert budget.approver_of("fam_a") == "alice" and budget.approver_of("fam_b") == "bob"
    assert budget.approver_of("fam_c") is None  # not listed: never unsealed by the loop
    with pytest.raises(TypeError):
        budget.approved_families["fam_c"] = "carol"  # type: ignore[index]
    for families in (
        {},
        {"  ": "alice"},
        {"fam": ""},
        {"fam": "   "},
        {"fam": "research_loop:synthetic_loop"},
        {"fam": "alice", " fam ": "bob"},
    ):
        with pytest.raises(ValueError):
            OosUnsealBudget(max_unsealings=1, approved_families=families)
    with pytest.raises(ValueError, match="positive"):
        OosUnsealBudget(max_unsealings=0, approved_families={"fam": "alice"})

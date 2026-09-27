"""Phase 7: strict LLM draft schema and auditable rejections (roadmap P7; ADR-0040)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from plugins.llm import ScriptedLLMProvider
from research.hypotheses import LlmDraftRejected, from_llm

NOW = datetime(2026, 9, 26, tzinfo=UTC)
VALID: dict[str, Any] = {
    "name": "h_llm_strict",
    "statement": "volume spikes precede reversals",
    "expected_direction": "negative",
    "minimum_meaningful_effect": "5 bp",
    "conditions": ["strategy = tsmom_bars@1.0.0", "param lookback = 60"],
}


def test_a_well_typed_output_is_a_draft_with_its_conditions() -> None:
    provider = ScriptedLLMProvider([VALID], clock=lambda: NOW)
    draft = from_llm(provider, "propose", {"round": 0}, "fam")
    assert draft.hypothesis.conditions == tuple(VALID["conditions"])
    assert draft.call == provider.calls[0] and not draft.reviewed


@pytest.mark.parametrize(
    "output",
    [
        {**VALID, "confidence": "high"},  # an extra key is refused, not dropped
        {**VALID, "statement": 42},  # a number is not coerced into a statement
        {**VALID, "conditions": "strategy = tsmom_bars@1.0.0"},  # a string is not a tuple
        {**VALID, "conditions": [1]},  # nor is a number a condition
        {**VALID, "name": True},
        {**VALID, "name": "Not A Name"},  # schema-valid, but not a valid Hypothesis name
        {"name": "x"},  # missing fields
    ],
)
def test_an_invalid_output_is_rejected_and_still_carries_its_call(output: dict[str, Any]) -> None:
    provider = ScriptedLLMProvider([output], clock=lambda: NOW)
    with pytest.raises(LlmDraftRejected, match="not a hypothesis draft") as caught:
        from_llm(provider, "propose", {"round": 0}, "fam")
    [call] = provider.calls
    assert caught.value.call == call  # the rejected exchange is recorded, not lost
    assert caught.value.reason == str(caught.value)
    assert isinstance(caught.value, ValueError)  # existing ValueError handlers still apply


def test_the_requested_schema_forbids_extra_keys() -> None:
    seen: list[dict[str, Any]] = []

    class _Recording(ScriptedLLMProvider):
        def complete(self, request: Any) -> Any:
            seen.append(request.model_dump(mode="json")["output_schema"])
            return super().complete(request)

    from_llm(_Recording([VALID], clock=lambda: NOW), "propose", {}, "fam")
    assert seen[0]["additionalProperties"] is False

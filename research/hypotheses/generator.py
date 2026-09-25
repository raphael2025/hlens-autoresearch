"""Hypothesis generation from knowledge and from an LLM (roadmap Phase 7; ADR-0040).

- ``from_knowledge``: one hypothesis per KnowledgeItem claim (``origin = knowledge``, the item's ref
  as origin); a claim is a pointer to test, never a conclusion.
- ``from_llm``: the provider's output must validate as hypothesis fields (schema check); the result
  is a **draft** carrying its ``LlmCall`` and ``reviewed = False`` — it cannot be registered until a
  human review marks it (roadmap P7: LLM output is recorded and never decides; 09-security.md §3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from core.contracts.llm import LLMProvider, LlmRequest
from core.domain.research import Hypothesis, HypothesisOrigin, KnowledgeItem, LlmCall

__all__ = ["HypothesisDraft", "from_knowledge", "from_llm"]


class _DraftFields(BaseModel):
    name: str
    statement: str
    expected_direction: str
    minimum_meaningful_effect: str
    conditions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HypothesisDraft:
    hypothesis: Hypothesis
    call: LlmCall
    reviewed: bool = False


def from_knowledge(items: tuple[KnowledgeItem, ...], family_id: str) -> tuple[Hypothesis, ...]:
    return tuple(
        Hypothesis(
            name=f"h_{item.name}",
            version="1.0.0",
            family_id=family_id,
            statement=item.claim,
            conditions=item.conditions,
            expected_direction="as claimed by the source",
            minimum_meaningful_effect="declared by the experiment before running",
            origin=HypothesisOrigin.KNOWLEDGE,
            origin_refs=(item.ref,),
        )
        for item in items
    )


def from_llm(
    provider: LLMProvider, prompt: str, context: dict[str, Any], family_id: str
) -> HypothesisDraft:
    request = LlmRequest.model_validate(
        {"prompt": prompt, "input": context, "output_schema": _DraftFields.model_json_schema()}
    )
    response = provider.complete(request)
    try:
        fields = _DraftFields.model_validate(response.model_dump(mode="json")["output"])
    except ValidationError as exc:
        raise ValueError(f"the LLM output is not a hypothesis draft: {exc}") from None
    hypothesis = Hypothesis(
        name=fields.name,
        version="1.0.0",
        family_id=family_id,
        statement=fields.statement,
        conditions=fields.conditions,
        expected_direction=fields.expected_direction,
        minimum_meaningful_effect=fields.minimum_meaningful_effect,
        origin=HypothesisOrigin.LLM,
    )
    return HypothesisDraft(hypothesis=hypothesis, call=response.call)

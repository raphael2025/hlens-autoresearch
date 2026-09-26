"""Hypothesis generation from knowledge and from an LLM (roadmap Phase 7; ADR-0040).

- ``from_knowledge``: one hypothesis per KnowledgeItem claim (``origin = knowledge``, the item's ref
  as origin); a claim is a pointer to test, never a conclusion.
- ``from_llm``: the provider's output must validate as hypothesis fields (schema check); the result
  is a **draft** carrying its ``LlmCall`` and ``reviewed = False`` — it cannot be registered until a
  human review marks it (roadmap P7: LLM output is recorded and never decides; 09-security.md §3).

Strict drafts (Phase 7 completion, 2026-09-26): the draft schema is strict — an unknown key is
refused (never silently dropped), and every field must already have its JSON type (no coercion,
e.g. a number is not a statement). An output that fails the schema, or whose fields do not make a
valid ``Hypothesis`` (e.g. a name outside the naming pattern), raises ``LlmDraftRejected``: a
``ValueError`` that still carries the exchange's ``LlmCall``, so a caller records the rejected
call (its content hash and refs) alongside the reason — every LLM output is recorded, accepted or
not (roadmap P7).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from core.contracts.llm import LLMProvider, LlmRequest
from core.domain.research import Hypothesis, HypothesisOrigin, KnowledgeItem, LlmCall

__all__ = ["HypothesisDraft", "LlmDraftRejected", "from_knowledge", "from_llm"]


class _DraftFields(BaseModel):
    """The fields an LLM may propose (strict: no extra key, no type coercion)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str
    statement: str
    expected_direction: str
    minimum_meaningful_effect: str
    conditions: tuple[str, ...] = ()


class LlmDraftRejected(ValueError):
    """The LLM output is not a hypothesis draft; ``call`` is the rejected exchange's record."""

    def __init__(self, reason: str, call: LlmCall) -> None:
        super().__init__(reason)
        self.reason = reason
        self.call = call


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
    output = response.model_dump(mode="json")["output"]
    try:  # JSON mode: an array is a tuple, but a number is never a string (strict)
        fields = _DraftFields.model_validate_json(json.dumps(output))
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
    except ValidationError as exc:
        raise LlmDraftRejected(
            f"the LLM output is not a hypothesis draft: {exc}", response.call
        ) from None
    return HypothesisDraft(hypothesis=hypothesis, call=response.call)

"""Phase 7: a rejected LLM output is recorded with its call in the hypothesis stage summary.

roadmap P7 "LLM 输出全部 Schema 校验并记录": the schema-invalid draft is never registered or
enqueued, but its ``LlmCall`` (content hash and prompt / input / output refs) is in the round's
record next to the reason. Runs without a rejected output keep their pinned hashes
(``test_loop_e2e.test_records_without_a_conditional_plan_are_pinned``).

Audit fix (2026-09-26): a ``ContentVerifiedLLM`` whose call content is not retrievable, or not what
was exchanged, raises ``LlmContentUnverified`` carrying the call; the stage records it exactly like
a schema-invalid draft. Any other provider error (a ``ValueError`` included) fails the stage
instead of being recorded as a rejection; an empty prompt is refused when the loop is composed.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from apps.worker import RoundStatus, StageStatus
from core.contracts.llm import LLMProvider, LlmProviderDescriptor, LlmRequest, LlmResponse
from core.domain.research import LlmCall
from infrastructure.content import LocalContentStore
from infrastructure.event_bus import InMemoryEventBus
from plugins.llm import ScriptedLLMProvider
from plugins.synthetic import RandomWalkMarket
from research.loop import ResearchMemory, build_synthetic_loop
from research.loop.llm_content import ContentVerifiedLLM
from research.strategies.failure_registry import FailureRegistry
from tests.research.loop import loop_fixtures as fx


def _run(tmp: Path, llm: LLMProvider) -> tuple[Any, ResearchMemory]:
    memory = ResearchMemory(failures=FailureRegistry(tmp / "failures.jsonl"))
    loop = build_synthetic_loop(
        fx.config(), provider=RandomWalkMarket(), bus=InMemoryEventBus(), memory=memory, llm=llm
    )
    [record] = loop.run_unattended(1)
    return record, memory


def _hypothesis(record: Any) -> Any:
    return next(s for s in record.stages if s.name == "hypothesis")


def _round(tmp: Path, output: dict[str, Any]) -> tuple[Any, ScriptedLLMProvider, ResearchMemory]:
    llm = ScriptedLLMProvider([output], clock=lambda: fx.T0)
    record, memory = _run(tmp, llm)
    return _hypothesis(record).summary, llm, memory


def _assert_rejected_with_call(summary: Any, call: LlmCall, memory: ResearchMemory) -> None:
    llm_summary = summary["llm"]
    assert set(llm_summary) == {"rejected", "call_hash", "call"}
    assert llm_summary["call_hash"] == call.content_hash()
    assert llm_summary["call"] == call.model_dump(mode="json")
    assert summary["pending_reviews"] == [] and memory.reviews.pending == ()
    assert all(h.name != "h_llm_0" for h in memory.ledger.hypotheses)


def test_a_rejected_output_records_its_call_and_is_never_registered(tmp_path: Path) -> None:
    output = {**fx.llm_output(0, 240), "confidence": "high"}  # an extra key: strict refusal
    summary, llm, memory = _round(tmp_path, output)
    [call] = llm.calls
    llm_summary = summary["llm"]
    assert "not a hypothesis draft" in llm_summary["rejected"]
    assert llm_summary["call_hash"] == call.content_hash()
    assert llm_summary["call"] == call.model_dump(mode="json")
    assert llm_summary["call"]["output"]["sha256"] == call.output.sha256
    assert "draft" not in llm_summary and summary["pending_reviews"] == []
    assert all(h.name != "h_llm_0" for h in memory.ledger.hypotheses)
    assert memory.reviews.pending == ()


def test_an_accepted_output_has_no_rejection_keys(tmp_path: Path) -> None:
    summary, llm, _ = _round(tmp_path, fx.llm_output(0, 240))
    assert set(summary["llm"]) == {"draft", "draft_hash", "call_hash", "enqueued_for_review"}
    assert summary["llm"]["call_hash"] == llm.calls[0].content_hash()


def test_unretrievable_call_content_is_recorded_with_its_call(tmp_path: Path) -> None:
    """In-memory refs (no store) never resolve: ``LlmContentUnverified`` carries the call."""
    inner = ScriptedLLMProvider([fx.llm_output(0, 240)], clock=lambda: fx.T0)
    llm = ContentVerifiedLLM(inner, LocalContentStore(tmp_path / "blobs"))
    record, memory = _run(tmp_path, llm)
    stage = _hypothesis(record)
    assert stage.status is StageStatus.COMPLETED and record.status is RoundStatus.COMPLETED
    [call] = inner.calls
    assert "not retrievable" in stage.summary["llm"]["rejected"]
    _assert_rejected_with_call(stage.summary, call, memory)


class _Lying:
    """TEST ONLY: answers, but its call names the stored content of another exchange."""

    def __init__(self, store: LocalContentStore) -> None:
        outputs = [fx.llm_output(0, 240), fx.llm_output(0, 240)]
        self.inner = ScriptedLLMProvider(outputs, clock=lambda: fx.T0, store=store)

    @property
    def descriptor(self) -> LlmProviderDescriptor:
        return self.inner.descriptor

    def complete(self, request: LlmRequest) -> LlmResponse:
        stored = self.inner.complete(request.model_copy(update={"prompt": "something else"}))
        return self.inner.complete(request).model_copy(update={"call": stored.call})


def test_content_that_is_not_what_was_exchanged_is_recorded_with_its_call(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path / "blobs")
    lying = _Lying(store)
    record, memory = _run(tmp_path, ContentVerifiedLLM(lying, store))
    stage = _hypothesis(record)
    assert stage.status is StageStatus.COMPLETED
    recorded = lying.inner.calls[0]  # the call the response names (the other exchange's)
    assert "the stored LLM prompt is not what was exchanged" in stage.summary["llm"]["rejected"]
    _assert_rejected_with_call(stage.summary, recorded, memory)


class _Failing:
    """TEST ONLY: a provider whose own error is a plain ``ValueError`` (not a rejection)."""

    def __init__(self) -> None:
        self._descriptor = ScriptedLLMProvider([], clock=lambda: fx.T0).descriptor

    @property
    def descriptor(self) -> LlmProviderDescriptor:
        return self._descriptor

    def complete(self, request: LlmRequest) -> LlmResponse:
        raise ValueError("the provider broke")


@pytest.mark.parametrize("verified", [False, True])
def test_another_provider_value_error_fails_the_stage(tmp_path: Path, verified: bool) -> None:
    """Only the typed rejections are recorded; a provider's own ValueError is not swallowed."""
    llm: LLMProvider = _Failing()
    if verified:
        llm = ContentVerifiedLLM(llm, LocalContentStore(tmp_path / "blobs"))
    record, memory = _run(tmp_path, llm)
    stage = _hypothesis(record)
    assert stage.status is StageStatus.FAILED and record.status is RoundStatus.FAILED
    assert "the provider broke" in (stage.error or "")
    assert memory.reviews.pending == ()


def test_an_empty_prompt_is_refused_when_the_loop_is_composed(tmp_path: Path) -> None:
    """Formerly recorded as a rejection every round (the LlmRequest refused it); now a
    configuration error before any round runs."""
    memory = ResearchMemory(failures=FailureRegistry(tmp_path / "failures.jsonl"))
    llm = ScriptedLLMProvider([fx.llm_output(0, 240)], clock=lambda: fx.T0)
    with pytest.raises(ValueError, match="LLM prompt must be a non-empty str"):
        build_synthetic_loop(
            replace(fx.config(), llm_prompt=""),
            provider=RandomWalkMarket(),
            bus=InMemoryEventBus(),
            memory=memory,
            llm=llm,
        )
    assert llm.calls == ()

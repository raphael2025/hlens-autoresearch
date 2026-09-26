"""Phase 7: a rejected LLM output is recorded with its call in the hypothesis stage summary.

roadmap P7 "LLM 输出全部 Schema 校验并记录": the schema-invalid draft is never registered or
enqueued, but its ``LlmCall`` (content hash and prompt / input / output refs) is in the round's
record next to the reason. Runs without a rejected output keep their pinned hashes
(``test_loop_e2e.test_records_without_a_conditional_plan_are_pinned``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from infrastructure.event_bus import InMemoryEventBus
from plugins.llm import ScriptedLLMProvider
from plugins.synthetic import RandomWalkMarket
from research.loop import ResearchMemory, build_synthetic_loop
from research.strategies.failure_registry import FailureRegistry
from tests.research.loop import loop_fixtures as fx


def _round(tmp: Path, output: dict[str, Any]) -> tuple[Any, ScriptedLLMProvider, ResearchMemory]:
    llm = ScriptedLLMProvider([output], clock=lambda: fx.T0)
    memory = ResearchMemory(failures=FailureRegistry(tmp / "failures.jsonl"))
    loop = build_synthetic_loop(
        fx.config(), provider=RandomWalkMarket(), bus=InMemoryEventBus(), memory=memory, llm=llm
    )
    [record] = loop.run_unattended(1)
    stage = next(s for s in record.stages if s.name == "hypothesis")
    return stage.summary, llm, memory


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

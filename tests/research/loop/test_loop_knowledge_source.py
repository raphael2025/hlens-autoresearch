"""Phase 7: an optional KnowledgeProvider search as a source of the loop's hypothesis stage.

The query hash and the ``KnowledgeResult.result_hash`` are recorded as the origin of the
hypotheses the search yields (stage summary and lifecycle evidence). Without a source every record
and fingerprint is unchanged (pinned:
``test_loop_e2e.test_records_without_a_conditional_plan_are_pinned``).

TEST ONLY numbers: see ``loop_fixtures``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from apps.worker import StageStatus
from core.contracts.knowledge import KnowledgeQuery, KnowledgeResult
from core.domain.base import content_hash
from core.domain.research import EvidenceLevel, KnowledgeItem
from infrastructure.event_bus import InMemoryEventBus
from plugins.knowledge import LocalKnowledgeProvider
from plugins.llm import ScriptedLLMProvider
from plugins.synthetic import RandomWalkMarket
from research.hypotheses import KnowledgeSource
from research.loop import ResearchMemory, build_synthetic_loop, loop_fingerprint
from research.strategies.failure_registry import FailureRegistry
from tests.research.loop import loop_fixtures as fx

LONG_ONLY = KnowledgeItem(
    name="k_search_tsmom_long_only",
    version="1.0.0",
    created_at=fx.T0,
    source="test://knowledge-search",
    license="test-only",
    claim="a long-only 60-bar time-series momentum beats costs on minute bars",
    conditions=("strategy = tsmom_bars@1.0.0", "param lookback = 60", "param long_only = true"),
    evidence_level=EvidenceLevel.E0_ANECDOTE,
)
QUERY = KnowledgeQuery(terms=("time-series momentum",), limit=10)


def _provider(tmp: Path) -> LocalKnowledgeProvider:
    """Holds the declared fixture item ``k_tsmom_lookback_60`` too (found by the same query)."""
    items = [fx.knowledge(60), LONG_ONLY]
    (tmp / "items").mkdir()
    (tmp / "items" / "items.json").write_text(
        json.dumps([item.model_dump(mode="json") for item in items]), encoding="utf-8"
    )
    return LocalKnowledgeProvider(tmp / "items")


def _config(source: KnowledgeSource | None) -> Any:
    wiring = replace(fx.wiring(evolution=False), knowledge_source=source)
    return replace(fx.config(lookbacks=(60,), loop_wiring=wiring), max_new_hypotheses_per_round=2)


def _run(tmp: Path, source: KnowledgeSource) -> tuple[Any, Any, ResearchMemory]:
    memory = ResearchMemory(failures=FailureRegistry(tmp / "failures.jsonl"))
    llm = ScriptedLLMProvider([fx.llm_output(0, None)], clock=lambda: fx.T0)
    loop = build_synthetic_loop(
        _config(source), provider=RandomWalkMarket(), bus=InMemoryEventBus(), memory=memory, llm=llm
    )
    [record] = loop.run_unattended(1)
    return loop, next(s for s in record.stages if s.name == "hypothesis"), memory


def test_searched_hypotheses_record_the_query_and_result_hash_as_their_origin(
    tmp_path: Path,
) -> None:
    provider = _provider(tmp_path)
    loop, stage, memory = _run(tmp_path, KnowledgeSource(provider, QUERY))
    result = provider.search(QUERY)
    searched = "hypothesis:h_k_search_tsmom_long_only@1.0.0"
    declared = "hypothesis:h_k_tsmom_lookback_60@1.0.0"
    assert stage.summary["registered"] == [declared, searched]  # declared first, never twice
    assert stage.summary["knowledge_search"] == {
        "provider": "hlens_knowledge_local@1.0.0",
        "query_hash": QUERY.content_hash(),
        "result_hash": result.result_hash,
        "items": [
            "knowledge:k_search_tsmom_long_only@1.0.0",
            "knowledge:k_tsmom_lookback_60@1.0.0",
        ],
        "registered": [searched],  # the declared item keeps its declared origin
    }
    evidence = {
        str(history.subject): history.transitions[0].evidence for history in loop.guard.histories
    }
    origin = (f"knowledge_query:{QUERY.content_hash()}", f"knowledge_result:{result.result_hash}")
    assert evidence[searched][1:] == origin
    assert not set(origin) & set(evidence[declared])
    [outcome] = [o for o in memory.trials if str(o.hypothesis.ref) == searched]
    assert outcome.error is None and outcome.request_params["long_only"] is True


class _OtherQuery:
    """TEST ONLY: answers another query than the one declared."""

    def __init__(self, inner: LocalKnowledgeProvider) -> None:
        self._inner = inner
        self.descriptor = inner.descriptor

    def search(self, query: KnowledgeQuery) -> KnowledgeResult:
        return self._inner.search(query.model_copy(update={"limit": 1}))


def test_a_search_answering_another_query_fails_the_stage_and_registers_nothing(
    tmp_path: Path,
) -> None:
    _, stage, memory = _run(tmp_path, KnowledgeSource(_OtherQuery(_provider(tmp_path)), QUERY))
    assert stage.status is StageStatus.FAILED
    assert memory.ledger.trials(fx.FAMILY) == 0


def test_the_fingerprint_binds_the_source_only_when_set(tmp_path: Path) -> None:
    plain = loop_fingerprint(_config(None))
    assert "knowledge_source" not in plain
    source = KnowledgeSource(_provider(tmp_path), QUERY)
    bound = loop_fingerprint(_config(source))
    assert bound.pop("knowledge_source") == source.payload()
    assert content_hash(bound) == content_hash(plain)

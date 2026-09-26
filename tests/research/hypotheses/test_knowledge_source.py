"""Phase 7: a declared KnowledgeProvider search as a hypothesis source (KnowledgeSource)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.contracts.knowledge import (
    KnowledgeProviderDescriptor,
    KnowledgeQuery,
    KnowledgeResult,
)
from core.domain.research import EvidenceLevel, HypothesisOrigin, KnowledgeItem
from plugins.knowledge import LocalKnowledgeProvider
from research.hypotheses import KnowledgeSource

NOW = datetime(2026, 9, 26, tzinfo=UTC)


def _item(name: str, claim: str) -> KnowledgeItem:
    return KnowledgeItem(
        name=name,
        version="1.0.0",
        created_at=NOW,
        source="test://knowledge",
        license="test-only",
        claim=claim,
        conditions=("strategy = tsmom_bars@1.0.0", "param lookback = 60"),
        evidence_level=EvidenceLevel.E1_EXAMPLE,
    )


@pytest.fixture
def provider(tmp_path: Path) -> LocalKnowledgeProvider:
    items = [
        _item("k_momentum_a", "minute momentum persists"),
        _item("k_momentum_b", "hourly momentum persists"),
        _item("k_reversal", "minute returns revert"),
    ]
    path = tmp_path / "items.json"
    path.write_text(json.dumps([i.model_dump(mode="json") for i in items]), encoding="utf-8")
    return LocalKnowledgeProvider(tmp_path)


def test_a_search_yields_knowledge_hypotheses_with_its_hashes(
    provider: LocalKnowledgeProvider,
) -> None:
    query = KnowledgeQuery(terms=("momentum",), limit=10)
    search = KnowledgeSource(provider, query).search("fam")
    direct = provider.search(query)
    assert search.query_hash == query.content_hash() == direct.query_hash
    assert search.result_hash == direct.result_hash
    assert search.provider == provider.descriptor.plugin_key
    assert [str(i.ref) for i in search.items] == [
        "knowledge:k_momentum_a@1.0.0",
        "knowledge:k_momentum_b@1.0.0",
    ]
    for hypothesis, item in zip(search.hypotheses, search.items, strict=True):
        assert hypothesis.origin is HypothesisOrigin.KNOWLEDGE
        assert hypothesis.origin_refs == (item.ref,) and hypothesis.family_id == "fam"
    assert search.summary()["result_hash"] == direct.result_hash
    assert search.evidence() == (
        f"knowledge_query:{query.content_hash()}",
        f"knowledge_result:{direct.result_hash}",
    )


def test_the_payload_binds_the_provider_identity_and_the_query(
    provider: LocalKnowledgeProvider,
) -> None:
    a = KnowledgeSource(provider, KnowledgeQuery(terms=("momentum",), limit=10)).payload()
    b = KnowledgeSource(provider, KnowledgeQuery(terms=("reversal",), limit=10)).payload()
    assert a["provider"] == b["provider"] == "hlens_knowledge_local@1.0.0"
    assert a["descriptor"] == provider.descriptor.content_hash()
    assert a["query"] != b["query"]


class _Wrong:
    """TEST ONLY: a provider whose answers do not belong to the declared query / identity."""

    def __init__(self, inner: LocalKnowledgeProvider, mode: str) -> None:
        self._inner, self._mode = inner, mode

    @property
    def descriptor(self) -> KnowledgeProviderDescriptor:
        return self._inner.descriptor

    def search(self, query: KnowledgeQuery) -> KnowledgeResult:
        if self._mode == "other_query":
            return self._inner.search(query.model_copy(update={"terms": ("reversal",)}))
        result = self._inner.search(query)
        if self._mode == "other_provider":
            return KnowledgeResult.build(query, "someone_else@1.0.0", result.items)
        return KnowledgeResult.model_construct(  # a tampered result: hash does not match
            query_hash=result.query_hash,
            provider=result.provider,
            items=result.items[:1],
            result_hash=result.result_hash,
        )


@pytest.mark.parametrize(
    ("mode", "match"),
    [
        ("other_query", "another query"),
        ("other_provider", "not the declared provider"),
        ("tampered", "result_hash"),
    ],
)
def test_a_result_that_is_not_the_declared_search_is_refused(
    provider: LocalKnowledgeProvider, mode: str, match: str
) -> None:
    source = KnowledgeSource(_Wrong(provider, mode), KnowledgeQuery(terms=("momentum",), limit=10))
    with pytest.raises(ValueError, match=match):
        source.search("fam")


def test_a_source_declares_a_query() -> None:
    with pytest.raises(TypeError):
        KnowledgeSource(LocalKnowledgeProvider(), {"terms": ["x"]})  # type: ignore[arg-type]

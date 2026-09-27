"""Phase 0.5: the local KnowledgeProvider and the seed knowledge base (ADR-0034)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.contracts.knowledge import KnowledgeProviderError, KnowledgeQuery, KnowledgeResult
from core.domain.research import KnowledgeStatus
from plugins.knowledge import LocalKnowledgeProvider
from tests.contract_suites import knowledge as suite


@pytest.mark.parametrize("check", suite.KNOWLEDGE_CHECKS, ids=lambda c: c.__name__)
def test_the_local_provider_passes_the_suite(check: suite.KnowledgeCheck) -> None:
    check(LocalKnowledgeProvider())


def test_the_seed_base_is_unverified_claims_with_provenance() -> None:
    items = LocalKnowledgeProvider().items
    assert len(items) >= 6
    assert all(item.status is KnowledgeStatus.UNVERIFIED for item in items)
    assert {item.name.split("_")[0] for item in items} >= {"strategy", "factor", "risk", "state"}


def test_retrieval_by_library_and_terms() -> None:
    provider = LocalKnowledgeProvider()
    result = provider.search(KnowledgeQuery(terms=("momentum",), name_prefix="strategy_"))
    assert isinstance(result, KnowledgeResult)
    assert [item.name for item in result.items] == [
        "strategy_crypto_time_series_momentum",
        "strategy_time_series_momentum",
    ]


def _write(tmp_path: Path, items: list[dict[str, object]]) -> Path:
    (tmp_path / "items.json").write_text(json.dumps(items), encoding="utf-8")
    return tmp_path


BASE: dict[str, object] = {
    "kind": "knowledge",
    "name": "strategy_x",
    "version": "1.0.0",
    "created_at": "2026-09-25T00:00:00Z",
    "source": "Someone (2020). A paper.",
    "license": "citation only",
    "claim": "Something holds.",
    "evidence_level": "E1",
}


@pytest.mark.parametrize(
    ("items", "match"),
    [
        ([dict(BASE, license=" ")], "no source or licence"),
        ([BASE, BASE], "duplicate"),
        ([dict(BASE, evidence_level="E9")], "invalid knowledge item"),
    ],
)
def test_items_without_provenance_or_valid_shape_fail_closed(
    tmp_path: Path, items: list[dict[str, object]], match: str
) -> None:
    with pytest.raises(KnowledgeProviderError, match=match):
        LocalKnowledgeProvider(_write(tmp_path, items))

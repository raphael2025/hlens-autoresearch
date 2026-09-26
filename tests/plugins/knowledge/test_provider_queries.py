"""Phase 0.5: LocalKnowledgeProvider query filters and error paths (ADR-0034)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.contracts.knowledge import KnowledgeProviderError, KnowledgeQuery
from core.domain.research import EvidenceLevel, KnowledgeStatus
from plugins.knowledge import LocalKnowledgeProvider


def _item(
    name: str, level: str, status: str = "unverified", claim: str = "Claim."
) -> dict[str, object]:
    return {
        "kind": "knowledge",
        "name": name,
        "version": "1.0.0",
        "created_at": "2026-09-25T00:00:00Z",
        "source": "S (2020).",
        "license": "citation only",
        "claim": claim,
        "evidence_level": level,
        "status": status,
    }


@pytest.fixture
def provider(tmp_path: Path) -> LocalKnowledgeProvider:
    items = [
        _item("factor_a", "E1", claim="Momentum in small caps."),
        _item("factor_b", "E3", "supported", claim="Momentum in large caps."),
        _item("risk_c", "E4", "contradicted", claim="Volatility scaling helps."),
    ]
    (tmp_path / "items.json").write_text(json.dumps(items), encoding="utf-8")
    return LocalKnowledgeProvider(tmp_path)


def _names(provider: LocalKnowledgeProvider, query: KnowledgeQuery) -> list[str]:
    return [item.name for item in provider.search(query).items]


def test_filters_by_evidence_status_limit_and_terms(provider: LocalKnowledgeProvider) -> None:
    assert _names(provider, KnowledgeQuery()) == ["factor_a", "factor_b", "risk_c"]
    assert _names(provider, KnowledgeQuery(evidence_at_least=EvidenceLevel("E3"))) == [
        "factor_b",
        "risk_c",
    ]
    assert _names(provider, KnowledgeQuery(statuses=(KnowledgeStatus.SUPPORTED,))) == ["factor_b"]
    assert _names(provider, KnowledgeQuery(limit=1)) == ["factor_a"]
    assert _names(provider, KnowledgeQuery(terms=("MOMENTUM", "large"))) == ["factor_b"]
    assert _names(provider, KnowledgeQuery(terms=("absent",))) == []
    assert _names(provider, KnowledgeQuery(name_prefix="risk_")) == ["risk_c"]


def test_search_is_deterministic(provider: LocalKnowledgeProvider) -> None:
    query = KnowledgeQuery(terms=("momentum",))
    assert provider.search(query) == provider.search(query)


def test_search_refuses_a_non_query(provider: LocalKnowledgeProvider) -> None:
    with pytest.raises(KnowledgeProviderError, match="KnowledgeQuery"):
        provider.search({"terms": ["x"]})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs", [{"terms": (" ",)}, {"limit": 0}, {"limit": 1001}, {"evidence_at_least": "E9"}]
)
def test_invalid_queries_are_refused_by_the_contract(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        KnowledgeQuery(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("{not json", "unreadable item file"),
        ('{"kind": "knowledge"}', "expected a list"),
    ],
)
def test_unreadable_or_mis_shaped_files_fail_closed(
    tmp_path: Path, content: str, match: str
) -> None:
    (tmp_path / "bad.json").write_text(content, encoding="utf-8")
    with pytest.raises(KnowledgeProviderError, match=match):
        LocalKnowledgeProvider(tmp_path)


def test_a_duplicate_across_files_fails_closed(tmp_path: Path) -> None:
    for name in ("a.json", "b.json"):
        (tmp_path / name).write_text(json.dumps([_item("factor_a", "E1")]), encoding="utf-8")
    with pytest.raises(KnowledgeProviderError, match="duplicate"):
        LocalKnowledgeProvider(tmp_path)


def test_an_empty_directory_is_an_empty_base(tmp_path: Path) -> None:
    provider = LocalKnowledgeProvider(tmp_path)
    assert provider.items == () and provider.search(KnowledgeQuery()).items == ()

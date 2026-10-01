"""ADR-0055: retrieval by tags (AND) and assets (OR, exact) in LocalKnowledgeProvider (V5).

Assets are research-scope identifiers (an asset class or a base asset), never exchange symbols,
listings or market data; matching is exact token equality — never substring, prefix or case folding.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.contracts.knowledge import KnowledgeProviderError, KnowledgeQuery, KnowledgeResult
from core.domain.base import CONTRACT_SCHEMA_VERSION
from core.domain.research import EvidenceLevel, KnowledgeItem, KnowledgeStatus
from plugins.knowledge import LocalKnowledgeProvider, LocalKnowledgeStore
from tests.contract_suites import knowledge as suite


def _item(
    name: str,
    *,
    tags: list[str] | None = None,
    assets: list[str] | None = None,
    level: str = "E2",
    status: str = "unverified",
    claim: str = "Momentum claim.",
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "kind": "knowledge",
        "name": name,
        "version": "1.0.0",
        "created_at": "2026-09-26T00:00:00Z",
        "source": "Fixture (not a real citation)",
        "license": "fixture; no full text",
        "claim": claim,
        "evidence_level": level,
        "status": status,
    }
    if tags is not None:
        raw["tags"] = tags
    if assets is not None:
        raw["assets"] = assets
    return raw


FIXTURE = [
    _item("factor_btc", tags=["momentum"], assets=["btc", "crypto"]),
    _item("factor_btcdom", tags=["dominance", "momentum"], assets=["btcdom"]),
    _item("factor_wbtc", tags=["momentum", "wrapped"], assets=["wbtc"]),
    _item("risk_eth", tags=["volatility_management"], assets=["crypto", "eth"], level="E3"),
    _item("strategy_equity", tags=["momentum", "trend"], assets=["equity"], status="supported"),
    _item("state_untagged", claim="Price deviations across btc venues."),
]


@pytest.fixture
def provider(tmp_path: Path) -> LocalKnowledgeProvider:
    (tmp_path / "items.json").write_text(json.dumps(FIXTURE), encoding="utf-8")
    return LocalKnowledgeProvider(tmp_path)


def _names(provider: LocalKnowledgeProvider, query: KnowledgeQuery) -> list[str]:
    return [item.name for item in provider.search(query).items]


def test_the_provider_is_version_1_1_0() -> None:
    assert LocalKnowledgeProvider().descriptor.plugin_key == "hlens_knowledge_local@1.1.0"


def test_assets_match_exactly_never_by_substring(provider: LocalKnowledgeProvider) -> None:
    assert _names(provider, KnowledgeQuery(assets_any=("btc",))) == ["factor_btc"]
    assert _names(provider, KnowledgeQuery(assets_any=("btcdom",))) == ["factor_btcdom"]
    assert _names(provider, KnowledgeQuery(assets_any=("bt",))) == []
    assert _names(provider, KnowledgeQuery(assets_any=("dom",))) == []
    # contrast: the free-text term filter is a substring match and hits every btc-ish item
    assert _names(provider, KnowledgeQuery(terms=("btc",))) == [
        "factor_btc",
        "factor_btcdom",
        "factor_wbtc",
        "state_untagged",
    ]


def test_assets_any_is_or(provider: LocalKnowledgeProvider) -> None:
    assert _names(provider, KnowledgeQuery(assets_any=("btc", "eth"))) == [
        "factor_btc",
        "risk_eth",
    ]
    assert _names(provider, KnowledgeQuery(assets_any=("crypto",))) == ["factor_btc", "risk_eth"]


def test_tags_all_is_and(provider: LocalKnowledgeProvider) -> None:
    assert _names(provider, KnowledgeQuery(tags_all=("momentum",))) == [
        "factor_btc",
        "factor_btcdom",
        "factor_wbtc",
        "strategy_equity",
    ]
    assert _names(provider, KnowledgeQuery(tags_all=("momentum", "trend"))) == ["strategy_equity"]
    assert _names(provider, KnowledgeQuery(tags_all=("momentum", "volatility_management"))) == []
    assert _names(provider, KnowledgeQuery(tags_all=("moment",))) == []  # no prefix match


def test_tags_are_not_assets_and_assets_are_not_tags(provider: LocalKnowledgeProvider) -> None:
    assert _names(provider, KnowledgeQuery(tags_all=("btc",))) == []
    assert _names(provider, KnowledgeQuery(assets_any=("momentum",))) == []


def test_the_new_filters_combine_with_the_others_before_the_limit(
    provider: LocalKnowledgeProvider,
) -> None:
    both = KnowledgeQuery(tags_all=("momentum",), assets_any=("btc", "equity"))
    assert _names(provider, both) == ["factor_btc", "strategy_equity"]
    assert _names(
        provider,
        KnowledgeQuery(tags_all=("momentum",), statuses=(KnowledgeStatus.SUPPORTED,)),
    ) == ["strategy_equity"]
    assert _names(
        provider,
        KnowledgeQuery(assets_any=("crypto",), evidence_at_least=EvidenceLevel.E3_OUT_OF_SAMPLE),
    ) == ["risk_eth"]
    assert _names(provider, KnowledgeQuery(tags_all=("momentum",), name_prefix="factor_")) == [
        "factor_btc",
        "factor_btcdom",
        "factor_wbtc",
    ]
    # the limit applies after the filters: the first *matching* item, not the first item
    assert _names(provider, KnowledgeQuery(assets_any=("eth",), limit=1)) == ["risk_eth"]


def test_empty_filters_do_not_restrict(provider: LocalKnowledgeProvider) -> None:
    assert _names(provider, KnowledgeQuery()) == sorted(item["name"] for item in FIXTURE)


def test_a_tagged_answer_is_a_valid_deterministic_result(
    provider: LocalKnowledgeProvider,
) -> None:
    query = KnowledgeQuery(tags_all=("momentum",), assets_any=("btc",))
    result = provider.search(query)
    assert KnowledgeResult.model_validate_json(result.model_dump_json()) == result
    assert result.query_hash == query.content_hash()
    assert provider.search(query).result_hash == result.result_hash
    assert result.schema_version == CONTRACT_SCHEMA_VERSION


@pytest.mark.parametrize("check", suite.KNOWLEDGE_CHECKS, ids=lambda c: c.__name__)
def test_a_tagged_base_passes_the_provider_suite(
    provider: LocalKnowledgeProvider, check: suite.KnowledgeCheck
) -> None:
    check(provider)


class _SubstringProvider:
    """A broken provider that matches assets / tags by substring (what V5 forbids)."""

    def __init__(self, inner: LocalKnowledgeProvider) -> None:
        self._inner = inner
        self.descriptor = inner.descriptor

    def search(self, query: KnowledgeQuery) -> KnowledgeResult:
        loose = query.model_copy(update={"tags_all": (), "assets_any": ()})
        hits = tuple(
            item
            for item in self._inner.search(loose.model_copy(update={"limit": 1000})).items
            if all(any(tag in own for own in item.tags) for tag in query.tags_all)
            and (
                not query.assets_any
                or any(asset in own for asset in query.assets_any for own in item.assets)
            )
        )
        return KnowledgeResult.build(query, self.descriptor.plugin_key, hits[: query.limit])


def test_the_suite_catches_a_substring_matcher(provider: LocalKnowledgeProvider) -> None:
    with pytest.raises(suite.KnowledgeSuiteFailure, match="exact"):
        suite.check_tag_and_asset_filters(_SubstringProvider(provider))


@pytest.mark.parametrize(
    "bad",
    [
        {"tags": ["Momentum"]},
        {"assets": ["BTCUSDT"]},
        {"assets": ["btc", "btc"]},
        {"tags": ["trend", "momentum"]},
        {"tags": ["momentum"], "schema_version": "2.1.0"},
    ],
)
def test_a_non_canonical_or_misversioned_item_file_fails_closed(
    tmp_path: Path, bad: dict[str, Any]
) -> None:
    (tmp_path / "items.json").write_text(json.dumps([{**_item("factor_x"), **bad}]), "utf-8")
    with pytest.raises(KnowledgeProviderError, match="invalid knowledge item"):
        LocalKnowledgeProvider(tmp_path)


def test_the_reviewed_write_path_keeps_tags_and_assets(tmp_path: Path) -> None:
    """ADR-0058: a tagged item goes through the reviewed path and verifies as written."""
    items_dir = tmp_path / "items"
    items_dir.mkdir()
    (items_dir / "seed.json").write_text(json.dumps([_item("state_seed")]), "utf-8")
    store = LocalKnowledgeStore(items_dir)
    tagged = KnowledgeItem.model_validate(
        _item("factor_tagged", tags=["momentum"], assets=["btc", "crypto"])
    )
    written = store.add(tagged, reviewed_by="a human reviewer")
    assert written.created and written.item == tagged
    review = store.review_of("factor_tagged", "1.0.0")
    assert review.item_hash == tagged.content_hash()
    found = LocalKnowledgeProvider(items_dir).search(KnowledgeQuery(assets_any=("btc",)))
    assert [item.name for item in found.items] == ["factor_tagged"]
    assert found.items[0].schema_version == CONTRACT_SCHEMA_VERSION

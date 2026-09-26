"""v2.1.0 knowledge golden vectors (ADR-0055 V1 / V2; `tests/golden/v2_1_0/README.md`).

Every recorded 2.1.0 knowledge payload must validate with the current models **as its recorded
version** (the envelope is kept, never rewritten), re-serialize byte-identically and hash to the
recorded hashes. The files were generated once at `1cd3284`, before any ADR-0055 change (H6).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from core.contracts.knowledge import KnowledgeQuery, KnowledgeResult
from core.domain.base import Contract, canonical_json
from core.domain.research import KnowledgeItem
from plugins.knowledge import LocalKnowledgeProvider

GOLDEN = Path(__file__).resolve().parent / "golden" / "v2_1_0"
MODELS: dict[str, type[Contract]] = {
    "KnowledgeItem": KnowledgeItem,
    "KnowledgeQuery": KnowledgeQuery,
    "KnowledgeResult": KnowledgeResult,
}
#: The files and their recorded content hashes (pinned here too, so a regenerated file cannot pass).
EXPECTED: dict[str, str] = {
    "knowledge_item.json": "5fd31cb590d78ec1072b6fca604d4ab378e9ac2a9ba301e245384627c13e2571",
    "knowledge_item_minimal.json": (
        "be4c52f9ce0e0f29afe9945ba0ad16155e20f975fdb126a632e3273353545875"
    ),
    "knowledge_query.json": "e99603b49c3f72470037a772a0b5f5486468ee9a555ec3a0d38be16d307d5f8c",
    "knowledge_query_default.json": (
        "ab93980cd243d4bb9f62417842203c70066044806328915ddc052d8f3b55b22b"
    ),
    "knowledge_result.json": "301a9eced6ee606d42091b4acd1c1eb3eeed67c0c47b3efb07087d1a347ebfd2",
}
SEED_HASHES: dict[str, str] = {
    "factor_crypto_market_size_momentum@1.0.0": (
        "1f55ae4fdd50412e81d489d595654bc551a35c40aed5029f6701bfdb8f2b107a"
    ),
    "risk_volatility_managed_portfolios@1.0.0": (
        "c820e822ecdfc704a1ac5fd9165af33a1a9a1f73291956c9eef87c787cb384e7"
    ),
    "risk_volatility_managed_portfolios_out_of_sample@1.0.0": (
        "507692f39d0b212a03ddecf2a152b07c5e9c6bd9965a0224b697e89d0fa3a604"
    ),
    "state_cross_exchange_price_deviations@1.0.0": (
        "5efe53b935efcb145da01ede51d8dbaa97348de53af86cfa29c29d8bfe039f12"
    ),
    "strategy_crypto_time_series_momentum@1.0.0": (
        "aed6ca02bc69a4d235e41a522ef33d2dbaf3c4b6141f7a45c2f55827ab897b22"
    ),
    "strategy_time_series_momentum@1.0.0": (
        "3db0829c0e7c10ae9b2fef89baa1916e978ee48c359892d2009dc182e0769ad9"
    ),
}
SEED_FULL_QUERY_HASH = "a72872c0d1970f44bb72c489415ccd3ac26f3e4bdc382dff882cb0a9dbf4fa49"
SEED_FULL_RESULT_HASH_AT_1_0_0 = "e5ea8dcd5721c8bc75f46547cdd72e522ab375cb6c0ac7795ad8dba5f340ea10"


def _load(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((GOLDEN / name).read_text(encoding="utf-8"))
    return document


def test_the_golden_set_is_exactly_the_pinned_files() -> None:
    present = {path.name for path in GOLDEN.glob("*.json")}
    assert present == {*EXPECTED, "knowledge_seed_hashes.json"}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_recorded_2_1_0_payload_reads_and_hashes_as_recorded(name: str) -> None:
    document = _load(name)
    assert document["content_hash"] == EXPECTED[name]
    assert document["contract_schema_version"] == "2.1.0"
    payload = document["payload"]
    assert payload["schema_version"] == "2.1.0"
    model = MODELS[document["model"]]
    for obj in (model.model_validate(payload), model.model_validate_json(json.dumps(payload))):
        assert obj.schema_version == "2.1.0"  # read as its recorded version, never rewritten
        assert obj.model_dump(mode="json") == payload  # no new key appears in the 2.1.0 shape
        assert obj.content_hash() == EXPECTED[name]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_the_recorded_hash_is_the_documented_rule(name: str) -> None:
    """`content_hash` = SHA-256 of the canonical JSON minus the model's non-semantic fields."""
    document = _load(name)
    model = MODELS[document["model"]]
    semantic = {
        key: value
        for key, value in document["payload"].items()
        if key not in model._non_semantic_fields()
    }
    digest = hashlib.sha256(canonical_json(semantic).encode("utf-8")).hexdigest()
    assert digest == EXPECTED[name]


def test_the_recorded_result_hash_binds_the_recorded_query_and_items() -> None:
    document = _load("knowledge_result.json")
    result = KnowledgeResult.model_validate(document["payload"])
    assert result.result_hash == document["result_hash"]
    assert result.query_hash == EXPECTED["knowledge_query.json"]
    assert [item.content_hash() for item in result.items] == [
        EXPECTED["knowledge_item_minimal.json"],
        EXPECTED["knowledge_item.json"],
    ]


def test_the_reviewed_seed_items_keep_the_hashes_the_2_1_0_code_served() -> None:
    document = _load("knowledge_seed_hashes.json")
    assert document["items"] == SEED_HASHES
    provider = LocalKnowledgeProvider()
    served = {
        f"{item.name}@{item.version}": item.content_hash()
        for item in provider.items
        if f"{item.name}@{item.version}" in SEED_HASHES
    }
    assert served == SEED_HASHES


def test_the_recorded_full_search_query_hash_reproduces_at_its_envelope() -> None:
    document = _load("knowledge_seed_hashes.json")["full_search"]
    assert document["query_hash"] == SEED_FULL_QUERY_HASH
    assert document["result_hash"] == SEED_FULL_RESULT_HASH_AT_1_0_0
    query = KnowledgeQuery.model_validate({**document["query"], "schema_version": "2.1.0"})
    assert query.content_hash() == SEED_FULL_QUERY_HASH


def test_a_changed_payload_does_not_keep_the_recorded_hash() -> None:
    """Negative control: the pins are sensitive to content."""
    document = _load("knowledge_item.json")
    changed = KnowledgeItem.model_validate({**document["payload"], "claim": "Another claim."})
    assert changed.content_hash() != EXPECTED["knowledge_item.json"]


def test_the_recorded_full_search_result_is_rebuilt_from_the_seed_items() -> None:
    """The 1.0.0 provider's recorded answer over the seed is reproduced from today's seed items."""
    document = _load("knowledge_seed_hashes.json")["full_search"]
    query = KnowledgeQuery.model_validate({**document["query"], "schema_version": "2.1.0"})
    seeds = tuple(
        item
        for item in LocalKnowledgeProvider().items
        if f"{item.name}@{item.version}" in SEED_HASHES
    )
    rebuilt = KnowledgeResult.build(query, document["provider"], seeds)
    assert document["provider"] == "hlens_knowledge_local@1.0.0"
    assert rebuilt.result_hash == SEED_FULL_RESULT_HASH_AT_1_0_0

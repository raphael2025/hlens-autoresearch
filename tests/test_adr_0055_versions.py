"""ADR-0055: knowledge tags / assets at contract 2.2.0 — version boundary and hash chain.

Matrix rows (ADR-0055 "验证矩阵"): V3 determinism of 2.2.0 hashes, V4 query / result hash binding,
V6 canonical tokens, V7 the 2.0.0 / 2.1.0 boundary, V8 the registry and the Schema. V1 / V2 (the
2.1.0 golden vectors) live in ``tests/test_v2_1_0_knowledge_golden.py``; V5 (exact matching) in
``tests/plugins/knowledge/test_tags_assets.py``.
"""

from __future__ import annotations

import json
import tomllib
from datetime import UTC, datetime
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts.knowledge import KnowledgeQuery, KnowledgeResult
from core.domain.base import (
    CONTRACT_SCHEMA_VERSION,
    PUBLISHED_CONTRACT_SCHEMA_VERSIONS,
    content_hash,
    contract_schema_version_scope,
)
from core.domain.research import (
    ADR_0055_VERSION,
    KNOWLEDGE_TOKEN_PATTERN,
    EvidenceLevel,
    KnowledgeItem,
)

REPO = Path(__file__).resolve().parents[1]
GOLDEN = REPO / "tests" / "golden" / "v2_1_0"
PROVIDER = "hlens_knowledge_local@1.1.0"
T = datetime(2026, 9, 26, tzinfo=UTC)

ITEM_FIELDS = ("tags", "assets")
QUERY_FIELDS = ("tags_all", "assets_any")


def _item(**overrides: Any) -> KnowledgeItem:
    fields: dict[str, Any] = {
        "name": "strategy_fixture",
        "version": "1.0.0",
        "created_at": T,
        "source": "Fixture (not a real citation)",
        "license": "fixture; no full text",
        "claim": "Past returns predict future returns.",
        "evidence_level": EvidenceLevel.E2_IN_SAMPLE,
    }
    fields.update(overrides)
    return KnowledgeItem(**fields)


def _golden(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((GOLDEN / name).read_text(encoding="utf-8"))
    return document


# ======================================================================================
# V8: the version registry, the field declarations, the Schema and the dependency floor
# ======================================================================================


def test_the_current_version_is_2_2_0_and_every_earlier_minor_stays_published() -> None:
    assert ADR_0055_VERSION == "2.2.0" == CONTRACT_SCHEMA_VERSION
    assert PUBLISHED_CONTRACT_SCHEMA_VERSIONS == ("2.0.0", "2.1.0", "2.2.0")
    assert PUBLISHED_CONTRACT_SCHEMA_VERSIONS[-1] == CONTRACT_SCHEMA_VERSION


def test_the_new_fields_are_declared_since_2_2_0_and_omitted_when_empty() -> None:
    assert dict(KnowledgeItem._FIELDS_SINCE) == dict.fromkeys(ITEM_FIELDS, "2.2.0")
    assert dict(KnowledgeQuery._FIELDS_SINCE) == dict.fromkeys(QUERY_FIELDS, "2.2.0")
    assert not KnowledgeResult._FIELDS_SINCE  # the result adds no field of its own
    for model, names in ((KnowledgeItem, ITEM_FIELDS), (KnowledgeQuery, QUERY_FIELDS)):
        for name in names:
            info = model.model_fields[name]
            assert info.default == ()
            assert info.exclude_if is not None
            assert info.exclude_if(()) and not info.exclude_if(("x",))


@pytest.mark.parametrize(
    ("model", "names"), [("KnowledgeItem", ITEM_FIELDS), ("KnowledgeQuery", QUERY_FIELDS)]
)
def test_the_committed_schema_declares_the_token_grammar(
    model: str, names: tuple[str, str]
) -> None:
    schema = json.loads((REPO / "schemas" / f"{model}.schema.json").read_text(encoding="utf-8"))
    assert schema["properties"]["schema_version"]["default"] == "2.2.0"
    for name in names:
        prop = schema["properties"][name]
        assert prop["type"] == "array" and prop["default"] == [] and prop["uniqueItems"] is True
        assert prop["items"] == {"type": "string", "pattern": KNOWLEDGE_TOKEN_PATTERN}
        assert name not in schema.get("required", [])


def test_the_result_schema_carries_the_item_fields() -> None:
    schema = json.loads(
        (REPO / "schemas" / "KnowledgeResult.schema.json").read_text(encoding="utf-8")
    )
    item = schema["$defs"]["KnowledgeItem"]["properties"]
    assert {"tags", "assets"} <= set(item)


def test_pydantic_is_declared_at_least_2_12_for_exclude_if() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert "pydantic>=2.12" in project["project"]["dependencies"]
    major, minor = (int(part) for part in installed_version("pydantic").split(".")[:2])
    assert (major, minor) >= (2, 12)
    lock = (REPO / "uv.lock").read_text(encoding="utf-8")
    assert '{ name = "pydantic", specifier = ">=2.12" }' in lock


# ======================================================================================
# V6: canonical tokens — non-canonical, duplicate and unordered values are refused
# ======================================================================================

NON_CANONICAL = [
    "Momentum",  # upper case is refused, never folded
    "BTCUSDT",
    "btc-usdt",  # hyphen
    "btc usdt",  # inner space
    "btc/usdt",
    "",  # empty
    "_btc",  # leading underscore
    "btc_",  # trailing underscore
    "btc__usdt",  # doubled underscore
    "bitcoiné",  # non-ASCII letter
    "ｂtc",  # full-width letter
    "١",  # Arabic-Indic digit
]


@pytest.mark.parametrize("bad", NON_CANONICAL)
@pytest.mark.parametrize("field", ITEM_FIELDS)
def test_an_item_with_a_non_canonical_token_is_refused(field: str, bad: str) -> None:
    with pytest.raises(ValidationError):
        _item(**{field: (bad,)})


@pytest.mark.parametrize("bad", NON_CANONICAL)
@pytest.mark.parametrize("field", QUERY_FIELDS)
def test_a_query_with_a_non_canonical_token_is_refused(field: str, bad: str) -> None:
    with pytest.raises(ValidationError):
        KnowledgeQuery.model_validate({field: (bad,)})


@pytest.mark.parametrize("values", [("btc", "btc"), ("eth", "btc"), ("a", "c", "b")])
def test_duplicate_or_unordered_tokens_are_refused_never_normalised(
    values: tuple[str, ...],
) -> None:
    for field in ITEM_FIELDS:
        with pytest.raises(ValidationError, match="重复|升序"):
            _item(**{field: values})
    for field in QUERY_FIELDS:
        with pytest.raises(ValidationError, match="重复|升序"):
            KnowledgeQuery.model_validate({field: values})


def test_outer_whitespace_follows_the_project_wide_strip_rule() -> None:
    """02-domain.md §3.7: surrounding whitespace is removed before the pattern applies."""
    item = _item(tags=(" momentum ",), assets=("btc\n",))
    assert (item.tags, item.assets) == (("momentum",), ("btc",))
    assert item == _item(tags=("momentum",), assets=("btc",))


def test_canonical_tokens_are_accepted() -> None:
    item = _item(tags=("52_week_high", "momentum"), assets=("btc", "crypto", "equity_index"))
    assert item.tags == ("52_week_high", "momentum")
    assert item.assets == ("btc", "crypto", "equity_index")


# ======================================================================================
# V7: the 2.0.0 / 2.1.0 boundary
# ======================================================================================


@pytest.mark.parametrize("old", ["2.0.0", "2.1.0"])
@pytest.mark.parametrize("field", ITEM_FIELDS)
def test_an_older_item_envelope_carrying_a_2_2_0_field_is_refused(old: str, field: str) -> None:
    with pytest.raises(ValidationError, match="2.2.0"):
        _item(schema_version=old, **{field: ("momentum",)})
    payload = {**_item().model_dump(mode="json"), "schema_version": old, field: ["momentum"]}
    with pytest.raises(ValidationError, match="2.2.0"):
        KnowledgeItem.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("old", ["2.0.0", "2.1.0"])
@pytest.mark.parametrize("field", QUERY_FIELDS)
def test_an_older_query_envelope_carrying_a_2_2_0_filter_is_refused(old: str, field: str) -> None:
    with pytest.raises(ValidationError, match="2.2.0"):
        KnowledgeQuery.model_validate({"schema_version": old, field: ("btc",)})
    with pytest.raises(ValidationError, match="2.2.0"):
        KnowledgeQuery.model_validate_json(json.dumps({"schema_version": old, field: ["btc"]}))


def test_new_content_cannot_be_built_inside_a_2_1_0_replay_scope() -> None:
    with contract_schema_version_scope("2.1.0"), pytest.raises(ValidationError, match="2.2.0"):
        _item(tags=("momentum",))
    with contract_schema_version_scope("2.1.0"), pytest.raises(ValidationError, match="2.2.0"):
        KnowledgeQuery(assets_any=("btc",))
    with contract_schema_version_scope("2.1.0"):
        assert _item().schema_version == "2.1.0"  # without new content it is a 2.1.0 item


def test_a_2_1_0_result_nesting_2_2_0_metadata_is_refused() -> None:
    query = KnowledgeQuery(schema_version="2.1.0")
    tagged = _item(tags=("momentum",))
    built = KnowledgeResult.build(query, PROVIDER, (tagged,))
    assert built.schema_version == "2.2.0"  # a new result: the current envelope
    payload = {**built.model_dump(mode="json"), "schema_version": "2.1.0"}
    with pytest.raises(ValidationError, match="tags / assets"):
        KnowledgeResult.model_validate(payload)
    with pytest.raises(ValidationError, match="tags / assets"):
        KnowledgeResult.model_validate_json(json.dumps(payload))


def test_a_2_1_0_result_may_nest_items_without_2_2_0_content() -> None:
    items = (_item(), _item(name="strategy_other", schema_version="2.1.0"))
    built = KnowledgeResult.build(KnowledgeQuery(schema_version="2.1.0"), PROVIDER, items)
    payload = {**built.model_dump(mode="json"), "schema_version": "2.1.0"}
    assert KnowledgeResult.model_validate(payload).result_hash == built.result_hash


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("knowledge_item.json", KnowledgeItem),
        ("knowledge_item_minimal.json", KnowledgeItem),
        ("knowledge_query.json", KnowledgeQuery),
        ("knowledge_query_default.json", KnowledgeQuery),
    ],
)
def test_empty_new_fields_keep_the_2_1_0_payload_shape_and_hash(
    name: str, model: type[KnowledgeItem] | type[KnowledgeQuery]
) -> None:
    """At a held envelope, explicit empty tags / assets / filters are the historical payload."""
    document = _golden(name)
    fields = ITEM_FIELDS if model is KnowledgeItem else QUERY_FIELDS
    explicit = model.model_validate({**document["payload"], **dict.fromkeys(fields, [])})
    assert explicit.schema_version == "2.1.0"
    assert explicit.model_dump(mode="json") == document["payload"]
    assert not set(fields) & set(explicit.model_dump(mode="json"))
    assert explicit.content_hash() == document["content_hash"]


def test_a_2_2_0_twin_of_a_2_1_0_object_has_the_same_shape_and_a_new_hash() -> None:
    """The envelope is part of the hash: a current-version object is a new identity (expected)."""
    for name, model in (
        ("knowledge_item.json", KnowledgeItem),
        ("knowledge_query.json", KnowledgeQuery),
    ):
        document = _golden(name)
        old = document["payload"]
        twin = model.model_validate({**old, "schema_version": "2.2.0"})
        assert twin.model_dump(mode="json") == {**old, "schema_version": "2.2.0"}
        assert twin.content_hash() != document["content_hash"]
        assert model.model_validate(old).content_hash() == document["content_hash"]


# ======================================================================================
# V3: 2.2.0 hashes are deterministic
# ======================================================================================


def test_a_tagged_item_and_a_filtered_query_hash_deterministically() -> None:
    item = _item(tags=("momentum", "time_series_momentum"), assets=("btc", "crypto"))
    query = KnowledgeQuery(terms=("momentum",), tags_all=("momentum",), assets_any=("btc", "eth"))
    for obj, model in ((item, KnowledgeItem), (query, KnowledgeQuery)):
        dumped = obj.model_dump(mode="json")
        reordered = dict(reversed(list(dumped.items())))
        rebuilt = (
            model.model_validate(dumped),
            model.model_validate(reordered),
            model.model_validate_json(obj.model_dump_json()),
            model.model_validate_json(json.dumps(reordered)),
        )
        assert {again.content_hash() for again in rebuilt} == {obj.content_hash()}
        assert dumped["schema_version"] == "2.2.0"
    assert item.model_dump(mode="json")["tags"] == ["momentum", "time_series_momentum"]
    assert query.model_dump(mode="json")["assets_any"] == ["btc", "eth"]
    result = KnowledgeResult.build(query, PROVIDER, (item,))
    again = KnowledgeResult.build(
        KnowledgeQuery.model_validate_json(query.model_dump_json()),
        PROVIDER,
        (KnowledgeItem.model_validate_json(item.model_dump_json()),),
    )
    assert result.result_hash == again.result_hash
    assert KnowledgeResult.model_validate_json(result.model_dump_json()) == result


def test_the_hash_rule_is_the_documented_one_for_2_2_0_content() -> None:
    item = _item(tags=("momentum",), assets=("btc",))
    semantic = {k: v for k, v in item.model_dump(mode="json").items() if k != "created_at"}
    assert item.content_hash() == content_hash(semantic)


# ======================================================================================
# V4: query_hash binds the filters; result_hash binds the query and the item metadata
# ======================================================================================


def test_every_filter_changes_the_query_hash() -> None:
    queries = [
        KnowledgeQuery(),
        KnowledgeQuery(tags_all=("momentum",)),
        KnowledgeQuery(tags_all=("volatility",)),
        KnowledgeQuery(tags_all=("momentum", "volatility")),
        KnowledgeQuery(assets_any=("momentum",)),  # same token, other field
        KnowledgeQuery(assets_any=("btc",)),
        KnowledgeQuery(assets_any=("btc", "eth")),
        KnowledgeQuery(tags_all=("momentum",), assets_any=("btc",)),
    ]
    assert len({query.content_hash() for query in queries}) == len(queries)


def test_the_result_hash_changes_with_the_query_filters() -> None:
    item = _item(tags=("momentum",), assets=("btc",))
    hashes = {
        KnowledgeResult.build(query, PROVIDER, (item,)).result_hash
        for query in (
            KnowledgeQuery(),
            KnowledgeQuery(tags_all=("momentum",)),
            KnowledgeQuery(assets_any=("btc",)),
        )
    }
    assert len(hashes) == 3  # same items, different questions -> different answers


def test_the_result_hash_changes_with_the_item_metadata() -> None:
    query = KnowledgeQuery()
    variants = [
        _item(),
        _item(tags=("momentum",)),
        _item(tags=("momentum", "trend")),
        _item(assets=("btc",)),
        _item(assets=("momentum",)),  # same token, other field
        _item(tags=("momentum",), assets=("btc",)),
    ]
    hashes = {KnowledgeResult.build(query, PROVIDER, (item,)).result_hash for item in variants}
    assert len(hashes) == len(variants)


def test_a_result_with_altered_metadata_does_not_keep_its_hash() -> None:
    result = KnowledgeResult.build(KnowledgeQuery(), PROVIDER, (_item(tags=("momentum",)),))
    payload = result.model_dump(mode="json")
    payload["items"][0]["tags"] = ["trend"]
    with pytest.raises(ValidationError, match="result_hash"):
        KnowledgeResult.model_validate(payload)

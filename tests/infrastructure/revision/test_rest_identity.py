"""REST identity rule ``hlens.binance.spot.rest-revision-identity@1.0.0`` (Phase 1 D3B; ADR-0027).

Pure-function evidence: no HTTP, no catalog. Covers the independent rule hash, the page-query
allowlist and canonical encoding, observation keys shared with the archive rule, response /
element revision identities, the time-free ``edge_id`` and the REST ``arrival_seq`` interval.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from core.contracts.revision import PolicyBinding, PolicyRole
from core.domain.base import canonical_json
from infrastructure.revision import identity as archive_identity
from infrastructure.revision import rest_identity
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from infrastructure.revision.rest_identity import (
    REST_ARRIVAL_SEQ_BASE,
    REST_ARRIVAL_SEQ_STRIDE,
    REST_IDENTITY_HASH,
    REST_IDENTITY_SPEC,
    RestArrivalSeqOverflow,
    RestIdentityViolation,
    RestPageQuery,
)

GOLDEN_REST_IDENTITY_HASH = "01f93537457b00ad572cc6771d12156743efe07896cee3476eb1ba46ee5a74ee"
GOLDEN_ARCHIVE_IDENTITY_HASH = "fc5f6f082554243c5ead89d389dc862f9c5b2b38d97a140bc8afdcd47b9b40aa"
ORIGIN = "https://market-data.invalid"
T_2025 = 1_735_689_600_000  # 2025-01-01T00:00:00Z in milliseconds
KEY = "binance:spot:agg_trade:BTCUSDT:1"
HASH_A = "a" * 64


def agg_natives(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "agg_trade_id": 26129,
        "price": Decimal("0.01633102"),
        "quantity": Decimal("4.70443515"),
        "first_trade_id": 27781,
        "last_trade_id": 27781,
        "timestamp_raw": 1_498_793_709_153,
        "is_buyer_maker": True,
        "is_best_match": True,
    }
    row.update(changes)
    return row


def kline_natives(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "open_time_raw": 1_499_040_000_000,
        "open": Decimal("0.01634790"),
        "high": Decimal("0.80000000"),
        "low": Decimal("0.01575800"),
        "close": Decimal("0.01577100"),
        "volume": Decimal("148976.11427815"),
        "close_time_raw": 1_499_040_059_999,
        "quote_asset_volume": Decimal("2434.19055334"),
        "number_of_trades": 308,
        "taker_buy_base_asset_volume": Decimal("1756.87402397"),
        "taker_buy_quote_asset_volume": Decimal("28.46694368"),
        "ignore_raw": "0",
    }
    row.update(changes)
    return row


# --------------------------------------------------------------------------- rule identity


def test_rule_hash_is_derived_from_the_full_spec_and_stable() -> None:
    assert canonical_json(REST_IDENTITY_SPEC)  # serialisable, no NaN
    derived = hashlib.sha256(canonical_json(REST_IDENTITY_SPEC).encode("utf-8")).hexdigest()
    assert REST_IDENTITY_HASH == derived == GOLDEN_REST_IDENTITY_HASH
    assert REST_IDENTITY_SPEC["rule"] == "hlens.binance.spot.rest-revision-identity"
    assert REST_IDENTITY_SPEC["version"] == "1.0.0"


@pytest.mark.parametrize(
    "path",
    [
        ("page_identity", "limit"),
        ("observation_key", "response"),
        ("edge_id", "document"),
        ("arrival_seq", "stride"),
        ("payload_hash", "agg_trade", "time_unit"),
    ],
)
def test_rule_hash_changes_with_any_part_of_the_rule(path: tuple[str, ...]) -> None:
    mutated = copy.deepcopy(REST_IDENTITY_SPEC)
    node: Any = mutated
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = ["mutated"]
    assert hashlib.sha256(canonical_json(mutated).encode("utf-8")).hexdigest() != (
        REST_IDENTITY_HASH
    )


def test_archive_rule_is_untouched_and_not_imported() -> None:
    assert archive_identity.IDENTITY_HASH == GOLDEN_ARCHIVE_IDENTITY_HASH
    tree = ast.parse(inspect.getsource(rest_identity))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert "infrastructure.revision.identity" not in imported
    assert not {"httpx", "urllib", "requests", "socket", "http"} & {
        (name or "").split(".")[0] for name in imported
    }
    assert "datetime.now" not in inspect.getsource(rest_identity)


def test_same_inputs_give_different_revision_ids_under_the_two_rules() -> None:
    rest = rest_identity.revision_id(KEY, "src", HASH_A)
    archive = archive_identity.revision_id(KEY, "src", HASH_A)
    assert rest.startswith("rev1-") and archive.startswith("rev1-")
    assert rest != archive  # the rule hash is part of the identity document


# --------------------------------------------------------------------------- page queries


def test_page_query_constructors_and_canonical_pairs() -> None:
    first = RestPageQuery.agg_trades_from_start("BTCUSDT", T_2025)
    assert first.pairs() == (("limit", "1000"), ("startTime", str(T_2025)), ("symbol", "BTCUSDT"))
    assert first.query_string() == f"limit=1000&startTime={T_2025}&symbol=BTCUSDT"
    assert first.path == "/api/v3/aggTrades"
    follow = RestPageQuery.agg_trades_from_id("ETHUSDT", 0)
    assert follow.query_string() == "fromId=0&limit=1000&symbol=ETHUSDT"
    kline = RestPageQuery.klines_from_start("BTCUSDT", T_2025)
    assert kline.query_string() == f"interval=1m&limit=1000&startTime={T_2025}&symbol=BTCUSDT"
    assert kline.path == "/api/v3/klines"
    assert rest_identity.page_source_uri(kline, ORIGIN) == (
        f"{ORIGIN}/api/v3/klines?interval=1m&limit=1000&startTime={T_2025}&symbol=BTCUSDT"
    )


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: RestPageQuery("agg_trades", "BTCUSDT"), id="latest-mode-agg"),
        pytest.param(lambda: RestPageQuery("klines_1m", "BTCUSDT"), id="latest-mode-klines"),
        pytest.param(
            lambda: RestPageQuery("agg_trades", "BTCUSDT", from_id=1, start_time_ms=T_2025),
            id="both-cursors",
        ),
        pytest.param(
            lambda: RestPageQuery("klines_1m", "BTCUSDT", from_id=1, start_time_ms=T_2025),
            id="klines-fromId",
        ),
        pytest.param(
            lambda: RestPageQuery.klines_from_start("BTCUSDT", T_2025 + 1), id="unaligned"
        ),
        pytest.param(
            lambda: RestPageQuery("agg_trades", "BTCUSDT", from_id=1, limit=500), id="limit"
        ),
        pytest.param(lambda: RestPageQuery.agg_trades_from_id("BTCUSDT", -1), id="negative"),
        pytest.param(lambda: RestPageQuery.agg_trades_from_id("BTCUSDT", True), id="bool"),
        pytest.param(lambda: RestPageQuery.agg_trades_from_id("BTCUSDT", 1 << 63), id="int64"),
        pytest.param(lambda: RestPageQuery.agg_trades_from_id("btcusdt", 1), id="lowercase"),
        pytest.param(lambda: RestPageQuery("trades", "BTCUSDT", from_id=1), id="data-type"),
    ],
)
def test_unlawful_page_queries_are_not_constructible(build: Any) -> None:
    with pytest.raises(RestIdentityViolation):
        build()


def test_from_pairs_accepts_only_the_canonical_allowlisted_encoding() -> None:
    query = RestPageQuery.agg_trades_from_id("BTCUSDT", 42)
    assert RestPageQuery.from_pairs("agg_trades", query.pairs()) == query
    kline = RestPageQuery.klines_from_start("ETHUSDT", T_2025)
    assert RestPageQuery.from_pairs("klines_1m", kline.pairs()) == kline
    bad: list[tuple[str, list[tuple[str, str]]]] = [
        ("agg_trades", [("endTime", "5"), ("fromId", "1"), ("limit", "1000"),
                        ("symbol", "BTCUSDT")]),
        ("klines_1m", [("interval", "1m"), ("limit", "1000"), ("startTime", "0"),
                       ("symbol", "BTCUSDT"), ("timeZone", "0")]),
        ("agg_trades", [("fromId", "1"), ("fromId", "1"), ("limit", "1000"),
                        ("symbol", "BTCUSDT")]),
        ("agg_trades", [("symbol", "BTCUSDT"), ("fromId", "1"), ("limit", "1000")]),
        ("agg_trades", [("fromId", "01"), ("limit", "1000"), ("symbol", "BTCUSDT")]),
        ("agg_trades", [("fromId", "+1"), ("limit", "1000"), ("symbol", "BTCUSDT")]),
        ("agg_trades", [("fromId", "١"), ("limit", "1000"), ("symbol", "BTCUSDT")]),
        ("agg_trades", [("fromId", "1"), ("limit", "1000")]),
        ("agg_trades", [("limit", "1000"), ("symbol", "BTCUSDT")]),
        ("klines_1m", [("interval", "5m"), ("limit", "1000"), ("startTime", "0"),
                       ("symbol", "BTCUSDT")]),
        ("klines_1m", [("limit", "1000"), ("startTime", "0"), ("symbol", "BTCUSDT")]),
        ("agg_trades", [("fromId", "1"), ("limit", "999"), ("symbol", "BTCUSDT")]),
    ]  # fmt: skip
    for data_type, pairs in bad:
        with pytest.raises(RestIdentityViolation):
            RestPageQuery.from_pairs(data_type, pairs)


def test_page_identity_vector_is_stable_and_excludes_the_attempt() -> None:
    query = RestPageQuery.agg_trades_from_start("BTCUSDT", T_2025)
    document = rest_identity.page_identity_document(query, ORIGIN)
    assert document == {
        "method": "GET",
        "origin": ORIGIN,
        "path": "/api/v3/aggTrades",
        "query": [["limit", "1000"], ["startTime", str(T_2025)], ["symbol", "BTCUSDT"]],
        "declared_time_unit": "millisecond",
        "rule": "hlens.binance.spot.rest-revision-identity@1.0.0",
    }
    digest = rest_identity.page_identity_sha256(query, ORIGIN)
    assert digest == "a03cc59437a389a83a45adb802605ca793a5b7283090a5f957d78bf2bb5873e5"
    assert digest == hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
    # Same query rebuilt from its pairs, or asked again later: same identity.
    again = RestPageQuery.from_pairs("agg_trades", query.pairs())
    assert rest_identity.page_identity_sha256(again, ORIGIN) == digest
    others = {
        rest_identity.page_identity_sha256(q, o)
        for q, o in (
            (RestPageQuery.agg_trades_from_start("BTCUSDT", T_2025 + 1), ORIGIN),
            (RestPageQuery.agg_trades_from_start("ETHUSDT", T_2025), ORIGIN),
            (RestPageQuery.agg_trades_from_id("BTCUSDT", T_2025), ORIGIN),
            (query, "https://other-market-data.invalid"),
        )
    }
    assert digest not in others and len(others) == 4


@pytest.mark.parametrize(
    "origin",
    [
        "http://market-data.invalid",
        "https://market-data.invalid/",
        "https://market-data.invalid/api",
        "https://market-data.invalid?x=1",
        "https://Market-Data.invalid",
        "https://user@market-data.invalid",
        "https://market-data.invalid:0",
        "https://market-data.invalid:080",
        "https://market-data.invalid:99999",
        "market-data.invalid",
    ],
)
def test_page_identity_requires_an_exact_https_origin(origin: str) -> None:
    query = RestPageQuery.agg_trades_from_id("BTCUSDT", 1)
    with pytest.raises(RestIdentityViolation):
        rest_identity.page_identity_sha256(query, origin)
    with pytest.raises(RestIdentityViolation):
        rest_identity.page_source_uri(query, origin)
    assert rest_identity.page_identity_sha256(query, "https://market-data.invalid:8443")


def test_response_keys() -> None:
    page = "b" * 64
    assert rest_identity.response_observation_key(page) == f"binance:spot:rest:{page}"
    assert rest_identity.response_object_key("klines_1m", "BTCUSDT", HASH_A, page) == (
        f"raw/binance/spot/rest/responses/{HASH_A}/klines_1m/BTCUSDT/{page}.json"
    )
    for bad in ("B" * 64, "b" * 63, ""):
        with pytest.raises(RestIdentityViolation):
            rest_identity.response_observation_key(bad)
    with pytest.raises(RestIdentityViolation):
        rest_identity.response_object_key("trades", "BTCUSDT", HASH_A, page)


# --------------------------------------------------------------------------- element identity


def test_element_observation_keys_equal_the_archive_rule() -> None:
    for symbol, trade in (("BTCUSDT", 0), ("ETHUSDT", 26129), ("BTCUSDT", (1 << 63) - 1)):
        assert rest_identity.agg_trade_observation_key(symbol, trade) == (
            archive_identity.agg_trade_observation_key(symbol, trade)
        )
    for start in (
        datetime(2024, 12, 31, 23, 59, tzinfo=UTC),
        datetime(2025, 1, 1, tzinfo=UTC),
    ):
        assert rest_identity.kline_1m_observation_key("BTCUSDT", start) == (
            archive_identity.kline_1m_observation_key("BTCUSDT", start)
        )
    with pytest.raises(RestIdentityViolation):
        rest_identity.kline_1m_observation_key("BTCUSDT", datetime(2025, 1, 1))  # naive
    with pytest.raises(RestIdentityViolation):
        rest_identity.agg_trade_observation_key("BTCUSDT", False)


def test_element_payload_hashes_match_the_archive_document_for_the_declared_unit() -> None:
    agg, kline = agg_natives(), kline_natives()
    assert rest_identity.agg_trade_payload_hash("BTCUSDT", agg) == (
        archive_identity.agg_trade_payload_hash("BTCUSDT", "millisecond", agg)
    )
    assert rest_identity.kline_1m_payload_hash("BTCUSDT", kline) == (
        archive_identity.kline_1m_payload_hash("BTCUSDT", "millisecond", kline)
    )
    # Microsecond archive documents are a different payload (the unit is part of the document).
    assert rest_identity.agg_trade_payload_hash("BTCUSDT", agg) != (
        archive_identity.agg_trade_payload_hash("BTCUSDT", "microsecond", agg)
    )
    for field, value in (("price", Decimal("0.01633103")), ("is_best_match", False)):
        assert rest_identity.agg_trade_payload_hash("BTCUSDT", agg_natives(**{field: value})) != (
            rest_identity.agg_trade_payload_hash("BTCUSDT", agg)
        )
    with pytest.raises(RestIdentityViolation):
        rest_identity.agg_trade_payload_hash("BTCUSDT", agg_natives(is_best_match=None))
    with pytest.raises(RestIdentityViolation):
        rest_identity.kline_1m_payload_hash("BTCUSDT", kline_natives(ignore_raw=0))


def test_element_revision_identity_is_idempotent_and_channel_level() -> None:
    source = rest_identity.rest_source_identity()
    assert source == "binance.public.spot.rest@1.0.0"
    payload = rest_identity.agg_trade_payload_hash("BTCUSDT", agg_natives())
    key = rest_identity.agg_trade_observation_key("BTCUSDT", 26129)
    first = rest_identity.revision_id(key, source, payload)
    # Delivered again by another page / response: the same revision (no response id inside).
    assert rest_identity.revision_id(key, source, payload) == first
    changed = rest_identity.agg_trade_payload_hash("BTCUSDT", agg_natives(quantity=Decimal(1)))
    assert rest_identity.revision_id(key, source, changed) != first
    assert rest_identity.revision_id(KEY, source, HASH_A) == (
        "rev1-6caf9e982b96ef44f7993588c3afff8fc006c60c285f2f2cb4f418066c345944"
    )
    for args in (("", source, HASH_A), (KEY, "", HASH_A), (KEY, source, "not-a-hash")):
        with pytest.raises(RestIdentityViolation):
            rest_identity.revision_id(*args)


def test_response_revision_identity_is_idempotent_per_page_and_bytes() -> None:
    query = RestPageQuery.agg_trades_from_id("BTCUSDT", 7)
    key = rest_identity.response_observation_key(rest_identity.page_identity_sha256(query, ORIGIN))
    source = rest_identity.rest_source_identity()
    same = {rest_identity.revision_id(key, source, HASH_A) for _ in range(3)}
    assert len(same) == 1
    assert rest_identity.revision_id(key, source, "c" * 64) not in same  # different bytes


# --------------------------------------------------------------------------- edge identity


def test_edge_id_is_time_free_stable_and_sensitive_to_every_input() -> None:
    edge = rest_identity.edge_id(DELIVERY_CHANNEL_BINDING, KEY, "rev1-x", "rev1-y")
    assert edge == "edge1-0f5f7ceb29917d8a25b4c47e11fe59fa613453082f0b5b3a168f69069219e989"
    parameters = inspect.signature(rest_identity.edge_id).parameters
    assert not any("time" in name for name in parameters)
    other_policy = PolicyBinding(
        role=PolicyRole.PRECEDENCE,
        policy_id=DELIVERY_CHANNEL_BINDING.policy_id,
        version="1.0.1",
        policy_hash=DELIVERY_CHANNEL_BINDING.policy_hash,
    )
    variants = {
        rest_identity.edge_id(other_policy, KEY, "rev1-x", "rev1-y"),
        rest_identity.edge_id(DELIVERY_CHANNEL_BINDING, KEY + "0", "rev1-x", "rev1-y"),
        rest_identity.edge_id(DELIVERY_CHANNEL_BINDING, KEY, "rev1-z", "rev1-y"),
        rest_identity.edge_id(DELIVERY_CHANNEL_BINDING, KEY, "rev1-y", "rev1-x"),
    }
    assert edge not in variants and len(variants) == 4
    availability = PolicyBinding(
        role=PolicyRole.AVAILABILITY, policy_id="x", version="1.0.0", policy_hash=HASH_A
    )
    with pytest.raises(RestIdentityViolation):
        rest_identity.edge_id(availability, KEY, "rev1-x", "rev1-y")
    with pytest.raises(RestIdentityViolation):
        rest_identity.edge_id(DELIVERY_CHANNEL_BINDING, KEY, "rev1-x", "rev1-x")
    with pytest.raises(RestIdentityViolation):
        rest_identity.edge_id(DELIVERY_CHANNEL_BINDING, "", "rev1-x", "rev1-y")


# --------------------------------------------------------------------------- arrival_seq


def test_rest_block_allocation_starts_at_two_to_the_62() -> None:
    assert REST_ARRIVAL_SEQ_BASE == 1 << 62 and REST_ARRIVAL_SEQ_STRIDE == 1 << 32
    first = rest_identity.rest_arrival_block_base(None)
    assert first == 1 << 62
    second = rest_identity.rest_arrival_block_base(first)
    assert second == first + (1 << 32)
    assert rest_identity.rest_arrival_block_base(second) == first + 2 * (1 << 32)
    top = (1 << 63) - (1 << 32)  # the last block that still fits below 2**63
    assert rest_identity.rest_arrival_block_base(top - (1 << 32)) == top
    with pytest.raises(RestArrivalSeqOverflow):
        rest_identity.rest_arrival_block_base(top)


@pytest.mark.parametrize(
    "anchor",
    [0, (1 << 62) - 1, (1 << 62) + 1, (1 << 62) + (1 << 32) + 5, 1 << 63, True, "1"],
)
def test_a_corrupt_rest_anchor_fails_closed(anchor: Any) -> None:
    with pytest.raises(RestIdentityViolation):
        rest_identity.rest_arrival_block_base(anchor)


def test_element_sequence_numbers_stay_inside_their_block() -> None:
    base = 1 << 62
    assert rest_identity.element_arrival_seq(base, 0) == base + 1
    assert rest_identity.element_arrival_seq(base, 999) == base + 1000
    top = (1 << 63) - (1 << 32)
    assert rest_identity.element_arrival_seq(top, 999) == top + 1000
    for index in (-1, 1000, True, 1.0):
        with pytest.raises(RestArrivalSeqOverflow):
            rest_identity.element_arrival_seq(base, index)  # type: ignore[arg-type]
    for bad_base in (base + 1, 0, (1 << 62) - (1 << 32)):
        with pytest.raises(RestIdentityViolation):
            rest_identity.element_arrival_seq(bad_base, 0)


def test_channel_interval_guards_partition_int64() -> None:
    boundary = 1 << 62
    assert rest_identity.check_archive_interval_arrival_seq(0) == 0
    assert rest_identity.check_archive_interval_arrival_seq(boundary - 1) == boundary - 1
    assert rest_identity.check_rest_arrival_seq(boundary) == boundary
    assert rest_identity.check_rest_arrival_seq((1 << 63) - 1) == (1 << 63) - 1
    for value in (boundary, -1, True):
        with pytest.raises(RestIdentityViolation):
            rest_identity.check_archive_interval_arrival_seq(value)
    for value in (boundary - 1, 1 << 63, False):
        with pytest.raises(RestIdentityViolation):
            rest_identity.check_rest_arrival_seq(value)
    # The D2 archive allocator is unchanged and its stride equals the REST stride.
    assert archive_identity.ARRIVAL_SEQ_STRIDE == REST_ARRIVAL_SEQ_STRIDE
    assert archive_identity.arrival_block_base(None) == 0


def test_no_wall_clock_is_read() -> None:
    # Identity is a function of its arguments: the same call later gives the same answer.
    start = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(minutes=3)
    assert rest_identity.kline_1m_observation_key("BTCUSDT", start) == (
        rest_identity.kline_1m_observation_key("BTCUSDT", start)
    )

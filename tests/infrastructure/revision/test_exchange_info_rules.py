"""E2 pure rules: the exchangeInfo identity rule and ``binance.spot.exchange-info-publication``.

No I/O: the query allowlist (acceptance #7, the rule side), request identity, revision ids,
arrival numbers and the ingest-fallback availability decision.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.contracts.revision import PolicyRole
from infrastructure.revision import exchange_info_identity as identity
from infrastructure.revision import rest_identity
from infrastructure.revision.exchange_info_availability import (
    EXCHANGE_INFO_AVAILABILITY_BINDING,
    EXCHANGE_INFO_AVAILABILITY_HASH,
    ExchangeInfoAvailabilitySubject,
    ExchangeInfoAvailabilityViolation,
    decide_exchange_info_availability,
    exchange_info_gap_for,
)
from infrastructure.revision.exchange_info_identity import (
    EXCHANGE_INFO_IDENTITY_HASH,
    ExchangeInfoIdentityViolation,
    ExchangeInfoQuery,
)

ORIGIN = "https://market.test"
CANONICAL_QUERY = "symbols=%5B%22BTCUSDT%22%2C%22ETHUSDT%22%5D"
T = datetime(2026, 9, 1, 12, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64


# --------------------------------------------------------------------------- goldens


def test_rule_hashes_are_golden() -> None:
    """Any rule change moves these values and must move the version."""
    assert EXCHANGE_INFO_IDENTITY_HASH == (
        "6c49785c5631759981194c6ae96ad11a32c89036b68188a33b760082f71f7fe3"
    )
    assert EXCHANGE_INFO_AVAILABILITY_HASH == (
        "26d948b271f0724fa9ae967adad27b43df39773839b44be8c3919c63050a50ba"
    )
    binding = EXCHANGE_INFO_AVAILABILITY_BINDING
    assert (binding.role, binding.policy_id, binding.version) == (
        PolicyRole.AVAILABILITY,
        "binance.spot.exchange-info-publication",
        "1.0.0",
    )


def test_the_identity_rule_is_physically_separate_from_the_rest_and_archive_rules() -> None:
    tree = ast.parse(Path(identity.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any("rest_identity" in item or item.endswith(".identity") for item in imported)
    assert EXCHANGE_INFO_IDENTITY_HASH != rest_identity.REST_IDENTITY_HASH


# --------------------------------------------------------------------------- the allowlist


def test_the_only_query_is_the_first_slice_pair() -> None:
    query = ExchangeInfoQuery()
    assert query.symbols == ("BTCUSDT", "ETHUSDT")
    assert query.path == "/api/v3/exchangeInfo"
    assert query.pairs() == (("symbols", '["BTCUSDT","ETHUSDT"]'),)
    assert query.query_string() == CANONICAL_QUERY
    assert ExchangeInfoQuery.from_query_string(CANONICAL_QUERY) == query


@pytest.mark.parametrize(
    "symbols",
    [
        ("BTCUSDT",),
        ("ETHUSDT", "BTCUSDT"),
        ("BTCUSDT", "ETHUSDT", "BNBUSDT"),
        ("btcusdt", "ethusdt"),
        ("BTCUSDT", "BTCUSDT"),
        (),
        ["BTCUSDT", "ETHUSDT"],
    ],
)
def test_no_other_symbol_set_is_constructible(symbols: object) -> None:
    with pytest.raises(ExchangeInfoIdentityViolation):
        ExchangeInfoQuery(symbols)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "symbols",
        f"{CANONICAL_QUERY}&apiKey=abc",
        f"{CANONICAL_QUERY}&signature=00",
        f"{CANONICAL_QUERY}&timestamp=1",
        f"{CANONICAL_QUERY}&recvWindow=5000",
        f"{CANONICAL_QUERY}&symbolStatus=TRADING",
        f"{CANONICAL_QUERY}&permissions=SPOT",
        f"{CANONICAL_QUERY}&showPermissionSets=false",
        f"{CANONICAL_QUERY}&{CANONICAL_QUERY}",
        "symbol=BTCUSDT",
        "apiKey=abc",
        'symbols=["BTCUSDT","ETHUSDT"]',
        "symbols=%5b%22BTCUSDT%22%2c%22ETHUSDT%22%5d",
        "symbols=%5B%22BTCUSDT%22,%22ETHUSDT%22%5D",
        "symbols=%5B%22ETHUSDT%22%2C%22BTCUSDT%22%5D",
        "symbols=%5B%22BTCUSDT%22%5D",
        "symbols=%5B%20%22BTCUSDT%22%2C%22ETHUSDT%22%5D",
    ],
)
def test_every_other_query_string_fails_closed(text: str) -> None:
    with pytest.raises(ExchangeInfoIdentityViolation):
        ExchangeInfoQuery.from_query_string(text)


# --------------------------------------------------------------------------- identity


def test_the_request_identity_depends_on_origin_and_query_only() -> None:
    query = ExchangeInfoQuery()
    document = identity.request_identity_document(query, ORIGIN)
    assert document == {
        "method": "GET",
        "origin": ORIGIN,
        "path": "/api/v3/exchangeInfo",
        "query": [["symbols", '["BTCUSDT","ETHUSDT"]']],
        "rule": "hlens.binance.spot.exchange-info-identity@1.0.0",
    }
    first = identity.request_identity_sha256(query, ORIGIN)
    assert first == identity.request_identity_sha256(ExchangeInfoQuery(), ORIGIN)
    assert first != identity.request_identity_sha256(query, "https://other.test")
    assert identity.request_source_uri(query, ORIGIN) == (
        f"{ORIGIN}/api/v3/exchangeInfo?{CANONICAL_QUERY}"
    )
    key = identity.snapshot_observation_key(first)
    assert key == f"binance:spot:exchange-info:{first}"
    assert identity.response_object_key(SHA_A, first) == (
        f"raw/binance/spot/exchange-info/responses/{SHA_A}/{first}.json"
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://market.test",
        "https://market.test/",
        "https://market.test/api",
        "https://user@market.test",
        "https://market.test:0",
        "https://market.test:080",
        "https://MARKET.test",
    ],
)
def test_the_origin_must_be_an_exact_https_origin(origin: str) -> None:
    with pytest.raises(ExchangeInfoIdentityViolation):
        identity.request_identity_sha256(ExchangeInfoQuery(), origin)


def test_revision_ids_are_stable_and_bytes_distinguish_revisions() -> None:
    key = identity.snapshot_observation_key(SHA_A)
    source = identity.exchange_info_source_identity()
    assert source == "binance.public.spot.exchange-info@1.0.0"
    first = identity.revision_id(key, source, SHA_A)
    assert first.startswith("rev1-") and first == identity.revision_id(key, source, SHA_A)
    assert identity.revision_id(key, source, SHA_B) != first
    assert first != rest_identity.revision_id(key, source, SHA_A)
    for bad in ("", "A" * 64, "a" * 63):
        with pytest.raises(ExchangeInfoIdentityViolation):
            identity.revision_id(key, source, bad)
    with pytest.raises(ExchangeInfoIdentityViolation):
        identity.snapshot_observation_key("not-a-sha")


def test_arrival_numbers_count_up_from_zero_and_never_leave_their_interval() -> None:
    assert identity.next_arrival_seq(None) == 0
    assert identity.next_arrival_seq(0) == 1
    assert identity.next_arrival_seq(41) == 42
    for bad in (-1, 1 << 62, True, "1"):
        with pytest.raises(ExchangeInfoIdentityViolation):
            identity.next_arrival_seq(bad)  # type: ignore[arg-type]
    with pytest.raises(ExchangeInfoIdentityViolation, match="exhausted"):
        identity.next_arrival_seq((1 << 62) - 1)


# --------------------------------------------------------------------------- availability


@pytest.mark.parametrize("subject", list(ExchangeInfoAvailabilitySubject))
def test_every_subject_falls_back_to_ingest_time_with_its_gap(
    subject: ExchangeInfoAvailabilitySubject,
) -> None:
    decision = decide_exchange_info_availability(
        subject,
        requested_at=T - timedelta(milliseconds=5),
        ingest_time=T,
        knowledge_time=T + timedelta(seconds=3),
    )
    times = decision.times
    assert (times.event_time, times.event_end_time) == (T - timedelta(milliseconds=5), T)
    assert times.available_time == times.ingest_time == T
    assert times.source_time is None and times.declared_latency == timedelta(0)
    assert decision.evidence == ()
    assert decision.evidence_gap == exchange_info_gap_for(subject)
    assert decision.policy == EXCHANGE_INFO_AVAILABILITY_BINDING


def test_the_listing_gap_states_the_observed_from_boundary() -> None:
    gap = exchange_info_gap_for(ExchangeInfoAvailabilitySubject.LISTING_OBSERVATION)
    assert "observed-from" in gap and "not exchange-declared" in gap
    assert gap.startswith("binance.spot.exchange-info-publication@1.0.0:")


@pytest.mark.parametrize(
    ("requested_at", "ingest_time", "knowledge_time"),
    [
        (T, T, T),
        (T + timedelta(seconds=1), T, T),
        (T - timedelta(seconds=1), T, T - timedelta(microseconds=1)),
        (T.replace(tzinfo=None), T, T),
        (T.astimezone(timezone(timedelta(hours=8))), T, T),
    ],
)
def test_unlawful_times_fail_closed(
    requested_at: datetime, ingest_time: datetime, knowledge_time: datetime
) -> None:
    with pytest.raises(ExchangeInfoAvailabilityViolation):
        decide_exchange_info_availability(
            ExchangeInfoAvailabilitySubject.SNAPSHOT,
            requested_at=requested_at,
            ingest_time=ingest_time,
            knowledge_time=knowledge_time,
        )

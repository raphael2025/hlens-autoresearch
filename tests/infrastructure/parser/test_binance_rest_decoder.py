"""Strict decoding evidence for ``binance.spot.rest.decoder@1.0.0`` (Phase 1 D3C; ADR-0027 §6).

Covers the binding / spec, the body limit, hostile JSON, exact element shapes and the field
grammar and row rules of both data types. Pagination, coverage and chain behaviour live in
``test_binance_rest_pagination.py``.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from core.contracts.revision import PolicyRole
from core.domain.base import canonical_json
from infrastructure.parser import (
    DECODER_BINDING,
    DECODER_HASH,
    DECODER_ID,
    DECODER_SPEC,
    DECODER_VERSION,
    MAX_BODY_LIMIT_BYTES,
    MIN_BODY_LIMIT_BYTES,
    PARSER_BINDING,
    AnsweredInterval,
    RestAggTradeElement,
    RestDecodeRequestError,
    RestKline1mElement,
    RestPageDecodeRequest,
    RestRejectionCode,
    decode_rest_page,
)
from infrastructure.parser import binance_rest as decoder_mod
from infrastructure.revision import identity as archive_identity
from infrastructure.revision import rest_identity
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_HASH
from infrastructure.revision.rest_identity import (
    PAGE_LIMIT,
    RestIdentityViolation,
    RestPageQuery,
)
from tests.infrastructure.parser.rest_support import (
    MAX_BODY,
    MINUTE_MS,
    RETRIEVED_AT,
    SYMBOL,
    T0,
    TARGET_END,
    agg_item,
    agg_items,
    agg_request,
    body,
    decoded,
    kline_item,
    kline_items,
    kline_request,
    rejected,
)

CODE = RestRejectionCode
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: Golden hashes frozen by C3 / D1 / D3B acceptance; D3C must not move any of them.
GOLDEN_ARCHIVE_IDENTITY_HASH = "fc5f6f082554243c5ead89d389dc862f9c5b2b38d97a140bc8afdcd47b9b40aa"
GOLDEN_REST_IDENTITY_HASH = "01f93537457b00ad572cc6771d12156743efe07896cee3476eb1ba46ee5a74ee"
GOLDEN_DELIVERY_CHANNEL_HASH = "399513973e6bb22e9e2c74a84ad226a220adf3b6035e8ca4290d26c19cdf1a85"
GOLDEN_ARCHIVE_PARSER_HASH = "c2c3c375e6fa87982759549ccbc0779c546929a64454ed14c2063677212ac033"


def _agg(items: Any, **kwargs: Any) -> object:
    return decode_rest_page(agg_request(**kwargs), body(items))


def _kline(items: Any, **kwargs: Any) -> object:
    return decode_rest_page(kline_request(**kwargs), body(items))


# --------------------------------------------------------------------------- binding / spec


def test_the_binding_is_a_parser_role_binding_derived_from_the_spec() -> None:
    assert DECODER_BINDING.role is PolicyRole.PARSER
    assert (DECODER_BINDING.policy_id, DECODER_BINDING.version) == (DECODER_ID, DECODER_VERSION)
    assert (DECODER_ID, DECODER_VERSION) == ("binance.spot.rest.decoder", "1.0.0")
    derived = hashlib.sha256(canonical_json(DECODER_SPEC).encode("utf-8")).hexdigest()
    assert DECODER_BINDING.policy_hash == derived == DECODER_HASH


def test_the_spec_states_every_rule_the_decoder_enforces() -> None:
    assert DECODER_SPEC["declared_time_unit"] == rest_identity.DECLARED_TIME_UNIT == "millisecond"
    assert DECODER_SPEC["body"]["max_bytes_argument_range"] == [
        MIN_BODY_LIMIT_BYTES,
        MAX_BODY_LIMIT_BYTES,
    ]
    assert (MIN_BODY_LIMIT_BYTES, MAX_BODY_LIMIT_BYTES) == (65_536, 67_108_864)
    assert DECODER_SPEC["time"]["magnitude_guessing"] is False
    assert DECODER_SPEC["time"]["tolerance"] == 0
    assert DECODER_SPEC["purity"]["no_io"] is True
    assert DECODER_SPEC["purity"]["no_clock"] is True
    assert DECODER_SPEC["purity"]["no_settings_lookup"] is True
    agg = DECODER_SPEC["data_types"]["agg_trades"]
    klines = DECODER_SPEC["data_types"]["klines_1m"]
    assert agg["continuation"] == "fromId = last a + 1"
    assert klines["continuation"] == "startTime = last closed open + 60000"
    assert klines["unclosed_kline"]["allowed"] == "at most one, and only as the final item"
    assert DECODER_SPEC["collection_target_window"]["never_rejects"].startswith("a valid element")
    framing = DECODER_SPEC["json"]["framing_whitespace"]
    assert framing.startswith("0x20, 0x09, 0x0A, 0x0D only (RFC 8259)")
    assert "no other Unicode whitespace" in framing
    assert DECODER_SPEC["json"]["trailing_content"].startswith("reject any non-whitespace")
    assert DECODER_SPEC["rejection"]["codes"] == sorted(code.value for code in RestRejectionCode)
    assert "the response body" in DECODER_SPEC["rejection"]["never_included"]


def test_the_spec_is_not_polluted_by_deployment_values() -> None:
    text = canonical_json(DECODER_SPEC)
    assert str(MAX_BODY) not in text  # the operational limit is an argument, never the rule
    assert "https://" not in text


def test_the_decoder_hash_changes_when_any_rule_changes() -> None:
    mutated = json.loads(canonical_json(DECODER_SPEC))
    mutated["time"]["tolerance"] = 1
    digest = hashlib.sha256(canonical_json(mutated).encode("utf-8")).hexdigest()
    assert digest != DECODER_HASH


def test_c3_d1_and_d3b_hashes_and_bindings_are_untouched() -> None:
    assert archive_identity.IDENTITY_HASH == GOLDEN_ARCHIVE_IDENTITY_HASH
    assert rest_identity.REST_IDENTITY_HASH == GOLDEN_REST_IDENTITY_HASH
    assert DELIVERY_CHANNEL_HASH == GOLDEN_DELIVERY_CHANNEL_HASH
    assert PARSER_BINDING.policy_hash == GOLDEN_ARCHIVE_PARSER_HASH
    assert PARSER_BINDING.policy_id == "binance.spot.archive.parser"
    # A separate identifier from the archive parser, so both can bind in one PIT spec.
    assert DECODER_BINDING.policy_id != PARSER_BINDING.policy_id
    assert DECODER_HASH != PARSER_BINDING.policy_hash


def test_the_decoder_does_not_reimplement_d3b_identity() -> None:
    source = inspect.getsource(decoder_mod)
    imported = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
        and node.module == "infrastructure.revision.rest_identity"
        for alias in node.names
    }
    assert imported == {
        "DATA_TYPES",
        "DECLARED_TIME_UNIT",
        "RestIdentityViolation",
        "RestPageQuery",
    }
    after_docstring = source.split('"""', 2)[-1]
    for derived in ("rev1-", "edge1-", "arrival_seq", "payload_hash", "observation_key"):
        assert derived not in after_docstring, derived


def test_the_decoder_module_performs_no_io_and_reads_no_settings() -> None:
    source = inspect.getsource(decoder_mod)
    imports: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    assert not imports & {"httpx", "os", "pathlib", "socket", "urllib", "time", "random"}
    assert "infrastructure.settings" not in source
    assert "datetime.now" not in source and "utcnow" not in source


# --------------------------------------------------------------------------- request hygiene


@pytest.mark.parametrize(
    "changes",
    [
        {"retrieved_at": datetime(2023, 11, 14, 22, 14)},
        {"retrieved_at": datetime(2023, 11, 14, 22, 14, tzinfo=timezone(timedelta(hours=1)))},
        {"retrieved_at": "2023-11-14T22:14:00Z"},
        {"target_start_ms": T0, "target_end_ms": T0},
        {"target_start_ms": TARGET_END + 1},
        {"query": RestPageQuery.agg_trades_from_start(SYMBOL, T0), "target_start_ms": -1},
        {"query": RestPageQuery.agg_trades_from_start(SYMBOL, T0), "target_end_ms": 1 << 63},
        {"max_body_bytes": MIN_BODY_LIMIT_BYTES - 1},
        {"max_body_bytes": MAX_BODY_LIMIT_BYTES + 1},
        {"max_body_bytes": True},
        {"page_index": -1},
        {"page_index": 1},
    ],
)
def test_a_malformed_request_is_a_caller_error_not_a_source_verdict(changes: Any) -> None:
    with pytest.raises(RestDecodeRequestError):
        agg_request(**changes)


def test_retrieved_at_must_not_precede_the_epoch() -> None:
    with pytest.raises(RestDecodeRequestError):
        agg_request(retrieved_at=EPOCH - timedelta(milliseconds=1))


def test_a_klines_target_window_must_be_minute_aligned() -> None:
    aligned = RestPageQuery.klines_from_start(SYMBOL, T0)
    with pytest.raises(RestDecodeRequestError, match="minute-aligned"):
        kline_request(query=aligned, target_start_ms=T0 + 1)
    with pytest.raises(RestDecodeRequestError, match="minute-aligned"):
        kline_request(target_end_ms=TARGET_END + 1)
    # The page query itself is already minute-aligned by the D3B identity rule.
    with pytest.raises(RestIdentityViolation):
        RestPageQuery.klines_from_start(SYMBOL, T0 + 1)


def test_the_first_page_of_a_chain_queries_the_target_start() -> None:
    with pytest.raises(RestDecodeRequestError):
        agg_request(query=RestPageQuery.agg_trades_from_start(SYMBOL, T0 + 1))
    with pytest.raises(RestDecodeRequestError):
        agg_request(query=RestPageQuery.agg_trades_from_id(SYMBOL, 1))
    with pytest.raises(RestDecodeRequestError):
        kline_request(query=RestPageQuery.klines_from_start(SYMBOL, T0 + MINUTE_MS))


def test_the_body_must_be_immutable_bytes() -> None:
    with pytest.raises(RestDecodeRequestError):
        decode_rest_page(agg_request(), bytearray(b"[]"))  # type: ignore[arg-type]
    with pytest.raises(RestDecodeRequestError):
        decode_rest_page(agg_request(), "[]")  # type: ignore[arg-type]


def test_decode_rest_page_requires_its_own_request_type() -> None:
    with pytest.raises(RestDecodeRequestError):
        decode_rest_page("not a request", b"[]")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- body limit


def test_a_body_exactly_at_the_limit_is_accepted_and_one_byte_above_is_not() -> None:
    payload = body(agg_items(3))
    # Padded with a non-whitespace byte, so the padded body is trailing content and never framing.
    padded = payload + b"!" * (MIN_BODY_LIMIT_BYTES - len(payload))
    assert len(padded) == MIN_BODY_LIMIT_BYTES
    at_limit = decode_rest_page(agg_request(max_body_bytes=MIN_BODY_LIMIT_BYTES), payload)
    assert decoded(at_limit).element_count == 3

    outcome = rejected(
        decode_rest_page(agg_request(max_body_bytes=MIN_BODY_LIMIT_BYTES), padded + b"!"),
        CODE.BODY_TOO_LARGE,
    )
    assert outcome.body_size_bytes == MIN_BODY_LIMIT_BYTES + 1
    assert "exceeds" in outcome.detail
    # Exactly at the limit the same padded body is only rejected for its trailing content,
    # which proves the size check runs before any JSON work.
    rejected(
        decode_rest_page(agg_request(max_body_bytes=MIN_BODY_LIMIT_BYTES), padded),
        CODE.TRAILING_CONTENT,
    )


# --------------------------------------------------------------------------- hostile JSON


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"", CODE.INVALID_JSON),
        (b"[", CODE.INVALID_JSON),
        (b"[,]", CODE.INVALID_JSON),
        (b"[]]", CODE.TRAILING_CONTENT),
        (b"[][]", CODE.TRAILING_CONTENT),
        (b"[]\n{}", CODE.TRAILING_CONTENT),
        (b" \t\r\n", CODE.INVALID_JSON),  # framing whitespace is not a JSON value
        (b"\xef\xbb\xbf[]", CODE.BYTE_ORDER_MARK),
        (b"\xff\xfe[\x00]\x00", CODE.INVALID_UTF8),
        (b'["\xc3("]', CODE.INVALID_UTF8),
        (b"{}", CODE.TOP_LEVEL_NOT_ARRAY),
        (b'"[]"', CODE.TOP_LEVEL_NOT_ARRAY),
        (b"null", CODE.TOP_LEVEL_NOT_ARRAY),
        (b"123", CODE.TOP_LEVEL_NOT_ARRAY),
        (b"[NaN]", CODE.NON_FINITE_NUMBER),
        (b"[Infinity]", CODE.NON_FINITE_NUMBER),
        (b"[-Infinity]", CODE.NON_FINITE_NUMBER),
        (b"[1.5]", CODE.FRACTIONAL_NUMBER),
        (b"[1e5]", CODE.FRACTIONAL_NUMBER),
        (b"[" + b"9" * 21 + b"]", CODE.INTEGER_OUT_OF_RANGE),
    ],
)
def test_hostile_or_malformed_json_is_rejected_whole_page(
    payload: bytes, code: RestRejectionCode
) -> None:
    for request in (agg_request(), kline_request()):
        assert rejected(decode_rest_page(request, payload), code).elements == ()


#: RFC 8259 §2 insignificant whitespace; the exchange may frame the value with any of it.
RFC_WHITESPACE = (b" ", b"\t", b"\n", b"\r")
#: Whitespace to Unicode, but not to JSON: form feed, vertical tab, NBSP, line and ideographic
#: separators. ``str.isspace()`` would accept every one of them.
NON_RFC_WHITESPACE = (b"\x0c", b"\x0b", b"\xc2\xa0", b"\xe2\x80\xa8", b"\xe3\x80\x80")


@pytest.mark.parametrize("space", RFC_WHITESPACE)
@pytest.mark.parametrize("items", [[], agg_items(3)])
def test_rfc_framing_whitespace_is_accepted_around_the_top_level_value(
    space: bytes, items: list[dict[str, Any]]
) -> None:
    payload = body(items)
    for framed in (space + payload, payload + space, space + payload + space):
        assert decoded(decode_rest_page(agg_request(), framed)).element_count == len(items)


def test_mixed_framing_whitespace_is_accepted_on_both_sides() -> None:
    for items in ([], kline_items(2)):
        framed = b" \t\r\n" + body(items) + b"\n\r\t "
        assert decoded(decode_rest_page(kline_request(), framed)).element_count == len(items)


@pytest.mark.parametrize("space", NON_RFC_WHITESPACE)
def test_non_rfc_unicode_whitespace_is_not_framing(space: bytes) -> None:
    payload = body(agg_items(2))
    rejected(decode_rest_page(agg_request(), space + payload), CODE.INVALID_JSON)
    rejected(decode_rest_page(agg_request(), payload + space), CODE.TRAILING_CONTENT)


@pytest.mark.parametrize("tail", [b" x", b"\n{}", b"\t[]", b" \r\n 0", b"\r\n\x00"])
def test_framing_whitespace_followed_by_content_is_still_trailing_content(tail: bytes) -> None:
    outcome = rejected(
        decode_rest_page(agg_request(), body(agg_items(2)) + tail), CODE.TRAILING_CONTENT
    )
    assert "follows the top-level JSON value" in outcome.detail


def test_duplicate_object_keys_are_rejected_at_any_level() -> None:
    top = b'[{"a":1,"a":2,"p":"1","q":"1","f":1,"l":1,"T":1,"m":true,"M":true}]'
    rejected(decode_rest_page(agg_request(), top), CODE.DUPLICATE_JSON_KEY)
    nested = b'[{"a":{"x":1,"x":2},"p":"1","q":"1","f":1,"l":1,"T":1,"m":true,"M":true}]'
    rejected(decode_rest_page(agg_request(), nested), CODE.DUPLICATE_JSON_KEY)
    rejected(decode_rest_page(kline_request(), b'[[{"x":1,"x":2}]]'), CODE.DUPLICATE_JSON_KEY)


def test_a_page_longer_than_the_requested_limit_is_rejected() -> None:
    rejected(_agg(agg_items(PAGE_LIMIT + 1)), CODE.PAGE_TOO_LONG)
    rejected(_kline(kline_items(PAGE_LIMIT + 1)), CODE.PAGE_TOO_LONG)
    assert decoded(_agg(agg_items(PAGE_LIMIT))).element_count == PAGE_LIMIT


def test_rejections_never_carry_the_response_body() -> None:
    secret = "92792.05000000"
    outcome = rejected(_agg([agg_item(1, T0, price=secret + "e9")]), CODE.INVALID_DECIMAL)
    rendered = f"{outcome!r} {outcome.detail}"
    assert secret not in rendered


def test_unexpected_key_names_are_counted_but_never_echoed() -> None:
    item = agg_item(1, T0)
    item["x-injected-secret-looking-key"] = 1
    outcome = rejected(_agg([item]), CODE.ELEMENT_SHAPE)
    assert "injected" not in f"{outcome!r} {outcome.detail}"
    assert "1 unexpected" in outcome.detail


# --------------------------------------------------------------------------- element shape


@pytest.mark.parametrize("item", [[], {}, "x", 1, True, None, [1, 2]])
def test_an_aggtrade_item_must_be_an_object_with_exactly_the_eight_keys(item: Any) -> None:
    rejected(_agg([item]), CODE.ELEMENT_SHAPE)


@pytest.mark.parametrize("missing", ["a", "p", "q", "f", "l", "T", "m", "M"])
def test_a_missing_aggtrade_key_rejects_the_page(missing: str) -> None:
    item = agg_item(1, T0)
    del item[missing]
    outcome = rejected(_agg([item]), CODE.ELEMENT_SHAPE)
    assert f"'{missing}'" in outcome.detail


def test_an_extra_aggtrade_key_rejects_the_page() -> None:
    rejected(_agg([{**agg_item(1, T0), "E": 1}]), CODE.ELEMENT_SHAPE)


@pytest.mark.parametrize("slots", [0, 1, 11, 13])
def test_a_kline_item_must_have_exactly_twelve_slots(slots: int) -> None:
    item = kline_item(T0)
    reshaped = item[:slots] if slots <= len(item) else [*item, "0"]
    rejected(_kline([reshaped]), CODE.ELEMENT_SHAPE)


@pytest.mark.parametrize("item", [{}, "x", 1, True, None])
def test_a_kline_item_must_be_a_json_array(item: Any) -> None:
    rejected(_kline([item]), CODE.ELEMENT_SHAPE)


# --------------------------------------------------------------------------- field grammar


@pytest.mark.parametrize("key", ["a", "f", "l", "T"])
def test_a_json_boolean_is_not_an_integer(key: str) -> None:
    outcome = rejected(_agg([{**agg_item(1, T0), key: True}]), CODE.INVALID_INTEGER)
    assert outcome.field_name == key
    assert "boolean" in outcome.detail


@pytest.mark.parametrize("key", ["a", "f", "l", "T"])
def test_integer_fields_reject_strings_and_negatives(key: str) -> None:
    rejected(_agg([{**agg_item(1, T0), key: "1"}]), CODE.INVALID_INTEGER)
    rejected(_agg([{**agg_item(1, T0), key: -1}]), CODE.INTEGER_OUT_OF_RANGE)


def test_integer_fields_reject_int64_overflow() -> None:
    rejected(_agg([{**agg_item(1, T0), "a": 1 << 63}]), CODE.INTEGER_OUT_OF_RANGE)
    rejected(_kline([kline_item(T0, trades=1 << 63)]), CODE.INTEGER_OUT_OF_RANGE)


@pytest.mark.parametrize("value", [1, 0, "true", None, []])
def test_boolean_fields_reject_everything_but_json_booleans(value: Any) -> None:
    rejected(_agg([agg_item(1, T0, buyer_maker=value)]), CODE.INVALID_BOOLEAN)
    rejected(_agg([agg_item(1, T0, best_match=value)]), CODE.INVALID_BOOLEAN)


@pytest.mark.parametrize(
    "text",
    ["+1.0", "-1.0", "1e5", "1E5", ".5", "1.", "01.5", "00", " 1", "1 ", "", "abc", "1_0", "０"],
)
def test_decimal_fields_require_a_plain_non_negative_decimal(text: str) -> None:
    rejected(_agg([agg_item(1, T0, price=text)]), CODE.INVALID_DECIMAL)


@pytest.mark.parametrize("value", [1, True, None, ["1"]])
def test_decimal_fields_reject_non_strings(value: Any) -> None:
    rejected(_agg([agg_item(1, T0, price=value)]), CODE.INVALID_DECIMAL)


def test_decimal_fields_reject_precision_and_scale_overflow_without_rounding() -> None:
    rejected(_agg([agg_item(1, T0, price="1" * 21)]), CODE.DECIMAL_OUT_OF_RANGE)
    rejected(_agg([agg_item(1, T0, price="1." + "1" * 19)]), CODE.DECIMAL_OUT_OF_RANGE)
    widest = "1" * 20 + "." + "1" * 18
    element = decoded(_agg([agg_item(1, T0, price=widest)])).elements[0]
    assert isinstance(element, RestAggTradeElement)
    assert element.price == Decimal(widest)
    assert format(element.price, "f") == widest  # never rounded, never renormalised


def test_the_kline_ignore_slot_is_kept_as_source_text() -> None:
    element = decoded(_kline([kline_item(T0, ignore="0.00000000")])).elements[0]
    assert isinstance(element, RestKline1mElement)
    assert element.ignore_raw == "0.00000000"
    assert element.native_fields()["ignore_raw"] == "0.00000000"
    rejected(_kline([kline_item(T0, ignore=0)]), CODE.INVALID_DECIMAL)


# --------------------------------------------------------------------------- row rules


def test_aggtrade_row_rules() -> None:
    rejected(_agg([agg_item(1, T0, price="0")]), CODE.NON_POSITIVE_PRICE)
    rejected(_agg([agg_item(1, T0, price="0.000000000000000000")]), CODE.NON_POSITIVE_PRICE)
    rejected(_agg([agg_item(1, T0, quantity="0")]), CODE.NON_POSITIVE_QUANTITY)
    rejected(_agg([agg_item(1, T0, first=20, last=19)]), CODE.TRADE_ID_RANGE_INVALID)
    assert decoded(_agg([agg_item(1, T0, first=20, last=20)])).element_count == 1


@pytest.mark.parametrize("name", ["open_", "high", "low", "close"])
def test_kline_prices_must_be_positive(name: str) -> None:
    changes: dict[str, Any] = {name: "0"}
    rejected(_kline([kline_item(T0, **changes)]), CODE.NON_POSITIVE_PRICE)


@pytest.mark.parametrize(
    "changes",
    [
        {"low": "92800.00000000"},
        {"high": "92700.00000000"},
        {"low": "92900.00000000", "high": "92800.00000000"},
    ],
)
def test_kline_ohlc_invariants(changes: Any) -> None:
    rejected(_kline([kline_item(T0, **changes)]), CODE.OHLC_INVARIANT)


def test_kline_taker_volumes_may_not_exceed_the_totals() -> None:
    rejected(_kline([kline_item(T0, taker_base="6.45789001")]), CODE.TAKER_VOLUME_INVARIANT)
    rejected(_kline([kline_item(T0, taker_quote="599298.29174061")]), CODE.TAKER_VOLUME_INVARIANT)
    equal = kline_item(T0, taker_base="6.45789000", taker_quote="599298.29174060")
    assert decoded(_kline([equal])).element_count == 1


def test_kline_open_must_be_minute_aligned() -> None:
    rejected(_kline([kline_item(T0 + 1)]), CODE.KLINE_OPEN_NOT_ALIGNED)
    rejected(_kline([kline_item(T0 + MINUTE_MS - 1)]), CODE.KLINE_OPEN_NOT_ALIGNED)


def test_kline_close_time_must_be_exactly_one_minute_minus_one_millisecond() -> None:
    rejected(_kline([kline_item(T0, close_ms=T0 + MINUTE_MS)]), CODE.KLINE_CLOSE_TIME_MISMATCH)
    rejected(_kline([kline_item(T0, close_ms=T0 + MINUTE_MS - 2)]), CODE.KLINE_CLOSE_TIME_MISMATCH)
    # A microsecond-shaped close time (…59999999) is caught by the same exact rule.
    rejected(_kline([kline_item(T0, close_ms=T0 + 59_999_999)]), CODE.KLINE_CLOSE_TIME_MISMATCH)


# --------------------------------------------------------------------------- derived times


def test_times_are_exact_utc_at_millisecond_resolution() -> None:
    element = decoded(_agg([agg_item(1, T0 + 123)])).elements[0]
    assert isinstance(element, RestAggTradeElement)
    assert element.event_time == EPOCH + timedelta(milliseconds=T0 + 123)
    assert element.event_time.utcoffset() == timedelta(0)
    assert element.timestamp_raw == T0 + 123

    kline = decoded(_kline([kline_item(T0)])).elements[0]
    assert isinstance(kline, RestKline1mElement)
    assert kline.interval_start == EPOCH + timedelta(milliseconds=T0)
    assert kline.interval_end - kline.interval_start == timedelta(minutes=1)
    assert kline.close_time_raw == T0 + MINUTE_MS - 1


def test_native_fields_feed_the_d3b_identity_rule_without_duplicating_it() -> None:
    agg_element = decoded(_agg([agg_item(7, T0)])).elements[0]
    assert isinstance(agg_element, RestAggTradeElement)
    natives = agg_element.native_fields()
    assert rest_identity.agg_trade_payload_hash(SYMBOL, natives)
    natives["price"] = Decimal("1")
    assert agg_element.price != Decimal("1")  # native_fields() hands out a copy

    kline_element = decoded(_kline([kline_item(T0)])).elements[0]
    assert isinstance(kline_element, RestKline1mElement)
    assert rest_identity.kline_1m_payload_hash(SYMBOL, kline_element.native_fields())


# --------------------------------------------------------------------------- unit errors


def test_a_microsecond_timestamp_read_as_milliseconds_is_caught_by_the_upper_bound() -> None:
    assert rejected(_agg([agg_item(1, T0 * 1000)]), CODE.FUTURE_TIMESTAMP).field_name == "T"
    micros = T0 * 1000
    rejected(_kline([kline_item(micros - micros % MINUTE_MS)]), CODE.FUTURE_TIMESTAMP)


def test_a_second_resolution_timestamp_is_caught_by_the_query_lower_bound() -> None:
    rejected(_agg([agg_item(1, T0 // 1000)]), CODE.QUERY_LOWER_BOUND)
    seconds = T0 // 1000
    rejected(_kline([kline_item(seconds - seconds % MINUTE_MS)]), CODE.QUERY_LOWER_BOUND)


def test_an_element_exactly_at_retrieved_at_is_still_accepted() -> None:
    request = agg_request()
    at_now = request.retrieved_at_us // 1000
    assert decoded(decode_rest_page(request, body([agg_item(1, at_now)]))).element_count == 1


def test_a_local_clock_behind_the_source_fails_the_page_closed() -> None:
    request = agg_request(retrieved_at=RETRIEVED_AT)
    one_ms_late = request.retrieved_at_us // 1000 + 1
    rejected(decode_rest_page(request, body([agg_item(1, one_ms_late)])), CODE.FUTURE_TIMESTAMP)


# --------------------------------------------------------------------------- atomicity


def test_a_failure_on_the_last_row_releases_no_element() -> None:
    items = agg_items(PAGE_LIMIT)
    items[-1]["p"] = "0"
    outcome = rejected(_agg(items), CODE.NON_POSITIVE_PRICE)
    assert outcome.element_index == PAGE_LIMIT - 1
    assert outcome.elements == ()
    assert not hasattr(outcome, "summary")

    klines = kline_items(PAGE_LIMIT)
    klines[-1][3] = "99999.00000000"  # low above the close
    assert rejected(_kline(klines), CODE.OHLC_INVARIANT).elements == ()


def test_an_answered_interval_cannot_end_before_it_starts() -> None:
    with pytest.raises(RestDecodeRequestError):
        AnsweredInterval(10, 9)
    assert AnsweredInterval(10, 10).is_empty
    assert not AnsweredInterval(10, 11).is_empty


def test_a_decode_request_is_frozen() -> None:
    request = agg_request()
    with pytest.raises(FrozenInstanceError):
        request.max_body_bytes = 1  # type: ignore[misc]
    assert isinstance(request, RestPageDecodeRequest)

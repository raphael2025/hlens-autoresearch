"""E2 decoder ``binance.spot.exchange-info.decoder@1.0.0``: strict, whole-snapshot, pure.

Synthetic bodies only (shaped like evidence L4); no network, no storage.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from infrastructure.parser.binance_exchange_info import (
    EXCHANGE_INFO_DECODER_BINDING,
    EXCHANGE_INFO_DECODER_HASH,
    MAX_BODY_LIMIT_BYTES,
    MIN_BODY_LIMIT_BYTES,
    ExchangeInfoDecoded,
    ExchangeInfoDecodeRequestError,
    ExchangeInfoRejection,
    ExchangeInfoRejectionCode,
    ExchangeInfoSymbol,
    decode_exchange_info,
)
from tests.infrastructure.revision.exchange_info_support import body, symbol_entry

REQUESTED = ("BTCUSDT", "ETHUSDT")
LIMIT = MIN_BODY_LIMIT_BYTES
Code = ExchangeInfoRejectionCode


def decode(payload: bytes) -> ExchangeInfoDecoded | ExchangeInfoRejection:
    return decode_exchange_info(payload, requested_symbols=REQUESTED, max_body_bytes=LIMIT)


def rejected(payload: bytes) -> ExchangeInfoRejection:
    outcome = decode(payload)
    assert isinstance(outcome, ExchangeInfoRejection), outcome
    return outcome


def document(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = json.loads(body({"BTCUSDT": "TRADING", "ETHUSDT": "HALT"}))
    base.update(overrides)
    return base


def encode(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def test_the_decoder_binding_is_versioned_and_hashed() -> None:
    assert EXCHANGE_INFO_DECODER_BINDING.policy_id == "binance.spot.exchange-info.decoder"
    assert EXCHANGE_INFO_DECODER_BINDING.version == "1.0.0"
    assert EXCHANGE_INFO_DECODER_BINDING.role.value == "parser"
    # Any rule change moves this golden value (and must move the version).
    assert EXCHANGE_INFO_DECODER_HASH == (
        "4d9796dc0a8a9e58c89a66b1a2b7fb351db1b240aef01f86c86245672b1127c8"
    )


def test_an_official_shaped_answer_decodes_to_the_four_natives_per_symbol() -> None:
    outcome = decode(body({"ETHUSDT": "HALT", "BTCUSDT": "TRADING"}, server_time=17))
    assert outcome == ExchangeInfoDecoded(
        server_time_raw=17,
        symbols=(
            ExchangeInfoSymbol("BTCUSDT", "TRADING", "BTC", "USDT"),
            ExchangeInfoSymbol("ETHUSDT", "HALT", "ETH", "USDT"),
        ),
        missing_symbols=(),
    )


def test_a_missing_symbol_and_an_unknown_status_are_facts_not_rejections() -> None:
    outcome = decode(body({"BTCUSDT": "PRE_TRADING", "ETHUSDT": None}))
    assert isinstance(outcome, ExchangeInfoDecoded)
    assert outcome.symbols == (ExchangeInfoSymbol("BTCUSDT", "PRE_TRADING", "BTC", "USDT"),)
    assert outcome.missing_symbols == ("ETHUSDT",)
    empty = decode(body({"BTCUSDT": None, "ETHUSDT": None}))
    assert isinstance(empty, ExchangeInfoDecoded)
    assert empty.symbols == () and empty.missing_symbols == REQUESTED


def test_the_decoder_is_deterministic() -> None:
    payload = body({"BTCUSDT": "TRADING", "ETHUSDT": "BREAK"})
    assert decode(payload) == decode(payload)


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"\xef\xbb\xbf" + body({"BTCUSDT": "TRADING"}), Code.BYTE_ORDER_MARK),
        (b'{"serverTime":1,"symbols":[]}\xff', Code.INVALID_UTF8),
        (b'{"serverTime":1,"symbols":[', Code.INVALID_JSON),
        (b'{"serverTime":1,"serverTime":2,"symbols":[]}', Code.DUPLICATE_JSON_KEY),
        (b'{"serverTime":1,"symbols":[],"x":{"a":1,"a":1}}', Code.DUPLICATE_JSON_KEY),
        (b'{"serverTime":1,"symbols":[],"x":NaN}', Code.NON_FINITE_NUMBER),
        (b'{"serverTime":1,"symbols":[],"x":Infinity}', Code.NON_FINITE_NUMBER),
        (b'{"serverTime":1,"symbols":[],"x":0.5}', Code.FRACTIONAL_NUMBER),
        (b'{"serverTime":1,"symbols":[],"x":1e3}', Code.FRACTIONAL_NUMBER),
        (b'{"serverTime":123456789012345678901,"symbols":[]}', Code.INTEGER_OUT_OF_RANGE),
        (b'{"serverTime":1,"symbols":[]} {}', Code.TRAILING_CONTENT),
        (b'{"serverTime":1,"symbols":[]}\xc2\xa0', Code.TRAILING_CONTENT),
        (b"[]", Code.TOP_LEVEL_NOT_OBJECT),
        (b'{"symbols":[]}', Code.MISSING_FIELD),
        (b'{"serverTime":1}', Code.MISSING_FIELD),
        (b'{"serverTime":-1,"symbols":[]}', Code.INVALID_FIELD),
        (b'{"serverTime":true,"symbols":[]}', Code.INVALID_FIELD),
        (b'{"serverTime":"1","symbols":[]}', Code.INVALID_FIELD),
        (b'{"serverTime":9223372036854775808,"symbols":[]}', Code.INVALID_FIELD),
        (b'{"serverTime":1,"symbols":{}}', Code.INVALID_FIELD),
        (b'{"serverTime":1,"symbols":["BTCUSDT"]}', Code.INVALID_FIELD),
    ],
)
def test_malformed_bodies_reject_the_whole_snapshot(payload: bytes, code: Code) -> None:
    assert rejected(payload).code is code


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda e: e.pop("status"), Code.MISSING_FIELD),
        (lambda e: e.pop("baseAsset"), Code.MISSING_FIELD),
        (lambda e: e.pop("quoteAsset"), Code.MISSING_FIELD),
        (lambda e: e.__setitem__("status", "trading"), Code.INVALID_FIELD),
        (lambda e: e.__setitem__("status", 1), Code.INVALID_FIELD),
        (lambda e: e.__setitem__("status", ""), Code.INVALID_FIELD),
        (lambda e: e.__setitem__("status", "TRADING "), Code.INVALID_FIELD),
        (lambda e: e.__setitem__("baseAsset", "btc"), Code.INVALID_FIELD),
        (lambda e: e.__setitem__("quoteAsset", None), Code.INVALID_FIELD),
        (lambda e: e.__setitem__("symbol", "btcusdt"), Code.INVALID_FIELD),
    ],
)
def test_the_four_read_fields_have_a_strict_grammar(mutate: Any, code: Code) -> None:
    entry = symbol_entry("BTCUSDT", "TRADING")
    mutate(entry)
    assert rejected(encode(document(symbols=[entry]))).code is code


def test_an_unrequested_or_repeated_symbol_does_not_answer_the_query() -> None:
    extra = document(symbols=[symbol_entry("BNBUSDT", "TRADING")])
    assert rejected(encode(extra)).code is Code.UNREQUESTED_SYMBOL
    twice = document(symbols=[symbol_entry("BTCUSDT", "TRADING"), symbol_entry("BTCUSDT", "HALT")])
    assert rejected(encode(twice)).code is Code.DUPLICATE_SYMBOL


def test_the_body_limit_is_enforced_by_the_decoder_too() -> None:
    payload = encode(document(padding="x" * LIMIT))
    assert rejected(payload).code is Code.BODY_TOO_LARGE


def test_a_rejection_never_echoes_the_body() -> None:
    secret = "SECRETVALUE"
    outcome = rejected(f'{{"serverTime":1,"symbols":[],"{secret}":0.5}}'.encode())
    assert secret not in outcome.detail and secret not in repr(outcome)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"requested_symbols": ("ETHUSDT", "BTCUSDT"), "max_body_bytes": LIMIT},
        {"requested_symbols": ("BTCUSDT", "BTCUSDT"), "max_body_bytes": LIMIT},
        {"requested_symbols": (), "max_body_bytes": LIMIT},
        {"requested_symbols": ("btcusdt",), "max_body_bytes": LIMIT},
        {"requested_symbols": REQUESTED, "max_body_bytes": MIN_BODY_LIMIT_BYTES - 1},
        {"requested_symbols": REQUESTED, "max_body_bytes": MAX_BODY_LIMIT_BYTES + 1},
        {"requested_symbols": REQUESTED, "max_body_bytes": True},
    ],
)
def test_caller_mistakes_are_request_errors_not_snapshot_verdicts(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ExchangeInfoDecodeRequestError):
        decode_exchange_info(body({"BTCUSDT": "TRADING"}), **kwargs)

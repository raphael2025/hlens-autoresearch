"""Strict exchangeInfo decoder ``binance.spot.exchange-info.decoder@1.0.0`` (Phase 1 E2; ADR-0029).

One complete response body in, one whole-snapshot verdict out. A **pure function**: no HTTP, no
storage, no catalog, no clock. The body-size limit and the requested symbols are explicit
arguments, so a replayed snapshot decodes identically forever.

What it decides (zero tolerance):

- **body** — no larger than the caller's limit, strict UTF-8, no BOM;
- **JSON** — one top-level object framed only by RFC 8259 whitespace, no duplicate object keys
  anywhere, no ``NaN`` / ``Infinity``, no fractional or exponent numbers, integer literals of at
  most 20 characters;
- **fields read** — ``serverTime`` a non-negative int64 JSON integer (kept as received, its unit is
  **not** interpreted and it is never a status time); ``symbols`` an array of objects, each with
  string ``symbol`` / ``baseAsset`` / ``quoteAsset`` (``[A-Z0-9]{1,32}``) and string ``status``
  (``[A-Z][A-Z0-9_]{0,63}``). Every other key is ignored (the venue adds fields over time);
- **scope** — every entry names a requested symbol, at most once. An unrequested or repeated
  symbol rejects the snapshot (it does not answer the query).

What it deliberately does **not** decide: whether a ``status`` value is known, and what a missing
requested symbol means. Both are facts of an accepted snapshot; the listing derivation turns them
into quality findings and fails closed (ADR-0029 §2, ``binance.spot.listing-status@1.0.0``).

A rejection is whole-snapshot and carries a stable code and a short non-secret detail, never the
body. ADR-0029 "failure semantics": a rejected snapshot is never a Raw revision.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from core.contracts.revision import PolicyBinding, PolicyRole
from core.domain.base import canonical_json

__all__ = [
    "EXCHANGE_INFO_DECODER_BINDING",
    "EXCHANGE_INFO_DECODER_HASH",
    "EXCHANGE_INFO_DECODER_ID",
    "EXCHANGE_INFO_DECODER_SPEC",
    "EXCHANGE_INFO_DECODER_VERSION",
    "MAX_BODY_LIMIT_BYTES",
    "MIN_BODY_LIMIT_BYTES",
    "ExchangeInfoDecodeRequestError",
    "ExchangeInfoDecoded",
    "ExchangeInfoRejection",
    "ExchangeInfoRejectionCode",
    "ExchangeInfoSymbol",
    "decode_exchange_info",
]

EXCHANGE_INFO_DECODER_ID: Final = "binance.spot.exchange-info.decoder"
EXCHANGE_INFO_DECODER_VERSION: Final = "1.0.0"

#: Allowed range of the operational body-size limit (the value is the collector's setting).
MIN_BODY_LIMIT_BYTES: Final = 65_536
MAX_BODY_LIMIT_BYTES: Final = 67_108_864

_MAX_INT_LITERAL_CHARS: Final = 20
_INT64_MAX: Final = (1 << 63) - 1
_MAX_DETAIL_CHARS: Final = 240
_ASSET_RE: Final = re.compile(r"[A-Z0-9]{1,32}")
_STATUS_RE: Final = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_JSON_WHITESPACE: Final = frozenset(" \t\n\r")


class ExchangeInfoDecodeRequestError(ValueError):
    """A caller mistake (limit out of range, malformed symbol list); never a snapshot verdict."""


class ExchangeInfoRejectionCode(StrEnum):
    BODY_TOO_LARGE = "body_too_large"
    INVALID_UTF8 = "invalid_utf8"
    BYTE_ORDER_MARK = "byte_order_mark"
    INVALID_JSON = "invalid_json"
    DUPLICATE_JSON_KEY = "duplicate_json_key"
    NON_FINITE_NUMBER = "non_finite_number"
    FRACTIONAL_NUMBER = "fractional_number"
    INTEGER_OUT_OF_RANGE = "integer_out_of_range"
    TRAILING_CONTENT = "trailing_content"
    TOP_LEVEL_NOT_OBJECT = "top_level_not_object"
    MISSING_FIELD = "missing_field"
    INVALID_FIELD = "invalid_field"
    UNREQUESTED_SYMBOL = "unrequested_symbol"
    DUPLICATE_SYMBOL = "duplicate_symbol"


class _Reject(Exception):
    def __init__(self, code: ExchangeInfoRejectionCode, detail: str) -> None:
        super().__init__(code.value)
        self.code = code
        self.detail = detail[:_MAX_DETAIL_CHARS]


@dataclass(frozen=True, slots=True)
class ExchangeInfoSymbol:
    """The four native fields of one requested symbol, exactly as received."""

    symbol: str
    status: str
    base_asset: str
    quote_asset: str


@dataclass(frozen=True, slots=True)
class ExchangeInfoDecoded:
    """An accepted snapshot: ``serverTime`` as received, present symbols (sorted), missing ones."""

    server_time_raw: int
    symbols: tuple[ExchangeInfoSymbol, ...]
    missing_symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExchangeInfoRejection:
    """A rejected snapshot: stable code + short detail; never a Raw revision."""

    code: ExchangeInfoRejectionCode
    detail: str


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names):
        raise _Reject(ExchangeInfoRejectionCode.DUPLICATE_JSON_KEY, "an object repeats a key")
    return dict(pairs)


def _no_constant(literal: str) -> Any:
    raise _Reject(
        ExchangeInfoRejectionCode.NON_FINITE_NUMBER, f"the body contains the constant {literal}"
    )


def _no_float(literal: str) -> Any:
    raise _Reject(
        ExchangeInfoRejectionCode.FRACTIONAL_NUMBER,
        "the body contains a fractional or exponent JSON number",
    )


def _json_int(literal: str) -> int:
    if len(literal) > _MAX_INT_LITERAL_CHARS:
        raise _Reject(
            ExchangeInfoRejectionCode.INTEGER_OUT_OF_RANGE,
            f"a JSON integer literal is longer than {_MAX_INT_LITERAL_CHARS} characters",
        )
    return int(literal)


_DECODER: Final = json.JSONDecoder(
    object_pairs_hook=_no_duplicate_keys,
    parse_constant=_no_constant,
    parse_float=_no_float,
    parse_int=_json_int,
)


def _skip_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index] in _JSON_WHITESPACE:
        index += 1
    return index


def _parse(body: bytes, max_body_bytes: int) -> dict[str, Any]:
    if len(body) > max_body_bytes:
        raise _Reject(
            ExchangeInfoRejectionCode.BODY_TOO_LARGE,
            f"body of {len(body)} bytes exceeds the {max_body_bytes} byte limit",
        )
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _Reject(
            ExchangeInfoRejectionCode.INVALID_UTF8, f"body is not UTF-8 at byte {exc.start}"
        ) from None
    if text.startswith("﻿"):
        raise _Reject(ExchangeInfoRejectionCode.BYTE_ORDER_MARK, "body starts with a BOM")
    try:
        value, end = _DECODER.raw_decode(text, _skip_whitespace(text, 0))
    except json.JSONDecodeError as exc:
        raise _Reject(
            ExchangeInfoRejectionCode.INVALID_JSON, f"{exc.msg} at character {exc.pos}"
        ) from None
    tail = _skip_whitespace(text, end)
    if tail != len(text):
        raise _Reject(
            ExchangeInfoRejectionCode.TRAILING_CONTENT,
            f"content follows the top-level JSON value at character {tail}",
        )
    if not isinstance(value, dict):
        raise _Reject(
            ExchangeInfoRejectionCode.TOP_LEVEL_NOT_OBJECT,
            f"top-level JSON value is {type(value).__name__}, not an object",
        )
    return value


def _field(document: dict[str, Any], name: str, where: str) -> Any:
    if name not in document:
        raise _Reject(ExchangeInfoRejectionCode.MISSING_FIELD, f"{where} has no {name!r}")
    return document[name]


def _text(document: dict[str, Any], name: str, where: str, pattern: re.Pattern[str]) -> str:
    value = _field(document, name, where)
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise _Reject(
            ExchangeInfoRejectionCode.INVALID_FIELD, f"{where} {name!r} is not a valid string"
        )
    return value


def _decode(body: bytes, requested: tuple[str, ...], max_body_bytes: int) -> ExchangeInfoDecoded:
    document = _parse(body, max_body_bytes)
    server_time = _field(document, "serverTime", "the snapshot")
    if (
        not isinstance(server_time, int)
        or isinstance(server_time, bool)
        or not 0 <= server_time <= _INT64_MAX
    ):
        raise _Reject(
            ExchangeInfoRejectionCode.INVALID_FIELD, "serverTime is not a non-negative int64"
        )
    entries = _field(document, "symbols", "the snapshot")
    if not isinstance(entries, list):
        raise _Reject(ExchangeInfoRejectionCode.INVALID_FIELD, "symbols is not an array")
    found: dict[str, ExchangeInfoSymbol] = {}
    for index, entry in enumerate(entries):
        where = f"symbols[{index}]"
        if not isinstance(entry, dict):
            raise _Reject(ExchangeInfoRejectionCode.INVALID_FIELD, f"{where} is not an object")
        symbol = _text(entry, "symbol", where, _ASSET_RE)
        if symbol not in requested:
            raise _Reject(
                ExchangeInfoRejectionCode.UNREQUESTED_SYMBOL,
                f"{where} names a symbol that was not requested",
            )
        if symbol in found:
            raise _Reject(
                ExchangeInfoRejectionCode.DUPLICATE_SYMBOL, f"{where} repeats symbol {symbol}"
            )
        found[symbol] = ExchangeInfoSymbol(
            symbol=symbol,
            status=_text(entry, "status", where, _STATUS_RE),
            base_asset=_text(entry, "baseAsset", where, _ASSET_RE),
            quote_asset=_text(entry, "quoteAsset", where, _ASSET_RE),
        )
    return ExchangeInfoDecoded(
        server_time_raw=server_time,
        symbols=tuple(found[symbol] for symbol in sorted(found)),
        missing_symbols=tuple(symbol for symbol in requested if symbol not in found),
    )


def decode_exchange_info(
    body: bytes, *, requested_symbols: tuple[str, ...], max_body_bytes: int
) -> ExchangeInfoDecoded | ExchangeInfoRejection:
    """Decode one complete exchangeInfo body for ``requested_symbols`` (sorted, distinct)."""
    if not isinstance(body, bytes):
        raise ExchangeInfoDecodeRequestError("body must be bytes")
    if (
        not isinstance(requested_symbols, tuple)
        or not requested_symbols
        or list(requested_symbols) != sorted(set(requested_symbols))
        or any(
            not isinstance(item, str) or _ASSET_RE.fullmatch(item) is None
            for item in requested_symbols
        )
    ):
        raise ExchangeInfoDecodeRequestError("requested_symbols must be sorted distinct symbols")
    if (
        not isinstance(max_body_bytes, int)
        or isinstance(max_body_bytes, bool)
        or not MIN_BODY_LIMIT_BYTES <= max_body_bytes <= MAX_BODY_LIMIT_BYTES
    ):
        raise ExchangeInfoDecodeRequestError(
            f"max_body_bytes must be in [{MIN_BODY_LIMIT_BYTES}, {MAX_BODY_LIMIT_BYTES}]"
        )
    try:
        return _decode(body, requested_symbols, max_body_bytes)
    except _Reject as exc:
        return ExchangeInfoRejection(code=exc.code, detail=exc.detail)
    except RecursionError:
        return ExchangeInfoRejection(
            code=ExchangeInfoRejectionCode.INVALID_JSON, detail="the body nests too deeply"
        )


#: Every rule of this decoder; the policy hash is its canonical-JSON SHA-256.
EXCHANGE_INFO_DECODER_SPEC: Final[dict[str, Any]] = {
    "decoder": EXCHANGE_INFO_DECODER_ID,
    "version": EXCHANGE_INFO_DECODER_VERSION,
    "role": PolicyRole.PARSER.value,
    "adr": "ADR-0029",
    "endpoint": "GET /api/v3/exchangeInfo?symbols=[...]",
    "body": {
        "limit_range_bytes": [MIN_BODY_LIMIT_BYTES, MAX_BODY_LIMIT_BYTES],
        "encoding": "UTF-8, no BOM",
    },
    "json": {
        "top_level": "object, framed only by RFC 8259 whitespace",
        "duplicate_keys": "reject",
        "non_finite": "reject",
        "fractional_or_exponent_numbers": "reject",
        "max_int_literal_chars": _MAX_INT_LITERAL_CHARS,
    },
    "fields": {
        "serverTime": "non-negative int64 JSON integer, kept raw; unit not interpreted",
        "symbols": "array of objects",
        "symbols[].symbol": _ASSET_RE.pattern,
        "symbols[].status": _STATUS_RE.pattern,
        "symbols[].baseAsset": _ASSET_RE.pattern,
        "symbols[].quoteAsset": _ASSET_RE.pattern,
        "other_keys": "ignored",
    },
    "scope": {
        "unrequested_symbol": "reject",
        "duplicate_symbol": "reject",
        "missing_requested_symbol": "accepted fact (listed as missing), never inferred",
        "unknown_status_value": "accepted fact, never interpreted here",
    },
    "rejection": "whole snapshot; stable code + short detail; never a Raw revision",
}
EXCHANGE_INFO_DECODER_HASH: Final = hashlib.sha256(
    canonical_json(EXCHANGE_INFO_DECODER_SPEC).encode("utf-8")
).hexdigest()
EXCHANGE_INFO_DECODER_BINDING: Final = PolicyBinding(
    role=PolicyRole.PARSER,
    policy_id=EXCHANGE_INFO_DECODER_ID,
    version=EXCHANGE_INFO_DECODER_VERSION,
    policy_hash=EXCHANGE_INFO_DECODER_HASH,
)

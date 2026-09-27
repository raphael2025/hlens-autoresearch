"""Strict REST page decoder ``binance.spot.rest.decoder@1.0.0`` (Phase 1 D3C; ADR-0027 §6 / §7).

One complete response body in, one whole-page verdict out. The decoder is a **pure function**:
no HTTP, no storage, no catalog, no clock, no settings. Everything it needs — the already
validated ``RestPageQuery``, the local ``retrieved_at``, the collection target window, the
previous accepted page summary and the operational body-size limit — is an explicit argument, so
D3D's settings can never become a hidden global and a replayed page decodes identically forever.

What it decides (ADR-0027 §6, zero tolerance):

- **body** — a complete entity body no larger than the caller's limit, strict UTF-8, no BOM;
- **JSON** — one top-level array, framed at most by RFC 8259 whitespace and followed by nothing
  else, no duplicate object keys anywhere, no ``NaN`` / ``Infinity``, no fractional numbers,
  ``0 <= len <= query.limit``, exact element shapes;
- **fields** — JSON integers that are non-negative int64 (a JSON boolean is *not* an integer),
  decimal fields as JSON strings exactly representable by ``decimal(38, 18)`` without rounding,
  and the accepted positive / OHLC / taker / minute-alignment row rules;
- **envelope** — the query's own lower bound (``T >= startTime`` / ``a >= fromId`` / ``open >=
  startTime``), the page's declared ordering, continuity against the previous page and the
  physical upper bound ``T <= retrieved_at`` / ``open <= retrieved_at``;
- **unclosed klines** — at most one, only as the final item; it is never a decoded element, only
  a quality fact plus the ``unclosed_tail`` stop reason, and the answered coverage stops at its
  open. Its bytes survive in the response payload, which is D3D's / D3E's business, not ours.

What it deliberately does **not** do:

- The **collection target window** ``[t0, t1)`` only drives stop and coverage accounting. A valid
  element after ``t1`` stays a decoded element and can never make a page invalid (ADR-0027 §6 F1).
- HTTP status, redirects, content-length or header validation, streaming truncation, retries and
  budgets are D3D's; this module never pretends to have checked them.
- Identity (``observation_key``, ``payload_hash``, ``revision_id``, ``arrival_seq``) belongs to
  ``hlens.binance.spot.rest-revision-identity@1.0.0`` (D3B) and is not re-implemented here; the
  decoded elements simply expose their native fields through ``native_fields()``.
- Nothing here writes a revision, a quality event row or an edge; ``RestQualityFact`` is the
  structured *fact*, and D3D / D3E turn it into a persisted event with the times they own.

Rejection is whole-page and atomic: a failure on the last element returns zero elements, exactly
like the D1 archive parser. Rejections carry a stable code, a short non-secret detail and the
decoder binding — never the response body, in the value, its ``repr`` or a quality fact.

Caller mistakes (a non-UTC ``retrieved_at``, a body-size limit outside the allowed range, a page
query that is not the previous summary's exact next query) are **not** page rejections: they are
``RestDecodeRequestError``, because the source is not at fault and such a verdict must never be
persisted as evidence about Binance.

Every rule lives in ``DECODER_SPEC``; ``DECODER_BINDING.policy_hash`` is the SHA-256 of its
canonical JSON, derived rather than written down, so any rule change changes the hash and must
change the version.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from core.contracts.revision import PolicyBinding, PolicyRole
from core.domain.base import canonical_json
from infrastructure.contract_version import PHASE1_PUBLICATION_VERSION
from infrastructure.revision.rest_identity import (
    DATA_TYPES,
    DECLARED_TIME_UNIT,
    RestIdentityViolation,
    RestPageQuery,
)

__all__ = [
    "AGG_TRADE_KEYS",
    "DECODER_BINDING",
    "DECODER_HASH",
    "DECODER_ID",
    "DECODER_SPEC",
    "DECODER_VERSION",
    "KLINE_SLOTS",
    "MAX_BODY_LIMIT_BYTES",
    "MINUTE_MS",
    "MIN_BODY_LIMIT_BYTES",
    "AnsweredInterval",
    "RestAggTradeElement",
    "RestDecodeOutcome",
    "RestDecodeRequestError",
    "RestElement",
    "RestKline1mElement",
    "RestPageDecodeRequest",
    "RestPageDecoded",
    "RestPageRejection",
    "RestPageSummary",
    "RestQualityFact",
    "RestQualityFactType",
    "RestQuantityUnit",
    "RestRejectionCode",
    "RestStopReason",
    "decode_rest_page",
]

DECODER_ID: Final = "binance.spot.rest.decoder"
DECODER_VERSION: Final = "1.0.0"

#: Allowed range of the operational body-size limit (``HLENS_BINANCE_REST_MAX_RESPONSE_BYTES``,
#: ADR-0027 §12). The *value* is D3D's setting and an explicit argument; only this range is part
#: of the decoder rule, so the decoder hash does not depend on any deployment's configuration.
MIN_BODY_LIMIT_BYTES: Final = 65_536
MAX_BODY_LIMIT_BYTES: Final = 67_108_864

MINUTE_MS: Final = 60_000
#: Exact keys of one ``GET /api/v3/aggTrades`` element (evidence R3).
AGG_TRADE_KEYS: Final[frozenset[str]] = frozenset({"a", "p", "q", "f", "l", "T", "m", "M"})
#: Exact number of slots of one ``GET /api/v3/klines`` element (evidence R8).
KLINE_SLOTS: Final = 12

_AGG_KEY_ORDER: Final[tuple[str, ...]] = ("a", "p", "q", "f", "l", "T", "m", "M")
_INT64_MAX: Final = (1 << 63) - 1
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND: Final = timedelta(microseconds=1)
_US_PER_MS: Final = 1_000
_MAX_DETAIL_CHARS: Final = 240
#: Longest JSON integer literal accepted before range checking (bounded work on hostile input).
_MAX_INT_LITERAL_CHARS: Final = 20

_DECIMAL_PRECISION: Final = 38
_DECIMAL_SCALE: Final = 18
#: Same grammar as the D1 archive parser: plain, non-negative, exactly ``decimal(38, 18)``-able.
_DECIMAL_PATTERN: Final = (
    rf"(?:0|[1-9][0-9]{{0,{_DECIMAL_PRECISION - _DECIMAL_SCALE - 1}}})"
    rf"(?:\.[0-9]{{1,{_DECIMAL_SCALE}}})?"
)
_DECIMAL_RE: Final = re.compile(_DECIMAL_PATTERN)
_DECIMAL_SHAPE_RE: Final = re.compile(r"[0-9]+(?:\.[0-9]+)?")


class RestDecodeRequestError(ValueError):
    """The caller's decode request is not well formed; nothing about the source is implied."""


class RestStopReason(StrEnum):
    """Why the chain stops after this page (ADR-0027 §6); absent means continue."""

    REACHED_TARGET_END = "reached_target_end"
    UNCLOSED_TAIL = "unclosed_tail"
    SHORT_PAGE = "short_page"
    EMPTY_PAGE = "empty_page"


class RestQualityFactType(StrEnum):
    """Structured facts about an *accepted* page (ADR-0027 §7 / open obligations)."""

    #: aggTrade ids are not guaranteed contiguous (evidence N2): ids are missing inside the answer.
    REST_AGG_TRADE_ID_GAP = "rest_agg_trade_id_gap"
    #: Minutes missing inside the page's answered window (a quality fact, not a coverage gap).
    REST_WINDOW_GAP = "rest_window_gap"
    #: The final item is an unclosed 1m kline: kept in the bytes, never a decoded element.
    REST_UNCLOSED_KLINE_SKIPPED = "rest_unclosed_kline_skipped"


class RestQuantityUnit(StrEnum):
    """Unit of a quality fact's ``range_start`` / ``range_end``; never guessed from magnitude."""

    AGG_TRADE_ID = "agg_trade_id"
    EPOCH_MILLISECOND = "epoch_millisecond"


class RestRejectionCode(StrEnum):
    """Stable whole-page rejection reasons; values are persisted tokens, add-only."""

    BODY_TOO_LARGE = "body_too_large"
    INVALID_UTF8 = "invalid_utf8"
    BYTE_ORDER_MARK = "byte_order_mark"
    INVALID_JSON = "invalid_json"
    TRAILING_CONTENT = "trailing_content"
    DUPLICATE_JSON_KEY = "duplicate_json_key"
    NON_FINITE_NUMBER = "non_finite_number"
    FRACTIONAL_NUMBER = "fractional_number"
    TOP_LEVEL_NOT_ARRAY = "top_level_not_array"
    PAGE_TOO_LONG = "page_too_long"
    ELEMENT_SHAPE = "element_shape"
    INVALID_INTEGER = "invalid_integer"
    INTEGER_OUT_OF_RANGE = "integer_out_of_range"
    INVALID_DECIMAL = "invalid_decimal"
    DECIMAL_OUT_OF_RANGE = "decimal_out_of_range"
    INVALID_BOOLEAN = "invalid_boolean"
    NON_POSITIVE_PRICE = "non_positive_price"
    NON_POSITIVE_QUANTITY = "non_positive_quantity"
    TRADE_ID_RANGE_INVALID = "trade_id_range_invalid"
    OHLC_INVARIANT = "ohlc_invariant"
    TAKER_VOLUME_INVARIANT = "taker_volume_invariant"
    KLINE_OPEN_NOT_ALIGNED = "kline_open_not_aligned"
    KLINE_CLOSE_TIME_MISMATCH = "kline_close_time_mismatch"
    QUERY_LOWER_BOUND = "query_lower_bound"
    DUPLICATE_ELEMENT = "duplicate_element"
    OUT_OF_ORDER = "out_of_order"
    TIMESTAMP_DECREASING = "timestamp_decreasing"
    TRADE_ID_OVERLAP = "trade_id_overlap"
    FUTURE_TIMESTAMP = "future_timestamp"
    UNCLOSED_KLINE_NOT_LAST = "unclosed_kline_not_last"
    PREVIOUS_PAGE_DISCONTINUITY = "previous_page_discontinuity"
    NEXT_QUERY_UNREPRESENTABLE = "next_query_unrepresentable"
    NON_PROGRESSING_PAGE = "non_progressing_page"


class _Reject(Exception):
    """Internal control flow: leave the decode with a whole-page reason."""

    def __init__(
        self,
        code: RestRejectionCode,
        detail: str,
        *,
        element_index: int | None = None,
        field_name: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.element_index = element_index
        self.field_name = field_name


# --------------------------------------------------------------------------- scalar helpers


def _check_utc(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RestDecodeRequestError(f"{label} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise RestDecodeRequestError(f"{label} must be UTC")
    return value


def _check_epoch_ms(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RestDecodeRequestError(f"{label} must be an int")
    if not 0 <= value <= _INT64_MAX:
        raise RestDecodeRequestError(f"{label} must be a non-negative int64")
    return value


def _micros(value: datetime) -> int:
    return (value - _EPOCH) // _MICROSECOND


def _at_ms(epoch_ms: int) -> datetime:
    """Exact UTC instant of an epoch millisecond; never rounded, never guessed."""
    return _EPOCH + timedelta(milliseconds=epoch_ms)


# --------------------------------------------------------------------------- decoded elements


@dataclass(frozen=True, slots=True)
class RestAggTradeElement:
    """One decoded aggTrade: the native REST fields plus the exact UTC event time."""

    element_index: int
    agg_trade_id: int
    price: Decimal
    quantity: Decimal
    first_trade_id: int
    last_trade_id: int
    timestamp_raw: int
    is_buyer_maker: bool
    is_best_match: bool
    event_time: datetime

    def native_fields(self) -> dict[str, Any]:
        """A fresh mapping of the native columns the D3B identity rule hashes."""
        return {
            "agg_trade_id": self.agg_trade_id,
            "price": self.price,
            "quantity": self.quantity,
            "first_trade_id": self.first_trade_id,
            "last_trade_id": self.last_trade_id,
            "timestamp_raw": self.timestamp_raw,
            "is_buyer_maker": self.is_buyer_maker,
            "is_best_match": self.is_best_match,
        }


@dataclass(frozen=True, slots=True)
class RestKline1mElement:
    """One decoded, **closed** 1m kline: native REST fields plus the exact UTC interval."""

    element_index: int
    open_time_raw: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_time_raw: int
    quote_asset_volume: Decimal
    number_of_trades: int
    taker_buy_base_asset_volume: Decimal
    taker_buy_quote_asset_volume: Decimal
    ignore_raw: str
    interval_start: datetime
    interval_end: datetime

    def native_fields(self) -> dict[str, Any]:
        """A fresh mapping of the native columns the D3B identity rule hashes."""
        return {
            "open_time_raw": self.open_time_raw,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "close_time_raw": self.close_time_raw,
            "quote_asset_volume": self.quote_asset_volume,
            "number_of_trades": self.number_of_trades,
            "taker_buy_base_asset_volume": self.taker_buy_base_asset_volume,
            "taker_buy_quote_asset_volume": self.taker_buy_quote_asset_volume,
            "ignore_raw": self.ignore_raw,
        }


type RestElement = RestAggTradeElement | RestKline1mElement


# --------------------------------------------------------------------------- page summary


@dataclass(frozen=True, slots=True)
class AnsweredInterval:
    """Half-open ``[start_ms, end_ms)`` the source's answer to **this exact query** covers.

    Not a claim about market completeness (ADR-0027 §7). An answer that covers no time is the
    explicit empty interval ``start_ms == end_ms``, anchored on the query's own lower bound: the
    decoder never invents a timestamp to stand in for "nothing".
    """

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        for label, value in (("start_ms", self.start_ms), ("end_ms", self.end_ms)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise RestDecodeRequestError(f"{label} must be an int")
            if not 0 <= value <= _INT64_MAX:
                raise RestDecodeRequestError(f"{label} must be a non-negative int64")
        if self.start_ms > self.end_ms:
            raise RestDecodeRequestError("an answered interval cannot end before it starts")

    @property
    def is_empty(self) -> bool:
        return self.start_ms == self.end_ms


@dataclass(frozen=True, slots=True)
class RestQualityFact:
    """A structured fact about an accepted page; never a rejection, never raw bytes."""

    fact_type: RestQualityFactType
    data_type: str
    symbol: str
    page_index: int
    unit: RestQuantityUnit
    range_start: int
    range_end: int
    occurrences: int
    missing: int
    detail: str


@dataclass(frozen=True, slots=True)
class RestPageSummary:
    """The deterministic summary of one accepted page; also the next page's precondition.

    ``stop_reason`` and ``next_query`` are mutually exclusive: a page either terminates the chain
    or names the exact next query, and the decoder of the next page requires that exact query.
    """

    decoder: PolicyBinding
    data_type: str
    symbol: str
    query: RestPageQuery
    page_index: int
    retrieved_at: datetime
    target_start_ms: int
    target_end_ms: int
    element_count: int
    served_count: int
    answered: AnsweredInterval
    stop_reason: RestStopReason | None
    next_query: RestPageQuery | None
    quality_facts: tuple[RestQualityFact, ...] = ()
    last_agg_trade_id: int | None = None
    last_event_time_ms: int | None = None
    last_trade_id: int | None = None
    last_closed_open_ms: int | None = None

    def __post_init__(self) -> None:
        if self.data_type not in DATA_TYPES:
            raise RestDecodeRequestError(f"unsupported data_type {self.data_type!r}")
        if (self.stop_reason is None) == (self.next_query is None):
            raise RestDecodeRequestError("a page either stops or names exactly one next query")
        if self.next_query is None:
            return
        if self.data_type == "agg_trades":
            if self.last_agg_trade_id is None or self.last_event_time_ms is None:
                raise RestDecodeRequestError("a continuing aggTrades page must have a last element")
        elif self.last_closed_open_ms is None:
            raise RestDecodeRequestError("a continuing klines page must have a last closed kline")

    @property
    def continues(self) -> bool:
        return self.next_query is not None


@dataclass(frozen=True, slots=True)
class RestPageDecoded:
    """An accepted page: immutable elements plus the deterministic summary."""

    summary: RestPageSummary
    elements: tuple[RestElement, ...]

    @property
    def element_count(self) -> int:
        return len(self.elements)


@dataclass(frozen=True, slots=True)
class RestPageRejection:
    """A whole-page rejection: stable code, bounded detail, zero elements, no body.

    Safe to persist as the decode outcome of a response revision (ADR-0027 §2 / §8): the page's
    bytes keep their own object and revision; this value only says *why* no element may be born.
    """

    decoder: PolicyBinding
    data_type: str
    symbol: str
    query: RestPageQuery
    page_index: int
    retrieved_at: datetime
    body_size_bytes: int
    code: RestRejectionCode
    detail: str
    element_index: int | None = None
    field_name: str | None = None

    @property
    def elements(self) -> tuple[RestElement, ...]:
        """Always empty: a rejected page never releases a partially decoded element."""
        return ()


type RestDecodeOutcome = RestPageDecoded | RestPageRejection


# --------------------------------------------------------------------------- decode request


@dataclass(frozen=True, slots=True)
class RestPageDecodeRequest:
    """Everything the decoder is allowed to know about one page.

    ``target_start_ms`` / ``target_end_ms`` are the collection target window ``[t0, t1)``. They
    drive **only** the stop reason and the answered-coverage anchor; they never reject a page or
    an element (ADR-0027 §6). The first page of a chain is the one the ADR defines — ``startTime
    = t0`` — and every later page must be the previous summary's exact ``next_query``.
    """

    query: RestPageQuery
    retrieved_at: datetime
    target_start_ms: int
    target_end_ms: int
    max_body_bytes: int
    page_index: int = 0
    previous: RestPageSummary | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.query, RestPageQuery):
            raise RestDecodeRequestError("query must be a validated RestPageQuery")
        retrieved_at = _check_utc(self.retrieved_at, "retrieved_at")
        if _micros(retrieved_at) < 0:
            raise RestDecodeRequestError("retrieved_at must not precede the UNIX epoch")
        start = _check_epoch_ms(self.target_start_ms, "target_start_ms")
        end = _check_epoch_ms(self.target_end_ms, "target_end_ms")
        if start >= end:
            raise RestDecodeRequestError("the collection target window must be non-empty")
        if self.data_type == "klines_1m" and (start % MINUTE_MS or end % MINUTE_MS):
            raise RestDecodeRequestError("a 1m klines target window must be minute-aligned")
        if not isinstance(self.max_body_bytes, int) or isinstance(self.max_body_bytes, bool):
            raise RestDecodeRequestError("max_body_bytes must be an int")
        if not MIN_BODY_LIMIT_BYTES <= self.max_body_bytes <= MAX_BODY_LIMIT_BYTES:
            raise RestDecodeRequestError(
                f"max_body_bytes must be in [{MIN_BODY_LIMIT_BYTES}, {MAX_BODY_LIMIT_BYTES}]"
            )
        if not isinstance(self.page_index, int) or isinstance(self.page_index, bool):
            raise RestDecodeRequestError("page_index must be an int")
        if self.page_index < 0:
            raise RestDecodeRequestError("page_index must not be negative")
        self._check_chain()

    def _check_chain(self) -> None:
        previous = self.previous
        if previous is None:
            if self.page_index != 0:
                raise RestDecodeRequestError("only page 0 may start a chain without a summary")
            if self.query.from_id is not None:
                raise RestDecodeRequestError("the first page of a chain is a startTime page")
            if self.query.start_time_ms != self.target_start_ms:
                raise RestDecodeRequestError("the first page must query startTime = t0")
            return
        if not isinstance(previous, RestPageSummary):
            raise RestDecodeRequestError("previous must be a RestPageSummary")
        if previous.decoder != DECODER_BINDING:
            raise RestDecodeRequestError("previous was produced by another decoder version")
        if previous.next_query is None:
            raise RestDecodeRequestError("the previous page stopped the chain")
        if previous.next_query != self.query:
            raise RestDecodeRequestError("query is not the previous summary's exact next query")
        if previous.page_index + 1 != self.page_index:
            raise RestDecodeRequestError("page_index must follow the previous page")
        if (previous.target_start_ms, previous.target_end_ms) != (
            self.target_start_ms,
            self.target_end_ms,
        ):
            raise RestDecodeRequestError("the collection target window changed inside one chain")
        if previous.retrieved_at > self.retrieved_at:
            raise RestDecodeRequestError("retrieved_at moved backwards inside one chain")

    @property
    def data_type(self) -> str:
        return self.query.data_type

    @property
    def symbol(self) -> str:
        return self.query.symbol

    @property
    def retrieved_at_us(self) -> int:
        return _micros(self.retrieved_at)


# --------------------------------------------------------------------------- strict JSON


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names):
        raise _Reject(RestRejectionCode.DUPLICATE_JSON_KEY, "an object repeats a key")
    return dict(pairs)


def _no_constant(literal: str) -> Any:
    raise _Reject(
        RestRejectionCode.NON_FINITE_NUMBER, f"the body contains the JSON constant {literal}"
    )


def _no_float(literal: str) -> Any:
    raise _Reject(
        RestRejectionCode.FRACTIONAL_NUMBER,
        "the body contains a fractional or exponent JSON number; "
        "integers must be JSON integers and decimals must be JSON strings",
    )


def _json_int(literal: str) -> int:
    if len(literal) > _MAX_INT_LITERAL_CHARS:
        raise _Reject(
            RestRejectionCode.INTEGER_OUT_OF_RANGE,
            f"a JSON integer literal is longer than {_MAX_INT_LITERAL_CHARS} characters",
        )
    return int(literal)


#: RFC 8259 §2 insignificant whitespace — the only characters allowed to frame the top-level
#: value. Deliberately not ``str.isspace()``: U+00A0, U+2028 and friends are trailing content.
_JSON_WHITESPACE: Final = frozenset(" \t\n\r")


def _skip_json_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index] in _JSON_WHITESPACE:
        index += 1
    return index


_DECODER: Final = json.JSONDecoder(
    object_pairs_hook=_no_duplicate_keys,
    parse_constant=_no_constant,
    parse_float=_no_float,
    parse_int=_json_int,
)


def _parse_body(request: RestPageDecodeRequest, body: bytes) -> list[Any]:
    """Untrusted bytes → a top-level JSON array of at most ``limit`` items, or ``_Reject``."""
    if len(body) > request.max_body_bytes:
        raise _Reject(
            RestRejectionCode.BODY_TOO_LARGE,
            f"body of {len(body)} bytes exceeds the {request.max_body_bytes} byte limit",
        )
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _Reject(
            RestRejectionCode.INVALID_UTF8, f"body is not UTF-8 at byte {exc.start}"
        ) from None
    if text.startswith("﻿"):
        raise _Reject(RestRejectionCode.BYTE_ORDER_MARK, "body starts with a byte order mark")
    try:
        value, end = _DECODER.raw_decode(text, _skip_json_whitespace(text, 0))
    except json.JSONDecodeError as exc:
        raise _Reject(RestRejectionCode.INVALID_JSON, f"{exc.msg} at character {exc.pos}") from None
    tail = _skip_json_whitespace(text, end)
    if tail != len(text):
        raise _Reject(
            RestRejectionCode.TRAILING_CONTENT,
            f"non-whitespace content follows the top-level JSON value at character {tail}",
        )
    if not isinstance(value, list):
        raise _Reject(
            RestRejectionCode.TOP_LEVEL_NOT_ARRAY,
            f"top-level JSON value is {type(value).__name__}, not an array",
        )
    if len(value) > request.query.limit:
        raise _Reject(
            RestRejectionCode.PAGE_TOO_LONG,
            f"page has {len(value)} items, more than the requested limit {request.query.limit}",
        )
    return value


# --------------------------------------------------------------------------- field grammar


def _int_field(value: object, label: str, index: int) -> int:
    if isinstance(value, bool):
        raise _Reject(
            RestRejectionCode.INVALID_INTEGER,
            f"{label} is a JSON boolean, not an integer",
            element_index=index,
            field_name=label,
        )
    if not isinstance(value, int):
        raise _Reject(
            RestRejectionCode.INVALID_INTEGER,
            f"{label} is not a JSON integer",
            element_index=index,
            field_name=label,
        )
    if not 0 <= value <= _INT64_MAX:
        raise _Reject(
            RestRejectionCode.INTEGER_OUT_OF_RANGE,
            f"{label} is not a non-negative int64",
            element_index=index,
            field_name=label,
        )
    return value


def _decimal_text(value: object, label: str, index: int) -> str:
    """The source text of a decimal field, proven exactly representable in ``decimal(38, 18)``."""
    if not isinstance(value, str):
        raise _Reject(
            RestRejectionCode.INVALID_DECIMAL,
            f"{label} is not a JSON string",
            element_index=index,
            field_name=label,
        )
    if _DECIMAL_RE.fullmatch(value) is not None:
        return value
    if _DECIMAL_SHAPE_RE.fullmatch(value) is not None and not (
        len(value) > 1 and value[0] == "0" and value[1] != "."
    ):
        raise _Reject(
            RestRejectionCode.DECIMAL_OUT_OF_RANGE,
            f"{label} exceeds decimal({_DECIMAL_PRECISION},{_DECIMAL_SCALE})",
            element_index=index,
            field_name=label,
        )
    raise _Reject(
        RestRejectionCode.INVALID_DECIMAL,
        f"{label} is not a plain non-negative decimal",
        element_index=index,
        field_name=label,
    )


def _decimal_field(value: object, label: str, index: int) -> Decimal:
    return Decimal(_decimal_text(value, label, index))


def _bool_field(value: object, label: str, index: int) -> bool:
    if not isinstance(value, bool):
        raise _Reject(
            RestRejectionCode.INVALID_BOOLEAN,
            f"{label} is not a JSON boolean",
            element_index=index,
            field_name=label,
        )
    return value


def _check_not_future(epoch_ms: int, label: str, index: int, retrieved_at_us: int) -> None:
    """Structural unit check: a millisecond value read as microseconds lands in the far future."""
    if epoch_ms * _US_PER_MS > retrieved_at_us:
        raise _Reject(
            RestRejectionCode.FUTURE_TIMESTAMP,
            f"{label} is after retrieved_at",
            element_index=index,
            field_name=label,
        )


# --------------------------------------------------------------------------- aggTrades


def _agg_shape(item: object, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise _Reject(
            RestRejectionCode.ELEMENT_SHAPE,
            f"item is {type(item).__name__}, not a JSON object",
            element_index=index,
        )
    keys = set(item)
    if keys != AGG_TRADE_KEYS:
        missing = sorted(AGG_TRADE_KEYS - keys)
        raise _Reject(
            RestRejectionCode.ELEMENT_SHAPE,
            f"expected exactly the keys {list(_AGG_KEY_ORDER)}; "
            f"missing {missing}; {len(keys - AGG_TRADE_KEYS)} unexpected",
            element_index=index,
        )
    return item


def _decode_agg_trades(
    request: RestPageDecodeRequest, items: Sequence[Any]
) -> tuple[tuple[RestAggTradeElement, ...], tuple[RestQualityFact, ...]]:
    query = request.query
    retrieved_at_us = request.retrieved_at_us
    previous = request.previous
    elements: list[RestAggTradeElement] = []
    gaps: list[tuple[int, int]] = []
    missing_ids = 0
    previous_row: RestAggTradeElement | None = None

    expected_id: int | None = query.from_id
    if expected_id is None and previous is not None and previous.last_agg_trade_id is not None:
        expected_id = previous.last_agg_trade_id + 1

    for index, item in enumerate(items):
        row = _agg_shape(item, index)
        agg_id = _int_field(row["a"], "a", index)
        price = _decimal_field(row["p"], "p", index)
        quantity = _decimal_field(row["q"], "q", index)
        first_id = _int_field(row["f"], "f", index)
        last_id = _int_field(row["l"], "l", index)
        timestamp_ms = _int_field(row["T"], "T", index)
        buyer_maker = _bool_field(row["m"], "m", index)
        best_match = _bool_field(row["M"], "M", index)
        if price <= 0:
            raise _Reject(
                RestRejectionCode.NON_POSITIVE_PRICE, "p <= 0", element_index=index, field_name="p"
            )
        if quantity <= 0:
            raise _Reject(
                RestRejectionCode.NON_POSITIVE_QUANTITY,
                "q <= 0",
                element_index=index,
                field_name="q",
            )
        if first_id > last_id:
            raise _Reject(
                RestRejectionCode.TRADE_ID_RANGE_INVALID,
                "f > l",
                element_index=index,
                field_name="f",
            )
        if query.start_time_ms is not None and timestamp_ms < query.start_time_ms:
            raise _Reject(
                RestRejectionCode.QUERY_LOWER_BOUND,
                f"T is before the requested startTime {query.start_time_ms}",
                element_index=index,
                field_name="T",
            )
        if query.from_id is not None and agg_id < query.from_id:
            raise _Reject(
                RestRejectionCode.QUERY_LOWER_BOUND,
                f"a is below the requested fromId {query.from_id}",
                element_index=index,
                field_name="a",
            )
        _check_not_future(timestamp_ms, "T", index, retrieved_at_us)
        if previous_row is None:
            _check_agg_page_boundary(previous, agg_id, first_id, timestamp_ms)
        else:
            _check_agg_order(previous_row, agg_id, first_id, timestamp_ms, index)
        if expected_id is not None and agg_id > expected_id:
            gaps.append((expected_id, agg_id))
            missing_ids += agg_id - expected_id
        expected_id = agg_id + 1 if agg_id < _INT64_MAX else None
        element = RestAggTradeElement(
            element_index=index,
            agg_trade_id=agg_id,
            price=price,
            quantity=quantity,
            first_trade_id=first_id,
            last_trade_id=last_id,
            timestamp_raw=timestamp_ms,
            is_buyer_maker=buyer_maker,
            is_best_match=best_match,
            event_time=_at_ms(timestamp_ms),
        )
        elements.append(element)
        previous_row = element

    facts: tuple[RestQualityFact, ...] = ()
    if gaps:
        facts = (
            RestQualityFact(
                fact_type=RestQualityFactType.REST_AGG_TRADE_ID_GAP,
                data_type=query.data_type,
                symbol=query.symbol,
                page_index=request.page_index,
                unit=RestQuantityUnit.AGG_TRADE_ID,
                range_start=gaps[0][0],
                range_end=gaps[0][1],
                occurrences=len(gaps),
                missing=missing_ids,
                detail=(
                    f"{missing_ids} aggTrade id(s) missing in {len(gaps)} gap(s); first gap "
                    f"[{gaps[0][0]}, {gaps[0][1]}); ids are not guaranteed contiguous"
                )[:_MAX_DETAIL_CHARS],
            ),
        )
    return tuple(elements), facts


def _check_agg_page_boundary(
    previous: RestPageSummary | None, agg_id: int, first_id: int, timestamp_ms: int
) -> None:
    if previous is None:
        return
    if previous.last_event_time_ms is not None and timestamp_ms < previous.last_event_time_ms:
        raise _Reject(
            RestRejectionCode.PREVIOUS_PAGE_DISCONTINUITY,
            "the first T is before the previous page's last T",
            element_index=0,
            field_name="T",
        )
    if previous.last_trade_id is not None and first_id <= previous.last_trade_id:
        raise _Reject(
            RestRejectionCode.PREVIOUS_PAGE_DISCONTINUITY,
            "the first f overlaps the previous page's last l",
            element_index=0,
            field_name="f",
        )
    if previous.last_agg_trade_id is not None and agg_id <= previous.last_agg_trade_id:
        raise _Reject(
            RestRejectionCode.PREVIOUS_PAGE_DISCONTINUITY,
            "the first a repeats or precedes the previous page's last a",
            element_index=0,
            field_name="a",
        )


def _check_agg_order(
    previous_row: RestAggTradeElement, agg_id: int, first_id: int, timestamp_ms: int, index: int
) -> None:
    if agg_id == previous_row.agg_trade_id:
        raise _Reject(
            RestRejectionCode.DUPLICATE_ELEMENT,
            "a repeats the previous item",
            element_index=index,
            field_name="a",
        )
    if agg_id < previous_row.agg_trade_id:
        raise _Reject(
            RestRejectionCode.OUT_OF_ORDER,
            "a decreases",
            element_index=index,
            field_name="a",
        )
    if timestamp_ms < previous_row.timestamp_raw:
        raise _Reject(
            RestRejectionCode.TIMESTAMP_DECREASING,
            "T decreases",
            element_index=index,
            field_name="T",
        )
    if first_id <= previous_row.last_trade_id:
        raise _Reject(
            RestRejectionCode.TRADE_ID_OVERLAP,
            "f overlaps the previous item's l",
            element_index=index,
            field_name="f",
        )


# --------------------------------------------------------------------------- klines


_KLINE_DECIMALS: Final[tuple[tuple[int, str], ...]] = (
    (1, "open"),
    (2, "high"),
    (3, "low"),
    (4, "close"),
    (5, "volume"),
    (7, "quote_asset_volume"),
    (9, "taker_buy_base_asset_volume"),
    (10, "taker_buy_quote_asset_volume"),
)


def _kline_shape(item: object, index: int) -> list[Any]:
    if not isinstance(item, list):
        raise _Reject(
            RestRejectionCode.ELEMENT_SHAPE,
            f"item is {type(item).__name__}, not a JSON array",
            element_index=index,
        )
    if len(item) != KLINE_SLOTS:
        raise _Reject(
            RestRejectionCode.ELEMENT_SHAPE,
            f"item has {len(item)} slots, expected exactly {KLINE_SLOTS}",
            element_index=index,
        )
    return item


def _decode_klines(
    request: RestPageDecodeRequest, items: Sequence[Any]
) -> tuple[tuple[RestKline1mElement, ...], tuple[RestQualityFact, ...], int | None]:
    """Decode closed klines; the optional third value is the skipped unclosed kline's open."""
    query = request.query
    start_time_ms = query.start_time_ms
    if start_time_ms is None:  # pragma: no cover - RestPageQuery guarantees it
        raise RestDecodeRequestError("a klines page always carries startTime")
    retrieved_at_us = request.retrieved_at_us
    elements: list[RestKline1mElement] = []
    gaps: list[tuple[int, int]] = []
    missing_minutes = 0
    previous_open: int | None = None
    expected_open = start_time_ms
    unclosed_open: int | None = None
    last_index = len(items) - 1

    for index, item in enumerate(items):
        row = _kline_shape(item, index)
        open_ms = _int_field(row[0], "open_time", index)
        values = {name: _decimal_field(row[slot], name, index) for slot, name in _KLINE_DECIMALS}
        close_ms = _int_field(row[6], "close_time", index)
        trades = _int_field(row[8], "number_of_trades", index)
        ignore_raw = _decimal_text(row[11], "ignore", index)
        for name in ("open", "high", "low", "close"):
            if values[name] <= 0:
                raise _Reject(
                    RestRejectionCode.NON_POSITIVE_PRICE,
                    f"{name} <= 0",
                    element_index=index,
                    field_name=name,
                )
        low, high = values["low"], values["high"]
        open_, close = values["open"], values["close"]
        if not (low <= min(open_, close) and high >= max(open_, close) and low <= high):
            raise _Reject(
                RestRejectionCode.OHLC_INVARIANT,
                "low <= min(open, close) <= max(open, close) <= high violated",
                element_index=index,
            )
        if values["taker_buy_base_asset_volume"] > values["volume"]:
            raise _Reject(
                RestRejectionCode.TAKER_VOLUME_INVARIANT,
                "taker buy base volume > volume",
                element_index=index,
                field_name="taker_buy_base_asset_volume",
            )
        if values["taker_buy_quote_asset_volume"] > values["quote_asset_volume"]:
            raise _Reject(
                RestRejectionCode.TAKER_VOLUME_INVARIANT,
                "taker buy quote volume > quote asset volume",
                element_index=index,
                field_name="taker_buy_quote_asset_volume",
            )
        if open_ms < start_time_ms:
            raise _Reject(
                RestRejectionCode.QUERY_LOWER_BOUND,
                f"open time is before the requested startTime {start_time_ms}",
                element_index=index,
                field_name="open_time",
            )
        if open_ms % MINUTE_MS:
            raise _Reject(
                RestRejectionCode.KLINE_OPEN_NOT_ALIGNED,
                "open time is not on a whole minute",
                element_index=index,
                field_name="open_time",
            )
        if close_ms != open_ms + MINUTE_MS - 1:
            raise _Reject(
                RestRejectionCode.KLINE_CLOSE_TIME_MISMATCH,
                "close time != open time + 1 minute - 1 millisecond",
                element_index=index,
                field_name="close_time",
            )
        _check_not_future(open_ms, "open_time", index, retrieved_at_us)
        if previous_open is not None:
            if open_ms == previous_open:
                raise _Reject(
                    RestRejectionCode.DUPLICATE_ELEMENT,
                    "open time repeats the previous item",
                    element_index=index,
                    field_name="open_time",
                )
            if open_ms < previous_open:
                raise _Reject(
                    RestRejectionCode.OUT_OF_ORDER,
                    "open time decreases",
                    element_index=index,
                    field_name="open_time",
                )
        previous_open = open_ms
        if (open_ms + MINUTE_MS) * _US_PER_MS > retrieved_at_us:
            if index != last_index:
                raise _Reject(
                    RestRejectionCode.UNCLOSED_KLINE_NOT_LAST,
                    "an unclosed 1m kline is not the final item",
                    element_index=index,
                    field_name="open_time",
                )
            unclosed_open = open_ms
            continue
        if open_ms > expected_open:
            gaps.append((expected_open, open_ms))
            missing_minutes += (open_ms - expected_open) // MINUTE_MS
        expected_open = open_ms + MINUTE_MS
        elements.append(
            RestKline1mElement(
                element_index=index,
                open_time_raw=open_ms,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=values["volume"],
                close_time_raw=close_ms,
                quote_asset_volume=values["quote_asset_volume"],
                number_of_trades=trades,
                taker_buy_base_asset_volume=values["taker_buy_base_asset_volume"],
                taker_buy_quote_asset_volume=values["taker_buy_quote_asset_volume"],
                ignore_raw=ignore_raw,
                interval_start=_at_ms(open_ms),
                interval_end=_at_ms(open_ms + MINUTE_MS),
            )
        )

    facts: list[RestQualityFact] = []
    if gaps:
        facts.append(
            RestQualityFact(
                fact_type=RestQualityFactType.REST_WINDOW_GAP,
                data_type=query.data_type,
                symbol=query.symbol,
                page_index=request.page_index,
                unit=RestQuantityUnit.EPOCH_MILLISECOND,
                range_start=gaps[0][0],
                range_end=gaps[0][1],
                occurrences=len(gaps),
                missing=missing_minutes,
                detail=(
                    f"{missing_minutes} minute(s) missing inside the answered window in "
                    f"{len(gaps)} gap(s); first gap [{gaps[0][0]}, {gaps[0][1]})"
                )[:_MAX_DETAIL_CHARS],
            )
        )
    if unclosed_open is not None:
        facts.append(
            RestQualityFact(
                fact_type=RestQualityFactType.REST_UNCLOSED_KLINE_SKIPPED,
                data_type=query.data_type,
                symbol=query.symbol,
                page_index=request.page_index,
                unit=RestQuantityUnit.EPOCH_MILLISECOND,
                range_start=unclosed_open,
                range_end=unclosed_open + MINUTE_MS,
                occurrences=1,
                missing=0,
                detail=(
                    f"the final 1m kline opening at {unclosed_open} is still open at "
                    "retrieved_at; kept in the response bytes only, never an element"
                )[:_MAX_DETAIL_CHARS],
            )
        )
    return tuple(elements), tuple(facts), unclosed_open


# --------------------------------------------------------------------------- page summary


def _next_query(data_type: str, symbol: str, cursor: int, current: RestPageQuery) -> RestPageQuery:
    """The exact continuation query, or ``_Reject`` rather than wrapping or looping."""
    if cursor > _INT64_MAX:
        raise _Reject(
            RestRejectionCode.NEXT_QUERY_UNREPRESENTABLE,
            "the next page cursor does not fit in an int64",
        )
    try:
        following = (
            RestPageQuery.agg_trades_from_id(symbol, cursor)
            if data_type == "agg_trades"
            else RestPageQuery.klines_from_start(symbol, cursor)
        )
    except RestIdentityViolation as exc:
        raise _Reject(
            RestRejectionCode.NEXT_QUERY_UNREPRESENTABLE,
            f"the next page query is not constructible: {exc}",
        ) from None
    if following == current:
        raise _Reject(
            RestRejectionCode.NON_PROGRESSING_PAGE,
            "the next page query repeats this page's query",
        )
    return following


def _agg_summary(
    request: RestPageDecodeRequest,
    served: int,
    elements: tuple[RestAggTradeElement, ...],
    facts: tuple[RestQualityFact, ...],
) -> RestPageSummary:
    query = request.query
    previous = request.previous
    if previous is None:
        lower = request.target_start_ms
    elif previous.last_event_time_ms is None:  # pragma: no cover - summary invariant
        raise RestDecodeRequestError("a continuing aggTrades page must have a last element")
    else:
        lower = previous.last_event_time_ms

    last = elements[-1] if elements else None
    if last is None:
        # An empty continuation page completes the boundary tick the previous page left open;
        # an empty first page answers nothing at all (ADR-0027 §7).
        answered = AnsweredInterval(lower, lower if previous is None else lower + 1)
    elif served == query.limit:
        answered = AnsweredInterval(lower, last.timestamp_raw)
    else:
        answered = AnsweredInterval(lower, last.timestamp_raw + 1)

    stop = _stop_reason(
        reached_target_end=last is not None and last.timestamp_raw >= request.target_end_ms,
        unclosed_tail=False,
        served=served,
        limit=query.limit,
    )
    following = (
        None
        if stop is not None or last is None
        else _next_query("agg_trades", query.symbol, last.agg_trade_id + 1, query)
    )
    return RestPageSummary(
        decoder=DECODER_BINDING,
        data_type="agg_trades",
        symbol=query.symbol,
        query=query,
        page_index=request.page_index,
        retrieved_at=request.retrieved_at,
        target_start_ms=request.target_start_ms,
        target_end_ms=request.target_end_ms,
        element_count=len(elements),
        served_count=served,
        answered=answered,
        stop_reason=stop,
        next_query=following,
        quality_facts=facts,
        last_agg_trade_id=None if last is None else last.agg_trade_id,
        last_event_time_ms=None if last is None else last.timestamp_raw,
        last_trade_id=None if last is None else last.last_trade_id,
    )


def _kline_summary(
    request: RestPageDecodeRequest,
    served: int,
    elements: tuple[RestKline1mElement, ...],
    facts: tuple[RestQualityFact, ...],
    unclosed_open: int | None,
) -> RestPageSummary:
    query = request.query
    lower = query.start_time_ms
    if lower is None:  # pragma: no cover - RestPageQuery guarantees it
        raise RestDecodeRequestError("a klines page always carries startTime")
    last = elements[-1] if elements else None
    # Coverage stops at the unclosed kline's open: the source answered nothing beyond it.
    answered = AnsweredInterval(lower, lower if last is None else last.open_time_raw + MINUTE_MS)

    # Opens increase strictly and an unclosed kline can only be the final item.
    highest_open = unclosed_open if unclosed_open is not None else _last_open(last)
    reached = (not answered.is_empty and answered.end_ms >= request.target_end_ms) or (
        highest_open is not None and highest_open >= request.target_end_ms
    )
    stop = _stop_reason(
        reached_target_end=reached,
        unclosed_tail=unclosed_open is not None,
        served=served,
        limit=query.limit,
    )
    following = (
        None
        if stop is not None or last is None
        else _next_query("klines_1m", query.symbol, last.open_time_raw + MINUTE_MS, query)
    )
    return RestPageSummary(
        decoder=DECODER_BINDING,
        data_type="klines_1m",
        symbol=query.symbol,
        query=query,
        page_index=request.page_index,
        retrieved_at=request.retrieved_at,
        target_start_ms=request.target_start_ms,
        target_end_ms=request.target_end_ms,
        element_count=len(elements),
        served_count=served,
        answered=answered,
        stop_reason=stop,
        next_query=following,
        quality_facts=facts,
        last_closed_open_ms=None if last is None else last.open_time_raw,
    )


def _last_open(last: RestKline1mElement | None) -> int | None:
    return None if last is None else last.open_time_raw


def _stop_reason(
    *, reached_target_end: bool, unclosed_tail: bool, served: int, limit: int
) -> RestStopReason | None:
    """The one deterministic stop reason of ADR-0027 §6, in its fixed order."""
    if reached_target_end:
        return RestStopReason.REACHED_TARGET_END
    if unclosed_tail:
        return RestStopReason.UNCLOSED_TAIL
    if served == 0:
        return RestStopReason.EMPTY_PAGE
    if served < limit:
        return RestStopReason.SHORT_PAGE
    return None


# --------------------------------------------------------------------------- entry point


def decode_rest_page(request: RestPageDecodeRequest, body: bytes) -> RestDecodeOutcome:
    """Decode one complete REST response body under ``binance.spot.rest.decoder@1.0.0``.

    Returns either a ``RestPageDecoded`` (immutable elements + deterministic summary) or one
    ``RestPageRejection`` with zero elements. Raises ``RestDecodeRequestError`` only for a
    malformed request — never for anything the source sent.
    """
    if not isinstance(request, RestPageDecodeRequest):
        raise RestDecodeRequestError("request must be a RestPageDecodeRequest")
    if not isinstance(body, bytes):
        raise RestDecodeRequestError("body must be the complete response entity body as bytes")
    try:
        items = _parse_body(request, body)
        if request.data_type == "agg_trades":
            agg_elements, agg_facts = _decode_agg_trades(request, items)
            summary = _agg_summary(request, len(items), agg_elements, agg_facts)
            elements: tuple[RestElement, ...] = agg_elements
        else:
            kline_elements, kline_facts, unclosed = _decode_klines(request, items)
            summary = _kline_summary(request, len(items), kline_elements, kline_facts, unclosed)
            elements = kline_elements
    except _Reject as reject:
        return RestPageRejection(
            decoder=DECODER_BINDING,
            data_type=request.data_type,
            symbol=request.symbol,
            query=request.query,
            page_index=request.page_index,
            retrieved_at=request.retrieved_at,
            body_size_bytes=len(body),
            code=reject.code,
            detail=reject.detail[:_MAX_DETAIL_CHARS],
            element_index=reject.element_index,
            field_name=reject.field_name,
        )
    return RestPageDecoded(summary=summary, elements=elements)


# --------------------------------------------------------------------------- spec / binding


#: The complete, versioned decoder rule. ``DECODER_HASH`` is the SHA-256 of its canonical JSON.
DECODER_SPEC: Final[dict[str, Any]] = {
    "decoder_id": DECODER_ID,
    "version": DECODER_VERSION,
    "role": PolicyRole.PARSER.value,
    "source": {"source_id": "binance.public.spot.rest", "version": "1.0.0"},
    "identity_rule": "hlens.binance.spot.rest-revision-identity@1.0.0",
    "purity": {
        "inputs": [
            "complete response entity body bytes",
            "validated RestPageQuery",
            "retrieved_at (timezone-aware UTC)",
            "collection target window [t0, t1) in epoch milliseconds",
            "previous accepted page summary (absent on the first page)",
            "operational maximum body size in bytes",
        ],
        "no_io": True,
        "no_clock": True,
        "no_settings_lookup": True,
        "d3d_concerns_not_checked_here": [
            "HTTP status",
            "redirects",
            "content-length and headers",
            "streaming truncation",
            "retries, rate limiting and page budget",
        ],
    },
    "body": {
        "must_be_complete": True,
        "max_bytes_argument_range": [MIN_BODY_LIMIT_BYTES, MAX_BODY_LIMIT_BYTES],
        "oversize_checked_before_json": True,
        "encoding": "utf-8, strict",
        "byte_order_mark": "reject",
    },
    "json": {
        "top_level": "array",
        "framing_whitespace": (
            "0x20, 0x09, 0x0A, 0x0D only (RFC 8259), accepted before and after the top-level "
            "value; no other Unicode whitespace"
        ),
        "trailing_content": "reject any non-whitespace after the top-level value",
        "duplicate_object_keys": "reject at any level",
        "constants": "NaN / Infinity / -Infinity rejected",
        "fractional_numbers": "rejected everywhere (decimals are JSON strings)",
        "max_integer_literal_chars": _MAX_INT_LITERAL_CHARS,
        "page_length": "0 <= len <= query.limit",
    },
    "grammar": {
        "integer": "JSON integer, non-negative int64; a JSON boolean is not an integer",
        "decimal": _DECIMAL_PATTERN,
        "decimal_type": f"decimal({_DECIMAL_PRECISION},{_DECIMAL_SCALE})",
        "decimal_rounding": "never; an inexact value is rejected",
        "boolean": "JSON true / false only",
    },
    "declared_time_unit": DECLARED_TIME_UNIT,
    "time": {
        "conversion": "epoch milliseconds x 1000 = epoch microseconds, exact",
        "magnitude_guessing": False,
        "tolerance": 0,
        "unit_errors_caught_by": ["query lower bound", "retrieved_at upper bound"],
    },
    "data_types": {
        "agg_trades": {
            "path": DATA_TYPES["agg_trades"],
            "element_shape": {"type": "object", "keys": list(_AGG_KEY_ORDER)},
            "field_kinds": {
                "a": "integer",
                "p": "decimal",
                "q": "decimal",
                "f": "integer",
                "l": "integer",
                "T": "integer",
                "m": "boolean",
                "M": "boolean",
            },
            "row_rules": ["p > 0", "q > 0", "f <= l"],
            "query_lower_bound": "T >= startTime (startTime page) or a >= fromId (fromId page)",
            "id_gap": "a > fromId and intra-page id gaps are accepted quality facts",
            "page_order": ["a strictly increasing", "T non-decreasing", "f > previous l"],
            "previous_page": ["first T >= previous last T", "first f > previous last l"],
            "physical_upper_bound": "T <= retrieved_at",
            "continuation": "fromId = last a + 1",
            "answered_interval": {
                "lower": "t0 on the first page, the previous page's last T afterwards",
                "upper_full_page": "last T (exclusive: the tick may continue on the next page)",
                "upper_short_page": "last T + 1",
                "upper_empty_continuation": "previous last T + 1",
                "empty_first_page": "the explicit empty interval [t0, t0)",
            },
        },
        "klines_1m": {
            "path": DATA_TYPES["klines_1m"],
            "element_shape": {"type": "array", "slots": KLINE_SLOTS},
            "field_kinds": [
                "integer open_time",
                "decimal open",
                "decimal high",
                "decimal low",
                "decimal close",
                "decimal volume",
                "integer close_time",
                "decimal quote_asset_volume",
                "integer number_of_trades",
                "decimal taker_buy_base_asset_volume",
                "decimal taker_buy_quote_asset_volume",
                "decimal ignore (source text preserved)",
            ],
            "row_rules": [
                "open, high, low, close > 0",
                "low <= min(open, close) and high >= max(open, close)",
                "taker_buy_base_asset_volume <= volume",
                "taker_buy_quote_asset_volume <= quote_asset_volume",
                "open time aligned to a whole minute",
                "close_time == open_time + 60000 - 1 millisecond",
            ],
            "query_lower_bound": "open >= startTime",
            "page_order": ["open strictly increasing"],
            "previous_page": "the continuation query is the previous summary's exact next query",
            "physical_upper_bound": "open <= retrieved_at",
            "unclosed_kline": {
                "definition": "exclusive interval end (open + 60000) is after retrieved_at",
                "allowed": "at most one, and only as the final item",
                "otherwise": "whole page rejected",
                "element": "never decoded; preserved only in the response bytes",
                "fact": RestQualityFactType.REST_UNCLOSED_KLINE_SKIPPED.value,
                "stop_reason": RestStopReason.UNCLOSED_TAIL.value,
                "answered_upper_bound": "stops at the unclosed kline's open",
            },
            "minute_gap": "missing minutes inside the answered window are quality facts",
            "continuation": "startTime = last closed open + 60000",
            "answered_interval": {
                "lower": "this page's startTime",
                "upper": "last closed open + 60000",
                "no_closed_kline": "the explicit empty interval [startTime, startTime)",
            },
        },
    },
    "collection_target_window": {
        "drives": ["stop reason", "answered coverage anchor"],
        "never_rejects": "a valid element at or after t1 stays a decoded element",
        "klines_alignment": "t0 and t1 must be minute-aligned",
    },
    "first_page": "startTime = t0 (the implicit 'most recent' mode is not constructible)",
    "chain": {
        "query_must_equal_previous_next_query": True,
        "page_index_must_follow": True,
        "target_window_must_not_change": True,
        "retrieved_at_must_not_move_backwards": True,
        "violation": "RestDecodeRequestError (a caller error, never a source verdict)",
    },
    "stop_reasons": [
        RestStopReason.REACHED_TARGET_END.value,
        RestStopReason.UNCLOSED_TAIL.value,
        RestStopReason.EMPTY_PAGE.value,
        RestStopReason.SHORT_PAGE.value,
    ],
    "stop_order": [
        "reached_target_end",
        "unclosed_tail",
        "empty_page (len == 0)",
        "short_page (0 < len < limit)",
        "otherwise continue",
    ],
    "continuation_guards": ["int64 overflow", "non-progressing next query"],
    "quality_facts": {
        fact.value: unit.value
        for fact, unit in (
            (RestQualityFactType.REST_AGG_TRADE_ID_GAP, RestQuantityUnit.AGG_TRADE_ID),
            (RestQualityFactType.REST_WINDOW_GAP, RestQuantityUnit.EPOCH_MILLISECOND),
            (RestQualityFactType.REST_UNCLOSED_KLINE_SKIPPED, RestQuantityUnit.EPOCH_MILLISECOND),
        )
    },
    "rejection": {
        "scope": "whole page; zero elements escape, including on a late-row failure",
        "codes": sorted(code.value for code in RestRejectionCode),
        "max_detail_chars": _MAX_DETAIL_CHARS,
        "never_included": [
            "the response body",
            "unexpected JSON key names",
            "credentials",
        ],
    },
}
DECODER_HASH: Final = hashlib.sha256(canonical_json(DECODER_SPEC).encode("utf-8")).hexdigest()

#: ``PolicyBinding(role=parser)`` for the decoder; the hash is derived, never written down.
DECODER_BINDING: Final = PolicyBinding(
    schema_version=PHASE1_PUBLICATION_VERSION,
    role=PolicyRole.PARSER,
    policy_id=DECODER_ID,
    version=DECODER_VERSION,
    policy_hash=DECODER_HASH,
)

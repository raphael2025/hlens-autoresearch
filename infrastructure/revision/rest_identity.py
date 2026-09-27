"""Stable REST revision identity ``hlens.binance.spot.rest-revision-identity@1.0.0`` (Phase 1 D3B).

ADR-0027 §2 / §3 / §5 / §11. This rule is **physically separate** from the D2 archive rule
(``infrastructure/revision/identity.py``): that module's ``IDENTITY_HASH`` is part of every
archive ``revision_id``, so adding REST to it would move every committed archive revision.
This module never imports it; the grammar the two rules share (element observation keys, the
element payload document) is written out again here and a cross-module test proves both rules
produce the same strings for the same logical element.

Everything an identity depends on lives in ``REST_IDENTITY_SPEC``; ``REST_IDENTITY_HASH`` is the
SHA-256 of its canonical JSON, derived rather than written down, so any rule change changes it.

What the rule fixes:

- **canonical page identity** — ``{method, origin, path, query, declared_time_unit, rule}``, with
  an allow-listed, name-sorted query. The implicit "most recent" mode (neither ``fromId`` nor
  ``startTime``) cannot be constructed. ``page_identity_sha256`` is its canonical-JSON SHA-256;
  the logical collection attempt (``CollectionRequest.request_id``) is **not** part of it;
- **response revision** — ``observation_key = binance:spot:rest:<page_identity_sha256>``; the
  payload hash is the SHA-256 of the response entity body (computed by storage, checked here);
- **element revision** — observation keys character-identical to the archive rule, a
  channel-level source identity (no response revision id inside), and the same native-field
  payload document as D2 with the declared REST time unit;
- ``revision_id`` — ``rev1-<sha256>`` of ``{rule id + version + hash, observation key, source
  identity, payload hash}``; nothing arrival-order or wall-clock dependent enters it;
- ``edge_id`` — ``edge1-<sha256>`` of ``{policy id + version + hash, observation key, revision
  id, superseded revision id}``: **no** knowledge time, so a replayed comparison can never mint
  a second identity for the same edge (ADR-0027 §4.6);
- ``arrival_seq`` — the REST interval ``[2**62, 2**63)``: one block of ``2**32`` per response
  revision, the response row at the block base and element ``i`` at ``base + i + 1``. The archive
  interval ``[0, 2**62)`` belongs to the unchanged D2 allocator. ``arrival_seq`` only has to be
  unique inside one aggregated ``RevisionGraph``; it orders nothing.

No I/O, no clock, no network: every function here is a pure function of its arguments.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from core.contracts.collector import NETWORK_ORIGIN_PATTERN
from core.contracts.revision import PolicyBinding, PolicyRole
from core.domain.base import canonical_json

__all__ = [
    "AGG_TRADES_PATH",
    "ARCHIVE_ARRIVAL_SEQ_LIMIT",
    "DATA_TYPES",
    "DECLARED_TIME_UNIT",
    "KLINES_PATH",
    "MAX_ARRIVAL_SEQ",
    "PAGE_LIMIT",
    "REST_ARRIVAL_SEQ_BASE",
    "REST_ARRIVAL_SEQ_STRIDE",
    "REST_IDENTITY_HASH",
    "REST_IDENTITY_RULE_ID",
    "REST_IDENTITY_RULE_VERSION",
    "REST_IDENTITY_SPEC",
    "REST_SOURCE_ID",
    "REST_SOURCE_VERSION",
    "RestArrivalSeqOverflow",
    "RestIdentityViolation",
    "RestPageQuery",
    "agg_trade_observation_key",
    "agg_trade_payload_hash",
    "check_archive_interval_arrival_seq",
    "check_rest_arrival_seq",
    "edge_id",
    "element_arrival_seq",
    "kline_1m_observation_key",
    "kline_1m_payload_hash",
    "page_identity_document",
    "page_identity_sha256",
    "page_source_uri",
    "response_object_key",
    "response_observation_key",
    "rest_arrival_block_base",
    "rest_source_identity",
    "revision_id",
]

REST_IDENTITY_RULE_ID: Final = "hlens.binance.spot.rest-revision-identity"
REST_IDENTITY_RULE_VERSION: Final = "1.0.0"

VENUE: Final = "binance"
MARKET: Final = "spot"
REST_SOURCE_ID: Final = "binance.public.spot.rest"
REST_SOURCE_VERSION: Final = "1.0.0"

#: The only declared unit of 1.0.0: no ``X-MBX-TIME-UNIT`` header is sent (evidence N8 / R12).
DECLARED_TIME_UNIT: Final = "millisecond"
#: Page size constant of the identity rule (official maximum, evidence R3 / R8); not a setting.
PAGE_LIMIT: Final = 1000
_MINUTE_MS: Final = 60_000

AGG_TRADES_PATH: Final = "/api/v3/aggTrades"
KLINES_PATH: Final = "/api/v3/klines"
#: ``data_type`` → frozen request path.
DATA_TYPES: Final[dict[str, str]] = {"agg_trades": AGG_TRADES_PATH, "klines_1m": KLINES_PATH}

#: ``arrival_seq`` layout (ADR-0027 §11). ``RevisionRecord.arrival_seq`` is an Iceberg ``long``.
ARCHIVE_ARRIVAL_SEQ_LIMIT: Final = 1 << 62
REST_ARRIVAL_SEQ_BASE: Final = 1 << 62
REST_ARRIVAL_SEQ_STRIDE: Final = 1 << 32
MAX_ARRIVAL_SEQ: Final = (1 << 63) - 1

_INT64_MAX: Final = (1 << 63) - 1
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_RE: Final = re.compile(r"^[A-Z0-9]{1,32}$")
#: Canonical non-negative decimal integer: no sign, no leading zero, ASCII digits only.
_CANONICAL_INT_RE: Final = re.compile(r"^(0|[1-9][0-9]*)$")
_ORIGIN_RE: Final = re.compile(NETWORK_ORIGIN_PATTERN)
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_OBJECT_KEY_PREFIX: Final = "raw/binance/spot/rest/responses"


class RestIdentityViolation(ValueError):
    """An identity input is not well formed; nothing may be derived from it (fail closed)."""


class RestArrivalSeqOverflow(RestIdentityViolation):
    """An arrival sequence number would leave its block or the REST interval."""


# --------------------------------------------------------------------------- scalar checks


def _check_symbol(symbol: object) -> str:
    if not isinstance(symbol, str) or _SYMBOL_RE.fullmatch(symbol) is None:
        raise RestIdentityViolation(f"symbol {symbol!r} is not a venue-native upper-case symbol")
    return symbol


def _check_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RestIdentityViolation(f"{label} must be an int")
    if not 0 <= value <= _INT64_MAX:
        raise RestIdentityViolation(f"{label} must be a non-negative int64")
    return value


def _check_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise RestIdentityViolation(f"{label} must be lower-case SHA-256 hex")
    return value


def _check_origin(origin: object) -> str:
    """Exact ``https://host[:port]`` (the configured market-data-only origin; no path, no query)."""
    if not isinstance(origin, str) or _ORIGIN_RE.fullmatch(origin) is None:
        raise RestIdentityViolation(f"origin {origin!r} is not an exact https://host[:port]")
    authority = origin.removeprefix("https://")
    if ":" in authority:
        port = authority.rsplit(":", 1)[1]
        if port.startswith("0") or not 1 <= int(port) <= 65535:
            raise RestIdentityViolation(f"origin {origin!r} has an invalid port")
    return origin


def _check_utc(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RestIdentityViolation(f"{label} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise RestIdentityViolation(f"{label} must be UTC")
    return value


def _micros(value: datetime) -> int:
    return (value - _EPOCH) // timedelta(microseconds=1)


def _digest(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- page identity


@dataclass(frozen=True, slots=True)
class RestPageQuery:
    """One allow-listed REST page query of ``binance.public.spot.rest@1.0.0`` (ADR-0027 §5).

    Construction is the allowlist: ``aggTrades`` takes exactly one of ``fromId`` / ``startTime``;
    ``klines`` takes ``interval=1m`` and a minute-aligned ``startTime``; ``limit`` is always the
    rule constant; ``endTime`` / ``timeZone`` and every other parameter are unrepresentable, so
    the implicit "most recent" mode cannot be built.
    """

    data_type: str
    symbol: str
    from_id: int | None = None
    start_time_ms: int | None = None
    limit: int = PAGE_LIMIT

    def __post_init__(self) -> None:
        if self.data_type not in DATA_TYPES:
            raise RestIdentityViolation(f"unsupported data_type {self.data_type!r}")
        _check_symbol(self.symbol)
        if self.limit != PAGE_LIMIT or isinstance(self.limit, bool):
            raise RestIdentityViolation(f"limit must be the rule constant {PAGE_LIMIT}")
        if self.from_id is not None:
            _check_int(self.from_id, "fromId")
        if self.start_time_ms is not None:
            _check_int(self.start_time_ms, "startTime")
        if self.data_type == "agg_trades":
            if (self.from_id is None) == (self.start_time_ms is None):
                raise RestIdentityViolation(
                    "an aggTrades page takes exactly one of fromId / startTime (the implicit "
                    "'most recent' mode is not constructible)"
                )
        else:
            if self.from_id is not None:
                raise RestIdentityViolation("klines pages do not take fromId")
            if self.start_time_ms is None:
                raise RestIdentityViolation(
                    "a klines page needs startTime (the implicit 'most recent' mode is not "
                    "constructible)"
                )
            if self.start_time_ms % _MINUTE_MS:
                raise RestIdentityViolation("a 1m klines startTime must be minute-aligned")

    @classmethod
    def agg_trades_from_start(cls, symbol: str, start_time_ms: int) -> RestPageQuery:
        """First aggTrades page of a chain: ``startTime = t0`` (evidence R11)."""
        return cls("agg_trades", symbol, start_time_ms=start_time_ms)

    @classmethod
    def agg_trades_from_id(cls, symbol: str, from_id: int) -> RestPageQuery:
        """aggTrades continuation page: ``fromId = previous last a + 1`` (INCLUSIVE, R3)."""
        return cls("agg_trades", symbol, from_id=from_id)

    @classmethod
    def klines_from_start(cls, symbol: str, start_time_ms: int) -> RestPageQuery:
        """1m klines page from ``startTime`` (first page or ``last closed open + 60_000``)."""
        return cls("klines_1m", symbol, start_time_ms=start_time_ms)

    @classmethod
    def from_pairs(cls, data_type: str, pairs: Sequence[tuple[str, str]]) -> RestPageQuery:
        """Parse a query given as ``(name, value)`` pairs; the canonical form is required.

        Unknown, duplicate or missing parameters, non-canonical integers (sign, leading zero,
        non-ASCII digit) and a wrong ``interval`` all fail closed. The pairs must already be in
        canonical (name-sorted) order: a query that would need rewriting is not this identity.
        """
        if data_type not in DATA_TYPES:
            raise RestIdentityViolation(f"unsupported data_type {data_type!r}")
        names = [name for name, _ in pairs]
        if len(set(names)) != len(names):
            raise RestIdentityViolation("query parameters must not repeat")
        if names != sorted(names):
            raise RestIdentityViolation("query parameters must be in canonical (sorted) order")
        allowed = (
            {"symbol", "fromId", "startTime", "limit"}
            if data_type == "agg_trades"
            else {"symbol", "interval", "startTime", "limit"}
        )
        extra = sorted(set(names) - allowed)
        if extra:
            raise RestIdentityViolation(f"query parameters not in the allowlist: {extra}")
        values = dict(pairs)
        if "symbol" not in values or "limit" not in values:
            raise RestIdentityViolation("symbol and limit are required")
        if data_type == "klines_1m" and values.get("interval") != "1m":
            raise RestIdentityViolation("klines pages must carry interval=1m")
        query = cls(
            data_type,
            values["symbol"],
            from_id=_parse_int(values["fromId"], "fromId") if "fromId" in values else None,
            start_time_ms=(
                _parse_int(values["startTime"], "startTime") if "startTime" in values else None
            ),
            limit=_parse_int(values["limit"], "limit"),
        )
        if query.pairs() != tuple(pairs):
            raise RestIdentityViolation("query is not in its canonical encoding")
        return query

    @property
    def path(self) -> str:
        return DATA_TYPES[self.data_type]

    def pairs(self) -> tuple[tuple[str, str], ...]:
        """The name-sorted canonical ``(name, value)`` pairs actually sent."""
        items: list[tuple[str, str]] = [("limit", str(self.limit)), ("symbol", self.symbol)]
        if self.data_type == "klines_1m":
            items.append(("interval", "1m"))
        if self.from_id is not None:
            items.append(("fromId", str(self.from_id)))
        if self.start_time_ms is not None:
            items.append(("startTime", str(self.start_time_ms)))
        return tuple(sorted(items))

    def query_string(self) -> str:
        """``name=value&…`` in canonical order; values are ASCII alphanumerics (no escaping)."""
        return "&".join(f"{name}={value}" for name, value in self.pairs())


def _parse_int(text: str, label: str) -> int:
    if not isinstance(text, str) or _CANONICAL_INT_RE.fullmatch(text) is None:
        raise RestIdentityViolation(f"{label} {text!r} is not a canonical non-negative integer")
    return _check_int(int(text), label)


def page_identity_document(query: RestPageQuery, origin: str) -> dict[str, Any]:
    """The canonical page identity (ADR-0027 §5); the logical request id is not part of it."""
    if not isinstance(query, RestPageQuery):
        raise RestIdentityViolation("query must be a RestPageQuery")
    return {
        "method": "GET",
        "origin": _check_origin(origin),
        "path": query.path,
        "query": [[name, value] for name, value in query.pairs()],
        "declared_time_unit": DECLARED_TIME_UNIT,
        "rule": f"{REST_IDENTITY_RULE_ID}@{REST_IDENTITY_RULE_VERSION}",
    }


def page_identity_sha256(query: RestPageQuery, origin: str) -> str:
    """SHA-256 of the canonical page identity document."""
    return _digest(page_identity_document(query, origin))


def page_source_uri(query: RestPageQuery, origin: str) -> str:
    """The exact request URI of the page (``origin + path + '?' + canonical query``).

    Pure string construction for provenance cross-checks; the origin is the caller's configured
    market-data-only base, never a constant of this module.
    """
    if not isinstance(query, RestPageQuery):
        raise RestIdentityViolation("query must be a RestPageQuery")
    return f"{_check_origin(origin)}{query.path}?{query.query_string()}"


def response_observation_key(page_identity: str) -> str:
    """Observation key of a response revision: the answer to one exact page query."""
    return f"{VENUE}:{MARKET}:rest:{_check_sha256(page_identity, 'page_identity_sha256')}"


def response_object_key(data_type: str, symbol: str, body_sha256: str, page_identity: str) -> str:
    """Content-addressed warehouse key of a response body (ADR-0027 §2)."""
    if data_type not in DATA_TYPES:
        raise RestIdentityViolation(f"unsupported data_type {data_type!r}")
    _check_symbol(symbol)
    body = _check_sha256(body_sha256, "body sha256")
    page = _check_sha256(page_identity, "page_identity_sha256")
    return f"{_OBJECT_KEY_PREFIX}/{body}/{data_type}/{symbol}/{page}.json"


# --------------------------------------------------------------------------- element keys


def agg_trade_observation_key(symbol: str, agg_trade_id: int) -> str:
    """Same grammar as the archive rule: venue + market + symbol + ``agg_trade_id``."""
    _check_symbol(symbol)
    _check_int(agg_trade_id, "agg_trade_id")
    return f"{VENUE}:{MARKET}:agg_trade:{symbol}:{agg_trade_id}"


def kline_1m_observation_key(symbol: str, interval_start: datetime) -> str:
    """Same grammar as the archive rule: … + interval + ``interval_start`` epoch microseconds."""
    _check_symbol(symbol)
    _check_utc(interval_start, "interval_start")
    return f"{VENUE}:{MARKET}:kline:{symbol}:1m:{_micros(interval_start)}"


def rest_source_identity() -> str:
    """Channel-level source identity of every REST response and element revision."""
    return f"{REST_SOURCE_ID}@{REST_SOURCE_VERSION}"


# --------------------------------------------------------------------------- payload hashes


def _decimal_text(value: object, label: str) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise RestIdentityViolation(f"{label} must be a finite Decimal")
    return format(value, "f")


def _bool(row: dict[str, Any], column: str) -> bool:
    value = row[column]
    if not isinstance(value, bool):
        raise RestIdentityViolation(f"{column} must be a bool")
    return value


def agg_trade_payload_hash(symbol: str, row: dict[str, Any]) -> str:
    """Native fields + instrument identity + declared unit (same document as the D2 rule)."""
    _check_symbol(symbol)
    document = {
        "kind": "binance.spot.agg_trade/1",
        "venue": VENUE,
        "market": MARKET,
        "symbol": symbol,
        "time_unit": DECLARED_TIME_UNIT,
        "agg_trade_id": _check_int(row["agg_trade_id"], "agg_trade_id"),
        "price": _decimal_text(row["price"], "price"),
        "quantity": _decimal_text(row["quantity"], "quantity"),
        "first_trade_id": _check_int(row["first_trade_id"], "first_trade_id"),
        "last_trade_id": _check_int(row["last_trade_id"], "last_trade_id"),
        "timestamp_raw": _check_int(row["timestamp_raw"], "timestamp_raw"),
        "is_buyer_maker": _bool(row, "is_buyer_maker"),
        "is_best_match": _bool(row, "is_best_match"),
    }
    return _digest(document)


def kline_1m_payload_hash(symbol: str, row: dict[str, Any]) -> str:
    """Native fields + instrument identity + declared unit (same document as the D2 rule)."""
    _check_symbol(symbol)
    ignore_raw = row["ignore_raw"]
    if not isinstance(ignore_raw, str):
        raise RestIdentityViolation("ignore_raw must be the source text")
    document = {
        "kind": "binance.spot.kline_1m/1",
        "venue": VENUE,
        "market": MARKET,
        "symbol": symbol,
        "interval": "1m",
        "time_unit": DECLARED_TIME_UNIT,
        "open_time_raw": _check_int(row["open_time_raw"], "open_time_raw"),
        "open": _decimal_text(row["open"], "open"),
        "high": _decimal_text(row["high"], "high"),
        "low": _decimal_text(row["low"], "low"),
        "close": _decimal_text(row["close"], "close"),
        "volume": _decimal_text(row["volume"], "volume"),
        "close_time_raw": _check_int(row["close_time_raw"], "close_time_raw"),
        "quote_asset_volume": _decimal_text(row["quote_asset_volume"], "quote_asset_volume"),
        "number_of_trades": _check_int(row["number_of_trades"], "number_of_trades"),
        "taker_buy_base_asset_volume": _decimal_text(
            row["taker_buy_base_asset_volume"], "taker_buy_base_asset_volume"
        ),
        "taker_buy_quote_asset_volume": _decimal_text(
            row["taker_buy_quote_asset_volume"], "taker_buy_quote_asset_volume"
        ),
        "ignore_raw": ignore_raw,
    }
    return _digest(document)


# --------------------------------------------------------------------------- revision / edge ids


def revision_id(observation_key: str, source_identity: str, payload_hash: str) -> str:
    """Stable REST revision identity: same inputs → same id, whenever they arrive."""
    if not isinstance(observation_key, str) or not observation_key:
        raise RestIdentityViolation("observation_key must be a non-empty string")
    if not isinstance(source_identity, str) or not source_identity:
        raise RestIdentityViolation("source_identity must be a non-empty string")
    document = {
        "rule": REST_IDENTITY_RULE_ID,
        "rule_version": REST_IDENTITY_RULE_VERSION,
        "rule_hash": REST_IDENTITY_HASH,
        "observation_key": observation_key,
        "source_identity": source_identity,
        "payload_hash": _check_sha256(payload_hash, "payload_hash"),
    }
    return f"rev1-{_digest(document)}"


def edge_id(
    policy: PolicyBinding, observation_key: str, revision: str, superseded_revision: str
) -> str:
    """Time-free identity of one precedence edge (ADR-0027 §1 / §4.6)."""
    if not isinstance(policy, PolicyBinding) or policy.role is not PolicyRole.PRECEDENCE:
        raise RestIdentityViolation("an edge identity needs a precedence PolicyBinding")
    for label, value in (
        ("observation_key", observation_key),
        ("revision_id", revision),
        ("superseded_revision_id", superseded_revision),
    ):
        if not isinstance(value, str) or not value:
            raise RestIdentityViolation(f"{label} must be a non-empty string")
    if revision == superseded_revision:
        raise RestIdentityViolation("an edge needs two different revisions")
    document = {
        "policy_id": policy.policy_id,
        "policy_version": policy.version,
        "policy_hash": policy.policy_hash,
        "observation_key": observation_key,
        "revision_id": revision,
        "superseded_revision_id": superseded_revision,
    }
    return f"edge1-{_digest(document)}"


# --------------------------------------------------------------------------- arrival_seq


def check_rest_arrival_seq(value: object) -> int:
    """``value`` if it lies in the REST interval ``[2**62, 2**63)``; otherwise fail closed."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise RestIdentityViolation("arrival_seq must be an int")
    if not REST_ARRIVAL_SEQ_BASE <= value <= MAX_ARRIVAL_SEQ:
        raise RestArrivalSeqOverflow(f"arrival_seq {value} is outside the REST interval")
    return value


def check_archive_interval_arrival_seq(value: object) -> int:
    """``value`` if it lies in the archive interval ``[0, 2**62)``; otherwise fail closed.

    A guard for graph assembly (D3E); the D2 allocator itself is not changed.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise RestIdentityViolation("arrival_seq must be an int")
    if not 0 <= value < ARCHIVE_ARRIVAL_SEQ_LIMIT:
        raise RestArrivalSeqOverflow(f"arrival_seq {value} is outside the archive interval")
    return value


def rest_arrival_block_base(max_committed_seq: int | None) -> int:
    """The next REST block base above every committed response ``arrival_seq``.

    ``max_committed_seq`` is the largest ``arrival_seq`` in ``raw.binance_spot_rest_responses``
    (``None`` for an empty table). Every committed value there is a block base; anything else is
    a corrupt anchor and fails closed. Blocks are never reused; gaps are allowed.
    """
    if max_committed_seq is None:
        return REST_ARRIVAL_SEQ_BASE
    check_rest_arrival_seq(max_committed_seq)
    if (max_committed_seq - REST_ARRIVAL_SEQ_BASE) % REST_ARRIVAL_SEQ_STRIDE:
        raise RestIdentityViolation("a committed response arrival_seq is not a block base")
    base = max_committed_seq + REST_ARRIVAL_SEQ_STRIDE
    if base > MAX_ARRIVAL_SEQ - REST_ARRIVAL_SEQ_STRIDE + 1:
        raise RestArrivalSeqOverflow("REST arrival sequence space exhausted")
    return base


def element_arrival_seq(base: int, element_index: int) -> int:
    """``base + element_index + 1``; the response row keeps the base itself."""
    check_rest_arrival_seq(base)
    if (base - REST_ARRIVAL_SEQ_BASE) % REST_ARRIVAL_SEQ_STRIDE:
        raise RestIdentityViolation("block base must be a multiple of the stride above 2**62")
    if (
        not isinstance(element_index, int)
        or isinstance(element_index, bool)
        or not 0 <= element_index < PAGE_LIMIT
    ):
        raise RestArrivalSeqOverflow(
            f"element_index {element_index!r} is outside [0, {PAGE_LIMIT}) of one page"
        )
    return check_rest_arrival_seq(base + element_index + 1)


#: The complete, versioned identity rule. ``REST_IDENTITY_HASH`` is the SHA-256 of its JSON.
REST_IDENTITY_SPEC: Final[dict[str, Any]] = {
    "rule": REST_IDENTITY_RULE_ID,
    "version": REST_IDENTITY_RULE_VERSION,
    "venue": VENUE,
    "market": MARKET,
    "source": {"source_id": REST_SOURCE_ID, "version": REST_SOURCE_VERSION},
    "separate_from": "hlens.binance.spot.raw-revision-identity@1.0.0 (not imported, not extended)",
    "page_identity": {
        "document": ["method", "origin", "path", "query", "declared_time_unit", "rule"],
        "method": "GET",
        "origin": "configured market-data-only https://host[:port], exact",
        "paths": dict(DATA_TYPES),
        "query_allowlist": {
            "agg_trades": ["symbol", "limit", "exactly one of fromId | startTime"],
            "klines_1m": ["symbol", "interval=1m", "limit", "startTime (minute-aligned)"],
        },
        "never_sent": ["endTime", "timeZone", "X-MBX-TIME-UNIT"],
        "implicit_most_recent_mode": "not constructible",
        "query_order": "sorted by name",
        "value_encoding": "base-10 integers without sign or leading zero; symbol as-is",
        "limit": PAGE_LIMIT,
        "declared_time_unit": DECLARED_TIME_UNIT,
        "excluded_inputs": ["CollectionRequest.request_id", "retrieved_at", "attempt number"],
        "digest": "sha256 of canonical JSON",
    },
    "observation_key": {
        "response": "<venue>:<market>:rest:<page_identity_sha256>",
        "agg_trade": "<venue>:<market>:agg_trade:<symbol>:<agg_trade_id>",
        "kline_1m": "<venue>:<market>:kline:<symbol>:1m:<interval_start epoch microseconds>",
        "element_keys_equal_archive_rule": True,
    },
    "object_key": {
        "layout": f"{_OBJECT_KEY_PREFIX}/<body sha256>/<data_type>/<symbol>/<page sha256>.json",
        "content_addressed_by": "sha256 of the response entity body (content coding removed)",
    },
    "source_identity": {
        "response": "<source_id>@<source_version>",
        "element": "<source_id>@<source_version> (channel level; no response revision id)",
    },
    "payload_hash": {
        "response": "sha256 of the response entity body",
        "agg_trade": {
            "kind": "binance.spot.agg_trade/1",
            "time_unit": DECLARED_TIME_UNIT,
            "same_document_as": "hlens.binance.spot.raw-revision-identity@1.0.0",
        },
        "kline_1m": {
            "kind": "binance.spot.kline_1m/1",
            "time_unit": DECLARED_TIME_UNIT,
            "same_document_as": "hlens.binance.spot.raw-revision-identity@1.0.0",
        },
        "decimal_encoding": "fixed-point text of the decimal value, no exponent",
        "excluded_inputs": [
            "element_index",
            "page_index",
            "ingest_time",
            "knowledge_time",
            "available_time",
            "arrival_seq",
            "retrieved_at",
        ],
    },
    "revision_id": {
        "format": "rev1-<sha256 hex>",
        "document": [
            "rule",
            "rule_version",
            "rule_hash",
            "observation_key",
            "source_identity",
            "payload_hash",
        ],
        "canonical_json": "sorted keys, compact separators, UTF-8, no NaN",
    },
    "edge_id": {
        "format": "edge1-<sha256 hex>",
        "document": [
            "policy_id",
            "policy_version",
            "policy_hash",
            "observation_key",
            "revision_id",
            "superseded_revision_id",
        ],
        "excluded_inputs": ["knowledge_time", "arrival order", "wall clock"],
    },
    "arrival_seq": {
        "archive_interval": [0, ARCHIVE_ARRIVAL_SEQ_LIMIT],
        "rest_interval": [REST_ARRIVAL_SEQ_BASE, MAX_ARRIVAL_SEQ],
        "stride": REST_ARRIVAL_SEQ_STRIDE,
        "response": "block base",
        "element": "block base + element_index + 1",
        "anchor_table": "raw.binance_spot_rest_responses",
        "uniqueness_scope": "one aggregated RevisionGraph",
        "gaps_allowed": True,
        "semantics": "audit, idempotency and recovery only; never precedence or selection",
    },
}
REST_IDENTITY_HASH: Final = _digest(REST_IDENTITY_SPEC)

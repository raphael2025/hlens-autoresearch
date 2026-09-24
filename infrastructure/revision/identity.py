"""Stable revision identity for the Binance spot first slice (Phase 1 D2; ADR-0023 §4).

Everything an identity depends on lives in ``IDENTITY_SPEC``: a versioned, canonical-JSON
document whose SHA-256 is ``IDENTITY_HASH``. The hash is **derived** from the full document, never
written down as a constant, so any rule change necessarily changes the hash (and must change
``IDENTITY_RULE_VERSION``).

What is frozen here (03-data.md §7.4, ADR-0023 §4):

- ``observation_key``: the stable key of a business observation.
  - archive: venue + market + the **official archive path** of the file. It contains no checksum,
    no download time and no local path, so replacing the file at the same official path is a new
    revision of the *same* observation.
  - aggTrade: venue + market + symbol + ``agg_trade_id``.
  - 1m kline: venue + market + symbol + interval + ``interval_start``.
- ``revision_id``: SHA-256 over the canonical document ``{identity rule id + version + hash,
  observation key, source identity, payload hash}``. It depends on nothing that varies with
  arrival order or wall clock, so the same input always yields the same id.
- payload hash: for an archive, the SHA-256 of the stored object (officially declared in
  ``.CHECKSUM`` and recomputed locally by the storage adapter); for a parsed row, the SHA-256 of
  the canonical document of its **native** fields plus the instrument identity and the timestamp
  unit. No arrival / ingest / knowledge time and no line number enter a payload hash.
- ``arrival_seq`` block layout: an archive revision owns one block of ``ARRIVAL_SEQ_STRIDE``
  sequence numbers; the archive row takes the block base and its parsed rows take
  ``base + archive_line_number`` (line numbers are 1-based). The stride strictly exceeds the D1
  parser's decompressed-member limit, so a single archive can never overflow its block.
  ``arrival_seq`` is audit / idempotency / recovery only and never orders anything.

The official archive path grammar is written out here **again** (the D0 collector builds the same
path for its URLs). That duplication is deliberate: the identity rule owns its own versioned
grammar, and the store cross-checks the collected ``source_uri`` and object key against it, so a
collector change can never silently move an observation key.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from core.domain.base import canonical_json

__all__ = [
    "ARCHIVE_SOURCE_ID",
    "ARCHIVE_SOURCE_VERSION",
    "ARRIVAL_SEQ_STRIDE",
    "IDENTITY_HASH",
    "IDENTITY_RULE_ID",
    "IDENTITY_RULE_VERSION",
    "IDENTITY_SPEC",
    "MARKET",
    "MAX_ARRIVAL_SEQ",
    "VENUE",
    "ArrivalSeqOverflow",
    "IdentityViolation",
    "agg_trade_observation_key",
    "agg_trade_payload_hash",
    "archive_object_key",
    "archive_observation_key",
    "archive_relative_path",
    "archive_source_identity",
    "arrival_block_base",
    "kline_1m_observation_key",
    "kline_1m_payload_hash",
    "revision_id",
    "row_arrival_seq",
    "row_source_identity",
]

IDENTITY_RULE_ID: Final = "hlens.binance.spot.raw-revision-identity"
IDENTITY_RULE_VERSION: Final = "1.0.0"

VENUE: Final = "binance"
MARKET: Final = "spot"
ARCHIVE_SOURCE_ID: Final = "binance.public.spot.archive"
ARCHIVE_SOURCE_VERSION: Final = "1.0.0"

#: Sequence numbers owned by one archive revision (2**32). The D1 parser refuses members larger
#: than 3 GiB and every CSV line needs at least one byte plus its LF, so a single archive can
#: never reach 2**32 lines; the store rejects anything out of range anyway.
ARRIVAL_SEQ_STRIDE: Final = 1 << 32
#: ``RevisionRecord.arrival_seq`` is an Iceberg ``long``.
MAX_ARRIVAL_SEQ: Final = (1 << 63) - 1

_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_RE: Final = re.compile(r"^[A-Z0-9]{1,32}$")
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

#: ``data_type`` → the official ``data/spot/daily/...`` directory and file infix.
_ARCHIVE_LAYOUT: Final[dict[str, tuple[str, str, str]]] = {
    # data_type: (official directory, extra directory below the symbol, file infix)
    "agg_trades": ("aggTrades", "", "aggTrades"),
    "klines_1m": ("klines", "1m", "1m"),
}
#: Prefix of the content-addressed warehouse key of an archive object.
_OBJECT_KEY_PREFIX: Final = "raw/binance/spot/archive/revisions"


class IdentityViolation(ValueError):
    """An identity input is not well formed; nothing may be derived from it (fail closed)."""


class ArrivalSeqOverflow(IdentityViolation):
    """An arrival sequence number would leave its block or the int64 range."""


def _check_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or _SYMBOL_RE.fullmatch(symbol) is None:
        raise IdentityViolation(f"symbol {symbol!r} is not a venue-native upper-case symbol")
    return symbol


def _check_data_type(data_type: str) -> str:
    if data_type not in _ARCHIVE_LAYOUT:
        raise IdentityViolation(f"unsupported data_type {data_type!r}")
    return data_type


def _check_sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise IdentityViolation(f"{label} must be lower-case SHA-256 hex")
    return value


def _check_day(day: date) -> date:
    if not isinstance(day, date) or isinstance(day, datetime):
        raise IdentityViolation("coverage day must be a datetime.date")
    return day


def _check_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise IdentityViolation(f"{label} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise IdentityViolation(f"{label} must be UTC")
    return value


def archive_relative_path(data_type: str, symbol: str, day: date) -> str:
    """The official ``data/spot/daily/...`` path of one daily archive (no base URL, no checksum)."""
    directory, sub, infix = _ARCHIVE_LAYOUT[_check_data_type(data_type)]
    _check_symbol(symbol)
    _check_day(day)
    tail = f"/{sub}" if sub else ""
    filename = f"{symbol}-{infix}-{day.isoformat()}.zip"
    return f"data/spot/daily/{directory}/{symbol}{tail}/{filename}"


def archive_object_key(data_type: str, symbol: str, day: date, sha256: str) -> str:
    """Content-addressed warehouse key of an archive object; the basename stays official.

    The same official path with the same checksum always maps to the same immutable object; a
    different checksum maps to a different object, so a replacement never overwrites its
    predecessor and never has to fail with ``ObjectConflict``.
    """
    directory, sub, infix = _ARCHIVE_LAYOUT[_check_data_type(data_type)]
    _check_symbol(symbol)
    _check_day(day)
    _check_sha256(sha256, "archive sha256")
    tail = f"/{sub}" if sub else ""
    filename = f"{symbol}-{infix}-{day.isoformat()}.zip"
    return f"{_OBJECT_KEY_PREFIX}/{sha256}/daily/{directory}/{symbol}{tail}/{filename}"


def archive_observation_key(data_type: str, symbol: str, day: date) -> str:
    """Observation key of an archive file: venue + market + official path, no checksum."""
    return f"{VENUE}:{MARKET}:archive:{archive_relative_path(data_type, symbol, day)}"


def agg_trade_observation_key(symbol: str, agg_trade_id: int) -> str:
    """Observation key of one aggregate trade: venue + market + symbol + ``agg_trade_id``."""
    _check_symbol(symbol)
    if not isinstance(agg_trade_id, int) or isinstance(agg_trade_id, bool) or agg_trade_id < 0:
        raise IdentityViolation(f"agg_trade_id {agg_trade_id!r} must be a non-negative int")
    return f"{VENUE}:{MARKET}:agg_trade:{symbol}:{agg_trade_id}"


def kline_1m_observation_key(symbol: str, interval_start: datetime) -> str:
    """Observation key of one 1m kline: venue + market + symbol + interval + ``interval_start``."""
    _check_symbol(symbol)
    _check_utc(interval_start, "interval_start")
    return f"{VENUE}:{MARKET}:kline:{symbol}:1m:{_micros(interval_start)}"


def archive_source_identity() -> str:
    """Stable source identity of an archive revision: the frozen ``SourceBinding``."""
    return f"{ARCHIVE_SOURCE_ID}@{ARCHIVE_SOURCE_VERSION}"


def row_source_identity(archive_revision_id: str) -> str:
    """Stable source identity of a parsed row: the archive revision it was parsed from.

    Binding the row's source identity to the archive revision is what makes a replacement archive
    produce its own row revisions: identical rows in two competing archives stay two revisions,
    each traceable to its own bytes, and neither overwrites the other.
    """
    if not isinstance(archive_revision_id, str) or not archive_revision_id:
        raise IdentityViolation("archive_revision_id must be a non-empty string")
    return f"{archive_source_identity()}:{archive_revision_id}"


def _micros(value: datetime) -> int:
    return (value - _EPOCH) // timedelta(microseconds=1)


def _decimal_text(value: Decimal, label: str) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise IdentityViolation(f"{label} must be a finite Decimal")
    return format(value, "f")


def revision_id(observation_key: str, source_identity: str, payload_hash: str) -> str:
    """Stable revision identity (ADR-0023 §4): same inputs → same id, whenever they arrive."""
    if not isinstance(observation_key, str) or not observation_key:
        raise IdentityViolation("observation_key must be a non-empty string")
    if not isinstance(source_identity, str) or not source_identity:
        raise IdentityViolation("source_identity must be a non-empty string")
    _check_sha256(payload_hash, "payload_hash")
    document = {
        "rule": IDENTITY_RULE_ID,
        "rule_version": IDENTITY_RULE_VERSION,
        "rule_hash": IDENTITY_HASH,
        "observation_key": observation_key,
        "source_identity": source_identity,
        "payload_hash": payload_hash,
    }
    digest = hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
    return f"rev1-{digest}"


def agg_trade_payload_hash(symbol: str, time_unit: str, row: dict[str, Any]) -> str:
    """Content hash of one aggTrades row: native CSV fields + instrument identity + unit."""
    _check_symbol(symbol)
    document = {
        "kind": "binance.spot.agg_trade/1",
        "venue": VENUE,
        "market": MARKET,
        "symbol": symbol,
        "time_unit": time_unit,
        "agg_trade_id": _int(row, "agg_trade_id"),
        "price": _decimal_text(row["price"], "price"),
        "quantity": _decimal_text(row["quantity"], "quantity"),
        "first_trade_id": _int(row, "first_trade_id"),
        "last_trade_id": _int(row, "last_trade_id"),
        "timestamp_raw": _int(row, "timestamp_raw"),
        "is_buyer_maker": _bool(row, "is_buyer_maker"),
        "is_best_match": _bool(row, "is_best_match"),
    }
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def kline_1m_payload_hash(symbol: str, time_unit: str, row: dict[str, Any]) -> str:
    """Content hash of one 1m kline row: native CSV fields + instrument identity + unit."""
    _check_symbol(symbol)
    ignore_raw = row["ignore_raw"]
    if not isinstance(ignore_raw, str):
        raise IdentityViolation("ignore_raw must be the source text")
    document = {
        "kind": "binance.spot.kline_1m/1",
        "venue": VENUE,
        "market": MARKET,
        "symbol": symbol,
        "interval": "1m",
        "time_unit": time_unit,
        "open_time_raw": _int(row, "open_time_raw"),
        "open": _decimal_text(row["open"], "open"),
        "high": _decimal_text(row["high"], "high"),
        "low": _decimal_text(row["low"], "low"),
        "close": _decimal_text(row["close"], "close"),
        "volume": _decimal_text(row["volume"], "volume"),
        "close_time_raw": _int(row, "close_time_raw"),
        "quote_asset_volume": _decimal_text(row["quote_asset_volume"], "quote_asset_volume"),
        "number_of_trades": _int(row, "number_of_trades"),
        "taker_buy_base_asset_volume": _decimal_text(
            row["taker_buy_base_asset_volume"], "taker_buy_base_asset_volume"
        ),
        "taker_buy_quote_asset_volume": _decimal_text(
            row["taker_buy_quote_asset_volume"], "taker_buy_quote_asset_volume"
        ),
        "ignore_raw": ignore_raw,
    }
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def _int(row: dict[str, Any], column: str) -> int:
    value = row[column]
    if not isinstance(value, int) or isinstance(value, bool):
        raise IdentityViolation(f"{column} must be an int")
    return value


def _bool(row: dict[str, Any], column: str) -> bool:
    value = row[column]
    if not isinstance(value, bool):
        raise IdentityViolation(f"{column} must be a bool")
    return value


def arrival_block_base(max_committed_seq: int | None) -> int:
    """The next arrival-sequence block base above every committed archive sequence.

    ``max_committed_seq`` is the largest ``arrival_seq`` currently committed to
    ``raw.binance_spot_archives`` (``None`` when the table is empty). Blocks are never reused:
    a failed attempt simply leaves a gap, which ADR-0023 §4 explicitly allows.
    """
    if max_committed_seq is None:
        return 0
    if not isinstance(max_committed_seq, int) or isinstance(max_committed_seq, bool):
        raise IdentityViolation("max_committed_seq must be an int or None")
    if max_committed_seq < 0:
        raise IdentityViolation("committed arrival_seq must not be negative")
    base = (max_committed_seq // ARRIVAL_SEQ_STRIDE + 1) * ARRIVAL_SEQ_STRIDE
    if base > MAX_ARRIVAL_SEQ - ARRIVAL_SEQ_STRIDE:
        raise ArrivalSeqOverflow("arrival sequence space exhausted")
    return base


def row_arrival_seq(base: int, archive_line_number: int) -> int:
    """``base + archive_line_number`` inside the block; anything else fails closed."""
    if not isinstance(base, int) or isinstance(base, bool) or base < 0:
        raise IdentityViolation("block base must be a non-negative int")
    if base % ARRIVAL_SEQ_STRIDE != 0:
        raise IdentityViolation("block base must be a multiple of the stride")
    if (
        not isinstance(archive_line_number, int)
        or isinstance(archive_line_number, bool)
        or archive_line_number < 1
    ):
        raise IdentityViolation("archive_line_number must be a positive int")
    if archive_line_number >= ARRIVAL_SEQ_STRIDE:
        raise ArrivalSeqOverflow(
            f"archive line {archive_line_number} leaves its arrival-sequence block"
        )
    seq = base + archive_line_number
    if seq > MAX_ARRIVAL_SEQ:
        raise ArrivalSeqOverflow("arrival sequence exceeds the int64 range")
    return seq


#: The complete, versioned identity rule. ``IDENTITY_HASH`` is the SHA-256 of its canonical JSON.
IDENTITY_SPEC: Final[dict[str, Any]] = {
    "rule": IDENTITY_RULE_ID,
    "version": IDENTITY_RULE_VERSION,
    "venue": VENUE,
    "market": MARKET,
    "source": {"source_id": ARCHIVE_SOURCE_ID, "version": ARCHIVE_SOURCE_VERSION},
    "observation_key": {
        "archive": "<venue>:<market>:archive:<official archive path>",
        "agg_trade": "<venue>:<market>:agg_trade:<symbol>:<agg_trade_id>",
        "kline_1m": "<venue>:<market>:kline:<symbol>:1m:<interval_start epoch microseconds>",
        "excluded_inputs": [
            "payload checksum",
            "download time",
            "local object key or path",
            "arrival_seq",
        ],
    },
    "archive_path": {
        "agg_trades": "data/spot/daily/aggTrades/<symbol>/<symbol>-aggTrades-<day>.zip",
        "klines_1m": "data/spot/daily/klines/<symbol>/1m/<symbol>-1m-<day>.zip",
        "day_format": "YYYY-MM-DD (UTC)",
    },
    "object_key": {
        "layout": f"{_OBJECT_KEY_PREFIX}/<sha256>/daily/<official tail>",
        "content_addressed_by": (
            "sha256 of the archive bytes (official .CHECKSUM, verified locally)"
        ),
        "basename": "official archive file name",
    },
    "source_identity": {
        "archive": "<source_id>@<source_version>",
        "parsed_row": "<source_id>@<source_version>:<archive revision_id>",
    },
    "payload_hash": {
        "archive": "sha256 of the stored archive object",
        "agg_trade": {
            "kind": "binance.spot.agg_trade/1",
            "fields": [
                "venue",
                "market",
                "symbol",
                "time_unit",
                "agg_trade_id",
                "price",
                "quantity",
                "first_trade_id",
                "last_trade_id",
                "timestamp_raw",
                "is_buyer_maker",
                "is_best_match",
            ],
        },
        "kline_1m": {
            "kind": "binance.spot.kline_1m/1",
            "fields": [
                "venue",
                "market",
                "symbol",
                "interval",
                "time_unit",
                "open_time_raw",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time_raw",
                "quote_asset_volume",
                "number_of_trades",
                "taker_buy_base_asset_volume",
                "taker_buy_quote_asset_volume",
                "ignore_raw",
            ],
        },
        "decimal_encoding": "fixed-point text of the decimal(38, 18) value, no exponent",
        "excluded_inputs": [
            "archive_line_number",
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
    "arrival_seq": {
        "stride": ARRIVAL_SEQ_STRIDE,
        "archive": "block base",
        "parsed_row": "block base + archive_line_number (1-based)",
        "anchor_table": "raw.binance_spot_archives",
        "gaps_allowed": True,
        "semantics": "audit, idempotency and recovery only; never precedence or selection",
    },
}
IDENTITY_HASH: Final = hashlib.sha256(canonical_json(IDENTITY_SPEC).encode("utf-8")).hexdigest()

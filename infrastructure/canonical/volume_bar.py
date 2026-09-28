"""Deterministic base-volume bars from a pinned Canonical trades snapshot.

Rule ``hlens.canonical.volume-bar@1.0.0``. The caller supplies an explicit positive base-asset
volume threshold and a fixed ``canonical.trades`` snapshot. Trades must have non-decreasing
``event_time`` and strictly increasing numeric ``venue_trade_id``; both are checked while
streaming because the bounded Iceberg scan does not promise a global sort. The monotonic ID check
rejects repeated venue IDs without retaining an unbounded set. A violated ordering or duplicate
trade key fails closed. The first trade that brings a bar's accumulated quantity to or above the threshold
closes it whole (trades are never split). A trailing partial bar is not emitted. Day boundaries do
not reset accumulation. Bar ``event_time`` and ``available_time`` are both copied from its last
trade.

The adapter scan uses the fixed-snapshot bounded path specified by ADR-0075. Only the current bar
and a single scan batch are retained by this implementation; the returned tuple is caller-owned.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import (
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    localcontext,
)
from typing import Any, Final

from pyiceberg.expressions import EqualTo

from core.domain.base import canonical_json
from infrastructure.catalog.phase1_tables import CANONICAL_TRADES

__all__ = [
    "VOLUME_BAR_HASH",
    "VOLUME_BAR_SPEC",
    "VOLUME_BAR_VERSION",
    "VolumeBar",
    "VolumeBarError",
    "volume_bars",
]

VOLUME_BAR_ID: Final = "hlens.canonical.volume-bar"
VOLUME_BAR_VERSION: Final = "1.0.0"
VOLUME_BAR_SPEC: Final[dict[str, Any]] = {
    "rule": VOLUME_BAR_ID,
    "version": VOLUME_BAR_VERSION,
    "source": "canonical.trades at an explicitly bound Iceberg snapshot",
    "order": "event_time non-decreasing and numeric venue_trade_id strictly increasing; otherwise reject",
    "selection": "snapshot must contain at most one row per venue trade; duplicates reject",
    "threshold": "explicit positive Decimal base-asset quantity; close on first cumulative >= threshold",
    "overshoot": "include the entire threshold-crossing trade; never split a trade",
    "partial": "do not emit a trailing bar below threshold",
    "day_boundary": "does not reset accumulation",
    "bar_time": "event_time and available_time are copied from the last trade",
    "arithmetic": "exact Decimal addition; inexact results reject",
    "content": "sha256 of canonical JSON bar values and an ordered length-prefixed trade digest",
}
VOLUME_BAR_HASH: Final = hashlib.sha256(
    canonical_json(VOLUME_BAR_SPEC).encode("utf-8")
).hexdigest()

_EXACT: Final = Context(prec=80, traps=[Inexact, InvalidOperation, Overflow, DivisionByZero])
class VolumeBarError(ValueError):
    """The pinned Canonical trade stream cannot safely produce deterministic volume bars."""


@dataclass(frozen=True, slots=True)
class VolumeBar:
    symbol: str
    sequence: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int
    event_time: datetime
    available_time: datetime
    content_sha256: str


def volume_bars(
    catalog: Any,
    *,
    snapshot_id: str,
    symbol: str,
    base_volume_threshold: Decimal,
) -> tuple[VolumeBar, ...]:
    """Build threshold-complete volume bars from one explicit ``canonical.trades`` snapshot.

    ``catalog`` must implement the infrastructure ``scan_column_batches`` API. The snapshot and
    threshold are mandatory: this function never reads a moving table head or supplies a default.
    The source must already represent the intended point-in-time trade set; duplicate venue trade
    IDs (including competing canonical revisions) are rejected rather than selected arbitrarily.
    """
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise VolumeBarError("snapshot_id must be an explicit non-empty string")
    if not isinstance(symbol, str) or not symbol.strip():
        raise VolumeBarError("symbol must be a non-empty canonical symbol")
    if (
        not isinstance(base_volume_threshold, Decimal)
        or not base_volume_threshold.is_finite()
        or base_volume_threshold <= 0
    ):
        raise VolumeBarError("base_volume_threshold must be an explicit positive finite Decimal")

    columns = (
        "symbol",
        "venue_trade_id",
        "event_time",
        "available_time",
        "price",
        "quantity",
        "revision_id",
    )
    reader = catalog.scan_column_batches(
        CANONICAL_TRADES.table,
        columns=columns,
        row_filter=EqualTo("symbol", symbol),  # type: ignore[call-arg, arg-type]
        snapshot_id=snapshot_id,
    )
    try:
        with localcontext(_EXACT):
            return _aggregate_batches(reader, symbol, base_volume_threshold)
    except VolumeBarError:
        raise
    except (DecimalException, TypeError, ValueError, OverflowError) as exc:
        raise VolumeBarError(f"trade stream is invalid: {type(exc).__name__}") from None
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            close()


def _aggregate_batches(
    batches: Iterator[Any], symbol: str, threshold: Decimal
) -> tuple[VolumeBar, ...]:
    bars: list[VolumeBar] = []
    current: dict[str, Any] | None = None
    previous_order: tuple[datetime, int] | None = None
    previous_trade_id: int | None = None

    for batch in batches:
        for row in batch.to_pylist():
            trade = _trade(row, symbol)
            order = (trade["event_time"], trade["venue_trade_id"])
            if previous_order is not None and order <= previous_order:
                reason = "duplicate venue trade" if order == previous_order else "out-of-order trade"
                raise VolumeBarError(f"{reason}: stream must be strictly ordered by event_time and venue_trade_id")
            if previous_trade_id is not None and trade["venue_trade_id"] <= previous_trade_id:
                reason = (
                    "duplicate venue trade ID"
                    if trade["venue_trade_id"] == previous_trade_id
                    else "out-of-order venue trade ID"
                )
                raise VolumeBarError(f"{reason}: venue_trade_id must be strictly increasing")
            previous_order = order
            previous_trade_id = trade["venue_trade_id"]

            encoded = canonical_json(
                {
                    "event_time": _time_text(trade["event_time"]),
                    "available_time": _time_text(trade["available_time"]),
                    "venue_trade_id": str(trade["venue_trade_id"]),
                    "revision_id": trade["revision_id"],
                    "price": _decimal_text(trade["price"]),
                    "quantity": _decimal_text(trade["quantity"]),
                }
            ).encode("utf-8")
            if current is None:
                current = _new_bar(trade)
            else:
                current["high"] = max(current["high"], trade["price"])
                current["low"] = min(current["low"], trade["price"])
                current["close"] = trade["price"]
                current["volume"] += trade["quantity"]
                current["trade_count"] += 1
                current["event_time"] = trade["event_time"]
                current["available_time"] = trade["available_time"]
            current["digest"].update(len(encoded).to_bytes(8, "big"))
            current["digest"].update(encoded)

            if current["volume"] >= threshold:
                bars.append(_finish_bar(current, symbol, len(bars)))
                current = None

    return tuple(bars)


def _trade(row: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise VolumeBarError("scan returned a non-mapping trade row")
    if row.get("symbol") != symbol:
        raise VolumeBarError("scan returned a row outside the requested symbol")
    trade_id = row.get("venue_trade_id")
    if not isinstance(trade_id, str) or not trade_id.isascii() or not trade_id.isdecimal():
        raise VolumeBarError("venue_trade_id must be a decimal digit string")
    event_time = row.get("event_time")
    available_time = row.get("available_time")
    if not _is_utc(event_time) or not _is_utc(available_time):
        raise VolumeBarError("event_time and available_time must be UTC datetimes")
    price, quantity = row.get("price"), row.get("quantity")
    if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
        raise VolumeBarError("price must be a positive finite Decimal")
    if not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity <= 0:
        raise VolumeBarError("quantity must be a positive finite Decimal")
    revision_id = row.get("revision_id")
    if not isinstance(revision_id, str) or not revision_id:
        raise VolumeBarError("revision_id must be a non-empty string")
    return {
        "venue_trade_id": int(trade_id),
        "event_time": event_time,
        "available_time": available_time,
        "price": price,
        "quantity": quantity,
        "revision_id": revision_id,
    }


def _new_bar(trade: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "open": trade["price"],
        "high": trade["price"],
        "low": trade["price"],
        "close": trade["price"],
        "volume": trade["quantity"],
        "trade_count": 1,
        "event_time": trade["event_time"],
        "available_time": trade["available_time"],
        "digest": hashlib.sha256(),
    }


def _finish_bar(current: Mapping[str, Any], symbol: str, sequence: int) -> VolumeBar:
    document = {
        "rule": f"{VOLUME_BAR_ID}@{VOLUME_BAR_VERSION}",
        "rule_hash": VOLUME_BAR_HASH,
        "symbol": symbol,
        "sequence": sequence,
        "open": _decimal_text(current["open"]),
        "high": _decimal_text(current["high"]),
        "low": _decimal_text(current["low"]),
        "close": _decimal_text(current["close"]),
        "volume": _decimal_text(current["volume"]),
        "trade_count": current["trade_count"],
        "event_time": _time_text(current["event_time"]),
        "available_time": _time_text(current["available_time"]),
        "ordered_trade_digest": current["digest"].hexdigest(),
    }
    return VolumeBar(
        symbol=symbol,
        sequence=sequence,
        open=current["open"],
        high=current["high"],
        low=current["low"],
        close=current["close"],
        volume=current["volume"],
        trade_count=current["trade_count"],
        event_time=current["event_time"],
        available_time=current["available_time"],
        content_sha256=hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest(),
    )


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _time_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _is_utc(value: Any) -> bool:
    return isinstance(value, datetime) and value.utcoffset() == timedelta(0)

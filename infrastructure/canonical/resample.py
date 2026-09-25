"""Deterministic higher-period bars from PIT-selected Canonical 1m bars (Phase 1 E4).

Rule ``hlens.canonical.resample@1.0.0`` (03-data.md §7.4: higher periods only derive from
``canonical.bars_1m``, and re-runs are bit-identical). Pure: no catalog I/O, no clock.

Input is a ``PitSelection`` of ``canonical.bars_1m`` for **one** simulation time (a point spec):
exactly the revisions a dataset may use, already proven at its bound snapshots. Conflicting
selections are refused (the dataset would fail closed anyway); an interval spec is refused too,
because its selection can change inside the interval and a bar needs one revision per minute.

- buckets of ``minutes`` (a divisor of 1440) aligned to the UTC epoch; only minutes selected in the
  window ``[start, end)`` count; a bucket is emitted when at least one minute is present;
- ``complete`` is true only when every minute of the bucket is present — **nothing is filled or
  interpolated**; consumers decide what an incomplete bar may be used for;
- OHLC: first present minute's open, last present minute's close, max high, min low; volumes,
  quote volume, taker volumes and trade count are sums;
- times (ADR-0023 §3): ``available_time = max(bucket end, max constituent available_time)`` (a bar
  is not observable before its interval ends), ``knowledge_time = max constituent knowledge_time``;
- ``content_sha256`` covers the bar's market content and constituent revision ids, so equal
  inputs always give equal bars.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from core.contracts.revision import PointInTimeStatus
from core.domain.base import canonical_json
from infrastructure.canonical import rules

__all__ = [
    "RESAMPLE_SPEC",
    "DerivedBar",
    "ResampleError",
    "resample_bars",
]

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MINUTE: Final = timedelta(minutes=1)
_MICRO: Final = timedelta(microseconds=1)
_SCALE: Final = Decimal("0.000000000000000001")
RESAMPLE_ID: Final = "hlens.canonical.resample"
RESAMPLE_VERSION: Final = "1.0.0"
RESAMPLE_SPEC: Final[dict[str, Any]] = {
    "rule": RESAMPLE_ID,
    "version": RESAMPLE_VERSION,
    "input": "PitSelection of canonical.bars_1m at one simulation_time, no conflict",
    "periods": "minutes dividing 1440, buckets aligned to the UTC epoch",
    "emit": "buckets with >= 1 selected minute in [start, end)",
    "complete": "all minutes of the bucket present; nothing filled or interpolated",
    "ohlc": "first open, last close, max high, min low; sums of volumes and trade counts",
    "available_time": "max(bucket end, max constituent available_time)",
    "knowledge_time": "max constituent knowledge_time",
    "content": "sha256 of canonical JSON of market content + constituent revision ids",
}
RESAMPLE_HASH: Final = hashlib.sha256(canonical_json(RESAMPLE_SPEC).encode("utf-8")).hexdigest()
_SUMS: Final = (
    "volume",
    "quote_volume",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
)


class ResampleError(ValueError):
    """The input cannot yield honest derived bars (fail closed)."""


@dataclass(frozen=True, slots=True)
class DerivedBar:
    symbol: str
    minutes: int
    interval_start: datetime
    interval_end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int
    taker_buy_base_volume: Decimal
    taker_buy_quote_volume: Decimal
    minutes_present: int
    complete: bool
    constituents: tuple[str, ...]
    available_time: datetime
    knowledge_time: datetime
    content_sha256: str


def _text(value: Decimal) -> str:
    return format(value.quantize(_SCALE), "f")


def resample_bars(
    selection: Any, minutes: int, start: datetime, end: datetime
) -> tuple[DerivedBar, ...]:
    """Derived ``minutes`` bars of every bucket with a selected minute in ``[start, end)``."""
    if selection.canonical_table != rules.CANONICAL_TABLES["klines_1m"].table:
        raise ResampleError("only canonical.bars_1m selections can be resampled")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1:
        raise ResampleError("minutes must be a positive int")
    if 1440 % minutes:
        raise ResampleError("minutes must divide one UTC day (1440)")
    for label, value in (("start", start), ("end", end)):
        if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
            raise ResampleError(f"{label} must be a UTC datetime")
    if not start < end:
        raise ResampleError("the window must not be empty")
    if selection.conflicts:
        raise ResampleError("the selection has competing heads: nothing may be derived")
    per_key: dict[str, list[Any]] = {}
    for item in selection.selections:
        per_key.setdefault(item.observation_key, []).append(item)
    if any(len(items) > 1 for items in per_key.values()):
        raise ResampleError("an interval selection cannot be resampled (one revision per minute)")
    rows: list[Mapping[str, Any]] = []
    for items in per_key.values():
        [item] = items
        if item.status is PointInTimeStatus.SELECTED:
            row = selection.selected_rows[item.selected_revision_id]
            if start <= row["interval_start"] < end:
                rows.append(row)
    period = timedelta(minutes=minutes)
    buckets: dict[datetime, list[Mapping[str, Any]]] = {}
    for row in rows:
        offset = (row["interval_start"] - _EPOCH) // period
        buckets.setdefault(_EPOCH + offset * period, []).append(row)
    bars = [_bar(bucket, period, minutes, members) for bucket, members in buckets.items()]
    return tuple(sorted(bars, key=lambda bar: bar.interval_start))


def _bar(
    bucket: datetime, period: timedelta, minutes: int, members: Sequence[Mapping[str, Any]]
) -> DerivedBar:
    ordered = sorted(members, key=lambda row: row["interval_start"])
    starts = [row["interval_start"] for row in ordered]
    if len(set(starts)) != len(starts):
        raise ResampleError(f"bucket {bucket.isoformat()} has two bars for one minute")
    symbols = {row["symbol"] for row in ordered}
    if len(symbols) != 1:
        raise ResampleError("a bucket mixes symbols")
    symbol = symbols.pop()
    values: dict[str, Any] = {
        "open": ordered[0]["open"],
        "close": ordered[-1]["close"],
        "high": max(row["high"] for row in ordered),
        "low": min(row["low"] for row in ordered),
        "trade_count": sum(row["trade_count"] for row in ordered),
        **{name: sum((row[name] for row in ordered), Decimal(0)) for name in _SUMS},
    }
    constituents = tuple(row["revision_id"] for row in ordered)
    end = bucket + period
    document = {
        "rule": f"{RESAMPLE_ID}@{RESAMPLE_VERSION}",
        "rule_hash": RESAMPLE_HASH,
        "symbol": symbol,
        "minutes": minutes,
        "interval_start_us": (bucket - _EPOCH) // _MICRO,
        "interval_end_us": (end - _EPOCH) // _MICRO,
        "trade_count": values["trade_count"],
        **{name: _text(values[name]) for name in ("open", "high", "low", "close", *_SUMS)},
        "constituents": list(constituents),
    }
    return DerivedBar(
        symbol=symbol,
        minutes=minutes,
        interval_start=bucket,
        interval_end=end,
        open=values["open"],
        high=values["high"],
        low=values["low"],
        close=values["close"],
        volume=values["volume"],
        quote_volume=values["quote_volume"],
        trade_count=values["trade_count"],
        taker_buy_base_volume=values["taker_buy_base_volume"],
        taker_buy_quote_volume=values["taker_buy_quote_volume"],
        minutes_present=len(ordered),
        complete=len(ordered) == minutes
        and all(starts[i] == bucket + i * _MINUTE for i in range(len(starts))),
        constituents=constituents,
        available_time=max(end, *(row["available_time"] for row in ordered)),
        knowledge_time=max(row["knowledge_time"] for row in ordered),
        content_sha256=hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest(),
    )

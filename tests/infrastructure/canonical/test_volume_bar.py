"""DATA-1: deterministic, explicitly thresholded Canonical volume bars."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from infrastructure.canonical.volume_bar import (
    VOLUME_BAR_HASH,
    VOLUME_BAR_SPEC,
    VolumeBarError,
    volume_bars,
)
from infrastructure.catalog.phase1_tables import CANONICAL_TRADES

SYMBOL = "BTC-USDT"
SNAPSHOT = "123456789"


class _Batch:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def to_pylist(self) -> list[dict[str, Any]]:
        return self._rows


class _Reader:
    def __init__(self, batches: list[_Batch]) -> None:
        self._batches = iter(batches)
        self.closed = False

    def __iter__(self) -> _Reader:
        return self

    def __next__(self) -> _Batch:
        return next(self._batches)

    def close(self) -> None:
        self.closed = True


class _Catalog:
    def __init__(self, batches: list[_Batch]) -> None:
        self.reader = _Reader(batches)
        self.arguments: dict[str, Any] = {}

    def scan_column_batches(self, table: str, **kwargs: Any) -> _Reader:
        self.arguments = {"table": table, **kwargs}
        return self.reader


def _trade(
    trade_id: int,
    quantity: str,
    price: str,
    event_time: datetime,
    *,
    available_time: datetime | None = None,
    revision: str | None = None,
) -> dict[str, Any]:
    return {
        "symbol": SYMBOL,
        "venue_trade_id": str(trade_id),
        "event_time": event_time,
        "available_time": available_time or event_time + timedelta(seconds=1),
        "price": Decimal(price),
        "quantity": Decimal(quantity),
        "revision_id": revision or f"crev1-{trade_id}",
    }


def test_volume_bars_use_explicit_base_volume_and_cross_utc_days() -> None:
    first = datetime(2024, 1, 1, 23, 59, 59, tzinfo=UTC)
    second = datetime(2024, 1, 2, 0, 0, 1, tzinfo=UTC)
    third = datetime(2024, 1, 2, 0, 0, 2, tzinfo=UTC)
    last_available = second + timedelta(seconds=3)
    catalog = _Catalog(
        [
            _Batch([_trade(10, "0.1", "100.00", first)]),
            _Batch(
                [
                    _trade(11, "0.2", "101.00", second, available_time=last_available),
                    _trade(12, "0.9", "99.00", third),
                ]
            ),
        ]
    )

    bars = volume_bars(
        catalog,
        snapshot_id=SNAPSHOT,
        symbol=SYMBOL,
        base_volume_threshold=Decimal("0.3"),
    )

    assert len(bars) == 1
    [bar] = bars
    assert (bar.open, bar.high, bar.low, bar.close) == tuple(
        Decimal(value) for value in ("100.00", "101.00", "100.00", "101.00")
    )
    assert bar.volume == Decimal("0.3")
    assert bar.trade_count == 2
    assert bar.event_time == second
    assert bar.available_time == last_available
    assert catalog.arguments["table"] == CANONICAL_TRADES.table
    assert catalog.arguments["snapshot_id"] == SNAPSHOT
    assert catalog.arguments["row_filter"].__class__.__name__ == "EqualTo"
    assert catalog.reader.closed


def test_volume_bar_rule_spec_is_fixed_and_incomplete_tail_is_not_emitted() -> None:
    at = datetime(2024, 2, 1, tzinfo=UTC)
    catalog = _Catalog([_Batch([_trade(1, "0.6", "1", at), _trade(2, "0.3", "2", at + timedelta(seconds=1))])])

    bars = volume_bars(
        catalog,
        snapshot_id=SNAPSHOT,
        symbol=SYMBOL,
        base_volume_threshold=Decimal("0.5"),
    )

    assert VOLUME_BAR_SPEC["rule"] == "hlens.canonical.volume-bar"
    assert VOLUME_BAR_SPEC["version"] == "1.0.0"
    assert len(VOLUME_BAR_HASH) == 64
    assert len(bars) == 1
    assert bars[0].volume == Decimal("0.6")  # threshold-crossing trade is never split
    assert bars[0].trade_count == 1


@pytest.mark.parametrize("threshold", [Decimal("0"), Decimal("-1"), Decimal("NaN"), 1])
def test_threshold_must_be_a_positive_finite_decimal(threshold: Any) -> None:
    catalog = _Catalog([])
    with pytest.raises(VolumeBarError, match="threshold"):
        volume_bars(
            catalog,
            snapshot_id=SNAPSHOT,
            symbol=SYMBOL,
            base_volume_threshold=threshold,
        )  # type: ignore[arg-type]
    assert catalog.arguments == {}


def test_unsorted_stream_fails_closed_and_reader_is_closed() -> None:
    at = datetime(2024, 3, 1, tzinfo=UTC)
    catalog = _Catalog([_Batch([_trade(2, "1", "1", at), _trade(1, "1", "1", at)])])

    with pytest.raises(VolumeBarError, match="out-of-order"):
        volume_bars(
            catalog,
            snapshot_id=SNAPSHOT,
            symbol=SYMBOL,
            base_volume_threshold=Decimal("1"),
        )
    assert catalog.reader.closed


def test_duplicate_trade_key_fails_closed() -> None:
    at = datetime(2024, 3, 2, tzinfo=UTC)
    catalog = _Catalog(
        [_Batch([_trade(9, "1", "1", at), _trade(9, "1", "2", at, revision="crev1-other")])]
    )

    with pytest.raises(VolumeBarError, match="duplicate venue trade"):
        volume_bars(
            catalog,
            snapshot_id=SNAPSHOT,
            symbol=SYMBOL,
            base_volume_threshold=Decimal("1"),
        )


def test_invalid_trade_fields_fail_closed() -> None:
    at = datetime(2024, 3, 3, tzinfo=UTC)
    bad = _trade(1, "0", "1", at)
    catalog = _Catalog([_Batch([bad])])

    with pytest.raises(VolumeBarError, match="quantity"):
        volume_bars(
            catalog,
            snapshot_id=SNAPSHOT,
            symbol=SYMBOL,
            base_volume_threshold=Decimal("1"),
        )

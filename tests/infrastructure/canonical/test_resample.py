"""E4 derived bars from PIT-selected Canonical 1m bars (roadmap #15; 03-data.md §7.4)."""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any

import pytest

from infrastructure.canonical.resample import ResampleError, resample_bars
from infrastructure.pit.selector import PitSelector
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.pit.test_selector import FAR, _spec
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import SYMBOL, RestHarness, StepClock, utc

M = ss.MINUTE_MS
T_ALIGNED = ss.T0 + M  # 22:15: a 5-minute boundary
DAY_START, DAY_END = utc(2023, 11, 14), utc(2023, 11, 15)


def _klines(count: int, first_ms: int = T_ALIGNED) -> list[list[Any]]:
    """Minutes with distinct prices; every bar lawful (low <= open/close <= high)."""
    out = []
    for i in range(count):
        o, cl = Decimal("100") + i, Decimal("101") + i
        out.append(
            ss.kline_item(
                first_ms + i * M,
                open_=f"{o:.8f}",
                high=f"{cl + 2:.8f}",
                low=f"{o - 1:.8f}",
                close=f"{cl:.8f}",
                volume="10.00000000",
                quote_volume="1000.00000000",
                trades=5 + i,
                taker_base="4.00000000",
                taker_quote="400.00000000",
            )
        )
    return out


def _selection(h: RestHarness, items: list[list[Any]], **spec: Any) -> Any:
    archive = c.ingest_archive(
        h, "klines_1m", ss.archive_kline_lines(items), knowledge=utc(2023, 12, 1)
    )
    c.normalizer(h, clock=StepClock(start=utc(2023, 12, 6))).normalize_unit(
        c.ARCHIVE_KLINES.table, archive
    )
    bars_spec = _spec(h, cutoff=FAR, **spec)
    return PitSelector(h.adapter, h.storage).select(
        bars_spec, "klines_1m", SYMBOL, DAY_START, DAY_END
    )


def test_a_complete_five_minute_bar_aggregates_exactly(h: RestHarness) -> None:
    selection = _selection(h, _klines(5))
    [bar] = resample_bars(selection, 5, DAY_START, DAY_END)
    assert (bar.interval_start, bar.interval_end) == (
        utc(2023, 11, 14, 22, 15),
        utc(2023, 11, 14, 22, 20),
    )
    assert (bar.open, bar.close) == (Decimal("100"), Decimal("105"))
    assert (bar.high, bar.low) == (Decimal("107"), Decimal("99"))
    assert (bar.volume, bar.quote_volume, bar.trade_count) == (
        Decimal("50"),
        Decimal("5000"),
        5 + 6 + 7 + 8 + 9,
    )
    assert (bar.taker_buy_base_volume, bar.taker_buy_quote_volume) == (
        Decimal("20"),
        Decimal("2000"),
    )
    assert bar.complete and bar.minutes_present == 5 and bar.symbol == "BTC-USDT"
    rows = sorted(h.rows(c.BARS), key=lambda row: row["interval_start"])
    assert bar.constituents == tuple(row["revision_id"] for row in rows)
    # Not observable before its interval ends, nor before its inputs are available.
    assert bar.available_time == max(bar.interval_end, *(row["available_time"] for row in rows))
    assert bar.knowledge_time == max(row["knowledge_time"] for row in rows)


def test_missing_minutes_make_incomplete_bars_and_nothing_is_filled(h: RestHarness) -> None:
    items = _klines(7, first_ms=ss.T0)  # 22:14 .. 22:20
    del items[3]  # 22:17 missing
    bars = resample_bars(_selection(h, items), 5, DAY_START, DAY_END)
    assert [(b.interval_start.minute, b.minutes_present, b.complete) for b in bars] == [
        (10, 1, False),  # 22:10 bucket: only 22:14
        (15, 4, False),  # 22:15 bucket: 22:15, 22:16, 22:18, 22:19 — 22:17 missing
        (20, 1, False),  # 22:20 bucket: only 22:20
    ]
    middle = bars[1]
    assert middle.volume == Decimal("40")  # four minutes, no invented fifth


def test_resampling_is_bit_identical_across_runs_and_catalogs(h: RestHarness) -> None:
    items = _klines(10)
    first = resample_bars(_selection(h, items), 5, DAY_START, DAY_END)
    assert resample_bars(_selection_again(h), 5, DAY_START, DAY_END) == first
    with ss.sqlite_harness(h.tmp_path / "other") as other:
        independent = resample_bars(_selection(other, items), 5, DAY_START, DAY_END)
    assert [b.content_sha256 for b in independent] == [b.content_sha256 for b in first]
    assert [
        b.interval_start for b in resample_bars(_selection_again(h), 60, DAY_START, DAY_END)
    ] == [utc(2023, 11, 14, 22)]


def _selection_again(h: RestHarness) -> Any:
    return PitSelector(h.adapter, h.storage).select(
        _spec(h, cutoff=FAR), "klines_1m", SYMBOL, DAY_START, DAY_END
    )


def test_the_window_bounds_which_minutes_count(h: RestHarness) -> None:
    selection = _selection(h, _klines(5))
    [bar] = resample_bars(selection, 5, utc(2023, 11, 14, 22, 17), DAY_END)
    assert bar.minutes_present == 3 and not bar.complete


@pytest.mark.parametrize("minutes", [0, 7, True])
def test_periods_must_divide_a_day(h: RestHarness, minutes: Any) -> None:
    selection = _selection(h, _klines(2))
    with pytest.raises(ResampleError):
        resample_bars(selection, minutes, DAY_START, DAY_END)


def test_an_interval_selection_is_refused(h: RestHarness) -> None:
    items = _klines(2)
    archive = c.ingest_archive(
        h, "klines_1m", ss.archive_kline_lines(items), knowledge=utc(2023, 12, 1)
    )
    c.normalizer(h, clock=StepClock(start=utc(2023, 12, 6))).normalize_unit(
        c.ARCHIVE_KLINES.table, archive
    )
    spec = _spec(h, cutoff=FAR, interval=(utc(2023, 11, 15), utc(2023, 12, 31)))
    selection = PitSelector(h.adapter, h.storage).select(
        spec, "klines_1m", SYMBOL, DAY_START, DAY_END
    )
    with pytest.raises(ResampleError, match="interval selection"):
        resample_bars(selection, 5, DAY_START, DAY_END)


def test_conflicts_and_trade_selections_are_refused(h: RestHarness) -> None:
    items = _klines(1)
    archive_items = [list(items[0])]
    archive_items[0][4] = f"{Decimal(items[0][4]) + 1:.8f}"  # close differs, still <= high
    archive = c.ingest_archive(
        h, "klines_1m", ss.archive_kline_lines(archive_items), knowledge=utc(2023, 12, 1)
    )
    [response] = c.ingest_rest(h, "klines_1m", items, knowledge=utc(2023, 12, 5))
    c.normalizer(h, clock=StepClock(start=utc(2023, 12, 6))).normalize_unit(
        c.ARCHIVE_KLINES.table, archive
    )
    c.normalizer(h, clock=StepClock(start=utc(2023, 12, 7))).normalize_unit(
        c.REST_KLINES.table, response
    )
    selection = PitSelector(h.adapter, h.storage).select(
        _spec(h, cutoff=FAR), "klines_1m", SYMBOL, DAY_START, DAY_END
    )
    assert selection.conflicts
    with pytest.raises(ResampleError, match="competing heads"):
        resample_bars(selection, 5, DAY_START, DAY_END)
    trades = dataclasses.replace(selection, canonical_table="canonical.trades", conflicts=())
    with pytest.raises(ResampleError, match="bars_1m"):
        resample_bars(trades, 5, DAY_START, DAY_END)


def test_a_bar_is_never_available_before_its_interval_ends(h: RestHarness) -> None:
    """REST minutes fetched at 22:17:01 are available then; their 5m bar only at 22:20."""
    retrieved = T_ALIGNED + 2 * M + 1_000
    items = _klines(2)  # 22:15, 22:16 — closed by 22:17:01
    [response] = c.ingest_rest(
        h, "klines_1m", items, knowledge=utc(2023, 12, 5), retrieved_ms=retrieved
    )
    c.normalizer(h, clock=StepClock(start=utc(2023, 12, 6))).normalize_unit(
        c.REST_KLINES.table, response
    )
    selection = PitSelector(h.adapter, h.storage).select(
        _spec(h, cutoff=FAR), "klines_1m", SYMBOL, DAY_START, DAY_END
    )
    [bar] = resample_bars(selection, 5, DAY_START, DAY_END)
    assert max(row["available_time"] for row in h.rows(c.BARS)) < bar.interval_end
    assert bar.available_time == bar.interval_end == utc(2023, 11, 14, 22, 20)

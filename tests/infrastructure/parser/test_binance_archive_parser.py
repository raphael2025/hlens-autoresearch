"""Behavior tests for ``binance.spot.archive.parser@1.0.0`` (Phase 1 D1; roadmap acceptance #11)."""

from __future__ import annotations

import ast
import hashlib
import inspect
import io
import tempfile
import zipfile
from dataclasses import fields
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pyarrow as pa  # type: ignore[import-untyped]
import pytest

from core.contracts.revision import PolicyRole
from core.contracts.storage import StorageAdapter
from core.domain.base import canonical_json
from infrastructure.parser import (
    AGG_TRADES_ROW_SCHEMA,
    KLINES_1M_ROW_SCHEMA,
    PARSER_BINDING,
    PARSER_SPEC,
    ArchiveParseRequest,
    ArchiveRejection,
    ParsedArchive,
    RejectionCode,
    TimeUnit,
    UnsupportedArchiveRequest,
    parse_archive,
    parse_archive_bytes,
    time_unit_for,
)
from infrastructure.parser import binance_archive as parser_mod
from infrastructure.parser.binance_archive import member_filename, parse_archive_spooled
from infrastructure.revision.row_integrity import (
    PersistedRowVerifier,
    VerifiedArchive,
    _archive_rows_at,
)
from tests.infrastructure.parser.parser_support import (
    MS_DAY,
    US_DAY,
    agg_rows,
    archive_for,
    csv_bytes,
    csv_case,
    day_start,
    expect_rejection,
    kline_row,
    kline_rows,
    make_case,
    make_zip,
    object_key,
    patch_member,
    start_ticks,
)

#: Golden parser hash for 1.0.0; any rule / limit / layout change must bump the parser version.
PARSER_HASH_1_0_0 = "c2c3c375e6fa87982759549ccbc0779c546929a64454ed14c2063677212ac033"
AGG = "agg_trades"
KLINES = "klines_1m"


def _parse(data_type: str, day: date, rows: list[str], *, symbol: str = "BTCUSDT") -> object:
    case = csv_case(data_type, day, csv_bytes(rows), symbol=symbol)
    return parse_archive_bytes(case.request, case.data)


def _parsed(data_type: str, day: date, rows: list[str]) -> ParsedArchive:
    outcome = _parse(data_type, day, rows)
    assert isinstance(outcome, ParsedArchive), outcome
    return outcome


def _rejected(data_type: str, day: date, rows: list[str]) -> ArchiveRejection:
    return expect_rejection(_parse(data_type, day, rows))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- binding / spec


def test_parser_binding_is_versioned_and_derived_from_the_spec() -> None:
    assert PARSER_BINDING.role is PolicyRole.PARSER
    assert (PARSER_BINDING.policy_id, PARSER_BINDING.version) == (
        "binance.spot.archive.parser",
        "1.0.0",
    )
    derived = hashlib.sha256(canonical_json(PARSER_SPEC).encode("utf-8")).hexdigest()
    assert PARSER_BINDING.policy_hash == derived == PARSER_HASH_1_0_0
    assert PARSER_SPEC["time_unit"]["switch_at"] == "2025-01-01T00:00:00Z"
    assert PARSER_SPEC["coverage"]["tolerance_ticks"] == 0


@pytest.mark.parametrize("data_type", [AGG, KLINES])
def test_time_unit_is_decided_by_coverage_date_only(data_type: str) -> None:
    assert time_unit_for(data_type, day_start(MS_DAY)) is TimeUnit.MILLISECOND
    assert time_unit_for(data_type, day_start(US_DAY)) is TimeUnit.MICROSECOND
    assert time_unit_for(data_type, day_start(date(2017, 8, 17))) is TimeUnit.MILLISECOND
    assert time_unit_for(data_type, day_start(date(2026, 9, 24))) is TimeUnit.MICROSECOND


def test_parser_never_guesses_units_from_magnitudes() -> None:
    source = inspect.getsource(parser_mod)
    tree = ast.parse(source)
    divisions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, (ast.Div, ast.FloorDiv))
        and "micros_per_tick" not in ast.unparse(node)
    ]
    assert divisions == []
    assert "float(" not in source


# --------------------------------------------------------------------------- happy paths


def test_klines_ms_day_success_binds_everything() -> None:
    parsed = _parsed(KLINES, MS_DAY, kline_rows(MS_DAY, 3))
    assert parsed.parser == PARSER_BINDING
    assert parsed.time_unit is TimeUnit.MILLISECOND
    assert (parsed.data_type, parsed.symbol) == (KLINES, "BTCUSDT")
    assert parsed.archive_revision_id == "archive-rev-1"
    assert parsed.member_name == "BTCUSDT-1m-2024-12-31.csv"
    assert parsed.coverage_start == day_start(MS_DAY)
    assert parsed.rows.schema.equals(KLINES_1M_ROW_SCHEMA)
    first = parsed.rows.to_pylist()[0]
    assert first["archive_line_number"] == 1
    assert first["open_time_raw"] == 1735603200000
    assert first["close_time_raw"] == 1735603259999
    assert first["interval_start"] == datetime(2024, 12, 31, tzinfo=UTC)
    assert first["interval_end"] == datetime(2024, 12, 31, 0, 1, tzinfo=UTC)
    assert first["open"] == Decimal("92792.05")
    assert first["number_of_trades"] == 1660
    assert first["ignore_raw"] == "0"
    assert [row["archive_line_number"] for row in parsed.rows.to_pylist()] == [1, 2, 3]


def test_klines_us_day_success() -> None:
    parsed = _parsed(KLINES, US_DAY, kline_rows(US_DAY, 2))
    assert parsed.time_unit is TimeUnit.MICROSECOND
    rows = parsed.rows.to_pylist()
    assert rows[0]["open_time_raw"] == 1735689600000000
    assert rows[0]["close_time_raw"] == 1735689659999999
    assert rows[1]["interval_start"] == datetime(2025, 1, 1, 0, 1, tzinfo=UTC)


@pytest.mark.parametrize(("day", "unit"), [(MS_DAY, 1_000), (US_DAY, 1)])
def test_agg_trades_success_converts_by_declared_unit(day: date, unit: int) -> None:
    parsed = _parsed(AGG, day, agg_rows(day, 3, step=7))
    assert parsed.rows.schema.equals(AGG_TRADES_ROW_SCHEMA)
    rows = parsed.rows.to_pylist()
    base = start_ticks(day)
    assert [row["timestamp_raw"] for row in rows] == [base, base + 7, base + 14]
    assert rows[1]["event_time"] == day_start(day) + timedelta(microseconds=7 * unit)
    assert rows[0]["is_buyer_maker"] is False and rows[1]["is_buyer_maker"] is True
    assert rows[0]["is_best_match"] is True
    assert rows[0]["price"] == Decimal("92792.05")
    assert rows[2]["archive_line_number"] == 3


def test_last_minute_and_last_tick_are_inside_coverage() -> None:
    assert _parsed(KLINES, US_DAY, [kline_row(US_DAY, 1439)]).row_count == 1
    end_tick = start_ticks(MS_DAY) + 86_400_000 - 1
    row = f"1,1.0,1.0,1,1,{end_tick},False,True"
    assert _parsed(AGG, MS_DAY, [row]).row_count == 1


def test_eth_symbol_supported() -> None:
    outcome = _parse(KLINES, US_DAY, kline_rows(US_DAY, 1), symbol="ETHUSDT")
    assert isinstance(outcome, ParsedArchive)
    assert outcome.member_name == "ETHUSDT-1m-2025-01-01.csv"


# --------------------------------------------------------------------------- counterexample 1


def test_ms_day_with_microsecond_values_is_rejected() -> None:
    kline = _rejected(KLINES, MS_DAY, kline_rows(MS_DAY, 2, tps=1_000_000))
    assert (kline.code, kline.line_number) == (RejectionCode.TIMESTAMP_OUT_OF_COVERAGE, 1)
    agg = _rejected(AGG, MS_DAY, agg_rows(MS_DAY, 2, tps=1_000_000))
    assert (agg.code, agg.line_number, agg.column) == (
        RejectionCode.TIMESTAMP_OUT_OF_COVERAGE,
        1,
        "timestamp_raw",
    )


def test_us_day_with_millisecond_values_is_rejected() -> None:
    kline = _rejected(KLINES, US_DAY, kline_rows(US_DAY, 2, tps=1_000))
    assert (kline.code, kline.line_number) == (RejectionCode.TIMESTAMP_OUT_OF_COVERAGE, 1)
    agg = _rejected(AGG, US_DAY, agg_rows(US_DAY, 2, tps=1_000))
    assert agg.code is RejectionCode.TIMESTAMP_OUT_OF_COVERAGE


def test_mixed_units_in_one_file_are_rejected_at_the_first_foreign_row() -> None:
    rows = agg_rows(US_DAY, 3)
    rows.append(f"503,1.0,1.0,2000,2000,{start_ticks(US_DAY, tps=1_000) + 5},False,True")
    rejection = _rejected(AGG, US_DAY, rows)
    assert (rejection.code, rejection.line_number) == (RejectionCode.TIMESTAMP_OUT_OF_COVERAGE, 4)


# --------------------------------------------------------------------------- counterexample 2


@pytest.mark.parametrize("day", [MS_DAY, US_DAY])
def test_agg_timestamp_at_coverage_end_or_before_start_is_rejected(day: date) -> None:
    per_day = 86_400 * (1_000_000 if day == US_DAY else 1_000)
    for ticks in (start_ticks(day) + per_day, start_ticks(day) - 1):
        row = f"1,1.0,1.0,1,1,{ticks},False,True"
        rejection = _rejected(AGG, day, [row])
        assert rejection.code is RejectionCode.TIMESTAMP_OUT_OF_COVERAGE


@pytest.mark.parametrize("day", [MS_DAY, US_DAY])
def test_kline_open_at_coverage_end_or_before_start_is_rejected(day: date) -> None:
    for minute in (1440, -1):
        rejection = _rejected(KLINES, day, [kline_row(day, minute)])
        assert rejection.code is RejectionCode.TIMESTAMP_OUT_OF_COVERAGE


def test_kline_close_time_in_wrong_unit_is_rejected() -> None:
    opened = start_ticks(US_DAY)
    row = kline_row(US_DAY, 0, close_ticks=opened // 1000 + 59_999)
    rejection = _rejected(KLINES, US_DAY, [row])
    assert (rejection.code, rejection.column) == (
        RejectionCode.KLINE_CLOSE_TIME_MISMATCH,
        "close_time_raw",
    )
    ms_open = start_ticks(MS_DAY)
    row = kline_row(MS_DAY, 0, close_ticks=ms_open * 1000 + 59_999_999)
    assert _rejected(KLINES, MS_DAY, [row]).code is RejectionCode.KLINE_CLOSE_TIME_MISMATCH


def test_kline_close_time_crossing_the_day_or_not_one_minute_is_rejected() -> None:
    last_open = start_ticks(MS_DAY) + 1439 * 60_000
    cases = [
        kline_row(MS_DAY, 1439, close_ticks=last_open + 60_000),  # == coverage end (next day)
        kline_row(MS_DAY, 1439, close_ticks=last_open + 120_000 - 1),  # 2m bar, crosses day
        kline_row(MS_DAY, 3, close_ticks=start_ticks(MS_DAY) + 3 * 60_000 + 300_000 - 1),  # 5m
        kline_row(MS_DAY, 3, close_ticks=start_ticks(MS_DAY) + 3 * 60_000 + 59_998),  # short
    ]
    for row in cases:
        assert _rejected(KLINES, MS_DAY, [row]).code is RejectionCode.KLINE_CLOSE_TIME_MISMATCH


@pytest.mark.parametrize("offset", [1, 30_000, 59_999])
def test_kline_open_not_on_a_whole_minute_is_rejected(offset: int) -> None:
    opened = start_ticks(MS_DAY) + offset
    rejection = _rejected(KLINES, MS_DAY, [kline_row(MS_DAY, 0, open_ticks=opened)])
    assert rejection.code is RejectionCode.KLINE_OPEN_NOT_ALIGNED


# --------------------------------------------------------------------------- counterexample 3


_BAD_AGG_FIELDS: list[tuple[int, str, RejectionCode]] = [
    (0, "-501", RejectionCode.INVALID_INTEGER),
    (0, "+501", RejectionCode.INVALID_INTEGER),
    (0, "0501", RejectionCode.INVALID_INTEGER),
    (0, " 501", RejectionCode.INVALID_INTEGER),
    (0, "5_01", RejectionCode.INVALID_INTEGER),
    (0, "501.0", RejectionCode.INVALID_INTEGER),
    (0, "", RejectionCode.INVALID_INTEGER),
    (0, "٥", RejectionCode.NON_ASCII_CONTENT),
    (0, "9223372036854775808", RejectionCode.INTEGER_OUT_OF_RANGE),
    (1, "NaN", RejectionCode.INVALID_DECIMAL),
    (1, "Infinity", RejectionCode.INVALID_DECIMAL),
    (1, "inf", RejectionCode.INVALID_DECIMAL),
    (1, "1e5", RejectionCode.INVALID_DECIMAL),
    (1, "-1.5", RejectionCode.INVALID_DECIMAL),
    (1, ".5", RejectionCode.INVALID_DECIMAL),
    (1, "5.", RejectionCode.INVALID_DECIMAL),
    (1, "01.5", RejectionCode.INVALID_DECIMAL),
    (1, "1,5", RejectionCode.COLUMN_COUNT),
    (1, '"1.5"', RejectionCode.INVALID_DECIMAL),
    (1, "0", RejectionCode.NON_POSITIVE_PRICE),
    (1, "0.00000000", RejectionCode.NON_POSITIVE_PRICE),
    (2, "0.00000000", RejectionCode.NON_POSITIVE_QUANTITY),
    (2, "-0.1", RejectionCode.INVALID_DECIMAL),
    (3, "-1", RejectionCode.INVALID_INTEGER),
    (5, "1735603200000.0", RejectionCode.INVALID_INTEGER),
    (5, "abc", RejectionCode.INVALID_INTEGER),
    (6, "true", RejectionCode.INVALID_BOOLEAN),
    (6, "1", RejectionCode.INVALID_BOOLEAN),
    (6, "", RejectionCode.INVALID_BOOLEAN),
    (7, "FALSE", RejectionCode.INVALID_BOOLEAN),
]


@pytest.mark.parametrize(("index", "value", "code"), _BAD_AGG_FIELDS)
def test_bad_agg_field_after_valid_prefix_rejects_whole_file(
    index: int, value: str, code: RejectionCode
) -> None:
    rows = agg_rows(MS_DAY, 4)
    fields_ = rows[2].split(",")
    fields_[index] = value
    rows[2] = ",".join(fields_)
    outcome = _parse(AGG, MS_DAY, rows)
    rejection = expect_rejection(outcome)  # type: ignore[arg-type]
    assert (rejection.code, rejection.line_number) == (code, 3)


@pytest.mark.parametrize(
    ("column_count"),
    [7, 9, 1],
)
def test_wrong_agg_column_count_rejected(column_count: int) -> None:
    rows = agg_rows(MS_DAY, 3)
    fields_ = (rows[1].split(",") + ["True"])[:column_count]
    rows[1] = ",".join(fields_)
    rejection = _rejected(AGG, MS_DAY, rows)
    assert (rejection.code, rejection.line_number) == (RejectionCode.COLUMN_COUNT, 2)


_BAD_KLINE_KWARGS: list[tuple[dict[str, str], RejectionCode]] = [
    ({"volume": "NaN"}, RejectionCode.INVALID_DECIMAL),
    ({"high": "Infinity"}, RejectionCode.INVALID_DECIMAL),
    ({"trades": "-3"}, RejectionCode.INVALID_INTEGER),
    ({"trades": "1.0"}, RejectionCode.INVALID_INTEGER),
    ({"ignore": "x"}, RejectionCode.INVALID_DECIMAL),
    ({"ignore": ""}, RejectionCode.INVALID_DECIMAL),
    ({"open_": "0"}, RejectionCode.NON_POSITIVE_PRICE),
    ({"low": "0.00000000"}, RejectionCode.NON_POSITIVE_PRICE),
    ({"high": "92782.12500000"}, RejectionCode.OHLC_INVARIANT),  # high < open
    ({"low": "92782.14000000"}, RejectionCode.OHLC_INVARIANT),  # low > close
    ({"low": "92900.00000000"}, RejectionCode.OHLC_INVARIANT),  # low > high
    ({"taker_base": "6.45789001"}, RejectionCode.TAKER_VOLUME_INVARIANT),
    ({"taker_quote": "599298.29174061"}, RejectionCode.TAKER_VOLUME_INVARIANT),
]


@pytest.mark.parametrize(("kwargs", "code"), _BAD_KLINE_KWARGS)
def test_bad_kline_row_after_valid_prefix_rejects_whole_file(
    kwargs: dict[str, str], code: RejectionCode
) -> None:
    rows = kline_rows(US_DAY, 2) + [kline_row(US_DAY, 2, **kwargs)]  # type: ignore[arg-type]
    rejection = _rejected(KLINES, US_DAY, rows)
    assert (rejection.code, rejection.line_number) == (code, 3)


def test_zero_volume_minute_is_valid() -> None:
    row = kline_row(
        US_DAY,
        0,
        volume="0.00000000",
        quote_volume="0.00000000",
        trades="0",
        taker_base="0.00000000",
        taker_quote="0.00000000",
        high="92792.05000000",
        low="92792.05000000",
        close="92792.05000000",
    )
    assert _parsed(KLINES, US_DAY, [row]).row_count == 1


def test_trade_id_range_inverted_rejected() -> None:
    rows = agg_rows(MS_DAY, 2)
    rows.append(f"502,1.0,1.0,2000,1999,{start_ticks(MS_DAY) + 9},False,True")
    rejection = _rejected(AGG, MS_DAY, rows)
    assert (rejection.code, rejection.line_number) == (RejectionCode.TRADE_ID_RANGE_INVALID, 3)


# --------------------------------------------------------------------------- counterexample 6


def test_duplicate_and_out_of_order_agg_rows_rejected() -> None:
    rows = agg_rows(MS_DAY, 3)
    duplicate = _rejected(AGG, MS_DAY, rows + [rows[-1]])
    assert (duplicate.code, duplicate.line_number) == (RejectionCode.DUPLICATE_ROW, 4)
    swapped = _rejected(AGG, MS_DAY, [rows[0], rows[2], rows[1]])
    assert (swapped.code, swapped.line_number) == (RejectionCode.OUT_OF_ORDER, 3)


def test_decreasing_agg_timestamp_rejected() -> None:
    base = start_ticks(MS_DAY)
    rows = [f"1,1.0,1.0,1,1,{base + 5},False,True", f"2,1.0,1.0,2,2,{base + 4},False,True"]
    rejection = _rejected(AGG, MS_DAY, rows)
    assert (rejection.code, rejection.line_number) == (RejectionCode.TIMESTAMP_DECREASING, 2)


def test_overlapping_trade_id_ranges_rejected() -> None:
    base = start_ticks(MS_DAY)
    rows = [f"1,1.0,1.0,10,20,{base},False,True", f"2,1.0,1.0,20,25,{base},False,True"]
    rejection = _rejected(AGG, MS_DAY, rows)
    assert (rejection.code, rejection.line_number) == (RejectionCode.TRADE_ID_OVERLAP, 2)


def test_agg_id_gaps_and_equal_timestamps_are_allowed() -> None:
    base = start_ticks(US_DAY)
    rows = [f"1,1.0,1.0,10,20,{base},False,True", f"9,1.0,1.0,30,30,{base},True,True"]
    assert _parsed(AGG, US_DAY, rows).row_count == 2


def test_duplicate_and_out_of_order_klines_rejected() -> None:
    rows = kline_rows(MS_DAY, 3)
    duplicate = _rejected(KLINES, MS_DAY, rows + [rows[-1]])
    assert (duplicate.code, duplicate.line_number) == (RejectionCode.DUPLICATE_ROW, 4)
    swapped = _rejected(KLINES, MS_DAY, [rows[1], rows[0]])
    assert (swapped.code, swapped.line_number) == (RejectionCode.OUT_OF_ORDER, 2)


def test_missing_minutes_are_not_a_parser_rejection() -> None:
    rows = [kline_row(MS_DAY, 0), kline_row(MS_DAY, 5), kline_row(MS_DAY, 1439)]
    assert _parsed(KLINES, MS_DAY, rows).row_count == 3


def _content_case(content: bytes, data_type: str = KLINES, day: date = US_DAY) -> ArchiveRejection:
    case = make_case(data_type, day, archive_for(data_type, "BTCUSDT", day, content))
    return expect_rejection(parse_archive_bytes(case.request, case.data))


def test_header_row_is_rejected() -> None:
    header = (
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
        "taker_buy_volume,taker_buy_quote_volume,ignore"
    )
    rejection = _content_case(csv_bytes([header] + kline_rows(US_DAY, 2)))
    assert (rejection.code, rejection.line_number, rejection.column) == (
        RejectionCode.INVALID_INTEGER,
        1,
        "open_time_raw",
    )


@pytest.mark.parametrize(
    ("content", "code", "line"),
    [
        (b"\n", RejectionCode.EMPTY_LINE, 1),
        (b"ROW0\n\nROW1\n", RejectionCode.EMPTY_LINE, 2),
        (b"ROW0\nROW1\n\n", RejectionCode.EMPTY_LINE, 3),
        (b"ROW0\r\nROW1\r\n", RejectionCode.INVALID_LINE_ENDING, 1),
        (b"ROW0\nROW1\rROW2\n", RejectionCode.INVALID_LINE_ENDING, 2),
        (b"ROW0\nROW1", RejectionCode.INVALID_LINE_ENDING, 2),
        (b"\xef\xbb\xbfROW0\n", RejectionCode.NON_ASCII_CONTENT, 1),
        (b"ROW0\nROW1\xff\n", RejectionCode.NON_ASCII_CONTENT, 2),
        (b"ROW0\n" + b"9" * 1025 + b"\n", RejectionCode.LINE_TOO_LONG, 2),
        (b"ROW0\nROW1\n\x00", RejectionCode.INVALID_LINE_ENDING, 3),
    ],
)
def test_csv_framing_rules(content: bytes, code: RejectionCode, line: int) -> None:
    rows = kline_rows(US_DAY, 3)
    for index, row in enumerate(rows):
        content = content.replace(f"ROW{index}".encode(), row.encode())
    rejection = _content_case(content)
    assert (rejection.code, rejection.line_number) == (code, line)


def test_line_of_exactly_max_length_is_framed_then_field_checked() -> None:
    name = member_filename(KLINES, "BTCUSDT", US_DAY)
    for body, code in (
        (b"9" * 1024, RejectionCode.COLUMN_COUNT),
        (b"9" * 1025, RejectionCode.LINE_TOO_LONG),
    ):
        data = make_zip([(name, body + b"\n")], compression=zipfile.ZIP_STORED)
        case = make_case(KLINES, US_DAY, data)
        rejection = expect_rejection(parse_archive_bytes(case.request, case.data))
        assert (rejection.code, rejection.line_number) == (code, 1)


def test_member_without_rows_rejected() -> None:
    assert _content_case(b"").code is RejectionCode.NO_ROWS


# --------------------------------------------------------------------------- counterexample 7


def test_decimal_values_are_exact_at_full_decimal_38_18_capacity() -> None:
    widest = "99999999999999999999.999999999999999999"  # 20 integer + 18 fraction digits
    tiny = "0.000000000000000001"
    float_trap = "0.10000000000000001"  # float(…) == 0.1
    row = kline_row(
        US_DAY,
        0,
        open_=float_trap,
        high=widest,
        low=tiny,
        close=float_trap,
        quote_volume=widest,
        taker_quote=widest,
    )
    first = _parsed(KLINES, US_DAY, [row]).rows.to_pylist()[0]
    assert first["high"] == Decimal(widest)
    assert first["low"] == Decimal(tiny)
    assert first["open"] == Decimal(float_trap) != Decimal(0.1)
    assert str(first["open"]) == "0.100000000000000010"


@pytest.mark.parametrize(
    "value",
    [
        "0.1234567890123456789",  # 19 fraction digits
        "100000000000000000000",  # 21 integer digits
        "100000000000000000000.5",
    ],
)
def test_decimal_beyond_decimal_38_18_rejected(value: str) -> None:
    rows = agg_rows(MS_DAY, 2)
    fields_ = rows[1].split(",")
    fields_[1] = value
    rows[1] = ",".join(fields_)
    rejection = _rejected(AGG, MS_DAY, rows)
    assert (rejection.code, rejection.line_number, rejection.column) == (
        RejectionCode.DECIMAL_OUT_OF_RANGE,
        2,
        "price",
    )


# --------------------------------------------------------------------------- counterexample 4


@pytest.mark.parametrize(
    "key",
    [
        object_key(KLINES, "BTCUSDT", date(2025, 1, 2)),  # other date
        object_key(KLINES, "ETHUSDT", US_DAY),  # other symbol
        object_key(AGG, "BTCUSDT", US_DAY),  # other data type
        "raw/binance/spot/archive/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2025-01-01.zip.bak",
    ],
)
def test_object_key_must_match_request(key: str) -> None:
    data = archive_for(KLINES, "BTCUSDT", US_DAY, csv_bytes(kline_rows(US_DAY, 1)))
    case = make_case(KLINES, US_DAY, data, key=key)
    rejection = expect_rejection(parse_archive_bytes(case.request, case.data))
    assert rejection.code is RejectionCode.OBJECT_KEY_MISMATCH


@pytest.mark.parametrize(
    ("data_type", "symbol", "day"),
    [
        (KLINES, "BTCUSDT", date(2024, 12, 30)),  # other date
        (KLINES, "ETHUSDT", US_DAY),  # other symbol
        (AGG, "BTCUSDT", US_DAY),  # other data type
    ],
)
def test_member_name_must_match_request(data_type: str, symbol: str, day: date) -> None:
    content = csv_bytes(kline_rows(US_DAY, 1))
    data = make_zip([(member_filename(data_type, symbol, day), content)])
    case = make_case(KLINES, US_DAY, data)
    rejection = expect_rejection(parse_archive_bytes(case.request, case.data))
    assert rejection.code is RejectionCode.ZIP_MEMBER_NAME_MISMATCH


def test_content_of_other_date_is_rejected_even_with_matching_names() -> None:
    content = csv_bytes(kline_rows(date(2025, 1, 2), 2))
    rejection = _content_case(content)
    assert rejection.code is RejectionCode.TIMESTAMP_OUT_OF_COVERAGE


def test_kline_content_under_agg_request_is_rejected() -> None:
    content = csv_bytes(kline_rows(US_DAY, 2))
    rejection = _content_case(content, data_type=AGG)
    assert (rejection.code, rejection.line_number) == (RejectionCode.COLUMN_COUNT, 1)


def test_object_bytes_must_match_object_ref() -> None:
    data = archive_for(KLINES, "BTCUSDT", US_DAY, csv_bytes(kline_rows(US_DAY, 1)))
    wrong_hash = make_case(KLINES, US_DAY, data, sha256="0" * 64)
    assert (
        expect_rejection(parse_archive_bytes(wrong_hash.request, data)).code
        is RejectionCode.OBJECT_INTEGRITY_MISMATCH
    )
    wrong_size = make_case(KLINES, US_DAY, data, size=len(data) + 1)
    assert (
        expect_rejection(parse_archive_bytes(wrong_size.request, data)).code
        is RejectionCode.OBJECT_INTEGRITY_MISMATCH
    )


def test_archive_size_cap_is_checked_before_reading() -> None:
    case = make_case(KLINES, US_DAY, b"not read", size=(1 << 30) + 1)
    rejection = expect_rejection(parse_archive_bytes(case.request, b"not read"))
    assert rejection.code is RejectionCode.ARCHIVE_TOO_LARGE


# --------------------------------------------------------------------------- requests


def _request(**overrides: object) -> ArchiveParseRequest:
    base = make_case(KLINES, US_DAY, b"x").request
    values = {f.name: getattr(base, f.name) for f in fields(base)}
    values.update(overrides)
    return ArchiveParseRequest(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"symbol": "XRPUSDT"},
        {"symbol": "btcusdt"},
        {"data_type": "klines_5m"},
        {"data_type": "trades"},
        {"coverage_start": datetime(2025, 1, 1, 1, tzinfo=UTC)},
        {"coverage_end": datetime(2025, 1, 3, tzinfo=UTC)},
        {"coverage_start": datetime(2025, 1, 1), "coverage_end": datetime(2025, 1, 2)},
        {"archive_revision_id": ""},
        {"archive_revision_id": " rev"},
        {"archive_revision_id": "rev\n"},
        {"archive_revision_id": "r" * 257},
        {"object_ref": "raw/key.zip"},
    ],
)
def test_unsupported_requests_raise(overrides: dict[str, object]) -> None:
    with pytest.raises(UnsupportedArchiveRequest):
        _request(**overrides)


# --------------------------------------------------------------------------- determinism / events


def test_parsing_twice_yields_equal_results_and_no_shared_state() -> None:
    rows = agg_rows(US_DAY, 70_000)  # crosses the 65 536-row Arrow chunk boundary
    case = csv_case(AGG, US_DAY, csv_bytes(rows))
    first = parse_archive_bytes(case.request, case.data)
    second = parse_archive_bytes(case.request, case.data)
    assert isinstance(first, ParsedArchive) and isinstance(second, ParsedArchive)
    assert first == second
    assert first.row_count == 70_000
    assert first.rows.column("archive_line_number").to_pylist() == list(range(1, 70_001))
    assert first.rows.column("agg_trade_id").num_chunks == 2


class _MemoryArchiveStorage:
    def __init__(self, expected_ref: object, data: bytes) -> None:
        self.expected_ref = expected_ref
        self.data = data

    def open_read(self, ref: object) -> io.BytesIO:
        assert ref == self.expected_ref
        return io.BytesIO(self.data)


def test_spooled_parser_matches_the_public_table_result_and_cursor_is_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parser_mod, "_CHUNK_ROWS", 2)
    case = csv_case(AGG, US_DAY, csv_bytes(agg_rows(US_DAY, 3)))
    parsed = parse_archive_bytes(case.request, case.data)
    assert isinstance(parsed, ParsedArchive)

    temp_files: list[io.BufferedRandom] = []
    make_temp = tempfile.TemporaryFile

    def tracked_temp_file(*args: Any, **kwargs: Any) -> io.BufferedRandom:
        handle = cast(io.BufferedRandom, make_temp(*args, **kwargs))
        temp_files.append(handle)
        return handle

    monkeypatch.setattr(tempfile, "TemporaryFile", tracked_temp_file)
    spooled = parse_archive_spooled(
        case.request,
        cast(StorageAdapter, _MemoryArchiveStorage(case.request.object_ref, case.data)),
    )
    assert isinstance(spooled, parser_mod.SpooledArchive)
    assert spooled.row_count == parsed.row_count == 3
    assert [row["archive_line_number"] for row in _archive_rows_at(spooled, [1, 3, 3])] == [
        1,
        3,
        3,
    ]

    cursor = spooled.open_cursor()
    first_batch = next(cursor)
    assert first_batch.num_rows == 2
    cursor.close()  # early close releases the reader but leaves the owned spool replayable
    with spooled.open_cursor() as replay:
        batches = list(replay)
    replayed = pa.Table.from_batches(batches, schema=parsed.rows.schema)
    assert replayed.equals(parsed.rows)
    assert len(temp_files) == 2
    assert not temp_files[0].closed  # parsed batches remain owned by the result
    assert temp_files[1].closed  # compressed input spool is scoped to parse_archive_spooled
    spooled.close()
    assert all(handle.closed for handle in temp_files)


def test_spooled_parser_rejects_late_csv_failure_and_cleans_provisional_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = agg_rows(US_DAY, 3)
    last = rows[-1].split(",")
    last[-2] = "no"
    rows[-1] = ",".join(last)
    case = csv_case(AGG, US_DAY, csv_bytes(rows))
    temp_files: list[io.BufferedRandom] = []
    make_temp = tempfile.TemporaryFile

    def tracked_temp_file(*args: Any, **kwargs: Any) -> io.BufferedRandom:
        handle = cast(io.BufferedRandom, make_temp(*args, **kwargs))
        temp_files.append(handle)
        return handle

    monkeypatch.setattr(tempfile, "TemporaryFile", tracked_temp_file)
    outcome = parse_archive_spooled(
        case.request,
        cast(StorageAdapter, _MemoryArchiveStorage(case.request.object_ref, case.data)),
    )
    assert isinstance(outcome, ArchiveRejection)
    assert outcome.code is RejectionCode.INVALID_BOOLEAN
    assert len(temp_files) == 2
    assert all(handle.closed for handle in temp_files)


def test_spooled_parser_rejects_bad_crc_after_flushing_and_cleans_spool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parser_mod, "_CHUNK_ROWS", 2)
    data = archive_for(AGG, "BTCUSDT", US_DAY, csv_bytes(agg_rows(US_DAY, 3)))
    damaged = patch_member(data, crc=0)
    case = make_case(AGG, US_DAY, damaged)
    temp_files: list[io.BufferedRandom] = []
    make_temp = tempfile.TemporaryFile

    def tracked_temp_file(*args: Any, **kwargs: Any) -> io.BufferedRandom:
        handle = cast(io.BufferedRandom, make_temp(*args, **kwargs))
        temp_files.append(handle)
        return handle

    monkeypatch.setattr(tempfile, "TemporaryFile", tracked_temp_file)
    outcome = parse_archive_spooled(
        case.request,
        cast(StorageAdapter, _MemoryArchiveStorage(case.request.object_ref, case.data)),
    )
    assert isinstance(outcome, ArchiveRejection)
    assert outcome.code is RejectionCode.ZIP_MEMBER_CORRUPT
    assert all(handle.closed for handle in temp_files)


def test_public_parse_archive_keeps_the_complete_table_api() -> None:
    case = csv_case(KLINES, US_DAY, csv_bytes(kline_rows(US_DAY, 2)))
    parsed = parse_archive(
        case.request,
        cast(StorageAdapter, _MemoryArchiveStorage(case.request.object_ref, case.data)),
    )
    assert isinstance(parsed, ParsedArchive)
    assert parsed.rows.num_rows == 2


def test_verifier_retains_at_most_one_archive_metadata_entry() -> None:
    verifier = PersistedRowVerifier(
        cast(Any, object()), cast(StorageAdapter, object()), cache_archives=True
    )
    first_item = VerifiedArchive("archive-rev-1", TimeUnit.MICROSECOND, {}, 2)
    second_item = VerifiedArchive("archive-rev-2", TimeUnit.MICROSECOND, {}, 2)

    verifier._cache_archive((AGG, "BTCUSDT", "archive-rev-1"), first_item)
    verifier._cache_archive((AGG, "BTCUSDT", "archive-rev-2"), second_item)
    assert len(verifier._archives) == 1
    assert verifier._archives[(AGG, "BTCUSDT", "archive-rev-2")] is second_item
    verifier.close()
    assert verifier._archives == {}


def test_parsed_archive_equality_is_by_value() -> None:
    first = _parsed(KLINES, MS_DAY, kline_rows(MS_DAY, 2))
    other_rows = _parsed(KLINES, MS_DAY, kline_rows(MS_DAY, 3))
    assert first != other_rows
    assert first != "not a parse result"


def test_rejection_is_deterministic_and_carries_no_raw_payload() -> None:
    marker = "SECRET" * 20
    rows = agg_rows(MS_DAY, 3)
    rows[1] = rows[1].replace("92792.05000000", marker)
    case = csv_case(AGG, MS_DAY, csv_bytes(rows))
    first = expect_rejection(parse_archive_bytes(case.request, case.data))
    second = expect_rejection(parse_archive_bytes(case.request, case.data))
    assert first == second
    assert (first.code, first.line_number, first.column) == (
        RejectionCode.INVALID_DECIMAL,
        2,
        "price",
    )
    assert first.parser == PARSER_BINDING
    assert first.object_key == case.request.object_ref.key
    assert first.object_sha256 == case.request.object_ref.sha256
    event = first.quality_event()
    assert event == second.quality_event()
    for value in (*[getattr(first, f.name) for f in fields(first)], *vars_of(event)):
        assert "SECRET" not in str(value)
    assert event.event_type == "archive_parse_rejected"
    assert event.table == "raw.binance_spot_archives"
    assert event.revision_ids == ("archive-rev-1",)
    assert (event.event_start, event.event_end) == (
        day_start(MS_DAY),
        day_start(MS_DAY) + timedelta(days=1),
    )
    assert event.detail.startswith("invalid_decimal at line 2 column price")
    assert len(event.detail) <= 240
    assert event.event_id.startswith("binance.spot.archive.parser.rejection.")


def test_distinct_rejections_have_distinct_event_ids() -> None:
    rows = agg_rows(MS_DAY, 3)
    bad_line_2 = rows.copy()
    bad_line_2[1] = bad_line_2[1].replace("True", "true", 1)
    bad_line_3 = rows.copy()
    bad_line_3[2] = bad_line_3[2].replace(",True", ",true")
    first = _rejected(AGG, MS_DAY, bad_line_2).quality_event()
    second = _rejected(AGG, MS_DAY, bad_line_3).quality_event()
    assert first.event_id != second.event_id


def vars_of(event: object) -> list[object]:
    return [getattr(event, f.name) for f in fields(event)]  # type: ignore[arg-type]

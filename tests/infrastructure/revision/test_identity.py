"""The frozen identity rule (D2): golden hash, stability, block layout, path binding."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from core.domain.base import canonical_json
from infrastructure.parser.binance_archive import _MAX_MEMBER_BYTES
from infrastructure.revision import identity
from infrastructure.revision.identity import (
    ARRIVAL_SEQ_STRIDE,
    IDENTITY_HASH,
    IDENTITY_RULE_ID,
    IDENTITY_RULE_VERSION,
    ArrivalSeqOverflow,
    IdentityViolation,
)

#: Golden hash of ``IDENTITY_SPEC``. Any rule change must change it *and* the rule version.
GOLDEN_IDENTITY_HASH = "fc5f6f082554243c5ead89d389dc862f9c5b2b38d97a140bc8afdcd47b9b40aa"

DAY = date(2025, 1, 1)
SHA_A = "a" * 64
SHA_B = "b" * 64


def _agg_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "agg_trade_id": 500,
        "price": Decimal("92792.050000000000000000"),
        "quantity": Decimal("0.001500000000000000"),
        "first_trade_id": 1000,
        "last_trade_id": 1002,
        "timestamp_raw": 1735689600000000,
        "is_buyer_maker": False,
        "is_best_match": True,
    }
    row.update(overrides)
    return row


def _kline_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "open_time_raw": 1735689600000000,
        "open": Decimal("1.000000000000000000"),
        "high": Decimal("2.000000000000000000"),
        "low": Decimal("0.500000000000000000"),
        "close": Decimal("1.500000000000000000"),
        "volume": Decimal("10.000000000000000000"),
        "close_time_raw": 1735689659999999,
        "quote_asset_volume": Decimal("15.000000000000000000"),
        "number_of_trades": 7,
        "taker_buy_base_asset_volume": Decimal("4.000000000000000000"),
        "taker_buy_quote_asset_volume": Decimal("6.000000000000000000"),
        "ignore_raw": "0",
    }
    row.update(overrides)
    return row


def test_identity_hash_is_derived_from_the_full_spec() -> None:
    assert IDENTITY_HASH == GOLDEN_IDENTITY_HASH
    assert (
        hashlib.sha256(canonical_json(identity.IDENTITY_SPEC).encode("utf-8")).hexdigest()
        == IDENTITY_HASH
    )
    assert identity.IDENTITY_SPEC["rule"] == IDENTITY_RULE_ID
    assert identity.IDENTITY_SPEC["version"] == IDENTITY_RULE_VERSION


def test_spec_changes_change_the_hash() -> None:
    mutated = json.loads(canonical_json(identity.IDENTITY_SPEC))
    mutated["arrival_seq"]["stride"] = ARRIVAL_SEQ_STRIDE * 2
    assert hashlib.sha256(canonical_json(mutated).encode("utf-8")).hexdigest() != IDENTITY_HASH


def test_observation_keys_are_frozen_shapes() -> None:
    assert (
        identity.archive_observation_key("agg_trades", "BTCUSDT", DAY)
        == "binance:spot:archive:data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2025-01-01.zip"
    )
    assert (
        identity.archive_observation_key("klines_1m", "ETHUSDT", DAY)
        == "binance:spot:archive:data/spot/daily/klines/ETHUSDT/1m/ETHUSDT-1m-2025-01-01.zip"
    )
    assert identity.agg_trade_observation_key("BTCUSDT", 42) == "binance:spot:agg_trade:BTCUSDT:42"
    assert (
        identity.kline_1m_observation_key("BTCUSDT", datetime(2025, 1, 1, tzinfo=UTC))
        == "binance:spot:kline:BTCUSDT:1m:1735689600000000"
    )


def test_archive_observation_key_ignores_the_checksum() -> None:
    """A replacement file is a new revision of the *same* observation."""
    assert identity.archive_observation_key(
        "agg_trades", "BTCUSDT", DAY
    ) == identity.archive_observation_key("agg_trades", "BTCUSDT", DAY)
    assert SHA_A not in identity.archive_observation_key("agg_trades", "BTCUSDT", DAY)


def test_object_key_is_content_addressed_and_keeps_the_official_basename() -> None:
    same = identity.archive_object_key("agg_trades", "BTCUSDT", DAY, SHA_A)
    again = identity.archive_object_key("agg_trades", "BTCUSDT", DAY, SHA_A)
    other = identity.archive_object_key("agg_trades", "BTCUSDT", DAY, SHA_B)
    assert same == again != other
    assert same.rsplit("/", 1)[-1] == "BTCUSDT-aggTrades-2025-01-01.zip"
    assert same.startswith(f"raw/binance/spot/archive/revisions/{SHA_A}/")
    assert identity.archive_relative_path("agg_trades", "BTCUSDT", DAY).endswith(
        same.rsplit("/", 1)[-1]
    )


@pytest.mark.parametrize(
    "bad",
    ["A" * 64, "g" * 64, "a" * 63, "", "../../etc/passwd", "a" * 64 + "/x"],
)
def test_object_key_refuses_non_canonical_digests(bad: str) -> None:
    with pytest.raises(IdentityViolation):
        identity.archive_object_key("agg_trades", "BTCUSDT", DAY, bad)


@pytest.mark.parametrize("symbol", ["btcusdt", "BTC/USDT", "BTC USDT", "", "BTC..USDT", "x" * 40])
def test_symbols_are_validated(symbol: str) -> None:
    with pytest.raises(IdentityViolation):
        identity.archive_relative_path("agg_trades", symbol, DAY)


def test_revision_id_is_stable_and_independent_of_time_and_order() -> None:
    key = identity.archive_observation_key("agg_trades", "BTCUSDT", DAY)
    source = identity.archive_source_identity()
    first = identity.revision_id(key, source, SHA_A)
    second = identity.revision_id(key, source, SHA_A)
    assert first == second
    assert first.startswith("rev1-") and len(first) == 5 + 64
    assert identity.revision_id(key, source, SHA_B) != first
    assert identity.revision_id(key + "x", source, SHA_A) != first
    assert identity.revision_id(key, source + "x", SHA_A) != first


def test_row_source_identity_binds_the_archive_revision() -> None:
    bound = identity.row_source_identity("rev1-" + "c" * 64)
    assert bound.startswith("binance.public.spot.archive@1.0.0:")
    assert bound.endswith("rev1-" + "c" * 64)
    with pytest.raises(IdentityViolation):
        identity.row_source_identity("")


def test_row_payload_hashes_cover_native_fields_only() -> None:
    base = identity.agg_trade_payload_hash("BTCUSDT", "microsecond", _agg_row())
    assert base == identity.agg_trade_payload_hash("BTCUSDT", "microsecond", _agg_row())
    # Every native field is part of the hash.
    for column, value in (
        ("agg_trade_id", 501),
        ("price", Decimal("92792.060000000000000000")),
        ("quantity", Decimal("0.001600000000000000")),
        ("first_trade_id", 1001),
        ("last_trade_id", 1003),
        ("timestamp_raw", 1735689600000001),
        ("is_buyer_maker", True),
        ("is_best_match", False),
    ):
        assert (
            identity.agg_trade_payload_hash("BTCUSDT", "microsecond", _agg_row(**{column: value}))
            != base
        )
    # Symbol and unit are part of the identity; the line number is not a field at all.
    assert identity.agg_trade_payload_hash("ETHUSDT", "microsecond", _agg_row()) != base
    assert identity.agg_trade_payload_hash("BTCUSDT", "millisecond", _agg_row()) != base
    assert (
        identity.agg_trade_payload_hash("BTCUSDT", "microsecond", _agg_row(archive_line_number=99))
        == base
    )


def test_kline_payload_hash_covers_native_fields() -> None:
    base = identity.kline_1m_payload_hash("BTCUSDT", "microsecond", _kline_row())
    for column, value in (
        ("open_time_raw", 1735689660000000),
        ("close", Decimal("1.600000000000000000")),
        ("number_of_trades", 8),
        ("ignore_raw", "1"),
    ):
        assert (
            identity.kline_1m_payload_hash("BTCUSDT", "microsecond", _kline_row(**{column: value}))
            != base
        )


def test_decimal_scale_is_part_of_the_canonical_text() -> None:
    """``decimal(38, 18)`` values hash by their fixed-point text, never through a float."""
    scaled = identity.agg_trade_payload_hash(
        "BTCUSDT", "microsecond", _agg_row(price=Decimal("1.000000000000000000"))
    )
    unscaled = identity.agg_trade_payload_hash(
        "BTCUSDT", "microsecond", _agg_row(price=Decimal("1"))
    )
    assert scaled != unscaled  # the Arrow column always delivers the scaled value


@pytest.mark.parametrize(
    "row", [_agg_row(price=1.0), _agg_row(agg_trade_id=True), _agg_row(is_buyer_maker=1)]
)
def test_payload_hash_refuses_wrong_python_types(row: dict[str, Any]) -> None:
    with pytest.raises(IdentityViolation):
        identity.agg_trade_payload_hash("BTCUSDT", "microsecond", row)


def test_block_stride_strictly_covers_the_parser_limit() -> None:
    """A CSV line needs at least one byte plus its LF, so lines <= member bytes < stride."""
    assert ARRIVAL_SEQ_STRIDE > _MAX_MEMBER_BYTES
    assert ARRIVAL_SEQ_STRIDE & (ARRIVAL_SEQ_STRIDE - 1) == 0  # a power of two


def test_block_bases_never_repeat_and_allow_gaps() -> None:
    assert identity.arrival_block_base(None) == 0
    assert identity.arrival_block_base(0) == ARRIVAL_SEQ_STRIDE
    assert identity.arrival_block_base(ARRIVAL_SEQ_STRIDE) == 2 * ARRIVAL_SEQ_STRIDE
    # A base derived from a row sequence inside a block still lands on the next block.
    assert identity.arrival_block_base(ARRIVAL_SEQ_STRIDE + 17) == 2 * ARRIVAL_SEQ_STRIDE


def test_row_sequences_stay_inside_their_block() -> None:
    base = 3 * ARRIVAL_SEQ_STRIDE
    assert identity.row_arrival_seq(base, 1) == base + 1
    assert identity.row_arrival_seq(base, ARRIVAL_SEQ_STRIDE - 1) == base + ARRIVAL_SEQ_STRIDE - 1
    with pytest.raises(ArrivalSeqOverflow):
        identity.row_arrival_seq(base, ARRIVAL_SEQ_STRIDE)
    for bad in (0, -1):
        with pytest.raises(IdentityViolation):
            identity.row_arrival_seq(base, bad)
    with pytest.raises(IdentityViolation):
        identity.row_arrival_seq(base + 1, 1)


def test_int64_overflow_fails_closed() -> None:
    with pytest.raises(ArrivalSeqOverflow):
        identity.arrival_block_base(identity.MAX_ARRIVAL_SEQ - 1)


def test_interval_keys_require_utc() -> None:
    with pytest.raises(IdentityViolation):
        identity.kline_1m_observation_key("BTCUSDT", datetime(2025, 1, 1))  # noqa: DTZ001
    with pytest.raises(IdentityViolation):
        identity.kline_1m_observation_key(
            "BTCUSDT", datetime(2025, 1, 1, tzinfo=timezone(timedelta(hours=8)))
        )

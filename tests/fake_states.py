"""Shared fixtures for Phase 2 state tests (ADR-0035): feature-value inputs and 1m bars.

Test-only numbers: the cut points, windows and thresholds below are fixture parameters of test
specs, not validation thresholds and not recommended model settings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.contracts.feature import FeatureObservation
from core.contracts.state import StateInput
from core.contracts.universe import SelectedRevisionLineage
from core.domain.base import FrozenMapping, Kind, Ref, content_hash

T0 = datetime(2024, 3, 1, 12, tzinfo=UTC)
MINUTE = timedelta(minutes=1)

VOL_FEATURE = Ref(kind=Kind.FEATURE, name="bar_realized_vol_5", version="1.0.0")
VOLUME_FEATURE = Ref(kind=Kind.FEATURE, name="bar_volume_sum_5", version="1.0.0")
RETURN_FEATURE = Ref(kind=Kind.FEATURE, name="bar_log_return", version="1.0.0")
SOURCE = content_hash({"fixture": "feature-result"})

#: Test-only spec parameters (fixture numbers, not validation thresholds).
TEST_CUTS = ("0.3333", "0.6667")
TEST_MIN_HISTORY = 4
TEST_WINDOW = 10 * MINUTE
TEST_SEED = 7
TEST_TREND_WINDOW = 5
TEST_TREND_THRESHOLD = "0.5"


def at(minute: int) -> datetime:
    return T0 + minute * MINUTE


def value_input(feature: Ref, minute: int, value: Decimal | None) -> StateInput:
    return StateInput(
        feature=feature, evaluation_time=at(minute), value=value, source_result_hash=SOURCE
    )


def level_inputs(feature: Ref, count: int = 31, *, missing: int = 2) -> tuple[StateInput, ...]:
    """A deterministic, non-monotone positive series; the first ``missing`` values are ``None``."""
    return tuple(
        value_input(feature, i, None if i < missing else Decimal((i * 7) % 11 + 1) / 100)
        for i in range(count)
    )


def return_inputs(count: int = 31) -> tuple[StateInput, ...]:
    """Log returns: an up run, a down run, then chop; the first value is ``None``."""
    pattern: list[str | None] = [None, *["0.01"] * 9, *["-0.02"] * 10, *["0.01", "-0.01"] * 6]
    return tuple(
        value_input(RETURN_FEATURE, i, None if (text := pattern[i]) is None else Decimal(text))
        for i in range(count)
    )


def negate(item: StateInput) -> StateInput:
    """Reverses the ranking (quantile buckets ignore monotone maps; negation is not one)."""
    if item.value is None:
        return item
    return item.model_copy(update={"value": -Decimal(item.value)})


def bar(minute: int, close: str, *, volume: str = "10") -> FeatureObservation:
    """The 1m bar starting at ``T0 + minute``, available when it closes."""
    start = at(minute)
    values: dict[str, Decimal | int | bool | str] = {
        "symbol": "BTC-USDT",
        "close": Decimal(close),
        "volume": Decimal(volume),
    }
    return FeatureObservation(
        observation_key=f"bar:BTC-USDT:{minute}",
        event_time=start,
        event_end_time=start + MINUTE,
        available_time=start + MINUTE,
        knowledge_time=start + MINUTE,
        values=FrozenMapping(values),
        lineage=SelectedRevisionLineage(
            canonical_table="canonical.bars_1m",
            canonical_revision_id=f"crev-{minute}",
            raw_table="raw.binance_spot_klines_1m",
            raw_revision_id=f"raw-{minute}",
            source_table="raw.binance_spot_archives",
            source_revision_id="archive-1",
        ),
    )


def bars(count: int) -> tuple[FeatureObservation, ...]:
    """``count`` contiguous bars with a deterministic, non-monotone close and volume path."""
    return tuple(
        bar(i, str(100 + (i * 13) % 17 - (i // 10)), volume=str(1 + (i * 5) % 9))
        for i in range(count)
    )

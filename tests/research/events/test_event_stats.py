"""Phase 3 event statistics (research/events/stats.py): hand-checked, deterministic."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from core.contracts.event import Event
from core.domain.base import Kind, Ref, content_hash
from research.events.stats import co_occurrence, event_frequency, lead_lag, overlap_diagnostics
from tests.fake_events import MINUTE, T0, X_INPUTS

A = Ref(kind=Kind.EVENT, name="a", version="1.0.0")
B = Ref(kind=Kind.EVENT, name="b", version="1.0.0")


def _events(ref: Ref, minutes: list[int]) -> tuple[Event, ...]:
    return tuple(
        Event.build(
            event=ref,
            spec_hash=content_hash({"spec": ref.name}),
            event_time=T0 + minute * MINUTE,
            attributes={"n": index},
            inputs=X_INPUTS[:1],
        )
        for index, minute in enumerate(minutes)
    )


EVENTS_A = _events(A, [0, 10, 20, 21, 50])
EVENTS_B = _events(B, [1, 12, 40])
END = T0 + 60 * MINUTE


def test_frequency() -> None:
    result = event_frequency(EVENTS_A, start=T0, end=END, bucket=20 * MINUTE)
    assert result.count == 5
    assert result.per_day == Decimal(5 * 24)
    assert result.buckets == ((T0, 2), (T0 + 20 * MINUTE, 2), (T0 + 40 * MINUTE, 1))
    with pytest.raises(ValueError):
        event_frequency(EVENTS_A, start=END, end=T0, bucket=MINUTE)


def test_co_occurrence() -> None:
    result = co_occurrence(EVENTS_A, EVENTS_B, window=2 * MINUTE, start=T0, end=END)
    assert (result.n_a, result.n_b) == (5, 3)
    assert result.a_with_b == 2  # 0~1, 10~12
    assert result.b_with_a == 2  # 1~0, 12~10
    # expected = 5 * min(1, 3 * 4 min / 60 min) = 1
    assert result.expected_a_with_b == Decimal(1)
    assert result.lift == Decimal(2)
    empty = co_occurrence((), EVENTS_B, window=MINUTE, start=T0, end=END)
    assert empty.lift is None  # undefined, not 0


def test_lead_lag() -> None:
    result = lead_lag(EVENTS_A, EVENTS_B, max_lag=5 * MINUTE, bin=5 * MINUTE)
    # pairs within 5 min: (0,1)=+1, (10,12)=+2 -> A leads twice.
    assert (result.pairs, result.a_leads, result.b_leads, result.simultaneous) == (2, 2, 0, 0)
    assert result.bins == ((-5 * MINUTE, 0), (timedelta(0), 2), (5 * MINUTE, 0))


def test_overlap_diagnostics() -> None:
    result = overlap_diagnostics(EVENTS_A, horizon=5 * MINUTE)
    assert result.n == 5
    assert result.overlapping_neighbours == 1  # 20 -> 21
    assert result.overlap_fraction == Decimal("0.25")
    assert result.independent_count == 4
    assert result.mean_gap_seconds == Decimal(50 * 60) / 4
    assert result.dispersion is not None and result.dispersion > 0
    single = overlap_diagnostics(EVENTS_A[:1], horizon=MINUTE)
    assert (single.overlap_fraction, single.mean_gap_seconds, single.dispersion) == (
        None,
        None,
        None,
    )


def test_statistics_are_order_independent() -> None:
    forward = co_occurrence(EVENTS_A, EVENTS_B, window=MINUTE, start=T0, end=END)
    backward = co_occurrence(
        tuple(reversed(EVENTS_A)), tuple(reversed(EVENTS_B)), window=MINUTE, start=T0, end=END
    )
    assert forward == backward
    assert isinstance(T0, datetime)


# -- serialisation ---------------------------------------------------------------------------


def _all_statistics() -> tuple[object, ...]:
    return (
        event_frequency(EVENTS_A, start=T0, end=END, bucket=20 * MINUTE),
        co_occurrence(EVENTS_A, EVENTS_B, window=2 * MINUTE, start=T0, end=END),
        lead_lag(EVENTS_A, EVENTS_B, max_lag=5 * MINUTE, bin=MINUTE),
        overlap_diagnostics(EVENTS_A, horizon=5 * MINUTE),
    )


def test_every_statistic_has_a_deterministic_json_payload() -> None:
    import json

    from research.events.stats import statistic_payload

    payloads = [statistic_payload(stat) for stat in _all_statistics()]  # type: ignore[arg-type]
    assert [p["kind"] for p in payloads] == [
        "event_frequency", "co_occurrence", "lead_lag", "overlap_diagnostics",
    ]  # fmt: skip
    assert json.loads(json.dumps(payloads)) == payloads  # JSON-ready, no floats
    frequency = payloads[0]
    assert frequency["per_day"] == "120" and frequency["start"] == T0.isoformat()
    assert frequency["buckets"][0] == [T0.isoformat(), 2]
    assert payloads[2]["max_lag"] == 5 * 60 * 1_000_000  # microseconds
    assert [statistic_payload(s) for s in _all_statistics()] == payloads  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        statistic_payload(object())  # type: ignore[arg-type]


def test_a_stats_report_binds_its_event_runs() -> None:
    from research.events.stats import EventStatsReport

    runs = (content_hash({"run": 1}), content_hash({"run": 2}))
    stats = _all_statistics()
    report = EventStatsReport(runs, stats)  # type: ignore[arg-type]
    assert report.to_payload()["source_result_hashes"] == sorted(runs)
    assert EventStatsReport(runs[::-1], stats).report_hash == report.report_hash  # type: ignore[arg-type]
    assert EventStatsReport(runs[:1], stats).report_hash != report.report_hash  # type: ignore[arg-type]
    for bad in ((), ("nope",), (runs[0], runs[0])):
        with pytest.raises(ValueError):
            EventStatsReport(bad, stats)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        EventStatsReport(runs, ())

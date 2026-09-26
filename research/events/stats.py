"""Event statistics (Phase 3 research; roadmap "事件共现与时序统计").

Descriptive statistics over event tables (``core.contracts.event.Event`` sequences): frequency,
co-occurrence, lead-lag and overlap / independence diagnostics. They describe; they do not decide.
No function here carries a validation threshold, and none of these numbers is a Validation Profile
input (constitution C-T2: overlapping events must be discounted — ``OverlapDiagnostics.
independent_count`` is a descriptive count to support that, not the Profile's rule).

Only ``event_time`` (the observable time) is used, so the statistics are as point-in-time as the
tables they are given. Exact where possible: times in whole microseconds, counts as ``int``, ratios
as ``Decimal`` in a fixed 28-digit context (deterministic, platform independent). A ratio whose
denominator is zero is ``None`` (undefined), never 0.

Serialisation (2026-09-26): ``statistic_payload`` turns any of the four results into
deterministic JSON-ready data (times as ISO-8601 UTC, durations as integer microseconds,
``Decimal`` as exact text, ``None`` kept) tagged with its ``kind``; ``EventStatsReport`` binds a
set of statistics to the ``EventResult.result_hash`` values of the event runs they were computed
from, and ``report_hash`` covers the whole payload — so a number in a report traces to its runs.

Research code (``research/``): never imported by production packages (ADR-0005).
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from itertools import pairwise
from typing import Any, Final

from core.contracts.event import Event
from core.domain.base import content_hash

__all__ = [
    "CoOccurrence",
    "EventFrequency",
    "EventStatsReport",
    "LeadLag",
    "OverlapDiagnostics",
    "co_occurrence",
    "event_frequency",
    "lead_lag",
    "overlap_diagnostics",
    "statistic_payload",
]

_US: Final = timedelta(microseconds=1)
_DAY_US: Final = 86_400_000_000
_CONTEXT: Final = Context(prec=28, rounding=ROUND_HALF_EVEN)


def _us(delta: timedelta) -> int:
    return delta // _US


def _ratio(numerator: Decimal | int, denominator: Decimal | int) -> Decimal | None:
    if denominator == 0:
        return None
    with localcontext(_CONTEXT):
        return Decimal(numerator) / Decimal(denominator)


def _times(events: Sequence[Event]) -> list[datetime]:
    return sorted(item.event_time for item in events)


def _require_span(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start / end must be timezone-aware UTC times")
    if end <= start:
        raise ValueError("end must be later than start")


# ======================================================================================
# Frequency
# ======================================================================================


@dataclass(frozen=True, slots=True)
class EventFrequency:
    """Counts of events in ``[start, end)`` and per ``bucket`` (bucket start → count)."""

    start: datetime
    end: datetime
    count: int
    per_day: Decimal
    buckets: tuple[tuple[datetime, int], ...]


def event_frequency(
    events: Sequence[Event], *, start: datetime, end: datetime, bucket: timedelta
) -> EventFrequency:
    """Event count, rate per day and per-bucket counts over ``[start, end)``."""
    _require_span(start, end)
    if bucket <= timedelta(0):
        raise ValueError("bucket must be positive")
    times = [at for at in _times(events) if start <= at < end]
    counts: dict[int, int] = {}
    for at in times:
        index = (at - start) // bucket
        counts[index] = counts.get(index, 0) + 1
    last = (end - start - _US) // bucket
    buckets = tuple((start + index * bucket, counts.get(index, 0)) for index in range(last + 1))
    per_day = _ratio(len(times) * _DAY_US, _us(end - start))
    return EventFrequency(
        start=start,
        end=end,
        count=len(times),
        per_day=per_day if per_day is not None else Decimal(0),
        buckets=buckets,
    )


# ======================================================================================
# Co-occurrence
# ======================================================================================


def _near(times: list[datetime], at: datetime, window: timedelta) -> int:
    """How many of ``times`` (sorted) lie in ``[at - window, at + window]``."""
    return bisect_right(times, at + window) - bisect_left(times, at - window)


@dataclass(frozen=True, slots=True)
class CoOccurrence:
    """How often A and B events fall within ``window`` of each other in ``[start, end)``.

    ``expected_a_with_b`` is the count expected if B were a homogeneous Poisson process with the
    observed rate, independent of A: ``n_a * (1 - exp(-rate_b * 2 * window))``, approximated to
    first order as ``n_a * min(1, rate_b * 2 * window)``. ``lift`` = observed / expected.
    """

    window: timedelta
    n_a: int
    n_b: int
    a_with_b: int
    b_with_a: int
    expected_a_with_b: Decimal
    lift: Decimal | None


def co_occurrence(
    a: Sequence[Event],
    b: Sequence[Event],
    *,
    window: timedelta,
    start: datetime,
    end: datetime,
) -> CoOccurrence:
    """Co-occurrence counts of two event tables within ``window`` (either order)."""
    _require_span(start, end)
    if window < timedelta(0):
        raise ValueError("window must not be negative")
    times_a = [at for at in _times(a) if start <= at < end]
    times_b = [at for at in _times(b) if start <= at < end]
    a_with_b = sum(1 for at in times_a if _near(times_b, at, window))
    b_with_a = sum(1 for at in times_b if _near(times_a, at, window))
    with localcontext(_CONTEXT):
        share = Decimal(len(times_b)) * Decimal(2 * _us(window)) / Decimal(_us(end - start))
        expected = Decimal(len(times_a)) * min(Decimal(1), share)
    return CoOccurrence(
        window=window,
        n_a=len(times_a),
        n_b=len(times_b),
        a_with_b=a_with_b,
        b_with_a=b_with_a,
        expected_a_with_b=expected,
        lift=_ratio(a_with_b, expected),
    )


# ======================================================================================
# Lead-lag
# ======================================================================================


@dataclass(frozen=True, slots=True)
class LeadLag:
    """Pairwise ``B.event_time - A.event_time`` within ``max_lag``, binned.

    ``bins``: (lower edge, count) for ``[edge, edge + bin)``, ascending from ``-max_lag``;
    ``a_leads`` / ``b_leads`` / ``simultaneous`` count pairs with a positive / negative / zero lag.
    """

    max_lag: timedelta
    bin: timedelta
    pairs: int
    a_leads: int
    b_leads: int
    simultaneous: int
    bins: tuple[tuple[timedelta, int], ...]


def lead_lag(
    a: Sequence[Event], b: Sequence[Event], *, max_lag: timedelta, bin: timedelta
) -> LeadLag:
    """Histogram of ``B - A`` lags over all pairs with ``|lag| <= max_lag``."""
    if max_lag <= timedelta(0) or bin <= timedelta(0):
        raise ValueError("max_lag and bin must be positive")
    times_b = _times(b)
    lags: list[timedelta] = []
    for at in _times(a):
        low = bisect_left(times_b, at - max_lag)
        high = bisect_right(times_b, at + max_lag)
        lags.extend(times_b[index] - at for index in range(low, high))
    count = (2 * max_lag) // bin + 1
    edges = [-max_lag + index * bin for index in range(count)]
    counts = [0] * count
    for lag in lags:
        counts[min((lag + max_lag) // bin, count - 1)] += 1
    return LeadLag(
        max_lag=max_lag,
        bin=bin,
        pairs=len(lags),
        a_leads=sum(1 for lag in lags if lag > timedelta(0)),
        b_leads=sum(1 for lag in lags if lag < timedelta(0)),
        simultaneous=sum(1 for lag in lags if lag == timedelta(0)),
        bins=tuple(zip(edges, counts, strict=True)),
    )


# ======================================================================================
# Overlap / independence
# ======================================================================================


@dataclass(frozen=True, slots=True)
class OverlapDiagnostics:
    """Whether events of one table overlap over a ``horizon`` (e.g. an outcome horizon).

    - ``overlapping_neighbours``: consecutive pairs closer than ``horizon``;
      ``overlap_fraction`` = that / (n - 1);
    - ``independent_count``: the greedy count of events whose ``[t, t + horizon)`` windows do not
      overlap a previously kept one (a descriptive effective sample size);
    - ``mean_gap_seconds`` and ``dispersion`` (variance / mean² of the inter-arrival gaps; about 1
      for a Poisson process, above 1 for clustered events, below 1 for regular ones).
    """

    horizon: timedelta
    n: int
    overlapping_neighbours: int
    overlap_fraction: Decimal | None
    independent_count: int
    mean_gap_seconds: Decimal | None
    dispersion: Decimal | None


def overlap_diagnostics(events: Sequence[Event], *, horizon: timedelta) -> OverlapDiagnostics:
    """Overlap and clustering diagnostics of one event table over ``horizon``."""
    if horizon <= timedelta(0):
        raise ValueError("horizon must be positive")
    times = _times(events)
    gaps = [_us(later - earlier) for earlier, later in pairwise(times)]
    overlapping = sum(1 for gap in gaps if gap < _us(horizon))
    independent = 0
    free_from: datetime | None = None
    for at in times:
        if free_from is None or at >= free_from:
            independent += 1
            free_from = at + horizon
    mean: Decimal | None = None
    dispersion: Decimal | None = None
    if gaps:
        with localcontext(_CONTEXT):
            mean_us = Decimal(sum(gaps)) / len(gaps)
            variance = sum(((Decimal(gap) - mean_us) ** 2 for gap in gaps), Decimal(0)) / len(gaps)
            mean = mean_us.scaleb(-6)
            dispersion = variance / mean_us**2 if mean_us else None
    return OverlapDiagnostics(
        horizon=horizon,
        n=len(times),
        overlapping_neighbours=overlapping,
        overlap_fraction=_ratio(overlapping, len(gaps)),
        independent_count=independent,
        mean_gap_seconds=mean,
        dispersion=dispersion,
    )


# ======================================================================================
# Serialisation
# ======================================================================================

_KINDS: Final = {
    "EventFrequency": "event_frequency",
    "CoOccurrence": "co_occurrence",
    "LeadLag": "lead_lag",
    "OverlapDiagnostics": "overlap_diagnostics",
}
_HASH_RE: Final = re.compile(r"^[0-9a-f]{64}$")
Statistic = EventFrequency | CoOccurrence | LeadLag | OverlapDiagnostics


def _plain(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("statistic times must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, timedelta):
        return _us(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("statistic values must be finite")
        return str(value)
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, int | str):
        return value
    raise TypeError(f"cannot serialise {type(value).__name__}")


def statistic_payload(statistic: Statistic) -> dict[str, Any]:
    """Deterministic JSON-ready form of one statistic (see module docs)."""
    kind = _KINDS.get(type(statistic).__name__)
    if kind is None:
        raise TypeError(f"not an event statistic: {type(statistic).__name__}")
    body: dict[str, Any] = {"kind": kind}
    for item in fields(statistic):
        body[item.name] = _plain(getattr(statistic, item.name))
    return body


@dataclass(frozen=True)
class EventStatsReport:
    """Statistics bound to the event runs (``EventResult.result_hash``) they describe."""

    source_result_hashes: tuple[str, ...]
    statistics: tuple[Statistic, ...]
    report_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.source_result_hashes or not self.statistics:
            raise ValueError("a report binds at least one event run and one statistic")
        if any(not _HASH_RE.match(h) for h in self.source_result_hashes):
            raise ValueError("source_result_hashes must be content hashes")
        if len(set(self.source_result_hashes)) != len(self.source_result_hashes):
            raise ValueError("source_result_hashes must be distinct")
        object.__setattr__(self, "report_hash", content_hash(self._body()))

    def _body(self) -> dict[str, Any]:
        return {
            "kind": "event_statistics",
            "schema_version": "1.0.0",
            "status": "FRAMEWORK_IMPLEMENTED / NOT_VALIDATED",
            "note": "descriptive only; no validation threshold; not a Validation Profile input",
            "source_result_hashes": sorted(self.source_result_hashes),
            "statistics": [statistic_payload(item) for item in self.statistics],
        }

    def to_payload(self) -> dict[str, Any]:
        return {**self._body(), "report_hash": self.report_hash}

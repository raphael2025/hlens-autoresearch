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

Research code (``research/``): never imported by production packages (ADR-0005).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from itertools import pairwise
from typing import Final

from core.contracts.event import Event

__all__ = [
    "CoOccurrence",
    "EventFrequency",
    "LeadLag",
    "OverlapDiagnostics",
    "co_occurrence",
    "event_frequency",
    "lead_lag",
    "overlap_diagnostics",
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

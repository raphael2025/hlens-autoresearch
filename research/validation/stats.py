"""Minimal statistics for the G2 / G3 gates (Constitution C-T1 ~ C-T3).

- ``effective_sample_size``: overlapping holding intervals are not independent (C-T2); the count
  is the number of disjoint clusters of overlapping intervals (connected components of their
  union), never the number of bars. A long interval blocks everything nested inside it: ``[0, 10)``
  and ``[2, 3)`` are one effective sample, and so is ``[0, 10)``, ``[2, 3)``, ``[4, 5)`` (Phase 8
  fix, ADR-0041: the earlier ``min`` shrank the blocked region and counted nested samples twice);
- ``hac_t_test``: the mean with a Newey–West (Bartlett) standard error whose lag is the maximal
  overlap between samples (C-T3: autocorrelation aware), and a normal-approximation p-value;
- ``adjust_p_value``: family-wise multiple-testing adjustment (C-T1) by the Profile's
  ``multiple_testing_method``; only ``bonferroni`` and ``sidak`` are implemented, any other name is
  refused (no silent fallback to a laxer method).

Statistics run on ``float`` converted from exact ``Decimal`` inputs in a fixed order, so they are
deterministic on one platform. The normal approximation is a framework choice (ADR-0037 §5).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "HacTest",
    "UnsupportedMethod",
    "adjust_p_value",
    "effective_sample_size",
    "hac_t_test",
    "overlap_lag",
]


class UnsupportedMethod(ValueError):
    """The Profile names a statistical method this pipeline does not implement."""


def effective_sample_size(intervals: Sequence[tuple[datetime, datetime]]) -> int:
    """Number of disjoint clusters of overlapping ``[start, end)`` intervals.

    A new cluster starts only when an interval starts at or after the end of **every** earlier
    interval (the blocked region grows with ``max``, it never shrinks).
    """
    count = 0
    free_from: datetime | None = None
    for start, end in sorted(intervals):
        if free_from is None or start >= free_from:
            count += 1
            free_from = end
        else:
            free_from = max(free_from, end)
    return count


def overlap_lag(intervals: Sequence[tuple[datetime, datetime]]) -> int:
    """The largest number of later samples that start before an earlier one ends."""
    ordered = sorted(intervals)
    lag = 0
    for index, (_, end) in enumerate(ordered):
        later = 0
        for start, _ in ordered[index + 1 :]:
            if start >= end:
                break
            later += 1
        lag = max(lag, later)
    return lag


@dataclass(frozen=True)
class HacTest:
    n: int
    mean: float
    std_error: float
    t_stat: float
    p_two_sided: float
    p_greater: float


def _normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def hac_t_test(values: Sequence[float], lag: int) -> HacTest:
    """Mean, Newey–West standard error and normal p-values; needs at least two values."""
    n = len(values)
    if n < 2:
        raise ValueError("a t-test needs at least two values")
    if lag < 0:
        raise ValueError("lag must be >= 0")
    mean = math.fsum(values) / n
    centered = [value - mean for value in values]
    gamma0 = math.fsum(x * x for x in centered) / n
    long_run = gamma0
    for k in range(1, min(lag, n - 1) + 1):
        weight = 1.0 - k / (lag + 1)
        gamma_k = math.fsum(centered[i] * centered[i - k] for i in range(k, n)) / n
        long_run += 2.0 * weight * gamma_k
    std_error = math.sqrt(max(long_run, 0.0) / n)
    if std_error == 0.0:
        t_stat = 0.0 if mean == 0.0 else math.copysign(1e300, mean)
    else:
        t_stat = mean / std_error
    return HacTest(
        n=n,
        mean=mean,
        std_error=std_error,
        t_stat=t_stat,
        p_two_sided=min(1.0, 2.0 * _normal_sf(abs(t_stat))),
        p_greater=_normal_sf(t_stat),
    )


def adjust_p_value(p_value: float, method: str, trials: int) -> float:
    """Family-wise adjusted p-value over ``trials`` attempts (failed attempts included)."""
    if trials < 1:
        raise ValueError("trials must be >= 1")
    if not 0.0 <= p_value <= 1.0:
        raise ValueError("p_value must be in [0, 1]")
    name = method.strip().lower()
    if name == "bonferroni":
        return min(1.0, p_value * trials)
    if name == "sidak":
        return 1.0 - (1.0 - p_value) ** trials
    raise UnsupportedMethod(f"multiple_testing_method {method!r} is not implemented")

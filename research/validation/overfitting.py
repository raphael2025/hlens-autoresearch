"""Selection-overfitting statistics: PBO via CSCV and the Deflated Sharpe ratio (Phase 8, ADR-0041).

Constitution C-T1 ("过拟合概率") and C-R1. Neither function holds a threshold: the verdict is made
by ``robustness.overfitting_check`` against ``significance.overfitting_threshold``.

- **PBO via CSCV** (Bailey, Borwein, López de Prado & Zhu, *The Probability of Backtest
  Overfitting*, 2017): the ``T x N`` matrix of per-period returns of ``N`` trials is cut into
  ``S`` contiguous blocks (``S`` even, an explicit method parameter — no default). For each of the
  ``C(S, S/2)`` ways to pick half of the blocks as the in-sample set, the trial with the best
  in-sample Sharpe ratio is ranked among all trials on the complementary blocks; with relative
  rank ``w = rank / (N + 1)`` and logit ``l = ln(w / (1 - w))``, PBO is the share of splits with
  ``l <= 0`` (the in-sample winner is at or below the out-of-sample median). Ties rank by average.
  Per-block sums and sums of squares make each split ``O(S * N)``.
  **Purge / embargo between blocks** (ADR-0041 review fix, 2026-09-25): the plain CSCV lets a
  holding period or serially correlated returns straddle an in-sample / out-of-sample block
  boundary. Every split therefore drops the in-sample periods that lie strictly within
  ``embargo`` before the start (purge) or after the end (embargo) of any out-of-sample block,
  exactly as ``splits.purge_and_embargo`` does for spans. The embargo is a required argument;
  ``robustness.overfitting_check`` passes the Profile's ``data_split.embargo`` (the same one the
  walk-forward splits use; C-L5 requires it to cover the longest Outcome horizon). An embargo of
  zero reproduces the unpurged CSCV. If a split keeps fewer than two in-sample periods the PBO is
  refused (``CscvPurgeTooWide``), never computed on a degenerate sample.
- **Deflated Sharpe ratio** (Bailey & López de Prado, *The Deflated Sharpe Ratio*, 2014): the
  probability that the true Sharpe ratio exceeds the expected maximum of ``N`` unskilled trials,
  ``SR0 = sqrt(V[SR]) * ((1 - g) * z(1 - 1/N) + g * z(1 - 1/(N e)))`` (``g`` Euler–Mascheroni),
  corrected for the sample's skewness and kurtosis. ``N`` is the family's cumulative trial count
  (failed attempts included, C-T1); ``V[SR]`` is estimated from the evaluated trials.

Sharpe ratios are per period (not annualized): only comparisons and ranks are used. Everything
runs on ``float`` in a fixed order (deterministic on one platform; ADR-0037 §5).
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import combinations
from statistics import NormalDist
from typing import Final

__all__ = [
    "CscvPurgeTooWide",
    "DeflatedSharpe",
    "Pbo",
    "deflated_sharpe_ratio",
    "expected_max_sharpe",
    "probability_of_backtest_overfitting",
    "sharpe_ratio",
]

EULER_GAMMA: Final = 0.5772156649015329
#: Stand-in for an infinite Sharpe ratio (a constant non-zero return): keeps every value finite.
_UNBOUNDED: Final = 1e12
_NORMAL: Final = NormalDist()


def _sr(n: float, total: float, squares: float) -> float:
    if n < 2:
        return 0.0
    mean = total / n
    variance = max(0.0, (squares - n * mean * mean) / (n - 1))
    if variance == 0.0:
        return 0.0 if mean == 0.0 else math.copysign(_UNBOUNDED, mean)
    return mean / math.sqrt(variance)


def sharpe_ratio(returns: Sequence[float]) -> float:
    """Per-period Sharpe ratio ``mean / sample std`` (0 for fewer than two values)."""
    return _sr(float(len(returns)), math.fsum(returns), math.fsum(x * x for x in returns))


@dataclass(frozen=True)
class Pbo:
    pbo: float
    splits: int
    partitions: int
    trials: int
    periods: int
    mean_logit: float
    #: Share of splits in which the in-sample winner lost money out of sample (reported only).
    oos_loss_share: float
    #: The purge / embargo applied around every out-of-sample block.
    embargo: timedelta
    #: The most in-sample periods any one split dropped for the purge / embargo.
    purged_in_sample_periods_max: int


class CscvPurgeTooWide(ValueError):
    """The purge / embargo leaves a CSCV split with fewer than two in-sample periods."""


Segment = tuple[int, int]


def _subtract(block: Segment, cuts: Sequence[Segment]) -> list[Segment]:
    """``[start, end)`` minus the (possibly overlapping) index ranges ``cuts``, in order."""
    pieces = [block]
    for cut_start, cut_end in cuts:
        if cut_start >= cut_end:
            continue
        kept: list[Segment] = []
        for start, end in pieces:
            if cut_end <= start or cut_start >= end:
                kept.append((start, end))
                continue
            if start < cut_start:
                kept.append((start, cut_start))
            if cut_end < end:
                kept.append((cut_end, end))
        pieces = kept
    return pieces


def _purge_cuts(
    times: Sequence[datetime], bounds: Sequence[Segment], rest: Sequence[int], embargo: timedelta
) -> list[Segment]:
    """Index ranges strictly within ``embargo`` before / after every out-of-sample block."""
    cuts: list[Segment] = []
    for block in rest:
        start, end = bounds[block]
        first, last = times[start], times[end - 1]
        cuts.append((bisect_right(times, first - embargo), bisect_left(times, first)))
        cuts.append((bisect_right(times, last), bisect_left(times, last + embargo)))
    return cuts


def _average_rank(values: Sequence[float], index: int) -> float:
    target = values[index]
    below = sum(1 for v in values if v < target)
    equal = sum(1 for v in values if v == target)
    return below + (equal + 1) / 2.0


def probability_of_backtest_overfitting(
    matrix: Sequence[Sequence[float]],
    partitions: int,
    *,
    times: Sequence[datetime],
    embargo: timedelta,
) -> Pbo:
    """PBO of a family: ``matrix[trial][period]`` per-period returns on one shared grid.

    ``times[i]`` is the time of period ``i`` (strictly ascending); in-sample periods within
    ``embargo`` of an out-of-sample block are purged in every split (see module docs).
    """
    trials = len(matrix)
    if trials < 2:
        raise ValueError("PBO needs at least two trials")
    periods = len(matrix[0])
    if any(len(row) != periods for row in matrix):
        raise ValueError("every trial needs the same number of periods")
    if isinstance(partitions, bool) or partitions < 2 or partitions % 2:
        raise ValueError("CSCV partitions must be an even int >= 2")
    if periods < 2 * partitions:
        raise ValueError("CSCV needs at least two periods per partition")
    if len(times) != periods or any(a >= b for a, b in zip(times, times[1:], strict=False)):
        raise ValueError("CSCV needs one strictly ascending time per period")
    if embargo < timedelta(0):
        raise ValueError("embargo must be >= 0")
    size, extra = divmod(periods, partitions)
    bounds: list[tuple[int, int]] = []
    begin = 0
    for block in range(partitions):
        end = begin + size + (1 if block < extra else 0)
        bounds.append((begin, end))
        begin = end
    cache: dict[Segment, list[tuple[float, float, float]]] = {}

    def segment_stats(segment: Segment) -> list[tuple[float, float, float]]:
        if segment not in cache:
            start, end = segment
            cache[segment] = [
                (
                    float(end - start),
                    math.fsum(row[start:end]),
                    math.fsum(x * x for x in row[start:end]),
                )
                for row in matrix
            ]
        return cache[segment]

    def sharpe_on(trial: int, segments: Sequence[Segment]) -> float:
        n = total = squares = 0.0
        for segment in segments:
            bn, bs, bq = segment_stats(segment)[trial]
            n, total, squares = n + bn, total + bs, squares + bq
        return _sr(n, total, squares)

    def mean_on(trial: int, segments: Sequence[Segment]) -> float:
        n = sum(segment_stats(segment)[trial][0] for segment in segments)
        return sum(segment_stats(segment)[trial][1] for segment in segments) / n

    overfit = losses = splits = purged_max = 0
    logits: list[float] = []
    everything = range(partitions)
    for chosen in combinations(everything, partitions // 2):
        rest = tuple(block for block in everything if block not in chosen)
        cuts = _purge_cuts(times, bounds, rest, embargo)
        kept = [piece for block in chosen for piece in _subtract(bounds[block], cuts)]
        full = sum(bounds[block][1] - bounds[block][0] for block in chosen)
        size_kept = sum(end - start for start, end in kept)
        if size_kept < 2:
            raise CscvPurgeTooWide(
                f"the embargo {embargo} leaves {size_kept} in-sample periods in a CSCV split"
            )
        purged_max = max(purged_max, full - size_kept)
        oos = [bounds[block] for block in rest]
        in_sample = [sharpe_on(trial, kept) for trial in range(trials)]
        out_sample = [sharpe_on(trial, oos) for trial in range(trials)]
        best = max(range(trials), key=lambda trial: (in_sample[trial], -trial))
        omega = _average_rank(out_sample, best) / (trials + 1)
        logit = math.log(omega / (1.0 - omega))
        logits.append(logit)
        overfit += logit <= 0.0
        losses += mean_on(best, oos) < 0.0
        splits += 1
    return Pbo(
        pbo=overfit / splits,
        splits=splits,
        partitions=partitions,
        trials=trials,
        periods=periods,
        mean_logit=math.fsum(logits) / splits,
        oos_loss_share=losses / splits,
        embargo=embargo,
        purged_in_sample_periods_max=purged_max,
    )


def expected_max_sharpe(sharpe_variance: float, trials: int) -> float:
    """Expected maximum Sharpe ratio of ``trials`` unskilled trials (0 for one trial)."""
    if trials < 1:
        raise ValueError("trials must be >= 1")
    if trials == 1 or sharpe_variance <= 0.0:
        return 0.0
    return math.sqrt(sharpe_variance) * (
        (1.0 - EULER_GAMMA) * _NORMAL.inv_cdf(1.0 - 1.0 / trials)
        + EULER_GAMMA * _NORMAL.inv_cdf(1.0 - 1.0 / (trials * math.e))
    )


@dataclass(frozen=True)
class DeflatedSharpe:
    dsr: float
    sharpe: float
    benchmark_sharpe: float
    skewness: float
    kurtosis: float
    periods: int
    trials: int


def deflated_sharpe_ratio(
    returns: Sequence[float], trial_sharpes: Sequence[float], trials: int
) -> DeflatedSharpe | None:
    """DSR of the selected trial; ``None`` when it is not computable (too few / constant data)."""
    n = len(returns)
    if n < 3:
        return None
    mean = math.fsum(returns) / n
    m2 = math.fsum((x - mean) ** 2 for x in returns) / n
    if m2 == 0.0:
        return None
    m3 = math.fsum((x - mean) ** 3 for x in returns) / n
    m4 = math.fsum((x - mean) ** 4 for x in returns) / n
    skew, kurt = m3 / m2**1.5, m4 / (m2 * m2)
    sharpe = sharpe_ratio(returns)
    if len(trial_sharpes) >= 2:
        center = math.fsum(trial_sharpes) / len(trial_sharpes)
        variance = math.fsum((s - center) ** 2 for s in trial_sharpes) / (len(trial_sharpes) - 1)
    else:
        variance = 0.0
    benchmark = expected_max_sharpe(variance, trials)
    denominator = 1.0 - skew * sharpe + (kurt - 1.0) / 4.0 * sharpe * sharpe
    if denominator <= 0.0:
        return None
    z = (sharpe - benchmark) * math.sqrt(n - 1) / math.sqrt(denominator)
    return DeflatedSharpe(
        dsr=_NORMAL.cdf(z),
        sharpe=sharpe,
        benchmark_sharpe=benchmark,
        skewness=skew,
        kurtosis=kurt,
        periods=n,
        trials=trials,
    )

"""Exact binomial rates and Clopper-Pearson intervals in rational arithmetic (Phase 9, ADR-0042).

The calibration report hashes every rate, so no float may reach it. ``clopper_pearson`` finds the
exact interval bounds by bisection on the binomial tail computed with ``fractions.Fraction`` (no
rounding inside the search); only the final bounds are quantized to ``PLACES`` — the lower bound
down, the upper bound up — so the reported interval always contains the exact one.

The confidence level (``alpha``) is a reporting parameter supplied by the caller and recorded in the
report; it is not a Validation Profile number and has no default here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from math import comb
from typing import Final

__all__ = ["INTERVAL_METHOD", "PLACES", "BinomialRate", "binomial_rate", "clopper_pearson"]

INTERVAL_METHOD: Final = "clopper-pearson"
#: Quantization of every reported rate / bound (6 decimal places).
PLACES: Final = Decimal("0.000001")
#: Bisection steps: 2**-40 is far below ``PLACES``, so the outward rounding dominates.
_STEPS: Final = 40


def _cdf(k: int, n: int, p: Fraction) -> Fraction:
    """``P(X <= k)`` for ``X ~ Binomial(n, p)``, exactly."""
    q = 1 - p
    return sum((comb(n, i) * p**i * q ** (n - i) for i in range(k + 1)), Fraction(0))


def _check(count: int, n: int) -> None:
    if isinstance(count, bool) or isinstance(n, bool) or n < 1 or not 0 <= count <= n:
        raise ValueError(f"need 0 <= count <= n and n >= 1, got count={count}, n={n}")


def _check_alpha(alpha: Decimal) -> Fraction:
    if not isinstance(alpha, Decimal) or not Decimal(0) < alpha < Decimal(1):
        raise ValueError(f"alpha must be a Decimal in (0, 1), got {alpha!r}")
    return Fraction(alpha)


def clopper_pearson(count: int, n: int, alpha: Decimal) -> tuple[Decimal, Decimal]:
    """The exact two-sided ``1 - alpha`` interval of a binomial proportion, rounded outward."""
    _check(count, n)
    half = _check_alpha(alpha) / 2
    if count == 0:
        lower = Fraction(0)
    else:  # P(X >= count | p) = half; increasing in p
        lo, hi = Fraction(0), Fraction(1)
        for _ in range(_STEPS):
            mid = (lo + hi) / 2
            if 1 - _cdf(count - 1, n, mid) < half:
                lo = mid
            else:
                hi = mid
        lower = lo
    if count == n:
        upper = Fraction(1)
    else:  # P(X <= count | p) = half; decreasing in p
        lo, hi = Fraction(0), Fraction(1)
        for _ in range(_STEPS):
            mid = (lo + hi) / 2
            if _cdf(count, n, mid) > half:
                lo = mid
            else:
                hi = mid
        upper = hi
    return _decimal(lower, ROUND_FLOOR), _decimal(upper, ROUND_CEILING)


def _decimal(value: Fraction, rounding: str) -> Decimal:
    scaled = value / Fraction(PLACES)
    whole = scaled.numerator // scaled.denominator
    if rounding == ROUND_CEILING and whole * scaled.denominator != scaled.numerator:
        whole += 1
    return (Decimal(whole) * PLACES).quantize(PLACES)


@dataclass(frozen=True, slots=True)
class BinomialRate:
    """``count`` of ``n`` runs, the rate and its Clopper-Pearson interval."""

    count: int
    n: int
    rate: Decimal
    lower: Decimal
    upper: Decimal
    alpha: Decimal

    def to_payload(self) -> dict[str, object]:
        return {
            "count": self.count,
            "n": self.n,
            "rate": str(self.rate),
            "interval": {
                "method": INTERVAL_METHOD,
                "alpha": str(self.alpha),
                "lower": str(self.lower),
                "upper": str(self.upper),
            },
        }


def binomial_rate(count: int, n: int, alpha: Decimal) -> BinomialRate:
    _check(count, n)
    lower, upper = clopper_pearson(count, n, alpha)
    rate = (Decimal(count) / Decimal(n)).quantize(PLACES, rounding=ROUND_HALF_EVEN)
    return BinomialRate(count=count, n=n, rate=rate, lower=lower, upper=upper, alpha=alpha)

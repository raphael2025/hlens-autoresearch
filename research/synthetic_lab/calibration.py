"""Method calibration on synthetic markets (roadmap Phase 9; ADR-0042).

A *detector* is any method under test — eventually the full validation pipeline (P4 / P8) —
reduced to ``market -> bool`` ("an effect was found"). ``calibrate`` runs it on ``trials``
pure-noise
markets (seeds ``seed_base ... seed_base + trials - 1``) and on as many markets with the planted
effect, and reports the empirical false-positive rate and power. The declared level the pipeline
must meet is a Validation Profile number (not set here); synthetic results never support claims
about real markets (roadmap P9).

A detector that raises on a market has neither found nor missed an effect: the market is counted
in ``noise_errors`` / ``planted_errors`` (never as a detection) and stays in the ``trials``
denominator, so the reported rates are exact counts over every generated market and the error
counts show how much of the evidence is missing. ``false_positive_rate_bounds`` /
``power_bounds`` give the range the rate could take had every errored market gone either way.
The endpoints are rounded outward (lower toward ``-inf``, upper toward ``+inf``) to 28 significant
digits in a local ``decimal`` context, independent of the caller's ambient context, so the
interval always contains the exact ratio. A ratio a 28-digit Decimal represents exactly (0, 1,
integer ratios such as 1 / 4 or 1 / 128) stays exact, so with no detector errors both endpoints
are the same point; a repeating ratio (1 / 3) with no errors is the tightest 28-digit enclosure.

Configuration errors still raise (``PROPAGATED_ERRORS``). The harness follows the validation
pipeline's own classification (``research.validation.g4``, **Check isolation**): ``ValueError`` —
which includes ``ProfileFieldMissing``, ``UnsupportedMethod``, ``ExplicitParamRefused`` and the
harness's ``DetectorConfigurationError`` — and ``TypeError`` are deliberate configuration /
input-contract refusals of the caller, not evidence about the method; ``MemoryError`` is a
resource failure whose occurrence is not reproducible. Recording any of them as an errored market
would turn a misconfigured candidate into optimistic evidence (every market errored, a 0 / n
false-positive rate). Only other exceptions (arithmetic, lookup, attribute, runtime, assertion
errors, ...) are the method failing on a market.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Context, Decimal
from typing import Final

from core.contracts.synthetic import (
    PlantedEffect,
    SyntheticMarket,
    SyntheticMarketProvider,
    SyntheticMarketSpec,
)

__all__ = ["PROPAGATED_ERRORS", "CalibrationReport", "calibrate"]

#: Exceptions a detector raises that are **never** recorded as an errored market: they propagate
#: out of the harness unchanged (module docs). The same tuple as the validation pipeline's G4
#: check isolation (``research.validation.g4``); ``ProfileFieldMissing``, ``UnsupportedMethod``
#: and ``DetectorConfigurationError`` are ``ValueError`` subclasses.
PROPAGATED_ERRORS: Final = (ValueError, TypeError, MemoryError)

#: Significant digits of the bound endpoints (module docs); fixed, never the ambient context's.
_BOUND_PRECISION: Final = 28


def _rate_bounds(low: int, high: int, trials: int) -> tuple[Decimal, Decimal]:
    """``[low / trials, high / trials]`` rounded outward: floor the lower, ceil the upper."""
    floor = Context(prec=_BOUND_PRECISION, rounding=ROUND_FLOOR)
    ceiling = Context(prec=_BOUND_PRECISION, rounding=ROUND_CEILING)
    return (
        floor.divide(Decimal(low), Decimal(trials)),
        ceiling.divide(Decimal(high), Decimal(trials)),
    )


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    detector: str
    trials: int
    false_positives: int
    detections: int
    false_positive_rate: Decimal
    power: Decimal
    planted: PlantedEffect
    noise_errors: int = 0
    planted_errors: int = 0

    @property
    def false_positive_rate_bounds(self) -> tuple[Decimal, Decimal]:
        """``[false_positives / trials, (false_positives + noise_errors) / trials]``, outward."""
        return _rate_bounds(
            self.false_positives, self.false_positives + self.noise_errors, self.trials
        )

    @property
    def power_bounds(self) -> tuple[Decimal, Decimal]:
        """``[detections / trials, (detections + planted_errors) / trials]``, outward."""
        return _rate_bounds(self.detections, self.detections + self.planted_errors, self.trials)


def _detected(detector: Callable[[SyntheticMarket], bool], market: SyntheticMarket) -> bool | None:
    """``None`` when the detector raised (see module docs); ``PROPAGATED_ERRORS`` still raise."""
    try:
        return bool(detector(market))
    except PROPAGATED_ERRORS:
        raise
    except Exception:  # the method failed on this market: evidence, never a detection
        return None


def calibrate(
    provider: SyntheticMarketProvider,
    base: SyntheticMarketSpec,
    planted: PlantedEffect,
    detector: Callable[[SyntheticMarket], bool],
    *,
    detector_name: str,
    trials: int,
    seed_base: int = 0,
) -> CalibrationReport:
    if trials < 1:
        raise ValueError("trials must be positive")
    if base.effects:
        raise ValueError("the base spec must be pure noise (no planted effects)")
    false_positives = detections = noise_errors = planted_errors = 0
    for offset in range(trials):
        seed = seed_base + offset
        noise = provider.generate(base.model_copy(update={"seed": seed}))
        found = _detected(detector, noise)
        false_positives += found is True
        noise_errors += found is None
        effect = provider.generate(base.model_copy(update={"seed": seed, "effects": (planted,)}))
        found = _detected(detector, effect)
        detections += found is True
        planted_errors += found is None
    return CalibrationReport(
        detector=detector_name,
        trials=trials,
        false_positives=false_positives,
        detections=detections,
        false_positive_rate=Decimal(false_positives) / trials,
        power=Decimal(detections) / trials,
        planted=planted,
        noise_errors=noise_errors,
        planted_errors=planted_errors,
    )

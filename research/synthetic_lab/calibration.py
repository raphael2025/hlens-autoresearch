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
counts show how much of the evidence is missing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from core.contracts.synthetic import (
    PlantedEffect,
    SyntheticMarket,
    SyntheticMarketProvider,
    SyntheticMarketSpec,
)

__all__ = ["CalibrationReport", "calibrate"]


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


def _detected(detector: Callable[[SyntheticMarket], bool], market: SyntheticMarket) -> bool | None:
    """``None`` when the detector raised (see module docs)."""
    try:
        return bool(detector(market))
    except Exception:
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

"""Method calibration on synthetic markets (roadmap Phase 9; ADR-0042).

A *detector* is any method under test — eventually the full validation pipeline (P4 / P8) —
reduced to ``market -> bool`` ("an effect was found"). ``calibrate`` runs it on ``trials``
pure-noise
markets (seeds ``seed_base ... seed_base + trials - 1``) and on as many markets with the planted
effect, and reports the empirical false-positive rate and power. The declared level the pipeline
must meet is a Validation Profile number (not set here); synthetic results never support claims
about real markets (roadmap P9).
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
    false_positives = detections = 0
    for offset in range(trials):
        seed = seed_base + offset
        noise = provider.generate(base.model_copy(update={"seed": seed}))
        if detector(noise):
            false_positives += 1
        effect = provider.generate(base.model_copy(update={"seed": seed, "effects": (planted,)}))
        if detector(effect):
            detections += 1
    return CalibrationReport(
        detector=detector_name,
        trials=trials,
        false_positives=false_positives,
        detections=detections,
        false_positive_rate=Decimal(false_positives) / trials,
        power=Decimal(detections) / trials,
        planted=planted,
    )

"""G4 robustness stage and the full in-sample pipeline G0 → G4 (Phase 8, ADR-0041).

``run_robustness`` runs every check of ``robustness.py`` on one ``RobustnessInput`` and returns a
``RobustnessResult`` (checks + their gates, in a fixed order). ``run_validation`` is the complete
in-sample flow of 07-validation.md §2: ``run_in_sample`` (G0 → G3, unchanged) and, only when no
stage failed, G4. The robustness input may be passed lazily (a callable): building it usually
means re-running every declared parameter point, which is pointless after an earlier FAIL.
Without any robustness input G4 is materialized as ``G4.robustness_input`` = ``INCONCLUSIVE``
(ADR-0013: a missing stage is never a PASS). G5 (sealed OOS) stays a separate, one-shot call
(``pipeline.run_sealed_oos``): a G0 – G4 ``PASS`` is **never promotable** on its own — the
report view labels it ``promotion.blocked_reason = "sealed_oos_not_evaluated"`` until G5 runs.

``RobustnessParams`` are the explicit parameters for rules the Profile contract has **no field**
for. Every field is required (``None`` = not given → ``profile_field_missing``); nothing here has a
default value, and every value that is used is recorded with the source ``param:<name>``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta

from core.contracts.validation_profile import ValidationProfile
from core.domain.research import GateResult, Verdict
from research.validation.gates import Threshold, explicit_threshold, inconclusive_gate
from research.validation.pipeline import InSampleInput, run_in_sample
from research.validation.returns import ParamPoint, PeriodReturns, TrialReturns, param_key
from research.validation.robustness import (
    CapacityFill,
    RobustnessCheck,
    StateTrade,
    capacity_check,
    cost_stress_check,
    cross_asset_check,
    delay_stress_check,
    overfitting_check,
    parameter_neighborhood_check,
    state_decomposition_check,
    time_alignment_check,
    walk_forward_check,
)

__all__ = [
    "RobustnessInput",
    "RobustnessParams",
    "RobustnessResult",
    "ValidationRun",
    "run_robustness",
    "run_validation",
]


@dataclass(frozen=True)
class RobustnessParams:
    """Explicit parameters for rules without a Profile field (all required; ``None`` = missing)."""

    cscv_partitions: int | None
    max_participation_rate: float | None
    min_capacity: float | None
    impact_coefficient: float | None
    cross_asset_min_positive_fraction: float | None
    max_undersampled_pnl_share: float | None

    def _threshold(self, name: str, value: float | None) -> Threshold | None:
        return None if value is None else explicit_threshold(name, value)

    @property
    def max_participation(self) -> Threshold | None:
        return self._threshold("capacity.max_participation_rate", self.max_participation_rate)

    @property
    def capacity_floor(self) -> Threshold | None:
        return self._threshold("capacity.min_capacity", self.min_capacity)

    @property
    def undersampled_share(self) -> Threshold | None:
        return self._threshold("state.max_undersampled_pnl_share", self.max_undersampled_pnl_share)

    @property
    def cross_asset_fraction(self) -> Threshold | None:
        return self._threshold(
            "cross_asset.min_positive_fraction", self.cross_asset_min_positive_fraction
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "cscv_partitions": self.cscv_partitions,
            "capacity.max_participation_rate": self.max_participation_rate,
            "capacity.min_capacity": self.min_capacity,
            "capacity.impact_coefficient": self.impact_coefficient,
            "cross_asset.min_positive_fraction": self.cross_asset_min_positive_fraction,
            "state.max_undersampled_pnl_share": self.max_undersampled_pnl_share,
            "source": "param (explicit; no Profile field exists)",
        }


@dataclass(frozen=True)
class RobustnessInput:
    """Everything G4 looks at. ``trials`` are every evaluated point of the declared space."""

    profile: ValidationProfile
    family_trial_count: int
    param_space: Mapping[str, tuple[str | int | float | bool, ...]]
    chosen: ParamPoint
    trials: tuple[TrialReturns, ...]
    delayed: PeriodReturns | None
    time_shifted: Mapping[timedelta, PeriodReturns]
    state_trades: tuple[StateTrade, ...] | None
    capacity_fills: tuple[CapacityFill, ...] | None
    per_asset: Mapping[str, PeriodReturns]
    declared_instruments: tuple[str, ...]
    params: RobustnessParams

    def __post_init__(self) -> None:
        if self.family_trial_count < 1:
            raise ValueError("family_trial_count must be >= 1")
        if not self.trials:
            raise ValueError("G4 needs at least the chosen trial")


@dataclass(frozen=True)
class RobustnessResult:
    checks: tuple[RobustnessCheck, ...]
    params: RobustnessParams | None

    @property
    def gates(self) -> tuple[GateResult, ...]:
        return tuple(gate for check in self.checks for gate in check.gates)

    def to_dict(self) -> dict[str, object]:
        return {
            "checks": [check.to_dict() for check in self.checks],
            "explicit_params": None if self.params is None else self.params.to_dict(),
        }


def run_robustness(inp: RobustnessInput) -> RobustnessResult:
    """Every G4 check in a fixed order (C-T1 / C-R1, C-R1, C-R4, C-S4 / C-R3, C-R2, C-R5, C-R3)."""
    profile, params = inp.profile, inp.params
    key = param_key(inp.chosen)
    chosen = next((trial for trial in inp.trials if trial.key() == key), None)
    if chosen is None:
        raise ValueError(f"no trial holds the chosen parameters {dict(inp.chosen)}")
    returns = chosen.returns
    checks = (
        overfitting_check(
            profile, inp.trials, inp.chosen, inp.family_trial_count, params.cscv_partitions
        ),
        parameter_neighborhood_check(profile, inp.trials, inp.chosen, inp.param_space),
        time_alignment_check(profile, returns, inp.time_shifted),
        delay_stress_check(profile, inp.delayed),
        cost_stress_check(profile, returns),
        walk_forward_check(profile, returns),
        state_decomposition_check(
            profile, inp.state_trades, max_undersampled_share=params.undersampled_share
        ),
        capacity_check(
            profile,
            inp.capacity_fills,
            len(returns),
            max_participation=params.max_participation,
            min_capacity=params.capacity_floor,
            impact_coefficient=params.impact_coefficient,
        ),
        cross_asset_check(
            profile, inp.per_asset, inp.declared_instruments, params.cross_asset_fraction
        ),
    )
    return RobustnessResult(checks=checks, params=params)


@dataclass(frozen=True)
class ValidationRun:
    """G0 → G4 gates, the robustness detail (``None`` when G4 did not run) and where it stopped."""

    gates: tuple[GateResult, ...]
    robustness: RobustnessResult | None

    @property
    def stopped_at(self) -> str | None:
        failed = next((gate for gate in self.gates if gate.verdict is Verdict.FAIL), None)
        return None if failed is None else failed.gate_id.split(".")[0]


RobustnessSource = RobustnessInput | Callable[[], RobustnessInput] | None


def run_validation(in_sample: InSampleInput, robustness: RobustnessSource) -> ValidationRun:
    """G0 → G3 (``run_in_sample``) then G4 unless a stage failed (see module docs)."""
    gates = run_in_sample(in_sample)
    if any(gate.verdict is Verdict.FAIL for gate in gates):
        return ValidationRun(gates=gates, robustness=None)
    if robustness is None:
        missing = inconclusive_gate("G4.robustness_input", "robustness_input_missing", 0.0)
        return ValidationRun(gates=(*gates, missing), robustness=None)
    source = robustness() if callable(robustness) else robustness
    if source.profile.content_hash() != in_sample.context.profile.content_hash():
        raise ValueError("G4 must use the Profile bound to the validation context")
    result = run_robustness(source)
    return ValidationRun(gates=(*gates, *result.gates), robustness=result)

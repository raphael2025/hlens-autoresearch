"""G4 robustness checks: Constitution C-R1 ~ C-R5 and the C-T1 overfitting probability (ADR-0041).

Every check returns a ``RobustnessCheck``: its gates (the only thing that decides a verdict,
ADR-0013), the thresholds it used **with the Profile field that supplied each one**, the fields it
needed but could not find, and a JSON-ready ``details`` block for the report. There is no numeric
threshold in this module: a threshold is ``gates.threshold(profile, <path>)`` or, for a rule the
Profile contract has no field for, an ``explicit_threshold`` recorded as ``param:<name>``; with
neither, the gate is ``missing_field_gate`` (``INCONCLUSIVE``, metric ``profile_field_missing:...``)
and the check's status is ``PROFILE_FIELD_MISSING``. The only literal comparisons are structural
signs (a profit is ``> 0``), never calibrated numbers.

No silent pass (ADR-0041 review fixes, 2026-09-25):

- every check of C-R1 ~ C-R5 is Constitution-required, so an empty or disabled configuration
  (no parameter neighbours, empty ``time_alignment_offsets``, ``delay_stress_bars == 0``, no
  declared instrument) is a ``configuration_missing:<what>`` gate (``INCONCLUSIVE``), never
  ``gates=()``; ``RobustnessCheck`` refuses a gate-less check without a recorded reason and a
  missing field that no ``INCONCLUSIVE`` gate carries;
- computing an estimate (capacity, impact, P&L shares) is reported, never a PASS gate by itself;
- when the backtest's execution model carries its own impact coefficient (ADR-0038 execution
  note; ``research.strategies.validation``) and an explicit ``param:capacity.impact_coefficient``
  is also given, the two must agree: a disagreement is never resolved by picking one silently
  (implementation note, 2026-09-26); ``capacity_check``'s ``impact_conflict`` makes
  ``G4.capacity.impact_estimated`` ``INCONCLUSIVE`` with the named reason
  ``impact_coefficient_mismatch``.

Checks (principle → gate ids → threshold sources):

- ``overfitting_check`` (C-T1 / C-R1) → ``G4.overfitting`` → method
  ``significance.overfitting_metric``, threshold ``significance.overfitting_threshold``; CSCV
  partitions ``param:cscv_partitions``, purged / embargoed between blocks by
  ``data_split.embargo`` and at least the label / holding horizon (``RobustnessInput``);
- ``parameter_neighborhood_check`` (C-R1) → ``G4.param_neighborhood.performance_ratio`` /
  ``.positive_fraction`` → ``parameter_stability.neighborhood_definition`` (method),
  ``.min_neighborhood_performance_ratio``, ``.min_positive_neighbor_fraction``;
- ``time_alignment_check`` (C-R1) → ``G4.time_alignment.<i>`` (``.offsets`` when none are
  configured) → ``parameter_stability.time_alignment_offsets[i]`` (offset),
  ``.min_neighborhood_performance_ratio``;
- ``delay_stress_check`` (C-R4 / A6) → ``G4.delay_stress`` → ``cost_stress.delay_stress_bars``
  (delay), ``cost_stress.min_breakeven_cost_multiple``;
- ``cost_stress_check`` (C-R4 / A6) → ``G4.cost_stress.breakeven`` / ``.<i>`` →
  ``cost_stress.min_breakeven_cost_multiple``, ``cost_stress.stress_multipliers[i]``;
- ``walk_forward_check`` (C-S4 / C-R3) → ``G4.walk_forward.positive_fraction`` /
  ``.max_window_share`` → ``data_split.walk_forward.*``, over the non-overlapping test windows
  (a window without returns is counted and makes the fraction ``INCONCLUSIVE``);
- ``state_decomposition_check`` (C-R2) → ``G4.state.sufficient_states`` /
  ``.pnl_outside_undersampled_states`` / ``.undersampled_pnl_share`` →
  ``sample_size.min_effective_trades_per_state``; no Profile field:
  ``param:state.max_undersampled_pnl_share``;
- ``capacity_check`` (C-R5) → ``G4.capacity.estimated`` (only INCONCLUSIVE) / ``.required`` /
  ``.impact_estimated`` → no Profile field: ``param:capacity.max_participation_rate``,
  ``param:capacity.min_capacity``, ``param:capacity.impact_coefficient``;
- ``cross_asset_check`` (C-R3) → ``G4.cross_asset.scope_covered`` / ``.positive_fraction`` → no
  Profile field: ``param:cross_asset.min_positive_fraction``. ADR-0059 (Accepted 2026-09-26): when
  every declared instrument's single-asset re-run is known to hold no position the fraction is
  ``INCONCLUSIVE`` (``not_applicable_zero_exposure_single_asset``); a strategy **declared**
  cross-sectional is judged over disjoint sub-universes (``subuniverse_partition``) with the same
  threshold, ``INCONCLUSIVE`` (``not_enough_instruments_for_subuniverses``) when fewer than two
  sub-universes exist.

Profile sources (ADR-0052 §2, implementation note 2026-09-26): a Profile that carries
``significance.cscv_partitions``, ``capacity.*``, ``cross_asset.min_positive_fraction`` or
``sample_size.max_undersampled_pnl_share`` supplies that value itself (``research.validation.g4``
resolves it with ``gates.sourced_threshold`` / ``sourced_parameter``; an explicit ``param:`` given
as well is refused, C-A4) and the recorded source is the Profile path; a Profile without them keeps
the ``param:`` / ``profile_field_missing`` behaviour above, bit for bit. ``capacity.impact_model``,
when given, must name an implemented impact law (``IMPACT_MODELS``) or is ``UnsupportedMethod``.

Performance is the per-period Sharpe ratio of **net** returns at cost multiplier 1
(``overfitting.sharpe_ratio``); window P&L is the sum of per-period net returns.
Status: FRAMEWORK_IMPLEMENTED / NOT_VALIDATED.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from core.contracts.validation_profile import ValidationProfile
from core.domain.research import GateResult, Verdict
from research.validation.costs import multiplier
from research.validation.gates import (
    PROFILE_FIELD_MISSING,
    Direction,
    Threshold,
    compare_gate,
    configuration_missing_gate,
    flag_gate,
    inconclusive_gate,
    missing_field_gate,
    threshold,
)
from research.validation.overfitting import (
    CscvPurgeTooWide,
    deflated_sharpe_ratio,
    probability_of_backtest_overfitting,
    sharpe_ratio,
)
from research.validation.returns import ParamPoint, PeriodReturns, TrialReturns, param_key
from research.validation.splits import non_overlapping_windows, walk_forward_windows
from research.validation.stats import UnsupportedMethod, effective_sample_size

__all__ = [
    "DSR_METHODS",
    "IMPACT_MODELS",
    "MIN_CROSS_SECTION",
    "NEIGHBORHOOD_METHODS",
    "NOT_ENOUGH_FOR_SUBUNIVERSES",
    "PBO_METHODS",
    "SUBUNIVERSE_RULE",
    "ZERO_EXPOSURE_SINGLE_ASSET",
    "BAR_VOLUME_SOURCE_MISMATCH",
    "CapacityFill",
    "VolumeSourceMismatch",
    "CheckStatus",
    "RobustnessCheck",
    "StateTrade",
    "SubUniverse",
    "ThresholdUse",
    "capacity_check",
    "cost_stress_check",
    "cross_asset_check",
    "delay_stress_check",
    "overfitting_check",
    "parameter_neighborhood_check",
    "state_decomposition_check",
    "subuniverse_partition",
    "time_alignment_check",
    "walk_forward_check",
]

#: ``significance.overfitting_metric`` names implemented here; any other name is refused.
PBO_METHODS: Final = frozenset({"pbo_cscv", "pbo", "cscv_pbo"})
DSR_METHODS: Final = frozenset({"deflated_sharpe", "deflated_sharpe_ratio", "dsr"})
#: ``parameter_stability.neighborhood_definition`` names implemented here.
NEIGHBORHOOD_METHODS: Final = frozenset({"adjacent_grid", "adjacent_grid_points", "one_step_grid"})
#: ``capacity.impact_model`` names implemented here (ADR-0052 §2): the square-root law of
#: ``capacity_check`` — the same name as ``plugins.backtest.execution.IMPACT_MODEL``.
IMPACT_MODELS: Final = frozenset({"square_root"})
#: Explicit parameter (no Profile field) bounding the P&L share of undersampled states (C-R2).
UNDERSAMPLED_SHARE_PARAM: Final = "state.max_undersampled_pnl_share"
#: What a parameter neighbourhood is built from (the candidate's declared search space).
NEIGHBORS_CONFIGURATION: Final = "param_search_space.neighbors"


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    PROFILE_FIELD_MISSING = "PROFILE_FIELD_MISSING"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class ThresholdUse:
    """One threshold a check compared against, and the field (or ``param:``) it came from."""

    name: str
    value: float
    source: str


@dataclass(frozen=True)
class RobustnessCheck:
    check_id: str
    principles: tuple[str, ...]
    gates: tuple[GateResult, ...]
    thresholds: tuple[ThresholdUse, ...] = ()
    missing_fields: tuple[str, ...] = ()
    details: Mapping[str, object] = field(default_factory=dict)
    note: str = ""

    def __post_init__(self) -> None:
        if not self.gates and not self.note.strip():
            raise ValueError(f"check {self.check_id!r} has no gate: NOT_APPLICABLE needs a reason")
        gated = {gate.metric for gate in self.gates if gate.verdict is Verdict.INCONCLUSIVE}
        for name in self.missing_fields:  # a missing field that no gate carries could PASS
            if f"{PROFILE_FIELD_MISSING}:{name}" not in gated:
                raise ValueError(
                    f"check {self.check_id!r}: missing field {name!r} has no INCONCLUSIVE gate"
                )

    @property
    def status(self) -> CheckStatus:
        verdicts = {gate.verdict for gate in self.gates}
        if Verdict.FAIL in verdicts:
            return CheckStatus.FAIL
        if self.missing_fields:
            return CheckStatus.PROFILE_FIELD_MISSING
        if Verdict.INCONCLUSIVE in verdicts:
            return CheckStatus.INCONCLUSIVE
        if not self.gates:
            return CheckStatus.NOT_APPLICABLE
        return CheckStatus.PASS

    def to_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "principles": list(self.principles),
            "status": self.status.value,
            "gates": [gate.model_dump(mode="json") for gate in self.gates],
            "thresholds": [
                {"name": use.name, "value": use.value, "source": use.source}
                for use in self.thresholds
            ],
            "missing_fields": list(self.missing_fields),
            "details": dict(self.details),
            "note": self.note,
        }


def _use(name: str, limit: Threshold) -> ThresholdUse:
    return ThresholdUse(name=name, value=limit.value, source=limit.source)


def _method(value: str, supported: frozenset[str], field_path: str) -> str:
    name = value.strip().lower()
    if name not in supported:
        raise UnsupportedMethod(f"{field_path} {value!r} is not implemented")
    return name


def _chosen(trials: Sequence[TrialReturns], chosen: ParamPoint) -> TrialReturns:
    key = param_key(chosen)
    match = [trial for trial in trials if trial.key() == key]
    if len(match) != 1:
        raise ValueError(f"exactly one trial must hold the chosen parameters {dict(chosen)}")
    return match[0]


def _sharpe(returns: PeriodReturns) -> float:
    return sharpe_ratio(returns.net_floats())


# ======================================================================================
# C-T1 / C-R1: selection overfitting (PBO via CSCV, Deflated Sharpe)
# ======================================================================================


def overfitting_check(
    profile: ValidationProfile,
    trials: Sequence[TrialReturns],
    chosen: ParamPoint,
    family_trial_count: int,
    cscv_partitions: int | None,
    *,
    horizon: timedelta,
    partitions_source: str = "param:cscv_partitions",
) -> RobustnessCheck:
    """Gate the Profile's overfitting metric; the other metric is reported only.

    ``horizon`` is the longest span one period shares a label or a position with later periods
    (the larger of the Outcome label horizon and the longest holding period); the CSCV purge is
    at least that wide (``overfitting.probability_of_backtest_overfitting``).
    ``partitions_source`` records where ``cscv_partitions`` came from (``param:cscv_partitions``,
    or ``significance.cscv_partitions`` when the Profile carries it, ADR-0052 §2).
    """
    method = _method(
        profile.significance.overfitting_metric,
        PBO_METHODS | DSR_METHODS,
        "significance.overfitting_metric",
    )
    limit = threshold(profile, "significance.overfitting_threshold")
    selected = _chosen(trials, chosen)
    if any(trial.returns.times != selected.returns.times for trial in trials):
        raise ValueError("every trial of the family must share one period grid")
    matrix = [trial.returns.net_floats() for trial in trials]
    count = max(family_trial_count, len(trials))
    details: dict[str, object] = {
        "method": method,
        "trials_evaluated": len(trials),
        "family_trial_count": count,
        "chosen_params": {name: value for name, value in sorted(selected.params.items())},
    }
    missing: list[str] = []
    pbo_value: float | None = None
    pbo_reason = ""
    if cscv_partitions is None:
        pbo_reason = "cscv_partitions_missing"
    elif len(trials) < 2:
        pbo_reason = "fewer_than_two_trials"
    elif len(selected.returns) < 2 * cscv_partitions:
        pbo_reason = "too_few_periods_for_partitions"
    else:
        embargo = profile.data_split.embargo
        try:
            pbo = probability_of_backtest_overfitting(
                matrix,
                cscv_partitions,
                times=selected.returns.times,
                embargo=embargo,
                horizon=horizon,
            )
        except CscvPurgeTooWide:
            pbo_reason = "purge_leaves_too_few_in_sample_periods"
        else:
            pbo_value = pbo.pbo
            details["pbo"] = {
                "pbo": pbo.pbo,
                "splits": pbo.splits,
                "partitions": pbo.partitions,
                "partitions_source": partitions_source,
                "mean_logit": pbo.mean_logit,
                "oos_loss_share": pbo.oos_loss_share,
                "embargo_seconds": embargo.total_seconds(),
                "embargo_source": "data_split.embargo",
                "purge_horizon_seconds": horizon.total_seconds(),
                "purge_horizon_source": "max(label horizon, longest holding period)",
                "purged_in_sample_periods_max": pbo.purged_in_sample_periods_max,
            }
    if pbo_reason:
        details["pbo"] = {"not_computed": pbo_reason}
    dsr = deflated_sharpe_ratio(
        matrix[trials.index(selected)], [sharpe_ratio(row) for row in matrix], count
    )
    details["deflated_sharpe"] = (
        {"not_computed": "degenerate_returns"}
        if dsr is None
        else {
            "dsr": dsr.dsr,
            "sharpe": dsr.sharpe,
            "benchmark_sharpe": dsr.benchmark_sharpe,
            "skewness": dsr.skewness,
            "kurtosis": dsr.kurtosis,
            "periods": dsr.periods,
            "trials": dsr.trials,
        }
    )
    if method in PBO_METHODS:
        if pbo_value is not None:
            gate = compare_gate(
                profile,
                "G4.overfitting",
                "probability_of_backtest_overfitting",
                pbo_value,
                limit,
                Direction.AT_MOST,
            )
        elif pbo_reason == "cscv_partitions_missing":
            missing.append("cscv_partitions")
            gate = missing_field_gate("G4.overfitting", "cscv_partitions")
        else:
            gate = inconclusive_gate("G4.overfitting", f"pbo_not_computed:{pbo_reason}", 0.0)
    elif dsr is None:
        gate = inconclusive_gate("G4.overfitting", "dsr_not_computed:degenerate_returns", 0.0)
    else:
        gate = compare_gate(
            profile,
            "G4.overfitting",
            "one_minus_deflated_sharpe_ratio",
            1.0 - dsr.dsr,
            limit,
            Direction.AT_MOST,
        )
    return RobustnessCheck(
        check_id="overfitting",
        principles=("C-T1", "C-R1"),
        gates=(gate,),
        thresholds=(_use("overfitting_threshold", limit),),
        missing_fields=tuple(missing),
        details=details,
    )


# ======================================================================================
# C-R1: parameter neighbourhood and time alignment
# ======================================================================================


Scalar = str | int | float | bool


def _neighbors(
    chosen: ParamPoint, space: Mapping[str, Sequence[Scalar]]
) -> list[dict[str, Scalar]]:
    points: list[dict[str, Scalar]] = []
    for name in sorted(space):
        values = list(space[name])
        if len(values) < 2:
            continue
        if name not in chosen:
            raise ValueError(f"the chosen point has no value for declared parameter {name!r}")
        reprs = [repr(value) for value in values]
        if repr(chosen[name]) not in reprs:
            raise ValueError(f"the chosen {name}={chosen[name]!r} is not in the declared space")
        index = reprs.index(repr(chosen[name]))
        for other in (index - 1, index + 1):
            if 0 <= other < len(values):
                points.append({**chosen, name: values[other]})
    return points


def parameter_neighborhood_check(
    profile: ValidationProfile,
    trials: Sequence[TrialReturns],
    chosen: ParamPoint,
    param_space: Mapping[str, Sequence[str | int | float | bool]],
) -> RobustnessCheck:
    params = profile.parameter_stability
    method = _method(
        params.neighborhood_definition,
        NEIGHBORHOOD_METHODS,
        "parameter_stability.neighborhood_definition",
    )
    ratio_limit = threshold(profile, "parameter_stability.min_neighborhood_performance_ratio")
    fraction_limit = threshold(profile, "parameter_stability.min_positive_neighbor_fraction")
    uses = (
        _use("min_neighborhood_performance_ratio", ratio_limit),
        _use("min_positive_neighbor_fraction", fraction_limit),
    )
    selected = _chosen(trials, chosen)
    points = _neighbors(selected.params, param_space)
    details: dict[str, object] = {"method": method, "declared_neighbors": len(points)}
    ratio_id = "G4.param_neighborhood.performance_ratio"
    fraction_id = "G4.param_neighborhood.positive_fraction"
    if not points:  # an isolated point is exactly what C-R1 declares invalid: never skipped
        return RobustnessCheck(
            check_id="parameter_neighborhood",
            principles=("C-R1",),
            gates=(
                configuration_missing_gate(ratio_id, NEIGHBORS_CONFIGURATION),
                configuration_missing_gate(fraction_id, NEIGHBORS_CONFIGURATION),
            ),
            thresholds=uses,
            details=details,
            note="no declared parameter varies: C-R1 has no neighbourhood to test (INCONCLUSIVE)",
        )
    by_key = {trial.key(): trial for trial in trials}
    found = [by_key.get(param_key(point)) for point in points]
    evaluated = [trial for trial in found if trial is not None]
    chosen_sr = _sharpe(selected.returns)
    sharpes = [_sharpe(trial.returns) for trial in evaluated]
    details.update(
        chosen_sharpe=chosen_sr,
        neighbors=[
            {"params": dict(sorted(trial.params.items())), "sharpe": sr}
            for trial, sr in zip(evaluated, sharpes, strict=True)
        ],
        neighbors_not_evaluated=len(points) - len(evaluated),
    )
    if len(evaluated) != len(points):
        missing = float(len(points) - len(evaluated))
        gates = (
            inconclusive_gate(ratio_id, "declared_neighbors_not_evaluated", missing),
            inconclusive_gate(fraction_id, "declared_neighbors_not_evaluated", missing),
        )
    else:
        fraction = sum(1 for sr in sharpes if sr > 0) / len(sharpes)
        fraction_gate = compare_gate(
            profile,
            fraction_id,
            "positive_neighbor_fraction",
            fraction,
            fraction_limit,
            Direction.AT_LEAST,
        )
        if chosen_sr <= 0:
            ratio_gate = inconclusive_gate(ratio_id, "chosen_sharpe_not_positive", chosen_sr)
        else:
            ratio = (sum(sharpes) / len(sharpes)) / chosen_sr
            ratio_gate = compare_gate(
                profile,
                ratio_id,
                "neighbor_mean_sharpe_over_chosen_sharpe",
                ratio,
                ratio_limit,
                Direction.AT_LEAST,
            )
        gates = (ratio_gate, fraction_gate)
    return RobustnessCheck(
        check_id="parameter_neighborhood",
        principles=("C-R1",),
        gates=gates,
        thresholds=uses,
        details=details,
    )


def time_alignment_check(
    profile: ValidationProfile,
    base: PeriodReturns,
    shifted: Mapping[timedelta, PeriodReturns],
) -> RobustnessCheck:
    """The result must survive the Profile's decision-time offsets (bar-offset alignment)."""
    offsets = profile.parameter_stability.time_alignment_offsets
    limit = threshold(profile, "parameter_stability.min_neighborhood_performance_ratio")
    uses = (_use("min_neighborhood_performance_ratio", limit),)
    if not offsets:  # the C-R1 time-alignment test is required (07-validation §5.1)
        return RobustnessCheck(
            check_id="time_alignment",
            principles=("C-R1",),
            gates=(
                configuration_missing_gate(
                    "G4.time_alignment.offsets", "parameter_stability.time_alignment_offsets"
                ),
            ),
            thresholds=uses,
            note="parameter_stability.time_alignment_offsets is empty (INCONCLUSIVE)",
        )
    base_sr = _sharpe(base)
    gates: list[GateResult] = []
    rows: list[dict[str, object]] = []
    for index, offset in enumerate(offsets):
        gate_id = f"G4.time_alignment.{index}"
        run = shifted.get(offset)
        row: dict[str, object] = {
            "offset_seconds": offset.total_seconds(),
            "offset_source": f"parameter_stability.time_alignment_offsets[{index}]",
        }
        if run is None:
            gates.append(inconclusive_gate(gate_id, "time_aligned_run_missing", 0.0))
        elif base_sr <= 0:
            gates.append(inconclusive_gate(gate_id, "base_sharpe_not_positive", base_sr))
        else:
            shifted_sr = _sharpe(run)
            row["sharpe"] = shifted_sr
            gates.append(
                compare_gate(
                    profile,
                    gate_id,
                    "shifted_sharpe_over_base_sharpe",
                    shifted_sr / base_sr,
                    limit,
                    Direction.AT_LEAST,
                )
            )
        rows.append(row)
    return RobustnessCheck(
        check_id="time_alignment",
        principles=("C-R1",),
        gates=tuple(gates),
        thresholds=uses,
        details={"base_sharpe": base_sr, "offsets": rows},
    )


# ======================================================================================
# C-R4 / A6: delay stress and cost stress
# ======================================================================================


def delay_stress_check(
    profile: ValidationProfile, delayed: PeriodReturns | None
) -> RobustnessCheck:
    """The edge must survive executing ``cost_stress.delay_stress_bars`` bars late."""
    bars = profile.cost_stress.delay_stress_bars
    limit = threshold(profile, "cost_stress.min_breakeven_cost_multiple")
    uses = (_use("min_breakeven_cost_multiple", limit),)
    details: dict[str, object] = {
        "delay_bars": bars,
        "delay_bars_source": "cost_stress.delay_stress_bars",
    }
    if bars == 0:  # delay stress is part of the required C-R4 / A6 suite (07-validation §5.1)
        return RobustnessCheck(
            check_id="delay_stress",
            principles=("C-R4",),
            gates=(configuration_missing_gate("G4.delay_stress", "cost_stress.delay_stress_bars"),),
            thresholds=uses,
            details=details,
            note="cost_stress.delay_stress_bars is 0: no delay stress configured (INCONCLUSIVE)",
        )
    if delayed is None:
        gate = inconclusive_gate("G4.delay_stress", "delayed_run_missing", float(bars))
    else:
        breakeven = delayed.breakeven_cost_multiple()
        details["net_total"] = float(sum(delayed.net(), Decimal(0)))
        if breakeven is None:
            gate = inconclusive_gate("G4.delay_stress", "no_cost_in_delayed_run", 0.0)
        else:
            details["breakeven_cost_multiple"] = float(breakeven)
            gate = compare_gate(
                profile,
                "G4.delay_stress",
                "delayed_breakeven_cost_multiple",
                float(breakeven),
                limit,
                Direction.AT_LEAST,
            )
    return RobustnessCheck(
        check_id="delay_stress",
        principles=("C-R4",),
        gates=(gate,),
        thresholds=uses,
        details=details,
    )


def cost_stress_check(profile: ValidationProfile, returns: PeriodReturns) -> RobustnessCheck:
    """Breakeven cost multiple of the realized returns vs every Profile stress multiplier."""
    stress = profile.cost_stress
    floor = threshold(profile, "cost_stress.min_breakeven_cost_multiple")
    limits = [
        threshold(profile, f"cost_stress.stress_multipliers[{index}]")
        for index in range(len(stress.stress_multipliers))
    ]
    uses = (_use("min_breakeven_cost_multiple", floor),) + tuple(
        _use(f"stress_multiplier_{index}", limit) for index, limit in enumerate(limits)
    )
    reported = {
        repr(value): float(sum(returns.net(multiplier(value)), Decimal(0)))
        for value in (*stress.stress_multipliers, *stress.reported_only_multipliers)
    }
    details: dict[str, object] = {"net_total_at_multiplier": reported}
    breakeven = returns.breakeven_cost_multiple()
    gate_ids = ["G4.cost_stress.breakeven"] + [
        f"G4.cost_stress.{index}" for index in range(len(limits))
    ]
    if breakeven is None:
        gates = tuple(inconclusive_gate(gate_id, "no_cost_no_trades", 0.0) for gate_id in gate_ids)
    else:
        value = float(breakeven)
        details["breakeven_cost_multiple"] = value
        gates = (
            compare_gate(
                profile, gate_ids[0], "breakeven_cost_multiple", value, floor, Direction.AT_LEAST
            ),
        ) + tuple(
            compare_gate(
                profile,
                gate_id,
                "breakeven_cost_multiple_vs_stress",
                value,
                limit,
                Direction.AT_LEAST,
            )
            for gate_id, limit in zip(gate_ids[1:], limits, strict=True)
        )
    return RobustnessCheck(
        check_id="cost_stress",
        principles=("C-R4",),
        gates=gates,
        thresholds=uses,
        details=details,
    )


# ======================================================================================
# C-S4 / C-R3: walk-forward window statistics (per-period report)
# ======================================================================================


def walk_forward_check(profile: ValidationProfile, returns: PeriodReturns) -> RobustnessCheck:
    """Positive-window fraction and single-window P&L share over the non-overlapping windows.

    Every non-overlapping Profile window counts (review fixes 2, 2026-09-25): a window without any
    return is reported (``periods = 0``, ``windows_without_returns``) and — the conservative
    choice — makes ``G4.walk_forward.positive_fraction`` ``INCONCLUSIVE``
    (``walk_forward_windows_without_returns``): the evidence does not cover the Profile's
    walk-forward (C-S4), so no fraction is computed over a shrunken denominator. Counting such a
    window as non-positive was rejected: it would turn missing evidence into a refutation
    (``FAIL`` → REJECTED, a terminal state). The P&L share is unaffected by empty windows (they add
    nothing to the maximum or the total) and is still computed over the windows with returns.
    """
    wf_limit = threshold(profile, "data_split.walk_forward.min_positive_window_fraction")
    share_limit = threshold(profile, "data_split.walk_forward.max_single_window_pnl_share")
    uses = (
        _use("min_positive_window_fraction", wf_limit),
        _use("max_single_window_pnl_share", share_limit),
    )
    rows: list[dict[str, object]] = []
    pnls: list[float] = []
    windows = walk_forward_windows(profile)
    disjoint = non_overlapping_windows(windows)  # overlapping tests would double-count periods
    empty = 0
    for window in disjoint:
        part = returns.window(window.train_end, window.test_end)
        if not len(part):
            empty += 1
            rows.append(
                {
                    "index": window.index,
                    "test_start": window.train_end.isoformat(),
                    "test_end": window.test_end.isoformat(),
                    "periods": 0,
                    "net_return": None,
                    "sharpe": None,
                }
            )
            continue
        pnl = float(sum(part.net(), Decimal(0)))
        pnls.append(pnl)
        rows.append(
            {
                "index": window.index,
                "test_start": window.train_end.isoformat(),
                "test_end": window.test_end.isoformat(),
                "periods": len(part),
                "net_return": pnl,
                "sharpe": _sharpe(part),
            }
        )
    fraction_id = "G4.walk_forward.positive_fraction"
    share_id = "G4.walk_forward.max_window_share"
    if not pnls:
        gates: tuple[GateResult, ...] = (
            inconclusive_gate(fraction_id, "no_walk_forward_window_with_returns", 0.0),
            inconclusive_gate(share_id, "no_walk_forward_window_with_returns", 0.0),
        )
    else:
        total = sum(pnls)
        if empty:
            fraction_gate = inconclusive_gate(
                fraction_id, "walk_forward_windows_without_returns", float(empty)
            )
        else:
            fraction_gate = compare_gate(
                profile,
                fraction_id,
                "positive_window_fraction",
                sum(1 for pnl in pnls if pnl > 0) / len(pnls),
                wf_limit,
                Direction.AT_LEAST,
            )
        if total <= 0:
            share_gate = inconclusive_gate(share_id, "total_window_pnl_not_positive", total)
        else:
            share_gate = compare_gate(
                profile,
                share_id,
                "max_window_pnl_share",
                max(pnls) / total,
                share_limit,
                Direction.AT_MOST,
            )
        gates = (fraction_gate, share_gate)
    return RobustnessCheck(
        check_id="walk_forward",
        principles=("C-S4", "C-R3"),
        gates=gates,
        thresholds=uses,
        details={
            "windows": rows,
            "window_selection": "non_overlapping_test_windows",
            "profile_windows": len(windows),
            "windows_skipped_overlapping": len(windows) - len(disjoint),
            "windows_without_returns": empty,
            "empty_window_rule": "positive_fraction INCONCLUSIVE when any window has no return",
        },
    )


# ======================================================================================
# C-R2: state decomposition with per-state effective sample size
# ======================================================================================


@dataclass(frozen=True)
class StateTrade:
    """One trade (holding interval) labelled with the market state known at its decision."""

    state: str
    start: datetime
    end: datetime
    net: Decimal


def state_decomposition_check(
    profile: ValidationProfile,
    trades: Sequence[StateTrade] | None,
    *,
    max_undersampled_share: Threshold | None,
) -> RobustnessCheck:
    """The result must not rest on states with too few effective independent trades.

    Structural gates: at least one state has enough effective trades, and the sufficient states
    alone are net profitable. The **share** of the net P&L that comes from undersampled states
    (``max(undersampled net, 0) / total net``) is always reported; it is gated
    (``G4.state.undersampled_pnl_share``) only against an explicit
    ``param:state.max_undersampled_pnl_share`` (the Profile has no such field). Without that
    parameter a positive share is ``profile_field_missing`` (INCONCLUSIVE): the result may rest on
    the undersampled states and nothing says how much is too much. A share of zero (the
    undersampled states add no profit) needs no threshold and adds no gate.
    """
    limit = threshold(profile, "sample_size.min_effective_trades_per_state")
    uses: list[ThresholdUse] = [_use("min_effective_trades_per_state", limit)]
    coverage_note = (
        "sample_size.min_regime_coverage is a free-form Profile string "
        f"({profile.sample_size.min_regime_coverage!r}); it is recorded, not machine-interpreted"
    )
    if trades is None:
        return RobustnessCheck(
            check_id="state_decomposition",
            principles=("C-R2",),
            gates=(inconclusive_gate("G4.state.sufficient_states", "state_labels_missing", 0.0),),
            thresholds=tuple(uses),
            note=coverage_note,
        )
    grouped: dict[str, list[StateTrade]] = {}
    for trade in trades:
        grouped.setdefault(trade.state, []).append(trade)
    rows: list[dict[str, object]] = []
    sufficient_net = Decimal(0)
    undersampled_net = Decimal(0)
    sufficient = 0
    for state in sorted(grouped):
        items = grouped[state]
        effective = effective_sample_size([(item.start, item.end) for item in items])
        net = sum((item.net for item in items), Decimal(0))
        enough = effective >= limit.value
        sufficient += enough
        if enough:
            sufficient_net += net
        else:
            undersampled_net += net
        rows.append(
            {
                "state": state,
                "trades": len(items),
                "effective_trades": effective,
                "net_return": float(net),
                "sufficient": enough,
            }
        )
    total_net = sufficient_net + undersampled_net
    share = float(max(undersampled_net, Decimal(0)) / total_net) if total_net > 0 else None
    details: dict[str, object] = {
        "states": rows,
        "sufficient_net_return": float(sufficient_net),
        "undersampled_net_return": float(undersampled_net),
        "undersampled_pnl_share": share,
    }
    missing: list[str] = []
    share_id = "G4.state.undersampled_pnl_share"
    if sufficient:
        gate_list: list[GateResult] = [
            flag_gate("G4.state.sufficient_states", "states_with_enough_trades", True, sufficient),
            flag_gate(
                "G4.state.pnl_outside_undersampled_states",
                "net_return_of_sufficient_states",
                sufficient_net > 0,
                float(sufficient_net),
            ),
        ]
        if max_undersampled_share is not None:
            uses.append(_use("max_undersampled_pnl_share", max_undersampled_share))
            if share is None:
                gate_list.append(
                    inconclusive_gate(share_id, "total_state_pnl_not_positive", float(total_net))
                )
            else:
                gate_list.append(
                    compare_gate(
                        profile,
                        share_id,
                        "undersampled_state_pnl_share",
                        share,
                        max_undersampled_share,
                        Direction.AT_MOST,
                    )
                )
        elif undersampled_net > 0:
            missing.append(UNDERSAMPLED_SHARE_PARAM)
            gate_list.append(
                missing_field_gate(share_id, UNDERSAMPLED_SHARE_PARAM, float(undersampled_net))
            )
        else:
            details["undersampled_share_not_gated"] = "undersampled states add no profit"
        gates = tuple(gate_list)
    else:
        gates = (
            inconclusive_gate("G4.state.sufficient_states", "states_with_enough_trades", 0.0),
            inconclusive_gate(
                "G4.state.pnl_outside_undersampled_states", "no_sufficient_state", 0.0
            ),
        )
    return RobustnessCheck(
        check_id="state_decomposition",
        principles=("C-R2",),
        gates=gates,
        thresholds=tuple(uses),
        missing_fields=tuple(missing),
        details=details,
        note=coverage_note,
    )


# ======================================================================================
# C-R5: capacity (turnover x participation vs volume) and impact estimate
# ======================================================================================


#: ADR-0064: the supplied bar volume and the executed bar's proven volume disagree.
BAR_VOLUME_SOURCE_MISMATCH: Final = "bar_volume_source_mismatch"


@dataclass(frozen=True)
class VolumeSourceMismatch:
    """ADR-0064: at one fill, ``bar_volume`` and the executed ``PriceBar.volume`` differ."""

    instrument: str
    time: datetime
    #: the separately bound ``ValidatorSetup.bar_volume`` value
    bar_volume: Decimal
    #: the proven ``PriceBar.volume`` of the executed bar (``ValidatorSetup.dataset_bars``)
    dataset_bars_volume: Decimal


@dataclass(frozen=True)
class CapacityFill:
    """One execution: traded notional as a fraction of equity, and its bar's traded notional."""

    time: datetime
    traded_fraction: Decimal
    bar_volume_notional: Decimal | None
    #: ADR-0064 (dataset path only): the two volume sources disagree at this fill
    source_mismatch: VolumeSourceMismatch | None = None


def capacity_check(
    profile: ValidationProfile,
    fills: Sequence[CapacityFill] | None,
    periods: int,
    *,
    max_participation: Threshold | None,
    min_capacity: Threshold | None,
    impact_coefficient: float | None,
    impact_coefficient_source: str = "param:capacity.impact_coefficient",
    impact_conflict: tuple[float, float] | None = None,
    impact_declared_source: str = "param:capacity.impact_coefficient",
    impact_model: str | None = None,
) -> RobustnessCheck:
    """Capacity = ``max_participation * min(bar volume / traded fraction)`` over the fills.

    The Profile contract has no capacity field: the participation limit and any required capacity
    are explicit parameters (``param:``) or missing. The impact estimate uses a square-root model
    ``coefficient * sqrt(participation)`` per unit traded, only when a coefficient is given.
    ``impact_coefficient_source`` records where the resolved coefficient came from (the caller's
    ``param:`` or, when a backtest execution model supplied it, ``"execution_model"``).

    Computing a number is never by itself a pass (ADR-0041 review fix): the estimates are
    reported in ``details`` only. ``G4.capacity.estimated`` exists only as ``INCONCLUSIVE`` when
    the capacity cannot be estimated; once it is, ``G4.capacity.required`` compares it with
    ``param:capacity.min_capacity`` (missing → ``profile_field_missing``, INCONCLUSIVE) and
    ``G4.capacity.impact_estimated`` is ``profile_field_missing`` when no impact coefficient is
    given (C-R5 requires the impact estimate as well).

    ``impact_conflict`` (ADR-0041 implementation note, 2026-09-26): the caller resolves it, never
    this function — when a backtest's execution model carries its own impact coefficient *and* an
    explicit ``param:capacity.impact_coefficient`` is also given and the two disagree, the caller
    passes both values here instead of a resolved ``impact_coefficient`` (which must then be
    ``None``): the impact estimate is not computed and ``G4.capacity.impact_estimated`` is
    ``INCONCLUSIVE`` with the named reason ``impact_coefficient_mismatch`` — never a silent choice
    of one value over the other.

    ADR-0052 §2: the participation limit, the required capacity and the impact coefficient may
    come from the Profile's ``capacity`` block (the caller resolves them; the ``Threshold.source``
    and ``impact_coefficient_source`` then name the Profile path). ``impact_declared_source`` names
    the declared coefficient in a conflict (``param:capacity.impact_coefficient`` or
    ``capacity.impact_coefficient``); ``impact_model`` is the Profile's ``capacity.impact_model``,
    which must be in ``IMPACT_MODELS`` (``None``: not given, the square-root law as before).

    ADR-0064 (dataset path): a traded fill carrying a ``source_mismatch`` (the supplied bar volume
    and the executed bar's proven volume differ) makes ``G4.capacity.estimated`` INCONCLUSIVE with
    ``bar_volume_source_mismatch`` -- before, and instead of, ``bar_volume_missing`` -- and nothing
    is computed from either source (no capacity, impact or ``G4.capacity.required``); ``details``
    record the mismatch count, the missing count and the first mismatch.
    """
    model_name = (
        None
        if impact_model is None
        else _method(impact_model, IMPACT_MODELS, "capacity.impact_model")
    )
    if impact_conflict is not None and impact_coefficient is not None:
        raise ValueError("capacity_check: pass either impact_coefficient or impact_conflict")
    missing: list[str] = []
    uses: list[ThresholdUse] = []
    details: dict[str, object] = {"periods": periods}
    if model_name is not None:
        details["impact_model"] = model_name
        details["impact_model_source"] = "capacity.impact_model"
    gates: list[GateResult] = []
    traded = [fill for fill in fills or () if fill.traded_fraction > 0]
    if fills is not None:
        turnover = float(sum((fill.traded_fraction for fill in traded), Decimal(0)))
        details["turnover_per_period"] = turnover / periods if periods else None
        details["fills"] = len(traded)
    mismatched = [fill.source_mismatch for fill in traded if fill.source_mismatch is not None]
    if max_participation is None:
        missing.append("capacity.max_participation_rate")
        gates.append(missing_field_gate("G4.capacity.estimated", "capacity.max_participation_rate"))
    elif mismatched:
        first = mismatched[0]
        details["bar_volume_source_mismatch"] = {
            "fills": len(mismatched),
            "missing": sum(1 for fill in traded if fill.bar_volume_notional is None),
            "first": {
                "instrument": first.instrument,
                "time": first.time.isoformat(),
                "bar_volume": str(first.bar_volume),
                "dataset_bars_volume": str(first.dataset_bars_volume),
            },
        }
        gates.append(
            inconclusive_gate(
                "G4.capacity.estimated", BAR_VOLUME_SOURCE_MISMATCH, float(len(mismatched))
            )
        )
    elif fills is None or any(fill.bar_volume_notional is None for fill in traded):
        gates.append(inconclusive_gate("G4.capacity.estimated", "bar_volume_missing", 0.0))
    elif not traded:
        gates.append(inconclusive_gate("G4.capacity.estimated", "no_trades", 0.0))
    else:
        uses.append(_use("max_participation_rate", max_participation))
        ratios = [
            float(fill.bar_volume_notional) / float(fill.traded_fraction)
            for fill in traded
            if fill.bar_volume_notional is not None
        ]
        capacity = max_participation.value * min(ratios)
        details["capacity"] = capacity  # an estimate, reported only (not a gate)
        if impact_conflict is not None:
            explicit_value, model_value = impact_conflict
            details["impact_cost_per_period_at_capacity"] = None
            details["impact_not_estimated"] = "impact_coefficient_mismatch"
            declared_key = (
                "param_capacity_impact_coefficient"
                if impact_declared_source.startswith("param:")
                else "profile_capacity_impact_coefficient"
            )
            details["impact_coefficient_conflict"] = {
                declared_key: explicit_value,
                "execution_model_impact_coefficient": model_value,
            }
            gates.append(
                inconclusive_gate(
                    "G4.capacity.impact_estimated",
                    "impact_coefficient_mismatch",
                    abs(explicit_value - model_value),
                )
            )
        elif impact_coefficient is not None:
            impact = sum(
                impact_coefficient * (capacity / ratio) ** 0.5 * float(fill.traded_fraction)
                for fill, ratio in zip(traded, ratios, strict=True)
            )
            details["impact_cost_per_period_at_capacity"] = impact / periods if periods else None
            details["impact_coefficient_source"] = impact_coefficient_source
        else:
            details["impact_cost_per_period_at_capacity"] = None
            details["impact_not_estimated"] = "param:capacity.impact_coefficient not given"
            missing.append("capacity.impact_coefficient")
            gates.append(
                missing_field_gate("G4.capacity.impact_estimated", "capacity.impact_coefficient")
            )
        if min_capacity is None:
            missing.append("capacity.min_capacity")
            gates.append(missing_field_gate("G4.capacity.required", "capacity.min_capacity"))
        else:
            uses.append(_use("min_capacity", min_capacity))
            gates.append(
                compare_gate(
                    profile,
                    "G4.capacity.required",
                    "capacity_notional",
                    capacity,
                    min_capacity,
                    Direction.AT_LEAST,
                )
            )
    return RobustnessCheck(
        check_id="capacity",
        principles=("C-R5",),
        gates=tuple(gates),
        thresholds=tuple(uses),
        missing_fields=tuple(missing),
        details=details,
    )


# ======================================================================================
# C-R3: cross-asset consistency inside the declared scope
# ======================================================================================

#: ``G4.cross_asset.positive_fraction`` reason when every declared instrument's single-asset
#: re-run holds no position at all (ADR-0059 C): the per-asset fraction is not computed.
ZERO_EXPOSURE_SINGLE_ASSET: Final = "not_applicable_zero_exposure_single_asset"
#: ``G4.cross_asset.positive_fraction`` reason of a declared cross-sectional strategy whose
#: declared instruments cannot be split into two or more sub-universes (ADR-0059 A).
NOT_ENOUGH_FOR_SUBUNIVERSES: Final = "not_enough_instruments_for_subuniverses"
#: The recorded partition rule of ``subuniverse_partition`` (ADR-0059 A).
SUBUNIVERSE_RULE: Final = "sorted_unique_consecutive_pairs_odd_remainder_joins_last"
#: The smallest cross-section: a ranking between instruments needs at least two of them. This is
#: the structural definition of a cross-section (ADR-0059 A), not a calibrated number.
MIN_CROSS_SECTION: Final = 2


@dataclass(frozen=True)
class SubUniverse:
    """One sub-universe re-run of a declared cross-sectional strategy (ADR-0059 A).

    ``exposed`` is whether the re-run ever held a position (``research.strategies.validation``:
    a non-flat target or a fill); it is reported, the gate judges the net return.
    """

    instruments: tuple[str, ...]
    returns: PeriodReturns
    exposed: bool


def subuniverse_partition(declared: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    """The deterministic partition of ``declared`` into disjoint sub-universes (ADR-0059 A).

    Rule ``SUBUNIVERSE_RULE``: the distinct names, sorted, cut into consecutive pairs; with an odd
    count the last name joins the last pair (so every sub-universe has at least
    ``MIN_CROSS_SECTION`` instruments and every declared instrument is in exactly one). Fewer than
    two sub-universes possible (fewer than four instruments) → ``()``: C-R3 cannot be tested this
    way, never a PASS.
    """
    names = sorted(set(declared))
    count = len(names) // MIN_CROSS_SECTION
    if count < 2:
        return ()
    chunks = [
        tuple(names[i * MIN_CROSS_SECTION : (i + 1) * MIN_CROSS_SECTION]) for i in range(count)
    ]
    chunks[-1] = tuple(names[(count - 1) * MIN_CROSS_SECTION :])
    return tuple(chunks)


def _all_zero_exposure(declared: Sequence[str], exposed: Mapping[str, bool] | None) -> bool:
    """ADR-0059 C: every declared instrument's single-asset re-run is known to hold no position
    (unknown exposure — no mapping or a missing name — is never assumed to be zero)."""
    if exposed is None:
        return False
    return all(name in exposed and not exposed[name] for name in declared)


def _subuniverse_gate(
    profile: ValidationProfile,
    declared: Sequence[str],
    absent: Sequence[str],
    sub_universes: tuple[SubUniverse, ...],
    min_positive_fraction: Threshold | None,
    missing: list[str],
    uses: list[ThresholdUse],
) -> GateResult:
    """``G4.cross_asset.positive_fraction`` of a declared cross-sectional strategy (ADR-0059 A)."""
    gate_id = "G4.cross_asset.positive_fraction"
    if min_positive_fraction is None:
        missing.append("cross_asset.min_positive_fraction")
        return missing_field_gate(gate_id, "cross_asset.min_positive_fraction")
    if absent:
        return inconclusive_gate(gate_id, "declared_instruments_not_tested", float(len(absent)))
    if len(sub_universes) < 2:
        return inconclusive_gate(gate_id, NOT_ENOUGH_FOR_SUBUNIVERSES, float(len(set(declared))))
    uses.append(_use("min_positive_fraction", min_positive_fraction))
    positive = sum(1 for item in sub_universes if sum(item.returns.net(), Decimal(0)) > 0)
    return compare_gate(
        profile,
        gate_id,
        "positive_subuniverse_fraction",
        positive / len(sub_universes),
        min_positive_fraction,
        Direction.AT_LEAST,
    )


def cross_asset_check(
    profile: ValidationProfile,
    per_asset: Mapping[str, PeriodReturns],
    declared: Sequence[str],
    min_positive_fraction: Threshold | None,
    *,
    exposed: Mapping[str, bool] | None = None,
    sub_universes: tuple[SubUniverse, ...] | None = None,
) -> RobustnessCheck:
    """Every declared instrument must be tested; with two or more, the sign must be consistent.

    The Profile scope is one symbol and has no cross-asset field: the consistency threshold is an
    explicit parameter (``param:cross_asset.min_positive_fraction``) or missing.

    ADR-0059 (Accepted 2026-09-26); both keyword arguments default to ``None`` = the behaviour
    before it, byte for byte:

    - ``exposed`` (C): per declared instrument, whether its single-asset re-run ever held a
      position. When every declared instrument's re-run is known to hold none, the per-asset
      fraction says nothing about the strategy: ``G4.cross_asset.positive_fraction`` is
      ``INCONCLUSIVE`` (``ZERO_EXPOSURE_SINGLE_ASSET``) instead of a structural FAIL. Only the
      exposure decides this, never zero returns; unknown exposure is never assumed to be zero;
    - ``sub_universes`` (A): given only for a strategy **declared** cross-sectional
      (``research.strategies.cross_section``; never inferred from results). Its sub-universe
      re-runs must be exactly ``subuniverse_partition(declared)`` (else ``ValueError``); the
      fraction of sub-universes with a positive net return is judged against the **same**
      ``min_positive_fraction`` (``()`` → ``INCONCLUSIVE`` ``NOT_ENOUGH_FOR_SUBUNIVERSES``). The
      per-asset rows are still reported; they do not gate a cross-sectional strategy.
    """
    if sub_universes is not None:
        expected = subuniverse_partition(declared)
        if tuple(item.instruments for item in sub_universes) != expected:
            raise ValueError(
                f"sub-universe re-runs must follow {SUBUNIVERSE_RULE}: expected {expected}"
            )
    if not declared:  # C-R3: the result must be tested inside a *declared* scope
        return RobustnessCheck(
            check_id="cross_asset",
            principles=("C-R3",),
            gates=(
                configuration_missing_gate("G4.cross_asset.scope_covered", "declared_instruments"),
            ),
            details={"declared": [], "instruments": [], "not_tested": []},
            note="no declared instrument scope (INCONCLUSIVE)",
        )
    rows = [
        {
            "instrument": name,
            "periods": len(per_asset[name]),
            "net_return": float(sum(per_asset[name].net(), Decimal(0))),
            "sharpe": _sharpe(per_asset[name]),
        }
        for name in sorted(per_asset)
    ]
    absent = [name for name in declared if name not in per_asset]
    gates: list[GateResult] = [
        flag_gate("G4.cross_asset.scope_covered", "declared_instruments_tested", True, 1.0)
        if not absent
        else inconclusive_gate(
            "G4.cross_asset.scope_covered", "declared_instruments_not_tested", float(len(absent))
        )
    ]
    missing: list[str] = []
    uses: list[ThresholdUse] = []
    details: dict[str, object] = {
        "declared": list(declared),
        "instruments": rows,
        "not_tested": absent,
    }
    note = ""
    if sub_universes is not None:
        gates.append(
            _subuniverse_gate(
                profile, declared, absent, sub_universes, min_positive_fraction, missing, uses
            )
        )
        details["cross_section"] = {
            "declared_cross_sectional": True,
            "rule": SUBUNIVERSE_RULE,
            "sub_universes": [
                {
                    "instruments": list(item.instruments),
                    "periods": len(item.returns),
                    "net_return": float(sum(item.returns.net(), Decimal(0))),
                    "sharpe": _sharpe(item.returns),
                    "exposed": item.exposed,
                }
                for item in sub_universes
            ],
        }
        note = "declared cross-sectional: C-R3 judged over disjoint sub-universes (ADR-0059 A)"
    elif len(declared) >= 2:
        if min_positive_fraction is None:
            missing.append("cross_asset.min_positive_fraction")
            gates.append(
                missing_field_gate(
                    "G4.cross_asset.positive_fraction", "cross_asset.min_positive_fraction"
                )
            )
        elif absent:
            gates.append(
                inconclusive_gate(
                    "G4.cross_asset.positive_fraction",
                    "declared_instruments_not_tested",
                    float(len(absent)),
                )
            )
        elif _all_zero_exposure(declared, exposed):
            gates.append(
                inconclusive_gate(
                    "G4.cross_asset.positive_fraction",
                    ZERO_EXPOSURE_SINGLE_ASSET,
                    float(len(declared)),
                )
            )
            details["zero_exposure"] = {
                "rule": "no single-asset re-run held a position (no non-flat target, no fill)",
                "instruments": sorted(declared),
            }
            note = "every single-asset re-run is flat: the per-asset fraction is not applicable"
        else:
            uses.append(_use("min_positive_fraction", min_positive_fraction))
            positive = sum(1 for name in declared if sum(per_asset[name].net(), Decimal(0)) > 0)
            gates.append(
                compare_gate(
                    profile,
                    "G4.cross_asset.positive_fraction",
                    "positive_instrument_fraction",
                    positive / len(declared),
                    min_positive_fraction,
                    Direction.AT_LEAST,
                )
            )
    return RobustnessCheck(
        check_id="cross_asset",
        principles=("C-R3",),
        gates=tuple(gates),
        thresholds=tuple(uses),
        missing_fields=tuple(missing),
        details=details,
        note=note,
    )

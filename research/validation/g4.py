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

``RobustnessInput.execution_impact_coefficient`` (implementation note, 2026-09-26): when the
candidate's backtest used a ``plugins.backtest.execution.ExecutionModel`` with its own impact
coefficient, ``research.strategies.validation`` passes it here; ``run_robustness`` then resolves
the capacity check's coefficient itself (``_resolved_impact``) — the model's value takes
priority, and a disagreeing explicit ``RobustnessParams.impact_coefficient`` is never silently
overridden: it becomes ``G4.capacity.impact_estimated`` = ``INCONCLUSIVE`` (metric
``impact_coefficient_mismatch``). ``None`` (every caller that predates this) is unaffected.

Exact comparison (ADR-0041 implementation note, review fixes 3, 2026-09-26): the two coefficients
are compared as ``Decimal`` values, never through ``float``. The model's coefficient is passed as
its own ``Decimal``; an explicit ``float`` parameter is read as its exact text (``repr``, the
shortest text that round-trips, e.g. ``0.1`` -> ``Decimal("0.1")``), an ``int`` or a ``Decimal``
as itself. So ``0.1`` and ``Decimal("0.10")`` agree, while a model coefficient that differs from
the parameter only beyond ``float`` precision is a mismatch (a ``float`` round trip used to hide
it). Only the resolved value handed to ``capacity_check`` (a ``float`` estimate) is converted.

Check isolation (debugging pass, 2026-09-26; CODE_COMPLETE / DEBUG_PENDING): ``run_robustness``
runs each check on its own. An **unexpected** exception inside one check neither aborts the
suite nor passes: that check becomes a ``RobustnessCheck`` with one ``INCONCLUSIVE`` gate
``<gate prefix>.check_error`` (metric ``check_error:<exception type>``) and the exception type
plus a short, deterministic message (memory addresses masked, cut to ``_ERROR_MESSAGE_CHARS``
characters) in its ``details``; every other check still runs, so the G4 verdict is at best
``INCONCLUSIVE``. **Deliberate refusals still raise** (``_PROPAGATED``): ``ValueError`` — which
includes ``UnsupportedMethod`` (an unimplemented Profile method), ``ProfileFieldMissing`` and the
documented input refusals (e.g. trials on different period grids) — and ``TypeError`` are
configuration / input-contract errors of the caller, not evidence about the candidate, and have
always been raised (tests pin them); ``MemoryError`` is a resource failure whose occurrence is not
reproducible, so recording it would make the result depend on the machine. Anything else
(arithmetic, lookup, attribute, runtime, assertion errors, ...) is the broken-check case.
Nothing changes when no check raises: the checks, their gates and ``to_dict`` are identical.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Final

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
    "CHECKS",
    "CHECK_ERROR",
    "RobustnessInput",
    "RobustnessParams",
    "RobustnessResult",
    "ValidationRun",
    "check_error",
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
    #: The longest span one period's return shares a label or a position with later periods:
    #: the larger of the Outcome label horizon and the longest holding period of the evaluated
    #: trades. Required (no default): the CSCV purge is at least this wide (review fixes 2).
    holding_horizon: timedelta
    #: The backtest's execution model's own impact coefficient (ADR-0038 execution note), when
    #: the candidate was backtested with one; ``None`` when there is none (default — every
    #: existing caller is unaffected). ``capacity_check`` must use it instead of, and never
    #: silently alongside, a differing explicit ``params.impact_coefficient`` (implementation
    #: note, 2026-09-26): see ``run_robustness``.
    #: A ``Decimal`` (the model's own value) is compared exactly (review fixes 3).
    execution_impact_coefficient: Decimal | float | None = None

    def __post_init__(self) -> None:
        if self.family_trial_count < 1:
            raise ValueError("family_trial_count must be >= 1")
        if self.holding_horizon < timedelta(0):
            raise ValueError("holding_horizon must be >= 0")
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


def _exact(value: Decimal | float | int, name: str) -> Decimal:
    """``value`` as an exact ``Decimal`` (module docs, **Exact comparison**): a ``float`` is read
    as its shortest round-trip text, never as its binary expansion."""
    if isinstance(value, bool) or not isinstance(value, Decimal | float | int):
        raise TypeError(f"{name} must be a Decimal, float or int, not {type(value).__name__}")
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(value)


def _resolved_impact(
    params: RobustnessParams, model_coefficient: Decimal | float | None
) -> tuple[float | None, str, tuple[float, float] | None]:
    """``(coefficient to use, its source, conflict)`` for ``capacity_check`` (implementation note,
    2026-09-26): the execution model's coefficient takes priority whenever the backtest carries
    one; an explicit ``params.impact_coefficient`` that disagrees with it is never overridden
    silently — it is reported as a conflict instead, and neither value is used. The two are
    compared exactly (module docs, **Exact comparison**, review fixes 3)."""
    explicit = params.impact_coefficient
    if explicit is not None and model_coefficient is not None:
        if _exact(explicit, "impact_coefficient") != _exact(
            model_coefficient, "execution_impact_coefficient"
        ):
            return None, "", (float(explicit), float(model_coefficient))
    if model_coefficient is not None:
        return float(model_coefficient), "execution_model", None
    return (
        None if explicit is None else float(explicit),
        "param:capacity.impact_coefficient",
        None,
    )


#: Exceptions a check raises deliberately (configuration / input-contract refusals) or that are
#: not reproducible (resources): they propagate out of ``run_robustness`` (module docs, **Check
#: isolation**). ``UnsupportedMethod`` and ``ProfileFieldMissing`` are ``ValueError`` subclasses.
_PROPAGATED: Final = (ValueError, TypeError, MemoryError)
#: Metric prefix of the ``INCONCLUSIVE`` gate of a check that raised unexpectedly.
CHECK_ERROR: Final = "check_error"
#: Report formatting only (not a validation threshold): how much of the message is recorded.
_ERROR_MESSAGE_CHARS: Final = 200
_ADDRESS = re.compile(r"0x[0-9A-Fa-f]+")

#: check id -> (principles, gate-id prefix) of every G4 check, in ``run_robustness`` order; the
#: same ids / principles / prefixes the checks themselves use (``robustness`` module docs).
CHECKS: Final[tuple[tuple[str, tuple[str, ...], str], ...]] = (
    ("overfitting", ("C-T1", "C-R1"), "G4.overfitting"),
    ("parameter_neighborhood", ("C-R1",), "G4.param_neighborhood"),
    ("time_alignment", ("C-R1",), "G4.time_alignment"),
    ("delay_stress", ("C-R4",), "G4.delay_stress"),
    ("cost_stress", ("C-R4",), "G4.cost_stress"),
    ("walk_forward", ("C-S4", "C-R3"), "G4.walk_forward"),
    ("state_decomposition", ("C-R2",), "G4.state"),
    ("capacity", ("C-R5",), "G4.capacity"),
    ("cross_asset", ("C-R3",), "G4.cross_asset"),
)


def _error_message(error: BaseException) -> str:
    """A deterministic, short rendering of ``error`` (memory addresses masked, one line)."""
    text = " ".join(_ADDRESS.sub("0x?", str(error)).split())
    return text[:_ERROR_MESSAGE_CHARS]


def check_error(check_id: str, error: Exception) -> RobustnessCheck:
    """The ``INCONCLUSIVE`` stand-in of a check that raised ``error`` unexpectedly."""
    spec = next((item for item in CHECKS if item[0] == check_id), None)
    if spec is None:
        raise ValueError(f"unknown G4 check {check_id!r}")
    _, principles, prefix = spec
    kind = type(error).__name__
    return RobustnessCheck(
        check_id=check_id,
        principles=principles,
        gates=(inconclusive_gate(f"{prefix}.{CHECK_ERROR}", f"{CHECK_ERROR}:{kind}", 0.0),),
        details={"check_error": {"type": kind, "message": _error_message(error)}},
        note=f"{CHECK_ERROR}: the check raised {kind}; recorded as INCONCLUSIVE, never a PASS",
    )


def _isolated(check_id: str, run: Callable[[], RobustnessCheck]) -> RobustnessCheck:
    try:
        return run()
    except _PROPAGATED:
        raise
    except Exception as error:  # noqa: BLE001 - a broken check is INCONCLUSIVE (module docs)
        return check_error(check_id, error)


def run_robustness(inp: RobustnessInput) -> RobustnessResult:
    """Every G4 check in a fixed order (C-T1 / C-R1, C-R1, C-R4, C-S4 / C-R3, C-R2, C-R5, C-R3);
    each one isolated (module docs, **Check isolation**)."""
    profile, params = inp.profile, inp.params
    coefficient, impact_source, impact_conflict = _resolved_impact(
        params, inp.execution_impact_coefficient
    )
    key = param_key(inp.chosen)
    chosen = next((trial for trial in inp.trials if trial.key() == key), None)
    if chosen is None:
        raise ValueError(f"no trial holds the chosen parameters {dict(inp.chosen)}")
    returns = chosen.returns
    runs: dict[str, Callable[[], RobustnessCheck]] = {
        "overfitting": lambda: overfitting_check(
            profile,
            inp.trials,
            inp.chosen,
            inp.family_trial_count,
            params.cscv_partitions,
            horizon=inp.holding_horizon,
        ),
        "parameter_neighborhood": lambda: parameter_neighborhood_check(
            profile, inp.trials, inp.chosen, inp.param_space
        ),
        "time_alignment": lambda: time_alignment_check(profile, returns, inp.time_shifted),
        "delay_stress": lambda: delay_stress_check(profile, inp.delayed),
        "cost_stress": lambda: cost_stress_check(profile, returns),
        "walk_forward": lambda: walk_forward_check(profile, returns),
        "state_decomposition": lambda: state_decomposition_check(
            profile, inp.state_trades, max_undersampled_share=params.undersampled_share
        ),
        "capacity": lambda: capacity_check(
            profile,
            inp.capacity_fills,
            len(returns),
            max_participation=params.max_participation,
            min_capacity=params.capacity_floor,
            impact_coefficient=coefficient,
            impact_coefficient_source=impact_source,
            impact_conflict=impact_conflict,
        ),
        "cross_asset": lambda: cross_asset_check(
            profile, inp.per_asset, inp.declared_instruments, params.cross_asset_fraction
        ),
    }
    checks = tuple(_isolated(check_id, runs[check_id]) for check_id, _, _ in CHECKS)
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

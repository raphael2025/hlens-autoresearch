"""Minimal Validation Pipeline: G0 – G3 in sample, G5 sealed OOS (ADR-0037; 07-validation §2).

Stages follow the 07-validation.md flow; a stage with any ``FAIL`` stops the in-sample pipeline
(the flow sends it to the Failure Registry), ``INCONCLUSIVE`` does not stop it. The overall verdict
is always ``derive_verdict(gates)`` (ADR-0013); there is no other way to set it.

Stages, gate ids and threshold sources:

- **G0 reproducibility, data & contract** (structural, no threshold): ``G0.bindings`` (Profile /
  cost model / outcome / experiment bindings agree), ``G0.run_state``, ``G0.data_available``,
  ``G0.reproducibility`` (re-run hash equals the recorded hash), ``G0.signal_determinism``;
- **G1 leakage**: ``G1.outcome_not_input`` (C-L2), ``G1.label_blind_sides`` (sides computed with
  the real labels equal the sides computed with blinded labels; see ``controls``),
  ``G1.embargo_covers_horizon`` (C-L5), ``G1.sealed_oos_excluded`` (C-S2), ``G1.shuffle_control``
  / ``G1.shift_control`` (C-L6; ``significance.multiple_testing_threshold``);
- **G2 in-sample statistics with cost** run only on the **walk-forward test folds** of the Profile
  (``splits.walk_forward_folds``: research window only, purged and embargoed training sets; a
  ``FittableStudy`` is fitted per fold on its training labels): ``G2.walk_forward_folds``
  (structural), ``G2.effective_sample_size`` (C-T2;
  ``sample_size.min_effective_trades_in_sample``), ``G2.breakeven_cost_multiple`` and
  ``G2.cost_stress.<i>`` (C-R4 / A6; ``cost_stress.*``), ``G2.cost_report.<i>`` (reported only),
  ``G2.null_model_percentile`` (C-T4; ``benchmark.null_model_percentile``);
- **G3 multiple-testing adjusted significance**: ``G3.adjusted_p_value`` (C-T1 / C-T3;
  ``significance.multiple_testing_threshold``);
- **G5 sealed OOS**: ``G5.unsealing_recorded``, ``G5.oos_effective_sample_size``
  (``sample_size.min_effective_trades_out_of_sample``), ``G5.oos_breakeven_cost_multiple``
  (``cost_stress.min_breakeven_cost_multiple``).

G4 (robustness, Phase 8) lives in ``research.validation.robustness`` and is composed after G3 by
``research.validation.g4.run_validation``. Every side this module uses is computed from blinded
labels (``controls.blind_labels``). Too few effective samples is ``INCONCLUSIVE`` (evidence
insufficient), never a PASS.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.contracts.cost_model import CostModelSpec
from core.contracts.outcome import OutcomeLabel, OutcomeLabelSpec
from core.contracts.profile_selection import ExperimentMetadata
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import Ref
from core.domain.research import (
    ExperimentRun,
    FailureRecord,
    GateResult,
    RunState,
    ValidationReport,
    Verdict,
    derive_verdict,
)
from core.domain.specs import STRATEGY_SIGNAL_KINDS
from core.errors import ReasonCode
from research.outcomes.table import OutcomeTable
from research.validation.controls import (
    FittableStudy,
    SignalStudy,
    blind_labels,
    shift_control,
    shuffle_control,
)
from research.validation.costs import (
    breakeven_cost_multiple,
    cost_model_is_bound,
    multiplier,
    net_returns,
)
from research.validation.gates import (
    Direction,
    compare_gate,
    flag_gate,
    inconclusive_gate,
    threshold,
)
from research.validation.sealed_oos import SealedOosVault, SealedWindow
from research.validation.splits import LabeledSpan, walk_forward_folds
from research.validation.stats import (
    UnsupportedMethod,
    adjust_p_value,
    effective_sample_size,
    hac_t_test,
    overlap_lag,
)

__all__ = [
    "InSampleInput",
    "SealedOosInput",
    "ValidationContext",
    "build_report",
    "failure_record",
    "reason_for_gate",
    "run_in_sample",
    "run_sealed_oos",
]

#: Null-model names this pipeline implements (random entries with the same trade count and the
#: same long / short mix, C-T4). Any other Profile ``benchmark.null_model`` is refused.
SUPPORTED_NULL_MODELS = frozenset({"random-entry", "random_entry"})


@dataclass(frozen=True)
class ValidationContext:
    """What a report binds: the run, its rule versions, the cost model and the label spec."""

    report_id: str
    subject: Ref
    run: ExperimentRun
    metadata: ExperimentMetadata
    profile: ValidationProfile
    cost_model: CostModelSpec
    label_spec: OutcomeLabelSpec


@dataclass(frozen=True)
class InSampleInput:
    context: ValidationContext
    outcomes: OutcomeTable
    study: SignalStudy
    seed: int
    reproduce: Callable[[], str]
    recorded_result_hash: str


@dataclass(frozen=True)
class SealedOosInput:
    context: ValidationContext
    vault: SealedOosVault
    outcomes: OutcomeTable
    study: SignalStudy


def _span(label: OutcomeLabel) -> LabeledSpan:
    end = label.available_time if label.available_time is not None else label.event_time
    return LabeledSpan(key=label.event_key, start=label.event_time, end=end)


def _computable(labels: Sequence[OutcomeLabel]) -> list[OutcomeLabel]:
    items = [label for label in labels if label.value is not None]
    return sorted(items, key=lambda label: (label.event_time, label.event_key))


def _values(labels: Sequence[OutcomeLabel]) -> tuple[Decimal, ...]:
    return tuple(label.value for label in labels if label.value is not None)


def _intervals(labels: Sequence[OutcomeLabel]) -> list[tuple[datetime, datetime]]:
    return [(span.start, span.end) for span in map(_span, labels)]


# ======================================================================================
# G0 reproducibility, data & contract
# ======================================================================================


def _binding_mismatches(ctx: ValidationContext, outcomes: OutcomeTable) -> list[str]:
    run, meta, profile = ctx.run, ctx.metadata, ctx.profile
    repro = run.repro
    profile_hash = profile.content_hash()
    checks = {
        "profile_hash(run)": repro.validation_profile_hash == profile_hash,
        "profile_hash(metadata)": meta.validation_profile_hash == profile_hash,
        "profile_ref(run)": repro.validation_profile.target_identity()
        == profile.ref.target_identity(),
        "profile_ref(metadata)": meta.validation_profile.target_identity()
        == profile.ref.target_identity(),
        "experiment_hash": meta.experiment_hash == run.experiment_hash,
        "cost_model(profile)": cost_model_is_bound(ctx.cost_model, profile),
        "cost_model(run)": repro.cost_model_ref.target_identity()
        == ctx.cost_model.ref.target_identity(),
        "cost_model_hash": repro.dependency_hashes.get(str(repro.cost_model_ref))
        == ctx.cost_model.content_hash(),
        "outcome(run)": repro.outcome_ref is not None
        and repro.outcome_ref.target_identity() == ctx.label_spec.outcome.target_identity(),
        "outcome_spec_hash": repro.outcome_ref is not None
        and repro.dependency_hashes.get(str(repro.outcome_ref)) == ctx.label_spec.outcome_spec_hash,
        "outcome_table": outcomes.outcome.target_identity()
        == ctx.label_spec.outcome.target_identity()
        and outcomes.label_spec_hash == ctx.label_spec.content_hash(),
    }
    return sorted(name for name, ok in checks.items() if not ok)


def _g0(inp: InSampleInput, labels: Sequence[OutcomeLabel]) -> list[GateResult]:
    ctx = inp.context
    mismatches = _binding_mismatches(ctx, inp.outcomes)
    gates = [
        flag_gate("G0.bindings", "binding_mismatch_count", not mismatches, float(len(mismatches))),
        flag_gate(
            "G0.run_state",
            "run_completed",
            ctx.run.state in {RunState.COMPLETED, RunState.VALIDATING},
            1.0 if ctx.run.state in {RunState.COMPLETED, RunState.VALIDATING} else 0.0,
        ),
    ]
    if labels:
        gates.append(flag_gate("G0.data_available", "computable_labels", True, float(len(labels))))
    else:
        gates.append(inconclusive_gate("G0.data_available", "computable_labels", 0.0))
    reproduced = inp.reproduce() == inp.recorded_result_hash
    gates.append(
        flag_gate("G0.reproducibility", "rerun_hash_equal", reproduced, 1.0 if reproduced else 0.0)
    )
    keys, blind = tuple(label.event_key for label in labels), blind_labels(len(labels))
    stable = inp.study.sides(keys, blind) == inp.study.sides(keys, blind)
    gates.append(
        flag_gate("G0.signal_determinism", "sides_equal_on_rerun", stable, 1.0 if stable else 0.0)
    )
    return gates


# ======================================================================================
# G1 leakage
# ======================================================================================


def _g1(inp: InSampleInput, labels: Sequence[OutcomeLabel]) -> list[GateResult]:
    ctx, profile = inp.context, inp.context.profile
    bad = [ref for ref in inp.study.signal_refs if ref.kind not in STRATEGY_SIGNAL_KINDS]
    keys, values = tuple(label.event_key for label in labels), _values(labels)
    blinded = inp.study.sides(keys, blind_labels(len(keys)))
    seen = inp.study.sides(keys, values)
    moved = sum(1 for a, b in zip(blinded, seen, strict=True) if a != b)
    gates = [
        flag_gate("G1.outcome_not_input", "non_signal_input_refs", not bad, float(len(bad))),
        flag_gate(
            "G1.label_blind_sides", "sides_changed_by_label_values", moved == 0, float(moved)
        ),
    ]
    slack = profile.data_split.embargo - ctx.label_spec.horizon
    gates.append(
        flag_gate(
            "G1.embargo_covers_horizon",
            "embargo_minus_horizon_seconds",
            slack.total_seconds() >= 0,
            slack.total_seconds(),
        )
    )
    window = SealedWindow.from_profile(profile)
    touching = sum(1 for label in inp.outcomes if window.touches(_span(label)))
    gates.append(
        flag_gate("G1.sealed_oos_excluded", "labels_touching_sealed_oos", touching == 0, touching)
    )
    lag = overlap_lag(_intervals(labels))
    alpha = threshold(profile, "significance.multiple_testing_threshold")
    for gate_id, control in (
        ("G1.shuffle_control", shuffle_control(inp.study, keys, values, lag, inp.seed)),
        ("G1.shift_control", shift_control(inp.study, keys, values, lag, inp.seed + 1)),
    ):
        metric = f"{control.name}_timing_p_value"
        if not control.applicable or len(values) < 2:
            gates.append(inconclusive_gate(gate_id, metric, control.p_value))
        else:
            gates.append(
                compare_gate(profile, gate_id, metric, control.p_value, alpha, Direction.AT_LEAST)
            )
    return gates


# ======================================================================================
# G2 in-sample statistics with cost / G3 multiple-testing adjusted significance
# ======================================================================================


@dataclass(frozen=True)
class _Trades:
    labels: tuple[OutcomeLabel, ...]
    sides: tuple[int, ...]
    gross: tuple[Decimal, ...]

    @property
    def gross_mean(self) -> Decimal:
        return sum(self.gross, Decimal(0)) / len(self.gross)


def _trades(labels: Sequence[OutcomeLabel], sides: Sequence[int]) -> _Trades:
    picked = [(label, side) for label, side in zip(labels, sides, strict=True) if side != 0]
    return _Trades(
        labels=tuple(label for label, _ in picked),
        sides=tuple(side for _, side in picked),
        gross=tuple(side * label.value for label, side in picked if label.value is not None),
    )


def _sample_gate(
    profile: ValidationProfile, gate_id: str, trades: _Trades, field: str
) -> GateResult:
    effective = effective_sample_size(_intervals(trades.labels))
    gate = compare_gate(
        profile,
        gate_id,
        "effective_independent_trades",
        float(effective),
        threshold(profile, field),
        Direction.AT_LEAST,
    )
    if gate.verdict is Verdict.FAIL:  # too little evidence is not a refutation
        return gate.model_copy(update={"verdict": Verdict.INCONCLUSIVE})
    return gate


def _null_percentile(
    profile: ValidationProfile,
    pool: Sequence[Decimal],
    trades: _Trades,
    seed: int,
) -> float | None:
    if profile.benchmark.null_model.strip().lower() not in SUPPORTED_NULL_MODELS:
        raise UnsupportedMethod(f"null_model {profile.benchmark.null_model!r} is not implemented")
    n = len(trades.gross)
    if len(pool) < n:
        return None
    rng = random.Random(seed)
    observed = trades.gross_mean
    below = equal = 0
    sims = profile.benchmark.null_model_simulations
    for _ in range(sims):
        picks = rng.sample(range(len(pool)), n)
        sides = rng.sample(trades.sides, n)
        null_mean = (
            sum((side * pool[index] for side, index in zip(sides, picks, strict=True)), Decimal(0))
            / n
        )
        if null_mean < observed:
            below += 1
        elif null_mean == observed:
            equal += 1
    return 100.0 * (below + 0.5 * equal) / sims


def _g2(inp: InSampleInput, split: _Evaluation) -> list[GateResult]:
    ctx, profile = inp.context, inp.context.profile
    labels, trades = split.labels, split.trades
    folds = float(split.folds)
    gates = [
        flag_gate("G2.walk_forward_folds", "walk_forward_test_folds", True, folds)
        if split.folds and labels
        else inconclusive_gate("G2.walk_forward_folds", "walk_forward_test_folds", folds),
        _sample_gate(
            profile,
            "G2.effective_sample_size",
            trades,
            "sample_size.min_effective_trades_in_sample",
        ),
    ]
    if len(trades.gross) < 2:
        for gate_id in ("G2.breakeven_cost_multiple", "G2.null_model_percentile"):
            gates.append(inconclusive_gate(gate_id, "too_few_trades", float(len(trades.gross))))
        return gates
    breakeven = float(breakeven_cost_multiple(trades.gross_mean, ctx.cost_model))
    gates.append(
        compare_gate(
            profile,
            "G2.breakeven_cost_multiple",
            "breakeven_cost_multiple",
            breakeven,
            threshold(profile, "cost_stress.min_breakeven_cost_multiple"),
            Direction.AT_LEAST,
        )
    )
    for index, _ in enumerate(profile.cost_stress.stress_multipliers):
        gates.append(
            compare_gate(
                profile,
                f"G2.cost_stress.{index}",
                "breakeven_cost_multiple_vs_stress",
                breakeven,
                threshold(profile, f"cost_stress.stress_multipliers[{index}]"),
                Direction.AT_LEAST,
            )
        )
    for index, value in enumerate(profile.cost_stress.reported_only_multipliers):
        net = net_returns(trades.gross, ctx.cost_model, multiplier(value))
        gates.append(
            GateResult(
                gate_id=f"G2.cost_report.{index}",
                metric="net_mean_return_at_reported_multiplier",
                value=float(sum(net, Decimal(0)) / len(net)),
                verdict=Verdict.PASS,  # a reported-only item (no threshold) never decides
            )
        )
    percentile = _null_percentile(profile, _values(labels), trades, inp.seed + 2)
    if percentile is None:
        gates.append(inconclusive_gate("G2.null_model_percentile", "null_pool_too_small", 0.0))
    else:
        gates.append(
            compare_gate(
                profile,
                "G2.null_model_percentile",
                "percentile_vs_random_entry_null",
                percentile,
                threshold(profile, "benchmark.null_model_percentile"),
                Direction.AT_LEAST,
            )
        )
    return gates


def _g3(inp: InSampleInput, trades: _Trades) -> list[GateResult]:
    ctx, profile = inp.context, inp.context.profile
    if len(trades.gross) < 2:
        return [
            inconclusive_gate("G3.adjusted_p_value", "too_few_trades", float(len(trades.gross)))
        ]
    net = [float(item) for item in net_returns(trades.gross, ctx.cost_model)]
    lag = overlap_lag(_intervals(trades.labels))
    test = hac_t_test(net, lag)
    adjusted = adjust_p_value(
        test.p_greater,
        profile.significance.multiple_testing_method,
        ctx.metadata.family_trial_count,
    )
    return [
        compare_gate(
            profile,
            "G3.adjusted_p_value",
            "net_mean_hac_p_greater_adjusted",
            adjusted,
            threshold(profile, "significance.multiple_testing_threshold"),
            Direction.AT_MOST,
        )
    ]


@dataclass(frozen=True)
class _Evaluation:
    """The walk-forward test labels, their trades and the number of folds that produced them."""

    labels: tuple[OutcomeLabel, ...]
    trades: _Trades
    folds: int


def _evaluation(inp: InSampleInput, labels: Sequence[OutcomeLabel]) -> _Evaluation:
    """Sides on each walk-forward test fold, from blinded labels.

    A ``FittableStudy`` is fitted per fold on that fold's purged and embargoed training labels
    only; an event tested by several (overlapping) folds keeps its first fold's side.
    """
    by_key = {label.event_key: label for label in labels}
    folds = walk_forward_folds([_span(label) for label in labels], inp.context.profile)
    sides: dict[str, int] = {}
    for fold in folds:
        test = tuple(key for key in fold.test if key not in sides)
        if not test:
            continue
        study: SignalStudy = inp.study
        if isinstance(study, FittableStudy):
            train = [by_key[key] for key in fold.train]
            study = study.fit(tuple(label.event_key for label in train), _values(train))
        answer = study.sides(test, blind_labels(len(test)))
        if len(answer) != len(test) or any(side not in (-1, 0, 1) for side in answer):
            raise ValueError("a study must return one side in {-1, 0, 1} per event")
        sides.update(zip(test, answer, strict=True))
    tested = [label for label in labels if label.event_key in sides]
    trades = _trades(tested, [sides[label.event_key] for label in tested])
    return _Evaluation(labels=tuple(tested), trades=trades, folds=len(folds))


def run_in_sample(inp: InSampleInput) -> tuple[GateResult, ...]:
    """G0 → G1 → G2 → G3; stops after the first stage with a ``FAIL``."""
    labels = _computable(inp.outcomes.labels)
    gates: list[GateResult] = []

    def failed(stage: list[GateResult]) -> bool:
        gates.extend(stage)
        return any(gate.verdict is Verdict.FAIL for gate in stage)

    if failed(_g0(inp, labels)) or failed(_g1(inp, labels)):
        return tuple(gates)
    split = _evaluation(inp, labels)
    if failed(_g2(inp, split)):
        return tuple(gates)
    gates.extend(_g3(inp, split.trades))
    return tuple(gates)


# ======================================================================================
# G5 sealed OOS
# ======================================================================================


def run_sealed_oos(inp: SealedOosInput) -> tuple[GateResult, ...]:
    """G5 on the sealed window; requires the family's recorded unsealing."""
    ctx, profile = inp.context, inp.context.profile
    family = ctx.metadata.hypothesis_family_id
    unsealed = inp.vault.is_unsealed(family)
    gates = [
        flag_gate("G5.unsealing_recorded", "oos_unsealing_recorded", unsealed, float(unsealed))
    ]
    if not unsealed:
        return tuple(gates)
    computable = _computable(inp.outcomes.labels)
    allowed = {span.key for span in inp.vault.sealed_view(family, [_span(x) for x in computable])}
    labels = [label for label in computable if label.event_key in allowed]
    keys = tuple(label.event_key for label in labels)
    trades = _trades(labels, inp.study.sides(keys, blind_labels(len(keys))))
    gates.append(
        _sample_gate(
            profile,
            "G5.oos_effective_sample_size",
            trades,
            "sample_size.min_effective_trades_out_of_sample",
        )
    )
    if len(trades.gross) < 1:
        gates.append(inconclusive_gate("G5.oos_breakeven_cost_multiple", "too_few_trades", 0.0))
        return tuple(gates)
    gates.append(
        compare_gate(
            profile,
            "G5.oos_breakeven_cost_multiple",
            "breakeven_cost_multiple",
            float(breakeven_cost_multiple(trades.gross_mean, ctx.cost_model)),
            threshold(profile, "cost_stress.min_breakeven_cost_multiple"),
            Direction.AT_LEAST,
        )
    )
    return tuple(gates)


# ======================================================================================
# Report and Failure Registry record
# ======================================================================================


def build_report(ctx: ValidationContext, gates: Sequence[GateResult]) -> ValidationReport:
    """The report; its verdict is exactly ``derive_verdict(gates)`` (ADR-0013)."""
    return ValidationReport(
        report_id=ctx.report_id,
        run_id=ctx.run.run_id,
        subject=ctx.subject,
        experiment_hash=ctx.run.experiment_hash,
        constitution_version=ctx.metadata.constitution_version,
        validation_profile=ctx.metadata.validation_profile,
        validation_profile_hash=ctx.metadata.validation_profile_hash,
        gates=tuple(gates),
        verdict=derive_verdict(gates),
    )


#: gate-id prefix → (terminal state, reason code); first match wins.
_REASONS: tuple[tuple[str, str, ReasonCode], ...] = (
    ("G0.reproducibility", "FAILED", ReasonCode.NOT_REPRODUCIBLE),
    ("G0.signal_determinism", "FAILED", ReasonCode.NOT_REPRODUCIBLE),
    ("G0.run_state", "FAILED", ReasonCode.RUN_ERRORED),
    ("G0.", "REJECTED", ReasonCode.CONTRACT_VIOLATION),
    ("G1.outcome_not_input", "REJECTED", ReasonCode.OUTCOME_USED_AS_INPUT),
    ("G1.", "REJECTED", ReasonCode.LEAKAGE_DETECTED),
    ("G2.effective_sample_size", "REJECTED", ReasonCode.INSUFFICIENT_EFFECTIVE_SAMPLE),
    ("G2.null_model", "REJECTED", ReasonCode.BENCHMARK_NOT_BEATEN),
    ("G2.", "REJECTED", ReasonCode.COST_KILLED),
    ("G3.", "REJECTED", ReasonCode.NOT_SIGNIFICANT_AFTER_MTC),
    # G4 robustness (Phase 8, ADR-0041)
    ("G4.overfitting", "REJECTED", ReasonCode.NOT_SIGNIFICANT_AFTER_MTC),
    ("G4.param_neighborhood", "REJECTED", ReasonCode.PARAM_UNSTABLE),
    ("G4.time_alignment", "REJECTED", ReasonCode.PARAM_UNSTABLE),
    ("G4.walk_forward.positive_fraction", "REJECTED", ReasonCode.OOS_DECAY),
    ("G4.walk_forward", "REJECTED", ReasonCode.STATE_CONCENTRATED),
    ("G4.state", "REJECTED", ReasonCode.STATE_CONCENTRATED),
    ("G4.cross_asset", "REJECTED", ReasonCode.STATE_CONCENTRATED),
    ("G4.", "REJECTED", ReasonCode.COST_KILLED),  # cost / delay stress, capacity
    ("G5.", "REJECTED", ReasonCode.OOS_DECAY),
)


def reason_for_gate(gate_id: str) -> tuple[str, ReasonCode]:
    """``(terminal state, reason code)`` of a failed gate; the first matching prefix wins."""
    for prefix, state, reason in _REASONS:
        if gate_id.startswith(prefix):
            return state, reason
    raise ValueError(f"no reason code for gate {gate_id!r}")


def failure_record(report: ValidationReport, family_id: str) -> FailureRecord | None:
    """The Failure Registry record of a ``FAIL`` report (``None`` otherwise)."""
    if report.verdict is not Verdict.FAIL:
        return None
    gate = next(gate for gate in report.gates if gate.verdict is Verdict.FAIL)
    state, reason = reason_for_gate(gate.gate_id)
    return FailureRecord(
        subject_ref=report.subject,
        terminal_state=state,
        reason_code=reason,
        gate_id=gate.gate_id,
        evidence=(f"validation_report:{report.report_id}", f"run:{report.run_id}"),
        hypothesis_family_id=family_id,
    )

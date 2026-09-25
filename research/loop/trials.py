"""Experiment and validation stages of the loop on the real Phase 4 / 5 / 6 / 8 components.

W2 wiring (ADR-0049 implementation note, 2026-09-25). Every number comes from ``TrialComponents``
(or a stage parameter); every validation threshold is read from the bound ``ValidationProfile`` or
from explicit ``RobustnessParams`` by ``research/validation`` itself.

``ExperimentStage`` — one trial per hypothesis registered this round (by the hypothesis stage and,
when present, the evolution stage):

1. the hypothesis must already be in the ``TrialLedger`` with exactly this content (pre-registered
   before it runs, Constitution A1 / C-T1); otherwise the trial is ERRORED and not run;
2. ``segment.trial_point`` resolves the strategy (catalog of ``StrategyCandidate``) and the
   parameter point; an unresolvable hypothesis is an ERRORED trial (recorded, never dropped);
3. an ``ExperimentSpec`` + ``ExperimentRun`` carry the full reproducibility tuple of
   06-experiment.md §2 (hypothesis / strategy / risk / outcome refs, dataset snapshot = the
   synthetic market hash, code commit, plugin versions, dependency hashes, params and search space,
   seeds, environment lock, Constitution version, Profile ref + hash + selection, split spec, cost
   model, LLM calls);
4. ``CandidateTrialRunner`` runs strategy → risk → backtest on the research bars with the F4
   ``bar_log_return`` signals of the state stage;
5. P6: the backtest's per-bar returns are compounded per decision period ``[d_i, d_i+1)`` and
   attributed to the P2 state evaluated at ``d_i`` (``state_strategy_matrix`` over the state
   result; the matrix binds the backtest and state result hashes);
6. a completed trial moves CANDIDATE → VALIDATION; an errored one is left for the memory stage
   (CANDIDATE → FAILED + FailureRecord).

``ValidationStage`` — ``PipelineBacktestValidator`` (G0 → G3, then G4 robustness) for every
completed trial, with a ``ValidationContext`` bound to the trial's run and an ``ExperimentMetadata``
whose ``trial_index`` / ``family_trial_count`` come from the ledger (failures included). G5 (sealed
OOS) runs only when an explicit ``OosUnsealBudget`` is configured **and lists the family** with its
approving human, the in-sample verdict is PASS, the round has sealed data and the family has not
used its one unsealing; otherwise the sealed window stays sealed and G5 is simply not run. Once
the family is unsealed, its single evaluation is claimed (``SealedOosVault.claim_evaluation``)
before any sealed bar is released, so it is consumed atomically: if the sealed run then ends
without a statistic (no sealed decision time, no non-flat target, an error) the G5 report is
``INCONCLUSIVE`` with ``G5.oos_evaluation`` = ``consumed_without_result:<reason>`` and the window
stays closed for good (ADR-0049 review fixes 2). The stage makes no lifecycle move; the memory
stage does (PASS → OOS at most; FAIL → REJECTED).

Record hashes never depend on the wall clock: reports and metadata are stamped with the round's
scheduled time, and floats are written as quantized Decimal text (``segment.decimal_text``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any, Final

from apps.worker.loop import RoundContext, StageFailed, StageResult, StageUsage
from core.contracts.cost_model import CostModelSpec
from core.contracts.outcome import (
    OutcomeEvent,
    OutcomeLabelSpec,
    OutcomePriceBar,
    OutcomeProvider,
    OutcomeRequest,
)
from core.contracts.profile_selection import ExperimentMetadata
from core.contracts.state import StateResult
from core.contracts.strategy import (
    BacktestCostModel,
    BacktestProvider,
    SignalObservation,
    TargetPosition,
)
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import FrozenMapping, Ref, content_hash
from core.domain.research import (
    ExperimentRun,
    ExperimentSpec,
    GateResult,
    Hypothesis,
    LlmCall,
    ReproducibilityTuple,
    RunState,
    ValidationReport,
    Verdict,
)
from core.domain.selection import ProfileSelection
from core.domain.specs import DatasetRef, Zone
from core.errors import ReasonCode
from core.lifecycle.strategy import LifecycleState
from research.experiments import RETURN_QUANTUM, backtest_returns, state_strategy_matrix
from research.loop.memory import ResearchMemory
from research.loop.segment import (
    Param,
    Segment,
    decimal_text,
    decision_grid,
    price_bar,
    trial_point,
)
from research.outcomes.table import materialize
from research.strategies.pipeline import CandidateTrialRunner, EvaluationInputs, StrategyCandidate
from research.strategies.validation import (
    BacktestValidation,
    PipelineBacktestValidator,
    TrialRun,
    ValidatorSetup,
)
from research.validation import (
    RobustnessParams,
    SealedOosInput,
    ValidationContext,
    build_report,
    reason_for_gate,
    run_sealed_oos,
)
from research.validation.controls import FixedSides
from research.validation.pipeline import CONSUMED_WITHOUT_RESULT, sealed_oos_without_result
from research.validation.sealed_oos import OosBudgetExhausted, SealedEvaluation, SealedOosVault

__all__ = [
    "ExperimentStage",
    "OosUnsealBudget",
    "TrialComponents",
    "TrialOutcome",
    "ValidationOutcome",
    "ValidationStage",
    "failure_of",
]

_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)
_AUTOMATION_PREFIX: Final = "research_loop:"


@dataclass(frozen=True)
class TrialComponents:
    """What experiments and validations bind. No field has a default: absence must be explicit."""

    backtester: BacktestProvider
    cost_model: CostModelSpec
    initial_equity: Decimal
    outcome_provider: OutcomeProvider
    label_spec: OutcomeLabelSpec
    robustness: RobustnessParams
    profile: ValidationProfile
    profile_selection: ProfileSelection
    constitution_version: str
    declared_research_class: str
    code_commit: str
    environment_lock: str

    @property
    def backtest_costs(self) -> BacktestCostModel:
        """The backtester's cost model: exactly the rates of the bound ``CostModelSpec``."""
        return BacktestCostModel(
            name=self.cost_model.name,
            version=self.cost_model.version,
            fee_rate=self.cost_model.fee_rate_per_side,
            slippage_rate=self.cost_model.slippage_rate_per_side,
        )


@dataclass(frozen=True)
class OosUnsealBudget:
    """Explicit, per-family permission to unseal the sealed OOS window (C-S2).

    Without this object the loop never unseals anything. ``approved_families`` maps every
    hypothesis family a human has approved for its one unsealing to **that** human's identity
    (recorded as ``approved_by`` in the family's ``OosUnsealing``); the loop may only unseal a
    family on this list, so a budget signed once at configuration time cannot be spent on
    families nobody looked at (ADR-0049 review fixes 2). ``max_unsealings`` still bounds the
    global count (``SealedOosVault``). An empty list, a blank family id or an automation
    identity is refused.
    """

    max_unsealings: int
    approved_families: Mapping[str, str]

    def __post_init__(self) -> None:
        if isinstance(self.max_unsealings, bool) or self.max_unsealings < 1:
            raise ValueError("max_unsealings must be a positive int")
        if not isinstance(self.approved_families, Mapping) or not self.approved_families:
            raise ValueError("an unseal budget needs at least one explicitly approved family")
        approved: dict[str, str] = {}
        for family, approver in self.approved_families.items():
            key = family.strip() if isinstance(family, str) else ""
            who = approver.strip() if isinstance(approver, str) else ""
            if not key:
                raise ValueError("an approved family id must not be blank")
            if key in approved:
                raise ValueError(f"family {key!r} is listed twice")
            if not who or who.startswith(_AUTOMATION_PREFIX):
                raise ValueError(f"family {key!r} needs the approving human's identity")
            approved[key] = who
        object.__setattr__(self, "approved_families", FrozenMapping(approved))

    def approver_of(self, family_id: str) -> str | None:
        """The human who approved ``family_id``'s unsealing (``None``: not approved)."""
        return self.approved_families.get(family_id)


@dataclass(frozen=True)
class TrialOutcome:
    """One trial of one round: its reproducibility records and what the run produced."""

    round_index: int
    hypothesis: Hypothesis
    origin: str
    experiment: ExperimentSpec
    run: ExperimentRun
    candidate: StrategyCandidate | None
    request_params: Mapping[str, Param]
    inputs: EvaluationInputs | None
    trial: TrialRun | None
    validation_seed: int
    summary: Mapping[str, Any]
    error: str | None = None
    reason: ReasonCode | None = None

    @property
    def completed(self) -> bool:
        return self.run.state is RunState.COMPLETED and self.trial is not None


@dataclass(frozen=True)
class ValidationOutcome:
    """The validation of one completed trial (in-sample report and, when run, the G5 report)."""

    round_index: int
    outcome: TrialOutcome
    report: ValidationReport | None
    failure_reason: ReasonCode | None
    sealed_report: ValidationReport | None
    sealed_status: Mapping[str, Any]
    summary: Mapping[str, Any]
    error: str | None = None

    @property
    def verdict(self) -> Verdict | None:
        return None if self.report is None else self.report.verdict

    def gate_value(self, gate_id: str) -> float | None:
        if self.report is None:
            return None
        return next((g.value for g in self.report.gates if g.gate_id == gate_id), None)


def _registered_this_round(ctx: RoundContext) -> tuple[tuple[Hypothesis, str], ...]:
    """Hypotheses registered this round, in order: hypothesis stage first, then evolution."""
    found: list[tuple[Hypothesis, str]] = [
        (h, "hypothesis") for h in ctx.artifacts.get("hypothesis", {}).get("registered", ())
    ]
    found += [(h, "evolution") for h in ctx.artifacts.get("evolution", {}).get("registered", ())]
    return tuple(found)


def _plugin(descriptor: Any) -> tuple[str, str]:
    return f"{descriptor.name}@{descriptor.version}", descriptor.content_hash()


def _seed(round_seed: int, hypothesis: Hypothesis) -> int:
    return int(content_hash({"round_seed": round_seed, "trial": str(hypothesis.ref)})[:8], 16)


def _llm_calls(ctx: RoundContext, hypothesis: Hypothesis) -> tuple[LlmCall, ...]:
    calls: Mapping[str, LlmCall] = ctx.artifacts.get("hypothesis", {}).get("llm_calls", {})
    call = calls.get(str(hypothesis.ref))
    return () if call is None else (call,)


def _period_returns(
    backtest_curve: Mapping[datetime, Decimal], decisions: Sequence[datetime]
) -> dict[datetime, Decimal]:
    """Per-bar returns compounded per decision period ``[d_i, d_i+1)`` (last: to end of data)."""
    out: dict[datetime, Decimal] = {}
    keys = sorted(backtest_curve)
    with localcontext(_CONTEXT):
        for index, start in enumerate(decisions):
            end = decisions[index + 1] if index + 1 < len(decisions) else None
            growth = Decimal(1)
            seen = False
            for key in keys:
                if key >= start and (end is None or key < end):
                    growth *= 1 + backtest_curve[key]
                    seen = True
            if seen:
                out[start] = (growth - 1).quantize(RETURN_QUANTUM)
    return out


def _request_point(
    candidate: StrategyCandidate, overrides: Mapping[str, Param]
) -> dict[str, Param]:
    """The declared parameters of the trial point: spec defaults overridden by the hypothesis."""
    spec = candidate.spec
    stray = sorted(set(overrides) - set(spec.param_search_space))
    if stray:
        raise ValueError(f"{spec.ref} declares no search space for {stray}")
    point: dict[str, Param] = {}
    for key, value in {**dict(spec.params), **dict(overrides)}.items():
        if key not in spec.param_search_space:
            continue
        if isinstance(value, float):
            raise ValueError(f"{spec.ref}: float parameter {key}={value!r} cannot be requested")
        point[key] = value
    return point


class ExperimentStage:
    """Runs every trial registered this round (see module docs)."""

    name = "experiment"

    def __init__(
        self,
        memory: ResearchMemory,
        components: TrialComponents,
        *,
        compute_seconds_per_trial: Decimal,
    ) -> None:
        self._memory = memory
        self._c = components
        self._per_trial = compute_seconds_per_trial

    def estimate(self, ctx: RoundContext) -> StageUsage:
        return StageUsage(compute_seconds=self._per_trial * len(_registered_this_round(ctx)))

    def run(self, ctx: RoundContext) -> StageResult:
        outcomes: list[TrialOutcome] = []
        try:
            for hypothesis, origin in _registered_this_round(ctx):
                outcomes.append(self._trial(ctx, hypothesis, origin))
        except Exception as exc:
            raise StageFailed(
                f"{type(exc).__name__}: {exc}",
                usage=StageUsage(compute_seconds=self._per_trial * (len(outcomes) + 1)),
            ) from exc
        self._memory.trials.extend(outcomes)
        self._memory.experiments.extend(
            {"round": ctx.round_index, **dict(o.summary)} for o in outcomes
        )
        return StageResult(
            {"experiments": [dict(o.summary) for o in outcomes]},
            StageUsage(compute_seconds=self._per_trial * len(outcomes)),
            {"outcomes": tuple(outcomes)},
        )

    # ------------------------------------------------------------------------------------------

    def _pre_registered(self, hypothesis: Hypothesis) -> bool:
        return any(
            h.name == hypothesis.name
            and h.version == hypothesis.version
            and h.content_hash() == hypothesis.content_hash()
            for h in self._memory.ledger.hypotheses
        )

    def _repro(
        self,
        ctx: RoundContext,
        hypothesis: Hypothesis,
        candidate: StrategyCandidate | None,
        params: Mapping[str, Param],
        segment: Segment,
        plugins: Mapping[str, str],
        seed: int,
    ) -> ReproducibilityTuple:
        c, profile = self._c, self._c.profile
        spec = None if candidate is None else candidate.spec
        dependencies = {
            str(hypothesis.ref): hypothesis.content_hash(),
            str(c.label_spec.outcome): c.label_spec.outcome_spec_hash,
            str(c.cost_model.ref): c.cost_model.content_hash(),
        }
        all_plugins = dict(plugins)
        all_plugins.update(dict([_plugin(c.backtester.descriptor)]))
        all_plugins.update(dict([_plugin(c.outcome_provider.descriptor)]))
        if candidate is not None and spec is not None:
            dependencies[str(spec.ref)] = spec.content_hash()
            all_plugins.update(dict([_plugin(candidate.strategy.descriptor)]))
            if candidate.risk_policy is not None and candidate.risk is not None:
                dependencies[str(candidate.risk_policy.ref)] = candidate.risk_policy.content_hash()
                all_plugins.update(dict([_plugin(candidate.risk.descriptor)]))
        first = segment.research[0].interval_start if segment.research else ctx.as_of
        last = segment.research_end or ctx.as_of
        boundary = profile.data_split.sealed_oos_boundary.isoformat()
        return ReproducibilityTuple(
            hypothesis_ref=hypothesis.ref,
            strategy_ref=None if spec is None else spec.ref,
            risk_policy_ref=None if spec is None else spec.risk_policy,
            outcome_ref=c.label_spec.outcome,
            dataset_snapshots=(
                DatasetRef(
                    zone=Zone.CANONICAL,
                    table=f"synthetic.{segment.provider_key}",
                    snapshot_id=segment.market.market_hash,
                    time_range_start=first,
                    time_range_end=last,
                ),
            ),
            code_commit=c.code_commit,
            plugin_versions=FrozenMapping(all_plugins),
            dependency_hashes=FrozenMapping(dependencies),
            params=FrozenMapping({} if spec is None else {**dict(spec.params), **dict(params)}),
            param_search_space=FrozenMapping({} if spec is None else dict(spec.param_search_space)),
            seeds=(ctx.seed, seed),
            environment_lock=c.environment_lock,
            constitution_version=c.constitution_version,
            validation_profile=profile.ref,
            validation_profile_hash=profile.content_hash(),
            profile_selection=c.profile_selection,
            split_spec=(
                f"walk_forward({profile.ref}); research [{first.isoformat()}, "
                f"{last.isoformat()}]; sealed_oos from {boundary} "
                f"for {profile.data_split.sealed_oos_length}"
            ),
            cost_model_ref=c.cost_model.ref,
            llm_calls=_llm_calls(ctx, hypothesis),
        )

    def _trial(self, ctx: RoundContext, hypothesis: Hypothesis, origin: str) -> TrialOutcome:
        segment: Segment = ctx.artifact("ingest", "segment")
        signals: tuple[SignalObservation, ...] = ctx.artifact("state", "signals")
        states: StateResult = ctx.artifact("state", "result")
        state_ref: Ref = ctx.artifact("state", "spec_ref")
        plugins: Mapping[str, str] = ctx.artifact("state", "plugins")
        seed = _seed(ctx.seed, hypothesis)
        candidate: StrategyCandidate | None = None
        params: dict[str, Param] = {}
        error: str | None = None
        reason: ReasonCode | None = None
        if not self._pre_registered(hypothesis):
            error, reason = (
                f"{hypothesis.ref} was not pre-registered",
                ReasonCode.CONTRACT_VIOLATION,
            )
        else:
            try:
                point = trial_point(hypothesis)
                candidate = self._memory.strategies.get(f"strategy:{point.strategy}")
                if candidate is None:
                    raise ValueError(f"no strategy {point.strategy} in the loop's catalog")
                params = _request_point(candidate, point.overrides)
            except ValueError as exc:
                error, reason = str(exc), ReasonCode.CONTRACT_VIOLATION
                candidate = None
        repro = self._repro(ctx, hypothesis, candidate, params, segment, plugins, seed)
        experiment = ExperimentSpec(
            name=f"e_{hypothesis.name}",
            version=hypothesis.version,
            created_at=ctx.as_of,
            repro=repro,
        )
        run_id = f"{ctx.loop_id}:{ctx.round_index}:{hypothesis.name}@{hypothesis.version}"
        inputs: EvaluationInputs | None = None
        trial: TrialRun | None = None
        matrix_hash: str | None = None
        if candidate is not None and error is None:
            if not segment.decision_times or segment.research_end is None:
                error, reason = "the segment has no decision time", ReasonCode.RUN_ERRORED
            else:
                inputs = EvaluationInputs(
                    instruments=(segment.symbol,),
                    bars=segment.research_bars,
                    decision_times=segment.decision_times,
                    knowledge_cutoff=segment.research_end,
                    cost_model=self._c.backtest_costs,
                    initial_equity=self._c.initial_equity,
                    signals=signals,
                    params=params,
                )
                try:
                    trial = CandidateTrialRunner(candidate, inputs, self._c.backtester).run(params)
                    matrix = state_strategy_matrix(
                        candidate.spec.ref,
                        state_ref,
                        _period_returns(backtest_returns(trial.backtest), segment.decision_times),
                        states,
                    )
                    matrix = replace(matrix, backtest_result_hash=trial.backtest.result_hash)
                    matrix_hash = matrix.matrix_hash
                except Exception as exc:  # noqa: BLE001 - an errored trial is recorded, not dropped
                    error, reason, trial = (
                        f"{type(exc).__name__}: {exc}",
                        ReasonCode.RUN_ERRORED,
                        None,
                    )
        state = RunState.COMPLETED if trial is not None else RunState.ERRORED
        run = ExperimentRun(
            run_id=run_id,
            experiment=experiment.ref,
            repro=repro,
            state=state,
            started_at=ctx.as_of,
            finished_at=ctx.as_of,
        )
        summary: dict[str, Any] = {
            "hypothesis": str(hypothesis.ref),
            "origin": origin,
            "strategy": None if candidate is None else str(candidate.spec.ref),
            "params": {
                k: decimal_text(v) if isinstance(v, float) else v
                for k, v in sorted(repro.params.items())
            },
            "experiment": str(experiment.ref),
            "experiment_hash": repro.experiment_hash,
            "run_id": run_id,
            "run_state": state.value,
            "market_hash": segment.market.market_hash,
            "error": None if error is None else error[:500],
        }
        if trial is not None:
            traded = sum(1 for t in trial.targets if t.target_weight != 0)
            final = trial.backtest.equity_curve[-1].equity if trial.backtest.equity_curve else None
            summary.update(
                backtest_result_hash=trial.backtest.result_hash,
                decisions=len(segment.decision_times),
                traded_decisions=traded,
                net_return=None
                if final is None
                else decimal_text(final / trial.backtest.initial_equity - 1),
                state_strategy_matrix_hash=matrix_hash,
            )
            ctx.advance(
                hypothesis.ref,
                LifecycleState.VALIDATION,
                reason="experiment completed; awaiting validation",
                evidence=(f"experiment:{repro.experiment_hash}", f"run:{run_id}"),
            )
        return TrialOutcome(
            round_index=ctx.round_index,
            hypothesis=hypothesis,
            origin=origin,
            experiment=experiment,
            run=run,
            candidate=candidate,
            request_params=FrozenMapping(params),
            inputs=inputs,
            trial=trial,
            validation_seed=seed,
            summary=summary,
            error=error,
            reason=reason,
        )


def _side(weight: Decimal) -> int:
    return (weight > 0) - (weight < 0)


def _event_key(target: TargetPosition) -> str:
    return f"{target.instrument}|{target.decision_time.isoformat()}"


def _gates(report: ValidationReport) -> list[dict[str, Any]]:
    return [
        {
            "gate_id": gate.gate_id,
            "value": decimal_text(gate.value),
            "threshold": decimal_text(gate.threshold),
            "threshold_source": gate.threshold_source,
            "verdict": gate.verdict.value,
        }
        for gate in report.gates
    ]


class ValidationStage:
    """G0 → G4 for every completed trial; G5 only with an explicit unseal budget (module docs)."""

    name = "validation"

    def __init__(
        self,
        memory: ResearchMemory,
        components: TrialComponents,
        *,
        compute_seconds_per_validation: Decimal,
        oos_unseal: OosUnsealBudget | None = None,
        sealed_decision_step: timedelta | None = None,
    ) -> None:
        if (oos_unseal is None) != (sealed_decision_step is None):
            raise ValueError("an unseal budget needs a sealed decision step and vice versa")
        self._memory = memory
        self._c = components
        self._per = compute_seconds_per_validation
        self._unseal = oos_unseal
        self._sealed_step = sealed_decision_step

    @staticmethod
    def _completed(ctx: RoundContext) -> tuple[TrialOutcome, ...]:
        outcomes: tuple[TrialOutcome, ...] = ctx.artifact("experiment", "outcomes")
        return tuple(o for o in outcomes if o.completed)

    def estimate(self, ctx: RoundContext) -> StageUsage:
        return StageUsage(compute_seconds=self._per * len(self._completed(ctx)))

    def run(self, ctx: RoundContext) -> StageResult:
        results: list[ValidationOutcome] = []
        try:
            for outcome in self._completed(ctx):
                results.append(self._validate(ctx, outcome))
        except Exception as exc:
            raise StageFailed(
                f"{type(exc).__name__}: {exc}",
                usage=StageUsage(compute_seconds=self._per * (len(results) + 1)),
            ) from exc
        self._memory.validations.extend(results)
        summary = {
            "scope": "PipelineBacktestValidator G0-G4; G5 only with an explicit unseal budget",
            "profile": str(self._c.profile.ref),
            "profile_hash": self._c.profile.content_hash(),
            "reports": [dict(r.summary) for r in results],
        }
        return StageResult(
            summary,
            StageUsage(compute_seconds=self._per * len(results)),
            {"validations": tuple(results)},
        )

    # ------------------------------------------------------------------------------------------

    def _context(self, ctx: RoundContext, outcome: TrialOutcome) -> ValidationContext:
        hypothesis, c = outcome.hypothesis, self._c
        family = [h for h in self._memory.ledger.hypotheses if h.family_id == hypothesis.family_id]
        index = next(i for i, h in enumerate(family, start=1) if h.ref == hypothesis.ref)
        metadata = ExperimentMetadata(
            experiment_hash=outcome.run.experiment_hash,
            constitution_version=c.constitution_version,
            validation_profile=c.profile.ref,
            validation_profile_hash=c.profile.content_hash(),
            profile_selection=c.profile_selection,
            hypothesis_family_id=hypothesis.family_id,
            trial_index=index,
            family_trial_count=len(family),
            declared_research_class=c.declared_research_class,
            llm_calls=outcome.run.repro.llm_calls,
            trace_id=outcome.run.run_id,
            recorded_at=ctx.as_of,
        )
        assert outcome.candidate is not None
        return ValidationContext(
            report_id=content_hash({"run": outcome.run.run_id, "stage": "in_sample"}),
            subject=outcome.candidate.spec.ref,
            run=outcome.run,
            metadata=metadata,
            profile=c.profile,
            cost_model=c.cost_model,
            label_spec=c.label_spec,
        )

    def _validate(self, ctx: RoundContext, outcome: TrialOutcome) -> ValidationOutcome:
        segment: Segment = ctx.artifact("ingest", "segment")
        labels: Mapping[datetime, str | None] = ctx.artifact("state", "labels")
        candidate, inputs, trial = outcome.candidate, outcome.inputs, outcome.trial
        assert candidate is not None and inputs is not None and trial is not None
        context = self._context(ctx, outcome)
        setup = ValidatorSetup(
            context=context,
            outcome_provider=self._c.outcome_provider,
            manifest_content_hash=segment.market.market_hash,
            instrument=segment.symbol,
            trials=CandidateTrialRunner(candidate, inputs, self._c.backtester),
            chosen_params=dict(outcome.request_params),
            seed=outcome.validation_seed,
            robustness=self._c.robustness,
            state_of=lambda t: labels.get(t) or "unknown",
            bar_volume={
                (segment.symbol, bar.interval_start): bar.volume for bar in segment.research
            },
            declared_instruments=(segment.symbol,),
        )
        base: dict[str, Any] = {
            "hypothesis": str(outcome.hypothesis.ref),
            "origin": outcome.origin,
            "subject": str(candidate.spec.ref),
            "experiment_hash": outcome.run.experiment_hash,
            "family_trial_count": context.metadata.family_trial_count,
            "trial_index": context.metadata.trial_index,
        }
        try:
            answer: BacktestValidation = PipelineBacktestValidator(setup).validate(
                candidate.spec.ref, candidate.spec, trial.backtest
            )
            answer.check_subject(candidate.spec.ref)
        except Exception as exc:  # noqa: BLE001 - a validator error is a FAILED trial, recorded
            error = f"{type(exc).__name__}: {exc}"
            summary = {**base, "verdict": None, "error": error[:500]}
            return ValidationOutcome(
                ctx.round_index, outcome, None, None, None, {"status": "not_run"}, summary, error
            )
        report = answer.report.model_copy(update={"created_at": ctx.as_of})
        sealed_report: ValidationReport | None = None
        sealed_status: dict[str, Any] = {"status": "not_run", "reason": "in-sample verdict"}
        if report.verdict is Verdict.PASS:
            sealed_report, sealed_status = self._sealed_oos(ctx, outcome, context, segment)
        summary = {
            **base,
            "report_id": report.report_id,
            "report_hash": report.content_hash(),
            "verdict": report.verdict.value,
            "failure_reason": None
            if answer.failure_reason is None
            else answer.failure_reason.value,
            "stopped_at": next(
                (g.gate_id for g in report.gates if g.verdict is Verdict.FAIL), None
            ),
            "gates": _gates(report),
            "sealed_oos": {
                **sealed_status,
                "report_hash": None if sealed_report is None else sealed_report.content_hash(),
                "verdict": None if sealed_report is None else sealed_report.verdict.value,
                "gates": [] if sealed_report is None else _gates(sealed_report),
            },
        }
        return ValidationOutcome(
            ctx.round_index,
            outcome,
            report,
            answer.failure_reason,
            sealed_report,
            sealed_status,
            summary,
        )

    def _sealed_oos(
        self,
        ctx: RoundContext,
        outcome: TrialOutcome,
        context: ValidationContext,
        segment: Segment,
    ) -> tuple[ValidationReport | None, dict[str, Any]]:
        unseal, step = self._unseal, self._sealed_step
        if unseal is None or step is None:
            return None, {"status": "sealed", "reason": "no unseal budget configured"}
        if not len(segment.sealed):
            return None, {"status": "sealed", "reason": "no sealed-window data in this round"}
        family = outcome.hypothesis.family_id
        approver = unseal.approver_of(family)
        if approver is None:
            return None, {
                "status": "sealed",
                "reason": "the family is not on the unseal budget's approved list",
            }
        vault = SealedOosVault(
            self._c.profile, self._memory.oos_ledger, max_unsealings=unseal.max_unsealings
        )
        if vault.is_unsealed(family):
            return None, {"status": "sealed", "reason": "the family already used its unsealing"}
        try:
            unsealing = vault.unseal(family, approver, ctx.as_of)
        except OosBudgetExhausted as exc:
            return None, {"status": "sealed", "reason": str(exc)}
        # From here on the family's single evaluation is consumed, whatever happens next: the
        # claim marks it evaluated before any sealed bar leaves the vault (review fixes 2).
        evaluation = vault.claim_evaluation(family)
        g5_context = replace(
            context,
            report_id=content_hash({"run": outcome.run.run_id, "stage": "sealed_oos"}),
            metadata=context.metadata.model_copy(update={"oos_unsealing": unsealing}),
        )
        status: dict[str, Any] = {"status": "unsealed", "approved_by": unsealing.approved_by}
        result: tuple[GateResult, ...] | str
        try:
            result = self._sealed_gates(
                ctx, outcome, segment, vault, evaluation, g5_context, status
            )
        except Exception as exc:  # noqa: BLE001 - the consumed evaluation is recorded, not lost
            status["error"] = f"{type(exc).__name__}: {exc}"[:500]
            result = "sealed run errored"
        if isinstance(result, str):
            status.update(status=CONSUMED_WITHOUT_RESULT, reason=result)
            result = sealed_oos_without_result(
                g5_context, vault, evaluation, result.replace(" ", "_")
            )
        report = build_report(g5_context, result).model_copy(update={"created_at": ctx.as_of})
        return report, status

    def _sealed_gates(
        self,
        ctx: RoundContext,
        outcome: TrialOutcome,
        segment: Segment,
        vault: SealedOosVault,
        evaluation: SealedEvaluation,
        g5_context: ValidationContext,
        status: dict[str, Any],
    ) -> tuple[GateResult, ...] | str:
        """G5 gates of the claimed evaluation, or why it ended without a result."""
        step = self._sealed_step
        assert step is not None
        sealed_bars = segment.sealed.release(evaluation)
        build_signals: Callable[..., tuple[SignalObservation, ...]] = ctx.artifact(
            "state", "signals_for"
        )
        candidate, inputs = outcome.candidate, outcome.inputs
        assert candidate is not None and inputs is not None
        bars = (*segment.research, *sealed_bars)
        decisions = decision_grid(
            sealed_bars, step=step, warmup=step, horizon=self._c.label_spec.horizon
        )
        status.update(sealed_bars=len(sealed_bars), decisions=len(decisions))
        if not decisions:
            return "no sealed decision time"
        sealed_inputs = replace(
            inputs,
            bars=tuple(price_bar(segment.symbol, bar) for bar in bars),
            decision_times=decisions,
            knowledge_cutoff=sealed_bars[-1].interval_end,
            signals=build_signals(bars),
        )
        run = CandidateTrialRunner(candidate, sealed_inputs, self._c.backtester).run(
            dict(outcome.request_params)
        )
        traded = [t for t in run.targets if _side(t.target_weight) != 0]
        outcome_bars = tuple(
            OutcomePriceBar(
                interval_start=bar.interval_start,
                interval_end=bar.interval_end,
                available_time=bar.interval_end,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
            )
            for bar in bars
        )
        if not traded:
            return "no non-flat target in the sealed window"
        table = materialize(
            self._c.outcome_provider,
            OutcomeRequest(
                label_spec=self._c.label_spec,
                manifest_content_hash=segment.market.market_hash,
                price_cutoff=max(bar.available_time for bar in outcome_bars),
                events=tuple(
                    OutcomeEvent(event_key=_event_key(t), event_time=t.decision_time)
                    for t in traded
                ),
                bars=outcome_bars,
            ),
        )
        study = FixedSides(
            refs=tuple(candidate.spec.signals),
            by_event={_event_key(t): _side(t.target_weight) for t in traded},
        )
        return run_sealed_oos(SealedOosInput(g5_context, vault, table, study, evaluation))


def failure_of(result: ValidationOutcome) -> tuple[str, ReasonCode, str] | None:
    """``(terminal state, reason, gate id)`` of a FAIL verdict, ``None`` otherwise."""
    report = result.sealed_report if _sealed_failed(result) else result.report
    if report is None or report.verdict is not Verdict.FAIL:
        return None
    gate = next(g for g in report.gates if g.verdict is Verdict.FAIL)
    state, reason = reason_for_gate(gate.gate_id)
    if report is result.report and result.failure_reason is not None:
        reason = result.failure_reason
    return state, reason, gate.gate_id


def _sealed_failed(result: ValidationOutcome) -> bool:
    return result.sealed_report is not None and result.sealed_report.verdict is Verdict.FAIL

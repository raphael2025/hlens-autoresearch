"""Strategy evaluation pipeline: strategy → risk → backtest → validation hook → Failure Registry.

Phase 5 (ADR-0038), research plane. ``evaluate_strategy`` runs one trial (one declared parameter
point) of one library candidate:

1. ``StrategyProvider.target_positions`` over all decision times, checked with
   ``StrategyResult.check_answers``;
2. if the spec names a ``risk_policy``: ``RiskProvider.constrain`` once per decision time, with only
   the risk signals visible at that time and the previous constrained weights as portfolio state
   (``equity=None``: risk runs ahead of the simulation, so path-dependent rules must refuse);
3. ``BacktestProvider.run`` on the (constrained) targets, checked with ``check_answers``;
4. the ``BacktestValidator`` (``validation.py``): ``PipelineBacktestValidator`` runs the Phase 4
   gates G0 – G3 and the Phase 8 robustness gates G4 (``research/validation``, ADR-0037 /
   ADR-0041). Without a validator the result is ``NOT_VALIDATED``, never a pass;
5. a ``FAIL`` verdict (→ ``REJECTED``) or a run / contract error — in the simulation or in the
   validator — (→ ``FAILED``) is appended to the ``FailureRegistry``. Nothing is ever deleted; a
   retry is a new version.

``CandidateTrialRunner`` re-runs steps 1 – 3 at any declared parameter point, optionally with the
targets executed ``delay_bars`` bars late (delay stress), the decision grid shifted by
``decision_offset`` (time alignment) or restricted to some instruments (cross-asset). The validator
uses it for reproduction and for the G4 trial family.

The pipeline performs no lifecycle transition (that is the Control Plane's) and decides nothing:
the verdict is whatever the validator's report says.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import ValidationError

from core.contracts.feature import ObservationScalar
from core.contracts.outcome import OutcomeUsedAsInput
from core.contracts.strategy import (
    BacktestCostModel,
    BacktestProvider,
    BacktestProviderError,
    BacktestRequest,
    BacktestResult,
    PortfolioState,
    PriceBar,
    RiskProvider,
    RiskProviderError,
    RiskRequest,
    RiskResult,
    SignalObservation,
    StrategyProvider,
    StrategyProviderError,
    StrategyRequest,
    StrategyResult,
    TargetPosition,
    execution_bar,
)
from core.domain.base import FrozenMapping, Ref
from core.domain.research import FailureRecord, Verdict
from core.domain.specs import RiskPolicy, StrategySpec
from core.errors import ReasonCode
from research.strategies.failure_registry import FailureRegistry
from research.strategies.validation import BacktestValidation, BacktestValidator, TrialRun

__all__ = [
    "CandidateTrialRunner",
    "EvaluationInputs",
    "EvaluationStatus",
    "StrategyCandidate",
    "StrategyEvaluation",
    "evaluate_strategy",
]


#: Validation failures that are technical (FAILED), not a refutation (REJECTED): C-P3, ADR-0006.
_TECHNICAL_FAILURES = frozenset({ReasonCode.NOT_REPRODUCIBLE, ReasonCode.RUN_ERRORED})


class EvaluationStatus(StrEnum):
    """``PASSED`` = every gate the validator ran passed (research window, G0–G4 by default); it is
    **not** promotable by itself — see ``StrategyEvaluation.promotion_blocked_reason`` (G5)."""

    NOT_VALIDATED = "NOT_VALIDATED"
    PASSED = "PASSED"
    INCONCLUSIVE = "INCONCLUSIVE"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class StrategyCandidate:
    """A library strategy wired to its providers."""

    spec: StrategySpec
    strategy: StrategyProvider
    hypothesis_family_id: str
    risk_policy: RiskPolicy | None = None
    risk: RiskProvider | None = None

    def __post_init__(self) -> None:
        declared = self.spec.risk_policy
        given = self.risk_policy.ref if self.risk_policy is not None else None
        if (declared is None) != (given is None) or (
            declared is not None
            and given is not None
            and declared.target_identity() != given.target_identity()
        ):
            raise ValueError(f"{self.spec.ref}: risk_policy must be exactly the spec's risk_policy")
        if (self.risk_policy is None) != (self.risk is None):
            raise ValueError("a risk policy needs a risk provider and vice versa")


@dataclass(frozen=True, slots=True)
class EvaluationInputs:
    """The data of one trial. ``signals`` feed the strategy, ``risk_signals`` the risk provider."""

    instruments: tuple[str, ...]
    bars: tuple[PriceBar, ...]
    decision_times: tuple[datetime, ...]
    knowledge_cutoff: datetime
    cost_model: BacktestCostModel
    initial_equity: Decimal
    signals: tuple[SignalObservation, ...]
    risk_signals: tuple[SignalObservation, ...] = ()
    params: Mapping[str, ObservationScalar] | None = None


@dataclass(frozen=True, slots=True)
class StrategyEvaluation:
    subject: Ref
    status: EvaluationStatus
    strategy_result: StrategyResult | None = None
    risk_results: tuple[RiskResult, ...] = ()
    backtest: BacktestResult | None = None
    validation: BacktestValidation | None = None
    failure: FailureRecord | None = None

    @property
    def promotion_blocked_reason(self) -> str | None:
        """``None`` only for a ``PASSED`` evaluation whose report also carries a sealed-OOS (G5)
        result; everything else is blocked, with the reason (ADR-0041 review fix R18)."""
        if self.status is not EvaluationStatus.PASSED or self.validation is None:
            return f"status_{self.status.value.lower()}"
        return self.validation.promotion_blocked_reason


def _risk_step(
    candidate: StrategyCandidate,
    inputs: EvaluationInputs,
    result: StrategyResult,
) -> tuple[tuple[RiskResult, ...], tuple[TargetPosition, ...]]:
    policy, risk = candidate.risk_policy, candidate.risk
    if policy is None or risk is None:
        return (), result.positions
    ordered = sorted(inputs.risk_signals, key=lambda item: item.available_time)
    weights: dict[str, Decimal] = {}
    results: list[RiskResult] = []
    targets: list[TargetPosition] = []
    for decision_time in inputs.decision_times:
        upstream = result.at(decision_time)
        request = RiskRequest(
            policy=policy.ref,
            policy_hash=policy.content_hash(),
            decision_time=decision_time,
            knowledge_cutoff=inputs.knowledge_cutoff,
            targets=upstream,
            portfolio=PortfolioState(as_of=decision_time, current_weights=FrozenMapping(weights)),
            signals=tuple(item for item in ordered if item.available_time <= decision_time),
        )
        answer = risk.constrain(request)
        answer.check_answers(request, risk.descriptor)
        results.append(answer)
        by_name = {item.instrument: item for item in upstream}
        for position in answer.positions:
            targets.append(position.as_target(by_name[position.instrument]))
            weights[position.instrument] = position.constrained_weight
    return tuple(results), tuple(targets)


def _failure(
    candidate: StrategyCandidate,
    terminal_state: str,
    reason: ReasonCode,
    evidence: Sequence[str],
    *,
    gate_id: str | None = None,
    lessons: str | None = None,
) -> FailureRecord:
    return FailureRecord(
        subject_ref=candidate.spec.ref,
        terminal_state=terminal_state,
        reason_code=reason,
        gate_id=gate_id,
        evidence=tuple(evidence),
        hypothesis_family_id=candidate.hypothesis_family_id,
        lessons=lessons,
    )


@dataclass(slots=True)
class _Simulation:
    strategy_result: StrategyResult | None = None
    risk_results: tuple[RiskResult, ...] = ()
    targets: tuple[TargetPosition, ...] = ()
    backtest: BacktestResult | None = None


def _strategy_step(
    candidate: StrategyCandidate, inputs: EvaluationInputs, sim: _Simulation
) -> None:
    request = StrategyRequest(
        strategy=candidate.spec.ref,
        spec_hash=candidate.spec.content_hash(),
        params=FrozenMapping(inputs.params or {}),
        instruments=inputs.instruments,
        knowledge_cutoff=inputs.knowledge_cutoff,
        decision_times=inputs.decision_times,
        signals=inputs.signals,
    )
    sim.strategy_result = candidate.strategy.target_positions(request)
    sim.strategy_result.check_answers(request, candidate.strategy.descriptor)
    sim.risk_results, sim.targets = _risk_step(candidate, inputs, sim.strategy_result)


def _backtest_step(
    inputs: EvaluationInputs, backtester: BacktestProvider, targets: tuple[TargetPosition, ...]
) -> BacktestResult:
    request = BacktestRequest(
        cost_model=inputs.cost_model,
        initial_equity=inputs.initial_equity,
        bars=inputs.bars,
        targets=targets,
    )
    result = backtester.run(request)
    result.check_answers(request, backtester.descriptor)
    return result


def _bar_length(bars: Sequence[PriceBar]) -> timedelta:
    lengths = {bar.interval_end - bar.interval_start for bar in bars}
    if len(lengths) != 1:
        raise ValueError("delay stress needs one uniform bar length")
    return lengths.pop()


def _delayed(
    targets: Sequence[TargetPosition], bars: Sequence[PriceBar], delay_bars: int
) -> tuple[TargetPosition, ...]:
    """Every target executed ``delay_bars`` bars later (same information, later fill).

    A target whose delayed decision has no execution bar left is dropped (it could never fill).
    """
    step = _bar_length(bars) * delay_bars
    ordered = sorted(bars, key=lambda bar: (bar.interval_start, bar.instrument))
    moved: list[TargetPosition] = []
    for target in targets:
        later = target.decision_time + step
        if execution_bar(ordered, target.instrument, later) is None:
            continue
        moved.append(target.model_validate({**target.model_dump(), "decision_time": later}))
    return tuple(moved)


@dataclass(frozen=True, slots=True)
class CandidateTrialRunner:
    """Re-runs strategy → risk → backtest of one candidate (a ``validation.TrialRunner``)."""

    candidate: StrategyCandidate
    inputs: EvaluationInputs
    backtester: BacktestProvider

    def run(
        self,
        params: Mapping[str, ObservationScalar],
        *,
        delay_bars: int = 0,
        decision_offset: timedelta = timedelta(0),
        instruments: tuple[str, ...] | None = None,
    ) -> TrialRun:
        if delay_bars < 0:
            raise ValueError("delay_bars must be >= 0")
        base = self.inputs
        names = base.instruments if instruments is None else tuple(sorted(set(instruments)))
        keep = set(names)
        if not keep <= set(base.instruments):
            raise ValueError(f"unknown instruments {sorted(keep - set(base.instruments))}")
        trial = replace(
            base,
            instruments=names,
            bars=tuple(bar for bar in base.bars if bar.instrument in keep),
            decision_times=tuple(t + decision_offset for t in base.decision_times),
            signals=tuple(item for item in base.signals if item.instrument in keep),
            risk_signals=tuple(item for item in base.risk_signals if item.instrument in keep),
            params=params,
        )
        sim = _Simulation()
        _strategy_step(self.candidate, trial, sim)
        targets = _delayed(sim.targets, trial.bars, delay_bars) if delay_bars else sim.targets
        backtest = _backtest_step(trial, self.backtester, targets)
        return TrialRun(
            targets=targets, backtest=backtest, bars=trial.bars, cost_model=trial.cost_model
        )


def evaluate_strategy(
    candidate: StrategyCandidate,
    inputs: EvaluationInputs,
    *,
    backtester: BacktestProvider,
    registry: FailureRegistry,
    validator: BacktestValidator | None = None,
) -> StrategyEvaluation:
    """Run one trial end to end; failures are appended to ``registry`` (see module docs)."""
    subject = candidate.spec.ref
    sim = _Simulation()
    try:
        _strategy_step(candidate, inputs, sim)
        backtest = _backtest_step(inputs, backtester, sim.targets)
    except (StrategyProviderError, RiskProviderError, BacktestProviderError) as exc:
        return _failed(candidate, registry, ReasonCode.RUN_ERRORED, exc, sim.strategy_result)
    except OutcomeUsedAsInput as exc:
        return _failed(
            candidate,
            registry,
            ReasonCode.OUTCOME_USED_AS_INPUT,
            exc,
            sim.strategy_result,
            terminal_state="REJECTED",
            status=EvaluationStatus.REJECTED,
        )
    except (ValidationError, ValueError) as exc:
        return _failed(candidate, registry, ReasonCode.CONTRACT_VIOLATION, exc, sim.strategy_result)
    strategy_result, risk_results = sim.strategy_result, sim.risk_results

    if validator is None:  # nothing validated it: never a pass
        return StrategyEvaluation(
            subject, EvaluationStatus.NOT_VALIDATED, strategy_result, risk_results, backtest
        )
    try:
        validation = validator.validate(subject, candidate.spec, backtest)
        validation.check_subject(subject)
    except (StrategyProviderError, RiskProviderError, BacktestProviderError) as exc:
        return _failed(candidate, registry, ReasonCode.RUN_ERRORED, exc, strategy_result)
    except OutcomeUsedAsInput as exc:
        return _failed(
            candidate,
            registry,
            ReasonCode.OUTCOME_USED_AS_INPUT,
            exc,
            strategy_result,
            terminal_state="REJECTED",
            status=EvaluationStatus.REJECTED,
        )
    except (ValidationError, ValueError) as exc:
        return _failed(candidate, registry, ReasonCode.CONTRACT_VIOLATION, exc, strategy_result)
    report = validation.report
    if report.verdict is Verdict.PASS:
        status = EvaluationStatus.PASSED
    elif report.verdict is Verdict.INCONCLUSIVE:
        status = EvaluationStatus.INCONCLUSIVE
    else:
        failed_gate = next(gate.gate_id for gate in report.gates if gate.verdict is Verdict.FAIL)
        reason = validation.failure_reason or ReasonCode.CONTRACT_VIOLATION
        technical = reason in _TECHNICAL_FAILURES  # C-P3: not reproducible = FAILED
        record = _failure(
            candidate,
            "FAILED" if technical else "REJECTED",
            reason,
            (
                f"validation_report:{report.report_id}",
                f"run:{report.run_id}",
                f"backtest_result:{backtest.result_hash}",
            ),
            gate_id=failed_gate,
        )
        registry.append(record)
        return StrategyEvaluation(
            subject,
            EvaluationStatus.FAILED if technical else EvaluationStatus.REJECTED,
            strategy_result,
            risk_results,
            backtest,
            validation,
            record,
        )
    return StrategyEvaluation(subject, status, strategy_result, risk_results, backtest, validation)


def _failed(
    candidate: StrategyCandidate,
    registry: FailureRegistry,
    reason: ReasonCode,
    error: Exception,
    strategy_result: StrategyResult | None,
    *,
    terminal_state: str = "FAILED",
    status: EvaluationStatus = EvaluationStatus.FAILED,
) -> StrategyEvaluation:
    record = _failure(
        candidate,
        terminal_state,
        reason,
        (f"error:{type(error).__name__}",),
        lessons=str(error)[:2000],
    )
    registry.append(record)
    return StrategyEvaluation(
        candidate.spec.ref,
        status,
        strategy_result,
        failure=record,
    )

"""Strategy evaluation pipeline: strategy → risk → backtest → validation hook → Failure Registry.

Phase 5 (ADR-0038), research plane. ``evaluate_strategy`` runs one trial (one declared parameter
point) of one library candidate:

1. ``StrategyProvider.target_positions`` over all decision times, checked with
   ``StrategyResult.check_answers``;
2. if the spec names a ``risk_policy``: ``RiskProvider.constrain`` once per decision time, with only
   the risk signals visible at that time and the previous constrained weights as portfolio state
   (``equity=None``: risk runs ahead of the simulation, so path-dependent rules must refuse);
3. ``BacktestProvider.run`` on the (constrained) targets, checked with ``check_answers``;
4. the ``BacktestValidator`` hook (``validation.py``; TODO(phase4-wiring)) — without it the result
   is ``NOT_VALIDATED``, never a pass;
5. a ``FAIL`` verdict (→ ``REJECTED``) or a run / contract error (→ ``FAILED``) is appended to the
   ``FailureRegistry``. Nothing is ever deleted; a retry is a new version.

The pipeline performs no lifecycle transition (that is the Control Plane's) and decides nothing:
the verdict is whatever the validator's report says.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import ValidationError

from core.contracts.feature import ObservationScalar
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
)
from core.domain.base import FrozenMapping, Ref
from core.domain.research import FailureRecord, Verdict
from core.domain.specs import RiskPolicy, StrategySpec
from core.errors import ReasonCode
from research.strategies.failure_registry import FailureRegistry
from research.strategies.validation import BacktestValidation, BacktestValidator

__all__ = [
    "EvaluationInputs",
    "EvaluationStatus",
    "StrategyCandidate",
    "StrategyEvaluation",
    "evaluate_strategy",
]


class EvaluationStatus(StrEnum):
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
    strategy_result: StrategyResult | None = None
    risk_results: tuple[RiskResult, ...] = ()
    try:
        request = StrategyRequest(
            strategy=subject,
            spec_hash=candidate.spec.content_hash(),
            params=FrozenMapping(inputs.params or {}),
            instruments=inputs.instruments,
            knowledge_cutoff=inputs.knowledge_cutoff,
            decision_times=inputs.decision_times,
            signals=inputs.signals,
        )
        strategy_result = candidate.strategy.target_positions(request)
        strategy_result.check_answers(request, candidate.strategy.descriptor)
        risk_results, targets = _risk_step(candidate, inputs, strategy_result)
        backtest_request = BacktestRequest(
            cost_model=inputs.cost_model,
            initial_equity=inputs.initial_equity,
            bars=inputs.bars,
            targets=targets,
        )
        backtest = backtester.run(backtest_request)
        backtest.check_answers(backtest_request, backtester.descriptor)
    except (StrategyProviderError, RiskProviderError, BacktestProviderError) as exc:
        return _failed(candidate, registry, ReasonCode.RUN_ERRORED, exc, strategy_result)
    except (ValidationError, ValueError) as exc:
        return _failed(candidate, registry, ReasonCode.CONTRACT_VIOLATION, exc, strategy_result)

    if validator is None:
        # TODO(phase4-wiring, ADR-0037): pass the Phase 4 validation adapter here.
        return StrategyEvaluation(
            subject, EvaluationStatus.NOT_VALIDATED, strategy_result, risk_results, backtest
        )
    validation = validator.validate(subject, candidate.spec, backtest)
    validation.check_subject(subject)
    report = validation.report
    if report.verdict is Verdict.PASS:
        status = EvaluationStatus.PASSED
    elif report.verdict is Verdict.INCONCLUSIVE:
        status = EvaluationStatus.INCONCLUSIVE
    else:
        failed_gate = next(gate.gate_id for gate in report.gates if gate.verdict is Verdict.FAIL)
        reason = validation.failure_reason or ReasonCode.CONTRACT_VIOLATION
        record = _failure(
            candidate,
            "REJECTED",
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
            EvaluationStatus.REJECTED,
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
) -> StrategyEvaluation:
    record = _failure(
        candidate,
        "FAILED",
        reason,
        (f"error:{type(error).__name__}",),
        lessons=str(error)[:2000],
    )
    registry.append(record)
    return StrategyEvaluation(
        candidate.spec.ref,
        EvaluationStatus.FAILED,
        strategy_result,
        failure=record,
    )

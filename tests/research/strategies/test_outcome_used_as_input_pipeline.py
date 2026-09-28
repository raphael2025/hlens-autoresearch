"""ADR-0086 decision 3: ``evaluate_strategy`` maps ``OutcomeUsedAsInput`` (C-L2 leakage) to a
REJECTED evaluation with ``ReasonCode.OUTCOME_USED_AS_INPUT``, appended to the Failure Registry —
not the generic ``CONTRACT_VIOLATION`` / FAILED path the broader ``ValueError`` catch would give
it (``OutcomeUsedAsInput`` is itself a ``ValueError``, so the guard must run first)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from core.contracts.outcome import OutcomeUsedAsInput
from core.contracts.strategy import (
    BacktestResult,
    StrategyProviderDescriptor,
    StrategyRequest,
    StrategyResult,
)
from core.domain.base import Ref
from core.domain.specs import StrategySpec
from core.errors import ReasonCode
from plugins.backtest import BarBacktester
from research.strategies.failure_registry import FailureRegistry
from research.strategies.library import library_entries
from research.strategies.pipeline import EvaluationInputs, EvaluationStatus, evaluate_strategy
from research.strategies.signals import LOG_RETURN_SIGNAL, bar_signals
from research.strategies.validation import BacktestValidation
from tests.strategy_fixtures import COSTS, MINUTE, T0, make_bars, wave_closes

BARS = make_bars("BTCUSDT", wave_closes(160)) + make_bars("ETHUSDT", wave_closes(160, phase=13))
SIGNALS = tuple(item for item in bar_signals(BARS) if item.signal == LOG_RETURN_SIGNAL)
DECISIONS = tuple(T0 + minute * MINUTE for minute in range(62, 160, 6))
CUTOFF = T0 + timedelta(days=1)
SMALL = {"lookback": 60}


def _inputs() -> EvaluationInputs:
    return EvaluationInputs(
        instruments=("BTCUSDT", "ETHUSDT"),
        bars=BARS,
        decision_times=DECISIONS,
        knowledge_cutoff=CUTOFF,
        cost_model=COSTS,
        initial_equity=Decimal(10000),
        signals=SIGNALS,
        params=SMALL,
    )


@dataclass(frozen=True, slots=True)
class _LeakingStrategy:
    """A stand-in ``StrategyProvider`` whose ``target_positions`` hits the C-L2 guard directly
    (no real Outcome payload needed: the pipeline only cares how the exception is mapped)."""

    descriptor: StrategyProviderDescriptor | None = None

    def target_positions(self, request: StrategyRequest) -> StrategyResult:
        raise OutcomeUsedAsInput("target_positions.signals 收到 Outcome 载荷（C-L2）")


@dataclass(frozen=True, slots=True)
class _LeakingValidator:
    """A stand-in validator: the guard can also fire downstream, inside validation."""

    def validate(
        self, subject: Ref, spec: StrategySpec, backtest: BacktestResult
    ) -> BacktestValidation:
        raise OutcomeUsedAsInput("validate.study.signal_refs 收到 Outcome 载荷（C-L2）")


def test_outcome_leak_in_the_strategy_step_is_rejected_not_failed(tmp_path: Path) -> None:
    entry = library_entries()[0]
    candidate = replace(entry.candidate(), strategy=_LeakingStrategy())
    registry = FailureRegistry(tmp_path / "failures.jsonl")

    result = evaluate_strategy(
        candidate, _inputs(), backtester=BarBacktester(), registry=registry
    )

    assert result.status is EvaluationStatus.REJECTED
    (record,) = registry.records()
    assert record.terminal_state == "REJECTED"
    assert record.reason_code is ReasonCode.OUTCOME_USED_AS_INPUT
    assert record.subject_ref == entry.spec.ref


def test_outcome_leak_in_validation_is_rejected_not_failed(tmp_path: Path) -> None:
    entry = library_entries()[0]
    registry = FailureRegistry(tmp_path / "failures.jsonl")

    result = evaluate_strategy(
        entry.candidate(),
        _inputs(),
        backtester=BarBacktester(),
        registry=registry,
        validator=_LeakingValidator(),
    )

    assert result.status is EvaluationStatus.REJECTED
    (record,) = registry.records()
    assert record.terminal_state == "REJECTED"
    assert record.reason_code is ReasonCode.OUTCOME_USED_AS_INPUT
    assert record.subject_ref == entry.spec.ref


def test_a_plain_contract_violation_is_still_failed_not_rejected(tmp_path: Path) -> None:
    """The new guard must not swallow the pre-existing ``ValueError`` -> FAILED path."""
    entry = library_entries()[0]
    registry = FailureRegistry(tmp_path / "failures.jsonl")
    bad = replace(_inputs(), params={"lookback": 7})  # not a declared parameter point

    result = evaluate_strategy(
        entry.candidate(), bad, backtester=BarBacktester(), registry=registry
    )

    assert result.status is EvaluationStatus.FAILED
    (record,) = registry.records()
    assert record.terminal_state == "FAILED"
    assert record.reason_code is ReasonCode.RUN_ERRORED

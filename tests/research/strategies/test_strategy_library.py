"""Phase 5 smoke tests: research strategies, risk, pipeline and Failure Registry (ADR-0038).

Framework level (FRAMEWORK_IMPLEMENTED / NOT_VALIDATED): no look-ahead, determinism, risk
constraints enforced, sources resolvable in the knowledge base, declared parameter spaces enforced,
and failures appended — never deleted.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from core.contracts.strategy import (
    PortfolioState,
    RiskRequest,
    SignalObservation,
    StrategyRequest,
    StrategyResult,
    TargetPosition,
    UnsupportedStrategy,
)
from core.domain.base import FrozenMapping, Kind, Ref
from core.domain.research import FailureRecord, GateResult, ValidationReport, Verdict
from core.domain.specs import StrategySpec
from core.errors import ReasonCode
from plugins.backtest import BarBacktester
from plugins.knowledge import LocalKnowledgeProvider
from research.strategies.failure_registry import FailureRegistry, FailureRegistryCorrupted
from research.strategies.library import library_entries, resolve_knowledge
from research.strategies.pipeline import (
    EvaluationInputs,
    EvaluationStatus,
    evaluate_strategy,
)
from research.strategies.signals import LOG_RETURN_SIGNAL, bar_signals, realized_vol_signal
from research.strategies.time_series_momentum import TimeSeriesMomentumProvider, tsmom_spec
from research.strategies.validation import BacktestValidation
from research.strategies.volatility_target import (
    VOL_TARGET_RULES,
    VolatilityTargetRiskProvider,
    vol_target_policy,
)
from tests.contract_suites import strategy as strategy_suite
from tests.contract_suites._support import ContractSuiteFailure
from tests.contract_suites.risk import RiskProviderContract, RiskSubject
from tests.contract_suites.strategy import StrategyProviderContract, StrategySubject
from tests.strategy_fixtures import COSTS, MINUTE, T0, make_bars, wave_closes

REPO = Path(__file__).resolve().parents[3]
INSTRUMENTS = ("BTCUSDT", "ETHUSDT")
BARS = make_bars("BTCUSDT", wave_closes(160)) + make_bars("ETHUSDT", wave_closes(160, phase=13))
VOL_WINDOW = 60
ALL_SIGNALS = bar_signals(BARS, vol_windows=(VOL_WINDOW,))
SIGNALS = tuple(item for item in ALL_SIGNALS if item.signal == LOG_RETURN_SIGNAL)
RISK_SIGNALS = tuple(item for item in ALL_SIGNALS if item.signal == realized_vol_signal(VOL_WINDOW))
DECISIONS = tuple(T0 + minute * MINUTE for minute in range(62, 160, 6))
CUTOFF = T0 + timedelta(days=1)
SMALL = {"lookback": 60}


def _request(spec: StrategySpec, params: dict[str, int | bool] | None = None) -> StrategyRequest:
    return StrategyRequest(
        strategy=spec.ref,
        spec_hash=spec.content_hash(),
        params=FrozenMapping(params if params is not None else SMALL),
        instruments=INSTRUMENTS,
        knowledge_cutoff=CUTOFF,
        decision_times=DECISIONS,
        signals=SIGNALS,
    )


def _perturb(item: SignalObservation) -> SignalObservation:
    value = item.value if isinstance(item.value, Decimal) else Decimal(0)
    return item.model_copy(update={"value": -value - Decimal("0.01")})


class TestTimeSeriesMomentum(StrategyProviderContract):
    @pytest.fixture
    def strategy_subject(self) -> StrategySubject:
        return StrategySubject(
            open=TimeSeriesMomentumProvider,
            request=_request(tsmom_spec()),
            unsupported=_request(tsmom_spec(), {"lookback": 61}),
            perturb=_perturb,
        )


def _risk_request(decision_time: datetime, weights: tuple[str, str]) -> RiskRequest:
    policy = vol_target_policy()
    return RiskRequest(
        policy=policy.ref,
        policy_hash=policy.content_hash(),
        decision_time=decision_time,
        knowledge_cutoff=CUTOFF,
        targets=tuple(
            TargetPosition(
                decision_time=decision_time,
                instrument=name,
                target_weight=Decimal(weight),
                inputs_used=1,
                latest_input_available_time=decision_time,
            )
            for name, weight in zip(INSTRUMENTS, weights, strict=True)
        ),
        portfolio=PortfolioState(as_of=decision_time),
        signals=tuple(item for item in RISK_SIGNALS if item.available_time <= decision_time),
    )


RISK_REQUESTS = (
    _risk_request(T0 + 30 * MINUTE, ("0.5", "-0.5")),  # no volatility yet → flat
    _risk_request(T0 + 100 * MINUTE, ("0.5", "-0.5")),
    _risk_request(T0 + 150 * MINUTE, ("1", "1")),
    _risk_request(T0 + 150 * MINUTE, ("0", "0")),
)


class TestVolatilityTarget(RiskProviderContract):
    @pytest.fixture
    def risk_subject(self) -> RiskSubject:
        other = vol_target_policy(target_volatility="0.01")
        return RiskSubject(
            open=VolatilityTargetRiskProvider,
            requests=RISK_REQUESTS,
            unsupported=RISK_REQUESTS[1].model_copy(update={"policy_hash": other.content_hash()}),
            rules=frozenset(VOL_TARGET_RULES),
        )


# --------------------------------------------------------------------------------------
# Risk constraints enforced
# --------------------------------------------------------------------------------------


def test_risk_constraints_are_enforced() -> None:
    provider = VolatilityTargetRiskProvider()
    for request in RISK_REQUESTS:
        result = provider.constrain(request)
        weights = [item.constrained_weight for item in result.positions]
        assert all(abs(weight) <= 1 for weight in weights), "position_cap"
        assert sum(abs(weight) for weight in weights) <= 1, "gross_exposure_cap"
    flat = provider.constrain(RISK_REQUESTS[0])
    assert all(item.constrained_weight == 0 for item in flat.positions)
    assert all(item.binding_rules == ("missing_volatility_flat",) for item in flat.positions)


def test_leverage_position_and_gross_caps_bind() -> None:
    loose = vol_target_policy(target_volatility="0.5")  # far above realized: every cap binds
    provider = VolatilityTargetRiskProvider((loose,))
    request = RISK_REQUESTS[2].model_copy(update={"policy_hash": loose.content_hash()})
    result = provider.constrain(request)
    for item in result.positions:
        assert {"leverage_cap", "position_cap", "gross_exposure_cap"} <= set(item.binding_rules)
    assert sum(abs(item.constrained_weight) for item in result.positions) <= 1


def test_tight_policy_caps_every_position() -> None:
    policy = vol_target_policy(max_abs_weight="0.1", max_gross_exposure="0.15")
    provider = VolatilityTargetRiskProvider((policy,))
    request = RISK_REQUESTS[2].model_copy(update={"policy_hash": policy.content_hash()})
    result = provider.constrain(request)
    assert all(abs(item.constrained_weight) <= Decimal("0.1") for item in result.positions)
    assert sum(abs(item.constrained_weight) for item in result.positions) <= Decimal("0.15")


def test_risk_request_refuses_future_signals() -> None:
    future = RISK_SIGNALS[-1]
    with pytest.raises(ValueError, match="未来函数"):
        RISK_REQUESTS[1].model_copy(update={"signals": (future,)})


# --------------------------------------------------------------------------------------
# Sources and parameter spaces
# --------------------------------------------------------------------------------------


def test_every_entry_has_sources_in_the_knowledge_base_and_a_parameter_space() -> None:
    knowledge = LocalKnowledgeProvider()
    for entry in library_entries():
        assert entry.sources, entry.spec.ref
        items = resolve_knowledge(entry.sources, knowledge)
        assert [item.ref.target_identity() for item in items] == [
            ref.target_identity() for ref in entry.sources
        ]
        assert entry.spec.param_search_space, entry.spec.ref
        assert all(
            entry.spec.params[key] in values
            for key, values in entry.spec.param_search_space.items()
        )
    names = {ref.name for entry in library_entries() for ref in entry.sources}
    assert {"strategy_time_series_momentum", "risk_volatility_managed_portfolios"} <= names


def test_undeclared_parameters_are_refused() -> None:
    provider = TimeSeriesMomentumProvider()
    with pytest.raises(UnsupportedStrategy):
        provider.target_positions(_request(tsmom_spec(), {"window": 60}))
    with pytest.raises(UnsupportedStrategy):
        provider.target_positions(_request(tsmom_spec(), {"long_only": 1}))


def test_nothing_is_promoted() -> None:
    for folder in ("strategies", "risk"):
        assert not list((REPO / folder).rglob("*.py")), f"{folder}/ must hold promoted code only"


# --------------------------------------------------------------------------------------
# Pipeline: determinism, no look-ahead, Failure Registry
# --------------------------------------------------------------------------------------


def _inputs(bars: tuple = BARS, **overrides: object) -> EvaluationInputs:  # type: ignore[type-arg]
    signals = bar_signals(bars, vol_windows=(VOL_WINDOW,))
    base = EvaluationInputs(
        instruments=INSTRUMENTS,
        bars=bars,
        decision_times=DECISIONS,
        knowledge_cutoff=CUTOFF,
        cost_model=COSTS,
        initial_equity=Decimal(10000),
        signals=tuple(item for item in signals if item.signal == LOG_RETURN_SIGNAL),
        risk_signals=tuple(
            item for item in signals if item.signal == realized_vol_signal(VOL_WINDOW)
        ),
        params=SMALL,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


@pytest.mark.parametrize("index", [0, 1])
def test_pipeline_is_deterministic_and_not_validated(tmp_path: Path, index: int) -> None:
    entry = library_entries()[index]
    registry = FailureRegistry(tmp_path / "failures.jsonl")
    first = evaluate_strategy(
        entry.candidate(), _inputs(), backtester=BarBacktester(), registry=registry
    )
    second = evaluate_strategy(
        entry.candidate(), _inputs(), backtester=BarBacktester(), registry=registry
    )
    assert first.status is EvaluationStatus.NOT_VALIDATED
    assert first.backtest is not None and second.backtest is not None
    assert first.backtest.result_hash == second.backtest.result_hash
    assert first.backtest.fills, "the fixture must trade"
    assert registry.records() == ()
    if entry.risk_policy is not None:
        assert len(first.risk_results) == len(DECISIONS)


def test_pipeline_has_no_look_ahead(tmp_path: Path) -> None:
    """Truncating the future (bars and signals after t) leaves the equity curve up to t as is."""
    entry = library_entries()[1]
    registry = FailureRegistry(tmp_path / "failures.jsonl")
    horizon = DECISIONS[len(DECISIONS) // 2]
    full = evaluate_strategy(
        entry.candidate(), _inputs(), backtester=BarBacktester(), registry=registry
    )
    cut_bars = tuple(bar for bar in BARS if bar.interval_end <= horizon)
    truncated = evaluate_strategy(
        entry.candidate(),
        _inputs(cut_bars, decision_times=tuple(t for t in DECISIONS if t <= horizon)),
        backtester=BarBacktester(),
        registry=registry,
    )
    assert full.backtest is not None and truncated.backtest is not None
    prefix = [p for p in full.backtest.equity_curve if p.time <= horizon]
    assert prefix == list(truncated.backtest.equity_curve)


class _FailingValidator:
    """A stand-in for the Phase 4 pipeline that fails one gate (no real threshold involved)."""

    def validate(self, subject: Ref, spec: StrategySpec, backtest: object) -> BacktestValidation:
        report = ValidationReport(
            report_id="report-1",
            run_id="run-1",
            subject=subject,
            experiment_hash="0" * 64,
            constitution_version="1.0.0",
            validation_profile=Ref(kind=Kind.PROFILE, name="stand_in", version="0.0.1"),
            validation_profile_hash="1" * 64,
            gates=(GateResult(gate_id="G2", metric="stand_in", value=0.0, verdict=Verdict.FAIL),),
            verdict=Verdict.FAIL,
        )
        return BacktestValidation(report=report, failure_reason=ReasonCode.COST_KILLED)


def test_rejected_strategies_are_appended_to_the_failure_registry(tmp_path: Path) -> None:
    entry = library_entries()[0]
    registry = FailureRegistry(tmp_path / "failures.jsonl")
    result = evaluate_strategy(
        entry.candidate(),
        _inputs(),
        backtester=BarBacktester(),
        registry=registry,
        validator=_FailingValidator(),
    )
    assert result.status is EvaluationStatus.REJECTED
    (record,) = registry.records()
    assert record.terminal_state == "REJECTED" and record.gate_id == "G2"
    assert record.reason_code is ReasonCode.COST_KILLED
    assert record.subject_ref == entry.spec.ref
    assert "validation_report:report-1" in record.evidence


def test_run_errors_are_failed_and_history_is_kept(tmp_path: Path) -> None:
    entry = library_entries()[0]
    path = tmp_path / "failures.jsonl"
    registry = FailureRegistry(path)
    bad = _inputs(params={"lookback": 7})  # not a declared point
    for _ in range(2):
        outcome = evaluate_strategy(
            entry.candidate(), bad, backtester=BarBacktester(), registry=registry
        )
        assert outcome.status is EvaluationStatus.FAILED
    records = FailureRegistry(path).records()
    assert len(records) == 2 and all(r.terminal_state == "FAILED" for r in records)
    assert all(r.reason_code is ReasonCode.RUN_ERRORED for r in records)
    assert not any(hasattr(registry, name) for name in ("delete", "remove", "clear", "update"))


def test_the_registry_refuses_rewritten_history(tmp_path: Path) -> None:
    path = tmp_path / "failures.jsonl"
    registry = FailureRegistry(path)
    record = FailureRecord(
        subject_ref=tsmom_spec().ref,
        terminal_state="REJECTED",
        reason_code=ReasonCode.OOS_DECAY,
    )
    registry.append(record)
    registry.append(record)
    path.write_text(path.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    with pytest.raises(FailureRegistryCorrupted):
        registry.append(record)


# --------------------------------------------------------------------------------------
# The suites catch faulty implementations
# --------------------------------------------------------------------------------------


class _PeekingMomentum(TimeSeriesMomentumProvider):
    """Faulty: sizes every computable position on the last (future) signal state."""

    def target_positions(self, request: StrategyRequest) -> StrategyResult:
        last = request.signals[-1].available_time
        widened = request.model_copy(update={"decision_times": (last,)})
        future = {
            item.instrument: item.target_weight
            for item in super().target_positions(widened).positions
        }
        honest = super().target_positions(request)
        positions = tuple(
            item.model_copy(update={"target_weight": future[item.instrument]})
            if item.inputs_used
            else item
            for item in honest.positions
        )
        return StrategyResult.build(request, self.descriptor, positions)


def test_the_strategy_suite_catches_look_ahead() -> None:
    subject = StrategySubject(
        open=_PeekingMomentum,
        request=_request(tsmom_spec()),
        unsupported=_request(tsmom_spec(), {"lookback": 61}),
        perturb=_perturb,
    )
    with pytest.raises(ContractSuiteFailure, match="changed"):
        strategy_suite.check_causality(subject)

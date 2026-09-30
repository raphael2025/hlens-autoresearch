"""``tsmom_bars_vol_scaled@1.0.0`` through the G0 – G4 validator wiring (strategy-library gap).

docs/research/strategy-library.md listed the volatility-targeted variant as having "no G0 – G4
wiring or golden experiment". These tests run the library entry (strategy → ``vol_target_bars``
risk → backtest → ``PipelineBacktestValidator``) on the same TEST ONLY synthetic setup as
``test_backtest_validation`` (every number there is an arbitrary, uncalibrated fixture value):

- with its risk signal (``bar_realized_vol_60``) the report covers G0 – G4, the re-run reproduces
  the risk-constrained backtest (``G0.reproducibility``) and the verdict is ``derive_verdict``;
- without it (today's research loop, which supplies no risk signals — gap ST-2 / R-2) every
  position is flattened by ``missing_volatility_flat`` and the evaluation is ``INCONCLUSIVE``
  (``G0.data_available``: no non-flat target), never a PASS and never a Failure Registry record.

No verdict of the synthetic run is asserted: it is not validation evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from core.contracts.strategy import ConstrainedPosition, PortfolioState, RiskRequest
from core.contracts.synthetic import SyntheticMarket
from core.domain.research import Verdict, derive_verdict
from plugins.backtest import BarBacktester
from research.strategies.failure_registry import FailureRegistry
from research.strategies.library import library_entries
from research.strategies.pipeline import (
    CandidateTrialRunner,
    EvaluationInputs,
    EvaluationStatus,
    StrategyEvaluation,
    evaluate_strategy,
)
from research.strategies.signals import LOG_RETURN_SIGNAL, bar_signals, realized_vol_signal
from research.strategies.validation import PipelineBacktestValidator
from research.strategies.volatility_target import VolatilityTargetRiskProvider
from tests.research.strategies.test_backtest_validation import _inputs, _market, _setup

VOL_WINDOW = 60  # the window of vol_target_bars@1.0.0's default volatility signal

_STATUS = {
    Verdict.PASS: EvaluationStatus.PASSED,
    Verdict.INCONCLUSIVE: EvaluationStatus.INCONCLUSIVE,
    Verdict.FAIL: EvaluationStatus.REJECTED,
}


def _with_risk_signals(market: SyntheticMarket) -> EvaluationInputs:
    base = _inputs(market)
    signals = bar_signals(base.bars, vol_windows=(VOL_WINDOW,))
    return replace(
        base,
        signals=tuple(item for item in signals if item.signal == LOG_RETURN_SIGNAL),
        risk_signals=tuple(
            item for item in signals if item.signal == realized_vol_signal(VOL_WINDOW)
        ),
    )


def _evaluate(
    market: SyntheticMarket, inputs: EvaluationInputs, tmp_path: Path
) -> tuple[StrategyEvaluation, FailureRegistry]:
    entry = library_entries()[1]
    assert entry.spec.name == "tsmom_bars_vol_scaled" and entry.risk_policy is not None
    candidate = entry.candidate()
    registry = FailureRegistry(tmp_path / "failures.jsonl")
    trials = CandidateTrialRunner(candidate, inputs, BarBacktester())
    result = evaluate_strategy(
        candidate,
        inputs,
        backtester=BarBacktester(),
        registry=registry,
        validator=PipelineBacktestValidator(_setup(market, candidate, trials=trials)),
    )
    return result, registry


@pytest.fixture(scope="module")
def planted(tmp_path_factory: pytest.TempPathFactory) -> tuple[StrategyEvaluation, FailureRegistry]:
    market = _market(seed=7, planted=True)
    return _evaluate(market, _with_risk_signals(market), tmp_path_factory.mktemp("vol_scaled"))


def test_the_vol_scaled_variant_is_validated_through_g0_to_g4(
    planted: tuple[StrategyEvaluation, FailureRegistry],
) -> None:
    result, registry = planted
    assert result.validation is not None and result.backtest is not None
    assert result.risk_results, "the risk policy ran at every decision time"
    report = result.validation.report
    assert report.subject == library_entries()[1].spec.ref
    assert report.verdict is derive_verdict(report.gates)
    assert result.status is _STATUS[report.verdict]
    stages = {gate.gate_id.split(".")[0] for gate in report.gates}
    assert stages == {"G0", "G1", "G2", "G3", "G4"}, sorted(g.gate_id for g in report.gates)
    by_id = {gate.gate_id: gate for gate in report.gates}
    assert by_id["G0.reproducibility"].verdict is Verdict.PASS  # the risk step is re-run too
    assert by_id["G0.backtest_cost_model"].verdict is Verdict.PASS
    assert by_id["G1.label_blind_sides"].verdict is Verdict.PASS
    if report.verdict is Verdict.FAIL:
        (record,) = registry.records()
        assert record.subject_ref == report.subject
    else:
        assert registry.records() == ()


def test_without_its_risk_signal_the_variant_is_flat_and_inconclusive(tmp_path: Path) -> None:
    market = _market(seed=7, planted=True)
    inputs = _inputs(market)
    assert inputs.risk_signals == ()  # what the research loop supplies today
    result, registry = _evaluate(market, inputs, tmp_path)
    constrained = [p for risk in result.risk_results for p in risk.positions]
    assert constrained and all(p.constrained_weight == 0 for p in constrained)
    assert any("missing_volatility_flat" in p.binding_rules for p in constrained)
    assert result.validation is not None
    report = result.validation.report
    assert report.verdict is Verdict.INCONCLUSIVE
    assert result.status is EvaluationStatus.INCONCLUSIVE
    data = next(gate for gate in report.gates if gate.gate_id == "G0.data_available")
    assert (data.verdict, data.metric) == (Verdict.INCONCLUSIVE, "non_flat_targets")
    assert registry.records() == ()


# ---------------------------------------------------------------------------------------------
# PM F-A / ADR-0088 decision 3: the pipeline's risk step now goes through
# ``BarBacktester.run_with_risk`` (a realized ``PortfolioState.equity`` / ``peak_equity``) instead
# of running risk ahead of the simulation with ``equity=None``. ``vol_target_bars`` never reads
# ``PortfolioState`` at all, so its result through the new path must be exactly what the old
# ``equity=None`` convention computed — checked decision by decision below.
# ---------------------------------------------------------------------------------------------


def _summary(
    positions: Sequence[ConstrainedPosition],
) -> list[tuple[str, Decimal, tuple[str, ...]]]:
    return [(p.instrument, p.constrained_weight, p.binding_rules) for p in positions]


def test_vol_target_bars_is_bit_identical_through_run_with_risk(tmp_path: Path) -> None:
    market = _market(seed=7, planted=True)
    inputs = _with_risk_signals(market)
    entry = library_entries()[1]
    assert entry.risk_policy is not None
    candidate = entry.candidate()
    registry = FailureRegistry(tmp_path / "failures.jsonl")
    result = evaluate_strategy(candidate, inputs, backtester=BarBacktester(), registry=registry)

    assert result.strategy_result is not None
    assert len(result.risk_results) == len(inputs.decision_times)
    provider = VolatilityTargetRiskProvider((entry.risk_policy,))
    ordered_signals = sorted(inputs.risk_signals, key=lambda item: item.available_time)
    for decision_time, actual in zip(inputs.decision_times, result.risk_results, strict=True):
        assert actual.decision_time == decision_time
        upstream = result.strategy_result.at(decision_time)
        old_style_request = RiskRequest(
            policy=entry.risk_policy.ref,
            policy_hash=entry.risk_policy.content_hash(),
            decision_time=decision_time,
            knowledge_cutoff=inputs.knowledge_cutoff,
            targets=upstream,
            portfolio=PortfolioState(as_of=decision_time),  # equity=None: the pre-fix convention
            signals=tuple(s for s in ordered_signals if s.available_time <= decision_time),
        )
        expected = provider.constrain(old_style_request)
        assert _summary(actual.positions) == _summary(expected.positions)
    assert result.backtest is not None and result.backtest.fills, "the fixture must trade"

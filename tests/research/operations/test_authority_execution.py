"""ADR-0098 (Returns, H7) / ADR-0100 修订 2: the window execution inputs of
``research.operations.authority``.

The caller's ``WindowExecution`` (backtest provider, bound cost model, equity, declared target
source) must carry exactly the identities the baseline run recorded — provider descriptor hash,
cost model ref + hash, strategy ref + spec hash, risk policy, params as canonical JSON, plugins —
and its decision grid / equity must equal the run's ``hlens.p11.inputs@1.0.0`` record. A run
without the record (never backfilled) is ``execution_unrecorded``. The window backtest is a real
``BarBacktester`` run on TEST ONLY bars; each target is checked against the window. Every number
is arbitrary.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts.strategy import TargetPosition
from core.domain.base import canonical_json
from plugins.backtest import BarBacktester, ExecutionModel
from research.experiments.run_inputs import RUN_INPUTS_KEY
from research.operations.authority import (
    BASELINE_BINDING_MISMATCH,
    BASELINE_INPUT_UNRECORDED,
    EXECUTION_MISMATCH,
    EXECUTION_UNRECORDED,
    AuthorityRefused,
    _check_baseline_run,
    _check_execution,
    _run_inputs,
    _window_returns,
)
from research.operations.degradation import ObservationWindow
from tests import factories
from tests.research.operations import authority_fixtures as af

INSTRUMENTS = ("BTC-USDT", "ETH-USDT")
START = datetime(2023, 11, 14, 22, 5, tzinfo=UTC)
WINDOW = ObservationWindow(start=START, end=START + 12 * af.MINUTE, label="TEST ONLY")
PROFILE = af.profile()
RUN = af.baseline_run(PROFILE)
BARS = af.bars(INSTRUMENTS, START, 12)


def _mismatch(execution: Any, run: Any = RUN, code: str = EXECUTION_MISMATCH) -> str:
    with pytest.raises(AuthorityRefused) as refused:
        _check_execution(execution, run)
    assert refused.value.code == code
    return str(refused.value)


def _with_identity(**changes: Any) -> Any:
    instruments = changes.pop("instruments", INSTRUMENTS)
    return af.execution(
        RUN,
        INSTRUMENTS,
        targets=af.DeclaredTargets(af.identity(RUN, instruments, **changes)),
    )


# ---- identities ---------------------------------------------------------------------------


def test_the_recorded_execution_is_accepted() -> None:
    descriptor, identity = _check_execution(af.execution(RUN, INSTRUMENTS), RUN)
    assert descriptor == BarBacktester().descriptor
    assert identity.instruments == INSTRUMENTS


def test_only_a_window_execution_is_accepted() -> None:
    _mismatch(object())


def test_another_backtest_provider_is_refused() -> None:
    variant = BarBacktester(execution=ExecutionModel(short_borrow_rate=Decimal("0.01")))
    assert variant.descriptor.plugin_key != BarBacktester().descriptor.plugin_key
    message = _mismatch(af.execution(RUN, INSTRUMENTS, backtester=variant))
    assert "not the one the baseline run recorded" in message

    class NoDescriptor:
        def run(self, request: object) -> object:
            raise AssertionError("never run")

    _mismatch(af.execution(RUN, INSTRUMENTS, backtester=NoDescriptor()))


def test_another_cost_model_is_refused() -> None:
    other_rates = af.COST_MODEL.model_copy(update={"fee_rate_per_side": Decimal("0.001")})
    assert other_rates.ref == af.COST_MODEL.ref  # same ref, other content hash
    _mismatch(af.execution(RUN, INSTRUMENTS, cost_model=other_rates))
    renamed = af.COST_MODEL.model_copy(update={"name": "cost_v2"})
    _mismatch(af.execution(RUN, INSTRUMENTS, cost_model=renamed))
    _mismatch(af.execution(RUN, INSTRUMENTS, cost_model="cost_v1"))


@pytest.mark.parametrize("equity", [Decimal(0), Decimal(-1), Decimal("NaN"), 10000])
def test_a_non_positive_or_non_decimal_equity_is_refused(equity: object) -> None:
    _mismatch(af.execution(RUN, INSTRUMENTS, initial_equity=equity))


def test_a_target_source_without_an_identity_is_refused() -> None:
    class Anonymous:
        def targets(self, bars: object, window: object) -> tuple[()]:
            return ()

    _mismatch(af.execution(RUN, INSTRUMENTS, targets=Anonymous()))


@pytest.mark.parametrize(
    "changes",
    [
        {"strategy_ref": factories.strategy_ref(name="s_other")},
        {"strategy_spec_hash": "7" * 64},
        {"risk_policy_ref": factories.risk_ref(), "risk_policy_hash": "8" * 64},
        {"risk_policy_hash": "8" * 64},
        {"params": {"lookback": 3}},
        {"params": {"lookback": 2.0}},  # canonical JSON: 2 and 2.0 differ (修订 2 §4)
        {"params": {"lookback": True}},
        {"params": {"lookback": 2, "extra": 1}},
        {"params": {**af.PARAMS, RUN_INPUTS_KEY: "x"}},  # the record is not a strategy param
        {"plugins": {}},
        {"plugins": {af.STRATEGY_PLUGIN[0]: "9" * 64}},
        {"plugins": {"unrecorded@1.0.0": "9" * 64}},
        {"instruments": ()},
        {"instruments": ("BTC-USDT", "BTC-USDT")},
        {"instruments": ("BTC-USDT", "")},
    ],
)
def test_every_declared_identity_must_be_the_baseline_runs(changes: dict[str, Any]) -> None:
    _mismatch(_with_identity(**changes))


def test_a_run_without_a_strategy_ref_is_refused() -> None:
    no_strategy = factories.experiment_run(
        repro=factories.repro_tuple(
            strategy_ref=None,
            risk_policy_ref=None,
            cost_model_ref=af.COST_MODEL.ref,
            dependency_hashes={
                **factories.dependency_hashes(factories.hypothesis_ref(), factories.outcome_ref()),
                str(af.COST_MODEL.ref): af.COST_MODEL.content_hash(),
            },
            plugin_versions=dict(RUN.repro.plugin_versions),
        )
    )
    _mismatch(af.execution(no_strategy, INSTRUMENTS), run=no_strategy)


# ---- the decision grid and equity against the run inputs record --------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"decision_step": timedelta(minutes=5)},
        {"decision_warmup": timedelta(minutes=1)},
        {"decision_step": timedelta(0)},
        {"decision_warmup": timedelta(seconds=-1)},
        {"initial_equity": Decimal("20000")},
        {"initial_equity": Decimal("10000.00")},  # same value, other text: not the same record
    ],
)
def test_the_decision_grid_and_equity_must_be_the_recorded_ones(changes: dict[str, Any]) -> None:
    _mismatch(_with_identity(**changes))


def test_the_declared_equity_must_be_the_executions_equity() -> None:
    execution = af.execution(RUN, INSTRUMENTS, initial_equity=Decimal("20000"))
    assert "not the window execution's" in _mismatch(execution)


def test_a_recorded_grid_that_differs_from_the_declared_one_is_refused() -> None:
    hourly = af.baseline_run(PROFILE, inputs=af.run_inputs(decision_step=timedelta(hours=1)))
    message = _mismatch(af.execution(hourly, INSTRUMENTS), run=hourly)
    assert "decision grid / initial equity is not the one the baseline run recorded" in message


def test_a_run_without_the_record_is_execution_unrecorded() -> None:
    old = af.baseline_run(PROFILE, record=False)
    message = _mismatch(af.execution(old, INSTRUMENTS), run=old, code=EXECUTION_UNRECORDED)
    assert "never backfilled" in message
    assert _run_inputs(old, EXECUTION_UNRECORDED) is None


def test_an_invalid_record_is_refused_with_the_callers_code() -> None:
    payload = af.run_inputs().payload()
    for broken in (
        canonical_json({**payload, "format": "hlens.p11.inputs@9.0.0"}),
        canonical_json({**payload, "execution": {}}),
        json.dumps(payload),  # the same payload, not in its canonical text
        "not json",
    ):
        params = {**af.PARAMS, RUN_INPUTS_KEY: broken}
        repro = RUN.repro.model_copy(update={"params": params})
        bad = factories.experiment_run(run_id=RUN.run_id, repro=repro)
        _mismatch(af.execution(bad, INSTRUMENTS), run=bad, code=EXECUTION_UNRECORDED)
        with pytest.raises(AuthorityRefused) as refused:
            _run_inputs(bad, BASELINE_INPUT_UNRECORDED)
        assert refused.value.code == BASELINE_INPUT_UNRECORDED


# ---- the window backtest ------------------------------------------------------------------


def _returns(positions: tuple[TargetPosition, ...] = (), **kwargs: Any) -> Any:
    execution = af.execution(RUN, INSTRUMENTS, positions, **kwargs)
    descriptor, identity = _check_execution(execution, RUN)
    return _window_returns(execution, descriptor, identity, BARS, WINDOW)


def test_the_window_backtest_runs_the_declared_targets_through_the_provider() -> None:
    positions = af.long_targets(BARS)
    returns, result, payload = _returns(positions)
    assert len(returns.times) >= 1 and all(WINDOW.start < t <= WINDOW.end for t in returns.times)
    assert result.fills  # the long targets traded
    assert payload["backtest_provider"] == BarBacktester().descriptor.plugin_key
    assert payload["cost_model_hash"] == af.COST_MODEL.content_hash()
    assert payload["initial_equity"] == str(af.INITIAL_EQUITY)
    assert payload["backtest_result_hash"] == result.result_hash
    again = _returns(positions)
    assert again[2] == payload  # deterministic


def test_a_target_outside_the_window_or_its_instruments_is_refused() -> None:
    first = af.long_targets(BARS)[0]
    for bad in (
        first.model_copy(update={"decision_time": WINDOW.end}),
        first.model_copy(update={"instrument": "SOL-USDT"}),
    ):
        with pytest.raises(AuthorityRefused) as refused:
            _returns((bad,))
        assert refused.value.code == EXECUTION_MISMATCH


def test_a_target_input_known_by_its_decision_time_is_accepted() -> None:
    """The resolver's "input available after the window end" check is defence in depth: a
    ``TargetPosition`` itself cannot use an input later than its decision time, and the decision
    time must lie inside the window."""
    late = TargetPosition(
        decision_time=WINDOW.end - af.MINUTE,
        instrument="BTC-USDT",
        target_weight=Decimal("0.5"),
        inputs_used=1,
        latest_input_available_time=WINDOW.end - af.MINUTE,
    )
    _returns((late,))
    with pytest.raises(ValidationError):
        TargetPosition(
            decision_time=WINDOW.end - af.MINUTE,
            instrument="BTC-USDT",
            target_weight=Decimal("0.5"),
            inputs_used=1,
            latest_input_available_time=WINDOW.end + timedelta(seconds=1),
        )


def test_a_refusing_target_source_is_an_execution_mismatch() -> None:
    refusing = af.DeclaredTargets(af.identity(RUN, INSTRUMENTS), refusal="TEST ONLY refusal")
    with pytest.raises(AuthorityRefused) as refused:
        _returns(targets=refusing)
    assert refused.value.code == EXECUTION_MISMATCH and "TEST ONLY refusal" in str(refused.value)


# ---- the baseline run ---------------------------------------------------------------------


def test_the_baseline_run_must_be_the_reports_run_under_the_profile() -> None:
    report = af.report_of(PROFILE, RUN)
    _check_baseline_run(af.SUBJECT, PROFILE, report, RUN)
    other_run = af.baseline_run(PROFILE, run_id="run-authority-2")
    other_profile = af.profile({af.METRIC: "0.25"})
    for subject, bound, report_, run in (
        (factories.strategy_ref(name="s_other"), PROFILE, report, RUN),
        (af.SUBJECT, PROFILE, report, other_run),
        (af.SUBJECT, other_profile, report, RUN),
        (af.SUBJECT, PROFILE, report, object()),
    ):
        with pytest.raises(AuthorityRefused) as refused:
            _check_baseline_run(subject, bound, report_, run)  # type: ignore[arg-type]
        assert refused.value.code == BASELINE_BINDING_MISMATCH

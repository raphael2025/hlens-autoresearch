"""Phase 5 contract layer: Strategy / Risk / Backtest DTO invariants (ADR-0038)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.contracts import strategy as contracts
from core.contracts.registry import CONTRACT_MODELS, export_json_schemas
from core.contracts.strategy import (
    BacktestCostModel,
    Fill,
    PortfolioState,
    PriceBar,
    RiskRequest,
    SignalObservation,
    StrategyProviderDescriptor,
    StrategyRequest,
    StrategyResult,
    TargetPosition,
)
from core.domain.base import FrozenMapping, Kind, Ref
from tests.strategy_fixtures import MINUTE, T0

CURRENT_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
P5_MODELS = (
    contracts.SignalObservation,
    contracts.StrategyRequest,
    contracts.TargetPosition,
    contracts.StrategyResult,
    contracts.StrategyProviderDescriptor,
    contracts.PortfolioState,
    contracts.RiskRequest,
    contracts.ConstrainedPosition,
    contracts.RiskResult,
    contracts.RiskProviderDescriptor,
    contracts.BacktestCostModel,
    contracts.PriceBar,
    contracts.BacktestRequest,
    contracts.Fill,
    contracts.EquityPoint,
    contracts.BacktestResult,
    contracts.BacktestProviderDescriptor,
)
FEATURE = Ref(kind=Kind.FEATURE, name="bar_log_return", version="1.0.0")
STRATEGY = Ref(kind=Kind.STRATEGY, name="demo", version="1.0.0")


def _signal(
    minute: int, value: Decimal | None = Decimal("0.01"), **kw: object
) -> SignalObservation:
    fields: dict[str, object] = {
        "signal": FEATURE,
        "instrument": "BTCUSDT",
        "event_time": T0 + minute * MINUTE,
        "available_time": T0 + minute * MINUTE,
        "knowledge_time": T0 + minute * MINUTE,
        "value": value,
    }
    fields.update(kw)
    return SignalObservation.model_validate(fields)


def _target(minute: int, weight: str = "1", inputs: int = 1) -> TargetPosition:
    return TargetPosition(
        decision_time=T0 + minute * MINUTE,
        instrument="BTCUSDT",
        target_weight=Decimal(weight),
        inputs_used=inputs,
        latest_input_available_time=T0 + minute * MINUTE if inputs else None,
    )


def test_p5_appends_seventeen_models_with_exported_schemas(tmp_path: Path) -> None:
    names = tuple(model.__name__ for model in CONTRACT_MODELS)
    assert len(names) == 118
    # Phases append in merge order: the 17 P5 models form one contiguous block after F4.
    start = min(names.index(model.__name__) for model in P5_MODELS)
    assert start >= 79
    assert set(names[start : start + 17]) == {model.__name__ for model in P5_MODELS}
    written = export_json_schemas(tmp_path)
    for model in P5_MODELS:
        committed = (CURRENT_SCHEMA_DIR / f"{model.__name__}.schema.json").read_bytes()
        assert committed == written[model.__name__].read_bytes(), model.__name__


def test_outcome_is_never_a_signal() -> None:
    with pytest.raises(ValidationError, match="C-L2"):
        _signal(1, signal=Ref(kind=Kind.OUTCOME, name="fwd_return", version="1.0.0"))


@pytest.mark.parametrize("bad", [0.5, Decimal("NaN"), Decimal("Infinity")])
def test_floats_and_non_finite_numbers_are_refused(bad: object) -> None:
    with pytest.raises(ValidationError):
        TargetPosition(
            decision_time=T0,
            instrument="BTCUSDT",
            target_weight=bad,  # type: ignore[arg-type]
            inputs_used=1,
            latest_input_available_time=T0,
        )
    with pytest.raises(ValidationError):
        BacktestCostModel(name="c", version="1.0.0", fee_rate=bad, slippage_rate=0)  # type: ignore[arg-type]


def test_target_without_inputs_is_flat_and_inputs_are_not_from_the_future() -> None:
    with pytest.raises(ValidationError, match="不持仓"):
        _target(1, "1", inputs=0)
    with pytest.raises(ValidationError, match="未来函数"):
        TargetPosition(
            decision_time=T0,
            instrument="BTCUSDT",
            target_weight=Decimal(1),
            inputs_used=1,
            latest_input_available_time=T0 + MINUTE,
        )


def test_strategy_request_visibility_and_cutoff() -> None:
    request = StrategyRequest(
        strategy=STRATEGY,
        spec_hash="a" * 64,
        instruments=("BTCUSDT",),
        knowledge_cutoff=T0 + 10 * MINUTE,
        decision_times=(T0 + 2 * MINUTE, T0 + 5 * MINUTE),
        signals=(_signal(3), _signal(1), _signal(1, available_time=T0 + 2 * MINUTE)),
    )
    visible = request.visible_at(T0 + 2 * MINUTE)
    assert len(visible) == 1 and visible[0].available_time == T0 + 2 * MINUTE  # PIT replacement
    with pytest.raises(ValidationError, match="knowledge_cutoff"):
        request.model_copy(update={"knowledge_cutoff": T0})
    with pytest.raises(ValidationError, match="instruments"):
        request.model_copy(update={"signals": (_signal(1, instrument="ETHUSDT"),)})


def test_strategy_result_hash_and_answers() -> None:
    request = StrategyRequest(
        strategy=STRATEGY,
        spec_hash="a" * 64,
        instruments=("BTCUSDT",),
        knowledge_cutoff=T0 + 10 * MINUTE,
        decision_times=(T0 + 2 * MINUTE,),
        signals=(_signal(1), _signal(3)),
    )
    descriptor = StrategyProviderDescriptor(
        name="demo",
        version="1.0.0",
        deterministic=True,
        supported_strategies=FrozenMapping({str(STRATEGY): "a" * 64}),
    )
    good = StrategyResult.build(
        request,
        descriptor,
        (_target(2).model_copy(update={"latest_input_available_time": T0 + MINUTE}),),
    )
    good.check_answers(request, descriptor)
    peeked = StrategyResult.build(
        request,
        descriptor,
        (_target(2).model_copy(update={"latest_input_available_time": T0 + 2 * MINUTE}),),
    )
    with pytest.raises(ValueError, match="可见"):
        peeked.check_answers(request, descriptor)
    with pytest.raises(ValidationError, match="result_hash"):
        StrategyResult.model_validate({**good.model_dump(), "result_hash": "b" * 64})


def test_risk_request_is_structurally_causal() -> None:
    policy = Ref(kind=Kind.RISK, name="demo_risk", version="1.0.0")
    fields: dict[str, object] = {
        "policy": policy,
        "policy_hash": "c" * 64,
        "decision_time": T0 + 2 * MINUTE,
        "knowledge_cutoff": T0 + 10 * MINUTE,
        "targets": (_target(2),),
        "portfolio": PortfolioState(as_of=T0 + 2 * MINUTE),
    }
    RiskRequest.model_validate(fields)
    with pytest.raises(ValidationError, match="未来函数"):
        RiskRequest.model_validate({**fields, "signals": (_signal(3),)})
    with pytest.raises(ValidationError, match="未来函数"):
        RiskRequest.model_validate({**fields, "portfolio": PortfolioState(as_of=T0 + 3 * MINUTE)})
    with pytest.raises(ValidationError, match="kind=risk"):
        RiskRequest.model_validate({**fields, "policy": STRATEGY})


def test_bars_and_fills_are_causal() -> None:
    with pytest.raises(ValidationError, match="interval_end"):
        PriceBar(
            instrument="BTCUSDT",
            interval_start=T0,
            interval_end=T0 + MINUTE,
            available_time=T0,
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal(1),
            close=Decimal(1),
        )
    with pytest.raises(ValidationError, match="未来函数"):
        Fill(
            instrument="BTCUSDT",
            decision_time=T0 + MINUTE,
            fill_time=T0,
            reference_price=Decimal(1),
            fill_price=Decimal(1),
            quantity=Decimal(1),
            fee=Decimal(0),
            slippage_cost=Decimal(0),
        )


def test_backtest_descriptor_is_simulation_only() -> None:
    with pytest.raises(ValidationError):
        contracts.BacktestProviderDescriptor.model_validate(
            {
                "name": "x",
                "version": "1.0.0",
                "deterministic": True,
                "simulation_only": False,
                "execution_model": "next_bar_open",
            }
        )

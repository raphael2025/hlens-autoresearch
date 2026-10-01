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
    assert len(names) == 148  # +2 PitConflict* models: ADR-0094 (b6f9e11)
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


# ---------------------------------------------------------------------------------------
# ADR-0054: partial-fill carry-over (additive; omitted from payloads when absent)
# ---------------------------------------------------------------------------------------


def _bar(**kw: object) -> PriceBar:
    fields: dict[str, object] = {
        "instrument": "BTCUSDT",
        "interval_start": T0,
        "interval_end": T0 + MINUTE,
        "available_time": T0 + MINUTE,
        "open": Decimal(1),
        "high": Decimal(1),
        "low": Decimal(1),
        "close": Decimal(1),
    }
    fields.update(kw)
    return PriceBar.model_validate(fields)


def _remainder(**kw: object) -> contracts.FillRemainder:
    fields: dict[str, object] = {
        "instrument": "BTCUSDT",
        "decision_time": T0,
        "requested_quantity": Decimal(100),
        "filled_quantity": Decimal(40),
        "remaining_quantity": Decimal(60),
        "ended_by": "superseded",
        "ended_at": T0 + 2 * MINUTE,
    }
    fields.update(kw)
    return contracts.FillRemainder.model_validate(fields)


def test_carry_over_appends_one_model_with_an_exported_schema(tmp_path: Path) -> None:
    names = tuple(model.__name__ for model in CONTRACT_MODELS)
    assert names[134] == "FillRemainder"  # ADR-0054's block; ADR-0077 appended after it
    written = export_json_schemas(tmp_path)
    committed = (CURRENT_SCHEMA_DIR / "FillRemainder.schema.json").read_bytes()
    assert committed == written["FillRemainder"].read_bytes()


def test_bar_volume_is_optional_non_negative_and_omitted_when_absent() -> None:
    bare = _bar()
    assert bare.volume is None
    assert "volume" not in bare.model_dump(mode="json")
    assert "volume" not in bare.model_dump()
    assert _bar(volume=Decimal(0)).model_dump(mode="json")["volume"] == "0"
    assert _bar(volume=Decimal(5)).content_hash() != bare.content_hash()
    assert PriceBar.model_validate_json(_bar(volume=Decimal(5)).model_dump_json()).volume == 5
    for bad in (Decimal(-1), 0.5, Decimal("NaN")):
        with pytest.raises(ValidationError):
            _bar(volume=bad)


def test_the_descriptor_accepts_exactly_the_two_execution_models() -> None:
    fields = {"name": "x", "version": "1.0.0", "deterministic": True, "simulation_only": True}
    for model in ("next_bar_open", "next_bar_open_participation"):
        descriptor = contracts.BacktestProviderDescriptor.model_validate(
            {**fields, "execution_model": model}
        )
        assert descriptor.execution_model == model
    with pytest.raises(ValidationError):
        contracts.BacktestProviderDescriptor.model_validate(
            {**fields, "execution_model": "next_bar_close"}
        )


def test_a_fill_remainder_is_self_consistent() -> None:
    assert _remainder().remaining_quantity == 60
    filled = _remainder(
        requested_quantity=Decimal(-100),
        filled_quantity=Decimal(-100),
        remaining_quantity=Decimal(0),
        ended_by="filled",
    )
    assert filled.ended_by == "filled"
    unfilled = _remainder(filled_quantity=Decimal(0), remaining_quantity=Decimal(100))
    assert unfilled.ended_by == "superseded"
    bad: list[tuple[dict[str, object], str]] = [
        ({"requested_quantity": Decimal(0), "filled_quantity": Decimal(0)}, "零目标变化量"),
        ({"filled_quantity": Decimal(-40), "remaining_quantity": Decimal(60)}, "方向"),
        ({"filled_quantity": Decimal(140), "remaining_quantity": Decimal(0)}, "超过"),
        ({"remaining_quantity": Decimal(59)}, "remaining_quantity"),
        ({"ended_by": "filled"}, "filled 当且仅当"),
        ({"filled_quantity": Decimal(100), "remaining_quantity": Decimal(0)}, "filled 当且仅当"),
        ({"ended_at": T0 - MINUTE}, "ended_at"),
        ({"ended_by": "cancelled"}, "ended_by"),
    ]
    for update, message in bad:
        with pytest.raises(ValidationError, match=message):
            _remainder(**update)


def test_result_remainders_are_ordered_and_omitted_from_the_hash_when_empty() -> None:
    second = _bar(
        interval_start=T0 + MINUTE, interval_end=T0 + 2 * MINUTE, available_time=T0 + 2 * MINUTE
    )
    request = contracts.BacktestRequest(
        cost_model=BacktestCostModel(
            name="c", version="1.0.0", fee_rate=Decimal(0), slippage_rate=Decimal(0)
        ),
        initial_equity=Decimal(1),
        bars=(_bar(), second),
        targets=(),
    )
    descriptor = contracts.BacktestProviderDescriptor(
        name="x",
        version="1.0.0",
        deterministic=True,
        simulation_only=True,
        execution_model="next_bar_open_participation",
    )
    curve = (
        contracts.EquityPoint(
            time=T0 + MINUTE, cash=Decimal(1), equity=Decimal(1), gross_exposure=Decimal(0)
        ),
    )
    empty = contracts.BacktestResult.build(
        request, descriptor, fills=(), equity_curve=curve, unexecuted_targets=0
    )
    assert "remainders" not in empty.model_dump(mode="json")
    assert "remainders" not in empty._hashed_fields()
    first = _remainder()
    later = _remainder(decision_time=T0 + MINUTE)
    with_records = contracts.BacktestResult.build(
        request,
        descriptor,
        fills=(),
        equity_curve=curve,
        unexecuted_targets=0,
        remainders=(first, later),
    )
    assert with_records.result_hash != empty.result_hash
    revived = contracts.BacktestResult.model_validate_json(with_records.model_dump_json())
    assert revived == with_records
    for disordered in ((later, first), (first, first)):
        with pytest.raises(ValidationError, match="remainders"):
            contracts.BacktestResult.build(
                request,
                descriptor,
                fills=(),
                equity_curve=curve,
                unexecuted_targets=0,
                remainders=disordered,
            )

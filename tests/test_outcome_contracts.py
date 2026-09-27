"""Phase 4 contracts: OutcomeProvider DTOs and cost model v1 (ADR-0037).

The central acceptance item: **an Outcome can never be used as an input** (Constitution C-L2),
enforced by the contract (``label_only`` discriminator + ``extra="forbid"`` input DTOs + the
information-flow whitelists of ADR-0012) and proven here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.contracts.cost_model import CostModelSpec
from core.contracts.feature import FeatureObservation, FeatureRequest
from core.contracts.outcome import (
    OUTCOME_MODELS,
    OutcomeEvent,
    OutcomeLabel,
    OutcomeLabelSpec,
    OutcomeMethod,
    OutcomePriceBar,
    OutcomeProviderDescriptor,
    OutcomeRequest,
    OutcomeResult,
    OutcomeUsedAsInput,
    is_outcome_payload,
    refuse_outcome_input,
)
from core.contracts.registry import CONTRACT_MODELS, export_json_schemas
from core.domain.base import Contract, FrozenMapping, Kind, Ref
from core.domain.specs import DatasetRef, FeatureSpec, OutcomeSpec, StrategySpec, Zone

REPO = Path(__file__).resolve().parents[1]
T0 = datetime(2024, 1, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HASH = "9" * 64

P4_MODELS = (
    OutcomeLabelSpec,
    OutcomePriceBar,
    OutcomeEvent,
    OutcomeRequest,
    OutcomeLabel,
    OutcomeProviderDescriptor,
    OutcomeResult,
    CostModelSpec,
)


def _spec() -> OutcomeSpec:
    return OutcomeSpec(
        name="fwd_10m", version="1.0.0", created_at=T0, horizon=10 * MINUTE, label_definition="x"
    )


def _label(**overrides: object) -> OutcomeLabel:
    fields: dict[str, object] = {
        "event_key": "e1",
        "event_time": T0,
        "value": Decimal("0.01"),
        "entry_time": T0,
        "exit_time": T0 + 10 * MINUTE,
        "entry_price": Decimal(100),
        "exit_price": Decimal(101),
        "available_time": T0 + 10 * MINUTE,
    }
    fields.update(overrides)
    return OutcomeLabel(**fields)  # type: ignore[arg-type]


def _bar(minute: int, **overrides: object) -> OutcomePriceBar:
    start = T0 + minute * MINUTE
    fields: dict[str, object] = {
        "interval_start": start,
        "interval_end": start + MINUTE,
        "available_time": start + MINUTE,
        "open": Decimal(100),
        "high": Decimal(101),
        "low": Decimal(99),
        "close": Decimal(100),
    }
    fields.update(overrides)
    return OutcomePriceBar(**fields)  # type: ignore[arg-type]


def _request(**overrides: object) -> OutcomeRequest:
    fields: dict[str, object] = {
        "label_spec": OutcomeLabelSpec.bind(_spec(), OutcomeMethod.FORWARD_RETURN),
        "manifest_content_hash": HASH,
        "price_cutoff": T0 + 20 * MINUTE,
        "events": (OutcomeEvent(event_key="e1", event_time=T0),),
        "bars": tuple(_bar(minute) for minute in range(20)),
    }
    fields.update(overrides)
    return OutcomeRequest(**fields)  # type: ignore[arg-type]


# ======================================================================================
# Outcome is never an input (C-L2)
# ======================================================================================


def test_an_outcome_label_is_not_a_feature_observation() -> None:
    label = _label()
    with pytest.raises(ValidationError):
        FeatureObservation.model_validate(label)
    with pytest.raises(ValidationError):
        FeatureObservation.model_validate(label.model_dump())
    with pytest.raises(ValidationError):
        FeatureObservation.model_validate_json(label.model_dump_json())


def test_an_outcome_label_cannot_enter_a_feature_request() -> None:
    with pytest.raises(ValidationError):
        FeatureRequest(
            feature=Ref(kind=Kind.FEATURE, name="f", version="1.0.0"),
            spec_hash=HASH,
            manifest_content_hash=HASH,
            knowledge_cutoff=T0,
            evaluation_times=(T0,),
            observations=(_label(),),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("model", OUTCOME_MODELS, ids=lambda model: model.__name__)
def test_every_outcome_payload_carries_the_label_only_discriminator(model: type[Contract]) -> None:
    assert model.model_fields["label_only"].default is True
    schema = model.model_json_schema()
    assert schema["properties"]["label_only"]["const"] is True


def test_even_a_label_dump_padded_with_observation_fields_is_refused() -> None:
    payload = _label().model_dump(mode="json")
    payload.update(
        observation_key="e1",
        knowledge_time=payload["available_time"],
        values={},
        lineage={},
    )
    with pytest.raises(ValidationError, match="label_only"):
        FeatureObservation.model_validate(payload)


def test_runtime_guard_refuses_outcome_payloads() -> None:
    label = _label()
    assert is_outcome_payload(label)
    assert is_outcome_payload(label.model_dump(mode="json"))
    assert not is_outcome_payload({"value": 1})
    with pytest.raises(OutcomeUsedAsInput):
        refuse_outcome_input([Decimal(1), label], "features")
    refuse_outcome_input([Decimal(1), {"x": 1}], "features")


def test_outcome_refs_and_zones_are_refused_as_inputs() -> None:
    outcome = _spec().ref
    with pytest.raises(ValidationError):
        FeatureSpec(
            name="f", version="1.0.0", definition="d", inputs=(outcome,), available_lag=MINUTE
        )
    with pytest.raises(ValidationError):
        FeatureSpec(
            name="f",
            version="1.0.0",
            definition="d",
            inputs=(
                DatasetRef(
                    zone=Zone.OUTCOME,
                    table="t",
                    snapshot_id="s",
                    time_range_start=T0,
                    time_range_end=T0,
                ),
            ),
            available_lag=MINUTE,
        )
    with pytest.raises(ValidationError):
        StrategySpec(name="s", version="1.0.0", signals=(outcome,))


# ======================================================================================
# DTO invariants
# ======================================================================================


def test_label_spec_binds_the_outcome_spec() -> None:
    spec = _spec()
    bound = OutcomeLabelSpec.bind(spec, OutcomeMethod.FORWARD_RETURN)
    assert bound.matches(spec)
    assert bound.horizon == spec.horizon
    other = OutcomeSpec(
        name="fwd_10m", version="1.0.0", created_at=T0, horizon=5 * MINUTE, label_definition="x"
    )
    assert not bound.matches(other)


@pytest.mark.parametrize(
    "fields",
    [
        {"method": OutcomeMethod.FORWARD_RETURN, "upper_barrier": Decimal("0.01")},
        {"method": OutcomeMethod.TRIPLE_BARRIER, "upper_barrier": Decimal("0.01")},
        {
            "method": OutcomeMethod.TRIPLE_BARRIER,
            "upper_barrier": Decimal("0.01"),
            "lower_barrier": Decimal(1),
        },
        {"method": OutcomeMethod.FORWARD_RETURN, "horizon": timedelta(0)},
        {
            "method": OutcomeMethod.FORWARD_RETURN,
            "outcome": Ref(kind=Kind.FEATURE, name="f", version="1.0.0"),
        },
    ],
)
def test_label_spec_shape_is_enforced(fields: dict[str, object]) -> None:
    payload: dict[str, object] = {
        "outcome": _spec().ref,
        "outcome_spec_hash": HASH,
        "horizon": MINUTE,
    }
    payload.update(fields)
    with pytest.raises(ValidationError):
        OutcomeLabelSpec(**payload)  # type: ignore[arg-type]


def test_prices_refuse_floats_and_non_finite_values() -> None:
    with pytest.raises(ValidationError):
        _bar(0, open=100.0)
    with pytest.raises(ValidationError):
        _bar(0, close=Decimal("NaN"))
    with pytest.raises(ValidationError):
        _bar(0, available_time=T0)  # available before the bar has ended
    with pytest.raises(ValidationError):
        _bar(0, high=Decimal("99.5"))  # high below open


@pytest.mark.parametrize(
    "overrides",
    [
        {"entry_time": T0 - MINUTE},  # label window before the event
        {"available_time": T0 + 5 * MINUTE},  # known before its exit
        {"exit_time": T0},
        {"value": None},  # None with details attached
        {"entry_price": None},  # computable without details
        {"value": 0.01},
    ],
)
def test_label_shape_is_enforced(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _label(**overrides)


def test_not_computable_label_is_explicit() -> None:
    label = OutcomeLabel(event_key="e", event_time=T0, value=None)
    assert label.value is None and label.available_time is None


def test_request_invariants() -> None:
    with pytest.raises(ValidationError):
        _request(price_cutoff=T0 + 5 * MINUTE)  # bars available after the cutoff
    with pytest.raises(ValidationError):
        _request(bars=(_bar(0), _bar(0)))  # overlapping bars
    with pytest.raises(ValidationError):
        _request(
            events=(
                OutcomeEvent(event_key="e", event_time=T0),
                OutcomeEvent(event_key="e", event_time=T0 + MINUTE),
            )
        )
    a = OutcomeEvent(event_key="a", event_time=T0)
    b = OutcomeEvent(event_key="b", event_time=T0 + MINUTE)
    assert _request(events=(a, b)).content_hash() == _request(events=(b, a)).content_hash()


def test_result_hash_is_recomputed() -> None:
    request = _request()
    descriptor = OutcomeProviderDescriptor(
        name="p",
        version="1.0.0",
        deterministic=True,
        supported_outcomes=FrozenMapping(
            {str(request.label_spec.outcome): request.label_spec.content_hash()}
        ),
    )
    label = OutcomeLabel(event_key="e1", event_time=T0, value=None)
    result = OutcomeResult.build(request, descriptor, (label,))
    result.check_answers(request, descriptor)
    payload = result.model_dump()
    payload["result_hash"] = "0" * 64
    with pytest.raises(ValidationError):
        OutcomeResult.model_validate(payload)


# ======================================================================================
# Cost model v1
# ======================================================================================


def test_cost_model_round_trip_cost() -> None:
    model = CostModelSpec(
        name="cost_v1",
        version="1.0.0",
        fee_rate_per_side=Decimal("0.0004"),
        slippage_rate_per_side=Decimal("0.0001"),
    )
    assert model.kind is Kind.COST_MODEL
    assert model.round_trip_cost() == Decimal("0.0010")
    assert model.round_trip_cost(Decimal(2)) == Decimal("0.0020")
    with pytest.raises(ValueError):
        model.round_trip_cost(Decimal(0))


def test_cost_model_cannot_be_skipped_or_float() -> None:
    with pytest.raises(ValidationError, match="跳过成本模型"):
        CostModelSpec(
            name="c",
            version="1.0.0",
            fee_rate_per_side=Decimal(0),
            slippage_rate_per_side=Decimal(0),
        )
    with pytest.raises(ValidationError):
        CostModelSpec(
            name="c",
            version="1.0.0",
            fee_rate_per_side=0.001,  # type: ignore[arg-type]
            slippage_rate_per_side=Decimal(0),
        )


# ======================================================================================
# Registry: append only
# ======================================================================================


def test_p4_appends_its_models_and_exports_them(tmp_path: Path) -> None:
    names = [model.__name__ for model in CONTRACT_MODELS]
    positions = [names.index(model.__name__) for model in P4_MODELS]
    assert positions == sorted(positions) and positions[0] >= 87  # after everything before P4
    written = export_json_schemas(tmp_path)
    for model in P4_MODELS:
        committed = (REPO / "schemas" / f"{model.__name__}.schema.json").read_bytes()
        assert committed == written[model.__name__].read_bytes(), model.__name__

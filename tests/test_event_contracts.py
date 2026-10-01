"""EventProvider DTOs (Phase 3; ADR-0036): invariants, registry, schema export."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts import event as event_module
from core.contracts.event import (
    Event,
    EventInputPoint,
    EventProviderDescriptor,
    EventRequest,
    EventResult,
)
from core.contracts.registry import CONTRACT_MODELS, export_json_schemas
from core.domain.base import FrozenMapping, Kind, Ref, content_hash
from tests.fake_events import LAG, MINUTE, REGIME_INPUTS, T0, X_INPUTS, X, x_point

CURRENT_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
EVENT_MODELS = (EventInputPoint, Event, EventRequest, EventResult, EventProviderDescriptor)
E = Ref(kind=Kind.EVENT, name="e", version="1.0.0")
SPEC_HASH = content_hash({"spec": "e"})


def _event(**overrides: Any) -> Event:
    fields: dict[str, Any] = {
        "event": E,
        "spec_hash": SPEC_HASH,
        "event_time": X_INPUTS[3].available_time + LAG,
        "attributes": {"direction": "up", "value": Decimal(6)},
        "inputs": X_INPUTS[2:4],
    }
    fields.update(overrides)
    return Event.build(**fields)


def _descriptor() -> EventProviderDescriptor:
    return EventProviderDescriptor(
        name="fake",
        version="1.0.0",
        deterministic=True,
        supported_events=FrozenMapping({str(E): SPEC_HASH}),
    )


def _request(**overrides: Any) -> EventRequest:
    fields: dict[str, Any] = {
        "event": E,
        "spec_hash": SPEC_HASH,
        "as_of": T0 + 20 * MINUTE,
        "inputs": X_INPUTS,
    }
    fields.update(overrides)
    return EventRequest(**fields)


# ======================================================================================
# Registry and schemas: append only
# ======================================================================================


def test_phase3_appends_five_contiguous_models(tmp_path: Path) -> None:
    names = tuple(model.__name__ for model in CONTRACT_MODELS)
    assert len(names) == 148  # +2 PitConflict* models: ADR-0094 (b6f9e11)
    start = names.index("EventInputPoint")
    assert start >= 87  # appended after every earlier model
    assert names[start : start + 5] == tuple(model.__name__ for model in EVENT_MODELS)
    written = export_json_schemas(tmp_path)
    for model in EVENT_MODELS:
        committed = (CURRENT_SCHEMA_DIR / f"{model.__name__}.schema.json").read_bytes()
        assert committed == written[model.__name__].read_bytes(), model.__name__


def test_event_probe_mirrors_event_without_the_id() -> None:
    probe = set(event_module._EventProbe.model_fields)
    assert probe == set(Event.model_fields) - {"event_id"}


# ======================================================================================
# EventInputPoint
# ======================================================================================


def test_input_point_invariants() -> None:
    good = X_INPUTS[0]
    with pytest.raises(ValidationError, match="available_time"):
        good.model_copy(update={"available_time": good.evaluation_time - MINUTE})
    with pytest.raises(ValidationError, match="feature 或 state"):
        good.model_copy(update={"source": Ref(kind=Kind.OUTCOME, name="y", version="1.0.0")})
    for bad in (1.5, float("nan"), Decimal("NaN"), Decimal("Infinity")):
        payload = good.model_dump()
        payload["value"] = bad
        with pytest.raises(ValidationError):
            EventInputPoint.model_validate(payload)
    assert REGIME_INPUTS[0].value == "calm"
    assert EventInputPoint.model_validate_json(good.model_dump_json()) == good


# ======================================================================================
# Event
# ======================================================================================


def test_event_id_binds_the_content() -> None:
    item = _event()
    assert Event.model_validate_json(item.model_dump_json()) == item
    payload = item.model_dump()
    payload["event_time"] = item.event_time + MINUTE
    with pytest.raises(ValidationError, match="event_id"):
        Event.model_validate(payload)
    assert _event(attributes={"direction": "down"}).event_id != item.event_id


def test_event_invariants() -> None:
    with pytest.raises(ValidationError, match="kind=event"):
        _event(event=X)
    with pytest.raises(ValidationError, match="可追溯"):
        _event(inputs=())
    payload = _event().model_dump()
    payload["input_ids"] = tuple(reversed(payload["input_ids"]))
    with pytest.raises(ValidationError, match="升序"):
        Event.model_validate(payload)
    with pytest.raises(ValidationError):
        _event(attributes={"x": 1.5})


# ======================================================================================
# EventRequest
# ======================================================================================


def test_request_is_canonical_and_refuses_ambiguity() -> None:
    assert _request(inputs=tuple(reversed(X_INPUTS))) == _request()
    with pytest.raises(ValidationError, match="重复"):
        _request(inputs=(*X_INPUTS, X_INPUTS[0].model_copy(update={"value": Decimal(99)})))
    late_early = (
        x_point(0, 1).model_copy(update={"available_time": T0 + 5 * MINUTE}),
        x_point(1, 2),
    )
    with pytest.raises(ValidationError, match="只追加"):
        _request(inputs=late_early)
    with pytest.raises(ValidationError, match="自身"):
        _request(upstream_events=(_event(),))
    with pytest.raises(ValidationError, match="kind=event"):
        _request(event=X)


def test_visible_set_and_truncation() -> None:
    request = _request()
    at = T0 + 7 * MINUTE
    points, upstream = request.visible_at(at, LAG)
    assert all(item.available_time + LAG <= at for item in points)
    assert {item.evaluation_time for item in points} == {T0 + m * MINUTE for m in range(5)}
    assert upstream == ()
    cut = request.truncated(at, LAG)
    assert cut.as_of == at and cut.inputs == points
    with pytest.raises(ValueError):
        request.visible_at(at, -LAG)


# ======================================================================================
# EventResult
# ======================================================================================


def test_result_hash_and_order() -> None:
    request = _request()
    result = EventResult.build(request, _descriptor(), [_event(), _event()])
    assert len(result.events) == 1  # the same event twice is one row
    assert EventResult.model_validate_json(result.model_dump_json()) == result
    payload = result.model_dump()
    payload["result_hash"] = content_hash({"forged": True})
    with pytest.raises(ValidationError, match="result_hash"):
        EventResult.model_validate(payload)
    with pytest.raises(ValidationError, match="as_of"):
        EventResult.build(request.truncated(T0, LAG), _descriptor(), [_event()])


def test_check_answers_enforces_observable_time_and_lineage() -> None:
    request = _request()
    descriptor = _descriptor()
    EventResult.build(request, descriptor, [_event()]).check_answers(request, descriptor, LAG)
    early = _event(event_time=X_INPUTS[3].available_time)
    with pytest.raises(ValueError, match="尚不可见"):
        EventResult.build(request, descriptor, [early]).check_answers(request, descriptor, LAG)
    late = _event(event_time=X_INPUTS[3].available_time + 2 * LAG)
    with pytest.raises(ValueError, match="可观测时间"):
        EventResult.build(request, descriptor, [late]).check_answers(request, descriptor, LAG)
    foreign = _event(inputs=(x_point(3, 7),))
    with pytest.raises(ValueError, match="没有的输入点"):
        EventResult.build(request, descriptor, [foreign]).check_answers(request, descriptor, LAG)
    other = _event(spec_hash=content_hash({"other": 1}))
    with pytest.raises(ValueError, match="事件定义"):
        EventResult.build(request, descriptor, [other]).check_answers(request, descriptor, LAG)


def test_descriptor_shape() -> None:
    descriptor = _descriptor()
    assert descriptor.plugin_key == "fake@1.0.0"
    assert descriptor.supports(E, SPEC_HASH)
    with pytest.raises(ValidationError):
        EventProviderDescriptor(
            name="fake", version="1.0.0", deterministic=True, supported_events=FrozenMapping({})
        )
    with pytest.raises(ValidationError):
        EventProviderDescriptor(
            name="fake",
            version="1.0.0",
            deterministic=True,
            supported_events=FrozenMapping({str(X): SPEC_HASH}),
        )
    schema = json.loads((CURRENT_SCHEMA_DIR / "EventProviderDescriptor.schema.json").read_text())
    assert schema["properties"]["deterministic"]["const"] is True


def test_lag_is_a_timedelta() -> None:
    with pytest.raises(ValueError):
        _request().visible_at(T0, timedelta(minutes=-1))

"""StateProvider DTO 与 Protocol 的契约测试（Phase 2；ADR-0035 §2 / §3）。"""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts import state
from core.contracts.registry import CONTRACT_MODELS, export_json_schemas
from core.contracts.state import (
    StateInput,
    StateProviderDescriptor,
    StateRequest,
    StateResult,
    StateValue,
    parse_state_method,
    state_method,
)
from core.domain.base import FrozenMapping, Kind, Ref, content_hash
from tests.fake_states import MINUTE, SOURCE, VOL_FEATURE, at, value_input

REPO = Path(__file__).resolve().parents[1]
CURRENT_SCHEMA_DIR = REPO / "schemas"
STATE_MODELS = (StateInput, StateRequest, StateValue, StateResult, StateProviderDescriptor)
STATE = Ref(kind=Kind.STATE, name="s", version="1.0.0")
SPEC_HASH = content_hash({"spec": "s"})
DESCRIPTOR = StateProviderDescriptor(
    name="fixture",
    version="1.0.0",
    deterministic=True,
    supported_states=FrozenMapping({"state:s@1.0.0": SPEC_HASH}),
)


def _request(**overrides: Any) -> StateRequest:
    fields: dict[str, Any] = {
        "state": STATE,
        "spec_hash": SPEC_HASH,
        "evaluation_times": (at(1), at(2)),
        "inputs": tuple(value_input(VOL_FEATURE, i, Decimal(i)) for i in range(4)),
    }
    fields.update(overrides)
    return StateRequest(**fields)


# ======================================================================================
# 注册表与 Schema：只追加
# ======================================================================================


def test_phase2_only_appends_five_models_to_the_registry(tmp_path: Path) -> None:
    names = tuple(model.__name__ for model in CONTRACT_MODELS)
    assert len(names) == 96
    # Phases append in merge order: the five state models are one contiguous block after F4.
    start = names.index(STATE_MODELS[0].__name__)
    assert start >= 79
    assert names[start : start + 5] == tuple(model.__name__ for model in STATE_MODELS)
    written = export_json_schemas(tmp_path)
    for model in STATE_MODELS:
        committed = (CURRENT_SCHEMA_DIR / f"{model.__name__}.schema.json").read_bytes()
        assert committed == written[model.__name__].read_bytes(), model.__name__


def test_the_module_exports_its_models_and_protocol() -> None:
    assert {model.__name__ for model in STATE_MODELS} | {"StateProvider"} <= set(state.__all__)


def test_the_module_reads_no_clock() -> None:
    tree = ast.parse(inspect.getsource(state))
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attrs & {"now", "utcnow", "today", "time_ns", "monotonic"}


# ======================================================================================
# 不变量
# ======================================================================================


def test_outcomes_can_never_be_state_inputs() -> None:
    for kind in (Kind.OUTCOME, Kind.STATE, Kind.EVENT, Kind.STRATEGY):
        with pytest.raises(ValidationError, match="kind=feature"):
            StateInput(
                feature=Ref(kind=kind, name="y", version="1.0.0"),
                evaluation_time=at(0),
                value=Decimal(1),
                source_result_hash=SOURCE,
            )


def test_inputs_are_canonical_and_unique() -> None:
    request = _request()
    assert _request(inputs=tuple(reversed(request.inputs))) == request
    with pytest.raises(ValidationError, match="重复"):
        _request(inputs=(*request.inputs, request.inputs[0]))
    with pytest.raises(ValidationError, match="严格升序"):
        _request(evaluation_times=(at(2), at(1)))
    with pytest.raises(ValidationError, match="kind=state"):
        _request(state=VOL_FEATURE)


def test_visible_set_is_past_only_and_windowed() -> None:
    request = _request()
    assert [i.evaluation_time for i in request.visible_at(at(2), None)] == [at(0), at(1), at(2)]
    assert [i.evaluation_time for i in request.visible_at(at(2), MINUTE)] == [at(2)]
    assert [i.evaluation_time for i in request.visible_at(at(3), 2 * MINUTE)] == [at(2), at(3)]
    with pytest.raises(ValueError):
        request.visible_at(at(2), -MINUTE)


def test_state_value_is_explicit_none_never_filled() -> None:
    StateValue(evaluation_time=at(1), state=None, inputs_used=0)
    with pytest.raises(ValidationError, match="不填补"):
        StateValue(evaluation_time=at(1), state="x", inputs_used=0)
    with pytest.raises(ValidationError):
        StateValue(evaluation_time=at(1), state="x", inputs_used=1, latest_input_time=at(2))
    with pytest.raises(ValidationError):
        StateValue(evaluation_time=at(1), state="x", inputs_used=1)


def test_result_hash_is_recomputed_and_forgery_refused() -> None:
    request = _request()
    values = [
        StateValue(evaluation_time=t, state="x", inputs_used=1, latest_input_time=t)
        for t in request.evaluation_times
    ]
    result = StateResult.build(request, DESCRIPTOR, values)
    assert StateResult.model_validate_json(result.model_dump_json()) == result
    payload = result.model_dump()
    payload["result_hash"] = content_hash({"forged": True})
    with pytest.raises(ValidationError, match="result_hash"):
        StateResult.model_validate(payload)


def test_descriptor_is_deterministic_and_state_keyed() -> None:
    assert DESCRIPTOR.supports(STATE, SPEC_HASH)
    assert not DESCRIPTOR.supports(STATE, content_hash({"other": 1}))
    with pytest.raises(ValidationError):
        StateProviderDescriptor(
            name="fixture",
            version="1.0.0",
            deterministic=False,  # type: ignore[arg-type]
            supported_states=FrozenMapping({"state:s@1.0.0": SPEC_HASH}),
        )
    with pytest.raises(ValidationError):
        StateProviderDescriptor(
            name="fixture",
            version="1.0.0",
            deterministic=True,
            supported_states=FrozenMapping({"feature:s@1.0.0": SPEC_HASH}),
        )


def test_float_values_are_refused() -> None:
    with pytest.raises(ValidationError):
        StateInput(
            feature=VOL_FEATURE,
            evaluation_time=at(0),
            value=0.5,  # type: ignore[arg-type]
            source_result_hash=SOURCE,
        )


# ======================================================================================
# method 编码
# ======================================================================================


def test_method_encoding_round_trips_and_is_canonical() -> None:
    method = state_method("m", {"b": 2, "a": "0.5", "flag": True})
    assert method == 'm:{"a":"0.5","b":2,"flag":true}'
    assert parse_state_method(method) == ("m", {"a": "0.5", "b": 2, "flag": True})
    for bad in ('m: {"a":1}', 'm:{"a":0.5}', "m", "m:[1]", 'm:{"a":null}'):
        with pytest.raises(ValueError):
            parse_state_method(bad)
    with pytest.raises(ValueError):
        state_method("m", {"a": 0.5})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        state_method("a:b", {})

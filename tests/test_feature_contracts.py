"""FeatureProvider DTO 与 Protocol 的契约测试（Phase 1 F4；ADR-0030 §2 / §3）。"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts import feature
from core.contracts.feature import (
    FeatureObservation,
    FeatureRequest,
    FeatureResult,
    FeatureValue,
    ProviderDescriptor,
)
from core.contracts.registry import CONTRACT_MODELS, export_json_schemas
from core.domain.base import Kind, Ref, content_hash
from tests.fake_features import (
    CUTOFF,
    EVALUATION_TIMES,
    MANIFEST,
    OBSERVATIONS,
    T0,
    LatestValueProvider,
    fake_spec,
    observation,
)

REPO = Path(__file__).resolve().parents[1]
CURRENT_SCHEMA_DIR = REPO / "schemas"
F4_MODELS = (FeatureObservation, FeatureRequest, FeatureValue, FeatureResult, ProviderDescriptor)
SPEC = fake_spec("latest_x")
MINUTE = timedelta(minutes=1)
#: SHA-256 of the committed FeatureSpec schema before F4 (unchanged by ADR-0030).
FEATURE_SPEC_SCHEMA_SHA256 = "241372e342e14635fdd4f505d58b74e318da5af5952462d162edc59aaa8da471"


def _request(**overrides: Any) -> FeatureRequest:
    fields: dict[str, Any] = {
        "feature": SPEC.ref,
        "spec_hash": SPEC.content_hash(),
        "manifest_content_hash": MANIFEST,
        "knowledge_cutoff": CUTOFF,
        "evaluation_times": EVALUATION_TIMES,
        "observations": OBSERVATIONS,
    }
    fields.update(overrides)
    return FeatureRequest(**fields)


# ======================================================================================
# 注册表与 Schema：只追加
# ======================================================================================


def test_f4_only_appends_five_models_to_the_registry(tmp_path: Path) -> None:
    names = tuple(model.__name__ for model in CONTRACT_MODELS)
    assert len(names) == 135
    assert names[74:79] == tuple(model.__name__ for model in F4_MODELS)  # later phases append
    written = export_json_schemas(tmp_path)
    for model in F4_MODELS:
        committed = (CURRENT_SCHEMA_DIR / f"{model.__name__}.schema.json").read_bytes()
        assert committed == written[model.__name__].read_bytes(), model.__name__


def test_feature_spec_schema_is_unchanged() -> None:
    committed = (CURRENT_SCHEMA_DIR / "FeatureSpec.schema.json").read_bytes()
    assert hashlib.sha256(committed).hexdigest() == FEATURE_SPEC_SCHEMA_SHA256


def test_the_module_exports_its_models_and_protocol() -> None:
    assert {model.__name__ for model in F4_MODELS} | {"FeatureProvider"} <= set(feature.__all__)
    assert not {name for name in feature.__all__ if name.startswith("_")}


def test_the_module_reads_no_clock_and_imports_no_outer_layer() -> None:
    tree = ast.parse(inspect.getsource(feature))
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attrs & {"now", "utcnow", "today", "time_ns", "monotonic"}
    roots = {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert roots <= {
        "__future__",
        "collections",
        "datetime",
        "decimal",
        "itertools",
        "typing",
        "pydantic",
        "core",
    }


def test_schemas_state_the_shapes() -> None:
    descriptor = json.loads((CURRENT_SCHEMA_DIR / "ProviderDescriptor.schema.json").read_text())
    assert descriptor["properties"]["deterministic"]["const"] is True
    assert list(descriptor["properties"]["supported_features"]["patternProperties"]) == [
        feature.FEATURE_REF_KEY_PATTERN
    ]
    request = json.loads((CURRENT_SCHEMA_DIR / "FeatureRequest.schema.json").read_text())
    assert request["properties"]["evaluation_times"]["minItems"] == 1
    value = json.loads((CURRENT_SCHEMA_DIR / "FeatureValue.schema.json").read_text())
    assert "value" in value["required"]  # None must be explicit


# ======================================================================================
# FeatureObservation
# ======================================================================================


def test_an_observation_is_not_available_before_it_is_observable() -> None:
    item = OBSERVATIONS[0]
    with pytest.raises(ValidationError, match="可被观察"):
        item.model_copy(update={"available_time": item.event_time - MINUTE})
    with pytest.raises(ValidationError, match="event_end_time"):
        item.model_copy(update={"event_end_time": item.event_time})
    bar = item.model_copy(
        update={
            "event_end_time": item.event_time + MINUTE,
            "available_time": item.event_time + MINUTE,
        }
    )
    with pytest.raises(ValidationError, match="可被观察"):
        bar.model_copy(update={"available_time": item.event_time + MINUTE / 2})


@pytest.mark.parametrize(
    "bad",
    [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity"), 1.5, float("inf")],
)
def test_observation_values_refuse_non_finite_and_float(bad: object) -> None:
    payload = OBSERVATIONS[0].model_dump()
    payload["values"] = {"x": bad}
    with pytest.raises(ValidationError):
        FeatureObservation.model_validate(payload)


@pytest.mark.parametrize("literal", ["1.5", "NaN", "Infinity", "1e400"])
def test_json_numbers_are_refused_in_observation_values(literal: str) -> None:
    raw = json.loads(OBSERVATIONS[0].model_dump_json())
    text = json.dumps(raw).replace('"x": "1.5"', f'"x": {literal}')
    assert text != json.dumps(raw)
    with pytest.raises(ValidationError):
        FeatureObservation.model_validate_json(text)


def test_numeric_text_must_be_a_decimal_and_values_round_trip() -> None:
    with pytest.raises(ValidationError, match="Decimal"):
        OBSERVATIONS[0].model_copy(update={"values": {"x": "1.5"}})
    item = OBSERVATIONS[0].model_copy(
        update={"values": {"x": Decimal("1.50"), "n": 3, "flag": True, "tag": "not-a-number"}}
    )
    again = FeatureObservation.model_validate_json(item.model_dump_json())
    assert again == item and again.content_hash() == item.content_hash()
    assert type(again.values["x"]) is Decimal and type(again.values["flag"]) is bool


def test_value_names_are_identifiers() -> None:
    with pytest.raises(ValidationError):
        OBSERVATIONS[0].model_copy(update={"values": {"Close": Decimal(1)}})


# ======================================================================================
# FeatureRequest
# ======================================================================================


def test_the_request_is_canonical_and_its_hash_stable() -> None:
    forward = _request()
    backward = _request(observations=tuple(reversed(OBSERVATIONS)))
    assert forward == backward and forward.content_hash() == backward.content_hash()
    order = [(item.available_time, item.observation_key) for item in forward.observations]
    assert order == sorted(order)


def test_observations_past_the_cutoff_are_refused() -> None:
    latest = max(item.knowledge_time for item in OBSERVATIONS)
    with pytest.raises(ValidationError, match="knowledge_cutoff"):
        _request(knowledge_cutoff=latest - timedelta(microseconds=1))
    assert _request(knowledge_cutoff=latest).knowledge_cutoff == latest


@pytest.mark.parametrize(
    "times",
    [(), (T0, T0), (T0 + MINUTE, T0)],
    ids=["empty", "duplicate", "descending"],
)
def test_evaluation_times_are_non_empty_and_strictly_ascending(times: tuple[Any, ...]) -> None:
    with pytest.raises(ValidationError):
        _request(evaluation_times=times)


def test_an_ambiguous_observation_order_is_refused() -> None:
    twin = OBSERVATIONS[0].model_copy(update={"values": {"x": Decimal(9)}})
    with pytest.raises(ValidationError, match="重复"):
        _request(observations=(*OBSERVATIONS, twin))


def test_the_feature_must_be_a_feature_ref() -> None:
    with pytest.raises(ValidationError, match="kind=feature"):
        _request(feature=Ref(kind=Kind.STATE, name="latest_x", version="1.0.0"))


def test_visible_at_applies_the_lag_and_keeps_the_latest_revision_per_key() -> None:
    request = _request()
    lag = timedelta(minutes=2)
    # k2's replacement is available at minute 8: visible from minute 10 with a 2-minute lag.
    before = {o.observation_key: o for o in request.visible_at(T0 + 9 * MINUTE, lag)}
    after = {o.observation_key: o for o in request.visible_at(T0 + 10 * MINUTE, lag)}
    assert before["k2"].lineage.canonical_revision_id == "rev-k2"
    assert after["k2"].lineage.canonical_revision_id == "rev-k2-b"
    for item in request.visible_at(T0 + 9 * MINUTE, lag):
        assert item.available_time + lag <= T0 + 9 * MINUTE
    assert request.visible_at(T0, lag) == ()
    with pytest.raises(ValueError, match="available_lag"):
        request.visible_at(T0, -MINUTE)


# ======================================================================================
# FeatureValue / FeatureResult / ProviderDescriptor
# ======================================================================================


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), 0.5])
def test_feature_values_refuse_non_finite_and_float(bad: Any) -> None:
    with pytest.raises(ValidationError):
        FeatureValue(evaluation_time=T0, value=bad, inputs_used=0)


def test_none_is_explicit_and_input_bookkeeping_is_consistent() -> None:
    with pytest.raises(ValidationError):
        FeatureValue(evaluation_time=T0, inputs_used=0)  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="inputs_used"):
        FeatureValue(evaluation_time=T0, value=None, inputs_used=1)
    with pytest.raises(ValidationError, match="inputs_used"):
        FeatureValue(evaluation_time=T0, value=1, inputs_used=0, latest_input_available_time=T0)
    with pytest.raises(ValidationError, match="不得晚于"):
        FeatureValue(
            evaluation_time=T0, value=1, inputs_used=1, latest_input_available_time=T0 + MINUTE
        )
    none = FeatureValue(evaluation_time=T0, value=None, inputs_used=0)
    assert FeatureValue.model_validate_json(none.model_dump_json()) == none


def test_the_result_hash_is_recomputed_and_answers_are_checked() -> None:
    provider = LatestValueProvider((SPEC,))
    request = _request()
    result = provider.compute(request)
    assert result.request_hash == request.content_hash()
    assert result.provider == "latest_value@1.0.0"
    assert result.provider_hash == provider.descriptor.content_hash()
    forged = result.model_dump()
    forged["result_hash"] = content_hash({"forged": 1})
    with pytest.raises(ValidationError, match="result_hash"):
        FeatureResult.model_validate(forged)
    result.check_answers(request, provider.descriptor, SPEC.available_lag)
    with pytest.raises(ValueError, match="request_hash"):
        result.check_answers(
            _request(manifest_content_hash=content_hash(1)), provider.descriptor, SPEC.available_lag
        )
    with pytest.raises(ValueError, match="inputs_used|latest_input_available_time"):
        result.check_answers(
            request, provider.descriptor, SPEC.available_lag + timedelta(minutes=3)
        )
    shorter = _request(evaluation_times=EVALUATION_TIMES[:-1])
    partial = FeatureResult.build(shorter, provider.descriptor, result.values)
    with pytest.raises(ValueError, match="一一对应"):
        partial.check_answers(shorter, provider.descriptor, SPEC.available_lag)


def test_result_values_are_ascending() -> None:
    provider = LatestValueProvider((SPEC,))
    result = provider.compute(_request())
    with pytest.raises(ValidationError, match="升序"):
        FeatureResult.build(_request(), provider.descriptor, tuple(reversed(result.values)))


def test_the_descriptor_declares_determinism_and_feature_keys() -> None:
    fields: dict[str, Any] = {
        "name": "p",
        "version": "1.0.0",
        "deterministic": True,
        "supported_features": {str(SPEC.ref): SPEC.content_hash()},
    }
    descriptor = ProviderDescriptor(**fields)
    assert descriptor.plugin_key == "p@1.0.0"
    assert descriptor.supports(SPEC.ref, SPEC.content_hash())
    assert not descriptor.supports(SPEC.ref, content_hash(0))
    updates: tuple[dict[str, Any], ...] = (
        {"deterministic": False},
        {"supported_features": {}},
        {"supported_features": {"state:x@1.0.0": SPEC.content_hash()}},
        {"version": "1.0"},
    )
    for update in updates:
        with pytest.raises(ValidationError):
            ProviderDescriptor(**{**fields, **update})
    with pytest.raises(ValidationError):
        ProviderDescriptor(**{k: v for k, v in fields.items() if k != "deterministic"})


def test_observation_helpers_build_valid_fixtures() -> None:
    item = observation("kx", 0, available_after=0, x="1", n=1)
    assert item.available_time == item.event_time


@pytest.mark.parametrize(
    "value", [Decimal("5"), 5, True, Decimal("1E+5"), Decimal("-0.000001"), None]
)
def test_feature_value_types_survive_json(value: Any) -> None:
    item = FeatureValue(
        evaluation_time=T0,
        value=value,
        inputs_used=0 if value is None else 1,
        latest_input_available_time=None if value is None else T0,
    )
    again = FeatureValue.model_validate_json(item.model_dump_json())
    assert type(again.value) is type(value) and again.content_hash() == item.content_hash()


# ======================================================================================
# 依赖方向：apps → application → domain ← plugins / infrastructure
# ======================================================================================


def _roots(folder: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in (REPO / folder).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        names |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        out[str(path.relative_to(REPO))] = {name.split(".")[0] for name in names}
    return out


def test_feature_plugins_depend_only_on_the_domain() -> None:
    found = _roots("plugins")
    assert found
    for path, roots in found.items():
        assert not roots & {"infrastructure", "research", "apps", "strategies", "risk", "tests"}, (
            path
        )


def test_the_runner_imports_no_plugin_or_research_code() -> None:
    found = _roots("infrastructure/feature")
    assert found
    for path, roots in found.items():
        assert not roots & {"plugins", "research", "apps"}, path


@pytest.mark.parametrize("text", ["NaN", "nan", "Infinity", "-Infinity", "inf", " sNaN "])
def test_non_finite_numeric_text_is_not_an_observation_value(text: str) -> None:
    """F4-R1 (cursor review 5): NaN / Infinity must not enter as strings either."""
    from core.contracts.feature import _observation_scalar

    class _Info:
        mode = "python"

    with pytest.raises(ValueError, match="非有限"):
        _observation_scalar(text, _Info())  # type: ignore[arg-type]

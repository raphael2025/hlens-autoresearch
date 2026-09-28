from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from core.domain.base import FrozenMapping, Kind, Ref, VersionedSpec
from core.domain.specs import EventSpec, FeatureSpec, StrategySpec
from research.hypotheses.plan_bindings import produce_lowered_output_bindings
from research.hypotheses.typed_plan import PlanLimits, PlanRejected, TypedPlan, parse_plan_json
from research.hypotheses.typed_plan_lowering import OperatorLoweringRefused, lower_typed_plan
from research.hypotheses.typed_plan_resolver import (
    DirectReferenceResolution,
    resolve_direct_references,
)

NOW = datetime(2026, 9, 28, tzinfo=UTC)
SOURCE_A = FeatureSpec(
    name="feature_a",
    version="1.0.0",
    created_at=NOW,
    definition="source a",
    inputs=(Ref(kind=Kind.REPRESENTATION, name="bars", version="1.0.0"),),
    params=FrozenMapping({}),
    available_lag=timedelta(0),
)
SOURCE_B = FeatureSpec(
    name="feature_b",
    version="1.0.0",
    created_at=NOW,
    definition="source b",
    inputs=(Ref(kind=Kind.REPRESENTATION, name="bars", version="1.0.0"),),
    params=FrozenMapping({}),
    available_lag=timedelta(0),
)
#: Two distinct EventSpecs, used only to show that ``temporal`` still refuses (ADR-0082): there is
#: no field on EventSpec that would let a bar-spec-equality check even be attempted.
EVENT_A = EventSpec(
    name="event_a",
    version="1.0.0",
    created_at=NOW,
    trigger="event_a_trigger",
    features=(Ref(kind=Kind.FEATURE, name="ev_feature", version="1.0.0"),),
    observable_lag=timedelta(0),
)
EVENT_B = EventSpec(
    name="event_b",
    version="1.0.0",
    created_at=NOW,
    trigger="event_b_trigger",
    features=(Ref(kind=Kind.FEATURE, name="ev_feature", version="1.0.0"),),
    observable_lag=timedelta(0),
)
#: A minimal StrategySpec, used only to show that ``negation`` (a StrategySpec-output operator)
#: still refuses without partial output.
STRATEGY_A = StrategySpec(
    name="strategy_a",
    version="1.0.0",
    created_at=NOW,
    signals=(Ref(kind=Kind.FEATURE, name="ev_feature", version="1.0.0"),),
)


class _Resolver:
    def __init__(self, specs: tuple[VersionedSpec, ...]) -> None:
        self.specs = {str(spec.ref): spec for spec in specs}

    def resolve(self, ref: Ref) -> VersionedSpec | None:
        return self.specs.get(str(ref))


def _plan(
    operator: str = "interaction",
    *,
    transform: str = "difference",
    window: int = 5,
    include_window: bool = True,
) -> tuple[TypedPlan, DirectReferenceResolution]:
    resolver_specs: tuple[VersionedSpec, ...]
    inputs: list[dict[str, object]]
    parameters: dict[str, object]
    if operator == "interaction":
        inputs = [
            {"ref": str(SOURCE_A.ref), "content_hash": SOURCE_A.content_hash()},
            {"ref": str(SOURCE_B.ref), "content_hash": SOURCE_B.content_hash()},
        ]
        parameters = {}
        resolver_specs = (SOURCE_A, SOURCE_B)
    elif operator == "transformation":
        inputs = [{"ref": str(SOURCE_A.ref), "content_hash": SOURCE_A.content_hash()}]
        parameters = {"transform": transform}
        if include_window:
            parameters["window"] = window
        resolver_specs = (SOURCE_A, SOURCE_B)
    elif operator == "temporal":
        inputs = [
            {"ref": str(EVENT_A.ref), "content_hash": EVENT_A.content_hash()},
            {"ref": str(EVENT_B.ref), "content_hash": EVENT_B.content_hash()},
        ]
        parameters = {"window": 3, "time_unit": "bar"}
        resolver_specs = (EVENT_A, EVENT_B)
    elif operator == "negation":
        inputs = [{"ref": str(STRATEGY_A.ref), "content_hash": STRATEGY_A.content_hash()}]
        parameters = {}
        resolver_specs = (STRATEGY_A,)
    else:
        raise ValueError(f"unsupported test operator {operator!r}")
    payload = {
        "schema_version": "1.1.0",
        "root": "combined",
        "nodes": [
            {
                "id": "combined",
                "operator": operator,
                "inputs": inputs,
                "parameters": parameters,
            }
        ],
    }
    plan = parse_plan_json(
        json.dumps(payload, separators=(",", ":")),
        limits=PlanLimits(
            max_depth=2,
            max_nodes=2,
            max_json_bytes=2048,
            max_parameters_per_node=2,
        ),
    )
    resolution = resolve_direct_references(plan, resolver=_Resolver(resolver_specs))
    return plan, resolution


def test_interaction_lowers_to_deterministic_feature_spec() -> None:
    plan, resolution = _plan()

    first = lower_typed_plan(plan, resolution=resolution, created_at=NOW)
    second = lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    assert tuple(first) == ("combined",)
    output = cast(FeatureSpec, first["combined"])
    assert type(output) is FeatureSpec
    assert output.ref == second["combined"].ref
    assert output.content_hash() == second["combined"].content_hash()
    later = lower_typed_plan(
        plan,
        resolution=resolution,
        created_at=datetime(2026, 9, 29, tzinfo=UTC),
    )["combined"]
    assert output.ref != later.ref
    assert output.definition == "p7.interaction.product@1.0.0"
    assert output.inputs == (SOURCE_A.ref, SOURCE_B.ref)
    assert output.lineage == (SOURCE_A.ref, SOURCE_B.ref)
    assert output.params == FrozenMapping(
        {
            "alignment": "exact_evaluation_time",
            "missing": "propagate_none",
            "numeric_domain": "decimal_or_int_excluding_bool",
            "operator": "product",
            "provider": "p7_interaction_product@1.0.0",
            "semantic_version": "1.0.0",
        }
    )
    assert output.available_lag == timedelta(0)
    assert plan.runnable is False
    bindings = produce_lowered_output_bindings(
        experiment_hash="a" * 64,
        plan=plan,
        specs_by_node=first,
    )
    assert len(bindings) == len(plan.nodes)
    assert bindings[0].node_id == "combined"
    assert bindings[0].spec.content_hash() == output.content_hash()


def test_open_operator_refuses_without_partial_output() -> None:
    # negation's nominal output is StrategySpec; ADR-0082 keeps it OPEN (no accepted lowering).
    plan, resolution = _plan("negation")

    with pytest.raises(OperatorLoweringRefused, match="operator_open") as error:
        lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    assert error.value.operator == "negation"
    assert error.value.node_id == "combined"
    assert plan.runnable is False


def test_lowering_requires_resolution_for_same_plan() -> None:
    plan, resolution = _plan()
    other_plan, _ = _plan("negation")

    with pytest.raises(OperatorLoweringRefused, match="plan_resolution_mismatch"):
        lower_typed_plan(other_plan, resolution=resolution, created_at=NOW)


def test_temporal_still_refuses_operator_open() -> None:
    """ADR-0082: temporal stays OPEN; EventSpec has no bar-spec field to compare (see report)."""
    plan, resolution = _plan("temporal")

    with pytest.raises(OperatorLoweringRefused, match="operator_open") as error:
        lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    assert error.value.operator == "temporal"
    assert error.value.node_id == "combined"
    assert "bar spec" in str(error.value)
    assert plan.runnable is False


@pytest.mark.parametrize(
    ("transform", "definition", "extra_param"),
    [
        (
            "standardize",
            "p7.transformation.standardize@1.0.0",
            ("fit_scope", "rolling_training_window"),
        ),
        ("difference", "p7.transformation.difference@1.0.0", None),
        ("smooth", "p7.transformation.smooth_sma@1.0.0", ("algorithm", "simple_moving_average")),
    ],
)
def test_transformation_accepted_transforms_lower_to_feature_spec(
    transform: str, definition: str, extra_param: tuple[str, str] | None
) -> None:
    plan, resolution = _plan("transformation", transform=transform, window=7)

    output = cast(
        FeatureSpec, lower_typed_plan(plan, resolution=resolution, created_at=NOW)["combined"]
    )

    assert type(output) is FeatureSpec
    assert output.definition == definition
    assert output.inputs == (SOURCE_A.ref,)
    assert output.lineage == (SOURCE_A.ref,)
    assert output.available_lag == timedelta(0)
    assert output.deterministic is True
    assert output.params["operator"] == transform
    assert output.params["window"] == 7
    assert output.params["direction"] == "backward_only"
    assert output.params["missing"] == "propagate_none"
    if extra_param is not None:
        key, value = extra_param
        assert output.params[key] == value
    assert plan.runnable is False


def test_transformation_lowering_is_deterministic_and_created_at_sensitive() -> None:
    plan, resolution = _plan("transformation", transform="standardize", window=10)

    first = lower_typed_plan(plan, resolution=resolution, created_at=NOW)["combined"]
    second = lower_typed_plan(plan, resolution=resolution, created_at=NOW)["combined"]
    later = lower_typed_plan(
        plan, resolution=resolution, created_at=datetime(2026, 9, 29, tzinfo=UTC)
    )["combined"]

    assert first.ref == second.ref
    assert first.content_hash() == second.content_hash()
    assert first.ref != later.ref


def test_transformation_rank_and_quantile_remain_operator_open() -> None:
    for transform in ("rank", "quantile"):
        plan, resolution = _plan("transformation", transform=transform, window=4)

        with pytest.raises(OperatorLoweringRefused, match="operator_open") as error:
            lower_typed_plan(plan, resolution=resolution, created_at=NOW)

        assert error.value.operator == "transformation"
        assert plan.runnable is False


def test_transformation_window_must_be_positive() -> None:
    for bad_window in (0, -1):
        with pytest.raises(PlanRejected):
            _plan("transformation", transform="standardize", window=bad_window)


def test_transformation_standardize_requires_bound_window() -> None:
    """No 'window' at all == no training-window binding for standardize; parse-time reject."""
    with pytest.raises(PlanRejected):
        _plan("transformation", transform="standardize", include_window=False)


def test_transformation_output_binds_via_adr_0078() -> None:
    plan, resolution = _plan("transformation", transform="difference", window=3)
    outputs = lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    bindings = produce_lowered_output_bindings(
        experiment_hash="b" * 64,
        plan=plan,
        specs_by_node=outputs,
    )

    assert len(bindings) == len(plan.nodes) == 1
    assert bindings[0].node_id == "combined"
    assert bindings[0].spec.content_hash() == outputs["combined"].content_hash()

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from core.domain.base import FrozenMapping, Kind, Ref
from core.domain.specs import FeatureSpec
from research.hypotheses.plan_bindings import produce_lowered_output_bindings
from research.hypotheses.typed_plan import PlanLimits, parse_plan_json
from research.hypotheses.typed_plan_lowering import OperatorLoweringRefused, lower_typed_plan
from research.hypotheses.typed_plan_resolver import resolve_direct_references

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


class _Resolver:
    def __init__(self, specs: tuple[FeatureSpec, ...]) -> None:
        self.specs = {str(spec.ref): spec for spec in specs}

    def resolve(self, ref: Ref) -> FeatureSpec | None:
        return self.specs.get(str(ref))


def _plan(operator: str = "interaction"):
    if operator == "interaction":
        inputs = [
            {"ref": str(SOURCE_A.ref), "content_hash": SOURCE_A.content_hash()},
            {"ref": str(SOURCE_B.ref), "content_hash": SOURCE_B.content_hash()},
        ]
        parameters = {}
    else:
        inputs = [{"ref": str(SOURCE_A.ref), "content_hash": SOURCE_A.content_hash()}]
        parameters = {"transform": "difference"}
    payload = {
        "schema_version": "1.0.0",
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
    resolution = resolve_direct_references(plan, resolver=_Resolver((SOURCE_A, SOURCE_B)))
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
    plan, resolution = _plan("transformation")

    with pytest.raises(OperatorLoweringRefused, match="operator_open") as error:
        lower_typed_plan(plan, resolution=resolution, created_at=NOW)

    assert error.value.operator == "transformation"
    assert error.value.node_id == "combined"
    assert plan.runnable is False


def test_lowering_requires_resolution_for_same_plan() -> None:
    plan, resolution = _plan()
    other_plan, _ = _plan("transformation")

    with pytest.raises(OperatorLoweringRefused, match="plan_resolution_mismatch"):
        lower_typed_plan(other_plan, resolution=resolution, created_at=NOW)

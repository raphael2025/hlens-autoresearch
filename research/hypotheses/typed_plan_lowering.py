"""Pure lowering of the currently specified P7 operator subset (ADR-0082).

The emitted values are ordinary versioned specs for ADR-0078 completeness checks. This module
does not register a Provider, compile a runnable plan, write admission evidence, or execute code.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Final

from core.domain.base import FrozenMapping, Kind, VersionedSpec, content_hash
from core.domain.specs import FeatureSpec
from research.hypotheses.typed_plan import (
    NodeInput,
    PlanOperator,
    PlanRefused,
    SpecInput,
    TypedPlan,
    parse_plan_json,
)
from research.hypotheses.typed_plan_resolver import DirectReferenceResolution, ResolvedSpecInput

__all__ = ["OperatorLoweringRefused", "lower_typed_plan"]

_SEMANTIC_VERSION: Final = "1.0.0"
_PRODUCT_DEFINITION: Final = "p7.interaction.product@1.0.0"


class OperatorLoweringRefused(PlanRefused):
    """A node has no accepted lowering or its required evidence is incomplete."""

    def __init__(self, code: str, node_id: str, operator: str, detail: str) -> None:
        self.code = code
        self.node_id = node_id
        self.operator = operator
        super().__init__(f"{code}: node {node_id!r} ({operator}): {detail}")


def _resolved_inputs(
    plan: TypedPlan, resolution: DirectReferenceResolution
) -> dict[tuple[str, int], VersionedSpec]:
    if type(resolution) is not DirectReferenceResolution:
        raise TypeError("resolution must be an exact DirectReferenceResolution")
    if resolution.plan_hash != plan.content_hash():
        raise OperatorLoweringRefused(
            "plan_resolution_mismatch", plan.root, "plan", "resolution belongs to another plan"
        )
    resolved: dict[tuple[str, int], VersionedSpec] = {}
    for occurrence in resolution.inputs:
        if type(occurrence) is not ResolvedSpecInput:
            raise OperatorLoweringRefused(
                "invalid_resolution",
                plan.root,
                "plan",
                "resolution contains an unknown input record",
            )
        if (
            not isinstance(occurrence.node_id, str)
            or type(occurrence.input_index) is not int
            or occurrence.input_index < 0
            or not isinstance(occurrence.spec, VersionedSpec)
        ):
            raise OperatorLoweringRefused(
                "invalid_resolution", plan.root, "plan", "resolution input record is malformed"
            )
        key = (occurrence.node_id, occurrence.input_index)
        if key in resolved:
            raise OperatorLoweringRefused(
                "duplicate_resolution", occurrence.node_id, "plan", "input resolved twice"
            )
        resolved[key] = occurrence.spec

    expected = {
        (node.node_id, index): item
        for node in plan.nodes
        for index, item in enumerate(node.inputs)
        if isinstance(item, SpecInput)
    }
    if set(resolved) != set(expected):
        raise OperatorLoweringRefused(
            "incomplete_resolution", plan.root, "plan", "direct input resolution is incomplete"
        )
    for key, spec_input in expected.items():
        spec = resolved[key]
        if spec.ref != spec_input.ref or spec.content_hash() != spec_input.content_hash:
            raise OperatorLoweringRefused(
                "resolution_binding_mismatch",
                key[0],
                "plan",
                f"resolved input {key[1]} does not match its ref and content hash",
            )
    return resolved


def lower_typed_plan(
    plan: TypedPlan,
    *,
    resolution: DirectReferenceResolution,
    created_at: datetime,
) -> dict[str, VersionedSpec]:
    """Lower every node to one core spec, or refuse the whole plan.

    ``created_at`` is mandatory because the core spec envelope includes it in content identity;
    no wall clock value is introduced implicitly. Currently only ``interaction`` has an accepted
    output declaration. Inputs from prior nodes are resolved from the already lowered node map.
    """
    if type(plan) is not TypedPlan:
        raise TypeError("plan must be an exact TypedPlan")
    try:
        canonical = parse_plan_json(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "root": plan.root,
                    "nodes": [
                        {
                            "id": node.node_id,
                            "operator": node.operator.value,
                            "inputs": [item.payload() for item in node.inputs],
                            "parameters": dict(node.parameters),
                        }
                        for node in plan.nodes
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            limits=plan.limits,
        )
    except Exception as exc:
        raise OperatorLoweringRefused(
            "invalid_plan",
            plan.root,
            "plan",
            f"canonical plan validation failed ({type(exc).__name__})",
        ) from exc
    if canonical != plan:
        raise OperatorLoweringRefused(
            "noncanonical_plan", plan.root, "plan", "plan did not round-trip through parser"
        )
    if not isinstance(created_at, datetime) or created_at.utcoffset() is None:
        raise ValueError("created_at must be an explicitly supplied timezone-aware datetime")
    direct = _resolved_inputs(plan, resolution)
    specs: dict[str, VersionedSpec] = {}

    for node in plan.nodes:
        if node.operator is not PlanOperator.INTERACTION:
            raise OperatorLoweringRefused(
                "operator_open",
                node.node_id,
                node.operator.value,
                "ADR-0082 has no accepted business semantics and Provider lowering for this operator",
            )

        sources: list[FeatureSpec] = []
        input_hashes: list[str] = []
        input_refs = []
        for index, item in enumerate(node.inputs):
            if isinstance(item, SpecInput):
                source = direct[(node.node_id, index)]
            elif isinstance(item, NodeInput):
                source = specs.get(item.node_id)
                if source is None:
                    raise OperatorLoweringRefused(
                        "unresolved_node_input",
                        node.node_id,
                        node.operator.value,
                        f"prior node {item.node_id!r} has no lowered spec",
                    )
            else:  # Defensive against forged objects outside the strict parser.
                raise OperatorLoweringRefused(
                    "unknown_input", node.node_id, node.operator.value, "input is not a plan input"
                )
            if type(source) is not FeatureSpec:
                raise OperatorLoweringRefused(
                    "wrong_input_spec",
                    node.node_id,
                    node.operator.value,
                    f"input {index} must resolve to exactly FeatureSpec",
                )
            sources.append(source)
            input_hashes.append(source.content_hash())
            input_refs.append(source.ref)

        # The product's generated name is derived only from the plan node and exact input
        # identities. The user-supplied creation timestamp remains an explicit spec field.
        identity = content_hash(
            {
                "operator": _PRODUCT_DEFINITION,
                "plan_node": node.payload(),
                "input_refs": [str(spec.ref) for spec in sources],
                "input_hashes": input_hashes,
                "created_at": created_at.astimezone(UTC).isoformat(),
            }
        )
        output = FeatureSpec(
            name=f"p7_interaction_{identity}",
            version=_SEMANTIC_VERSION,
            created_at=created_at,
            definition=_PRODUCT_DEFINITION,
            inputs=tuple(input_refs),
            params=FrozenMapping(
                {
                    "alignment": "exact_evaluation_time",
                    "missing": "propagate_none",
                    "numeric_domain": "decimal_or_int_excluding_bool",
                    "operator": "product",
                    "provider": "p7_interaction_product@1.0.0",
                    "semantic_version": _SEMANTIC_VERSION,
                }
            ),
            available_lag=timedelta(0),
            deterministic=True,
            lineage=tuple(input_refs),
        )
        if output.kind is not Kind.FEATURE:  # pragma: no cover - core model invariant
            raise OperatorLoweringRefused(
                "wrong_output_spec", node.node_id, node.operator.value, "expected FeatureSpec"
            )
        specs[node.node_id] = output

    return specs

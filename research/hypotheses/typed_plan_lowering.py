"""Pure lowering of the currently specified P7 operator subset (ADR-0082).

The emitted values are ordinary versioned specs for ADR-0078 completeness checks. This module
does not register a Provider, compile a runnable plan, write admission evidence, or execute code.

Accepted operators (ADR-0082 §2): ``interaction`` (FeatureSpec, product) and ``transformation``
for exactly ``standardize`` / ``difference`` / ``smooth`` (FeatureSpec, explicit backward-looking
window). Every other operator and every other ``transform`` name (``rank``, ``quantile``) still
refuses the whole plan with ``operator_open``; ``temporal`` in particular remains OPEN because
``EventSpec`` has no field that lets pure, registry-free lowering compare the two inputs' declared
bar spec (see the module-level note below and ADR-0082 §2).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final, NoReturn

from core.domain.base import FrozenMapping, Kind, VersionedSpec, content_hash
from core.domain.specs import FeatureSpec
from research.hypotheses.typed_plan import (
    PLAN_FORMAT_VERSION,
    NodeInput,
    PlanNode,
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

#: ADR-0082 (transformation acceptance): only these three ``transform`` names have accepted
#: business semantics. ``rank`` and ``quantile`` are cross-sectional and stay ``operator_open``.
_ACCEPTED_TRANSFORMS: Final = frozenset({"standardize", "difference", "smooth"})
_TRANSFORMATION_DEFINITIONS: Final[dict[str, str]] = {
    "standardize": "p7.transformation.standardize@1.0.0",
    "difference": "p7.transformation.difference@1.0.0",
    # The algorithm is explicit in the definition identity itself (ADR-0082): simple moving
    # average, not any other smoothing family.
    "smooth": "p7.transformation.smooth_sma@1.0.0",
}


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


def _operator_open(node: PlanNode, detail: str) -> NoReturn:
    raise OperatorLoweringRefused("operator_open", node.node_id, node.operator.value, detail)


_TEMPORAL_OPEN_DETAIL: Final = (
    "ADR-0082 keeps temporal OPEN: EventSpec (core.domain.specs) has no field that declares a "
    "bar spec, so pure, registry-free lowering cannot compare the two resolved EventSpec inputs' "
    "bar cadence without either a new EventSpec contract field or a transitive Registry lookup "
    "that ADR-0082 §1.1 forbids during lowering; ADR-0061's microsecond seq window is explicitly "
    "not reused as a silent substitute"
)
_DEFAULT_OPEN_DETAIL: Final = (
    "ADR-0082 has no accepted business semantics and Provider lowering for this operator"
)


def _resolve_inputs(
    node: PlanNode,
    direct: dict[tuple[str, int], VersionedSpec],
    specs: dict[str, VersionedSpec],
    *,
    expected_type: type[VersionedSpec],
) -> list[VersionedSpec]:
    """Resolve every input of ``node`` in AST order, requiring exactly ``expected_type``."""
    resolved: list[VersionedSpec] = []
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
        if type(source) is not expected_type:
            raise OperatorLoweringRefused(
                "wrong_input_spec",
                node.node_id,
                node.operator.value,
                f"input {index} must resolve to exactly {expected_type.__name__}",
            )
        resolved.append(source)
    return resolved


def _spec_identity(
    definition: str, node: PlanNode, sources: Sequence[VersionedSpec], created_at: datetime
) -> str:
    """Deterministic content identity: plan node payload + exact resolved input identities."""
    return content_hash(
        {
            "operator": definition,
            "plan_node": node.payload(),
            "input_refs": [str(spec.ref) for spec in sources],
            "input_hashes": [spec.content_hash() for spec in sources],
            "created_at": created_at.astimezone(UTC).isoformat(),
        }
    )


def _lower_interaction(
    node: PlanNode,
    direct: dict[tuple[str, int], VersionedSpec],
    specs: dict[str, VersionedSpec],
    created_at: datetime,
) -> FeatureSpec:
    sources = _resolve_inputs(node, direct, specs, expected_type=FeatureSpec)
    input_refs = [spec.ref for spec in sources]
    identity = _spec_identity(_PRODUCT_DEFINITION, node, sources, created_at)
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
    return output


def _lower_transformation(
    node: PlanNode,
    direct: dict[tuple[str, int], VersionedSpec],
    specs: dict[str, VersionedSpec],
    created_at: datetime,
) -> FeatureSpec:
    transform = node.parameters["transform"]
    if not isinstance(transform, str) or transform not in _ACCEPTED_TRANSFORMS:
        _operator_open(
            node,
            f"transform {transform!r} remains OPEN (ADR-0082): only standardize / difference / "
            "smooth have accepted time-series lowering; rank / quantile are cross-sectional",
        )
    window = node.parameters["window"]
    if type(window) is not int or window < 1:  # Defensive: typed_plan.py already enforces this.
        raise OperatorLoweringRefused(
            "invalid_window", node.node_id, node.operator.value, "window must be a positive integer"
        )

    sources = _resolve_inputs(node, direct, specs, expected_type=FeatureSpec)
    (source,) = sources
    definition = _TRANSFORMATION_DEFINITIONS[transform]
    identity = _spec_identity(definition, node, sources, created_at)

    params: dict[str, str | int | float | bool] = {
        "operator": transform,
        "provider": f"p7_transformation_{transform}@1.0.0",
        "semantic_version": _SEMANTIC_VERSION,
        "window": window,
        # Only look backward from the evaluation time; never at future bars.
        "direction": "backward_only",
        # A missing input value at any bar in the window propagates as a missing output; nothing
        # is filled, interpolated or zero-substituted.
        "missing": "propagate_none",
    }
    if transform == "standardize":
        # Constitution C-L3: fit parameters (mean / std) may only use training-window data. A
        # strictly rolling, backward-only computation confined to `window` trailing bars is, by
        # construction, always scoped to a bound window and never sees data outside it — the same
        # explicit `window` binding therefore *is* the required training-window binding. There is
        # no separate, unbound "global" fit mode this lowering could silently fall back to.
        params["fit_scope"] = "rolling_training_window"
    elif transform == "smooth":
        # The algorithm is explicit (ADR-0082): simple moving average, nothing else.
        params["algorithm"] = "simple_moving_average"

    output = FeatureSpec(
        name=f"p7_transformation_{identity}",
        version=_SEMANTIC_VERSION,
        created_at=created_at,
        definition=definition,
        inputs=(source.ref,),
        params=FrozenMapping(params),
        available_lag=timedelta(0),
        deterministic=True,
        lineage=(source.ref,),
    )
    if output.kind is not Kind.FEATURE:  # pragma: no cover - core model invariant
        raise OperatorLoweringRefused(
            "wrong_output_spec", node.node_id, node.operator.value, "expected FeatureSpec"
        )
    return output


def lower_typed_plan(
    plan: TypedPlan,
    *,
    resolution: DirectReferenceResolution,
    created_at: datetime,
) -> dict[str, VersionedSpec]:
    """Lower every node to one core spec, or refuse the whole plan.

    ``created_at`` is mandatory because the core spec envelope includes it in content identity;
    no wall clock value is introduced implicitly. Only ``interaction`` and ``transformation``
    (restricted to ``standardize`` / ``difference`` / ``smooth``) have an accepted output
    declaration (ADR-0082). Inputs from prior nodes are resolved from the already lowered node map.
    """
    if type(plan) is not TypedPlan:
        raise TypeError("plan must be an exact TypedPlan")
    try:
        canonical = parse_plan_json(
            json.dumps(
                {
                    "schema_version": PLAN_FORMAT_VERSION,
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
        if node.operator is PlanOperator.INTERACTION:
            specs[node.node_id] = _lower_interaction(node, direct, specs, created_at)
        elif node.operator is PlanOperator.TRANSFORMATION:
            specs[node.node_id] = _lower_transformation(node, direct, specs, created_at)
        elif node.operator is PlanOperator.TEMPORAL:
            _operator_open(node, _TEMPORAL_OPEN_DETAIL)
        else:
            _operator_open(node, _DEFAULT_OPEN_DETAIL)

    return specs

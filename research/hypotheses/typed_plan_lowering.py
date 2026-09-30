"""Pure lowering of the specified P7 operator set (ADR-0082, contract 2.4.0 / ADR-0088).

The emitted values are ordinary versioned specs for ADR-0078 completeness checks. This module
does not register a Provider, compile a runnable plan, write admission evidence, or execute code.

Accepted operators:

* ``interaction`` (ADR-0082 §2): FeatureSpec, exact point-in-time product.
* ``transformation`` for ``standardize`` / ``difference`` / ``smooth`` (ADR-0082 §4) and, in a
  plan of format "1.2.0", time-series ``rank`` / ``quantile`` (ADR-0099): FeatureSpec, explicit
  backward-looking window. In a "1.1.0" plan ``rank`` / ``quantile`` stay ``operator_open``.
* ``transformation`` for cross-sectional ``rank_cs`` / ``quantile_cs`` in a plan of format "1.3.0"
  (ADR-0100 §2): FeatureSpec over the members of a pinned universe snapshot at the same bar
  ``interval_end``. The node's ``universe`` / ``universe_hash`` must bind a caller-supplied
  ``ResearchDatasetManifest`` exactly (content hash and dataset identity); the manifest's
  ``DatasetRef`` becomes a direct input of the emitted FeatureSpec.
* ``temporal`` (ADR-0088 decision 1, pending-decisions §3 option A): EventSpec. Both input
  EventSpecs must declare the same non-empty ``bar_spec``; the window counts bars of that spec,
  left-open / right-closed (the second event falls 1..N bars after the first); the result carries
  the same ``bar_spec`` and becomes visible when the second event becomes visible.
* ``conditioning`` / ``ensemble`` / ``negation`` (ADR-0088 decision 2): StrategySpec whose
  ``composition`` is ``ConditionedStrategy`` / ``EnsembleStrategy`` / ``NegatedStrategy``.

Every other operator shape refuses the whole plan; no partial node map is ever returned.
``TypedPlan.runnable`` stays ``False``. Execution Providers for every emitted definition exist
since ADR-0100 item 1 (``plugins.features.p7_operators``, ``plugins.events.p7_temporal``,
``research.strategies.p7_compositions``); only ``typed_plan_compiler`` (default OFF) binds them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Final, NoReturn, cast

from core.contracts.universe import ResearchDatasetManifest
from core.domain.base import (
    FrozenMapping,
    Kind,
    Ref,
    RefTargetIdentity,
    VersionedSpec,
    canonical_json,
    content_hash,
)
from core.domain.specs import (
    ConditionedStrategy,
    EnsembleStrategy,
    EventSpec,
    FeatureSpec,
    NegatedStrategy,
    StateSpec,
    StrategySpec,
    Zone,
)
from research.hypotheses.typed_plan import (
    CROSS_SECTIONAL_TRANSFORMS,
    NodeInput,
    PlanNode,
    PlanOperator,
    PlanRefused,
    SpecInput,
    TypedPlan,
    parse_plan_json,
    parse_universe_reference,
)
from research.hypotheses.typed_plan_resolver import DirectReferenceResolution, ResolvedSpecInput

__all__ = ["OperatorLoweringRefused", "lower_typed_plan"]

_SEMANTIC_VERSION: Final = "1.0.0"
_PRODUCT_DEFINITION: Final = "p7.interaction.product@1.0.0"

#: ADR-0082 (transformation acceptance): these three ``transform`` names have accepted
#: time-series semantics in every supported plan format version.
_ACCEPTED_TRANSFORMS: Final = frozenset({"standardize", "difference", "smooth"})
#: ADR-0099: time-series ``rank`` / ``quantile`` are accepted only in plans of format "1.2.0".
#: A "1.1.0" plan's ``rank`` / ``quantile`` node keeps its original meaning (``operator_open``);
#: an old plan's meaning is never changed retroactively (ADR-0099 decision 4).
#: ADR-0100 §2: cross-sectional ``rank_cs`` / ``quantile_cs`` are accepted only in format "1.3.0".
_ACCEPTED_TRANSFORMS_BY_PLAN_FORMAT: Final[dict[str, frozenset[str]]] = {
    "1.1.0": _ACCEPTED_TRANSFORMS,
    "1.2.0": _ACCEPTED_TRANSFORMS | frozenset({"rank", "quantile"}),
    "1.3.0": _ACCEPTED_TRANSFORMS | frozenset({"rank", "quantile"}) | CROSS_SECTIONAL_TRANSFORMS,
}
_TRANSFORMATION_DEFINITIONS: Final[dict[str, str]] = {
    "standardize": "p7.transformation.standardize@1.0.0",
    "difference": "p7.transformation.difference@1.0.0",
    # The algorithm is explicit in the definition identity itself (ADR-0082): simple moving
    # average, not any other smoothing family.
    "smooth": "p7.transformation.smooth_sma@1.0.0",
    # ADR-0099: the ``_ts`` suffix makes the time-series (not cross-sectional) population explicit
    # in the definition identity itself.
    "rank": "p7.transformation.rank_ts@1.0.0",
    "quantile": "p7.transformation.quantile_ts@1.0.0",
    # ADR-0100 §2: the ``_cs`` suffix makes the cross-sectional population explicit.
    "rank_cs": "p7.transformation.rank_cs@1.0.0",
    "quantile_cs": "p7.transformation.quantile_cs@1.0.0",
}
#: ADR-0100 §2: a cross-sectional rank needs at least two valid members (it divides by ``n - 1``).
_MIN_CROSS_SECTION: Final = 2
#: Decimal places of the emitted ``rank_cs`` value (a representation choice declared in the spec
#: and hash-bound, matching the 18-place outputs of the existing bar FeatureProviders).
_RANK_CS_DECIMAL_PLACES: Final = 18
#: ADR-0099: the percentile rank divides by ``window - 1``; quantile is computed from that rank.
_RANK_BASED_TRANSFORMS: Final = frozenset({"rank", "quantile"})
_MIN_RANK_WINDOW: Final = 2
_MIN_BUCKETS: Final = 2

#: Operator semantic keys of the ADR-0088 lowerings (identity inputs, never Provider code).
_TEMPORAL_DEFINITION: Final = "p7.temporal.sequence_within_bars@1.0.0"
_CONDITIONING_DEFINITION: Final = "p7.conditioning.state_gate@1.0.0"
_ENSEMBLE_DEFINITION: Final = "p7.ensemble.equal_weight_mean@1.0.0"
_NEGATION_DEFINITION: Final = "p7.negation.target_position@1.0.0"

#: The only accepted ``temporal`` time unit: the window counts bars of the shared ``bar_spec``
#: (pending-decisions §3 option A). Any other unit has no accepted semantics and stays OPEN;
#: ADR-0061's microsecond seq window is not reused as a substitute.
_TEMPORAL_TIME_UNIT: Final = "bar"


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


def _refuse(node: PlanNode, code: str, detail: str) -> NoReturn:
    raise OperatorLoweringRefused(code, node.node_id, node.operator.value, detail)


def _resolve_inputs(
    node: PlanNode,
    direct: dict[tuple[str, int], VersionedSpec],
    specs: dict[str, VersionedSpec],
    *,
    expected_types: Sequence[type[VersionedSpec]] | type[VersionedSpec],
) -> list[VersionedSpec]:
    """Resolve every input of ``node`` in AST order, requiring the exact class per position.

    ``expected_types`` is either one class for every input, or one class per input position.
    """
    if isinstance(expected_types, type):
        per_position: Sequence[type[VersionedSpec]] = (expected_types,) * len(node.inputs)
    else:
        per_position = expected_types
    if len(per_position) != len(node.inputs):  # Defensive: typed_plan.py enforces arity.
        _refuse(node, "wrong_input_arity", "input count does not match the operator signature")
    resolved: list[VersionedSpec] = []
    for index, (item, expected_type) in enumerate(zip(node.inputs, per_position, strict=True)):
        source: VersionedSpec | None
        if isinstance(item, SpecInput):
            source = direct[(node.node_id, index)]
        elif isinstance(item, NodeInput):
            source = specs.get(item.node_id)
            if source is None:
                _refuse(
                    node,
                    "unresolved_node_input",
                    f"prior node {item.node_id!r} has no lowered spec",
                )
        else:  # Defensive against forged objects outside the strict parser.
            _refuse(node, "unknown_input", "input is not a plan input")
        if type(source) is not expected_type:
            _refuse(
                node,
                "wrong_input_spec",
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


def _build_output[SpecT: VersionedSpec](node: PlanNode, factory: Callable[[], SpecT]) -> SpecT:
    """Construct one output spec; a core-contract rejection refuses the whole plan."""
    try:
        return factory()
    except (TypeError, ValueError) as exc:  # pydantic.ValidationError is a ValueError.
        raise OperatorLoweringRefused(
            "invalid_output_spec",
            node.node_id,
            node.operator.value,
            f"output spec failed core contract validation ({type(exc).__name__})",
        ) from exc


def _union_refs(groups: Iterable[Iterable[Ref]]) -> tuple[Ref, ...]:
    """Concatenate refs in order, keeping the first ref of every target identity."""
    seen: set[RefTargetIdentity] = set()
    result: list[Ref] = []
    for group in groups:
        for ref in group:
            identity = ref.target_identity()
            if identity not in seen:
                seen.add(identity)
                result.append(ref)
    return tuple(result)


def _lower_interaction(
    node: PlanNode,
    direct: dict[tuple[str, int], VersionedSpec],
    specs: dict[str, VersionedSpec],
    created_at: datetime,
) -> FeatureSpec:
    sources = _resolve_inputs(node, direct, specs, expected_types=FeatureSpec)
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
    *,
    plan_format: str,
    universes: Mapping[str, ResearchDatasetManifest],
) -> FeatureSpec:
    transform = node.parameters["transform"]
    accepted = _ACCEPTED_TRANSFORMS_BY_PLAN_FORMAT.get(plan_format, frozenset())
    if not isinstance(transform, str) or transform not in accepted:
        _operator_open(
            node,
            f"transform {transform!r} has no accepted lowering in plan format {plan_format!r}: "
            "standardize / difference / smooth are accepted (ADR-0082); time-series rank / "
            "quantile from plan format 1.2.0 (ADR-0099); cross-sectional rank_cs / quantile_cs "
            "only in plan format 1.3.0 (ADR-0100 §2)",
        )
    if transform in CROSS_SECTIONAL_TRANSFORMS:
        return _lower_cross_sectional(
            node, direct, specs, created_at, transform=transform, universes=universes
        )
    window = node.parameters.get("window")
    if type(window) is not int or window < 1:  # Defensive: typed_plan.py already enforces this.
        raise OperatorLoweringRefused(
            "invalid_window", node.node_id, node.operator.value, "window must be a positive integer"
        )
    if transform in _RANK_BASED_TRANSFORMS and window < _MIN_RANK_WINDOW:
        # Defensive: typed_plan.py enforces this for format 1.2.0. The percentile rank divides by
        # `window - 1` (ADR-0099 decision 2), so a one-bar window has no defined rank.
        _refuse(node, "invalid_window", f"{transform} requires window >= {_MIN_RANK_WINDOW}")
    buckets = node.parameters.get("buckets")
    if transform == "quantile":
        if type(buckets) is not int or buckets < _MIN_BUCKETS:  # Defensive: parser enforces.
            _refuse(
                node, "invalid_buckets", f"quantile requires integer buckets >= {_MIN_BUCKETS}"
            )
    elif buckets is not None:  # Defensive: parser rejects buckets on every other transform.
        _refuse(node, "invalid_buckets", "buckets is only admitted for transform 'quantile'")

    sources = _resolve_inputs(node, direct, specs, expected_types=FeatureSpec)
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
    elif transform == "rank":
        # ADR-0099 decision 2: percentile rank of the current bar among the trailing `window`
        # bars including it, (count_less + 0.5 * (count_equal - 1)) / (window - 1), in [0, 1].
        params["ties"] = "average"
        params["scale"] = "unit_interval"
    elif transform == "quantile":
        # ADR-0099 decision 3: floor(rank * buckets) clipped to [0, buckets - 1], with `rank` as
        # in decision 2. `buckets` is explicit; no default bucket count is ever assumed.
        params["buckets"] = cast(int, buckets)
        params["ties"] = "average"

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


def _lower_cross_sectional(
    node: PlanNode,
    direct: dict[tuple[str, int], VersionedSpec],
    specs: dict[str, VersionedSpec],
    created_at: datetime,
    *,
    transform: str,
    universes: Mapping[str, ResearchDatasetManifest],
) -> FeatureSpec:
    """Cross-sectional ``rank_cs`` / ``quantile_cs`` over a pinned universe (ADR-0100 §2).

    Population: every member of the pinned universe snapshot at the same bar time, aligned by bar
    ``interval_end``; members whose source value is missing are excluded; with ``n`` valid members
    the rank is ``(count_less + 0.5 * (count_equal - 1)) / (n - 1)`` (ties: average rank), and
    ``n < 2`` yields a missing value. ``quantile_cs`` is ``min(floor(rank * buckets),
    buckets - 1)``. Only ``available_time <= t`` data enter the value at ``t``.
    """
    parameters = node.parameters
    try:
        reference = parse_universe_reference(
            parameters.get("universe"), parameters.get("universe_hash")
        )
    except ValueError as exc:  # Defensive: typed_plan.py enforces the same syntax.
        _refuse(node, "invalid_universe", str(exc))
    if "window" in parameters:  # Defensive: the parser rejects a window on these transforms.
        _refuse(node, "invalid_window", f"{transform} does not take a trailing window")
    buckets = parameters.get("buckets")
    if transform == "quantile_cs":
        if type(buckets) is not int or buckets < _MIN_BUCKETS:  # Defensive: parser enforces.
            _refuse(
                node, "invalid_buckets", f"quantile_cs requires integer buckets >= {_MIN_BUCKETS}"
            )
    elif buckets is not None:  # Defensive: parser rejects buckets on rank_cs.
        _refuse(node, "invalid_buckets", "buckets is only admitted for transform 'quantile_cs'")

    manifest = universes.get(reference.manifest_hash)
    if manifest is None:
        _refuse(
            node,
            "unresolved_universe",
            "no caller-supplied ResearchDatasetManifest has content hash "
            f"{reference.manifest_hash}",
        )
    dataset = manifest.dataset
    if (dataset.zone, dataset.table, dataset.snapshot_id) != (
        Zone.RESEARCH_DATASET,
        reference.table,
        reference.snapshot_id,
    ):
        _refuse(
            node,
            "universe_binding_mismatch",
            f"manifest {reference.manifest_hash} describes {dataset.zone.value}:{dataset.table}@"
            f"{dataset.snapshot_id}, not {reference.text()}",
        )

    sources = _resolve_inputs(node, direct, specs, expected_types=FeatureSpec)
    (source,) = sources
    definition = _TRANSFORMATION_DEFINITIONS[transform]
    # The node payload (hence the identity) already carries `universe` and `universe_hash`.
    identity = _spec_identity(definition, node, sources, created_at)
    universe_spec = manifest.universe_spec
    params: dict[str, str | int | float | bool] = {
        "operator": transform,
        "provider": f"p7_transformation_{transform}@1.0.0",
        "semantic_version": _SEMANTIC_VERSION,
        # Population: all members of the pinned universe snapshot effective at the bar time.
        "population": "universe_snapshot_members_at_bar",
        "universe": reference.text(),
        "universe_manifest_hash": reference.manifest_hash,
        "universe_spec": f"{universe_spec.name}@{universe_spec.version}",
        "universe_spec_hash": universe_spec.spec_hash,
        # One cross-section per bar, keyed by the bar's `interval_end` (no cross-bar mixing).
        "alignment": "bar_interval_end",
        # A member without a visible value at that bar is excluded from the population; nothing
        # is filled, interpolated or carried forward.
        "missing": "exclude_from_population",
        # Fewer valid members than this yields a missing value for every member of that bar.
        "min_population": _MIN_CROSS_SECTION,
        "ties": "average",
    }
    if transform == "rank_cs":
        params["scale"] = "unit_interval"
        params["output_decimal_places"] = _RANK_CS_DECIMAL_PLACES
        params["rounding"] = "half_even"
    else:
        params["buckets"] = cast(int, buckets)

    return _build_output(
        node,
        lambda: FeatureSpec(
            name=f"p7_transformation_{identity}",
            version=_SEMANTIC_VERSION,
            created_at=created_at,
            definition=definition,
            # The pinned universe snapshot is a direct input: membership decides the population.
            inputs=(source.ref, dataset),
            params=FrozenMapping(params),
            available_lag=timedelta(0),
            deterministic=True,
            lineage=(source.ref,),
        ),
    )


def _universe_map(
    universes: Iterable[ResearchDatasetManifest],
) -> dict[str, ResearchDatasetManifest]:
    """Caller-supplied pinned universe manifests keyed by their recomputed content hash."""
    if not isinstance(universes, Iterable):
        raise TypeError("universes must be an iterable of ResearchDatasetManifest")
    result: dict[str, ResearchDatasetManifest] = {}
    for manifest in universes:
        if type(manifest) is not ResearchDatasetManifest:
            raise TypeError("universes must contain exact ResearchDatasetManifest instances")
        result[manifest.content_hash()] = manifest
    return result


def _lower_temporal(
    node: PlanNode,
    direct: dict[tuple[str, int], VersionedSpec],
    specs: dict[str, VersionedSpec],
    created_at: datetime,
) -> EventSpec:
    """``second`` occurs 1..``window`` bars after ``first`` (left-open, right-closed).

    Both EventSpecs must declare the same non-empty ``bar_spec`` (ADR-0088 decision 1). The output
    event time is the second event's time and it is visible when the second event is visible.
    """
    time_unit = node.parameters["time_unit"]
    if time_unit != _TEMPORAL_TIME_UNIT:
        _operator_open(
            node,
            f"time_unit {time_unit!r} has no accepted temporal semantics: the window counts bars "
            "of the inputs' shared bar_spec, so time_unit must be exactly "
            f"{_TEMPORAL_TIME_UNIT!r}; ADR-0061's microsecond window is not a substitute",
        )
    window = node.parameters["window"]
    if type(window) is not int or window < 1:  # Defensive: typed_plan.py already enforces this.
        _refuse(node, "invalid_window", "window must be a positive integer")

    sources = _resolve_inputs(node, direct, specs, expected_types=EventSpec)
    # `_resolve_inputs` already required each input to be exactly EventSpec.
    first, second = cast(list[EventSpec], sources)
    # ADR-0100 修订 1 §3: the two inputs must be different EventSpecs. A sequence of an event
    # with itself has no accepted meaning (every occurrence would pair with its own predecessor,
    # and the upstream binding would name one ref twice), so it is refused, not lowered.
    if first.ref.target_identity() == second.ref.target_identity():
        _refuse(
            node,
            "temporal_same_input",
            f"both temporal inputs are the same EventSpec ({first.ref}); the first and second "
            "event must be different EventSpecs (ADR-0100 revision 1 §3)",
        )
    if first.bar_spec is None or second.bar_spec is None:
        _operator_open(
            node,
            "both temporal inputs must declare a non-empty EventSpec.bar_spec (ADR-0088 "
            "decision 1); an undeclared bar spec cannot anchor a bar-count window",
        )
    if first.bar_spec.target_identity() != second.bar_spec.target_identity():
        _operator_open(
            node,
            f"temporal inputs declare different bar specs ({first.bar_spec} vs "
            f"{second.bar_spec}); a bar-count window needs one shared bar spec (ADR-0088 "
            "decision 1)",
        )
    # The output becomes visible when the second event is visible. That is only sound when the
    # first event is certainly visible by then: it occurs at least one bar earlier, but the bar
    # duration is not readable here (bar_spec is a bare Ref), so the only provable case is a first
    # event whose observable lag does not exceed the second's. Otherwise refuse rather than emit a
    # spec whose declared visibility could precede the first event's availability.
    if first.observable_lag > second.observable_lag:
        _refuse(
            node,
            "temporal_visibility_unprovable",
            "the first event's observable_lag exceeds the second's, so the combined event could "
            "be declared visible before its first event is observable",
        )

    bar_spec = first.bar_spec
    identity = _spec_identity(_TEMPORAL_DEFINITION, node, sources, created_at)
    trigger = canonical_json(
        {
            "definition": _TEMPORAL_DEFINITION,
            "operator": "temporal_sequence",
            "provider": "p7_temporal_sequence@1.0.0",
            "semantic_version": _SEMANTIC_VERSION,
            "first_event": str(first.ref),
            "second_event": str(second.ref),
            "bar_spec": str(bar_spec),
            "window_bars": window,
            # The second event lies in (first, first + window bars]: 1..window bars after it.
            "interval": "left_open_right_closed",
            "event_time": "second_event_time",
            "visibility": "second_event_observable_time",
            "missing": "no_event",
        }
    )
    return _build_output(
        node,
        lambda: EventSpec(
            name=f"p7_temporal_{identity}",
            version=_SEMANTIC_VERSION,
            created_at=created_at,
            trigger=trigger,
            features=_union_refs((first.features, second.features)),
            states=_union_refs((first.states, second.states)),
            observable_lag=second.observable_lag,
            bar_spec=bar_spec,
            lineage=(first.ref, second.ref),
        ),
    )


def _lower_conditioning(
    node: PlanNode,
    direct: dict[tuple[str, int], VersionedSpec],
    specs: dict[str, VersionedSpec],
    created_at: datetime,
) -> StrategySpec:
    """Hold ``base``'s target position while ``state == state_value``; flat otherwise.

    Unknown or missing state labels are flat. One composed strategy is one trial per
    (base, state, state_value) (ADR-0088 decision 2).
    """
    raw_state_value = node.parameters["state_value"]
    if not isinstance(raw_state_value, str) or not raw_state_value:  # Parser enforces this too.
        _refuse(node, "invalid_state_value", "state_value must be non-empty text")
    state_value: str = raw_state_value
    sources = _resolve_inputs(node, direct, specs, expected_types=(StrategySpec, StateSpec))
    # `_resolve_inputs` already required the exact class per position.
    base = cast(StrategySpec, sources[0])
    state = cast(StateSpec, sources[1])
    if state_value not in state.state_space:
        # A gate on a label outside the declared state space can never open: refuse instead of
        # emitting a strategy that is always flat by construction.
        _refuse(
            node,
            "unknown_state_value",
            f"state_value {state_value!r} is not in {state.ref}'s declared state_space",
        )

    identity = _spec_identity(_CONDITIONING_DEFINITION, node, sources, created_at)
    return _build_output(
        node,
        lambda: StrategySpec(
            name=f"p7_conditioning_{identity}",
            version=_SEMANTIC_VERSION,
            created_at=created_at,
            signals=_union_refs((base.signals, (state.ref,))),
            params=FrozenMapping(
                {
                    "definition": _CONDITIONING_DEFINITION,
                    "operator": "conditioned",
                    "provider": "p7_conditioning_state_gate@1.0.0",
                    "semantic_version": _SEMANTIC_VERSION,
                    "unmatched_state": "flat",
                    "unknown_state": "flat",
                }
            ),
            risk_policy=base.risk_policy,
            applicable_instruments=base.applicable_instruments,
            composition=ConditionedStrategy(
                base=base.ref, state=state.ref, state_value=state_value
            ),
            lineage=(base.ref, state.ref),
        ),
    )


def _lower_ensemble(
    node: PlanNode,
    direct: dict[tuple[str, int], VersionedSpec],
    specs: dict[str, VersionedSpec],
    created_at: datetime,
) -> StrategySpec:
    """Equal-weight mean of the members' target positions (ADR-0088 decision 2)."""
    sources = _resolve_inputs(node, direct, specs, expected_types=StrategySpec)
    members = cast(list[StrategySpec], sources)  # Exact class checked by `_resolve_inputs`.
    if len(members) < 2:  # Defensive: typed_plan.py enforces arity.
        _refuse(node, "wrong_input_arity", "ensemble needs at least two StrategySpec members")
    identities = [member.ref.target_identity() for member in members]
    if len(set(identities)) != len(identities):
        _refuse(node, "duplicate_ensemble_member", "ensemble members must be distinct strategies")
    # ADR-0069 combine rule: risk policy and applicable instruments must match exactly across
    # members; nothing is inferred, chosen or merged.
    head = members[0]
    for member in members[1:]:
        if member.risk_policy != head.risk_policy:
            _refuse(
                node,
                "ensemble_risk_policy_mismatch",
                f"{member.ref} risk_policy differs from {head.ref} (ADR-0069)",
            )
        if member.applicable_instruments != head.applicable_instruments:
            _refuse(
                node,
                "ensemble_instruments_mismatch",
                f"{member.ref} applicable_instruments differ from {head.ref} (ADR-0069)",
            )

    identity = _spec_identity(_ENSEMBLE_DEFINITION, node, sources, created_at)
    member_refs = tuple(member.ref for member in members)
    return _build_output(
        node,
        lambda: StrategySpec(
            name=f"p7_ensemble_{identity}",
            version=_SEMANTIC_VERSION,
            created_at=created_at,
            signals=_union_refs(member.signals for member in members),
            params=FrozenMapping(
                {
                    "definition": _ENSEMBLE_DEFINITION,
                    "operator": "ensemble",
                    "provider": "p7_ensemble_equal_weight_mean@1.0.0",
                    "semantic_version": _SEMANTIC_VERSION,
                    "rule": "equal_weight_mean",
                    "cost_basis": "net_combined_position_change",
                }
            ),
            risk_policy=head.risk_policy,
            applicable_instruments=head.applicable_instruments,
            composition=EnsembleStrategy(members=member_refs, rule="equal_weight_mean"),
            lineage=member_refs,
        ),
    )


def _lower_negation(
    node: PlanNode,
    direct: dict[tuple[str, int], VersionedSpec],
    specs: dict[str, VersionedSpec],
    created_at: datetime,
) -> StrategySpec:
    """Negate ``base``'s target position; not a validation negative control (ADR-0088)."""
    sources = _resolve_inputs(node, direct, specs, expected_types=StrategySpec)
    (base,) = cast(list[StrategySpec], sources)  # Exact class checked by `_resolve_inputs`.
    identity = _spec_identity(_NEGATION_DEFINITION, node, sources, created_at)
    params: dict[str, str | int | float] = {
        "definition": _NEGATION_DEFINITION,
        "operator": "negated",
        "provider": "p7_negation_target_position@1.0.0",
        "semantic_version": _SEMANTIC_VERSION,
        "negates": "target_position",
        "cost_basis": "negated_trades",
        # Negative controls are defined by the validation layer, never by this spec.
        "validation_negative_control": False,
        # Spot short-cost gap (ST-4): the future Provider must fail closed.
        "short_exposure": "provider_fail_closed_without_short_cost_model",
    }
    return _build_output(
        node,
        lambda: StrategySpec(
            name=f"p7_negation_{identity}",
            version=_SEMANTIC_VERSION,
            created_at=created_at,
            signals=base.signals,
            params=FrozenMapping(params),
            risk_policy=base.risk_policy,
            applicable_instruments=base.applicable_instruments,
            composition=NegatedStrategy(base=base.ref),
            lineage=(base.ref,),
        ),
    )


_LOWERERS: Final[
    dict[
        PlanOperator,
        Callable[
            [PlanNode, dict[tuple[str, int], VersionedSpec], dict[str, VersionedSpec], datetime],
            VersionedSpec,
        ],
    ]
] = {
    PlanOperator.INTERACTION: _lower_interaction,
    # TRANSFORMATION is dispatched in `lower_typed_plan`: it also needs the plan format version.
    PlanOperator.TEMPORAL: _lower_temporal,
    PlanOperator.CONDITIONING: _lower_conditioning,
    PlanOperator.ENSEMBLE: _lower_ensemble,
    PlanOperator.NEGATION: _lower_negation,
}


def lower_typed_plan(
    plan: TypedPlan,
    *,
    resolution: DirectReferenceResolution,
    created_at: datetime,
    universes: Iterable[ResearchDatasetManifest] = (),
) -> dict[str, VersionedSpec]:
    """Lower every node to one core spec, or refuse the whole plan.

    ``created_at`` is mandatory because the core spec envelope includes it in content identity;
    no wall clock value is introduced implicitly. Inputs from prior nodes are resolved from the
    already lowered node map. In a "1.1.0" plan, ``transformation`` with ``rank`` / ``quantile``
    is still ``operator_open`` and refuses the whole plan; in a "1.2.0" plan it lowers to the
    time-series definitions of ADR-0099.

    ``universes`` are the caller-supplied pinned universe manifests for cross-sectional nodes
    (format "1.3.0", ADR-0100 §2). Like ``resolution`` they are evidence, not a Registry lookup:
    each node's ``universe_hash`` must equal one manifest's recomputed content hash and its
    ``universe`` must name that manifest's dataset, otherwise the whole plan is refused.
    """
    if type(plan) is not TypedPlan:
        raise TypeError("plan must be an exact TypedPlan")
    try:
        canonical = parse_plan_json(
            json.dumps(
                {
                    # Re-parse with the plan's own format version (ADR-0099 decision 4).
                    "schema_version": plan.schema_version,
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
    universe_by_hash = _universe_map(universes)
    specs: dict[str, VersionedSpec] = {}

    for node in plan.nodes:
        if node.operator is PlanOperator.TRANSFORMATION:
            # The accepted transform set depends on the plan's own format version (ADR-0099
            # decision 4), so this lowerer also receives it; cross-sectional nodes (ADR-0100 §2)
            # also need the caller-supplied pinned universe manifests.
            specs[node.node_id] = _lower_transformation(
                node,
                direct,
                specs,
                created_at,
                plan_format=plan.schema_version,
                universes=universe_by_hash,
            )
            continue
        lowerer = _LOWERERS.get(node.operator)
        if lowerer is None:  # Defensive: every other closed-world operator has an entry.
            _operator_open(node, "no accepted lowering for this operator")
        specs[node.node_id] = lowerer(node, direct, specs, created_at)

    return specs

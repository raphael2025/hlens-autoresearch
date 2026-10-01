"""Pure, fail-closed binding checks for typed-plan admission evidence (ADR-0073 §1).

These checks establish that caller-supplied ``ExperimentSpec`` values bind the exact batch
``Hypothesis`` values and that each declared lowered output is a direct, hash-matched dependency
of its associated experiment. They do not lower a plan, validate an operator's semantics, persist
evidence, register trials, or authorize execution.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from core.domain.base import Contract, Ref, VersionedSpec
from core.domain.research import ExperimentSpec, Hypothesis
from core.domain.specs import (
    ConditionedStrategy,
    EnsembleStrategy,
    EventSpec,
    FeatureSpec,
    NegatedStrategy,
    StateSpec,
    StrategySpec,
)
from research.hypotheses.typed_plan import (
    PlanNode,
    PlanOperator,
    PlanOutputType,
    PlanRefused,
    TypedPlan,
    parse_plan_json,
)

__all__ = [
    "ExperimentHypothesisBinding",
    "LoweredOutputBinding",
    "PlanBindingRefused",
    "produce_lowered_output_bindings",
    "validate_experiment_bindings",
    "validate_complete_experiment_bindings",
]

_HASH_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
_LOWERED_SPEC_TYPES: Final = (EventSpec, FeatureSpec, StateSpec, StrategySpec)


class PlanBindingRefused(PlanRefused):
    """The supplied ExperimentSpec / Hypothesis / lowered-output evidence disagrees."""

    def __init__(self, code: str, where: str, detail: str) -> None:
        self.code = code
        self.where = where
        super().__init__(f"{code}: {where}: {detail}")


@dataclass(frozen=True, slots=True)
class LoweredOutputBinding:
    """One lowered core spec and the experiment whose dependency it claims to satisfy.

    ``experiment_hash`` is the ExperimentSpec's existing ``repro.experiment_hash`` identity.
    The association is in-memory evidence for validation; it adds no persisted or domain field.
    """

    experiment_hash: str
    spec: VersionedSpec
    node_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.experiment_hash, str)
            or _HASH_PATTERN.fullmatch(self.experiment_hash) is None
        ):
            raise ValueError("experiment_hash must be a canonical lowercase SHA-256")
        if type(self.spec) not in _LOWERED_SPEC_TYPES:
            raise TypeError("lowered output must be an exact Feature/State/Event/StrategySpec")
        if self.node_id is not None and (
            not isinstance(self.node_id, str)
            or not self.node_id
            or self.node_id != self.node_id.strip()
        ):
            raise ValueError("node_id must be non-empty canonical text when supplied")


@dataclass(frozen=True, slots=True)
class ExperimentHypothesisBinding:
    """Detached values returned by the validator; this object is not an admission credential."""

    experiment: ExperimentSpec
    hypothesis: Hypothesis
    outputs: tuple[VersionedSpec, ...]


def _copy_validated[T: Contract](value: T, expected: type[T], where: str) -> T:
    """Rebuild from canonical JSON so validation does not trust a caller's mutable internals."""
    if type(value) is not expected:
        raise PlanBindingRefused(
            "wrong_type", where, f"expected exactly {expected.__name__}, got {type(value).__name__}"
        )
    try:
        payload = value.model_dump(mode="json")
        rebuilt = expected.model_validate(payload)
    except Exception as exc:
        raise PlanBindingRefused(
            "invalid_contract", where, f"contract reconstruction failed ({type(exc).__name__})"
        ) from exc
    if rebuilt.model_dump(mode="json") != payload:
        raise PlanBindingRefused("noncanonical_contract", where, "payload did not round-trip")
    return rebuilt


def _canonical_spec(spec: VersionedSpec, where: str) -> VersionedSpec:
    expected = next((kind for kind in _LOWERED_SPEC_TYPES if type(spec) is kind), None)
    if expected is None:
        raise PlanBindingRefused(
            "unsupported_output_type",
            where,
            "lowered output must be exactly FeatureSpec, StateSpec, EventSpec, or StrategySpec",
        )
    return _copy_validated(spec, expected, where)


def _canonical_plan(plan: TypedPlan, where: str) -> TypedPlan:
    if type(plan) is not TypedPlan:
        raise PlanBindingRefused("wrong_type", where, "must be an exact TypedPlan")
    try:
        payload = {
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
        }
        rebuilt = parse_plan_json(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            limits=plan.limits,
        )
    except Exception as exc:
        raise PlanBindingRefused(
            "invalid_plan", where, f"typed plan reconstruction failed ({type(exc).__name__})"
        ) from exc
    if rebuilt != plan:
        raise PlanBindingRefused(
            "noncanonical_plan", where, "plan did not round-trip through parser"
        )
    return rebuilt


#: ADR-0088 decision 2 gives the conditional strategy plan a versioned core representation: a
#: StrategySpec whose ``composition`` is ``ConditionedStrategy``. Before contract 2.4.0 it had none
#: and ADR-0078 required this output type to fail closed.
_PLAN_OUTPUT_SPEC_TYPES: Final[dict[PlanOutputType, type[VersionedSpec]]] = {
    PlanOutputType.CONDITIONAL_STRATEGY: StrategySpec,
    PlanOutputType.EVENT: EventSpec,
    PlanOutputType.FEATURE: FeatureSpec,
    PlanOutputType.STRATEGY: StrategySpec,
}

#: The StrategySpec produced by a composing operator must carry exactly that operator's
#: composition (ADR-0088 decision 2); a plain or differently composed StrategySpec is refused.
_OPERATOR_COMPOSITIONS: Final[dict[PlanOperator, type[object]]] = {
    PlanOperator.CONDITIONING: ConditionedStrategy,
    PlanOperator.ENSEMBLE: EnsembleStrategy,
    PlanOperator.NEGATION: NegatedStrategy,
}


def _require_operator_composition(node: PlanNode, spec: VersionedSpec, where: str) -> None:
    composition_type = _OPERATOR_COMPOSITIONS.get(node.operator)
    if composition_type is None:
        return
    if not isinstance(spec, StrategySpec) or type(spec.composition) is not composition_type:
        raise PlanBindingRefused(
            "plan_output_composition_mismatch",
            where,
            f"{node.operator.value} requires a StrategySpec composed as "
            f"{composition_type.__name__}",
        )


def produce_lowered_output_bindings(
    *,
    experiment_hash: str,
    plan: TypedPlan,
    specs_by_node: dict[str, VersionedSpec],
) -> tuple[LoweredOutputBinding, ...]:
    """Build the complete, node-addressed output set for a typed plan.

    The AST node inventory is the authority for expected outputs: exactly one output spec per
    node, with the nominal kind declared by that node. This records/validates supplied lowering
    products; it does not implement any of the six operator semantics or make plans runnable.
    """
    if not isinstance(experiment_hash, str) or _HASH_PATTERN.fullmatch(experiment_hash) is None:
        raise PlanBindingRefused(
            "invalid_experiment_hash", "experiment_hash", "must be a canonical lowercase SHA-256"
        )
    plan = _canonical_plan(plan, "plan")
    if not isinstance(specs_by_node, dict):
        raise PlanBindingRefused(
            "invalid_output_map", "specs_by_node", "must be a node-to-spec mapping"
        )
    if any(not isinstance(node_id, str) for node_id in specs_by_node):
        raise PlanBindingRefused(
            "invalid_output_map",
            "specs_by_node",
            "all node ids must be strings",
        )

    expected_nodes = {node.node_id: node for node in plan.nodes}
    actual_nodes = set(specs_by_node)
    missing = sorted(set(expected_nodes) - actual_nodes)
    extra = sorted(actual_nodes - set(expected_nodes))
    if missing:
        raise PlanBindingRefused("missing_node_output", "specs_by_node", ", ".join(missing))
    if extra:
        raise PlanBindingRefused("extra_node_output", "specs_by_node", ", ".join(extra))

    result: list[LoweredOutputBinding] = []
    for node in plan.nodes:
        where = f"specs_by_node[{node.node_id!r}]"
        expected_type = _PLAN_OUTPUT_SPEC_TYPES.get(node.output_type)
        if expected_type is None:
            raise PlanBindingRefused(
                "unsupported_plan_output_type",
                where,
                f"{node.output_type.value} has no approved versioned output spec",
            )
        spec = specs_by_node[node.node_id]
        if type(spec) is not expected_type:
            raise PlanBindingRefused(
                "plan_output_type_mismatch",
                where,
                f"{node.output_type.value} requires exactly {expected_type.__name__}",
            )
        _require_operator_composition(node, spec, where)
        result.append(
            LoweredOutputBinding(
                experiment_hash=experiment_hash,
                spec=_canonical_spec(spec, where),
                node_id=node.node_id,
            )
        )
    return tuple(result)


def _identity(ref: Ref) -> str:
    return str(ref)


def validate_experiment_bindings(
    *,
    experiment_specs: Sequence[ExperimentSpec],
    hypotheses: Sequence[Hypothesis],
    lowered_outputs: Sequence[LoweredOutputBinding],
) -> tuple[ExperimentHypothesisBinding, ...]:
    """Validate exact batch-to-experiment and lowered-output bindings without side effects.

    The ExperimentSpec and Hypothesis sequences must be non-empty. Each ExperimentSpec must name
    one exact batch
    ``hypothesis:name@version`` and bind that hypothesis's recomputed content hash in
    ``dependency_hashes``. Every batch hypothesis must be consumed once and only once. Every
    supplied output must be unique within its experiment, and its exact ``kind:name@version`` and
    recomputed hash must appear in that experiment's direct ``dependency_hashes``. The same
    immutable output may be referenced by more than one experiment.
    No transitive dependency or compiler completeness claim is made.

    The returned tuple preserves ExperimentSpec order and contains detached, reconstructed
    contracts. Any discrepancy raises ``PlanBindingRefused``; no partial bindings are returned.
    """
    if not isinstance(experiment_specs, Sequence) or isinstance(experiment_specs, (str, bytes)):
        raise PlanBindingRefused(
            "invalid_sequence", "experiment_specs", "must be an ordered sequence"
        )
    if not isinstance(hypotheses, Sequence) or isinstance(hypotheses, (str, bytes)):
        raise PlanBindingRefused("invalid_sequence", "hypotheses", "must be an ordered sequence")
    if not isinstance(lowered_outputs, Sequence) or isinstance(lowered_outputs, (str, bytes)):
        raise PlanBindingRefused(
            "invalid_sequence", "lowered_outputs", "must be an ordered sequence"
        )
    if not experiment_specs or not hypotheses:
        raise PlanBindingRefused(
            "empty_batch", "bindings", "experiments and hypotheses are required"
        )
    if len(experiment_specs) != len(hypotheses):
        raise PlanBindingRefused(
            "batch_cardinality_mismatch",
            "bindings",
            f"{len(experiment_specs)} ExperimentSpecs for {len(hypotheses)} hypotheses",
        )

    verified_hypotheses: dict[str, Hypothesis] = {}
    hypothesis_hashes: dict[str, str] = {}
    for index, hypothesis_item in enumerate(hypotheses):
        where = f"hypotheses[{index}]"
        verified_hypothesis = _copy_validated(hypothesis_item, Hypothesis, where)
        ref = verified_hypothesis.ref
        key = _identity(ref)
        if key in verified_hypotheses:
            raise PlanBindingRefused("duplicate_hypothesis", where, f"repeated {key}")
        verified_hypotheses[key] = verified_hypothesis
        hypothesis_hashes[key] = verified_hypothesis.content_hash()

    verified_experiments: list[ExperimentSpec] = []
    experiment_hashes: set[str] = set()
    experiment_hypotheses: dict[str, str] = {}
    hypothesis_use_count: dict[str, int] = dict.fromkeys(verified_hypotheses, 0)
    for index, experiment_item in enumerate(experiment_specs):
        where = f"experiment_specs[{index}]"
        verified_experiment = _copy_validated(experiment_item, ExperimentSpec, where)
        experiment_hash = verified_experiment.experiment_hash
        if experiment_hash in experiment_hashes:
            raise PlanBindingRefused("duplicate_experiment", where, "repeated ExperimentSpec hash")
        experiment_hashes.add(experiment_hash)

        hypothesis_ref = verified_experiment.repro.hypothesis_ref
        hypothesis_key = _identity(hypothesis_ref)
        matched_hypothesis = verified_hypotheses.get(hypothesis_key)
        if matched_hypothesis is None:
            raise PlanBindingRefused(
                "unknown_hypothesis", where, f"{hypothesis_key} is not in the supplied batch"
            )
        dependency_hash = verified_experiment.repro.dependency_hashes.get(hypothesis_key)
        expected_hash = hypothesis_hashes[hypothesis_key]
        if dependency_hash != expected_hash:
            raise PlanBindingRefused(
                "hypothesis_hash_mismatch",
                where,
                f"dependency hash for {hypothesis_key} does not match the Hypothesis content hash",
            )
        hypothesis_use_count[hypothesis_key] += 1
        experiment_hypotheses[experiment_hash] = hypothesis_key
        verified_experiments.append(verified_experiment)

    repeated = sorted(key for key, count in hypothesis_use_count.items() if count > 1)
    if repeated:
        raise PlanBindingRefused(
            "hypothesis_used_more_than_once", "experiment_specs", ", ".join(repeated)
        )
    missing_hypotheses = sorted(key for key, count in hypothesis_use_count.items() if count == 0)
    if missing_hypotheses:
        raise PlanBindingRefused(
            "hypothesis_not_bound", "experiment_specs", ", ".join(missing_hypotheses)
        )

    outputs_by_experiment: dict[str, list[VersionedSpec]] = {
        experiment_hash: [] for experiment_hash in experiment_hashes
    }
    seen_outputs: set[tuple[str, str]] = set()
    experiment_by_hash = {item.experiment_hash: item for item in verified_experiments}
    for index, output_item in enumerate(lowered_outputs):
        where = f"lowered_outputs[{index}]"
        if not isinstance(output_item, LoweredOutputBinding):
            raise PlanBindingRefused(
                "wrong_type", where, "must be a LoweredOutputBinding with experiment association"
            )
        output_experiment = experiment_by_hash.get(output_item.experiment_hash)
        if output_experiment is None:
            raise PlanBindingRefused(
                "extra_output_experiment",
                where,
                f"{output_item.experiment_hash} is not one of the supplied ExperimentSpecs",
            )
        canonical_output = _canonical_spec(output_item.spec, where)
        output_ref = canonical_output.ref
        output_key = _identity(output_ref)
        output_identity = (output_item.experiment_hash, output_key)
        if output_identity in seen_outputs:
            raise PlanBindingRefused("duplicate_output", where, f"repeated {output_key}")
        seen_outputs.add(output_identity)

        dependency_hash = output_experiment.repro.dependency_hashes.get(output_key)
        if dependency_hash is None:
            raise PlanBindingRefused(
                "output_not_direct_dependency",
                where,
                f"{output_key} is absent from {output_item.experiment_hash}'s direct dependencies",
            )
        output_hash = canonical_output.content_hash()
        if dependency_hash != output_hash:
            raise PlanBindingRefused(
                "output_hash_mismatch",
                where,
                f"dependency hash for {output_key} differs from its recomputed content hash",
            )
        outputs_by_experiment[output_item.experiment_hash].append(canonical_output)

    return tuple(
        ExperimentHypothesisBinding(
            experiment=experiment,
            hypothesis=verified_hypotheses[experiment_hypotheses[experiment.experiment_hash]],
            outputs=tuple(outputs_by_experiment[experiment.experiment_hash]),
        )
        for experiment in verified_experiments
    )


def validate_complete_experiment_bindings(
    *,
    experiment_specs: Sequence[ExperimentSpec],
    hypotheses: Sequence[Hypothesis],
    plans_by_experiment: dict[str, TypedPlan],
    lowered_outputs: Sequence[LoweredOutputBinding],
) -> tuple[ExperimentHypothesisBinding, ...]:
    """Validate exact AST-node output coverage plus the existing direct ref/hash bindings.

    Every ExperimentSpec must have exactly one associated TypedPlan. Each plan node must occur
    once in the output evidence; missing, additional, duplicate, wrong-kind, and hash-mismatched
    outputs fail closed before any caller receives partial bindings.
    """
    if not isinstance(experiment_specs, Sequence) or isinstance(experiment_specs, (str, bytes)):
        raise PlanBindingRefused(
            "invalid_sequence", "experiment_specs", "must be an ordered sequence"
        )
    experiment_hashes: list[str] = []
    for index, item in enumerate(experiment_specs):
        experiment = _copy_validated(item, ExperimentSpec, f"experiment_specs[{index}]")
        experiment_hashes.append(experiment.experiment_hash)
    if len(set(experiment_hashes)) != len(experiment_hashes):
        raise PlanBindingRefused(
            "duplicate_experiment", "experiment_specs", "repeated ExperimentSpec hash"
        )
    if not isinstance(plans_by_experiment, dict):
        raise PlanBindingRefused(
            "invalid_plan_map", "plans_by_experiment", "must be an experiment-to-plan mapping"
        )
    if any(
        not isinstance(experiment_hash, str) or _HASH_PATTERN.fullmatch(experiment_hash) is None
        for experiment_hash in plans_by_experiment
    ):
        raise PlanBindingRefused(
            "invalid_plan_map",
            "plans_by_experiment",
            "keys must be canonical experiment hashes",
        )
    if not isinstance(lowered_outputs, Sequence) or isinstance(lowered_outputs, (str, bytes)):
        raise PlanBindingRefused(
            "invalid_sequence", "lowered_outputs", "must be an ordered sequence"
        )
    missing_plans = sorted(set(experiment_hashes) - set(plans_by_experiment))
    extra_plans = sorted(set(plans_by_experiment) - set(experiment_hashes))
    if missing_plans:
        raise PlanBindingRefused("missing_plan", "plans_by_experiment", ", ".join(missing_plans))
    if extra_plans:
        raise PlanBindingRefused("extra_plan", "plans_by_experiment", ", ".join(extra_plans))

    expected_nodes_by_experiment: dict[str, set[str]] = {}
    canonical_plans: dict[str, TypedPlan] = {}
    for experiment_hash, plan in plans_by_experiment.items():
        plan = _canonical_plan(plan, f"plans_by_experiment[{experiment_hash}]")
        canonical_plans[experiment_hash] = plan
        node_ids = [node.node_id for node in plan.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise PlanBindingRefused(
                "duplicate_plan_node",
                f"plans_by_experiment[{experiment_hash}]",
                "node ids repeat",
            )
        expected_nodes_by_experiment[experiment_hash] = set(node_ids)

    seen: set[tuple[str, str]] = set()
    supplied_nodes: dict[str, set[str]] = {key: set() for key in experiment_hashes}
    for index, output in enumerate(lowered_outputs):
        where = f"lowered_outputs[{index}]"
        if not isinstance(output, LoweredOutputBinding):
            raise PlanBindingRefused("wrong_type", where, "must be a LoweredOutputBinding")
        if output.experiment_hash not in expected_nodes_by_experiment:
            raise PlanBindingRefused(
                "extra_output_experiment", where, "unknown ExperimentSpec association"
            )
        if output.node_id is None:
            raise PlanBindingRefused(
                "missing_node_identity", where, "complete validation requires a plan node id"
            )
        identity = (output.experiment_hash, output.node_id)
        if identity in seen:
            raise PlanBindingRefused(
                "duplicate_node_output", where, f"repeated node {output.node_id}"
            )
        seen.add(identity)
        if output.node_id not in expected_nodes_by_experiment[output.experiment_hash]:
            raise PlanBindingRefused("extra_node_output", where, f"unknown node {output.node_id}")
        node = next(
            item
            for item in canonical_plans[output.experiment_hash].nodes
            if item.node_id == output.node_id
        )
        expected_type = _PLAN_OUTPUT_SPEC_TYPES.get(node.output_type)
        if expected_type is None:
            raise PlanBindingRefused(
                "unsupported_plan_output_type",
                where,
                f"{node.output_type.value} has no approved versioned output spec",
            )
        if type(output.spec) is not expected_type:
            raise PlanBindingRefused(
                "plan_output_type_mismatch",
                where,
                f"node {output.node_id} requires exactly {expected_type.__name__}",
            )
        _require_operator_composition(node, output.spec, where)
        supplied_nodes[output.experiment_hash].add(output.node_id)

    for experiment_hash, expected in expected_nodes_by_experiment.items():
        missing = sorted(expected - supplied_nodes[experiment_hash])
        if missing:
            raise PlanBindingRefused(
                "missing_node_output", f"lowered_outputs[{experiment_hash}]", ", ".join(missing)
            )

    return validate_experiment_bindings(
        experiment_specs=experiment_specs,
        hypotheses=hypotheses,
        lowered_outputs=lowered_outputs,
    )

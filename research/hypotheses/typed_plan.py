"""Closed-world, non-runnable Phase 7 operator plan data (ADR-0068).

This module parses and type-checks plan structure only. It deliberately has no operator
implementations, registry integration, persistence, or path into the research loop. A valid
``TypedPlan`` is always non-runnable until a separately approved operator implementation and
the remaining execution / audit protocol exist.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from core.domain.base import (
    NAME_PATTERN,
    SHA256_PATTERN,
    FrozenMapping,
    Kind,
    Ref,
    content_hash,
)

__all__ = [
    "PLAN_FORMAT_VERSION",
    "PlanInput",
    "PlanLimits",
    "PlanNode",
    "PlanOperator",
    "PlanOutputType",
    "PlanRejected",
    "PlanRefused",
    "SpecInput",
    "NodeInput",
    "TypedPlan",
    "compile_plan",
    "parse_plan_json",
]

#: Bumped 1.0.0 -> 1.1.0 (ADR-0082, transformation acceptance): a ``transformation`` node now
#: additionally requires an explicit ``window`` parameter (positive integer bar count). A "1.0.0"
#: transformation payload with only ``transform`` no longer parses under "1.1.0"; no other
#: operator's grammar changed. No migration is required: no ``TypedPlan.runnable`` was ever True
#: and no persisted "1.0.0" transformation payload exists (transformation was unconditionally
#: ``operator_open`` before this ADR amendment).
PLAN_FORMAT_VERSION: Final = "1.1.0"
_NAME = re.compile(NAME_PATTERN)
_HASH = re.compile(SHA256_PATTERN)


class PlanRejected(ValueError):
    """The supplied plan data is malformed, ambiguous, or outside explicit resource limits."""


class PlanRefused(RuntimeError):
    """A structurally valid plan cannot be compiled or run under the current closed-world set."""


class PlanOperator(StrEnum):
    CONDITIONING = "conditioning"
    INTERACTION = "interaction"
    TEMPORAL = "temporal"
    TRANSFORMATION = "transformation"
    ENSEMBLE = "ensemble"
    NEGATION = "negation"


class PlanOutputType(StrEnum):
    """Nominal plan output types; these are not executable Provider contracts."""

    CONDITIONAL_STRATEGY = "conditional_strategy_plan"
    EVENT = "event"
    FEATURE = "feature"
    STRATEGY = "strategy"


@dataclass(frozen=True, slots=True)
class PlanLimits:
    """Caller-supplied parsing bounds. No resource limit has an implicit default."""

    max_depth: int
    max_nodes: int
    max_json_bytes: int
    max_parameters_per_node: int

    def __post_init__(self) -> None:
        for name in (
            "max_depth",
            "max_nodes",
            "max_json_bytes",
            "max_parameters_per_node",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be an explicitly supplied positive integer")

    def payload(self) -> dict[str, int]:
        return {
            "max_depth": self.max_depth,
            "max_nodes": self.max_nodes,
            "max_json_bytes": self.max_json_bytes,
            "max_parameters_per_node": self.max_parameters_per_node,
        }


@dataclass(frozen=True, slots=True)
class SpecInput:
    """An exact versioned object reference plus the caller-resolved content hash."""

    ref: Ref
    content_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.ref, Ref):
            raise TypeError("SpecInput.ref must be a parsed Ref")
        if not isinstance(self.content_hash, str) or _HASH.fullmatch(self.content_hash) is None:
            raise ValueError("SpecInput.content_hash must be a lowercase SHA-256 content hash")

    def payload(self) -> dict[str, str]:
        return {"ref": str(self.ref), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class NodeInput:
    """A dependency on an earlier node in the same topologically ordered plan."""

    node_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or _NAME.fullmatch(self.node_id) is None:
            raise ValueError("NodeInput.node_id must match the project name syntax")

    def payload(self) -> dict[str, str]:
        return {"node": self.node_id}


PlanInput = SpecInput | NodeInput
PlanScalar = str | int | bool


@dataclass(frozen=True, slots=True)
class PlanNode:
    """A typed, declarative AST node. ``runnable`` is intentionally always false."""

    node_id: str
    operator: PlanOperator
    inputs: tuple[PlanInput, ...]
    parameters: FrozenMapping[str, PlanScalar]

    @property
    def output_type(self) -> PlanOutputType:
        return _SIGNATURES[self.operator][1]

    @property
    def runnable(self) -> bool:
        return False

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "operator": self.operator.value,
            "inputs": [item.payload() for item in self.inputs],
            "parameters": dict(self.parameters),
            "output_type": self.output_type.value,
            "runnable": False,
        }


@dataclass(frozen=True, slots=True)
class TypedPlan:
    """Immutable checked plan data. A valid plan does not imply an executable operator."""

    root: str
    nodes: tuple[PlanNode, ...]
    limits: PlanLimits

    @property
    def runnable(self) -> bool:
        return False

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": PLAN_FORMAT_VERSION,
            "root": self.root,
            "limits": self.limits.payload(),
            "nodes": [node.payload() for node in self.nodes],
            "runnable": False,
        }

    def content_hash(self) -> str:
        """Canonical project JSON SHA-256, including the explicit resource limits."""
        return content_hash(self.payload())


_SIGNATURES: Final[dict[PlanOperator, tuple[tuple[Kind, ...], PlanOutputType, int | None]]] = {
    PlanOperator.CONDITIONING: (
        (Kind.STRATEGY, Kind.STATE),
        PlanOutputType.CONDITIONAL_STRATEGY,
        2,
    ),
    PlanOperator.INTERACTION: ((Kind.FEATURE, Kind.FEATURE), PlanOutputType.FEATURE, 2),
    PlanOperator.TEMPORAL: ((Kind.EVENT, Kind.EVENT), PlanOutputType.EVENT, 2),
    PlanOperator.TRANSFORMATION: ((Kind.FEATURE,), PlanOutputType.FEATURE, 1),
    PlanOperator.ENSEMBLE: ((Kind.STRATEGY,), PlanOutputType.STRATEGY, None),
    PlanOperator.NEGATION: ((Kind.STRATEGY,), PlanOutputType.STRATEGY, 1),
}

# Closed syntactic parameter shapes drawn from ADR-0068 and the existing declaration helpers.
# They specify data shape only; they do not define an operator algorithm or make it runnable.
_PARAMETER_KEYS: Final[dict[PlanOperator, frozenset[str]]] = {
    PlanOperator.CONDITIONING: frozenset({"state_value"}),
    PlanOperator.INTERACTION: frozenset(),
    PlanOperator.TEMPORAL: frozenset({"window", "time_unit"}),
    #: ADR-0082 (transformation acceptance): ``window`` is a required positive integer bar count
    #: for every transformation node, regardless of ``transform`` name. For the three accepted
    #: time-series transforms (standardize / difference / smooth) it is the sole, mandatory,
    #: backward-looking-only computation window; for ``standardize`` it doubles as the Constitution
    #: C-L3 training window (a rolling fit confined to this trailing window never sees data outside
    #: it, so a bound window is by construction a bound training window). A missing ``window`` is
    #: rejected here at parse time — there is no separate "training window binding" to omit.
    #: ``rank`` / ``quantile`` remain ``operator_open`` at lowering time and are still required to
    #: supply a syntactically valid ``window`` even though it is unused; the closed-world grammar
    #: does not vary by parameter value.
    PlanOperator.TRANSFORMATION: frozenset({"transform", "window"}),
    PlanOperator.ENSEMBLE: frozenset(),
    PlanOperator.NEGATION: frozenset(),
}
_TRANSFORMS: Final = frozenset({"standardize", "rank", "quantile", "difference", "smooth"})
_OUTPUT_KINDS: Final[dict[PlanOutputType, Kind]] = {
    PlanOutputType.EVENT: Kind.EVENT,
    PlanOutputType.FEATURE: Kind.FEATURE,
    PlanOutputType.STRATEGY: Kind.STRATEGY,
}


def _reject_float(value: str) -> None:
    raise PlanRejected(f"JSON floating point values are not admitted: {value}")


def _reject_constant(value: str) -> None:
    raise PlanRejected(f"non-standard JSON constant is not admitted: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlanRejected(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _object(value: object, fields: frozenset[str], what: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PlanRejected(f"{what} must be a JSON object")
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown:
        raise PlanRejected(f"{what} has unknown field(s): {', '.join(sorted(unknown))}")
    if missing:
        raise PlanRejected(f"{what} is missing field(s): {', '.join(sorted(missing))}")
    if not all(isinstance(key, str) for key in value):
        raise PlanRejected(f"{what} keys must be strings")
    return value


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PlanRejected(f"{what} must be non-empty text without surrounding whitespace")
    return value


def _parse_input(value: object, prior: Mapping[str, PlanNode], what: str) -> PlanInput:
    if not isinstance(value, dict):
        raise PlanRejected(f"{what} must be a reference object")
    keys = frozenset(value)
    if keys == {"ref", "content_hash"}:
        raw_ref = _text(value["ref"], f"{what}.ref")
        try:
            ref = Ref.parse(raw_ref)
            if str(ref) != raw_ref:
                raise ValueError("reference is not canonical")
            return SpecInput(ref, value["content_hash"])
        except (TypeError, ValueError) as exc:
            raise PlanRejected(f"invalid {what}: {exc}") from exc
    if keys == {"node"}:
        node_id = _text(value["node"], f"{what}.node")
        if node_id not in prior:
            raise PlanRejected(f"{what} must reference an earlier, known node")
        return NodeInput(node_id)
    raise PlanRejected(f"{what} must contain exactly ref+content_hash or node")


def _input_type(item: PlanInput, prior: Mapping[str, PlanNode]) -> Kind | PlanOutputType:
    if isinstance(item, SpecInput):
        return item.ref.kind
    return prior[item.node_id].output_type


def _check_parameters(
    operator: PlanOperator, value: object, limits: PlanLimits, node_id: str
) -> FrozenMapping[str, PlanScalar]:
    if not isinstance(value, dict):
        raise PlanRejected(f"node {node_id}.parameters must be a JSON object")
    allowed = _PARAMETER_KEYS[operator]
    unknown = set(value) - allowed
    missing = allowed - set(value)
    if unknown:
        raise PlanRejected(
            f"node {node_id}.parameters has unknown field(s): {', '.join(sorted(unknown))}"
        )
    if missing:
        raise PlanRejected(
            f"node {node_id}.parameters is missing field(s): {', '.join(sorted(missing))}"
        )
    if len(value) > limits.max_parameters_per_node:
        raise PlanRejected(f"node {node_id} exceeds max_parameters_per_node")
    params: dict[str, PlanScalar] = {}
    for key, item in value.items():
        if not isinstance(key, str) or _NAME.fullmatch(key) is None:
            raise PlanRejected(f"node {node_id} parameter names must match project name syntax")
        if type(item) not in {str, int, bool}:
            raise PlanRejected(
                f"node {node_id} parameter {key!r} must be a string, integer, or boolean; "
                "implicit conversion is not performed"
            )
        params[key] = item
    if operator is PlanOperator.CONDITIONING:
        _text(params["state_value"], f"node {node_id}.parameters.state_value")
    elif operator is PlanOperator.TEMPORAL:
        window = params["window"]
        if type(window) is not int or window < 1:
            raise PlanRejected(f"node {node_id}.parameters.window must be a positive integer")
        _text(params["time_unit"], f"node {node_id}.parameters.time_unit")
    elif operator is PlanOperator.TRANSFORMATION:
        transform = _text(params["transform"], f"node {node_id}.parameters.transform")
        if transform not in _TRANSFORMS:
            raise PlanRejected(f"node {node_id} has unknown transformation {transform!r}")
        window = params["window"]
        if type(window) is not int or window < 1:
            raise PlanRejected(f"node {node_id}.parameters.window must be a positive integer")
    return FrozenMapping(params)


def _parse_node(
    raw: object,
    prior: Mapping[str, PlanNode],
    limits: PlanLimits,
    depths: dict[str, int],
) -> PlanNode:
    data = _object(raw, frozenset({"id", "operator", "inputs", "parameters"}), "node")
    node_id = _text(data["id"], "node.id")
    if _NAME.fullmatch(node_id) is None:
        raise PlanRejected("node.id must match project name syntax")
    if node_id in prior:
        raise PlanRejected(f"duplicate node id: {node_id!r}")
    try:
        operator = PlanOperator(data["operator"])
    except (TypeError, ValueError) as exc:
        raise PlanRejected(f"node {node_id} has unknown operator") from exc
    raw_inputs = data["inputs"]
    if not isinstance(raw_inputs, list):
        raise PlanRejected(f"node {node_id}.inputs must be an array")
    inputs = tuple(
        _parse_input(item, prior, f"node {node_id}.inputs[{index}]")
        for index, item in enumerate(raw_inputs)
    )
    input_kinds, _, exact_arity = _SIGNATURES[operator]
    if exact_arity is not None and len(inputs) != exact_arity:
        raise PlanRejected(
            f"node {node_id} operator {operator.value} requires {exact_arity} inputs"
        )
    if exact_arity is None and len(inputs) < 2:
        raise PlanRejected(f"node {node_id} operator {operator.value} requires at least two inputs")
    if operator is PlanOperator.ENSEMBLE:
        identities = [
            ("spec", str(item.ref), item.content_hash)
            if isinstance(item, SpecInput)
            else ("node", item.node_id)
            for item in inputs
        ]
        if len(set(identities)) != len(identities):
            raise PlanRejected(f"node {node_id} ensemble inputs must not contain duplicates")
    expected_kinds = input_kinds if exact_arity is not None else input_kinds * len(inputs)
    for index, (item, expected) in enumerate(zip(inputs, expected_kinds, strict=True)):
        actual = _input_type(item, prior)
        if isinstance(actual, PlanOutputType):
            actual = _OUTPUT_KINDS.get(actual, actual)
        if actual is not expected:
            raise PlanRejected(
                f"node {node_id}.inputs[{index}] has type {actual}; expected {expected.value}; "
                "implicit conversion is not performed"
            )
    parameters = _check_parameters(operator, data["parameters"], limits, node_id)
    input_depth = max(
        (depths[item.node_id] for item in inputs if isinstance(item, NodeInput)), default=0
    )
    depth = input_depth + 1
    if depth > limits.max_depth:
        raise PlanRejected(f"node {node_id} exceeds max_depth")
    depths[node_id] = depth
    return PlanNode(node_id, operator, inputs, parameters)


def parse_plan_json(raw: str, *, limits: PlanLimits) -> TypedPlan:
    """Parse and type-check one strict plan document using caller-supplied resource bounds.

    This operation resolves only the references and hashes written in the document; it does not
    consult a Registry and does not claim that the referenced hash exists or is authoritative.
    """
    if not isinstance(limits, PlanLimits):
        raise TypeError("limits must be explicitly supplied as PlanLimits")
    if not isinstance(raw, str):
        raise PlanRejected("plan input must be JSON text")
    try:
        encoded = raw.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PlanRejected("plan JSON must be valid UTF-8 text") from exc
    if len(encoded) > limits.max_json_bytes:
        raise PlanRejected("plan exceeds max_json_bytes")
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except PlanRejected:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise PlanRejected(f"invalid plan JSON: {exc}") from exc
    document = _object(payload, frozenset({"schema_version", "root", "nodes"}), "plan")
    if document["schema_version"] != PLAN_FORMAT_VERSION:
        raise PlanRejected(f"unsupported plan schema_version: {document['schema_version']!r}")
    root = _text(document["root"], "plan.root")
    raw_nodes = document["nodes"]
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise PlanRejected("plan.nodes must be a non-empty array")
    if len(raw_nodes) > limits.max_nodes:
        raise PlanRejected("plan exceeds max_nodes")
    nodes: list[PlanNode] = []
    prior: dict[str, PlanNode] = {}
    depths: dict[str, int] = {}
    for raw_node in raw_nodes:
        node = _parse_node(raw_node, prior, limits, depths)
        prior[node.node_id] = node
        nodes.append(node)
    if root not in prior:
        raise PlanRejected("plan.root does not name a declared node")

    reachable: set[str] = set()
    pending = [root]
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        pending.extend(
            item.node_id for item in prior[node_id].inputs if isinstance(item, NodeInput)
        )
    if len(reachable) != len(nodes):
        raise PlanRejected("plan contains node(s) unreachable from root")
    return TypedPlan(root, tuple(nodes), limits)


def compile_plan(plan: TypedPlan) -> None:
    """Fail closed: no executable implementation allowlist or lowering is registered."""
    if not isinstance(plan, TypedPlan):
        raise TypeError("plan must be a TypedPlan")
    raise PlanRefused(
        "no executable operator implementations are registered; all six DSL operators "
        "remain non-runnable"
    )

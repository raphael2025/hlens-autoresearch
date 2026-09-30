"""Closed-world Phase 7 operator plan data (ADR-0068) and its compile entry point.

This module parses and type-checks plan structure. ``TypedPlan`` itself stays plan *data*: its
``runnable`` is always ``False`` and is part of the payload, so the content hash of every "1.1.0" /
"1.2.0" plan is unchanged. Execution readiness is decided by ``compile_plan`` (ADR-0100 item 1),
which delegates to ``research.hypotheses.typed_plan_compiler``: it compiles against an explicit
Provider allowlist and returns a ``CompiledPlan`` whose ``runnable`` is ``True`` only when the
caller explicitly enabled execution (``P7ExecutionSwitch``, default OFF) and every node's lowered
definition has an allowlisted Provider; otherwise it refuses with a precise ``PlanRefused``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from core.contracts.revision import SNAPSHOT_TABLE_PATTERN
from core.domain.base import (
    NAME_PATTERN,
    SHA256_PATTERN,
    FrozenMapping,
    Kind,
    Ref,
    content_hash,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from core.contracts.universe import ResearchDatasetManifest
    from research.hypotheses.typed_plan_compiler import (
        CompiledPlan,
        OperatorImplementation,
        P7ExecutionSwitch,
    )
    from research.hypotheses.typed_plan_resolver import DirectReferenceResolution

__all__ = [
    "CROSS_SECTIONAL_TRANSFORMS",
    "PLAN_FORMAT_VERSION",
    "SUPPORTED_PLAN_FORMAT_VERSIONS",
    "UNIVERSE_REFERENCE_PREFIX",
    "UniverseReference",
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
    "parse_universe_reference",
]

#: Bumped 1.0.0 -> 1.1.0 (ADR-0082, transformation acceptance): a ``transformation`` node now
#: additionally requires an explicit ``window`` parameter (positive integer bar count). A "1.0.0"
#: transformation payload with only ``transform`` no longer parses under "1.1.0"; no other
#: operator's grammar changed. No migration is required: no ``TypedPlan.runnable`` was ever True
#: and no persisted "1.0.0" transformation payload exists (transformation was unconditionally
#: ``operator_open`` before this ADR amendment).
#:
#: Bumped 1.1.0 -> 1.2.0 (ADR-0099, time-series rank / quantile): a ``transformation`` node may
#: additionally carry ``buckets`` (integer >= 2). It is required for ``transform == "quantile"``
#: and rejected for every other transform; under 1.2.0 ``rank`` / ``quantile`` also require
#: ``window >= 2`` (the percentile rank divides by ``window - 1``). The change is additive:
#: "1.1.0" plans still parse with their original grammar, keep their original payload and content
#: hash, and their ``rank`` / ``quantile`` nodes keep their original meaning (``operator_open`` at
#: lowering time) — an old plan's meaning is never changed retroactively (ADR-0099 decision 4).
#:
#: Bumped 1.2.0 -> 1.3.0 (ADR-0100 §2, cross-sectional rank / quantile): ``transformation``
#: additionally admits the ``transform`` names ``rank_cs`` / ``quantile_cs``. A cross-sectional
#: node carries no ``window``; it requires ``universe`` (the pinned universe snapshot, written as
#: ``research_dataset:<namespace.table>@<snapshot_id>``, i.e. the ``DatasetRef`` of a
#: ``ResearchDatasetManifest``) and ``universe_hash`` (that manifest's content hash);
#: ``quantile_cs`` also requires ``buckets`` (integer >= 2). Under 1.3.0 ``window`` is therefore required per
#: transform (every time-series transform) instead of for every transformation node; time-series
#: nodes keep exactly their 1.2.0 grammar and meaning. "1.1.0" / "1.2.0" plans still parse with
#: their original grammar (``rank_cs`` / ``quantile_cs`` are unknown transforms there) and keep
#: their payload, content hash and meaning.
PLAN_FORMAT_VERSION: Final = "1.3.0"
_LEGACY_PLAN_FORMAT_VERSION: Final = "1.1.0"
_RANK_TS_PLAN_FORMAT_VERSION: Final = "1.2.0"
#: Every plan format version this parser admits. Each document is parsed with the grammar of the
#: version it declares, and a ``TypedPlan`` carries that version in its payload and hash.
SUPPORTED_PLAN_FORMAT_VERSIONS: Final = frozenset(
    {_LEGACY_PLAN_FORMAT_VERSION, _RANK_TS_PLAN_FORMAT_VERSION, PLAN_FORMAT_VERSION}
)
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
class UniverseReference:
    """The pinned universe snapshot a cross-sectional node declares (ADR-0100 §2, format 1.3.0).

    ``table`` / ``snapshot_id`` name the ``DatasetRef`` of a ``ResearchDatasetManifest`` (zone
    ``research_dataset``); ``manifest_hash`` is that manifest's content hash. The parser only checks
    the syntax; the lowering binds it to a caller-supplied manifest by hash and by dataset identity.
    """

    table: str
    snapshot_id: str
    manifest_hash: str

    def text(self) -> str:
        return f"{UNIVERSE_REFERENCE_PREFIX}{self.table}@{self.snapshot_id}"


def parse_universe_reference(universe: object, universe_hash: object) -> UniverseReference:
    """Parse the ``universe`` / ``universe_hash`` node parameters; ``ValueError`` when malformed.

    ``universe`` is ``research_dataset:<namespace.table>@<snapshot_id>``: the table matches the
    Iceberg ``namespace.table`` syntax (which contains no ``@``), so the first ``@`` after the
    prefix separates it from the non-empty snapshot ID.
    """
    if not isinstance(universe, str) or not universe.startswith(UNIVERSE_REFERENCE_PREFIX):
        raise ValueError(
            f"universe must be text of the form {UNIVERSE_REFERENCE_PREFIX}<namespace.table>@"
            "<snapshot_id>"
        )
    table, separator, snapshot_id = universe.removeprefix(UNIVERSE_REFERENCE_PREFIX).partition("@")
    if not separator or _SNAPSHOT_TABLE.fullmatch(table) is None:
        raise ValueError("universe must name an Iceberg namespace.table followed by @<snapshot_id>")
    if not snapshot_id or snapshot_id != snapshot_id.strip():
        raise ValueError("universe snapshot_id must be non-empty without surrounding whitespace")
    if not isinstance(universe_hash, str) or _HASH.fullmatch(universe_hash) is None:
        raise ValueError("universe_hash must be a lowercase SHA-256 manifest content hash")
    return UniverseReference(table=table, snapshot_id=snapshot_id, manifest_hash=universe_hash)


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
    #: The plan format version the plan was written in (ADR-0099 decision 4). It selects the
    #: grammar used to parse the plan and the lowering semantics of its nodes, and it is part of
    #: the payload and content hash, so a "1.1.0" plan keeps its original hash and meaning.
    schema_version: str = PLAN_FORMAT_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, str)
            or self.schema_version not in SUPPORTED_PLAN_FORMAT_VERSIONS
        ):
            raise ValueError(
                "TypedPlan.schema_version must be one of "
                f"{', '.join(sorted(SUPPORTED_PLAN_FORMAT_VERSIONS))}"
            )

    @property
    def runnable(self) -> bool:
        return False

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
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
    #: ADR-0099 (plan format 1.2.0): ``rank`` / ``quantile`` have accepted time-series semantics
    #: over the same trailing ``window`` (which must then be >= 2); ``quantile`` additionally
    #: requires ``buckets`` (see ``_OPTIONAL_PARAMETER_KEYS``). In a "1.1.0" plan they remain
    #: ``operator_open`` at lowering time and still supply a syntactically valid ``window``.
    PlanOperator.TRANSFORMATION: frozenset({"transform", "window"}),
    PlanOperator.ENSEMBLE: frozenset(),
    PlanOperator.NEGATION: frozenset(),
}
#: Optional node parameters admitted per plan format version, on top of ``_PARAMETER_KEYS``.
#: ADR-0099: under "1.2.0" a ``transformation`` node may carry ``buckets``; whether it is required
#: or rejected depends on the ``transform`` name (checked in ``_check_rank_parameters``).
#: ADR-0100 §2 (format 1.3.0): ``window`` becomes per transform (time-series transforms only), and
#: ``universe`` / ``universe_hash`` are admitted for the cross-sectional transforms. Which of them a
#: node must / must not carry depends on ``transform`` (``_check_transform_parameters_1_3``).
_OPTIONAL_PARAMETER_KEYS: Final[dict[str, dict[PlanOperator, frozenset[str]]]] = {
    _LEGACY_PLAN_FORMAT_VERSION: {},
    _RANK_TS_PLAN_FORMAT_VERSION: {PlanOperator.TRANSFORMATION: frozenset({"buckets"})},
    PLAN_FORMAT_VERSION: {
        PlanOperator.TRANSFORMATION: frozenset({"window", "buckets", "universe", "universe_hash"})
    },
}
#: Per-version overrides of the required keys in ``_PARAMETER_KEYS`` (ADR-0100 §2): under 1.3.0 a
#: ``transformation`` node always requires ``transform`` only; everything else is per transform.
_REQUIRED_PARAMETER_OVERRIDES: Final[dict[str, dict[PlanOperator, frozenset[str]]]] = {
    PLAN_FORMAT_VERSION: {PlanOperator.TRANSFORMATION: frozenset({"transform"})},
}
_TRANSFORMS: Final = frozenset({"standardize", "rank", "quantile", "difference", "smooth"})
#: ADR-0100 §2: cross-sectional transforms over a pinned universe snapshot (format 1.3.0 only).
CROSS_SECTIONAL_TRANSFORMS: Final = frozenset({"rank_cs", "quantile_cs"})
_TRANSFORMS_BY_PLAN_FORMAT: Final[dict[str, frozenset[str]]] = {
    _LEGACY_PLAN_FORMAT_VERSION: _TRANSFORMS,
    _RANK_TS_PLAN_FORMAT_VERSION: _TRANSFORMS,
    PLAN_FORMAT_VERSION: _TRANSFORMS | CROSS_SECTIONAL_TRANSFORMS,
}
#: Canonical text of a pinned universe snapshot: ``research_dataset:<namespace.table>@<snapshot>``,
#: the ``zone`` / ``table`` / ``snapshot_id`` of a ``ResearchDatasetManifest.dataset``.
UNIVERSE_REFERENCE_PREFIX: Final = "research_dataset:"
_SNAPSHOT_TABLE = re.compile(SNAPSHOT_TABLE_PATTERN)
#: Parameters only a cross-sectional transform may carry (format 1.3.0).
_UNIVERSE_PARAMETERS: Final = frozenset({"universe", "universe_hash"})
#: ADR-0099: the time-series percentile rank divides by ``window - 1``; quantile is built on it.
_RANK_BASED_TRANSFORMS: Final = frozenset({"rank", "quantile"})
_MIN_RANK_WINDOW: Final = 2
_MIN_BUCKETS: Final = 2
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
    operator: PlanOperator,
    value: object,
    limits: PlanLimits,
    node_id: str,
    schema_version: str,
) -> FrozenMapping[str, PlanScalar]:
    if not isinstance(value, dict):
        raise PlanRejected(f"node {node_id}.parameters must be a JSON object")
    required = _REQUIRED_PARAMETER_OVERRIDES.get(schema_version, {}).get(
        operator, _PARAMETER_KEYS[operator]
    )
    allowed = required | _OPTIONAL_PARAMETER_KEYS[schema_version].get(operator, frozenset())
    unknown = set(value) - allowed
    missing = required - set(value)
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
        if transform not in _TRANSFORMS_BY_PLAN_FORMAT[schema_version]:
            raise PlanRejected(f"node {node_id} has unknown transformation {transform!r}")
        if transform in CROSS_SECTIONAL_TRANSFORMS:
            # Only reachable under format 1.3.0 (ADR-0100 §2).
            _check_cross_sectional_parameters(params, transform, node_id)
            return FrozenMapping(params)
        if schema_version == PLAN_FORMAT_VERSION:
            # 1.3.0: a time-series transform keeps its 1.2.0 grammar exactly; ``window`` is
            # required here instead of via ``_PARAMETER_KEYS`` and the universe is foreign to it.
            if "window" not in params:
                raise PlanRejected(f"node {node_id}.parameters is missing field(s): window")
            foreign = sorted(_UNIVERSE_PARAMETERS & set(params))
            if foreign:
                raise PlanRejected(
                    f"node {node_id}.parameters.{foreign[0]} is only admitted for the "
                    "cross-sectional transforms rank_cs / quantile_cs"
                )
        window = params["window"]
        if type(window) is not int or window < 1:
            raise PlanRejected(f"node {node_id}.parameters.window must be a positive integer")
        if schema_version != _LEGACY_PLAN_FORMAT_VERSION:
            _check_rank_parameters(params, transform, window, node_id)
    return FrozenMapping(params)


def _check_cross_sectional_parameters(
    params: Mapping[str, PlanScalar], transform: str, node_id: str
) -> None:
    """ADR-0100 §2 (format 1.3.0): pinned universe required, no window, buckets on quantile_cs.

    A cross-sectional value is computed over the universe members at one bar time, so a trailing
    ``window`` has no meaning and is rejected rather than silently ignored.
    """
    if "window" in params:
        raise PlanRejected(
            f"node {node_id}.parameters.window is not admitted for the cross-sectional "
            f"transform {transform!r}"
        )
    missing = sorted(_UNIVERSE_PARAMETERS - set(params))
    if missing:
        raise PlanRejected(f"node {node_id}.parameters is missing field(s): {', '.join(missing)}")
    try:
        parse_universe_reference(params["universe"], params["universe_hash"])
    except ValueError as exc:
        raise PlanRejected(f"node {node_id}.parameters: {exc}") from exc
    if transform == "quantile_cs":
        if "buckets" not in params:
            raise PlanRejected(f"node {node_id}.parameters is missing field(s): buckets")
        buckets = params["buckets"]
        if type(buckets) is not int or buckets < _MIN_BUCKETS:
            raise PlanRejected(
                f"node {node_id}.parameters.buckets must be an integer >= {_MIN_BUCKETS}"
            )
    elif "buckets" in params:
        raise PlanRejected(
            f"node {node_id}.parameters.buckets is only admitted for transform 'quantile' / "
            "'quantile_cs'"
        )


def _check_rank_parameters(
    params: Mapping[str, PlanScalar], transform: str, window: int, node_id: str
) -> None:
    """ADR-0099 (format 1.2.0): ``buckets`` only on ``quantile``; rank-based windows >= 2."""
    if transform == "quantile":
        if "buckets" not in params:
            raise PlanRejected(f"node {node_id}.parameters is missing field(s): buckets")
        buckets = params["buckets"]
        if type(buckets) is not int or buckets < _MIN_BUCKETS:
            raise PlanRejected(
                f"node {node_id}.parameters.buckets must be an integer >= {_MIN_BUCKETS}"
            )
    elif "buckets" in params:
        raise PlanRejected(
            f"node {node_id}.parameters.buckets is only admitted for transform 'quantile'"
        )
    if transform in _RANK_BASED_TRANSFORMS and window < _MIN_RANK_WINDOW:
        raise PlanRejected(
            f"node {node_id}.parameters.window must be >= {_MIN_RANK_WINDOW} for {transform!r}"
        )


def _parse_node(
    raw: object,
    prior: Mapping[str, PlanNode],
    limits: PlanLimits,
    depths: dict[str, int],
    schema_version: str,
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
    parameters = _check_parameters(operator, data["parameters"], limits, node_id, schema_version)
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
    schema_version = document["schema_version"]
    if not isinstance(schema_version, str) or schema_version not in SUPPORTED_PLAN_FORMAT_VERSIONS:
        raise PlanRejected(f"unsupported plan schema_version: {schema_version!r}")
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
        node = _parse_node(raw_node, prior, limits, depths, schema_version)
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
    return TypedPlan(root, tuple(nodes), limits, schema_version)


def compile_plan(
    plan: TypedPlan,
    *,
    resolution: DirectReferenceResolution | None = None,
    created_at: datetime | None = None,
    allowlist: Mapping[str, OperatorImplementation] | None = None,
    switch: P7ExecutionSwitch | None = None,
    universes: Iterable[ResearchDatasetManifest] = (),
) -> CompiledPlan:
    """Compile ``plan`` against an explicit Provider allowlist, or refuse (ADR-0100 item 1).

    Default OFF: without ``switch=P7ExecutionSwitch(enabled=True)`` every plan is refused
    (``execution_disabled``), exactly as before this ADR. With the switch on, ``resolution`` (the
    plan's hash-verified direct references), an explicit timezone-aware ``created_at`` and an
    explicit ``allowlist`` (e.g. ``typed_plan_compiler.P7_OPERATOR_ALLOWLIST``) are required, and
    every node's lowered definition must map to an allowlisted Provider. ``universes`` (the
    caller-supplied pinned universe manifests) are passed to the lowering so cross-sectional nodes
    can lower; their execution is not supported by the compiled wiring, so such a plan is refused
    with ``cross_sectional_execution_unsupported``. Trial counting and admission rules are
    unchanged; see ``research.hypotheses.typed_plan_compiler``.
    """
    if not isinstance(plan, TypedPlan):
        raise TypeError("plan must be a TypedPlan")
    # Imported here: the compiler imports this module (and the lowering, which imports it too).
    from research.hypotheses.typed_plan_compiler import compile_lowered_plan

    return compile_lowered_plan(
        plan,
        resolution=resolution,
        created_at=created_at,
        allowlist=allowlist,
        switch=switch,
        universes=universes,
    )

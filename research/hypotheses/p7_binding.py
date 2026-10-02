"""Hash-bind a compiled P7 plan to the run that executes it (ADR-0103 §1).

An ``ExperimentSpec`` that runs a compiled P7 plan must carry the plan's identity, or the plan
that produced a result could be swapped after the fact. The frozen ``ReproducibilityTuple`` has no
field for it, so — like the ADR-0100 修订 2 run inputs (``research.experiments.run_inputs``) — the
record goes into the existing, hashed ``repro.params`` mapping under one reserved, namespaced key,
``P7_PLAN_KEY`` (``hlens.p7.plan@1.0.0``), whose value is the canonical JSON text of
``P7PlanRecord.payload()``. ``repro.params`` is part of ``experiment_hash``, so the record is bound
to the run; no ``core/`` contract changes. Every node output's ``ref -> content_hash`` also joins
``repro.dependency_hashes`` (``plan_dependency_hashes`` / ``with_plan_dependency_hashes``), which
is what ``plan_bindings.validate_complete_experiment_bindings`` checks.

Rules (same as the run inputs record):

- **Old runs are never backfilled or inferred** (H3 / H6): params without the key have no record
  (``recorded_p7_plan`` returns ``None``).
- The key is reserved: ``with_p7_plan`` refuses params that already carry it, and
  ``strategy_params`` is the strategy's own parameters.
- ``recorded_p7_plan`` is strict: a value that is not exactly the canonical JSON text of a valid
  record raises ``P7PlanRecordError`` (never a partial read).

``universe_manifest_hashes`` are the pinned cross-sectional universe manifest hashes the plan
names (``universe_hash`` parameter of its cross-sectional nodes), sorted and unique; ``()`` for
a plan without such nodes. A plan is recorded only from a runnable ``CompiledPlan``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from core.domain.base import SHA256_PATTERN, Kind, Ref, canonical_json
from core.domain.research import ExperimentSpec, Hypothesis, ReproducibilityTuple
from research.hypotheses.typed_plan import SUPPORTED_PLAN_FORMAT_VERSIONS, TypedPlan

if TYPE_CHECKING:
    from research.hypotheses.typed_plan_compiler import CompiledPlan

__all__ = [
    "P7_PLAN_CONDITION_PREFIX",
    "P7_PLAN_FORMAT",
    "P7_PLAN_KEY",
    "P7PlanNodeRecord",
    "P7PlanRecord",
    "P7PlanRecordError",
    "bind_experiment",
    "hypothesis_p7_plan_hash",
    "p7_plan_condition",
    "plan_dependency_hashes",
    "recorded_p7_plan",
    "strategy_params",
    "universe_manifest_hashes",
    "with_p7_plan",
    "with_plan_dependency_hashes",
]

#: Identity and version of the recorded payload.
P7_PLAN_FORMAT: Final = "hlens.p7.plan@1.0.0"
#: The ``repro.params`` key that carries it (the format id itself: namespaced, never a strategy
#: parameter name).
P7_PLAN_KEY: Final = P7_PLAN_FORMAT

#: The Hypothesis condition that names the plan (``p7_plan = <plan_hash>``), alongside
#: ``strategy = <root ref>`` (ADR-0103 §1); the ``trial_point`` that resolves it is P7-LOOP's.
P7_PLAN_CONDITION_PREFIX: Final = "p7_plan = "

_HASH: Final = re.compile(SHA256_PATTERN)
_RECORD_KEYS: Final = frozenset(
    {
        "plan_hash",
        "plan_format",
        "root",
        "compiler",
        "allowlist_hash",
        "universe_manifest_hashes",
        "nodes",
    }
)
_NODE_KEYS: Final = frozenset(
    {"node_id", "definition", "spec_ref", "spec_hash", "provider_key", "implementation_hash"}
)
_OUTPUT_KINDS: Final = frozenset({Kind.FEATURE, Kind.EVENT, Kind.STRATEGY})

#: A ``repro.params`` value (``ReproducibilityTuple.params``).
type RunParam = str | int | float | bool


class P7PlanRecordError(ValueError):
    """A ``hlens.p7.plan@1.0.0`` record (or the params / dependencies it goes into) is invalid."""


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise P7PlanRecordError(f"{what} must be non-empty text without surrounding whitespace")
    return value


def _hash(value: object, what: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise P7PlanRecordError(f"{what} must be a lowercase SHA-256 hash")
    return value


def _versioned(value: object, what: str) -> str:
    text = _text(value, what)
    if text.count("@") != 1 or not all(text.split("@")):
        raise P7PlanRecordError(f"{what} must be exact name@version text")
    return text


def p7_plan_condition(plan_hash: str) -> str:
    """The Hypothesis condition text that names the plan."""
    return f"{P7_PLAN_CONDITION_PREFIX}{_hash(plan_hash, 'plan_hash')}"


def hypothesis_p7_plan_hash(hypothesis: Hypothesis) -> str | None:
    """The plan hash a Hypothesis names (``None``: no ``p7_plan`` condition). More than one
    ``p7_plan`` condition, or a malformed hash, is refused."""
    found = [item for item in hypothesis.conditions if item.startswith(P7_PLAN_CONDITION_PREFIX)]
    if not found:
        return None
    if len(found) > 1:
        raise P7PlanRecordError("a Hypothesis names at most one p7_plan condition")
    return _hash(found[0][len(P7_PLAN_CONDITION_PREFIX) :], "p7_plan condition")


def universe_manifest_hashes(plan: TypedPlan) -> tuple[str, ...]:
    """The pinned universe manifest hashes the plan's nodes name, sorted and unique."""
    found = {
        node.parameters["universe_hash"]
        for node in plan.nodes
        if "universe_hash" in node.parameters
    }
    return tuple(sorted(_hash(item, "universe_hash") for item in found))


@dataclass(frozen=True, slots=True)
class P7PlanNodeRecord:
    """One compiled node as recorded: its lowered spec and the Provider that serves it."""

    node_id: str
    definition: str
    spec_ref: str
    spec_hash: str
    provider_key: str
    implementation_hash: str

    def __post_init__(self) -> None:
        _text(self.node_id, "node_id")
        _versioned(self.definition, "definition")
        _versioned(self.provider_key, "provider_key")
        _hash(self.spec_hash, "spec_hash")
        _hash(self.implementation_hash, "implementation_hash")
        spec_ref = _text(self.spec_ref, "spec_ref")
        try:
            parsed = Ref.parse(spec_ref)
        except ValueError as exc:
            raise P7PlanRecordError(f"spec_ref {spec_ref!r} is not a ref: {exc}") from exc
        if str(parsed) != spec_ref or parsed.kind not in _OUTPUT_KINDS:
            raise P7PlanRecordError(f"spec_ref {spec_ref!r} is not a feature/event/strategy ref")

    def payload(self) -> dict[str, str]:
        return {
            "node_id": self.node_id,
            "definition": self.definition,
            "spec_ref": self.spec_ref,
            "spec_hash": self.spec_hash,
            "provider_key": self.provider_key,
            "implementation_hash": self.implementation_hash,
        }


@dataclass(frozen=True, slots=True)
class P7PlanRecord:
    """The identity of a compiled P7 plan as an experiment records it (module docs)."""

    plan_hash: str
    plan_format: str
    root: str
    compiler: str
    allowlist_hash: str
    universe_manifest_hashes: tuple[str, ...]
    nodes: tuple[P7PlanNodeRecord, ...]

    def __post_init__(self) -> None:
        _hash(self.plan_hash, "plan_hash")
        if self.plan_format not in SUPPORTED_PLAN_FORMAT_VERSIONS:
            raise P7PlanRecordError(f"unsupported plan_format {self.plan_format!r}")
        _text(self.root, "root")
        _versioned(self.compiler, "compiler")
        _hash(self.allowlist_hash, "allowlist_hash")
        universes = self.universe_manifest_hashes
        if not isinstance(universes, tuple):
            raise P7PlanRecordError("universe_manifest_hashes must be a tuple")
        for item in universes:
            _hash(item, "universe_manifest_hashes item")
        if list(universes) != sorted(set(universes)):
            raise P7PlanRecordError("universe_manifest_hashes must be sorted and unique")
        if not isinstance(self.nodes, tuple) or not self.nodes:
            raise P7PlanRecordError("nodes must be a non-empty tuple")
        if any(type(node) is not P7PlanNodeRecord for node in self.nodes):
            raise P7PlanRecordError("nodes must contain only P7PlanNodeRecord values")
        node_ids = [node.node_id for node in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise P7PlanRecordError("node ids repeat")
        refs = [node.spec_ref for node in self.nodes]
        if len(set(refs)) != len(refs):
            raise P7PlanRecordError("spec refs repeat")
        if self.root not in node_ids:
            raise P7PlanRecordError(f"root {self.root!r} is not one of the recorded nodes")

    @classmethod
    def from_compiled(cls, compiled: CompiledPlan) -> P7PlanRecord:
        """The record of a runnable compiled plan (anything else is refused)."""
        # Imported here: the compiler module is heavy and this module stays importable alone.
        from research.hypotheses.typed_plan_compiler import CompiledPlan

        if not isinstance(compiled, CompiledPlan):
            raise P7PlanRecordError("compiled must be a CompiledPlan")
        if not compiled.runnable:
            raise P7PlanRecordError("only a runnable compiled plan can be recorded")
        return cls(
            plan_hash=compiled.plan_hash,
            plan_format=compiled.plan.schema_version,
            root=compiled.plan.root,
            compiler=compiled.compiler,
            allowlist_hash=compiled.allowlist_hash,
            universe_manifest_hashes=universe_manifest_hashes(compiled.plan),
            nodes=tuple(
                P7PlanNodeRecord(
                    node_id=node.node_id,
                    definition=node.definition,
                    spec_ref=str(node.spec.ref),
                    spec_hash=node.spec.content_hash(),
                    provider_key=node.implementation.provider_key,
                    implementation_hash=node.implementation.implementation_hash,
                )
                for node in compiled.nodes
            ),
        )

    def payload(self) -> dict[str, Any]:
        return {
            "plan_hash": self.plan_hash,
            "plan_format": self.plan_format,
            "root": self.root,
            "compiler": self.compiler,
            "allowlist_hash": self.allowlist_hash,
            "universe_manifest_hashes": list(self.universe_manifest_hashes),
            "nodes": [node.payload() for node in self.nodes],
        }

    def text(self) -> str:
        """The recorded value: the canonical JSON text of ``payload()``."""
        try:
            return canonical_json(self.payload())
        except (TypeError, ValueError) as exc:
            raise P7PlanRecordError(f"the plan record is not canonical JSON: {exc}") from exc

    @classmethod
    def from_payload(cls, payload: Any) -> P7PlanRecord:
        """Strict inverse of ``payload()`` (exact key sets, exact JSON types)."""
        if not isinstance(payload, dict) or set(payload) != _RECORD_KEYS:
            raise P7PlanRecordError("not a hlens.p7.plan payload")
        universes, nodes = payload["universe_manifest_hashes"], payload["nodes"]
        if not isinstance(universes, list) or not isinstance(nodes, list):
            raise P7PlanRecordError("universe_manifest_hashes and nodes must be arrays")
        parsed_nodes: list[P7PlanNodeRecord] = []
        for index, raw in enumerate(nodes):
            if not isinstance(raw, dict) or set(raw) != _NODE_KEYS:
                raise P7PlanRecordError(f"nodes[{index}] does not have exactly its fields")
            parsed_nodes.append(P7PlanNodeRecord(**raw))
        return cls(
            plan_hash=payload["plan_hash"],
            plan_format=payload["plan_format"],
            root=payload["root"],
            compiler=payload["compiler"],
            allowlist_hash=payload["allowlist_hash"],
            universe_manifest_hashes=tuple(universes),
            nodes=tuple(parsed_nodes),
        )


def with_p7_plan(params: Mapping[str, RunParam], record: P7PlanRecord) -> dict[str, RunParam]:
    """``params`` plus the record under ``P7_PLAN_KEY`` (refused when the key is taken)."""
    if P7_PLAN_KEY in params:
        raise P7PlanRecordError(f"{P7_PLAN_KEY} is reserved for the P7 plan record")
    if type(record) is not P7PlanRecord:
        raise P7PlanRecordError("record must be a P7PlanRecord")
    return {**dict(params), P7_PLAN_KEY: record.text()}


def strategy_params(params: Mapping[str, RunParam]) -> dict[str, RunParam]:
    """The strategy's own parameters: ``params`` without the P7 plan record."""
    return {key: value for key, value in params.items() if key != P7_PLAN_KEY}


def recorded_p7_plan(params: Mapping[str, RunParam]) -> P7PlanRecord | None:
    """The record a run carries in ``repro.params`` (``None``: the run records none — an old run,
    never backfilled). A value that is not exactly the canonical text of a valid record raises
    ``P7PlanRecordError``."""
    if P7_PLAN_KEY not in params:
        return None
    text = params[P7_PLAN_KEY]
    if not isinstance(text, str):
        raise P7PlanRecordError(f"{P7_PLAN_KEY} is not a JSON text")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise P7PlanRecordError(f"{P7_PLAN_KEY} is not JSON: {exc}") from exc
    try:
        record = P7PlanRecord.from_payload(payload)
    except TypeError as exc:
        raise P7PlanRecordError(f"{P7_PLAN_KEY} has values of the wrong type: {exc}") from exc
    if record.text() != text:
        raise P7PlanRecordError(f"{P7_PLAN_KEY} is not in its canonical JSON form")
    return record


def plan_dependency_hashes(record: P7PlanRecord) -> dict[str, str]:
    """Every node output of the plan, ``ref -> content_hash`` (plan order)."""
    if type(record) is not P7PlanRecord:
        raise P7PlanRecordError("record must be a P7PlanRecord")
    return {node.spec_ref: node.spec_hash for node in record.nodes}


def with_plan_dependency_hashes(
    dependency_hashes: Mapping[str, str], record: P7PlanRecord
) -> dict[str, str]:
    """``dependency_hashes`` plus every node output of the plan. A ref already present with a
    different hash is refused (the same ref with the same hash is idempotent)."""
    merged = dict(dependency_hashes)
    for ref, digest in plan_dependency_hashes(record).items():
        existing = merged.get(ref)
        if existing is not None and existing != digest:
            raise P7PlanRecordError(
                f"dependency {ref} is already bound to {existing}, the plan output is {digest}"
            )
        merged[ref] = digest
    return merged


def _rebuilt[T: (ReproducibilityTuple, ExperimentSpec)](value: T, **changes: Any) -> T:
    """``value`` with ``changes``, re-validated by its own constructor (never ``model_copy``)."""
    fields = {name: getattr(value, name) for name in type(value).model_fields}
    return type(value)(**{**fields, **changes})


def bind_experiment(experiment: ExperimentSpec, record: P7PlanRecord) -> ExperimentSpec:
    """``experiment`` bound to the plan: the record under ``P7_PLAN_KEY`` in ``repro.params``
    and every node output in ``repro.dependency_hashes`` (``with_p7_plan`` /
    ``with_plan_dependency_hashes`` refusals apply). The result is re-validated, so its
    ``experiment_hash`` covers the binding."""
    if type(experiment) is not ExperimentSpec:
        raise P7PlanRecordError("experiment must be an exact ExperimentSpec")
    repro = experiment.repro
    bound = _rebuilt(
        repro,
        params=with_p7_plan(repro.params, record),
        dependency_hashes=with_plan_dependency_hashes(repro.dependency_hashes, record),
    )
    return _rebuilt(experiment, repro=bound)

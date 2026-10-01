"""Compile a P7 typed plan against an explicit Provider allowlist (ADR-0068 §1.2, ADR-0100 item 1).

``compile_lowered_plan`` (exposed as ``research.hypotheses.typed_plan.compile_plan``) turns a
strictly parsed ``TypedPlan`` into a ``CompiledPlan`` only when **both** hold:

1. the caller explicitly enabled execution (``P7ExecutionSwitch(enabled=True)``; the default and
   every missing / malformed switch is OFF), and
2. every node's lowered spec names a definition (``definition@version``) that the caller's
   explicit allowlist maps to an ``OperatorImplementation`` for that operator, output kind and
   Provider key.

Otherwise it raises ``PlanCompileRefused`` (a ``PlanRefused``) with a precise code
(``execution_disabled``, ``no_allowlist``, ``missing_lowering_evidence``,
``provider_not_registered``, ``provider_identity_mismatch``, ...). A refusal of the lowering itself
propagates unchanged as ``OperatorLoweringRefused``. Nothing is partially compiled.

Cross-sectional ``rank_cs`` / ``quantile_cs`` nodes (plan format 1.3.0, ADR-0100 §2) lower when the
caller passes the pinned universe manifests (``universes=``, handed to the lowering as evidence),
and may be compiled only as the plan root. Their Providers consume an explicit
``CrossSectionalRequest`` and return a ``CrossSectionalResult``; ``build_providers`` constructs
these separately from single-series ``FeatureProvider`` nodes using the same caller-supplied pinned
universe manifests. The returned cross-sectional Provider is invoked with its dedicated request
shape; it is never adapted to a single-series request. A cross-sectional result consumed by another
plan node is refused because no such adapter semantics are approved. The Research Loop still
accepts only strategy roots, so a cross-sectional feature result is not itself a loop candidate.

Compilation is pure and deterministic: it re-runs the pure lowering
(``typed_plan_lowering.lower_typed_plan``) on the caller's hash-verified direct-reference
resolution and explicit ``created_at``, then checks the allowlist. It does not read a clock, does
not consult a registry, does not write admission evidence or TrialLedger entries, and does not
change trial counting or admission rules: ``TypedPlan.runnable`` and the plan payload / hash are
unchanged (a "1.1.0" or "1.2.0" plan keeps its hash). Runnability lives on ``CompiledPlan``.

``CompiledPlan.build_providers`` wires the allowlisted Provider classes for every node, in plan
order, with explicitly supplied Providers for the external inputs; ``operator_evidence`` /
``provider_evidence`` / ``compiler_evidence`` give ADR-0073 PREPARE evidence items. The Research
Loop receives a compiled plan only through ``research.loop.p7_plan`` (existing ``LoopWiring.
strategies`` extension point), guarded by the same switch.

``P7_OPERATOR_ALLOWLIST`` is the reviewed default allowlist of every accepted P7 definition; the
caller must still pass it (or another allowlist) explicitly.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from core.contracts.feature import FeatureProvider
from core.contracts.strategy import StrategyProvider
from core.contracts.universe import ResearchDatasetManifest
from core.domain.base import Kind, VersionedSpec, content_hash
from core.domain.specs import EventSpec, FeatureSpec, InstrumentType, StrategySpec
from plugins.events.p7_temporal import P7TemporalSequenceProvider
from plugins.features.p7_cross_sectional import (
    QUANTILE_CS_DEFINITION,
    RANK_CS_DEFINITION,
    P7QuantileCsProvider,
    P7RankCsProvider,
)
from plugins.features.p7_operators import (
    P7DifferenceProvider,
    P7InteractionProductProvider,
    P7QuantileTsProvider,
    P7RankTsProvider,
    P7SmoothSmaProvider,
    P7StandardizeProvider,
    UpstreamFeature,
)
from research.hypotheses.typed_plan import (
    CROSS_SECTIONAL_TRANSFORMS,
    NodeInput,
    PlanOperator,
    PlanRefused,
    TypedPlan,
)
from research.hypotheses.typed_plan_audit import PlanAdmissionEvidence
from research.hypotheses.typed_plan_lowering import lower_typed_plan
from research.hypotheses.typed_plan_resolver import DirectReferenceResolution
from research.strategies.composite import ResolvedStrategy
from research.strategies.p7_compositions import (
    P7ConditionedStrategyProvider,
    P7EnsembleStrategyProvider,
    P7NegatedStrategyProvider,
)

__all__ = [
    "COMPILER_IDENTITY",
    "P7_EXECUTION_CONFIG_KEY",
    "P7_OPERATOR_ALLOWLIST",
    "CompiledNode",
    "CompiledPlan",
    "OperatorImplementation",
    "P7ExecutionSwitch",
    "PlanCompileRefused",
    "compile_lowered_plan",
]

#: Compiler identity recorded in evidence; bump on any change of compile semantics.
COMPILER_IDENTITY: Final = "hlens.p7.typed_plan_compiler@1.1.0"
#: The configuration key ``P7ExecutionSwitch.from_config`` reads.
P7_EXECUTION_CONFIG_KEY: Final = "p7_operator_execution"

_SPEC_CLASS_BY_KIND: Final[dict[Kind, type[VersionedSpec]]] = {
    Kind.FEATURE: FeatureSpec,
    Kind.EVENT: EventSpec,
    Kind.STRATEGY: StrategySpec,
}


class PlanCompileRefused(PlanRefused):
    """The plan cannot be compiled into a runnable plan; ``code`` names the precise reason."""

    def __init__(self, code: str, where: str, detail: str) -> None:
        self.code = code
        self.where = where
        super().__init__(f"{code}: {where}: {detail}")


@dataclass(frozen=True, slots=True)
class P7ExecutionSwitch:
    """The single default-off switch for P7 operator execution (ADR-0100 item 1).

    ``enabled`` must be exactly ``True`` to enable; the default is ``False``. The same switch
    guards compilation and the Research Loop wiring.
    """

    enabled: bool = False

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("P7ExecutionSwitch.enabled must be a bool")

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> P7ExecutionSwitch:
        """Read ``p7_operator_execution`` from a config mapping: absent -> OFF; only ``True``
        enables; any non-bool value is refused rather than coerced."""
        value = config.get(P7_EXECUTION_CONFIG_KEY, False)
        if type(value) is not bool:
            raise ValueError(f"{P7_EXECUTION_CONFIG_KEY} must be a bool, got {value!r}")
        return cls(enabled=value)


@functools.cache
def _module_source_hash(module_name: str) -> str:
    module = sys.modules.get(module_name)
    if module is None:
        raise PlanCompileRefused(
            "implementation_hash_unavailable", module_name, "module is not loaded"
        )
    try:
        source = inspect.getsource(module)
    except (OSError, TypeError) as exc:
        raise PlanCompileRefused(
            "implementation_hash_unavailable", module_name, f"source unreadable: {exc}"
        ) from exc
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _module_hash(provider_class: object) -> str:
    """SHA-256 of the source of the module that defines ``provider_class`` (computed lazily)."""
    return _module_source_hash(str(getattr(provider_class, "__module__", "")))


@dataclass(frozen=True, slots=True)
class OperatorImplementation:
    """One allowlisted, reviewed Provider for one lowered definition (ADR-0068 §3.1).

    ``provider_class`` is called with the node spec and its explicit wiring (see
    ``CompiledPlan.build_providers``); it is never resolved from a string.
    """

    definition: str
    operator: PlanOperator
    output_kind: Kind
    provider_key: str
    provider_class: Callable[..., Any]

    def __post_init__(self) -> None:
        if not isinstance(self.operator, PlanOperator):
            raise ValueError("operator must be a PlanOperator")
        if self.output_kind not in _SPEC_CLASS_BY_KIND:
            raise ValueError("output_kind must be feature, event or strategy")
        for name in ("definition", "provider_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or value.count("@") != 1:
                raise ValueError(f"{name} must be exact name@version text")
        declared = getattr(self.provider_class, "plugin_key", None)
        if not callable(declared) or declared() != self.provider_key:
            raise ValueError(
                f"{self.provider_class!r} does not declare provider key {self.provider_key}"
            )
        if getattr(self.provider_class, "DEFINITION", None) != self.definition:
            raise ValueError(
                f"{self.provider_class!r} does not declare definition {self.definition}"
            )

    @property
    def implementation_hash(self) -> str:
        """SHA-256 of the defining module's source: the reviewed implementation content."""
        return _module_hash(self.provider_class)

    def payload(self) -> dict[str, str]:
        cls = self.provider_class
        module = getattr(cls, "__module__", "?")
        name = getattr(cls, "__qualname__", repr(cls))
        return {
            "definition": self.definition,
            "operator": self.operator.value,
            "output_kind": self.output_kind.value,
            "provider_key": self.provider_key,
            "provider_class": f"{module}.{name}",
            "implementation_hash": self.implementation_hash,
        }


def _implementation(
    cls: Any, operator: PlanOperator, output_kind: Kind
) -> tuple[str, OperatorImplementation]:
    definition = str(cls.DEFINITION)
    return definition, OperatorImplementation(
        definition=definition,
        operator=operator,
        output_kind=output_kind,
        provider_key=cls.plugin_key(),
        provider_class=cls,
    )


#: The reviewed allowlist: every accepted P7 definition -> its execution Provider.
P7_OPERATOR_ALLOWLIST: Final[Mapping[str, OperatorImplementation]] = MappingProxyType(
    dict(
        [
            _implementation(P7StandardizeProvider, PlanOperator.TRANSFORMATION, Kind.FEATURE),
            _implementation(P7DifferenceProvider, PlanOperator.TRANSFORMATION, Kind.FEATURE),
            _implementation(P7SmoothSmaProvider, PlanOperator.TRANSFORMATION, Kind.FEATURE),
            _implementation(P7RankTsProvider, PlanOperator.TRANSFORMATION, Kind.FEATURE),
            _implementation(P7QuantileTsProvider, PlanOperator.TRANSFORMATION, Kind.FEATURE),
            _implementation(P7RankCsProvider, PlanOperator.TRANSFORMATION, Kind.FEATURE),
            _implementation(P7QuantileCsProvider, PlanOperator.TRANSFORMATION, Kind.FEATURE),
            _implementation(P7InteractionProductProvider, PlanOperator.INTERACTION, Kind.FEATURE),
            _implementation(P7TemporalSequenceProvider, PlanOperator.TEMPORAL, Kind.EVENT),
            _implementation(
                P7ConditionedStrategyProvider, PlanOperator.CONDITIONING, Kind.STRATEGY
            ),
            _implementation(P7EnsembleStrategyProvider, PlanOperator.ENSEMBLE, Kind.STRATEGY),
            _implementation(P7NegatedStrategyProvider, PlanOperator.NEGATION, Kind.STRATEGY),
        ]
    )
)


def _declared_identity(spec: VersionedSpec) -> tuple[object, object]:
    """``(definition, provider)`` as written by the lowering into the spec."""
    if type(spec) is FeatureSpec:
        return spec.definition, spec.params.get("provider")
    if type(spec) is EventSpec:
        try:
            trigger = json.loads(spec.trigger)
        except json.JSONDecodeError:
            return None, None
        if not isinstance(trigger, dict):
            return None, None
        return trigger.get("definition"), trigger.get("provider")
    if type(spec) is StrategySpec:
        return spec.params.get("definition"), spec.params.get("provider")
    return None, None


@dataclass(frozen=True, slots=True)
class CompiledNode:
    """One compiled node: its lowered spec, resolved inputs (AST order) and implementation."""

    node_id: str
    operator: PlanOperator
    definition: str
    spec: VersionedSpec
    #: Input specs in AST order: resolved direct references or earlier nodes' lowered specs.
    input_specs: tuple[VersionedSpec, ...]
    #: For each input, the earlier node it comes from, or ``None`` for an external reference.
    input_nodes: tuple[str | None, ...]
    implementation: OperatorImplementation


#: Private seal set only by ``compile_lowered_plan`` on the ``CompiledPlan`` it returns.
_COMPILE_SEAL: Final = object()


def _node_matches(node: CompiledNode, plan_node: object) -> bool:
    """Re-verify one compiled node against its plan node and its implementation."""
    implementation = node.implementation
    if type(implementation) is not OperatorImplementation:
        return False
    return (
        getattr(plan_node, "node_id", None) == node.node_id
        and getattr(plan_node, "operator", None) is node.operator
        and implementation.operator is node.operator
        and implementation.definition == node.definition
        and type(node.spec) is _SPEC_CLASS_BY_KIND[implementation.output_kind]
        and _declared_identity(node.spec) == (node.definition, implementation.provider_key)
        and getattr(implementation.provider_class, "DEFINITION", None) == node.definition
    )


@dataclass(frozen=True, slots=True)
class CompiledPlan:
    """A plan compiled against an explicit allowlist with execution explicitly enabled.

    Only ``compile_lowered_plan`` produces a runnable instance: it seals the value it returns with
    a private token. An instance constructed (or ``dataclasses.replace``-d) anywhere else carries
    no seal and is never runnable, and ``runnable`` also re-verifies every node against its plan
    node and its implementation.
    """

    plan: TypedPlan
    plan_hash: str
    nodes: tuple[CompiledNode, ...]
    execution_enabled: bool
    allowlist_hash: str
    compiler: str = COMPILER_IDENTITY
    #: Set only by ``compile_lowered_plan`` (not an init field: ``replace`` drops it).
    _seal: object = field(default=None, init=False, repr=False, compare=False)

    @property
    def runnable(self) -> bool:
        """True only for a compiled (sealed) plan with execution enabled and one verified
        implementation for every plan node."""
        return (
            self._seal is _COMPILE_SEAL
            and self.execution_enabled is True
            and self.compiler == COMPILER_IDENTITY
            and self.plan_hash == self.plan.content_hash()
            and len(self.nodes) == len(self.plan.nodes)
            and all(
                _node_matches(node, plan_node)
                for node, plan_node in zip(self.nodes, self.plan.nodes, strict=True)
            )
        )

    @property
    def root(self) -> CompiledNode:
        return next(node for node in self.nodes if node.node_id == self.plan.root)

    def specs_by_node(self) -> dict[str, VersionedSpec]:
        """The lowered node map (input to ``plan_bindings.produce_lowered_output_bindings``)."""
        return {node.node_id: node.spec for node in self.nodes}

    # ------------------------------------------------------------------ ADR-0073 evidence

    def compiler_evidence(self) -> PlanAdmissionEvidence:
        return PlanAdmissionEvidence.from_data(
            {
                "identity": self.compiler,
                "value": {
                    "allowlist_hash": self.allowlist_hash,
                    "execution_enabled": self.execution_enabled,
                    "plan_hash": self.plan_hash,
                },
            }
        )

    def operator_evidence(self) -> tuple[PlanAdmissionEvidence, ...]:
        return tuple(
            PlanAdmissionEvidence.from_data(
                {
                    "identity": {"node_id": node.node_id, "definition": node.definition},
                    "value": {
                        "operator": node.operator.value,
                        "spec_ref": str(node.spec.ref),
                        "spec_hash": node.spec.content_hash(),
                        "provider_key": node.implementation.provider_key,
                        "implementation_hash": node.implementation.implementation_hash,
                    },
                }
            )
            for node in self.nodes
        )

    def provider_evidence(self) -> tuple[PlanAdmissionEvidence, ...]:
        seen: dict[str, OperatorImplementation] = {}
        for node in self.nodes:
            seen.setdefault(node.implementation.provider_key, node.implementation)
        return tuple(
            PlanAdmissionEvidence.from_data({"identity": key, "value": item.payload()})
            for key, item in seen.items()
        )

    # ------------------------------------------------------------------ provider wiring

    def build_providers(
        self,
        *,
        feature_providers: Mapping[str, FeatureProvider] | None = None,
        strategy_providers: Mapping[str, StrategyProvider] | None = None,
        bar_durations: Mapping[str, timedelta] | None = None,
        instrument_type: InstrumentType | None = None,
        universes: Iterable[ResearchDatasetManifest] = (),
    ) -> dict[str, Any]:
        """Instantiate every node's allowlisted Provider, in plan order.

        External inputs need an explicit Provider keyed by ``str(ref)``: ``feature_providers``
        for feature inputs, ``strategy_providers`` for strategy inputs (state inputs need none —
        the state gate reads state observations from the strategy request). ``bar_durations``
        (``str(bar_spec)`` -> duration) is required by ``temporal`` nodes and ``instrument_type``
        by strategy nodes; nothing is defaulted. Earlier nodes feed later ones. Any missing piece
        refuses the whole wiring (``PlanCompileRefused``). Cross-sectional Providers also require
        the exact pinned manifests supplied when compiling the plan; pass them here as well so
        each Provider can verify the lowered spec's universe binding.
        """
        if not self.runnable:
            raise PlanCompileRefused(
                "not_runnable", self.plan.root, "the compiled plan is not runnable"
            )
        features = dict(feature_providers or {})
        strategies = dict(strategy_providers or {})
        pinned_universes = tuple(universes)
        built: dict[str, Any] = {}
        for node in self.nodes:
            try:
                built[node.node_id] = self._build(
                    node,
                    built,
                    features,
                    strategies,
                    bar_durations or {},
                    instrument_type,
                    pinned_universes,
                )
            except PlanCompileRefused:
                raise
            except (TypeError, ValueError) as exc:
                raise PlanCompileRefused(
                    "provider_construction_failed", node.node_id, str(exc)
                ) from exc
        return built

    @staticmethod
    def _input_provider(
        node: CompiledNode,
        index: int,
        built: Mapping[str, Any],
        external: Mapping[str, Any],
    ) -> Any:
        source = node.input_nodes[index]
        spec = node.input_specs[index]
        provider = built.get(source) if source is not None else external.get(str(spec.ref))
        if provider is None:
            raise PlanCompileRefused(
                "missing_input_provider",
                node.node_id,
                f"no Provider was supplied for input {index} ({spec.ref})",
            )
        return provider

    def _build(
        self,
        node: CompiledNode,
        built: Mapping[str, Any],
        features: Mapping[str, FeatureProvider],
        strategies: Mapping[str, StrategyProvider],
        bar_durations: Mapping[str, timedelta],
        instrument_type: InstrumentType | None,
        universes: tuple[ResearchDatasetManifest, ...],
    ) -> Any:
        factory = node.implementation.provider_class
        kind = node.implementation.output_kind
        if kind is Kind.FEATURE:
            if node.definition in {RANK_CS_DEFINITION, QUANTILE_CS_DEFINITION}:
                try:
                    return factory((node.spec,), universes=universes)
                except (TypeError, ValueError) as exc:
                    raise PlanCompileRefused(
                        "cross_sectional_universe_unbound",
                        node.node_id,
                        f"the pinned universe manifest is missing or mismatched: {exc}",
                    ) from exc
            upstream: dict[str, UpstreamFeature] = {}
            for index, spec in enumerate(node.input_specs):
                if type(spec) is not FeatureSpec:
                    raise PlanCompileRefused(
                        "wrong_input_spec", node.node_id, f"input {index} is not a FeatureSpec"
                    )
                upstream[str(spec.ref)] = UpstreamFeature(
                    spec, self._input_provider(node, index, built, features)
                )
            return factory((node.spec,), upstream=upstream)
        if kind is Kind.EVENT:
            return factory(
                (node.spec,),
                upstream={str(spec.ref): spec for spec in node.input_specs},
                bar_durations=bar_durations,
            )
        if instrument_type is None:
            raise PlanCompileRefused(
                "missing_instrument_type",
                node.node_id,
                "strategy nodes need an explicit instrument_type (spot negation fails closed)",
            )
        table: dict[str, ResolvedStrategy] = {}
        for index, spec in enumerate(node.input_specs):
            if type(spec) is not StrategySpec:
                continue  # the conditioning state input: read from the strategy request
            table[str(spec.ref)] = ResolvedStrategy(
                spec, self._input_provider(node, index, built, strategies)
            )
        return factory((node.spec,), resolution=table, instrument_type=instrument_type)


def _check_allowlist(allowlist: object) -> dict[str, OperatorImplementation]:
    if allowlist is None:
        raise PlanCompileRefused(
            "no_allowlist", "allowlist", "an explicit Provider allowlist must be supplied"
        )
    if not isinstance(allowlist, Mapping):
        raise PlanCompileRefused("invalid_allowlist", "allowlist", "must be a mapping")
    checked: dict[str, OperatorImplementation] = {}
    for key, item in allowlist.items():
        if not isinstance(item, OperatorImplementation) or key != item.definition:
            raise PlanCompileRefused(
                "invalid_allowlist",
                f"allowlist[{key!r}]",
                "entries must be OperatorImplementation values keyed by their definition",
            )
        checked[key] = item
    return checked


def compile_lowered_plan(
    plan: TypedPlan,
    *,
    resolution: DirectReferenceResolution | None = None,
    created_at: datetime | None = None,
    allowlist: Mapping[str, OperatorImplementation] | None = None,
    switch: P7ExecutionSwitch | None = None,
    universes: Iterable[ResearchDatasetManifest] = (),
) -> CompiledPlan:
    """Compile ``plan`` or refuse with a precise reason (module docstring).

    ``universes`` are the caller-supplied pinned universe manifests, passed to the lowering.
    """
    if type(plan) is not TypedPlan:
        raise TypeError("plan must be an exact TypedPlan")
    if switch is None or type(switch) is not P7ExecutionSwitch or switch.enabled is not True:
        raise PlanCompileRefused(
            "execution_disabled",
            plan.root,
            "P7 operator execution is off by default; pass P7ExecutionSwitch(enabled=True) to "
            "compile a runnable plan",
        )
    checked = _check_allowlist(allowlist)
    if resolution is None or created_at is None:
        raise PlanCompileRefused(
            "missing_lowering_evidence",
            plan.root,
            "compilation needs the plan's DirectReferenceResolution and an explicit created_at",
        )
    lowered = lower_typed_plan(
        plan, resolution=resolution, created_at=created_at, universes=universes
    )
    direct = {(item.node_id, item.input_index): item.spec for item in resolution.inputs}

    nodes: list[CompiledNode] = []
    cross_sectional_nodes = {
        node.node_id
        for node in plan.nodes
        if node.operator is PlanOperator.TRANSFORMATION
        and node.parameters.get("transform") in CROSS_SECTIONAL_TRANSFORMS
    }
    for node in plan.nodes:
        if any(
            isinstance(item, NodeInput) and item.node_id in cross_sectional_nodes
            for item in node.inputs
        ):
            raise PlanCompileRefused(
                "cross_sectional_output_consumption_unsupported",
                node.node_id,
                "cross-sectional outputs require a dedicated batch consumer and cannot be wired "
                "as a single-series plan input",
            )

    for node in plan.nodes:
        spec = lowered[node.node_id]
        definition, provider = _declared_identity(spec)
        if not isinstance(definition, str):
            raise PlanCompileRefused(
                "undeclared_definition", node.node_id, "the lowered spec declares no definition"
            )
        implementation = checked.get(definition)
        if implementation is None:
            raise PlanCompileRefused(
                "provider_not_registered",
                node.node_id,
                f"definition {definition} has no Provider in the explicit allowlist",
            )
        if implementation.operator is not node.operator:
            raise PlanCompileRefused(
                "operator_mismatch",
                node.node_id,
                f"{definition} is registered for {implementation.operator.value}, not "
                f"{node.operator.value}",
            )
        if type(spec) is not _SPEC_CLASS_BY_KIND[implementation.output_kind]:
            raise PlanCompileRefused(
                "output_kind_mismatch", node.node_id, f"{definition} does not emit {type(spec)}"
            )
        if provider != implementation.provider_key:
            raise PlanCompileRefused(
                "provider_identity_mismatch",
                node.node_id,
                f"the spec requests {provider!r}, the allowlist maps {definition} to "
                f"{implementation.provider_key}",
            )
        input_specs: list[VersionedSpec] = []
        input_nodes: list[str | None] = []
        for index, item in enumerate(node.inputs):
            if isinstance(item, NodeInput):
                input_specs.append(lowered[item.node_id])
                input_nodes.append(item.node_id)
            else:
                reference = direct[(node.node_id, index)]
                if _declared_identity(reference)[0] in {
                    RANK_CS_DEFINITION,
                    QUANTILE_CS_DEFINITION,
                }:
                    # A directly referenced cross-sectional feature is the same batch output as
                    # a cross-sectional plan node: no single-series consumer may read it.
                    raise PlanCompileRefused(
                        "cross_sectional_output_consumption_unsupported",
                        node.node_id,
                        f"input {index} references a cross-sectional feature, which cannot be "
                        "wired as a single-series plan input",
                    )
                input_specs.append(reference)
                input_nodes.append(None)
        nodes.append(
            CompiledNode(
                node_id=node.node_id,
                operator=node.operator,
                definition=definition,
                spec=spec,
                input_specs=tuple(input_specs),
                input_nodes=tuple(input_nodes),
                implementation=implementation,
            )
        )

    allowlist_hash = content_hash(
        {"allowlist": [checked[key].payload() for key in sorted(checked)]}
    )
    compiled = CompiledPlan(
        plan=plan,
        plan_hash=plan.content_hash(),
        nodes=tuple(nodes),
        execution_enabled=True,
        allowlist_hash=allowlist_hash,
    )
    # The only place a CompiledPlan is sealed as produced by the compile path.
    object.__setattr__(compiled, "_seal", _COMPILE_SEAL)
    if not compiled.runnable:  # pragma: no cover - every branch above refuses first
        raise PlanCompileRefused("not_runnable", plan.root, "compiled plan failed its own check")
    return compiled

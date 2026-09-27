"""Read-only direct-reference validation for non-runnable Phase 7 typed plans (ADR-0068).

``resolve_direct_references`` looks up every external ``SpecInput`` of a ``TypedPlan`` through a
caller-injected ``SpecResolver`` and fails closed unless each returned object is the exact core
spec class for the reference kind, names the same ``kind:name@version`` target, and recomputes to
the content hash claimed in the plan.

This verifies **direct references only**. It does not verify transitive dependency closure,
register or consult operator implementations, lower, compile or execute a plan, change
``TypedPlan.runnable``, write reports or journals, or touch ``TrialLedger``. A successful result
is not execution authorization; ``compile_plan`` still refuses every plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

from core.domain.base import Kind, Ref, RefTargetIdentity, VersionedSpec
from core.domain.specs import EventSpec, FeatureSpec, StateSpec, StrategySpec
from research.hypotheses.typed_plan import PlanRefused, SpecInput, TypedPlan

__all__ = [
    "DIRECT_REFERENCE_SCOPE",
    "DirectReferenceResolution",
    "PlanReferenceRefused",
    "ResolvedSpecInput",
    "SpecResolver",
    "resolve_direct_references",
]

DIRECT_REFERENCE_SCOPE: Final = (
    "direct SpecInput references only; transitive dependency closure is not verified, no "
    "operator is registered, the plan is not lowered or executed, TypedPlan.runnable is "
    "unchanged (always false), and no report, journal or TrialLedger entry is written"
)

# Plan inputs can only name these kinds (ADR-0068 §2). The exact class is required so that a
# resolver cannot hand back a subclass that overrides ``ref`` or the hash computation.
_SPEC_CLASSES: Final[dict[Kind, type[VersionedSpec]]] = {
    Kind.EVENT: EventSpec,
    Kind.FEATURE: FeatureSpec,
    Kind.STATE: StateSpec,
    Kind.STRATEGY: StrategySpec,
}


class SpecResolver(Protocol):
    """Explicitly injected lookup; ``None`` means the exact reference is not available."""

    def resolve(self, ref: Ref) -> VersionedSpec | None: ...


class PlanReferenceRefused(PlanRefused):
    """A direct plan reference could not be verified; no partial resolution is returned."""

    def __init__(self, code: str, spec_input: SpecInput, where: str, detail: str) -> None:
        self.code = code
        self.spec_input = spec_input
        self.where = where
        super().__init__(f"{code}: {where} {spec_input.ref}: {detail}")


@dataclass(frozen=True, slots=True)
class ResolvedSpecInput:
    """One ``SpecInput`` occurrence, in plan order, with the verified spec it names."""

    node_id: str
    input_index: int
    spec_input: SpecInput
    spec: VersionedSpec


@dataclass(frozen=True, slots=True)
class DirectReferenceResolution:
    """Verified direct references of one plan. Not a runnable plan or execution audit record."""

    plan_hash: str
    inputs: tuple[ResolvedSpecInput, ...]
    specs: tuple[VersionedSpec, ...]

    @property
    def scope(self) -> str:
        return DIRECT_REFERENCE_SCOPE

    @property
    def transitive_closure_verified(self) -> bool:
        return False

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def runnable(self) -> bool:
        return False


def _verified_copy(spec_input: SpecInput, where: str, resolver: SpecResolver) -> VersionedSpec:
    ref = spec_input.ref
    expected = _SPEC_CLASSES.get(ref.kind)
    if expected is None:
        raise PlanReferenceRefused(
            "unsupported_kind", spec_input, where, "kind is not a plan input kind"
        )
    try:
        resolved = resolver.resolve(ref)
    except Exception as exc:
        raise PlanReferenceRefused(
            "resolver_error", spec_input, where, f"resolver raised {type(exc).__name__}"
        ) from exc
    if resolved is None:
        raise PlanReferenceRefused("missing", spec_input, where, "resolver returned no spec")
    if type(resolved) is not expected:
        raise PlanReferenceRefused(
            "wrong_type",
            spec_input,
            where,
            f"expected exactly {expected.__name__}, got {type(resolved).__name__}",
        )
    # Hash a detached copy: it starts without a memoised hash and shares no nested objects with
    # the resolver, so the recorded spec is exactly the content that was verified.
    spec = resolved.model_copy(deep=True)
    if spec.ref.target_identity() != ref.target_identity():
        raise PlanReferenceRefused(
            "ref_mismatch", spec_input, where, f"resolver returned {spec.ref}"
        )
    actual = spec.content_hash()
    if actual != spec_input.content_hash:
        raise PlanReferenceRefused(
            "content_hash_mismatch",
            spec_input,
            where,
            f"claimed {spec_input.content_hash}, recomputed {actual}",
        )
    return spec


def resolve_direct_references(
    plan: TypedPlan, *, resolver: SpecResolver
) -> DirectReferenceResolution:
    """Verify every direct ``SpecInput`` of ``plan``; raise ``PlanReferenceRefused`` on any failure.

    Inputs are visited in node order, then input order. Each distinct reference target is resolved
    once, on first occurrence; conflicting claimed hashes for one target are refused before the
    resolver is called. See the module docstring for everything this does not verify.
    """
    if not isinstance(plan, TypedPlan):
        raise TypeError("plan must be a TypedPlan")
    if not callable(getattr(resolver, "resolve", None)):
        raise TypeError("resolver must be an explicitly supplied SpecResolver")

    occurrences: list[tuple[str, int, SpecInput]] = []
    claimed: dict[RefTargetIdentity, SpecInput] = {}
    for node in plan.nodes:
        for index, item in enumerate(node.inputs):
            if not isinstance(item, SpecInput):
                continue
            where = f"node {node.node_id}.inputs[{index}]"
            first = claimed.setdefault(item.ref.target_identity(), item)
            if first.content_hash != item.content_hash:
                raise PlanReferenceRefused(
                    "conflicting_claimed_hash",
                    item,
                    where,
                    f"claimed {item.content_hash}, earlier input claimed {first.content_hash}",
                )
            occurrences.append((node.node_id, index, item))

    verified: dict[RefTargetIdentity, VersionedSpec] = {}
    resolved_inputs: list[ResolvedSpecInput] = []
    for node_id, index, item in occurrences:
        key = item.ref.target_identity()
        spec = verified.get(key)
        if spec is None:
            spec = _verified_copy(item, f"node {node_id}.inputs[{index}]", resolver)
            verified[key] = spec
        resolved_inputs.append(ResolvedSpecInput(node_id, index, item, spec))
    return DirectReferenceResolution(
        plan_hash=plan.content_hash(),
        inputs=tuple(resolved_inputs),
        specs=tuple(verified.values()),
    )

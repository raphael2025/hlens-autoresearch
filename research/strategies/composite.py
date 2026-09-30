"""Composite strategies (ADR-0088 decision 2, contract 2.4.0): ``StrategySpec.composition``.

Research code — never production (H5). ``CompositeStrategyProvider`` serves ``StrategySpec`` objects
whose ``composition`` is set, by running the referenced (base / member) strategies through
providers the **caller injects** in a resolution table (``str(ref)`` → ``ResolvedStrategy``). It
never looks anything up in a global registry.

Execution semantics, per decision time ``t`` and instrument (one sub-request per referenced
strategy, over the same instruments, decision times and knowledge cutoff, carrying only the
observations of that strategy's own ``signals``; every sub-result is checked with
``check_answers``):

- ``conditioned`` (``ConditionedStrategy``): the gate is the latest visible (``available_time <=
  t``, by ``(event_time, available_time)``) observation of ``state`` for the instrument. When its
  value equals ``state_value`` the target is the base's target; otherwise — another label, no
  visible observation, or an explicit ``None`` (unknown) — the target is ``0``. A non-text state
  value is refused (``StrategyInputError``);
- ``ensemble`` (``EnsembleStrategy``, ``rule = equal_weight_mean``): the equal-weight mean of the
  members' targets (a member without information contributes its ``0``), half-even at 18 places;
- ``negated`` (``NegatedStrategy``): the base's target with the sign flipped. It is **not** a
  validation negative control. In a spot execution context (``InstrumentType.SPOT``) a negative
  target cannot be executed — the short cost is undefined (gap ST-4) — so a negated target below
  zero is refused (``UnsupportedStrategy``), never silently truncated to ``0``.

Input provenance: ``inputs_used`` is the sum of the used observations of the parts (the base /
members, plus the gate observation for ``conditioned``), capped at the size of the visible set
(parts may share observations); ``latest_input_available_time`` is the latest of theirs.

Construction refuses (fail closed, ``ValueError``): a spec without ``composition``; a composite
with its own ``params`` / ``param_search_space`` (a composite has no tunable parameter — the base's
point is fixed by the base spec, so another point is another base spec and another composite; a
subclass may admit one exact, non-tunable declaration per composition type, and then requires
it: a spec of that type must carry exactly those ``params``, empty ones included); a
reference missing from the resolution table or resolved to a provider that does not support that
spec's hash; a self-reference or a reference cycle through the table's composite specs; ``signals``
that do not cover every referenced strategy's signals; and ensemble members whose ``risk_policy`` or
``applicable_instruments`` differ (ADR-0069 rules 3 / 4: exact equality, nothing inferred).
Requests with parameters (``UnsupportedStrategy``) or with signals outside the composite's
``signals`` (``StrategyInputError``) are refused.

The referenced strategies' own risk policies are **not** applied here: a ``StrategyProvider``
returns pre-risk targets; the composite's ``risk_policy`` is applied downstream by the pipeline.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import ClassVar, Final

from core.contracts.strategy import (
    SignalObservation,
    StrategyInputError,
    StrategyProvider,
    StrategyProviderDescriptor,
    StrategyRequest,
    StrategyResult,
    TargetPosition,
    UnsupportedStrategy,
)
from core.domain.base import FrozenMapping, Ref, RefTargetIdentity
from core.domain.specs import (
    ConditionedStrategy,
    EnsembleStrategy,
    InstrumentType,
    NegatedStrategy,
    StrategySpec,
)
from research.strategies._params import resolve_params

__all__ = ["CompositeStrategyProvider", "ResolvedStrategy", "composition_references"]

_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_EVEN)
_WEIGHT_QUANTUM: Final = Decimal("1e-18")

type _Key = tuple[datetime, str]
#: One of the three composition classes (``StrategySpec.composition`` is their tagged union).
type _CompositionType = type[ConditionedStrategy | EnsembleStrategy | NegatedStrategy]


@dataclass(frozen=True, slots=True)
class ResolvedStrategy:
    """One entry of the caller's resolution table: a referenced spec and the provider serving it."""

    spec: StrategySpec
    provider: StrategyProvider


def composition_references(spec: StrategySpec) -> tuple[Ref, ...]:
    """The strategies a composite spec refers to (empty for a plain spec)."""
    if spec.composition is None:
        return ()
    return tuple(spec.composition.references())


def _identities(refs: Sequence[Ref]) -> set[RefTargetIdentity]:
    return {ref.target_identity() for ref in refs}


def _check_acyclic(root: StrategySpec, table: Mapping[str, ResolvedStrategy]) -> None:
    """Depth-first walk over the composite specs reachable through ``table``; a reference to a
    spec on the current path is a cycle (a self-reference is the shortest one)."""
    path: list[str] = []

    def visit(spec: StrategySpec) -> None:
        path.append(str(spec.ref))
        for ref in composition_references(spec):
            key = str(ref)
            if key in path:
                chain = " -> ".join([*path[path.index(key) :], key])
                raise ValueError(
                    f"composite strategies form a cycle: {chain} (ADR-0088 decision 2)"
                )
            entry = table.get(key)
            if entry is not None:
                visit(entry.spec)
        path.pop()

    visit(root)


class _Composite:
    """A served composite spec with its resolved parts."""

    __slots__ = ("parts", "signal_ids", "spec")

    def __init__(
        self,
        spec: StrategySpec,
        table: Mapping[str, ResolvedStrategy],
        declarations: Mapping[_CompositionType, Mapping[str, object]],
    ) -> None:
        if not isinstance(spec, StrategySpec):
            raise ValueError("a StrategySpec is required")
        composition = spec.composition
        if composition is None:
            raise ValueError(f"{spec.ref} has no composition; it is not a composite strategy")
        declared = declarations.get(type(composition))
        # Without a declaration for this composition type the spec must carry no params; with
        # one, its params must equal the declaration exactly (empty params are refused too: the
        # declaration is part of the served spec's identity, not optional).
        params_ok = (
            not spec.params if declared is None else _same_params(spec.params, declared)
        )
        if spec.param_search_space or not params_ok:
            raise ValueError(
                f"{spec.ref}: a composite strategy has no parameters of its own; the referenced "
                "specs fix their points (only a served declaration, e.g. the P7 lowering's, is "
                "admitted, exactly and required when the provider declares one)"
            )
        _check_acyclic(spec, table)
        self.spec = spec
        self.signal_ids = _identities(spec.signals)
        parts: list[ResolvedStrategy] = []
        for ref in composition.references():
            entry = table.get(str(ref))
            if entry is None:
                raise ValueError(f"{spec.ref}: {ref} is not in the resolution table")
            missing = _identities(entry.spec.signals) - self.signal_ids
            if missing:
                raise ValueError(
                    f"{spec.ref}: signals do not cover {ref}'s signals "
                    f"{sorted(f'{kind.value}:{name}@{version}' for kind, name, version in missing)}"
                )
            parts.append(entry)
        if isinstance(composition, EnsembleStrategy):
            first = parts[0].spec
            for other in parts[1:]:
                if other.spec.risk_policy != first.risk_policy:
                    raise ValueError(
                        f"{spec.ref}: ensemble members {first.ref} and {other.spec.ref} have "
                        "different risk_policy (ADR-0069 rule 3)"
                    )
                if other.spec.applicable_instruments != first.applicable_instruments:
                    raise ValueError(
                        f"{spec.ref}: ensemble members {first.ref} and {other.spec.ref} have "
                        "different applicable_instruments (ADR-0069 rule 4)"
                    )
        self.parts = tuple(parts)


def _same_params(params: Mapping[str, object], declared: Mapping[str, object]) -> bool:
    """Exact equality, type included (``True`` is not ``1``)."""
    return set(params) == set(declared) and all(
        type(params[key]) is type(value) and params[key] == value
        for key, value in declared.items()
    )


def _check_table(table: Mapping[str, ResolvedStrategy]) -> dict[str, ResolvedStrategy]:
    out: dict[str, ResolvedStrategy] = {}
    for key, entry in table.items():
        if not isinstance(entry, ResolvedStrategy):
            raise ValueError(f"resolution entry {key!r} must be a ResolvedStrategy")
        if key != str(entry.spec.ref):
            raise ValueError(f"resolution key {key!r} does not name its spec {entry.spec.ref}")
        if not entry.provider.descriptor.supports(entry.spec.ref, entry.spec.content_hash()):
            raise ValueError(f"the provider resolved for {key} does not support that spec's hash")
        out[key] = entry
    return out


class CompositeStrategyProvider:
    """``StrategyProvider`` for composite specs (ADR-0088 decision 2); deterministic, ``Decimal``.

    ``resolution`` maps ``str(ref)`` of every referenced strategy to its spec and provider
    (explicit; no registry lookup). ``instrument_type`` is the execution context of the targets —
    explicit, no default; ``SPOT`` refuses negated short targets (gap ST-4).

    Subclasses may narrow the served compositions (``COMPOSITIONS``), change the descriptor
    identity (``NAME`` / ``VERSION``) and admit an exact, non-tunable ``params`` declaration per
    composition type (``DECLARED_PARAMS``) — the P7 execution Providers in
    ``research.strategies.p7_compositions`` do. The base class keeps its identity and refuses any
    ``params``.
    """

    NAME: ClassVar[str] = "research_composite"
    VERSION: ClassVar[str] = "0.1.0"
    #: ``None``: every composition type; otherwise only these.
    COMPOSITIONS: ClassVar[tuple[_CompositionType, ...] | None] = None
    #: Exact ``params`` admitted per composition type (none by default).
    DECLARED_PARAMS: ClassVar[Mapping[_CompositionType, Mapping[str, object]]] = {}

    def __init__(
        self,
        specs: Sequence[StrategySpec],
        *,
        resolution: Mapping[str, ResolvedStrategy],
        instrument_type: InstrumentType,
    ) -> None:
        chosen = tuple(specs)
        if not chosen:
            raise ValueError("at least one explicit composite spec is required")
        if not isinstance(instrument_type, InstrumentType):
            raise ValueError("instrument_type must be an InstrumentType")
        table = _check_table(resolution)
        self._instrument_type = instrument_type
        self._specs: dict[str, _Composite] = {}
        for spec in chosen:
            key = str(spec.ref)
            if key in self._specs:
                raise ValueError(f"{key} is given more than once")
            if (
                self.COMPOSITIONS is not None
                and isinstance(spec, StrategySpec)
                and type(spec.composition) not in self.COMPOSITIONS
            ):
                raise ValueError(f"{key}: {self.NAME} does not serve this composition type")
            self._specs[key] = _Composite(spec, table, self.DECLARED_PARAMS)
        self._descriptor = StrategyProviderDescriptor(
            name=self.NAME,
            version=self.VERSION,
            deterministic=True,
            supported_strategies=FrozenMapping(
                {key: item.spec.content_hash() for key, item in self._specs.items()}
            ),
        )

    @property
    def descriptor(self) -> StrategyProviderDescriptor:
        return self._descriptor

    @property
    def instrument_type(self) -> InstrumentType:
        return self._instrument_type

    def target_positions(self, request: StrategyRequest) -> StrategyResult:
        if not isinstance(request, StrategyRequest):
            raise StrategyInputError("target_positions needs a StrategyRequest")
        if not self._descriptor.supports(request.strategy, request.spec_hash):
            raise UnsupportedStrategy(f"{request.strategy} with this spec hash is not supported")
        composite = self._specs[str(request.strategy)]
        resolve_params(composite.spec, request.params)  # a composite declares no parameter
        for item in request.signals:
            if item.signal.target_identity() not in composite.signal_ids:
                raise StrategyInputError(
                    f"{composite.spec.ref} does not consume signal {item.signal}"
                )
        parts = [self._run_part(request, part) for part in composite.parts]
        composition = composite.spec.composition
        positions: list[TargetPosition] = []
        with localcontext(_CONTEXT):
            for decision_time in request.decision_times:
                visible = request.visible_at(decision_time)
                for instrument in request.instruments:
                    key = (decision_time, instrument)
                    if isinstance(composition, ConditionedStrategy):
                        position = _conditioned(composition, parts[0][key], visible, instrument)
                    elif isinstance(composition, EnsembleStrategy):
                        position = _ensemble([part[key] for part in parts], len(visible))
                    elif isinstance(composition, NegatedStrategy):
                        position = self._negated(parts[0][key], composite.spec)
                    else:  # pragma: no cover - the contract admits exactly three compositions
                        raise UnsupportedStrategy(f"{composite.spec.ref}: unknown composition")
                    positions.append(position)
        return StrategyResult.build(request, self._descriptor, positions)

    @staticmethod
    def _run_part(request: StrategyRequest, part: ResolvedStrategy) -> dict[_Key, TargetPosition]:
        wanted = _identities(part.spec.signals)
        sub = StrategyRequest(
            strategy=part.spec.ref,
            spec_hash=part.spec.content_hash(),
            params=FrozenMapping({}),
            instruments=request.instruments,
            knowledge_cutoff=request.knowledge_cutoff,
            decision_times=request.decision_times,
            signals=tuple(
                item for item in request.signals if item.signal.target_identity() in wanted
            ),
        )
        result = part.provider.target_positions(sub)
        try:
            result.check_answers(sub, part.provider.descriptor)
        except ValueError as exc:
            raise StrategyInputError(f"{part.spec.ref} answered inconsistently: {exc}") from exc
        return {(item.decision_time, item.instrument): item for item in result.positions}

    def _negated(self, base: TargetPosition, spec: StrategySpec) -> TargetPosition:
        weight = -base.target_weight if base.target_weight != 0 else Decimal(0)
        if weight < 0 and self._instrument_type is InstrumentType.SPOT:
            raise UnsupportedStrategy(
                f"{spec.ref}: negation would give a short target for {base.instrument} at "
                f"{base.decision_time.isoformat()} in a spot context; the short cost is undefined "
                "(gap ST-4), so it is refused rather than truncated to 0"
            )
        return TargetPosition(
            decision_time=base.decision_time,
            instrument=base.instrument,
            target_weight=weight,
            inputs_used=base.inputs_used,
            latest_input_available_time=base.latest_input_available_time,
        )


def _latest_state(
    visible: Sequence[SignalObservation], state: Ref, instrument: str
) -> SignalObservation | None:
    identity = state.target_identity()
    series = [
        item
        for item in visible
        if item.instrument == instrument and item.signal.target_identity() == identity
    ]
    if not series:
        return None
    return max(series, key=lambda item: (item.event_time, item.available_time))


def _combined(
    decision_time: datetime,
    instrument: str,
    weight: Decimal,
    used: Sequence[tuple[int, datetime | None]],
    visible_count: int,
) -> TargetPosition:
    inputs = min(sum(count for count, _ in used), visible_count)
    times = [time for _, time in used if time is not None]
    if inputs == 0:
        return TargetPosition(
            decision_time=decision_time,
            instrument=instrument,
            target_weight=Decimal(0),
            inputs_used=0,
        )
    return TargetPosition(
        decision_time=decision_time,
        instrument=instrument,
        target_weight=weight,
        inputs_used=inputs,
        latest_input_available_time=max(times),
    )


def _conditioned(
    composition: ConditionedStrategy,
    base: TargetPosition,
    visible: Sequence[SignalObservation],
    instrument: str,
) -> TargetPosition:
    gate = _latest_state(visible, composition.state, instrument)
    if gate is None or gate.value is None:  # missing or explicitly unknown state: flat
        return TargetPosition(
            decision_time=base.decision_time,
            instrument=instrument,
            target_weight=Decimal(0),
            inputs_used=0,
        )
    if not isinstance(gate.value, str):
        raise StrategyInputError(f"{composition.state} values must be state labels (text) or None")
    used: list[tuple[int, datetime | None]] = [(1, gate.available_time)]
    weight = Decimal(0)
    if gate.value == composition.state_value:
        used.append((base.inputs_used, base.latest_input_available_time))
        weight = base.target_weight
    return _combined(base.decision_time, instrument, weight, used, len(visible))


def _ensemble(members: Sequence[TargetPosition], visible_count: int) -> TargetPosition:
    first = members[0]
    total = sum((item.target_weight for item in members), Decimal(0))
    weight = (total / len(members)).quantize(_WEIGHT_QUANTUM)
    if weight == 0:
        weight = Decimal(0)
    used = [(item.inputs_used, item.latest_input_available_time) for item in members]
    return _combined(first.decision_time, first.instrument, weight, used, visible_count)

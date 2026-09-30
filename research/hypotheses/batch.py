"""Batch hypothesis generation over a declared grid (roadmap Phase 7 completion; ADR-0040).

A batch is the explicit product **operator × input strategy × parameter point** of a declared
``BatchGrid`` — every factor is listed by the caller, nothing has a default. Each cell becomes one
``Hypothesis`` (``origin = combination``, the input strategy as ``origin_ref``) whose conditions are
exactly the two forms the research loop's ``trial_point`` runs::

    strategy = <name>@<version>
    param <name> = <value>          (one line per declared parameter, sorted by name)

so a batch hypothesis is always run as what it says, never turned into an ERRORED trial later.

Refusals (``BatchRefused``, all at construction, before anything is registered or run):

- an operator that is not on the ``ReviewedOperators`` allowlist — a declared, versioned list of
  ``BatchOperator`` s a named human reviewed; an operator whose ``name@version`` is listed but
  whose content differs from the reviewed one is not on it either;
- an operator of a kind whose conditions ``trial_point`` cannot run (the combination operators of
  ``research.hypotheses.dsl``: conditioning, interaction, temporal, transformation, ensemble,
  negation); an unknown kind is refused when the operator is declared;
- a parameter point that is not a runnable point of the input strategy: an undeclared parameter,
  a value outside the strategy's declared ``param_search_space``, a float (the loop never requests
  one), or a value whose condition text would not read back as the same value;
- an empty or duplicated factor.

``preregister_batch`` validates and registers the **whole** batch in a ``TrialLedger`` before any
of it runs. Conflicts are checked before writing; a durable ledger records new members in one
journal event, and updates in-memory state only after that append succeeds. This guarantee is
limited to the TrialLedger journal and is not a transaction with loop or plan-audit journals, so the
family's trial count — what the multiple-testing correction uses — covers every registered cell.

Operators are data (a claim, an expected direction and a kind from a fixed list), never code: the
expansion is this module's fixed function, and nothing generated is ever executed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from core.domain.base import NAME_PATTERN, SEMVER_PATTERN, FrozenMapping, content_hash
from core.domain.research import Hypothesis, HypothesisOrigin
from core.domain.specs import StrategySpec
from research.hypotheses.ledger import TrialLedger

__all__ = [
    "BATCH_SCHEMA_VERSION",
    "NOT_RUNNABLE_KINDS",
    "RUNNABLE_KINDS",
    "BatchGrid",
    "BatchOperator",
    "BatchRefused",
    "HypothesisBatch",
    "ParamValue",
    "ReviewedOperators",
    "expand_batch",
    "preregister_batch",
]

BATCH_SCHEMA_VERSION: Final = "1.0.0"
#: Operator kinds whose hypotheses are a strategy at a parameter point (``trial_point`` runs them).
RUNNABLE_KINDS: Final = frozenset({"parameter_point"})
#: The ``research.hypotheses.dsl`` operators: their conditions (``state = …``, ``window = …``,
#: ``transform = …``, or none) are not a strategy point, so the loop cannot run them.
NOT_RUNNABLE_KINDS: Final = frozenset(
    {"conditioning", "interaction", "temporal", "transformation", "ensemble", "negation"}
)

type ParamValue = str | int | bool

_NAME: Final = re.compile(NAME_PATTERN)
_SEMVER: Final = re.compile(SEMVER_PATTERN)
_TOKEN: Final = re.compile(r"^\S+$")
_INT: Final = re.compile(r"^-?\d+$")


class BatchRefused(ValueError):
    """The batch cannot be generated as declared (see module docs); nothing was registered."""


def _name(value: object, what: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise BatchRefused(f"{what} must match {NAME_PATTERN}, got {value!r}")
    return value


def _semver(value: object, what: str) -> str:
    if not isinstance(value, str) or not _SEMVER.fullmatch(value):
        raise BatchRefused(f"{what} must be a SemVer version, got {value!r}")
    return value


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BatchRefused(f"{what} must be non-empty text")
    return value.strip()


@dataclass(frozen=True, slots=True)
class BatchOperator:
    """A declared batch operator: data only (``kind`` from a fixed list; see module docs)."""

    name: str
    version: str
    kind: str
    claim: str
    expected_direction: str

    def __post_init__(self) -> None:
        _name(self.name, "operator name")
        _semver(self.version, f"operator {self.name} version")
        if self.kind not in RUNNABLE_KINDS | NOT_RUNNABLE_KINDS:
            raise BatchRefused(f"operator {self.name}: unknown kind {self.kind!r}")
        object.__setattr__(self, "claim", _text(self.claim, f"operator {self.name} claim"))
        object.__setattr__(
            self,
            "expected_direction",
            _text(self.expected_direction, f"operator {self.name} expected_direction"),
        )

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"

    def payload(self) -> dict[str, str]:
        return {
            "name": self.name,
            "version": self.version,
            "kind": self.kind,
            "claim": self.claim,
            "expected_direction": self.expected_direction,
        }

    def content_hash(self) -> str:
        return content_hash(self.payload())


@dataclass(frozen=True, slots=True)
class ReviewedOperators:
    """The declared, versioned allowlist of batch operators a named human reviewed."""

    name: str
    version: str
    reviewer: str
    operators: tuple[BatchOperator, ...]

    def __post_init__(self) -> None:
        _name(self.name, "allowlist name")
        _semver(self.version, f"allowlist {self.name} version")
        object.__setattr__(self, "reviewer", _text(self.reviewer, "allowlist reviewer"))
        operators = tuple(self.operators)
        if not operators or not all(isinstance(op, BatchOperator) for op in operators):
            raise BatchRefused("an allowlist lists at least one BatchOperator")
        keys = [op.key for op in operators]
        if len(set(keys)) != len(keys):
            raise BatchRefused(f"allowlist {self.name} lists an operator twice")
        object.__setattr__(self, "operators", operators)

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"

    def admits(self, operator: BatchOperator) -> bool:
        """``operator`` is listed with exactly the reviewed content."""
        return any(
            op.key == operator.key and op.content_hash() == operator.content_hash()
            for op in self.operators
        )

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "reviewer": self.reviewer,
            "operators": [op.payload() for op in self.operators],
        }

    def content_hash(self) -> str:
        return content_hash(self.payload())


def _render(value: object) -> str:
    """The condition text of ``value``; it must read back as exactly ``value`` (``trial_point``)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if not _TOKEN.fullmatch(value) or value in {"true", "false"} or _INT.fullmatch(value):
            raise BatchRefused(f"the text value {value!r} would not read back as itself")
        return value
    raise BatchRefused(f"a batch parameter is a str, int or bool, got {type(value).__name__}")


def _same(value: ParamValue, declared: object) -> bool:
    return type(value) is type(declared) and value == declared


@dataclass(frozen=True, slots=True)
class BatchGrid:
    """The declared grid: every factor explicit, no defaults (see module docs).

    ``name`` prefixes every hypothesis name; ``created_at`` is the hypotheses' creation time.
    ``points`` are the parameter overrides of each cell (an explicitly empty mapping is the
    strategy's own declared parameters).
    """

    name: str
    family_id: str
    created_at: datetime
    operators: tuple[BatchOperator, ...]
    strategies: tuple[StrategySpec, ...]
    points: tuple[Mapping[str, ParamValue], ...]
    minimum_meaningful_effect: str

    def __post_init__(self) -> None:
        _name(self.name, "batch name")
        object.__setattr__(self, "family_id", _text(self.family_id, "family_id"))
        object.__setattr__(
            self,
            "minimum_meaningful_effect",
            _text(self.minimum_meaningful_effect, "minimum_meaningful_effect"),
        )
        if not isinstance(self.created_at, datetime) or self.created_at.utcoffset() is None:
            raise BatchRefused("created_at must be a timezone-aware datetime")
        operators, strategies = tuple(self.operators), tuple(self.strategies)
        if not operators or not all(isinstance(op, BatchOperator) for op in operators):
            raise BatchRefused("a batch declares at least one BatchOperator")
        if not strategies or not all(isinstance(s, StrategySpec) for s in strategies):
            raise BatchRefused("a batch declares at least one input StrategySpec")
        points: list[Mapping[str, ParamValue]] = []
        for point in self.points:
            if not isinstance(point, Mapping):
                raise BatchRefused("every parameter point is a mapping of name to value")
            points.append(FrozenMapping(dict(sorted(point.items()))))
        if not points:
            raise BatchRefused("a batch declares at least one parameter point")
        for what, keys in (
            ("operator", [op.key for op in operators]),
            ("strategy", [str(s.ref) for s in strategies]),
            # ``FrozenMapping`` is intentionally immutable but the canonical JSON encoder
            # accepts plain dict payloads. Keep the hash representation the same as ``payload``.
            ("parameter point", [content_hash(dict(p)) for p in points]),
        ):
            if len(set(keys)) != len(keys):
                raise BatchRefused(f"the batch declares a {what} twice")
        object.__setattr__(self, "operators", operators)
        object.__setattr__(self, "strategies", strategies)
        object.__setattr__(self, "points", tuple(points))

    @property
    def cells(self) -> int:
        return len(self.operators) * len(self.strategies) * len(self.points)

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": BATCH_SCHEMA_VERSION,
            "name": self.name,
            "family_id": self.family_id,
            "created_at": self.created_at.isoformat(),
            "operators": [op.payload() for op in self.operators],
            "strategies": {str(s.ref): s.content_hash() for s in self.strategies},
            "points": [dict(p) for p in self.points],
            "minimum_meaningful_effect": self.minimum_meaningful_effect,
        }

    def content_hash(self) -> str:
        return content_hash(self.payload())


def _conditions(spec: StrategySpec, point: Mapping[str, ParamValue]) -> tuple[str, ...]:
    space = spec.param_search_space
    lines = [f"strategy = {spec.name}@{spec.version}"]
    for key, value in point.items():
        _name(key, f"{spec.ref} parameter name")
        if key not in space:
            raise BatchRefused(f"{spec.ref} declares no search space for {key!r}")
        if isinstance(value, float) or not any(_same(value, v) for v in space[key]):
            raise BatchRefused(
                f"{spec.ref}: {key}={value!r} is not a declared point of its search space"
            )
        lines.append(f"param {key} = {_render(value)}")
    return tuple(lines)


def _cell(
    grid: BatchGrid, operator: BatchOperator, spec: StrategySpec, point: Mapping[str, ParamValue]
) -> Hypothesis:
    conditions = _conditions(spec, point)
    digest = content_hash(
        {"operator": operator.key, "strategy": str(spec.ref), "point": dict(point)}
    )
    shown = ", ".join(line.removeprefix("param ") for line in conditions[1:]) or "declared params"
    return Hypothesis(
        name=f"{grid.name}_{digest[:16]}",
        version="1.0.0",
        created_at=grid.created_at,
        family_id=grid.family_id,
        statement=f"{operator.claim} [{operator.key}: {spec.ref} at {shown}]",
        conditions=conditions,
        expected_direction=operator.expected_direction,
        minimum_meaningful_effect=grid.minimum_meaningful_effect,
        origin=HypothesisOrigin.COMBINATION,
        origin_refs=(spec.ref,),
    )


@dataclass(frozen=True, slots=True)
class HypothesisBatch:
    """The expansion of ``grid`` under ``allowlist``; ``hypotheses`` is computed, never passed."""

    grid: BatchGrid
    allowlist: ReviewedOperators
    hypotheses: tuple[Hypothesis, ...] = field(init=False)

    def __post_init__(self) -> None:
        for operator in self.grid.operators:
            if not self.allowlist.admits(operator):
                raise BatchRefused(
                    f"operator {operator.key} is not on the reviewed allowlist "
                    f"{self.allowlist.key} (unknown, or its content differs from the reviewed one)"
                )
            if operator.kind not in RUNNABLE_KINDS:
                raise BatchRefused(
                    f"operator {operator.key} ({operator.kind}) produces conditions the loop's "
                    "trial_point cannot run"
                )
        hypotheses = tuple(
            _cell(self.grid, operator, spec, point)
            for operator in self.grid.operators
            for spec in self.grid.strategies
            for point in self.grid.points
        )
        names = [h.name for h in hypotheses]
        if len(set(names)) != len(names):  # a 64-bit prefix collision: refuse, never merge
            raise BatchRefused("two batch cells got the same hypothesis name")
        object.__setattr__(self, "hypotheses", hypotheses)

    def payload(self) -> dict[str, Any]:
        """What a loop fingerprint binds (the grid, the allowlist and every cell's hash)."""
        return {
            "schema_version": BATCH_SCHEMA_VERSION,
            "grid": self.grid.content_hash(),
            "allowlist": self.allowlist.key,
            "allowlist_hash": self.allowlist.content_hash(),
            "hypotheses": [h.content_hash() for h in self.hypotheses],
        }


def expand_batch(grid: BatchGrid, allowlist: ReviewedOperators) -> HypothesisBatch:
    """Every cell of ``grid`` as a hypothesis, or ``BatchRefused`` (see module docs)."""
    return HypothesisBatch(grid, allowlist)


def preregister_batch(batch: HypothesisBatch, ledger: TrialLedger) -> tuple[Hypothesis, ...]:
    """Register every new cell in one TrialLedger event; return those newly registered.

    ``TrialLedger.register_batch`` preflights every member and, when durable, appends one journal
    event before changing memory. Identical prior registrations remain idempotent.
    """
    return ledger.register_batch(batch.hypotheses)

"""Event interaction DSL: a JSON expression compiled into ordinary interaction specs (ADR-0061).

The expression is **data**, never code. A node is exactly one of

- ``{"ref": "event:<name>@<semver>"}`` — an already registered event spec (looked up in the
  registry passed to ``compile_expression``);
- ``{"op": "seq", "a": <node>, "b": <node>, "within_us": <int>}`` — A then B within ``within``;
- ``{"op": "and", "a": <node>, "b": <node>, "within_us": <int>}`` — A and B within ``within`` of
  each other (either order);
- ``{"op": "not", "a": <node>, "b": <node>, "within_us": <int>}`` — A occurs and no B within
  ``[A, A + within]``;
- ``{"op": "count", "a": <node>, "at_least": <int>, "within_us": <int>}`` — at an A event, at least
  ``at_least`` A events within ``[A - within, A]``.

``within_us`` is a positive whole number of microseconds, ``at_least`` a positive int; every field
is required (no defaults) and any unknown node, operator or field — or a float, a JSON constant, a
repeated key, an ill-typed value — is refused (``DslError``). The compile limits
(``CompileLimits.max_depth`` / ``max_nodes``) are required parameters, checked while parsing.

**Compilation** (post-order): every internal node becomes an ordinary interaction ``EventSpec``
served by a reviewed provider — ``seq`` → ``event_sequence``, ``and`` → ``event_co_occurrence``
(``plugins.events.interactions``), ``count`` → ``event_count`` and ``not`` →
``event_window_end`` + ``event_absence`` (``plugins.events.windows``; one spec cannot date an
event at the window end *and* see the Bs inside the window, see that module). Each spec's
``lineage`` is exactly its upstream refs and its trigger binds each upstream spec hash verbatim
(``<name>`` / ``<name>_hash``), so every hop runs through ``infrastructure.event.run_events`` with
its upstream verification. Canonical: ``and`` operands are sorted by spec hash, ``seq`` / ``not``
keep their order; a compiled spec's name is derived from its content (``dsl_<op>_<hash16>``,
version ``1.0.0``, ``observable_lag`` zero except the window end), so the same expression over the
same registry always compiles to the same specs and hashes, and equal sub-expressions share one
spec. The ``Compilation`` records the expression hash, the leaf bindings and every compiled spec
hash (``record`` / ``compilation_hash``) and is re-verified by ``verify_compilation``.

Event time (ADR-0061 §4): an interaction event's time is the observable time of its last required
input; a ``not`` event's time is the **end** of A's window.

Only ``core`` and the event plugins are imported (execution is the runner's; ``hops`` only says
which provider and upstream specs each compiled spec needs).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from core.contracts.event import EventProvider
from core.domain.base import Kind, Ref, canonical_json, content_hash
from core.domain.specs import EventSpec
from plugins.events._base import EventProviderBase
from plugins.events.interactions import EventCoOccurrenceProvider, EventSequenceProvider
from plugins.events.windows import EventAbsenceProvider, EventCountProvider, EventWindowEndProvider

__all__ = [
    "OPERATORS",
    "Compilation",
    "CompileLimits",
    "CountNode",
    "DslError",
    "Hop",
    "Node",
    "PairNode",
    "RefNode",
    "compile_expression",
    "hops",
    "parse_expression",
    "to_data",
    "verify_compilation",
]

#: The four reviewed operators (ADR-0061 §1).
OPERATORS: Final = frozenset({"seq", "and", "not", "count"})
_PAIR_FIELDS: Final = frozenset({"op", "a", "b", "within_us"})
_COUNT_FIELDS: Final = frozenset({"op", "a", "at_least", "within_us"})
_VERSION: Final = "1.0.0"
_MICROSECOND: Final = timedelta(microseconds=1)
#: Compiled specs are served by these providers, keyed by trigger operator.
_PROVIDERS: Final[dict[str, type[EventProviderBase]]] = {
    item.OPERATOR: item
    for item in (
        EventSequenceProvider,
        EventCoOccurrenceProvider,
        EventWindowEndProvider,
        EventAbsenceProvider,
        EventCountProvider,
    )
}


class DslError(ValueError):
    """The expression is not a valid DSL expression, or cannot be compiled (fail closed)."""


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DslError(f"{name} must be a positive int, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class CompileLimits:
    """Explicit compile limits (ADR-0061 consequences): both required, no defaults.

    ``max_depth``: nodes on the longest root-to-leaf path (a bare ``ref`` has depth 1);
    ``max_nodes``: every node of the tree, leaves included (before sharing equal sub-trees).
    """

    max_depth: int
    max_nodes: int

    def __post_init__(self) -> None:
        _positive_int(self.max_depth, "max_depth")
        _positive_int(self.max_nodes, "max_nodes")


@dataclass(frozen=True, slots=True)
class RefNode:
    ref: Ref


@dataclass(frozen=True, slots=True)
class PairNode:
    op: str  # "seq" | "and" | "not"
    a: Node
    b: Node
    within_us: int


@dataclass(frozen=True, slots=True)
class CountNode:
    a: Node
    at_least: int
    within_us: int


type Node = RefNode | PairNode | CountNode


# ======================================================================================
# Parsing
# ======================================================================================


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise DslError(f"the key {key!r} is repeated")
        out[key] = value
    return out


def _refuse_float(text: str) -> Any:
    raise DslError(f"numbers must be ints, got {text}")


def _refuse_constant(text: str) -> Any:
    raise DslError(f"{text} is not allowed")


def _load(expression: str | Mapping[str, Any]) -> Any:
    if isinstance(expression, str):
        try:
            return json.loads(
                expression,
                object_pairs_hook=_no_duplicate_keys,
                parse_float=_refuse_float,
                parse_constant=_refuse_constant,
            )
        except json.JSONDecodeError as exc:
            raise DslError(f"the expression is not JSON: {exc}") from None
    if isinstance(expression, Mapping):
        return expression
    raise DslError("the expression must be JSON text or a mapping")


class _Parser:
    def __init__(self, limits: CompileLimits) -> None:
        if not isinstance(limits, CompileLimits):
            raise DslError("limits must be CompileLimits")
        self._limits = limits
        self._nodes = 0

    def node(self, raw: object, depth: int) -> Node:
        if depth > self._limits.max_depth:
            raise DslError(f"the expression is deeper than max_depth={self._limits.max_depth}")
        self._nodes += 1
        if self._nodes > self._limits.max_nodes:
            raise DslError(f"the expression has more than max_nodes={self._limits.max_nodes}")
        if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
            raise DslError(f"a node must be a JSON object, got {raw!r}")
        keys = frozenset(raw)
        if "ref" in keys:
            if keys != {"ref"}:
                raise DslError(f"a ref node has exactly the field 'ref', got {sorted(keys)}")
            return RefNode(ref=self._ref(raw["ref"]))
        if "op" not in keys:
            raise DslError(f"unknown node (neither 'ref' nor 'op'): {sorted(keys)}")
        op = raw["op"]
        if not isinstance(op, str) or op not in OPERATORS:
            raise DslError(f"unknown operator {op!r} (allowed: {sorted(OPERATORS)})")
        expected = _COUNT_FIELDS if op == "count" else _PAIR_FIELDS
        if keys != expected:
            raise DslError(
                f"{op} takes exactly the fields {sorted(expected)}, got {sorted(keys)} "
                "(no defaults, no unknown fields)"
            )
        within_us = _positive_int(raw["within_us"], "within_us")
        if op == "count":
            at_least = _positive_int(raw["at_least"], "at_least")
            a = self.node(raw["a"], depth + 1)
            return CountNode(a=a, at_least=at_least, within_us=within_us)
        return PairNode(
            op=op,
            a=self.node(raw["a"], depth + 1),
            b=self.node(raw["b"], depth + 1),
            within_us=within_us,
        )

    @staticmethod
    def _ref(value: object) -> Ref:
        if not isinstance(value, str):
            raise DslError(f"ref must be an event reference string, got {value!r}")
        try:
            ref = Ref.parse(value)
        except ValueError as exc:
            raise DslError(f"ref {value!r} is not a reference: {exc}") from None
        if ref.kind is not Kind.EVENT or str(ref) != value:
            raise DslError(f"ref must be a canonical event reference, got {value!r}")
        return ref


def parse_expression(expression: str | Mapping[str, Any], limits: CompileLimits) -> Node:
    """The expression tree; refuses anything but the grammar in the module docstring."""
    return _Parser(limits).node(_load(expression), 1)


def to_data(node: Node) -> dict[str, Any]:
    """The JSON data of a node (the inverse of ``parse_expression``)."""
    if isinstance(node, RefNode):
        return {"ref": str(node.ref)}
    if isinstance(node, CountNode):
        return {
            "op": "count",
            "a": to_data(node.a),
            "at_least": node.at_least,
            "within_us": node.within_us,
        }
    return {"op": node.op, "a": to_data(node.a), "b": to_data(node.b), "within_us": node.within_us}


# ======================================================================================
# Compilation
# ======================================================================================


@dataclass(frozen=True, slots=True)
class Compilation:
    """The compiled specs of one expression over one registry (ADR-0061 §3).

    ``specs`` are the compiled interaction specs in post-order (each after its upstream; shared
    sub-expressions once); ``leaves`` the ``(ref, spec hash)`` of every registry spec used;
    ``root`` the spec that answers the whole expression (a leaf for a bare ``ref``).
    """

    expression: str
    expression_hash: str
    limits: CompileLimits
    leaves: tuple[tuple[str, str], ...]
    specs: tuple[EventSpec, ...]
    root: EventSpec

    def record(self) -> dict[str, Any]:
        """The re-verifiable record: expression hash, limits, leaf and compiled spec hashes."""
        return {
            "expression_hash": self.expression_hash,
            "limits": {"max_depth": self.limits.max_depth, "max_nodes": self.limits.max_nodes},
            "leaves": [list(item) for item in self.leaves],
            "specs": [[str(item.ref), item.content_hash()] for item in self.specs],
            "root": [str(self.root.ref), self.root.content_hash()],
        }

    @property
    def compilation_hash(self) -> str:
        return content_hash(self.record())


def _name(op: str, payload: dict[str, Any]) -> str:
    return f"dsl_{op}_{content_hash({'op': op, **payload})[:16]}"


def _bound(spec: EventSpec) -> list[str]:
    return [str(spec.ref), spec.content_hash()]


class _Compiler:
    def __init__(self, registry: Iterable[EventSpec]) -> None:
        by_ref: dict[str, EventSpec] = {}
        for item in registry:
            if not isinstance(item, EventSpec):
                raise DslError("the registry must hold EventSpec instances")
            key = str(item.ref)
            if key in by_ref:
                raise DslError(f"the registry holds {key} twice")
            by_ref[key] = item
        self._registry = by_ref
        self._leaves: dict[str, str] = {}
        self._specs: dict[str, EventSpec] = {}

    def spec(self, node: Node) -> EventSpec:
        if isinstance(node, RefNode):
            found = self._registry.get(str(node.ref))
            if found is None:
                raise DslError(f"{node.ref} is not a registered event spec")
            self._leaves[str(node.ref)] = found.content_hash()
            return found
        window = node.within_us * _MICROSECOND
        a = self.spec(node.a)
        try:
            if isinstance(node, CountNode):
                params = {"a": _bound(a), "at_least": node.at_least, "within_us": node.within_us}
                return self._add(
                    EventCountProvider.spec(
                        a, node.at_least, window, name=_name("count", params), version=_VERSION
                    )
                )
            b = self.spec(node.b)
            if a.ref == b.ref:
                raise DslError(f"{node.op}: the two operands are the same definition {a.ref}")
            if node.op == "not":
                return self._not(a, b, node.within_us, window)
            if node.op == "and":
                a, b = sorted((a, b), key=lambda item: item.content_hash())
            params = {"a": _bound(a), "b": _bound(b), "within_us": node.within_us}
            builder = EventSequenceProvider if node.op == "seq" else EventCoOccurrenceProvider
            return self._add(
                builder.spec(a, b, window, name=_name(node.op, params), version=_VERSION)
            )
        except DslError:
            raise
        except ValueError as exc:
            raise DslError(f"{_label(node)} cannot be compiled: {exc}") from None

    def _not(self, a: EventSpec, b: EventSpec, within_us: int, window: timedelta) -> EventSpec:
        end = self._add(
            EventWindowEndProvider.spec(
                a,
                window,
                name=_name("window_end", {"a": _bound(a), "within_us": within_us}),
                version=_VERSION,
            )
        )
        params = {"a": _bound(a), "b": _bound(b), "within_us": within_us}
        return self._add(
            EventAbsenceProvider.spec(end, b, window, name=_name("not", params), version=_VERSION)
        )

    def _add(self, spec: EventSpec) -> EventSpec:
        key = str(spec.ref)
        for known in (self._registry.get(key), self._specs.get(key)):
            if known is not None and known.content_hash() != spec.content_hash():
                raise DslError(f"the compiled {key} collides with another spec of that ref")
        return self._specs.setdefault(key, spec)

    def compilation(self, text: str, limits: CompileLimits, root: EventSpec) -> Compilation:
        return Compilation(
            expression=text,
            expression_hash=content_hash(json.loads(text)),
            limits=limits,
            leaves=tuple(sorted(self._leaves.items())),
            specs=tuple(self._specs.values()),
            root=root,
        )


def _label(node: Node) -> str:
    if isinstance(node, RefNode):
        return str(node.ref)
    return "count" if isinstance(node, CountNode) else node.op


def compile_expression(
    expression: str | Mapping[str, Any], registry: Iterable[EventSpec], limits: CompileLimits
) -> Compilation:
    """Parse and compile ``expression`` over the ``registry`` specs (see the module docstring)."""
    tree = parse_expression(expression, limits)
    compiler = _Compiler(registry)
    root = compiler.spec(tree)
    return compiler.compilation(canonical_json(to_data(tree)), limits, root)


def verify_compilation(compilation: Compilation, registry: Iterable[EventSpec]) -> None:
    """Recompile the recorded expression over ``registry``; any difference → ``DslError``."""
    if not isinstance(compilation, Compilation):
        raise DslError("expected a Compilation")
    try:
        expected_hash = content_hash(json.loads(compilation.expression))
    except (ValueError, TypeError) as exc:
        raise DslError(f"the recorded expression is not JSON: {exc}") from None
    if compilation.expression_hash != expected_hash:
        raise DslError("the recorded expression hash does not match the expression")
    again = compile_expression(compilation.expression, registry, compilation.limits)
    if again.expression != compilation.expression:
        raise DslError("the recorded expression is not in canonical form")
    if again.record() != compilation.record():
        raise DslError("the compilation does not match a recompilation of its expression")


# ======================================================================================
# Execution plan (the runner executes; plugins do not import infrastructure)
# ======================================================================================


@dataclass(frozen=True, slots=True)
class Hop:
    """One compiled spec, the provider serving it and the upstream specs its run needs."""

    spec: EventSpec
    provider: EventProvider
    upstream: tuple[EventSpec, ...]


def hops(compilation: Compilation, registry: Iterable[EventSpec]) -> tuple[Hop, ...]:
    """Each compiled spec (post-order) with its provider and exactly its declared upstream specs.

    One provider instance per operator serves all compiled specs of that operator; each hop is
    then run with ``infrastructure.event.run_events(hop.provider, hop.spec, request,
    upstream_specs=hop.upstream, ...)``.
    """
    registry = tuple(registry)
    verify_compilation(compilation, registry)
    known = {str(item.ref): item for item in registry}
    known.update((str(item.ref), item) for item in compilation.specs)
    by_operator: dict[str, list[EventSpec]] = {}
    for item in compilation.specs:
        by_operator.setdefault(json.loads(item.trigger)["operator"], []).append(item)
    providers = {op: _PROVIDERS[op](specs) for op, specs in by_operator.items()}
    return tuple(
        Hop(
            spec=item,
            provider=providers[json.loads(item.trigger)["operator"]],
            upstream=tuple(known[str(ref)] for ref in item.lineage),
        )
        for item in compilation.specs
    )

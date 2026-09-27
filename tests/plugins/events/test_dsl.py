"""Interaction DSL (ADR-0061): parsing / refusal, canonical compilation, hash stability, pins."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from core.domain.base import canonical_json, content_hash
from core.domain.specs import EventSpec
from plugins.events import (
    EventCoOccurrenceProvider,
    EventSequenceProvider,
    FeatureThresholdCrossProvider,
    StateSwitchProvider,
)
from plugins.events.dsl import (
    Compilation,
    CompileLimits,
    CountNode,
    DslError,
    PairNode,
    RefNode,
    compile_expression,
    hops,
    parse_expression,
    to_data,
    verify_compilation,
)
from tests.contract_version_support import at_pre_bump, built_at_pre_bump
from tests.fake_events import LAG, MINUTE, REGIME, X

CROSS_UP = FeatureThresholdCrossProvider.spec(X, Decimal("4.5"), "up", observable_lag=LAG)
CROSS_DOWN = FeatureThresholdCrossProvider.spec(X, Decimal("4.5"), "down", observable_lag=LAG)
SWITCH = StateSwitchProvider.spec(REGIME, observable_lag=LAG)
REGISTRY = (CROSS_UP, CROSS_DOWN, SWITCH)
LIMITS = CompileLimits(max_depth=4, max_nodes=16)
MIN_US = 60_000_000


def ref(spec: EventSpec) -> dict[str, Any]:
    return {"ref": str(spec.ref)}


def pair(op: str, a: dict[str, Any], b: dict[str, Any], within_us: int = MIN_US) -> dict[str, Any]:
    return {"op": op, "a": a, "b": b, "within_us": within_us}


def count(a: dict[str, Any], at_least: int = 2, within_us: int = 3 * MIN_US) -> dict[str, Any]:
    return {"op": "count", "a": a, "at_least": at_least, "within_us": within_us}


def _expression(cross_up: EventSpec, cross_down: EventSpec, switch: EventSpec) -> dict[str, Any]:
    """Every operator, a shared sub-expression, depth 4."""
    return pair(
        "and",
        pair("seq", count(ref(switch)), pair("not", ref(cross_up), ref(switch), 2 * MIN_US)),
        pair("and", count(ref(switch)), ref(cross_down)),
        5 * MIN_US,
    )


EXPRESSION = _expression(CROSS_UP, CROSS_DOWN, SWITCH)


def _compile(expression: Any = EXPRESSION, limits: CompileLimits = LIMITS) -> Compilation:
    return compile_expression(expression, REGISTRY, limits)


# ======================================================================================
# Parsing and refusal
# ======================================================================================


def test_a_valid_expression_parses_into_its_tree() -> None:
    tree = parse_expression(json.dumps(EXPRESSION), LIMITS)
    assert isinstance(tree, PairNode) and tree.op == "and" and tree.within_us == 5 * MIN_US
    assert isinstance(tree.a, PairNode) and isinstance(tree.a.a, CountNode)
    assert tree.a.a.a == RefNode(ref=SWITCH.ref) and tree.a.a.at_least == 2
    assert to_data(tree) == EXPRESSION
    assert parse_expression(EXPRESSION, LIMITS) == tree


GOOD_PAIR = pair("seq", ref(CROSS_UP), ref(SWITCH))


@pytest.mark.parametrize(
    ("expression", "match"),
    [
        ({"op": "or", "a": ref(CROSS_UP), "b": ref(SWITCH), "within_us": 1}, "unknown operator"),
        ({"op": 1, "a": ref(CROSS_UP), "b": ref(SWITCH), "within_us": 1}, "unknown operator"),
        ({**GOOD_PAIR, "extra": 1}, "exactly the fields"),
        ({key: v for key, v in GOOD_PAIR.items() if key != "within_us"}, "exactly the fields"),
        ({"op": "count", "a": ref(SWITCH), "within_us": 1}, "exactly the fields"),
        ({**count(ref(SWITCH)), "b": ref(CROSS_UP)}, "exactly the fields"),
        ({"ref": str(SWITCH.ref), "op": "seq"}, "exactly the field 'ref'"),
        ({"reference": str(SWITCH.ref)}, "unknown node"),
        ({}, "unknown node"),
        ([ref(SWITCH)], "JSON text or a mapping"),
        (json.dumps([ref(SWITCH)]), "must be a JSON object"),
        ({**GOOD_PAIR, "b": "event:x_cross_up@1.0.0"}, "must be a JSON object"),
        (pair("seq", ref(CROSS_UP), ref(SWITCH), 0), "within_us"),
        (pair("seq", ref(CROSS_UP), ref(SWITCH), -5), "within_us"),
        (pair("seq", ref(CROSS_UP), ref(SWITCH), True), "within_us"),
        ({**GOOD_PAIR, "within_us": "60"}, "within_us"),
        ({**GOOD_PAIR, "within_us": 1.5}, "within_us"),
        (count(ref(SWITCH), at_least=0), "at_least"),
        (count(ref(SWITCH), at_least=True), "at_least"),
        ({"ref": "feature:x@1.0.0"}, "event reference"),
        ({"ref": "event:x"}, "not a reference"),
        ({"ref": 5}, "event reference string"),
        ({"ref": "event:never_registered@1.0.0"}, "not a registered event spec"),
        (pair("seq", ref(SWITCH), ref(SWITCH)), "same definition"),
        (pair("and", count(ref(SWITCH)), count(ref(SWITCH))), "same definition"),
        (pair("not", ref(CROSS_UP), ref(CROSS_UP)), "same definition"),
        ("[1, 2", "not JSON"),
        ('{"ref": "event:x_cross_up@1.0.0", "ref": "event:regime_switch@1.0.0"}', "repeated"),
        ('{"op": "seq", "a": {"ref": "a"}, "b": {"ref": "b"}, "within_us": 1.0}', "must be ints"),
        ('{"op": "seq", "a": {"ref": "a"}, "b": {"ref": "b"}, "within_us": NaN}', "NaN"),
        (5, "JSON text or a mapping"),
    ],
)
def test_anything_outside_the_grammar_is_refused(expression: Any, match: str) -> None:
    with pytest.raises(DslError, match=match):
        _compile(expression)


def test_the_compile_limits_are_explicit_and_enforced() -> None:
    with pytest.raises(TypeError):
        CompileLimits()  # type: ignore[call-arg]  # no defaults
    with pytest.raises(TypeError):
        CompileLimits(max_depth=4)  # type: ignore[call-arg]
    for bad in (0, -1, True, 1.5):
        with pytest.raises(DslError):
            CompileLimits(max_depth=bad, max_nodes=4)  # type: ignore[arg-type]
        with pytest.raises(DslError):
            CompileLimits(max_depth=4, max_nodes=bad)  # type: ignore[arg-type]
    with pytest.raises(DslError, match="limits must be CompileLimits"):
        parse_expression(EXPRESSION, {"max_depth": 4, "max_nodes": 16})  # type: ignore[arg-type]
    # EXPRESSION: depth 4, 11 nodes.
    _compile(limits=CompileLimits(max_depth=4, max_nodes=11))
    with pytest.raises(DslError, match="max_depth=3"):
        _compile(limits=CompileLimits(max_depth=3, max_nodes=11))
    with pytest.raises(DslError, match="max_nodes=10"):
        _compile(limits=CompileLimits(max_depth=4, max_nodes=10))
    deep: dict[str, Any] = ref(SWITCH)
    for _ in range(50):
        deep = count(deep)
    with pytest.raises(DslError, match="max_depth"):
        _compile(deep)


def test_the_registry_must_be_unambiguous() -> None:
    with pytest.raises(DslError, match="twice"):
        compile_expression(GOOD_PAIR, (*REGISTRY, CROSS_UP), LIMITS)
    with pytest.raises(DslError, match="EventSpec"):
        compile_expression(GOOD_PAIR, ("event:x_cross_up@1.0.0",), LIMITS)  # type: ignore[arg-type]


# ======================================================================================
# Compilation: shape, canonical form, hash stability
# ======================================================================================


def _operator(spec: EventSpec) -> str:
    return str(json.loads(spec.trigger)["operator"])


def test_every_internal_node_compiles_to_an_ordinary_interaction_spec() -> None:
    compiled = _compile()
    ops = [_operator(spec) for spec in compiled.specs]
    # Post-order, the shared count(SWITCH) once: count, window_end + absence (not), seq,
    # co-occurrence (inner and), co-occurrence (root).
    assert ops == [
        "event_count",
        "event_window_end",
        "event_absence",
        "event_sequence",
        "event_co_occurrence",
        "event_co_occurrence",
    ]
    assert compiled.root == compiled.specs[-1]
    assert compiled.leaves == tuple(
        sorted((str(spec.ref), spec.content_hash()) for spec in REGISTRY)
    )
    known = {str(spec.ref): spec for spec in (*REGISTRY, *compiled.specs)}
    position = {str(spec.ref): index for index, spec in enumerate(compiled.specs)}
    for index, spec in enumerate(compiled.specs):
        trigger = json.loads(spec.trigger)
        assert spec.trigger == canonical_json(trigger)
        assert spec.version == "1.0.0" and spec.name.startswith("dsl_")
        upstream = [known[str(item)] for item in spec.lineage]
        assert upstream, "every compiled spec is an interaction"
        # Every upstream is bound verbatim by the <name> / <name>_hash convention.
        bound = {
            value: trigger[f"{key}_hash"]
            for key, value in trigger.items()
            if f"{key}_hash" in trigger
        }
        assert bound == {str(item.ref): item.content_hash() for item in upstream}
        # Upstream specs come earlier (post-order); inputs are exactly their union.
        assert all(position.get(str(item.ref), -1) < index for item in upstream)
        assert set(spec.features) == {f for item in upstream for f in item.features}
        assert set(spec.states) == {s for item in upstream for s in item.states}
        window_end = _operator(spec) == "event_window_end"
        assert spec.observable_lag == (2 * MINUTE if window_end else timedelta(0))


def test_the_not_node_is_window_end_then_absence() -> None:
    compiled = _compile(pair("not", ref(CROSS_UP), ref(SWITCH), 2 * MIN_US))
    end, absence = compiled.specs
    assert end.lineage == (CROSS_UP.ref,) and end.observable_lag == 2 * MINUTE
    assert absence.lineage == (end.ref, SWITCH.ref) and compiled.root == absence
    assert json.loads(absence.trigger)["window_us"] == 2 * MIN_US


def test_the_same_expression_always_compiles_to_the_same_specs() -> None:
    first, second = _compile(), _compile()
    assert first.record() == second.record()
    assert first.compilation_hash == second.compilation_hash
    reordered = json.dumps(EXPRESSION, sort_keys=True, indent=2)
    assert _compile(reordered).record() == first.record()
    assert first.expression == canonical_json(EXPRESSION)
    assert first.expression_hash == content_hash(EXPRESSION)


def test_and_operands_are_sorted_by_spec_hash_but_seq_and_not_keep_their_order() -> None:
    ab = _compile(pair("and", ref(CROSS_UP), ref(SWITCH)))
    ba = _compile(pair("and", ref(SWITCH), ref(CROSS_UP)))
    assert ab.expression_hash != ba.expression_hash
    assert ab.root.content_hash() == ba.root.content_hash()
    assert ab.record()["specs"] == ba.record()["specs"]
    left, right = ab.root.lineage
    by_ref = {spec.ref: spec.content_hash() for spec in REGISTRY}
    assert by_ref[left] < by_ref[right]
    for op in ("seq", "not"):
        forward = _compile(pair(op, ref(CROSS_UP), ref(SWITCH))).root
        backward = _compile(pair(op, ref(SWITCH), ref(CROSS_UP))).root
        assert forward.content_hash() != backward.content_hash()


def test_every_parameter_changes_the_compiled_hashes() -> None:
    roots = {
        _compile(expression).root.content_hash()
        for expression in (
            pair("seq", ref(CROSS_UP), ref(SWITCH)),
            pair("seq", ref(CROSS_UP), ref(SWITCH), 2 * MIN_US),
            pair("seq", ref(CROSS_DOWN), ref(SWITCH)),
            pair("and", ref(CROSS_UP), ref(SWITCH)),
            pair("not", ref(CROSS_UP), ref(SWITCH)),
            count(ref(SWITCH)),
            count(ref(SWITCH), at_least=3),
            count(ref(SWITCH), within_us=MIN_US),
        )
    }
    assert len(roots) == 8


def test_a_changed_leaf_spec_changes_every_hash_above_it() -> None:
    changed = FeatureThresholdCrossProvider.spec(
        X, Decimal("5"), "up", name=CROSS_UP.name, observable_lag=LAG
    )
    assert changed.ref == CROSS_UP.ref
    before = _compile()
    after = compile_expression(EXPRESSION, (changed, CROSS_DOWN, SWITCH), LIMITS)
    assert before.expression_hash == after.expression_hash
    assert before.root.content_hash() != after.root.content_hash()
    with pytest.raises(DslError, match="does not match a recompilation"):
        verify_compilation(before, (changed, CROSS_DOWN, SWITCH))


def test_a_bare_ref_compiles_to_no_spec() -> None:
    compiled = _compile(ref(SWITCH))
    assert compiled.specs == () and compiled.root == SWITCH
    assert hops(compiled, REGISTRY) == ()


#: Golden: a change here changes every stored DSL spec (hash stability across runs / versions).
#: Recorded at contract 2.0.0 (ADR-0061 landed before the 2.1.0 bump); checked on the 2.0.0 twins.
GOLDEN_COMPILATION_HASH_2_0_0 = "637ef43ef02a1f43b927bcf254d7617f66880b2d5ad280c2b2c5d02f3f104081"
GOLDEN_ROOT_HASH_2_0_0 = "db08263d35a11058bab35aaa7362a70eee2fa49ce90bafc8a875ecfa6dcc12fb"
#: Re-pinned at contract 2.1.0 (ADR-0052 §4): the registry specs and the compiled root are new
#: 2.1.0 objects, and the envelope is part of every content hash. This is the intended envelope
#: change only: the same compilation built at 2.0.0 still gives the 2.0.0 pins above.
GOLDEN_COMPILATION_HASH = "d5457212439e1a71ac83fa0dd2ed9daff6f8207e46f02a61f77c608c7ba697d3"
GOLDEN_ROOT_HASH = "7abb8b4e5ee2dcec5e2343e265cb542b38665553d9fbd7bc8e78dd76d084fd52"


def test_the_compiled_hashes_are_pinned() -> None:
    compiled = _compile()
    assert compiled.compilation_hash == GOLDEN_COMPILATION_HASH
    assert compiled.root.content_hash() == GOLDEN_ROOT_HASH


def test_the_compiled_hashes_at_2_0_0_are_unchanged() -> None:
    with built_at_pre_bump():
        registry = tuple(at_pre_bump(spec) for spec in REGISTRY)
        compiled = compile_expression(_expression(*registry), registry, LIMITS)
    assert compiled.root.schema_version == "2.0.0"
    assert compiled.compilation_hash == GOLDEN_COMPILATION_HASH_2_0_0
    assert compiled.root.content_hash() == GOLDEN_ROOT_HASH_2_0_0


# ======================================================================================
# Re-verification and execution plan
# ======================================================================================


def test_a_compilation_is_re_verifiable() -> None:
    compiled = _compile()
    verify_compilation(compiled, REGISTRY)
    with pytest.raises(DslError, match="expression hash"):
        verify_compilation(replace(compiled, expression_hash=content_hash("x")), REGISTRY)
    with pytest.raises(DslError, match="does not match a recompilation"):
        verify_compilation(replace(compiled, specs=compiled.specs[:-1]), REGISTRY)
    other = _compile(GOOD_PAIR)
    with pytest.raises(DslError, match="does not match a recompilation"):
        verify_compilation(replace(compiled, root=other.root), REGISTRY)
    with pytest.raises(DslError, match="max_nodes=10"):
        verify_compilation(replace(compiled, limits=CompileLimits(4, 10)), REGISTRY)
    spaced = json.dumps(EXPRESSION, indent=1)
    with pytest.raises(DslError, match="canonical form"):
        verify_compilation(
            replace(compiled, expression=spaced, expression_hash=content_hash(EXPRESSION)),
            REGISTRY,
        )
    with pytest.raises(DslError, match="not a registered"):
        verify_compilation(compiled, (CROSS_UP, SWITCH))


def test_hops_name_the_provider_and_exactly_the_declared_upstream() -> None:
    compiled = _compile()
    plan = hops(compiled, REGISTRY)
    assert [hop.spec for hop in plan] == list(compiled.specs)
    for hop in plan:
        assert hop.provider.descriptor.supports(hop.spec.ref, hop.spec.content_hash())
        assert tuple(item.ref for item in hop.upstream) == hop.spec.lineage
    with pytest.raises(DslError):
        hops(replace(compiled, specs=compiled.specs[:-1]), REGISTRY)


# ======================================================================================
# Existing interaction specs are unchanged (ADR-0061: not a breaking change)
# ======================================================================================


def test_existing_interaction_specs_and_hashes_are_unchanged() -> None:
    # Pinned at contract 2.0.0: the specs rebuilt as the 2.0.0 code built them (ADR-0052 §4).
    with built_at_pre_bump():
        cross_up, switch = at_pre_bump(CROSS_UP), at_pre_bump(SWITCH)
        sequence = EventSequenceProvider.spec(cross_up, switch, 3 * MINUTE, observable_lag=LAG)
        co_occur = EventCoOccurrenceProvider.spec(cross_up, switch, MINUTE, observable_lag=LAG)
    assert cross_up.content_hash() == (
        "80db63d34f5ad9f06cdcb995b946d3b729f0f2d9ca7af15c75ce069a5fbe1aec"
    )
    assert switch.content_hash() == (
        "1e34d438767222513c28256fb92f2aab3914dcc9ab19620d9b4a27a6175b350b"
    )
    assert sequence.content_hash() == (
        "6e4bb4fa5dbef356d9c8305760c4da67b58ba21c4510c7a3eff3149c19423bb5"
    )
    assert co_occur.content_hash() == (
        "934d1c77a283bdd2be96cb7ee8ef7cb11cd08dcd8fb3a455a00716050eb31558"
    )
    assert sequence.name == "x_cross_up_then_regime_switch"
    # The same specs built now are 2.1.0 (ADR-0052 M2; the envelope is in every content hash).
    now_sequence = EventSequenceProvider.spec(CROSS_UP, SWITCH, 3 * MINUTE, observable_lag=LAG)
    now_co_occur = EventCoOccurrenceProvider.spec(CROSS_UP, SWITCH, MINUTE, observable_lag=LAG)
    assert (
        CROSS_UP.content_hash(),
        SWITCH.content_hash(),
        now_sequence.content_hash(),
        now_co_occur.content_hash(),
    ) == (
        "6579b3f26b2d3767c97acf4acc3d2b6a79c8007b5f2846012f94965c97f18de6",
        "f9738dcbc9823a16e6484522c87f99dbb16b425f81e1637a34542d8bdbc9677f",
        "805b92185be9e55b08b672122b8e1b700814d46e5f7783803009636e5ebf01b4",
        "8cde62efeb31103c45d9e738baefc99ab5f82a2e8cca6ee12aabc8c88c1e44c8",
    )
    assert co_occur.name == "x_cross_up_with_regime_switch"

"""ADR-0103 §1: the ``hlens.p7.plan@1.0.0`` record, its reserved ``repro.params`` key and the
node-output dependency hashes."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from core.domain.base import canonical_json
from research.hypotheses.p7_binding import (
    P7_PLAN_FORMAT,
    P7_PLAN_KEY,
    P7PlanNodeRecord,
    P7PlanRecord,
    P7PlanRecordError,
    bind_experiment,
    hypothesis_p7_plan_hash,
    p7_plan_condition,
    plan_dependency_hashes,
    recorded_p7_plan,
    strategy_params,
    universe_manifest_hashes,
    with_p7_plan,
    with_plan_dependency_hashes,
)
from research.hypotheses.typed_plan import PlanLimits, parse_plan_json
from research.hypotheses.typed_plan_compiler import P7_OPERATOR_ALLOWLIST
from tests.research.hypotheses.p7_fixtures import (
    SOURCE_A,
    compile_nodes,
    experiment_for,
    feature_chain,
    feature_chain_nodes,
    hypothesis_for,
    spec_input,
    strategy_chain,
)
from tests.test_universe_contracts import manifest


def _record() -> P7PlanRecord:
    return P7PlanRecord.from_compiled(feature_chain()[0])


def _mutated(**changes: Any) -> str:
    """The canonical text of the record payload with ``changes`` applied (top level)."""
    payload = _record().payload()
    payload.update(changes)
    return canonical_json(payload)


def test_format_and_key_are_the_reserved_namespaced_id() -> None:
    assert P7_PLAN_FORMAT == "hlens.p7.plan@1.0.0"
    assert P7_PLAN_KEY == P7_PLAN_FORMAT


def test_from_compiled_records_the_plan_identity_and_every_node() -> None:
    compiled, _ = feature_chain()
    record = P7PlanRecord.from_compiled(compiled)

    assert record.plan_hash == compiled.plan_hash == compiled.plan.content_hash()
    assert record.plan_format == compiled.plan.schema_version == "1.3.0"
    assert record.root == "prod"
    assert record.compiler == compiled.compiler
    assert record.allowlist_hash == compiled.allowlist_hash
    assert record.universe_manifest_hashes == ()
    assert [node.node_id for node in record.nodes] == ["diff", "prod"]
    for recorded, node in zip(record.nodes, compiled.nodes, strict=True):
        assert recorded.definition == node.definition
        assert recorded.spec_ref == str(node.spec.ref)
        assert recorded.spec_hash == node.spec.content_hash()
        assert recorded.provider_key == node.implementation.provider_key
        assert recorded.implementation_hash == node.implementation.implementation_hash
    assert record.nodes[0].definition == "p7.transformation.difference@1.0.0"


def test_record_is_deterministic_and_bound_to_the_allowlist() -> None:
    first, second = _record(), _record()
    assert first.text() == second.text()

    subset = {
        key: value
        for key, value in P7_OPERATOR_ALLOWLIST.items()
        if key != "p7.transformation.rank_ts@1.0.0"
    }
    other, _ = compile_nodes(feature_chain_nodes(), "prod", allowlist=subset)
    assert P7PlanRecord.from_compiled(other).allowlist_hash != first.allowlist_hash


def test_from_compiled_refuses_a_non_runnable_or_foreign_value() -> None:
    compiled, _ = feature_chain()
    unsealed = dataclasses.replace(compiled)  # the seal is not carried over
    assert unsealed.runnable is False
    with pytest.raises(P7PlanRecordError, match="runnable"):
        P7PlanRecord.from_compiled(unsealed)
    with pytest.raises(P7PlanRecordError, match="CompiledPlan"):
        P7PlanRecord.from_compiled(compiled.plan)  # type: ignore[arg-type]


def test_text_is_the_canonical_json_of_the_payload_and_round_trips() -> None:
    record = _record()
    text = record.text()

    assert text == canonical_json(record.payload())
    assert set(json.loads(text)) == {
        "plan_hash",
        "plan_format",
        "root",
        "compiler",
        "allowlist_hash",
        "universe_manifest_hashes",
        "nodes",
    }
    assert P7PlanRecord.from_payload(json.loads(text)) == record
    assert recorded_p7_plan({P7_PLAN_KEY: text}) == record


def test_with_p7_plan_adds_the_record_and_keeps_strategy_params() -> None:
    record = _record()
    params = with_p7_plan({"window": 5, "mode": "x"}, record)

    assert params == {"window": 5, "mode": "x", P7_PLAN_KEY: record.text()}
    assert recorded_p7_plan(params) == record
    assert strategy_params(params) == {"window": 5, "mode": "x"}
    assert strategy_params({"window": 5}) == {"window": 5}


def test_with_p7_plan_refuses_an_occupied_reserved_key_and_does_not_mutate() -> None:
    params = {P7_PLAN_KEY: "shadow"}
    with pytest.raises(P7PlanRecordError, match="reserved"):
        with_p7_plan(params, _record())
    assert params == {P7_PLAN_KEY: "shadow"}
    with pytest.raises(P7PlanRecordError, match="P7PlanRecord"):
        with_p7_plan({}, "not a record")  # type: ignore[arg-type]


def test_params_without_the_key_have_no_record() -> None:
    assert recorded_p7_plan({}) is None
    assert recorded_p7_plan({"window": 5}) is None


@pytest.mark.parametrize(
    "value",
    [
        7,
        "not json",
        "[]",
        "{}",
        json.dumps(json.loads(_mutated()), indent=2),  # same data, not canonical text
        json.dumps(json.loads(_mutated()), sort_keys=False, separators=(", ", ": ")),
        _mutated(extra="x"),
        _mutated(plan_hash="A" * 64),
        _mutated(plan_hash="1" * 63),
        _mutated(plan_format="9.9.9"),
        _mutated(root="missing"),
        _mutated(root=""),
        _mutated(compiler="no-version"),
        _mutated(allowlist_hash=None),
        _mutated(universe_manifest_hashes="x"),
        _mutated(universe_manifest_hashes=["b" * 64, "a" * 64]),  # not sorted
        _mutated(universe_manifest_hashes=["a" * 64, "a" * 64]),  # not unique
        _mutated(universe_manifest_hashes=["short"]),
        _mutated(nodes=[]),
        _mutated(nodes="x"),
    ],
)
def test_recorded_p7_plan_is_strict(value: object) -> None:
    with pytest.raises(P7PlanRecordError):
        recorded_p7_plan({P7_PLAN_KEY: value})  # type: ignore[dict-item]


def _with_node(index: int, **changes: Any) -> str:
    payload = _record().payload()
    payload["nodes"][index].update(changes)
    return canonical_json(payload)


@pytest.mark.parametrize(
    "value",
    [
        _with_node(0, spec_hash="0" * 63),
        _with_node(0, implementation_hash=1),
        _with_node(0, definition="no-version"),
        _with_node(0, definition="a@b@c"),
        _with_node(0, provider_key="x"),
        _with_node(0, spec_ref="not a ref"),
        _with_node(0, spec_ref="hypothesis:h@1.0.0"),  # not a feature / event / strategy ref
        _with_node(0, node_id=""),
        _with_node(1, node_id="diff"),  # duplicate node id
        _with_node(1, spec_ref=_record().nodes[0].spec_ref),  # duplicate spec ref
    ],
)
def test_recorded_p7_plan_is_strict_per_node(value: str) -> None:
    with pytest.raises(P7PlanRecordError):
        recorded_p7_plan({P7_PLAN_KEY: value})


def test_recorded_p7_plan_refuses_missing_or_extra_node_fields() -> None:
    payload = _record().payload()
    del payload["nodes"][0]["spec_hash"]
    with pytest.raises(P7PlanRecordError, match="exactly its fields"):
        recorded_p7_plan({P7_PLAN_KEY: canonical_json(payload)})
    payload = _record().payload()
    payload["nodes"][0]["extra"] = "x"
    with pytest.raises(P7PlanRecordError, match="exactly its fields"):
        recorded_p7_plan({P7_PLAN_KEY: canonical_json(payload)})


def test_record_values_are_validated_on_construction() -> None:
    node = _record().nodes[0]
    with pytest.raises(P7PlanRecordError):
        dataclasses.replace(node, spec_hash="nope")
    with pytest.raises(P7PlanRecordError, match="non-empty"):
        dataclasses.replace(_record(), nodes=())
    with pytest.raises(P7PlanRecordError, match="P7PlanNodeRecord"):
        dataclasses.replace(_record(), nodes=(node, "x"))  # type: ignore[arg-type]
    assert isinstance(node, P7PlanNodeRecord)


def test_plan_dependency_hashes_cover_every_node_output() -> None:
    compiled, _ = strategy_chain()
    record = P7PlanRecord.from_compiled(compiled)

    assert plan_dependency_hashes(record) == {
        str(node.spec.ref): node.spec.content_hash() for node in compiled.nodes
    }
    assert list(plan_dependency_hashes(record)) == [str(n.spec.ref) for n in compiled.nodes]


def test_with_plan_dependency_hashes_merges_without_mutating_and_is_idempotent() -> None:
    record = _record()
    existing = {"hypothesis:h@1.0.0": "a" * 64}

    merged = with_plan_dependency_hashes(existing, record)

    assert existing == {"hypothesis:h@1.0.0": "a" * 64}
    assert merged == {**existing, **plan_dependency_hashes(record)}
    assert with_plan_dependency_hashes(merged, record) == merged


def test_with_plan_dependency_hashes_refuses_a_conflicting_hash() -> None:
    record = _record()
    ref = record.nodes[0].spec_ref
    with pytest.raises(P7PlanRecordError, match="already bound"):
        with_plan_dependency_hashes({ref: "0" * 64}, record)
    with pytest.raises(P7PlanRecordError, match="P7PlanRecord"):
        plan_dependency_hashes("x")  # type: ignore[arg-type]


def test_plan_condition_names_the_plan_hash() -> None:
    compiled, _ = feature_chain()
    hypothesis = hypothesis_for(compiled)

    assert p7_plan_condition(compiled.plan_hash) == f"p7_plan = {compiled.plan_hash}"
    assert hypothesis_p7_plan_hash(hypothesis) == compiled.plan_hash
    none = hypothesis.model_copy(update={"conditions": ("strategy = strategy:s@1.0.0",)})
    assert hypothesis_p7_plan_hash(none) is None
    with pytest.raises(P7PlanRecordError, match="SHA-256"):
        p7_plan_condition("zz")
    twice = hypothesis.model_copy(
        update={"conditions": (p7_plan_condition("a" * 64), p7_plan_condition("b" * 64))}
    )
    with pytest.raises(P7PlanRecordError, match="at most one"):
        hypothesis_p7_plan_hash(twice)
    bad = hypothesis.model_copy(update={"conditions": ("p7_plan = nothex",)})
    with pytest.raises(P7PlanRecordError, match="SHA-256"):
        hypothesis_p7_plan_hash(bad)


def test_universe_manifest_hashes_come_from_the_plan_cross_sectional_nodes() -> None:
    universe = manifest()
    reference = f"research_dataset:{universe.dataset.table}@{universe.dataset.snapshot_id}"
    payload = {
        "schema_version": "1.3.0",
        "root": "xs",
        "nodes": [
            {
                "id": "xs",
                "operator": "transformation",
                "inputs": [spec_input(SOURCE_A)],
                "parameters": {
                    "transform": "rank_cs",
                    "universe": reference,
                    "universe_hash": universe.content_hash(),
                },
            }
        ],
    }
    plan = parse_plan_json(
        json.dumps(payload, separators=(",", ":")),
        limits=PlanLimits(max_depth=2, max_nodes=2, max_json_bytes=4096, max_parameters_per_node=4),
    )
    assert universe_manifest_hashes(plan) == (universe.content_hash(),)
    assert universe_manifest_hashes(feature_chain()[0].plan) == ()


def test_bind_experiment_adds_the_record_and_every_output_and_changes_the_hash() -> None:
    compiled, _ = feature_chain()
    record = P7PlanRecord.from_compiled(compiled)
    hypothesis = hypothesis_for(compiled)
    unbound = experiment_for(compiled, hypothesis, with_record=False, bind_outputs=False)

    bound = bind_experiment(unbound, record)

    assert recorded_p7_plan(bound.repro.params) == record
    assert recorded_p7_plan(unbound.repro.params) is None
    for ref, digest in plan_dependency_hashes(record).items():
        assert bound.repro.dependency_hashes[ref] == digest
    assert bound.experiment_hash != unbound.experiment_hash
    assert (bound.name, bound.version, bound.created_at) == (
        unbound.name,
        unbound.version,
        unbound.created_at,
    )
    with pytest.raises(P7PlanRecordError, match="reserved"):
        bind_experiment(bound, record)
    with pytest.raises(P7PlanRecordError, match="exact ExperimentSpec"):
        bind_experiment(hypothesis, record)  # type: ignore[arg-type]


def test_run_inputs_strategy_params_strip_the_p7_plan_record() -> None:
    """The run-inputs helper used by P11 authority / evolution strips the P7 record too."""
    from research.experiments import run_inputs

    assert run_inputs.P7_PLAN_PARAM_KEY == P7_PLAN_KEY
    params: dict[str, str | int | float | bool] = {
        "lookback": 5,
        P7_PLAN_KEY: "{}",
        run_inputs.RUN_INPUTS_KEY: "{}",
    }
    assert run_inputs.strategy_params(params) == {"lookback": 5}

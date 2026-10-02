"""ADR-0103 §2: generated PREPARE evidence is accepted by the audit journal, and the
cross-check refuses evidence that is not the compiled plan's / batch's."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.domain.research import ExperimentSpec, Hypothesis
from research.hypotheses.p7_binding import P7_PLAN_KEY, P7PlanRecord, p7_plan_condition
from research.hypotheses.p7_evidence import (
    P7EvidenceRefused,
    cross_check_admission_evidence,
    experiment_evidence,
    inputs_evidence,
    outputs_evidence,
)
from research.hypotheses.plan_bindings import (
    produce_lowered_output_bindings,
    validate_complete_experiment_bindings,
)
from research.hypotheses.typed_plan_audit import (
    PlanAdmissionEvidence,
    PlanAdmissionJournal,
    RoundStartedIdentity,
)
from research.hypotheses.typed_plan_compiler import CompiledPlan
from research.hypotheses.typed_plan_resolver import DirectReferenceResolution
from tests.research.hypotheses.p7_fixtures import (
    compile_nodes,
    cross_sectional_nodes,
    experiment_for,
    feature_chain,
    hypothesis_for,
    strategy_chain,
    temporal_nodes,
)
from tests.test_universe_contracts import manifest


@dataclasses.dataclass
class Case:
    compiled: CompiledPlan
    resolution: DirectReferenceResolution
    hypotheses: list[Hypothesis]
    experiments: list[ExperimentSpec]

    def kwargs(self) -> dict[str, Any]:
        return {
            "compiled": self.compiled,
            "inputs": inputs_evidence(self.resolution),
            "outputs": outputs_evidence(self.compiled),
            "operators": self.compiled.operator_evidence(),
            "experiment_specs": self.experiments,
            "experiment_evidence_items": experiment_evidence(self.experiments),
            "hypotheses": self.hypotheses,
        }


def _case(count: int = 1) -> Case:
    compiled, resolution = feature_chain()
    hypotheses = [hypothesis_for(compiled, f"h_plan_{index}") for index in range(count)]
    experiments = [experiment_for(compiled, item) for item in hypotheses]
    return Case(compiled, resolution, hypotheses, experiments)


def _refused(code: str, **overrides: Any) -> P7EvidenceRefused:
    case = _case()
    kwargs = {**case.kwargs(), **overrides}
    with pytest.raises(P7EvidenceRefused) as caught:
        cross_check_admission_evidence(**kwargs)
    assert caught.value.code == code, str(caught.value)
    return caught.value


def _tweaked(item: PlanAdmissionEvidence, **value_changes: Any) -> PlanAdmissionEvidence:
    data = item.payload()["data"]
    data["value"].update(value_changes)
    return PlanAdmissionEvidence.from_data(data)


def _identity(item: PlanAdmissionEvidence, identity: object) -> PlanAdmissionEvidence:
    data = item.payload()["data"]
    data["identity"] = identity
    return PlanAdmissionEvidence.from_data(data)


# --- the generated evidence -------------------------------------------------------------------


def test_inputs_evidence_has_one_item_per_direct_reference_in_plan_order() -> None:
    compiled, resolution = feature_chain()
    items = inputs_evidence(resolution)

    assert [item.payload()["data"]["identity"] for item in items] == [
        {"node_id": "diff", "input_index": 0},
        {"node_id": "prod", "input_index": 1},
    ]
    first = items[0].payload()["data"]["value"]
    assert first == {
        "ref": str(resolution.inputs[0].spec.ref),
        "content_hash": resolution.inputs[0].spec.content_hash(),
    }
    assert all(isinstance(item, PlanAdmissionEvidence) for item in items)
    assert compiled.runnable


def test_outputs_evidence_has_one_item_per_node_with_the_lowered_spec() -> None:
    compiled, _ = feature_chain()
    items = outputs_evidence(compiled)

    assert [item.payload()["data"]["identity"]["node_id"] for item in items] == ["diff", "prod"]
    for item, node in zip(items, compiled.nodes, strict=True):
        value = item.payload()["data"]["value"]
        assert value == {
            "definition": node.definition,
            "ref": str(node.spec.ref),
            "content_hash": node.spec.content_hash(),
        }


def test_cross_sectional_outputs_carry_the_pinned_universe_manifest_hash() -> None:
    """ADR-0103 §2 / 修订 1: a cross-sectional node's universe manifest hash enters the
    evidence (and the plan record); a single-series node carries none."""
    universe = manifest()
    compiled, _ = compile_nodes(cross_sectional_nodes(universe), "xs", universes=(universe,))
    (item,) = outputs_evidence(compiled)
    value = item.payload()["data"]["value"]
    assert value["universe_manifest_hash"] == universe.content_hash()
    assert value["ref"] == str(compiled.nodes[0].spec.ref)
    record = P7PlanRecord.from_compiled(compiled)
    assert record.universe_manifest_hashes == (universe.content_hash(),)
    plain, _ = feature_chain()
    assert all(
        "universe_manifest_hash" not in out.payload()["data"]["value"]
        for out in outputs_evidence(plain)
    )


def test_experiment_evidence_binds_hypothesis_and_plan() -> None:
    case = _case(2)
    items = experiment_evidence(case.experiments)

    assert len(items) == 2
    for item, experiment, hypothesis in zip(items, case.experiments, case.hypotheses, strict=True):
        data = item.payload()["data"]
        assert data["identity"] == {"experiment_hash": experiment.experiment_hash}
        assert data["value"]["hypothesis_ref"] == str(hypothesis.ref)
        assert data["value"]["hypothesis_hash"] == hypothesis.content_hash()
        assert data["value"]["p7_plan_hash"] == case.compiled.plan_hash


def test_experiment_evidence_without_a_plan_record_carries_none() -> None:
    compiled, _ = feature_chain()
    experiment = experiment_for(compiled, hypothesis_for(compiled), with_record=False)
    value = experiment_evidence([experiment])[0].payload()["data"]["value"]
    assert value["p7_plan_hash"] is None


def test_evidence_builders_refuse_wrong_inputs() -> None:
    compiled, _ = feature_chain()
    with pytest.raises(P7EvidenceRefused, match="invalid_resolution"):
        inputs_evidence("x")  # type: ignore[arg-type]
    with pytest.raises(P7EvidenceRefused, match="wrong_type"):
        outputs_evidence(compiled.plan)  # type: ignore[arg-type]
    with pytest.raises(P7EvidenceRefused, match="not_runnable"):
        outputs_evidence(dataclasses.replace(compiled))
    with pytest.raises(P7EvidenceRefused, match="invalid_sequence"):
        experiment_evidence("x")  # type: ignore[arg-type]
    with pytest.raises(P7EvidenceRefused, match="wrong_type"):
        experiment_evidence([object()])  # type: ignore[list-item]


def test_experiment_evidence_refuses_an_invalid_plan_record() -> None:
    compiled, _ = feature_chain()
    hypothesis = hypothesis_for(compiled)
    experiment = experiment_for(compiled, hypothesis, with_record=False)
    repro = experiment.repro.model_copy(
        update={"params": {**experiment.repro.params, P7_PLAN_KEY: "{}"}}
    )
    broken = experiment.model_copy(update={"repro": repro})
    with pytest.raises(P7EvidenceRefused) as caught:
        experiment_evidence([broken])
    assert caught.value.code == "plan_record_invalid"


# --- PREPARE accepts the generated evidence ---------------------------------------------------


@pytest.mark.parametrize("chain", [feature_chain, strategy_chain])
def test_generated_evidence_is_accepted_by_prepare(
    tmp_path: Path, chain: Callable[[], tuple[CompiledPlan, DirectReferenceResolution]]
) -> None:
    compiled, resolution = chain()
    hypotheses = [hypothesis_for(compiled, "h_one"), hypothesis_for(compiled, "h_two")]
    experiments = [experiment_for(compiled, item) for item in hypotheses]
    case = Case(compiled, resolution, hypotheses, experiments)
    cross_check_admission_evidence(**case.kwargs())

    journal = PlanAdmissionJournal(tmp_path / "plan_admission.jsonl", loop_id="loop", create=True)
    prepared = journal.prepare(
        round=RoundStartedIdentity("loop", 0, 1, "a" * 64),
        plan=compiled.plan,
        compiler=compiled.compiler_evidence(),
        operators=compiled.operator_evidence(),
        providers=compiled.provider_evidence(),
        inputs=inputs_evidence(resolution),
        outputs=outputs_evidence(compiled),
        experiment_specs=experiment_evidence(experiments),
        hypotheses=hypotheses,
        ledger_baseline_seq=0,
        ledger_baseline_hash="0" * 64,
    )

    assert journal.pending is prepared
    assert [item.content_hash for item in prepared.outputs] == [
        item.content_hash for item in outputs_evidence(compiled)
    ]
    assert len(prepared.experiment_specs) == 2 and len(prepared.inputs) == len(resolution.inputs)
    # The same evidence also survives a strict reopen of the journal (replay re-validates it).
    reopened = PlanAdmissionJournal(tmp_path / "plan_admission.jsonl", loop_id="loop")
    assert reopened.pending is not None
    assert reopened.pending.transaction_id == prepared.transaction_id


def test_temporal_event_plan_evidence_is_consistent() -> None:
    compiled, resolution = compile_nodes(temporal_nodes(), "seq")
    hypothesis = hypothesis_for(compiled)
    experiment = experiment_for(compiled, hypothesis)
    cross_check_admission_evidence(
        compiled=compiled,
        inputs=inputs_evidence(resolution),
        outputs=outputs_evidence(compiled),
        operators=compiled.operator_evidence(),
        experiment_specs=[experiment],
        experiment_evidence_items=experiment_evidence([experiment]),
        hypotheses=[hypothesis],
    )


def test_complete_binding_validator_accepts_the_same_experiment() -> None:
    """The recorded dependency hashes satisfy the existing plan_bindings validator."""
    case = _case()
    experiment = case.experiments[0]
    outputs = produce_lowered_output_bindings(
        experiment_hash=experiment.experiment_hash,
        plan=case.compiled.plan,
        specs_by_node=case.compiled.specs_by_node(),
    )
    bindings = validate_complete_experiment_bindings(
        experiment_specs=[experiment],
        hypotheses=case.hypotheses,
        plans_by_experiment={experiment.experiment_hash: case.compiled.plan},
        lowered_outputs=outputs,
    )
    assert len(bindings) == 1 and len(bindings[0].outputs) == len(case.compiled.nodes)


def test_cross_check_passes_for_a_consistent_batch() -> None:
    cross_check_admission_evidence(**_case(3).kwargs())


# --- outputs / operators / inputs ⇔ the compiled plan -----------------------------------------


def test_outputs_missing_extra_duplicate_mismatch_order() -> None:
    compiled, _ = feature_chain()
    outputs = list(outputs_evidence(compiled))
    other = outputs_evidence(strategy_chain()[0])[0]

    _refused("output_missing", outputs=outputs[:1])
    _refused("output_extra", outputs=[*outputs, other])
    _refused("output_duplicate", outputs=[*outputs, outputs[0]])
    _refused("output_mismatch", outputs=[_tweaked(outputs[0], content_hash="0" * 64), outputs[1]])
    _refused("output_order", outputs=[outputs[1], outputs[0]])
    _refused("output_invalid", outputs=["x"])
    _refused("output_invalid", outputs="x")


def test_operators_missing_extra_duplicate_mismatch_order() -> None:
    compiled, _ = feature_chain()
    operators = list(compiled.operator_evidence())
    other = strategy_chain()[0].operator_evidence()[0]

    _refused("operator_missing", operators=operators[:1])
    _refused("operator_extra", operators=[*operators, other])
    _refused("operator_duplicate", operators=[*operators, operators[0]])
    _refused(
        "operator_mismatch",
        operators=[_tweaked(operators[0], implementation_hash="0" * 64), operators[1]],
    )
    _refused(
        "operator_mismatch",
        operators=[_tweaked(operators[0], provider_key="plugin:fake@1.0.0"), operators[1]],
    )
    _refused("operator_order", operators=[operators[1], operators[0]])


def test_inputs_missing_extra_duplicate_mismatch_order() -> None:
    case = _case()
    inputs = list(inputs_evidence(case.resolution))
    extra = _identity(inputs[0], {"node_id": "prod", "input_index": 5})

    _refused("input_missing", inputs=inputs[:1])
    _refused("input_extra", inputs=[*inputs, extra])
    _refused("input_duplicate", inputs=[*inputs, inputs[0]])
    _refused("input_mismatch", inputs=[_tweaked(inputs[0], content_hash="0" * 64), inputs[1]])
    _refused("input_order", inputs=[inputs[1], inputs[0]])


# --- experiment_specs ⇔ hypotheses ------------------------------------------------------------


def test_batch_shape_is_checked() -> None:
    case = _case(2)
    _refused("empty_batch", experiment_specs=[], hypotheses=[], experiment_evidence_items=[])
    with pytest.raises(P7EvidenceRefused) as caught:
        cross_check_admission_evidence(**{**case.kwargs(), "hypotheses": case.hypotheses[:1]})
    assert caught.value.code == "batch_cardinality_mismatch"
    _refused("invalid_sequence", experiment_specs="x")
    _refused("invalid_sequence", hypotheses="x")


def test_experiment_evidence_items_must_match_the_specs() -> None:
    case = _case(2)
    items = list(experiment_evidence(case.experiments))
    with pytest.raises(P7EvidenceRefused) as caught:
        cross_check_admission_evidence(**{**case.kwargs(), "experiment_evidence_items": items[:1]})
    assert caught.value.code == "experiment_missing"
    for override, code in (
        ([*items, items[0]], "experiment_duplicate"),
        ([_tweaked(items[0], hypothesis_hash="0" * 64), items[1]], "experiment_mismatch"),
        ([items[1], items[0]], "experiment_order"),
    ):
        with pytest.raises(P7EvidenceRefused) as caught:
            cross_check_admission_evidence(
                **{**case.kwargs(), "experiment_evidence_items": override}
            )
        assert caught.value.code == code


def test_an_unknown_hypothesis_is_refused() -> None:
    case = _case(2)
    stranger = hypothesis_for(case.compiled, "h_stranger")
    kwargs = {**case.kwargs(), "hypotheses": [case.hypotheses[0], stranger]}
    with pytest.raises(P7EvidenceRefused) as caught:
        cross_check_admission_evidence(**kwargs)
    assert caught.value.code == "unknown_hypothesis"


def test_two_experiments_binding_one_hypothesis_is_refused() -> None:
    case = _case(2)
    twin = experiment_for(case.compiled, case.hypotheses[0])
    twin = twin.model_copy(
        update={"name": "experiment_twin", "repro": twin.repro.model_copy(update={"seeds": (1,)})}
    )  # a distinct experiment hash binding the same hypothesis
    experiments = [case.experiments[0], twin]
    kwargs = {
        **case.kwargs(),
        "experiment_specs": experiments,
        "experiment_evidence_items": experiment_evidence(experiments),
    }
    with pytest.raises(P7EvidenceRefused) as caught:
        cross_check_admission_evidence(**kwargs)
    assert caught.value.code == "hypothesis_used_more_than_once"


def test_an_experiment_whose_hypothesis_is_not_in_the_batch_is_refused() -> None:
    case = _case(2)
    renamed = hypothesis_for(case.compiled, "h_unbound")
    kwargs = {**case.kwargs(), "hypotheses": [case.hypotheses[0], renamed]}
    with pytest.raises(P7EvidenceRefused) as caught:
        cross_check_admission_evidence(**kwargs)
    assert caught.value.code == "unknown_hypothesis"


def test_a_duplicated_hypothesis_is_refused() -> None:
    case = _case(2)
    kwargs = {**case.kwargs(), "hypotheses": [case.hypotheses[0], case.hypotheses[0]]}
    with pytest.raises(P7EvidenceRefused) as caught:
        cross_check_admission_evidence(**kwargs)
    assert caught.value.code == "duplicate_hypothesis"


def test_a_hypothesis_hash_the_experiment_did_not_bind_is_refused() -> None:
    case = _case()
    edited = case.hypotheses[0].model_copy(update={"statement": "edited after binding"})
    with pytest.raises(P7EvidenceRefused) as caught:
        cross_check_admission_evidence(**{**case.kwargs(), "hypotheses": [edited]})
    assert caught.value.code == "hypothesis_hash_mismatch"


def test_a_wrong_hypothesis_type_is_refused() -> None:
    _refused("wrong_type", hypotheses=["x"])


# --- hypothesis condition / plan record / dependency hashes -----------------------------------


@pytest.mark.parametrize(
    ("conditions", "code"),
    [
        ((), "hypothesis_plan_condition_missing"),
        ((p7_plan_condition("b" * 64),), "hypothesis_plan_condition_mismatch"),
        (("p7_plan = nothex",), "hypothesis_plan_condition_invalid"),
    ],
)
def test_the_hypothesis_must_name_the_compiled_plan(conditions: tuple[str, ...], code: str) -> None:
    case = _case()
    bad = case.hypotheses[0].model_copy(update={"conditions": conditions})
    experiment = experiment_for(case.compiled, bad)
    with pytest.raises(P7EvidenceRefused) as caught:
        cross_check_admission_evidence(
            **{
                **case.kwargs(),
                "hypotheses": [bad],
                "experiment_specs": [experiment],
                "experiment_evidence_items": experiment_evidence([experiment]),
            }
        )
    assert caught.value.code == code


def test_an_experiment_without_the_plan_record_is_refused() -> None:
    case = _case()
    bare = experiment_for(case.compiled, case.hypotheses[0], with_record=False)
    with pytest.raises(P7EvidenceRefused) as caught:
        cross_check_admission_evidence(
            **{
                **case.kwargs(),
                "experiment_specs": [bare],
                "experiment_evidence_items": experiment_evidence([bare]),
            }
        )
    assert caught.value.code == "plan_record_missing"


def test_an_experiment_with_another_plans_record_is_refused() -> None:
    case = _case()
    other = P7PlanRecord.from_compiled(strategy_chain()[0])
    wrong = experiment_for(case.compiled, case.hypotheses[0], record=other, bind_outputs=False)
    # Rebuild its dependencies for the real plan so only the record differs.
    real = P7PlanRecord.from_compiled(case.compiled)
    dependencies = {
        **wrong.repro.dependency_hashes,
        **{node.spec_ref: node.spec_hash for node in real.nodes},
    }
    wrong = wrong.model_copy(
        update={"repro": wrong.repro.model_copy(update={"dependency_hashes": dependencies})}
    )
    with pytest.raises(P7EvidenceRefused) as caught:
        cross_check_admission_evidence(
            **{
                **case.kwargs(),
                "experiment_specs": [wrong],
                "experiment_evidence_items": experiment_evidence([wrong]),
            }
        )
    assert caught.value.code == "plan_record_mismatch"


def test_an_experiment_missing_a_node_output_dependency_is_refused() -> None:
    case = _case()
    partial = experiment_for(case.compiled, case.hypotheses[0], bind_outputs=False)
    with pytest.raises(P7EvidenceRefused) as caught:
        cross_check_admission_evidence(
            **{
                **case.kwargs(),
                "experiment_specs": [partial],
                "experiment_evidence_items": experiment_evidence([partial]),
            }
        )
    assert caught.value.code == "output_not_direct_dependency"


def test_an_experiment_with_a_stale_node_output_hash_is_refused() -> None:
    case = _case()
    record = P7PlanRecord.from_compiled(case.compiled)
    stale = experiment_for(case.compiled, case.hypotheses[0], bind_outputs=False)
    dependencies = {
        **stale.repro.dependency_hashes,
        **{node.spec_ref: "0" * 64 for node in record.nodes},
    }
    stale = stale.model_copy(
        update={"repro": stale.repro.model_copy(update={"dependency_hashes": dependencies})}
    )
    with pytest.raises(P7EvidenceRefused) as caught:
        cross_check_admission_evidence(
            **{
                **case.kwargs(),
                "experiment_specs": [stale],
                "experiment_evidence_items": experiment_evidence([stale]),
            }
        )
    assert caught.value.code == "output_hash_mismatch"


def test_a_non_runnable_compiled_plan_is_refused() -> None:
    case = _case()
    with pytest.raises(P7EvidenceRefused) as caught:
        cross_check_admission_evidence(
            **{**case.kwargs(), "compiled": dataclasses.replace(case.compiled)}
        )
    assert caught.value.code == "not_runnable"

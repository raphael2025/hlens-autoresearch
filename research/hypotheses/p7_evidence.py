"""Generate and cross-check the PREPARE evidence of a compiled P7 plan (ADR-0103 §2).

``typed_plan_audit.PlanAdmissionJournal.prepare`` validates only the *shape* of its evidence (an
``{"identity", "value"}`` envelope, content hashes). It cannot tell whether the evidence is the
evidence **of this plan**. This module produces it from the objects that are the authority —

- ``inputs_evidence(resolution)``: the verified direct references (ref + recomputed content hash),
  one item per ``SpecInput`` occurrence in plan order;
- ``outputs_evidence(compiled)``: one item per plan node (the lowered spec's ref + content hash),
  with the pinned universe manifest hash on a cross-sectional node;
- ``experiment_evidence(experiment_specs)``: one item per ``ExperimentSpec`` (its hash, the batch
  hypothesis it binds, the plan record it carries);

and ``cross_check_admission_evidence`` re-derives everything from the ``CompiledPlan`` and refuses
(``P7EvidenceRefused`` with a precise ``code``) unless, before any PREPARE is written:

- inputs / outputs / operators evidence equal the compiled plan's, item for item, in plan order
  (``input_*``, ``output_*``, ``operator_*``: ``missing`` / ``extra`` / ``duplicate`` /
  ``order`` / ``mismatch``);
- the ExperimentSpecs and Hypotheses are one-to-one, the experiment evidence matches the specs,
  each experiment binds its hypothesis' recomputed content hash, carries the plan's
  ``hlens.p7.plan@1.0.0`` record and every node output in its ``dependency_hashes``, and each
  hypothesis names the plan (``p7_plan = <plan_hash>``).

Pure functions: no clock, no registry, no journal write, no execution.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from core.domain.base import canonical_json
from core.domain.research import ExperimentSpec, Hypothesis
from research.hypotheses.p7_binding import (
    P7PlanRecord,
    P7PlanRecordError,
    hypothesis_p7_plan_hash,
    plan_dependency_hashes,
    recorded_p7_plan,
)
from research.hypotheses.typed_plan import PlanRefused
from research.hypotheses.typed_plan_audit import PlanAdmissionEvidence
from research.hypotheses.typed_plan_compiler import CompiledPlan
from research.hypotheses.typed_plan_resolver import DirectReferenceResolution

__all__ = [
    "P7EvidenceRefused",
    "cross_check_admission_evidence",
    "experiment_evidence",
    "inputs_evidence",
    "outputs_evidence",
]

_UNIVERSE_PARAMETER: Final = "universe_hash"


class P7EvidenceRefused(PlanRefused):
    """The admission evidence is not the evidence of the compiled plan / batch."""

    def __init__(self, code: str, where: str, detail: str) -> None:
        self.code = code
        self.where = where
        super().__init__(f"{code}: {where}: {detail}")


def inputs_evidence(resolution: DirectReferenceResolution) -> tuple[PlanAdmissionEvidence, ...]:
    """One evidence item per resolved ``SpecInput`` occurrence, in plan order."""
    if not isinstance(resolution, DirectReferenceResolution):
        raise P7EvidenceRefused(
            "invalid_resolution", "resolution", "must be a DirectReferenceResolution"
        )
    return tuple(
        _input_item(item.node_id, item.input_index, str(item.spec.ref), item.spec.content_hash())
        for item in resolution.inputs
    )


def _input_item(node_id: str, index: int, ref: str, digest: str) -> PlanAdmissionEvidence:
    return PlanAdmissionEvidence.from_data(
        {
            "identity": {"node_id": node_id, "input_index": index},
            "value": {"ref": ref, "content_hash": digest},
        }
    )


def _expected_inputs(compiled: CompiledPlan) -> tuple[PlanAdmissionEvidence, ...]:
    return tuple(
        _input_item(node.node_id, index, str(spec.ref), spec.content_hash())
        for node in compiled.nodes
        for index, (spec, source) in enumerate(zip(node.input_specs, node.input_nodes, strict=True))
        if source is None
    )


def outputs_evidence(compiled: CompiledPlan) -> tuple[PlanAdmissionEvidence, ...]:
    """One evidence item per plan node (the lowered spec), in plan order."""
    _require_compiled(compiled)
    items: list[PlanAdmissionEvidence] = []
    for node, plan_node in zip(compiled.nodes, compiled.plan.nodes, strict=True):
        value: dict[str, Any] = {
            "definition": node.definition,
            "ref": str(node.spec.ref),
            "content_hash": node.spec.content_hash(),
        }
        universe = plan_node.parameters.get(_UNIVERSE_PARAMETER)
        if universe is not None:
            value["universe_manifest_hash"] = universe
        items.append(
            PlanAdmissionEvidence.from_data({"identity": {"node_id": node.node_id}, "value": value})
        )
    return tuple(items)


def experiment_evidence(
    experiment_specs: Sequence[ExperimentSpec],
) -> tuple[PlanAdmissionEvidence, ...]:
    """One evidence item per ``ExperimentSpec``: its hash, the hypothesis it binds (with the
    hash its ``dependency_hashes`` records) and the plan hash of its ``hlens.p7.plan`` record."""
    if not isinstance(experiment_specs, Sequence) or isinstance(experiment_specs, str | bytes):
        raise P7EvidenceRefused(
            "invalid_sequence", "experiment_specs", "must be an ordered sequence"
        )
    items: list[PlanAdmissionEvidence] = []
    for index, experiment in enumerate(experiment_specs):
        where = f"experiment_specs[{index}]"
        if type(experiment) is not ExperimentSpec:
            raise P7EvidenceRefused("wrong_type", where, "must be an exact ExperimentSpec")
        repro = experiment.repro
        hypothesis_key = str(repro.hypothesis_ref)
        hypothesis_hash = repro.dependency_hashes.get(hypothesis_key)
        if hypothesis_hash is None:
            raise P7EvidenceRefused(
                "hypothesis_hash_missing", where, f"{hypothesis_key} is not in dependency_hashes"
            )
        plan_hash = _recorded_record(experiment, where)
        items.append(
            PlanAdmissionEvidence.from_data(
                {
                    "identity": {"experiment_hash": experiment.experiment_hash},
                    "value": {
                        "ref": str(experiment.ref),
                        "content_hash": experiment.content_hash(),
                        "hypothesis_ref": hypothesis_key,
                        "hypothesis_hash": hypothesis_hash,
                        "p7_plan_hash": None if plan_hash is None else plan_hash.plan_hash,
                    },
                }
            )
        )
    return tuple(items)


def _recorded_record(experiment: ExperimentSpec, where: str) -> P7PlanRecord | None:
    try:
        return recorded_p7_plan(experiment.repro.params)
    except P7PlanRecordError as exc:
        raise P7EvidenceRefused("plan_record_invalid", where, str(exc)) from exc


def _require_compiled(compiled: object) -> None:
    if not isinstance(compiled, CompiledPlan):
        raise P7EvidenceRefused("wrong_type", "compiled", "must be a CompiledPlan")
    if not compiled.runnable:
        raise P7EvidenceRefused("not_runnable", compiled.plan.root, "plan is not runnable")


def _identity_key(evidence: PlanAdmissionEvidence) -> str:
    return canonical_json(evidence.payload()["data"]["identity"])


def _match(
    prefix: str,
    supplied: object,
    expected: Sequence[PlanAdmissionEvidence],
) -> None:
    """``supplied`` must be ``expected``: same identities, same content, same order."""
    if not isinstance(supplied, Sequence) or isinstance(supplied, str | bytes):
        raise P7EvidenceRefused(f"{prefix}_invalid", prefix, "evidence must be an ordered sequence")
    seen: dict[str, PlanAdmissionEvidence] = {}
    for index, item in enumerate(supplied):
        where = f"{prefix}[{index}]"
        if not isinstance(item, PlanAdmissionEvidence):
            raise P7EvidenceRefused(f"{prefix}_invalid", where, "must be PlanAdmissionEvidence")
        key = _identity_key(item)
        if key in seen:
            raise P7EvidenceRefused(f"{prefix}_duplicate", where, f"repeated identity {key}")
        seen[key] = item
    wanted = {_identity_key(item): item for item in expected}
    extra = [key for key in seen if key not in wanted]
    if extra:
        raise P7EvidenceRefused(f"{prefix}_extra", prefix, ", ".join(extra))
    missing = [key for key in wanted if key not in seen]
    if missing:
        raise P7EvidenceRefused(f"{prefix}_missing", prefix, ", ".join(missing))
    for key, item in seen.items():
        if item.content_hash != wanted[key].content_hash:
            raise P7EvidenceRefused(
                f"{prefix}_mismatch", prefix, f"{key} differs from the compiled plan"
            )
    if list(seen) != list(wanted):
        raise P7EvidenceRefused(f"{prefix}_order", prefix, "must follow the plan's node order")


def cross_check_admission_evidence(
    *,
    compiled: CompiledPlan,
    inputs: Sequence[PlanAdmissionEvidence],
    outputs: Sequence[PlanAdmissionEvidence],
    operators: Sequence[PlanAdmissionEvidence],
    experiment_specs: Sequence[ExperimentSpec],
    experiment_evidence_items: Sequence[PlanAdmissionEvidence],
    hypotheses: Sequence[Hypothesis],
) -> None:
    """Refuse (``P7EvidenceRefused``) unless the evidence is exactly the compiled plan's and the
    batch binds it (module docs). Returns ``None``; nothing is written."""
    _require_compiled(compiled)
    _match("input", inputs, _expected_inputs(compiled))
    _match("output", outputs, outputs_evidence(compiled))
    _match("operator", operators, compiled.operator_evidence())

    if not isinstance(experiment_specs, Sequence) or isinstance(experiment_specs, str | bytes):
        raise P7EvidenceRefused(
            "invalid_sequence", "experiment_specs", "must be an ordered sequence"
        )
    if not isinstance(hypotheses, Sequence) or isinstance(hypotheses, str | bytes):
        raise P7EvidenceRefused("invalid_sequence", "hypotheses", "must be an ordered sequence")
    if not experiment_specs or not hypotheses:
        raise P7EvidenceRefused(
            "empty_batch", "bindings", "experiments and hypotheses are required"
        )
    if len(experiment_specs) != len(hypotheses):
        raise P7EvidenceRefused(
            "batch_cardinality_mismatch",
            "bindings",
            f"{len(experiment_specs)} ExperimentSpecs for {len(hypotheses)} hypotheses",
        )
    _match("experiment", experiment_evidence_items, experiment_evidence(experiment_specs))

    by_ref: dict[str, Hypothesis] = {}
    for index, hypothesis in enumerate(hypotheses):
        where = f"hypotheses[{index}]"
        if type(hypothesis) is not Hypothesis:
            raise P7EvidenceRefused("wrong_type", where, "must be an exact Hypothesis")
        key = str(hypothesis.ref)
        if key in by_ref:
            raise P7EvidenceRefused("duplicate_hypothesis", where, f"repeated {key}")
        by_ref[key] = hypothesis
        try:
            named = hypothesis_p7_plan_hash(hypothesis)
        except P7PlanRecordError as exc:
            raise P7EvidenceRefused("hypothesis_plan_condition_invalid", where, str(exc)) from exc
        if named is None:
            raise P7EvidenceRefused(
                "hypothesis_plan_condition_missing", where, "no p7_plan condition"
            )
        if named != compiled.plan_hash:
            raise P7EvidenceRefused(
                "hypothesis_plan_condition_mismatch",
                where,
                f"names plan {named}, the compiled plan is {compiled.plan_hash}",
            )

    expected_record = P7PlanRecord.from_compiled(compiled)
    expected_dependencies = plan_dependency_hashes(expected_record)
    used: dict[str, int] = dict.fromkeys(by_ref, 0)
    for index, experiment in enumerate(experiment_specs):
        where = f"experiment_specs[{index}]"
        repro = experiment.repro
        key = str(repro.hypothesis_ref)
        bound_hypothesis = by_ref.get(key)
        if bound_hypothesis is None:
            raise P7EvidenceRefused(
                "unknown_hypothesis", where, f"{key} is not in the supplied batch"
            )
        if repro.dependency_hashes.get(key) != bound_hypothesis.content_hash():
            raise P7EvidenceRefused(
                "hypothesis_hash_mismatch",
                where,
                f"dependency hash for {key} does not match the Hypothesis content hash",
            )
        used[key] += 1
        record = _recorded_record(experiment, where)
        if record is None:
            raise P7EvidenceRefused(
                "plan_record_missing", where, "the experiment carries no hlens.p7.plan record"
            )
        if record != expected_record:
            raise P7EvidenceRefused(
                "plan_record_mismatch", where, "the recorded plan is not the compiled plan"
            )
        for ref, digest in expected_dependencies.items():
            bound = repro.dependency_hashes.get(ref)
            if bound is None:
                raise P7EvidenceRefused(
                    "output_not_direct_dependency", where, f"{ref} is not in dependency_hashes"
                )
            if bound != digest:
                raise P7EvidenceRefused(
                    "output_hash_mismatch", where, f"dependency hash for {ref} differs"
                )
    repeated = sorted(key for key, count in used.items() if count > 1)
    if repeated:
        raise P7EvidenceRefused(
            "hypothesis_used_more_than_once", "experiment_specs", ", ".join(repeated)
        )

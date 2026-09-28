"""Pure, fail-closed binding checks for typed-plan admission evidence (ADR-0073 §1).

These checks establish that caller-supplied ``ExperimentSpec`` values bind the exact batch
``Hypothesis`` values and that each declared lowered output is a direct, hash-matched dependency
of its associated experiment. They do not lower a plan, validate an operator's semantics, persist
evidence, register trials, or authorize execution.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from core.domain.base import Ref, VersionedSpec
from core.domain.research import ExperimentSpec, Hypothesis
from core.domain.specs import EventSpec, FeatureSpec, StateSpec, StrategySpec
from research.hypotheses.typed_plan import PlanRefused

__all__ = [
    "ExperimentHypothesisBinding",
    "LoweredOutputBinding",
    "PlanBindingRefused",
    "validate_experiment_bindings",
]

_HASH_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
_LOWERED_SPEC_TYPES: Final = (EventSpec, FeatureSpec, StateSpec, StrategySpec)


class PlanBindingRefused(PlanRefused):
    """The supplied ExperimentSpec / Hypothesis / lowered-output evidence disagrees."""

    def __init__(self, code: str, where: str, detail: str) -> None:
        self.code = code
        self.where = where
        super().__init__(f"{code}: {where}: {detail}")


@dataclass(frozen=True, slots=True)
class LoweredOutputBinding:
    """One lowered core spec and the experiment whose dependency it claims to satisfy.

    ``experiment_hash`` is the ExperimentSpec's existing ``repro.experiment_hash`` identity.
    The association is in-memory evidence for validation; it adds no persisted or domain field.
    """

    experiment_hash: str
    spec: VersionedSpec

    def __post_init__(self) -> None:
        if not isinstance(self.experiment_hash, str) or _HASH_PATTERN.fullmatch(
            self.experiment_hash
        ) is None:
            raise ValueError("experiment_hash must be a canonical lowercase SHA-256")
        if type(self.spec) not in _LOWERED_SPEC_TYPES:
            raise TypeError("lowered output must be an exact Feature/State/Event/StrategySpec")


@dataclass(frozen=True, slots=True)
class ExperimentHypothesisBinding:
    """Detached values returned by the validator; this object is not an admission credential."""

    experiment: ExperimentSpec
    hypothesis: Hypothesis
    outputs: tuple[VersionedSpec, ...]


def _copy_validated[T](value: T, expected: type[T], where: str) -> T:
    """Rebuild from canonical JSON so validation does not trust a caller's mutable internals."""
    if type(value) is not expected:
        raise PlanBindingRefused(
            "wrong_type", where, f"expected exactly {expected.__name__}, got {type(value).__name__}"
        )
    try:
        payload = value.model_dump(mode="json")  # type: ignore[attr-defined]
        rebuilt = expected.model_validate(payload)  # type: ignore[attr-defined]
    except Exception as exc:
        raise PlanBindingRefused(
            "invalid_contract", where, f"contract reconstruction failed ({type(exc).__name__})"
        ) from exc
    if rebuilt.model_dump(mode="json") != payload:  # type: ignore[attr-defined]
        raise PlanBindingRefused("noncanonical_contract", where, "payload did not round-trip")
    return rebuilt


def _canonical_spec(spec: VersionedSpec, where: str) -> VersionedSpec:
    expected = next((kind for kind in _LOWERED_SPEC_TYPES if type(spec) is kind), None)
    if expected is None:
        raise PlanBindingRefused(
            "unsupported_output_type",
            where,
            "lowered output must be exactly FeatureSpec, StateSpec, EventSpec, or StrategySpec",
        )
    return _copy_validated(spec, expected, where)


def _identity(ref: Ref) -> str:
    return str(ref)


def validate_experiment_bindings(
    *,
    experiment_specs: Sequence[ExperimentSpec],
    hypotheses: Sequence[Hypothesis],
    lowered_outputs: Sequence[LoweredOutputBinding],
) -> tuple[ExperimentHypothesisBinding, ...]:
    """Validate exact batch-to-experiment and lowered-output bindings without side effects.

    The ExperimentSpec and Hypothesis sequences must be non-empty. Each ExperimentSpec must name one exact batch
    ``hypothesis:name@version`` and bind that hypothesis's recomputed content hash in
    ``dependency_hashes``. Every batch hypothesis must be consumed once and only once. Every
    supplied output must be unique within its experiment, and its exact ``kind:name@version`` and
    recomputed hash must appear in that experiment's direct ``dependency_hashes``. The same
    immutable output may be referenced by more than one experiment.
    No transitive dependency or compiler completeness claim is made.

    The returned tuple preserves ExperimentSpec order and contains detached, reconstructed
    contracts. Any discrepancy raises ``PlanBindingRefused``; no partial bindings are returned.
    """
    if not isinstance(experiment_specs, Sequence) or isinstance(experiment_specs, (str, bytes)):
        raise PlanBindingRefused("invalid_sequence", "experiment_specs", "must be an ordered sequence")
    if not isinstance(hypotheses, Sequence) or isinstance(hypotheses, (str, bytes)):
        raise PlanBindingRefused("invalid_sequence", "hypotheses", "must be an ordered sequence")
    if not isinstance(lowered_outputs, Sequence) or isinstance(lowered_outputs, (str, bytes)):
        raise PlanBindingRefused("invalid_sequence", "lowered_outputs", "must be an ordered sequence")
    if not experiment_specs or not hypotheses:
        raise PlanBindingRefused(
            "empty_batch", "bindings", "experiments and hypotheses are required"
        )
    if len(experiment_specs) != len(hypotheses):
        raise PlanBindingRefused(
            "batch_cardinality_mismatch",
            "bindings",
            f"{len(experiment_specs)} ExperimentSpecs for {len(hypotheses)} hypotheses",
        )

    verified_hypotheses: dict[str, Hypothesis] = {}
    hypothesis_hashes: dict[str, str] = {}
    for index, item in enumerate(hypotheses):
        where = f"hypotheses[{index}]"
        hypothesis = _copy_validated(item, Hypothesis, where)
        ref = hypothesis.ref
        key = _identity(ref)
        if key in verified_hypotheses:
            raise PlanBindingRefused("duplicate_hypothesis", where, f"repeated {key}")
        verified_hypotheses[key] = hypothesis
        hypothesis_hashes[key] = hypothesis.content_hash()

    verified_experiments: list[ExperimentSpec] = []
    experiment_hashes: set[str] = set()
    experiment_hypotheses: dict[str, str] = {}
    hypothesis_use_count: dict[str, int] = dict.fromkeys(verified_hypotheses, 0)
    for index, item in enumerate(experiment_specs):
        where = f"experiment_specs[{index}]"
        experiment = _copy_validated(item, ExperimentSpec, where)
        experiment_hash = experiment.experiment_hash
        if experiment_hash in experiment_hashes:
            raise PlanBindingRefused("duplicate_experiment", where, "repeated ExperimentSpec hash")
        experiment_hashes.add(experiment_hash)

        hypothesis_ref = experiment.repro.hypothesis_ref
        hypothesis_key = _identity(hypothesis_ref)
        hypothesis = verified_hypotheses.get(hypothesis_key)
        if hypothesis is None:
            raise PlanBindingRefused(
                "unknown_hypothesis", where, f"{hypothesis_key} is not in the supplied batch"
            )
        dependency_hash = experiment.repro.dependency_hashes.get(hypothesis_key)
        expected_hash = hypothesis_hashes[hypothesis_key]
        if dependency_hash != expected_hash:
            raise PlanBindingRefused(
                "hypothesis_hash_mismatch",
                where,
                f"dependency hash for {hypothesis_key} does not match the Hypothesis content hash",
            )
        hypothesis_use_count[hypothesis_key] += 1
        experiment_hypotheses[experiment_hash] = hypothesis_key
        verified_experiments.append(experiment)

    repeated = sorted(key for key, count in hypothesis_use_count.items() if count > 1)
    if repeated:
        raise PlanBindingRefused(
            "hypothesis_used_more_than_once", "experiment_specs", ", ".join(repeated)
        )
    missing_hypotheses = sorted(key for key, count in hypothesis_use_count.items() if count == 0)
    if missing_hypotheses:
        raise PlanBindingRefused(
            "hypothesis_not_bound", "experiment_specs", ", ".join(missing_hypotheses)
        )

    outputs_by_experiment: dict[str, list[VersionedSpec]] = {
        experiment_hash: [] for experiment_hash in experiment_hashes
    }
    seen_outputs: set[tuple[str, str]] = set()
    experiment_by_hash = {item.experiment_hash: item for item in verified_experiments}
    for index, item in enumerate(lowered_outputs):
        where = f"lowered_outputs[{index}]"
        if not isinstance(item, LoweredOutputBinding):
            raise PlanBindingRefused(
                "wrong_type", where, "must be a LoweredOutputBinding with experiment association"
            )
        experiment = experiment_by_hash.get(item.experiment_hash)
        if experiment is None:
            raise PlanBindingRefused(
                "extra_output_experiment",
                where,
                f"{item.experiment_hash} is not one of the supplied ExperimentSpecs",
            )
        output = _canonical_spec(item.spec, where)
        output_ref = output.ref
        output_key = _identity(output_ref)
        output_identity = (item.experiment_hash, output_key)
        if output_identity in seen_outputs:
            raise PlanBindingRefused("duplicate_output", where, f"repeated {output_key}")
        seen_outputs.add(output_identity)

        dependency_hash = experiment.repro.dependency_hashes.get(output_key)
        if dependency_hash is None:
            raise PlanBindingRefused(
                "output_not_direct_dependency",
                where,
                f"{output_key} is absent from {item.experiment_hash}'s direct dependencies",
            )
        output_hash = output.content_hash()
        if dependency_hash != output_hash:
            raise PlanBindingRefused(
                "output_hash_mismatch",
                where,
                f"dependency hash for {output_key} differs from its recomputed content hash",
            )
        outputs_by_experiment[item.experiment_hash].append(output)

    return tuple(
        ExperimentHypothesisBinding(
            experiment=experiment,
            hypothesis=verified_hypotheses[experiment_hypotheses[experiment.experiment_hash]],
            outputs=tuple(outputs_by_experiment[experiment.experiment_hash]),
        )
        for experiment in verified_experiments
    )

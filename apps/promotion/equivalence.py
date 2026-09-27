"""Equivalence Gate and deployment record (ADR-0005 §4 / §5; 2026-09-26).

CODE_COMPLETE / DEBUG_PENDING. No contract, Schema or lifecycle change. Nothing here runs,
schedules or connects any execution: a ``DeploymentRecord`` is an audit record, not a deployment.

**Placement.** The gate runs a *production implementation* against the artifact's golden outputs,
so it belongs to the Application Plane (``apps/``) and must never import ``research/`` (01-system.md
§3, statically tested): it reads the golden inputs and outputs from the Strategy Registry
(``infrastructure.registry``), never recomputes them with research code. It is not in
``apps/execution`` (that service may not import ``infrastructure``, ADR-0046).

**The gate** (``run_equivalence_gate``): for a registered artifact, a candidate ``StrategyProvider``
and its ``GitCodeRevision`` (commit + tree), run the candidate on every golden request (read from
the registry's blob store and verified against the artifact's hashes) and compare:

- ``signals_match`` — the candidate answered exactly the golden requests: every answer passes
  ``StrategyResult.check_answers`` for its golden request (its ``request_hash`` binds the request,
  hence every signal observation) under the candidate's own descriptor;
- ``positions_match`` — the candidate's positions payload (``infrastructure.registry.golden``)
  hashes to ``golden_outputs.positions_hash``: an **exact**, byte-for-byte comparison of every
  ``TargetPosition`` (``Decimal("0.5")`` and ``Decimal("0.50")`` differ). ``EquivalenceCheck`` has
  an optional ``tolerance`` slot but no declared tolerance semantics, and ADR-0005 §5 allows a
  tolerance only when declared; none is declared, so ``tolerance`` stays ``None`` and nothing is
  approximated.

A candidate that does not declare the spec, raises, or answers another request fails the check
(both flags ``False``, reasons in ``EquivalenceOutcome.mismatches``) — the failed check is still a
record. A candidate whose class lives in ``research`` is refused before it runs
(``candidate_is_research_code``; H5: research code is never the production implementation).
Unknown artifact → ``unknown_artifact``. Missing / corrupted golden blobs are registry errors,
never a check.

**Deployment** (``record_deployment``): only for a check that passed **and** is recorded in the
registry, when no recorded check of the same artifact and production code identity failed
(a non-deterministic candidate is not deployable), for a lifecycle history of the artifact's
strategy that is ``PRODUCTION_CANDIDATE`` or ``ACTIVE`` (ADR-0005 §4: the lifecycle state must allow
running). ``deployment_id`` = SHA-256 of ``{artifact_id, production code (commit, tree),
config_hash}`` — ADR-0005 §3's definition; the contract leaves the algorithm open, this is this
implementation's choice.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import ValidationError

from core.contracts.strategy import StrategyProvider, StrategyResult
from core.domain.artifact import DeploymentRecord, EquivalenceCheck, StrategyArtifact
from core.domain.base import GitCodeRevision, content_hash
from core.errors import HlensError
from core.lifecycle.strategy import LifecycleHistory, LifecycleState
from infrastructure.registry import (
    StrategyRegistry,
    UnknownArtifact,
    golden_positions_payload,
    payload_hash,
)

__all__ = [
    "DEPLOYABLE_STATES",
    "EquivalenceOutcome",
    "EquivalenceRefusal",
    "EquivalenceRefused",
    "deployment_id",
    "record_deployment",
    "run_equivalence_gate",
]

#: Lifecycle states in which a deployment record may be written (ADR-0005 §1 flowchart:
#: PRODUCTION_CANDIDATE → deployment review → ACTIVE).
DEPLOYABLE_STATES: Final = frozenset({LifecycleState.PRODUCTION_CANDIDATE, LifecycleState.ACTIVE})


class EquivalenceRefusal(StrEnum):
    UNKNOWN_ARTIFACT = "unknown_artifact"
    CANDIDATE_IS_RESEARCH_CODE = "candidate_is_research_code"
    EQUIVALENCE_NOT_RECORDED = "equivalence_not_recorded"
    EQUIVALENCE_NOT_PASSED = "equivalence_not_passed"
    EQUIVALENCE_CONFLICT = "equivalence_conflict"
    LIFECYCLE_INVALID = "lifecycle_invalid"
    LIFECYCLE_SUBJECT_MISMATCH = "lifecycle_subject_mismatch"
    LIFECYCLE_NOT_DEPLOYABLE = "lifecycle_not_deployable"


class EquivalenceRefused(Exception):
    """The gate could not run, or a deployment record may not be written."""

    def __init__(self, reason: EquivalenceRefusal, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class EquivalenceOutcome:
    """One gate run: the contract record and, when it failed, why."""

    check: EquivalenceCheck
    mismatches: tuple[str, ...]


def _artifact(registry: StrategyRegistry, artifact_id: str) -> StrategyArtifact:
    try:
        return registry.artifact(artifact_id)
    except UnknownArtifact as exc:
        raise EquivalenceRefused(EquivalenceRefusal.UNKNOWN_ARTIFACT, str(exc)) from exc


def run_equivalence_gate(
    registry: StrategyRegistry,
    artifact_id: str,
    candidate: StrategyProvider,
    production_code: GitCodeRevision,
) -> EquivalenceOutcome:
    """Run ``candidate`` on the artifact's golden inputs (see module docs). Writes nothing."""
    artifact = _artifact(registry, artifact_id)
    module = type(candidate).__module__
    if module.split(".")[0] == "research":
        raise EquivalenceRefused(
            EquivalenceRefusal.CANDIDATE_IS_RESEARCH_CODE,
            f"{module}.{type(candidate).__qualname__} is research code, not a production "
            "implementation",
        )
    production_code = GitCodeRevision.model_validate_json(production_code.model_dump_json())
    requests = registry.golden_inputs(artifact_id)  # both blobs verified before anything runs
    answers = registry.golden_answers(artifact_id)
    golden = artifact.golden_outputs
    spec_ref = artifact.strategy_spec
    spec_hash = artifact.dependencies[str(spec_ref)]
    descriptor = candidate.descriptor
    mismatches: list[str] = []
    results: list[StrategyResult] = []
    if not descriptor.supports(spec_ref, spec_hash):
        mismatches.append(f"candidate {descriptor.plugin_key} does not declare {spec_ref}")
    else:
        for index, request in enumerate(requests):
            try:
                result = candidate.target_positions(request)
                result.check_answers(request, descriptor)
            except Exception as exc:  # a failing candidate is a failed check, not a crash
                mismatches.append(f"golden request {index}: {type(exc).__name__}: {exc}")
                continue
            results.append(result)
    signals_match = not mismatches and len(results) == len(requests)
    positions_match = False
    if signals_match:
        positions_match = payload_hash(golden_positions_payload(results)) == golden.positions_hash
        if not positions_match:
            for index, (result, answer) in enumerate(zip(results, answers, strict=True)):
                if result.positions != answer.positions:
                    mismatches.append(f"golden request {index}: positions differ")
            if not mismatches:  # equal as values but not as bytes (e.g. Decimal exponent)
                mismatches.append("positions differ in their exact encoding")
    check = EquivalenceCheck(
        artifact_id=artifact.artifact_id,
        production_code_hash=production_code,
        signals_match=signals_match,
        positions_match=positions_match,
        tolerance=None,
    )
    return EquivalenceOutcome(check, tuple(mismatches))


def deployment_id(artifact_id: str, production_code: GitCodeRevision, config_hash: str) -> str:
    """ADR-0005 §3: the deployment identity of (artifact, production code, config)."""
    commit, tree = production_code.code_identity()
    return content_hash(
        {
            "artifact_id": artifact_id,
            "production_code": {"commit_oid": commit, "tree_oid": tree},
            "config_hash": config_hash,
        }
    )


def record_deployment(
    registry: StrategyRegistry,
    check: EquivalenceCheck,
    lifecycle: LifecycleHistory,
    config_hash: str,
) -> DeploymentRecord:
    """Write a ``DeploymentRecord`` for a recorded, passing check (see module docs)."""
    artifact = _artifact(registry, check.artifact_id)
    if not check.passed:
        raise EquivalenceRefused(
            EquivalenceRefusal.EQUIVALENCE_NOT_PASSED,
            f"signals_match={check.signals_match}, positions_match={check.positions_match}",
        )
    if not registry.has_equivalence(check):
        raise EquivalenceRefused(
            EquivalenceRefusal.EQUIVALENCE_NOT_RECORDED,
            "record the passing check in the registry before deploying",
        )
    code = check.production_code_hash.code_identity()
    failed = [
        other
        for other in registry.equivalence_checks(check.artifact_id)
        if other.production_code_hash.code_identity() == code and not other.passed
    ]
    if failed:
        raise EquivalenceRefused(
            EquivalenceRefusal.EQUIVALENCE_CONFLICT,
            f"{len(failed)} recorded check(s) of the same production code failed",
        )
    try:
        lifecycle = LifecycleHistory.model_validate_json(lifecycle.model_dump_json())
    except (ValidationError, HlensError) as exc:  # LifecycleViolation is not a ValueError
        raise EquivalenceRefused(EquivalenceRefusal.LIFECYCLE_INVALID, str(exc)) from exc
    if lifecycle.subject.target_identity() != artifact.strategy_spec.target_identity():
        raise EquivalenceRefused(
            EquivalenceRefusal.LIFECYCLE_SUBJECT_MISMATCH,
            f"the lifecycle history is about {lifecycle.subject}, not {artifact.strategy_spec}",
        )
    if lifecycle.current_state not in DEPLOYABLE_STATES:
        raise EquivalenceRefused(
            EquivalenceRefusal.LIFECYCLE_NOT_DEPLOYABLE,
            f"{artifact.strategy_spec} is {lifecycle.current_state.value}",
        )
    record = DeploymentRecord(
        deployment_id=deployment_id(check.artifact_id, check.production_code_hash, config_hash),
        artifact_id=check.artifact_id,
        production_code_hash=check.production_code_hash,
        config_hash=config_hash,
        equivalence=check,
    )
    registry.record_deployment(record)
    return record

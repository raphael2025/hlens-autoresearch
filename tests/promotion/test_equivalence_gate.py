"""Equivalence Gate and deployment record (``apps.promotion``, ADR-0005 §4 / §5) on TEST ONLY
fixtures: exact comparison with the registered golden outputs; a deployment only for a recorded,
passing check of a strategy at PRODUCTION_CANDIDATE / ACTIVE."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.promotion import (
    EquivalenceRefusal,
    EquivalenceRefused,
    deployment_id,
    record_deployment,
    run_equivalence_gate,
)
from core.domain.artifact import StrategyArtifact
from core.domain.base import Kind, Ref
from core.lifecycle.strategy import LifecycleState
from infrastructure.registry import DuplicateRecord, RegistryCorrupted, StrategyRegistry
from research.promotion import promote
from research.strategies.library import library_entries
from tests.factories import HASH_A, HASH_CONFIG, git_code_revision
from tests.promotion.fixtures import (
    PATH_TO_PAPER,
    PATH_TO_PRODUCTION_CANDIDATE,
    RaisingStrategy,
    ToyProductionSign,
    flip_sign,
    history,
    toy_evidence,
    toy_spec,
    widen_exponent,
)

S = LifecycleState
E = EquivalenceRefusal
PRODUCTION_CODE = git_code_revision(
    commit_oid="1" * 40, tree_oid="2" * 40
)  # TEST ONLY production revision
OTHER_STRATEGY = Ref(kind=Kind.STRATEGY, name="someone_else", version="1.0.0")


@pytest.fixture
def registered(tmp_path: Path) -> Iterator[tuple[StrategyRegistry, StrategyArtifact]]:
    with StrategyRegistry(tmp_path / "registry") as registry:
        yield registry, promote(toy_evidence(), registry)


def test_a_faithful_reimplementation_passes_and_can_be_deployed(
    registered: tuple[StrategyRegistry, StrategyArtifact], tmp_path: Path
) -> None:
    registry, artifact = registered
    outcome = run_equivalence_gate(
        registry, artifact.artifact_id, ToyProductionSign(toy_spec()), PRODUCTION_CODE
    )
    assert outcome.mismatches == ()
    check = outcome.check
    assert check.passed and check.tolerance is None
    assert check.artifact_id == artifact.artifact_id
    assert check.production_code_hash == PRODUCTION_CODE
    assert len(registry) == 1  # the gate itself writes nothing
    registry.record_equivalence(check)
    lifecycle = history(toy_spec().ref, PATH_TO_PRODUCTION_CANDIDATE)
    record = record_deployment(registry, check, lifecycle, HASH_CONFIG)
    assert record.deployment_id == deployment_id(artifact.artifact_id, PRODUCTION_CODE, HASH_CONFIG)
    assert record.equivalence == check
    assert registry.deployments(artifact.artifact_id) == (record,)
    with pytest.raises(DuplicateRecord):
        record_deployment(registry, check, lifecycle, HASH_CONFIG)
    active = history(toy_spec().ref, (*PATH_TO_PRODUCTION_CANDIDATE, S.ACTIVE))
    other_config = record_deployment(registry, check, active, HASH_A)
    assert other_config.deployment_id != record.deployment_id


def test_the_research_provider_itself_is_never_the_candidate(
    registered: tuple[StrategyRegistry, StrategyArtifact],
) -> None:
    registry, artifact = registered
    for entry in library_entries():
        with pytest.raises(EquivalenceRefused) as caught:
            run_equivalence_gate(
                registry, artifact.artifact_id, entry.candidate().strategy, PRODUCTION_CODE
            )
        assert caught.value.reason is E.CANDIDATE_IS_RESEARCH_CODE


def test_an_unknown_artifact_is_refused(
    registered: tuple[StrategyRegistry, StrategyArtifact],
) -> None:
    registry, _ = registered
    with pytest.raises(EquivalenceRefused) as caught:
        run_equivalence_gate(registry, HASH_A, ToyProductionSign(toy_spec()), PRODUCTION_CODE)
    assert caught.value.reason is E.UNKNOWN_ARTIFACT


def test_different_positions_fail_the_check(
    registered: tuple[StrategyRegistry, StrategyArtifact],
) -> None:
    registry, artifact = registered
    candidate = ToyProductionSign(toy_spec(), transform=flip_sign)
    outcome = run_equivalence_gate(registry, artifact.artifact_id, candidate, PRODUCTION_CODE)
    assert outcome.check.signals_match and not outcome.check.positions_match
    assert not outcome.check.passed
    assert any("positions differ" in m for m in outcome.mismatches)


def test_the_comparison_is_exact_not_numeric(
    registered: tuple[StrategyRegistry, StrategyArtifact],
) -> None:
    """``0.25`` and ``0.2500`` are equal numbers but not equal bytes: no tolerance is declared."""
    registry, artifact = registered
    candidate = ToyProductionSign(toy_spec(), transform=widen_exponent)
    outcome = run_equivalence_gate(registry, artifact.artifact_id, candidate, PRODUCTION_CODE)
    assert not outcome.check.positions_match
    assert outcome.check.tolerance is None
    assert outcome.mismatches == ("positions differ in their exact encoding",)


def test_a_candidate_that_does_not_declare_the_spec_fails(
    registered: tuple[StrategyRegistry, StrategyArtifact],
) -> None:
    registry, artifact = registered
    other = toy_spec().model_copy(update={"name": "not_toy"})
    outcome = run_equivalence_gate(
        registry, artifact.artifact_id, ToyProductionSign(other), PRODUCTION_CODE
    )
    assert not outcome.check.signals_match and not outcome.check.positions_match
    assert "does not declare" in outcome.mismatches[0]


def test_a_raising_candidate_fails_and_the_failure_is_recorded(
    registered: tuple[StrategyRegistry, StrategyArtifact],
) -> None:
    registry, artifact = registered
    outcome = run_equivalence_gate(
        registry, artifact.artifact_id, RaisingStrategy(toy_spec()), PRODUCTION_CODE
    )
    assert not outcome.check.passed
    assert all("RuntimeError" in m for m in outcome.mismatches)
    registry.record_equivalence(outcome.check)  # a failed check is history too (H6)
    assert registry.equivalence_checks(artifact.artifact_id) == (outcome.check,)
    with pytest.raises(EquivalenceRefused) as caught:
        record_deployment(
            registry,
            outcome.check,
            history(toy_spec().ref, PATH_TO_PRODUCTION_CANDIDATE),
            HASH_CONFIG,
        )
    assert caught.value.reason is E.EQUIVALENCE_NOT_PASSED


def test_a_passing_check_must_be_recorded_before_deploying(
    registered: tuple[StrategyRegistry, StrategyArtifact],
) -> None:
    registry, artifact = registered
    outcome = run_equivalence_gate(
        registry, artifact.artifact_id, ToyProductionSign(toy_spec()), PRODUCTION_CODE
    )
    with pytest.raises(EquivalenceRefused) as caught:
        record_deployment(
            registry,
            outcome.check,
            history(toy_spec().ref, PATH_TO_PRODUCTION_CANDIDATE),
            HASH_CONFIG,
        )
    assert caught.value.reason is E.EQUIVALENCE_NOT_RECORDED
    assert registry.deployments() == ()


def test_a_failed_check_of_the_same_code_blocks_deployment(
    registered: tuple[StrategyRegistry, StrategyArtifact],
) -> None:
    registry, artifact = registered
    failed = run_equivalence_gate(
        registry, artifact.artifact_id, RaisingStrategy(toy_spec()), PRODUCTION_CODE
    ).check
    passed = run_equivalence_gate(
        registry, artifact.artifact_id, ToyProductionSign(toy_spec()), PRODUCTION_CODE
    ).check
    registry.record_equivalence(failed)
    registry.record_equivalence(passed)
    with pytest.raises(EquivalenceRefused) as caught:
        record_deployment(
            registry, passed, history(toy_spec().ref, PATH_TO_PRODUCTION_CANDIDATE), HASH_CONFIG
        )
    assert caught.value.reason is E.EQUIVALENCE_CONFLICT


@pytest.mark.parametrize(
    ("subject", "path", "reason"),
    [
        (None, PATH_TO_PAPER, E.LIFECYCLE_NOT_DEPLOYABLE),
        (None, (*PATH_TO_PAPER, S.RETIRED), E.LIFECYCLE_NOT_DEPLOYABLE),
        (OTHER_STRATEGY, PATH_TO_PRODUCTION_CANDIDATE, E.LIFECYCLE_SUBJECT_MISMATCH),
    ],
)
def test_deployment_needs_the_strategy_at_production_candidate_or_active(
    registered: tuple[StrategyRegistry, StrategyArtifact],
    subject: Ref | None,
    path: tuple[S, ...],
    reason: EquivalenceRefusal,
) -> None:
    registry, artifact = registered
    check = run_equivalence_gate(
        registry, artifact.artifact_id, ToyProductionSign(toy_spec()), PRODUCTION_CODE
    ).check
    registry.record_equivalence(check)
    with pytest.raises(EquivalenceRefused) as caught:
        record_deployment(registry, check, history(subject or toy_spec().ref, path), HASH_CONFIG)
    assert caught.value.reason is reason
    assert registry.deployments() == ()


def test_corrupted_golden_data_is_an_error_not_a_check(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    with StrategyRegistry(root) as registry:
        artifact = promote(toy_evidence(), registry)
    blob = root / "blobs" / f"{artifact.golden_outputs.positions_hash}.json"
    blob.write_bytes(blob.read_bytes() + b" ")
    with StrategyRegistry(root) as registry, pytest.raises(RegistryCorrupted):
        run_equivalence_gate(
            registry, artifact.artifact_id, ToyProductionSign(toy_spec()), PRODUCTION_CODE
        )

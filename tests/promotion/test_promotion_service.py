"""Promotion service (``research.promotion``, ADR-0005): the happy path on TEST ONLY evidence and a
typed refusal for every missing / non-PASS / mismatched piece (including a Profile that is not
frozen and calibrated, C-A8); no library strategy is promotable."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from core.contracts.validation_profile import ProfileStatus, ValidationProfile
from core.domain.artifact import StrategyArtifact
from core.domain.base import FrozenMapping, Kind, Ref
from core.domain.research import ValidationReport, Verdict
from core.lifecycle.strategy import LifecycleHistory, LifecycleState, LifecycleTransition
from infrastructure.registry import (
    StrategyRegistry,
    decode_golden_positions,
    decode_golden_signals,
    payload_hash,
)
from research.promotion import (
    PromotionEvidence,
    PromotionRefusal,
    PromotionRefused,
    build_artifact,
    promote,
)
from research.strategies.library import library_entries
from tests.factories import HASH_A, HASH_PROFILE, OTHER_GIT_COMMIT_OID, git_code_revision
from tests.fake_strategy import SIGNAL_REF, FakeSignStrategy, RequestShiftingStrategy
from tests.promotion.fixtures import (
    ARTIFACT_TIME,
    PATH_TO_PAPER,
    PATH_TO_PRODUCTION_CANDIDATE,
    REPORT_TIME,
    TEST_ONLY_SNAPSHOT,
    FlakyStrategy,
    golden_request,
    history,
    toy_evidence,
    toy_experiment,
    toy_profile,
    toy_report,
    toy_spec,
)

S = LifecycleState
R = PromotionRefusal
OTHER_STRATEGY = Ref(kind=Kind.STRATEGY, name="someone_else", version="1.0.0")


def _refusal(evidence: PromotionEvidence) -> PromotionRefused:
    with pytest.raises(PromotionRefused) as caught:
        build_artifact(evidence)
    return caught.value


# ---- happy path ------------------------------------------------------------------------


def test_the_toy_strategy_builds_a_deterministic_artifact() -> None:
    evidence = toy_evidence()
    package = build_artifact(evidence)
    artifact = package.artifact
    spec = evidence.spec
    assert artifact.strategy_spec == spec.ref
    assert artifact.dependencies[str(spec.ref)] == spec.content_hash()
    assert str(SIGNAL_REF) in artifact.dependencies
    assert artifact.validation_reports == ("TEST-ONLY-in-sample", "TEST-ONLY-sealed-oos")
    assert artifact.experiment_hashes == (evidence.experiments[0].experiment_hash,)
    assert artifact.research_code_commit == evidence.research_code.commit_oid
    assert artifact.research_code_tree_hash == evidence.research_code.tree_oid
    golden = artifact.golden_outputs
    assert golden.dataset_snapshot_id == TEST_ONLY_SNAPSHOT
    assert golden.signals_hash == payload_hash(package.signals_payload)
    assert golden.positions_hash == payload_hash(package.positions_payload)
    assert decode_golden_signals(package.signals_payload) == tuple(evidence.golden_inputs)
    answers = decode_golden_positions(package.positions_payload)
    expected = [FakeSignStrategy([spec]).target_positions(r) for r in evidence.golden_inputs]
    assert [a.positions for a in answers] == [r.positions for r in expected]
    # same evidence → same artifact id (content addressed; created_at is explicit)
    assert build_artifact(toy_evidence()).artifact.artifact_id == artifact.artifact_id
    # the artifact round-trips as its contract
    assert StrategyArtifact.model_validate(artifact.model_dump(mode="json")) == artifact


def test_paper_is_the_earliest_promotable_state() -> None:
    spec = toy_spec()
    for path in (
        PATH_TO_PAPER,
        PATH_TO_PRODUCTION_CANDIDATE,
        (*PATH_TO_PRODUCTION_CANDIDATE, S.ACTIVE),
    ):
        build_artifact(toy_evidence(lifecycle=history(spec.ref, path)))


def test_promote_stores_blobs_and_registers(tmp_path: Path) -> None:
    with StrategyRegistry(tmp_path / "registry") as registry:
        artifact = promote(toy_evidence(), registry)
        assert registry.artifact(artifact.artifact_id) == artifact
        assert len(registry) == 1
        with pytest.raises(PromotionRefused) as caught:
            promote(toy_evidence(), registry)
        assert caught.value.reason is R.ALREADY_REGISTERED
        assert len(registry) == 1
    with StrategyRegistry(tmp_path / "registry") as reopened:
        assert reopened.golden_inputs(artifact.artifact_id) == toy_evidence().golden_inputs


def test_a_refused_promotion_writes_nothing(tmp_path: Path) -> None:
    with StrategyRegistry(tmp_path / "registry") as registry:
        with pytest.raises(PromotionRefused):
            promote(toy_evidence(reports=()), registry)
        assert len(registry) == 0
    assert not (tmp_path / "registry" / "blobs").exists()


# ---- reports ---------------------------------------------------------------------------


def test_no_report_is_refused() -> None:
    assert _refusal(toy_evidence(reports=())).reason is R.NO_VALIDATION_REPORT


def test_a_report_about_another_subject_is_refused() -> None:
    evidence = toy_evidence()
    other = evidence.reports[0].model_copy(update={"subject": OTHER_STRATEGY})
    other = ValidationReport.model_validate(other.model_dump(mode="json"))
    refused = _refusal(replace(evidence, reports=(other, evidence.reports[1])))
    assert refused.reason is R.REPORT_SUBJECT_MISMATCH


@pytest.mark.parametrize("verdict", [Verdict.FAIL, Verdict.INCONCLUSIVE])
@pytest.mark.parametrize("stage", ["G0", "G2", "G4"])
def test_a_non_pass_in_sample_report_is_refused(verdict: Verdict, stage: str) -> None:
    evidence = toy_evidence()
    spec, experiment = evidence.spec, evidence.experiments[0]
    bad = toy_report(
        spec,
        experiment,
        "TEST-ONLY-bad",
        ("G0", "G1", "G2", "G3", "G4"),
        failing=stage,
        verdict=verdict,
    )
    refused = _refusal(replace(evidence, reports=(bad, evidence.reports[1])))
    assert refused.reason is R.VERDICT_NOT_PASS
    assert refused.reason.value == "verdict_not_pass"


def test_a_non_pass_sealed_oos_report_is_refused() -> None:
    evidence = toy_evidence()
    bad = toy_report(evidence.spec, evidence.experiments[0], "TEST-ONLY-oos", ("G5",), failing="G5")
    refused = _refusal(replace(evidence, reports=(evidence.reports[0], bad)))
    assert refused.reason is R.VERDICT_NOT_PASS


@pytest.mark.parametrize("missing", ["G0", "G1", "G2", "G3", "G4"])
def test_a_missing_in_sample_stage_is_refused(missing: str) -> None:
    evidence = toy_evidence()
    stages = tuple(s for s in ("G0", "G1", "G2", "G3", "G4") if s != missing)
    partial = toy_report(evidence.spec, evidence.experiments[0], "TEST-ONLY-partial", stages)
    refused = _refusal(replace(evidence, reports=(partial, evidence.reports[1])))
    assert refused.reason is R.STAGE_NOT_EVALUATED
    assert missing in refused.detail


def test_without_a_sealed_oos_pass_nothing_is_promoted() -> None:
    evidence = toy_evidence()
    refused = _refusal(replace(evidence, reports=(evidence.reports[0],)))
    assert refused.reason is R.SEALED_OOS_NOT_EVALUATED
    assert refused.reason.value == "sealed_oos_not_evaluated"


def test_a_constructed_report_that_lies_about_its_verdict_is_refused() -> None:
    evidence = toy_evidence()
    honest = toy_report(
        evidence.spec,
        evidence.experiments[0],
        "TEST-ONLY-lie",
        ("G0", "G1", "G2", "G3", "G4"),
        failing="G3",
    )
    lying = ValidationReport.model_construct(
        **{**dict(honest), "verdict": Verdict.PASS}  # bypasses derive_verdict
    )
    refused = _refusal(replace(evidence, reports=(lying, evidence.reports[1])))
    assert refused.reason is R.EVIDENCE_INVALID


def test_a_duplicate_report_id_is_refused() -> None:
    evidence = toy_evidence()
    refused = _refusal(replace(evidence, reports=(*evidence.reports, evidence.reports[0])))
    assert refused.reason is R.EVIDENCE_INVALID


# ---- experiments, code, dependencies ---------------------------------------------------


def test_a_report_whose_experiment_is_missing_is_refused() -> None:
    assert _refusal(toy_evidence(experiments=())).reason is R.EXPERIMENT_MISSING


def test_an_uncited_experiment_is_refused() -> None:
    evidence = toy_evidence()
    extra = toy_experiment(evidence.spec, seeds=(7,))
    refused = _refusal(replace(evidence, experiments=(*evidence.experiments, extra)))
    assert refused.reason is R.EXPERIMENT_NOT_EVIDENCED


def test_an_experiment_of_another_strategy_is_refused() -> None:
    evidence = toy_evidence()
    spec = evidence.spec
    other = toy_experiment(
        spec,
        strategy_ref=OTHER_STRATEGY,
        dependency_hashes={
            **evidence.experiments[0].repro.dependency_hashes,
            str(OTHER_STRATEGY): HASH_A,
        },
    )
    reports = tuple(
        toy_report(spec, other, r.report_id, sorted({g.gate_id.split(".")[0] for g in r.gates}))
        for r in evidence.reports
    )
    refused = _refusal(replace(evidence, experiments=(other,), reports=reports))
    assert refused.reason is R.EXPERIMENT_MISMATCH


def test_an_experiment_bound_to_another_spec_hash_is_refused() -> None:
    evidence = toy_evidence()
    spec = evidence.spec
    deps = dict(evidence.experiments[0].repro.dependency_hashes)
    deps[str(spec.ref)] = HASH_A
    other = toy_experiment(spec, dependency_hashes=deps)
    reports = tuple(
        toy_report(spec, other, r.report_id, sorted({g.gate_id.split(".")[0] for g in r.gates}))
        for r in evidence.reports
    )
    refused = _refusal(replace(evidence, experiments=(other,), reports=reports))
    assert refused.reason is R.EXPERIMENT_MISMATCH


def test_a_report_with_another_profile_binding_is_refused() -> None:
    evidence = toy_evidence()
    spec, experiment = evidence.spec, evidence.experiments[0]
    drifted = toy_report(
        spec,
        experiment,
        "TEST-ONLY-in-sample",
        ("G0", "G1", "G2", "G3", "G4"),
        validation_profile_hash="9" * 64,
    )
    assert HASH_PROFILE != "9" * 64
    refused = _refusal(replace(evidence, reports=(drifted, evidence.reports[1])))
    assert refused.reason is R.EXPERIMENT_MISMATCH


def test_research_code_of_another_commit_is_refused() -> None:
    evidence = toy_evidence(research_code=git_code_revision(commit_oid=OTHER_GIT_COMMIT_OID))
    assert _refusal(evidence).reason is R.RESEARCH_CODE_MISMATCH


def test_an_unbound_signal_dependency_is_refused() -> None:
    assert _refusal(toy_evidence(signal_dependencies={})).reason is R.DEPENDENCY_UNBOUND


def test_conflicting_dependency_hashes_are_refused() -> None:
    spec = toy_spec()
    evidence = toy_evidence(signal_dependencies={str(SIGNAL_REF): HASH_A, str(spec.ref): HASH_A})
    assert _refusal(evidence).reason is R.DEPENDENCY_CONFLICT


# ---- validation profile (C-A8) ---------------------------------------------------------


def _rebound(evidence: PromotionEvidence, profile: ValidationProfile) -> PromotionEvidence:
    """``evidence`` with its reports and experiment re-run under ``profile``."""
    spec = evidence.spec
    experiment = toy_experiment(spec, profile=profile)
    reports = tuple(
        toy_report(spec, experiment, r.report_id, sorted({g.gate_id[:2] for g in r.gates}))
        for r in evidence.reports
    )
    return replace(evidence, reports=reports, experiments=(experiment,), profiles=(profile,))


def _under(profile: ValidationProfile) -> PromotionEvidence:
    return _rebound(toy_evidence(), profile)


def test_the_toy_happy_path_runs_under_a_frozen_calibrated_profile() -> None:
    evidence = toy_evidence()
    (profile,) = evidence.profiles
    assert profile.status is ProfileStatus.FROZEN
    assert profile.provenance.calibration_report is not None
    assert {r.validation_profile_hash for r in evidence.reports} == {profile.content_hash()}
    build_artifact(_under(profile))


def test_a_draft_profile_is_refused() -> None:
    draft = toy_profile(status=ProfileStatus.DRAFT)
    assert draft.content_hash() == toy_profile().content_hash()  # status is not in the hash
    refused = _refusal(_under(draft))
    assert refused.reason is R.PROFILE_NOT_FROZEN
    assert refused.reason.value == "profile_not_frozen"
    uncalibrated_draft = toy_profile(status=ProfileStatus.DRAFT, calibration_report=None)
    assert _refusal(_under(uncalibrated_draft)).reason is R.PROFILE_NOT_FROZEN


def test_a_superseded_profile_is_refused() -> None:
    superseded = toy_profile(status=ProfileStatus.SUPERSEDED)
    assert _refusal(_under(superseded)).reason is R.PROFILE_NOT_FROZEN


@pytest.mark.parametrize("calibration", [None, "   "])
def test_a_frozen_profile_without_calibration_provenance_is_refused(
    calibration: str | None,
) -> None:
    # the contract refuses FROZEN without calibration_report; a constructed object bypasses it
    real = toy_profile(status=ProfileStatus.DRAFT, calibration_report=calibration)
    forged = ValidationProfile.model_construct(**{**dict(real), "status": ProfileStatus.FROZEN})
    refused = _refusal(_under(forged))
    assert refused.reason is R.PROFILE_NOT_CALIBRATED
    assert refused.reason.value == "profile_not_calibrated"


def test_a_missing_profile_is_refused() -> None:
    assert _refusal(toy_evidence(profiles=())).reason is R.PROFILE_MISSING
    unrelated = toy_profile(name="other_scope")
    assert _refusal(toy_evidence(profiles=(unrelated,))).reason is R.PROFILE_MISSING


def test_a_profile_whose_hash_differs_from_the_reports_is_refused() -> None:
    other = toy_profile(calibration_report="TEST-ONLY-another-calibration-reference")
    assert other.ref == toy_profile().ref
    assert other.content_hash() != toy_profile().content_hash()
    refused = _refusal(toy_evidence(profiles=(other,)))
    assert refused.reason is R.PROFILE_HASH_MISMATCH
    assert refused.reason.value == "profile_hash_mismatch"


def test_an_uncited_or_repeated_profile_is_refused() -> None:
    extra = toy_profile(name="other_scope")
    assert _refusal(toy_evidence(profiles=(toy_profile(), extra))).reason is (
        R.PROFILE_NOT_EVIDENCED
    )
    assert _refusal(toy_evidence(profiles=(toy_profile(), toy_profile()))).reason is (
        R.PROFILE_NOT_EVIDENCED
    )


def test_something_other_than_a_profile_is_refused() -> None:
    evidence = toy_evidence(profiles=("not a profile",))
    assert _refusal(evidence).reason is R.EVIDENCE_INVALID


# ---- lifecycle -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        (S.IDEA,),
        (S.IDEA, S.CANDIDATE, S.VALIDATION),
        (S.IDEA, S.CANDIDATE, S.VALIDATION, S.OOS),
        (*PATH_TO_PAPER, S.REJECTED),
        (*PATH_TO_PAPER, S.RETIRED),
        (*PATH_TO_PRODUCTION_CANDIDATE, S.ACTIVE, S.DEGRADED),
        (*PATH_TO_PRODUCTION_CANDIDATE, S.ACTIVE, S.DEGRADED, S.REVALIDATION),
    ],
    ids=lambda path: path[-1].value,
)
def test_a_lifecycle_before_paper_or_off_the_path_is_refused(path: tuple[S, ...]) -> None:
    evidence = toy_evidence(lifecycle=history(toy_spec().ref, path))
    refused = _refusal(evidence)
    assert refused.reason is R.LIFECYCLE_NOT_ELIGIBLE


def test_a_lifecycle_of_another_subject_is_refused() -> None:
    evidence = toy_evidence(lifecycle=history(OTHER_STRATEGY, PATH_TO_PAPER))
    assert _refusal(evidence).reason is R.LIFECYCLE_SUBJECT_MISMATCH


def test_a_constructed_lifecycle_that_skips_the_human_approval_is_refused() -> None:
    spec = toy_spec()
    real = history(spec.ref, PATH_TO_PAPER)
    unapproved = tuple(
        LifecycleTransition.model_construct(**{**dict(t), "approved_by": None})
        for t in real.transitions
    )
    forged = LifecycleHistory.model_construct(subject=spec.ref, transitions=unapproved)
    assert forged.current_state is S.PAPER
    assert _refusal(toy_evidence(lifecycle=forged)).reason is R.EVIDENCE_INVALID


def test_an_artifact_may_not_predate_its_evidence() -> None:
    evidence = toy_evidence(created_at=REPORT_TIME - timedelta(days=1))
    assert _refusal(evidence).reason is R.EVIDENCE_INVALID
    last = evidence.lifecycle.transitions[-1].occurred_at
    assert _refusal(toy_evidence(created_at=last - timedelta(seconds=1))).reason is (
        R.EVIDENCE_INVALID
    )
    assert ARTIFACT_TIME > last


# ---- golden outputs --------------------------------------------------------------------


def test_missing_golden_inputs_or_snapshot_are_refused() -> None:
    assert _refusal(toy_evidence(golden_inputs=())).reason is R.GOLDEN_INPUTS_MISSING
    assert _refusal(toy_evidence(dataset_snapshot_id="  ")).reason is R.GOLDEN_INPUTS_MISSING


def test_golden_inputs_for_another_spec_hash_are_refused() -> None:
    spec = toy_spec()
    evidence = toy_evidence(golden_inputs=(golden_request(spec, spec_hash=HASH_A),))
    assert _refusal(evidence).reason is R.GOLDEN_INPUT_MISMATCH


def test_golden_inputs_that_override_params_are_refused() -> None:
    spec = toy_spec()
    request = golden_request(spec, params=FrozenMapping({"lookback": 3}))
    assert _refusal(toy_evidence(golden_inputs=(request,))).reason is R.GOLDEN_INPUT_MISMATCH


def test_duplicate_golden_inputs_are_refused() -> None:
    request = golden_request(toy_spec())
    evidence = toy_evidence(golden_inputs=(request, request))
    assert _refusal(evidence).reason is R.GOLDEN_INPUT_MISMATCH


def test_a_provider_that_does_not_declare_the_spec_is_refused() -> None:
    other_spec = toy_spec().model_copy(update={"name": "not_toy"})
    evidence = toy_evidence(research_provider=FakeSignStrategy([other_spec]))
    assert _refusal(evidence).reason is R.PROVIDER_UNSUPPORTED


def test_a_provider_whose_answers_fail_check_answers_is_refused() -> None:
    spec = toy_spec()
    evidence = toy_evidence(research_provider=RequestShiftingStrategy(FakeSignStrategy([spec])))
    assert _refusal(evidence).reason is R.GOLDEN_OUTPUT_INVALID


def test_a_non_deterministic_provider_is_refused() -> None:
    evidence = toy_evidence(research_provider=FlakyStrategy(toy_spec()))
    assert _refusal(evidence).reason is R.GOLDEN_OUTPUT_NOT_DETERMINISTIC


# ---- the real library ------------------------------------------------------------------


@pytest.mark.parametrize("entry", library_entries(), ids=lambda entry: entry.spec.name)
def test_no_library_strategy_can_be_promoted_today(entry: object, tmp_path: Path) -> None:
    """Complete TEST ONLY evidence for each library strategy (G0–G4 + G5 PASS reports, the bound
    experiment, a human-approved lifecycle, golden inputs, every dependency bound) run under a
    Profile that is not frozen — as every Profile is today — is refused with the Profile reason
    (C-A8), writing nothing."""
    from research.strategies.library import LibraryEntry

    assert isinstance(entry, LibraryEntry)
    spec = entry.spec
    draft = toy_profile(status=ProfileStatus.DRAFT, calibration_report=None)
    experiment = toy_experiment(spec, profile=draft)
    required = [*spec.signals, *([spec.risk_policy] if spec.risk_policy is not None else [])]
    evidence = PromotionEvidence(
        spec=spec,
        reports=(
            toy_report(spec, experiment, "TEST-ONLY-in-sample", ("G0", "G1", "G2", "G3", "G4")),
            toy_report(spec, experiment, "TEST-ONLY-sealed-oos", ("G5",)),
        ),
        profiles=(draft,),
        experiments=(experiment,),
        lifecycle=history(spec.ref, PATH_TO_PRODUCTION_CANDIDATE),
        research_code=git_code_revision(),
        research_provider=entry.candidate().strategy,
        golden_inputs=(golden_request(spec),),
        dataset_snapshot_id=TEST_ONLY_SNAPSHOT,
        created_at=ARTIFACT_TIME,
        signal_dependencies={str(ref): HASH_A for ref in required},
    )
    with StrategyRegistry(tmp_path / "registry") as registry:
        with pytest.raises(PromotionRefused) as caught:
            promote(evidence, registry)
        assert caught.value.reason is R.PROFILE_NOT_FROZEN
        assert str(draft.ref) in caught.value.detail
        assert len(registry) == 0
    # the same bundle under the (TEST ONLY) frozen twin of the Profile gets past the Profile
    # check: the refusal above is the Profile's, not a gap elsewhere in the bundle
    try:
        build_artifact(_rebound(evidence, toy_profile()))
    except PromotionRefused as refused:
        assert not refused.reason.value.startswith("profile_")

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
    ProfileFreezeRegistry,
    RegistryCorrupted,
    StrategyRegistry,
    decode_golden_positions,
    decode_golden_signals,
    payload_hash,
)
from research.promotion import (
    PromotionEvidence,
    PromotionPackage,
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
    TEST_ONLY_CALIBRATION,
    TEST_ONLY_SNAPSHOT,
    TOY_CALIBRATION_REPORT,
    TOY_FREEZE_APPROVER,
    TOY_FREEZE_TIME,
    FlakyStrategy,
    golden_request,
    history,
    toy_evidence,
    toy_experiment,
    toy_freezes,
    toy_profile,
    toy_report,
    toy_spec,
)

S = LifecycleState
R = PromotionRefusal
OTHER_STRATEGY = Ref(kind=Kind.STRATEGY, name="someone_else", version="1.0.0")


def _build(evidence: PromotionEvidence, *frozen: ValidationProfile) -> PromotionPackage:
    """``build_artifact`` against a TEST ONLY freeze registry in which ``frozen`` (default: the toy
    Profile) — and nothing else — is registered as frozen (ADR-0062)."""
    with toy_freezes(*frozen) as freezes:
        return build_artifact(evidence, freezes=freezes)


def _promote(evidence: PromotionEvidence, registry: StrategyRegistry) -> StrategyArtifact:
    """``promote`` against a TEST ONLY freeze registry holding the toy Profile (ADR-0062)."""
    with toy_freezes() as freezes:
        return promote(evidence, registry, freezes=freezes)


def _refusal(evidence: PromotionEvidence, *frozen: ValidationProfile) -> PromotionRefused:
    with pytest.raises(PromotionRefused) as caught:
        _build(evidence, *frozen)
    return caught.value


# ---- happy path ------------------------------------------------------------------------


def test_the_toy_strategy_builds_a_deterministic_artifact() -> None:
    evidence = toy_evidence()
    package = _build(evidence)
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
    assert _build(toy_evidence()).artifact.artifact_id == artifact.artifact_id
    # the artifact round-trips as its contract
    assert StrategyArtifact.model_validate(artifact.model_dump(mode="json")) == artifact


def test_paper_is_the_earliest_promotable_state() -> None:
    spec = toy_spec()
    for path in (
        PATH_TO_PAPER,
        PATH_TO_PRODUCTION_CANDIDATE,
        (*PATH_TO_PRODUCTION_CANDIDATE, S.ACTIVE),
    ):
        _build(toy_evidence(lifecycle=history(spec.ref, path)))


def test_promote_stores_blobs_and_registers(tmp_path: Path) -> None:
    with StrategyRegistry(tmp_path / "registry") as registry:
        artifact = _promote(toy_evidence(), registry)
        assert registry.artifact(artifact.artifact_id) == artifact
        assert len(registry) == 1
        with pytest.raises(PromotionRefused) as caught:
            _promote(toy_evidence(), registry)
        assert caught.value.reason is R.ALREADY_REGISTERED
        assert len(registry) == 1
    with StrategyRegistry(tmp_path / "registry") as reopened:
        assert reopened.golden_inputs(artifact.artifact_id) == toy_evidence().golden_inputs


def test_a_refused_promotion_writes_nothing(tmp_path: Path) -> None:
    with StrategyRegistry(tmp_path / "registry") as registry:
        with pytest.raises(PromotionRefused):
            _promote(toy_evidence(reports=()), registry)
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
    _build(_under(profile))


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


def _stages(report: ValidationReport) -> list[str]:
    return sorted({g.gate_id[:2] for g in report.gates})


def test_a_g2_report_without_the_market_benchmark_item_is_refused() -> None:
    evidence = toy_evidence()
    spec, experiment = evidence.spec, evidence.experiments[0]
    assert "G2.market_benchmark.flat" in {g.gate_id for g in evidence.reports[0].gates}
    bare = toy_report(
        spec, experiment, "TEST-ONLY-in-sample", _stages(evidence.reports[0]), market_benchmark=None
    )
    refused = _refusal(replace(evidence, reports=(bare, evidence.reports[1])))
    assert refused.reason is R.MARKET_BENCHMARK_MISSING
    assert refused.reason.value == "market_benchmark_missing"
    assert "G2.market_benchmark.flat" in refused.detail


def test_the_market_benchmark_item_must_be_the_profiles_rule() -> None:
    profile = toy_profile(market_benchmark_rule="buy_and_hold_equal_weight")
    # frozen (registered), so the refusal is the benchmark item's, not the freeze's
    refused = _refusal(_under(profile), profile)  # the toy reports carry .flat only
    assert refused.reason is R.MARKET_BENCHMARK_MISSING
    assert "G2.market_benchmark.buy_and_hold_equal_weight" in refused.detail


def test_an_unregistered_rule_needs_its_inconclusive_item() -> None:
    # an unregistered rule is reported as a bare INCONCLUSIVE G2.market_benchmark; a report that
    # lacks even that is refused as missing (with it, the verdict is not PASS anyway)
    profile = toy_profile(market_benchmark_rule="TEST-ONLY-unregistered")
    refused = _refusal(_under(profile), profile)
    assert refused.reason is R.MARKET_BENCHMARK_MISSING
    assert refused.detail.endswith("G2.market_benchmark")


def _without_inverse(evidence: PromotionEvidence, item: str | None = None) -> PromotionEvidence:
    """``evidence`` whose G2 report carries ``item`` (default: none) for G2.inverse_control."""
    spec, experiment, in_sample = evidence.spec, evidence.experiments[0], evidence.reports[0]
    bare = toy_report(
        spec, experiment, in_sample.report_id, _stages(in_sample), inverse_control=item
    )
    return replace(evidence, reports=(bare, *evidence.reports[1:]))


def test_a_g2_report_without_the_inverse_control_item_is_refused() -> None:
    evidence = toy_evidence()
    (profile,) = evidence.profiles
    assert profile.benchmark.inverse_control_reported  # the toy Profile reports it
    in_sample, sealed = evidence.reports
    assert "G2.inverse_control" in {g.gate_id for g in in_sample.gates}
    # the sealed-OOS report does not evaluate G2, so it needs no G2 item (the happy path)
    assert "G2" not in _stages(sealed)
    assert "G2.inverse_control" not in {g.gate_id for g in sealed.gates}
    # frozen (registered) by _build, so the refusal is the inverse control's, not the freeze's
    refused = _refusal(_without_inverse(evidence))
    assert refused.reason is R.INVERSE_CONTROL_MISSING
    assert refused.reason.value == "inverse_control_missing"
    assert "TEST-ONLY-in-sample" in refused.detail
    assert "benchmark.inverse_control_reported=true" in refused.detail
    assert refused.detail.endswith("without the ADR-0060 item G2.inverse_control")
    # the gate id must match exactly: no prefix, suffix or case folding
    for near in ("G2.inverse_control.flat", "G2.inverse", "g2.inverse_control"):
        near_miss = _refusal(_without_inverse(evidence, near))
        assert near_miss.reason is R.INVERSE_CONTROL_MISSING, near


def test_the_inverse_control_is_checked_after_the_freeze_and_the_market_benchmark() -> None:
    evidence = toy_evidence()
    spec, experiment, in_sample = evidence.spec, evidence.experiments[0], evidence.reports[0]
    neither = toy_report(
        spec,
        experiment,
        in_sample.report_id,
        _stages(in_sample),
        market_benchmark=None,
        inverse_control=None,
    )
    both_missing = replace(evidence, reports=(neither, evidence.reports[1]))
    assert _refusal(both_missing).reason is R.MARKET_BENCHMARK_MISSING
    # not frozen in the registry (another Profile is): the freeze refusal comes first
    other = toy_profile(market_benchmark_rule="none")
    assert other.content_hash() != toy_profile().content_hash()
    assert _refusal(_without_inverse(evidence), other).reason is R.PROFILE_NOT_FROZEN


def test_a_profile_that_does_not_report_the_inverse_control_needs_no_item() -> None:
    profile = toy_profile(inverse_control_reported=False)
    assert profile.content_hash() != toy_profile().content_hash()
    evidence = toy_evidence()
    spec = evidence.spec
    experiment = toy_experiment(spec, profile=profile)
    reports = tuple(
        toy_report(spec, experiment, r.report_id, _stages(r), inverse_control=None)
        for r in evidence.reports
    )
    _build(
        replace(evidence, reports=reports, experiments=(experiment,), profiles=(profile,)),
        profile,
    )


def test_a_profile_with_rule_none_needs_no_market_benchmark_item() -> None:
    profile = toy_profile(market_benchmark_rule="none")
    evidence = toy_evidence()
    spec = evidence.spec
    experiment = toy_experiment(spec, profile=profile)
    reports = tuple(
        toy_report(spec, experiment, r.report_id, _stages(r), market_benchmark=None)
        for r in evidence.reports
    )
    _build(
        replace(evidence, reports=reports, experiments=(experiment,), profiles=(profile,)),
        profile,
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
            _promote(evidence, registry)
        assert caught.value.reason is R.PROFILE_NOT_FROZEN
        assert str(draft.ref) in caught.value.detail
        assert len(registry) == 0
    # the same bundle under the (TEST ONLY) frozen twin of the Profile gets past the Profile
    # check: the refusal above is the Profile's, not a gap elsewhere in the bundle
    try:
        _build(_rebound(evidence, toy_profile()))
    except PromotionRefused as refused:
        assert not refused.reason.value.startswith("profile_")


# ---- ADR-0062: the Profile freeze registry is the authoritative freeze source ----------------


def _empty_freezes(tmp_path: Path) -> ProfileFreezeRegistry:
    """An open, anchored, **empty** Profile freeze registry (temp dir + sibling anchor)."""
    return ProfileFreezeRegistry(tmp_path / "freezes", anchor=tmp_path / "freezes.anchor.jsonl")


def test_a_registered_freeze_lets_complete_evidence_promote(tmp_path: Path) -> None:
    evidence = toy_evidence()
    (profile,) = evidence.profiles
    with toy_freezes(profile, root=tmp_path) as freezes:
        record = freezes.frozen_record(profile)
        assert record is not None and record.approved_by == TOY_FREEZE_APPROVER
        package = build_artifact(evidence, freezes=freezes)
        with StrategyRegistry(tmp_path / "registry") as registry:
            artifact = promote(evidence, registry, freezes=freezes)
            assert registry.has_artifact(artifact.artifact_id)
    assert artifact == package.artifact


def test_status_frozen_without_a_registered_freeze_is_refused(tmp_path: Path) -> None:
    """ADR-0008: ``status`` is not in the content hash, so the object's FROZEN is not evidence."""
    evidence = toy_evidence()
    (profile,) = evidence.profiles
    assert profile.status is ProfileStatus.FROZEN
    assert profile.provenance.calibration_report is not None
    with _empty_freezes(tmp_path) as freezes:
        with pytest.raises(PromotionRefused) as caught:
            build_artifact(evidence, freezes=freezes)
        with StrategyRegistry(tmp_path / "registry") as registry:
            with pytest.raises(PromotionRefused):
                promote(evidence, registry, freezes=freezes)
            assert len(registry) == 0  # nothing written
    assert caught.value.reason is R.PROFILE_NOT_FROZEN
    assert "not authoritative" in caught.value.detail


def test_a_freeze_of_another_content_hash_of_the_ref_is_refused() -> None:
    evidence = toy_evidence()
    (profile,) = evidence.profiles
    other = toy_profile(market_benchmark_rule="buy_and_hold_equal_weight")  # same ref, other hash
    assert other.ref == profile.ref and other.content_hash() != profile.content_hash()
    refused = _refusal(evidence, other)
    assert refused.reason is R.PROFILE_NOT_FROZEN
    assert profile.content_hash() in refused.detail


def test_a_draft_twin_of_a_registered_profile_is_still_refused() -> None:
    """The record answers for the content (hash); the object's own status must also be FROZEN."""
    frozen = toy_profile()
    draft = toy_profile(
        status=ProfileStatus.DRAFT, calibration_report=frozen.provenance.calibration_report
    )
    assert draft.content_hash() == frozen.content_hash()
    assert _refusal(_under(draft), frozen).reason is R.PROFILE_NOT_FROZEN


def test_a_closed_or_poisoned_freeze_registry_cannot_support_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = toy_evidence()
    with toy_freezes(root=tmp_path / "closed") as closed:
        pass  # closed on exit
    with pytest.raises(PromotionRefused) as caught:
        build_artifact(evidence, freezes=closed)
    assert caught.value.reason is R.PROFILE_NOT_FROZEN and "cannot answer" in caught.value.detail

    other = toy_profile(name="test_only_poison_trigger", calibration_report=TEST_ONLY_CALIBRATION)
    with toy_freezes(root=tmp_path / "poisoned") as poisoned:
        monkeypatch.setattr(poisoned._anchor, "append", _failing_append)
        with pytest.raises(RegistryCorrupted):
            poisoned.register_freeze(
                other,
                TOY_CALIBRATION_REPORT,
                approved_by=TOY_FREEZE_APPROVER,
                approved_at=TOY_FREEZE_TIME,
            )
        with pytest.raises(PromotionRefused) as caught:
            build_artifact(evidence, freezes=poisoned)  # its memory still holds the toy freeze
    assert caught.value.reason is R.PROFILE_NOT_FROZEN and "poisoned" in caught.value.detail


def _failing_append(*_args: object, **_kwargs: object) -> None:
    raise OSError("TEST ONLY: the anchor disk is gone")


@pytest.mark.parametrize("freezes", [None, "a path, not a registry", object()])
def test_without_a_freeze_registry_nothing_is_promoted(freezes: object) -> None:
    with pytest.raises(PromotionRefused) as caught:
        build_artifact(toy_evidence(), freezes=freezes)  # type: ignore[arg-type]
    assert caught.value.reason is R.PROFILE_NOT_FROZEN


def test_an_artifact_may_not_predate_the_profile_freeze_approval(tmp_path: Path) -> None:
    evidence = toy_evidence(created_at=TOY_FREEZE_TIME - timedelta(seconds=1))
    assert evidence.created_at > REPORT_TIME  # after every report: only the freeze is later
    with toy_freezes(root=tmp_path) as freezes, pytest.raises(PromotionRefused) as caught:
        build_artifact(evidence, freezes=freezes)
    assert caught.value.reason is R.EVIDENCE_INVALID
    assert "freeze approval" in caught.value.detail

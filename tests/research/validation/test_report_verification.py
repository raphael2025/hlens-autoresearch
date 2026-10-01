"""ADR-0013 report ↔ Profile verification (``research.validation.verification``).

!!! TEST ONLY !!! Every number is an arbitrary, uncalibrated fixture value (the TEST ONLY Profile of
``fixtures`` and its copies); none is a proposal or a calibration result.

A report the pipeline produced under a Profile verifies against that Profile; a report whose
thresholded gates do not agree with the Profile instance (unknown source, other value, non-canonical
source, a ``param:`` overriding a Profile field, a verdict contradicting the metric's comparison,
another Profile) is listed with a named discrepancy — never repaired, never silently accepted.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest

from core.contracts.outcome import OutcomeEvent
from core.contracts.synthetic import SyntheticMarket
from core.contracts.validation_profile import CrossAssetParams, ValidationProfile
from core.domain.research import GateResult, ValidationReport, Verdict
from research.outcomes import OutcomeTable
from research.validation import build_report, run_validation
from research.validation.calibration import MomentumSignStudy
from research.validation.gate_set import stage_of
from research.validation.verification import ReportDiscrepancy, verify_report
from tests.research.validation.fixtures import (
    TEST_ONLY_PROFILE,
    context,
    generate,
    in_sample_input,
    outcome_table,
    research_events,
)

#: ADR-0086 decision 1: every stage a "complete" in-sample report needs, other than the ones the
#: gate(s) under test already carry. Filler gates carry no threshold, so they add no discrepancy
#: of their own (``verify_report`` only compares thresholded gates) and exist only to make the
#: report's stage set complete for tests that are not themselves about gate-set completeness.
_FILLER_STAGES = ("G0", "G1", "G2", "G3", "G4")


def _filler_gates(present: frozenset[str] = frozenset()) -> list[GateResult]:
    return [
        GateResult(gate_id=f"{stage}.filler", metric="filler", value=1.0, verdict=Verdict.PASS)
        for stage in _FILLER_STAGES
        if stage not in present
    ]


def _with(profile: ValidationProfile, group: str, **update: Any) -> ValidationProfile:
    block = getattr(profile, group).model_copy(update=update)
    return profile.model_copy(update={group: block})


@pytest.fixture(scope="module")
def planted() -> tuple[SyntheticMarket, OutcomeTable]:
    market = generate(seed=3, strength="0.6")
    return market, outcome_table(market, research_events(market))


def _pipeline_report(
    planted: tuple[SyntheticMarket, OutcomeTable], profile: ValidationProfile = TEST_ONLY_PROFILE
) -> ValidationReport:
    """A real pipeline report, G0 - G4 (ADR-0086 decision 1: a gate-set-complete in-sample
    report). No robustness input is given: G4 collapses to the single
    ``G4.robustness_input`` INCONCLUSIVE gate (``research.validation.g4``), which is enough to
    satisfy the G4 stage requirement without building a ``RobustnessInput`` fixture; the report's
    overall verdict is then INCONCLUSIVE, not PASS — no test in this file depends on PASS."""
    market, table = planted
    events = [OutcomeEvent(event_key=x.event_key, event_time=x.event_time) for x in table]
    ctx = context(profile)
    in_sample = in_sample_input(table, MomentumSignStudy(market, events), ctx=ctx)
    run = run_validation(in_sample, robustness=None)
    return build_report(ctx, run.gates)


def _problems(report: ValidationReport, profile: ValidationProfile) -> set[ReportDiscrepancy]:
    return {item.problem for item in verify_report(report, profile).discrepancies}


def _replace_gate(report: ValidationReport, gate_id: str, **update: Any) -> ValidationReport:
    gates = [
        GateResult.model_validate({**gate.model_dump(), **update})
        if gate.gate_id == gate_id
        else gate
        for gate in report.gates
    ]
    return build_report(context(), gates)


def _hand_report(gates: Sequence[GateResult], profile: ValidationProfile) -> ValidationReport:
    """A report of exactly ``gates``, topped up with threshold-less filler gates (module docs,
    ``_filler_gates``) for every in-sample stage ``gates`` does not already touch, so that tests
    about one gate's threshold discrepancies are not also about ADR-0086 gate-set completeness."""
    present = frozenset(stage_of(gate.gate_id) for gate in gates)
    return build_report(context(profile), (*gates, *_filler_gates(present)))


def test_a_pipeline_report_verifies_against_its_profile(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    report = _pipeline_report(planted)
    verification = verify_report(report, TEST_ONLY_PROFILE)
    assert verification.ok, verification.to_dict()
    assert verification.gates_checked > 0  # G1 controls, G2 statistics, G3 all carry thresholds
    assert verification.to_dict()["discrepancies"] == []


def test_a_pipeline_report_under_an_exact_profile_verifies(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    exact = _with(
        TEST_ONLY_PROFILE, "significance", multiple_testing_threshold_exact=Decimal("0.01")
    )
    report = _pipeline_report(planted, exact)
    g3 = next(gate for gate in report.gates if gate.gate_id == "G3.adjusted_p_value")
    assert g3.threshold_source == "significance.multiple_testing_threshold_exact"
    assert verify_report(report, exact).ok


def test_the_float_source_of_a_field_with_an_exact_sibling_is_not_canonical(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    report = _pipeline_report(planted)  # float sources, under the float Profile
    exact = _with(
        TEST_ONLY_PROFILE, "significance", multiple_testing_threshold_exact=Decimal("0.01")
    )
    problems = _problems(report, exact)
    assert ReportDiscrepancy.PROFILE_NOT_BOUND in problems  # another Profile hash
    assert ReportDiscrepancy.SOURCE_NOT_CANONICAL in problems


def test_a_threshold_other_than_the_profiles_is_a_mismatch(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    report = _pipeline_report(planted)
    g3 = next(gate for gate in report.gates if gate.gate_id == "G3.adjusted_p_value")
    assert g3.threshold is not None
    tampered = _replace_gate(report, "G3.adjusted_p_value", threshold=g3.threshold * 10)
    assert _problems(tampered, TEST_ONLY_PROFILE) == {ReportDiscrepancy.THRESHOLD_MISMATCH}


def test_a_source_the_profile_does_not_have_is_named(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    report = _pipeline_report(planted)
    tampered = _replace_gate(
        report, "G3.adjusted_p_value", threshold_source="significance.no_such_threshold"
    )
    assert _problems(tampered, TEST_ONLY_PROFILE) == {ReportDiscrepancy.SOURCE_NOT_IN_PROFILE}


def test_a_verdict_contradicting_the_comparison_is_inconsistent(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    report = _pipeline_report(planted)
    g3 = next(gate for gate in report.gates if gate.gate_id == "G3.adjusted_p_value")
    assert g3.verdict is Verdict.PASS
    tampered = _replace_gate(report, "G3.adjusted_p_value", verdict=Verdict.FAIL)
    assert _problems(tampered, TEST_ONLY_PROFILE) == {ReportDiscrepancy.VERDICT_INCONSISTENT}


def test_inconclusive_is_never_contradicted() -> None:
    gate = GateResult(
        gate_id="G2.breakeven_cost_multiple",
        metric="breakeven_cost_multiple[>=]",
        value=99.0,  # far above the threshold: a band or an aggregation may still say INCONCLUSIVE
        threshold=1.5,
        threshold_source="cost_stress.min_breakeven_cost_multiple",
        verdict=Verdict.INCONCLUSIVE,
    )
    assert verify_report(_hand_report([gate], TEST_ONLY_PROFILE), TEST_ONLY_PROFILE).ok


def test_a_thresholded_metric_without_a_comparison_is_named() -> None:
    gate = GateResult(
        gate_id="G2.breakeven_cost_multiple",
        metric="breakeven_cost_multiple",
        value=2.0,
        threshold=1.5,
        threshold_source="cost_stress.min_breakeven_cost_multiple",
        verdict=Verdict.PASS,
    )
    report = _hand_report([gate], TEST_ONLY_PROFILE)
    assert _problems(report, TEST_ONLY_PROFILE) == {ReportDiscrepancy.DIRECTION_MISSING}


def test_an_indexed_profile_source_is_resolved() -> None:
    gate = GateResult(
        gate_id="G2.cost_stress.0",
        metric="breakeven_cost_multiple_vs_stress[>=]",
        value=1.0,
        threshold=2.0,
        threshold_source="cost_stress.stress_multipliers[0]",
        verdict=Verdict.FAIL,
    )
    assert verify_report(_hand_report([gate], TEST_ONLY_PROFILE), TEST_ONLY_PROFILE).ok


def _cross_asset_gate() -> GateResult:
    return GateResult(
        gate_id="G4.cross_asset.positive_fraction",
        metric="positive_asset_fraction[>=]",
        value=1.0,
        threshold=0.5,
        threshold_source="param:cross_asset.min_positive_fraction",
        verdict=Verdict.PASS,
    )


def test_a_param_source_is_accepted_when_the_profile_has_no_such_field() -> None:
    report = _hand_report([_cross_asset_gate()], TEST_ONLY_PROFILE)
    assert verify_report(report, TEST_ONLY_PROFILE).ok


def test_a_param_source_overriding_a_profile_field_is_named() -> None:
    carrying = TEST_ONLY_PROFILE.model_copy(
        update={"cross_asset": CrossAssetParams(min_positive_fraction=Decimal("0.6"))}
    )
    report = _hand_report([_cross_asset_gate()], carrying)
    assert _problems(report, carrying) == {ReportDiscrepancy.PARAM_OVERRIDES_PROFILE}


def test_an_exact_threshold_other_than_the_profiles_is_a_mismatch() -> None:
    gate = GateResult(
        gate_id="G2.breakeven_cost_multiple",
        metric="breakeven_cost_multiple[>=]",
        value=2.0,
        value_exact=Decimal("2"),
        threshold=1.5,
        threshold_exact=Decimal("1.5"),
        threshold_source="cost_stress.min_breakeven_cost_multiple",
        verdict=Verdict.PASS,
    )
    # the float Profile has no exact value, so a recorded exact threshold cannot be its own
    report = _hand_report([gate], TEST_ONLY_PROFILE)
    assert _problems(report, TEST_ONLY_PROFILE) == {ReportDiscrepancy.THRESHOLD_EXACT_MISMATCH}


def test_verify_report_refuses_non_contract_inputs(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    report = _pipeline_report(planted)
    with pytest.raises(TypeError):
        verify_report(report, object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        verify_report(object(), TEST_ONLY_PROFILE)  # type: ignore[arg-type]


# ---- ADR-0086 decision 1: gate-set completeness (research.validation.gate_set) ---------------


def _gate(gate_id: str, verdict: Verdict = Verdict.PASS) -> GateResult:
    return GateResult(gate_id=gate_id, metric="filler", value=1.0, verdict=verdict)


def _raw_report(gates: Sequence[GateResult], **overrides: Any) -> ValidationReport:
    """A report of exactly ``gates`` (no filler): unlike ``_hand_report``, for tests that are
    themselves about gate-set completeness."""
    payload = build_report(context(), gates).model_dump()
    payload.update(overrides)
    return ValidationReport.model_validate(payload)


def test_a_pipeline_report_is_gate_set_complete(
    planted: tuple[SyntheticMarket, OutcomeTable],
) -> None:
    report = _pipeline_report(planted)
    verification = verify_report(report, TEST_ONLY_PROFILE)
    assert not verification.gate_set_discrepancies, verification.to_dict()


def test_a_report_missing_an_in_sample_stage_is_flagged() -> None:
    report = _raw_report([_gate("G0.filler"), _gate("G1.filler"), _gate("G2.filler")])
    problems = verify_report(report, TEST_ONLY_PROFILE).gate_set_discrepancies
    assert {p.problem for p in problems} == {ReportDiscrepancy.GATE_STAGE_MISSING}
    assert {p.gate_id for p in problems} == {"G3", "G4"}


def test_a_report_under_an_unknown_stage_is_flagged() -> None:
    report = _raw_report(
        [_gate("G0.filler"), _gate("G1.filler"), _gate("G2.filler"), _gate("Gx.bogus")]
    )
    problems = verify_report(report, TEST_ONLY_PROFILE).gate_set_discrepancies
    unknown = [p for p in problems if p.problem is ReportDiscrepancy.GATE_STAGE_UNKNOWN]
    assert {p.gate_id for p in unknown} == {"Gx"}


def test_a_standalone_sealed_oos_report_is_gate_set_complete() -> None:
    """A G5-only report (``research/loop``'s standalone sealed-OOS report, and
    ``tests/promotion/fixtures.py::toy_evidence``'s sealed-OOS evidence report) is not "in-sample
    mode + G5" — see ``research.validation.gate_set`` module docs for why it is checked on its
    own, not held to the combined in-sample + G5 set."""
    report = _raw_report([_gate("G5.unsealing_recorded")])
    verification = verify_report(report, TEST_ONLY_PROFILE)
    assert not verification.gate_set_discrepancies, verification.to_dict()


def test_a_combined_report_needs_the_in_sample_stages_and_g5() -> None:
    present = [s for s in _FILLER_STAGES if s != "G3"]  # G3 left out on purpose
    gates = [_gate(f"{s}.filler") for s in present] + [_gate("G5.filler")]
    problems = verify_report(_raw_report(gates), TEST_ONLY_PROFILE).gate_set_discrepancies
    missing = {p.gate_id for p in problems if p.problem is ReportDiscrepancy.GATE_STAGE_MISSING}
    assert missing == {"G3"}


def test_an_unregistered_pipeline_version_is_flagged() -> None:
    gates = [_gate(f"{s}.filler") for s in _FILLER_STAGES]
    report = _raw_report(gates, constitution_version="9.9.9")
    problems = verify_report(report, TEST_ONLY_PROFILE).gate_set_discrepancies
    assert {p.problem for p in problems} == {ReportDiscrepancy.PIPELINE_VERSION_UNREGISTERED}
    assert problems[0].gate_id is None


def test_threshold_and_gate_set_discrepancies_are_reported_separately() -> None:
    """A report both threshold-mismatched and gate-set-incomplete carries both, split apart by
    ``threshold_discrepancies`` / ``gate_set_discrepancies`` (``research.promotion`` checks them
    in that order, module docs)."""
    mismatched = GateResult(
        gate_id="G2.breakeven_cost_multiple",
        metric="breakeven_cost_multiple[>=]",
        # >= the recorded (wrong) threshold: self-consistent, PASS, no VERDICT_INCONSISTENT
        value=20.0,
        threshold=1.5 * 10,  # the Profile's actual value is 1.5 (fixtures.TEST_ONLY_PROFILE)
        threshold_source="cost_stress.min_breakeven_cost_multiple",
        verdict=Verdict.PASS,
    )
    report = _raw_report([mismatched, _gate("G0.filler"), _gate("G1.filler")])
    verification = verify_report(report, TEST_ONLY_PROFILE)
    assert verification.threshold_discrepancies and {
        p.problem for p in verification.threshold_discrepancies
    } == {ReportDiscrepancy.THRESHOLD_MISMATCH}
    assert verification.gate_set_discrepancies and {
        p.problem for p in verification.gate_set_discrepancies
    } == {ReportDiscrepancy.GATE_STAGE_MISSING}
    assert not verification.ok

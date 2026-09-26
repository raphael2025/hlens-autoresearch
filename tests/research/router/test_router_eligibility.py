"""P10-ELIG: router eligibility bound to validation evidence (research level, paper only)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from core.domain.base import Ref
from core.domain.research import GateResult, ValidationReport, Verdict
from core.lifecycle.strategy import LifecycleState
from plugins.backtest import BarBacktester
from research.reports.router import write_router_paper_run, write_router_stop
from research.reports.validation import KIND as VALIDATION_KIND
from research.reports.validation import write_validation_report
from research.router import (
    EligibilityEvidence,
    ReportResolver,
    RouterEligibilityRefused,
    RouterError,
    RouterPaperRun,
    RouterSpec,
    RouterStop,
    RouterStopped,
    StrategyRouter,
    paper_run,
    paper_run_or_stop,
    report_store_resolver,
)
from research.router.evidence import VALIDATION_REPORT_KIND
from tests.factories import HASH_EXPERIMENT, HASH_PROFILE, profile_ref
from tests.research.router.test_paper import BARS, LIFECYCLE, STATES, STRATEGIES, ZERO, A, B, _run
from tests.research.router.test_router_completion import (
    BASELINE_RUN_HASH,
    FLAT,
    REPORT_A,
    REPORT_B,
    SPEC,
    _paper,
)

#: Trust-mode hashes computed with the code before this change (1fb7918): unchanged.
#: Re-pinned for contract 2.1.0 (ADR-0052 §4, 2026-09-26): the intended envelope change only —
#: every newly built contract object is 2.1.0 and the envelope is part of each content hash.
#: The previous values (d832b544…, 6f342bc7…, 577d66c4…, 918770b0…) still hold when the same test
#: builds every object at 2.0.0 (verified inside ``contract_schema_version_scope("2.0.0")``).
TRUST_REPORTS_RUN_HASH = "d3c8cc3b5b7e97e6883b0f285b2cfad16f898cd7201bac0a4bbab2b3f5105954"
TRUST_STOP_FLAT = "64c340616747be0377f0b48e4d4baeecbf1f72d41547bc7961fe24b2478ed4d9"
TRUST_STOP_NO_CANDIDATE = "6528f56bf3fd98372459e1965e7fb73e24ff243cc02c62fd629c64a7e46aafdb"
TRUST_STOP_FLAT_WITH_REPORTS = "cbc771dc96c37792f354cca39b6af9dedb35179b13ffea4304b8318772ed9358"
CREATED = datetime(2026, 9, 1, tzinfo=UTC)


def _gate(gate_id: str, verdict: Verdict = Verdict.PASS) -> GateResult:
    return GateResult(gate_id=gate_id, metric="fixture", value=1.0, verdict=verdict)


def _report(
    subject: Ref,
    *,
    in_sample: Verdict = Verdict.PASS,
    sealed_oos: Verdict | None = Verdict.PASS,
    run_id: str = "run-1",
) -> ValidationReport:
    gates = [_gate("G1.fixture", in_sample)]
    if sealed_oos is not None:
        gates.append(_gate("G5.fixture", sealed_oos))
    verdicts = {gate.verdict for gate in gates}
    verdict = (
        Verdict.FAIL
        if Verdict.FAIL in verdicts
        else Verdict.INCONCLUSIVE
        if Verdict.INCONCLUSIVE in verdicts
        else Verdict.PASS
    )
    return ValidationReport(
        report_id=f"rep-{subject.name}",
        run_id=run_id,
        subject=subject,
        experiment_hash=HASH_EXPERIMENT,
        constitution_version="0.2.0-draft",
        validation_profile=profile_ref(),
        validation_profile_hash=HASH_PROFILE,
        gates=tuple(gates),
        verdict=verdict,
        created_at=CREATED,
    )


GOOD = {A: _report(A), B: _report(B)}
HASHES = {ref: report.content_hash() for ref, report in GOOD.items()}


def _evidence(
    reports: Mapping[Ref, object] | ReportResolver | None = None,
    hashes: Mapping[Ref, str] | None = None,
) -> EligibilityEvidence:
    return EligibilityEvidence(
        report_hashes=HASHES if hashes is None else hashes,
        reports=GOOD if reports is None else reports,  # type: ignore[arg-type]
    )


def _router(evidence: EligibilityEvidence, spec: RouterSpec = SPEC) -> StrategyRouter:
    return StrategyRouter(spec, LIFECYCLE, evidence=evidence)


def _evidence_run(evidence: EligibilityEvidence | None = None, **kwargs: object) -> RouterPaperRun:
    return paper_run(
        _router(evidence or _evidence()),
        STATES,
        STRATEGIES,
        bars=BARS,
        cost_model=ZERO,
        initial_equity=Decimal(1000),
        backtester=BarBacktester(),
        **kwargs,  # type: ignore[arg-type]
    )


def _or_stop(
    spec: RouterSpec,
    lifecycle: Mapping[Ref, LifecycleState] = LIFECYCLE,
    evidence: EligibilityEvidence | None = None,
) -> RouterPaperRun | RouterStop:
    return paper_run_or_stop(
        spec,
        lifecycle,
        STATES,
        STRATEGIES,
        bars=BARS,
        cost_model=ZERO,
        initial_equity=Decimal(1000),
        backtester=BarBacktester(),
        evidence=evidence,
    )


def _store(tmp_path: Path, *reports: ValidationReport) -> ReportResolver:
    for report in reports:
        write_validation_report(tmp_path, report)
    return report_store_resolver(tmp_path)


# --------------------------------------------------------------------------- valid evidence routes


def test_valid_evidence_routes_and_is_recorded_and_hashed() -> None:
    run = _evidence_run()
    trust = _run()
    assert (run.decisions, run.targets, run.result) == (
        trust.decisions,
        trust.targets,
        trust.result,
    )
    assert run.validation_reports == {str(k): v for k, v in sorted(HASHES.items(), key=str)}
    assert run.eligibility is not None
    assert [c.strategy for c in run.eligibility] == sorted([str(A), str(B)])
    for check in run.eligibility:
        assert check.verified and check.refusal is None
        assert check.report_hash == run.validation_reports[check.strategy]
        assert check.subject == check.strategy and check.verdict == "PASS"
        assert check.sealed_oos_gates == ("G5.fixture",)
    assert {c.strategy: c.lifecycle for c in run.eligibility} == {
        str(A): "ACTIVE",
        str(B): "PRODUCTION_CANDIDATE",
    }
    assert run.run_hash not in (BASELINE_RUN_HASH, TRUST_REPORTS_RUN_HASH)
    run.verify()
    assert run == _evidence_run()  # deterministic
    # the same hashes supplied as validation_reports are accepted; other ones are refused
    assert _evidence_run(validation_reports=HASHES) == run
    with pytest.raises(RouterError, match="differ from the router's verified evidence"):
        _evidence_run(validation_reports={A: REPORT_A, B: REPORT_B})


def test_the_recorded_evidence_is_covered_by_the_run_hash() -> None:
    run = _evidence_run()
    assert run.eligibility is not None
    first, second = run.eligibility
    for tampered in (
        replace(run, eligibility=None),
        replace(run, eligibility=(second,)),
        replace(run, eligibility=(replace(first, verdict="FAIL"), second)),
        replace(run, eligibility=(replace(first, report_hash=REPORT_A), second)),
        replace(run, validation_reports={str(A): REPORT_A, str(B): REPORT_B}),
    ):
        with pytest.raises(RouterError, match="run_hash does not match"):
            tampered.verify()


def test_the_report_store_resolver_is_the_same_evidence(tmp_path: Path) -> None:
    assert VALIDATION_REPORT_KIND == VALIDATION_KIND
    resolver = _store(tmp_path, *GOOD.values())
    run = _evidence_run(_evidence(resolver))
    assert run.run_hash == _evidence_run().run_hash
    run.verify()


# ------------------------------------------------------------------------------ refusals, specific


def _refusal(evidence: EligibilityEvidence) -> RouterEligibilityRefused:
    with pytest.raises(RouterEligibilityRefused) as caught:
        _router(evidence)
    refused = caught.value
    assert isinstance(refused, RouterStopped) and isinstance(refused, RouterError)
    assert refused.reason == "eligibility_not_evidenced"
    assert refused.eligibility is not None and len(refused.eligibility) == 2
    return refused


@pytest.mark.parametrize(
    ("evidence", "refusal"),
    [
        (_evidence(hashes={A: HASHES[A], B: REPORT_B}), "report_hash_mismatch"),
        (_evidence(reports={A: GOOD[A], B: _report(B, run_id="run-2")}), "report_hash_mismatch"),
        (
            _evidence(
                reports={A: GOOD[A], B: _report(A, run_id="b")},
                hashes={A: HASHES[A], B: _report(A, run_id="b").content_hash()},
            ),
            "subject_mismatch",
        ),
        (
            _evidence(
                reports={A: GOOD[A], B: _report(B, in_sample=Verdict.FAIL)},
                hashes={A: HASHES[A], B: _report(B, in_sample=Verdict.FAIL).content_hash()},
            ),
            "verdict_not_pass",
        ),
        (
            _evidence(
                reports={A: GOOD[A], B: _report(B, sealed_oos=Verdict.INCONCLUSIVE)},
                hashes={
                    A: HASHES[A],
                    B: _report(B, sealed_oos=Verdict.INCONCLUSIVE).content_hash(),
                },
            ),
            "verdict_not_pass",
        ),
        (
            _evidence(
                reports={A: GOOD[A], B: _report(B, sealed_oos=None)},
                hashes={A: HASHES[A], B: _report(B, sealed_oos=None).content_hash()},
            ),
            "sealed_oos_not_evaluated",
        ),
        (_evidence(reports={A: GOOD[A]}), "report_not_found"),
        (_evidence(hashes={A: HASHES[A]}), "report_hash_missing"),
        (_evidence(reports={A: GOOD[A], B: "not a report"}), "report_invalid"),
    ],
    ids=[
        "wrong_hash",
        "hash_of_other_content",
        "wrong_subject",
        "fail",
        "g5_inconclusive",
        "missing_g5",
        "missing_report",
        "missing_hash",
        "not_a_report",
    ],
)
def test_each_unbacked_claim_is_refused_with_its_specific_reason(
    evidence: EligibilityEvidence, refusal: str
) -> None:
    refused = _refusal(evidence)
    assert refused.refusal == refusal and refused.strategy == str(B)
    assert refused.refusals == {str(B): refusal}
    assert refusal in str(refused)
    assert refused.eligibility is not None
    assert [c.verified for c in refused.eligibility] == [False, True]  # B sorts before A


def test_a_g5_gate_that_did_not_pass_is_refused_even_if_the_verdict_claims_pass() -> None:
    forged = ValidationReport.model_construct(
        **{**dict(GOOD[B]), "gates": (_gate("G1.fixture"), _gate("G5.fixture", Verdict.FAIL))}
    )
    refused = _refusal(_evidence({A: GOOD[A], B: forged}, {A: HASHES[A], B: forged.content_hash()}))
    assert refused.refusal == "sealed_oos_not_passed" and "G5.fixture" in refused.detail


def test_every_refused_strategy_is_reported() -> None:
    refused = _refusal(_evidence(reports={}))
    assert refused.refusals == {str(B): "report_not_found", str(A): "report_not_found"}


def test_the_report_store_refuses_absent_corrupt_and_tampered_files(tmp_path: Path) -> None:
    resolver = _store(tmp_path, GOOD[A])  # B was never written
    assert _refusal(_evidence(resolver)).refusal == "report_not_found"
    directory = tmp_path / VALIDATION_KIND
    (directory / f"{HASHES[B]}.json").write_text("{not json", encoding="utf-8")
    assert _refusal(_evidence(resolver)).refusal == "report_invalid"
    (directory / f"{HASHES[B]}.json").write_text(json.dumps({"x": 1}), encoding="utf-8")
    assert _refusal(_evidence(resolver)).refusal == "report_invalid"
    # a well-formed report of other content under B's claimed hash
    other = _report(B, run_id="run-2").model_dump(mode="json")
    (directory / f"{HASHES[B]}.json").write_text(json.dumps(other), encoding="utf-8")
    refused = _refusal(_evidence(resolver))
    assert refused.refusal == "report_hash_mismatch"


def test_ill_formed_evidence_is_a_plain_error_not_a_refusal() -> None:
    stray = Ref(kind=A.kind, name="stray", version="1.0.0")
    cases: list[EligibilityEvidence] = [
        _evidence(hashes={**HASHES, stray: REPORT_A}),
        _evidence(reports={**GOOD, stray: GOOD[A]}),
        _evidence(hashes={A: HASHES[A], B: "ABC"}),
    ]
    for evidence in cases:
        with pytest.raises(RouterError) as caught:
            _router(evidence)
        assert not isinstance(caught.value, RouterStopped)
    with pytest.raises(RouterError, match="EligibilityEvidence"):
        StrategyRouter(SPEC, LIFECYCLE, evidence=HASHES)  # type: ignore[arg-type]
    with pytest.raises(RouterError, match="mapping or a resolver"):
        _router(EligibilityEvidence(report_hashes=HASHES, reports=42))  # type: ignore[arg-type]
    # evidence never widens the lifecycle claim: an unvalidated route is still refused
    with pytest.raises(RouterError, match="unvalidated"):
        StrategyRouter(
            SPEC, {A: LifecycleState.ACTIVE, B: LifecycleState.VALIDATION}, evidence=_evidence()
        )


# ------------------------------------------------------------------------ recorded as a stop


def test_an_evidence_refusal_is_recorded_as_a_router_stop(tmp_path: Path) -> None:
    bad = _evidence(reports={A: GOOD[A], B: _report(B, sealed_oos=None)})
    stop = _or_stop(SPEC, evidence=bad)
    assert isinstance(stop, RouterStop)
    assert stop.reason == "eligibility_not_evidenced" and "report_hash_mismatch" in stop.detail
    assert stop.validation_reports == {str(k): v for k, v in HASHES.items()}
    assert stop.eligibility is not None
    assert {c.strategy: c.refusal for c in stop.eligibility} == {
        str(A): None,
        str(B): "report_hash_mismatch",
    }
    assert stop == _or_stop(SPEC, evidence=bad)  # deterministic
    payload = json.loads(write_router_stop(tmp_path, stop).path.read_text(encoding="utf-8"))
    assert payload["reason"] == "eligibility_not_evidenced"
    assert payload["eligibility"] == [c.to_dict() for c in stop.eligibility]
    run = _or_stop(SPEC, evidence=_evidence())
    assert isinstance(run, RouterPaperRun) and run == _evidence_run()


def test_an_evidence_mode_stop_before_verification_records_no_checks() -> None:
    stop = _or_stop(FLAT, evidence=_evidence(hashes={A: HASHES[A]}, reports={A: GOOD[A]}))
    assert isinstance(stop, RouterStop) and stop.reason == "all_routes_flat"
    assert stop.eligibility == () and stop.validation_reports == {str(A): HASHES[A]}
    trust = _or_stop(FLAT)
    assert isinstance(trust, RouterStop) and stop.stop_hash != trust.stop_hash
    with pytest.raises(RouterError, match=r"differ from evidence\.report_hashes"):
        paper_run_or_stop(
            FLAT,
            LIFECYCLE,
            STATES,
            STRATEGIES,
            bars=BARS,
            cost_model=ZERO,
            initial_equity=Decimal(1000),
            backtester=BarBacktester(),
            validation_reports={A: REPORT_A},
            evidence=_evidence(hashes={A: HASHES[A]}, reports={A: GOOD[A]}),
        )


# ------------------------------------------------------------------ trust mode byte-identical


def test_trust_mode_hashes_and_payloads_are_unchanged(tmp_path: Path) -> None:
    run = _run()
    assert run.run_hash == BASELINE_RUN_HASH and run.eligibility is None
    assert _paper(validation_reports={A: REPORT_A, B: REPORT_B}).run_hash == TRUST_REPORTS_RUN_HASH
    assert StrategyRouter(SPEC, LIFECYCLE).eligibility is None
    stops = [
        _or_stop(FLAT),
        _or_stop(FLAT, {A: LifecycleState.VALIDATION}),
        paper_run_or_stop(
            FLAT,
            LIFECYCLE,
            STATES,
            STRATEGIES,
            bars=BARS,
            cost_model=ZERO,
            initial_equity=Decimal(1000),
            backtester=BarBacktester(),
            validation_reports={A: REPORT_A, B: REPORT_B},
        ),
    ]
    expected = [TRUST_STOP_FLAT, TRUST_STOP_NO_CANDIDATE, TRUST_STOP_FLAT_WITH_REPORTS]
    for stop, pinned in zip(stops, expected, strict=True):
        assert isinstance(stop, RouterStop) and stop.eligibility is None
        assert stop.stop_hash == pinned
        payload = json.loads(write_router_stop(tmp_path, stop).path.read_text(encoding="utf-8"))
        assert "eligibility" not in payload
    written = json.loads(write_router_paper_run(tmp_path, run).path.read_text(encoding="utf-8"))
    assert "eligibility" not in written and "validation_reports" not in written


def test_the_run_report_carries_the_evidence_only_in_evidence_mode(tmp_path: Path) -> None:
    run = _evidence_run()
    assert run.eligibility is not None
    payload = json.loads(write_router_paper_run(tmp_path, run).path.read_text(encoding="utf-8"))
    assert payload["run_hash"] == run.run_hash
    assert payload["eligibility"] == [c.to_dict() for c in run.eligibility]
    assert payload["validation_reports"] == dict(run.validation_reports or {})

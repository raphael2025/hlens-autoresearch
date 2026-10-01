"""P10-ELIG: router eligibility bound to validation evidence (research level, paper only)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from core.contracts.validation_profile import BenchmarkParams, ValidationProfile
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
from tests.contract_version_support import PINNED_CONTRACT_VERSION, at_contract_version
from tests.factories import HASH_EXPERIMENT, HASH_PROFILE, validation_profile
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
#: Re-pinned for contract 2.2.0 (ADR-0055, 2026-09-26): envelope change only; the 2.1.0 values
#: still hold when the test builds every object at 2.1.0 (verified: the unmodified test passes
#: inside ``contract_schema_version_scope("2.1.0")``). 2.1.0 values (evidence, git history):
#: d3c8cc3b…, stops 64c34061…, 6528f56b…, cbc771dc…
#: Recorded at contract 2.2.0: checked with every contract object built at 2.2.0
#: (``at_contract_version``) after the envelope-only 2.3.0 – 2.5.0 minors.
TRUST_REPORTS_RUN_HASH = "ed62d7c73313331ae9e429fceec3e9d18c5cb75b661be6e130990e4870aea149"
TRUST_STOP_FLAT = "bff151b450dd125758410b9ed78e352030d36a27cc58a95f5d2ed8216b630382"
TRUST_STOP_NO_CANDIDATE = "274b2162f939a8fed92dfa7a39536fbfc35f5e28f13191f9ab51c8799b0abca7"
TRUST_STOP_FLAT_WITH_REPORTS = "2e8a462646ba73292be204afb568744907d4290979f70c1c3673ceccc409ebe1"
CREATED = datetime(2026, 9, 1, tzinfo=UTC)


def _profile(rule: str, *, inverse: bool = False) -> ValidationProfile:
    """A TEST ONLY Profile (factory values, not calibrated) with ``market_benchmark_rule=rule``
    and ``inverse_control_reported=inverse`` (the factory's own default is ``True``)."""
    base = validation_profile().benchmark
    return validation_profile(
        benchmark=BenchmarkParams.model_validate(
            {
                **base.model_dump(),
                "market_benchmark_rule": rule,
                "inverse_control_reported": inverse,
            }
        )
    )


#: The fixture reports ran under this Profile: rule ``none`` and no inverse control need no
#: ADR-0060 item.
PROFILE = _profile("none")


def _gate(gate_id: str, verdict: Verdict = Verdict.PASS) -> GateResult:
    return GateResult(gate_id=gate_id, metric="fixture", value=1.0, verdict=verdict)


def _report(
    subject: Ref,
    *,
    in_sample: Verdict = Verdict.PASS,
    sealed_oos: Verdict | None = Verdict.PASS,
    run_id: str = "run-1",
    profile: ValidationProfile = PROFILE,
    extra: tuple[str, ...] = (),
) -> ValidationReport:
    gates = [_gate("G1.fixture", in_sample), *(_gate(gate_id) for gate_id in extra)]
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
        validation_profile=profile.ref,
        validation_profile_hash=profile.content_hash(),
        gates=tuple(gates),
        verdict=verdict,
        created_at=CREATED,
    )


GOOD = {A: _report(A), B: _report(B)}
HASHES = {ref: report.content_hash() for ref, report in GOOD.items()}


def _evidence(
    reports: Mapping[Ref, object] | ReportResolver | None = None,
    hashes: Mapping[Ref, str] | None = None,
    profiles: tuple[ValidationProfile, ...] = (PROFILE,),
) -> EligibilityEvidence:
    return EligibilityEvidence(
        report_hashes=HASHES if hashes is None else hashes,
        reports=GOOD if reports is None else reports,  # type: ignore[arg-type]
        profiles=profiles,
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
        _router(
            EligibilityEvidence(report_hashes=HASHES, reports=42, profiles=(PROFILE,))  # type: ignore[arg-type]
        )
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


def _trust_mode_hashes_and_payloads(tmp: str) -> None:
    tmp_path = Path(tmp)
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


def test_trust_mode_hashes_and_payloads_are_unchanged(tmp_path: Path) -> None:
    call = f"{__name__}:_trust_mode_hashes_and_payloads"
    assert at_contract_version(PINNED_CONTRACT_VERSION, call, str(tmp_path)) is None


def test_the_run_report_carries_the_evidence_only_in_evidence_mode(tmp_path: Path) -> None:
    run = _evidence_run()
    assert run.eligibility is not None
    payload = json.loads(write_router_paper_run(tmp_path, run).path.read_text(encoding="utf-8"))
    assert payload["run_hash"] == run.run_hash
    assert payload["eligibility"] == [c.to_dict() for c in run.eligibility]
    assert payload["validation_reports"] == dict(run.validation_reports or {})


# --------------------------------------------------- the report's Profile and ADR-0060 (C-T4)


def _with_b(report: ValidationReport, *profiles: ValidationProfile) -> EligibilityEvidence:
    return _evidence(
        reports={A: GOOD[A], B: report},
        hashes={A: HASHES[A], B: report.content_hash()},
        profiles=(PROFILE, *profiles),
    )


def test_the_fixture_reports_were_produced_under_a_real_profile_object() -> None:
    assert HASH_PROFILE != PROFILE.content_hash()
    assert {r.validation_profile_hash for r in GOOD.values()} == {PROFILE.content_hash()}


def test_a_report_whose_profile_is_not_given_is_refused() -> None:
    refused = _refusal(_with_b(_report(B, profile=_profile("flat"))))
    assert refused.refusal == "profile_not_found" and refused.strategy == str(B)
    # no profiles at all: every routed strategy is refused
    none_given = _refusal(_evidence(profiles=()))
    assert none_given.refusals == {str(B): "profile_not_found", str(A): "profile_not_found"}


def test_a_profile_of_other_content_is_not_the_reports_profile() -> None:
    other = _profile("flat")
    assert other.ref == PROFILE.ref and other.content_hash() != PROFILE.content_hash()
    refused = _refusal(_evidence(profiles=(other,)))
    assert set(refused.refusals.values()) == {"profile_not_found"}


@pytest.mark.parametrize("rule", ["flat", "buy_and_hold_equal_weight"])
def test_a_report_without_its_profiles_market_benchmark_item_is_refused(rule: str) -> None:
    profile = _profile(rule)
    refused = _refusal(_with_b(_report(B, profile=profile), profile))
    assert refused.refusal == "market_benchmark_missing" and refused.strategy == str(B)
    assert f"G2.market_benchmark.{rule}" in refused.detail
    # an item for another rule is not the one the Profile calls for
    wrong = _report(B, profile=profile, extra=("G2.market_benchmark.none_of_these",))
    assert _refusal(_with_b(wrong, profile)).refusal == "market_benchmark_missing"


def test_an_unregistered_rule_needs_the_bare_market_benchmark_item() -> None:
    profile = _profile("TEST-ONLY-unregistered")
    refused = _refusal(_with_b(_report(B, profile=profile), profile))
    assert refused.refusal == "market_benchmark_missing"
    assert refused.detail.split("calls for ")[1].startswith("G2.market_benchmark,")


def test_a_report_with_its_profiles_market_benchmark_item_routes() -> None:
    profile = _profile("flat")
    report = _report(B, profile=profile, extra=("G2.market_benchmark.flat",))
    router = _router(_with_b(report, profile))
    assert router.eligibility is not None
    assert all(check.verified for check in router.eligibility)


# ------------------------------------------ ADR-0060 inverse control (reported only, presence)

INVERSE = "G2.inverse_control"


def _with_gates(report: ValidationReport, *gates: GateResult, verdict: Verdict) -> ValidationReport:
    """``report`` with ``gates`` appended and ``verdict`` (a validated, re-built report)."""
    data = report.model_dump()
    data["gates"] = (*report.gates, *gates)
    data["verdict"] = verdict
    return ValidationReport.model_validate(data)


def test_a_report_without_the_inverse_control_its_profile_asks_for_is_refused() -> None:
    profile = _profile("none", inverse=True)
    refused = _refusal(_with_b(_report(B, profile=profile), profile))
    assert refused.refusal == "inverse_control_missing" and refused.strategy == str(B)
    assert refused.refusals == {str(B): "inverse_control_missing"}
    assert "G2.inverse_control" in refused.detail and "ADR-0060" in refused.detail
    assert refused.eligibility is not None
    [check_b] = [c for c in refused.eligibility if c.strategy == str(B)]
    assert (check_b.verdict, check_b.sealed_oos_gates) == ("PASS", ("G5.fixture",))
    # the gate id must match exactly: no prefix, suffix or case folding
    for near in (
        "G2.inverse_control.flat",
        "G2.inverse",
        "g2.inverse_control",
        "G3.inverse_control",
    ):
        near_miss = _report(B, profile=profile, extra=(near,))
        assert _refusal(_with_b(near_miss, profile)).refusal == "inverse_control_missing"


def test_a_report_with_the_inverse_control_its_profile_asks_for_routes() -> None:
    profile = _profile("none", inverse=True)
    report = _report(B, profile=profile, extra=(INVERSE,))
    router = _router(_with_b(report, profile))
    assert router.eligibility is not None
    assert all(check.verified for check in router.eligibility)
    # with a market benchmark rule as well, both items are needed, each on its own
    both = _profile("flat", inverse=True)
    complete = _report(B, profile=both, extra=("G2.market_benchmark.flat", INVERSE))
    checked = _router(_with_b(complete, both)).eligibility
    assert checked is not None and all(check.verified for check in checked)
    only_market = _report(B, profile=both, extra=("G2.market_benchmark.flat",))
    assert _refusal(_with_b(only_market, both)).refusal == "inverse_control_missing"
    only_inverse = _report(B, profile=both, extra=(INVERSE,))
    assert _refusal(_with_b(only_inverse, both)).refusal == "market_benchmark_missing"


def test_a_profile_that_does_not_ask_for_the_inverse_control_does_not_need_it() -> None:
    assert not PROFILE.benchmark.inverse_control_reported
    assert all(INVERSE not in {g.gate_id for g in r.gates} for r in GOOD.values())
    checked = _router(_evidence()).eligibility
    assert checked is not None and all(check.verified for check in checked)
    # an item the Profile did not ask for is not refused either
    unasked = _router(_with_b(_report(B, extra=(INVERSE,)))).eligibility
    assert unasked is not None and all(check.verified for check in unasked)
    flat = _profile("flat")
    market_only = _report(B, profile=flat, extra=("G2.market_benchmark.flat",))
    routed = _router(_with_b(market_only, flat)).eligibility
    assert routed is not None and all(check.verified for check in routed)


def test_the_inverse_control_is_presence_only_and_its_verdict_is_the_reports() -> None:
    """ADR-0060: the item is reported only — no threshold on its value; an INCONCLUSIVE item
    (``benchmark_unavailable``) makes the validator's verdict INCONCLUSIVE, refused before."""
    profile = _profile("none", inverse=True)
    base = _report(B, profile=profile)
    # a valid report with an INCONCLUSIVE item: its derived verdict is INCONCLUSIVE (ADR-0013)
    unavailable = _with_gates(
        base, _gate(INVERSE, Verdict.INCONCLUSIVE), verdict=Verdict.INCONCLUSIVE
    )
    assert _refusal(_with_b(unavailable, profile)).refusal == "verdict_not_pass"
    # any computed value routes: no threshold on the inverse control's return (ADR-0060)
    negative = GateResult(
        gate_id=INVERSE, metric="inverse_net_return", value=-0.5, verdict=Verdict.PASS
    )
    losing = _with_gates(base, negative, verdict=Verdict.PASS)
    checked = _router(_with_b(losing, profile)).eligibility
    assert checked is not None and all(check.verified for check in checked)


def test_the_inverse_control_is_checked_after_the_profile_and_the_market_benchmark() -> None:
    both = _profile("buy_and_hold_equal_weight", inverse=True)
    neither = _report(B, profile=both)
    assert _refusal(_with_b(neither, both)).refusal == "market_benchmark_missing"
    # the Profile is not given: its flags cannot be read, so profile_not_found comes first
    assert _refusal(_with_b(neither)).refusal == "profile_not_found"
    inverse_only = _profile("none", inverse=True)
    earlier = _report(B, profile=inverse_only, sealed_oos=None)
    assert _refusal(_with_b(earlier, inverse_only)).refusal == "sealed_oos_not_evaluated"


def test_an_inverse_control_refusal_is_recorded_as_a_router_stop(tmp_path: Path) -> None:
    profile = _profile("none", inverse=True)
    missing = _report(B, profile=profile)
    evidence = _with_b(missing, profile)
    stop = _or_stop(SPEC, evidence=evidence)
    assert isinstance(stop, RouterStop)
    assert stop.reason == "eligibility_not_evidenced" and "inverse_control_missing" in stop.detail
    assert stop.eligibility is not None
    assert {c.strategy: c.refusal for c in stop.eligibility} == {
        str(A): None,
        str(B): "inverse_control_missing",
    }
    assert stop == _or_stop(SPEC, evidence=evidence)  # deterministic
    payload = json.loads(write_router_stop(tmp_path, stop).path.read_text(encoding="utf-8"))
    assert payload["stop_hash"] == stop.stop_hash
    assert payload["eligibility"] == [c.to_dict() for c in stop.eligibility]
    [record] = [c for c in payload["eligibility"] if c["strategy"] == str(B)]
    assert record["refusal"] == "inverse_control_missing"
    # the same report with the item routes
    present = _report(B, profile=profile, extra=(INVERSE,))
    assert isinstance(_or_stop(SPEC, evidence=_with_b(present, profile)), RouterPaperRun)


def test_profiles_that_are_not_profiles_are_a_plain_error() -> None:
    for bad in (("not a profile",), "not a sequence", 42):
        with pytest.raises(RouterError, match="evidence.profiles") as caught:
            _router(_evidence(profiles=bad))  # type: ignore[arg-type]
        assert not isinstance(caught.value, RouterStopped)

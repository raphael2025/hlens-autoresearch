"""P10 paper deviation: the router's net paper result vs a declared reference backtest.

The run is ``tests/research/router/test_paper.py``'s (a two-strategy router over seven BTC bars,
zero instrument cost, 1% switching cost); the reference is strategy A run alone through the same
backtester, bars and cost model.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal
from typing import Any, TypedDict, cast

import pytest

from core.contracts.strategy import BacktestCostModel, BacktestRequest, BacktestResult
from core.contracts.validation_profile import ProfileScope, ValidationProfile
from core.domain.base import Kind, Ref, content_hash
from core.domain.research import GateResult, ValidationReport, Verdict
from plugins.backtest import BarBacktester
from research.router import (
    DeviationError,
    PaperDeviation,
    RouterError,
    RouterPaperRun,
    RunBinding,
    paper_deviation,
    validate_scope_bound_payload,
)
from research.router.deviation import RETURN_QUANTUM
from research.router.validation import (
    RouterValidation,
    _binding_hash,
    router_strategy_spec,
)
from research.strategies.validation import BacktestValidation
from tests.factories import repro_tuple, validation_profile
from tests.research.router.test_paper import BARS, STATE, STRATEGIES, ZERO, A, _router, _run
from tests.strategy_fixtures import MINUTE, T0, make_bars


class ScopeKwargs(TypedDict):
    validation_profile: ValidationProfile
    validation_report: ValidationReport
    router_validation: RouterValidation
    reference_request: BacktestRequest


def reference_request(initial: str = "1000") -> BacktestRequest:
    """Strategy A alone over the run's bars and cost model (the declared reference)."""
    return BacktestRequest(
        cost_model=ZERO,
        initial_equity=Decimal(initial),
        bars=BARS,
        targets=STRATEGIES[A].positions,
    )


def reference(initial: str = "1000") -> BacktestResult:
    return BarBacktester().run(reference_request(initial))


ROUTER = Ref(kind=Kind.STRATEGY, name="vol_router", version="1.0.0")
#: The router's experiment identity: the hash of a complete reproducibility tuple about it.
EXPERIMENT_HASH = repro_tuple(strategy_ref=ROUTER).experiment_hash


def scope_evidence() -> tuple[ValidationProfile, ValidationReport]:
    profile = validation_profile(
        name="p10_deviation_scope",
        scope=ProfileScope(venue="testvenue", symbol="BTC", timeframe="1m", research_class="swing"),
    )
    report = ValidationReport(
        report_id="rep-vol-router",
        run_id="run-vol-router",
        subject=Ref(kind=Kind.STRATEGY, name="vol_router", version="1.0.0"),
        experiment_hash=EXPERIMENT_HASH,
        constitution_version="1.0.0",
        validation_profile=profile.ref,
        validation_profile_hash=profile.content_hash(),
        gates=(
            GateResult(
                gate_id="G5.sealed_oos",
                metric="sealed_oos",
                value=1.0,
                verdict=Verdict.PASS,
            ),
        ),
        verdict=Verdict.PASS,
    )
    return profile, report


def router_validation(
    run: RouterPaperRun, report: ValidationReport, rate: str = "0.01"
) -> RouterValidation:
    """The router's validation bound to ``run`` and ``report`` (the P10 binding of
    ``research.router.validation.validate_router``, without re-running the pipeline)."""
    strategy_hash = router_strategy_spec(_router(rate).spec, state=STATE).content_hash()
    return RouterValidation(
        validation=BacktestValidation(report=report),
        router_spec_hash=run.router_spec_hash,
        router_strategy_spec_hash=strategy_hash,
        paper_run_hash=run.run_hash,
        binding_hash=_binding_hash(
            report.content_hash(), run.router_spec_hash, strategy_hash, run.run_hash
        ),
    )


def scope_kwargs(rate: str = "0.01") -> ScopeKwargs:
    """Every binding input ``paper_deviation`` needs, for ``_run(rate)`` against ``reference()``."""
    profile, report = scope_evidence()
    return {
        "validation_profile": profile,
        "validation_report": report,
        "router_validation": router_validation(_run(rate), report, rate),
        "reference_request": reference_request(),
    }


def scope_with(rate: str = "0.01", **overrides: Any) -> dict[str, Any]:
    """``scope_kwargs(rate)`` with ``overrides`` applied (untyped: for refusal tests)."""
    return {**scope_kwargs(rate), **overrides}


def deviation() -> PaperDeviation:
    return paper_deviation(_run(), reference(), **scope_kwargs())


def test_every_mark_compares_the_net_paper_equity_with_the_reference() -> None:
    run, ref = _run(), reference()
    report = deviation()
    assert [mark.time for mark in report.marks] == [p.time for p in run.result.equity_curve]
    previous_paper = previous_reference = Decimal(1000)
    for mark, ours, theirs in zip(
        report.marks, run.result.equity_curve, ref.equity_curve, strict=True
    ):
        assert (mark.paper_equity, mark.reference_equity) == (ours.equity, theirs.equity)
        assert mark.equity_difference == ours.equity - theirs.equity
        assert mark.paper_return == (ours.equity / previous_paper - 1).quantize(RETURN_QUANTUM)
        assert mark.reference_return == (theirs.equity / previous_reference - 1).quantize(
            RETURN_QUANTUM
        )
        assert mark.paper_return is not None and mark.reference_return is not None
        assert mark.return_difference == mark.paper_return - mark.reference_return
        previous_paper, previous_reference = ours.equity, theirs.equity


def test_hand_checked_marks() -> None:
    """A alone: flat at t1, long 10 @ 100 from t2 (1000, 1000, 1100, 1210, ...); the router is
    long at t2 (net of the 10 switching charge) then short half at t4 (test_paper's numbers)."""
    report = deviation()
    first, second, third = report.marks[:3]
    assert (first.paper_equity, first.reference_equity) == (Decimal(1000), Decimal(1000))
    assert first.equity_difference == 0 and first.return_difference == 0
    assert second.paper_equity == Decimal(990)  # 1000 - the 10 charged at t2
    assert second.equity_difference == Decimal(-10)
    assert second.paper_return == Decimal("-0.01").quantize(RETURN_QUANTUM)
    assert third.reference_equity == Decimal(1100)
    assert third.equity_difference == Decimal(1090) - Decimal(1100)


def test_summary_statistics() -> None:
    run, ref = _run(), reference()
    report = deviation()
    summary = report.summary
    differences = [mark.equity_difference for mark in report.marks]
    returns = [mark.return_difference for mark in report.marks]
    assert all(value is not None for value in returns)
    values = [value for value in returns if value is not None]
    assert summary.marks == len(report.marks) == len(run.result.equity_curve)
    assert summary.initial_equity == Decimal(1000)
    assert summary.final_equity_difference == run.result.final_equity - ref.final_equity
    assert summary.final_equity_difference == differences[-1]
    assert summary.max_abs_equity_difference == max(abs(value) for value in differences)
    widest = next(m for m in report.marks if abs(m.equity_difference) == max(map(abs, differences)))
    assert summary.max_abs_equity_difference_at == widest.time
    assert summary.paper_total_return == (run.result.final_equity / 1000 - 1).quantize(
        RETURN_QUANTUM
    )
    assert summary.total_return_difference == (
        summary.paper_total_return - summary.reference_total_return
    )
    assert summary.return_marks == len(values)
    mean = sum(values, Decimal(0)) / len(values)
    assert summary.mean_return_difference == mean.quantize(RETURN_QUANTUM)
    assert summary.mean_abs_return_difference == (
        sum((abs(v) for v in values), Decimal(0)) / len(values)
    ).quantize(RETURN_QUANTUM)
    assert summary.tracking_error is not None and summary.tracking_error > 0
    # sample variance, n - 1
    variance = sum(((v - mean) ** 2 for v in values), Decimal(0)) / (len(values) - 1)
    assert abs(summary.tracking_error**2 - variance) < Decimal("1e-15")


def test_the_report_is_deterministic_and_content_hashed() -> None:
    report, again = deviation(), deviation()
    assert report == again and report.deviation_hash == again.deviation_hash
    payload = cast(dict[str, Any], report.to_payload())
    body = {key: value for key, value in payload.items() if key != "deviation_hash"}
    assert payload["deviation_hash"] == report.deviation_hash == content_hash(body)
    assert payload["kind"] == "paper_deviation"
    assert payload["run_hash"] == _run().run_hash
    assert payload["reference_result_hash"] == reference().result_hash
    assert payload["instruments"] == ["BTC"]
    assert payload["schema_version"] == "2.1.0"
    assert payload["declared_scope"]["scope_schema_version"] == "1.1.0"
    assert payload["declared_scope"]["symbol"] == "BTC"
    scope_body = {k: v for k, v in payload["declared_scope"].items() if k != "scope_hash"}
    assert payload["declared_scope"]["scope_hash"] == content_hash(scope_body)
    profile, validation = scope_evidence()
    assert (
        validate_scope_bound_payload(
            payload,
            validation_report=validation,
            validation_profile=profile,
            required_min_scope_version="1.1.0",
        )
        == "run_bound"
    )
    # a different run (another switching rate) is another report
    other = paper_deviation(_run("0.02"), reference(), **scope_kwargs("0.02"))
    assert other.deviation_hash != report.deviation_hash


def test_a_run_compared_with_its_own_gross_result_has_only_the_switching_cost() -> None:
    run = _run()
    report = paper_deviation(run, run.gross, **scope_with(reference_request=run.request))
    assert report.summary.final_equity_difference == -run.total_switching_cost
    assert all(mark.equity_difference <= 0 for mark in report.marks)


def test_a_zero_switching_rate_against_its_gross_result_deviates_nowhere() -> None:
    run = _run("0")
    summary = paper_deviation(
        run, run.gross, **scope_with("0", reference_request=run.request)
    ).summary
    assert summary.max_abs_equity_difference == 0
    assert summary.tracking_error == 0 and summary.mean_return_difference == 0


# --- refusals ------------------------------------------------------------------------------


def test_misaligned_marks_are_refused() -> None:
    later = make_bars("BTC", [bar.close for bar in BARS], start=T0 + timedelta(seconds=1))
    moved_request = BacktestRequest(
        cost_model=ZERO,
        initial_equity=Decimal(1000),
        bars=later,
        targets=STRATEGIES[A].positions,
    )
    moved = BarBacktester().run(moved_request)
    assert len(moved.equity_curve) == len(_run().result.equity_curve)  # same count, other times
    with pytest.raises(DeviationError, match="equity marks"):
        paper_deviation(_run(), moved, **scope_with(reference_request=moved_request))
    shorter_request = BacktestRequest(
        cost_model=ZERO,
        initial_equity=Decimal(1000),
        bars=BARS[:-1],
        targets=STRATEGIES[A].positions,
    )
    shorter = BarBacktester().run(shorter_request)
    with pytest.raises(DeviationError, match="equity marks"):
        paper_deviation(_run(), shorter, **scope_with(reference_request=shorter_request))


def test_another_initial_equity_is_refused() -> None:
    with pytest.raises(DeviationError, match="starts at"):
        paper_deviation(
            _run(),
            reference("2000"),
            **scope_with(reference_request=reference_request("2000")),
        )


def test_a_reference_on_other_instruments_is_refused() -> None:
    eth = make_bars("ETH", [Decimal(c) for c in ("100", "100", "110", "121", "110", "99", "99")])
    positions = tuple(p.model_copy(update={"instrument": "ETH"}) for p in STRATEGIES[A].positions)
    request = BacktestRequest(
        cost_model=ZERO, initial_equity=Decimal(1000), bars=eth, targets=positions
    )
    other = BarBacktester().run(request)
    with pytest.raises(DeviationError, match="does not price") as refused:
        paper_deviation(_run(), other, **scope_with(reference_request=request))
    assert refused.value.code == "symbol_mismatch"
    # a reference that answers its request, trades only BTC, but prices BTC and ETH
    both = BacktestRequest(
        cost_model=ZERO,
        initial_equity=Decimal(1000),
        bars=BARS + eth,
        targets=STRATEGIES[A].positions,
    )
    wider = BarBacktester().run(both)
    with pytest.raises(DeviationError, match="the reference prices"):
        paper_deviation(_run(), wider, **scope_with(reference_request=both))
    with pytest.raises(DeviationError, match="does not answer"):
        paper_deviation(
            _run(),
            reference(),
            **scope_with(reference_request=reference_request("2000")),
        )


def test_a_tampered_run_is_refused() -> None:
    run = _run()
    tampered = dataclasses.replace(run, router="other@1.0.0")
    with pytest.raises(RouterError, match="run_hash"):
        paper_deviation(tampered, reference(), **scope_kwargs())


def test_only_a_run_and_a_backtest_result_are_compared() -> None:
    with pytest.raises(DeviationError):
        paper_deviation(_run(), _run(), **scope_kwargs())  # type: ignore[arg-type]


def refusal(
    *, run: RouterPaperRun | None = None, result: BacktestResult | None = None, **overrides: Any
) -> DeviationError:
    """The ``DeviationError`` of ``paper_deviation`` with ``overrides`` applied to the valid
    binding inputs (``run`` / ``result`` default to ``_run()`` / ``reference()``)."""
    arguments: dict[str, Any] = {**scope_kwargs(), **overrides}
    with pytest.raises(DeviationError) as raised:
        paper_deviation(run or _run(), result or reference(), **arguments)
    return raised.value


def test_scope_binding_is_required_and_fail_closed() -> None:
    with pytest.raises(DeviationError, match="requires a P8") as missing:
        paper_deviation(_run(), reference())
    assert missing.value.code == "scope_evidence_missing"
    profile, report = scope_evidence()
    wrong_report = report.model_copy(update={"subject": A})
    wrong = refusal(validation_report=wrong_report)
    assert "not about this router" in str(wrong) and wrong.code == "subject_mismatch"
    other_profile = validation_profile(
        name="other_scope",
        scope=ProfileScope(venue="testvenue", symbol="ETH", timeframe="1m", research_class="swing"),
    )
    other = refusal(validation_profile=other_profile)
    assert "does not bind" in str(other) and other.code == "profile_mismatch"
    changed_scope = ProfileScope(
        venue="testvenue", symbol="ETH", timeframe="1m", research_class="swing"
    )
    changed_profile = profile.model_copy(update={"scope": changed_scope})
    changed_report = report.model_copy(
        update={"validation_profile_hash": changed_profile.content_hash()}
    )
    changed = refusal(
        validation_profile=changed_profile,
        validation_report=changed_report,
        router_validation=router_validation(_run(), changed_report),
    )
    assert "exactly match" in str(changed) and changed.code == "symbol_mismatch"
    no_g5 = report.model_copy(
        update={
            "gates": (
                GateResult(gate_id="G0.repro", metric="repro", value=1.0, verdict=Verdict.PASS),
            )
        }
    )
    ungated = refusal(validation_report=no_g5)
    assert "G5" in str(ungated) and ungated.code == "sealed_oos_missing"
    failed = report.model_copy(
        update={
            "gates": (
                GateResult(
                    gate_id="G5.sealed_oos", metric="sealed_oos", value=0.0, verdict=Verdict.FAIL
                ),
            ),
            "verdict": Verdict.FAIL,
        }
    )
    assert refusal(validation_report=failed).code == "verdict_not_pass"


def test_subject_and_profile_ref_comparisons_ignore_the_envelope_schema_version() -> None:
    """P8 ``subject`` / ``validation_profile`` binding is checked via ``Ref.target_identity()``
    (ADR-0018 §D-26.5): a Ref whose Contract envelope ``schema_version`` differs from the
    current default, but whose ``(kind, name, version)`` agree, is still the same target. A
    different ``name`` (or ``version``) is still refused."""
    profile, report = scope_evidence()
    same_target_other_envelope = Ref(
        kind=Kind.STRATEGY, name="vol_router", version="1.0.0", schema_version="2.0.0"
    )
    assert same_target_other_envelope.schema_version != report.subject.schema_version
    assert same_target_other_envelope != report.subject  # structural equality still differs
    accepted = report.model_copy(update={"subject": same_target_other_envelope})
    result = paper_deviation(
        _run(),
        reference(),
        **scope_with(
            validation_report=accepted, router_validation=router_validation(_run(), accepted)
        ),
    )
    assert isinstance(result, PaperDeviation)

    different_name = same_target_other_envelope.model_copy(update={"name": "other_router"})
    rejected = report.model_copy(update={"subject": different_name})
    with pytest.raises(DeviationError, match="not about this router"):
        paper_deviation(
            _run(),
            reference(),
            **scope_with(
                validation_report=rejected, router_validation=router_validation(_run(), rejected)
            ),
        )


def test_scope_payload_validation_rejects_legacy_and_tampering() -> None:
    payload = deviation().to_payload()
    profile, report = scope_evidence()
    scope: dict[str, Any] = {"validation_profile": profile, "validation_report": report}
    legacy = {**payload, "schema_version": "1.0.0"}
    with pytest.raises(DeviationError, match="schema 2.0.0"):
        validate_scope_bound_payload(legacy, **scope)
    declared_scope = cast(dict[str, Any], payload["declared_scope"])
    changed_scope = {
        **payload,
        "declared_scope": {**declared_scope, "symbol": "ETH"},
    }
    with pytest.raises(DeviationError, match="scope hash"):
        validate_scope_bound_payload(changed_scope, **scope)
    changed_symbol = {**payload, "instruments": ["ETH"]}
    with pytest.raises(DeviationError, match="do not match"):
        validate_scope_bound_payload(changed_symbol, **scope)


# --- ADR-0104: run binding, refusal codes, versions -----------------------------------------


def test_the_declared_scope_binds_the_run_and_the_reference() -> None:
    run, request = _run(), reference_request()
    scope = deviation().declared_scope
    spec = router_strategy_spec(_router().spec, state=STATE)
    assert scope.schema_version == "1.1.0"
    assert scope.run_binding == RunBinding(
        router_spec_hash=run.router_spec_hash,
        router_strategy_spec_hash=spec.content_hash(),
        experiment_hash=EXPERIMENT_HASH,
        bars_hash=content_hash([bar.content_hash() for bar in run.request.bars]),
        window_start=min(bar.interval_start for bar in BARS),
        window_end=max(bar.interval_end for bar in BARS),
        cost_model_hash=ZERO.content_hash(),
        reference_request_hash=request.content_hash(),
    )
    assert scope.run_binding.reference_request_hash == deviation().reference_request_hash
    payload = cast(dict[str, Any], scope.to_payload())
    assert payload["run_binding"] == scope.run_binding.to_payload()
    body = {key: value for key, value in payload.items() if key != "scope_hash"}
    assert payload["scope_hash"] == content_hash(body)  # the hash covers the run binding
    # any single binding field changes the scope hash
    moved = dataclasses.replace(scope.run_binding, window_end=scope.run_binding.window_end + MINUTE)
    changed = dataclasses.replace(scope, run_binding=moved)
    assert changed.scope_hash != scope.scope_hash
    with pytest.raises(ValueError, match="scope 1.1.0"):
        dataclasses.replace(scope, schema_version="1.0.0")


def test_router_validation_binds_the_report_to_the_router_spec() -> None:
    _, report = scope_evidence()
    foreign = refusal(router_validation=router_validation(_run("0.02"), report, "0.02"))
    assert foreign.code == "router_spec_mismatch" and "another router spec" in str(foreign)
    genuine = router_validation(_run(), report)
    forged = refusal(router_validation=dataclasses.replace(genuine, binding_hash="e" * 64))
    assert forged.code == "router_spec_mismatch" and "binding_hash" in str(forged)
    other_strategy = refusal(
        router_validation=dataclasses.replace(genuine, router_strategy_spec_hash="d" * 64)
    )
    assert other_strategy.code == "router_spec_mismatch"
    # the supplied report must be the very report the binding covers
    rebuilt = report.model_copy(update={"report_id": "rep-other"})
    assert rebuilt.experiment_hash == report.experiment_hash
    unbound = refusal(validation_report=rebuilt)
    assert unbound.code == "router_spec_mismatch" and "not the report" in str(unbound)
    # paper_run_hash is deliberately not compared (ADR-0104 §2)
    other_run = dataclasses.replace(genuine, paper_run_hash="c" * 64)
    other_run = dataclasses.replace(
        other_run,
        binding_hash=_binding_hash(
            report.content_hash(),
            other_run.router_spec_hash,
            other_run.router_strategy_spec_hash,
            "c" * 64,
        ),
    )
    assert isinstance(
        paper_deviation(_run(), reference(), **scope_with(router_validation=other_run)),
        PaperDeviation,
    )
    not_validation = refusal(router_validation=object())
    assert not_validation.code == "scope_evidence_missing"
    missing = refusal(router_validation=None)
    assert missing.code == "scope_evidence_missing"


def test_a_report_of_another_experiment_is_refused() -> None:
    _, report = scope_evidence()
    other = report.model_copy(update={"experiment_hash": repro_tuple().experiment_hash})
    assert other.experiment_hash != EXPERIMENT_HASH
    error = refusal(validation_report=other)  # the binding covers the original report
    assert error.code == "experiment_mismatch"


def test_the_reference_request_is_required() -> None:
    error = refusal(reference_request=None)
    assert error.code == "reference_request_missing"


def test_a_reference_over_other_bars_is_refused() -> None:
    other_bars = make_bars(
        "BTC", [Decimal(c) for c in ("100", "101", "110", "121", "110", "99", "98")]
    )
    request = BacktestRequest(
        cost_model=ZERO,
        initial_equity=Decimal(1000),
        bars=other_bars,
        targets=STRATEGIES[A].positions,
    )
    result = BarBacktester().run(request)
    assert [p.time for p in result.equity_curve] == [p.time for p in _run().result.equity_curve]
    error = refusal(result=result, reference_request=request)
    assert error.code == "bars_mismatch"


def test_a_reference_under_another_cost_model_is_always_refused() -> None:
    costly = BacktestCostModel(
        name="costly", version="1.0.0", fee_rate=Decimal("0.001"), slippage_rate=Decimal(0)
    )
    request = BacktestRequest(
        cost_model=costly,
        initial_equity=Decimal(1000),
        bars=BARS,
        targets=STRATEGIES[A].positions,
    )
    result = BarBacktester().run(request)
    error = refusal(result=result, reference_request=request)
    assert error.code == "cost_model_mismatch"


def test_uncoded_alignment_refusals_have_no_code() -> None:
    assert (
        refusal(result=reference("2000"), reference_request=reference_request("2000")).code is None
    )


def test_the_error_code_is_an_attribute_and_the_message_is_unchanged() -> None:
    error = DeviationError("text")
    assert str(error) == "text" and error.code is None and isinstance(error, RouterError)
    assert DeviationError("text", code="bars_mismatch").code == "bars_mismatch"


def payload_with(**changes: Any) -> dict[str, Any]:
    """The current payload with ``changes`` applied and every hash re-sealed bottom-up, so that
    only the semantic checks (not the hash checks) can refuse it."""
    payload = cast(dict[str, Any], deviation().to_payload())
    scope = {**payload["declared_scope"], **changes.pop("scope", {})}
    binding = {**scope["run_binding"], **changes.pop("binding", {})}
    scope = {**scope, "run_binding": binding}
    scope["scope_hash"] = content_hash({k: v for k, v in scope.items() if k != "scope_hash"})
    payload = {**payload, **changes, "declared_scope": scope}
    payload["deviation_hash"] = content_hash(
        {k: v for k, v in payload.items() if k != "deviation_hash"}
    )
    return payload


def legacy_payload() -> dict[str, Any]:
    """An ADR-0079 payload: scope 1.0.0 (no run binding), payload 2.0.0, correctly hashed."""
    profile, report = scope_evidence()
    payload = cast(dict[str, Any], deviation().to_payload())
    scope = {
        "scope_schema_version": "1.0.0",
        "validation_profile": str(profile.ref),
        "validation_profile_hash": profile.content_hash(),
        "validation_report_hash": report.content_hash(),
        "venue": profile.scope.venue,
        "symbol": profile.scope.symbol,
        "timeframe": profile.scope.timeframe,
        "research_class": profile.scope.research_class,
    }
    scope["scope_hash"] = content_hash(scope)
    legacy = {**payload, "schema_version": "2.0.0", "declared_scope": scope}
    legacy["deviation_hash"] = content_hash(
        {k: v for k, v in legacy.items() if k != "deviation_hash"}
    )
    return legacy


def test_the_current_payload_is_run_bound_and_a_legacy_one_is_scope_only() -> None:
    profile, report = scope_evidence()
    kwargs: dict[str, Any] = {"validation_report": report, "validation_profile": profile}
    assert validate_scope_bound_payload(deviation().to_payload(), **kwargs) == "run_bound"
    # ADR-0079 payloads stay readable as they are (H6) ...
    assert validate_scope_bound_payload(legacy_payload(), **kwargs) == "scope_only"
    assert (
        validate_scope_bound_payload(legacy_payload(), required_min_scope_version="1.0.0", **kwargs)
        == "scope_only"
    )
    # ... but are refused by a consumer that needs a run binding
    with pytest.raises(DeviationError, match="below the required 1.1.0") as refused:
        validate_scope_bound_payload(legacy_payload(), required_min_scope_version="1.1.0", **kwargs)
    assert refused.value.code == "scope_version_unsupported"
    # a consumer on another major version refuses both
    for payload in (deviation().to_payload(), legacy_payload()):
        with pytest.raises(DeviationError) as other_major:
            validate_scope_bound_payload(payload, required_min_scope_version="2.0.0", **kwargs)
        assert other_major.value.code == "scope_version_unsupported"
    with pytest.raises(ValueError, match="MAJOR.MINOR.PATCH"):
        validate_scope_bound_payload(
            deviation().to_payload(), required_min_scope_version="one", **kwargs
        )


@pytest.mark.parametrize("scope_version", ["1.2.0", "2.0.0", "0.9.0", "1.1", "x", "1.0.0"])
def test_an_unsupported_or_mismatched_scope_version_is_refused(scope_version: str) -> None:
    profile, report = scope_evidence()
    payload = payload_with(scope={"scope_schema_version": scope_version})
    with pytest.raises(DeviationError) as refused:
        validate_scope_bound_payload(payload, validation_report=report, validation_profile=profile)
    assert refused.value.code == "scope_version_unsupported"  # incl. 2.1.0 carrying scope 1.0.0


def test_a_payload_schema_other_than_2_0_0_and_2_1_0_is_refused() -> None:
    profile, report = scope_evidence()
    for version in ("1.0.0", "2.2.0", "3.0.0"):
        payload = {**cast(dict[str, Any], deviation().to_payload()), "schema_version": version}
        with pytest.raises(DeviationError, match="schema 2.0.0") as refused:
            validate_scope_bound_payload(
                payload, validation_report=report, validation_profile=profile
            )
        assert refused.value.code == "scope_version_unsupported"


def test_a_tampered_run_binding_is_refused() -> None:
    profile, report = scope_evidence()
    kwargs: dict[str, Any] = {"validation_report": report, "validation_profile": profile}
    genuine = cast(dict[str, Any], deviation().to_payload())
    # edited in place: the scope hash (and the deviation hash) no longer match
    edited = {
        **genuine,
        "declared_scope": {
            **genuine["declared_scope"],
            "run_binding": {**genuine["declared_scope"]["run_binding"], "bars_hash": "0" * 64},
        },
    }
    with pytest.raises(DeviationError, match="scope hash"):
        validate_scope_bound_payload(edited, **kwargs)
    # re-sealed scope but stale deviation hash
    scope = dict(edited["declared_scope"])
    scope["scope_hash"] = content_hash({k: v for k, v in scope.items() if k != "scope_hash"})
    with pytest.raises(DeviationError, match="deviation hash|hash does not match its payload"):
        validate_scope_bound_payload({**edited, "declared_scope": scope}, **kwargs)
    # fully re-sealed forgeries are refused by what the P8 evidence and the payload say
    forged_experiment = payload_with(binding={"experiment_hash": "9" * 64})
    with pytest.raises(DeviationError) as experiment:
        validate_scope_bound_payload(forged_experiment, **kwargs)
    assert experiment.value.code == "experiment_mismatch"
    for binding in (
        {"reference_request_hash": "9" * 64},  # not the payload's reference request
        {"bars_hash": "NOT-A-HASH"},
        {"window_start": "yesterday"},
        {"window_start": "2099-01-01T00:00:00+00:00"},  # after window_end
        {"surprise": "1"},  # an unknown field
    ):
        with pytest.raises(DeviationError, match="run binding"):
            validate_scope_bound_payload(payload_with(binding=binding), **kwargs)
    removed = payload_with()
    del removed["declared_scope"]["run_binding"]
    removed["declared_scope"]["scope_hash"] = content_hash(
        {k: v for k, v in removed["declared_scope"].items() if k != "scope_hash"}
    )
    removed["deviation_hash"] = content_hash(
        {k: v for k, v in removed.items() if k != "deviation_hash"}
    )
    with pytest.raises(DeviationError, match="run binding is malformed"):
        validate_scope_bound_payload(removed, **kwargs)
    # the report must be the declared one
    wrong_report = report.model_copy(update={"report_id": "rep-other"})
    with pytest.raises(DeviationError, match="does not match the P8 evidence"):
        validate_scope_bound_payload(
            deviation().to_payload(), validation_report=wrong_report, validation_profile=profile
        )

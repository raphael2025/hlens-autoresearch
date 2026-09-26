"""Phase 9 framework: the gate calibration harness (evidence for D-09, never a Profile decision).

Two detectors:

- the full G0 → G4 pipeline through ``PipelineBacktestValidator`` (``gate_fixtures.detector``);
- a toy, Profile-reading detector (TEST ONLY) whose pass rule is a single lag-autocorrelation
  test at the candidate's ``significance.multiple_testing_threshold`` — it isolates the harness
  mechanics (a lax candidate must show a visibly high pipeline-level false-positive rate).

Every Profile here is TEST ONLY and deliberately extreme (see ``gate_fixtures``).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from core.contracts.synthetic import PlantedEffect, SyntheticMarket
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import content_hash
from core.domain.research import GateResult, ValidationReport, Verdict
from plugins.synthetic import RandomWalkMarket
from research.reports import ReportConflict
from research.synthetic_lab.gate_calibration import (
    DISCLAIMER,
    NOISE_ARM,
    GateCalibrationReport,
    GateCalibrationSetup,
    main,
    planted_arm_id,
    run_gate_calibration,
    write_gate_calibration,
)
from research.synthetic_lab.intervals import binomial_rate, clopper_pearson
from tests import factories
from tests.research.synthetic_lab import gate_fixtures as fx

SEEDS = 8


@pytest.fixture(scope="module")
def pipeline_report() -> GateCalibrationReport:
    return run_gate_calibration(fx.setup(SEEDS))


# --------------------------------------------------------------------------------------
# Intervals
# --------------------------------------------------------------------------------------


def test_clopper_pearson_matches_known_values() -> None:
    # Closed forms: k = 0 -> upper = 1 - (alpha/2)^(1/n); k = n -> lower = (alpha/2)^(1/n).
    alpha = Decimal("0.05")
    lower, upper = clopper_pearson(0, 8, alpha)
    assert lower == Decimal(0)
    assert abs(float(upper) - (1 - 0.025 ** (1 / 8))) < 2e-6
    lower, upper = clopper_pearson(8, 8, alpha)
    assert upper == Decimal(1)
    assert abs(float(lower) - 0.025 ** (1 / 8)) < 2e-6
    lower, upper = clopper_pearson(4, 8, alpha)  # symmetric around 1/2
    assert lower + upper in {Decimal(1), Decimal("1.000001")}
    assert Decimal("0.15") < lower < Decimal("0.16") and Decimal("0.84") < upper < Decimal("0.85")


def test_binomial_rate_is_decimal_and_contains_the_rate() -> None:
    rate = binomial_rate(3, 8, Decimal("0.05"))
    assert rate.rate == Decimal("0.375000")
    assert rate.lower <= rate.rate <= rate.upper
    assert all(isinstance(v, Decimal) for v in (rate.rate, rate.lower, rate.upper))
    with pytest.raises(ValueError):
        binomial_rate(9, 8, Decimal("0.05"))
    with pytest.raises(ValueError):
        clopper_pearson(1, 8, Decimal(1))


# --------------------------------------------------------------------------------------
# Harness mechanics with a toy, Profile-reading detector (TEST ONLY)
# --------------------------------------------------------------------------------------


def _lag_autocorrelation_z(market: SyntheticMarket, lag: int) -> float:
    returns = [float(bar.close / bar.open - 1) for bar in market.bars]
    mean = sum(returns) / len(returns)
    num = sum((returns[i] - mean) * (returns[i - lag] - mean) for i in range(lag, len(returns)))
    den = sum((r - mean) ** 2 for r in returns)
    return (num / den) * math.sqrt(len(returns))


class _ToyDetector:
    """TEST ONLY: PASS iff the two-sided lag-60 autocorrelation p-value <= the candidate level."""

    name = "toy_lag60_autocorrelation"

    def describe(self) -> Mapping[str, str]:
        return {"kind": "test_only_toy"}

    def detect(self, market: SyntheticMarket, profile: ValidationProfile) -> ValidationReport:
        z = _lag_autocorrelation_z(market, 60)
        p_value = math.erfc(abs(z) / math.sqrt(2))
        level = profile.significance.multiple_testing_threshold
        verdict = Verdict.PASS if p_value <= level else Verdict.FAIL
        gate = GateResult(
            gate_id="T0.autocorrelation",
            metric="lag60_p_value[<=]",
            value=p_value,
            threshold=level,
            threshold_source="significance.multiple_testing_threshold",
            verdict=verdict,
        )
        return factories.validation_report(
            verdict,
            gates=(gate,),
            validation_profile=profile.ref,
            validation_profile_hash=profile.content_hash(),
        )


def _with_level(name: str, level: float) -> ValidationProfile:
    significance = fx.LAX_TEST_ONLY_PROFILE.significance.model_copy(
        update={"multiple_testing_threshold": level}
    )
    return fx.LAX_TEST_ONLY_PROFILE.model_copy(update={"name": name, "significance": significance})


#: TEST ONLY — a lax and a strict candidate for the toy detector.
TOY_LAX = _with_level("test_only_toy_lax", 0.9)
TOY_STRICT = _with_level("test_only_toy_strict", 1e-9)
TOY_EFFECT = PlantedEffect(lag_minutes=60, strength=Decimal("0.08"))


def _toy_setup() -> GateCalibrationSetup:
    return GateCalibrationSetup(
        provider=RandomWalkMarket(),
        base=fx.BASE_SPEC,
        detector=_ToyDetector(),
        candidates=(TOY_LAX, TOY_STRICT),
        noise_seeds=tuple(range(SEEDS)),
        planted=(TOY_EFFECT,),
        planted_seeds=tuple(range(100, 100 + SEEDS)),
        alpha=fx.TEST_ONLY_ALPHA,
    )


@pytest.fixture(scope="module")
def toy_report() -> GateCalibrationReport:
    return run_gate_calibration(_toy_setup())


def test_a_lax_candidate_shows_a_visibly_high_false_positive_rate(
    toy_report: GateCalibrationReport,
) -> None:
    lax = toy_report.candidate(TOY_LAX).false_positive_rate
    strict = toy_report.candidate(TOY_STRICT).false_positive_rate
    assert lax.rate >= Decimal("0.5"), lax
    assert lax.lower > Decimal("0.1")  # the interval itself excludes a small nominal level
    assert strict.rate == 0


def test_a_strict_candidate_loses_power(toy_report: GateCalibrationReport) -> None:
    lax = toy_report.candidate(TOY_LAX).power(TOY_EFFECT)
    strict = toy_report.candidate(TOY_STRICT).power(TOY_EFFECT)
    assert lax.rate == 1
    assert strict.rate < lax.rate


# --------------------------------------------------------------------------------------
# The full pipeline detector
# --------------------------------------------------------------------------------------


def test_every_run_is_reported_per_candidate_and_arm(
    pipeline_report: GateCalibrationReport,
) -> None:
    arms = (NOISE_ARM, planted_arm_id(fx.STRONG), planted_arm_id(fx.WEAK))
    assert len(pipeline_report.candidates) == 2
    for candidate in pipeline_report.candidates:
        assert len(candidate.runs) == 3 * SEEDS
        assert tuple(arm.arm for arm in candidate.arms) == arms
        for arm in candidate.arms:
            assert arm.passed.n == SEEDS
            assert arm.passed.count + arm.inconclusive.count + arm.failed == SEEDS
        stages = {gate_id.split(".")[0] for gate_id in candidate.gate_ids()}
        assert {"G0", "G1", "G2"} <= stages
        for gate in candidate.gates:
            evaluated = gate.passed.count + gate.inconclusive.count + gate.failed
            assert evaluated + gate.not_evaluated == SEEDS


def test_the_lax_candidate_passes_noise_at_a_high_rate_on_a_lax_gate(
    pipeline_report: GateCalibrationReport,
) -> None:
    """Per gate: a 0th-percentile null-model bar lets noise through almost whenever reached."""
    lax = pipeline_report.candidate(fx.LAX_TEST_ONLY_PROFILE)
    strict = pipeline_report.candidate(fx.STRICT_TEST_ONLY_PROFILE)
    gate = "G2.null_model_percentile"
    lax_fpr = lax.gate(gate, NOISE_ARM).passed
    assert lax_fpr.rate >= Decimal("0.5"), lax_fpr
    assert strict.gate(gate, NOISE_ARM).passed.rate < lax_fpr.rate


def test_the_strict_candidate_loses_power(pipeline_report: GateCalibrationReport) -> None:
    lax = pipeline_report.candidate(fx.LAX_TEST_ONLY_PROFILE)
    strict = pipeline_report.candidate(fx.STRICT_TEST_ONLY_PROFILE)
    assert lax.power(fx.STRONG).count > 0
    assert strict.power(fx.STRONG).rate < lax.power(fx.STRONG).rate
    assert strict.power(fx.WEAK).rate <= lax.power(fx.WEAK).rate


def test_only_passes_consume_the_sealed_oos(pipeline_report: GateCalibrationReport) -> None:
    for candidate in pipeline_report.candidates:
        for arm in candidate.arms:
            assert arm.sealed_oos_consumed.count == arm.passed.count
        unsealed = [run for run in candidate.runs if run.sealed_oos_unsealed]
        assert all(run.verdict is Verdict.PASS for run in unsealed)
        assert candidate.sealed_oos_unsealings == len(unsealed)
    lax = pipeline_report.candidate(fx.LAX_TEST_ONLY_PROFILE)
    assert lax.sealed_oos_unsealings > 0


def test_the_report_records_every_input(pipeline_report: GateCalibrationReport) -> None:
    payload = pipeline_report.to_payload()
    inputs = payload["inputs"]
    assert isinstance(inputs, dict)
    assert inputs["noise_seeds"] == list(range(SEEDS))
    assert inputs["planted_seeds"] == list(range(100, 100 + SEEDS))
    assert inputs["base_spec_hash"] == fx.BASE_SPEC.content_hash()
    assert [p["profile_hash"] for p in inputs["candidate_profiles"]] == [
        fx.LAX_TEST_ONLY_PROFILE.content_hash(),
        fx.STRICT_TEST_ONLY_PROFILE.content_hash(),
    ]
    assert [e["effect_hash"] for e in inputs["planted_effects"]] == [
        fx.STRONG.content_hash(),
        fx.WEAK.content_hash(),
    ]
    assert inputs["interval"] == {"method": "clopper-pearson", "alpha": "0.05"}
    assert inputs["detector"]["name"] == "tsmom60_full_pipeline"
    assert payload["disclaimer"] == DISCLAIMER == "evidence only — not a Profile decision"
    body = {k: v for k, v in payload.items() if k != "report_hash"}
    assert payload["report_hash"] == content_hash(body)


def _keys(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def _leaves(value: object) -> Iterator[object]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    else:
        yield value


def test_the_report_has_no_recommended_value(pipeline_report: GateCalibrationReport) -> None:
    payload = pipeline_report.to_payload()
    banned = ("recommend", "suggest", "default", "proposed", "best", "selected", "optimal")
    offending = [key for key in _keys(payload) if any(word in key.lower() for word in banned)]
    assert offending == []
    assert not any(isinstance(leaf, float) for leaf in _leaves(payload))  # nothing unhashable


def test_the_report_is_deterministic() -> None:
    first = run_gate_calibration(fx.setup(2))
    second = run_gate_calibration(fx.setup(2))
    assert first.report_hash == second.report_hash
    assert first.to_payload() == second.to_payload()
    other = run_gate_calibration(fx.setup(2, planted=(fx.STRONG,)))
    assert other.report_hash != first.report_hash


def test_the_report_is_written_through_the_report_writer(
    toy_report: GateCalibrationReport, tmp_path: Path
) -> None:
    written = write_gate_calibration(tmp_path, _toy_setup())
    assert written.written and written.id == toy_report.report_hash
    assert written.path == tmp_path / "gate_calibration" / f"{toy_report.report_hash}.json"
    loaded = json.loads(written.path.read_text(encoding="utf-8"))
    assert loaded == json.loads(json.dumps(toy_report.to_payload()))
    again = write_gate_calibration(tmp_path, _toy_setup())
    assert not again.written  # same content: an idempotent no-op
    written.path.write_text('{"tampered": true}', encoding="utf-8")
    with pytest.raises(ReportConflict):
        write_gate_calibration(tmp_path, _toy_setup())


def test_the_console_serves_the_written_report(
    toy_report: GateCalibrationReport, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient

    from apps.api.app import create_app
    from apps.api.store import ReportKind, ReportStore

    write_gate_calibration(tmp_path, _toy_setup())
    envelope = ReportStore(tmp_path).get(ReportKind.GATE_CALIBRATION, toy_report.report_hash)
    assert envelope.payload == json.loads(json.dumps(toy_report.to_payload()))
    client = TestClient(create_app(reports_root=tmp_path))
    listed = client.get("/reports/gate_calibration").json()["reports"]
    assert [item["id"] for item in listed] == [toy_report.report_hash]


def test_the_cli_writes_the_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["--setup", "tests.research.synthetic_lab.gate_fixtures:cli_setup", "--out", str(tmp_path)]
    )
    assert code == 0
    (path,) = (tmp_path / "gate_calibration").iterdir()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["disclaimer"] == DISCLAIMER
    assert path.stem == loaded["report_hash"]
    assert "gate_calibration/" in capsys.readouterr().out


def test_the_setup_refuses_ambiguous_inputs() -> None:
    base = fx.setup(1)
    with pytest.raises(ValueError, match="pure noise"):
        GateCalibrationSetup(
            **{**base.__dict__, "base": fx.BASE_SPEC.model_copy(update={"effects": (fx.WEAK,)})}
        )
    with pytest.raises(ValueError, match="distinct"):
        GateCalibrationSetup(**{**base.__dict__, "candidates": (TOY_LAX, TOY_LAX)})
    with pytest.raises(ValueError, match="candidate"):
        GateCalibrationSetup(**{**base.__dict__, "candidates": ()})
    with pytest.raises(ValueError, match="alpha"):
        GateCalibrationSetup(**{**base.__dict__, "alpha": Decimal(0)})
    with pytest.raises(ValueError, match="distinct"):
        GateCalibrationSetup(**{**base.__dict__, "noise_seeds": (1, 1)})


# --------------------------------------------------------------------------------------
# Detector failures are INCONCLUSIVE evidence; harness misconfiguration still raises
# --------------------------------------------------------------------------------------


class _RaisingOnPlantedDetector(_ToyDetector):
    """TEST ONLY: the toy detector, except it raises on every market with a planted effect."""

    name = "toy_raising_on_planted"

    def detect(self, market: SyntheticMarket, profile: ValidationProfile) -> ValidationReport:
        if market.truth:
            raise ZeroDivisionError("toy detector broke on\nthis market")
        return super().detect(market, profile)


def _raising_setup() -> GateCalibrationSetup:
    return GateCalibrationSetup(
        **{**_toy_setup().__dict__, "detector": _RaisingOnPlantedDetector()}
    )


def test_a_raising_detector_yields_inconclusive_runs_not_an_exception() -> None:
    report = run_gate_calibration(_raising_setup())
    arm = planted_arm_id(TOY_EFFECT)
    for profile in (TOY_LAX, TOY_STRICT):
        candidate = report.candidate(profile)
        planted = candidate.arm(arm)
        assert planted.passed.count == 0 and planted.failed == 0
        assert planted.inconclusive.count == SEEDS and planted.detector_errors == SEEDS
        assert candidate.arm(NOISE_ARM).detector_errors == 0
        gate = candidate.gate("T0.autocorrelation", arm)
        assert gate.not_evaluated == SEEDS and gate.passed.count == 0
        errored = [run for run in candidate.runs if run.arm == arm]
        assert all(run.detector_error == "ZeroDivisionError: toy detector broke on this market"
                   for run in errored)  # fmt: skip
        assert not any(run.sealed_oos_unsealed for run in errored)
    payload: Any = json.loads(json.dumps(report.to_payload()))
    lax = next(c for c in payload["candidates"] if c["profile_hash"] == TOY_LAX.content_hash())
    assert lax["pipeline"][arm]["detector_errors"] == SEEDS
    assert "detector_errors" not in lax["pipeline"][NOISE_ARM]
    assert run_gate_calibration(_raising_setup()).report_hash == report.report_hash


def test_reports_without_detector_errors_have_no_error_keys(
    toy_report: GateCalibrationReport,
) -> None:
    text = json.dumps(toy_report.to_payload())
    assert "detector_error" not in text


def test_an_errored_run_record_must_be_inconclusive_without_gates() -> None:
    from research.synthetic_lab.gate_calibration import RunRecord

    fields = {
        "arm": NOISE_ARM, "seed": 0, "market_spec_hash": "a", "market_hash": "b",
        "gates_hash": content_hash([]), "detector_error": "RuntimeError: x",
    }  # fmt: skip
    with pytest.raises(ValueError, match="INCONCLUSIVE"):
        RunRecord(verdict=Verdict.FAIL, gates=(), **fields)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="INCONCLUSIVE"):
        RunRecord(
            verdict=Verdict.INCONCLUSIVE,
            gates=(("G1.x", Verdict.INCONCLUSIVE),),
            **fields,  # type: ignore[arg-type]
        )


def test_harness_misconfiguration_still_raises() -> None:
    from research.synthetic_lab.gate_calibration import DetectorConfigurationError

    class _Misconfigured(_ToyDetector):
        def detect(self, market: SyntheticMarket, profile: ValidationProfile) -> ValidationReport:
            raise DetectorConfigurationError("setup_for must use the trial runner it is given")

    setup = GateCalibrationSetup(**{**_toy_setup().__dict__, "detector": _Misconfigured()})
    with pytest.raises(DetectorConfigurationError):
        run_gate_calibration(setup)

    class _WrongProfile(_ToyDetector):
        def detect(self, market: SyntheticMarket, profile: ValidationProfile) -> ValidationReport:
            return super().detect(market, TOY_STRICT if profile is TOY_LAX else TOY_LAX)

    setup = GateCalibrationSetup(**{**_toy_setup().__dict__, "detector": _WrongProfile()})
    with pytest.raises(ValueError, match="report is not under"):
        run_gate_calibration(setup)

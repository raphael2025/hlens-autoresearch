"""Phase 9: the gate calibration harness's optional G5 mode (evidence only, not a Profile decision).

G5 mode (``GateCalibrationSetup.sealed_oos_g5``) is opt-in. With it off every report hash is the
pre-G5 harness's (pinned below). With it on, only a G0 – G4 ``PASS`` is unsealed and claimed; the
sealed-window bars are withheld from ``detect`` and released only after the claim; an early exit
or an exception after the claim is ``INCONCLUSIVE`` ``consumed_without_result``, never a pass.

Every Profile here is TEST ONLY and deliberately extreme (see ``gate_fixtures``); the toy detectors
are TEST ONLY too.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from core.contracts.profile_selection import OosUnsealing
from core.contracts.synthetic import SyntheticBar, SyntheticMarket
from core.contracts.validation_profile import ValidationProfile
from core.domain.research import GateResult, ValidationReport, Verdict
from research.synthetic_lab import gate_calibration as gc
from research.synthetic_lab.calibration import PROPAGATED_ERRORS
from research.synthetic_lab.gate_calibration import (
    NOISE_ARM,
    DetectorConfigurationError,
    GateCalibrationReport,
    GateCalibrationSetup,
    SealedArmEvidence,
    SealedRelease,
    planted_arm_id,
    run_gate_calibration,
)
from research.synthetic_lab.intervals import binomial_rate
from research.validation import build_report, sealed_oos_without_result
from research.validation.gates import ProfileFieldMissing, profile_value
from research.validation.sealed_oos import InMemoryUnsealingLedger
from research.validation.splits import LabeledSpan
from tests import factories
from tests.research.synthetic_lab import gate_fixtures as fx
from tests.research.synthetic_lab.test_gate_calibration import (
    TOY_EFFECT,
    TOY_LAX,
    TOY_STRICT,
    _keys,
    _leaves,
    _raising_setup,
    _toy_setup,
    _ToyDetector,
)

SEEDS = 4

#: Report hashes of the harness **before** G5 mode existed (computed at b3a46e3). G5 mode off must
#: keep every report byte-identical; the toy hash is also a committed console fixture
#: (``apps/web/fixtures``). ``PRE_G5_PIPELINE_ONE_SEED_HASH`` was re-pinned for ADR-0060
#: enforcement (2026-09-26): the pipeline detector now validates with ``market_benchmark=True``
#: under Profiles naming ``buy_and_hold_equal_weight`` + inverse control, so its reports carry
#: the reported-only ``G2.market_benchmark.*`` / ``G2.inverse_control`` items. The toy and
#: raising hashes are unchanged (the toy Profiles keep their pre-enforcement benchmark block,
#: ``test_gate_calibration._TOY_BENCHMARK``).
#: Re-pinned for contract 2.1.0 (ADR-0052 §4, 2026-09-26): the intended envelope change only —
#: every newly built contract object is 2.1.0 and the envelope is part of each content hash.
#: The previous values still hold when the same test builds every object at 2.0.0
#: (verified by running it inside ``contract_schema_version_scope("2.0.0")``).
#: 2.0.0 values (evidence, git history): deaba504…, 358eb551…, dc7816c9…
#: Re-pinned for contract 2.2.0 (ADR-0055, 2026-09-26): envelope change only; the 2.1.0
#: values still hold when the test builds every object at 2.1.0 (verified: the unmodified
#: test passes inside ``contract_schema_version_scope("2.1.0")``).
#: 2.1.0 values (evidence, git history): c5147ea3…, 03fcfad6… (raising), 4cc8dc82… (pipeline)
#: ``PRE_G5_RAISING_HASH`` re-pinned (audit fix, 2026-09-26): an arm with detector errors now
#: also reports ``pass_rate_bounds``; the raising report is the only pinned one with errors.
#: Removing that key from its payload reproduces the previous value e31fc17f… exactly (verified);
#: the toy and pipeline hashes (no detector errors) are unchanged.
PRE_G5_TOY_HASH = "8499223e39d1c260e76e2cb18b6af4c227914b140ffcf6089771accd3b16b535"
PRE_G5_RAISING_HASH = "0ea408c308dcb739b585892941af14e5c06e8777a70a8ad762c18d8cea311a7e"
PRE_G5_PIPELINE_ONE_SEED_HASH = "6fa8c6ec2c479b3d96039465cce050933f341385dcde272e061e09e8703c91dc"


class RecordingLedger(InMemoryUnsealingLedger):
    """TEST ONLY: an in-memory ledger that logs every unsealing and every claimed evaluation."""

    log: ClassVar[list[tuple[str, str]]] = []

    def record(self, family_id: str, unsealing: OosUnsealing) -> None:
        super().record(family_id, unsealing)
        RecordingLedger.log.append(("unseal", family_id))

    def mark_evaluated(self, family_id: str) -> None:
        super().mark_evaluated(family_id)
        RecordingLedger.log.append(("evaluated", family_id))


@pytest.fixture
def ledger_log(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, str]]]:
    RecordingLedger.log = []
    monkeypatch.setattr(gc, "InMemoryUnsealingLedger", RecordingLedger)
    yield RecordingLedger.log


def _families(log: list[tuple[str, str]], event: str) -> list[str]:
    """Sorted (the log interleaves the candidates' vaults: market-outer, Profile-inner)."""
    return sorted(family for kind, family in log if kind == event)


def _passing_families(report: GateCalibrationReport, *profiles: ValidationProfile) -> list[str]:
    return sorted(
        f"gate_calibration:{r.arm}:{r.seed}"
        for profile in profiles
        for r in report.candidate(profile).runs
        if r.verdict is Verdict.PASS
    )


# --------------------------------------------------------------------------------------
# Toy detectors (TEST ONLY)
# --------------------------------------------------------------------------------------


def _lag60_z(bars: Sequence[SyntheticBar]) -> float:
    """The toy detector's lag-60 autocorrelation z-score, on a bar sequence."""
    returns = [float(bar.close / bar.open - 1) for bar in bars]
    mean = sum(returns) / len(returns)
    num = sum((returns[i] - mean) * (returns[i - 60] - mean) for i in range(60, len(returns)))
    den = sum((r - mean) ** 2 for r in returns)
    return (num / den) * math.sqrt(len(returns))


class _ToySealedDetector(_ToyDetector):
    """TEST ONLY: the toy lag-60 test, repeated on the released sealed window as its G5 gate."""

    name = "toy_lag60_autocorrelation_with_g5"

    def __init__(self) -> None:
        self.detect_last_bar_end: list[Any] = []
        self.sealed_families: list[str] = []

    def detect(self, market: SyntheticMarket, profile: ValidationProfile) -> ValidationReport:
        self.detect_last_bar_end.append(market.bars[-1].interval_end)
        return super().detect(market, profile)

    def _g5_context(self, profile: ValidationProfile, sealed: SealedRelease) -> Any:
        context = fx.context(fx.candidate(), profile)
        metadata = context.metadata.model_copy(
            update={"hypothesis_family_id": sealed.family_id, "oos_unsealing": sealed.unsealing}
        )
        return replace(context, metadata=metadata)

    def detect_sealed(
        self, market: SyntheticMarket, profile: ValidationProfile, sealed: SealedRelease
    ) -> ValidationReport:
        self.sealed_families.append(sealed.family_id)
        bars = sealed.release()
        spans = [
            LabeledSpan(key=str(i), start=bar.interval_start, end=bar.interval_end)
            for i, bar in enumerate(bars)
        ]
        kept = {span.key for span in sealed.evaluation.view(spans)}  # the labels, taken once
        z = _lag60_z([bar for i, bar in enumerate(bars) if str(i) in kept])
        p_value = math.erfc(abs(z) / math.sqrt(2))
        level = profile.significance.multiple_testing_threshold
        gate = GateResult(
            gate_id="G5.toy_autocorrelation",
            metric="lag60_p_value[<=]",
            value=p_value,
            threshold=level,
            threshold_source="significance.multiple_testing_threshold",
            verdict=Verdict.PASS if p_value <= level else Verdict.FAIL,
        )
        return factories.validation_report(
            gate.verdict,
            gates=(gate,),
            validation_profile=profile.ref,
            validation_profile_hash=profile.content_hash(),
        )


class _RaisingAfterRelease(_ToySealedDetector):
    name = "toy_g5_raising_after_release"

    def detect_sealed(
        self, market: SyntheticMarket, profile: ValidationProfile, sealed: SealedRelease
    ) -> ValidationReport:
        sealed.release()
        raise RuntimeError("toy G5 broke\non the sealed window")


class _RaisingBeforeRelease(_ToySealedDetector):
    name = "toy_g5_raising_before_release"

    def detect_sealed(
        self, market: SyntheticMarket, profile: ValidationProfile, sealed: SealedRelease
    ) -> ValidationReport:
        # A runtime failure (not a ValueError: that is a configuration refusal and propagates,
        # ``calibration.PROPAGATED_ERRORS``).
        raise LookupError("toy G5 gave up")


class _EarlyExit(_ToySealedDetector):
    """Returns ``consumed_without_result`` after the claim, like a sealed window without trades."""

    name = "toy_g5_early_exit"

    def detect_sealed(
        self, market: SyntheticMarket, profile: ValidationProfile, sealed: SealedRelease
    ) -> ValidationReport:
        sealed.release()
        context = self._g5_context(profile, sealed)
        gates = sealed_oos_without_result(context, sealed.vault, sealed.evaluation, "toy_no_trade")
        return build_report(context, gates)


class _PassWithoutEvaluation(_ToySealedDetector):
    """Misconfigured: a G5 PASS that never took the claimed labels."""

    name = "toy_g5_pass_without_evaluation"

    def detect_sealed(
        self, market: SyntheticMarket, profile: ValidationProfile, sealed: SealedRelease
    ) -> ValidationReport:
        gate = GateResult(gate_id="G5.toy", metric="m", value=0.0, verdict=Verdict.PASS)
        return factories.validation_report(
            Verdict.PASS,
            gates=(gate,),
            validation_profile=profile.ref,
            validation_profile_hash=profile.content_hash(),
        )


class _NoG5Gates(_ToySealedDetector):
    name = "toy_g5_without_g5_gates"

    def detect_sealed(
        self, market: SyntheticMarket, profile: ValidationProfile, sealed: SealedRelease
    ) -> ValidationReport:
        return super().detect(market, profile)


def _toy_g5_setup(detector: _ToySealedDetector | None = None) -> GateCalibrationSetup:
    return GateCalibrationSetup(
        **{
            **_toy_setup().__dict__,
            "base": fx.G5_BASE_SPEC,
            "detector": detector if detector is not None else _ToySealedDetector(),
            "noise_seeds": tuple(range(SEEDS)),
            "planted_seeds": tuple(range(100, 100 + SEEDS)),
            "sealed_oos_g5": True,
        }
    )


# --------------------------------------------------------------------------------------
# G5 mode off: byte-identical
# --------------------------------------------------------------------------------------


def test_g5_mode_off_keeps_every_pre_g5_report_hash() -> None:
    assert _toy_setup().sealed_oos_g5 is False  # opt-in: off unless the caller sets it
    assert run_gate_calibration(_toy_setup()).report_hash == PRE_G5_TOY_HASH
    explicit = GateCalibrationSetup(**{**_toy_setup().__dict__, "sealed_oos_g5": False})
    assert run_gate_calibration(explicit).report_hash == PRE_G5_TOY_HASH
    assert run_gate_calibration(_raising_setup()).report_hash == PRE_G5_RAISING_HASH
    pipeline = run_gate_calibration(fx.setup(1))
    assert pipeline.report_hash == PRE_G5_PIPELINE_ONE_SEED_HASH
    text = json.dumps(pipeline.to_payload())
    assert "sealed_oos_g5" not in text and "end_to_end" not in text
    with pytest.raises(ValueError, match="sealed_oos_g5 is off"):
        _ = pipeline.candidate(fx.LAX_TEST_ONLY_PROFILE).end_to_end_false_positive_rate


def test_g5_mode_off_never_claims_an_evaluation(ledger_log: list[tuple[str, str]]) -> None:
    report = run_gate_calibration(_toy_setup())
    assert _families(ledger_log, "evaluated") == []
    unsealed = _families(ledger_log, "unseal")
    assert unsealed == _passing_families(report, TOY_LAX, TOY_STRICT)


# --------------------------------------------------------------------------------------
# G5 mode: refusals (decided from the setup only)
# --------------------------------------------------------------------------------------


def test_g5_mode_refuses_a_detector_without_detect_sealed() -> None:
    with pytest.raises(ValueError, match="detect_sealed"):
        GateCalibrationSetup(
            **{**_toy_setup().__dict__, "base": fx.G5_BASE_SPEC, "sealed_oos_g5": True}
        )
    # The strategy detector supports G5 only when it is given sealed_inputs_for.
    with pytest.raises(ValueError, match="detect_sealed"):
        GateCalibrationSetup(
            **{**fx.setup(1, sealed_oos_g5=True).__dict__, "detector": fx.detector()}
        )


def test_g5_mode_refuses_a_base_spec_without_the_sealed_window() -> None:
    with pytest.raises(ValueError, match="sealed window"):
        GateCalibrationSetup(**{**_toy_g5_setup().__dict__, "base": fx.BASE_SPEC})
    with pytest.raises(ValueError, match="bool"):
        GateCalibrationSetup(**{**_toy_g5_setup().__dict__, "sealed_oos_g5": 1})


# --------------------------------------------------------------------------------------
# G5 mode with a toy detector: mechanics
# --------------------------------------------------------------------------------------


def test_only_g0_g4_passes_reach_g5_and_sealed_bars_stay_withheld(
    ledger_log: list[tuple[str, str]],
) -> None:
    detector = _ToySealedDetector()
    report = run_gate_calibration(_toy_g5_setup(detector))
    # detect never received a sealed-window bar (the research view ends at the boundary)
    assert detector.detect_last_bar_end and max(detector.detect_last_bar_end) <= fx.BOUNDARY
    passing = _passing_families(report, TOY_LAX, TOY_STRICT)
    assert passing  # the lax candidate passes noise and planted markets
    # unsealed, claimed and handed to detect_sealed: exactly the G0 - G4 passes, in order
    assert _families(ledger_log, "unseal") == passing
    assert _families(ledger_log, "evaluated") == passing
    assert sorted(detector.sealed_families) == passing
    for profile in (TOY_LAX, TOY_STRICT):
        candidate = report.candidate(profile)
        for run in candidate.runs:
            if run.verdict is Verdict.PASS:
                assert run.g5 is not None and run.g5.sealed_bars_released
                assert run.g5.family_id == f"gate_calibration:{run.arm}:{run.seed}"
                assert not run.g5.consumed_without_result
            else:
                assert run.g5 is None and not run.sealed_oos_unsealed
        assert candidate.sealed_oos_g5_evaluations == len(_passing_families(report, profile))


def test_g5_rates_are_reported_per_arm_with_end_to_end_rates() -> None:
    report = run_gate_calibration(_toy_g5_setup())
    arms = (NOISE_ARM, planted_arm_id(TOY_EFFECT))
    for profile in (TOY_LAX, TOY_STRICT):
        candidate = report.candidate(profile)
        for arm in arms:
            pipeline, g5 = candidate.arm(arm), candidate.g5(arm)
            assert g5.reached == pipeline.passed.count == pipeline.sealed_oos_consumed.count
            assert g5.end_to_end.n == SEEDS
            if g5.reached:
                assert g5.passed is not None and g5.inconclusive is not None and g5.failed
                assert g5.passed.n == g5.reached
                total = g5.passed.count + g5.inconclusive.count + g5.failed.count
                assert total == g5.reached
                assert g5.end_to_end.count == g5.passed.count
            else:
                assert g5.passed is None and g5.end_to_end.count == 0
            assert g5.end_to_end.count <= pipeline.passed.count  # G5 only removes passes
            if "G5.toy_autocorrelation" in candidate.gate_ids():  # some run of the candidate
                gate = candidate.gate("G5.toy_autocorrelation", arm)
                assert gate.not_evaluated == SEEDS - g5.reached
            else:  # no run reached G5: no G5 gate row (as for any gate never evaluated)
                assert sum(candidate.g5(a).reached for a in arms) == 0
    lax = report.candidate(TOY_LAX)
    assert lax.end_to_end_power(TOY_EFFECT).count > 0  # the planted effect survives G5
    assert lax.end_to_end_false_positive_rate is lax.g5(NOISE_ARM).end_to_end
    strict = report.candidate(TOY_STRICT)
    assert strict.end_to_end_false_positive_rate.count == 0
    payload: Any = json.loads(json.dumps(report.to_payload()))
    assert payload["inputs"]["sealed_oos_g5"] == {
        "enabled": True,
        "family_id": "gate_calibration:<arm>:<seed>",
    }
    noise = payload["candidates"][0]["pipeline"][NOISE_ARM]["sealed_oos_g5"]
    assert set(noise["end_to_end_g0_g5"]) == {"false_positive_rate"}
    planted = payload["candidates"][0]["pipeline"][planted_arm_id(TOY_EFFECT)]["sealed_oos_g5"]
    assert set(planted["end_to_end_g0_g5"]) == {"power"}


def test_the_g5_report_is_deterministic_and_holds_no_recommendation() -> None:
    first = run_gate_calibration(_toy_g5_setup())
    second = run_gate_calibration(_toy_g5_setup())
    assert first.report_hash == second.report_hash
    assert first.to_payload() == second.to_payload()
    assert first.report_hash != run_gate_calibration(_toy_setup()).report_hash
    payload = first.to_payload()
    banned = ("recommend", "suggest", "default", "proposed", "best", "selected", "optimal")
    assert [k for k in _keys(payload) if any(word in k.lower() for word in banned)] == []
    assert not any(isinstance(leaf, float) for leaf in _leaves(payload))  # nothing unhashable


# --------------------------------------------------------------------------------------
# After the claim: consumed_without_result, never a pass
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("detector", "released", "error"),
    [
        (_RaisingAfterRelease(), True, "RuntimeError: toy G5 broke on the sealed window"),
        (_RaisingBeforeRelease(), False, "LookupError: toy G5 gave up"),
    ],
)
def test_a_raising_g5_is_inconclusive_consumed_without_result(
    ledger_log: list[tuple[str, str]],
    detector: _ToySealedDetector,
    released: bool,
    error: str,
) -> None:
    report = run_gate_calibration(_toy_g5_setup(detector))
    lax = report.candidate(TOY_LAX)
    reached = [run for run in lax.runs if run.g5 is not None]
    assert reached
    for run in reached:
        assert run.g5 is not None
        assert run.g5.verdict is Verdict.INCONCLUSIVE and run.g5.gates == ()
        assert run.g5.consumed_without_result and run.g5.detector_error == error
        assert run.g5.sealed_bars_released is released
    for arm in (NOISE_ARM, planted_arm_id(TOY_EFFECT)):
        g5 = lax.g5(arm)
        assert g5.end_to_end.count == 0 and (g5.passed is None or g5.passed.count == 0)
        assert g5.consumed_without_result == g5.detector_errors == g5.reached
    # the evaluation was consumed (claimed) before the detector ran, for every G0 - G4 pass
    passing = _passing_families(report, TOY_LAX, TOY_STRICT)
    assert _families(ledger_log, "evaluated") == passing
    payload: Any = json.loads(json.dumps(report.to_payload()))
    run = next(r for r in payload["candidates"][0]["runs"] if "sealed_oos_g5" in r)
    assert run["sealed_oos_g5"]["status"] == "consumed_without_result"
    assert run["sealed_oos_g5"]["detector_error"] == error


def test_an_early_exit_after_the_claim_is_inconclusive_consumed_without_result() -> None:
    report = run_gate_calibration(_toy_g5_setup(_EarlyExit()))
    lax = report.candidate(TOY_LAX)
    reached = [run for run in lax.runs if run.g5 is not None]
    assert reached
    for run in reached:
        assert run.g5 is not None
        assert run.g5.verdict is Verdict.INCONCLUSIVE and run.g5.consumed_without_result
        assert dict(run.g5.gates)["G5.oos_evaluation"] is Verdict.INCONCLUSIVE
        assert run.g5.detector_error is None
    assert all(lax.g5(arm).end_to_end.count == 0 for arm in (NOISE_ARM, planted_arm_id(TOY_EFFECT)))


def test_misconfigured_g5_detectors_raise() -> None:
    with pytest.raises(DetectorConfigurationError, match="claimed evaluation"):
        run_gate_calibration(_toy_g5_setup(_PassWithoutEvaluation()))
    with pytest.raises(DetectorConfigurationError, match="G5 gates"):
        run_gate_calibration(_toy_g5_setup(_NoG5Gates()))


class _MissingFieldInG5(_ToySealedDetector):
    """TEST ONLY: a G5 that needs a Profile field the TEST ONLY Profiles do not carry."""

    name = "toy_g5_missing_field"

    def detect_sealed(
        self, market: SyntheticMarket, profile: ValidationProfile, sealed: SealedRelease
    ) -> ValidationReport:
        profile_value(profile, "significance.negative_control_threshold")
        raise AssertionError("unreachable")  # pragma: no cover


def test_a_g5_configuration_error_raises_instead_of_being_recorded() -> None:
    with pytest.raises(ProfileFieldMissing, match="negative_control_threshold") as caught:
        run_gate_calibration(_toy_g5_setup(_MissingFieldInG5()))
    assert any(note.startswith("gate calibration G5: gate_calibration:")
               for note in caught.value.__notes__)  # fmt: skip


def test_a_g5_record_can_never_be_a_consumed_pass() -> None:
    fields: dict[str, Any] = {
        "family_id": "f", "gates": (), "gates_hash": "h", "sealed_bars_released": True,
    }  # fmt: skip
    with pytest.raises(ValueError, match="never a PASS"):
        gc.SealedRunRecord(verdict=Verdict.PASS, consumed_without_result=True, **fields)
    with pytest.raises(ValueError, match="detector error"):
        gc.SealedRunRecord(
            verdict=Verdict.FAIL, consumed_without_result=True, detector_error="E: x", **fields
        )


class _RaisingOnPlanted(_ToySealedDetector):
    """TEST ONLY: the toy G5, except that it fails at runtime on every planted family."""

    name = "toy_g5_raising_on_planted"

    def detect_sealed(
        self, market: SyntheticMarket, profile: ValidationProfile, sealed: SealedRelease
    ) -> ValidationReport:
        if sealed.family_id.startswith(f"gate_calibration:{planted_arm_id(TOY_EFFECT)}:"):
            raise ZeroDivisionError("toy G5 divided by zero")
        return super().detect_sealed(market, profile, sealed)


@pytest.mark.parametrize(
    "error", [ValueError("toy G5 refused"), TypeError("toy G5 input"), MemoryError()]
)
def test_every_propagated_error_of_a_g5_raises_instead_of_being_recorded(
    error: BaseException,
) -> None:
    # The same classification as ``calibration.calibrate`` (``PROPAGATED_ERRORS``).
    assert isinstance(error, PROPAGATED_ERRORS)

    class _Raising(_ToySealedDetector):
        name = "toy_g5_propagated_error"

        def detect_sealed(
            self, market: SyntheticMarket, profile: ValidationProfile, sealed: SealedRelease
        ) -> ValidationReport:
            raise error

    with pytest.raises(type(error)) as caught:
        run_gate_calibration(_toy_g5_setup(_Raising()))
    assert caught.value is error
    assert any(note.startswith("gate calibration G5: gate_calibration:")
               for note in caught.value.__notes__)  # fmt: skip


def test_g5_detector_errors_report_bounded_g5_pass_rates() -> None:
    report = run_gate_calibration(_toy_g5_setup(_RaisingOnPlanted()))
    planted = planted_arm_id(TOY_EFFECT)
    payload: Any = json.loads(json.dumps(report.to_payload()))
    seen_bounds = 0
    for profile, candidate_payload in zip(
        (TOY_LAX, TOY_STRICT), payload["candidates"], strict=True
    ):
        candidate = report.candidate(profile)
        for arm in (NOISE_ARM, planted):
            g5 = candidate.g5(arm)
            block = candidate_payload["pipeline"][arm]["sealed_oos_g5"]
            if arm == planted:  # every planted G5 raised: errors == reached
                assert g5.detector_errors == g5.reached
            else:  # the noise G5s ran normally: no errors
                assert g5.detector_errors == 0
            if g5.detector_errors:
                assert g5.passed is not None and g5.passed.count == 0
                assert g5.pass_rate_bounds == (Decimal(0), Decimal(1))  # 0 passed, all errored
                assert block["pass_rate_bounds"] == ["0.000000", "1.000000"]
                # end to end: no G0 - G4 errors here, so the errored runs are the G5 ones
                assert g5.end_to_end_errors == g5.detector_errors
                assert g5.end_to_end.count == 0
                n = g5.end_to_end.n
                low, high = g5.end_to_end_bounds
                assert low == 0 and high >= Decimal(g5.detector_errors) / n
                assert block["end_to_end_bounds"] == [str(low), str(high)]
                seen_bounds += 1
            else:
                assert "pass_rate_bounds" not in block
                assert g5.end_to_end_errors == 0 and "end_to_end_bounds" not in block
    assert seen_bounds  # the planted arm reached G5 under at least one candidate


def test_g5_reports_without_g5_detector_errors_have_no_g5_bounds() -> None:
    report = run_gate_calibration(_toy_g5_setup())
    for profile in (TOY_LAX, TOY_STRICT):
        for arm in (NOISE_ARM, planted_arm_id(TOY_EFFECT)):
            assert report.candidate(profile).g5(arm).detector_errors == 0
            assert report.candidate(profile).g5(arm).end_to_end_errors == 0
    assert "pass_rate_bounds" not in json.dumps(report.to_payload())
    assert "end_to_end_bounds" not in json.dumps(report.to_payload())


def test_g5_pass_rate_bounds_round_outward() -> None:
    alpha = fx.TEST_ONLY_ALPHA
    g5 = SealedArmEvidence(
        arm=NOISE_ARM,
        reached=7,
        passed=binomial_rate(3, 7, alpha),
        inconclusive=binomial_rate(2, 7, alpha),
        failed=binomial_rate(2, 7, alpha),
        consumed_without_result=2,
        detector_errors=2,
        end_to_end=binomial_rate(3, 9, alpha),
        end_to_end_errors=2,
    )
    # 3/7 = 0.4285714... (down), 5/7 = 0.7142857... (up); n is ``reached``, not every run
    assert g5.pass_rate_bounds == (Decimal("0.428571"), Decimal("0.714286"))
    assert g5.to_payload()["pass_rate_bounds"] == ["0.428571", "0.714286"]
    assert "pass_rate_bounds" not in replace(g5, detector_errors=0).to_payload()
    unreached = replace(g5, reached=0, passed=None, inconclusive=None, failed=None,
                        consumed_without_result=0, detector_errors=0)  # fmt: skip
    assert unreached.pass_rate_bounds is None
    assert "pass_rate_bounds" not in unreached.to_payload()
    with pytest.raises(ValueError, match="reached G5"):
        replace(g5, detector_errors=8)
    # end to end over every run (n = 9): 3 passed, 2 G5 + 1 G0 - G4 errored runs -> [3/9, 6/9]
    e2e = replace(g5, end_to_end_errors=3)
    assert e2e.end_to_end_bounds == (Decimal("0.333333"), Decimal("0.666667"))
    assert e2e.to_payload()["end_to_end_bounds"] == ["0.333333", "0.666667"]
    assert (
        "end_to_end_bounds" not in replace(g5, detector_errors=0, end_to_end_errors=0).to_payload()
    )
    with pytest.raises(ValueError, match="include the G5 errors"):
        replace(g5, end_to_end_errors=1)  # fewer than the 2 G5 errors
    with pytest.raises(ValueError, match="never an end-to-end pass"):
        replace(g5, end_to_end_errors=7)  # 3 passes + 7 errors > 9 runs


# --------------------------------------------------------------------------------------
# G5 mode with the full pipeline detector (research.validation.run_sealed_oos)
# --------------------------------------------------------------------------------------


def test_the_full_pipeline_runs_the_sealed_oos_gates_on_passing_runs(
    ledger_log: list[tuple[str, str]],
) -> None:
    setup = fx.setup(3, candidates=(fx.LAX_TEST_ONLY_PROFILE,), planted=(fx.STRONG,),
                     sealed_oos_g5=True)  # fmt: skip
    report = run_gate_calibration(setup)
    lax = report.candidate(fx.LAX_TEST_ONLY_PROFILE)
    passing = _passing_families(report, fx.LAX_TEST_ONLY_PROFILE)
    assert passing  # TEST ONLY lax candidate: the strong planted arm passes G0 - G4
    assert _families(ledger_log, "evaluated") == passing
    g5_gates = {g for g in lax.gate_ids() if g.startswith("G5.")}
    assert g5_gates == {
        "G5.unsealing_recorded",
        "G5.oos_effective_sample_size",
        "G5.oos_breakeven_cost_multiple",
    }
    for run in lax.runs:
        if run.verdict is Verdict.PASS:
            assert run.g5 is not None and run.g5.sealed_bars_released
            assert dict(run.g5.gates)["G5.unsealing_recorded"] is Verdict.PASS
        else:
            assert run.g5 is None
    # G5 mode leaves the G0 - G4 evidence unchanged (same markets, G5 off)
    off = run_gate_calibration(
        GateCalibrationSetup(
            **{**setup.__dict__, "detector": fx.detector(), "sealed_oos_g5": False}
        )
    )
    before = [(r.arm, r.seed, r.verdict, r.gates_hash) for r in off.candidates[0].runs]
    assert [(r.arm, r.seed, r.verdict, r.gates_hash) for r in lax.runs] == before


def test_the_full_pipeline_records_an_empty_sealed_grid_as_consumed_without_result() -> None:
    def no_decisions(research: Any, sealed_bars: Any) -> Any:
        return replace(fx.sealed_inputs(research, sealed_bars), decision_times=())

    base = fx.setup(1, candidates=(fx.LAX_TEST_ONLY_PROFILE,), planted=(fx.STRONG,),
                    sealed_oos_g5=True)  # fmt: skip
    detector = fx.detector(sealed_inputs_for=no_decisions)
    report = run_gate_calibration(GateCalibrationSetup(**{**base.__dict__, "detector": detector}))
    reached = [r for r in report.candidates[0].runs if r.g5 is not None]
    assert reached
    for run in reached:
        assert run.g5 is not None
        assert run.g5.verdict is Verdict.INCONCLUSIVE and run.g5.consumed_without_result
        assert run.g5.sealed_bars_released
        assert [g for g, v in run.g5.gates if v is Verdict.INCONCLUSIVE] == ["G5.oos_evaluation"]

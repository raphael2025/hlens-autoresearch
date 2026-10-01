"""Phase 9: the gate calibration harness's multi-instrument mode (evidence only, not a Profile
decision; implementation note 2026-09-26, CODE_COMPLETE / DEBUG_PENDING).

Multi-instrument mode is opt-in (``MultiInstrumentCalibrationSetup`` /
``run_multi_instrument_calibration``); every ``GateCalibrationSetup`` report keeps its hash. Each
run generates k independent instruments and validates them through the Phase 8 multi-instrument
path; the report gives per-arm (``all_noise`` / ``all_planted`` / ``mixed``, declared by the
caller) and per-gate pass / inconclusive / fail rates, the pooled G1 controls and their
per-instrument sub-gates included, and each instrument's own verdict rates.

Every Profile here is TEST ONLY and deliberately extreme (see ``gate_fixtures``); the toy
detectors are TEST ONLY too. Seed counts are smoke scale.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from core.contracts.synthetic import SyntheticMarket
from core.contracts.validation_profile import ValidationProfile
from core.domain.research import GateResult, ValidationReport, Verdict, derive_verdict
from research.strategies.validation import ValidatorSetup
from research.synthetic_lab.gate_calibration import (
    INSTRUMENT_SEED_RULE,
    DetectorConfigurationError,
    GateCalibrationReport,
    MultiInstrumentArm,
    MultiInstrumentCalibrationSetup,
    instrument_seed,
    main,
    planted_arm_id,
    run_gate_calibration,
    run_multi_instrument_calibration,
    write_gate_calibration,
)
from research.validation.gates import ProfileFieldMissing, profile_value
from research.validation.stats import UnsupportedMethod
from tests import factories
from tests.contract_version_support import PINNED_CONTRACT_VERSION, at_contract_version
from tests.research.synthetic_lab import gate_fixtures as fx
from tests.research.synthetic_lab.test_gate_calibration import (
    TOY_EFFECT,
    TOY_LAX,
    TOY_STRICT,
    _keys,
    _lag_autocorrelation_z,
    _leaves,
    _raising_setup,
    _toy_setup,
    _ToyDetector,
)
from tests.research.synthetic_lab.test_gate_calibration_g5 import (
    PRE_G5_PIPELINE_ONE_SEED_HASH,
    PRE_G5_RAISING_HASH,
    PRE_G5_TOY_HASH,
)

SEEDS = 2
PLANTED = planted_arm_id(fx.STRONG)
S0, S1 = fx.MULTI_SYMBOLS
CONTROLS = ("G1.shuffle_control", "G1.shift_control")


@pytest.fixture(scope="module")
def pipeline_report() -> GateCalibrationReport:
    return run_multi_instrument_calibration(fx.multi_setup(SEEDS))


# --------------------------------------------------------------------------------------
# Mode off: byte-identical
# --------------------------------------------------------------------------------------


def _mode_off_keeps_every_single_instrument_report_hash() -> None:
    assert run_gate_calibration(_toy_setup()).report_hash == PRE_G5_TOY_HASH
    assert run_gate_calibration(_raising_setup()).report_hash == PRE_G5_RAISING_HASH
    pipeline = run_gate_calibration(fx.setup(1))
    assert pipeline.report_hash == PRE_G5_PIPELINE_ONE_SEED_HASH
    payload: Any = pipeline.to_payload()
    assert "multi_instrument" not in payload["inputs"]
    for candidate in payload["candidates"]:
        for arm in candidate["pipeline"].values():
            assert not {"kind", "fail_rate", "instruments"} & set(arm)
        for by_arm in candidate["gates"].values():
            assert all("fail_rate" not in gate for gate in by_arm.values())
        assert all("instruments" not in run for run in candidate["runs"])


def test_mode_off_keeps_every_single_instrument_report_hash() -> None:
    # the pre-G5 hashes were recorded at contract 2.2.0: every object built at 2.2.0
    call = f"{__name__}:_mode_off_keeps_every_single_instrument_report_hash"
    assert at_contract_version(PINNED_CONTRACT_VERSION, call) is None


# --------------------------------------------------------------------------------------
# A toy multi-instrument detector (TEST ONLY): harness mechanics, independent of the pipeline
# --------------------------------------------------------------------------------------


def _toy_gate(gate_id: str, z: float, level: float) -> GateResult:
    p_value = math.erfc(abs(z) / math.sqrt(2))
    return GateResult(
        gate_id=gate_id,
        metric="lag60_p_value[<=]",
        value=p_value,
        threshold=level,
        threshold_source="significance.multiple_testing_threshold",
        verdict=Verdict.PASS if p_value <= level else Verdict.FAIL,
    )


class _ToyBookDetector:
    """TEST ONLY: pooled gate on the mean lag-60 z (scaled by sqrt(k)); per-instrument gates on
    each instrument's own z, evaluated only when the pooled gate did not fail (like Phase 8)."""

    name = "toy_book_lag60_autocorrelation"

    def describe(self) -> Mapping[str, str]:
        return {"kind": "test_only_toy_book"}

    def gates(
        self, markets: Mapping[str, SyntheticMarket], profile: ValidationProfile
    ) -> list[GateResult]:
        level = profile.significance.multiple_testing_threshold
        zs = {name: _lag_autocorrelation_z(market, 60) for name, market in markets.items()}
        pooled = _toy_gate("T0.pooled", sum(zs.values()) / math.sqrt(len(zs)), level)
        if pooled.verdict is Verdict.FAIL:
            return [pooled]
        return [pooled, *(_toy_gate(f"T0.own.instrument.{n}", z, level) for n, z in zs.items())]

    def detect_instruments(
        self, markets: Mapping[str, SyntheticMarket], profile: ValidationProfile
    ) -> ValidationReport:
        gates = self.gates(markets, profile)
        return factories.validation_report(
            derive_verdict(gates),
            gates=tuple(gates),
            validation_profile=profile.ref,
            validation_profile_hash=profile.content_hash(),
        )


def _toy_arms(seeds: int, k: int = 3) -> tuple[MultiInstrumentArm, ...]:
    return (
        MultiInstrumentArm("noise", "all_noise", (None,) * k, tuple(range(seeds))),
        MultiInstrumentArm(
            "planted", "all_planted", (TOY_EFFECT,) * k, tuple(range(50, 50 + seeds))
        ),
        MultiInstrumentArm(
            "one_planted", "mixed", (TOY_EFFECT, *(None,) * (k - 1)), tuple(range(90, 90 + seeds))
        ),
    )


TOY_SYMBOLS = ("A-USDT", "B-USDT", "C-USDT")


def _toy_multi(detector: object = None, **fields: object) -> MultiInstrumentCalibrationSetup:
    values: dict[str, object] = {
        "provider": _toy_setup().provider,
        "base": fx.BASE_SPEC,
        "detector": _ToyBookDetector() if detector is None else detector,
        "candidates": (TOY_LAX, TOY_STRICT),
        "symbols": TOY_SYMBOLS,
        "arms": _toy_arms(4),
        "alpha": fx.TEST_ONLY_ALPHA,
    }
    values.update(fields)
    return MultiInstrumentCalibrationSetup(**values)  # type: ignore[arg-type]


#: The toy multi-instrument report, pinned at its first computation (mode-on determinism across
#: processes; independent of the validation pipeline).
#: Re-pinned for contract 2.1.0 (ADR-0052 §4, 2026-09-26): the intended envelope change only —
#: every newly built contract object is 2.1.0 and the envelope is part of each content hash.
#: The previous values still hold when the same test builds every object at 2.0.0
#: (verified by running it inside ``contract_schema_version_scope("2.0.0")``).
#: 2.0.0 values (evidence, git history): 7b81912a…
#: Re-pinned for contract 2.2.0 (ADR-0055, 2026-09-26): envelope change only; the 2.1.0
#: values still hold when the test builds every object at 2.1.0 (verified: the unmodified
#: test passes inside ``contract_schema_version_scope("2.1.0")``).
#: 2.1.0 values (evidence, git history): f10b41aa…
#: Recorded at contract 2.2.0: checked with every contract object built at 2.2.0
#: (``at_contract_version``) after the envelope-only 2.3.0 – 2.5.0 minors.
TOY_MULTI_HASH = "bca62e54c1c8e16b5fefdb537414f9f54e2f4f40e400a2d4465c7cd727aa9bc3"


def _mode_on_is_deterministic_and_pinned() -> None:
    first = run_multi_instrument_calibration(_toy_multi())
    second = run_multi_instrument_calibration(_toy_multi())
    assert first.report_hash == second.report_hash == TOY_MULTI_HASH
    assert first.to_payload() == second.to_payload()
    other = run_multi_instrument_calibration(_toy_multi(arms=_toy_arms(3)))
    assert other.report_hash != first.report_hash
    body = {k: v for k, v in first.to_payload().items() if k != "report_hash"}
    assert json.loads(json.dumps(body)) == body


def test_mode_on_is_deterministic_and_pinned() -> None:
    call = f"{__name__}:_mode_on_is_deterministic_and_pinned"
    assert at_contract_version(PINNED_CONTRACT_VERSION, call) is None


def test_instrument_seeds_are_derived_from_the_run_seed() -> None:
    report = run_multi_instrument_calibration(_toy_multi())
    for candidate in report.candidates:
        for run in candidate.runs:
            assert run.instruments is not None
            seeds = [item.seed for item in run.instruments]
            assert seeds == [instrument_seed(run.seed, i, s) for i, s in enumerate(TOY_SYMBOLS)]
            assert len(set(seeds)) == len(seeds)
            assert len({item.market_hash for item in run.instruments}) == len(TOY_SYMBOLS)
    assert instrument_seed(0, 0, "A") == instrument_seed(0, 0, "A")
    assert len({instrument_seed(0, 0, "A"), instrument_seed(0, 1, "A"), instrument_seed(1, 0, "A"),
                instrument_seed(0, 0, "B")}) == 4  # fmt: skip
    inputs: Any = report.to_payload()["inputs"]
    assert inputs["multi_instrument"]["instrument_seed"] == INSTRUMENT_SEED_RULE
    assert inputs["multi_instrument"]["symbols"] == list(TOY_SYMBOLS)
    arms = {arm["arm"]: arm for arm in inputs["multi_instrument"]["arms"]}
    assert [i["role"] for i in arms["one_planted"]["instruments"]] == [
        planted_arm_id(TOY_EFFECT),
        "noise",
        "noise",
    ]
    assert arms["one_planted"]["instruments"][1]["effect"] is None


def test_the_toy_arms_are_reported_per_arm_gate_and_instrument() -> None:
    report = run_multi_instrument_calibration(_toy_multi())
    for candidate in report.candidates:
        assert [arm.arm for arm in candidate.arms] == ["noise", "planted", "one_planted"]
        for arm in candidate.arms:
            n = arm.passed.n
            assert n == 4 and arm.passed.count + arm.inconclusive.count + arm.failed == n
            assert arm.failed_rate is not None and arm.failed_rate.count == arm.failed
            assert arm.instruments is not None and len(arm.instruments) == 3
            runs = [run for run in candidate.runs if run.arm == arm.arm]
            for index, item in enumerate(arm.instruments):
                own = [run.instruments[index].verdict for run in runs]  # type: ignore[index]
                assert item.passed.count == own.count(Verdict.PASS)
                assert item.failed.count == own.count(Verdict.FAIL)
                assert item.not_evaluated == own.count(None)
                gate = candidate.gate(f"T0.own.instrument.{item.symbol}", arm.arm)
                assert gate.not_evaluated == item.not_evaluated
        mixed = candidate.arm("one_planted").instruments
        assert mixed is not None
        assert [item.role for item in mixed] == [planted_arm_id(TOY_EFFECT), "noise", "noise"]
    lax = report.candidate(TOY_LAX)
    strict = report.candidate(TOY_STRICT)
    assert lax.arm("planted").passed.rate == 1  # the toy lax level finds every planted book
    assert strict.arm("noise").passed.count == 0
    assert lax.instrument("one_planted", "A-USDT").passed.count >= 1


def test_pass_rates_are_named_by_arm_kind_and_instrument_role() -> None:
    payload: Any = run_multi_instrument_calibration(_toy_multi()).to_payload()
    lax = payload["candidates"][0]
    pipeline = lax["pipeline"]
    assert "false_positive_rate" in pipeline["noise"] and pipeline["noise"]["kind"] == "all_noise"
    assert "power" in pipeline["planted"] and pipeline["planted"]["kind"] == "all_planted"
    assert "pass_rate" in pipeline["one_planted"] and pipeline["one_planted"]["kind"] == "mixed"
    mixed = pipeline["one_planted"]["instruments"]
    assert "power" in mixed["A-USDT"] and "false_positive_rate" in mixed["B-USDT"]
    assert all("fail_rate" in item for item in mixed.values())
    gates = lax["gates"]
    assert "pass_rate" in gates["T0.pooled"]["one_planted"]
    assert "power" in gates["T0.own.instrument.A-USDT"]["one_planted"]
    assert "false_positive_rate" in gates["T0.own.instrument.C-USDT"]["one_planted"]
    assert "false_positive_rate" in gates["T0.own.instrument.A-USDT"]["noise"]
    assert all("fail_rate" in g for by_arm in gates.values() for g in by_arm.values())
    run = next(r for r in lax["runs"] if r["arm"] == "one_planted")
    assert [i["symbol"] for i in run["instruments"]] == list(TOY_SYMBOLS)


class _RaisingOnMixed(_ToyBookDetector):
    name = "toy_book_raising_on_mixed"

    def detect_instruments(
        self, markets: Mapping[str, SyntheticMarket], profile: ValidationProfile
    ) -> ValidationReport:
        truths = {bool(market.truth) for market in markets.values()}
        if truths == {True, False}:
            raise ZeroDivisionError("toy book broke")
        return super().detect_instruments(markets, profile)


def test_a_raising_detector_is_inconclusive_evidence() -> None:
    report = run_multi_instrument_calibration(_toy_multi(_RaisingOnMixed()))
    for candidate in report.candidates:
        mixed = candidate.arm("one_planted")
        assert mixed.inconclusive.count == 4 and mixed.detector_errors == 4
        assert mixed.passed.count == 0 and not mixed.sealed_oos_consumed.count
        for symbol in TOY_SYMBOLS:
            assert candidate.instrument("one_planted", symbol).not_evaluated == 4
        errored = [run for run in candidate.runs if run.arm == "one_planted"]
        assert {run.detector_error for run in errored} == {"ZeroDivisionError: toy book broke"}
        assert candidate.arm("noise").detector_errors == 0


def test_a_raising_book_reports_bounded_pass_rates() -> None:
    report = run_multi_instrument_calibration(_toy_multi(_RaisingOnMixed()))
    payload: Any = json.loads(json.dumps(report.to_payload()))
    for candidate in report.candidates:
        assert candidate.arm("one_planted").pass_rate_bounds == (Decimal(0), Decimal(1))
    for candidate in payload["candidates"]:
        assert candidate["pipeline"]["one_planted"]["pass_rate_bounds"] == ["0.000000", "1.000000"]
        assert "pass_rate_bounds" not in candidate["pipeline"]["noise"]


class _BookNeedsMissingField(_ToyBookDetector):
    """TEST ONLY: needs a Profile field the TEST ONLY candidates do not carry."""

    name = "toy_book_needs_missing_field"

    def detect_instruments(
        self, markets: Mapping[str, SyntheticMarket], profile: ValidationProfile
    ) -> ValidationReport:
        profile_value(profile, "significance.negative_control_threshold")
        return super().detect_instruments(markets, profile)


def test_a_profile_missing_a_field_the_validator_needs_raises() -> None:
    with pytest.raises(ProfileFieldMissing, match="negative_control_threshold") as caught:
        run_multi_instrument_calibration(_toy_multi(_BookNeedsMissingField()))
    assert caught.value.__notes__ == [f"gate calibration: noise/0 under {TOY_LAX.ref}"]


def test_the_multi_instrument_pipeline_refusing_an_unimplemented_method_raises() -> None:
    lax = fx.LAX_TEST_ONLY_PROFILE
    bogus = lax.model_copy(
        update={
            "name": "test_only_unimplemented_null_model",
            "benchmark": lax.benchmark.model_copy(update={"null_model": "test-only-unknown"}),
        }
    )
    with pytest.raises(UnsupportedMethod, match="test-only-unknown"):
        run_multi_instrument_calibration(fx.multi_setup(1, candidates=(bogus,)))


def test_harness_misconfiguration_still_raises() -> None:
    class _SingleInstrument(_ToyBookDetector):
        def detect_instruments(
            self, markets: Mapping[str, SyntheticMarket], profile: ValidationProfile
        ) -> ValidationReport:
            gate = _toy_gate("G0.single_instrument_adapter", 0.0, 1.0)
            return factories.validation_report(
                Verdict.PASS,
                gates=(gate,),
                validation_profile=profile.ref,
                validation_profile_hash=profile.content_hash(),
            )

    with pytest.raises(DetectorConfigurationError, match="single-instrument"):
        run_multi_instrument_calibration(_toy_multi(_SingleInstrument()))

    class _WrongProfile(_ToyBookDetector):
        def detect_instruments(
            self, markets: Mapping[str, SyntheticMarket], profile: ValidationProfile
        ) -> ValidationReport:
            other = TOY_STRICT if profile is TOY_LAX else TOY_LAX
            return super().detect_instruments(markets, other)

    with pytest.raises(ValueError, match="report is not under"):
        run_multi_instrument_calibration(_toy_multi(_WrongProfile()))


def test_the_pipeline_detector_refuses_a_single_instrument_setup() -> None:
    detector = fx.multi_detector()
    inner = detector._setup_for

    def single(markets, profile, runner) -> ValidatorSetup:  # type: ignore[no-untyped-def]
        setup = inner(markets, profile, runner)
        return ValidatorSetup(**{**setup.__dict__, "instruments": None})

    detector._setup_for = single
    setup = fx.multi_setup(1)
    setup = MultiInstrumentCalibrationSetup(**{**setup.__dict__, "detector": detector})
    with pytest.raises(DetectorConfigurationError, match="exactly the book's instruments"):
        run_multi_instrument_calibration(setup)


def test_the_pipeline_detector_refuses_a_setup_without_the_market_benchmark() -> None:
    """ADR-0060 is enforced in the lab: a setup that does not opt in is a configuration error."""
    detector = fx.multi_detector()
    inner = detector._setup_for

    def unenforced(markets, profile, runner) -> ValidatorSetup:  # type: ignore[no-untyped-def]
        setup = inner(markets, profile, runner)
        return ValidatorSetup(**{**setup.__dict__, "market_benchmark": False})

    detector._setup_for = unenforced
    setup = fx.multi_setup(1)
    setup = MultiInstrumentCalibrationSetup(**{**setup.__dict__, "detector": detector})
    with pytest.raises(DetectorConfigurationError, match="ADR-0060"):
        run_multi_instrument_calibration(setup)


def test_the_pooled_reports_carry_the_market_benchmark(
    pipeline_report: GateCalibrationReport,
) -> None:
    """ADR-0060 enforced: the TEST ONLY Profile's registered rule and inverse control are
    computed on the pooled scope (reported only), never the unregistered-rule gap."""
    for candidate in pipeline_report.candidates:
        ids = set(candidate.gate_ids())
        assert {"G2.market_benchmark.buy_and_hold_equal_weight", "G2.inverse_control"} <= ids
        assert "G2.market_benchmark" not in ids


# --------------------------------------------------------------------------------------
# Refusal of ambiguous setups
# --------------------------------------------------------------------------------------


def test_ambiguous_setups_are_refused() -> None:
    k2 = fx.multi_arms(1)
    with pytest.raises(ValueError, match="at least two symbols"):
        _toy_multi(symbols=("A-USDT",), arms=(MultiInstrumentArm("n", "all_noise", (None,), (0,)),))
    with pytest.raises(ValueError, match="at least two symbols"):
        _toy_multi(symbols=["A-USDT", "B-USDT"], arms=k2)
    with pytest.raises(ValueError, match="distinct"):
        _toy_multi(symbols=("A-USDT", "A-USDT"), arms=k2)
    with pytest.raises(ValueError, match="without"):
        _toy_multi(symbols=("A|B", "C"), arms=k2)
    with pytest.raises(ValueError, match="at least one declared arm"):
        _toy_multi(arms=())
    with pytest.raises(ValueError, match="effect entries"):
        _toy_multi(arms=k2)  # two entries for three symbols
    with pytest.raises(ValueError, match="arm names must be distinct"):
        _toy_multi(arms=(_toy_arms(1)[0], _toy_arms(1)[0]))
    with pytest.raises(ValueError, match="declared MultiInstrumentArm"):
        _toy_multi(arms=((None, None, None),))
    with pytest.raises(ValueError, match="detect_instruments"):
        _toy_multi(_ToyDetector())
    with pytest.raises(ValueError, match="pure noise"):
        _toy_multi(base=fx.BASE_SPEC.model_copy(update={"effects": (fx.WEAK,)}))
    with pytest.raises(ValueError, match="distinct"):
        _toy_multi(candidates=(TOY_LAX, TOY_LAX))


def test_arms_must_be_declared_explicitly() -> None:
    with pytest.raises(TypeError):
        MultiInstrumentArm("x", "mixed", (None, fx.STRONG))  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="declared kind 'mixed' but its effects are all_noise"):
        MultiInstrumentArm("x", "mixed", (None, None), (0,))
    with pytest.raises(ValueError, match="declared kind 'all_planted' but its effects are mixed"):
        MultiInstrumentArm("x", "all_planted", (fx.STRONG, None), (0,))
    with pytest.raises(ValueError, match="kind must be one of"):
        MultiInstrumentArm("x", "noise", (None, None), (0,))
    with pytest.raises(ValueError, match="must match"):
        MultiInstrumentArm("a:b", "all_noise", (None, None), (0,))
    with pytest.raises(ValueError, match="every instrument"):
        MultiInstrumentArm("x", "all_noise", (), (0,))
    with pytest.raises(ValueError, match="strength 0"):
        zero = fx.STRONG.model_copy(update={"strength": Decimal(0)})
        MultiInstrumentArm("x", "all_planted", (zero, zero), (0,))
    with pytest.raises(ValueError, match="at least one seed"):
        MultiInstrumentArm("x", "all_noise", (None, None), ())
    with pytest.raises(ValueError, match="distinct"):
        MultiInstrumentArm("x", "all_noise", (None, None), (1, 1))
    with pytest.raises(ValueError, match="non-negative"):
        MultiInstrumentArm("x", "all_noise", (None, None), (-1,))


# --------------------------------------------------------------------------------------
# The full multi-instrument pipeline detector (smoke scale)
# --------------------------------------------------------------------------------------


def test_every_arm_and_run_is_reported(pipeline_report: GateCalibrationReport) -> None:
    (candidate,) = pipeline_report.candidates
    assert [(arm.arm, arm.kind) for arm in candidate.arms] == [
        ("all_noise", "all_noise"),
        ("all_planted", "all_planted"),
        ("mixed", "mixed"),
    ]
    assert len(candidate.runs) == 3 * SEEDS
    for arm in candidate.arms:
        assert arm.passed.n == SEEDS
        assert arm.passed.count + arm.inconclusive.count + arm.failed == SEEDS
        assert arm.sealed_oos_consumed.count == arm.passed.count
    for gate in candidate.gates:
        evaluated = gate.passed.count + gate.inconclusive.count + gate.failed
        assert evaluated + gate.not_evaluated == SEEDS
        assert gate.failed_rate is not None and gate.failed_rate.count == gate.failed
    payload: Any = pipeline_report.to_payload()
    path = payload["inputs"]["detector"]["path"]
    assert path == "research.validation.instruments.run_multi_instrument_validation"


def test_the_pooled_controls_and_their_sub_gates_are_reported(
    pipeline_report: GateCalibrationReport,
) -> None:
    (candidate,) = pipeline_report.candidates
    ids = set(candidate.gate_ids())
    for control in CONTROLS:
        assert {control, f"{control}.instrument.{S0}", f"{control}.instrument.{S1}"} <= ids
        for arm in ("all_noise", "all_planted", "mixed"):
            pooled = candidate.gate(control, arm)
            assert pooled.not_evaluated == 0  # G1 always runs on the pooled table
    # A per-instrument control is evaluated exactly when the pooled stages did not fail.
    for run in candidate.runs:
        gates = dict(run.gates)
        pooled_failed = any(
            verdict is Verdict.FAIL
            for gate, verdict in run.gates
            if ".instrument." not in gate and gate.split(".")[0] in {"G0", "G1", "G2", "G3"}
        )
        assert (f"G1.shuffle_control.instrument.{S0}" in gates) is not pooled_failed


def test_the_mixed_arm_is_reported_per_instrument(
    pipeline_report: GateCalibrationReport,
) -> None:
    (candidate,) = pipeline_report.candidates
    planted = candidate.instrument("mixed", S0)
    noise = candidate.instrument("mixed", S1)
    assert (planted.role, noise.role) == (PLANTED, "noise")
    runs = [run for run in candidate.runs if run.arm == "mixed"]
    for index, item in enumerate((planted, noise)):
        own = [run.instruments[index].verdict for run in runs]  # type: ignore[index]
        assert (item.passed.count, item.inconclusive.count, item.failed.count) == (
            own.count(Verdict.PASS),
            own.count(Verdict.INCONCLUSIVE),
            own.count(Verdict.FAIL),
        )
        assert item.not_evaluated == own.count(None)
    for run in runs:
        assert run.instruments is not None
        assert [(i.symbol, i.role) for i in run.instruments] == [(S0, PLANTED), (S1, "noise")]
    # A mixed PASS needs the noise instrument's own gates to pass (Phase 8 rule).
    for run in runs:
        if run.verdict is Verdict.PASS:
            assert run.instruments is not None and run.instruments[1].verdict is Verdict.PASS


def test_the_report_has_no_recommended_value(pipeline_report: GateCalibrationReport) -> None:
    payload = pipeline_report.to_payload()
    banned = ("recommend", "suggest", "default", "proposed", "best", "selected", "optimal")
    assert [k for k in _keys(payload) if any(word in k.lower() for word in banned)] == []
    assert not any(isinstance(leaf, float) for leaf in _leaves(payload))


def test_the_cli_writes_a_multi_instrument_report(tmp_path: Path) -> None:
    code = main(
        [
            "--setup",
            "tests.research.synthetic_lab.gate_fixtures:multi_cli_setup",
            "--out",
            str(tmp_path),
        ]
    )
    assert code == 0
    (path,) = (tmp_path / "gate_calibration").iterdir()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert path.stem == loaded["report_hash"]
    assert loaded["inputs"]["multi_instrument"]["symbols"] == list(fx.MULTI_SYMBOLS)
    again = write_gate_calibration(tmp_path, fx.multi_cli_setup())
    assert not again.written and again.id == loaded["report_hash"]

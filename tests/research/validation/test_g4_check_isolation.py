"""G4 per-check exception isolation (Phase 8 debugging pass, 2026-09-26; ``g4`` module docs,
**Check isolation**).

An unexpected exception inside one G4 check becomes that check's single ``INCONCLUSIVE`` gate
``<prefix>.check_error``; every other check still runs and the G4 verdict is at best
``INCONCLUSIVE``. Deliberate refusals (``ValueError`` and its subclasses ``UnsupportedMethod`` /
``ProfileFieldMissing``, ``TypeError``) and ``MemoryError`` still raise. Without an exception the
output is byte-identical to the pre-isolation code (hashes pinned below).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from core.domain.research import GateResult, Verdict, derive_verdict
from research.validation import (
    RobustnessResult,
    build_report,
    g4,
    report_view,
    run_robustness,
    run_validation,
    to_json,
)
from research.validation.g4 import CHECK_ERROR, CHECKS, check_error
from research.validation.gates import ProfileFieldMissing, flag_gate
from research.validation.robustness import CheckStatus, RobustnessCheck
from research.validation.stats import UnsupportedMethod
from tests.research.validation import robustness_fixtures as rf
from tests.research.validation.fixtures import context
from tests.research.validation.test_robustness import _p4_input

#: sha256 of ``to_json(run_robustness(...).to_dict())`` for the two standard G4 fixtures, computed
#: with the pre-isolation ``g4.py`` (commit 1843ff5) on the development platform. Float-derived
#: (ADR-0037 §5: same-platform reproducible, D-FLOAT): a different platform may need a re-pin, a
#: change on the same platform means the no-exception output changed.
PINNED = {
    "momentum": "6290e21f9cd2ee7e46c476f3c86ea620dc0748b57c91e7413cc9769ebd4b02e0",
    "noise": "217028ecd767b95fde38036b612a64fe16c36ed70821be3c287ef260cb06dd8f",
}
#: The module-level name ``run_robustness`` calls for every check id.
FUNCTIONS = {
    "overfitting": "overfitting_check",
    "parameter_neighborhood": "parameter_neighborhood_check",
    "time_alignment": "time_alignment_check",
    "delay_stress": "delay_stress_check",
    "cost_stress": "cost_stress_check",
    "walk_forward": "walk_forward_check",
    "state_decomposition": "state_decomposition_check",
    "capacity": "capacity_check",
    "cross_asset": "cross_asset_check",
}


def _input(family: str):  # type: ignore[no-untyped-def]
    if family == "momentum":
        _, trials = rf.momentum_family(3, "0.3")
        space: dict[str, tuple[str | int | float | bool, ...]] = {"lookback": rf.LOOKBACKS}
    else:
        _, trials = rf.noise_family(5)
        space = {"variant": tuple(range(len(trials)))}
    return rf.robustness_input(trials, rf.best(trials).params, space)


def _digest(result: RobustnessResult) -> str:
    return hashlib.sha256(to_json(result.to_dict()).encode()).hexdigest()


def _raiser(error: BaseException):  # type: ignore[no-untyped-def]
    def check(*_args: object, **_kwargs: object) -> RobustnessCheck:
        raise error

    return check


def _passing(check_id: str, principles: tuple[str, ...], prefix: str):  # type: ignore[no-untyped-def]
    def check(*_args: object, **_kwargs: object) -> RobustnessCheck:
        gate = flag_gate(f"{prefix}.ok", "test_only_structural_pass", True, 1.0)
        return RobustnessCheck(check_id=check_id, principles=principles, gates=(gate,))

    return check


# --------------------------------------------------------------------------------------
# no exception: identical output
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("family", ["momentum", "noise"])
def test_without_an_exception_the_output_is_byte_identical(family: str) -> None:
    result = run_robustness(_input(family))
    assert _digest(result) == PINNED[family]
    assert all(CHECK_ERROR not in gate.gate_id for gate in result.gates)


def test_the_checks_table_matches_the_checks_themselves() -> None:
    result = run_robustness(_input("momentum"))
    assert [(c.check_id, c.principles) for c in result.checks] == [
        (check_id, principles) for check_id, principles, _ in CHECKS
    ]
    for check, (_, _, prefix) in zip(result.checks, CHECKS, strict=True):
        assert check.gates
        assert all(gate.gate_id.startswith(f"{prefix}") for gate in check.gates)
    assert set(FUNCTIONS) == {check_id for check_id, _, _ in CHECKS}


# --------------------------------------------------------------------------------------
# an unexpected exception: that check INCONCLUSIVE, the suite continues
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("family", ["momentum", "noise"])
def test_a_raising_check_becomes_its_inconclusive_check_error_gate(
    family: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = run_robustness(_input(family))
    monkeypatch.setattr(
        g4, "walk_forward_check", _raiser(ZeroDivisionError("float division at 0x7f3a00dead"))
    )
    result = run_robustness(_input(family))
    assert [c.check_id for c in result.checks] == [c.check_id for c in baseline.checks]
    broken = next(c for c in result.checks if c.check_id == "walk_forward")
    assert broken.principles == ("C-S4", "C-R3")
    assert len(broken.gates) == 1
    gate = broken.gates[0]
    assert gate.gate_id == "G4.walk_forward.check_error"
    assert gate.metric == "check_error:ZeroDivisionError"
    assert gate.verdict is Verdict.INCONCLUSIVE
    assert gate.threshold is None
    assert broken.status is CheckStatus.INCONCLUSIVE
    assert broken.details == {
        "check_error": {"type": "ZeroDivisionError", "message": "float division at 0x?"}
    }
    # every other check ran and is unchanged
    for before, after in zip(baseline.checks, result.checks, strict=True):
        if after.check_id != "walk_forward":
            assert after.to_dict() == before.to_dict()
    # never a PASS; a FAIL elsewhere still fails
    expected = Verdict.FAIL if derive_verdict(baseline.gates) is Verdict.FAIL else None
    verdict = derive_verdict(result.gates)
    assert verdict is not Verdict.PASS
    if expected is not None:
        assert verdict is Verdict.FAIL
    json.loads(to_json(result.to_dict()))  # still canonical JSON


def test_an_otherwise_passing_suite_is_at_best_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for check_id, principles, prefix in CHECKS:
        monkeypatch.setattr(g4, FUNCTIONS[check_id], _passing(check_id, principles, prefix))
    assert derive_verdict(run_robustness(_input("momentum")).gates) is Verdict.PASS
    monkeypatch.setattr(g4, "capacity_check", _raiser(KeyError("fill")))
    result = run_robustness(_input("momentum"))
    assert derive_verdict(result.gates) is Verdict.INCONCLUSIVE
    ids = [gate.gate_id for gate in result.gates]
    assert "G4.capacity.check_error" in ids and "G4.capacity.ok" not in ids
    assert "G4.cross_asset.ok" in ids  # the check after the broken one still ran


def test_several_raising_checks_are_each_isolated_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(g4, "overfitting_check", _raiser(RuntimeError("a")))
    monkeypatch.setattr(g4, "cross_asset_check", _raiser(AttributeError("b")))
    result = run_robustness(_input("momentum"))
    assert [c.check_id for c in result.checks] == [check_id for check_id, _, _ in CHECKS]
    assert result.checks[0].gates[0].gate_id == "G4.overfitting.check_error"
    assert result.checks[0].gates[0].metric == "check_error:RuntimeError"
    assert result.checks[-1].gates[0].gate_id == "G4.cross_asset.check_error"
    assert result.checks[-1].gates[0].metric == "check_error:AttributeError"
    assert all(len(c.gates) >= 1 for c in result.checks[1:-1])
    assert all(CHECK_ERROR not in g.gate_id for c in result.checks[1:-1] for g in c.gates)


def test_the_error_message_is_one_short_deterministic_line() -> None:
    long = RuntimeError("line one\n  line two <obj at 0xABCDEF12> " + "x" * 500)
    first = check_error("delay_stress", long).details["check_error"]
    again = check_error("delay_stress", RuntimeError(str(long))).details["check_error"]
    assert first == again
    message = first["message"]  # type: ignore[index]
    assert "\n" not in message and "0xABCDEF12" not in message
    assert message.startswith("line one line two <obj at 0x?> x")
    assert len(message) == 200


def test_check_error_refuses_an_unknown_check() -> None:
    with pytest.raises(ValueError, match="unknown G4 check"):
        check_error("not_a_check", RuntimeError("x"))


def test_a_check_error_flows_through_run_validation_and_the_report_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(g4, "state_decomposition_check", _raiser(IndexError("state 3")))
    _, trials = rf.momentum_family(3, "0.3")
    source = rf.robustness_input(trials, rf.best(trials).params, {"lookback": rf.LOOKBACKS})
    run = run_validation(_p4_input(), source)
    gate = next(g for g in run.gates if g.gate_id == "G4.state.check_error")
    assert gate.verdict is Verdict.INCONCLUSIVE
    report = build_report(context(profile=rf.G4_TEST_ONLY_PROFILE), run.gates)
    assert report.verdict is not Verdict.PASS
    view = json.loads(to_json(report_view(report, run.robustness)))
    checks = {c["check_id"]: c for c in view["robustness"]["checks"]}
    assert checks["state_decomposition"]["status"] == "INCONCLUSIVE"
    assert checks["state_decomposition"]["details"]["check_error"]["type"] == "IndexError"


# --------------------------------------------------------------------------------------
# deliberate refusals still raise
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ValueError("every trial of the family must share one period grid"),
        UnsupportedMethod("significance.overfitting_metric 'x' is not implemented"),
        ProfileFieldMissing("the Profile has no field 'x'"),
        TypeError("must be a Decimal"),
        MemoryError(),
    ],
    ids=lambda error: type(error).__name__,
)
def test_deliberate_refusals_still_raise(
    error: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(g4, "time_alignment_check", _raiser(error))
    with pytest.raises(type(error)):
        run_robustness(_input("momentum"))


def test_a_base_exception_is_never_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(g4, "cost_stress_check", _raiser(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run_robustness(_input("momentum"))


def test_a_check_error_gate_is_never_a_pass() -> None:
    check = check_error("capacity", ArithmeticError("x"))
    gates: tuple[GateResult, ...] = check.gates
    assert [g.verdict for g in gates] == [Verdict.INCONCLUSIVE]
    assert derive_verdict(RobustnessResult(checks=(check,), params=None).gates) is (
        Verdict.INCONCLUSIVE
    )

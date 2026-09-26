"""ADR-0059 (Accepted 2026-09-26): C-R3 for cross-sectional strategies, at the check level.

C: when every declared instrument's single-asset re-run is known to hold no position,
``G4.cross_asset.positive_fraction`` is ``INCONCLUSIVE`` (never computed, never a PASS). A: a
strategy declared cross-sectional is judged over the disjoint sub-universes of
``subuniverse_partition`` with the same threshold. Without the new keyword arguments the check is
unchanged. !!! TEST ONLY !!! thresholds: ``G4_TEST_ONLY_PROFILE`` and explicit TEST ONLY fractions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from core.domain.research import GateResult, Verdict
from research.validation.gates import explicit_threshold
from research.validation.returns import PeriodReturns
from research.validation.robustness import (
    NOT_ENOUGH_FOR_SUBUNIVERSES,
    SUBUNIVERSE_RULE,
    ZERO_EXPOSURE_SINGLE_ASSET,
    CheckStatus,
    RobustnessCheck,
    SubUniverse,
    cross_asset_check,
    subuniverse_partition,
)
from tests.research.validation import robustness_fixtures as rf

PROFILE = rf.G4_TEST_ONLY_PROFILE
T0 = datetime(2026, 1, 1, tzinfo=UTC)
#: TEST ONLY explicit fractions (``param:cross_asset.min_positive_fraction``).
HALF = explicit_threshold("cross_asset.min_positive_fraction", 0.5)
ZERO = explicit_threshold("cross_asset.min_positive_fraction", 0.0)
FRACTION = "G4.cross_asset.positive_fraction"


def _returns(*gross: str) -> PeriodReturns:
    return PeriodReturns(
        times=tuple(T0 + timedelta(minutes=i + 1) for i in range(len(gross))),
        gross=tuple(Decimal(g) for g in gross),
        cost=tuple(Decimal(0) for _ in gross),
    )


FLAT = _returns("0", "0", "0")
UP = _returns("0.01", "0.00", "0.01")
DOWN = _returns("-0.01", "0.00", "-0.01")


def _details(check: RobustnessCheck, key: str) -> Any:
    return check.to_dict()["details"][key]  # type: ignore[index]


def _gate(check_gates: tuple[GateResult, ...], gate_id: str = FRACTION) -> GateResult:
    return next(g for g in check_gates if g.gate_id == gate_id)


# --------------------------------------------------------------------------------------
# partition rule
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ((), ()),
        (("A",), ()),
        (("B", "A"), ()),
        (("C", "A", "B"), ()),
        (("D", "C", "B", "A"), (("A", "B"), ("C", "D"))),
        (("E", "D", "C", "B", "A"), (("A", "B"), ("C", "D", "E"))),
        (("A", "B", "C", "D", "E", "F"), (("A", "B"), ("C", "D"), ("E", "F"))),
        (("A", "A", "B", "C", "D"), (("A", "B"), ("C", "D"))),
    ],
)
def test_the_partition_is_deterministic_disjoint_and_covering(
    declared: tuple[str, ...], expected: tuple[tuple[str, ...], ...]
) -> None:
    parts = subuniverse_partition(declared)
    assert parts == expected
    assert subuniverse_partition(tuple(reversed(declared))) == parts  # order-free
    if parts:
        flat = [name for part in parts for name in part]
        assert sorted(flat) == sorted(set(declared)) and len(flat) == len(set(flat))
        assert all(len(part) >= 2 for part in parts)


# --------------------------------------------------------------------------------------
# C: zero-exposure single-asset re-runs
# --------------------------------------------------------------------------------------


def test_every_flat_single_asset_run_is_inconclusive_not_fail() -> None:
    before = cross_asset_check(PROFILE, {"A": FLAT, "B": FLAT}, ("A", "B"), HALF)
    assert _gate(before.gates).verdict is Verdict.FAIL
    check = cross_asset_check(
        PROFILE, {"A": FLAT, "B": FLAT}, ("A", "B"), HALF, exposed={"A": False, "B": False}
    )
    gate = _gate(check.gates)
    assert (gate.verdict, gate.metric, gate.value) == (
        Verdict.INCONCLUSIVE,
        ZERO_EXPOSURE_SINGLE_ASSET,
        2.0,
    )
    assert check.status is CheckStatus.INCONCLUSIVE
    assert check.thresholds == ()  # the fraction was not computed
    assert _details(check, "zero_exposure")["instruments"] == ["A", "B"]


def test_zero_exposure_is_never_a_pass_even_with_a_zero_threshold() -> None:
    check = cross_asset_check(
        PROFILE, {"A": FLAT, "B": FLAT}, ("A", "B"), ZERO, exposed={"A": False, "B": False}
    )
    assert _gate(check.gates).verdict is Verdict.INCONCLUSIVE


def test_exposure_not_returns_decides() -> None:
    """Zero returns with a position held are judged (FAIL here); unknown exposure is not zero."""
    held = cross_asset_check(
        PROFILE, {"A": FLAT, "B": FLAT}, ("A", "B"), HALF, exposed={"A": True, "B": True}
    )
    partial = cross_asset_check(
        PROFILE, {"A": FLAT, "B": FLAT}, ("A", "B"), HALF, exposed={"A": False, "B": True}
    )
    unknown = cross_asset_check(
        PROFILE, {"A": FLAT, "B": FLAT}, ("A", "B"), HALF, exposed={"A": False}
    )
    for check in (held, partial, unknown):
        gate = _gate(check.gates)
        assert (gate.verdict, gate.metric) == (
            Verdict.FAIL,
            "positive_instrument_fraction[>=]",
        )


def test_the_missing_threshold_and_untested_instruments_still_come_first() -> None:
    flat = {"A": False, "B": False}
    missing = cross_asset_check(PROFILE, {"A": FLAT, "B": FLAT}, ("A", "B"), None, exposed=flat)
    assert missing.missing_fields == ("cross_asset.min_positive_fraction",)
    untested = cross_asset_check(PROFILE, {"A": FLAT}, ("A", "B"), HALF, exposed=flat)
    assert _gate(untested.gates).metric == "declared_instruments_not_tested"


def test_without_the_new_arguments_nothing_changes() -> None:
    per_asset = {"A": UP, "B": DOWN}
    old = cross_asset_check(PROFILE, per_asset, ("A", "B"), HALF)
    new = cross_asset_check(PROFILE, per_asset, ("A", "B"), HALF, exposed={"A": True, "B": True})
    assert old.to_dict() == new.to_dict()
    assert set(old.details) == {"declared", "instruments", "not_tested"}
    assert old.note == ""


# --------------------------------------------------------------------------------------
# A: sub-universes of a declared cross-sectional strategy
# --------------------------------------------------------------------------------------

FOUR = ("A", "B", "C", "D")
FLAT_ASSETS = dict.fromkeys(FOUR, FLAT)


def _subs(first: PeriodReturns, second: PeriodReturns) -> tuple[SubUniverse, ...]:
    return (
        SubUniverse(("A", "B"), first, exposed=True),
        SubUniverse(("C", "D"), second, exposed=True),
    )


def test_sub_universes_are_judged_with_the_same_threshold() -> None:
    both = cross_asset_check(PROFILE, FLAT_ASSETS, FOUR, HALF, sub_universes=_subs(UP, UP))
    gate = _gate(both.gates)
    assert (gate.verdict, gate.metric, gate.value) == (
        Verdict.PASS,
        "positive_subuniverse_fraction[>=]",
        1.0,
    )
    assert gate.threshold_source == "param:cross_asset.min_positive_fraction"
    section = _details(both, "cross_section")
    assert section["rule"] == SUBUNIVERSE_RULE
    assert [s["instruments"] for s in section["sub_universes"]] == [["A", "B"], ["C", "D"]]
    # The flat per-asset rows are reported, never judged, for a declared cross-sectional strategy.
    assert [row["instrument"] for row in _details(both, "instruments")] == list(FOUR)
    none = cross_asset_check(PROFILE, FLAT_ASSETS, FOUR, HALF, sub_universes=_subs(DOWN, DOWN))
    assert _gate(none.gates).verdict is Verdict.FAIL
    assert none.status is CheckStatus.FAIL
    flat = cross_asset_check(PROFILE, FLAT_ASSETS, FOUR, HALF, sub_universes=_subs(FLAT, FLAT))
    assert _gate(flat.gates).verdict is Verdict.FAIL


def test_fewer_than_two_sub_universes_is_inconclusive() -> None:
    three = ("A", "B", "C")
    check = cross_asset_check(PROFILE, dict.fromkeys(three, UP), three, HALF, sub_universes=())
    gate = _gate(check.gates)
    assert (gate.verdict, gate.metric, gate.value) == (
        Verdict.INCONCLUSIVE,
        NOT_ENOUGH_FOR_SUBUNIVERSES,
        3.0,
    )
    # One declared instrument: a cross-section cannot even exist, and it is never a PASS.
    lone = cross_asset_check(PROFILE, {"A": UP}, ("A",), HALF, sub_universes=())
    assert lone.status is CheckStatus.INCONCLUSIVE


def test_sub_universes_keep_the_missing_threshold_and_scope_rules() -> None:
    missing = cross_asset_check(PROFILE, FLAT_ASSETS, FOUR, None, sub_universes=_subs(UP, UP))
    assert missing.status is CheckStatus.PROFILE_FIELD_MISSING
    partial = {name: FLAT for name in FOUR[:3]}
    untested = cross_asset_check(PROFILE, partial, FOUR, HALF, sub_universes=_subs(UP, UP))
    assert _gate(untested.gates).metric == "declared_instruments_not_tested"


def test_sub_universes_must_follow_the_recorded_rule() -> None:
    wrong = (
        SubUniverse(("A", "C"), UP, exposed=True),
        SubUniverse(("B", "D"), UP, exposed=True),
    )
    with pytest.raises(ValueError, match=SUBUNIVERSE_RULE):
        cross_asset_check(PROFILE, FLAT_ASSETS, FOUR, HALF, sub_universes=wrong)
    with pytest.raises(ValueError, match=SUBUNIVERSE_RULE):
        cross_asset_check(PROFILE, FLAT_ASSETS, FOUR, HALF, sub_universes=())

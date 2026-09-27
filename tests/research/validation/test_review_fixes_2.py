"""Regression tests of the second read-only review of the validation code (ADR-0041 implementation
note "review fixes 2", 2026-09-25; backlog rows R21 - R23).

- R21: a sealed OOS evaluation is consumed atomically when it is claimed, before any sealed sample
  leaves the vault; an evaluation that ends without a statistic is an INCONCLUSIVE
  ``consumed_without_result`` report and nothing can read the window again;
- R22: the CSCV purge is at least the label / holding horizon wide, consistent with
  ``splits.purge_and_embargo``; a purge that is too wide stays INCONCLUSIVE;
- R23: walk-forward windows without returns are counted and make the positive fraction
  INCONCLUSIVE (never dropped from the denominator).

TEST ONLY: all Profile numbers come from the TEST ONLY fixtures (arbitrary, uncalibrated).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.contracts.outcome import OutcomeEvent
from core.domain.research import Verdict
from research.validation import (
    SealedOosInput,
    build_report,
    run_sealed_oos,
    sealed_oos_without_result,
)
from research.validation.calibration import MomentumSignStudy
from research.validation.overfitting import (
    CscvPurgeTooWide,
    probability_of_backtest_overfitting,
)
from research.validation.robustness import overfitting_check, walk_forward_check
from research.validation.sealed_oos import (
    InMemoryUnsealingLedger,
    OosAlreadyUnsealed,
    SealedOosAlreadyEvaluated,
    SealedOosLocked,
    SealedOosVault,
)
from research.validation.splits import LabeledSpan, purge_and_embargo
from tests.research.validation import robustness_fixtures as rf
from tests.research.validation.fixtures import (
    BOUNDARY,
    TEST_ONLY_PROFILE,
    context,
    generate,
    outcome_table,
    sealed_events,
)

T0 = datetime(2024, 1, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
PROFILE = rf.G4_TEST_ONLY_PROFILE


def _vault() -> tuple[SealedOosVault, InMemoryUnsealingLedger, str]:
    ledger = InMemoryUnsealingLedger()
    vault = SealedOosVault(TEST_ONLY_PROFILE, ledger, max_unsealings=2)
    family = context().metadata.hypothesis_family_id
    vault.unseal(family, "test-human", BOUNDARY + timedelta(days=2))
    return vault, ledger, family


# --------------------------------------------------------------------------------------
# R21 — one-shot sealed evaluation consumed at claim time
# --------------------------------------------------------------------------------------


def test_r21_a_claim_consumes_the_evaluation_before_any_sample_leaves() -> None:
    ledger = InMemoryUnsealingLedger()
    vault = SealedOosVault(TEST_ONLY_PROFILE, ledger, max_unsealings=1)
    with pytest.raises(SealedOosLocked):
        vault.claim_evaluation("fam")  # no unsealing, no claim
    vault.unseal("fam", "test-human", BOUNDARY)
    assert not ledger.is_evaluated("fam")
    claim = vault.claim_evaluation("fam")
    # consumed now, before the claimant took anything
    assert ledger.is_evaluated("fam") and vault.is_evaluated("fam")
    assert not claim.taken("bars") and not claim.taken("labels")
    with pytest.raises(SealedOosAlreadyEvaluated):
        vault.claim_evaluation("fam")
    with pytest.raises(SealedOosAlreadyEvaluated):
        vault.sealed_view("fam", [])
    with pytest.raises(OosAlreadyUnsealed):
        vault.unseal("fam", "test-human", BOUNDARY)
    # the claim hands each part out once
    claim.take("bars")
    with pytest.raises(SealedOosAlreadyEvaluated):
        claim.take("bars")
    inside = LabeledSpan("in", vault.window.start, vault.window.start + MINUTE)
    outside = LabeledSpan("out", T0, T0 + MINUTE)
    assert claim.view([inside, outside]) == (inside,)
    with pytest.raises(SealedOosAlreadyEvaluated):
        claim.view([inside])
    with pytest.raises(ValueError, match="unknown"):
        claim.take("everything")


def test_r21_a_consumed_evaluation_without_result_is_inconclusive_and_final() -> None:
    vault, ledger, family = _vault()
    ctx = context()
    claim = vault.claim_evaluation(family)
    gates = sealed_oos_without_result(ctx, vault, claim, "no_sealed_decision_time")
    assert [(g.gate_id, g.verdict) for g in gates] == [
        ("G5.unsealing_recorded", Verdict.PASS),
        ("G5.oos_evaluation", Verdict.INCONCLUSIVE),
    ]
    assert gates[1].metric == "consumed_without_result:no_sealed_decision_time"
    assert build_report(ctx, gates).verdict is Verdict.INCONCLUSIVE  # never a PASS
    assert ledger.is_evaluated(family)
    with pytest.raises(SealedOosAlreadyEvaluated):
        vault.sealed_view(family, [])
    with pytest.raises(ValueError, match="reason"):
        sealed_oos_without_result(ctx, vault, claim, " ")


def test_r21_a_claim_is_bound_to_its_family_and_vault() -> None:
    vault, _, family = _vault()
    vault.unseal("other_family", "test-human", BOUNDARY + timedelta(days=2))
    foreign = vault.claim_evaluation("other_family")
    with pytest.raises(ValueError, match="belongs to"):
        sealed_oos_without_result(context(), vault, foreign, "x")
    other_vault = SealedOosVault(TEST_ONLY_PROFILE, InMemoryUnsealingLedger(), max_unsealings=1)
    other_vault.unseal(family, "test-human", BOUNDARY)
    stray = other_vault.claim_evaluation(family)
    with pytest.raises(ValueError, match="not claimed from this vault"):
        sealed_oos_without_result(context(), vault, stray, "x")  # vault: family not evaluated


def test_r21_g5_runs_on_the_claimed_evaluation_and_reads_the_labels_once() -> None:
    market = generate(seed=3, strength="0.6")
    oos_table = outcome_table(market, sealed_events(market))
    study = MomentumSignStudy(
        market, [OutcomeEvent(event_key=x.event_key, event_time=x.event_time) for x in oos_table]
    )
    vault, _, family = _vault()
    ctx = context()
    claim = vault.claim_evaluation(family)
    claim.take("bars")  # the caller re-ran its strategy on the sealed bars first
    gates = run_sealed_oos(SealedOosInput(ctx, vault, oos_table, study, claim))
    assert {g.gate_id for g in gates} == {
        "G5.unsealing_recorded",
        "G5.oos_effective_sample_size",
        "G5.oos_breakeven_cost_multiple",
    }
    assert claim.taken("labels")
    with pytest.raises(SealedOosAlreadyEvaluated):
        run_sealed_oos(SealedOosInput(ctx, vault, oos_table, study, claim))
    with pytest.raises(SealedOosAlreadyEvaluated):
        run_sealed_oos(SealedOosInput(ctx, vault, oos_table, study))


# --------------------------------------------------------------------------------------
# R22 — CSCV purge at least the label / holding horizon wide
# --------------------------------------------------------------------------------------


def _grid(periods: int = 20) -> tuple[list[list[float]], list[datetime]]:
    trial0 = [0.001 * ((i % 3) - 1) for i in range(periods)]
    trial1 = [0.0005 + 0.001 * ((i % 2) * 2 - 1) for i in range(periods)]
    return [trial0, trial1], [T0 + i * MINUTE for i in range(periods)]


def _expected_purge(times: list[datetime], horizon: timedelta, embargo: timedelta) -> int:
    """Most in-sample periods ``splits.purge_and_embargo`` drops over both 2-block splits."""
    spans = [LabeledSpan(f"p{i:02d}", t, t + horizon) for i, t in enumerate(times)]
    half = len(spans) // 2
    dropped = []
    for train, test in ((spans[:half], spans[half:]), (spans[half:], spans[:half])):
        _, purged, embargoed = purge_and_embargo(train, test, embargo)
        dropped.append(len(purged) + len(embargoed))
    return max(dropped)


def test_r22_the_purge_covers_a_horizon_longer_than_the_embargo() -> None:
    matrix, times = _grid()
    embargo = 2 * MINUTE
    plain = probability_of_backtest_overfitting(
        matrix, 2, times=times, embargo=embargo, horizon=timedelta(0)
    )
    wide = probability_of_backtest_overfitting(
        matrix, 2, times=times, embargo=embargo, horizon=5 * MINUTE
    )
    assert plain.purged_in_sample_periods_max == 1  # strictly within 2 minutes
    assert wide.horizon == 5 * MINUTE and wide.embargo == embargo
    # after the out-of-sample block: 5 minutes of overlapping spans, then 2 of embargo
    assert wide.purged_in_sample_periods_max == 5 + 1
    assert wide.purged_in_sample_periods_max > plain.purged_in_sample_periods_max


@pytest.mark.parametrize(
    ("horizon", "embargo"),
    [(3 * MINUTE, 0 * MINUTE), (4 * MINUTE, 2 * MINUTE), (6 * MINUTE, 3 * MINUTE)],
)
def test_r22_the_purge_matches_splits_purge_and_embargo(
    horizon: timedelta, embargo: timedelta
) -> None:
    # with embargo <= horizon the "embargo before a block" of R17 lies inside the purge
    matrix, times = _grid()
    pbo = probability_of_backtest_overfitting(
        matrix, 2, times=times, embargo=embargo, horizon=horizon
    )
    assert pbo.purged_in_sample_periods_max == _expected_purge(times, horizon, embargo)


def test_r22_horizon_is_required_and_non_negative() -> None:
    matrix, times = _grid()
    with pytest.raises(ValueError, match="horizon"):
        probability_of_backtest_overfitting(
            matrix, 2, times=times, embargo=timedelta(0), horizon=-MINUTE
        )
    with pytest.raises(TypeError):
        probability_of_backtest_overfitting(matrix, 2, times=times, embargo=timedelta(0))  # type: ignore[call-arg]


def test_r22_a_horizon_that_purges_too_much_is_inconclusive() -> None:
    matrix, times = _grid()
    with pytest.raises(CscvPurgeTooWide):
        probability_of_backtest_overfitting(
            matrix, 2, times=times, embargo=timedelta(0), horizon=timedelta(hours=1)
        )
    _, trials = rf.momentum_family(3, "0.3")
    chosen = rf.best(trials)
    ok = overfitting_check(PROFILE, trials, chosen.params, len(trials), 10, horizon=MINUTE)
    pbo = ok.details["pbo"]
    assert isinstance(pbo, dict) and pbo["purge_horizon_seconds"] == 60.0
    (refused,) = overfitting_check(
        PROFILE, trials, chosen.params, len(trials), 10, horizon=timedelta(days=1)
    ).gates
    assert refused.verdict is Verdict.INCONCLUSIVE
    assert refused.metric == "pbo_not_computed:purge_leaves_too_few_in_sample_periods"


# --------------------------------------------------------------------------------------
# R23 — walk-forward windows without returns are counted (INCONCLUSIVE), never dropped
# --------------------------------------------------------------------------------------


def test_r23_empty_walk_forward_windows_make_the_fraction_inconclusive() -> None:
    _, trials = rf.momentum_family(3, "0.3")
    full = trials[0].returns
    covered = walk_forward_check(PROFILE, full)
    assert covered.details["windows_without_returns"] == 0
    (fraction, _) = covered.gates
    assert fraction.metric.startswith("positive_window_fraction")
    assert fraction.verdict is not Verdict.INCONCLUSIVE
    # the same returns without the afternoon: the later Profile windows have no return at all
    half = full.window(T0, T0 + timedelta(hours=14))
    partial = walk_forward_check(PROFILE, half)
    rows = partial.details["windows"]
    assert isinstance(rows, list) and len(rows) == len(covered.details["windows"])  # type: ignore[arg-type]
    empty = [row for row in rows if row["periods"] == 0]
    assert empty and partial.details["windows_without_returns"] == len(empty)
    fraction_gate, share_gate = partial.gates
    assert fraction_gate.gate_id == "G4.walk_forward.positive_fraction"
    assert fraction_gate.verdict is Verdict.INCONCLUSIVE
    assert fraction_gate.metric == "walk_forward_windows_without_returns"
    assert fraction_gate.value == float(len(empty))
    assert share_gate.gate_id == "G4.walk_forward.max_window_share"  # still computed

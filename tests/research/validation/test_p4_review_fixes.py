"""Regression tests for the five Phase 4 review findings fixed in Phase 8 (ADR-0041 §6).

1. label values cannot reach the sides the statistics use (blinding + ``G1.label_blind_sides``);
2. ``effective_sample_size`` counts nested overlaps as one sample;
3. sealed OOS: a global unsealing budget (explicit parameter) and one-shot evaluation;
4. G2 / G3 statistics run on the Profile's purged, embargoed walk-forward test folds only;
5. ``purged_k_fold`` never puts sealed OOS samples into a fold.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.contracts.outcome import OutcomeEvent
from core.domain.base import Kind, Ref
from core.domain.research import Verdict, derive_verdict
from research.outcomes import OutcomeTable
from research.validation import SealedOosInput, run_in_sample, run_sealed_oos
from research.validation.calibration import MomentumSignStudy
from research.validation.controls import SignalStudy
from research.validation.sealed_oos import (
    InMemoryUnsealingLedger,
    OosBudgetExhausted,
    SealedOosAlreadyEvaluated,
    SealedOosVault,
)
from research.validation.splits import LabeledSpan, purged_k_fold, walk_forward_folds
from research.validation.stats import effective_sample_size
from tests.research.validation.fixtures import (
    BOUNDARY,
    TEST_ONLY_PROFILE,
    context,
    generate,
    in_sample_input,
    outcome_table,
    research_events,
    sealed_events,
)

T0 = datetime(2024, 1, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
SIGNAL = (Ref(kind=Kind.FEATURE, name="innocent_looking", version="1.0.0"),)


def _events(table: OutcomeTable) -> list[OutcomeEvent]:
    return [OutcomeEvent(event_key=x.event_key, event_time=x.event_time) for x in table]


@pytest.fixture(scope="module")
def noise() -> tuple[object, OutcomeTable]:
    market = generate(seed=5)
    return market, outcome_table(market, research_events(market))


# 1. label leak ---------------------------------------------------------------------------


class MemoizingLeakyStudy:
    """Leaks on its first call only, then replays the memo (dodges the re-run controls)."""

    def __init__(self) -> None:
        self._memo: dict[str, int] = {}

    @property
    def signal_refs(self) -> tuple[Ref, ...]:
        return SIGNAL

    def sides(self, event_keys: Sequence[str], label_values: Sequence[Decimal]) -> tuple[int, ...]:
        if not self._memo:
            self._memo = {
                key: (value > 0) - (value < 0)
                for key, value in zip(event_keys, label_values, strict=True)
            }
        return tuple(self._memo.get(key, 0) for key in event_keys)


class LabelReadingStudy:
    @property
    def signal_refs(self) -> tuple[Ref, ...]:
        return SIGNAL

    def sides(self, event_keys: Sequence[str], label_values: Sequence[Decimal]) -> tuple[int, ...]:
        return tuple((value > 0) - (value < 0) for value in label_values)


def test_a_study_that_reads_labels_fails_the_label_blind_gate(
    noise: tuple[object, OutcomeTable],
) -> None:
    _, table = noise
    gates = {g.gate_id: g for g in run_in_sample(in_sample_input(table, LabelReadingStudy()))}
    gate = gates["G1.label_blind_sides"]
    assert gate.verdict is Verdict.FAIL and gate.value > 0
    assert not any(key.startswith("G2") for key in gates)


def test_a_memoized_leak_never_reaches_the_statistics(
    noise: tuple[object, OutcomeTable],
) -> None:
    _, table = noise
    gates = run_in_sample(in_sample_input(table, MemoizingLeakyStudy()))
    # the first call the study ever sees is blinded: it memoizes nothing useful
    assert derive_verdict(gates) is not Verdict.PASS
    by_id = {gate.gate_id: gate for gate in gates}
    assert by_id["G1.label_blind_sides"].verdict is Verdict.PASS
    assert by_id["G2.effective_sample_size"].value == 0.0


# 2. effective sample size ----------------------------------------------------------------


def _iv(start: int, end: int) -> tuple[datetime, datetime]:
    return (T0 + start * MINUTE, T0 + end * MINUTE)


@pytest.mark.parametrize(
    ("intervals", "expected"),
    [
        ([(0, 10), (2, 3)], 1),
        ([(0, 10), (2, 3), (4, 5)], 1),  # the old min() counted 2
        ([(0, 2), (1, 3), (2, 4)], 1),  # a chain of overlaps is one cluster
        ([(0, 1), (1, 2), (2, 3)], 3),  # touching half-open intervals do not overlap
        ([(0, 10), (2, 3), (10, 11)], 2),
    ],
)
def test_nested_overlaps_are_one_effective_sample(
    intervals: list[tuple[int, int]], expected: int
) -> None:
    assert effective_sample_size([_iv(a, b) for a, b in intervals]) == expected


# 3. sealed OOS budget and one-shot evaluation --------------------------------------------


def test_switching_family_cannot_exceed_the_unsealing_budget() -> None:
    vault = SealedOosVault(TEST_ONLY_PROFILE, InMemoryUnsealingLedger(), max_unsealings=1)
    assert vault.budget_source == "param:max_unsealings"
    vault.unseal("family-1", "raphael", BOUNDARY + timedelta(days=5))
    with pytest.raises(OosBudgetExhausted):
        vault.unseal("family-2", "raphael", BOUNDARY + timedelta(days=5))
    with pytest.raises(ValueError):
        SealedOosVault(TEST_ONLY_PROFILE, InMemoryUnsealingLedger(), max_unsealings=0)
    with pytest.raises(TypeError):
        SealedOosVault(TEST_ONLY_PROFILE, InMemoryUnsealingLedger())  # type: ignore[call-arg]


def test_an_unsealing_buys_exactly_one_evaluation() -> None:
    vault = SealedOosVault(TEST_ONLY_PROFILE, InMemoryUnsealingLedger(), max_unsealings=2)
    inside = LabeledSpan(key="in", start=BOUNDARY + MINUTE, end=BOUNDARY + 3 * MINUTE)
    vault.unseal("family-1", "raphael", BOUNDARY + timedelta(days=5))
    assert vault.sealed_view("family-1", [inside]) == (inside,)
    with pytest.raises(SealedOosAlreadyEvaluated):
        vault.sealed_view("family-1", [inside])


def test_run_sealed_oos_cannot_be_repeated() -> None:
    market = generate(seed=3, strength="0.6")
    table = outcome_table(market, sealed_events(market))
    study = MomentumSignStudy(market, _events(table))
    vault = SealedOosVault(TEST_ONLY_PROFILE, InMemoryUnsealingLedger(), max_unsealings=1)
    ctx = context()
    vault.unseal(ctx.metadata.hypothesis_family_id, "raphael", BOUNDARY + timedelta(days=2))
    run_sealed_oos(SealedOosInput(ctx, vault, table, study))
    with pytest.raises(SealedOosAlreadyEvaluated):
        run_sealed_oos(SealedOosInput(ctx, vault, table, study))


# 4. split-driven statistics --------------------------------------------------------------


class EarlyOnlyStudy:
    """Trades only in the first walk-forward training window (never a test fold)."""

    def __init__(self, table: OutcomeTable) -> None:
        cut = T0 + TEST_ONLY_PROFILE.data_split.walk_forward.train_window
        self._early = {x.event_key for x in table if x.event_time < cut}

    @property
    def signal_refs(self) -> tuple[Ref, ...]:
        return SIGNAL

    def sides(self, event_keys: Sequence[str], label_values: Sequence[Decimal]) -> tuple[int, ...]:
        return tuple(1 if key in self._early else 0 for key in event_keys)


class RecordingFittableStudy(MomentumSignStudy):
    fits: list[tuple[str, ...]]

    def fit(self, train_keys: Sequence[str], train_values: Sequence[Decimal]) -> SignalStudy:
        self.fits.append(tuple(train_keys))
        return self


def test_statistics_only_see_walk_forward_test_folds(
    noise: tuple[object, OutcomeTable],
) -> None:
    _, table = noise
    gates = {g.gate_id: g for g in run_in_sample(in_sample_input(table, EarlyOnlyStudy(table)))}
    assert gates["G2.walk_forward_folds"].verdict is Verdict.PASS
    assert gates["G2.walk_forward_folds"].value > 0
    assert gates["G2.effective_sample_size"].value == 0.0  # the early trades are never tested
    assert gates["G2.effective_sample_size"].verdict is Verdict.INCONCLUSIVE


def test_a_fittable_study_is_fitted_on_purged_and_embargoed_training_labels(
    noise: tuple[object, OutcomeTable],
) -> None:
    market, table = noise
    study = RecordingFittableStudy(market, _events(table))  # type: ignore[arg-type]
    study.fits = []
    run_in_sample(in_sample_input(table, study))
    spans = [
        LabeledSpan(x.event_key, x.event_time, x.available_time or x.event_time)
        for x in table.computable()
    ]
    folds = walk_forward_folds(spans, TEST_ONLY_PROFILE)
    assert study.fits == [fold.train for fold in folds]
    for fold in folds:
        assert set(fold.train).isdisjoint(fold.test)
        assert set(fold.train).isdisjoint(fold.purged)
        assert set(fold.train).isdisjoint(fold.embargoed)


# 5. purged k-fold stays out of the sealed window ------------------------------------------


def test_purged_k_fold_excludes_sealed_oos_samples() -> None:
    inside = [
        LabeledSpan(f"r{i:03d}", T0 + i * 10 * MINUTE, T0 + (i * 10 + 5) * MINUTE)
        for i in range(40)
    ]
    sealed = [
        LabeledSpan(f"s{i:03d}", BOUNDARY + i * MINUTE, BOUNDARY + (i + 2) * MINUTE)
        for i in range(10)
    ]
    straddling = [LabeledSpan("x", BOUNDARY - MINUTE, BOUNDARY + MINUTE)]
    folds = purged_k_fold(
        inside + sealed + straddling, 4, timedelta(minutes=5), profile=TEST_ONLY_PROFILE
    )
    seen = {key for fold in folds for key in (*fold.train, *fold.test)}
    assert seen == {span.key for span in inside}

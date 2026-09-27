"""Phase 4 acceptance: purging and embargo are implemented and tested; Sealed OOS unseals once."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from research.validation.sealed_oos import (
    InMemoryUnsealingLedger,
    OosAlreadyUnsealed,
    SealedOosLocked,
    SealedOosVault,
    SealedWindow,
)
from research.validation.splits import (
    LabeledSpan,
    purge_and_embargo,
    purged_k_fold,
    research_spans,
    walk_forward_folds,
)
from tests.research.validation.fixtures import BOUNDARY, TEST_ONLY_PROFILE

T0 = datetime(2024, 1, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def span(key: str, start_minute: int, length: int = 10) -> LabeledSpan:
    start = T0 + start_minute * MINUTE
    return LabeledSpan(key=key, start=start, end=start + length * MINUTE)


def test_purging_removes_training_labels_that_overlap_the_test_span() -> None:
    test = [span("t1", 100), span("t2", 110)]  # test span: [100, 120]
    train = [
        span("before", 50),  # [50, 60]: kept
        span("leaks_into_test", 95),  # [95, 105]: its label overlaps the test span
        span("inside", 105),  # overlaps
        span("after_embargo", 200),  # far after: kept
        span("in_embargo", 125),  # starts within 30 min after the test span ends
    ]
    kept, purged, embargoed = purge_and_embargo(train, test, timedelta(minutes=30))
    assert [s.key for s in kept] == ["before", "after_embargo"]
    assert [s.key for s in purged] == ["leaks_into_test", "inside"]
    assert [s.key for s in embargoed] == ["in_embargo"]


def test_zero_embargo_is_legal_and_purging_still_applies() -> None:
    kept, purged, embargoed = purge_and_embargo(
        [span("a", 0), span("b", 21)], [span("t", 10)], timedelta(0)
    )
    assert [s.key for s in purged] == ["a"] and not embargoed
    assert [s.key for s in kept] == ["b"]


def test_purged_k_fold_never_trains_on_overlapping_or_embargoed_labels() -> None:
    spans = [span(f"s{i:03d}", i * 5) for i in range(60)]  # 10-minute labels every 5 minutes
    embargo = timedelta(minutes=20)
    by_key = {s.key: s for s in spans}
    for fold in purged_k_fold(spans, 4, embargo, profile=TEST_ONLY_PROFILE):
        test = [by_key[k] for k in fold.test]
        lo = min(s.start for s in test)
        hi = max(s.end for s in test)
        for key in fold.train:
            s = by_key[key]
            assert s.end < lo or s.start > hi, "a training label overlaps the test span"
            assert not hi < s.start < hi + embargo, "a training label sits in the embargo"
        assert set(fold.train).isdisjoint(fold.test)
        assert fold.purged and set(fold.purged).isdisjoint(fold.train)


def test_walk_forward_uses_profile_windows_and_stays_out_of_sealed_oos() -> None:
    spans = [span(f"s{i:04d}", i * 10, length=12) for i in range(24 * 6 + 10)]
    folds = walk_forward_folds(spans, TEST_ONLY_PROFILE)
    wf = TEST_ONLY_PROFILE.data_split.walk_forward
    assert folds, "the test-only profile yields at least one fold"
    by_key = {s.key: s for s in spans}
    for fold in folds:
        test_starts = [by_key[k].start for k in fold.test]
        train_starts = [by_key[k].start for k in fold.train]
        assert max(test_starts) - min(test_starts) < wf.test_window
        assert all(by_key[k].end < BOUNDARY for k in (*fold.train, *fold.test))
        assert max(train_starts) < min(test_starts)
        assert fold.purged  # 12-minute labels every 10 minutes overlap the test start
    assert all(s.end < BOUNDARY for s in research_spans(spans, TEST_ONLY_PROFILE))


def test_sealed_window_comes_from_fixed_profile_dates() -> None:
    window = SealedWindow.from_profile(TEST_ONLY_PROFILE)
    assert window.start == BOUNDARY
    assert window.end == BOUNDARY + TEST_ONLY_PROFILE.data_split.sealed_oos_length
    reaching = LabeledSpan(key="x", start=BOUNDARY - MINUTE, end=BOUNDARY + MINUTE)
    assert window.touches(reaching) and not window.contains(reaching)


def test_sealed_oos_is_locked_until_unsealed_and_unseals_only_once() -> None:
    ledger = InMemoryUnsealingLedger()
    vault = SealedOosVault(TEST_ONLY_PROFILE, ledger, max_unsealings=2)
    inside = LabeledSpan(key="in", start=BOUNDARY + MINUTE, end=BOUNDARY + 3 * MINUTE)
    before = LabeledSpan(key="before", start=T0, end=T0 + MINUTE)
    assert vault.research_view([inside, before]) == (before,)
    with pytest.raises(SealedOosLocked):
        vault.sealed_view("family-1", [inside])
    record = vault.unseal("family-1", "raphael", BOUNDARY + timedelta(days=5))
    assert record.family_unseal_count == 1
    assert vault.sealed_view("family-1", [inside, before]) == (inside,)
    with pytest.raises(OosAlreadyUnsealed):
        vault.unseal("family-1", "raphael", BOUNDARY + timedelta(days=6))
    with pytest.raises(OosAlreadyUnsealed):
        ledger.record("family-1", record)
    assert ledger.get("family-1") == record  # the record is never replaced or removed
    assert ledger.count() == 1
    with pytest.raises(SealedOosLocked):
        vault.sealed_view("family-2", [inside])  # unsealing is per family

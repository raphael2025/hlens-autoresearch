"""ADR-0083 retry journal and exact ledger-tail reducer tests (not executed in this task)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from core.domain.base import content_hash
from core.domain.research import Hypothesis, HypothesisOrigin
from research.hypotheses.ledger import TrialLedger
from research.loop.retry_admission import (
    RetryAdmissionError,
    RetryJournal,
    reduce_retry_ledger_tail,
)
from research.persistence import AppendOnlyJournal


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        name="h_retry",
        version="1.0.0",
        family_id="family_retry",
        statement="retry test hypothesis",
        conditions=("strategy = tsmom@1.0.0",),
        expected_direction="higher",
        minimum_meaningful_effect="1 bp",
        origin=HypothesisOrigin.HUMAN,
    )


def _prepare(hypothesis: Hypothesis, baseline: tuple[int, str]) -> dict[str, object]:
    item = {
        "name": hypothesis.name,
        "version": hypothesis.version,
        "hypothesis_hash": hypothesis.content_hash(),
        "attempt": "retry-1",
    }
    second = {**item, "attempt": "retry-2"}
    packet = {
        "packet_version": "1.0.0",
        "loop_record": {"record_content_hash": "a" * 64},
    }
    return {
        "state_version": 6,
        "loop_id": "loop-retry",
        "retry_id": "retry-001",
        "packet": packet,
        "packet_hash": content_hash(packet),
        "failed_record_hash": "a" * 64,
        "reviewer": "human-reviewer",
        "manifest": [item, second],
        "ledger_baseline_seq": baseline[0],
        "ledger_baseline_hash": baseline[1],
    }


def test_retry_reducer_recovers_only_the_exact_ledger_prefix(tmp_path: Path) -> None:
    ledger = TrialLedger(tmp_path / "trial_ledger.jsonl")
    hypothesis = _hypothesis()
    assert ledger.register(hypothesis)
    baseline = ledger.journal_head()
    assert baseline is not None
    ledger.register_reevaluation(hypothesis, "retry-1")
    retry = RetryJournal(tmp_path / "retry_admission.jsonl", loop_id="loop-retry", create=True)
    retry.append_prepare(_prepare(hypothesis, baseline))

    recovery = reduce_retry_ledger_tail(
        retry,
        cast(Any, ledger.journal_snapshot()),
        loop_id="loop-retry",
        state_version=6,
    )
    assert recovery is not None
    assert len(recovery.existing_entries) == 1
    assert [item.attempt for item in recovery.missing_items] == ["retry-2"]

    ledger.register_reevaluation(hypothesis, "retry-2")
    complete = reduce_retry_ledger_tail(
        retry,
        cast(Any, ledger.journal_snapshot()),
        loop_id="loop-retry",
        state_version=6,
    )
    assert complete is not None and not complete.missing_items
    assert ledger.trials("family_retry") == 3


def test_retry_journal_rejects_legacy_version_and_duplicate_attempts(tmp_path: Path) -> None:
    legacy = RetryJournal(tmp_path / "legacy-retry.jsonl", loop_id="loop-retry", create=True)
    hypothesis = _hypothesis()
    baseline = (0, "0" * 64)
    payload = _prepare(hypothesis, baseline)
    legacy.append_prepare(payload)
    with pytest.raises(RetryAdmissionError, match="state version 6"):
        reduce_retry_ledger_tail(
            legacy,
            cast(Any, TrialLedger(tmp_path / "empty-ledger.jsonl").journal_snapshot()),
            loop_id="loop-retry",
            state_version=5,
        )

    duplicated = RetryJournal(tmp_path / "duplicate-retry.jsonl", loop_id="loop-retry", create=True)
    payload["manifest"] = [payload["manifest"][0], payload["manifest"][0]]  # type: ignore[index]
    with pytest.raises(RetryAdmissionError, match="unique"):
        duplicated.append_prepare(payload)


def test_retry_journal_requires_a_human_reviewer_and_matching_packet_hash(
    tmp_path: Path,
) -> None:
    retry = RetryJournal(tmp_path / "retry_admission.jsonl", loop_id="loop-retry", create=True)
    payload = _prepare(_hypothesis(), (0, "0" * 64))
    payload["reviewer"] = "automation"
    with pytest.raises(RetryAdmissionError, match="human reviewer"):
        retry.append_prepare(payload)

    payload["reviewer"] = "human-reviewer"
    payload["packet_hash"] = "b" * 64
    with pytest.raises(RetryAdmissionError, match="packet hash"):
        retry.append_prepare(payload)


def test_retry_reducer_refuses_a_ledger_line_that_does_not_match_manifest(
    tmp_path: Path,
) -> None:
    ledger = TrialLedger(tmp_path / "trial_ledger.jsonl")
    hypothesis = _hypothesis()
    ledger.register(hypothesis)
    baseline = ledger.journal_head()
    assert baseline is not None
    ledger.register_reevaluation(hypothesis, "different-attempt")
    retry = RetryJournal(tmp_path / "retry_admission.jsonl", loop_id="loop-retry", create=True)
    retry.append_prepare(_prepare(hypothesis, baseline))

    with pytest.raises(RetryAdmissionError, match="prefix differs"):
        reduce_retry_ledger_tail(
            retry,
            cast(Any, ledger.journal_snapshot()),
            loop_id="loop-retry",
            state_version=6,
        )


def test_retry_journal_rejects_unknown_event_types(tmp_path: Path) -> None:
    path = tmp_path / "retry_admission.jsonl"
    AppendOnlyJournal(path).append("retry_cancelled", {})
    with pytest.raises(RetryAdmissionError, match="start with retry_prepare"):
        RetryJournal(path, loop_id="loop-retry")

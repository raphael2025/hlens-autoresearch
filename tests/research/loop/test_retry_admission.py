"""ADR-0083 retry journal, manifest checks and exact ledger-tail reducer (pure units)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.domain.base import content_hash
from core.domain.research import Hypothesis, HypothesisOrigin
from research.hypotheses.ledger import TrialLedger
from research.loop.retry_admission import (
    RetryAdmissionError,
    RetryJournal,
    RetryJournals,
    RetryManifestItem,
    check_retry_budget,
    manifest_items,
    reduce_retry_ledger_tail,
    resolve_manifest,
    retry_commit_payload,
    retry_prepare_payload,
    validate_reviewer,
)
from research.persistence import AppendOnlyJournal, JournalEntry

LOOP = "loop-retry"
FAILED = "a" * 64


def _hypothesis(name: str = "h_retry") -> Hypothesis:
    return Hypothesis(
        name=name,
        version="1.0.0",
        family_id="family_retry",
        statement="retry test hypothesis",
        conditions=("strategy = tsmom@1.0.0",),
        expected_direction="higher",
        minimum_meaningful_effect="1 bp",
        origin=HypothesisOrigin.HUMAN,
    )


def _item(hypothesis: Hypothesis, attempt: str) -> RetryManifestItem:
    return RetryManifestItem(
        hypothesis.name, hypothesis.version, hypothesis.content_hash(), attempt
    )


def _prepare(
    hypothesis: Hypothesis, baseline: tuple[int, str], *, failed: str = FAILED
) -> dict[str, Any]:
    packet = {"packet_version": "1.0.0", "loop_record": {"record_content_hash": failed}}
    return retry_prepare_payload(
        loop_id=LOOP,
        packet=packet,
        packet_hash=content_hash(packet),
        failed_record_hash=failed,
        reviewer="human-reviewer",
        manifest=[_item(hypothesis, "retry-1"), _item(hypothesis, "retry-2")],
        ledger_baseline=baseline,
    )


def _journal(tmp_path: Path, failed: str = FAILED) -> RetryJournal:
    return RetryJournal(tmp_path / f"{failed}.jsonl", loop_id=LOOP, failed_record_hash=failed)


def _entries(ledger: TrialLedger) -> tuple[JournalEntry, ...]:
    snapshot = ledger.journal_snapshot()
    assert snapshot is not None
    return snapshot.entries


def test_the_reducer_recovers_only_the_exact_ledger_prefix(tmp_path: Path) -> None:
    ledger = TrialLedger(tmp_path / "trial_ledger.jsonl")
    hypothesis = _hypothesis()
    assert ledger.register(hypothesis)
    baseline = ledger.journal_head()
    assert baseline is not None
    journal = _journal(tmp_path)
    prepare = journal.append_prepare(_prepare(hypothesis, baseline))
    assert ledger.register_reevaluation(hypothesis, "retry-1")

    partial = reduce_retry_ledger_tail(journal, _entries(ledger), loop_id=LOOP)
    assert len(partial.existing_entries) == 1 and partial.commit is None
    assert [item.attempt for item in partial.missing_items] == ["retry-2"]

    assert ledger.register_reevaluation(hypothesis, "retry-2")
    complete = reduce_retry_ledger_tail(journal, _entries(ledger), loop_id=LOOP)
    assert not complete.missing_items
    journal.append_commit(
        retry_commit_payload(prepare, complete.existing_entries, memory_checkpoint_seq=3)
    )
    committed = reduce_retry_ledger_tail(journal, _entries(ledger), loop_id=LOOP)
    assert committed.commit is not None and not committed.missing_items
    # the failed trial is never removed: registration + both retries all count
    assert ledger.trials("family_retry") == 3


def test_a_journal_admits_exactly_one_retry(tmp_path: Path) -> None:
    hypothesis = _hypothesis()
    journal = _journal(tmp_path)
    journal.append_prepare(_prepare(hypothesis, (0, "0" * 64)))
    with pytest.raises(RetryAdmissionError, match="one journal admits one retry"):
        journal.append_prepare(_prepare(hypothesis, (0, "0" * 64)))
    journals = RetryJournals(tmp_path, loop_id=LOOP)
    with pytest.raises(RetryAdmissionError, match="already has a retry admission"):
        journals.create(FAILED)


def test_a_journal_is_bound_to_the_failed_record_it_is_named_after(tmp_path: Path) -> None:
    hypothesis = _hypothesis()
    other = "b" * 64
    journal = _journal(tmp_path, other)
    with pytest.raises(RetryAdmissionError, match="another failed record"):
        journal.append_prepare(_prepare(hypothesis, (0, "0" * 64)))
    AppendOnlyJournal(tmp_path / "moved.jsonl").append(
        "retry_prepare", _prepare(hypothesis, (0, "0" * 64))
    )
    with pytest.raises(RetryAdmissionError, match="not a retry journal file"):
        RetryJournals(tmp_path, loop_id=LOOP)


def test_the_manifest_is_explicit_unique_and_never_takes_a_loop_attempt_key() -> None:
    hypothesis = _hypothesis()
    with pytest.raises(RetryAdmissionError, match="non-empty"):
        manifest_items([])
    with pytest.raises(RetryAdmissionError, match="unique"):
        manifest_items([_item(hypothesis, "retry-1"), _item(hypothesis, "retry-1")])
    with pytest.raises(RetryAdmissionError, match="reserved"):
        manifest_items([_item(hypothesis, "loop_round:loop-retry:3")])
    with pytest.raises(RetryAdmissionError, match="normalized"):
        manifest_items([_item(hypothesis, " retry-1")])
    # the same hypothesis may be listed twice, under distinct fresh attempts
    assert len(manifest_items([_item(hypothesis, "a"), _item(hypothesis, "b")])) == 2


def test_g1_every_item_is_registered_with_exactly_that_content() -> None:
    hypothesis = _hypothesis()
    assert resolve_manifest([hypothesis], [_item(hypothesis, "r")]) == (hypothesis,)
    with pytest.raises(RetryAdmissionError, match="never registered"):
        resolve_manifest([hypothesis], [_item(_hypothesis("h_other"), "r")])
    forged = RetryManifestItem(hypothesis.name, hypothesis.version, "f" * 64, "r")
    with pytest.raises(RetryAdmissionError, match="differs from the content"):
        resolve_manifest([hypothesis], [forged])


def test_g2_the_declared_retry_fits_the_round_cap_and_the_remaining_total() -> None:
    budget = {"max_trials_per_round": 3, "max_trials_total": 10}
    check_retry_budget(budget, total_trials_spent=7, requested=3)
    with pytest.raises(RetryAdmissionError, match="max_trials_per_round"):
        check_retry_budget(budget, total_trials_spent=0, requested=4)
    with pytest.raises(RetryAdmissionError, match="remain under max_trials_total"):
        check_retry_budget(budget, total_trials_spent=8, requested=3)
    with pytest.raises(RetryAdmissionError, match="LoopBudget"):
        check_retry_budget(None, total_trials_spent=0, requested=1)


def test_a_human_reviewer_declaration_is_required() -> None:
    assert validate_reviewer("human-reviewer") == "human-reviewer"
    for automated in ("", " human", "system", "Automation", "research_loop:loop-retry"):
        with pytest.raises(RetryAdmissionError, match="reviewer"):
            validate_reviewer(automated)


def test_prepare_binds_its_packet_hash_and_retry_id(tmp_path: Path) -> None:
    hypothesis = _hypothesis()
    journal = _journal(tmp_path)
    payload = _prepare(hypothesis, (0, "0" * 64))
    with pytest.raises(RetryAdmissionError, match="packet hash"):
        journal.append_prepare({**payload, "packet_hash": "b" * 64})
    with pytest.raises(RetryAdmissionError, match="retry_id"):
        journal.append_prepare({**payload, "retry_id": "c" * 64})
    with pytest.raises(RetryAdmissionError, match="state version 6"):
        journal.append_prepare({**payload, "state_version": 5})
    assert not journal.entries and not journal.path.exists()


def test_the_reducer_refuses_a_divergent_or_overlong_ledger_tail(tmp_path: Path) -> None:
    ledger = TrialLedger(tmp_path / "trial_ledger.jsonl")
    hypothesis = _hypothesis()
    ledger.register(hypothesis)
    baseline = ledger.journal_head()
    assert baseline is not None
    journal = _journal(tmp_path)
    journal.append_prepare(_prepare(hypothesis, baseline))
    ledger.register_reevaluation(hypothesis, "different-attempt")
    with pytest.raises(RetryAdmissionError, match="prefix differs"):
        reduce_retry_ledger_tail(journal, _entries(ledger), loop_id=LOOP)

    longer = TrialLedger(tmp_path / "longer.jsonl")
    longer.register(hypothesis)
    for attempt in ("retry-1", "retry-2", "retry-3"):
        longer.register_reevaluation(hypothesis, attempt)
    with pytest.raises(RetryAdmissionError, match="beyond the retry manifest"):
        reduce_retry_ledger_tail(journal, _entries(longer), loop_id=LOOP)


def test_an_attempt_already_in_the_ledger_before_prepare_is_refused(tmp_path: Path) -> None:
    ledger = TrialLedger(tmp_path / "trial_ledger.jsonl")
    hypothesis = _hypothesis()
    ledger.register(hypothesis)
    ledger.register_reevaluation(hypothesis, "retry-1")
    baseline = ledger.journal_head()
    assert baseline is not None
    journal = _journal(tmp_path)
    journal.append_prepare(_prepare(hypothesis, baseline))
    with pytest.raises(RetryAdmissionError, match="already used"):
        reduce_retry_ledger_tail(journal, _entries(ledger), loop_id=LOOP)


def test_a_journal_with_unknown_events_is_refused(tmp_path: Path) -> None:
    path = tmp_path / f"{FAILED}.jsonl"
    AppendOnlyJournal(path).append("retry_cancelled", {})
    with pytest.raises(RetryAdmissionError, match="start with retry_prepare"):
        RetryJournal(path, loop_id=LOOP, failed_record_hash=FAILED)

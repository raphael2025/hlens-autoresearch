"""Durable sealed-OOS unsealing ledger survives a process restart (debugging pass, 2026-09-25;
ADR-0041 implementation note; backlog row R26).

TEST ONLY: Profile numbers come from ``tests/research/validation/fixtures.py`` (uncalibrated).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from research.persistence import AppendOnlyJournal, JournalCorrupted
from research.validation.sealed_oos import (
    DurableUnsealingLedger,
    InMemoryUnsealingLedger,
    OosAlreadyUnsealed,
    OosBudgetExhausted,
    SealedOosAlreadyEvaluated,
    SealedOosVault,
)
from tests.research.validation.fixtures import BOUNDARY, TEST_ONLY_PROFILE


def _vault(path: Path, *, max_unsealings: int = 2) -> SealedOosVault:
    return SealedOosVault(TEST_ONLY_PROFILE, max_unsealings=max_unsealings, path=path)


def test_a_family_unsealed_in_process_a_cannot_be_unsealed_again_in_process_b(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "unsealing.jsonl"

    process_a = _vault(ledger_path)
    record = process_a.unseal("fam-1", "raphael", BOUNDARY + timedelta(days=1))
    assert record.family_unseal_count == 1

    process_b = _vault(ledger_path)  # a fresh process opening the same ledger file
    assert process_b.is_unsealed("fam-1")
    with pytest.raises(OosAlreadyUnsealed):
        process_b.unseal("fam-1", "raphael", BOUNDARY + timedelta(days=2))


def test_an_evaluation_claimed_in_a_cannot_be_claimed_again_in_b(tmp_path: Path) -> None:
    ledger_path = tmp_path / "unsealing.jsonl"

    process_a = _vault(ledger_path)
    process_a.unseal("fam-1", "raphael", BOUNDARY + timedelta(days=1))
    process_a.claim_evaluation("fam-1")

    process_b = _vault(ledger_path)
    assert process_b.is_evaluated("fam-1")
    with pytest.raises(SealedOosAlreadyEvaluated):
        process_b.claim_evaluation("fam-1")
    with pytest.raises(SealedOosAlreadyEvaluated):
        process_b.sealed_view("fam-1", [])


def test_the_global_unsealing_budget_counts_across_restarts(tmp_path: Path) -> None:
    ledger_path = tmp_path / "unsealing.jsonl"

    process_a = _vault(ledger_path, max_unsealings=1)
    process_a.unseal("fam-1", "raphael", BOUNDARY + timedelta(days=1))  # the one budget slot used

    process_b = _vault(ledger_path, max_unsealings=1)
    assert process_b.max_unsealings == 1
    with pytest.raises(OosBudgetExhausted):
        process_b.unseal("fam-2", "raphael", BOUNDARY + timedelta(days=2))


def test_ledger_and_path_together_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="either ledger or path"):
        SealedOosVault(
            TEST_ONLY_PROFILE,
            InMemoryUnsealingLedger(),
            max_unsealings=1,
            path=tmp_path / "x.jsonl",
        )


def test_omitting_both_ledger_and_path_defaults_to_in_memory(tmp_path: Path) -> None:
    vault = SealedOosVault(TEST_ONLY_PROFILE, max_unsealings=1)
    vault.unseal("fam-1", "raphael", BOUNDARY + timedelta(days=1))
    # nothing was written to disk: a fresh vault with a path does not see it
    fresh = _vault(tmp_path / "unrelated.jsonl")
    assert not fresh.is_unsealed("fam-1")


def test_a_tampered_ledger_file_is_refused(tmp_path: Path) -> None:
    ledger_path = tmp_path / "unsealing.jsonl"
    _vault(ledger_path).unseal("fam-1", "raphael", BOUNDARY + timedelta(days=1))

    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["payload"]["family_id"] = "fam-2"  # rewrite who was unsealed
    ledger_path.write_text(json.dumps(tampered) + "\n", encoding="utf-8")

    with pytest.raises(JournalCorrupted):
        _vault(ledger_path)


def test_deterministic_replay_gives_the_same_state_hash(tmp_path: Path) -> None:
    ledger_path = tmp_path / "unsealing.jsonl"
    process_a = _vault(ledger_path)
    process_a.unseal("fam-1", "raphael", BOUNDARY + timedelta(days=1))
    process_a.claim_evaluation("fam-1")

    reopened_1 = DurableUnsealingLedger(ledger_path)
    reopened_2 = DurableUnsealingLedger(ledger_path)
    assert reopened_1.get("fam-1") == reopened_2.get("fam-1")
    assert reopened_1.is_evaluated("fam-1") == reopened_2.is_evaluated("fam-1") is True
    assert reopened_1.count() == reopened_2.count() == 1


def test_mark_evaluated_before_unsealing_is_corruption_on_replay(tmp_path: Path) -> None:
    path = tmp_path / "unsealing.jsonl"
    journal = AppendOnlyJournal(path)
    journal.append("mark_evaluated", {"family_id": "fam-1"})  # never unsealed first
    with pytest.raises(JournalCorrupted):
        DurableUnsealingLedger(path)


def test_an_unknown_record_type_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "unsealing.jsonl"
    journal = AppendOnlyJournal(path)
    journal.append("some_other_kind", {"family_id": "fam-1"})
    with pytest.raises(JournalCorrupted):
        DurableUnsealingLedger(path)

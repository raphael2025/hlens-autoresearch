"""A journal instance whose view of the file is stale must not append (2026-09-27).

Two holders of the same durable ledger file — two processes, or two instances in one process —
each replay the file when they open it. Before this fix ``AppendOnlyJournal.append`` refused only
a file that had **shrunk** since it was last read; a file that had **grown** (another writer
appended) was appended to with the stale ``seq`` / ``prev_hash``. Both writers saw success, each
kept acting on its own stale state, and the file was left with a broken chain that every later
open refuses. For the sealed-OOS unsealing ledger that is two unsealings / two evaluations of one
family (Constitution C-S1 ~ C-S3), for ``TrialLedger`` an undercounted trial family (the multiple
testing correction), and afterwards a ledger nobody can open.

The fix: ``append`` takes an exclusive ``flock`` on the file and refuses (``JournalCorrupted``,
nothing written) unless the file is exactly the size this instance last read or wrote; replay
reads under a shared lock, so a reader never sees a half-written line of a concurrent append.
"""

from __future__ import annotations

import multiprocessing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from core.domain.base import Kind, Ref
from research.hypotheses import TrialLedger, negation
from research.persistence import AppendOnlyJournal, JournalCorrupted
from research.validation.sealed_oos import SealedOosVault
from tests.research.validation.fixtures import BOUNDARY, TEST_ONLY_PROFILE

S = Ref(kind=Kind.STRATEGY, name="tsmom", version="1.0.0")


def test_a_stale_instance_cannot_append_after_another_writer(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    first = AppendOnlyJournal(path)
    stale = AppendOnlyJournal(path)  # opened before ``first`` writes: its view goes stale
    written = first.append("kind_a", {"n": 1})
    before = path.read_bytes()

    with pytest.raises(JournalCorrupted, match="another writer"):
        stale.append("kind_a", {"n": 2})
    assert path.read_bytes() == before  # nothing written
    assert stale.entries == ()  # its memory did not move either

    reopened = AppendOnlyJournal(path)  # the file is intact: exactly the first writer's line
    assert reopened.entries == (written,)
    # a fresh view appends normally, and the first writer then sees it is stale in turn
    reopened.append("kind_a", {"n": 3})
    with pytest.raises(JournalCorrupted, match="another writer"):
        first.append("kind_a", {"n": 4})
    assert len(AppendOnlyJournal(path).entries) == 2


def test_a_shrunk_file_is_still_refused_as_rewritten_history(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    journal = AppendOnlyJournal(path)
    journal.append("kind_a", {"n": 1})
    journal.append("kind_a", {"n": 2})
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text(lines[0], encoding="utf-8")
    with pytest.raises(JournalCorrupted, match="shrank"):
        journal.append("kind_a", {"n": 3})


def test_two_vaults_on_one_ledger_cannot_both_evaluate_a_family(tmp_path: Path) -> None:
    """C-S1 ~ C-S3: one unsealing and one evaluation per family, whoever holds the ledger."""
    path = tmp_path / "unsealing.jsonl"
    vault_a = SealedOosVault(TEST_ONLY_PROFILE, max_unsealings=2, path=path)
    vault_b = SealedOosVault(TEST_ONLY_PROFILE, max_unsealings=2, path=path)  # opened concurrently

    vault_a.unseal("fam-1", "raphael", BOUNDARY + timedelta(days=1))
    vault_a.claim_evaluation("fam-1")

    # B still believes fam-1 is sealed; its unsealing must be refused, not appended
    with pytest.raises(JournalCorrupted, match="another writer"):
        vault_b.unseal("fam-1", "raphael", BOUNDARY + timedelta(days=2))
    assert not vault_b.is_unsealed("fam-1")  # nothing admitted in B's memory

    reopened = SealedOosVault(TEST_ONLY_PROFILE, max_unsealings=2, path=path)
    assert reopened.is_unsealed("fam-1") and reopened.is_evaluated("fam-1")
    assert [e.type for e in AppendOnlyJournal(path).entries] == ["unseal", "mark_evaluated"]


def test_a_stale_vault_cannot_exceed_the_global_unsealing_budget(tmp_path: Path) -> None:
    path = tmp_path / "unsealing.jsonl"
    vault_a = SealedOosVault(TEST_ONLY_PROFILE, max_unsealings=1, path=path)
    vault_b = SealedOosVault(TEST_ONLY_PROFILE, max_unsealings=1, path=path)
    vault_a.unseal("fam-1", "raphael", BOUNDARY + timedelta(days=1))
    with pytest.raises(JournalCorrupted, match="another writer"):
        vault_b.unseal("fam-2", "raphael", BOUNDARY + timedelta(days=1))
    reopened = SealedOosVault(TEST_ONLY_PROFILE, max_unsealings=1, path=path)
    assert reopened.is_unsealed("fam-1") and not reopened.is_unsealed("fam-2")


def test_a_stale_trial_ledger_cannot_undercount_a_family(tmp_path: Path) -> None:
    path = tmp_path / "trials.jsonl"
    ledger_a = TrialLedger(path)
    ledger_b = TrialLedger(path)
    assert ledger_a.register(negation("h1", "fam", S, "0.1"))
    with pytest.raises(JournalCorrupted, match="another writer"):
        ledger_b.register(negation("h2", "fam", S, "0.1"))
    assert ledger_b.trials("fam") == 0  # nothing admitted in memory
    reopened = TrialLedger(path)
    assert reopened.trials("fam") == 1
    assert reopened.register(negation("h2", "fam", S, "0.1"))  # a fresh view records it
    assert TrialLedger(path).trials("fam") == 2


def _racer(path: str, barrier: Any, results: Any, index: int) -> None:
    journal = AppendOnlyJournal(Path(path))  # every racer replays the same (empty) file first
    barrier.wait()
    try:
        journal.append("race", {"writer": index})
    except JournalCorrupted:
        results.put((index, "refused"))
    else:
        results.put((index, "written"))


def test_concurrent_processes_never_corrupt_the_journal(tmp_path: Path) -> None:
    """Writers in separate processes, all opened on the same state, append at once: exactly one
    wins, the rest are refused, and the file replays cleanly with only the winner's line."""
    path = tmp_path / "race.jsonl"
    context = multiprocessing.get_context("fork")
    writers = 4
    barrier = context.Barrier(writers)
    results = context.Queue()
    processes = [
        context.Process(target=_racer, args=(str(path), barrier, results, index))
        for index in range(writers)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
        assert process.exitcode == 0
    outcomes = dict(results.get(timeout=10) for _ in range(writers))
    written = sorted(index for index, outcome in outcomes.items() if outcome == "written")
    assert len(written) == 1, outcomes
    entries = AppendOnlyJournal(path).entries  # replays and verifies the chain
    assert [entry.payload["writer"] for entry in entries] == written

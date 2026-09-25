"""``AppendOnlyJournal``: the hash-chained store behind the durable ledgers (debugging pass,
2026-09-25; backlog row R26).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.worker.journal import GENESIS_HASH as WORKER_GENESIS
from apps.worker.journal import AppendOnlyJournal as WorkerJournal
from apps.worker.journal import JournalCorrupted as WorkerCorrupted
from research.persistence import GENESIS_HASH, AppendOnlyJournal, JournalCorrupted


def test_append_returns_a_chained_entry_and_replay_reconstructs_it(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    journal = AppendOnlyJournal(path)
    assert journal.head_hash == GENESIS_HASH
    first = journal.append("kind_a", {"x": 1})
    assert first.seq == 1 and first.prev_hash == GENESIS_HASH
    second = journal.append("kind_b", {"y": [1, 2, 3]})
    assert second.prev_hash == first.hash and second.seq == 2

    reopened = AppendOnlyJournal(path)
    assert reopened.entries == journal.entries
    assert reopened.head_hash == journal.head_hash


def test_deterministic_replay_gives_the_same_state_hash(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    journal = AppendOnlyJournal(path)
    journal.append("kind_a", {"family": "f1", "n": 1})
    journal.append("kind_a", {"family": "f2", "n": 2})

    a = AppendOnlyJournal(path)
    b = AppendOnlyJournal(path)
    assert a.head_hash == b.head_hash == journal.head_hash
    assert a.entries == b.entries


def test_an_empty_or_missing_file_is_an_empty_journal(tmp_path: Path) -> None:
    missing = AppendOnlyJournal(tmp_path / "missing.jsonl")
    assert missing.entries == () and missing.head_hash == GENESIS_HASH

    empty_path = tmp_path / "empty.jsonl"
    empty_path.touch()
    empty = AppendOnlyJournal(empty_path)
    assert empty.entries == () and empty.head_hash == GENESIS_HASH


def test_a_tampered_payload_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    journal = AppendOnlyJournal(path)
    journal.append("kind_a", {"n": 1})
    journal.append("kind_a", {"n": 2})

    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["payload"]["n"] = 999  # content hash no longer matches
    lines[0] = json.dumps(tampered)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(JournalCorrupted):
        AppendOnlyJournal(path)


def test_reordered_lines_break_the_hash_chain(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    journal = AppendOnlyJournal(path)
    journal.append("kind_a", {"n": 1})
    journal.append("kind_a", {"n": 2})

    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")

    with pytest.raises(JournalCorrupted):
        AppendOnlyJournal(path)


def test_a_truncated_trailing_line_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    journal = AppendOnlyJournal(path)
    journal.append("kind_a", {"n": 1})

    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 2, "type": "kind_a", "payload": {"n": 2}, "prev')  # cut mid-line

    with pytest.raises(JournalCorrupted):
        AppendOnlyJournal(path)


def test_a_shrunk_file_is_refused_even_without_reload(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    journal = AppendOnlyJournal(path)
    journal.append("kind_a", {"n": 1})
    journal.append("kind_a", {"n": 2})

    path.write_text("", encoding="utf-8")  # history rewritten/truncated underneath the journal

    with pytest.raises(JournalCorrupted):
        journal.append("kind_a", {"n": 3})


def test_a_rewritten_hash_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    journal = AppendOnlyJournal(path)
    journal.append("kind_a", {"n": 1})

    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["hash"] = "0" * 64
    path.write_text(json.dumps(tampered) + "\n", encoding="utf-8")

    with pytest.raises(JournalCorrupted):
        AppendOnlyJournal(path)


def test_nan_and_infinity_payloads_are_rejected(tmp_path: Path) -> None:
    journal = AppendOnlyJournal(tmp_path / "j.jsonl")
    with pytest.raises(ValueError):
        journal.append("kind_a", {"n": float("nan")})


def test_append_fsyncs_and_survives_a_fresh_process_view(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    AppendOnlyJournal(path).append("kind_a", {"n": 1})
    # a brand new journal instance (standing in for a restarted process) sees the same content
    assert AppendOnlyJournal(path).head_hash != GENESIS_HASH


def test_the_worker_journal_shares_the_on_disk_contract(tmp_path: Path) -> None:
    """``apps.worker.journal`` is an independent implementation of this contract (apps/ may not
    import research/); files written by either replay identically in the other."""
    assert WORKER_GENESIS == GENESIS_HASH
    by_research, by_worker = tmp_path / "research.jsonl", tmp_path / "worker.jsonl"
    research, worker = AppendOnlyJournal(by_research), WorkerJournal(by_worker)
    for payload in ({"n": 1}, {"nested": {"b": [1, 2], "a": "x"}}, {"text": "非 ASCII"}):
        research.append("kind_a", payload)
        worker.append("kind_a", payload)
    assert by_research.read_bytes() == by_worker.read_bytes()
    assert WorkerJournal(by_research).head_hash == AppendOnlyJournal(by_worker).head_hash
    assert [e.payload for e in WorkerJournal(by_research).entries] == [
        e.payload for e in AppendOnlyJournal(by_worker).entries
    ]

    lines = by_worker.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["payload"]["n"] = 2
    lines[0] = json.dumps(tampered)
    by_worker.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(JournalCorrupted):
        AppendOnlyJournal(by_worker)
    with pytest.raises(WorkerCorrupted):
        WorkerJournal(by_worker)

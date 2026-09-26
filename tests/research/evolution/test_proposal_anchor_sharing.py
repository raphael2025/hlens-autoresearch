"""B49 (Codex full-code review 2026-09-26): one ``ProposalAnchor`` shared by ledgers / processes.

The review reproduced a corrupt anchor: two ``ProposalLedger`` paths sharing one anchor both
returned success and the anchor held two ``seq=1`` lines (each ``ProposalAnchor`` appended from
its own cached journal; the two ledger locks do not exclude each other). An anchor belongs to one
ledger: the second ledger is refused and writes nothing, stale anchor objects never append from a
cache, and separate processes serialize on the anchor's own ``flock``.

Invariant checked after **every** call that returned success (``_assert_anchor_replays``): a newly
constructed ``AppendOnlyJournal(anchor)`` replays (contiguous ``seq``, valid hash chain), the
anchored counts strictly increase, and every anchored head is the hash of the ledger's line at that
count; the last one is the ledger's current head.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from research.evolution import ProposalAnchor, ProposalLedger, ProposalLedgerInconsistent
from research.persistence import AppendOnlyJournal
from tests.research.evolution.anchor_child import RESULT_PREFIX
from tests.research.evolution.test_replacement_proposals import _propose

REPO = Path(__file__).resolve().parents[3]
CHILD = "tests.research.evolution.anchor_child"
TIMEOUT = 120  # seconds per child


def _chain(ledger: Path) -> list[str]:
    return [entry.hash for entry in AppendOnlyJournal(ledger).entries]


def _assert_anchor_replays(anchor: Path, ledger: Path | list[str]) -> None:
    chain = ledger if isinstance(ledger, list) else _chain(ledger)
    entries = AppendOnlyJournal(anchor).entries  # a fresh replay verifies the whole hash chain
    assert [e.seq for e in entries] == list(range(1, len(entries) + 1))
    counts = [e.payload["count"] for e in entries]
    assert counts == sorted(set(counts))  # strictly increasing: never back or sideways
    for entry in entries:
        assert entry.payload["head"] == chain[entry.payload["count"] - 1]
    if chain:
        assert (entries[-1].payload["count"], entries[-1].payload["head"]) == (
            len(chain),
            chain[-1],
        )
        assert ProposalAnchor(anchor).load() == (len(chain), chain[-1])


# --- (a) two different ledgers sharing one anchor --------------------------------------------


def test_a_second_ledger_sharing_the_anchor_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    """The review's reproduction: both ledgers open against the empty anchor, then both record."""
    anchor = tmp_path / "anchor.jsonl"
    a_path, b_path = tmp_path / "a" / "proposals.jsonl", tmp_path / "b" / "proposals.jsonl"
    a = ProposalLedger(a_path, anchor=anchor)
    b = ProposalLedger(b_path, anchor=anchor)  # both hold their own ledger lock, not the anchor's
    a.record(_propose())
    _assert_anchor_replays(anchor, a_path)
    with pytest.raises(ProposalLedgerInconsistent, match="another ledger"):
        b.record(_propose(reason="b's own proposal"))
    assert _chain(b_path) == [] and b.proposals == ()  # the refused record wrote nothing
    _assert_anchor_replays(anchor, a_path)
    a.record(_propose(reason="a's second proposal"))  # the anchor's own ledger goes on
    _assert_anchor_replays(anchor, a_path)
    with pytest.raises(ProposalLedgerInconsistent, match="another ledger"):
        b.record(_propose(reason="b tries again"))
    assert _chain(b_path) == []
    _assert_anchor_replays(anchor, a_path)
    a.close()
    b.close()


def test_a_ledger_with_its_own_lines_cannot_take_over_another_ledgers_anchor(
    tmp_path: Path,
) -> None:
    """Shorter, same-length and longer foreign ledgers: diverged, never re-anchored."""
    anchor = tmp_path / "anchor.jsonl"
    a_path = tmp_path / "a" / "proposals.jsonl"
    with ProposalLedger(a_path, anchor=anchor) as a:
        a.record(_propose())
        a.record(_propose(reason="a2"))
    for n in (1, 2, 3):  # shorter, same length, longer — each diverges at line 1
        other = tmp_path / f"other{n}" / "proposals.jsonl"
        with ProposalLedger(other, anchor=tmp_path / f"own{n}" / "anchor.jsonl") as ledger:
            for i in range(n):
                ledger.record(_propose(reason=f"other {n}.{i}"))
        before = other.read_bytes()
        with pytest.raises(ProposalLedgerInconsistent, match="another ledger"):
            ProposalLedger(other, anchor=anchor)
        assert other.read_bytes() == before
        _assert_anchor_replays(anchor, a_path)


def test_stale_anchor_objects_never_append_from_a_cache(tmp_path: Path) -> None:
    """Two ``ProposalAnchor`` objects made before either wrote: each answer comes from disk."""
    a_path, b_path = tmp_path / "a" / "p.jsonl", tmp_path / "b" / "p.jsonl"
    for path, tag in ((a_path, "a"), (b_path, "b")):
        with ProposalLedger(path) as ledger:
            for i in range(3):
                ledger.record(_propose(reason=f"{tag}{i}"))
    chain_a, chain_b = _chain(a_path), _chain(b_path)
    anchor = tmp_path / "anchor.jsonl"
    first, second = ProposalAnchor(anchor), ProposalAnchor(anchor)
    assert first.load() is None and second.load() is None
    first.publish(chain_a[:1])
    _assert_anchor_replays(anchor, chain_a[:1])
    assert second.load() == (1, chain_a[0])  # not a cached "empty"
    for chain in (chain_b[:1], chain_b[:2], chain_b):  # another ledger's chain, any length
        with pytest.raises(ProposalLedgerInconsistent, match="diverged"):
            second.publish(chain)
    second.publish(chain_a[:2])  # the anchored ledger's longer chain moves it up
    _assert_anchor_replays(anchor, chain_a[:2])
    with pytest.raises(ProposalLedgerInconsistent, match="truncated"):
        first.publish(chain_a[:1])  # stale: behind the anchor on disk, refused
    first.publish(chain_a)
    second.publish(chain_a)  # already anchored: no-op
    _assert_anchor_replays(anchor, chain_a)
    assert len(AppendOnlyJournal(anchor).entries) == 3


def test_a_ledger_whose_anchor_moved_under_it_refuses_to_record(tmp_path: Path) -> None:
    """The anchor is re-read before every line: a ledger never appends past a moved anchor."""
    anchor = tmp_path / "anchor.jsonl"
    a_path = tmp_path / "a" / "p.jsonl"
    other = tmp_path / "other" / "p.jsonl"
    with ProposalLedger(other) as ledger:
        ledger.record(_propose(reason="foreign"))
    with ProposalLedger(a_path, anchor=anchor) as a:  # opened on the empty anchor
        ProposalAnchor(anchor).publish(_chain(other))  # someone anchors another ledger meanwhile
        with pytest.raises(ProposalLedgerInconsistent):
            a.record(_propose())
    assert _chain(a_path) == []
    _assert_anchor_replays(anchor, other)


def test_the_anchor_itself_is_verified_on_every_read(tmp_path: Path) -> None:
    anchor = tmp_path / "anchor.jsonl"
    journal = AppendOnlyJournal(anchor)  # a valid chain of heads that moves back
    journal.append("proposal_ledger_head", {"count": 2, "head": "h2"})
    journal.append("proposal_ledger_head", {"count": 1, "head": "h1"})
    with pytest.raises(ProposalLedgerInconsistent, match="back or sideways"):
        ProposalAnchor(anchor).load()
    with pytest.raises(ProposalLedgerInconsistent, match="back or sideways"):
        ProposalAnchor(anchor).publish(("h1", "h2", "h3"))
    foreign = tmp_path / "foreign.jsonl"
    AppendOnlyJournal(foreign).append("something_else", {"count": 1, "head": "h1"})
    with pytest.raises(ProposalLedgerInconsistent, match="not a ledger head"):
        ProposalAnchor(foreign).load()
    assert len(AppendOnlyJournal(anchor).entries) == 2  # refusals wrote nothing


# --- (c) reopening / recovery of the same ledger ----------------------------------------------


def test_the_same_ledger_reopens_and_recovers_through_its_anchor(tmp_path: Path) -> None:
    path, anchor = tmp_path / "ledger" / "p.jsonl", tmp_path / "anchor.jsonl"
    stale = ProposalAnchor(anchor)  # made before anything exists; used again at the end
    for round_ in range(3):
        with ProposalLedger(path, anchor=anchor) as ledger:
            assert len(ledger.proposals) == 2 * round_
            for i in range(2):
                ledger.record(_propose(reason=f"round {round_} proposal {i}"))
                _assert_anchor_replays(anchor, path)
    # the process died after appending, before anchoring — twice
    for i in range(2):
        AppendOnlyJournal(path).append(
            "replacement_proposal", _propose(reason=f"late {i}").to_payload()
        )
    with ProposalLedger(path, anchor=anchor) as ledger:
        assert len(ledger.proposals) == 8
        _assert_anchor_replays(anchor, path)  # the anchor moved up to the recovered head
        ledger.record(_propose(reason="after recovery"))
        _assert_anchor_replays(anchor, path)
    assert stale.load() == (9, AppendOnlyJournal(path).head_hash)
    with pytest.raises(ProposalLedgerInconsistent, match="truncated"):
        stale.publish(_chain(path)[:8])  # a stale view of the same ledger never moves it back


# --- (b) separate processes publishing to one anchor ------------------------------------------


def _start(children: list[dict[str, Any]], tmp_path: Path) -> list[dict[str, Any]]:
    """Start every child, wait until each is ready, release them together, collect RESULTs."""
    start = tmp_path / "start"
    procs = []
    for i, args in enumerate(children):
        args = {**args, "ready": str(tmp_path / f"ready{i}"), "start": str(start)}
        procs.append(
            subprocess.Popen(
                [sys.executable, "-m", CHILD, json.dumps(args)],
                cwd=REPO,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    deadline = time.monotonic() + TIMEOUT
    while not all((tmp_path / f"ready{i}").exists() for i in range(len(children))):
        assert time.monotonic() < deadline, "the children never became ready"
        assert all(p.poll() is None for p in procs), [p.communicate() for p in procs]
        time.sleep(0.01)
    start.touch()
    results = []
    for proc in procs:
        out, err = proc.communicate(timeout=TIMEOUT)
        assert proc.returncode == 0, err
        lines = [line for line in out.splitlines() if line.startswith(RESULT_PREFIX)]
        assert len(lines) == 1, (out, err)
        results.append(json.loads(lines[0].removeprefix(RESULT_PREFIX)))
    return results


def test_ledgers_in_separate_processes_sharing_one_anchor_one_wins(tmp_path: Path) -> None:
    anchor = tmp_path / "anchor.jsonl"
    ledgers = [tmp_path / f"l{i}" / "p.jsonl" for i in range(4)]
    results = _start(
        [
            {"mode": "ledger", "ledger": str(p), "anchor": str(anchor), "n": 5, "tag": f"p{i}"}
            for i, p in enumerate(ledgers)
        ],
        tmp_path,
    )
    winners = [i for i, r in enumerate(results) if r["recorded"]]
    assert len(winners) == 1, results
    (winner,) = winners
    assert results[winner] == {"recorded": 5, "refused": None}
    for i, result in enumerate(results):
        if i != winner:
            assert result["recorded"] == 0 and "another ledger" in result["refused"], result
            assert _chain(ledgers[i]) == []  # a refused ledger holds no line
    _assert_anchor_replays(anchor, ledgers[winner])
    assert len(AppendOnlyJournal(anchor).entries) == 5


def test_stale_publishers_in_separate_processes_keep_the_anchor_valid(tmp_path: Path) -> None:
    path, anchor = tmp_path / "ledger" / "p.jsonl", tmp_path / "anchor.jsonl"
    with ProposalLedger(path) as ledger:
        for i in range(12):
            ledger.record(_propose(reason=f"line {i}"))
    chain = _chain(path)
    ProposalAnchor(anchor).publish(chain[:1])
    with ProposalLedger(tmp_path / "other" / "p.jsonl") as other:
        for i in range(12):
            other.record(_propose(reason=f"foreign {i}"))
    foreign = _chain(tmp_path / "other" / "p.jsonl")
    rng = random.Random(49)
    children: list[dict[str, Any]] = []
    for _ in range(4):
        prefixes = list(range(1, 13))
        rng.shuffle(prefixes)
        children.append(
            {"mode": "publish", "anchor": str(anchor), "chain": chain, "prefixes": prefixes}
        )
    children.append(
        {"mode": "publish", "anchor": str(anchor), "chain": foreign, "prefixes": list(range(1, 13))}
    )
    results = _start(children, tmp_path)
    assert results[-1] == {"published": [], "refused": list(range(1, 13))}  # never adopted
    for result in results[:-1]:
        assert sorted(result["published"] + result["refused"]) == list(range(1, 13))
    _assert_anchor_replays(anchor, chain)  # every anchored head lies on the ledger's chain
    assert ProposalAnchor(anchor).load() == (12, chain[-1])

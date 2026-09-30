"""Durable ``TrialLedger``: family trial counts survive a process restart (debugging pass,
2026-09-25; ADR-0040 implementation note; backlog row R26).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, cast

import pytest

from core.domain.base import Kind, Ref
from core.domain.research import HypothesisOrigin, LlmCall
from research.hypotheses import LedgerError, TrialLedger, negation
from research.hypotheses.generator import HypothesisDraft
from research.persistence import AppendOnlyJournal, JournalCorrupted

S = Ref(kind=Kind.STRATEGY, name="tsmom", version="1.0.0")


class _ToggleWriteGate:
    def __init__(self) -> None:
        self.open = True

    @contextmanager
    def write_scope(self, what: str, token: object | None = None) -> Iterator[None]:
        if not self.open:
            raise LedgerError(f"write gate is closed: {what}")
        yield


def test_the_family_trial_count_continues_across_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "trials.jsonl"

    process_a = TrialLedger(path)
    assert process_a.register(negation("h1", "fam", S, "0.1"))
    assert process_a.register(negation("h2", "fam", S, "0.1"))
    assert process_a.trials("fam") == 2

    process_b = TrialLedger(path)  # a fresh process opening the same ledger file
    assert process_b.trials("fam") == 2
    assert process_b.register(negation("h3", "fam", S, "0.1"))
    assert process_b.trials("fam") == 3

    process_c = TrialLedger(path)
    assert process_c.trials("fam") == 3
    assert {h.name for h in process_c.hypotheses} == {"h1", "h2", "h3"}


def test_re_registering_the_same_hypothesis_after_reload_is_not_a_new_trial(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trials.jsonl"
    h1 = negation("h1", "fam", S, "0.1")
    TrialLedger(path).register(h1)

    reloaded = TrialLedger(path)
    assert not reloaded.register(h1)  # idempotent: the family count does not double
    assert reloaded.trials("fam") == 1


def test_changing_a_registered_hypothesis_after_reload_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "trials.jsonl"
    original = negation("h1", "fam", S, "0.1")
    TrialLedger(path).register(original)

    reloaded = TrialLedger(path)
    changed = original.model_copy(update={"statement": "something else"})
    with pytest.raises(LedgerError, match="new version"):
        reloaded.register(changed)


def test_omitting_path_keeps_the_ledger_purely_in_memory(tmp_path: Path) -> None:
    ledger = TrialLedger()
    ledger.register(negation("h1", "fam", S, "0.1"))
    # nothing was written to disk: a fresh path-backed ledger does not see it
    fresh = TrialLedger(tmp_path / "unrelated.jsonl")
    assert fresh.trials("fam") == 0


def test_a_tampered_ledger_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "trials.jsonl"
    TrialLedger(path).register(negation("h1", "fam", S, "0.1"))

    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["payload"]["family_id"] = "other_fam"
    path.write_text(json.dumps(tampered) + "\n", encoding="utf-8")

    with pytest.raises(JournalCorrupted):
        TrialLedger(path)


def test_an_unknown_record_type_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "trials.jsonl"
    AppendOnlyJournal(path).append("not_a_registration", {"x": 1})
    with pytest.raises(JournalCorrupted):
        TrialLedger(path)


def test_deterministic_replay_gives_the_same_state(tmp_path: Path) -> None:
    path = tmp_path / "trials.jsonl"
    ledger = TrialLedger(path)
    ledger.register(negation("h1", "fam", S, "0.1"))
    ledger.register(negation("h2", "fam", S, "0.1"))

    a = TrialLedger(path)
    b = TrialLedger(path)
    assert a.trials("fam") == b.trials("fam") == 2
    assert [h.content_hash() for h in a.hypotheses] == [h.content_hash() for h in b.hypotheses]


def test_re_evaluations_are_durable_trials_across_a_restart(tmp_path: Path) -> None:
    """ADR-0049 accumulated window × R26: re-evaluations count after a restart too."""
    path = tmp_path / "trials.jsonl"
    h1 = negation("h1", "fam", S, "0.1")

    process_a = TrialLedger(path)
    assert process_a.register(h1)
    assert process_a.register_reevaluation(h1, "round-1")
    assert process_a.trials("fam") == 2

    process_b = TrialLedger(path)
    assert process_b.trials("fam") == 2
    assert process_b.is_registered(h1, "round-1")
    assert process_b.trial_index(h1, "round-1") == 2
    assert not process_b.register_reevaluation(h1, "round-1")  # same attempt: not a new trial
    assert process_b.register_reevaluation(h1, "round-2")
    assert TrialLedger(path).trials("fam") == 3
    assert [e.attempt for e in TrialLedger(path).trial_log] == [None, "round-1", "round-2"]


def test_a_re_evaluation_line_without_its_registration_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "trials.jsonl"
    h1 = negation("h1", "fam", S, "0.1")
    AppendOnlyJournal(path).append(
        "reevaluate", {"hypothesis": h1.model_dump(mode="json"), "attempt": "round-1"}
    )
    with pytest.raises(JournalCorrupted, match="inconsistent"):
        TrialLedger(path)


def test_exact_duplicates_are_read_only_after_the_write_gate_closes(tmp_path: Path) -> None:
    ledger = TrialLedger(tmp_path / "trials.jsonl")
    hypothesis = negation("h1", "fam", S, "0.1")
    assert ledger.register(hypothesis)
    assert ledger.register_reevaluation(hypothesis, "round-1")

    changed = hypothesis.model_copy(update={"statement": "changed content"})
    with pytest.raises(LedgerError, match="new version"):
        ledger.register(changed)

    gate = _ToggleWriteGate()
    ledger.bind_write_gate(gate)
    before_head = ledger.journal_head()
    before_trials = ledger.trials("fam")
    gate.open = False  # models a closed round / closed durable-state write gate

    assert not ledger.register(hypothesis)
    assert not ledger.register_reevaluation(hypothesis, " round-1 ")
    assert ledger.journal_head() == before_head
    assert ledger.trials("fam") == before_trials == 2

    with pytest.raises(LedgerError, match="write gate is closed"):
        ledger.register(negation("h2", "fam", S, "0.1"))
    with pytest.raises(LedgerError, match="write gate is closed"):
        ledger.register_reevaluation(hypothesis, "round-2")
    assert ledger.journal_head() == before_head
    assert ledger.trials("fam") == before_trials


def test_exact_duplicates_are_read_only_during_an_active_ledger_lease(tmp_path: Path) -> None:
    ledger = TrialLedger(tmp_path / "trials.jsonl")
    hypothesis = negation("h1", "fam", S, "0.1")
    assert ledger.register(hypothesis)
    assert ledger.register_reevaluation(hypothesis, "round-1")
    gate = _ToggleWriteGate()
    ledger.bind_write_gate(gate)
    lease = ledger.acquire_write_lease()
    before_head = ledger.journal_head()
    before_trials = ledger.trials("fam")

    assert not ledger.register(hypothesis)
    assert not ledger.register_reevaluation(hypothesis, "round-1")
    assert ledger.journal_head() == before_head
    assert ledger.trials("fam") == before_trials == 2

    with pytest.raises(LedgerError, match="write lease"):
        ledger.register(negation("h2", "fam", S, "0.1"))
    with pytest.raises(LedgerError, match="write lease"):
        ledger.register_reevaluation(hypothesis, "round-2")
    assert ledger.journal_head() == before_head
    assert ledger.trials("fam") == before_trials
    ledger.release_write_lease(lease)


def test_llm_origin_exact_duplicate_is_still_rejected_by_register(tmp_path: Path) -> None:
    ledger = TrialLedger(tmp_path / "trials.jsonl")
    hypothesis = negation("h1", "fam", S, "0.1").model_copy(
        update={"origin": HypothesisOrigin.LLM}
    )
    reviewed = HypothesisDraft(hypothesis, cast(LlmCall, object()), reviewed=True)
    assert ledger.register_draft(reviewed)
    before_head = ledger.journal_head()
    before_trials = ledger.trials("fam")

    with pytest.raises(LedgerError, match="reviewed draft"):
        ledger.register(hypothesis)

    assert ledger.journal_head() == before_head
    assert ledger.trials("fam") == before_trials == 1

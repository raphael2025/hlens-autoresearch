"""Durable ``TrialLedger``: family trial counts survive a process restart (debugging pass,
2026-09-25; ADR-0040 implementation note; backlog row R26).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.domain.base import Kind, Ref
from core.domain.research import HypothesisOrigin
from research.hypotheses import LedgerError, TrialLedger, negation
from research.hypotheses.generator import HypothesisDraft
from research.persistence import AppendOnlyJournal, JournalCorrupted
from tests.factories import llm_call

S = Ref(kind=Kind.STRATEGY, name="tsmom", version="1.0.0")


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


def test_direct_register_rejects_an_already_reviewed_llm_hypothesis() -> None:
    ledger = TrialLedger()
    hypothesis = negation("llm_h1", "fam", S, "0.1").model_copy(
        update={"origin": HypothesisOrigin.LLM}
    )
    assert ledger.register_draft(
        HypothesisDraft(hypothesis=hypothesis, call=llm_call(), reviewed=True)
    )

    with pytest.raises(LedgerError, match="registered only as a reviewed draft"):
        ledger.register(hypothesis)


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

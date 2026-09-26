"""Phase 7: LLM call content must be retrievable and be exactly what was exchanged (opt-in)."""

from __future__ import annotations

import gc
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.contracts.llm import LlmRequest, LlmResponse
from infrastructure.content import LocalContentStore
from plugins.llm import ScriptedLLMProvider
from plugins.synthetic import RandomWalkMarket
from research.hypotheses import from_llm
from research.loop.compose import open_synthetic_loop
from research.loop.llm_content import ContentVerifiedLLM, verify_call_content
from tests.research.loop import loop_fixtures as fx
from tests.research.loop.test_loop_durable import DRAFT, REVIEWER, _config

T0 = datetime(2026, 1, 1, tzinfo=UTC)
OUTPUT = fx.llm_output(0, None)


def _scripted(store: LocalContentStore | None, outputs: int = 1) -> ScriptedLLMProvider:
    return ScriptedLLMProvider(
        [fx.llm_output(i, None) for i in range(outputs)], clock=lambda: T0, store=store
    )


def test_a_stored_call_verifies_and_the_draft_is_built(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path / "blobs")
    llm = ContentVerifiedLLM(_scripted(store), store)
    draft = from_llm(llm, "propose", {"round": 0}, "fam")
    assert draft.call.output.uri.startswith("cas://sha256/")
    verify_call_content(draft.call, store)


def test_memory_refs_are_not_retrievable_and_the_draft_is_rejected(tmp_path: Path) -> None:
    llm = ContentVerifiedLLM(_scripted(None), LocalContentStore(tmp_path / "blobs"))
    with pytest.raises(ValueError, match="not retrievable"):
        from_llm(llm, "propose", {"round": 0}, "fam")


def test_a_tampered_blob_is_refused(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path / "blobs")
    draft = from_llm(ContentVerifiedLLM(_scripted(store), store), "p", {"r": 0}, "fam")
    for path in (tmp_path / "blobs").rglob("*"):
        if path.is_file():
            path.chmod(0o644)
            path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="not retrievable"):
        verify_call_content(draft.call, store)


class _Lying:
    """TEST ONLY: answers, but stores other content than it exchanged (all refs resolve)."""

    def __init__(self, store: LocalContentStore) -> None:
        self._inner = _scripted(store, outputs=2)
        self._store = store

    @property
    def descriptor(self) -> Any:
        return self._inner.descriptor

    def complete(self, request: LlmRequest) -> LlmResponse:
        other = request.model_copy(update={"prompt": "something else"})
        stored = self._inner.complete(other)  # content of another exchange
        real = self._inner.complete(request)
        return real.model_copy(update={"call": stored.call})


def test_content_that_is_not_what_was_exchanged_is_refused(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path / "blobs")
    with pytest.raises(ValueError, match="not what was exchanged"):
        from_llm(ContentVerifiedLLM(_Lying(store), store), "propose", {"round": 0}, "fam")


def test_a_reviewed_draft_whose_content_vanished_stops_the_round(tmp_path: Path) -> None:
    """Round 0 drafts through the verified LLM; a human approves; the blobs are deleted before
    round 1 takes the draft: the hypothesis stage refuses it (fail closed)."""
    store = LocalContentStore(tmp_path / "blobs")
    outputs = [fx.llm_output(i, None) for i in range(3)]

    def open_loop() -> Any:
        return open_synthetic_loop(
            _config(),
            state_dir=tmp_path / "state",
            provider=RandomWalkMarket(),
            llm=ContentVerifiedLLM(
                ScriptedLLMProvider(outputs, clock=lambda: T0, store=store), store
            ),
        )

    with open_loop() as first:
        first.loop.run_unattended(1)
        assert DRAFT in first.memory.reviews.pending
        first.memory.reviews.approve(DRAFT, reviewer=REVIEWER)
    gc.collect()
    for path in (tmp_path / "blobs").rglob("*"):
        if path.is_file():
            path.unlink()
    with open_loop() as second:
        second.loop.run_unattended(1)
        last = second.loop.audit.records[-1]
        stages = {stage.name: stage for stage in last.stages}
        assert last.status.value == "FAILED"
        assert stages["hypothesis"].status.value == "FAILED"
        assert "not retrievable" in (stages["hypothesis"].error or "")
        assert all(
            stages[name].status.value == "SKIPPED"
            for name in ("evolution", "experiment", "validation", "memory")
        )
        registered = {str(h.ref) for h in second.memory.ledger.hypotheses}
        assert DRAFT not in registered  # never registered from vanished content

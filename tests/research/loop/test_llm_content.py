"""Phase 7: LLM call content must be retrievable and be exactly what was exchanged (opt-in)."""

from __future__ import annotations

import gc
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.contracts.llm import LLMProvider, LlmRequest, LlmResponse
from infrastructure.content import LocalContentStore
from infrastructure.event_bus import InMemoryEventBus
from plugins.llm import ScriptedLLMProvider
from plugins.synthetic import RandomWalkMarket
from research.hypotheses import from_llm
from research.loop import LoopStateInconsistent
from research.loop import compose as loop_compose
from research.loop.compose import (
    compose_durable,
    llm_content_fingerprint,
    loop_fingerprint,
    open_synthetic_loop,
)
from research.loop.durable import MEMORY_FILE, open_state
from research.loop.llm_content import (
    ContentVerifiedLLM,
    LlmContentUnverified,
    verify_call_content,
)
from research.persistence import AppendOnlyJournal
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
    inner = _scripted(None)
    llm = ContentVerifiedLLM(inner, LocalContentStore(tmp_path / "blobs"))
    with pytest.raises(ValueError, match="not retrievable") as caught:
        from_llm(llm, "propose", {"round": 0}, "fam")
    assert isinstance(caught.value, LlmContentUnverified)  # a ValueError that keeps the call
    assert caught.value.call == inner.calls[0]


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
    lying = _Lying(store)
    with pytest.raises(LlmContentUnverified, match="not what was exchanged") as caught:
        from_llm(ContentVerifiedLLM(lying, store), "propose", {"round": 0}, "fam")
    assert caught.value.call == lying._inner.calls[0]  # the call the response named


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


# ------------------------------------------------------------ the verification mode of a state_dir


def _verified(store: LocalContentStore) -> ContentVerifiedLLM:
    return ContentVerifiedLLM(_scripted(store), store)


def _open(state_dir: Path, llm: LLMProvider | None) -> Any:
    return open_synthetic_loop(
        _config(), state_dir=state_dir, provider=RandomWalkMarket(), bus=InMemoryEventBus(), llm=llm
    )


def _header_fingerprint(state_dir: Path) -> Any:
    return AppendOnlyJournal(state_dir / MEMORY_FILE).entries[0].payload["fingerprint"]


def test_the_verification_mode_is_fingerprinted_only_when_on(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path / "blobs")
    assert llm_content_fingerprint(None) == {} and llm_content_fingerprint(_scripted(None)) == {}
    assert llm_content_fingerprint(_verified(store)) == {"llm_content_verified": True}
    with _open(tmp_path / "plain", _scripted(None)):
        pass
    with _open(tmp_path / "verified", _verified(store)):
        pass
    gc.collect()
    plain = _header_fingerprint(tmp_path / "plain")
    assert plain == loop_fingerprint(_config())  # byte-identical to the unverified fingerprint
    assert _header_fingerprint(tmp_path / "verified") == {**plain, "llm_content_verified": True}


@pytest.mark.parametrize("reopen_with", ["plain", "none"])
def test_a_verified_directory_reopened_without_verification_is_refused(
    tmp_path: Path, reopen_with: str
) -> None:
    store = LocalContentStore(tmp_path / "blobs")
    with _open(tmp_path / "state", _verified(store)):
        pass
    gc.collect()
    other = _scripted(None) if reopen_with == "plain" else None
    with pytest.raises(LoopStateInconsistent, match="llm_content_verified"):
        _open(tmp_path / "state", other)
    gc.collect()
    with _open(tmp_path / "state", _verified(store)):  # the same mode reopens
        pass


@pytest.mark.parametrize("opened_with", ["plain", "none"])
def test_an_unverified_directory_reopened_with_verification_is_refused(
    tmp_path: Path, opened_with: str
) -> None:
    store = LocalContentStore(tmp_path / "blobs")
    with _open(tmp_path / "state", _scripted(None) if opened_with == "plain" else None):
        pass
    gc.collect()
    with pytest.raises(LoopStateInconsistent, match="llm_content_verified"):
        _open(tmp_path / "state", _verified(store))
    gc.collect()
    with _open(tmp_path / "state", None):  # the same (unverified) mode reopens
        pass


@pytest.mark.parametrize("verified_on_disk", [False, True])
def test_compose_durable_checks_the_recorded_mode(tmp_path: Path, verified_on_disk: bool) -> None:
    """A caller composing ``open_state`` directly cannot switch the mode either."""
    store = LocalContentStore(tmp_path / "blobs")
    config = _config()
    wiring = config.wiring
    recorded = _verified(store) if verified_on_disk else None
    state = open_state(
        tmp_path / "state",
        fingerprint={**loop_fingerprint(config), **llm_content_fingerprint(recorded)},
        strategies=wiring.strategies,
        provider=RandomWalkMarket(),
        provider_for=None if wiring.evolution is None else wiring.evolution.provider_for,
    )
    try:
        ingest = loop_compose._synthetic_ingest(config, RandomWalkMarket(), state.memory)
        switched = None if verified_on_disk else _verified(store)
        expected = "opened with LLM content verification " + ("on" if verified_on_disk else "off")
        with pytest.raises(LoopStateInconsistent, match=expected):
            compose_durable(config, state, ingest, InMemoryEventBus(), switched)
    finally:
        assert state.lock is not None
        state.lock.release()

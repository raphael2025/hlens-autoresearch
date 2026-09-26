"""Phase 7: the scripted LLM provider with and without a content store (ADR-0040, ADR-0016)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.contracts.llm import LlmRequest
from core.domain.base import ContentBlobRef
from infrastructure.content import LocalContentStore, verify_llm_call
from plugins.llm import BlobSink, ScriptedLLMProvider
from tests.contract_version_support import at_pre_bump, at_version, built_at, built_at_pre_bump

NOW = datetime(2026, 9, 26, tzinfo=UTC)
REQUEST = LlmRequest.model_validate(
    {"prompt": "propose", "input": {"topic": "volume"}, "output_schema": {"type": "object"}}
)
OUTPUT = {"name": "h_1", "x": [1, "é"]}

# Reference hashes captured at 01d8ded (before the store option existed); the no-store path must
# keep producing exactly these.
PROMPT_SHA = "b6a91f60a335157c252542f12a5fb513c001b3d74553e0f4216a2de18792370a"
INPUT_SHA = "16afd62096c653183beeedae162bf18c09e597ce2f60ad20f221a9c623be8c90"
OUTPUT_SHA = "0c664aa8afe4f64b1e67134d6533cef8b98cb09942a6477b788eec32777d2fc7"
LEGACY_CALL_HASH = "2d8207cb39211d1950f2e4311fc971fffefafc61001c9279309aec33d0115931"
LEGACY_RESPONSE_HASH = "42dd70737cbebdd319ac522ea682c8e1ba12e710f687a38c0992abe939c455f4"
#: The same objects built now carry the 2.1.0 envelope (ADR-0052 M2: new objects are 2.1.0
#: and the envelope is part of every content hash); pinned next to the 2.0.0 evidence above.
CALL_HASH_2_1_0 = "61c33bdf7fc3bfe9d3b4e455f5ca3a95cd0ed30f83a48d06b4c317618237ba94"
RESPONSE_HASH_2_1_0 = "eda3ee65e2279b5e2c619fbea9a9fadac012a4037fa912b9351c6de5c2a3ee21"


def test_without_a_store_the_call_is_byte_identical_to_before() -> None:
    # The reference hashes were captured at contract 2.0.0: rebuilt as the 2.0.0 code built them
    # (every envelope 2.0.0, ADR-0052 §4) the no-store path is still byte-identical.
    with built_at_pre_bump():
        response = ScriptedLLMProvider([OUTPUT], clock=lambda: NOW).complete(at_pre_bump(REQUEST))
    call = response.call
    assert (call.prompt.sha256, call.input.sha256, call.output.sha256) == (
        PROMPT_SHA,
        INPUT_SHA,
        OUTPUT_SHA,
    )
    assert call.prompt.uri == f"memory://sha256/{PROMPT_SHA}"
    assert call.prompt.byte_size is None
    assert call.content_hash() == LEGACY_CALL_HASH
    assert response.content_hash() == LEGACY_RESPONSE_HASH
    with built_at("2.1.0"):  # the 2.1.0 pins, on what the 2.1.0 code builds (ADR-0055)
        now = ScriptedLLMProvider([OUTPUT], clock=lambda: NOW).complete(
            at_version(REQUEST, "2.1.0")
        )
    assert (now.call.content_hash(), now.content_hash()) == (CALL_HASH_2_1_0, RESPONSE_HASH_2_1_0)


def test_with_a_store_the_refs_resolve_and_carry_the_same_hashes(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path)
    provider = ScriptedLLMProvider([OUTPUT], clock=lambda: NOW, store=store)
    response = provider.complete(REQUEST)
    call = response.call
    assert (call.prompt.sha256, call.input.sha256, call.output.sha256) == (
        PROMPT_SHA,
        INPUT_SHA,
        OUTPUT_SHA,
    )
    assert call.output.uri == f"cas://sha256/{OUTPUT_SHA}"
    assert call.output.byte_size == len(store.get(call.output))
    assert provider.calls == (call,)
    assert response.model_dump(mode="json")["output"] == OUTPUT
    # the recorded call differs from the legacy one only in the refs' uri / byte_size
    assert call.content_hash() != LEGACY_CALL_HASH
    content = verify_llm_call(call, LocalContentStore(tmp_path))
    assert (content.prompt, content.input, content.output) == (
        "propose",
        {"topic": "volume"},
        OUTPUT,
    )


def test_a_repeated_call_reuses_the_stored_blobs(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path)
    provider = ScriptedLLMProvider([OUTPUT, OUTPUT], clock=lambda: NOW, store=store)
    first, second = provider.complete(REQUEST), provider.complete(REQUEST)
    assert first.call == second.call
    assert len([p for p in tmp_path.rglob("*") if p.is_file()]) == 3


class _WrongHash:
    def put(self, data: bytes, media_type: str | None = None) -> ContentBlobRef:
        return ContentBlobRef(uri="x://y", sha256="0" * 64, media_type=media_type)


class _WrongSize:
    def __init__(self, inner: BlobSink) -> None:
        self._inner = inner

    def put(self, data: bytes, media_type: str | None = None) -> ContentBlobRef:
        ref = self._inner.put(data, media_type)
        return ref.model_copy(update={"byte_size": len(data) + 1})


def test_a_store_returning_a_mismatched_ref_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="does not match the content"):
        ScriptedLLMProvider([OUTPUT], clock=lambda: NOW, store=_WrongHash()).complete(REQUEST)
    wrong_size = _WrongSize(LocalContentStore(tmp_path))
    with pytest.raises(RuntimeError, match="wrong byte size"):
        ScriptedLLMProvider([OUTPUT], clock=lambda: NOW, store=wrong_size).complete(REQUEST)


def test_the_provider_still_refuses_when_the_script_is_exhausted(tmp_path: Path) -> None:
    provider = ScriptedLLMProvider([], clock=lambda: NOW, store=LocalContentStore(tmp_path))
    with pytest.raises(RuntimeError, match="no output left"):
        provider.complete(REQUEST)

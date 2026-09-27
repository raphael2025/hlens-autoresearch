"""Phase 7: the local content-addressed blob store and LLM call verification (ADR-0016 / 0040)."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.contracts.llm import LlmRequest
from core.domain.base import ContentBlobRef, content_hash
from core.domain.research import LlmCall
from infrastructure.content import (
    BlobConflict,
    BlobCorrupted,
    BlobMissing,
    ContentStoreError,
    LocalContentStore,
    verify_llm_call,
)
from plugins.llm import ScriptedLLMProvider

NOW = datetime(2026, 9, 26, tzinfo=UTC)


def _writable(path: Path) -> None:
    os.chmod(path, 0o644)


def test_put_get_round_trip_is_content_addressed(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path)
    data = b'{"a":1}'
    digest = hashlib.sha256(data).hexdigest()
    ref = store.put(data, "application/json")
    assert ref == ContentBlobRef(
        uri=f"cas://sha256/{digest}", sha256=digest, media_type="application/json", byte_size=7
    )
    path = tmp_path / "sha256" / digest[:2] / digest
    assert path.read_bytes() == data
    assert not os.access(path, os.W_OK), "published blobs are read-only"
    assert store.get(ref) == data
    # a fresh store over the same root resolves the ref (survives a restart)
    assert LocalContentStore(tmp_path).get(ref) == data


def test_identical_re_put_is_idempotent_and_leaves_no_temporary_files(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path)
    first = store.put(b"same")
    assert store.put(b"same") == first
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files == [store.path_for(first.sha256)]


def test_empty_blob_is_legal(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path)
    ref = store.put(b"")
    assert ref.byte_size == 0 and store.get(ref) == b""


def test_put_json_hash_equals_the_contract_content_hash(tmp_path: Path) -> None:
    payload = {"b": [1, "é"], "a": None}
    ref = LocalContentStore(tmp_path).put_json(payload)
    assert ref.sha256 == content_hash(payload)
    assert ref.media_type == "application/json"


def test_differing_bytes_at_an_address_are_refused_not_overwritten(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path)
    ref = store.put(b"original")
    path = store.path_for(ref.sha256)
    _writable(path)
    path.write_bytes(b"tampered")
    with pytest.raises(BlobConflict, match="different byte string"):
        store.put(b"original")
    assert path.read_bytes() == b"tampered", "the store never overwrites"


def test_put_refuses_non_bytes(tmp_path: Path) -> None:
    with pytest.raises(ContentStoreError, match="bytes"):
        LocalContentStore(tmp_path).put("text")  # type: ignore[arg-type]


@pytest.mark.parametrize("damage", ["tamper", "truncate", "extend"])
def test_tampered_or_truncated_blobs_are_refused(tmp_path: Path, damage: str) -> None:
    store = LocalContentStore(tmp_path)
    ref = store.put(b"0123456789")
    path = store.path_for(ref.sha256)
    _writable(path)
    new = {"tamper": b"0123456780", "truncate": b"01234", "extend": b"0123456789\n"}[damage]
    path.write_bytes(new)
    with pytest.raises(BlobCorrupted, match="does not hash"):
        store.get(ref)


def test_missing_blob_is_refused(tmp_path: Path) -> None:
    ref = LocalContentStore(tmp_path / "a").put(b"only in a")
    with pytest.raises(BlobMissing):
        LocalContentStore(tmp_path / "b").get(ref)


def test_a_ref_with_the_wrong_byte_size_is_refused(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path)
    ref = store.put(b"abc")
    with pytest.raises(BlobCorrupted, match="the ref says 4"):
        store.get(ref.model_copy(update={"byte_size": 4}))


@pytest.mark.parametrize(
    "uri",
    [
        "memory://sha256/{h}",
        "cas://sha256/" + "0" * 64,
        "file:///etc/{h}",
    ],
)
def test_foreign_refs_are_refused(tmp_path: Path, uri: str) -> None:
    store = LocalContentStore(tmp_path)
    ref = store.put(b"x")
    with pytest.raises(ContentStoreError, match="not a ref of this store"):
        store.get(ref.model_copy(update={"uri": uri.format(h=ref.sha256)}))


@pytest.mark.parametrize("bad", ["../../etc/passwd", "A" * 64, "0" * 63])
def test_addresses_must_be_sha256(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ContentStoreError, match="not a sha256"):
        LocalContentStore(tmp_path).path_for(bad)


# --------------------------------------------------------------------------------------
# verify_llm_call
# --------------------------------------------------------------------------------------

REQUEST = LlmRequest.model_validate(
    {"prompt": "propose", "input": {"topic": "volume"}, "output_schema": {"type": "object"}}
)
OUTPUT = {"name": "h_1", "x": [1, "é"]}


def _stored_call(store: LocalContentStore) -> LlmCall:
    provider = ScriptedLLMProvider([OUTPUT], clock=lambda: NOW, store=store)
    return provider.complete(REQUEST).call


def test_a_stored_call_verifies_and_returns_its_content(tmp_path: Path) -> None:
    call = _stored_call(LocalContentStore(tmp_path))
    content = verify_llm_call(call, LocalContentStore(tmp_path))
    assert content.prompt == "propose"
    assert content.input == {"topic": "volume"}
    assert content.output == OUTPUT


def test_a_legacy_memory_call_is_not_resolvable(tmp_path: Path) -> None:
    call = ScriptedLLMProvider([OUTPUT], clock=lambda: NOW).complete(REQUEST).call
    with pytest.raises(ContentStoreError, match="not a ref of this store"):
        verify_llm_call(call, LocalContentStore(tmp_path))


def test_a_tampered_output_blob_fails_verification(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path)
    call = _stored_call(store)
    path = store.path_for(call.output.sha256)
    _writable(path)
    path.write_bytes(path.read_bytes().replace(b"h_1", b"h_2"))
    with pytest.raises(BlobCorrupted):
        verify_llm_call(call, store)


def test_a_missing_blob_fails_verification(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path)
    call = _stored_call(store)
    store.path_for(call.input.sha256).unlink()
    with pytest.raises(BlobMissing):
        verify_llm_call(call, store)


def test_non_canonical_json_is_refused(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path)
    good = _stored_call(store)
    loose = store.put(b'{"topic": "volume"}', "application/json")
    with pytest.raises(BlobCorrupted, match="input: not canonical JSON"):
        verify_llm_call(good.model_copy(update={"input": loose}), store)
    not_json = store.put(b"\xff\xfe", "application/json")
    with pytest.raises(BlobCorrupted, match="output: not JSON"):
        verify_llm_call(good.model_copy(update={"output": not_json}), store)


def test_a_non_json_media_type_is_refused(tmp_path: Path) -> None:
    store = LocalContentStore(tmp_path)
    call = _stored_call(store)
    text = call.prompt.model_copy(update={"media_type": "text/plain"})
    with pytest.raises(BlobCorrupted, match="media type"):
        verify_llm_call(call.model_copy(update={"prompt": text}), store)


def test_a_lying_resolver_is_caught(tmp_path: Path) -> None:
    call = _stored_call(LocalContentStore(tmp_path))

    class Liar:
        def get(self, ref: ContentBlobRef) -> bytes:
            return b'"something else"'

    with pytest.raises(BlobCorrupted, match="do not match the ref"):
        verify_llm_call(call, Liar())

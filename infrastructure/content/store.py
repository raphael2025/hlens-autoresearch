"""Local, append-only, content-addressed blob store (Phase 7; ADR-0016 §D-18.3, ADR-0040).

``LlmCall`` registers every prompt / input / output as a ``ContentBlobRef`` (retrieval URI +
SHA-256) but the contract itself never opens the URI: whether it resolves, and whether what it
resolves to really hashes to ``sha256``, is the storage layer's obligation (ADR-0016 §D-18.3).
This module is that storage layer for local runs.

Layout under ``root``::

    sha256/<first two hex>/<full sha256>     one immutable blob per distinct byte string

URI scheme: ``cas://sha256/<hash>``. ``ContentBlobRef.uri`` is only required to be non-empty (the
scheme is deliberately not frozen in the contract, D-01 / D-02); this scheme is local to this store.

- ``put`` hashes the bytes, writes them to a temporary file in the target directory, ``fsync``s,
  and publishes with ``os.link`` (which never replaces an existing file), then ``fsync``s the
  directory. Re-putting identical bytes is idempotent and returns the same ref. If a file already
  sits at the address with **different** bytes (a tampered or damaged blob), ``put`` refuses with
  ``BlobConflict`` and never overwrites it.
- ``get`` refuses any ref that is not a ``cas://sha256/<sha256>`` of this store, re-hashes the
  bytes on every read and checks ``byte_size`` when the ref carries it: a missing blob raises
  ``BlobMissing``; a tampered, truncated or size-mismatched blob raises ``BlobCorrupted``.
- There is no delete and no overwrite (append-only); published blobs are made read-only.

``verify_llm_call`` resolves all three blobs of an ``LlmCall`` through any ``ContentResolver`` and
checks that each is canonical JSON (``core.domain.base.canonical_json``) — the exact bytes whose
SHA-256 the call records. Honest boundary: it proves the recorded call is retrievable and
unaltered; it does not prove that the experiment registered **every** call it made (a Registry /
Runner obligation, ADR-0016 §D-18.3).

Concurrency: safe for concurrent writers of the same blob (``os.link`` is atomic and never
clobbers); no locking is needed because addresses are immutable. POSIX only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from core.domain.base import ContentBlobRef, canonical_json
from core.domain.research import LlmCall

__all__ = [
    "JSON_MEDIA_TYPE",
    "URI_PREFIX",
    "BlobConflict",
    "BlobCorrupted",
    "BlobMissing",
    "ContentResolver",
    "ContentStoreError",
    "LlmCallContent",
    "LocalContentStore",
    "verify_llm_call",
]

URI_PREFIX: Final = "cas://sha256/"
JSON_MEDIA_TYPE: Final = "application/json"
_HEX64: Final = re.compile(r"[0-9a-f]{64}")


class ContentStoreError(RuntimeError):
    """A blob cannot be stored or resolved."""


class BlobMissing(ContentStoreError):
    """The ref names no blob in this store."""


class BlobCorrupted(ContentStoreError):
    """The stored bytes no longer match the ref (tampered, truncated, wrong size or not JSON)."""


class BlobConflict(ContentStoreError):
    """A different byte string already sits at the address; it is never overwritten."""


class ContentResolver(Protocol):
    def get(self, ref: ContentBlobRef) -> bytes: ...


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class LocalContentStore:
    """Append-only content-addressed blobs under ``root`` (see the module docstring)."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        (self._root / "sha256").mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, sha256: str) -> Path:
        if not isinstance(sha256, str) or not _HEX64.fullmatch(sha256):
            raise ContentStoreError(f"not a sha256 content address: {sha256!r}")
        return self._root / "sha256" / sha256[:2] / sha256

    def put(self, data: bytes, media_type: str | None = None) -> ContentBlobRef:
        if not isinstance(data, bytes):
            raise ContentStoreError("a blob must be bytes")
        digest = hashlib.sha256(data).hexdigest()
        target = self.path_for(digest)
        ref = ContentBlobRef(
            uri=f"{URI_PREFIX}{digest}", sha256=digest, media_type=media_type, byte_size=len(data)
        )
        if target.exists():
            self._check_existing(target, data)
            return ref
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o444)
            try:
                os.link(tmp, target)
            except FileExistsError:
                # a concurrent writer published first: accept only identical bytes
                self._check_existing(target, data)
                return ref
            _fsync_directory(target.parent)
        finally:
            tmp.unlink(missing_ok=True)
        return ref

    def put_json(self, payload: Any) -> ContentBlobRef:
        """Store ``canonical_json(payload)`` (UTF-8); SHA-256 == ``content_hash(payload)``."""
        return self.put(canonical_json(payload).encode("utf-8"), JSON_MEDIA_TYPE)

    def get(self, ref: ContentBlobRef) -> bytes:
        if not isinstance(ref, ContentBlobRef):
            raise ContentStoreError("get needs a ContentBlobRef")
        if ref.uri != f"{URI_PREFIX}{ref.sha256}":
            raise ContentStoreError(f"not a ref of this store: {ref.uri!r}")
        path = self.path_for(ref.sha256)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise BlobMissing(f"no blob {ref.sha256}") from None
        except OSError as exc:
            raise ContentStoreError(f"blob {ref.sha256} unreadable: {exc}") from None
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise BlobCorrupted(f"blob {ref.sha256} does not hash to its address")
        if ref.byte_size is not None and ref.byte_size != len(data):
            raise BlobCorrupted(
                f"blob {ref.sha256} is {len(data)} bytes, the ref says {ref.byte_size}"
            )
        return data

    @staticmethod
    def _check_existing(target: Path, data: bytes) -> None:
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise BlobConflict(f"{target.name}: existing blob unreadable: {exc}") from None
        if existing != data:
            raise BlobConflict(f"{target.name}: a different byte string is already stored")


@dataclass(frozen=True, slots=True)
class LlmCallContent:
    """The retrieved, verified JSON payloads of one ``LlmCall``."""

    prompt: Any
    input: Any
    output: Any


def _json_blob(ref: ContentBlobRef, resolver: ContentResolver, field: str) -> Any:
    data = resolver.get(ref)
    if hashlib.sha256(data).hexdigest() != ref.sha256:
        raise BlobCorrupted(f"{field}: the resolver returned bytes that do not match the ref")
    if ref.byte_size is not None and ref.byte_size != len(data):
        raise BlobCorrupted(f"{field}: byte_size {ref.byte_size} != {len(data)}")
    if ref.media_type is not None and ref.media_type != JSON_MEDIA_TYPE:
        raise BlobCorrupted(f"{field}: media type {ref.media_type!r} is not {JSON_MEDIA_TYPE}")
    try:
        payload = json.loads(data.decode("utf-8"))
        canonical = canonical_json(payload).encode("utf-8")
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise BlobCorrupted(f"{field}: not JSON: {exc}") from None
    if canonical != data:
        raise BlobCorrupted(f"{field}: not canonical JSON")
    return payload


def verify_llm_call(call: LlmCall, resolver: ContentResolver) -> LlmCallContent:
    """Resolve and verify prompt / input / output of ``call``; raises ``ContentStoreError``."""
    if not isinstance(call, LlmCall):
        raise ContentStoreError("verify_llm_call needs an LlmCall")
    return LlmCallContent(
        prompt=_json_blob(call.prompt, resolver, "prompt"),
        input=_json_blob(call.input, resolver, "input"),
        output=_json_blob(call.output, resolver, "output"),
    )

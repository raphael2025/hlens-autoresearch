"""Write-once, content-addressed JSON blob store of the Strategy Registry (2026-09-26).

ADR-0005 §7 puts artifact storage in "Control Plane + object storage, not in Git". Until the
object-storage decision (D-01 / D-02) this is a local directory: one file per blob, named by the
SHA-256 of its bytes, the bytes being the canonical JSON (``core.domain.base.canonical_json``) of
the payload. URIs are ``registry-blob:sha256:<hex>`` — an identifier inside this store, not a
storage-scheme decision (``GoldenOutputs.*_uri`` is deliberately not tightened, ADR-0015 §D-21.3).

- ``put`` writes to a private temporary file, ``fsync`` s it, then ``os.link`` s it to its final
  name (never overwrites); a blob already present must hold identical bytes, else
  ``BlobCorrupted``.
- ``get`` re-hashes the bytes against both the URI and the caller's expected hash, requires a
  regular file (no symlink), and requires the JSON to re-encode to the exact stored bytes.
- There is no delete and no overwrite.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from core.domain.base import canonical_json
from infrastructure.event_bus.journal import fsync_directory

__all__ = ["BLOB_URI_PREFIX", "BlobCorrupted", "BlobMissing", "BlobStore", "blob_uri"]

BLOB_URI_PREFIX: Final = "registry-blob:sha256:"
_HEX = frozenset("0123456789abcdef")


class BlobCorrupted(RuntimeError):
    """A stored blob does not hash to its name, is not canonical JSON, or is not a regular file."""


class BlobMissing(LookupError):
    """No blob is stored under this URI (or the URI is not a registry blob URI)."""


def blob_uri(digest: str) -> str:
    return f"{BLOB_URI_PREFIX}{digest}"


def _digest_of(uri: str) -> str:
    if not uri.startswith(BLOB_URI_PREFIX):
        raise BlobMissing(f"{uri!r} is not a registry blob URI")
    digest = uri.removeprefix(BLOB_URI_PREFIX)
    if len(digest) != 64 or not set(digest) <= _HEX:
        raise BlobMissing(f"{uri!r} does not name a SHA-256 blob")
    return digest


class BlobStore:
    """One directory of write-once blobs (see module docs)."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, digest: str) -> Path:
        return self._root / f"{digest}.json"

    def _read_bytes(self, digest: str) -> bytes:
        path = self._path(digest)
        try:
            info = os.lstat(path)
        except FileNotFoundError as exc:
            raise BlobMissing(f"no blob {digest}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise BlobCorrupted(f"blob {digest} is not a regular file")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise BlobCorrupted(f"blob {digest} does not hash to its name")
        return data

    def put(self, payload: Mapping[str, Any]) -> tuple[str, str]:
        """Store ``payload``; returns ``(uri, sha256)``. Idempotent for identical content."""
        data = canonical_json(dict(payload)).encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        if self._path(digest).exists() or self._path(digest).is_symlink():
            if self._read_bytes(digest) != data:  # pragma: no cover - a SHA-256 collision
                raise BlobCorrupted(f"blob {digest} holds different bytes")
            return blob_uri(digest), digest
        self._root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=self._root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp, self._path(digest))
            except FileExistsError:  # a concurrent identical put won the race
                if self._read_bytes(digest) != data:  # pragma: no cover - collision
                    raise BlobCorrupted(f"blob {digest} holds different bytes") from None
        finally:
            os.unlink(tmp)
        fsync_directory(self._root)
        return blob_uri(digest), digest

    def get(self, uri: str, expected_hash: str) -> Any:
        """The payload stored under ``uri``; it must hash to ``expected_hash``."""
        digest = _digest_of(uri)
        if digest != expected_hash:
            raise BlobCorrupted(f"{uri} is bound to hash {expected_hash}, not to its own name")
        data = self._read_bytes(digest)
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BlobCorrupted(f"blob {digest} is not JSON") from exc
        if canonical_json(payload).encode("utf-8") != data:
            raise BlobCorrupted(f"blob {digest} is not canonical JSON")
        return payload

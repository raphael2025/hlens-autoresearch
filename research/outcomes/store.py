"""Content-addressed, write-once file store for ``OutcomeTable`` (Phase 4, ADR-0037 known gap
"the Outcome table is not persisted"; debugging pass, 2026-09-26; CODE_COMPLETE / DEBUG_PENDING).

One table = one file ``<root>/<table_hash>.json`` holding the canonical JSON
(``core.domain.base.canonical_json``: sorted keys, no whitespace, no NaN / Infinity) of::

    {"kind": "research.outcome_table", "format_version": "1.0.0",
     "outcome": <Ref>, "label_spec_hash", "provider", "provider_hash", "request_hash",
     "result_hash", "labels": [<OutcomeLabel>...], "table_hash"}

``table_hash`` is the SHA-256 of the canonical JSON of the same object **without** ``table_hash``.
Only a table that came out of ``materialize`` (``request_hash`` / ``provider_hash`` present, the
``OutcomeResult`` it describes rebuilds with a matching ``result_hash``) can be stored.

Writing (``OutcomeTableStore.put``): the bytes go to a fresh temporary file in ``root``
(``O_EXCL | O_NOFOLLOW``, write-all, ``fsync``), are published with ``os.link`` (never
``rename`` over, never overwrite), the temporary name is removed and the directory is ``fsync``-ed;
the published file is then read back and compared byte for byte. If the final name already exists
it must hold **exactly** the same bytes (an identical rewrite is a no-op that leaves the file
untouched); anything else is ``OutcomeTableConflict`` and the existing file is kept as it is.

Reading (``OutcomeTableStore.get``): the file is read without following a symlink, parsed strictly
(duplicate keys, NaN / Infinity and non-canonical text refused), its key set and format checked,
``table_hash`` recomputed and compared with the file name and the embedded value, every label
re-validated as an ``OutcomeLabel`` and the ``OutcomeResult`` rebuilt so that its own
``result_hash`` check runs again. Any mismatch — a tampered value, a truncated file, an edited
hash — is ``OutcomeTableCorrupted``: refused, never repaired.

Outcomes stay labels (Constitution C-L2): the store takes and returns ``OutcomeTable`` only; a
loaded table offers exactly what a materialized one offers (``known_as_of`` etc.) and nothing that
builds an input DTO. Research code, not production (H5); ``root`` is the caller's directory
(tests use a temporary one — never commit market-derived files, H9).
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Final

from core.contracts.outcome import OutcomeLabel, OutcomeResult
from core.domain.base import Ref, canonical_json, content_hash
from research.outcomes.table import OutcomeTable

__all__ = [
    "FORMAT_VERSION",
    "TABLE_KIND",
    "OutcomeTableConflict",
    "OutcomeTableCorrupted",
    "OutcomeTableStore",
    "table_hash",
]

TABLE_KIND: Final = "research.outcome_table"
FORMAT_VERSION: Final = "1.0.0"
_KEYS: Final = frozenset(
    {
        "kind",
        "format_version",
        "outcome",
        "label_spec_hash",
        "provider",
        "provider_hash",
        "request_hash",
        "result_hash",
        "labels",
        "table_hash",
    }
)
_HASH: Final = re.compile(r"^[0-9a-f]{64}$")
_READ_FLAGS: Final = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_WRITE_FLAGS: Final = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class OutcomeTableCorrupted(ValueError):
    """A stored table does not verify (tampered, truncated, non-canonical or mis-named)."""


class OutcomeTableConflict(ValueError):
    """The table's file already exists with different bytes; it is never overwritten."""


def _result(table: OutcomeTable) -> OutcomeResult:
    """The ``OutcomeResult`` a stored table describes; its validator re-checks ``result_hash``."""
    if table.request_hash is None or table.provider_hash is None:
        raise ValueError(
            "only a materialized OutcomeTable (with request_hash / provider_hash) can be stored"
        )
    if not isinstance(table.label_spec_hash, str) or not _HASH.fullmatch(table.label_spec_hash):
        raise ValueError("label_spec_hash must be a 64-character lowercase hex content hash")
    return OutcomeResult(
        request_hash=table.request_hash,
        provider=table.provider,
        provider_hash=table.provider_hash,
        labels=table.labels,
        result_hash=table.result_hash,
    )


def _payload(table: OutcomeTable) -> dict[str, Any]:
    _result(table)
    return {
        "kind": TABLE_KIND,
        "format_version": FORMAT_VERSION,
        "outcome": table.outcome.model_dump(mode="json"),
        "label_spec_hash": table.label_spec_hash,
        "provider": table.provider,
        "provider_hash": table.provider_hash,
        "request_hash": table.request_hash,
        "result_hash": table.result_hash,
        "labels": [label.model_dump(mode="json") for label in table.labels],
    }


def table_hash(table: OutcomeTable) -> str:
    """The content hash naming ``table``'s file (module docs)."""
    return content_hash(_payload(table))


def _document(table: OutcomeTable) -> tuple[str, bytes]:
    payload = _payload(table)
    digest = content_hash(payload)
    return digest, canonical_json({**payload, "table_hash": digest}).encode("utf-8")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise OutcomeTableCorrupted("duplicate key in a stored outcome table")
    return dict(pairs)


def _refuse_constant(name: str) -> Any:
    raise OutcomeTableCorrupted(f"{name} is not valid in a stored outcome table")


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(fd, 1 << 16):
        chunks.append(chunk)
    return b"".join(chunks)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write while storing an outcome table")
        view = view[written:]


class OutcomeTableStore:
    """``put`` / ``get`` of ``OutcomeTable`` files under one directory (module docs)."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def path_of(self, digest: str) -> Path:
        if not isinstance(digest, str) or _HASH.fullmatch(digest) is None:
            raise ValueError(f"not a table hash: {digest!r}")
        return self._root / f"{digest}.json"

    def _read(self, path: Path) -> bytes:
        fd = os.open(path, _READ_FLAGS)
        try:
            return _read_all(fd)
        finally:
            os.close(fd)

    def put(self, table: OutcomeTable) -> str:
        """Store ``table`` once; return its ``table_hash`` (an identical rewrite is a no-op)."""
        digest, data = _document(table)
        final = self.path_of(digest)
        self._root.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(final):
            self._same_or_conflict(final, data)
            return digest
        temp = self._root / f".tmp-{digest}-{secrets.token_hex(8)}"
        fd = os.open(temp, _WRITE_FLAGS, 0o644)
        try:
            try:
                _write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(temp, final, follow_symlinks=False)
            except FileExistsError:
                self._same_or_conflict(final, data)
                return digest
        finally:
            temp.unlink(missing_ok=True)
        self._fsync_root()
        if self._read(final) != data:
            raise OutcomeTableCorrupted(f"{final.name} does not hold the bytes just written")
        return digest

    def _same_or_conflict(self, final: Path, data: bytes) -> None:
        try:
            existing = self._read(final)
        except OSError as error:
            raise OutcomeTableConflict(f"{final.name} exists and cannot be read: {error}") from None
        if existing != data:
            raise OutcomeTableConflict(f"{final.name} already exists with different content")

    def _fsync_root(self) -> None:
        fd = os.open(self._root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def get(self, digest: str) -> OutcomeTable:
        """Load and fully re-verify the table named ``digest`` (module docs)."""
        path = self.path_of(digest)
        try:
            data = self._read(path)
        except FileNotFoundError:
            raise FileNotFoundError(f"no stored outcome table {digest}") from None
        except OSError as error:
            raise OutcomeTableCorrupted(f"{path.name} cannot be read safely: {error}") from None
        return _decode(digest, data)


def _decode(digest: str, data: bytes) -> OutcomeTable:
    try:
        text = data.decode("utf-8")
        raw = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_refuse_constant)
    except OutcomeTableCorrupted:
        raise
    except ValueError as error:  # UnicodeDecodeError, JSONDecodeError (e.g. truncated)
        raise OutcomeTableCorrupted(f"stored outcome table {digest} is not valid JSON") from error
    if not isinstance(raw, dict) or set(raw) != _KEYS:
        raise OutcomeTableCorrupted(f"stored outcome table {digest} has the wrong fields")
    if (raw["kind"], raw["format_version"]) != (TABLE_KIND, FORMAT_VERSION):
        raise OutcomeTableCorrupted(f"stored outcome table {digest} has an unknown format")
    if canonical_json(raw) != text:
        raise OutcomeTableCorrupted(f"stored outcome table {digest} is not canonical JSON")
    payload = {key: value for key, value in raw.items() if key != "table_hash"}
    if not (raw["table_hash"] == digest == content_hash(payload)):
        raise OutcomeTableCorrupted(f"stored outcome table {digest} does not match its hash")
    if not isinstance(raw["labels"], list):
        raise OutcomeTableCorrupted(f"stored outcome table {digest} has no label list")
    try:
        labels = tuple(OutcomeLabel.model_validate(item) for item in raw["labels"])
        table = OutcomeTable(
            outcome=Ref.model_validate(raw["outcome"]),
            label_spec_hash=raw["label_spec_hash"],
            provider=raw["provider"],
            result_hash=raw["result_hash"],
            labels=labels,
            request_hash=raw["request_hash"],
            provider_hash=raw["provider_hash"],
        )
        rebuilt_hash, rebuilt = _document(table)  # rebuilds the OutcomeResult (result_hash)
    except (ValueError, TypeError) as error:
        raise OutcomeTableCorrupted(f"stored outcome table {digest} does not verify") from error
    if rebuilt_hash != digest or rebuilt != data:
        raise OutcomeTableCorrupted(f"stored outcome table {digest} does not round-trip")
    return table

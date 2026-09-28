"""Local content-addressed artifact store for complete StateResult objects (ADR-0089)."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from core.contracts.state import StateResult
from core.domain.base import canonical_json, contract_schema_version_scope

__all__ = ["StateResultStore", "StateStoreCorrupted"]

_HASH_RE: Final = re.compile(r"^[0-9a-f]{64}$")


class StateStoreCorrupted(RuntimeError):
    """A local StateResult artifact does not match its name or canonical content."""


def _encode(result: StateResult) -> bytes:
    return (canonical_json(result.model_dump(mode="json")) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class StateResultStore:
    """Store full state runs by result hash; this is an artifact store, not the Iceberg table."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, result_hash: str) -> Path:
        if not isinstance(result_hash, str) or _HASH_RE.fullmatch(result_hash) is None:
            raise ValueError("result_hash must be 64 lowercase hexadecimal characters")
        return self._root / f"{result_hash}.json"

    def put(self, result: StateResult) -> Path:
        if not isinstance(result, StateResult):
            raise TypeError("put needs a StateResult")
        path = self._path(result.result_hash)
        data = _encode(result)
        if path.exists():
            if path.read_bytes() != data:
                raise StateStoreCorrupted(f"{path} exists with other content; never overwritten")
            return path
        temporary_file = tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=self._root,
            delete=False,
        )
        temporary = Path(temporary_file.name)
        try:
            with temporary_file as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise StateStoreCorrupted(f"{path} appeared with other content") from None
        finally:
            temporary.unlink(missing_ok=True)
        _fsync_directory(self._root)
        return path

    def get(self, result_hash: str) -> StateResult:
        path = self._path(result_hash)
        if not path.exists():
            raise KeyError(result_hash)
        data = path.read_bytes()
        try:
            document = json.loads(data)
            if not isinstance(document, dict):
                raise ValueError("StateResult JSON must be an object")
            version = document.get("schema_version")
            if not isinstance(version, str):
                raise ValueError("StateResult has no schema_version")
            with contract_schema_version_scope(version):
                result = StateResult.model_validate(document)
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StateStoreCorrupted(f"{path} is not a valid StateResult") from exc
        if result.result_hash != result_hash:
            raise StateStoreCorrupted(f"{path} holds state run {result.result_hash}")
        if _encode(result) != data:
            raise StateStoreCorrupted(f"{path} is not the canonical form of its StateResult")
        return result

    def hashes(self) -> tuple[str, ...]:
        names: list[str] = []
        for path in sorted(self._root.iterdir()):
            if path.name.startswith(".") and path.name.endswith(".tmp"):
                continue
            stem = path.name.removesuffix(".json")
            if not path.is_file() or path.suffix != ".json" or _HASH_RE.fullmatch(stem) is None:
                raise StateStoreCorrupted(f"{path} is not a stored StateResult")
            names.append(stem)
        return tuple(names)

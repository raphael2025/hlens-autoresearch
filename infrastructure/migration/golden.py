"""Golden experiment reruns (Phase 14; 10-migration.md §2, roadmap P14 acceptance).

A migration (new compute engine, backtest engine, catalog, ...) must reproduce the gold-standard
experiments: ``record_golden`` freezes a run's named outputs (``Decimal`` values, canonical JSON
hash); ``compare_golden`` reruns and reports every output that differs beyond the migration's
declared tolerance (bit-exact when the tolerance is zero). The tolerance is declared by the
migration's own ADR; it is not a validation threshold (Constitution / Profile untouched).

Persistence: ``save_golden`` writes a record content-addressed as ``<record_hash>.json``
(canonical JSON; ``record_hash`` is the SHA-256 of the file bytes) with a write-once, no-clobber
publish; ``load_golden`` re-hashes the file, re-derives ``outputs_hash`` and refuses any mismatch,
so a golden record cannot be edited silently. ``GoldenDiff.report()`` renders a deterministic
plain-text rerun report (the golden rerun report a migration ADR cites).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from core.domain.base import canonical_json, content_hash

__all__ = [
    "GoldenDiff",
    "GoldenError",
    "GoldenRecord",
    "compare_golden",
    "load_golden",
    "record_golden",
    "save_golden",
]

_ZERO: Final = Decimal(0)
_FORMAT: Final = "hlens.golden_record"
_SCHEMA_VERSION: Final = "1.0.0"
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_RECORD_KEYS: Final = frozenset({"format", "schema_version", "name", "outputs", "outputs_hash"})


class GoldenError(ValueError):
    """The golden record or the rerun cannot be compared (shape, types)."""


@dataclass(frozen=True, slots=True)
class GoldenRecord:
    name: str
    outputs: Mapping[str, Decimal]
    outputs_hash: str

    def _payload(self) -> dict[str, Any]:
        return {
            "format": _FORMAT,
            "schema_version": _SCHEMA_VERSION,
            "name": self.name,
            "outputs": {key: str(value) for key, value in sorted(self.outputs.items())},
            "outputs_hash": self.outputs_hash,
        }

    @property
    def record_hash(self) -> str:
        """SHA-256 of the saved file (canonical JSON of name, outputs and outputs hash)."""
        return content_hash(self._payload())


@dataclass(frozen=True, slots=True)
class GoldenDiff:
    name: str
    tolerance: Decimal
    bit_identical: bool
    #: output name -> (golden, rerun) for every value beyond the tolerance or missing / extra.
    differences: Mapping[str, tuple[Decimal | None, Decimal | None]]
    #: ``outputs_hash`` of the golden record and of the rerun (equal iff bit-identical).
    golden_hash: str = ""
    rerun_hash: str = ""

    @property
    def passed(self) -> bool:
        return not self.differences

    def report(self) -> str:
        """Deterministic plain-text rerun report (the same diff always renders the same text)."""

        def show(value: Decimal | None) -> str:
            return "<missing>" if value is None else str(value)

        lines = [
            f"golden rerun report: {self.name}",
            f"tolerance: {self.tolerance}",
            f"golden outputs_hash: {self.golden_hash}",
            f"rerun outputs_hash: {self.rerun_hash}",
            f"bit_identical: {'yes' if self.bit_identical else 'no'}",
            f"verdict: {'PASS' if self.passed else 'FAIL'}",
            f"differences: {len(self.differences)}",
        ]
        for key in sorted(self.differences):
            old, new = self.differences[key]
            delta = "" if old is None or new is None else f" delta={new - old}"
            lines.append(f"  {key}: golden={show(old)} rerun={show(new)}{delta}")
        return "\n".join(lines) + "\n"


def _check(outputs: Mapping[str, Decimal]) -> dict[str, Decimal]:
    checked: dict[str, Decimal] = {}
    for key, value in outputs.items():
        if not isinstance(key, str) or not key:
            raise GoldenError("golden output names must be non-empty strings")
        if not isinstance(value, Decimal) or not value.is_finite():
            raise GoldenError(f"golden output {key!r} must be a finite Decimal")
        checked[key] = value
    return checked


def _hash(outputs: Mapping[str, Decimal]) -> str:
    return content_hash({key: str(value) for key, value in sorted(outputs.items())})


def record_golden(name: str, run: Callable[[], Mapping[str, Decimal]]) -> GoldenRecord:
    outputs = _check(run())
    return GoldenRecord(name=name, outputs=dict(outputs), outputs_hash=_hash(outputs))


def compare_golden(
    golden: GoldenRecord, rerun: Callable[[], Mapping[str, Decimal]], tolerance: Decimal
) -> GoldenDiff:
    if not isinstance(tolerance, Decimal) or not tolerance.is_finite() or tolerance < _ZERO:
        raise GoldenError("tolerance must be a finite, non-negative Decimal")
    outputs = _check(rerun())
    differences: dict[str, tuple[Decimal | None, Decimal | None]] = {}
    for key in sorted(set(golden.outputs) | set(outputs)):
        old, new = golden.outputs.get(key), outputs.get(key)
        if old is None or new is None or abs(old - new) > tolerance:
            differences[key] = (old, new)
    return GoldenDiff(
        name=golden.name,
        tolerance=tolerance,
        bit_identical=_hash(outputs) == golden.outputs_hash,
        differences=differences,
        golden_hash=golden.outputs_hash,
        rerun_hash=_hash(outputs),
    )


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_golden(record: GoldenRecord, directory: Path) -> Path:
    """Write ``<record_hash>.json`` once; an identical re-save is a no-op, never an overwrite."""
    if not isinstance(record, GoldenRecord) or not isinstance(record.name, str) or not record.name:
        raise GoldenError("save_golden needs a named GoldenRecord")
    if _hash(_check(record.outputs)) != record.outputs_hash:
        raise GoldenError(f"{record.name}: outputs_hash does not match the outputs")
    data = canonical_json(record._payload()).encode("utf-8")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{record.record_hash}.json"
    if target.exists():
        if target.read_bytes() != data:
            raise GoldenError(f"{target.name}: a different file already sits at this address")
        return target
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, target)
        except FileExistsError:
            if target.read_bytes() != data:
                raise GoldenError(f"{target.name}: a different file already sits here") from None
            return target
        _fsync_directory(directory)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def load_golden(directory: Path, record_hash: str) -> GoldenRecord:
    """Load and verify a saved record; any tampering raises ``GoldenError``."""
    if not isinstance(record_hash, str) or not _HEX64.fullmatch(record_hash):
        raise GoldenError(f"not a golden record hash: {record_hash!r}")
    path = Path(directory) / f"{record_hash}.json"
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GoldenError(f"{path.name}: unreadable golden record: {exc}") from None
    if hashlib.sha256(data).hexdigest() != record_hash:
        raise GoldenError(f"{path.name}: the file does not hash to its name")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GoldenError(f"{path.name}: not JSON: {exc}") from None
    if (
        not isinstance(raw, dict)
        or set(raw) != _RECORD_KEYS
        or raw["format"] != _FORMAT
        or raw["schema_version"] != _SCHEMA_VERSION
        or not isinstance(raw["name"], str)
        or not raw["name"]
        or not isinstance(raw["outputs"], dict)
        or not all(isinstance(value, str) for value in raw["outputs"].values())
    ):
        raise GoldenError(f"{path.name}: not a golden record")
    try:
        outputs = _check({key: Decimal(value) for key, value in raw["outputs"].items()})
    except InvalidOperation:
        raise GoldenError(f"{path.name}: an output is not a decimal") from None
    if _hash(outputs) != raw["outputs_hash"]:
        raise GoldenError(f"{path.name}: outputs_hash does not match the outputs")
    record = GoldenRecord(name=raw["name"], outputs=outputs, outputs_hash=raw["outputs_hash"])
    if record.record_hash != record_hash:
        raise GoldenError(f"{path.name}: not in canonical form")
    return record

"""File-backed, append-only Validation Profile freeze registry (ADR-0062; B56, 2026-09-27).

**Why.** ``ValidationProfile.status`` is an operational state and is excluded from the Profile's
content hash (ADR-0008 decision 3), so a Profile object saying ``status = FROZEN`` proves nothing:
any caller can build one. This registry is the authoritative, replayable record that a named person
approved freezing one exact Profile (ref + content hash) on one exact calibration report. Promotion
(``research.promotion``) requires a record here (ADR-0062 decision 5).

**Placement.** Like ``infrastructure.registry.registry.StrategyRegistry`` (ADR-0005 implementation
note), this is a file-backed stand-in for Control Plane storage; it imports only ``core`` and
``infrastructure`` (never ``research`` or ``apps``). It is a separate registry (ADR-0062 Q1 = A):
it composes the existing ``AppendOnlyJournal``, ``BlobStore``, an ``fcntl.flock`` and an anchor
journal directly and does not touch ``StrategyRegistry``.

**On disk** (``root``): ``freezes.jsonl`` — the hash-chained journal (shared on-disk contract of
``infrastructure.event_bus.journal``); ``blobs/`` — the calibration reports' original bytes,
write-once, SHA-256-named; ``.lock`` — the single-writer lock (``RegistryLocked``; POSIX only).
The **anchor** (``anchor=``, a path outside ``root``) is **mandatory**: a hash-chained
``(length, head)`` journal appended after every record. Whole trailing lines of the registry can be
dropped without breaking its chain; only the anchor detects that, so an unanchored registry is not
evidence Promotion may rely on (ADR-0062 decision 1).

**The one record** (``profile.frozen``; its payload has exactly these keys):

- ``format_version``: ``"1.0.0"``;
- ``profile``: the Profile's ``model_dump(mode="json")``; ``profile_ref``: ``kind:name@version``;
  ``profile_hash``: its ``content_hash()``;
- ``calibration``: ``{"kind": "gate_calibration", "report_hash", "sha256", "uri"}`` — the
  report's kind and self-hash, the SHA-256 of its original bytes and their blob URI;
- ``approved_by``: the named approver (non-blank, no surrounding whitespace);
- ``approved_at``: the approval time, UTC, ``YYYY-MM-DDTHH:MM:SS[.ffffff]Z``;
- ``freeze_id``: ``content_hash`` of every other key.

**Rules** — the same on append and on replay; a failure on append is ``RegistryRefused`` /
``DuplicateRecord`` / ``FreezeConflict`` (nothing written), on open ``RegistryCorrupted`` (the
registry cannot be opened at all): an unknown record type, extra / missing keys, another
``format_version``, a ``freeze_id`` that is not the content hash, a Profile that does not validate,
is not ``FROZEN``, or whose ref / hash / re-dumped JSON differ from the record; a calibration kind
other than ``gate_calibration``; ``provenance.calibration_report`` not **exactly** the report hash;
a calibration blob that is missing, tampered, not canonical JSON, of another kind, or whose
``report_hash`` is not the content hash of the rest of the report (ADR-0062 decision 3 — the
report's ``candidate_profiles`` are deliberately not required to contain the frozen Profile); a
blank approver or a non-UTC time; the same Profile (ref and hash) twice (``DuplicateRecord``); a
ref already frozen under another hash (``FreezeConflict``). Blobs are re-verified on replay.
Tampering, a broken chain, a partial trailing line (a crash mid-write) or a shrunken file are
``RegistryCorrupted`` via the journal. There is no edit, delete, unfreeze or supersede operation.

**Append path.** ``register_freeze`` runs every rule — including status, provenance, duplicate /
conflict and the calibration report's kind and self-hash, checked on the given bytes — **before
anything is written**, so a refused registration leaves the journal, the anchor and ``blobs/``
unchanged. Only then does it store the report blob, append the journal record, append the anchor
and, last, admit the freeze in memory. If anything fails once the disk may have changed (the blob,
the journal record or the anchor), the instance is **poisoned** and closed: every read that could
carry authority (``frozen_record``, ``freeze_of``, ``freezes``, ``len``) and every further write
raise ``RegistryCorrupted`` until a fresh ``ProfileFreezeRegistry`` replays and verifies the disk —
a record fully written but not anchored is the one crash window and is re-anchored; a partial
trailing line makes the registry unopenable. The in-memory view is never used after such a
failure.

**Honest boundary** (ADR-0062 decision 6): ``approved_by`` is a declared name — the registry does
not authenticate an operating-system user or prove the approver's authority. It is not the
production Control Plane and grants no live trading, funds or production-deployment authority. It
freezes no Profile values: a real record may only be written on Raphael's freeze decision (D-09).
Rolling the anchor back together with the registry is still undetectable: the anchor must live on
storage the registry's writer cannot roll back.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from pydantic import ValidationError

from core.contracts.validation_profile import ProfileStatus, ValidationProfile
from core.domain.base import Ref, canonical_json, content_hash
from infrastructure.event_bus.journal import AppendOnlyJournal, JournalCorrupted
from infrastructure.registry.blobs import BlobCorrupted, BlobMissing, BlobStore, blob_uri
from infrastructure.registry.registry import (
    DuplicateRecord,
    RegistryCorrupted,
    RegistryError,
    RegistryLocked,
    RegistryRefused,
)

__all__ = [
    "CALIBRATION_KIND",
    "FORMAT_VERSION",
    "PROFILE_FROZEN",
    "FreezeConflict",
    "ProfileFreeze",
    "ProfileFreezeRegistry",
    "verify_integrity_snapshot",
]

PROFILE_FROZEN: Final = "profile.frozen"
FORMAT_VERSION: Final = "1.0.0"
CALIBRATION_KIND: Final = "gate_calibration"
_ANCHOR_TYPE: Final = "profile_freeze.head"
_KEYS: Final = frozenset(
    {
        "format_version",
        "freeze_id",
        "profile",
        "profile_ref",
        "profile_hash",
        "calibration",
        "approved_by",
        "approved_at",
    }
)
_CALIBRATION_KEYS: Final = frozenset({"kind", "report_hash", "sha256", "uri"})
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


class FreezeConflict(RegistryRefused):
    """The Profile ref is already frozen under another content hash; nothing was written."""


@dataclass(frozen=True)
class ProfileFreeze:
    """One verified ``profile.frozen`` record."""

    freeze_id: str
    profile_ref: str
    profile_hash: str
    report_hash: str
    report_sha256: str
    report_uri: str
    approved_by: str
    approved_at: datetime


def _utc_text(moment: datetime) -> str:
    """The one accepted rendering of an approval time (UTC, ``Z`` suffix)."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise RegistryRefused("approved_at must be a string")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RegistryRefused(f"approved_at {value!r} is not an ISO 8601 time") from exc
    if moment.tzinfo is None or moment.utcoffset() != UTC.utcoffset(None):
        raise RegistryRefused(f"approved_at {value!r} is not UTC")
    if _utc_text(moment) != value:
        raise RegistryRefused(f"approved_at {value!r} is not in the canonical UTC form")
    return moment


def _check_approver(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise RegistryRefused("approved_by must name an approver (non-blank, no outer whitespace)")
    return value


def _check_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise RegistryRefused(f"{label} must be a lowercase SHA-256 hex string")
    return value


def _check_report(report: object, report_hash: str) -> None:
    """A calibration report's kind and self-hash (ADR-0062 decision 3)."""
    if not isinstance(report, dict):
        raise RegistryRefused("the calibration report is not a JSON object")
    if report.get("kind") != CALIBRATION_KIND:
        raise RegistryRefused(f"the calibration report's kind is not {CALIBRATION_KIND!r}")
    if report.get("report_hash") != report_hash:
        raise RegistryRefused("the calibration report does not carry the recorded report_hash")
    body = {key: value for key, value in report.items() if key != "report_hash"}
    if content_hash(body) != report_hash:
        raise RegistryRefused("the calibration report's report_hash is not its content hash")


def _profile_of(raw: object) -> ValidationProfile:
    try:
        profile = ValidationProfile.model_validate_json(canonical_json(raw))
    except (ValidationError, TypeError, ValueError) as exc:
        raise RegistryRefused(f"the recorded Profile does not validate: {exc}") from exc
    if profile.model_dump(mode="json") != raw:
        raise RegistryRefused("the recorded Profile is not its own canonical JSON")
    return profile


class ProfileFreezeRegistry:
    """The append-only Profile freeze registry (see module docs). Use as a context manager or
    ``close()`` it to release the single-writer lock. ``anchor`` is mandatory."""

    def __init__(self, root: Path, *, anchor: Path) -> None:
        self._root = Path(root)
        if anchor is None:  # a runtime guard: the anchor is mandatory (ADR-0062 decision 1)
            raise ValueError("a Profile freeze registry needs an anchor outside its directory")
        anchor_path = Path(anchor)
        if anchor_path.resolve().is_relative_to(self._root.resolve()):
            raise ValueError("the freeze registry anchor must live outside the registry directory")
        self._root.mkdir(parents=True, exist_ok=True)
        #: Why this instance may no longer be trusted (a failed append; see module docs).
        self._poisoned: str | None = None
        self._lock_fd: int | None = os.open(self._root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise RegistryLocked(f"{self._root} is open elsewhere") from exc
        try:
            self._blobs = BlobStore(self._root / "blobs")
            self._by_ref: dict[str, ProfileFreeze] = {}
            self._order: list[str] = []
            try:
                self._journal = AppendOnlyJournal(self._root / "freezes.jsonl")
            except JournalCorrupted as exc:
                raise RegistryCorrupted(f"freeze journal: {exc}") from exc
            for entry in self._journal.entries:
                try:
                    self._apply(entry.type, entry.payload)()
                except (RegistryRefused, ValidationError, ValueError) as exc:
                    raise RegistryCorrupted(f"freeze record {entry.seq}: {exc}") from exc
            self._anchor = self._open_anchor(anchor_path)
        except BaseException:
            self.close()
            raise

    # ---- lifecycle ----------------------------------------------------------------------

    def close(self) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._poisoned is not None:
            raise RegistryCorrupted(
                f"this freeze registry instance is poisoned ({self._poisoned}); open a new one to "
                "replay and verify the disk"
            )
        if self._lock_fd is None:
            raise RegistryError("the freeze registry is closed")

    def _poison(self, reason: str) -> None:
        self._poisoned = reason
        self.close()

    # ---- anchor (mandatory) -------------------------------------------------------------

    def _open_anchor(self, path: Path) -> AppendOnlyJournal:
        try:
            anchor = AppendOnlyJournal(path)
        except JournalCorrupted as exc:
            raise RegistryCorrupted(f"freeze registry anchor: {exc}") from exc
        length, head = 0, ""
        for entry in anchor.entries:
            payload = entry.payload
            if entry.type != _ANCHOR_TYPE or set(payload) != {"length", "head"}:
                raise RegistryCorrupted(f"anchor record {entry.seq} is not a head record")
            new_length, new_head = payload["length"], payload["head"]
            if not isinstance(new_length, int) or isinstance(new_length, bool) or new_length < 1:
                raise RegistryCorrupted(f"anchor record {entry.seq} has a bad length")
            if new_length <= length or not isinstance(new_head, str):
                raise RegistryCorrupted(f"anchor record {entry.seq} goes backwards")
            length, head = new_length, new_head
        have = len(self._journal)
        if have < length:
            raise RegistryCorrupted(
                f"the freeze registry was anchored at {length} records and now has {have}: "
                "records were removed"
            )
        if length and self._journal.entry(length - 1).hash != head:
            raise RegistryCorrupted("the freeze registry is not the history its anchor saw")
        if have > length + 1:
            raise RegistryCorrupted(
                f"the freeze registry has {have} records but its anchor saw {length}: records "
                "were written without the anchor"
            )
        if have == length + 1:  # the one legitimate crash window: record fsync'd, anchor not
            self._write_anchor(anchor)
        return anchor

    def _write_anchor(self, anchor: AppendOnlyJournal) -> None:
        try:
            anchor.append(
                _ANCHOR_TYPE, {"length": len(self._journal), "head": self._journal.head_hash}
            )
        except JournalCorrupted as exc:
            raise RegistryCorrupted(f"freeze registry anchor: {exc}") from exc

    # ---- rules (shared by append and replay) ----------------------------------------------

    def _apply(
        self, type_: str, payload: Mapping[str, Any], *, report: object = None
    ) -> Callable[[], None]:
        """Check one record against every rule; returns the commit that admits it in memory.

        ``report`` (register only): the calibration report parsed from the given bytes, checked in
        place of the not-yet-stored blob; on replay (``None``) the blob is read and verified.
        """
        if type_ != PROFILE_FROZEN:
            raise RegistryRefused(f"unknown record type {type_!r}")
        if set(payload) != _KEYS:
            raise RegistryRefused(f"a {PROFILE_FROZEN} record has exactly the keys {sorted(_KEYS)}")
        if payload["format_version"] != FORMAT_VERSION:
            raise RegistryRefused(
                f"format_version {payload['format_version']!r} is not {FORMAT_VERSION!r}"
            )
        body = {key: value for key, value in payload.items() if key != "freeze_id"}
        freeze_id = payload["freeze_id"]
        if freeze_id != content_hash(body):
            raise RegistryRefused("the recorded freeze_id is not the record's content hash")
        profile = _profile_of(payload["profile"])
        if profile.status is not ProfileStatus.FROZEN:
            raise RegistryRefused(f"{profile.ref} is {profile.status!s}, not frozen")
        profile_ref, profile_hash = str(profile.ref), profile.content_hash()
        if payload["profile_ref"] != profile_ref or payload["profile_hash"] != profile_hash:
            raise RegistryRefused("the recorded profile_ref / profile_hash are not the Profile's")
        calibration = payload["calibration"]
        if not isinstance(calibration, dict) or set(calibration) != _CALIBRATION_KEYS:
            raise RegistryRefused(f"calibration has exactly the keys {sorted(_CALIBRATION_KEYS)}")
        if calibration["kind"] != CALIBRATION_KIND:
            raise RegistryRefused(f"the calibration kind is not {CALIBRATION_KIND!r}")
        report_hash = _check_sha256(calibration["report_hash"], "calibration.report_hash")
        sha256 = _check_sha256(calibration["sha256"], "calibration.sha256")
        if calibration["uri"] != blob_uri(sha256):
            raise RegistryRefused("calibration.uri is not the blob URI of calibration.sha256")
        if profile.provenance.calibration_report != report_hash:
            raise RegistryRefused(
                f"{profile_ref} cites calibration report {profile.provenance.calibration_report!r}"
                f", not {report_hash}"
            )
        if report is None:
            try:
                report = self._blobs.get(calibration["uri"], sha256)
            except (BlobMissing, BlobCorrupted) as exc:
                raise RegistryRefused(f"calibration report blob: {exc}") from exc
        _check_report(report, report_hash)
        approved_by = _check_approver(payload["approved_by"])
        approved_at = _parse_utc(payload["approved_at"])
        existing = self._by_ref.get(profile_ref)
        if existing is not None:
            if existing.profile_hash == profile_hash:
                raise DuplicateRecord(f"{profile_ref} ({profile_hash}) is already frozen")
            raise FreezeConflict(
                f"{profile_ref} is already frozen as {existing.profile_hash}, not {profile_hash}"
            )
        freeze = ProfileFreeze(
            freeze_id=freeze_id,
            profile_ref=profile_ref,
            profile_hash=profile_hash,
            report_hash=report_hash,
            report_sha256=sha256,
            report_uri=calibration["uri"],
            approved_by=approved_by,
            approved_at=approved_at,
        )

        def commit() -> None:
            self._by_ref[profile_ref] = freeze
            self._order.append(profile_ref)

        return commit

    # ---- writes -------------------------------------------------------------------------

    def register_freeze(
        self,
        profile: ValidationProfile,
        calibration_report: bytes,
        *,
        approved_by: str,
        approved_at: datetime,
    ) -> ProfileFreeze:
        """Record that ``approved_by`` approved freezing ``profile`` on ``calibration_report``
        (the report file's original bytes) at ``approved_at``. Every rule runs before anything is
        written; the report bytes are stored write-once (see module docs)."""
        self._require_open()
        if not isinstance(profile, ValidationProfile):
            raise RegistryRefused("register_freeze needs a ValidationProfile")
        try:  # no model_construct shortcut
            profile = ValidationProfile.model_validate_json(profile.model_dump_json())
        except ValidationError as exc:
            raise RegistryRefused(f"the Profile does not validate: {exc}") from exc
        if not isinstance(calibration_report, bytes):
            raise RegistryRefused("the calibration report must be given as its original bytes")
        if not isinstance(approved_at, datetime) or approved_at.tzinfo is None:
            raise RegistryRefused("approved_at must be a timezone-aware datetime")
        _check_approver(approved_by)
        try:
            report = json.loads(calibration_report.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegistryRefused("the calibration report is not UTF-8 JSON") from exc
        if not isinstance(report, dict):
            raise RegistryRefused("the calibration report is not a JSON object")
        if canonical_json(report).encode("utf-8") != calibration_report:
            raise RegistryRefused(
                "the calibration report bytes are not canonical JSON (they are stored as given, "
                "never re-serialized)"
            )
        report_hash = report.get("report_hash")
        if not isinstance(report_hash, str):
            raise RegistryRefused("the calibration report has no report_hash")
        sha256 = hashlib.sha256(calibration_report).hexdigest()
        body: dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "profile": profile.model_dump(mode="json"),
            "profile_ref": str(profile.ref),
            "profile_hash": profile.content_hash(),
            "calibration": {
                "kind": CALIBRATION_KIND,
                "report_hash": report_hash,
                "sha256": sha256,
                "uri": blob_uri(sha256),
            },
            "approved_by": approved_by,
            "approved_at": _utc_text(approved_at),
        }
        payload = {**body, "freeze_id": content_hash(body)}
        # every rule — status, provenance, duplicate / conflict, the report — on the given bytes,
        # before anything is written: a refusal leaves the journal, the anchor and blobs/ unchanged
        commit = self._apply(PROFILE_FROZEN, payload, report=report)
        try:
            stored = self._blobs.put(report)
            if stored != (blob_uri(sha256), sha256):  # pragma: no cover - canonical bytes checked
                raise RegistryCorrupted("the blob store stored other bytes than were given")
            self._journal.append(PROFILE_FROZEN, payload)
            self._write_anchor(self._anchor)
        except BaseException as exc:
            # the disk may have changed: never trust this instance's memory again
            self._poison(f"register_freeze failed after writing began: {type(exc).__name__}")
            if isinstance(exc, RegistryError):
                raise
            raise RegistryCorrupted(
                f"register_freeze failed after writing began ({type(exc).__name__}: {exc}); this "
                "instance is closed — open a new one to replay and verify the disk"
            ) from exc
        commit()  # admitted in memory only once the record and its anchor are durable
        return self._by_ref[str(profile.ref)]

    # ---- reads --------------------------------------------------------------------------

    def __len__(self) -> int:
        self._require_open()
        return len(self._journal)

    @property
    def freezes(self) -> tuple[ProfileFreeze, ...]:
        """Every verified record, in journal order."""
        self._require_open()
        return tuple(self._by_ref[ref] for ref in self._order)

    @property
    def anchor_snapshot(self) -> tuple[int, str]:
        """The verified external anchor journal identity: record count and head hash.

        This read-only snapshot lets evidence reports bind to the verified rollback-detection
        state without exposing the anchor path or any profile calibration payload. It reopens the
        journal to detect changes made outside this registry instance; such changes fail closed
        and require the caller to reopen the registry.
        """
        self._require_open()
        try:
            current = AppendOnlyJournal(self._anchor.path)
        except JournalCorrupted as exc:
            raise RegistryCorrupted(f"freeze registry anchor: {exc}") from exc
        if len(current) != len(self._anchor) or current.head_hash != self._anchor.head_hash:
            raise RegistryCorrupted(
                "the freeze registry anchor changed outside this instance; reopen the registry"
            )
        return len(current), current.head_hash

    def freeze_of(self, ref: Ref | str) -> ProfileFreeze | None:
        """The record freezing ``ref`` (any content hash), or ``None``."""
        self._require_open()
        return self._by_ref.get(str(ref))

    def frozen_record(self, profile: ValidationProfile) -> ProfileFreeze | None:
        """The record freezing exactly this Profile — its ref **and** content hash — whose
        calibration report is the one the Profile cites; ``None`` otherwise."""
        self._require_open()
        record = self._by_ref.get(str(profile.ref))
        if record is None or record.profile_hash != profile.content_hash():
            return None
        if record.report_hash != profile.provenance.calibration_report:
            return None
        return record


def verify_integrity_snapshot(root: Path, *, anchor: Path) -> dict[str, object]:
    """Verify a stable Profile Freeze snapshot without creating locks or repairing anchors."""
    root = Path(root)
    journal_path = root / "freezes.jsonl"
    anchor = Path(anchor)
    if not root.is_dir() or not journal_path.is_file():
        raise RegistryCorrupted(f"the Profile Freeze Registry does not exist at {root}")
    resolved_root = root.resolve()
    if anchor.resolve().is_relative_to(resolved_root):
        raise RegistryCorrupted("the freeze registry anchor must live outside its directory")
    if not anchor.is_file():
        raise RegistryCorrupted(f"the Profile Freeze Registry anchor does not exist: {anchor}")

    def signature(path: Path) -> tuple[bytes, tuple[int, int, int, int]]:
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        stamp = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if stamp != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise RegistryCorrupted(f"{path} changed while the audit snapshot was read")
        return data, stamp

    def blob_snapshot() -> tuple[tuple[str, int, int, int, str], ...]:
        blob_root = resolved_root / "blobs"
        if not blob_root.exists():
            return ()
        rows: list[tuple[str, int, int, int, str]] = []
        for path in sorted(blob_root.iterdir()):
            before = path.lstat()
            if not path.is_file() or path.is_symlink():
                raise RegistryCorrupted(f"{path} is not a regular calibration blob")
            data = path.read_bytes()
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise RegistryCorrupted(f"{path} changed while the audit snapshot was read")
            rows.append(
                (
                    path.name,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    hashlib.sha256(data).hexdigest(),
                )
            )
        return tuple(rows)

    journal_bytes, journal_stamp = signature(journal_path)
    anchor_bytes, anchor_stamp = signature(anchor)
    blobs_before = blob_snapshot()
    try:
        journal = AppendOnlyJournal(journal_path)
    except JournalCorrupted as exc:
        raise RegistryCorrupted(f"freeze journal: {exc}") from exc
    try:
        anchor_journal = AppendOnlyJournal(anchor)
    except JournalCorrupted as exc:
        raise RegistryCorrupted(f"freeze registry anchor: {exc}") from exc

    # Replay in an isolated instance. _apply only validates and returns an in-memory commit;
    # no normal constructor, lock file, journal append, or recovery path is involved.
    replay = object.__new__(ProfileFreezeRegistry)
    replay._by_ref = {}
    replay._order = []
    replay._blobs = BlobStore(resolved_root / "blobs")
    for entry in journal.entries:
        try:
            replay._apply(entry.type, entry.payload)()
        except (RegistryRefused, ValidationError, ValueError) as exc:
            raise RegistryCorrupted(f"freeze record {entry.seq}: {exc}") from exc

    length, head = 0, ""
    for entry in anchor_journal.entries:
        payload = entry.payload
        if entry.type != _ANCHOR_TYPE or set(payload) != {"length", "head"}:
            raise RegistryCorrupted(f"anchor record {entry.seq} is not a head record")
        new_length, new_head = payload["length"], payload["head"]
        if (
            not isinstance(new_length, int)
            or isinstance(new_length, bool)
            or new_length <= length
            or not isinstance(new_head, str)
        ):
            raise RegistryCorrupted(f"anchor record {entry.seq} is invalid")
        if new_length > len(journal) or journal.entry(new_length - 1).hash != new_head:
            raise RegistryCorrupted(f"anchor record {entry.seq} does not match its journal prefix")
        length, head = new_length, new_head
    if length != len(journal) or (length and journal.entry(length - 1).hash != head):
        raise RegistryCorrupted("the freeze registry anchor does not match the journal tip")
    if not length and head:
        raise RegistryCorrupted("the empty freeze registry anchor has a non-empty head")

    if signature(journal_path) != (journal_bytes, journal_stamp):
        raise RegistryCorrupted(f"{journal_path} changed during the audit")
    if signature(anchor) != (anchor_bytes, anchor_stamp):
        raise RegistryCorrupted(f"{anchor} changed during the audit")
    if blob_snapshot() != blobs_before:
        raise RegistryCorrupted("the calibration blob store changed during the audit")
    return {
        "status": "OK",
        "evidence": "HASH_CHAIN",
        "path": str(resolved_root),
        "journal_records": len(journal),
        "journal_head_hash": journal.head_hash,
        "anchor_status": "VERIFIED",
        "anchor_path": str(anchor.resolve()),
        "limitations": [
            "rollback of the journal and its external anchor together is not detectable"
        ],
    }

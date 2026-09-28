"""File-backed, append-only, hash-chained Strategy Registry (ADR-0005 §1 / §4; 2026-09-26).

**Placement.** ADR-0005 §1 defines the Strategy Registry as the Control Plane's append-only
register of artifacts and "the only source of what may run"; §7 puts its storage in the Control
Plane (PostgreSQL) + object storage, and 01-system.md §3–§4 puts storage adapters in the
Infrastructure layer, which depends only on the Domain. PostgreSQL is not available to this batch
(no database, H12), so this is the **file-backed** stand-in, placed in ``infrastructure/registry``:

- it imports only ``core`` and ``infrastructure`` (never ``research`` — research code is not
  production code, H5; never ``apps`` — that inverts the dependency direction);
- both the research-side packer (``research.promotion``) and the production-side Equivalence Gate
  (``apps.promotion``) can use it without either importing the other;
- it holds **storage integrity rules only** (append-only, identity, referential integrity); the
  promotion rules (what evidence makes an artifact) live in ``research.promotion`` and the
  equivalence rules in ``apps.promotion``.

**On disk** (``root``): ``registry.jsonl`` — one hash-chained JSON-lines journal in the shared
on-disk contract (``infrastructure.event_bus.journal.AppendOnlyJournal``, reused, not copied);
``blobs/`` — the write-once golden blobs (``infrastructure.registry.blobs``); ``.lock`` — a
``fcntl.flock`` single-writer lock (``RegistryLocked``; POSIX only).

**Records** (the journal ``type``; every payload has exactly the listed keys):

- ``artifact.registered`` — ``{"artifact_id", "artifact"}``: a ``StrategyArtifact`` whose
  recomputed ``artifact_id`` equals the recorded one, whose golden blobs are present, hash to the
  bound hashes, decode as golden payloads, answer the artifact's own strategy / spec hash and are
  mutually consistent (one answer per golden request, same ``request_hash`` in order);
- ``equivalence.recorded`` — ``{"check_hash", "check"}``: an ``EquivalenceCheck`` of a registered
  artifact (passing **or failing** — a failed check is history too, H6);
- ``deployment.recorded`` — ``{"deployment_id", "record"}``: a ``DeploymentRecord`` of a registered
  artifact whose embedded equivalence check is content-identical to a recorded one (the contract
  already requires it to pass and to bind the same artifact and production code).

**Fail closed.** The same rules run on ``append`` and on replay: a duplicate (same artifact id,
same check content, same deployment id), a dangling reference, an unknown record type, a payload
with extra / missing keys, a payload that does not validate as its contract, or a recorded
identity that does not match the recomputed one is refused — ``DuplicateRecord`` /
``UnknownArtifact`` / ``RegistryRefused`` on append, ``RegistryCorrupted`` on open (the registry
then cannot be opened at all). Tampering, a broken chain, a partial trailing line, a shrunken file
are ``RegistryCorrupted`` via the journal. Nothing is ever edited or removed.

**Truncation.** Dropping whole trailing lines leaves a valid, shorter chain; only an anchor outside
the directory detects that. ``StrategyRegistry(root, anchor=<path outside root>)`` appends
``(length, head_hash)`` to a separate hash-chained anchor journal after every record; opening
verifies that the registry still has the anchored length (or exactly one more — the one legitimate
crash window, re-anchored on open) and that the entry at the anchored length carries the anchored
hash. Truncating the anchor together with the registry is still undetectable — the anchor must
live on storage the registry's writer cannot roll back.

**Honest boundary.** Blobs are verified when an artifact is registered and on every golden read,
not on replay. Git identities (``research_code_*``, ``production_code_hash``) are format-checked by
the contracts; whether those objects exist in a repository is not checked here.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from pydantic import BaseModel, ValidationError

from core.contracts.strategy import StrategyRequest
from core.domain.artifact import DeploymentRecord, EquivalenceCheck, StrategyArtifact
from core.domain.base import canonical_json
from infrastructure.event_bus.journal import AppendOnlyJournal, JournalCorrupted
from infrastructure.registry.blobs import BlobCorrupted, BlobMissing, BlobStore
from infrastructure.registry.golden import (
    GoldenAnswer,
    GoldenPayloadInvalid,
    decode_golden_positions,
    decode_golden_signals,
)

__all__ = [
    "ARTIFACT_REGISTERED",
    "DEPLOYMENT_RECORDED",
    "EQUIVALENCE_RECORDED",
    "DuplicateRecord",
    "RegistryCorrupted",
    "RegistryError",
    "RegistryLocked",
    "RegistryRefused",
    "StrategyRegistry",
    "UnknownArtifact",
    "verify_integrity_snapshot",
]

ARTIFACT_REGISTERED: Final = "artifact.registered"
EQUIVALENCE_RECORDED: Final = "equivalence.recorded"
DEPLOYMENT_RECORDED: Final = "deployment.recorded"
_ANCHOR_TYPE: Final = "registry.head"

_KEYS: Final[Mapping[str, frozenset[str]]] = {
    ARTIFACT_REGISTERED: frozenset({"artifact_id", "artifact"}),
    EQUIVALENCE_RECORDED: frozenset({"check_hash", "check"}),
    DEPLOYMENT_RECORDED: frozenset({"deployment_id", "record"}),
}


class RegistryError(RuntimeError):
    """Base of every Strategy Registry error."""


class RegistryCorrupted(RegistryError):
    """The registry (or its anchor) on disk breaks the chain or the registry's own rules."""


class RegistryLocked(RegistryError):
    """Another open registry instance holds the single-writer lock."""


class RegistryRefused(RegistryError):
    """A record was refused before being written (it breaks a registry rule)."""


class DuplicateRecord(RegistryRefused):
    """The record's identity is already registered; nothing was written."""


class UnknownArtifact(RegistryRefused, LookupError):
    """No artifact with this ``artifact_id`` is registered."""


def _payload_of(model: BaseModel) -> Any:
    return model.model_dump(mode="json")


class StrategyRegistry:
    """The append-only Strategy Registry (see module docs). Use as a context manager or
    ``close()`` it to release the single-writer lock."""

    def __init__(self, root: Path, *, anchor: Path | None = None) -> None:
        self._root = Path(root)
        if anchor is not None and Path(anchor).resolve().is_relative_to(self._root.resolve()):
            raise ValueError("the registry anchor must live outside the registry directory")
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock_fd: int | None = os.open(self._root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise RegistryLocked(f"{self._root} is open elsewhere") from exc
        try:
            self._blobs = BlobStore(self._root / "blobs")
            self._artifacts: dict[str, StrategyArtifact] = {}
            self._checks: dict[str, EquivalenceCheck] = {}
            self._check_order: list[str] = []
            self._deployments: dict[str, DeploymentRecord] = {}
            try:
                self._journal = AppendOnlyJournal(self._root / "registry.jsonl")
            except JournalCorrupted as exc:
                raise RegistryCorrupted(f"registry journal: {exc}") from exc
            for entry in self._journal.entries:
                try:
                    self._apply(entry.type, entry.payload, replay=True)()
                except (RegistryRefused, ValidationError, ValueError) as exc:
                    raise RegistryCorrupted(f"registry record {entry.seq}: {exc}") from exc
            self._anchor: AppendOnlyJournal | None = None
            if anchor is not None:
                self._open_anchor(Path(anchor))
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
        if self._lock_fd is None:
            raise RegistryError("the registry is closed")

    # ---- anchor -------------------------------------------------------------------------

    def _open_anchor(self, path: Path) -> None:
        try:
            anchor = AppendOnlyJournal(path)
        except JournalCorrupted as exc:
            raise RegistryCorrupted(f"registry anchor: {exc}") from exc
        length, head = 0, ""
        for entry in anchor.entries:
            payload = entry.payload
            if entry.type != _ANCHOR_TYPE or set(payload) != {"length", "head"}:
                raise RegistryCorrupted(f"registry anchor record {entry.seq} is not a head record")
            new_length, new_head = payload["length"], payload["head"]
            if not isinstance(new_length, int) or isinstance(new_length, bool) or new_length < 1:
                raise RegistryCorrupted(f"registry anchor record {entry.seq} has a bad length")
            if new_length <= length or not isinstance(new_head, str):
                raise RegistryCorrupted(f"registry anchor record {entry.seq} goes backwards")
            length, head = new_length, new_head
        have = len(self._journal)
        if length:
            if have < length:
                raise RegistryCorrupted(
                    f"the registry was anchored at {length} records and now has {have}: "
                    "records were removed"
                )
            if self._journal.entry(length - 1).hash != head:
                raise RegistryCorrupted("the registry is not the history its anchor saw")
        if have > length + 1:
            raise RegistryCorrupted(
                f"the registry has {have} records but its anchor saw {length}: records were "
                "written without the anchor"
            )
        self._anchor = anchor
        if have == length + 1:  # the one legitimate crash window: record fsync'd, anchor not
            self._write_anchor()

    def _write_anchor(self) -> None:
        if self._anchor is None:
            return
        try:
            self._anchor.append(
                _ANCHOR_TYPE, {"length": len(self._journal), "head": self._journal.head_hash}
            )
        except JournalCorrupted as exc:
            raise RegistryCorrupted(f"registry anchor: {exc}") from exc

    # ---- rules (shared by append and replay) ----------------------------------------------

    def _apply(self, type_: str, payload: Mapping[str, Any], *, replay: bool) -> Callable[[], None]:
        """Check one record against every rule; returns the commit that admits it in memory.

        Nothing is mutated before the commit runs, so a refused record (or a failed disk write
        after the checks) leaves the in-memory view untouched.
        """
        keys = _KEYS.get(type_)
        if keys is None:
            raise RegistryRefused(f"unknown record type {type_!r}")
        if set(payload) != keys:
            raise RegistryRefused(f"a {type_} record has exactly the keys {sorted(keys)}")
        if type_ == ARTIFACT_REGISTERED:
            artifact = StrategyArtifact.model_validate_json(canonical_json(payload["artifact"]))
            return self._check_artifact(artifact, payload["artifact_id"], verify_blobs=not replay)
        if type_ == EQUIVALENCE_RECORDED:
            check = EquivalenceCheck.model_validate_json(canonical_json(payload["check"]))
            return self._check_equivalence(check, payload["check_hash"])
        record = DeploymentRecord.model_validate_json(canonical_json(payload["record"]))
        return self._check_deployment(record, payload["deployment_id"])

    def _check_artifact(
        self, artifact: StrategyArtifact, recorded_id: object, *, verify_blobs: bool
    ) -> Callable[[], None]:
        artifact_id = artifact.artifact_id
        if recorded_id != artifact_id:
            raise RegistryRefused("the recorded artifact_id is not the artifact's content hash")
        if artifact_id in self._artifacts:
            raise DuplicateRecord(f"artifact {artifact_id} is already registered")
        if verify_blobs:
            self._verify_golden(artifact)

        def commit() -> None:
            self._artifacts[artifact_id] = artifact

        return commit

    def _check_equivalence(
        self, check: EquivalenceCheck, recorded_hash: object
    ) -> Callable[[], None]:
        check_hash = check.content_hash()
        if recorded_hash != check_hash:
            raise RegistryRefused("the recorded check_hash is not the check's content hash")
        if check.artifact_id not in self._artifacts:
            raise UnknownArtifact(f"equivalence check of unregistered artifact {check.artifact_id}")
        if check_hash in self._checks:
            raise DuplicateRecord(f"this equivalence check ({check_hash}) is already recorded")

        def commit() -> None:
            self._checks[check_hash] = check
            self._check_order.append(check_hash)

        return commit

    def _check_deployment(
        self, record: DeploymentRecord, recorded_id: object
    ) -> Callable[[], None]:
        if recorded_id != record.deployment_id:
            raise RegistryRefused("the recorded deployment_id is not the record's deployment_id")
        if record.artifact_id not in self._artifacts:
            raise UnknownArtifact(f"deployment of unregistered artifact {record.artifact_id}")
        if record.equivalence.content_hash() not in self._checks:
            raise RegistryRefused("a deployment's equivalence check must be recorded first")
        if record.deployment_id in self._deployments:
            raise DuplicateRecord(f"deployment {record.deployment_id} is already recorded")

        def commit() -> None:
            self._deployments[record.deployment_id] = record

        return commit

    def _verify_golden(self, artifact: StrategyArtifact) -> None:
        requests = self._golden_requests(artifact)
        answers = self._golden_answers(artifact)
        spec_hash = artifact.dependencies[str(artifact.strategy_spec)]
        for request in requests:
            if request.strategy.target_identity() != artifact.strategy_spec.target_identity():
                raise RegistryRefused("a golden request asks for another strategy")
            if request.spec_hash != spec_hash:
                raise RegistryRefused("a golden request is bound to another spec hash")
        if [a.request_hash for a in answers] != [r.content_hash() for r in requests]:
            raise RegistryRefused("the golden positions do not answer the golden requests in order")

    def _golden_requests(self, artifact: StrategyArtifact) -> tuple[StrategyRequest, ...]:
        golden = artifact.golden_outputs
        try:
            return decode_golden_signals(self._blobs.get(golden.signals_uri, golden.signals_hash))
        except BlobMissing as exc:
            raise RegistryRefused(f"golden signals blob: {exc}") from exc
        except (BlobCorrupted, GoldenPayloadInvalid) as exc:
            raise RegistryCorrupted(f"golden signals blob: {exc}") from exc

    def _golden_answers(self, artifact: StrategyArtifact) -> tuple[GoldenAnswer, ...]:
        golden = artifact.golden_outputs
        try:
            return decode_golden_positions(
                self._blobs.get(golden.positions_uri, golden.positions_hash)
            )
        except BlobMissing as exc:
            raise RegistryRefused(f"golden positions blob: {exc}") from exc
        except (BlobCorrupted, GoldenPayloadInvalid) as exc:
            raise RegistryCorrupted(f"golden positions blob: {exc}") from exc

    # ---- writes -------------------------------------------------------------------------

    def _append(self, type_: str, payload: Mapping[str, Any]) -> None:
        self._require_open()
        commit = self._apply(type_, payload, replay=False)  # every rule before any write
        try:
            self._journal.append(type_, payload)
        except JournalCorrupted as exc:
            raise RegistryCorrupted(f"registry journal: {exc}") from exc
        commit()
        self._write_anchor()

    def put_blob(self, payload: Mapping[str, Any]) -> tuple[str, str]:
        """Store one golden payload (write-once); returns ``(uri, sha256)``."""
        self._require_open()
        return self._blobs.put(payload)

    def register_artifact(self, artifact: StrategyArtifact) -> str:
        """Register an artifact (its golden blobs must already be stored); returns its id."""
        artifact = StrategyArtifact.model_validate_json(
            artifact.model_dump_json()
        )  # no model_construct
        self._append(
            ARTIFACT_REGISTERED,
            {"artifact_id": artifact.artifact_id, "artifact": _payload_of(artifact)},
        )
        return artifact.artifact_id

    def record_equivalence(self, check: EquivalenceCheck) -> str:
        """Record one equivalence check (passing or failing); returns its content hash."""
        check = EquivalenceCheck.model_validate_json(check.model_dump_json())
        self._append(
            EQUIVALENCE_RECORDED, {"check_hash": check.content_hash(), "check": _payload_of(check)}
        )
        return check.content_hash()

    def record_deployment(self, record: DeploymentRecord) -> str:
        """Record one deployment (its passing check must be recorded); returns its id."""
        record = DeploymentRecord.model_validate_json(record.model_dump_json())
        self._append(
            DEPLOYMENT_RECORDED,
            {"deployment_id": record.deployment_id, "record": _payload_of(record)},
        )
        return record.deployment_id

    # ---- reads --------------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._journal)

    @property
    def head_hash(self) -> str:
        return self._journal.head_hash

    def has_artifact(self, artifact_id: str) -> bool:
        return artifact_id in self._artifacts

    def artifact(self, artifact_id: str) -> StrategyArtifact:
        try:
            return self._artifacts[artifact_id]
        except KeyError:
            raise UnknownArtifact(f"artifact {artifact_id} is not registered") from None

    def artifacts(self) -> tuple[StrategyArtifact, ...]:
        return tuple(self._artifacts.values())

    def golden_inputs(self, artifact_id: str) -> tuple[StrategyRequest, ...]:
        """The artifact's golden requests, read from the blob store and verified."""
        return self._golden_requests(self.artifact(artifact_id))

    def golden_answers(self, artifact_id: str) -> tuple[GoldenAnswer, ...]:
        """The artifact's golden answers, read from the blob store and verified."""
        return self._golden_answers(self.artifact(artifact_id))

    def equivalence_checks(self, artifact_id: str) -> tuple[EquivalenceCheck, ...]:
        """Every recorded check of ``artifact_id``, in record order."""
        return tuple(
            self._checks[key]
            for key in self._check_order
            if self._checks[key].artifact_id == artifact_id
        )

    def has_equivalence(self, check: EquivalenceCheck) -> bool:
        return check.content_hash() in self._checks

    def deployments(self, artifact_id: str | None = None) -> tuple[DeploymentRecord, ...]:
        return tuple(
            record
            for record in self._deployments.values()
            if artifact_id is None or record.artifact_id == artifact_id
        )


def verify_integrity_snapshot(root: Path, *, anchor: Path | None = None) -> dict[str, object]:
    """Verify a stable on-disk snapshot without opening or mutating a registry instance.

    Unlike ``StrategyRegistry(...)``, this does not create a directory or lock and never repairs
    an anchor. A supplied anchor must match the journal tip exactly; without one, whole trailing
    journal records cannot be detected and the result is marked ``UNANCHORED``.
    """
    root = Path(root)
    journal_path = root / "registry.jsonl"
    if not root.is_dir() or not journal_path.is_file():
        raise RegistryCorrupted(f"the Strategy Registry does not exist at {root}")
    root = root.resolve()
    if anchor is not None:
        anchor = Path(anchor)
        if anchor.resolve().is_relative_to(root):
            raise RegistryCorrupted("the Strategy Registry anchor must be outside its directory")

    def signature(path: Path) -> tuple[bytes, tuple[int, int, int, int]]:
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        stamp = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if stamp != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise RegistryCorrupted(f"{path} changed while the audit snapshot was read")
        return data, stamp

    journal_bytes, journal_stamp = signature(journal_path)
    try:
        journal = AppendOnlyJournal(journal_path)
    except JournalCorrupted as exc:
        raise RegistryCorrupted(f"registry journal: {exc}") from exc

    # Replay the same business rules in an isolated, in-memory instance. No constructor, lock,
    # registry write method, or anchor recovery path is entered.
    replay = object.__new__(StrategyRegistry)
    replay._artifacts = {}
    replay._checks = {}
    replay._check_order = []
    replay._deployments = {}
    replay._blobs = BlobStore(root / "blobs")
    for entry in journal.entries:
        try:
            replay._apply(entry.type, entry.payload, replay=True)()
        except (RegistryRefused, ValidationError, ValueError) as exc:
            raise RegistryCorrupted(f"registry record {entry.seq}: {exc}") from exc

    anchor_status = "UNANCHORED"
    anchor_path: Path | None = None
    anchor_stamp: tuple[int, int, int, int] | None = None
    anchor_bytes: bytes | None = None
    if anchor is not None:
        anchor_path = Path(anchor)
        if not anchor_path.is_file():
            raise RegistryCorrupted(f"the Strategy Registry anchor does not exist: {anchor_path}")
        anchor_bytes, anchor_stamp = signature(anchor_path)
        try:
            anchor_journal = AppendOnlyJournal(anchor_path)
        except JournalCorrupted as exc:
            raise RegistryCorrupted(f"registry anchor: {exc}") from exc
        length, head = 0, ""
        for entry in anchor_journal.entries:
            payload = entry.payload
            if entry.type != _ANCHOR_TYPE or set(payload) != {"length", "head"}:
                raise RegistryCorrupted(f"registry anchor record {entry.seq} is not a head record")
            new_length, new_head = payload["length"], payload["head"]
            if (
                not isinstance(new_length, int)
                or isinstance(new_length, bool)
                or new_length <= length
                or not isinstance(new_head, str)
            ):
                raise RegistryCorrupted(f"registry anchor record {entry.seq} is invalid")
            if (
                new_length > len(journal)
                or journal.entry(new_length - 1).hash != new_head
            ):
                raise RegistryCorrupted(
                    f"registry anchor record {entry.seq} does not match its journal prefix"
                )
            length, head = new_length, new_head
        if length != len(journal) or (length and journal.entry(length - 1).hash != head):
            raise RegistryCorrupted("the Strategy Registry anchor does not match the journal tip")
        if not length and head:
            raise RegistryCorrupted("the empty Strategy Registry anchor has a non-empty head")
        anchor_status = "VERIFIED"

    # Refuse a mixed-time report if either journal or anchor changed during replay.
    if signature(journal_path) != (journal_bytes, journal_stamp):
        raise RegistryCorrupted(f"{journal_path} changed during the audit")
    if anchor_path is not None and signature(anchor_path) != (anchor_bytes, anchor_stamp):
        raise RegistryCorrupted(f"{anchor_path} changed during the audit")
    return {
        "status": "OK",
        "evidence": "HASH_CHAIN",
        "path": str(root),
        "journal_records": len(journal),
        "journal_head_hash": journal.head_hash,
        "anchor_status": anchor_status,
        "anchor_path": None if anchor_path is None else str(anchor_path.resolve()),
        "limitations": [
            "rollback of the journal and its external anchor together is not detectable"
        ]
        if anchor_status == "VERIFIED"
        else ["without an external anchor, whole trailing journal records cannot be detected"],
    }

"""Durable primitives for ADR-0083 failed-round retry admission.

The retry journal is deliberately separate from the typed-plan journal.  This module contains
strict journal validation and the pure ledger-tail reducer; the state opener owns when recovery
may persist the reducer's unique suffix.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.domain.base import content_hash
from core.domain.research import Hypothesis
from research.persistence import AppendOnlyJournal, JournalEntry, JournalSnapshot

__all__ = [
    "RETRY_COMMIT",
    "RETRY_FILE",
    "RETRY_PREPARE",
    "RetryAdmissionError",
    "RetryJournal",
    "RetryManifestItem",
    "RetryRecovery",
    "reduce_retry_ledger_tail",
]

RETRY_FILE = "retry_admission.jsonl"
RETRY_PREPARE = "retry_prepare"
RETRY_COMMIT = "retry_commit"
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PREPARE_KEYS = frozenset(
    {
        "state_version",
        "loop_id",
        "retry_id",
        "packet",
        "packet_hash",
        "failed_record_hash",
        "reviewer",
        "manifest",
        "ledger_baseline_seq",
        "ledger_baseline_hash",
    }
)
_COMMIT_KEYS = frozenset(
    {
        "retry_id",
        "prepare_seq",
        "prepare_hash",
        "ledger_entries",
        "ledger_head_seq",
        "ledger_head_hash",
    }
)


class RetryAdmissionError(ValueError):
    """A retry journal or prepared ledger tail is ambiguous or inconsistent."""


@dataclass(frozen=True, slots=True)
class RetryManifestItem:
    """One explicit re-evaluation of an already registered, content-bound hypothesis."""

    name: str
    version: str
    hypothesis_hash: str
    attempt: str

    def payload(self) -> dict[str, str]:
        return {
            "name": self.name,
            "version": self.version,
            "hypothesis_hash": self.hypothesis_hash,
            "attempt": self.attempt,
        }


@dataclass(frozen=True, slots=True)
class RetryRecovery:
    """The only ledger suffix a v6 opener may append after a retry PREPARE."""

    retry_id: str
    items: tuple[RetryManifestItem, ...]
    existing_entries: tuple[JournalEntry, ...]
    missing_items: tuple[RetryManifestItem, ...]
    prepare: JournalEntry
    commit: JournalEntry | None


class RetryJournal:
    """Hash-chain journal restricted to ADR-0083 PREPARE and COMMIT events."""

    def __init__(self, path: Path, *, loop_id: str, create: bool = False) -> None:
        self._journal = AppendOnlyJournal(path)
        self.loop_id = loop_id
        if not self._journal.entries and create:
            # No synthetic header: every event is one of the two ADR-defined event types.
            return
        self._validate()

    @property
    def path(self) -> Path:
        return self._journal.path

    @property
    def entries(self) -> tuple[JournalEntry, ...]:
        return self._journal.entries

    @property
    def head_hash(self) -> str:
        return self._journal.head_hash

    def append_prepare(self, payload: Mapping[str, Any]) -> JournalEntry:
        _validate_prepare(payload, self.loop_id)
        if self.entries and self.entries[-1].type == RETRY_PREPARE:
            raise RetryAdmissionError("a retry PREPARE is already pending")
        if any(entry.type == RETRY_PREPARE for entry in self.entries):
            raise RetryAdmissionError("a completed retry journal cannot admit a second retry")
        return self._journal.append(RETRY_PREPARE, payload)

    def append_commit(self, payload: Mapping[str, Any]) -> JournalEntry:
        if not self.entries or self.entries[-1].type != RETRY_PREPARE:
            raise RetryAdmissionError("retry COMMIT requires the unique pending PREPARE")
        prepared = self.entries[-1]
        _validate_commit(payload, prepared)
        return self._journal.append(RETRY_COMMIT, payload)

    def _validate(self) -> None:
        entries = self.entries
        if len(entries) > 2:
            raise RetryAdmissionError(f"{self.path} contains more than one retry transaction")
        if not entries:
            return
        if entries[0].type != RETRY_PREPARE:
            raise RetryAdmissionError(f"{self.path}: retry journal must start with retry_prepare")
        _validate_prepare(entries[0].payload, self.loop_id)
        if len(entries) == 2:
            if entries[1].type != RETRY_COMMIT:
                raise RetryAdmissionError(f"{self.path}: retry journal has a non-terminal event")
            _validate_commit(entries[1].payload, entries[0])


def reduce_retry_ledger_tail(
    retry: RetryJournal,
    ledger: JournalSnapshot,
    *,
    loop_id: str,
    state_version: int,
) -> RetryRecovery | None:
    """Validate the exact contiguous ``reevaluate`` prefix named by PREPARE.

    This is pure: it never appends, calls a provider, or mutates a ledger.  ``missing_items`` is
    the only suffix the opener may recover; any divergent, repeated, or overlong tail fails
    closed.  A fully checkpointed transaction returns ``None``.
    """
    if state_version != 6:
        if retry.entries:
            raise RetryAdmissionError("retry journals are valid only for durable state version 6")
        return None
    entries = retry.entries
    if not entries:
        return None
    prepare = entries[0]
    raw = prepare.payload
    if raw["loop_id"] != loop_id or raw["state_version"] != 6:
        raise RetryAdmissionError("retry PREPARE is bound to another loop or state version")
    items = tuple(_manifest(raw["manifest"]))
    baseline_seq = raw["ledger_baseline_seq"]
    baseline_hash = raw["ledger_baseline_hash"]
    ledger_entries = ledger.entries
    if baseline_seq > len(ledger_entries):
        raise RetryAdmissionError("TrialLedger is shorter than the prepared baseline")
    observed_baseline = ledger_entries[baseline_seq - 1].hash if baseline_seq else "0" * 64
    if observed_baseline != baseline_hash:
        raise RetryAdmissionError("TrialLedger does not match the prepared baseline")
    suffix = ledger_entries[baseline_seq:]
    proposed_attempts = {item.attempt for item in items}
    for entry in ledger_entries[:baseline_seq]:
        if entry.type == "reevaluate" and entry.payload.get("attempt") in proposed_attempts:
            raise RetryAdmissionError("a retry attempt key was already used in the TrialLedger")
    if len(suffix) > len(items):
        raise RetryAdmissionError("TrialLedger contains entries beyond the retry manifest")
    for item, entry in zip(items, suffix, strict=False):
        if not _ledger_entry_matches(entry, item):
            raise RetryAdmissionError("TrialLedger retry prefix differs from PREPARE manifest")
    commit = entries[1] if len(entries) == 2 else None
    if commit is not None:
        if len(suffix) != len(items):
            raise RetryAdmissionError("retry COMMIT exists before every manifest entry")
        _validate_committed_ledger(commit.payload, suffix)
        return RetryRecovery(str(raw["retry_id"]), items, tuple(suffix), (), prepare, commit)
    return RetryRecovery(
        str(raw["retry_id"]),
        items,
        tuple(suffix),
        items[len(suffix) :],
        prepare,
        None,
    )


def _validate_prepare(payload: Mapping[str, Any], loop_id: str) -> None:
    if set(payload) != _PREPARE_KEYS:
        raise RetryAdmissionError("retry_prepare has unknown or missing fields")
    if payload["state_version"] != 6 or type(payload["state_version"]) is not int:
        raise RetryAdmissionError("retry_prepare must bind state version 6")
    if payload["loop_id"] != loop_id or not isinstance(loop_id, str) or not loop_id:
        raise RetryAdmissionError("retry_prepare must bind the current loop_id")
    for key in ("retry_id", "reviewer"):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise RetryAdmissionError(f"retry_prepare {key} must be non-empty")
    reviewer = payload["reviewer"].strip().casefold()
    if reviewer in {"system", "loop", "research_loop", "automation", "automated"}:
        raise RetryAdmissionError("retry reviewer must be a human reviewer declaration")
    for key in ("packet_hash", "failed_record_hash", "ledger_baseline_hash"):
        if not isinstance(payload[key], str) or _HASH.fullmatch(payload[key]) is None:
            raise RetryAdmissionError(f"retry_prepare {key} must be lowercase SHA-256")
    if content_hash(payload["packet"]) != payload["packet_hash"]:
        raise RetryAdmissionError("retry_prepare packet hash does not match its canonical payload")
    packet = payload["packet"]
    try:
        packet_record_hash = packet["loop_record"]["record_content_hash"]
    except (KeyError, TypeError) as exc:
        raise RetryAdmissionError("retry packet does not bind a failed loop record") from exc
    if packet_record_hash != payload["failed_record_hash"]:
        raise RetryAdmissionError("retry packet names another failed record")
    seq = payload["ledger_baseline_seq"]
    if type(seq) is not int or seq < 0:
        raise RetryAdmissionError("retry_prepare ledger baseline sequence must be non-negative")
    _manifest(payload["manifest"])


def _manifest(value: Any) -> tuple[RetryManifestItem, ...]:
    if not isinstance(value, list) or not value:
        raise RetryAdmissionError("retry manifest must be a non-empty ordered array")
    items: list[RetryManifestItem] = []
    attempts: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != {
            "name",
            "version",
            "hypothesis_hash",
            "attempt",
        }:
            raise RetryAdmissionError("retry manifest item has unknown or missing fields")
        if any(
            not isinstance(raw[key], str)
            for key in ("name", "version", "hypothesis_hash", "attempt")
        ):
            raise RetryAdmissionError("retry manifest fields must be strings")
        item = RetryManifestItem(**raw)
        if not item.name.strip() or not item.version.strip() or not item.attempt.strip():
            raise RetryAdmissionError("retry manifest name, version, and attempt are required")
        if _HASH.fullmatch(item.hypothesis_hash) is None:
            raise RetryAdmissionError("retry manifest hypothesis_hash must be lowercase SHA-256")
        normalized = item.attempt.strip()
        if normalized != item.attempt or normalized in attempts:
            raise RetryAdmissionError("retry attempts must be normalized and unique in the manifest")
        attempts.add(normalized)
        items.append(item)
    return tuple(items)


def _validate_commit(payload: Mapping[str, Any], prepare: JournalEntry) -> None:
    if set(payload) != _COMMIT_KEYS:
        raise RetryAdmissionError("retry_commit has unknown or missing fields")
    if (
        not isinstance(payload["retry_id"], str)
        or payload["retry_id"] != prepare.payload["retry_id"]
    ):
        raise RetryAdmissionError("retry_commit names another retry_id")
    if (
        type(payload["prepare_seq"]) is not int
        or payload["prepare_seq"] != prepare.seq
        or payload["prepare_hash"] != prepare.hash
    ):
        raise RetryAdmissionError("retry_commit does not bind the exact PREPARE entry")
    positions = payload["ledger_entries"]
    if not isinstance(positions, list) or len(positions) != len(prepare.payload["manifest"]):
        raise RetryAdmissionError("retry_commit must name every ordered TrialLedger entry")
    previous_seq = prepare.payload["ledger_baseline_seq"]
    for pos in positions:
        if not isinstance(pos, Mapping) or set(pos) != {"seq", "hash"}:
            raise RetryAdmissionError("retry_commit has an invalid ledger entry position")
        if (
            type(pos["seq"]) is not int
            or pos["seq"] != previous_seq + 1
            or not isinstance(pos["hash"], str)
            or _HASH.fullmatch(pos["hash"]) is None
        ):
            raise RetryAdmissionError("retry_commit has an invalid ledger sequence or hash")
        previous_seq = pos["seq"]
    if (
        type(payload["ledger_head_seq"]) is not int
        or payload["ledger_head_seq"] != previous_seq
    ):
        raise RetryAdmissionError("retry_commit has an invalid final ledger sequence")
    if (
        not isinstance(payload["ledger_head_hash"], str)
        or _HASH.fullmatch(payload["ledger_head_hash"]) is None
    ):
        raise RetryAdmissionError("retry_commit has an invalid final ledger hash")


def _ledger_entry_matches(entry: JournalEntry, item: RetryManifestItem) -> bool:
    payload = entry.payload
    if entry.type != "reevaluate" or set(payload) != {"hypothesis", "attempt"}:
        return False
    if payload["attempt"] != item.attempt or not isinstance(payload["hypothesis"], Mapping):
        return False
    try:
        hypothesis = Hypothesis.model_validate(payload["hypothesis"])
    except (TypeError, ValueError):
        return False
    return (
        hypothesis.model_dump(mode="json") == dict(payload["hypothesis"])
        and hypothesis.name == item.name
        and hypothesis.version == item.version
        and hypothesis.content_hash() == item.hypothesis_hash
    )


def _validate_committed_ledger(commit: Mapping[str, Any], suffix: Sequence[JournalEntry]) -> None:
    positions = commit["ledger_entries"]
    if any(
        pos != {"seq": entry.seq, "hash": entry.hash}
        for pos, entry in zip(positions, suffix, strict=True)
    ):
        raise RetryAdmissionError("retry COMMIT ledger positions differ from the verified entries")
    last = suffix[-1]
    if (commit["ledger_head_seq"], commit["ledger_head_hash"]) != (last.seq, last.hash):
        raise RetryAdmissionError("retry COMMIT does not bind the final TrialLedger head")

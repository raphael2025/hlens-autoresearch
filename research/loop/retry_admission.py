"""Durable primitives for ADR-0083 failed-round retry admission.

One retry journal file carries exactly **one** retry admission (ADR-0083, "一次失败一个 journal"):
it lives at ``<state_dir>/retry_admission/<failed record_hash>.jsonl``, is bound to that failed
audit record by its file name and by every event it holds, and contains at most the pair
``retry_prepare`` → ``retry_commit``. A retry that fails again produces a new failed audit record,
a new ADR-0071 review packet and therefore a new journal file; a journal is never reused.

This module is pure protocol: strict event validation, the manifest checks (G1: every item is an
already registered hypothesis with exactly that content; G2: the retry fits the remaining hard trial
budget), and the reducer that recognizes the unique ``reevaluate`` TrialLedger suffix a PREPARE
authorizes. It never appends to the TrialLedger or the memory journal; ``research.loop.durable``
owns when (and under which locks) the reducer's unique suffix may be written.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from core.domain.base import content_hash
from core.domain.research import Hypothesis
from research.persistence import GENESIS_HASH, AppendOnlyJournal, JournalEntry

__all__ = [
    "RETRY_COMMIT",
    "RETRY_DIR",
    "RETRY_PREPARE",
    "RETRY_STATE_VERSION",
    "RetryAdmissionError",
    "RetryJournal",
    "RetryJournals",
    "RetryManifestItem",
    "RetryRecovery",
    "check_retry_budget",
    "manifest_items",
    "reduce_retry_ledger_tail",
    "resolve_manifest",
    "retry_commit_payload",
    "retry_id_for",
    "retry_prepare_payload",
    "retry_summary_rows",
    "validate_reviewer",
]

#: Directory (under the state directory) holding one journal per retried failed record.
RETRY_DIR: Final = "retry_admission"
RETRY_PREPARE: Final = "retry_prepare"
RETRY_COMMIT: Final = "retry_commit"
#: The only durable state version a retry journal may bind (ADR-0083).
RETRY_STATE_VERSION: Final = 6
#: Attempt keys the loop itself writes for automatic re-evaluations (``reevaluation_attempt``):
#: a retry may never take one, or a later automatic re-evaluation would collide with it.
RESERVED_ATTEMPT_PREFIX: Final = "loop_round:"
_HASH: Final = re.compile(r"^[0-9a-f]{64}$")
_FILE: Final = re.compile(r"^([0-9a-f]{64})\.jsonl$")
_AUTOMATED_REVIEWERS: Final = frozenset(
    {"system", "loop", "research_loop", "automation", "automated", "scheduler", "worker"}
)
_AUTOMATED_PREFIXES: Final = ("research_loop:", "system:", "automation:", "scheduler:")
_PREPARE_KEYS: Final = frozenset(
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
_COMMIT_KEYS: Final = frozenset(
    {
        "state_version",
        "loop_id",
        "retry_id",
        "failed_record_hash",
        "prepare_seq",
        "prepare_hash",
        "ledger_entries",
        "ledger_head_seq",
        "ledger_head_hash",
        "memory_checkpoint_seq",
    }
)
_ITEM_KEYS: Final = frozenset({"name", "version", "hypothesis_hash", "attempt"})


class RetryAdmissionError(ValueError):
    """A retry request, journal or prepared ledger tail is ambiguous or inconsistent."""


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
    """What the reducer recognized: the verified ``reevaluate`` entries after the PREPARE
    baseline and the unique suffix (``missing_items``) that may still be appended."""

    retry_id: str
    failed_record_hash: str
    items: tuple[RetryManifestItem, ...]
    existing_entries: tuple[JournalEntry, ...]
    missing_items: tuple[RetryManifestItem, ...]
    prepare: JournalEntry
    commit: JournalEntry | None


# ------------------------------------------------------------------------------ request checks


def validate_reviewer(reviewer: object) -> str:
    """A non-empty, normalized, non-automation reviewer declaration (not an authentication)."""
    if not isinstance(reviewer, str) or not reviewer.strip() or reviewer != reviewer.strip():
        raise RetryAdmissionError("retry reviewer must be a non-empty, normalized declaration")
    folded = reviewer.casefold()
    if folded in _AUTOMATED_REVIEWERS or folded.startswith(_AUTOMATED_PREFIXES):
        raise RetryAdmissionError(
            "retry reviewer must be a human reviewer declaration, not an automation identity"
        )
    return reviewer


def manifest_items(value: object) -> tuple[RetryManifestItem, ...]:
    """Parse and check a manifest (payload list or ``RetryManifestItem`` values).

    ADR-0083 "PM 决定" §4 (2026-09-28): a hypothesis (its ``name@version``) may appear at most
    once in one retry admission's manifest — distinct attempt keys no longer excuse a repeat. This
    is checked here, at the manifest's one parsing point, so it fails closed identically whether
    the manifest is about to be PREPAREd or is being re-verified while reopening a v6 state
    directory (``reduce_retry_ledger_tail`` parses the PREPARE's saved manifest through this same
    function).
    """
    if isinstance(value, str | bytes | Mapping) or not isinstance(value, Iterable):
        raise RetryAdmissionError("retry manifest must be a non-empty ordered sequence")
    raw_items = [item.payload() if isinstance(item, RetryManifestItem) else item for item in value]
    if not raw_items:
        raise RetryAdmissionError("retry manifest must be a non-empty ordered sequence")
    items: list[RetryManifestItem] = []
    attempts: set[str] = set()
    hypotheses: set[tuple[str, str]] = set()
    for raw in raw_items:
        if not isinstance(raw, Mapping) or set(raw) != _ITEM_KEYS:
            raise RetryAdmissionError("retry manifest item has unknown or missing fields")
        if any(not isinstance(raw[key], str) for key in _ITEM_KEYS):
            raise RetryAdmissionError("retry manifest fields must be strings")
        item = RetryManifestItem(
            name=raw["name"],
            version=raw["version"],
            hypothesis_hash=raw["hypothesis_hash"],
            attempt=raw["attempt"],
        )
        if not item.name.strip() or not item.version.strip():
            raise RetryAdmissionError("retry manifest items need an exact name@version")
        if _HASH.fullmatch(item.hypothesis_hash) is None:
            raise RetryAdmissionError("retry manifest hypothesis_hash must be lowercase SHA-256")
        hypothesis_key = (item.name, item.version)
        if hypothesis_key in hypotheses:
            raise RetryAdmissionError(
                f"retry manifest repeats {item.name}@{item.version}: "
                "hypothesis keys must be unique; "
                "one admission may retry each hypothesis at most once (ADR-0083 PM decision 4)"
            )
        hypotheses.add(hypothesis_key)
        attempt = item.attempt
        if not attempt.strip() or attempt != attempt.strip():
            raise RetryAdmissionError("retry attempt keys must be non-empty and normalized")
        if attempt.startswith(RESERVED_ATTEMPT_PREFIX):
            raise RetryAdmissionError(
                f"retry attempt keys may not use the loop's reserved {RESERVED_ATTEMPT_PREFIX!r} "
                "prefix"
            )
        if attempt in attempts:
            raise RetryAdmissionError("retry attempt keys must be unique in the manifest")
        attempts.add(attempt)
        items.append(item)
    return tuple(items)


def resolve_manifest(
    registered: Iterable[Hypothesis], items: Sequence[RetryManifestItem]
) -> tuple[Hypothesis, ...]:
    """G1: every item names a hypothesis already in the TrialLedger's registered table, with
    exactly the manifest's content hash; otherwise the whole request fails closed."""
    table = {(hypothesis.name, hypothesis.version): hypothesis for hypothesis in registered}
    resolved: list[Hypothesis] = []
    for item in items:
        hypothesis = table.get((item.name, item.version))
        if hypothesis is None:
            raise RetryAdmissionError(
                f"retry manifest names {item.name}@{item.version}, which the TrialLedger has "
                "never registered"
            )
        if hypothesis.content_hash() != item.hypothesis_hash:
            raise RetryAdmissionError(
                f"retry manifest hash of {item.name}@{item.version} differs from the content the "
                "TrialLedger registered"
            )
        resolved.append(hypothesis)
    return tuple(resolved)


def check_retry_budget(budget: object, *, total_trials_spent: int, requested: int) -> None:
    """G2 (ADR-0049 budget semantics): the retry declares ``requested`` trials before anything
    runs; they are charged to the retry round at ``max(declared, actual)``. Refuse a request whose
    declaration alone would break the per-round cap or the remaining hard total."""
    if not isinstance(budget, Mapping):
        raise RetryAdmissionError("the durable state binds no LoopBudget")
    per_round, total = budget.get("max_trials_per_round"), budget.get("max_trials_total")
    if type(per_round) is not int or type(total) is not int or per_round < 0 or total < 0:
        raise RetryAdmissionError("the bound LoopBudget has no integral trial limits")
    if type(total_trials_spent) is not int or total_trials_spent < 0:
        raise RetryAdmissionError("the audit's spent trial total is unreadable")
    if requested > per_round:
        raise RetryAdmissionError(
            f"retry declares {requested} trials; the round cap max_trials_per_round is {per_round}"
        )
    remaining = total - total_trials_spent
    if requested > remaining:
        raise RetryAdmissionError(
            f"retry declares {requested} trials but only {max(remaining, 0)} remain under "
            f"max_trials_total ({total_trials_spent} of {total} spent)"
        )


def retry_summary_rows(pairs: Iterable[tuple[Hypothesis, str]]) -> list[dict[str, str]]:
    """The hypothesis-stage summary rows of a consumed retry (``retry_reevaluations``)."""
    return [
        {
            "hypothesis": str(hypothesis.ref),
            "hypothesis_hash": hypothesis.content_hash(),
            "attempt": attempt,
        }
        for hypothesis, attempt in pairs
    ]


# ------------------------------------------------------------------------------- event payloads


def retry_id_for(
    *,
    failed_record_hash: str,
    packet_hash: str,
    reviewer: str,
    manifest: Sequence[RetryManifestItem],
    ledger_baseline: tuple[int, str],
) -> str:
    """Deterministic identity of one retry request (recomputed on every replay)."""
    return content_hash(
        {
            "failed_record_hash": failed_record_hash,
            "packet_hash": packet_hash,
            "reviewer": reviewer,
            "manifest": [item.payload() for item in manifest],
            "ledger_baseline": {"seq": ledger_baseline[0], "hash": ledger_baseline[1]},
        }
    )


def retry_prepare_payload(
    *,
    loop_id: str,
    packet: Mapping[str, Any],
    packet_hash: str,
    failed_record_hash: str,
    reviewer: str,
    manifest: Sequence[RetryManifestItem],
    ledger_baseline: tuple[int, str],
) -> dict[str, Any]:
    return {
        "state_version": RETRY_STATE_VERSION,
        "loop_id": loop_id,
        "retry_id": retry_id_for(
            failed_record_hash=failed_record_hash,
            packet_hash=packet_hash,
            reviewer=reviewer,
            manifest=manifest,
            ledger_baseline=ledger_baseline,
        ),
        "packet": dict(packet),
        "packet_hash": packet_hash,
        "failed_record_hash": failed_record_hash,
        "reviewer": reviewer,
        "manifest": [item.payload() for item in manifest],
        "ledger_baseline_seq": ledger_baseline[0],
        "ledger_baseline_hash": ledger_baseline[1],
    }


def retry_commit_payload(
    prepare: JournalEntry,
    ledger_entries: Sequence[JournalEntry],
    *,
    memory_checkpoint_seq: int,
) -> dict[str, Any]:
    if not ledger_entries:
        raise RetryAdmissionError("a retry COMMIT names at least one TrialLedger entry")
    raw = prepare.payload
    last = ledger_entries[-1]
    return {
        "state_version": RETRY_STATE_VERSION,
        "loop_id": raw["loop_id"],
        "retry_id": raw["retry_id"],
        "failed_record_hash": raw["failed_record_hash"],
        "prepare_seq": prepare.seq,
        "prepare_hash": prepare.hash,
        "ledger_entries": [{"seq": entry.seq, "hash": entry.hash} for entry in ledger_entries],
        "ledger_head_seq": last.seq,
        "ledger_head_hash": last.hash,
        "memory_checkpoint_seq": memory_checkpoint_seq,
    }


# ----------------------------------------------------------------------------------- journals


class RetryJournal:
    """The hash-chained journal of one retry admission: at most ``retry_prepare`` →
    ``retry_commit``, bound to ``failed_record_hash`` (its file name) and ``loop_id``.

    Opening never creates the file: the first append does, so a file exists only once its
    PREPARE was written (a crash before the line leaves at most an empty file, which holds no
    event).
    """

    def __init__(self, path: Path, *, loop_id: str, failed_record_hash: str) -> None:
        if not isinstance(loop_id, str) or not loop_id:
            raise RetryAdmissionError("a retry journal is bound to a non-empty loop_id")
        if _HASH.fullmatch(failed_record_hash) is None:
            raise RetryAdmissionError("a retry journal is bound to a failed record SHA-256")
        self._journal = AppendOnlyJournal(path)
        self.loop_id = loop_id
        self.failed_record_hash = failed_record_hash
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

    @property
    def prepare(self) -> JournalEntry | None:
        entries = self.entries
        return entries[0] if entries else None

    @property
    def commit(self) -> JournalEntry | None:
        entries = self.entries
        return entries[1] if len(entries) == 2 else None

    def position(self) -> dict[str, Any]:
        """This journal's position in a checkpoint's ``heads["retry_admission"]`` list."""
        return {
            "failed_record_hash": self.failed_record_hash,
            "seq": len(self.entries),
            "hash": self.head_hash,
        }

    def append_prepare(self, payload: Mapping[str, Any]) -> JournalEntry:
        if self.entries:
            raise RetryAdmissionError(
                f"{self.path} already holds a retry admission: one journal admits one retry"
            )
        _validate_prepare(payload, self.loop_id, self.failed_record_hash)
        return self._journal.append(RETRY_PREPARE, payload)

    def append_commit(self, payload: Mapping[str, Any]) -> JournalEntry:
        entries = self.entries
        if len(entries) != 1:
            raise RetryAdmissionError("retry COMMIT requires the journal's unique pending PREPARE")
        _validate_commit(payload, entries[0])
        return self._journal.append(RETRY_COMMIT, payload)

    def _validate(self) -> None:
        entries = self.entries
        if len(entries) > 2:
            raise RetryAdmissionError(f"{self.path} holds more than one retry transaction")
        if not entries:
            return
        if entries[0].type != RETRY_PREPARE:
            raise RetryAdmissionError(f"{self.path}: a retry journal must start with retry_prepare")
        _validate_prepare(entries[0].payload, self.loop_id, self.failed_record_hash)
        if len(entries) == 2:
            if entries[1].type != RETRY_COMMIT:
                raise RetryAdmissionError(f"{self.path}: a retry journal ends with retry_commit")
            _validate_commit(entries[1].payload, entries[0])


class RetryJournals:
    """Every retry journal of one state directory (``<state_dir>/retry_admission``).

    Any other file in the directory — a name that is not ``<64 hex>.jsonl``, a sub-directory or a
    symbolic link — is refused (fail closed).
    """

    def __init__(self, directory: Path, *, loop_id: str) -> None:
        self._dir = Path(directory)
        self.loop_id = loop_id
        self._journals: dict[str, RetryJournal] = {}
        if self._dir.is_symlink() or (self._dir.exists() and not self._dir.is_dir()):
            raise RetryAdmissionError(f"{self._dir} is not a retry journal directory")
        if not self._dir.exists():
            return
        for path in sorted(self._dir.iterdir()):
            match = _FILE.fullmatch(path.name)
            if match is None or path.is_symlink() or not path.is_file():
                raise RetryAdmissionError(f"{path} is not a retry journal file")
            failed = match.group(1)
            self._journals[failed] = RetryJournal(path, loop_id=loop_id, failed_record_hash=failed)

    @property
    def directory(self) -> Path:
        return self._dir

    def get(self, failed_record_hash: str) -> RetryJournal | None:
        return self._journals.get(failed_record_hash)

    def create(self, failed_record_hash: str) -> RetryJournal:
        """The (still empty) journal of a new retry of ``failed_record_hash``; refused once that
        failed record has any retry event (a journal admits exactly one retry)."""
        known = self._journals.get(failed_record_hash)
        if known is not None and known.entries:
            raise RetryAdmissionError(
                "this failed round already has a retry admission; a failed retry yields a new "
                "failed record, a new review packet and a new journal"
            )
        journal = known or RetryJournal(
            self._dir / f"{failed_record_hash}.jsonl",
            loop_id=self.loop_id,
            failed_record_hash=failed_record_hash,
        )
        self._journals[failed_record_hash] = journal
        return journal

    @property
    def journals(self) -> tuple[RetryJournal, ...]:
        """Every journal holding at least one event, ordered by failed record hash."""
        return tuple(journal for _, journal in sorted(self._journals.items()) if journal.entries)

    def positions(self) -> list[dict[str, Any]]:
        """``heads["retry_admission"]``: the position of every non-empty journal, sorted."""
        return [journal.position() for journal in self.journals]


# ------------------------------------------------------------------------------------ reducer


def reduce_retry_ledger_tail(
    journal: RetryJournal,
    ledger_entries: Sequence[JournalEntry],
    *,
    loop_id: str,
    allow_later_entries: bool = False,
) -> RetryRecovery:
    """Recognize the exact contiguous ``reevaluate`` suffix a retry PREPARE authorizes.

    Pure: never appends, calls a Provider, or mutates a ledger. Without a COMMIT the ledger must
    end in an exact prefix of the manifest after the PREPARE baseline, and ``missing_items`` is
    the unique suffix recovery may still append; with a COMMIT every manifest entry must be there
    and match the COMMIT's positions. ``allow_later_entries`` (a checkpointed, historical retry
    only) accepts later rounds' entries after the retry's own; otherwise any extra, divergent,
    reordered or repeated line fails closed.
    """
    prepare, commit = journal.prepare, journal.commit
    if prepare is None:
        raise RetryAdmissionError(f"{journal.path} holds no retry PREPARE")
    raw = prepare.payload
    if raw["loop_id"] != loop_id or journal.loop_id != loop_id:
        raise RetryAdmissionError("retry PREPARE is bound to another loop")
    items = manifest_items(raw["manifest"])
    baseline_seq, baseline_hash = raw["ledger_baseline_seq"], raw["ledger_baseline_hash"]
    if baseline_seq > len(ledger_entries):
        raise RetryAdmissionError("TrialLedger is shorter than the prepared baseline")
    observed = ledger_entries[baseline_seq - 1].hash if baseline_seq else GENESIS_HASH
    if observed != baseline_hash:
        raise RetryAdmissionError("TrialLedger does not match the prepared baseline")
    proposed = {item.attempt for item in items}
    for entry in ledger_entries[:baseline_seq]:
        if entry.type == "reevaluate" and entry.payload.get("attempt") in proposed:
            raise RetryAdmissionError("a retry attempt key was already used in the TrialLedger")
    suffix = tuple(ledger_entries[baseline_seq:])
    own = suffix[: len(items)]
    if len(suffix) > len(items) and (commit is None or not allow_later_entries):
        raise RetryAdmissionError("TrialLedger holds entries beyond the retry manifest")
    for item, entry in zip(items, own, strict=False):
        if not _ledger_entry_matches(entry, item):
            raise RetryAdmissionError("TrialLedger retry prefix differs from the PREPARE manifest")
    retry_id = str(raw["retry_id"])
    failed = str(raw["failed_record_hash"])
    if commit is not None:
        if len(own) != len(items):
            raise RetryAdmissionError("retry COMMIT exists before every manifest entry")
        positions = commit.payload["ledger_entries"]
        if positions != [{"seq": entry.seq, "hash": entry.hash} for entry in own]:
            raise RetryAdmissionError("retry COMMIT ledger positions differ from the TrialLedger")
        last = own[-1]
        if (commit.payload["ledger_head_seq"], commit.payload["ledger_head_hash"]) != (
            last.seq,
            last.hash,
        ):
            raise RetryAdmissionError("retry COMMIT does not bind the final TrialLedger head")
        return RetryRecovery(retry_id, failed, items, own, (), prepare, commit)
    return RetryRecovery(retry_id, failed, items, own, items[len(own) :], prepare, None)


# --------------------------------------------------------------------------------- validation


def _validate_prepare(payload: Mapping[str, Any], loop_id: str, failed_record_hash: str) -> None:
    if not isinstance(payload, Mapping) or set(payload) != _PREPARE_KEYS:
        raise RetryAdmissionError("retry_prepare has unknown or missing fields")
    if type(payload["state_version"]) is not int or payload["state_version"] != 6:
        raise RetryAdmissionError("retry_prepare must bind durable state version 6")
    if payload["loop_id"] != loop_id:
        raise RetryAdmissionError("retry_prepare must bind the current loop_id")
    if payload["failed_record_hash"] != failed_record_hash:
        raise RetryAdmissionError(
            "retry_prepare names another failed record than its journal is bound to"
        )
    validate_reviewer(payload["reviewer"])
    for key in ("packet_hash", "failed_record_hash", "ledger_baseline_hash", "retry_id"):
        if not isinstance(payload[key], str) or _HASH.fullmatch(payload[key]) is None:
            raise RetryAdmissionError(f"retry_prepare {key} must be lowercase SHA-256")
    packet = payload["packet"]
    if not isinstance(packet, Mapping) or content_hash(packet) != payload["packet_hash"]:
        raise RetryAdmissionError("retry_prepare packet hash does not match its canonical payload")
    try:
        packet_record_hash = packet["loop_record"]["record_content_hash"]
    except (KeyError, TypeError) as exc:
        raise RetryAdmissionError("retry packet does not bind a failed loop record") from exc
    if packet_record_hash != payload["failed_record_hash"]:
        raise RetryAdmissionError("retry packet names another failed record")
    seq = payload["ledger_baseline_seq"]
    if type(seq) is not int or seq < 0:
        raise RetryAdmissionError("retry_prepare ledger baseline sequence must be non-negative")
    items = manifest_items(payload["manifest"])
    if [item.payload() for item in items] != payload["manifest"]:
        raise RetryAdmissionError("retry_prepare manifest is not in its canonical form")
    expected_id = retry_id_for(
        failed_record_hash=payload["failed_record_hash"],
        packet_hash=payload["packet_hash"],
        reviewer=payload["reviewer"],
        manifest=items,
        ledger_baseline=(seq, payload["ledger_baseline_hash"]),
    )
    if payload["retry_id"] != expected_id:
        raise RetryAdmissionError("retry_prepare retry_id does not identify its own request")


def _validate_commit(payload: Mapping[str, Any], prepare: JournalEntry) -> None:
    if not isinstance(payload, Mapping) or set(payload) != _COMMIT_KEYS:
        raise RetryAdmissionError("retry_commit has unknown or missing fields")
    raw = prepare.payload
    if (
        type(payload["state_version"]) is not int
        or payload["state_version"] != 6
        or payload["loop_id"] != raw["loop_id"]
        or payload["retry_id"] != raw["retry_id"]
        or payload["failed_record_hash"] != raw["failed_record_hash"]
    ):
        raise RetryAdmissionError(
            "retry_commit binds another state version, loop, retry or failed record"
        )
    if (
        type(payload["prepare_seq"]) is not int
        or payload["prepare_seq"] != prepare.seq
        or payload["prepare_hash"] != prepare.hash
    ):
        raise RetryAdmissionError("retry_commit does not bind the exact PREPARE entry")
    positions = payload["ledger_entries"]
    if not isinstance(positions, list) or len(positions) != len(raw["manifest"]):
        raise RetryAdmissionError("retry_commit must name every ordered TrialLedger entry")
    previous_seq = raw["ledger_baseline_seq"]
    for position in positions:
        if not isinstance(position, Mapping) or set(position) != {"seq", "hash"}:
            raise RetryAdmissionError("retry_commit has an invalid ledger entry position")
        if (
            type(position["seq"]) is not int
            or position["seq"] != previous_seq + 1
            or not isinstance(position["hash"], str)
            or _HASH.fullmatch(position["hash"]) is None
        ):
            raise RetryAdmissionError("retry_commit has an invalid ledger sequence or hash")
        previous_seq = position["seq"]
    if (
        type(payload["ledger_head_seq"]) is not int
        or payload["ledger_head_seq"] != previous_seq
        or payload["ledger_head_hash"] != positions[-1]["hash"]
    ):
        raise RetryAdmissionError("retry_commit does not name its final TrialLedger entry")
    checkpoint_seq = payload["memory_checkpoint_seq"]
    if type(checkpoint_seq) is not int or checkpoint_seq < 2:
        raise RetryAdmissionError("retry_commit has an invalid memory checkpoint sequence")


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

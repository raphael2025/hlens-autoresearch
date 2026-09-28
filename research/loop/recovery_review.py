"""Read-only evidence projection for ADR-0070 failed experiment rounds.

This module never opens a state directory or performs recovery. Its caller supplies a currently
locked ``DurableState`` returned by ``open_state`` and must prevent concurrent mutation while a
packet is being built. See ADR-0071 for the evidence limits and trust boundary.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from core.domain.base import canonical_json, content_hash
from research.loop.durable import BETWEEN_ROUNDS, ROUND_MEMORY, DurableState
from research.persistence.journal import GENESIS_HASH, JournalEntry

__all__ = [
    "FailedRoundReviewPacket",
    "FailedRoundReviewRefused",
    "failed_round_review_packet",
]


_PACKET_VERSION = "1.0.0"
_EVIDENCE_GAPS = (
    "per_trial_outcomes_before_batch_completion_not_persisted",
    "exact_failing_trial_index_not_persisted",
    "full_exception_traceback_not_persisted",
    "audit_journal_envelope_hash_not_exposed_by_loop_audit_log",
    "external_llm_or_provider_internal_state_unavailable",
)


class FailedRoundReviewRefused(ValueError):
    """The loaded state cannot support an unambiguous failed-round evidence projection."""


@dataclass(frozen=True, slots=True)
class FailedRoundReviewPacket:
    """Immutable JSON-backed packet; ``payload()`` returns a fresh decoded copy."""

    _canonical_payload: str
    packet_hash: str

    def __post_init__(self) -> None:
        try:
            value = json.loads(self._canonical_payload)
        except (TypeError, ValueError) as exc:
            raise ValueError("review packet payload is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("review packet payload must be a JSON object")
        if canonical_json(value) != self._canonical_payload:
            raise ValueError("review packet payload is not canonical JSON")
        if content_hash(value) != self.packet_hash:
            raise ValueError("review packet hash does not match its payload")

    def payload(self) -> dict[str, Any]:
        """Return a detached JSON-compatible copy of the packet payload."""
        return cast(dict[str, Any], json.loads(self._canonical_payload))


def failed_round_review_packet(
    state: DurableState,
) -> FailedRoundReviewPacket | None:
    """Project the final failed experiment round from a currently opened durable state.

    Returns ``None`` when there is no recorded round or its final experiment stage did not fail.
    Invalid opener preconditions or inconsistent audit/checkpoint/ledger evidence raise
    ``FailedRoundReviewRefused`` instead of returning a misleading empty packet.
    """
    if not isinstance(state, DurableState):
        raise FailedRoundReviewRefused("a DurableState returned by open_state is required")
    if not state.audit.durable:
        raise FailedRoundReviewRefused("the loop audit is not durable")
    if state.lock is None or not state.lock.held:
        raise FailedRoundReviewRefused("the opened state lock is not held")
    if state.audit.open_round is not None:
        raise FailedRoundReviewRefused("the audit has an unrecorded open round")
    if not state.audit.verify():
        raise FailedRoundReviewRefused("the in-memory audit records do not verify")

    records = state.audit.records
    if not records:
        return None
    record = records[-1]
    experiment_stages = tuple(stage for stage in record.stages if stage.name == "experiment")
    failed_stages = tuple(stage for stage in experiment_stages if stage.status.value == "FAILED")
    if not failed_stages:
        return None
    if len(experiment_stages) != 1:
        raise FailedRoundReviewRefused("the failed record has an ambiguous experiment stage")
    if record.round_index != len(records) - 1:
        raise FailedRoundReviewRefused("the final record index is inconsistent")

    memory_entries = state.checkpoint.journal.entries
    round_entries = tuple(entry for entry in memory_entries if entry.type == ROUND_MEMORY)
    if len(round_entries) != len(records):
        raise FailedRoundReviewRefused("round checkpoints do not match the audit record count")
    for index, (known_record, entry) in enumerate(zip(records, round_entries, strict=True)):
        checkpoint_payload = entry.payload
        if (
            checkpoint_payload.get("round_index") != index
            or checkpoint_payload.get("record_hash") != known_record.record_hash
        ):
            raise FailedRoundReviewRefused(
                f"round checkpoint {index} does not match its audit record"
            )

    checkpoint = round_entries[record.round_index]
    checkpoint_payload = checkpoint.payload
    # Read-only detached snapshot of the verified TrialLedger journal (never its writable journal).
    ledger_snapshot = state.memory.ledger.journal_snapshot()
    if ledger_snapshot is None:
        raise FailedRoundReviewRefused("the TrialLedger has no verified append-only journal")
    ledger_entries = ledger_snapshot.entries

    current_seq, current_hash = _ledger_position(checkpoint_payload, "failed-round checkpoint")
    previous_seq = 0
    previous_hash = GENESIS_HASH
    if record.round_index > 0:
        previous_checkpoint = round_entries[record.round_index - 1]
        previous_seq, previous_hash = _ledger_position(
            previous_checkpoint.payload, "previous-round checkpoint"
        )

    _verify_boundary(ledger_entries, previous_seq, previous_hash, "previous-round checkpoint")
    _verify_boundary(ledger_entries, current_seq, current_hash, "failed-round checkpoint")
    if previous_seq > current_seq:
        raise FailedRoundReviewRefused("the TrialLedger checkpoint range moves backwards")
    if current_seq != len(ledger_entries):
        raise FailedRoundReviewRefused(
            "the TrialLedger has entries beyond the failed-round checkpoint"
        )

    raw_ledger_slice = ledger_entries[previous_seq:current_seq]
    expected_prev_hash = previous_hash
    for expected_seq, entry in enumerate(raw_ledger_slice, start=previous_seq + 1):
        if entry.seq != expected_seq or entry.prev_hash != expected_prev_hash:
            raise FailedRoundReviewRefused(
                "the TrialLedger slice is not contiguous with its checkpoint"
            )
        expected_prev_hash = entry.hash
    if expected_prev_hash != current_hash:
        raise FailedRoundReviewRefused("the TrialLedger slice does not end at its checkpoint hash")

    # Valid approvals may append between-round checkpoints after the failed round. They may not
    # move TrialLedger; this verifies the allowed tail without mistaking its tip for a round line.
    checkpoint_index = memory_entries.index(checkpoint)
    for entry in memory_entries[checkpoint_index + 1 :]:
        if entry.type != BETWEEN_ROUNDS:
            raise FailedRoundReviewRefused(
                "unexpected memory journal entry follows the failed round"
            )
        tail_seq, tail_hash = _ledger_position(entry.payload, "between-rounds checkpoint")
        if (tail_seq, tail_hash) != (current_seq, current_hash):
            raise FailedRoundReviewRefused("a between-rounds checkpoint moved the TrialLedger")

    record_payload = _json_copy(record.payload())
    stage_payload = _json_copy(failed_stages[0].payload())
    transitions = record_payload.get("transitions")
    if not isinstance(transitions, list):
        raise FailedRoundReviewRefused("the audit record has no readable transition payload")
    payload: dict[str, Any] = {
        "packet_version": _PACKET_VERSION,
        "loop_record": {
            "payload": record_payload,
            "record_content_hash": record.record_hash,
        },
        "failed_experiment_stage": stage_payload,
        "lifecycle_transitions": transitions,
        "round_memory_entry": _entry_payload(checkpoint),
        "trial_ledger_slice": {
            "previous_seq_exclusive": previous_seq,
            "failed_round_seq_inclusive": current_seq,
            "entries": [_entry_payload(entry) for entry in raw_ledger_slice],
        },
        "evidence_gaps": list(_EVIDENCE_GAPS),
        "evidence_asymmetry": (
            "Lifecycle transitions and TrialLedger entries may exist for the round while the "
            "complete per-trial outcomes do not."
        ),
    }
    wire = canonical_json(payload)
    return FailedRoundReviewPacket(_canonical_payload=wire, packet_hash=content_hash(payload))


def _ledger_position(payload: Mapping[str, Any], label: str) -> tuple[int, str]:
    try:
        heads = payload["heads"]
        position = heads["trial_ledger"]
        seq, hash_ = position["seq"], position["hash"]
    except (KeyError, TypeError) as exc:
        raise FailedRoundReviewRefused(f"{label} has no TrialLedger position") from exc
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise FailedRoundReviewRefused(f"{label} has an invalid TrialLedger sequence")
    if not isinstance(hash_, str) or len(hash_) != 64:
        raise FailedRoundReviewRefused(f"{label} has an invalid TrialLedger hash")
    return seq, hash_


def _verify_boundary(entries: tuple[JournalEntry, ...], seq: int, hash_: str, label: str) -> None:
    if seq > len(entries):
        raise FailedRoundReviewRefused(f"{label} is beyond the available TrialLedger journal")
    observed = entries[seq - 1].hash if seq else GENESIS_HASH
    if observed != hash_:
        raise FailedRoundReviewRefused(f"{label} does not match the TrialLedger journal")


def _entry_payload(entry: JournalEntry) -> dict[str, Any]:
    return {
        "seq": entry.seq,
        "type": entry.type,
        "payload": _json_copy(entry.payload),
        "prev_hash": entry.prev_hash,
        "hash": entry.hash,
    }


def _json_copy(value: Any) -> Any:
    return json.loads(canonical_json(value))

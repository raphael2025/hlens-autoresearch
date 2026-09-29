"""ADR-0086 决策 2: the file-backed, append-only Retirement Registry (`infrastructure.registry.
retirement`).

Every subject here is a TEST ONLY `Ref` — nothing is really promoted, ACTIVE or retired; the
registry only persists whatever `RetirementRecord` it is given (see the module's "honest boundary"
note).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.domain.base import Kind, Ref, canonical_json
from core.domain.execution import ExecutionMode
from core.domain.research import RetirementRecord
from infrastructure.event_bus.journal import AppendOnlyJournal
from infrastructure.registry.registry import (
    DuplicateRecord,
    RegistryCorrupted,
    RegistryError,
    RegistryLocked,
    RegistryRefused,
)
from infrastructure.registry.retirement import (
    FORMAT_VERSION,
    RETIREMENT_RECORDED,
    RetirementRegistry,
    verify_integrity,
)

SUBJECT = Ref(kind=Kind.STRATEGY, name="s_retire_test", version="1.0.0")
OTHER_VERSION = Ref(kind=Kind.STRATEGY, name="s_retire_test", version="2.0.0")
OTHER_SUBJECT = Ref(kind=Kind.STRATEGY, name="s_retire_other", version="1.0.0")
RECORDED_AT = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)


def _record(
    subject: Ref = SUBJECT,
    reason: str = "TEST ONLY: sample retired",
    **overrides: Any,
) -> RetirementRecord:
    fields: dict[str, Any] = {
        "subject_ref": subject,
        "retirement_reason": reason,
        "evidence": ("report:test-only-evidence",),
        "active_from": datetime(2026, 1, 1, tzinfo=UTC),
        "active_to": datetime(2026, 6, 1, tzinfo=UTC),
        "execution_mode": ExecutionMode.SIMULATED,
        "lessons": "TEST ONLY: no lesson",
        "recorded_at": RECORDED_AT,
        **overrides,
    }
    return RetirementRecord(**fields)


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    """A temporary registry directory and its temporary sibling anchor file."""
    return tmp_path / "retirements", tmp_path / "retirements.anchor.jsonl"


def _open(paths: tuple[Path, Path], *, anchored: bool = True) -> RetirementRegistry:
    root, anchor = paths
    return RetirementRegistry(root, anchor=anchor if anchored else None)


def _journal(paths: tuple[Path, Path]) -> Path:
    return paths[0] / "retirements.jsonl"


def _payloads(paths: tuple[Path, Path]) -> list[dict[str, Any]]:
    return [dict(entry.payload) for entry in AppendOnlyJournal(_journal(paths)).entries]


def _forge(paths: tuple[Path, Path], records: list[tuple[str, dict[str, Any]]]) -> None:
    """Rewrite the registry as a **valid chain** of ``records`` with a matching anchor, so only
    the registry's own rules can refuse it (the journal and anchor checks pass)."""
    root, anchor = paths
    _journal(paths).unlink()
    if anchor.exists():
        anchor.unlink()
    journal = AppendOnlyJournal(_journal(paths))
    heads = AppendOnlyJournal(anchor)
    for type_, payload in records:
        journal.append(type_, payload)
        heads.append(
            "retirement_registry.head", {"length": len(journal), "head": journal.head_hash}
        )
    assert root.is_dir()


# ======================================================================================
# register, replay, read
# ======================================================================================


def test_a_retirement_is_recorded_and_replayed(paths: tuple[Path, Path]) -> None:
    record = _record()
    with _open(paths) as registry:
        stored = registry.register_retirement(record)
        assert len(registry) == 1
    assert stored == record
    assert stored.subject_ref == SUBJECT
    with _open(paths) as again:  # a fresh process: full replay and verification
        assert again.retirements == (record,)
        assert again.retirement_of(SUBJECT) == record
        assert again.is_retired(SUBJECT) is True
        assert len(again) == 1
    [payload] = _payloads(paths)
    assert set(payload) == {"format_version", "record", "record_id"}
    assert payload["format_version"] == FORMAT_VERSION
    assert payload["record_id"] == record.content_hash()
    assert payload["record"] == record.model_dump(mode="json")


def test_verify_integrity_reads_without_writing(paths: tuple[Path, Path]) -> None:
    with _open(paths) as registry:
        registry.register_retirement(_record())
    before = _journal(paths).read_bytes()
    assert verify_integrity(paths[0], anchor=paths[1]) == 1
    assert _journal(paths).read_bytes() == before  # a read-only audit, no line appended


# ======================================================================================
# "insufficient history" — an empty / freshly created registry
# ======================================================================================


def test_a_fresh_registry_has_no_records_and_creates_its_directory(tmp_path: Path) -> None:
    root = tmp_path / "not_yet_created" / "retirements"
    assert not root.exists()
    with RetirementRegistry(root) as registry:  # no anchor given: still valid (optional)
        assert len(registry) == 0
        assert registry.retirements == ()
        assert registry.retirement_of(SUBJECT) is None
        assert registry.is_retired(SUBJECT) is False
    assert root.is_dir()
    assert not (root / "retirements.jsonl").exists()  # nothing was ever appended


def test_verify_integrity_of_an_empty_registry_is_zero(paths: tuple[Path, Path]) -> None:
    assert verify_integrity(paths[0], anchor=paths[1]) == 0


# ======================================================================================
# same-object duplicate retirement is rejected (ADR-0086 决策 2)
# ======================================================================================


def test_the_same_subject_cannot_be_retired_twice(paths: tuple[Path, Path]) -> None:
    with _open(paths) as registry:
        registry.register_retirement(_record())
        before = (_journal(paths).read_bytes(), paths[1].read_bytes())
        # a different reason / lessons / recorded_at is still the same subject — still refused
        with pytest.raises(DuplicateRecord, match="already retired"):
            registry.register_retirement(
                _record(reason="TEST ONLY: a different story", recorded_at=RECORDED_AT)
            )
        assert len(registry) == 1
    assert (_journal(paths).read_bytes(), paths[1].read_bytes()) == before


def test_another_version_of_the_same_name_is_a_different_subject(paths: tuple[Path, Path]) -> None:
    """`target_identity()` includes the version: retiring v1.0.0 does not block v2.0.0."""
    with _open(paths) as registry:
        registry.register_retirement(_record(subject=SUBJECT))
        registry.register_retirement(_record(subject=OTHER_VERSION))
        assert len(registry) == 2
        assert registry.is_retired(SUBJECT) and registry.is_retired(OTHER_VERSION)


def test_another_name_is_a_different_subject(paths: tuple[Path, Path]) -> None:
    with _open(paths) as registry:
        registry.register_retirement(_record(subject=SUBJECT))
        registry.register_retirement(_record(subject=OTHER_SUBJECT))
        assert {r.subject_ref for r in registry.retirements} == {SUBJECT, OTHER_SUBJECT}


def test_a_replayed_duplicate_is_corruption(paths: tuple[Path, Path]) -> None:
    with _open(paths) as registry:
        registry.register_retirement(_record())
    [first] = _payloads(paths)
    _forge(paths, [(RETIREMENT_RECORDED, first), (RETIREMENT_RECORDED, first)])
    with pytest.raises(RegistryCorrupted, match="already retired"):
        _open(paths)


# ======================================================================================
# a future append never changes an already-read past record
# ======================================================================================


def test_a_later_retirement_does_not_change_an_earlier_ones_identity(
    paths: tuple[Path, Path],
) -> None:
    with _open(paths) as registry:
        first = registry.register_retirement(_record(subject=SUBJECT))
        first_hash = first.content_hash()
        first_head_after_one = registry.head_hash
        registry.register_retirement(_record(subject=OTHER_SUBJECT))
        # the first record, once read, is unchanged by the second (later) append
        assert registry.retirement_of(SUBJECT) == first
        retrieved = registry.retirement_of(SUBJECT)
        assert retrieved is not None and retrieved.content_hash() == first_hash
    with _open(paths) as again:
        assert again.retirements[0] == first
        assert again.retirements[0].content_hash() == first_hash
        # the journal entry for the first record is exactly what it was before the second append
        replayed_first_entry = AppendOnlyJournal(_journal(paths)).entries[0]
        assert replayed_first_entry.hash == first_head_after_one
        assert replayed_first_entry.payload["record_id"] == first_hash


# ======================================================================================
# hand-computed exact values
# ======================================================================================


def test_record_id_is_by_hand_the_sha256_of_the_records_canonical_json(
    paths: tuple[Path, Path],
) -> None:
    record = _record()
    with _open(paths) as registry:
        registry.register_retirement(record)
    [payload] = _payloads(paths)
    # independently recomputed, not via RetirementRecord.content_hash()
    dumped = record.model_dump(mode="json")
    assert dumped == payload["record"]
    excluded = {"created_at"}  # Contract's default _non_semantic_fields (recorded_at IS semantic)
    semantic = {k: v for k, v in dumped.items() if k not in excluded}
    expected = hashlib.sha256(canonical_json(semantic).encode("utf-8")).hexdigest()
    assert payload["record_id"] == expected == record.content_hash()


def test_the_journal_line_hash_is_by_hand_the_sha256_of_seq_type_payload_prev_hash(
    paths: tuple[Path, Path],
) -> None:
    with _open(paths) as registry:
        registry.register_retirement(_record())
    [entry] = AppendOnlyJournal(_journal(paths)).entries
    fields = {
        "seq": entry.seq,
        "type": entry.type,
        "payload": entry.payload,
        "prev_hash": entry.prev_hash,
    }
    body = canonical_json(fields)
    assert entry.hash == hashlib.sha256(body.encode("utf-8")).hexdigest()


# ======================================================================================
# anchor: optional, truncation detection when present
# ======================================================================================


def test_the_anchor_is_optional(paths: tuple[Path, Path]) -> None:
    root, _anchor = paths
    with RetirementRegistry(root) as registry:  # no anchor kwarg at all
        registry.register_retirement(_record())
        assert len(registry) == 1
    with RetirementRegistry(root) as again:
        assert len(again) == 1


def test_the_anchor_must_live_outside_the_registry_directory(tmp_path: Path) -> None:
    root = tmp_path / "retirements"
    with pytest.raises(ValueError, match="outside"):
        RetirementRegistry(root, anchor=root / "anchor.jsonl")


def test_whole_line_truncation_is_detected_by_the_anchor(paths: tuple[Path, Path]) -> None:
    with _open(paths) as registry:
        registry.register_retirement(_record(subject=SUBJECT))
        registry.register_retirement(_record(subject=OTHER_SUBJECT))
    lines = _journal(paths).read_bytes().splitlines(keepends=True)
    assert len(lines) == 2
    _journal(paths).write_bytes(lines[0])  # a valid, shorter chain
    AppendOnlyJournal(_journal(paths))  # the journal alone cannot tell
    with pytest.raises(RegistryCorrupted, match="removed"):
        _open(paths)


def test_without_an_anchor_truncation_is_not_detected(paths: tuple[Path, Path]) -> None:
    """Documents the honest boundary: no anchor means no truncation detection (module docs)."""
    root, _unused_anchor = paths
    with RetirementRegistry(root) as registry:
        registry.register_retirement(_record(subject=SUBJECT))
        registry.register_retirement(_record(subject=OTHER_SUBJECT))
    lines = _journal(paths).read_bytes().splitlines(keepends=True)
    _journal(paths).write_bytes(lines[0])  # a valid, shorter chain
    with RetirementRegistry(root) as reopened:  # opens fine: no anchor to catch the truncation
        assert len(reopened) == 1


def test_the_one_crash_window_is_re_anchored(paths: tuple[Path, Path]) -> None:
    with _open(paths) as registry:
        registry.register_retirement(_record(subject=SUBJECT))
    saved_anchor = paths[1].read_bytes()
    with _open(paths) as registry:
        registry.register_retirement(_record(subject=OTHER_SUBJECT))
    paths[1].write_bytes(saved_anchor)  # crash: the record is fsync'd, its anchor line is not
    with _open(paths) as registry:
        assert len(registry) == 2
    heads = AppendOnlyJournal(paths[1]).entries
    assert [entry.payload["length"] for entry in heads] == [1, 2]


# ======================================================================================
# crash tail / tampering (the journal itself)
# ======================================================================================


def test_a_partial_trailing_line_is_refused(paths: tuple[Path, Path]) -> None:
    with _open(paths) as registry:
        registry.register_retirement(_record())
    with _journal(paths).open("ab") as handle:
        handle.write(b'{"seq": 2, "type": "retirement.recorded"')  # a crash mid-write
    with pytest.raises(RegistryCorrupted, match="partial trailing line"):
        _open(paths)


def test_a_tampered_record_breaks_the_chain(paths: tuple[Path, Path]) -> None:
    with _open(paths) as registry:
        registry.register_retirement(_record())
    text = _journal(paths).read_text(encoding="utf-8")
    _journal(paths).write_text(text.replace("TEST ONLY", "TAMPERED"), "utf-8")
    with pytest.raises(RegistryCorrupted, match="hash"):
        _open(paths)


def test_an_unknown_record_type_makes_the_registry_unopenable(paths: tuple[Path, Path]) -> None:
    with _open(paths) as registry:
        registry.register_retirement(_record())
    [payload] = _payloads(paths)
    _forge(paths, [("retirement.unrecorded", payload)])
    with pytest.raises(RegistryCorrupted, match="unknown record type"):
        _open(paths)


# ======================================================================================
# rule violations on replay (a valid chain + anchor; only the registry's rules can refuse)
# ======================================================================================


def _reidentified(record: RetirementRecord, **overrides: Any) -> dict[str, Any]:
    dumped = record.model_dump(mode="json")
    base = {"format_version": FORMAT_VERSION, "record": dumped, "record_id": record.content_hash()}
    return {**base, **overrides}


REPLAY_CASES: list[tuple[str, Callable[[RetirementRecord], dict[str, Any]], str]] = [
    ("format_version", lambda r: _reidentified(r, format_version="2.0.0"), "format_version"),
    ("record_id", lambda r: _reidentified(r, record_id="0" * 64), "record_id"),
    (
        "extra key",
        lambda r: {**_reidentified(r), "note": "x"},
        "exactly the keys",
    ),
    (
        "missing key",
        lambda r: {k: v for k, v in _reidentified(r).items() if k != "record_id"},
        "exactly the keys",
    ),
    (
        "record does not validate",
        lambda r: {
            **_reidentified(r),
            "record": {**r.model_dump(mode="json"), "retirement_reason": ""},
        },
        "does not validate",
    ),
]


@pytest.mark.parametrize(
    ("label", "mutate", "match"), REPLAY_CASES, ids=[c[0] for c in REPLAY_CASES]
)
def test_a_record_breaking_a_rule_makes_the_registry_unopenable(
    paths: tuple[Path, Path],
    label: str,
    mutate: Callable[[RetirementRecord], dict[str, Any]],
    match: str,
) -> None:
    record = _record()
    with _open(paths) as registry:
        registry.register_retirement(record)
    forged = mutate(record)
    _forge(paths, [(RETIREMENT_RECORDED, json.loads(json.dumps(forged)))])
    with pytest.raises(RegistryCorrupted, match=match):
        _open(paths)


# ======================================================================================
# type / validation errors on the public API
# ======================================================================================


def test_register_retirement_needs_a_retirement_record(paths: tuple[Path, Path]) -> None:
    with _open(paths) as registry, pytest.raises(RegistryRefused, match="RetirementRecord"):
        registry.register_retirement("not a record")  # type: ignore[arg-type]


def test_a_malformed_retirement_reason_is_refused_at_construction_not_by_the_registry() -> None:
    """H4: the registry does not weaken `RetirementRecord`'s own validation; the contract itself
    already refuses a blank reason (min_length=1), before the registry ever sees it."""
    with pytest.raises(ValidationError):
        RetirementRecord(subject_ref=SUBJECT, retirement_reason="")


# ======================================================================================
# single writer / closed / poisoned
# ======================================================================================


def test_a_single_writer_holds_the_registry(paths: tuple[Path, Path]) -> None:
    first = _open(paths)
    with pytest.raises(RegistryLocked):
        _open(paths)
    first.close()
    with _open(paths) as second:
        assert len(second) == 0
    with pytest.raises(RegistryError, match="closed"):
        first.register_retirement(_record())
    with pytest.raises(RegistryError, match="closed"):
        first.retirement_of(SUBJECT)


def test_a_write_failure_after_the_journal_line_poisons_the_instance(
    paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _open(paths)
    assert registry._anchor is not None

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("TEST ONLY: the anchor disk is gone")

    monkeypatch.setattr(registry._anchor, "append", fail)
    with pytest.raises(RegistryCorrupted, match="OSError"):
        registry.register_retirement(_record())
    # poisoned: every further read and write refuses, nothing comes from memory
    for read in (
        lambda: registry.retirement_of(SUBJECT),
        lambda: registry.is_retired(SUBJECT),
        lambda: registry.retirements,
        lambda: len(registry),
    ):
        with pytest.raises(RegistryCorrupted, match="poisoned"):
            read()
    with pytest.raises(RegistryCorrupted, match="poisoned"):
        registry.register_retirement(_record(subject=OTHER_SUBJECT))
    assert len(AppendOnlyJournal(_journal(paths))) == 1  # the record itself is durable
    monkeypatch.undo()
    # the lock was released; a fresh open replays, verifies and re-anchors the one crash window
    with _open(paths) as again:
        assert again.retirement_of(SUBJECT) is not None
        assert len(again) == 1
    assert [e.payload["length"] for e in AppendOnlyJournal(paths[1]).entries] == [1]


def test_a_refused_duplicate_does_not_poison_the_instance(paths: tuple[Path, Path]) -> None:
    with _open(paths) as registry:
        registry.register_retirement(_record())
        with pytest.raises(DuplicateRecord):
            registry.register_retirement(_record())
        # still usable: a *refused* registration (nothing written) is not the same as a poisoning
        # write failure (something written, then a later step failed)
        assert len(registry) == 1
        assert registry.retirement_of(SUBJECT) is not None

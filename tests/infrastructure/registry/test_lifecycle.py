"""ADR-0098 §1: the file-backed, append-only Lifecycle Registry (`infrastructure.registry.lifecycle`).

Every subject here is a TEST ONLY `Ref`; nothing is really promoted or ACTIVE — the registry only
persists and replays the legal transitions it is given.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.domain.base import Kind, Ref
from core.lifecycle.strategy import LifecycleState, LifecycleTransition
from infrastructure.event_bus.journal import GENESIS_HASH, AppendOnlyJournal
from infrastructure.registry.lifecycle import (
    FORMAT_VERSION,
    HeadMismatch,
    LifecycleHead,
    LifecycleRegistry,
    UnknownHead,
    verify_integrity_snapshot,
)
from infrastructure.registry.registry import RegistryCorrupted, RegistryLocked, RegistryRefused

S = LifecycleState
SUBJECT = Ref(kind=Kind.STRATEGY, name="s_lifecycle_test", version="1.0.0")
OTHER = Ref(kind=Kind.STRATEGY, name="s_lifecycle_other", version="1.0.0")
T0 = datetime(2026, 9, 1, tzinfo=UTC)

#: The legal path IDEA → ACTIVE (ADR-0006), with approvals where they are required.
PATH_TO_ACTIVE = (
    (S.IDEA, S.CANDIDATE, None),
    (S.CANDIDATE, S.VALIDATION, None),
    (S.VALIDATION, S.OOS, None),
    (S.OOS, S.PAPER, "TEST ONLY approver"),
    (S.PAPER, S.PRODUCTION_CANDIDATE, None),
    (S.PRODUCTION_CANDIDATE, S.ACTIVE, "TEST ONLY approver"),
)


def _transition(
    subject: Ref, from_state: S, to_state: S, approved_by: str | None, step: int
) -> LifecycleTransition:
    return LifecycleTransition(
        subject=subject,
        from_state=from_state,
        to_state=to_state,
        reason="TEST ONLY transition",
        evidence=("report:test-only",),
        triggered_by="TEST ONLY",
        approved_by=approved_by,
        occurred_at=T0 + timedelta(hours=step),
    )


def _to_active(registry: LifecycleRegistry, subject: Ref = SUBJECT) -> LifecycleHead:
    head = registry.head
    for step, (from_state, to_state, approver) in enumerate(PATH_TO_ACTIVE):
        head = registry.append(
            _transition(subject, from_state, to_state, approver, step), expected_head=head
        )
    return head


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "lifecycle"


def test_an_empty_registry_has_the_genesis_head(root: Path) -> None:
    with LifecycleRegistry(root) as registry:
        assert registry.head == LifecycleHead(0, GENESIS_HASH)
        assert registry.active_set(registry.head).strategies == ()
        lifecycle = registry.lifecycle_of(SUBJECT, registry.head)
        assert lifecycle.current_state is S.IDEA
        assert lifecycle.record_hashes == ()


def test_a_legal_path_replays_to_active_and_survives_reopen(root: Path) -> None:
    with LifecycleRegistry(root) as registry:
        head = _to_active(registry)
        assert head.record_count == len(PATH_TO_ACTIVE)
        assert registry.active_set(head).strategies == (SUBJECT,)
    with LifecycleRegistry(root) as again:  # a fresh process: full replay and verification
        assert again.head == head
        lifecycle = again.lifecycle_of(SUBJECT, head)
        assert lifecycle.current_state is S.ACTIVE
        assert len(lifecycle.record_hashes) == len(PATH_TO_ACTIVE)
        assert lifecycle.history.subject == SUBJECT
    payload = dict(AppendOnlyJournal(root / "lifecycle.jsonl").entry(0).payload)
    assert payload["format_version"] == FORMAT_VERSION
    assert payload["record_index"] == 1
    assert payload["prev_record_hash"] == GENESIS_HASH
    assert payload["strategy"] == str(SUBJECT)


def test_a_stale_expected_head_writes_nothing(root: Path) -> None:
    with LifecycleRegistry(root) as registry:
        stale = registry.head
        registry.append(_transition(SUBJECT, S.IDEA, S.CANDIDATE, None, 0), expected_head=stale)
        with pytest.raises(HeadMismatch):
            registry.append(
                _transition(OTHER, S.IDEA, S.CANDIDATE, None, 0), expected_head=stale
            )
        assert len(registry) == 1


def test_an_illegal_or_out_of_state_transition_is_refused(root: Path) -> None:
    with LifecycleRegistry(root) as registry:
        with pytest.raises(RegistryRefused):  # not from the replayed state (IDEA)
            registry.append(
                _transition(SUBJECT, S.CANDIDATE, S.VALIDATION, None, 0),
                expected_head=registry.head,
            )
        with pytest.raises(RegistryRefused):  # not an ADR-0006 edge
            registry.append(
                _transition(SUBJECT, S.IDEA, S.ACTIVE, "TEST ONLY approver", 0),
                expected_head=registry.head,
            )
        assert len(registry) == 0


def test_degraded_leaves_the_active_set_but_pinned_heads_still_answer(root: Path) -> None:
    with LifecycleRegistry(root) as registry:
        active_head = _to_active(registry)
        degraded_head = registry.append(
            _transition(SUBJECT, S.ACTIVE, S.DEGRADED, None, 10), expected_head=active_head
        )
        assert registry.active_set(degraded_head).strategies == ()
        assert registry.active_set(active_head).strategies == (SUBJECT,)
        assert registry.head_for_hash(active_head.last_record_hash) == active_head
        with pytest.raises(UnknownHead):
            registry.verify_head(LifecycleHead(active_head.record_count, "a" * 64))
        with pytest.raises(UnknownHead):
            registry.verify_head(LifecycleHead(degraded_head.record_count + 1, "b" * 64))


def test_a_second_instance_is_locked_out(root: Path) -> None:
    with LifecycleRegistry(root), pytest.raises(RegistryLocked):
        LifecycleRegistry(root)


def test_a_tampered_record_refuses_to_open(root: Path) -> None:
    with LifecycleRegistry(root) as registry:
        registry.append(
            _transition(SUBJECT, S.IDEA, S.CANDIDATE, None, 0), expected_head=registry.head
        )
    path = root / "lifecycle.jsonl"
    path.write_text(path.read_text().replace("CANDIDATE", "VALIDATION", 1))
    with pytest.raises(RegistryCorrupted):
        LifecycleRegistry(root)


def test_the_anchor_detects_dropped_records(tmp_path: Path) -> None:
    root, anchor = tmp_path / "lifecycle", tmp_path / "lifecycle.anchor.jsonl"
    with LifecycleRegistry(root, anchor=anchor) as registry:
        _to_active(registry)
    path = root / "lifecycle.jsonl"
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines[:-1]))
    with pytest.raises(RegistryCorrupted):
        LifecycleRegistry(root, anchor=anchor)


def test_the_snapshot_audit_is_read_only(tmp_path: Path) -> None:
    root, anchor = tmp_path / "lifecycle", tmp_path / "lifecycle.anchor.jsonl"
    with LifecycleRegistry(root, anchor=anchor) as registry:
        head = _to_active(registry)
    before = (root / "lifecycle.jsonl").read_bytes(), anchor.read_bytes()
    report: dict[str, Any] = verify_integrity_snapshot(root, anchor=anchor)
    assert report["anchor_status"] == "VERIFIED"
    assert report["head"] == head.payload()
    assert report["active_strategies"] == [str(SUBJECT)]
    assert ((root / "lifecycle.jsonl").read_bytes(), anchor.read_bytes()) == before

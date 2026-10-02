"""ADR-0098 §1 (修订 2 §2 / §6): the resolver's lifecycle authority
(``research.operations.authority._resolve_lifecycle``) over a real ``LifecycleRegistry``.

The subject's replay at an **explicit** head, read from a read-only snapshot, truncated to
``occurred_at <= as_of``; it must end in ACTIVE, entered no later than the window start, with no
transition in ``(window.start, as_of]``. A head not on the chain (unknown, or a competing chain's
head) and a writer instance fail closed. Every subject, actor and time is TEST ONLY.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.lifecycle.strategy import LifecycleState, LifecycleTransition
from infrastructure.event_bus.journal import GENESIS_HASH
from infrastructure.registry.lifecycle import LifecycleHead, LifecycleRegistry
from research.operations.authority import (
    LIFECYCLE_HEAD_UNKNOWN,
    LIFECYCLE_NOT_ACTIVE,
    LIFECYCLE_UNAVAILABLE,
    AuthorityRefused,
    _resolve_lifecycle,
)
from research.operations.degradation import ObservationWindow
from tests.research.operations.authority_fixtures import (
    OTHER_SUBJECT,
    PATH_TO_ACTIVE,
    SUBJECT,
    transitions,
    write_registry,
)

S = LifecycleState
T0 = datetime(2026, 1, 1, tzinfo=UTC)  # the first transition (IDEA → CANDIDATE)
WINDOW = ObservationWindow(
    start=datetime(2026, 2, 1, tzinfo=UTC), end=datetime(2026, 3, 1, tzinfo=UTC), label="TEST ONLY"
)
AS_OF = datetime(2026, 3, 2, tzinfo=UTC)


def _active(start: datetime = T0) -> list[LifecycleTransition]:
    return list(transitions(SUBJECT, PATH_TO_ACTIVE, start))


def _snapshot(root: Path, anchor: Path | None = None) -> LifecycleRegistry:
    return LifecycleRegistry.open_snapshot(root, anchor=anchor)


def _refused(code: str, *args: object) -> AuthorityRefused:
    with pytest.raises(AuthorityRefused) as refused:
        _resolve_lifecycle(*args)  # type: ignore[arg-type]
    assert refused.value.code == code
    return refused.value


def test_an_active_subject_resolves_to_its_replay_and_record_hashes(tmp_path: Path) -> None:
    records = _active()
    heads = write_registry(tmp_path / "reg", records)
    with _snapshot(tmp_path / "reg") as snapshot:
        pinned, history, hashes = _resolve_lifecycle(
            snapshot, snapshot.head, SUBJECT, WINDOW, AS_OF
        )
        assert pinned == heads[-1] == snapshot.head
        assert history.current_state is S.ACTIVE
        assert history.transitions == tuple(records)
        replay = snapshot.lifecycle_of(SUBJECT, pinned)
        assert hashes == replay.record_hashes and len(hashes) == len(records)
        assert history.content_hash() == replay.history_hash


def test_other_subjects_records_do_not_enter_the_replay(tmp_path: Path) -> None:
    mine = _active()
    theirs = list(transitions(OTHER_SUBJECT, PATH_TO_ACTIVE[:3], T0 + timedelta(seconds=30)))
    interleaved = sorted(mine + theirs, key=lambda item: item.occurred_at)
    write_registry(tmp_path / "reg", interleaved)
    with _snapshot(tmp_path / "reg") as snapshot:
        _, history, hashes = _resolve_lifecycle(snapshot, snapshot.head, SUBJECT, WINDOW, AS_OF)
        assert history.transitions == tuple(mine)
        assert len(hashes) == len(mine)
        # the other subject (VALIDATION) is not ACTIVE
        _refused(LIFECYCLE_NOT_ACTIVE, snapshot, snapshot.head, OTHER_SUBJECT, WINDOW, AS_OF)


def test_a_subject_that_is_not_active_is_refused(tmp_path: Path) -> None:
    write_registry(tmp_path / "reg", list(transitions(SUBJECT, PATH_TO_ACTIVE[:-1], T0)))
    with _snapshot(tmp_path / "reg") as snapshot:
        refused = _refused(LIFECYCLE_NOT_ACTIVE, snapshot, snapshot.head, SUBJECT, WINDOW, AS_OF)
        assert "PRODUCTION_CANDIDATE" in str(refused)


def test_a_subject_without_records_replays_to_idea_and_is_refused(tmp_path: Path) -> None:
    write_registry(tmp_path / "reg", list(transitions(OTHER_SUBJECT, PATH_TO_ACTIVE, T0)))
    with _snapshot(tmp_path / "reg") as snapshot:
        refused = _refused(LIFECYCLE_NOT_ACTIVE, snapshot, snapshot.head, SUBJECT, WINDOW, AS_OF)
        assert "IDEA" in str(refused)


def test_the_genesis_head_resolves_nothing_active(tmp_path: Path) -> None:
    heads = write_registry(tmp_path / "reg", _active())
    with _snapshot(tmp_path / "reg") as snapshot:
        genesis = LifecycleHead(0, GENESIS_HASH)
        assert heads[0] == genesis == snapshot.verify_head(genesis)
        _refused(LIFECYCLE_NOT_ACTIVE, snapshot, genesis, SUBJECT, WINDOW, AS_OF)


def test_an_earlier_pinned_head_is_replayed_at_that_head_not_the_latest(tmp_path: Path) -> None:
    records = _active()
    heads = write_registry(tmp_path / "reg", records)
    with _snapshot(tmp_path / "reg") as snapshot:
        before_active = heads[-2]  # PRODUCTION_CANDIDATE at that head
        _refused(LIFECYCLE_NOT_ACTIVE, snapshot, before_active, SUBJECT, WINDOW, AS_OF)
        pinned, _, _ = _resolve_lifecycle(snapshot, heads[-1], SUBJECT, WINDOW, AS_OF)
        assert pinned == heads[-1]


def test_entering_active_after_the_window_start_is_refused(tmp_path: Path) -> None:
    # every transition lands inside the window: ACTIVE is entered after window.start
    write_registry(tmp_path / "reg", _active(WINDOW.start + timedelta(days=1)))
    with _snapshot(tmp_path / "reg") as snapshot:
        refused = _refused(LIFECYCLE_NOT_ACTIVE, snapshot, snapshot.head, SUBJECT, WINDOW, AS_OF)
        assert "after the window start" in str(refused)


def test_entering_active_exactly_at_the_window_start_is_accepted(tmp_path: Path) -> None:
    start = WINDOW.start - (len(PATH_TO_ACTIVE) - 2) * timedelta(minutes=1)
    records = _active(start)
    assert records[-1].occurred_at == WINDOW.start
    write_registry(tmp_path / "reg", records)
    with _snapshot(tmp_path / "reg") as snapshot:
        _, history, _ = _resolve_lifecycle(snapshot, snapshot.head, SUBJECT, WINDOW, AS_OF)
        assert history.current_state is S.ACTIVE


def test_a_round_trip_inside_the_window_that_ends_active_is_refused(tmp_path: Path) -> None:
    # ACTIVE → DEGRADED → REVALIDATION → ACTIVE inside the window: ends ACTIVE, but the ACTIVE
    # state of as_of was entered after the window start (it did not hold through the window)
    records = _active()
    records += transitions(
        SUBJECT, (S.ACTIVE, S.DEGRADED, S.REVALIDATION, S.ACTIVE), WINDOW.start + timedelta(days=3)
    )
    write_registry(tmp_path / "reg", records)
    with _snapshot(tmp_path / "reg") as snapshot:
        refused = _refused(LIFECYCLE_NOT_ACTIVE, snapshot, snapshot.head, SUBJECT, WINDOW, AS_OF)
        assert "after the window start" in str(refused)


def test_a_degradation_inside_the_window_is_refused_as_not_active(tmp_path: Path) -> None:
    records = _active()
    records += transitions(SUBJECT, (S.ACTIVE, S.DEGRADED), WINDOW.start + timedelta(days=3))
    write_registry(tmp_path / "reg", records)
    with _snapshot(tmp_path / "reg") as snapshot:
        refused = _refused(LIFECYCLE_NOT_ACTIVE, snapshot, snapshot.head, SUBJECT, WINDOW, AS_OF)
        assert "DEGRADED" in str(refused)


def test_a_round_trip_that_ends_active_before_the_window_is_accepted(tmp_path: Path) -> None:
    """Re-entering ACTIVE (through REVALIDATION) before the window start is a subject that is
    ACTIVE through the window: its whole replay is kept."""
    early = _active()
    early += transitions(
        SUBJECT, (S.ACTIVE, S.DEGRADED, S.REVALIDATION, S.ACTIVE), T0 + timedelta(days=5)
    )
    write_registry(tmp_path / "early", early)
    with _snapshot(tmp_path / "early") as snapshot:
        _, history, hashes = _resolve_lifecycle(snapshot, snapshot.head, SUBJECT, WINDOW, AS_OF)
        assert history.current_state is S.ACTIVE and len(hashes) == len(early)


def test_transitions_after_as_of_are_truncated_away(tmp_path: Path) -> None:
    records = _active()
    later = transitions(SUBJECT, (S.ACTIVE, S.DEGRADED), AS_OF + timedelta(hours=1))
    heads = write_registry(tmp_path / "reg", records + list(later))
    with _snapshot(tmp_path / "reg") as snapshot:
        assert snapshot.lifecycle_of(SUBJECT, snapshot.head).current_state is S.DEGRADED
        pinned, history, hashes = _resolve_lifecycle(
            snapshot, snapshot.head, SUBJECT, WINDOW, AS_OF
        )
        assert pinned == heads[-1]  # the pinned head itself is recorded unchanged
        assert history.current_state is S.ACTIVE
        assert history.transitions == tuple(records)
        assert hashes == snapshot.lifecycle_of(SUBJECT, pinned).record_hashes[: len(records)]


def test_a_transition_exactly_at_as_of_is_kept(tmp_path: Path) -> None:
    records = _active()
    at_as_of = transitions(SUBJECT, (S.ACTIVE, S.DEGRADED), AS_OF)
    write_registry(tmp_path / "reg", records + list(at_as_of))
    with _snapshot(tmp_path / "reg") as snapshot:
        refused = _refused(LIFECYCLE_NOT_ACTIVE, snapshot, snapshot.head, SUBJECT, WINDOW, AS_OF)
        assert "DEGRADED" in str(refused)


def test_an_unknown_head_fails_closed(tmp_path: Path) -> None:
    heads = write_registry(tmp_path / "reg", _active())
    with _snapshot(tmp_path / "reg") as snapshot:
        beyond = LifecycleHead(heads[-1].record_count + 1, "b" * 64)
        _refused(LIFECYCLE_HEAD_UNKNOWN, snapshot, beyond, SUBJECT, WINDOW, AS_OF)
        wrong_hash = LifecycleHead(heads[-1].record_count, "a" * 64)
        _refused(LIFECYCLE_HEAD_UNKNOWN, snapshot, wrong_hash, SUBJECT, WINDOW, AS_OF)
        _refused(LIFECYCLE_HEAD_UNKNOWN, snapshot, "latest", SUBJECT, WINDOW, AS_OF)


def test_a_competing_chains_head_fails_closed(tmp_path: Path) -> None:
    """Two registries with the same record count but different histories: the other chain's
    head is not on this chain (never "the same count is close enough")."""
    ours = write_registry(tmp_path / "ours", _active())
    theirs = write_registry(tmp_path / "theirs", _active(T0 + timedelta(hours=1)))
    assert ours[-1].record_count == theirs[-1].record_count
    assert ours[-1] != theirs[-1]
    with _snapshot(tmp_path / "ours") as snapshot:
        _refused(LIFECYCLE_HEAD_UNKNOWN, snapshot, theirs[-1], SUBJECT, WINDOW, AS_OF)
        for head in theirs[1:]:  # no head of the competing chain is on ours
            _refused(LIFECYCLE_HEAD_UNKNOWN, snapshot, head, SUBJECT, WINDOW, AS_OF)


def test_a_writer_instance_is_unavailable(tmp_path: Path) -> None:
    write_registry(tmp_path / "reg", _active())
    with LifecycleRegistry(tmp_path / "reg") as writer:
        refused = _refused(LIFECYCLE_UNAVAILABLE, writer, writer.head, SUBJECT, WINDOW, AS_OF)
        assert "read-only snapshot" in str(refused)


def test_anything_but_a_registry_is_unavailable(tmp_path: Path) -> None:
    heads = write_registry(tmp_path / "reg", _active())
    _refused(LIFECYCLE_UNAVAILABLE, object(), heads[-1], SUBJECT, WINDOW, AS_OF)


def test_a_closed_snapshot_is_unavailable(tmp_path: Path) -> None:
    heads = write_registry(tmp_path / "reg", _active())
    snapshot = _snapshot(tmp_path / "reg")
    snapshot.close()
    _refused(LIFECYCLE_UNAVAILABLE, snapshot, heads[-1], SUBJECT, WINDOW, AS_OF)


def test_an_anchored_snapshot_reports_anchored(tmp_path: Path) -> None:
    anchor = tmp_path / "anchor.jsonl"
    write_registry(tmp_path / "reg", _active(), anchor=anchor)
    with _snapshot(tmp_path / "reg", anchor) as anchored:
        assert anchored.anchored
        _resolve_lifecycle(anchored, anchored.head, SUBJECT, WINDOW, AS_OF)
    with _snapshot(tmp_path / "reg") as plain:
        assert not plain.anchored

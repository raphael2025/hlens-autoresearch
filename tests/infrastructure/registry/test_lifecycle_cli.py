"""ADR-0105 §2: the explicit Lifecycle Registry writer / reader CLI
(`infrastructure.registry.lifecycle_cli`).

Every subject is a TEST ONLY `Ref`; nothing is really promoted or ACTIVE.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.domain.base import Kind, Ref
from core.lifecycle.strategy import LifecycleState, LifecycleTransition
from infrastructure.event_bus.journal import GENESIS_HASH, AppendOnlyJournal
from infrastructure.registry.lifecycle import LifecycleRegistry
from infrastructure.registry.lifecycle_cli import main

S = LifecycleState
SUBJECT = Ref(kind=Kind.STRATEGY, name="s_lifecycle_cli_test", version="1.0.0")
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _transition(from_state: S, to_state: S, step: int, approved_by: str | None = None) -> dict:  # type: ignore[type-arg]
    transition = LifecycleTransition(
        subject=SUBJECT,
        from_state=from_state,
        to_state=to_state,
        reason="TEST ONLY transition",
        evidence=("report:test-only",),
        triggered_by="TEST ONLY",
        approved_by=approved_by,
        occurred_at=T0 + timedelta(hours=step),
    )
    return transition.model_dump(mode="json")


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "lifecycle"


@pytest.fixture
def anchor(tmp_path: Path) -> Path:
    return tmp_path / "lifecycle.anchor.jsonl"


def _args(
    command: str,
    root: Path,
    anchor: Path | None,
    *extra: str,
) -> list[str]:
    anchoring = ["--no-anchor"] if anchor is None else ["--anchor", str(anchor)]
    return [command, "--registry", str(root), *anchoring, *extra]


def _fields(out: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def _commit(root: Path, anchor: Path | None, tmp_path: Path, transition: dict, head: str) -> int:  # type: ignore[type-arg]
    path = _write(tmp_path / f"t_{transition['to_state']}.json", transition)
    return main(
        _args(
            "append",
            root,
            anchor,
            "--transition",
            str(path),
            "--expected-head",
            head,
            "--create",
            "--commit",
        )
    )


def _journal_bytes(root: Path) -> bytes:
    return (root / "lifecycle.jsonl").read_bytes()


def test_the_default_append_is_a_dry_run_that_writes_nothing(
    root: Path, anchor: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path / "t.json", _transition(S.IDEA, S.CANDIDATE, 0))
    argv = _args(
        "append",
        root,
        anchor,
        "--transition",
        str(path),
        "--expected-head",
        GENESIS_HASH,
        "--create",
    )
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "committed=false" in out
    assert _fields(out)["record_count"] == "0"
    assert not root.exists() and not anchor.exists()  # not even a created registry


def test_commit_appends_and_the_head_chains_on(
    root: Path, anchor: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _commit(root, anchor, tmp_path, _transition(S.IDEA, S.CANDIDATE, 0), GENESIS_HASH) == 0
    first = _fields(capsys.readouterr().out)
    assert first["committed"] == "true" and first["record_count"] == "1"
    assert first["anchor"] == "present"
    assert (
        _commit(root, anchor, tmp_path, _transition(S.CANDIDATE, S.VALIDATION, 1), first["head"])
        == 0
    )
    second = _fields(capsys.readouterr().out)
    assert second["record_count"] == "2" and second["head"] != first["head"]
    # a fresh reader verifies the chain and the anchor, and agrees on the head
    assert main(_args("show-head", root, anchor)) == 0
    shown = _fields(capsys.readouterr().out)
    assert shown == {"record_count": "2", "head": second["head"], "anchor": "present"}
    with LifecycleRegistry(root, anchor=anchor) as registry:
        assert registry.lifecycle_of(SUBJECT, registry.head).current_state is S.VALIDATION


def test_a_head_that_is_not_the_current_one_is_refused_and_nothing_is_written(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _commit(root, None, tmp_path, _transition(S.IDEA, S.CANDIDATE, 0), GENESIS_HASH) == 0
    first = _fields(capsys.readouterr().out)["head"]
    assert _commit(root, None, tmp_path, _transition(S.CANDIDATE, S.VALIDATION, 1), first) == 0
    capsys.readouterr()
    before = _journal_bytes(root)
    # stale (on the chain, but not the head), genesis, an unknown hash, a malformed hash
    for head in (first, GENESIS_HASH, "a" * 64, "not-a-hash"):
        rc = _commit(root, None, tmp_path, _transition(S.VALIDATION, S.OOS, 2), head)
        assert rc == 1
    assert _journal_bytes(root) == before
    assert "refused" in capsys.readouterr().err


def test_an_illegal_or_out_of_state_transition_is_refused(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _commit(root, None, tmp_path, _transition(S.IDEA, S.CANDIDATE, 0), GENESIS_HASH) == 0
    head = _fields(capsys.readouterr().out)["head"]
    before = _journal_bytes(root)
    illegal = _transition(S.CANDIDATE, S.ACTIVE, 1, "TEST ONLY approver")  # not an edge
    wrong_state = _transition(S.VALIDATION, S.OOS, 1)  # the strategy replays to CANDIDATE
    needs_approval = _transition(S.PAPER, S.PRODUCTION_CANDIDATE, 1)  # also not from CANDIDATE
    for transition in (illegal, wrong_state, needs_approval):
        assert _commit(root, None, tmp_path, transition, head) == 1
        # the dry run refuses it too, without touching the registry
        path = _write(tmp_path / "dry.json", transition)
        assert (
            main(_args("append", root, None, "--transition", str(path), "--expected-head", head))
            == 1
        )
    assert _journal_bytes(root) == before


def test_the_transition_file_must_be_strict_and_state_its_time(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = _transition(S.IDEA, S.CANDIDATE, 0)
    no_clock = {key: value for key, value in good.items() if key != "occurred_at"}
    duplicate = tmp_path / "dup.json"
    duplicate.write_text(
        json.dumps(good)[:-1] + ', "reason": "again"}', encoding="utf-8"
    )  # a repeated key
    nan = tmp_path / "nan.json"
    nan.write_text(json.dumps(good)[:-1] + ', "x": NaN}', encoding="utf-8")
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    cases = {
        "no occurred_at": _write(tmp_path / "no_clock.json", no_clock),
        "unknown field": _write(tmp_path / "extra.json", {**good, "surprise": 1}),
        "not an object": _write(tmp_path / "list.json", [good]),
        "duplicate key": duplicate,
        "NaN": nan,
        "broken": broken,
    }
    for name, path in cases.items():
        argv = _args(
            "append",
            root,
            None,
            "--transition",
            str(path),
            "--expected-head",
            GENESIS_HASH,
            "--create",
        )
        assert main(argv) == 1, name
    assert not root.exists()
    assert "occurred_at" in capsys.readouterr().err


def test_a_missing_transition_file_is_an_io_failure(root: Path, tmp_path: Path) -> None:
    argv = _args(
        "append",
        root,
        None,
        "--transition",
        str(tmp_path / "missing.json"),
        "--expected-head",
        GENESIS_HASH,
        "--create",
    )
    assert main(argv) == 3


def test_exactly_one_anchor_choice_is_required(root: Path, anchor: Path, tmp_path: Path) -> None:
    path = _write(tmp_path / "t.json", _transition(S.IDEA, S.CANDIDATE, 0))
    base = ["append", "--registry", str(root), "--transition", str(path)]
    base += ["--expected-head", GENESIS_HASH, "--create"]
    with pytest.raises(SystemExit) as neither:
        main(base)
    with pytest.raises(SystemExit) as both:
        main([*base, "--anchor", str(anchor), "--no-anchor"])
    with pytest.raises(SystemExit) as show:
        main(["show-head", "--registry", str(root)])
    assert (neither.value.code, both.value.code, show.value.code) == (2, 2, 2)
    assert not root.exists()


def test_the_registry_is_only_created_with_create_and_for_the_genesis_head(
    root: Path, tmp_path: Path
) -> None:
    path = _write(tmp_path / "t.json", _transition(S.IDEA, S.CANDIDATE, 0))
    no_create = _args(
        "append", root, None, "--transition", str(path), "--expected-head", GENESIS_HASH, "--commit"
    )
    assert main(no_create) == 1 and not root.exists()
    not_genesis = _args(
        "append",
        root,
        None,
        "--transition",
        str(path),
        "--expected-head",
        "a" * 64,
        "--create",
        "--commit",
    )
    assert main(not_genesis) == 1 and not root.exists()
    assert main(_args("show-head", root, None)) == 1 and not root.exists()  # reads never create


def test_an_anchor_inside_the_registry_root_is_refused(root: Path, tmp_path: Path) -> None:
    path = _write(tmp_path / "t.json", _transition(S.IDEA, S.CANDIDATE, 0))
    inside = root / "anchor.jsonl"
    argv = _args(
        "append",
        root,
        inside,
        "--transition",
        str(path),
        "--expected-head",
        GENESIS_HASH,
        "--create",
        "--commit",
    )
    assert main(argv) == 1 and not root.exists()


def test_a_missing_anchor_next_to_history_is_refused_not_started(
    root: Path, anchor: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _commit(root, None, tmp_path, _transition(S.IDEA, S.CANDIDATE, 0), GENESIS_HASH) == 0
    head = _fields(capsys.readouterr().out)["head"]
    before = _journal_bytes(root)
    assert _commit(root, anchor, tmp_path, _transition(S.CANDIDATE, S.VALIDATION, 1), head) == 1
    assert main(_args("show-head", root, anchor)) == 1
    assert _journal_bytes(root) == before and not anchor.exists()


def test_a_rolled_back_registry_is_detected_through_its_anchor(
    root: Path, anchor: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _commit(root, anchor, tmp_path, _transition(S.IDEA, S.CANDIDATE, 0), GENESIS_HASH) == 0
    head = _fields(capsys.readouterr().out)["head"]
    assert _commit(root, anchor, tmp_path, _transition(S.CANDIDATE, S.VALIDATION, 1), head) == 0
    capsys.readouterr()
    lines = _journal_bytes(root).splitlines(keepends=True)
    (root / "lifecycle.jsonl").write_bytes(lines[0])  # the last record is dropped
    assert main(_args("show-head", root, anchor)) == 3
    assert "RegistryCorrupted" in capsys.readouterr().err
    # without the anchor the same rollback is not detectable (reported as anchor=absent)
    assert main(_args("show-head", root, None)) == 0
    assert _fields(capsys.readouterr().out)["anchor"] == "absent"


def test_a_held_writer_lock_fails_a_commit_but_not_a_dry_run(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _commit(root, None, tmp_path, _transition(S.IDEA, S.CANDIDATE, 0), GENESIS_HASH) == 0
    head = _fields(capsys.readouterr().out)["head"]
    path = _write(tmp_path / "t2.json", _transition(S.CANDIDATE, S.VALIDATION, 1))
    argv = _args("append", root, None, "--transition", str(path), "--expected-head", head)
    with LifecycleRegistry(root):  # another writer holds the lock
        assert main(argv) == 0  # the dry run reads a lock-free snapshot
        assert main([*argv, "--commit"]) == 3
        assert "RegistryLocked" in capsys.readouterr().err
    assert len(AppendOnlyJournal(root / "lifecycle.jsonl").entries) == 1

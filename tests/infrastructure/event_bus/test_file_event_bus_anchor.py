"""``FileEventBus(root, anchor=...)``: an external topic-head anchor makes lines dropped from the
end of any topic log detectable (backlog P11 "尾部删除发现限制", 2026-09-26)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.contracts.event_bus import BusMessage
from infrastructure.event_bus import BusCorrupted, FileEventBus
from infrastructure.event_bus.journal import AppendOnlyJournal
from tests.contract_suites import event_bus as suite


def _msg(topic: str, key: str, **payload: Any) -> BusMessage:
    return BusMessage.build(topic, key, dict(payload))


def _drop_last_lines(path: Path, count: int) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(lines[:-count]), encoding="utf-8")


def _publish(root: Path, anchor: Path, *messages: BusMessage) -> None:
    with FileEventBus(root, anchor=anchor) as bus:
        for message in messages:
            bus.publish(message)


@pytest.mark.parametrize("check", suite.BUS_CHECKS, ids=lambda c: c.__name__)
def test_an_anchored_bus_passes_the_suite(check: suite.BusCheck, tmp_path: Path) -> None:
    with FileEventBus(tmp_path / "bus", anchor=tmp_path / "anchor.jsonl") as bus:
        check(bus)


def test_every_publish_moves_the_anchor(tmp_path: Path) -> None:
    root, anchor = tmp_path / "bus", tmp_path / "anchor.jsonl"
    _publish(root, anchor, _msg("a.x", "1"), _msg("a.x", "2"), _msg("b.y", "1"))
    heads = [entry.payload for entry in AppendOnlyJournal(anchor).entries]
    assert [(h["topic"], h["length"]) for h in heads] == [("a.x", 1), ("a.x", 2), ("b.y", 1)]
    with FileEventBus(root, anchor=anchor) as bus:  # verified reopen appends nothing new
        assert len(bus.poll("c", "a.x", 10)) == 2
    assert len(AppendOnlyJournal(anchor).entries) == 3


def test_tail_deletion_on_an_unacknowledged_topic_is_detected(tmp_path: Path) -> None:
    root, anchor = tmp_path / "bus", tmp_path / "anchor.jsonl"
    _publish(root, anchor, _msg("a.x", "1"), _msg("a.x", "2"), _msg("a.x", "3"))
    _drop_last_lines(root / "topics" / "a.x.jsonl", 1)
    FileEventBus(root).close()  # without the anchor the shorter chain looks valid ...
    with pytest.raises(BusCorrupted, match="dropped"):
        FileEventBus(root, anchor=anchor)  # ... the anchor sees it


def test_a_replaced_log_of_the_same_length_is_detected(tmp_path: Path) -> None:
    root, anchor = tmp_path / "bus", tmp_path / "anchor.jsonl"
    _publish(root, anchor, _msg("a.x", "1"))
    other = tmp_path / "other"
    _publish(other, tmp_path / "other-anchor.jsonl", _msg("a.x", "forged"))
    (root / "topics" / "a.x.jsonl").write_bytes((other / "topics" / "a.x.jsonl").read_bytes())
    with pytest.raises(BusCorrupted, match="not the log the anchor saw"):
        FileEventBus(root, anchor=anchor)


def test_a_deleted_topic_log_is_detected(tmp_path: Path) -> None:
    root, anchor = tmp_path / "bus", tmp_path / "anchor.jsonl"
    _publish(root, anchor, _msg("a.x", "1"), _msg("b.y", "1"))
    (root / "topics" / "b.y.jsonl").unlink()
    with pytest.raises(BusCorrupted, match="b.y"):
        FileEventBus(root, anchor=anchor)


def test_the_crash_window_is_accepted_and_re_anchored(tmp_path: Path) -> None:
    root, anchor = tmp_path / "bus", tmp_path / "anchor.jsonl"
    _publish(root, anchor, _msg("a.x", "1"))
    with FileEventBus(root) as bus:  # a publish whose anchor line never got written
        bus.publish(_msg("a.x", "2"))
    with FileEventBus(root, anchor=anchor) as bus:
        assert len(bus.poll("c", "a.x", 10)) == 2
    last = AppendOnlyJournal(anchor).entries[-1].payload
    assert (last["topic"], last["length"]) == ("a.x", 2)
    _drop_last_lines(root / "topics" / "a.x.jsonl", 1)  # now the second message is anchored too
    with pytest.raises(BusCorrupted):
        FileEventBus(root, anchor=anchor)


def test_the_anchor_must_live_outside_the_bus_directory(tmp_path: Path) -> None:
    root = tmp_path / "bus"
    with pytest.raises(ValueError, match="outside"):
        FileEventBus(root, anchor=root / "anchor.jsonl")


def test_a_tampered_or_backwards_anchor_is_refused(tmp_path: Path) -> None:
    root, anchor = tmp_path / "bus", tmp_path / "anchor.jsonl"
    _publish(root, anchor, _msg("a.x", "1"))
    text = anchor.read_text(encoding="utf-8")
    anchor.write_text(text.replace('"length":1', '"length":2'), encoding="utf-8")
    with pytest.raises(BusCorrupted, match="bus anchor"):
        FileEventBus(root, anchor=anchor)
    backwards = tmp_path / "backwards.jsonl"
    journal = AppendOnlyJournal(backwards)
    with FileEventBus(root) as bus:
        head = bus.poll("c", "a.x", 1)
    assert head
    log = AppendOnlyJournal(root / "topics" / "a.x.jsonl")
    journal.append("topic_head", {"topic": "a.x", "length": 1, "head_hash": log.head_hash})
    journal.append("topic_head", {"topic": "a.x", "length": 1, "head_hash": log.head_hash})
    with pytest.raises(BusCorrupted, match="backwards"):
        FileEventBus(root, anchor=backwards)


def test_without_an_anchor_nothing_changes(tmp_path: Path) -> None:
    root = tmp_path / "bus"
    with FileEventBus(root) as bus:
        bus.publish(_msg("a.x", "1"))
    assert sorted(p.name for p in root.iterdir()) == [".lock", "bus.json", "consumers", "topics"]

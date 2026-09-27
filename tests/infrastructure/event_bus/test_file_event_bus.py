"""``FileEventBus``: the durable bus passes the provider-agnostic suite, survives restarts, behaves
exactly like the in-memory bus, and refuses corruption (ADR-0044 implementation note, file-backed
bus, 2026-09-26; backlog P11 "总线只在内存中").
"""

from __future__ import annotations

import ast
import hashlib
import json
import random
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from apps.worker import JobRunner, JobSpec
from apps.worker.journal import AppendOnlyJournal as WorkerJournal
from core.contracts.event_bus import BusMessage
from core.domain.base import canonical_json
from infrastructure.event_bus import BusCorrupted, BusLocked, FileEventBus, InMemoryEventBus
from infrastructure.event_bus.journal import GENESIS_HASH, AppendOnlyJournal
from tests.contract_suites import event_bus as suite

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "bus"


@pytest.fixture
def bus(root: Path) -> Iterator[FileEventBus]:
    with FileEventBus(root) as opened:
        yield opened


def _reopen(bus: FileEventBus) -> FileEventBus:
    """Drop the object (standing in for a restarted process) and open the same directory."""
    root = bus.root
    bus.close()
    del bus
    return FileEventBus(root)


def _msg(topic: str, key: str, **payload: Any) -> BusMessage:
    return BusMessage.build(topic, key, dict(payload))


def _state_files(root: Path) -> list[Path]:
    return sorted(p for p in (root / "consumers").iterdir() if p.suffix == ".json")


# -- the provider-agnostic contract suite --------------------------------------------------------


@pytest.mark.parametrize("check", suite.BUS_CHECKS, ids=lambda c: c.__name__)
def test_the_file_bus_passes_the_suite(check: suite.BusCheck, bus: FileEventBus) -> None:
    check(bus)


@pytest.mark.parametrize("check", suite.BUS_CHECKS, ids=lambda c: c.__name__)
def test_the_file_bus_passes_the_suite_on_a_reopened_directory(
    check: suite.BusCheck, root: Path
) -> None:
    FileEventBus(root).close()  # an existing, empty bus directory
    with FileEventBus(root) as reopened:
        check(reopened)


# -- durability ----------------------------------------------------------------------------------


def test_published_messages_survive_a_restart_and_acks_persist(bus: FileEventBus) -> None:
    first, second, third = (_msg("jobs", str(i), i=i) for i in range(3))
    for message in (first, second, third):
        bus.publish(message)
    bus.ack("c", "jobs", first.message_id)

    reopened = _reopen(bus)
    try:
        assert reopened.poll("c", "jobs", 10) == (second, third)
        assert reopened.poll("other", "jobs", 10) == (first, second, third)
        reopened.ack("c", "jobs", second.message_id)
    finally:
        reopened.close()
    with FileEventBus(reopened.root) as again:
        assert again.poll("c", "jobs", 10) == (third,)


def test_a_crash_between_poll_and_ack_redelivers_after_reopening(bus: FileEventBus) -> None:
    message = _msg("jobs", "k", n=1)
    bus.publish(message)
    assert bus.poll("c", "jobs", 10) == (message,)  # delivered, then the process dies before ack
    with _reopen(bus) as reopened:
        assert reopened.poll("c", "jobs", 10) == (message,), "at least once across restarts"


def test_the_saved_offset_is_the_low_water_mark_of_acknowledged_messages(
    bus: FileEventBus,
) -> None:
    messages = [_msg("t", str(i), i=i) for i in range(4)]
    for message in messages:
        bus.publish(message)
    bus.ack("c", "t", messages[2].message_id)  # out of order: the mark cannot move past 0
    [state_file] = _state_files(bus.root)
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["offset"] == 0 and saved["log_length"] == 4
    bus.ack("c", "t", messages[0].message_id)
    bus.ack("c", "t", messages[1].message_id)
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["offset"] == 3  # 0, 1 and 2 are acknowledged; polling starts at 3
    assert saved["acked"] == sorted(m.message_id for m in messages[:3])
    with _reopen(bus) as reopened:
        assert reopened.poll("c", "t", 10) == (messages[3],)


def test_an_acknowledged_id_is_never_redelivered_even_as_a_later_copy(bus: FileEventBus) -> None:
    message = _msg("t", "k", n=1)
    bus.publish(message)
    bus.publish(message)  # at least once: a duplicate publish appends a second copy
    assert bus.poll("c", "t", 10) == (message, message)
    bus.ack("c", "t", message.message_id)
    bus.publish(message)
    assert bus.poll("c", "t", 10) == ()
    with _reopen(bus) as reopened:
        assert reopened.poll("c", "t", 10) == ()
        assert reopened.poll("fresh", "t", 10) == (message, message, message)


def test_replayed_messages_equal_the_ones_delivered_before_the_restart(bus: FileEventBus) -> None:
    message = _msg("t", "k", nested={"b": [1, 2], "a": "x"}, text="非 ASCII", flag=True)
    bus.publish(message)
    before = bus.poll("c", "t", 10)
    with _reopen(bus) as reopened:
        after = reopened.poll("c", "t", 10)
    assert before == after == (message,)
    assert [m.message_id for m in after] == [message.message_id]


def test_jobs_submitted_before_a_restart_run_once_after_it(root: Path) -> None:
    calls: list[Mapping[str, Any]] = []

    def work(params: Mapping[str, Any]) -> int:
        calls.append(params)
        return int(params["x"]) * 2

    with FileEventBus(root) as bus:
        JobRunner(bus, consumer="w", topic="jobs", handlers={"double": work}).submit(
            JobSpec("double", {"x": 21})
        )
    with FileEventBus(root) as bus:
        runner = JobRunner(bus, consumer="w", topic="jobs", handlers={"double": work})
        [outcome] = runner.run_pending()
        assert outcome.succeeded and outcome.result == 42
    with FileEventBus(root) as bus:
        runner = JobRunner(bus, consumer="w", topic="jobs", handlers={"double": work})
        assert runner.run_pending() == [] and len(calls) == 1


# -- equivalence with the in-memory bus ----------------------------------------------------------


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_the_file_bus_behaves_exactly_like_the_memory_bus(seed: int, root: Path) -> None:
    rng = random.Random(seed)
    pool = [_msg(topic, str(k), k=k) for topic in ("a", "b.c") for k in range(5)]
    consumers = ("x", "y")
    memory = InMemoryEventBus()
    durable = FileEventBus(root)
    try:
        for step in range(120):
            action = rng.random()
            topic = rng.choice(("a", "b.c"))
            consumer = rng.choice(consumers)
            if action < 0.4:
                message = rng.choice([m for m in pool if m.topic == topic])
                memory.publish(message)
                durable.publish(message)
            elif action < 0.75:
                limit = rng.randint(1, 4)
                polled = memory.poll(consumer, topic, limit)
                assert durable.poll(consumer, topic, limit) == polled, f"step {step}"
                if polled and rng.random() < 0.7:
                    chosen = rng.choice(polled).message_id
                    memory.ack(consumer, topic, chosen)
                    durable.ack(consumer, topic, chosen)
            elif action < 0.85:  # acknowledging an id not (yet) published is accepted by both
                chosen = rng.choice(pool).message_id
                memory.ack(consumer, topic, chosen)
                durable.ack(consumer, topic, chosen)
            else:
                durable = _reopen(durable)
        for consumer in consumers:
            for topic in ("a", "b.c"):
                assert durable.poll(consumer, topic, 100) == memory.poll(consumer, topic, 100)
    finally:
        durable.close()


# -- locking and lifecycle -----------------------------------------------------------------------


def test_a_second_writer_is_refused_until_the_first_closes(root: Path) -> None:
    first = FileEventBus(root)
    with pytest.raises(BusLocked):
        FileEventBus(root)
    first.close()
    first.close()  # idempotent
    FileEventBus(root).close()


def test_a_closed_bus_refuses_every_operation(bus: FileEventBus) -> None:
    message = _msg("t", "k", n=1)
    bus.close()
    with pytest.raises(RuntimeError, match="closed"):
        bus.publish(message)
    with pytest.raises(RuntimeError, match="closed"):
        bus.poll("c", "t", 1)
    with pytest.raises(RuntimeError, match="closed"):
        bus.ack("c", "t", message.message_id)


def test_bad_arguments_are_refused(bus: FileEventBus) -> None:
    with pytest.raises(TypeError):
        bus.publish({"topic": "t"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="limit"):
        bus.poll("c", "t", 0)
    with pytest.raises(ValueError, match="content hash"):
        bus.ack("c", "t", "not-a-hash")
    assert not _state_files(bus.root)


def test_an_interrupted_atomic_write_leaves_the_previous_state(bus: FileEventBus) -> None:
    first, second = _msg("t", "1", i=1), _msg("t", "2", i=2)
    bus.publish(first)
    bus.publish(second)
    bus.ack("c", "t", first.message_id)
    [state_file] = _state_files(bus.root)
    root = bus.root
    bus.close()
    (state_file.with_name(state_file.name + ".tmp")).write_text("{half", encoding="utf-8")
    with FileEventBus(root) as reopened:
        assert reopened.poll("c", "t", 10) == (second,)


# -- fail closed on corruption -------------------------------------------------------------------


def _published(root: Path, *messages: BusMessage, ack: tuple[str, str] | None = None) -> None:
    with FileEventBus(root) as bus:
        for message in messages:
            bus.publish(message)
        if ack is not None:
            bus.ack(ack[0], ack[1], messages[0].message_id)


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def _refused(root: Path, match: str) -> None:
    with pytest.raises(BusCorrupted, match=match):
        FileEventBus(root)
    with pytest.raises(BusCorrupted):  # the failed open released the lock: not BusLocked
        FileEventBus(root)


def test_a_tampered_log_line_is_refused(root: Path) -> None:
    _published(root, _msg("t", "1", n=1), _msg("t", "2", n=2))
    log = root / "topics" / "t.jsonl"
    lines = _lines(log)
    record = json.loads(lines[0])
    record["payload"]["payload"]["n"] = 99
    lines[0] = canonical_json(record)
    _write_lines(log, lines)
    _refused(root, "content hash")


def test_a_broken_chain_is_refused(root: Path) -> None:
    _published(root, *(_msg("t", str(i), i=i) for i in range(3)))
    log = root / "topics" / "t.jsonl"
    lines = _lines(log)
    _write_lines(log, [lines[0], lines[2]])  # a line removed from the middle
    _refused(root, "sequence|chain")


def test_a_partial_trailing_line_is_refused(root: Path) -> None:
    _published(root, _msg("t", "1", n=1))
    log = root / "topics" / "t.jsonl"
    with log.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 2, "type": "bus_mes')
    _refused(root, "partial trailing line")


def test_a_well_chained_forged_message_is_refused(root: Path) -> None:
    """A rewritten log with a valid chain still has to hold valid messages of its topic."""
    real = _msg("t", "1", n=1)
    forged = real.model_dump(mode="json") | {"payload": {"n": 2}}  # message_id no longer matches
    FileEventBus(root).close()
    WorkerJournal(root / "topics" / "t.jsonl").append("bus_message", forged)
    _refused(root, "not a valid BusMessage")


def test_a_message_filed_under_another_topic_is_refused(root: Path) -> None:
    FileEventBus(root).close()
    other = _msg("other", "1", n=1)
    WorkerJournal(root / "topics" / "t.jsonl").append("bus_message", other.model_dump(mode="json"))
    _refused(root, "belongs to topic")


def test_lines_dropped_from_the_end_below_a_saved_state_are_refused(root: Path) -> None:
    messages = [_msg("t", str(i), i=i) for i in range(3)]
    _published(root, *messages, ack=("c", "t"))  # the state saw a log of length 3
    log = root / "topics" / "t.jsonl"
    _write_lines(log, _lines(log)[:2])  # a valid, shorter chain
    _refused(root, "lost lines")


def test_a_different_log_under_a_saved_state_is_refused(root: Path) -> None:
    _published(root, _msg("t", "1", n=1), ack=("c", "t"))
    log = root / "topics" / "t.jsonl"
    log.unlink()
    WorkerJournal(log).append("bus_message", _msg("t", "2", n=2).model_dump(mode="json"))
    _refused(root, "not the log")


def test_an_edited_consumer_state_is_refused(root: Path) -> None:
    _published(root, _msg("t", "1", n=1), _msg("t", "2", n=2), ack=("c", "t"))
    [state_file] = _state_files(root)
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    saved["acked"] = []
    state_file.write_text(canonical_json(saved) + "\n", encoding="utf-8")
    _refused(root, "state hash")


def test_a_rehashed_state_with_a_wrong_offset_is_refused(root: Path) -> None:
    _published(root, _msg("t", "1", n=1), _msg("t", "2", n=2), ack=("c", "t"))
    [state_file] = _state_files(root)
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    saved["offset"] = 2  # skips an unacknowledged message
    body = {k: v for k, v in saved.items() if k != "state_hash"}
    saved["state_hash"] = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    state_file.write_text(canonical_json(saved) + "\n", encoding="utf-8")
    _refused(root, "offset disagrees")


def test_a_consumer_state_under_another_name_is_refused(root: Path) -> None:
    _published(root, _msg("t", "1", n=1), ack=("c", "t"))
    [state_file] = _state_files(root)
    state_file.rename(state_file.with_name("0" * 64 + ".json"))
    _refused(root, "file name")


def test_unknown_entries_and_foreign_directories_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "bus"
    _published(root, _msg("t", "1", n=1))
    (root / "topics" / "Bad Name.jsonl").write_text("", encoding="utf-8")
    _refused(root, "not a topic log")

    stray = tmp_path / "stray"
    _published(stray)
    (stray / "notes.txt").write_text("x", encoding="utf-8")
    _refused(stray, "unknown entries")

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "data.csv").write_text("x", encoding="utf-8")
    _refused(foreign, "not a bus")

    marker = tmp_path / "marker"
    _published(marker)
    (marker / "bus.json").write_text('{"format": "x", "schema_version": "9"}', encoding="utf-8")
    _refused(marker, "expected")


# -- the journal shares the on-disk contract; the adapter stays in its layer ---------------------


def test_the_journal_shares_the_worker_journals_on_disk_contract(tmp_path: Path) -> None:
    ours, theirs = tmp_path / "infra.jsonl", tmp_path / "worker.jsonl"
    infra, worker = AppendOnlyJournal(ours), WorkerJournal(theirs)
    assert infra.head_hash == GENESIS_HASH == worker.head_hash
    for payload in ({"n": 1}, {"nested": {"b": [1, 2], "a": "x"}}, {"text": "非 ASCII"}):
        infra.append("kind_a", payload)
        worker.append("kind_a", payload)
    assert ours.read_bytes() == theirs.read_bytes()
    assert WorkerJournal(ours).head_hash == AppendOnlyJournal(theirs).head_hash


def test_the_bus_package_imports_neither_research_nor_apps() -> None:
    for path in (REPO / "infrastructure" / "event_bus").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        roots = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 0
        }
        assert not roots & {"research", "apps", "plugins"}, path.name

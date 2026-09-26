"""Phase 3: the local content-addressed event-run store (artifact store, not an Iceberg table)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from core.contracts.event import EventResult
from infrastructure.event.runner import run_events
from infrastructure.event.store import EventResultStore, EventStoreCorrupted
from infrastructure.event.table import event_table
from plugins.events import FeatureThresholdCrossProvider
from tests.fake_events import LAG, X_INPUTS, X, request

CROSS = FeatureThresholdCrossProvider.spec(X, Decimal("4.5"), "both", observable_lag=LAG)


def _result() -> EventResult:
    return run_events(
        FeatureThresholdCrossProvider((CROSS,)), CROSS, request(CROSS, inputs=X_INPUTS)
    )


def test_put_get_round_trip_keeps_the_event_table(tmp_path: Path) -> None:
    store, result = EventResultStore(tmp_path / "events"), _result()
    assert result.events
    path = store.put(result)
    assert path.name == f"{result.result_hash}.json"
    loaded = store.get(result.result_hash)
    assert loaded == result and event_table(loaded) == event_table(result)
    assert store.hashes() == (result.result_hash,)


def test_an_identical_put_is_a_no_op_and_nothing_is_overwritten(tmp_path: Path) -> None:
    store, result = EventResultStore(tmp_path), _result()
    path = store.put(result)
    before = path.stat().st_mtime_ns
    assert store.put(result) == path and path.stat().st_mtime_ns == before
    path.write_bytes(path.read_bytes().rstrip(b"\n") + b" \n")  # other bytes under the name
    with pytest.raises(EventStoreCorrupted, match="never overwritten"):
        store.put(result)


def test_an_edited_file_is_refused(tmp_path: Path) -> None:
    store, result = EventResultStore(tmp_path), _result()
    path = store.put(result)
    first = result.events[0].event_time.isoformat().encode()
    path.write_bytes(path.read_bytes().replace(first[:4], b"1999", 1))
    with pytest.raises(EventStoreCorrupted):
        store.get(result.result_hash)


def test_a_truncated_or_renamed_file_is_refused(tmp_path: Path) -> None:
    store, result = EventResultStore(tmp_path), _result()
    path = store.put(result)
    data = path.read_bytes()
    path.write_bytes(data[:-20])
    with pytest.raises(EventStoreCorrupted):
        store.get(result.result_hash)
    path.write_bytes(data)
    other = "0" * 64
    path.rename(tmp_path / f"{other}.json")
    with pytest.raises(EventStoreCorrupted, match="holds event run"):
        store.get(other)
    with pytest.raises(KeyError):
        store.get(result.result_hash)


def test_non_canonical_bytes_are_refused(tmp_path: Path) -> None:
    store, result = EventResultStore(tmp_path), _result()
    path = store.put(result)
    path.write_bytes(path.read_bytes().rstrip(b"\n") + b"  \n")  # same JSON, other bytes
    with pytest.raises(EventStoreCorrupted, match="canonical"):
        store.get(result.result_hash)


def test_bad_names_and_stray_files_are_refused(tmp_path: Path) -> None:
    store = EventResultStore(tmp_path)
    with pytest.raises(ValueError):
        store.get("not-a-hash")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(EventStoreCorrupted, match="not a stored event run"):
        store.hashes()

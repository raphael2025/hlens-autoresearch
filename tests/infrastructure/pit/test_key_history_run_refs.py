"""Single-key spill references stay fanout-bounded as history grows (ADR-0077 §6.1.2)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from infrastructure.pit.runs import KeyHistoryBuffer, RunLimits
from infrastructure.storage import LocalFileStorageAdapter


def _storage(tmp_path: Path) -> LocalFileStorageAdapter:
    warehouse = tmp_path / "warehouse"
    staging = warehouse / "staging"
    warehouse.mkdir(parents=True, exist_ok=True)
    staging.mkdir(exist_ok=True)
    return LocalFileStorageAdapter(warehouse.as_uri(), staging.as_uri())


def _key(row: Mapping[str, Any]) -> tuple[str, str]:
    return (row["observation_key"], row["revision_id"])


def test_single_key_history_folds_spill_refs_online_and_reassembles(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    fanout = 4
    total = 2048
    buffer = KeyHistoryBuffer(
        storage=storage,
        buffer_limit=1,
        merge_fanout=fanout,
        key=_key,
        limits=RunLimits(leaf_max_records=32, leaf_max_bytes=8192, fanout=fanout),
    )
    max_refs_held = 0
    max_levels = 0

    for index in range(total):
        buffer.add(
            {
                "observation_key": "one-key",
                "revision_id": f"r{index:06d}",
                "event_time": datetime(2024, 1, 1, tzinfo=UTC),
                "arrival_seq": index,
            }
        )
        levels = buffer._refs._levels
        max_levels = max(max_levels, len(levels))
        max_refs_held = max(max_refs_held, sum(len(level) for level in levels))
        assert all(len(level) < fanout for level in levels)

    assert buffer.spilled
    # A base-fanout counter has at most fanout-1 refs at each of O(log_fanout(N)) levels.
    assert max_levels <= 1 + (total.bit_length() + 1)
    assert max_refs_held <= (fanout - 1) * max_levels

    with buffer.rows() as rows:
        actual_ids = [row["revision_id"] for row in rows]
    assert actual_ids == [f"r{index:06d}" for index in range(total)]

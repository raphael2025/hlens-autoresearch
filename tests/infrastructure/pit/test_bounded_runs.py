"""Content-addressed bounded sorted runs (ADR-0077 §6.1.2 / §6.1.3; ``infrastructure/pit/runs.py``).

Standalone unit tests of the run object format itself: write / read round-trips, fail-closed
tamper and shape checks, the spill -> merge pipeline, and :class:`KeyHistoryBuffer`'s per-key
overflow spill. No PIT selection logic is involved here (see ``test_selector_v3.py`` for that).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from infrastructure.pit.runs import (
    KeyHistoryBuffer,
    RunIntegrityError,
    RunLimits,
    RunObjectRef,
    RunRef,
    RunSetBuilder,
    RunWriteError,
    iter_run,
    merge_sorted_runs,
    run_object_key,
    spill_sorted_runs,
    write_sorted_run,
)
from infrastructure.storage import LocalFileStorageAdapter

_GENEROUS = RunLimits(leaf_max_records=1000, leaf_max_bytes=1 << 20, fanout=4)
#: leaf_max_records=1 / fanout=2 force one leaf per row and a multi-level index tree; the byte
#: budget is deliberately generous (not tight) so it never interacts with that fragmentation.
_TIGHT = RunLimits(leaf_max_records=1, leaf_max_bytes=4096, fanout=2)


def _storage(tmp_path: Path) -> LocalFileStorageAdapter:
    warehouse = tmp_path / "warehouse"
    staging = warehouse / "staging"
    warehouse.mkdir(parents=True, exist_ok=True)
    staging.mkdir(exist_ok=True)
    return LocalFileStorageAdapter(warehouse.as_uri(), staging.as_uri())


def _row(i: int, key: str = "k") -> dict[str, object]:
    return {
        "observation_key": key,
        "revision_id": f"{key}-{i:04d}",
        "arrival_seq": i,
        "price": Decimal("100.5") + Decimal(i),
        "available_time": datetime(2024, 1, 1, tzinfo=UTC) + timedelta(seconds=i),
        "as_of": date(2024, 1, 1),
        "supersedes": [f"{key}-{j:04d}" for j in range(i)],
        "note": None,
        "flag": i % 2 == 0,
    }


def _rows(n: int, *, key: str = "k") -> list[dict[str, object]]:
    return [_row(i, key) for i in range(n)]


def _collect(storage: LocalFileStorageAdapter, ref: RunRef) -> list[dict[str, object]]:
    with iter_run(storage, ref) as rows:
        return list(rows)


# =============================================================================================
# write / read round-trip
# =============================================================================================


def test_write_then_iter_round_trips_rows_in_order(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    rows = _rows(5)
    ref = write_sorted_run(storage, rows, _GENEROUS)
    assert ref.record_count == 5
    assert ref.leaf_count == 1  # 5 small rows fit one leaf under _GENEROUS
    assert ref.depth == 1
    assert _collect(storage, ref) == rows


def test_row_codec_round_trips_datetime_date_decimal_and_nesting(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    row = {
        "when": datetime(2023, 12, 31, 23, 59, 59, 500000, tzinfo=UTC),
        "day": date(2023, 12, 31),
        "amount": Decimal("-12.34500"),
        "tags": ["a", "b", ["nested", 1, None]],
        "meta": {"inner": {"deep": Decimal("1"), "when": datetime(2020, 1, 1, tzinfo=UTC)}},
        "flag": True,
        "nothing": None,
        "count": 3,
        "ratio": 0.5,
    }
    ref = write_sorted_run(storage, [row], _GENEROUS)
    [got] = _collect(storage, ref)
    assert got == row
    assert isinstance(got["when"], datetime)
    assert isinstance(got["day"], date) and not isinstance(got["day"], datetime)
    assert isinstance(got["amount"], Decimal)


def test_an_unsupported_value_type_is_refused(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    with pytest.raises(RunWriteError, match="unsupported row value type"):
        write_sorted_run(storage, [{"bad": object()}], _GENEROUS)


def test_empty_run_round_trips_as_zero_records(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    ref = write_sorted_run(storage, [], _GENEROUS)
    assert (ref.record_count, ref.leaf_count, ref.depth) == (0, 0, 1)
    assert _collect(storage, ref) == []


def test_tight_limits_force_multiple_leaves_and_index_levels(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    rows = _rows(9)  # fanout=2, leaf_max_records=1 -> 9 leaves -> a multi-level index tree
    ref = write_sorted_run(storage, rows, _TIGHT)
    assert ref.record_count == 9
    assert ref.leaf_count == 9
    assert ref.depth >= 3  # ceil(log2(9)) index levels, plus the leaf-wrapping level
    assert _collect(storage, ref) == rows


def test_a_record_too_large_for_one_leaf_fails_closed(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    tiny = RunLimits(leaf_max_records=10, leaf_max_bytes=600, fanout=2)
    huge_row = {"blob": "x" * 10_000}
    with pytest.raises(RunWriteError, match="exceeds what leaf_max_bytes"):
        write_sorted_run(storage, [huge_row], tiny)


def test_run_limits_rejects_bad_parameters() -> None:
    with pytest.raises(RunWriteError, match="leaf_max_records"):
        RunLimits(leaf_max_records=0, leaf_max_bytes=10_000, fanout=2)
    with pytest.raises(RunWriteError, match="leaf_max_bytes"):
        RunLimits(leaf_max_records=10, leaf_max_bytes=10, fanout=2)
    with pytest.raises(RunWriteError, match="fanout"):
        RunLimits(leaf_max_records=10, leaf_max_bytes=10_000, fanout=1)


def test_run_object_key_is_derived_only_from_its_sha256() -> None:
    sha = "a" * 64
    assert run_object_key(sha) == f"research/pit-sorted-run/v1/{sha}.jsonl"
    with pytest.raises(RunWriteError, match="sha256"):
        run_object_key("not-hex")


# =============================================================================================
# fail closed: tamper / missing / malformed
# =============================================================================================


def test_a_reference_with_the_wrong_sha256_is_refused(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    ref = write_sorted_run(storage, _rows(3), _GENEROUS)
    forged_root = dataclasses.replace(ref.root, sha256="0" * 64)
    forged = dataclasses.replace(ref, root=forged_root)
    with pytest.raises(RunIntegrityError, match="does not match its reference"):
        _collect(storage, forged)


def test_a_reference_to_an_unpublished_object_is_refused(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    unpublished = RunObjectRef(key=run_object_key("b" * 64), sha256="b" * 64, size=10)
    fake = RunRef(root=unpublished, record_count=0, leaf_count=0, depth=1)
    with pytest.raises(RunIntegrityError, match="not published"):
        _collect(storage, fake)


def test_a_record_count_claim_that_does_not_match_the_root_is_refused(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    ref = write_sorted_run(storage, _rows(3), _GENEROUS)
    inflated = dataclasses.replace(ref, record_count=ref.record_count + 1)
    with pytest.raises(RunIntegrityError, match="produced .* record"):
        _collect(storage, inflated)


# =============================================================================================
# spill (arbitrary order -> fixed-capacity sorted runs) + bounded k-way merge
# =============================================================================================


def _key(row: dict[str, object]) -> tuple[object, object]:
    return (row["observation_key"], row["revision_id"])


def test_spill_bounds_batch_size_and_merge_reconstructs_global_order(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    keys = ["c", "a", "b"]
    rows = [_row(i, key=k) for k in keys for i in range(4)]
    shuffled = list(reversed(rows))  # "arbitrary scan order"
    refs = list(
        spill_sorted_runs(shuffled, key=_key, capacity=5, storage=storage, limits=_GENEROUS)
    )
    assert len(refs) == 3  # ceil(12 / 5)
    for ref in refs:
        assert ref.record_count <= 5
    with merge_sorted_runs(
        storage, refs, key=_key, merge_fanout=2, limits=_GENEROUS
    ) as merged:
        got = list(merged)
    assert got == sorted(rows, key=_key)


def test_merge_with_more_runs_than_fanout_does_a_multi_pass_reduction(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    rows = _rows(20)
    refs = list(
        spill_sorted_runs(rows, key=_key, capacity=1, storage=storage, limits=_GENEROUS)
    )
    assert len(refs) == 20  # one row per run: forces several reduction passes at fanout=3
    with merge_sorted_runs(storage, refs, key=_key, merge_fanout=3, limits=_TIGHT) as merged:
        got = list(merged)
    assert got == sorted(rows, key=_key)


def test_merge_consumes_a_lazy_run_stream_with_bounded_multilevel_folding(
    tmp_path: Path,
) -> None:
    """Input refs are consumed incrementally; fanout=2 forces several persisted merge levels."""
    storage = _storage(tmp_path)
    rows = [_row(i) for i in range(65)]
    class ObservedStorage:
        def __init__(self) -> None:
            self.merge_reads = 0

        def __getattr__(self, name: str) -> object:
            return getattr(storage, name)

        def open_read(self, ref: object) -> object:
            self.merge_reads += 1
            return storage.open_read(ref)  # type: ignore[arg-type]

    observed = ObservedStorage()

    def refs():
        for i, row in enumerate(reversed(rows)):
            if i == 2:
                # The first fanout group has already been merged before the third source ref is
                # requested. A collector that first materializes the entire iterable fails this
                # interleaving assertion and retains O(number of refs).
                assert observed.merge_reads > 0
            yield write_sorted_run(storage, [row], _TIGHT)

    with merge_sorted_runs(
        observed, refs(), key=_key, merge_fanout=2, limits=_TIGHT  # type: ignore[arg-type]
    ) as merged:
        got = list(merged)
    assert got == sorted(rows, key=_key)
    assert observed.merge_reads > 0


def test_multilevel_merge_iterator_closes_after_early_consumer_exit(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    rows = [_row(i) for i in range(17)]
    refs = (write_sorted_run(storage, [row], _TIGHT) for row in reversed(rows))
    with merge_sorted_runs(storage, refs, key=_key, merge_fanout=2, limits=_TIGHT) as merged:
        iterator = iter(merged)
        assert next(iterator) == rows[0]
    with pytest.raises(StopIteration):
        next(iterator)


def test_run_set_builder_returns_one_root_from_unordered_rows(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    rows = [_row(i, key=key) for key in ("d", "a", "c", "b") for i in range(7)]
    with RunSetBuilder(
        storage, key=_key, capacity=2, merge_fanout=2, limits=_TIGHT
    ) as builder:
        builder.extend(reversed(rows))
        root = builder.finish()
        assert root is not None
        with iter_run(storage, root) as ordered:
            assert list(ordered) == sorted(rows, key=_key)


def test_run_set_builder_empty_input_has_no_root(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    with RunSetBuilder(
        storage, key=_key, capacity=2, merge_fanout=2, limits=_TIGHT
    ) as builder:
        assert builder.finish() is None


def test_merge_of_no_runs_yields_nothing(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    with merge_sorted_runs(storage, [], key=_key, merge_fanout=2, limits=_GENEROUS) as merged:
        assert list(merged) == []


def test_spill_and_merge_reject_bad_parameters(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    with pytest.raises(RunWriteError, match="capacity"):
        list(spill_sorted_runs([], key=_key, capacity=0, storage=storage, limits=_GENEROUS))
    with pytest.raises(RunWriteError, match="merge_fanout"):
        with merge_sorted_runs(storage, [], key=_key, merge_fanout=1, limits=_GENEROUS):
            pass


# =============================================================================================
# KeyHistoryBuffer: one key's history may itself exceed a fixed buffer
# =============================================================================================


def test_key_history_buffer_spills_past_its_limit_and_reassembles_in_order(
    tmp_path: Path,
) -> None:
    storage = _storage(tmp_path)
    buffer = KeyHistoryBuffer(storage=storage, buffer_limit=3, limits=_GENEROUS)
    rows = _rows(10)
    for row in rows:
        buffer.add(row)
    assert buffer.spilled is True
    with buffer.rows() as reassembled:
        assert list(reassembled) == rows


def test_key_history_buffer_under_its_limit_never_spills(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    buffer = KeyHistoryBuffer(storage=storage, buffer_limit=100, limits=_GENEROUS)
    rows = _rows(4)
    for row in rows:
        buffer.add(row)
    assert buffer.spilled is False
    with buffer.rows() as reassembled:
        assert list(reassembled) == rows


def test_key_history_buffer_rejects_a_non_positive_limit(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    with pytest.raises(RunWriteError, match="buffer_limit"):
        KeyHistoryBuffer(storage=storage, buffer_limit=0, limits=_GENEROUS)

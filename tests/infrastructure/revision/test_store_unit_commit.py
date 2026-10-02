"""ADR-0108 for Raw archive element rows: one archive revision = one element-table snapshot.

§9(a) structural (one snapshot whatever the rows and the D2 microbatch size), §9(b) both
layouts hold bitwise-identical rows, old per-microbatch history still verifies and completes,
a mixed layout fails closed, and §9(c) crash injection before / after the unit commit. SQLite
catalog (never PostgreSQL evidence).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest

from core.contracts.catalog import BatchConflict, CommitOutcome, CommitRequest
from infrastructure.catalog import CatalogIntegrityError
from infrastructure.catalog.bounded_metadata import BoundedMetadataLimits
from infrastructure.catalog.iceberg_adapter import CommitLayout
from infrastructure.catalog.phase1_tables import BINANCE_SPOT_AGG_TRADES
from infrastructure.catalog.unit_commit import StagedUnitWriter
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision import ARCHIVE_TABLE, ROW_TABLES, ArchiveIngested, RawRevisionStore
from infrastructure.revision.row_integrity import PersistedRowVerifier, history_from
from infrastructure.revision.store import (
    ROW_UNIT_INFIX,
    _row_batch_id,
    _row_unit_batch_id,
    parse_row_unit_batch_id,
)
from infrastructure.streaming.content_key_tree import KeyTreeParams
from infrastructure.streaming.runs import RunLimits
from tests.infrastructure.parser import parser_support as ps
from tests.infrastructure.revision import revision_support as rs
from tests.infrastructure.revision.revision_support import StoreHarness

AGG = BINANCE_SPOT_AGG_TRADES
AGG_TABLE = ROW_TABLES["agg_trades"]
COLUMNS = tuple(field.name for field in AGG.arrow_schema)


class _Crash(RuntimeError):
    """Injected process death."""


class _CrashAfterUnitCommit:
    """Adapter proxy: the unit commit succeeds, then the process dies before it returns."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter
        self.committed: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    def commit_unit(self, request: Any, batches: Any, **kwargs: Any) -> Any:
        result = self._adapter.commit_unit(request, batches, **kwargs)
        self.committed.append(result.snapshot.snapshot_id)
        raise _Crash("simulated crash after the unit commit, before it returned")


class _CrashAfterCommits:
    """Adapter proxy that dies before the commit after ``commits`` successful ones."""

    def __init__(self, adapter: Any, *, commits: int) -> None:
        self._adapter = adapter
        self._left = commits

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    def commit_batch(self, request: Any, batch: Any) -> Any:
        if self._left <= 0:
            raise _Crash("simulated crash before the next commit")
        self._left -= 1
        return self._adapter.commit_batch(request, batch)

    def commit_unit(self, request: Any, batches: Any, **kwargs: Any) -> Any:
        if self._left <= 0:
            raise _Crash("simulated crash before the next commit")
        self._left -= 1
        return self._adapter.commit_unit(request, batches, **kwargs)


def _ingested(outcome: Any) -> ArchiveIngested:
    assert isinstance(outcome, ArchiveIngested), outcome
    return outcome


def _element_history(harness: StoreHarness) -> list[Any]:
    head = harness.snapshot_id(AGG_TABLE)
    return list(history_from(harness.adapter, AGG_TABLE, head))


def _rows(harness: StoreHarness) -> list[dict[str, Any]]:
    return sorted(harness.rows(AGG_TABLE, COLUMNS), key=lambda row: row["archive_line_number"])


def _verify_in_windows(
    verifier: PersistedRowVerifier, rows: list[dict[str, Any]], window: int
) -> None:
    for offset in range(0, len(rows), window):
        verifier.verify_archive_elements(
            AGG, "agg_trades", "BTCUSDT", rows[offset : offset + window]
        )


# ------------------------------------------------------------------ §9(a) structure


@pytest.mark.parametrize(
    ("row_count", "d2_batch"), [(1, 1), (7, 2), (25, 4), (25, 25), (25, 1000), (40, 3)]
)
def test_a_new_archive_is_exactly_one_element_snapshot(
    harness: StoreHarness, row_count: int, d2_batch: int
) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=row_count))
    result = _ingested(harness.store(microbatch_rows=d2_batch).ingest(item.collected, item.context))

    history = _element_history(harness)
    assert len(history) == 1
    [unit] = history
    assert unit.batch_id == f"{result.archive_revision_id}{ROW_UNIT_INFIX}{row_count}"
    assert unit.added_rows == row_count and unit.parent_snapshot_id is None
    assert harness.adapter.commit_layout(AGG_TABLE, unit.snapshot_id) == CommitLayout(
        unit=True, window_rows=d2_batch
    )
    # The unit fingerprint is the rule on the whole unit, as committed.
    whole = pa.Table.from_pylist(_rows(harness), schema=AGG.arrow_schema)
    assert unit.batch_fingerprint == AGG.fingerprint_rule.fingerprint(whole)
    assert [(c.batch_id, c.row_count, c.outcome) for c in result.row_commits] == [
        (unit.batch_id, row_count, CommitOutcome.COMMITTED)
    ]
    assert harness.total_rows(AGG_TABLE) == row_count


def test_each_archive_adds_one_element_snapshot(harness: StoreHarness) -> None:
    store = harness.store(microbatch_rows=2)
    for symbol, day, count in (
        ("BTCUSDT", ps.US_DAY, 5),
        ("ETHUSDT", ps.US_DAY, 9),
        ("BTCUSDT", ps.MS_DAY, 1),
    ):
        item = rs.archive(
            harness.storage,
            symbol=symbol,
            day=day,
            rows=ps.agg_rows(day, count=count),
            retrieved_at=rs.INGEST,
        )
        _ingested(store.ingest(item.collected, item.context))
    assert [snapshot.added_rows for snapshot in _element_history(harness)] == [1, 9, 5]


def test_a_unit_batch_id_never_parses_as_a_microbatch_id() -> None:
    revision = "a" * 64
    unit = _row_unit_batch_id(revision, 12345678)
    assert parse_row_unit_batch_id(unit) == (revision, 12345678)
    assert not unit.startswith(f"{revision}.rows.")
    assert parse_row_unit_batch_id(_row_batch_id(revision, 3)) is None
    for malformed in (f"{revision}{ROW_UNIT_INFIX}007", f"{revision}{ROW_UNIT_INFIX}0"):
        with pytest.raises(CatalogIntegrityError, match="malformed"):
            parse_row_unit_batch_id(malformed)


# ------------------------------------------------------------------ idempotency


def test_re_ingest_replays_the_unit_whatever_the_microbatch_size(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=9))
    first = _ingested(harness.store(microbatch_rows=2).ingest(item.collected, item.context))
    harness.reopen()
    for d2_batch in (2, 5, 1000):
        again = _ingested(
            harness.store(microbatch_rows=d2_batch).ingest(item.collected, item.context)
        )
        assert again.replayed
        assert [c.snapshot_id for c in again.row_commits] == [
            c.snapshot_id for c in first.row_commits
        ]
    assert len(_element_history(harness)) == 1
    assert harness.total_rows(AGG_TABLE) == 9


def test_a_unit_committed_with_other_content_is_a_batch_conflict(
    harness: StoreHarness, tmp_path: Path
) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=4))
    # The archive row commits, then the process dies before the unit commit.
    with pytest.raises(_Crash):
        RawRevisionStore(
            _CrashAfterCommits(harness.adapter, commits=1), harness.storage, clock=harness.clock
        ).ingest(item.collected, item.context)
    assert harness.snapshot_id(AGG_TABLE) is None
    # A foreign writer commits other rows under this archive revision's unit id.
    with rs.store_harness(tmp_path / "other") as other:
        other_item = rs.archive(other.storage, rows=rs.replacement_rows(ps.US_DAY, count=4))
        other.store().ingest(other_item.collected, other_item.context)
        foreign = pa.Table.from_pylist(_rows(other), schema=AGG.arrow_schema)
    revision = harness.store().archive_revision_id(item.collected, item.context)
    request = CommitRequest(
        table=AGG_TABLE,
        batch_id=_row_unit_batch_id(revision, 4),
        batch_fingerprint=AGG.fingerprint_rule.fingerprint(foreign),
        row_count=4,
        expected_parent_snapshot_id=None,
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    harness.adapter.commit_unit(
        request, lambda: iter([foreign]), scratch_directory=scratch, window_rows=4
    )
    head = harness.snapshot_id(AGG_TABLE)

    with pytest.raises(BatchConflict):
        harness.store().ingest(item.collected, item.context)
    assert harness.snapshot_id(AGG_TABLE) == head


# ------------------------------------------------------------------ §9(c) crash injection


def test_a_crash_after_staging_before_the_commit_is_invisible_and_rerun_commits(
    harness: StoreHarness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=11))
    original_finish = StagedUnitWriter.finish
    staged: list[str] = []

    def finish_then_die(self: StagedUnitWriter) -> Any:
        files = original_finish(self)
        staged.extend(str(data_file.file_path) for data_file in files)
        raise _Crash("simulated crash after staging, before the unit commit")

    monkeypatch.setattr(StagedUnitWriter, "finish", finish_then_die)
    with pytest.raises(_Crash):
        harness.store(microbatch_rows=3).ingest(item.collected, item.context)
    monkeypatch.undo()

    assert staged, "the unit's data files were staged before the crash"
    assert harness.total_rows(ARCHIVE_TABLE) == 1
    assert harness.snapshot_id(AGG_TABLE) is None  # staged orphans are referenced by nothing
    assert harness.rows(AGG_TABLE, ("revision_id",)) == []

    harness.reopen()
    result = _ingested(harness.store(microbatch_rows=3).ingest(item.collected, item.context))

    assert result.archive_commit.replayed
    assert [c.outcome for c in result.row_commits] == [CommitOutcome.COMMITTED]
    history = _element_history(harness)
    assert len(history) == 1 and history[0].added_rows == 11
    assert harness.total_rows(AGG_TABLE) == 11
    with rs.store_harness(tmp_path / "clean") as clean:
        clean_item = rs.archive(clean.storage, rows=ps.agg_rows(ps.US_DAY, count=11))
        clean.store(microbatch_rows=3).ingest(clean_item.collected, clean_item.context)
        assert _rows(clean) == _rows(harness)


def test_a_crash_after_the_commit_before_return_replays(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=8))
    crashing = _CrashAfterUnitCommit(harness.adapter)
    with pytest.raises(_Crash):
        RawRevisionStore(crashing, harness.storage, clock=harness.clock, microbatch_rows=3).ingest(
            item.collected, item.context
        )
    [committed] = crashing.committed
    rows = _rows(harness)

    harness.reopen()
    result = _ingested(harness.store(microbatch_rows=3).ingest(item.collected, item.context))

    assert result.replayed
    assert [(c.snapshot_id, c.outcome) for c in result.row_commits] == [
        (committed, CommitOutcome.ALREADY_COMMITTED)
    ]
    assert [snapshot.snapshot_id for snapshot in _element_history(harness)] == [committed]
    assert _rows(harness) == rows


# ------------------------------------------------------------------ §9(b) both layouts


def test_both_layouts_hold_bitwise_identical_rows(harness: StoreHarness, tmp_path: Path) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=10))
    _ingested(harness.store(microbatch_rows=3).ingest(item.collected, item.context))
    with rs.store_harness(tmp_path / "legacy") as legacy:
        legacy_item = rs.archive(legacy.storage, rows=ps.agg_rows(ps.US_DAY, count=10))
        _ingested(
            legacy.store(microbatch_rows=3, _legacy_batch_commits=True).ingest(
                legacy_item.collected, legacy_item.context
            )
        )
        assert [s.added_rows for s in _element_history(legacy)] == [1, 3, 3, 3]
        legacy_rows = _rows(legacy)
    unit_rows = _rows(harness)
    assert unit_rows == legacy_rows
    assert pa.Table.from_pylist(unit_rows, schema=AGG.arrow_schema).equals(
        pa.Table.from_pylist(legacy_rows, schema=AGG.arrow_schema)
    )


def test_unit_rows_verify_in_windows_and_the_unit_is_proven_once(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=9))
    _ingested(harness.store(microbatch_rows=4).ingest(item.collected, item.context))
    rows = _rows(harness)
    view = PinnedCatalogView(
        harness.adapter,
        {table: harness.snapshot_id(table) for table in (AGG_TABLE, ARCHIVE_TABLE)},
    )
    for catalog in (harness.adapter, view):
        verifier = PersistedRowVerifier(catalog, harness.storage, cache_archives=True)
        try:
            _verify_in_windows(verifier, rows, 2)
            assert len(verifier._proven_units) == 1
        finally:
            verifier.close()


def test_a_bounded_metadata_view_reads_the_unit_layout(harness: StoreHarness) -> None:
    """The normalizer's pinned view resolves the layout from its bounded metadata pin."""
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=5))
    _ingested(harness.store(microbatch_rows=2).ingest(item.collected, item.context))
    limits = BoundedMetadataLimits(
        max_metadata_bytes=16 * 1024 * 1024,
        max_item_bytes=256 * 1024,
        max_retained_json_bytes=2 * 1024 * 1024,
        read_chunk_bytes=16 * 1024,
        max_small_array_items=512,
        max_map_items=512,
        max_snapshots=1000,
        run_capacity=64,
        run_limits=RunLimits(leaf_max_records=32, leaf_max_bytes=1024 * 1024, fanout=8),
        run_merge_fanout=8,
        key_tree_params=KeyTreeParams(page_max_bytes=1024 * 1024, leaf_max_records=64, fanout=8),
    )
    tables = (AGG_TABLE, ARCHIVE_TABLE)
    bounded = {
        table: harness.adapter.pin_bounded_metadata(table, storage=harness.storage, limits=limits)
        for table in tables
    }
    view = PinnedCatalogView(
        harness.adapter,
        {table: bounded[table].selected_snapshot_id for table in tables},
        bounded_metadata=bounded,
    )
    [unit] = _element_history(harness)
    assert view.commit_layout(AGG_TABLE, unit.snapshot_id) == CommitLayout(unit=True, window_rows=2)
    verifier = PersistedRowVerifier(view, harness.storage, cache_archives=True)
    try:
        _verify_in_windows(verifier, _rows(harness), 2)
        assert len(verifier._proven_units) == 1
    finally:
        verifier.close()


def test_old_per_microbatch_history_still_verifies(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=7))
    _ingested(
        harness.store(microbatch_rows=2, _legacy_batch_commits=True).ingest(
            item.collected, item.context
        )
    )
    assert [s.added_rows for s in _element_history(harness)] == [1, 2, 2, 2]
    verifier = PersistedRowVerifier(harness.adapter, harness.storage)
    _verify_in_windows(verifier, _rows(harness), 3)
    assert verifier._proven_units == {}


def test_an_old_prefix_is_completed_along_the_old_path(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=7))
    with pytest.raises(_Crash):
        RawRevisionStore(
            _CrashAfterCommits(harness.adapter, commits=3),
            harness.storage,
            clock=harness.clock,
            microbatch_rows=2,
            _legacy_batch_commits=True,
        ).ingest(item.collected, item.context)
    assert harness.total_rows(AGG_TABLE) == 4

    harness.reopen()
    result = _ingested(harness.store(microbatch_rows=2).ingest(item.collected, item.context))

    assert [c.replayed for c in result.row_commits] == [True, True, False, False]
    assert all(ROW_UNIT_INFIX not in s.batch_id for s in _element_history(harness))
    _verify_in_windows(PersistedRowVerifier(harness.adapter, harness.storage), _rows(harness), 3)


def test_a_mixed_layout_fails_closed(
    harness: StoreHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=6))
    with pytest.raises(_Crash):
        RawRevisionStore(
            _CrashAfterCommits(harness.adapter, commits=2),
            harness.storage,
            clock=harness.clock,
            microbatch_rows=2,
            _legacy_batch_commits=True,
        ).ingest(item.collected, item.context)
    # A buggy writer that ignores the committed prefix and adds a unit on top of it.
    buggy = harness.store(microbatch_rows=2)
    monkeypatch.setattr(buggy, "_element_layout", lambda table, revision: None)
    _ingested(buggy.ingest(item.collected, item.context))
    batch_ids = [s.batch_id for s in _element_history(harness)]
    assert ROW_UNIT_INFIX in batch_ids[0] and batch_ids[1].endswith(".rows.00000000")

    with pytest.raises(CatalogIntegrityError, match="mix the unit and the per-microbatch"):
        harness.store(microbatch_rows=2).ingest(item.collected, item.context)
    # Lines 3 … 6 are held once (by the unit); the verifier still refuses the mixed lineage.
    rows = [row for row in _rows(harness) if row["archive_line_number"] >= 3]
    assert len(rows) == 4
    with pytest.raises(CatalogIntegrityError, match="mix the unit and the per-microbatch"):
        PersistedRowVerifier(harness.adapter, harness.storage).verify_archive_elements(
            AGG, "agg_trades", "BTCUSDT", rows
        )


def test_a_unit_whose_lines_gained_a_row_fails_closed(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=5))
    result = _ingested(harness.store(microbatch_rows=2).ingest(item.collected, item.context))
    rows = _rows(harness)
    # A hostile writer adds a copy of line 3 under another revision id and arrival number.
    forged = dict(rows[2])
    forged.update(revision_id="f" * 64, arrival_seq=result.arrival_seq_base + 999)
    batch = pa.Table.from_pylist([forged], schema=AGG.arrow_schema)
    harness.adapter.commit_batch(
        CommitRequest(
            table=AGG_TABLE,
            batch_id="forged.rows",
            batch_fingerprint=AGG.fingerprint_rule.fingerprint(batch),
            row_count=1,
            expected_parent_snapshot_id=harness.snapshot_id(AGG_TABLE),
        ),
        batch,
    )
    with pytest.raises(CatalogIntegrityError, match="was committed with other content"):
        PersistedRowVerifier(harness.adapter, harness.storage).verify_archive_elements(
            AGG, "agg_trades", "BTCUSDT", rows[:1]
        )


def test_a_row_beyond_the_unit_is_refused(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=3))
    _ingested(harness.store().ingest(item.collected, item.context))
    verifier = PersistedRowVerifier(harness.adapter, harness.storage)
    [unit] = _element_history(harness)
    row = dict(_rows(harness)[0], archive_line_number=4)
    with pytest.raises(CatalogIntegrityError, match="is not committed by any batch"):
        verifier._verify_unit_rows(AGG, row["archive_revision_id"], unit, [row], unit.snapshot_id)

"""E1-CAP-1: proving archive rows needs one window, not the archive.

The strict D1 re-parse is spooled to disk and read back block by block; the archive's row-batch
plan is proven by one ordered history walk that keeps three ints; history walks load the table
metadata once. Verdicts are the ones of the collected forms (the rest of the D3E suite).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pyiceberg.expressions import EqualTo

from core.contracts.catalog import SnapshotInfo, SnapshotNotFound
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import BINANCE_SPOT_AGG_TRADES, BINANCE_SPOT_ARCHIVES
from infrastructure.parser import SpooledArchive
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision import RawRevisionStore
from infrastructure.revision.row_integrity import (
    PersistedRowVerifier,
    history_from,
    ordered_batches,
)
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision import revision_support as rs
from tests.infrastructure.revision.rest_store_support import (
    DAY,
    SYMBOL,
    ProxyCatalog,
    RestHarness,
    StepClock,
    agg_items,
    archive_agg_lines,
    utc,
)

TABLE = BINANCE_SPOT_AGG_TRADES.table
KNOWLEDGE = utc(2024, 1, 3)


@pytest.fixture
def h(tmp_path: Path) -> Iterator[RestHarness]:
    with ss.sqlite_harness(tmp_path) as opened:
        yield opened


@pytest.fixture
def spools(tmp_path: Path) -> Path:
    directory = tmp_path / "spools"
    directory.mkdir()
    return directory


def _ingest(h: RestHarness, count: int, *, batch: int) -> str:
    archive = rs.archive(
        h.storage,
        data_type="agg_trades",
        symbol=SYMBOL,
        day=DAY,
        rows=archive_agg_lines(agg_items(count)),
        retrieved_at=utc(2023, 11, 16),
        request_id="archive-1",
    )
    store = RawRevisionStore(
        h.adapter, h.storage, clock=StepClock(start=KNOWLEDGE), microbatch_rows=batch
    )
    outcome = store.ingest(archive.collected, archive.context)
    revision: str = outcome.archive_revision_id  # type: ignore[union-attr]
    return revision


def _rows(h: RestHarness) -> list[dict[str, Any]]:
    return sorted(h.rows(BINANCE_SPOT_AGG_TRADES), key=lambda row: row["archive_line_number"])


# ------------------------------------------------------------------ one-load history walks


def test_the_one_load_history_walk_is_the_snapshot_by_snapshot_walk(h: RestHarness) -> None:
    _ingest(h, 7, batch=2)
    head = h.head(TABLE)
    assert head is not None
    stepwise = list(history_from(ProxyCatalog(h.adapter), TABLE, head))  # no ``history``
    assert len(stepwise) >= 4
    assert list(h.adapter.history(TABLE, head)) == stepwise
    assert list(history_from(h.adapter, TABLE, head)) == stepwise
    view = PinnedCatalogView(h.adapter, {TABLE: head})
    assert list(history_from(view, TABLE, head)) == stepwise
    middle = stepwise[2].snapshot_id
    assert list(h.adapter.history(TABLE, middle)) == stepwise[2:]
    for missing in ("123", "not-an-id"):
        with pytest.raises(SnapshotNotFound):
            list(h.adapter.history(TABLE, missing))
    assert list(history_from(h.adapter, TABLE, None)) == []


# ------------------------------------------------------------------ ordered batch walks


def _snapshot(number: int, batch_id: str | None) -> SnapshotInfo:
    return SnapshotInfo(
        table=TABLE,
        snapshot_id=str(number),
        parent_snapshot_id=None,
        committed_at=datetime(2024, 1, 1, tzinfo=UTC),
        batch_id=batch_id,
        batch_fingerprint=None if batch_id is None else "0" * 64,
        added_rows=1,
        total_rows=number,
    )


def _walk(newest_first: list[str | None]) -> list[int]:
    history = [_snapshot(i + 1, batch_id) for i, batch_id in enumerate(newest_first)]
    return [
        index
        for index, _ in ordered_batches(
            history,
            TABLE,
            lambda batch_id: int(batch_id[2:]) if batch_id.startswith("b.") else None,
            lambda index: f"b.{index}",
            "test batches",
        )
    ]


def test_an_in_order_prefix_walks_in_constant_memory() -> None:
    assert _walk(["b.2", None, "other", "b.1", "b.0"]) == [2, 1, 0]
    assert _walk(["other", None]) == []


@pytest.mark.parametrize(
    ("newest_first", "match"),
    [
        (["b.1", "b.1", "b.0"], "b.1 but more than one snapshot committing it"),
        (["b.2", "b.1", "b.0", "b.2"], "b.2 but more than one snapshot committing it"),
        (["b.2", "b.0"], "not a contiguous prefix committed in order"),  # hole
        (["b.2", "b.1"], "not a contiguous prefix committed in order"),  # no batch 0
        (["b.0", "b.1"], "not a contiguous prefix committed in order"),  # 1 committed first
        (["b.1", "b.0", "b.3"], "not a contiguous prefix committed in order"),
    ],
)
def test_twice_committed_holed_or_reordered_batches_are_refused(
    newest_first: list[str | None], match: str
) -> None:
    with pytest.raises(CatalogIntegrityError, match=match):
        _walk(newest_first)


# ------------------------------------------------------------------ the verifier


def test_a_re_committed_archive_row_batch_is_refused(h: RestHarness, spools: Path) -> None:
    """Row 2 deleted and committed again under its own batch id: one copy of each row, but
    batch 1 committed twice (after batch 2) — refused by the ordered plan walk."""
    archive = _ingest(h, 3, batch=1)
    second = _rows(h)[1]
    h.delete_rows(
        BINANCE_SPOT_AGG_TRADES,
        EqualTo("revision_id", second["revision_id"]),  # type: ignore[call-arg, arg-type]
    )
    h.forge_snapshot(BINANCE_SPOT_AGG_TRADES, [second], batch_id=f"{archive}.rows.00000001")
    verifier = PersistedRowVerifier(h.adapter, h.storage, spool_dir=spools)
    with pytest.raises(
        CatalogIntegrityError,
        match="more than one snapshot committing it|not a contiguous prefix committed in order",
    ):
        verifier.verify_archive_elements(BINANCE_SPOT_AGG_TRADES, "agg_trades", SYMBOL, [second])
    assert not any(spools.iterdir())  # the refused proof left no spool behind


def test_a_verifier_keeps_no_spool_it_does_not_cache(h: RestHarness, spools: Path) -> None:
    archive = _ingest(h, 5, batch=2)
    rows = _rows(h)
    plain = PersistedRowVerifier(h.adapter, h.storage, spool_dir=spools)
    lineage = plain.verify_archive_elements(BINANCE_SPOT_AGG_TRADES, "agg_trades", SYMBOL, rows)
    assert lineage[archive].parsed.closed and not any(spools.iterdir())
    assert plain.archive_row_count("agg_trades", SYMBOL, archive) == 5
    assert not any(spools.iterdir())
    with PersistedRowVerifier(
        h.adapter, h.storage, cache_archives=True, spool_dir=spools
    ) as caching:
        caching.verify_archive_elements(BINANCE_SPOT_AGG_TRADES, "agg_trades", SYMBOL, rows[:2])
        caching.verify_archive_elements(BINANCE_SPOT_AGG_TRADES, "agg_trades", SYMBOL, rows[2:])
        assert len(list(spools.iterdir())) == 1  # parsed once, kept for every window
    assert not any(spools.iterdir())


class _Reads(ProxyCatalog):
    """Rows returned by every scan of the archive row table."""

    def __init__(self, inner: Any) -> None:
        super().__init__(inner)
        self.rows: list[int] = []

    def scan_columns(self, table: str, **kwargs: Any) -> Any:
        result = self.inner.scan_columns(table, **kwargs)
        if table == TABLE:
            self.rows.append(result.num_rows)
        return result


class _ArchiveMetadataReads(ProxyCatalog):
    """Limits and result sizes of scans that resolve archive revision metadata rows."""

    def __init__(self, inner: Any) -> None:
        super().__init__(inner)
        self.reads: list[tuple[int | None, int]] = []

    def scan_columns(self, table: str, **kwargs: Any) -> Any:
        result = super().scan_columns(table, **kwargs)
        if table == BINANCE_SPOT_ARCHIVES.table and tuple(kwargs["columns"]) == tuple(
            field.name for field in BINANCE_SPOT_ARCHIVES.arrow_schema
        ):
            self.reads.append((kwargs.get("limit"), result.num_rows))
        return result


def test_duplicate_archive_revision_lookup_reads_at_most_expected_plus_one(
    h: RestHarness, spools: Path
) -> None:
    """A million duplicate archive rows cannot become a million Python dicts in one proof."""
    archive = _ingest(h, 1, batch=1)
    [stored] = h.rows(BINANCE_SPOT_ARCHIVES)
    h.forge_rows(BINANCE_SPOT_ARCHIVES, [stored], "twin-archive")

    reads = _ArchiveMetadataReads(h.adapter)
    verifier = PersistedRowVerifier(reads, h.storage, spool_dir=spools)
    with pytest.raises(CatalogIntegrityError, match="archive revision .* committed 2 time"):
        verifier.verify_archive_elements(BINANCE_SPOT_AGG_TRADES, "agg_trades", SYMBOL, _rows(h))

    # One requested id is expected once; the scan reads only enough to prove a duplicate.
    assert reads.reads == [(2, 2)]
    assert archive == stored["revision_id"]
    assert not any(spools.iterdir())


def test_proving_a_window_reads_the_window_not_the_archive(
    h: RestHarness, spools: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window of 2 lines of a 40-line archive (row batches of 2, spool blocks of 2): every
    row-table read and every spool block read is at most 3 rows, whatever the archive holds."""
    _ingest(h, 40, batch=2)
    rows = _rows(h)
    blocks: list[int] = []
    original = SpooledArchive._chunk

    def logged(self: SpooledArchive, chunk: int) -> Any:
        table = original(self, chunk)
        blocks.append(table.num_rows)
        return table

    monkeypatch.setattr(SpooledArchive, "_chunk", logged)
    reads = _Reads(h.adapter)
    verifier = PersistedRowVerifier(
        reads, h.storage, cache_archives=True, spool_dir=spools, spool_rows=2
    )
    for window in (rows[10:12], rows[12:14], rows[38:40]):
        verifier.verify_archive_elements(BINANCE_SPOT_AGG_TRADES, "agg_trades", SYMBOL, window)
    verifier.close()
    assert reads.rows and max(reads.rows) <= 3
    assert blocks == [2, 2, 2]
    head = h.head(BINANCE_SPOT_ARCHIVES.table)
    assert head is not None

"""Archive lineage verification releases every strict-parser spool immediately."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.contracts.catalog import SnapshotInfo
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import BINANCE_SPOT_AGG_TRADES
from infrastructure.revision import row_integrity
from infrastructure.revision.row_integrity import PersistedRowVerifier
from infrastructure.revision.store import ArchiveIngested
from tests.infrastructure.parser import parser_support as ps
from tests.infrastructure.revision import revision_support as rs
from tests.infrastructure.revision.revision_support import StoreHarness


class _SnapshotHistoryAdapter:
    """Small deterministic catalog history source for the SQLite lookup tests."""

    def __init__(self, snapshots: list[SnapshotInfo], *, fail_after: int | None = None) -> None:
        self.snapshots = snapshots
        self.fail_after = fail_after

    def load_table(self, table: str) -> Any:
        return SimpleNamespace(current_snapshot=self.snapshots[0] if self.snapshots else None)

    def history(self, table: str, snapshot_id: str) -> Any:
        start = next(
            (
                index
                for index, snapshot in enumerate(self.snapshots)
                if snapshot.snapshot_id == snapshot_id
            ),
            len(self.snapshots),
        )
        for index, snapshot in enumerate(self.snapshots[start:]):
            if self.fail_after is not None and index == self.fail_after:
                raise RuntimeError("injected history failure")
            yield snapshot


def _snapshot(snapshot_id: str, parent: str | None, batch_id: str | None) -> SnapshotInfo:
    return SnapshotInfo(
        table="raw.test",
        snapshot_id=snapshot_id,
        parent_snapshot_id=parent,
        committed_at=datetime(2025, 1, 1, tzinfo=UTC),
        batch_id=batch_id,
        batch_fingerprint=None if batch_id is None else "a" * 64,
        added_rows=1,
        total_rows=1,
    )


def test_spooled_batch_lookup_counts_matches_and_keeps_newest_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshots = [
        _snapshot("s3", "s2", "wanted"),
        _snapshot("s2", "s1", "other"),
        _snapshot("s1", None, "wanted"),
    ]
    adapter = _SnapshotHistoryAdapter(snapshots)
    original_mkstemp = tempfile.mkstemp
    created_paths: list[Path] = []

    def tracked_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        kwargs["dir"] = tmp_path
        fd, name = original_mkstemp(*args, **kwargs)
        created_paths.append(Path(name))
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", tracked_mkstemp)

    with row_integrity._spooled_snapshots_of_batches(
        cast(Any, adapter), "raw.test", ("wanted", "wanted", "missing")
    ) as lookup:
        assert len(created_paths) == 1
        assert created_paths[0].exists()
        count, newest = lookup.one("wanted")
        assert count == 2
        assert newest is not None and newest.snapshot_id == "s3"
        assert lookup.one("missing") == (0, None)

    assert not created_paths[0].exists()
    lookup.close()  # close is safe to call more than once
    with pytest.raises(RuntimeError, match="closed"):
        lookup.one("wanted")


def test_spooled_batch_lookup_closes_and_unlinks_when_history_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshots = [
        _snapshot("s2", "s1", "wanted"),
        _snapshot("s1", "s0", "other"),
        _snapshot("s0", None, "other"),
    ]
    adapter = _SnapshotHistoryAdapter(snapshots, fail_after=1)
    original_mkstemp = tempfile.mkstemp
    created_paths: list[Path] = []

    def tracked_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        kwargs["dir"] = tmp_path
        fd, name = original_mkstemp(*args, **kwargs)
        created_paths.append(Path(name))
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", tracked_mkstemp)

    with pytest.raises(RuntimeError, match="injected history failure"):
        row_integrity._spooled_snapshots_of_batches(cast(Any, adapter), "raw.test", ("wanted",))

    assert len(created_paths) == 1
    assert not created_paths[0].exists()


def _two_archive_rows(harness: StoreHarness) -> list[dict[str, Any]]:
    first = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=3))
    second = rs.archive(
        harness.storage,
        rows=rs.replacement_rows(ps.US_DAY, count=2),
        retrieved_at=first.collected.retrieved_at + timedelta(days=30),
    )
    harness.store().ingest(first.collected, first.context)
    harness.store().ingest(second.collected, second.context)
    columns = tuple(field.name for field in BINANCE_SPOT_AGG_TRADES.arrow_schema)
    return harness.rows(BINANCE_SPOT_AGG_TRADES.table, columns)


def test_multi_archive_verification_uses_one_spool_and_returns_metadata(
    harness: StoreHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _two_archive_rows(harness)
    verifier = PersistedRowVerifier(harness.adapter, harness.storage)
    original_reparse = verifier._reparse
    original_verify_rows = verifier._verify_archive_rows
    opened: list[Any] = []
    events: list[tuple[str, str]] = []

    def tracked_reparse(collected: Any, data_type: str, archive_id: str) -> Any:
        assert all(spool._spool.closed for spool in opened)
        parsed = original_reparse(collected, data_type, archive_id)
        opened.append(parsed)
        events.append(("parse", archive_id))
        return parsed

    def tracked_verify_rows(
        definition: Any, data_type: str, archive: Any, parsed: Any, members: Any
    ) -> None:
        events.append(("verify", archive.revision_id))
        original_verify_rows(definition, data_type, archive, parsed, members)

    monkeypatch.setattr(verifier, "_reparse", tracked_reparse)
    monkeypatch.setattr(verifier, "_verify_archive_rows", tracked_verify_rows)

    verified = verifier.verify_archive_elements(
        BINANCE_SPOT_AGG_TRADES, "agg_trades", "BTCUSDT", rows
    )

    archive_ids = sorted({row["archive_revision_id"] for row in rows})
    assert list(verified) == archive_ids
    assert all(not hasattr(item, "parsed") for item in verified.values())
    assert all(spool._spool.closed for spool in opened)
    # The first pass strictly parses every archive before history checks; only then does the
    # second pass reparse and verify members one archive at a time.
    assert events[:2] == [("parse", archive_id) for archive_id in archive_ids]
    assert events[2:] == [
        pair
        for archive_id in archive_ids
        for pair in (("parse", archive_id), ("verify", archive_id))
    ]
    assert verifier._archives == {}


def test_member_validation_failure_closes_current_archive_spool(
    harness: StoreHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _two_archive_rows(harness)
    verifier = PersistedRowVerifier(harness.adapter, harness.storage, cache_archives=True)
    original_reparse = verifier._reparse
    opened: list[Any] = []

    def tracked_reparse(collected: Any, data_type: str, archive_id: str) -> Any:
        assert all(spool._spool.closed for spool in opened)
        parsed = original_reparse(collected, data_type, archive_id)
        opened.append(parsed)
        return parsed

    def reject_members(*_: Any) -> None:
        raise CatalogIntegrityError("injected member validation failure")

    monkeypatch.setattr(verifier, "_reparse", tracked_reparse)
    monkeypatch.setattr(verifier, "_verify_archive_rows", reject_members)

    with pytest.raises(CatalogIntegrityError, match="injected member validation failure"):
        verifier.verify_archive_elements(BINANCE_SPOT_AGG_TRADES, "agg_trades", "BTCUSDT", rows)

    assert len(opened) == 3  # two preflight parses, then the first member-verification parse
    assert all(spool._spool.closed for spool in opened)


def test_archive_row_count_closes_its_strict_parse_spool(
    harness: StoreHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=3))
    result = harness.store().ingest(item.collected, item.context)
    assert isinstance(result, ArchiveIngested)
    verifier = PersistedRowVerifier(harness.adapter, harness.storage, cache_archives=True)
    original_reparse = verifier._reparse
    opened: list[Any] = []

    def tracked_reparse(collected: Any, data_type: str, archive_id: str) -> Any:
        parsed = original_reparse(collected, data_type, archive_id)
        opened.append(parsed)
        return parsed

    monkeypatch.setattr(verifier, "_reparse", tracked_reparse)

    for _ in range(2):
        assert verifier.archive_row_count("agg_trades", "BTCUSDT", result.archive_revision_id) == 3
    assert len(opened) == 2
    assert all(spool._spool.closed for spool in opened)

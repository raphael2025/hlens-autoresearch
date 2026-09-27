"""Unit behaviour of ``RawRevisionStore`` on an injected SQLite catalog (never PG evidence).

Covers the write path, idempotent replay, crash recovery, replacement / competing heads,
microbatching and the fail-closed guards. The PostgreSQL evidence for the same scenarios is in
``test_store_postgres.py``.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

import pytest

from core.contracts.catalog import CommitOutcome
from core.contracts.collector import SourceBinding
from infrastructure.catalog import CatalogIntegrityError
from infrastructure.parser import parse_archive
from infrastructure.parser.binance_archive import PARSER_BINDING, ArchiveParseRequest
from infrastructure.revision import (
    ARCHIVE_TABLE,
    ROW_TABLES,
    ArchiveIngested,
    ArchiveRejected,
    RawRevisionStore,
    RevisionStoreConflict,
    RevisionStoreError,
    identity,
)
from infrastructure.revision.availability import AvailabilitySubject, rule_for
from tests.infrastructure.catalog.catalog_support import ScanSpy
from tests.infrastructure.parser import parser_support as ps
from tests.infrastructure.revision import revision_support as rs
from tests.infrastructure.revision.revision_support import StoreHarness

AGG_TABLE = ROW_TABLES["agg_trades"]
KLINE_TABLE = ROW_TABLES["klines_1m"]


def _ingested(outcome: Any) -> ArchiveIngested:
    assert isinstance(outcome, ArchiveIngested), outcome
    return outcome


def test_first_ingest_writes_archive_then_rows(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=5))
    result = _ingested(harness.store().ingest(item.collected, item.context))

    assert result.archive_commit.outcome is CommitOutcome.COMMITTED
    assert result.row_count == 5
    assert [commit.outcome for commit in result.row_commits] == [CommitOutcome.COMMITTED]
    assert result.rows_table == AGG_TABLE
    assert harness.total_rows(ARCHIVE_TABLE) == 1
    assert harness.total_rows(AGG_TABLE) == 5
    assert not result.replayed
    assert not result.has_competing_heads
    assert result.maximal_heads == (result.archive_revision_id,)


def test_archive_row_carries_the_contract_and_the_object(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage)
    result = _ingested(harness.store().ingest(item.collected, item.context))

    row = harness.rows(
        ARCHIVE_TABLE,
        (
            "observation_key",
            "revision_id",
            "source_id",
            "payload_hash",
            "arrival_seq",
            "event_time",
            "event_end_time",
            "source_time",
            "available_time",
            "ingest_time",
            "knowledge_time",
            "availability_evidence",
            "availability_evidence_gap",
            "supersedes",
            "precedence_evidence",
            "object_key",
            "object_sha256",
            "object_size_bytes",
            "source_uri",
            "retrieved_at",
            "collector_id",
            "collector_version",
            "collection_request_id",
            "source_metadata",
        ),
    )[0]
    assert row["revision_id"] == result.archive_revision_id
    assert row["payload_hash"] == item.sha256 == row["object_sha256"]
    assert row["object_key"] == item.collected.ref.key
    assert row["object_size_bytes"] == len(item.data)
    assert row["source_uri"] == item.collected.source_uri
    assert row["arrival_seq"] == 0
    # Interval observation: the archive covers one UTC day.
    assert row["event_time"] == item.collected.coverage_start
    assert row["event_end_time"] == item.collected.coverage_end
    # No official evidence bounds an archive revision's publication time (policy 1.0.0).
    assert row["source_time"] is None
    assert row["ingest_time"] == item.collected.retrieved_at == row["retrieved_at"]
    assert row["available_time"] == row["ingest_time"]
    assert row["availability_evidence"] == []
    assert row["availability_evidence_gap"] == rule_for(AvailabilitySubject.ARCHIVE).gap
    assert row["supersedes"] == [] and row["precedence_evidence"] == []
    assert row["collector_version"] == item.context.collector_version
    assert row["collection_request_id"] == "req-1"
    assert [pair["name"] for pair in row["source_metadata"]] == ["etag", "last-modified"]


def test_parsed_rows_bind_identity_lineage_and_arrival_block(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=3))
    result = _ingested(harness.store().ingest(item.collected, item.context))

    rows = sorted(
        harness.rows(
            AGG_TABLE,
            (
                "observation_key",
                "revision_id",
                "source_id",
                "payload_hash",
                "arrival_seq",
                "archive_revision_id",
                "archive_line_number",
                "parser_id",
                "parser_version",
                "parser_hash",
                "symbol",
                "agg_trade_id",
                "event_time",
                "available_time",
                "ingest_time",
                "knowledge_time",
                "availability_evidence_gap",
            ),
        ),
        key=lambda row: row["archive_line_number"],
    )
    assert [row["archive_line_number"] for row in rows] == [1, 2, 3]
    assert [row["arrival_seq"] for row in rows] == [1, 2, 3]
    assert all(row["archive_revision_id"] == result.archive_revision_id for row in rows)
    assert all(row["source_id"].endswith(result.archive_revision_id) for row in rows)
    assert all(row["parser_id"] == PARSER_BINDING.policy_id for row in rows)
    assert all(row["parser_hash"] == PARSER_BINDING.policy_hash for row in rows)
    assert [row["observation_key"] for row in rows] == [
        identity.agg_trade_observation_key("BTCUSDT", trade_id) for trade_id in (500, 501, 502)
    ]
    assert all(
        row["revision_id"]
        == identity.revision_id(
            row["observation_key"],
            identity.row_source_identity(result.archive_revision_id),
            row["payload_hash"],
        )
        for row in rows
    )
    assert all(row["available_time"] == row["ingest_time"] for row in rows)
    assert all(
        row["availability_evidence_gap"] == rule_for(AvailabilitySubject.AGG_TRADE).gap
        for row in rows
    )


def test_klines_use_interval_columns_and_their_own_block(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage, data_type="klines_1m", rows=ps.kline_rows(ps.US_DAY, 4))
    result = _ingested(harness.store().ingest(item.collected, item.context))

    rows = sorted(
        harness.rows(
            KLINE_TABLE,
            ("observation_key", "interval_start", "interval_end", "arrival_seq", "open_time_raw"),
        ),
        key=lambda row: row["arrival_seq"],
    )
    assert result.rows_table == KLINE_TABLE
    assert [row["arrival_seq"] for row in rows] == [1, 2, 3, 4]
    assert rows[1]["interval_end"] - rows[1]["interval_start"] == timedelta(minutes=1)
    assert rows[0]["observation_key"] == identity.kline_1m_observation_key(
        "BTCUSDT", rows[0]["interval_start"]
    )


def test_replay_of_the_same_archive_changes_nothing(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=7))
    store = harness.store()
    first = _ingested(store.ingest(item.collected, item.context))
    archive_snapshot = harness.snapshot_id(ARCHIVE_TABLE)
    rows_snapshot = harness.snapshot_id(AGG_TABLE)
    seqs = sorted(row["arrival_seq"] for row in harness.rows(AGG_TABLE, ("arrival_seq",)))

    second = _ingested(store.ingest(item.collected, item.context))

    assert second.replayed
    assert second.archive_revision_id == first.archive_revision_id
    assert second.arrival_seq_base == first.arrival_seq_base
    assert second.archive_commit.outcome is CommitOutcome.ALREADY_COMMITTED
    assert all(commit.replayed for commit in second.row_commits)
    assert harness.snapshot_id(ARCHIVE_TABLE) == archive_snapshot
    assert harness.snapshot_id(AGG_TABLE) == rows_snapshot
    assert harness.total_rows(AGG_TABLE) == 7
    assert sorted(row["arrival_seq"] for row in harness.rows(AGG_TABLE, ("arrival_seq",))) == seqs


def test_replay_after_restart_and_a_later_retrieved_at(harness: StoreHarness) -> None:
    """A re-download reuses the persisted knowledge-axis times, so nothing is rewritten."""
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=4))
    first = _ingested(harness.store().ingest(item.collected, item.context))
    before = harness.snapshot_id(AGG_TABLE)

    harness.reopen()
    later = rs.archive(
        harness.storage,
        rows=ps.agg_rows(ps.US_DAY, count=4),
        retrieved_at=item.collected.retrieved_at + timedelta(days=5),
    )
    second = _ingested(harness.store().ingest(later.collected, later.context))

    assert second.replayed
    assert second.archive_revision_id == first.archive_revision_id
    assert harness.snapshot_id(AGG_TABLE) == before
    stored = harness.rows(ARCHIVE_TABLE, ("ingest_time",))[0]
    assert stored["ingest_time"] == item.collected.retrieved_at


def test_recovery_after_a_crash_between_archive_and_rows(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=6))
    crashing = _CrashAfter(harness.adapter, commits=1)
    store = RawRevisionStore(crashing, harness.storage, clock=harness.clock)
    with pytest.raises(_Crash):
        store.ingest(item.collected, item.context)
    assert harness.total_rows(ARCHIVE_TABLE) == 1
    assert harness.total_rows(AGG_TABLE) == 0

    harness.reopen()
    result = _ingested(harness.store().ingest(item.collected, item.context))

    assert result.archive_commit.replayed
    assert result.arrival_seq_base == 0
    assert [commit.outcome for commit in result.row_commits] == [CommitOutcome.COMMITTED]
    assert harness.total_rows(AGG_TABLE) == 6


def test_recovery_after_a_crash_between_two_microbatches(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=6))
    crashing = _CrashAfter(harness.adapter, commits=3)  # archive + two row microbatches
    store = RawRevisionStore(crashing, harness.storage, clock=harness.clock, microbatch_rows=2)
    with pytest.raises(_Crash):
        store.ingest(item.collected, item.context)
    partial = harness.total_rows(AGG_TABLE)
    assert partial == 4  # only whole committed batches are visible

    harness.reopen()
    result = _ingested(harness.store(microbatch_rows=2).ingest(item.collected, item.context))

    assert [commit.replayed for commit in result.row_commits] == [True, True, False]
    assert harness.total_rows(AGG_TABLE) == 6
    assert sorted(row["arrival_seq"] for row in harness.rows(AGG_TABLE, ("arrival_seq",))) == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]


def test_a_crashed_run_and_a_clean_run_agree_row_by_row(
    harness: StoreHarness, tmp_path: Any
) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=6))
    crashing = _CrashAfter(harness.adapter, commits=2)
    with pytest.raises(_Crash):
        RawRevisionStore(crashing, harness.storage, clock=rs.StepClock(), microbatch_rows=2).ingest(
            item.collected, item.context
        )
    harness.reopen()
    harness.store(microbatch_rows=2).ingest(item.collected, item.context)
    recovered = _logical(harness.rows(AGG_TABLE, _ROW_COLUMNS))

    with rs.store_harness(tmp_path / "clean") as clean:
        clean_item = rs.archive(
            clean.storage,
            rows=ps.agg_rows(ps.US_DAY, count=6),
            retrieved_at=item.collected.retrieved_at,
        )
        clean.store(microbatch_rows=2).ingest(clean_item.collected, clean_item.context)
        assert _logical(clean.rows(AGG_TABLE, _ROW_COLUMNS)) == recovered


def test_replacement_appends_and_reports_competing_heads(harness: StoreHarness) -> None:
    original = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=3))
    first = _ingested(harness.store().ingest(original.collected, original.context))
    replacement = rs.archive(
        harness.storage,
        rows=rs.replacement_rows(ps.US_DAY, count=3),
        retrieved_at=original.collected.retrieved_at + timedelta(days=30),
    )
    assert replacement.sha256 != original.sha256
    assert replacement.collected.ref.key != original.collected.ref.key

    second = _ingested(harness.store().ingest(replacement.collected, replacement.context))

    assert second.archive_revision_id != first.archive_revision_id
    assert second.observation_key == first.observation_key
    assert second.has_competing_heads
    assert set(second.maximal_heads) == {first.archive_revision_id, second.archive_revision_id}
    assert second.competing_revision_ids == second.maximal_heads
    assert second.supersedes == ()
    # Both archives, both objects and both row sets stay readable.
    assert harness.total_rows(ARCHIVE_TABLE) == 2
    assert harness.total_rows(AGG_TABLE) == 6
    assert harness.storage.lookup(original.collected.ref.key) is not None
    assert harness.storage.lookup(replacement.collected.ref.key) is not None
    assert second.arrival_seq_base == identity.ARRIVAL_SEQ_STRIDE


def test_competing_heads_do_not_depend_on_arrival_order(
    harness: StoreHarness, tmp_path: Any
) -> None:
    old_rows = ps.agg_rows(ps.US_DAY, count=3)
    new_rows = rs.replacement_rows(ps.US_DAY, count=3)

    def ingest_in_order(target: StoreHarness, first: list[str], second: list[str]) -> set[str]:
        one = rs.archive(target.storage, rows=first)
        two = rs.archive(target.storage, rows=second, retrieved_at=rs.INGEST + timedelta(days=30))
        a = _ingested(target.store().ingest(one.collected, one.context))
        b = _ingested(target.store().ingest(two.collected, two.context))
        assert b.has_competing_heads
        return {a.archive_revision_id, b.archive_revision_id}

    forward = ingest_in_order(harness, old_rows, new_rows)
    with rs.store_harness(tmp_path / "reverse") as other:
        backward = ingest_in_order(other, new_rows, old_rows)
    assert forward == backward


def test_rejected_parse_writes_nothing(harness: StoreHarness) -> None:
    broken = ps.csv_bytes(ps.agg_rows(ps.US_DAY, count=2))[:-1]  # missing final LF
    item = rs.archive(
        harness.storage, data=ps.archive_for("agg_trades", "BTCUSDT", ps.US_DAY, broken)
    )
    outcome = harness.store().ingest(item.collected, item.context)

    assert isinstance(outcome, ArchiveRejected)
    assert outcome.quality_event.table == ARCHIVE_TABLE
    assert harness.total_rows(ARCHIVE_TABLE) == 0
    assert harness.total_rows(AGG_TABLE) == 0
    assert harness.snapshot_id(AGG_TABLE) is None
    # The verified immutable object survives; only the revisions are refused.
    assert harness.storage.lookup(item.collected.ref.key) is not None


def test_microbatches_are_bounded_and_ordered(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=10))
    result = _ingested(harness.store(microbatch_rows=3).ingest(item.collected, item.context))

    assert [commit.row_count for commit in result.row_commits] == [3, 3, 3, 1]
    assert len({commit.batch_id for commit in result.row_commits}) == 4
    assert harness.total_rows(AGG_TABLE) == 10


def test_two_archives_share_no_arrival_sequence_number(harness: StoreHarness) -> None:
    agg = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=4))
    kline = rs.archive(harness.storage, data_type="klines_1m", rows=ps.kline_rows(ps.US_DAY, 4))
    first = _ingested(harness.store().ingest(agg.collected, agg.context))
    second = _ingested(harness.store().ingest(kline.collected, kline.context))

    seqs = [row["arrival_seq"] for row in harness.rows(ARCHIVE_TABLE, ("arrival_seq",))]
    seqs += [row["arrival_seq"] for row in harness.rows(AGG_TABLE, ("arrival_seq",))]
    seqs += [row["arrival_seq"] for row in harness.rows(KLINE_TABLE, ("arrival_seq",))]
    assert len(seqs) == len(set(seqs)) == 10
    assert first.arrival_seq_base == 0
    assert second.arrival_seq_base == identity.ARRIVAL_SEQ_STRIDE


def test_blocks_continue_after_a_restart(harness: StoreHarness) -> None:
    one = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=2))
    _ingested(harness.store().ingest(one.collected, one.context))
    harness.reopen()
    two = rs.archive(harness.storage, symbol="ETHUSDT", rows=ps.agg_rows(ps.US_DAY, count=2))
    result = _ingested(harness.store().ingest(two.collected, two.context))
    assert result.arrival_seq_base == identity.ARRIVAL_SEQ_STRIDE


def test_foreign_object_key_is_refused(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage)
    moved = item.collected.model_copy(
        update={"source_uri": "https://data.binance.vision/data/spot/daily/aggTrades/x/y.zip"}
    )
    with pytest.raises(RevisionStoreError, match="official archive path"):
        harness.store().ingest(moved, item.context)


def test_unsupported_source_binding_is_refused(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage)
    with pytest.raises(RevisionStoreError, match="unsupported source"):
        item.context.__class__(
            request_id="r",
            data_type="agg_trades",
            collector_id="x.y",
            collector_version="1.0.0",
            source=SourceBinding(source_id="other.source", version="1.0.0"),
        )


def test_parse_of_another_archive_is_refused(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage)
    other = rs.archive(harness.storage, symbol="ETHUSDT")
    parsed = parse_archive(
        ArchiveParseRequest.for_collected_object(
            other.collected,
            data_type="agg_trades",
            archive_revision_id=harness.store().archive_revision_id(other.collected, other.context),
        ),
        harness.storage,
    )
    with pytest.raises(RevisionStoreError, match="another archive revision"):
        harness.store().ingest_parsed(item.collected, item.context, parsed)


def test_clock_before_ingest_time_fails_closed(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage)
    early = rs.StepClock(start=item.collected.retrieved_at - timedelta(minutes=1))
    store = RawRevisionStore(harness.adapter, harness.storage, clock=early)
    with pytest.raises(RevisionStoreConflict, match="knowledge_time"):
        store.ingest(item.collected, item.context)
    assert harness.total_rows(ARCHIVE_TABLE) == 0


def test_archive_retrieved_before_its_coverage_ends_fails_closed(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage, retrieved_at=ps.day_start(ps.US_DAY) + timedelta(hours=1))
    with pytest.raises(Exception, match="observable"):
        harness.store().ingest(item.collected, item.context)
    assert harness.total_rows(ARCHIVE_TABLE) == 0


def test_stored_archive_disagreeing_with_the_request_fails_closed(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage)
    harness.store().ingest(item.collected, item.context)
    tampered = item.collected.model_copy(
        update={"source_uri": item.collected.source_uri.replace("https://", "https://x.")}
    )
    with pytest.raises(RevisionStoreError):
        harness.store().ingest(tampered, item.context)


def test_microbatch_bounds_are_enforced(harness: StoreHarness) -> None:
    with pytest.raises(RevisionStoreError):
        harness.store(microbatch_rows=0)
    with pytest.raises(RevisionStoreError):
        harness.store(microbatch_rows=10**9)


def test_empty_archive_is_rejected_by_the_parser_and_writes_nothing(
    harness: StoreHarness,
) -> None:
    """D1 refuses an empty member, so D2 never reaches the "zero rows" commit path."""
    item = rs.archive(harness.storage, rows=[])
    outcome = harness.store().ingest(item.collected, item.context)
    assert isinstance(outcome, ArchiveRejected)
    assert outcome.rejection.code.value == "no_rows"
    assert harness.total_rows(ARCHIVE_TABLE) == 0


_ROW_COLUMNS = (
    "observation_key",
    "revision_id",
    "payload_hash",
    "arrival_seq",
    "archive_line_number",
    "agg_trade_id",
    "available_time",
    "ingest_time",
    "knowledge_time",
)


def _logical(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return sorted(tuple(row[column] for column in _ROW_COLUMNS) for row in rows)


class _Crash(RuntimeError):
    """Injected process death between two commits."""


class _CrashAfter:
    """Adapter proxy that dies after ``commits`` successful commits (no unwinding)."""

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


def test_sha256_of_the_published_object_is_the_archive_payload_hash(harness: StoreHarness) -> None:
    item = rs.archive(harness.storage)
    result = _ingested(harness.store().ingest(item.collected, item.context))
    assert result.archive_revision_id == identity.revision_id(
        result.observation_key,
        identity.archive_source_identity(),
        hashlib.sha256(item.data).hexdigest(),
    )


class _RecordingAdapter:
    """Adapter proxy recording the size and schema of every committed batch."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter
        self.batches: list[tuple[str, int]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    def commit_batch(self, request: Any, batch: Any) -> Any:
        self.batches.append((request.table, batch.num_rows))
        return self._adapter.commit_batch(request, batch)


def test_no_batch_exceeds_the_configured_microbatch_size(harness: StoreHarness) -> None:
    """Bounded memory: the store never aggregates beyond one microbatch, ever."""
    recording = _RecordingAdapter(harness.adapter)
    store = RawRevisionStore(recording, harness.storage, clock=harness.clock, microbatch_rows=4)
    first = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=11))
    second = rs.archive(harness.storage, symbol="ETHUSDT", rows=ps.agg_rows(ps.US_DAY, count=9))
    store.ingest(first.collected, first.context)
    store.ingest(second.collected, second.context)

    row_batches = [rows for table, rows in recording.batches if table == AGG_TABLE]
    assert max(row_batches) <= 4
    assert row_batches == [4, 4, 3, 4, 4, 1]  # per archive, never merged across archives
    assert [rows for table, rows in recording.batches if table == ARCHIVE_TABLE] == [1, 1]


def test_two_archives_never_share_a_row_batch(harness: StoreHarness) -> None:
    recording = _RecordingAdapter(harness.adapter)
    store = RawRevisionStore(recording, harness.storage, clock=harness.clock)
    first = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=3))
    second = rs.archive(
        harness.storage,
        day=ps.MS_DAY,
        rows=ps.agg_rows(ps.MS_DAY, count=3),
        retrieved_at=rs.INGEST,
    )
    a = _ingested(store.ingest(first.collected, first.context))
    b = _ingested(store.ingest(second.collected, second.context))

    batch_ids = {commit.batch_id for commit in (*a.row_commits, *b.row_commits)}
    assert len(batch_ids) == 2
    assert all(commit.batch_id.startswith(a.archive_revision_id) for commit in a.row_commits)
    assert all(commit.batch_id.startswith(b.archive_revision_id) for commit in b.row_commits)
    # Different coverage days are different observation keys and different blocks.
    assert a.observation_key != b.observation_key
    assert b.arrival_seq_base == identity.ARRIVAL_SEQ_STRIDE


def test_millisecond_day_archives_parse_and_persist(harness: StoreHarness) -> None:
    """The D1 unit boundary keeps working through the D2 write path."""
    item = rs.archive(
        harness.storage, day=ps.MS_DAY, rows=ps.agg_rows(ps.MS_DAY, count=3), retrieved_at=rs.INGEST
    )
    result = _ingested(harness.store().ingest(item.collected, item.context))
    rows = harness.rows(AGG_TABLE, ("timestamp_raw", "event_time", "payload_hash"))
    assert result.row_count == 3
    assert all(row["event_time"].year == 2024 for row in rows)
    # Millisecond ticks: the raw value is far smaller than the microsecond epoch value.
    assert all(row["timestamp_raw"] < 10**13 for row in rows)


def test_row_payload_hash_ignores_the_line_number(harness: StoreHarness) -> None:
    """Two archives with the same rows in the same order hash their rows identically."""
    item = rs.archive(harness.storage, rows=ps.agg_rows(ps.US_DAY, count=3))
    result = _ingested(harness.store().ingest(item.collected, item.context))
    rows = sorted(
        harness.rows(AGG_TABLE, ("payload_hash", "agg_trade_id", "archive_line_number")),
        key=lambda row: row["archive_line_number"],
    )
    expected = [
        identity.agg_trade_payload_hash(
            "BTCUSDT",
            "microsecond",
            {
                "agg_trade_id": row["agg_trade_id"],
                "price": _DEC("92792.05"),
                "quantity": _DEC("0.0015"),
                "first_trade_id": 1000 + (row["agg_trade_id"] - 500) * 3,
                "last_trade_id": 1002 + (row["agg_trade_id"] - 500) * 3,
                "timestamp_raw": _timestamp_of(row["agg_trade_id"]),
                "is_buyer_maker": (row["agg_trade_id"] - 500) % 2 == 1,
                "is_best_match": True,
            },
        )
        for row in rows
    ]
    assert [row["payload_hash"] for row in rows] == expected
    assert result.row_revision_count == 3


def _timestamp_of(agg_trade_id: int) -> int:
    return ps.start_ticks(ps.US_DAY) + (agg_trade_id - 500)


def _DEC(text: str) -> Any:
    from decimal import Decimal

    return Decimal(text).quantize(Decimal(1).scaleb(-18))


def test_identity_is_the_same_under_a_different_clock_and_a_different_order(
    harness: StoreHarness, tmp_path: Any
) -> None:
    """Revision ids depend on content, never on when or in which order we saw it."""
    first_rows = ps.agg_rows(ps.US_DAY, count=3)
    second_rows = ps.kline_rows(ps.US_DAY, count=3)

    def ingest_both(target: StoreHarness, agg_first: bool) -> dict[str, list[str]]:
        agg = rs.archive(target.storage, rows=first_rows)
        kline = rs.archive(target.storage, data_type="klines_1m", rows=second_rows)
        order = [agg, kline] if agg_first else [kline, agg]
        for item in order:
            target.store().ingest(item.collected, item.context)
        return {
            table: sorted(row["revision_id"] for row in target.rows(table, ("revision_id",)))
            for table in (ARCHIVE_TABLE, AGG_TABLE, KLINE_TABLE)
        }

    forward = ingest_both(harness, agg_first=True)
    with rs.store_harness(tmp_path / "later-clock") as other:
        other.clock = rs.StepClock(start=rs.KNOWLEDGE + timedelta(days=400))
        backward = ingest_both(other, agg_first=False)

    assert forward == backward


# ------------------------------------------------------------------ D2-R1: source evidence


def test_an_archive_without_an_official_checksum_is_refused(harness: StoreHarness) -> None:
    """A missing ``.CHECKSUM`` declaration may never be faked from the local object hash.

    The archive payload hash is a *source* claim. Without it there is nothing to bind the
    revision to, so ingest must fail before any snapshot exists on any of the three tables.
    """
    item = rs.archive(harness.storage)
    undeclared = item.collected.model_copy(update={"source_sha256": None})
    assert undeclared.source_sha256 is None

    with pytest.raises(RevisionStoreError, match="CHECKSUM"):
        harness.store().ingest(undeclared, item.context)

    for table in (ARCHIVE_TABLE, AGG_TABLE, KLINE_TABLE):
        assert harness.total_rows(table) == 0
        assert harness.snapshot_id(table) is None


def test_a_missing_checksum_is_refused_before_the_parse_too(harness: StoreHarness) -> None:
    """``ingest_parsed`` shares the same binding gate: no back door around the source claim."""
    item = rs.archive(harness.storage)
    parsed = parse_archive(
        ArchiveParseRequest.for_collected_object(
            item.collected,
            data_type=item.context.data_type,
            archive_revision_id=harness.store().archive_revision_id(item.collected, item.context),
        ),
        harness.storage,
    )
    undeclared = item.collected.model_copy(update={"source_sha256": None})

    with pytest.raises(RevisionStoreError, match="CHECKSUM"):
        harness.store().ingest_parsed(undeclared, item.context, parsed)

    assert harness.total_rows(ARCHIVE_TABLE) == 0


def test_the_persisted_source_hash_is_the_source_declaration(harness: StoreHarness) -> None:
    """The normal path writes the verified source claim, equal to the local object hash."""
    item = rs.archive(harness.storage)
    _ingested(harness.store().ingest(item.collected, item.context))

    rows = harness.rows(ARCHIVE_TABLE, ("source_sha256", "object_sha256", "payload_hash"))
    assert len(rows) == 1
    assert rows[0]["source_sha256"] == item.collected.source_sha256
    assert rows[0]["source_sha256"] == rows[0]["object_sha256"] == item.sha256
    assert rows[0]["payload_hash"] == item.sha256


def test_a_replay_verifies_the_persisted_source_hash(harness: StoreHarness) -> None:
    """``source_sha256`` is part of what a recovery re-checks field by field."""
    item = rs.archive(harness.storage)
    harness.store().ingest(item.collected, item.context)
    second = _ingested(harness.store().ingest(item.collected, item.context))

    assert second.archive_commit.outcome is CommitOutcome.ALREADY_COMMITTED
    assert harness.total_rows(ARCHIVE_TABLE) == 1


# ------------------------------------------------------------------ D2-R1: bounded anchor


def _distinct_archives(harness: StoreHarness, count: int) -> None:
    """Ingest ``count`` archives of different days: one archive row each, one key each."""
    store = harness.store()
    for index in range(count):
        item = rs.archive(
            harness.storage,
            day=ps.US_DAY + timedelta(days=index),
            retrieved_at=rs.INGEST + timedelta(days=index),
            rows=ps.agg_rows(ps.US_DAY + timedelta(days=index), count=2),
        )
        store.ingest(item.collected, item.context)


def test_the_allocation_anchor_never_materialises_the_whole_archive_history(
    harness: StoreHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the ``scan_columns(...).to_arrow()`` anchor (D2-R1 defect 2).

    After six archives the table holds six rows. The seventh ingest still has to find the
    largest committed ``arrival_seq``, but every Arrow table it materialises must stay a
    single-key read: only the streaming reducer may touch the whole history.
    """
    _distinct_archives(harness, 6)
    assert harness.total_rows(ARCHIVE_TABLE) == 6

    spy = ScanSpy(monkeypatch)
    item = rs.archive(
        harness.storage,
        day=ps.US_DAY + timedelta(days=6),
        retrieved_at=rs.INGEST + timedelta(days=6),
        rows=ps.agg_rows(ps.US_DAY + timedelta(days=6), count=2),
    )
    result = _ingested(harness.store().ingest(item.collected, item.context))

    assert result.arrival_seq_base == 6 * identity.ARRIVAL_SEQ_STRIDE
    assert spy.readers >= 1, "the anchor must go through the streaming batch reader"
    # The old implementation read all six committed rows into one table here.
    assert spy.largest_table <= 1, spy.to_arrow_rows
    assert sum(spy.batch_rows) == 6, "the reduction still visited every committed archive row"


def test_a_committed_anchor_that_is_not_a_block_base_fails_closed(
    harness: StoreHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt anchor must stop the allocation, not seed the next block from garbage."""
    monkeypatch.setattr(identity, "arrival_block_base", lambda _max: 5)
    poisoned = rs.archive(harness.storage)
    # The archive row commits with an unlawful ``arrival_seq``; the rows then refuse it.
    with pytest.raises(identity.IdentityViolation):
        harness.store().ingest(poisoned.collected, poisoned.context)
    monkeypatch.undo()
    assert harness.rows(ARCHIVE_TABLE, ("arrival_seq",)) == [{"arrival_seq": 5}]

    later = rs.archive(
        harness.storage,
        day=ps.US_DAY + timedelta(days=1),
        retrieved_at=rs.INGEST + timedelta(days=1),
        rows=ps.agg_rows(ps.US_DAY + timedelta(days=1), count=2),
    )
    with pytest.raises(CatalogIntegrityError, match="block base"):
        harness.store().ingest(later.collected, later.context)

    assert harness.total_rows(ARCHIVE_TABLE) == 1
    assert harness.total_rows(AGG_TABLE) == 0

"""E3 partition quality reports (roadmap #16; ADR-0023 §2, ADR-0028 §7)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pyiceberg.expressions import And, EqualTo

from core.contracts.catalog import CommitConflict, CommitRequest
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import DATA_QUALITY_REPORTS, QUALITY_EVIDENCE_GAPS
from infrastructure.pit.selector import PIT_BINDING
from infrastructure.quality import reporter as q
from infrastructure.quality.reporter import QualityReporter, QualityReportError
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.pit.test_selector import _chain
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import (
    DAY,
    SYMBOL,
    RestHarness,
    StepClock,
    utc,
)

K_A, K_R, N_A, N_R, K_E = (utc(2023, 12, d) for d in (1, 5, 6, 7, 10))
K_Q = utc(2023, 12, 20)
REPORTS = DATA_QUALITY_REPORTS


def _ingest(h: RestHarness, data_type: str, items: list[Any], archive_items: Any = None) -> None:
    lines = (ss.archive_agg_lines if data_type == "agg_trades" else ss.archive_kline_lines)(
        items if archive_items is None else archive_items
    )
    archive = c.ingest_archive(h, data_type, lines, knowledge=K_A)
    [response] = c.ingest_rest(h, data_type, items, knowledge=K_R)
    raw = (
        (c.ARCHIVE_AGGS, c.REST_AGGS)
        if data_type == "agg_trades"
        else (
            c.ARCHIVE_KLINES,
            c.REST_KLINES,
        )
    )
    c.normalizer(h, clock=StepClock(start=N_A)).normalize_unit(raw[0].table, archive)
    c.normalizer(h, clock=StepClock(start=N_R)).normalize_unit(raw[1].table, response)
    h.reconciler(clock=StepClock(start=K_E)).reconcile(data_type, SYMBOL, DAY)


def _events(row: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    return [event for event in row["events"] if event["event_type"] == kind]


def test_a_bar_partition_report_lists_gaps_inputs_and_evidence_gaps(h: RestHarness) -> None:
    items = ss.kline_items(2)  # 22:14 and 22:15 of the day
    _ingest(h, "klines_1m", items)
    clock = StepClock(start=K_Q)

    out = QualityReporter(
        h.adapter, h.storage, canonical_scratch_directory=h.canonical_scratch_directory, clock=clock
    ).report("klines_1m", SYMBOL, DAY)

    assert clock.calls == 1 and not out.reused
    [row] = h.rows(REPORTS)
    assert row == out.row
    start = utc(2023, 11, 14)
    assert (row["subject_table"], row["subject_symbol"]) == ("canonical.bars_1m", SYMBOL)
    assert (row["subject_start"], row["subject_end"]) == (start, start + timedelta(days=1))
    assert row["subject_snapshot_id"] == h.head(c.BARS.table)
    assert row["knowledge_time"] == K_Q
    assert row["quality_rule_hash"] == q.QUALITY_RULE_HASH
    [inputs] = _events(row, "report_inputs")
    bindings = json.loads(inputs["detail"])
    assert bindings["canonical.bars_1m"] == h.head(c.BARS.table)
    assert bindings["raw.binance_spot_precedence_evidence"] == h.head(c.EVIDENCE.table)
    gaps = sorted((e["event_start"], e["event_end"]) for e in _events(row, "bar_1m_gap"))
    assert gaps == [
        (start, utc(2023, 11, 14, 22, 14)),
        (utc(2023, 11, 14, 22, 16), start + timedelta(days=1)),
    ]
    assert _events(row, "competing_heads") == []  # the D-33 edges order both keys
    bars = h.rows(c.BARS)
    assert sorted(
        g["revision_id"] for g in q.evidence_gaps_of(h.adapter, row["report_id"])
    ) == sorted(b["revision_id"] for b in bars)
    assert all(
        g["gap"].startswith("inherited from ")
        for g in q.evidence_gaps_of(h.adapter, row["report_id"])
    )


# ------------------------------------------------------------------ ADR-0031 (QG-2)

GAPS = QUALITY_EVIDENCE_GAPS


def _gap_batches(h: RestHarness, report_id: str) -> list[tuple[str | None, int | None]]:
    return [
        (s.batch_id, s.added_rows)
        for s in h.history(GAPS.table)
        if s.batch_id is not None and s.batch_id.startswith(f"{report_id}.gaps.")
    ]


def test_gaps_live_in_their_own_table_in_bounded_batches(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(q, "_GAP_BATCH_ROWS", 2)
    _ingest(h, "klines_1m", ss.kline_items(3))
    out = QualityReporter(
        h.adapter,
        h.storage,
        canonical_scratch_directory=h.canonical_scratch_directory,
        clock=StepClock(start=K_Q),
    ).report("klines_1m", SYMBOL, DAY)
    assert out.row["evidence_gaps"] == []
    gaps = q.evidence_gaps_of(h.adapter, out.report_id)
    assert len(gaps) == 6  # 3 archive + 3 REST bar revisions, all without publication evidence
    assert {g["subject_symbol"] for g in gaps} == {SYMBOL}
    assert {g["subject_start"] for g in gaps} == {utc(2023, 11, 14)}
    assert _gap_batches(h, out.report_id) == [
        (f"{out.report_id}.gaps.00000000", 2),
        (f"{out.report_id}.gaps.00000001", 2),
        (f"{out.report_id}.gaps.00000002", 2),
    ]
    [event] = _events(out.row, "evidence_gaps")
    assert event["detail"].startswith("6 evidence gap(s) in 3 batch(es)")


def test_a_reused_report_verifies_its_gaps_without_writing(h: RestHarness) -> None:
    _ingest(h, "klines_1m", ss.kline_items(2))
    first = QualityReporter(
        h.adapter,
        h.storage,
        canonical_scratch_directory=h.canonical_scratch_directory,
        clock=StepClock(start=K_Q),
    ).report("klines_1m", SYMBOL, DAY)
    heads = (h.head(GAPS.table), h.head(REPORTS.table))
    again = QualityReporter(
        h.adapter,
        h.storage,
        canonical_scratch_directory=h.canonical_scratch_directory,
        clock=StepClock(start=K_Q),
    ).report("klines_1m", SYMBOL, DAY)
    assert again.reused and again.row == first.row
    assert (h.head(GAPS.table), h.head(REPORTS.table)) == heads


@pytest.mark.parametrize("tamper", ["delete", "extra"])
def test_a_reused_report_with_tampered_gaps_fails_closed(h: RestHarness, tamper: str) -> None:
    _ingest(h, "klines_1m", ss.kline_items(2))
    first = QualityReporter(
        h.adapter,
        h.storage,
        canonical_scratch_directory=h.canonical_scratch_directory,
        clock=StepClock(start=K_Q),
    ).report("klines_1m", SYMBOL, DAY)
    [gap, *_] = q.evidence_gaps_of(h.adapter, first.report_id)
    if tamper == "delete":
        h.delete_rows(GAPS, EqualTo("revision_id", gap["revision_id"]))  # type: ignore[call-arg, arg-type]
        heads_before = h.head(GAPS.table)
    else:
        h.forge_rows(GAPS, [dict(gap, batch_index=0)], f"{first.report_id}.gaps.00000099")
        heads_before = h.head(GAPS.table)
    clock = StepClock(start=K_Q)
    # A deleted row leaves every batch snapshot intact: the rows themselves are compared.
    with pytest.raises(CatalogIntegrityError, match="gap row"):
        QualityReporter(
            h.adapter,
            h.storage,
            canonical_scratch_directory=h.canonical_scratch_directory,
            clock=clock,
        ).report("klines_1m", SYMBOL, DAY)
    assert clock.calls == 0 and h.head(GAPS.table) == heads_before


def _stored_gaps(h: RestHarness, report_id: str) -> list[dict[str, Any]]:
    return [row for row in h.rows(GAPS) if row["quality_report_id"] == report_id]


def _replace_row(h: RestHarness, old: dict[str, Any], new: dict[str, Any]) -> None:
    """A hostile maintenance writer: one row deleted, one row appended (the count is kept)."""
    h.delete_rows(
        GAPS,
        And(
            EqualTo("revision_id", old["revision_id"]),  # type: ignore[call-arg, arg-type]
            EqualTo("table", old["table"]),  # type: ignore[call-arg, arg-type]
        ),
    )
    h.forge_rows(GAPS, [new], "maintenance")


def test_a_reused_report_with_an_altered_gap_row_fails_closed(h: RestHarness) -> None:
    """Review QG-R1 #2: same row count, one gap text changed."""
    _ingest(h, "klines_1m", ss.kline_items(2))
    first = QualityReporter(
        h.adapter,
        h.storage,
        canonical_scratch_directory=h.canonical_scratch_directory,
        clock=StepClock(start=K_Q),
    ).report("klines_1m", SYMBOL, DAY)
    stored = _stored_gaps(h, first.report_id)
    _replace_row(h, stored[0], dict(stored[0], gap=stored[0]["gap"] + " (altered)"))
    assert len(_stored_gaps(h, first.report_id)) == len(stored)
    clock = StepClock(start=K_Q)
    with pytest.raises(CatalogIntegrityError, match="other gap rows in batch"):
        QualityReporter(
            h.adapter,
            h.storage,
            canonical_scratch_directory=h.canonical_scratch_directory,
            clock=clock,
        ).report("klines_1m", SYMBOL, DAY)
    assert clock.calls == 0


def test_a_reused_report_with_a_row_moved_to_another_batch_fails_closed(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review QG-R1 #2: the report's rows are the same multiset, one row in the wrong batch."""
    monkeypatch.setattr(q, "_GAP_BATCH_ROWS", 2)
    _ingest(h, "klines_1m", ss.kline_items(3))
    first = QualityReporter(
        h.adapter,
        h.storage,
        canonical_scratch_directory=h.canonical_scratch_directory,
        clock=StepClock(start=K_Q),
    ).report("klines_1m", SYMBOL, DAY)
    stored = _stored_gaps(h, first.report_id)
    [moved, *_] = [row for row in stored if row["batch_index"] == 0]
    _replace_row(h, moved, dict(moved, batch_index=1))
    after = _stored_gaps(h, first.report_id)
    assert sorted((r["table"], r["revision_id"], r["gap"]) for r in after) == sorted(
        (r["table"], r["revision_id"], r["gap"]) for r in stored
    )
    clock = StepClock(start=K_Q)
    with pytest.raises(CatalogIntegrityError, match=r"other gap rows in batch .*\.gaps\.00000000"):
        QualityReporter(
            h.adapter,
            h.storage,
            canonical_scratch_directory=h.canonical_scratch_directory,
            clock=clock,
        ).report("klines_1m", SYMBOL, DAY)
    assert clock.calls == 0


@pytest.mark.parametrize("reuse", [False, True], ids=["write", "verify"])
def test_a_gap_batch_appearing_mid_run_fails_closed(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch, reuse: bool
) -> None:
    """Review QG-R1 #3: a batch committed after the history was read is still caught, both
    when the report is written and when a committed one is verified."""
    monkeypatch.setattr(q, "_GAP_BATCH_ROWS", 2)
    _ingest(h, "klines_1m", ss.kline_items(3))
    if reuse:
        QualityReporter(
            h.adapter,
            h.storage,
            canonical_scratch_directory=h.canonical_scratch_directory,
            clock=StepClock(start=K_Q),
        ).report("klines_1m", SYMBOL, DAY)
    reports_before = h.rows(REPORTS)
    check = q._GapWriter._check_rows
    forged: list[str] = []

    def check_then_forge(writer: Any, index: int, rows: list[dict[str, Any]]) -> None:
        check(writer, index, rows)
        if index == 0 and not forged:
            if reuse:
                assert writer._committed is not None  # the batch history has been read
            batch_id = f"{writer._report_id}.gaps.00000007"
            h.forge_rows(GAPS, [dict(rows[0], batch_index=7)], batch_id)
            forged.append(batch_id)

    monkeypatch.setattr(q._GapWriter, "_check_rows", check_then_forge)
    clock = StepClock(start=K_Q)
    with pytest.raises(CatalogIntegrityError, match=r"in batch indices 0\.\.7"):
        QualityReporter(
            h.adapter,
            h.storage,
            canonical_scratch_directory=h.canonical_scratch_directory,
            clock=clock,
        ).report("klines_1m", SYMBOL, DAY)
    assert forged and clock.calls == 0 and h.rows(REPORTS) == reports_before


@pytest.mark.parametrize("reuse", [False, True], ids=["write", "verify"])
def test_gap_verification_reads_at_most_one_batch_per_scan(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch, reuse: bool
) -> None:
    """Review QG-R1 #1: no scan of the gap table returns more than one batch of gap rows; the
    one scan over the whole report reads the batch_index column only."""
    monkeypatch.setattr(q, "_GAP_BATCH_ROWS", 2)
    _ingest(h, "klines_1m", ss.kline_items(3))
    if reuse:
        QualityReporter(
            h.adapter,
            h.storage,
            canonical_scratch_directory=h.canonical_scratch_directory,
            clock=StepClock(start=K_Q),
        ).report("klines_1m", SYMBOL, DAY)
    scan = h.adapter.scan_columns
    scans: list[tuple[tuple[str, ...], int]] = []

    def spy(table: str, **kwargs: Any) -> Any:
        found = scan(table, **kwargs)
        if table == GAPS.table:
            scans.append((tuple(kwargs["columns"]), found.num_rows))
        return found

    monkeypatch.setattr(h.adapter, "scan_columns", spy)
    out = QualityReporter(
        h.adapter,
        h.storage,
        canonical_scratch_directory=h.canonical_scratch_directory,
        clock=StepClock(start=K_Q),
    ).report("klines_1m", SYMBOL, DAY)
    assert out.reused is reuse
    wide = [rows for columns, rows in scans if columns != ("batch_index",)]
    narrow = [rows for columns, rows in scans if columns == ("batch_index",)]
    assert wide == [2, 2, 2]  # one scan per batch, each bounded by the batch size
    assert narrow == [6]  # the whole report: one int64 column


def test_a_gap_batch_losing_a_commit_race_is_retried(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review QG-R1 #4: a CommitConflict on a gap batch goes through the report's retry loop;
    the batch already written replays idempotently."""
    monkeypatch.setattr(q, "_GAP_BATCH_ROWS", 2)
    _ingest(h, "klines_1m", ss.kline_items(3))
    commit = h.adapter.commit_batch
    gap_commits: list[str] = []

    def racing(request: CommitRequest, table: Any) -> Any:
        if request.table == GAPS.table:
            gap_commits.append(request.batch_id)
            if len(gap_commits) == 2:
                raise CommitConflict("another writer moved the gap table")
        return commit(request, table)

    monkeypatch.setattr(h.adapter, "commit_batch", racing)
    out = QualityReporter(
        h.adapter,
        h.storage,
        canonical_scratch_directory=h.canonical_scratch_directory,
        clock=StepClock(start=K_Q),
    ).report("klines_1m", SYMBOL, DAY)
    batches = [f"{out.report_id}.gaps.{i:08d}" for i in range(3)]
    assert gap_commits == [batches[0], batches[1], *batches]
    assert _gap_batches(h, out.report_id) == [(batch_id, 2) for batch_id in batches]
    assert len(q.evidence_gaps_of(h.adapter, out.report_id)) == 6


def test_competing_heads_and_trade_id_jumps_are_reported(h: RestHarness) -> None:
    items = ss.agg_items(3)
    archive_items = [dict(item) for item in items]
    archive_items[1]["p"] = "92792.04000000"  # key 101 differs across channels: no edge
    _ingest(h, "agg_trades", items, archive_items)
    [later] = c.ingest_rest(
        h, "agg_trades", ss.agg_items(1, first_id=110, first_ms=ss.T0 + 10), knowledge=K_R,
        request_id="req-110",
    )  # fmt: skip
    c.normalizer(h, clock=StepClock(start=N_R)).normalize_unit(c.REST_AGGS.table, later)

    out = QualityReporter(
        h.adapter,
        h.storage,
        canonical_scratch_directory=h.canonical_scratch_directory,
        clock=StepClock(start=K_Q),
    ).report("agg_trades", SYMBOL, DAY)

    [competing] = _events(out.row, "competing_heads")
    assert competing["observation_key"] == f"binance:spot:agg_trade:{SYMBOL}:101"
    key_rows = [r for r in h.rows(c.TRADES) if r["observation_key"] == competing["observation_key"]]
    assert competing["revision_ids"] == sorted(r["revision_id"] for r in key_rows)
    [jump] = _events(out.row, "agg_trade_id_discontinuity")
    assert jump["detail"] == "aggregate trade ids jump from 102 to 110 (7 id(s) absent)"


@pytest.mark.parametrize(("next_id", "absent"), [(103, None), (104, 1)])
def test_trade_id_jumps_at_the_boundary(h: RestHarness, next_id: int, absent: int | None) -> None:
    """Consecutive ids are not a jump; a single missing id is."""
    _ingest(h, "agg_trades", ss.agg_items(3))
    [later] = c.ingest_rest(
        h, "agg_trades", ss.agg_items(1, first_id=next_id, first_ms=ss.T0 + 10), knowledge=K_R,
        request_id="req-next",
    )  # fmt: skip
    c.normalizer(h, clock=StepClock(start=N_R)).normalize_unit(c.REST_AGGS.table, later)
    out = QualityReporter(
        h.adapter,
        h.storage,
        canonical_scratch_directory=h.canonical_scratch_directory,
        clock=StepClock(start=K_Q),
    ).report("agg_trades", SYMBOL, DAY)
    jumps = [e["detail"] for e in _events(out.row, "agg_trade_id_discontinuity")]
    if absent is None:
        assert jumps == []
    else:
        assert jumps == [f"aggregate trade ids jump from 102 to {next_id} ({absent} id(s) absent)"]


def test_a_trade_day_is_proven_hour_by_hour_into_one_report(h: RestHarness) -> None:
    """G3-S3: slices are an implementation detail; a jump across an hour boundary, and every
    evidence gap of every slice, land in the one day report."""
    early = ss.agg_items(2, first_id=100, first_ms=ss.T0 - 15 * ss.MINUTE_MS)  # 21:59
    late = ss.agg_items(2, first_id=105, first_ms=ss.T0)  # 22:14
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(early + late), knowledge=K_A)
    c.normalizer(h, clock=StepClock(start=N_A)).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    selected: list[tuple[datetime, datetime]] = []
    reporter = QualityReporter(
        h.adapter,
        h.storage,
        canonical_scratch_directory=h.canonical_scratch_directory,
        clock=StepClock(start=K_Q),
    )
    select = reporter._select

    def spy(data_type: str, symbol: str, start: datetime, end: datetime, bindings: Any) -> Any:
        selected.append((start, end))
        return select(data_type, symbol, start, end, bindings)

    reporter._select = spy  # type: ignore[method-assign]
    out = reporter.report("agg_trades", SYMBOL, DAY)
    assert selected == [
        (utc(2023, 11, 14, 21), utc(2023, 11, 14, 22)),
        (utc(2023, 11, 14, 22), utc(2023, 11, 14, 23)),
    ]
    [jump] = _events(out.row, "agg_trade_id_discontinuity")
    assert jump["detail"] == "aggregate trade ids jump from 101 to 105 (3 id(s) absent)"
    rows = h.rows(c.TRADES)
    assert [gap["revision_id"] for gap in q.evidence_gaps_of(h.adapter, out.report_id)] == sorted(
        row["revision_id"] for row in rows if row["availability_evidence_gap"] is not None
    )
    assert len(q.evidence_gaps_of(h.adapter, out.report_id)) == 4


def test_a_replay_reuses_the_report_and_new_data_makes_a_new_one(h: RestHarness) -> None:
    _ingest(h, "klines_1m", ss.kline_items(1))
    first = QualityReporter(
        h.adapter,
        h.storage,
        canonical_scratch_directory=h.canonical_scratch_directory,
        clock=StepClock(start=K_Q),
    ).report("klines_1m", SYMBOL, DAY)
    later = StepClock(start=K_Q + timedelta(days=3))
    again = QualityReporter(
        h.adapter, h.storage, canonical_scratch_directory=h.canonical_scratch_directory, clock=later
    ).report("klines_1m", SYMBOL, DAY)
    assert again.reused and later.calls == 0 and again.row == first.row
    assert len(h.rows(REPORTS)) == 1
    [response] = c.ingest_rest(
        h, "klines_1m", ss.kline_items(1, first_ms=ss.T0 + 3 * ss.MINUTE_MS), knowledge=K_R,
        request_id="req-k2",
    )  # fmt: skip
    c.normalizer(h, clock=StepClock(start=N_R)).normalize_unit(c.REST_KLINES.table, response)
    newer = QualityReporter(
        h.adapter, h.storage, canonical_scratch_directory=h.canonical_scratch_directory, clock=later
    ).report("klines_1m", SYMBOL, DAY)
    assert newer.report_id != first.report_id and len(h.rows(REPORTS)) == 2


def test_a_raw_unit_not_yet_normalized_gets_no_report(h: RestHarness) -> None:
    """G2-R1a / RT-2: every Raw element revision of the partition needs its Canonical image."""
    _ingest(h, "klines_1m", ss.kline_items(1))
    [response] = c.ingest_rest(
        h, "klines_1m", ss.kline_items(1, first_ms=ss.T0 + 3 * ss.MINUTE_MS), knowledge=K_R,
        request_id="req-k2",
    )  # fmt: skip
    clock = StepClock(start=K_Q)
    with pytest.raises(q.RawNotDerived, match="1 Raw revision"):
        QualityReporter(
            h.adapter,
            h.storage,
            canonical_scratch_directory=h.canonical_scratch_directory,
            clock=clock,
        ).report("klines_1m", SYMBOL, DAY)
    assert clock.calls == 0 and h.rows(REPORTS) == [] and h.rows(QUALITY_EVIDENCE_GAPS) == []
    c.normalizer(h, clock=StepClock(start=N_R)).normalize_unit(c.REST_KLINES.table, response)
    out = QualityReporter(
        h.adapter, h.storage, canonical_scratch_directory=h.canonical_scratch_directory, clock=clock
    ).report("klines_1m", SYMBOL, DAY)
    assert not out.reused and len(h.rows(REPORTS)) == 1


def test_an_unprovable_partition_gets_no_report(h: RestHarness) -> None:
    _ingest(h, "klines_1m", ss.kline_items(1))
    [row] = [r for r in h.rows(c.BARS) if r["lineage_raw_table"] == c.REST_KLINES.table]
    h.delete_rows(c.BARS, EqualTo("revision_id", row["revision_id"]))  # type: ignore[call-arg, arg-type]
    h.forge_rows(
        c.BARS, [dict(row, knowledge_time=row["knowledge_time"] + timedelta(hours=1))], "x"
    )
    clock = StepClock(start=K_Q)
    with pytest.raises(CatalogIntegrityError):
        QualityReporter(
            h.adapter,
            h.storage,
            canonical_scratch_directory=h.canonical_scratch_directory,
            clock=clock,
        ).report("klines_1m", SYMBOL, DAY)
    assert clock.calls == 0 and h.rows(REPORTS) == []


def test_a_clock_before_what_it_describes_is_refused(h: RestHarness) -> None:
    _ingest(h, "klines_1m", ss.kline_items(1))
    with pytest.raises(QualityReportError, match="precedes"):
        QualityReporter(
            h.adapter,
            h.storage,
            canonical_scratch_directory=h.canonical_scratch_directory,
            clock=StepClock(start=K_A),
        ).report("klines_1m", SYMBOL, DAY)
    assert h.rows(REPORTS) == []


def test_a_clock_before_a_precedence_edge_it_relies_on_is_refused(h: RestHarness) -> None:
    """Review C-2: every revision is known by 12-08, the archive->REST edge only on 12-10."""
    _chain(h)
    clock = StepClock(start=utc(2023, 12, 8))
    with pytest.raises(QualityReportError, match="precedence edge"):
        QualityReporter(
            h.adapter,
            h.storage,
            canonical_scratch_directory=h.canonical_scratch_directory,
            clock=clock,
        ).report("agg_trades", SYMBOL, DAY)
    assert h.rows(REPORTS) == []
    out = QualityReporter(
        h.adapter,
        h.storage,
        canonical_scratch_directory=h.canonical_scratch_directory,
        clock=StepClock(start=K_E),
    ).report("agg_trades", SYMBOL, DAY)
    assert out.row["knowledge_time"] == K_E


def test_a_committed_report_before_what_it_describes_is_never_reused(h: RestHarness) -> None:
    """E1-R4: a report row committed with a knowledge_time before the edge (an older writer or a
    forger) is refused on the reuse path too, not only when a fresh report is written."""
    _chain(h)
    early = utc(2023, 12, 1)
    reporter = QualityReporter(
        h.adapter,
        h.storage,
        canonical_scratch_directory=h.canonical_scratch_directory,
        clock=StepClock(start=early),
    )
    bindings = reporter._pinned_heads(q._INPUT_TABLES["agg_trades"])
    report_id = q.quality_report_id(c.TRADES.table, SYMBOL, DAY, bindings)
    partition = reporter._survey("agg_trades", SYMBOL, DAY, bindings, report_id, verify=False)
    body = reporter._body("agg_trades", SYMBOL, DAY, bindings, report_id, partition)
    reporter._commit(report_id, reporter._row(body, early))
    clock = StepClock(start=K_Q)
    with pytest.raises(CatalogIntegrityError, match="knowledge_time before a revision"):
        QualityReporter(
            h.adapter,
            h.storage,
            canonical_scratch_directory=h.canonical_scratch_directory,
            clock=clock,
        ).report("agg_trades", SYMBOL, DAY)
    assert clock.calls == 0


def test_the_report_id_changes_with_any_rule_it_relies_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review H-3: a PIT (or required policy) change is a new report, not a stale one."""
    bindings = {c.TRADES.table: "1"}
    before = q.quality_report_id(c.TRADES.table, SYMBOL, DAY, bindings)
    changed = PIT_BINDING.model_copy(update={"policy_hash": "0" * 64})
    monkeypatch.setattr(q, "PIT_BINDING", changed)
    assert q.quality_report_id(c.TRADES.table, SYMBOL, DAY, bindings) != before


def test_bar_invariants_are_checked_without_thresholds() -> None:
    start = utc(2023, 11, 14)
    base = {
        "interval_start": start,
        "interval_end": start + timedelta(minutes=1),
        "observation_key": "k",
        "revision_id": "crev1-x",
        "open": Decimal("2"),
        "high": Decimal("3"),
        "low": Decimal("1"),
        "close": Decimal("2"),
        "volume": Decimal("5"),
        "quote_volume": Decimal("10"),
        "taker_buy_base_volume": Decimal("1"),
        "taker_buy_quote_volume": Decimal("2"),
    }
    lawful = q._bar_events("canonical.bars_1m", [base], start)
    assert [e["event_type"] for e in lawful].count("bar_1m_invariant_violation") == 0
    broken = dict(base, low=Decimal("4"), taker_buy_base_volume=Decimal("6"))
    [event] = [
        e
        for e in q._bar_events("canonical.bars_1m", [broken], start)
        if e["event_type"] == "bar_1m_invariant_violation"
    ]
    assert event["detail"] == ("low > high; low above open/close; taker buy base volume > volume")


def test_unknown_scopes_are_refused(h: RestHarness) -> None:
    reporter = QualityReporter(
        h.adapter,
        h.storage,
        canonical_scratch_directory=h.canonical_scratch_directory,
        clock=StepClock(start=K_Q),
    )
    with pytest.raises(QualityReportError, match="data_type"):
        reporter.report("trades", SYMBOL, DAY)
    with pytest.raises(QualityReportError, match="venue symbol"):
        reporter.report("klines_1m", "BTC-USDT", DAY)
    with pytest.raises(QualityReportError, match="nothing to report"):
        reporter.report("klines_1m", SYMBOL, DAY)

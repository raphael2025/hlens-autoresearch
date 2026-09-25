"""The listing-history quality report (F2 / F3; ADR-0023 §2, ADR-0029 §2 quality events)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest

from core.contracts.catalog import CommitRequest
from infrastructure.canonical import listing_rules as lr
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import DATA_QUALITY_REPORTS
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.quality import listing_report as q
from infrastructure.quality.listing_report import ListingQualityReporter
from infrastructure.quality.reporter import (
    QualityReportError,
    QualityReportMissing,
    evidence_gaps_of,
)
from tests.infrastructure.revision import exchange_info_support as xs
from tests.infrastructure.revision.exchange_info_support import (
    EXCHANGE_INFO,
    LISTINGS,
    T1,
    T2,
    TRADING,
    Harness,
)
from tests.infrastructure.revision.rest_store_support import StepClock

LATER = xs.KNOWLEDGE + timedelta(days=1)


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    with xs.harness(tmp_path) as opened:
        yield opened


def _reporter(
    h: Harness, adapter: Any = None, clock: StepClock | None = None
) -> ListingQualityReporter:
    return ListingQualityReporter(
        adapter or h.adapter,
        h.storage,
        market_data_base_url=xs.ORIGIN,
        clock=clock or StepClock(start=LATER),
    )


def _derive(h: Harness) -> None:
    deriver = h.deriver()
    try:
        deriver.derive()
    finally:
        deriver.close()


def test_the_report_binds_inputs_findings_and_every_listing_gap(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    h.observe("snap-2", {"BTCUSDT": "PRE_TRADING", "ETHUSDT": "TRADING"}, T2, server_time=2)
    _derive(h)
    clock = StepClock(start=LATER)

    out = _reporter(h, clock=clock).report()

    assert clock.calls == 1 and not out.reused
    [row] = _quality_rows(h)
    assert row == out.row
    assert (row["subject_table"], row["subject_snapshot_id"]) == (
        LISTINGS.table,
        h.head(LISTINGS.table),
    )
    assert row["quality_rule_hash"] == q.LISTING_QUALITY_RULE_HASH
    heads = {table: h.head(table) or "" for table in (LISTINGS.table, EXCHANGE_INFO.table)}
    assert out.report_id == q.listing_quality_report_id(heads)
    [inputs] = [e for e in row["events"] if e["event_type"] == "report_inputs"]
    assert json.loads(inputs["detail"]) == heads
    [unknown] = [e for e in row["events"] if e["event_type"] == lr.FINDING_STATUS_UNKNOWN]
    assert unknown["event_start"] == T2 and unknown["detail"].startswith("BTCUSDT: ")
    listings = h.rows(LISTINGS.table)
    gaps = evidence_gaps_of(h.adapter, out.report_id)
    assert [(g["table"], g["revision_id"]) for g in gaps] == sorted(
        (LISTINGS.table, r["revision_id"]) for r in listings
    )
    expected = {r["revision_id"]: r["availability_evidence_gap"] for r in listings}
    assert all(g["gap"] == expected[g["revision_id"]] for g in gaps)


def test_rerun_reuses_and_existing_only_never_writes(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    _derive(h)
    with pytest.raises(QualityReportMissing):
        _reporter(h).report(existing_only=True)
    assert _quality_rows(h) == []
    first = _reporter(h).report()
    clock = StepClock(start=LATER + timedelta(days=9))
    again = _reporter(h, clock=clock).report()
    assert again.reused and again.row == first.row and clock.calls == 0
    pinned = PinnedCatalogView(
        h.adapter,
        {
            LISTINGS.table: h.head(LISTINGS.table) or "",
            EXCHANGE_INFO.table: h.head(EXCHANGE_INFO.table) or "",
            DATA_QUALITY_REPORTS.table: h.head(DATA_QUALITY_REPORTS.table) or "",
        },
    )
    assert _reporter(h, pinned).report(existing_only=True).row == first.row
    # New listing knowledge = new inputs: the old report does not describe them.
    h.observe("snap-2", {"BTCUSDT": "HALT", "ETHUSDT": "TRADING"}, T2, server_time=2)
    _derive(h)
    with pytest.raises(QualityReportMissing):
        _reporter(h).report(existing_only=True)


def test_a_tampered_report_or_early_clock_is_refused(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    _derive(h)
    with pytest.raises(QualityReportError, match="precedes"):
        _reporter(h, clock=StepClock(start=T1)).report()
    out = _reporter(h).report()
    forged = dict(
        out.row, events=[e for e in out.row["events"] if e["event_type"] != "report_inputs"]
    )
    _forge(h, forged)  # a second row under the same id, bypassing every writer
    with pytest.raises(CatalogIntegrityError):
        _reporter(h).report()


def test_no_listing_snapshot_no_report(h: Harness) -> None:
    with pytest.raises(QualityReportError, match="no snapshot"):
        _reporter(h).report()


def _quality_rows(h: Harness) -> list[dict[str, Any]]:
    columns = tuple(field.name for field in DATA_QUALITY_REPORTS.arrow_schema)
    rows: list[dict[str, Any]] = h.adapter.scan_columns(
        DATA_QUALITY_REPORTS.table, columns=columns
    ).to_pylist()
    return rows


def _forge(h: Harness, row: dict[str, Any]) -> None:
    batch = pa.Table.from_pylist([row], schema=DATA_QUALITY_REPORTS.arrow_schema)
    h.adapter.commit_batch(
        CommitRequest(
            table=DATA_QUALITY_REPORTS.table,
            batch_id="forged-listing-report",
            batch_fingerprint=DATA_QUALITY_REPORTS.fingerprint_rule.fingerprint(batch),
            row_count=1,
            expected_parent_snapshot_id=h.head(DATA_QUALITY_REPORTS.table),
        ),
        batch,
    )

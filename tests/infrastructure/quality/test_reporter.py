"""E3 partition quality reports (roadmap #16; ADR-0023 §2, ADR-0028 §7)."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from pyiceberg.expressions import EqualTo

from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import DATA_QUALITY_REPORTS
from infrastructure.quality import reporter as q
from infrastructure.quality.reporter import QualityReporter, QualityReportError
from tests.infrastructure.canonical import canonical_support as c
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


def _events(row: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [event for event in row["events"] if event["event_type"] == kind]


def test_a_bar_partition_report_lists_gaps_inputs_and_evidence_gaps(h: RestHarness) -> None:
    items = ss.kline_items(2)  # 22:14 and 22:15 of the day
    _ingest(h, "klines_1m", items)
    clock = StepClock(start=K_Q)

    out = QualityReporter(h.adapter, h.storage, clock=clock).report("klines_1m", SYMBOL, DAY)

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
    assert sorted(g["revision_id"] for g in row["evidence_gaps"]) == sorted(
        b["revision_id"] for b in bars
    )
    assert all(g["gap"].startswith("inherited from ") for g in row["evidence_gaps"])


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

    out = QualityReporter(h.adapter, h.storage, clock=StepClock(start=K_Q)).report(
        "agg_trades", SYMBOL, DAY
    )

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
    out = QualityReporter(h.adapter, h.storage, clock=StepClock(start=K_Q)).report(
        "agg_trades", SYMBOL, DAY
    )
    jumps = [e["detail"] for e in _events(out.row, "agg_trade_id_discontinuity")]
    if absent is None:
        assert jumps == []
    else:
        assert jumps == [f"aggregate trade ids jump from 102 to {next_id} ({absent} id(s) absent)"]


def test_a_replay_reuses_the_report_and_new_data_makes_a_new_one(h: RestHarness) -> None:
    _ingest(h, "klines_1m", ss.kline_items(1))
    first = QualityReporter(h.adapter, h.storage, clock=StepClock(start=K_Q)).report(
        "klines_1m", SYMBOL, DAY
    )
    later = StepClock(start=K_Q + timedelta(days=3))
    again = QualityReporter(h.adapter, h.storage, clock=later).report("klines_1m", SYMBOL, DAY)
    assert again.reused and later.calls == 0 and again.row == first.row
    assert len(h.rows(REPORTS)) == 1
    [response] = c.ingest_rest(
        h, "klines_1m", ss.kline_items(1, first_ms=ss.T0 + 3 * ss.MINUTE_MS), knowledge=K_R,
        request_id="req-k2",
    )  # fmt: skip
    c.normalizer(h, clock=StepClock(start=N_R)).normalize_unit(c.REST_KLINES.table, response)
    newer = QualityReporter(h.adapter, h.storage, clock=later).report("klines_1m", SYMBOL, DAY)
    assert newer.report_id != first.report_id and len(h.rows(REPORTS)) == 2


def test_an_unprovable_partition_gets_no_report(h: RestHarness) -> None:
    _ingest(h, "klines_1m", ss.kline_items(1))
    [row] = [r for r in h.rows(c.BARS) if r["lineage_raw_table"] == c.REST_KLINES.table]
    h.delete_rows(c.BARS, EqualTo("revision_id", row["revision_id"]))  # type: ignore[call-arg, arg-type]
    h.forge_rows(
        c.BARS, [dict(row, knowledge_time=row["knowledge_time"] + timedelta(hours=1))], "x"
    )
    clock = StepClock(start=K_Q)
    with pytest.raises(CatalogIntegrityError):
        QualityReporter(h.adapter, h.storage, clock=clock).report("klines_1m", SYMBOL, DAY)
    assert clock.calls == 0 and h.rows(REPORTS) == []


def test_a_clock_before_what_it_describes_is_refused(h: RestHarness) -> None:
    _ingest(h, "klines_1m", ss.kline_items(1))
    with pytest.raises(QualityReportError, match="precedes"):
        QualityReporter(h.adapter, h.storage, clock=StepClock(start=K_A)).report(
            "klines_1m", SYMBOL, DAY
        )
    assert h.rows(REPORTS) == []


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
    reporter = QualityReporter(h.adapter, h.storage, clock=StepClock(start=K_Q))
    with pytest.raises(QualityReportError, match="data_type"):
        reporter.report("trades", SYMBOL, DAY)
    with pytest.raises(QualityReportError, match="venue symbol"):
        reporter.report("klines_1m", "BTC-USDT", DAY)
    with pytest.raises(QualityReportError, match="nothing to report"):
        reporter.report("klines_1m", SYMBOL, DAY)

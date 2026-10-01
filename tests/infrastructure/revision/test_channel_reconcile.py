"""D3E cross-channel reconciler and graph guard (ADR-0027 §4 / §9 / §11; acceptance #6 ~ #8,
#15 crash point 3 (r1), #21).

Archive rows come from the real D2 store over real ZIP bytes; REST rows from the real D3D
collector plus the D3E store. The reconciler under test is then the only writer of the evidence
table. The four ``knowledge_cutoff`` segments are evaluated with the real contracts
(``RevisionGraph``, ``PointInTimeSelection``) and the accepted maximal-head helper; the expected
selection in each segment is written down by hand from ADR-0027 §4.7.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from pyiceberg.expressions import EqualTo

from core.contracts.catalog import CommitRequest
from core.contracts.revision import (
    PointInTimeSelection,
    PointInTimeStatus,
    PrecedenceEvidence,
    RevisionGraph,
    RevisionRecord,
)
from core.domain.base import CONTRACT_SCHEMA_VERSION, canonical_json
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_AGG_TRADES,
    BINANCE_SPOT_ARCHIVES,
    BINANCE_SPOT_KLINES_1M,
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_KLINES_1M,
    BINANCE_SPOT_REST_RESPONSES,
)
from infrastructure.parser.binance_archive import AGG_TRADES_ROW_SCHEMA, time_unit_for
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision import channel_reconcile, rest_identity
from infrastructure.revision import identity as archive_identity
from infrastructure.revision.availability import AVAILABILITY_BINDING, AvailabilitySubject
from infrastructure.revision.channel_precedence import (
    AGG_TRADE_PROJECTION,
    DELIVERY_CHANNEL_BINDING,
    DELIVERY_CHANNEL_HASH,
    KLINE_1M_PROJECTION,
    POLICY_STATEMENT,
    Channel,
    ChannelComparison,
    ChannelEdge,
    ComparisonOutcome,
    compare_channels,
)
from infrastructure.revision.channel_reconcile import (
    FINDING_CHANNEL_INCOMPARABLE,
    FINDING_CHANNEL_MISMATCH,
    ArrivalSeqRangeViolation,
    ChannelReconcileConflict,
    ChannelReconciled,
    ChannelReconcileError,
    ChannelReconciler,
    assemble_channel_graph,
    check_arrival_seq,
    evidence_from_row,
    revision_record_from_row,
)
from infrastructure.revision.precedence import maximal_heads
from infrastructure.revision.rest_availability import RestAvailabilitySubject
from infrastructure.revision.row_integrity import (
    ELEMENT_NATIVE_COLUMNS,
    element_batch_id,
    element_columns,
)
from infrastructure.revision.store import _row_batch, _row_batch_id, _row_records, _times_from_row
from infrastructure.streaming.runs import RunLimits
from tests.infrastructure.catalog.phase1_support import rest_record
from tests.infrastructure.collector import rest_support as cs
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import (
    DAY,
    DAY_2025,
    MINUTE_MS,
    SYMBOL,
    T0,
    T0_2025,
    Crash,
    ProxyCatalog,
    RestHarness,
    StepClock,
    utc,
)

ARCHIVE_AGGS = BINANCE_SPOT_AGG_TRADES
ARCHIVE_KLINES = BINANCE_SPOT_KLINES_1M
REST_AGGS = BINANCE_SPOT_REST_AGG_TRADES
REST_KLINES = BINANCE_SPOT_REST_KLINES_1M
EVIDENCE = BINANCE_SPOT_PRECEDENCE_EVIDENCE
BASE = 1 << 62
STRIDE = 1 << 32
EARLY = utc(2023, 12, 1)
LATE = utc(2023, 12, 5)
K_EDGE = utc(2023, 12, 10)
ARCHIVE_RETRIEVED = utc(2023, 11, 16)
TICK = timedelta(microseconds=1)
FAR = utc(2030, 1, 1)


@pytest.fixture
def h(tmp_path: Path) -> Iterator[RestHarness]:
    with ss.sqlite_harness(tmp_path) as opened:
        yield opened


# =========================================================================================
# builders
# =========================================================================================


def _rest(
    h: RestHarness,
    data_type: str,
    items: list[Any],
    *,
    knowledge: datetime,
    request_id: str = "req-rest",
    t0: int = T0,
    retrieved_ms: int = cs.RETRIEVED_AT_MS,
) -> None:
    if data_type == "agg_trades":
        cs.queue_agg_chain(h.venue, SYMBOL, t0, [items])
        request = ss.agg_request(request_id, start_ms=t0)
    else:
        cs.queue_kline_chain(h.venue, SYMBOL, t0, [items], retrieved_at_ms=retrieved_ms)
        request = ss.kline_request(request_id, start_ms=t0)
    collected = h.collect(request, start_ms=retrieved_ms)  # a side effect: never inside assert
    assert not isinstance(collected, Exception)
    h.store(clock=StepClock(start=knowledge)).ingest_collection(request)


def _archive(
    h: RestHarness,
    data_type: str,
    lines: list[str],
    *,
    knowledge: datetime,
    day: Any = DAY,
    retrieved_at: datetime = ARCHIVE_RETRIEVED,
    request_id: str = "archive-1",
) -> None:
    outcome = h.ingest_archive(
        data_type,
        lines,
        day=day,
        clock=StepClock(start=knowledge),
        retrieved_at=retrieved_at,
        request_id=request_id,
    )
    assert type(outcome).__name__ == "ArchiveIngested", outcome


def _both(h: RestHarness, order: str, items: list[dict[str, Any]]) -> tuple[datetime, datetime]:
    """Ingest the same aggTrades on both channels in ``order``; reconcile after each ingest."""
    reconciler = h.reconciler(clock=StepClock(start=K_EDGE))
    if order == "archive_first":
        k_archive, k_rest = EARLY, LATE
        _archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=k_archive)
        assert reconciler.reconcile("agg_trades", SYMBOL, DAY).edges == ()
        _rest(h, "agg_trades", items, knowledge=k_rest)
    else:
        k_rest, k_archive = EARLY, LATE
        _rest(h, "agg_trades", items, knowledge=k_rest)
        assert reconciler.reconcile("agg_trades", SYMBOL, DAY).edges == ()
        _archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=k_archive)
    return k_archive, k_rest


def _records(h: RestHarness, key: str) -> tuple[list[RevisionRecord], list[RevisionRecord]]:
    archive = [row for row in h.rows(ARCHIVE_AGGS) if row["observation_key"] == key]
    rest = [row for row in h.rows(REST_AGGS) if row["observation_key"] == key]
    return (
        [revision_record_from_row(row) for row in archive],
        [revision_record_from_row(row) for row in rest],
    )


def _evidence(h: RestHarness, key: str) -> list[PrecedenceEvidence]:
    return [evidence_from_row(row) for row in h.rows(EVIDENCE) if row["observation_key"] == key]


def _select(
    records: list[RevisionRecord],
    evidence: list[PrecedenceEvidence],
    key: str,
    cutoff: datetime,
) -> PointInTimeSelection:
    """PIT at ``knowledge_cutoff``: only revisions and edges known by then, real contracts."""
    visible = [item for item in records if item.availability.times.knowledge_time <= cutoff]
    edges = [item for item in evidence if item.knowledge_time <= cutoff]
    RevisionGraph(revisions=tuple(visible), precedence_evidence=tuple(edges))
    heads = maximal_heads(visible, edges) if visible else ()
    if not heads:
        status, selected = PointInTimeStatus.ABSENT, None
    elif len(heads) == 1:
        status, selected = PointInTimeStatus.SELECTED, heads[0]
    else:
        status, selected = PointInTimeStatus.CONFLICT, None
    return PointInTimeSelection(
        observation_key=key,
        simulation_time=FAR,
        knowledge_cutoff=cutoff,
        status=status,
        selected_revision_id=selected,
        maximal_heads=heads,
    )


def _pit_table(h: RestHarness, key: str, k_edge: datetime) -> dict[str, Any]:
    archive, rest = _records(h, key)
    [a], [r] = archive, rest
    evidence = _evidence(h, key)
    graph = assemble_channel_graph(archive, rest, evidence)  # the guard accepts the graph
    records = list(graph.revisions)
    k_a, k_r = a.availability.times.knowledge_time, r.availability.times.knowledge_time
    first, last = sorted((k_a, k_r))
    early_id = a.revision_id if k_a < k_r else r.revision_id
    table: dict[str, Any] = {}
    for label, cutoff in (
        ("before both", first - TICK),
        ("first known", first),
        ("just before the second", last - TICK),
        ("both known", last),
        ("just before the edge", k_edge - TICK),
        ("edge known", k_edge),
    ):
        table[label] = _select(records, evidence, key, cutoff)
    return {
        "table": table,
        "archive": a.revision_id,
        "rest": r.revision_id,
        "early": early_id,
    }


# =========================================================================================
# #6: both arrival orders × four knowledge_cutoff segments
# =========================================================================================


@pytest.mark.parametrize("order", ["archive_first", "rest_first"])
def test_both_arrival_orders_give_the_adr_four_segments(h: RestHarness, order: str) -> None:
    items = ss.agg_items(3)
    _both(h, order, items)
    before = {table.table: h.rows(table) for table in (ARCHIVE_AGGS, REST_AGGS)}
    heads = {table: h.head(table.table) for table in (ARCHIVE_AGGS, REST_AGGS)}

    out = h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)

    assert len(out.edges) == 3 and out.findings == ()
    assert all(not item.reused for item in out.edges)
    # Nothing but the evidence table was written; no revision row changed.
    assert {table.table: h.rows(table) for table in (ARCHIVE_AGGS, REST_AGGS)} == before
    assert {table: h.head(table.table) for table in (ARCHIVE_AGGS, REST_AGGS)} == heads
    for item in items:
        key = f"binance:spot:agg_trade:{SYMBOL}:{item['a']}"
        pit = _pit_table(h, key, K_EDGE)
        table = pit["table"]
        assert table["before both"].status is PointInTimeStatus.ABSENT
        assert table["first known"].selected_revision_id == pit["early"]
        assert table["just before the second"].selected_revision_id == pit["early"]
        for label in ("both known", "just before the edge"):
            assert table[label].status is PointInTimeStatus.CONFLICT
            assert set(table[label].maximal_heads) == {pit["archive"], pit["rest"]}
        assert table["edge known"].status is PointInTimeStatus.SELECTED
        assert table["edge known"].selected_revision_id == pit["archive"]


def test_both_arrival_orders_converge_to_the_same_edge_set(tmp_path: Path) -> None:
    edge_sets = []
    for order in ("archive_first", "rest_first"):
        with ss.sqlite_harness(tmp_path / order) as h:
            _both(h, order, ss.agg_items(3))
            out = h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)
            edge_sets.append(
                sorted(
                    (row["edge_id"], row["revision_id"], row["superseded_revision_id"])
                    for row in h.rows(EVIDENCE)
                )
            )
            assert len(out.edge_ids) == 3
    assert edge_sets[0] == edge_sets[1]


def test_a_rerun_reuses_the_first_edge_and_never_rewrites_old_cutoffs(h: RestHarness) -> None:
    _both(h, "archive_first", ss.agg_items(3))
    first = h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)
    key = f"binance:spot:agg_trade:{SYMBOL}:100"
    before = _pit_table(h, key, K_EDGE)["table"]
    evidence_head = h.head(EVIDENCE.table)
    later = StepClock(start=K_EDGE + timedelta(days=30))

    again = h.reconciler(clock=later).reconcile("agg_trades", SYMBOL, DAY)

    assert later.calls == 0  # nothing to stamp: every edge is reused
    assert again.commits == () and h.head(EVIDENCE.table) == evidence_head
    assert again.edge_ids == first.edge_ids and all(item.reused for item in again.edges)
    assert {row["knowledge_time"] for row in h.rows(EVIDENCE)} == {K_EDGE}
    assert len(h.rows(EVIDENCE)) == 3
    assert _pit_table(h, key, K_EDGE)["table"] == before


# =========================================================================================
# #12: the exact evidence row
# =========================================================================================


def test_the_edge_row_is_exact_and_points_archive_to_rest(h: RestHarness) -> None:
    items = ss.agg_items(1)
    _both(h, "archive_first", items)
    archive_snapshot = h.head(ARCHIVE_AGGS.table)
    rest_snapshot = h.head(REST_AGGS.table)
    assert archive_snapshot is not None and rest_snapshot is not None

    out = h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)

    [row] = h.rows(EVIDENCE)
    [archive] = h.rows(ARCHIVE_AGGS)
    [rest] = h.rows(REST_AGGS)
    item = items[0]
    projection = {
        "kind": AGG_TRADE_PROJECTION,
        "symbol": SYMBOL,
        "agg_trade_id": item["a"],
        "price": "92792.050000000000000000",
        "quantity": "0.001500000000000000",
        "first_trade_id": item["f"],
        "last_trade_id": item["l"],
        "event_time_us": item["T"] * 1000,
        "is_buyer_maker": item["m"],
        "is_best_match": item["M"],
    }
    digest = hashlib.sha256(canonical_json(projection).encode("utf-8")).hexdigest()
    key = f"binance:spot:agg_trade:{SYMBOL}:{item['a']}"
    assert row == {
        "edge_id": rest_identity.edge_id(
            out.edges[0].edge.evidence.policy, key, archive["revision_id"], rest["revision_id"]
        ),
        "observation_key": key,
        "revision_id": archive["revision_id"],
        "revision_table": "raw.binance_spot_agg_trades",
        "superseded_revision_id": rest["revision_id"],
        "superseded_table": "raw.binance_spot_rest_agg_trades",
        "policy_id": "binance.spot.delivery-channel",
        "policy_version": "1.0.0",
        "policy_hash": DELIVERY_CHANNEL_HASH,
        "evidence": [
            POLICY_STATEMENT,
            f"projection={AGG_TRADE_PROJECTION}",
            f"projection_sha256={digest}",
            f"archive_payload_hash={archive['payload_hash']}",
            f"rest_payload_hash={rest['payload_hash']}",
            f"archive_revision_table=raw.binance_spot_agg_trades@snapshot:{archive_snapshot}",
            f"rest_revision_table=raw.binance_spot_rest_agg_trades@snapshot:{rest_snapshot}",
        ],
        "knowledge_time": K_EDGE,
        "revision_snapshot_id": archive_snapshot,
        "superseded_snapshot_id": rest_snapshot,
        "projection_sha256": digest,
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,  # a new edge (ADR-0052 V2)
    }
    assert row["knowledge_time"] >= max(archive["knowledge_time"], rest["knowledge_time"])
    assert "not a source-declared revision order" in POLICY_STATEMENT
    assert archive["supersedes"] == rest["supersedes"] == []
    assert archive["precedence_evidence"] == rest["precedence_evidence"] == []
    assert out.rest_snapshot_id == rest_snapshot and out.archive_snapshot_id == archive_snapshot
    # The pinned snapshots really hold both revisions (time travel through PyIceberg).
    assert [r["revision_id"] for r in h.rows_at(REST_AGGS.table, rest_snapshot)] == [
        rest["revision_id"]
    ]
    assert [r["revision_id"] for r in h.rows_at(ARCHIVE_AGGS.table, archive_snapshot)] == [
        archive["revision_id"]
    ]


# =========================================================================================
# #7 / #11: no edge — mismatch, incomparable, no counterpart, microseconds, kline equivalence
# =========================================================================================


def test_a_mismatch_yields_a_finding_and_no_edge(h: RestHarness) -> None:
    items = ss.agg_items(3)
    archive_items = [dict(item) for item in items]
    archive_items[1]["p"] = "92792.04000000"
    _archive(h, "agg_trades", ss.archive_agg_lines(archive_items), knowledge=EARLY)
    _rest(h, "agg_trades", items, knowledge=LATE)

    out = h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)

    [finding] = out.findings
    key = f"binance:spot:agg_trade:{SYMBOL}:101"
    assert finding.code == FINDING_CHANNEL_MISMATCH and finding.observation_key == key
    assert finding.reasons == ("differs: price",)
    assert len(out.edges) == 2 and key not in {row["observation_key"] for row in h.rows(EVIDENCE)}
    pit = _select(*_flat(h, key), key, FAR)
    assert pit.status is PointInTimeStatus.CONFLICT  # competing heads stay: fail closed


def _flat(h: RestHarness, key: str) -> tuple[list[RevisionRecord], list[PrecedenceEvidence]]:
    archive, rest = _records(h, key)
    return [*archive, *rest], _evidence(h, key)


def test_rest_only_and_archive_only_record_nothing(h: RestHarness) -> None:
    _rest(h, "agg_trades", ss.agg_items(2), knowledge=EARLY)
    out = h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)
    assert (out.edges, out.findings, out.commits) == ((), (), ())
    assert h.head(EVIDENCE.table) is None
    _archive(h, "agg_trades", ss.archive_agg_lines(ss.agg_items(2, first_id=500)), knowledge=LATE)
    out = h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)
    assert (out.edges, out.findings, out.commits) == ((), (), ())
    assert h.rows(EVIDENCE) == []


def test_sub_millisecond_archive_trades_never_equal_millisecond_rest(h: RestHarness) -> None:
    rest_items = ss.agg_items(2, first_ms=T0_2025)
    lines = ss.archive_agg_lines(rest_items, factor=1000)
    lines[0] = lines[0].replace(
        f",{rest_items[0]['T'] * 1000},", f",{rest_items[0]['T'] * 1000 + 123},"
    )
    _archive(
        h,
        "agg_trades",
        lines,
        knowledge=utc(2025, 1, 5),
        day=DAY_2025,
        retrieved_at=utc(2025, 1, 3),
    )
    _rest(
        h,
        "agg_trades",
        rest_items,
        knowledge=utc(2025, 1, 20),
        t0=T0_2025,
        retrieved_ms=T0_2025 + 10_000 * MINUTE_MS,
    )

    out = h.reconciler(clock=StepClock(start=utc(2025, 2, 1))).reconcile(
        "agg_trades", SYMBOL, DAY_2025
    )

    assert [(item.code, item.reasons) for item in out.findings] == [
        (FINDING_CHANNEL_MISMATCH, ("differs: event_time_us",))
    ]
    assert len(out.edges) == 1  # the whole-millisecond trade is equal across units


def test_microsecond_archive_klines_equal_millisecond_rest_klines(h: RestHarness) -> None:
    items = ss.kline_items(3, first_ms=T0_2025)
    _archive(
        h,
        "klines_1m",
        ss.archive_kline_lines(items, factor=1000),
        knowledge=utc(2025, 1, 5),
        day=DAY_2025,
        retrieved_at=utc(2025, 1, 3),
    )
    _rest(
        h,
        "klines_1m",
        items,
        knowledge=utc(2025, 1, 20),
        t0=T0_2025,
        retrieved_ms=T0_2025 + 10_000 * MINUTE_MS,
    )

    out = h.reconciler(clock=StepClock(start=utc(2025, 2, 1))).reconcile(
        "klines_1m", SYMBOL, DAY_2025
    )

    assert len(out.edges) == 3 and out.findings == ()
    for edge in out.edges:
        assert f"projection={KLINE_1M_PROJECTION}" in edge.edge.evidence.evidence
        assert edge.edge.revision_table == "raw.binance_spot_klines_1m"
        assert edge.edge.superseded_table == "raw.binance_spot_rest_klines_1m"
    units = {row["open_time_raw"] for row in h.rows(ARCHIVE_KLINES)}
    assert units == {item[0] * 1000 for item in items}  # really microsecond rows


def _competing_rest_klines(h: RestHarness) -> tuple[dict[str, Any], dict[str, Any]]:
    """An archive kline, the equal REST kline and a lawful competing REST kline of that minute.

    Both REST revisions come from the real collector and store: the same page identity is
    answered twice with different bytes (volume), so the second is a competing revision.
    """
    items = ss.kline_items(1)
    changed = [list(items[0])]
    changed[0][5] = "7.50000000"
    _archive(h, "klines_1m", ss.archive_kline_lines(items), knowledge=EARLY)
    cs.queue_kline_chain(h.venue, SYMBOL, T0, [items])
    cs.queue_kline_chain(h.venue, SYMBOL, T0, [changed])
    request_a, request_b = ss.kline_request("req-rest-a"), ss.kline_request("req-rest-b")
    first = h.collect(request_a)
    second = h.collect(request_b, start_ms=cs.RETRIEVED_AT_MS + 60_000)
    assert not isinstance(first, Exception) and not isinstance(second, Exception)
    h.store(clock=StepClock(start=LATE)).ingest_collection(request_a)
    h.store(clock=StepClock(start=LATE + timedelta(hours=1))).ingest_collection(request_b)
    [real] = [row for row in h.rows(REST_KLINES) if row["volume"] != Decimal("7.5")]
    [odd] = [row for row in h.rows(REST_KLINES) if row["volume"] == Decimal("7.5")]
    return real, odd


def test_an_incomparable_outcome_yields_a_finding_next_to_an_equal_one(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconciler's handling of ``INCOMPARABLE``. Both frozen parsers only store in-domain
    values, so a *lawful* pair never projects as incomparable (the pure policy tests cover that
    branch); here the accepted policy's verdict for the competing pair is replaced by an
    ``INCOMPARABLE`` one, and everything else — rows, verification, the equal edge — is real."""
    real, odd = _competing_rest_klines(h)
    genuine = compare_channels

    def policy(archive: Any, rest: Any) -> ChannelComparison:
        result = genuine(archive, rest)
        if rest.revision_id != odd["revision_id"]:
            return result
        assert result.outcome is ComparisonOutcome.MISMATCH  # the real verdict
        return dataclasses.replace(
            result,
            outcome=ComparisonOutcome.INCOMPARABLE,
            reasons=("rest: ignore_raw is empty",),
        )

    monkeypatch.setattr(channel_reconcile, "compare_channels", policy)
    out = h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("klines_1m", SYMBOL, DAY)

    [edge] = out.edges
    assert edge.edge.evidence.superseded_revision_id == real["revision_id"]
    [finding] = out.findings
    assert finding.code == FINDING_CHANNEL_INCOMPARABLE
    assert finding.rest_revision_id == odd["revision_id"]
    assert finding.reasons == ("rest: ignore_raw is empty",)
    key = real["observation_key"]
    archive = [revision_record_from_row(row) for row in h.rows(ARCHIVE_KLINES)]
    rest = [revision_record_from_row(row) for row in h.rows(REST_KLINES)]
    evidence = [evidence_from_row(row) for row in h.rows(EVIDENCE)]
    pit = _select([*archive, *rest], evidence, key, FAR)
    # The archive supersedes the equal REST revision, the incomparable one still competes.
    assert pit.status is PointInTimeStatus.CONFLICT
    assert set(pit.maximal_heads) == {archive[0].revision_id, odd["revision_id"]}


def test_a_lawful_competing_rest_revision_is_a_mismatch_finding(h: RestHarness) -> None:
    real, odd = _competing_rest_klines(h)
    out = h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("klines_1m", SYMBOL, DAY)
    [edge] = out.edges
    assert edge.edge.evidence.superseded_revision_id == real["revision_id"]
    [finding] = out.findings
    assert (finding.code, finding.rest_revision_id) == (
        FINDING_CHANNEL_MISMATCH,
        odd["revision_id"],
    )
    assert finding.reasons == ("differs: volume",)


def test_a_forged_incomparable_rest_revision_is_refused_before_comparison(
    h: RestHarness,
) -> None:
    """A row no pipeline can produce (empty ``ignore``) is an integrity error, not a finding."""
    items = ss.kline_items(1)
    _archive(h, "klines_1m", ss.archive_kline_lines(items), knowledge=EARLY)
    _rest(h, "klines_1m", items, knowledge=LATE)
    [real] = h.rows(REST_KLINES)
    odd = dict(real)
    odd["ignore_raw"] = ""
    odd["payload_hash"] = rest_identity.kline_1m_payload_hash(SYMBOL, odd)
    odd["revision_id"] = rest_identity.revision_id(
        odd["observation_key"], odd["source_id"], odd["payload_hash"]
    )
    odd["arrival_seq"] = BASE + 7 * STRIDE + 1
    h.forge_rows(REST_KLINES, [odd], "second-rest-revision")
    clock = StepClock(start=K_EDGE)

    with pytest.raises(CatalogIntegrityError, match=r"\['arrival_seq'\]"):
        h.reconciler(clock=clock).reconcile("klines_1m", SYMBOL, DAY)

    assert h.rows(EVIDENCE) == [] and clock.calls == 0


# =========================================================================================
# #7 / #8: integrity — no partial edge
# =========================================================================================


def test_a_stored_payload_that_does_not_re_derive_aborts_with_zero_edges(h: RestHarness) -> None:
    items = ss.agg_items(3)
    _archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=EARLY)
    _rest(h, "agg_trades", items, knowledge=LATE)
    row = sorted(h.rows(REST_AGGS), key=lambda item: item["element_index"])[2]
    forged = dict(row)
    forged["payload_hash"] = "0" * 64
    forged["revision_id"] = rest_identity.revision_id(
        forged["observation_key"], forged["source_id"], forged["payload_hash"]
    )
    forged["arrival_seq"] = BASE + 9 * STRIDE + 3
    h.forge_rows(REST_AGGS, [forged], "forged-payload")
    clock = StepClock(start=K_EDGE)

    with pytest.raises(
        CatalogIntegrityError, match="committed payload_hash of .* does not re-derive"
    ):
        h.reconciler(clock=clock).reconcile("agg_trades", SYMBOL, DAY)

    assert h.rows(EVIDENCE) == [] and clock.calls == 0


def test_an_archive_row_without_its_archive_revision_aborts(h: RestHarness) -> None:
    items = ss.agg_items(1)
    _archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=EARLY)
    _rest(h, "agg_trades", items, knowledge=LATE)
    [row] = h.rows(ARCHIVE_AGGS)
    orphan = dict(row)
    orphan["archive_revision_id"] = "rev1-" + "9" * 64
    orphan["source_id"] = archive_identity.row_source_identity(orphan["archive_revision_id"])
    orphan["revision_id"] = archive_identity.revision_id(
        orphan["observation_key"], orphan["source_id"], orphan["payload_hash"]
    )
    orphan["arrival_seq"] = 12345
    h.forge_rows(ARCHIVE_AGGS, [orphan], "orphan-archive-row")
    with pytest.raises(CatalogIntegrityError, match="not committed for this data type"):
        h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)
    assert h.rows(EVIDENCE) == []


def _one_equal_pair(h: RestHarness) -> tuple[dict[str, Any], dict[str, Any]]:
    items = ss.agg_items(1)
    _archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=EARLY)
    _rest(h, "agg_trades", items, knowledge=LATE)
    [archive] = h.rows(ARCHIVE_AGGS)
    [rest] = h.rows(REST_AGGS)
    return archive, rest


def _edge_row(h: RestHarness) -> dict[str, Any]:
    """The lawful edge row of the only pair, produced in an independent catalog."""
    with ss.sqlite_harness(h.tmp_path / "reference") as ref:
        _one_equal_pair(ref)
        ref.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)
        [row] = ref.rows(EVIDENCE)
    return row


def test_a_duplicated_edge_row_fails_closed(h: RestHarness) -> None:
    _one_equal_pair(h)
    h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)
    [row] = h.rows(EVIDENCE)
    h.forge_rows(EVIDENCE, [dict(row)], "duplicate-edge")
    with pytest.raises(CatalogIntegrityError, match="committed twice"):
        h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)


@pytest.mark.parametrize(
    ("column", "value", "match"),
    [
        ("projection_sha256", "0" * 64, "disagrees"),
        ("evidence", ["canonical market content equal"], "disagrees"),
        ("revision_snapshot_id", "12345", "pins snapshot"),
        ("superseded_table", "raw.binance_spot_rest_klines_1m", "names"),
        ("knowledge_time", utc(2023, 12, 2), "not lawful"),  # before the REST revision
        ("policy_hash", "0" * 64, "does not describe"),
    ],
)
def test_a_drifted_edge_row_fails_closed(
    h: RestHarness, column: str, value: Any, match: str
) -> None:
    _one_equal_pair(h)
    forged = _edge_row(h)
    # The reference catalog has other snapshot ids: pin this catalog's real ones first.
    forged["revision_snapshot_id"] = h.head(ARCHIVE_AGGS.table)
    forged["superseded_snapshot_id"] = h.head(REST_AGGS.table)
    forged["evidence"] = [
        item.replace(item.split("@snapshot:")[1], forged["revision_snapshot_id"])
        if item.startswith("archive_revision_table=")
        else item.replace(item.split("@snapshot:")[1], forged["superseded_snapshot_id"])
        if item.startswith("rest_revision_table=")
        else item
        for item in forged["evidence"]
    ]
    forged[column] = value
    if column == "policy_hash":
        forged["edge_id"] = "edge1-" + "7" * 64
    h.forge_rows(EVIDENCE, [forged], "drifted-edge")
    with pytest.raises(CatalogIntegrityError, match=match):
        h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)


def test_an_edge_between_unequal_revisions_fails_closed(h: RestHarness) -> None:
    items = ss.agg_items(1)
    archive_items = [dict(items[0], p="92792.04000000")]
    with ss.sqlite_harness(h.tmp_path / "equal") as ref:
        _one_equal_pair(ref)
        ref.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)
        [edge] = ref.rows(EVIDENCE)
    _archive(h, "agg_trades", ss.archive_agg_lines(archive_items), knowledge=EARLY)
    _rest(h, "agg_trades", items, knowledge=LATE)
    [archive] = h.rows(ARCHIVE_AGGS)
    forged = dict(edge)
    forged["revision_id"] = archive["revision_id"]
    forged["edge_id"] = rest_identity.edge_id(
        evidence_from_row(edge).policy,
        edge["observation_key"],
        archive["revision_id"],
        edge["superseded_revision_id"],
    )
    h.forge_rows(EVIDENCE, [forged], "unequal-edge")
    with pytest.raises(CatalogIntegrityError, match="does not describe an equal"):
        h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)


# =========================================================================================
# the edge clock
# =========================================================================================


def test_an_edge_clock_before_a_revision_is_refused(h: RestHarness) -> None:
    _one_equal_pair(h)
    with pytest.raises(ChannelReconcileConflict, match="backfill"):
        h.reconciler(clock=StepClock(start=LATE - TICK)).reconcile("agg_trades", SYMBOL, DAY)
    with pytest.raises(ChannelReconcileError, match="timezone-aware"):
        h.reconciler(clock=lambda: datetime(2024, 1, 1)).reconcile("agg_trades", SYMBOL, DAY)
    assert h.rows(EVIDENCE) == []


def test_the_clock_is_read_once_after_every_comparison(h: RestHarness) -> None:
    _both(h, "rest_first", ss.agg_items(3))
    clock = StepClock(start=K_EDGE)
    h.reconciler(clock=clock).reconcile("agg_trades", SYMBOL, DAY)
    assert clock.calls == 1
    assert {row["knowledge_time"] for row in h.rows(EVIDENCE)} == {K_EDGE}


# =========================================================================================
# #15 (r1) / #8: crash and race recovery of the evidence table
# =========================================================================================


def test_a_crash_between_edge_batches_resumes_without_a_second_time(h: RestHarness) -> None:
    _both(h, "archive_first", ss.agg_items(3))
    proxy = ProxyCatalog(h.adapter, after=ss.crash_after_commits(1, table=EVIDENCE.table))
    with pytest.raises(Crash):
        h.reconciler(clock=StepClock(start=K_EDGE), adapter=proxy, edge_microbatch_rows=1)\
            .reconcile("agg_trades", SYMBOL, DAY)  # fmt: skip
    [survivor] = h.rows(EVIDENCE)
    later = K_EDGE + timedelta(hours=2)

    out = h.reconciler(clock=StepClock(start=later), edge_microbatch_rows=1).reconcile(
        "agg_trades", SYMBOL, DAY
    )

    assert [item.reused for item in out.edges].count(True) == 1
    rows = h.rows(EVIDENCE)
    assert len(rows) == len({row["edge_id"] for row in rows}) == 3
    times = {row["edge_id"]: row["knowledge_time"] for row in rows}
    assert times[survivor["edge_id"]] == K_EDGE  # the first commit stays authoritative
    assert sorted(times.values()) == [K_EDGE, later, later]
    assert len(out.commits) == 2


def test_a_concurrent_reconcile_wins_and_its_time_is_reused(h: RestHarness) -> None:
    _both(h, "archive_first", ss.agg_items(3))
    rival = h.reconciler(clock=StepClock(start=K_EDGE + timedelta(minutes=1)))
    fired: list[bool] = []

    def interleave(commit: CommitRequest) -> None:
        if commit.table == EVIDENCE.table and not fired:
            fired.append(True)
            rival.reconcile("agg_trades", SYMBOL, DAY)

    proxy = ProxyCatalog(h.adapter, before=interleave)
    out = h.reconciler(clock=StepClock(start=K_EDGE), adapter=proxy).reconcile(
        "agg_trades", SYMBOL, DAY
    )

    assert fired and all(item.reused for item in out.edges) and out.commits == ()
    rows = h.rows(EVIDENCE)
    assert len(rows) == 3 and {row["knowledge_time"] for row in rows} == {
        K_EDGE + timedelta(minutes=1)
    }


def test_a_reconcile_outcome_reports_the_pinned_snapshots(h: RestHarness) -> None:
    _both(h, "archive_first", ss.agg_items(2))
    out: ChannelReconciled = h.reconciler(clock=StepClock(start=K_EDGE)).reconcile(
        "agg_trades", SYMBOL, DAY
    )
    assert out.evidence_snapshot_id == h.head(EVIDENCE.table) == out.commits[-1].snapshot_id
    assert out.new_edge_ids == out.edge_ids


# =========================================================================================
# #21: the cross-channel arrival guard
# =========================================================================================


@pytest.mark.parametrize(
    ("channel", "value", "ok"),
    [
        (Channel.ARCHIVE, 0, True),
        (Channel.ARCHIVE, BASE - 1, True),
        (Channel.ARCHIVE, BASE, False),
        (Channel.ARCHIVE, -1, False),
        (Channel.REST, BASE, True),
        (Channel.REST, (1 << 63) - 1, True),
        (Channel.REST, 1 << 63, False),
        (Channel.REST, BASE - 1, False),
        (Channel.REST, True, False),
        (Channel.ARCHIVE, False, False),
        (Channel.REST, float(BASE), False),
        (Channel.REST, str(BASE), False),
    ],
)
def test_arrival_numbers_must_sit_in_their_channel_interval(
    channel: Channel, value: Any, ok: bool
) -> None:
    if ok:
        assert check_arrival_seq(channel, value) == value
    else:
        with pytest.raises(ArrivalSeqRangeViolation):
            check_arrival_seq(channel, value)


def _record(
    source: str, arrival: int, payload: str, key: str = "binance:spot:agg_trade:X:1"
) -> RevisionRecord:
    """A contract record of either channel (the guard only reads source, id and arrival)."""
    record = rest_record(
        key,
        payload,
        RestAvailabilitySubject.AGG_TRADE,
        event_time=utc(2024, 1, 1),
        event_end_time=None,
        ingest_time=utc(2024, 1, 2),
        arrival_seq=arrival,
    )
    if source == "rest":
        return record
    archive_source = "binance.public.spot.archive@1.0.0:rev1-" + "a" * 64
    return record.model_copy(
        update={
            "source_id": archive_source,
            "revision_id": f"archive-{payload[:8]}",
        }
    )


def test_the_graph_guard_accepts_disjoint_intervals_and_an_evidence_edge() -> None:
    archive = _record("archive", 17, "a" * 64)
    rest = _record("rest", BASE + 1, "b" * 64)
    edge = PrecedenceEvidence(
        observation_key=archive.observation_key,
        revision_id=archive.revision_id,
        superseded_revision_id=rest.revision_id,
        policy=DELIVERY_CHANNEL_BINDING,
        evidence=("equal",),
        knowledge_time=utc(2024, 2, 1),
    )
    graph = assemble_channel_graph([archive], [rest], [edge])
    assert maximal_heads(graph.revisions, graph.precedence_evidence) == (archive.revision_id,)


@pytest.mark.parametrize(
    "case",
    ["collision", "archive_in_rest_list", "rest_in_archive_list", "rest_below_interval",
     "archive_above_interval", "unknown_source"],
)  # fmt: skip
def test_the_graph_guard_fails_closed(case: str) -> None:
    archive = _record("archive", 17, "a" * 64)
    rest = _record("rest", BASE + 1, "b" * 64)
    archive_list, rest_list = [archive], [rest]
    if case == "collision":
        rest_list.append(_record("rest", BASE + 1, "c" * 64))
    elif case == "archive_in_rest_list":
        archive_list, rest_list = [], [archive, rest]
    elif case == "rest_in_archive_list":
        archive_list, rest_list = [archive, rest], []
    elif case == "rest_below_interval":
        rest_list = [rest.model_copy(update={"arrival_seq": 17 + 1})]
    elif case == "archive_above_interval":
        archive_list = [archive.model_copy(update={"arrival_seq": BASE + 2})]
    else:
        archive_list = [archive.model_copy(update={"source_id": "somewhere@1.0.0"})]
    with pytest.raises(ArrivalSeqRangeViolation):
        assemble_channel_graph(archive_list, rest_list)


def test_the_graph_guard_rejects_a_duplicate_payload_graph() -> None:
    rest = _record("rest", BASE + 1, "b" * 64)
    twin = rest.model_copy(update={"arrival_seq": BASE + 2})
    with pytest.raises(CatalogIntegrityError, match="invalid"):
        assemble_channel_graph([], [rest, twin])


def test_record_mapping_round_trips_real_rows(h: RestHarness) -> None:
    _one_equal_pair(h)
    for table in (ARCHIVE_AGGS, REST_AGGS):
        [row] = h.rows(table)
        record = revision_record_from_row(row)
        assert (record.revision_id, record.arrival_seq, record.payload_hash) == (
            row["revision_id"],
            row["arrival_seq"],
            row["payload_hash"],
        )
        assert record.availability.times.knowledge_time == row["knowledge_time"]


# =========================================================================================
# #8 (D3E-R2): every row the reconciler compares is proven lawful first — zero edges otherwise
# =========================================================================================

ARCHIVES = BINANCE_SPOT_ARCHIVES
RESPONSES = BINANCE_SPOT_REST_RESPONSES
FORGED_ID = "rev1-" + "9" * 64
PRECEDENCE_ITEM = {
    "superseded_revision_id": "rev1-" + "a" * 64,
    "policy_id": "binance.spot.delivery-channel",
    "policy_version": "1.0.0",
    "policy_hash": "b" * 64,
    "evidence": ["made up"],
    "knowledge_time": EARLY,
}
_ALL_TABLES = (ARCHIVES, ARCHIVE_AGGS, RESPONSES, REST_AGGS, EVIDENCE)


def _state(h: RestHarness) -> dict[str, Any]:
    """Head and full content of every table the reconciler reads or writes."""
    return {
        definition.table: (h.head(definition.table), h.rows(definition))
        for definition in _ALL_TABLES
    }


def _drifted(row: dict[str, Any], drift: dict[str, Any]) -> dict[str, Any]:
    forged = dict(row)
    for column, value in drift.items():
        forged[column] = value(row) if callable(value) else value
    return forged


def _replace(
    h: RestHarness,
    definition: Any,
    old: dict[str, Any],
    new: dict[str, Any] | None,
    *,
    batch_id: str = "corruption",
) -> None:
    """Swap one committed row for ``new`` (or drop it): a metadata-consistent delete + append."""
    h.delete_rows(definition, EqualTo("revision_id", old["revision_id"]))  # type: ignore[call-arg, arg-type]
    if new is not None:
        h.forge_rows(definition, [new], batch_id)


def _refused(h: RestHarness, match: str) -> None:
    """The reconcile fails closed: no clock reading, no evidence, no table touched at all."""
    before = _state(h)
    clock = StepClock(start=K_EDGE)
    with pytest.raises(CatalogIntegrityError, match=match):
        h.reconciler(clock=clock).reconcile("agg_trades", SYMBOL, DAY)
    assert clock.calls == 0
    assert _state(h) == before
    assert h.rows(EVIDENCE) == [] and h.head(EVIDENCE.table) is None


def _pair(h: RestHarness, count: int = 1) -> list[dict[str, Any]]:
    items = ss.agg_items(count)
    _archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=EARLY)
    _rest(h, "agg_trades", items, knowledge=LATE)
    return items


#: What must catch each batch tamper: the committed fingerprint, the committed row count, or —
#: for a second row claiming a taken arrival number — the arrival-number uniqueness guard. A
#: dropped REST element leaves its lineage one row short of its batches; a dropped archive row
#: leaves its (bounded, re-read) row batch without the committed content.
_BATCH_TAMPER = {
    # D3E-R3: the body binding catches a swapped element before its batch fingerprint does.
    ("element", "renatived"): r"is not element 1 of its lineage response's body: \['quantity'\]",
    ("element", "dropped"): r"has 2 element row\(s\) but its element batches committed 3",
    ("element", "added"): "arrival_seq .* is not unique",
    # D3E-R3: the re-parse catches a swapped archive row before its batch fingerprint does.
    ("row", "renatived"): r"is not line 2 of archive revision .*: \['quantity'\]",
    ("row", "dropped"): "committed with other content",
    ("row", "added"): "arrival_seq .* is not unique",
}


def _renatived(row: dict[str, Any], channel: str) -> dict[str, Any]:
    """Another quantity with a self-consistent payload hash and revision id (row-level lawful)."""
    forged = dict(row, quantity=row["quantity"] + Decimal("0.001"))
    rules: Any = rest_identity if channel == "rest" else archive_identity
    if channel == "rest":
        forged["payload_hash"] = rest_identity.agg_trade_payload_hash(SYMBOL, forged)
    else:
        forged["payload_hash"] = archive_identity.agg_trade_payload_hash(
            SYMBOL, "millisecond", forged
        )
    forged["revision_id"] = rules.revision_id(
        forged["observation_key"], forged["source_id"], forged["payload_hash"]
    )
    return forged


# ------------------------------------------------------------------ Codex counterexamples


def test_codex_r2_a_forged_rest_lineage_and_time_write_no_edge(h: RestHarness) -> None:
    """``/tmp/d3e_r1_reconciler_lineage_probe.py``: lineage ``rev1-999…``, knowledge +1 h."""
    _pair(h)
    [row] = h.rows(REST_AGGS)
    forged = _drifted(
        row,
        {
            "response_revision_id": FORGED_ID,
            "knowledge_time": lambda r: r["knowledge_time"] + timedelta(hours=1),
        },
    )
    h.overwrite_rows(REST_AGGS, [forged], batch_id="forged-lineage")
    _refused(h, f"lineage response revision {FORGED_ID} is committed 0 time")
    assert h.rows(REST_AGGS) == [forged]  # never repaired


def test_codex_r2_b_forged_archive_time_and_policy_write_no_edge(h: RestHarness) -> None:
    """``/tmp/d3e_r1_reconciler_archive_probe.py``: knowledge +1 h, policy hash zeroed."""
    _pair(h)
    [row] = h.rows(ARCHIVE_AGGS)
    forged = _drifted(
        row,
        {
            "knowledge_time": lambda r: r["knowledge_time"] + timedelta(hours=1),
            "availability_policy_hash": "0" * 64,
        },
    )
    h.overwrite_rows(ARCHIVE_AGGS, [forged], batch_id="forged-archive-time-policy")
    _refused(h, r"\['availability_policy_hash', 'knowledge_time'\]")
    assert h.rows(ARCHIVE_AGGS) == [forged]


# ------------------------------------------------------------------ REST element rows


@pytest.mark.parametrize(
    ("drift", "match"),
    [
        ({"response_revision_id": FORGED_ID}, "committed 0 time"),
        ({"arrival_seq": BASE + 7}, r"\['arrival_seq'\]"),
        ({"element_index": 5, "arrival_seq": BASE + 6}, "outside the 1 element"),
        (
            {"knowledge_time": lambda r: r["knowledge_time"] + timedelta(hours=1)},
            r"\['knowledge_time'\]",
        ),
        (
            {
                "ingest_time": lambda r: r["ingest_time"] - timedelta(seconds=1),
                "available_time": lambda r: r["available_time"] - timedelta(seconds=1),
            },
            r"\['available_time', 'ingest_time'\]",
        ),
        ({"availability_policy_hash": "0" * 64}, r"\['availability_policy_hash'\]"),
        ({"availability_evidence_gap": "made up"}, r"\['availability_evidence_gap'\]"),
        ({"decoder_hash": "0" * 64}, r"\['decoder_hash'\]"),
        ({"decoder_version": "9.9.9"}, r"\['decoder_version'\]"),
        ({"contract_schema_version": "9.9.9"}, r"\['contract_schema_version'\]"),
        ({"supersedes": ["rev1-" + "a" * 64]}, r"\['supersedes'\]"),
        ({"precedence_evidence": [PRECEDENCE_ITEM]}, r"\['precedence_evidence'\]"),
        ({"source_revision_id": "venue-7"}, r"\['source_revision_id'\]"),
    ],
)
def test_a_rest_row_whose_lineage_does_not_hold_writes_no_edge(
    h: RestHarness, drift: dict[str, Any], match: str
) -> None:
    _pair(h)
    [row] = h.rows(REST_AGGS)
    _replace(h, REST_AGGS, row, _drifted(row, drift))
    _refused(h, match)


@pytest.mark.parametrize(
    ("corruption", "match"),
    [
        ("twin-response", "committed 2 time"),
        ("response-knowledge", "committed with other content"),
        ("response-policy", r"\['availability_policy_hash'\]"),
        ("response-batch-replayed", "2 snapshots committing it"),
        ("response-body-missing", "is not published"),
    ],
)
def test_a_rest_row_whose_lineage_response_is_not_lawful_writes_no_edge(
    h: RestHarness, corruption: str, match: str
) -> None:
    _pair(h)
    [response] = h.rows(RESPONSES)
    batch_id = f"{response['revision_id']}.response.{response['arrival_seq']}"
    if corruption == "twin-response":
        h.forge_rows(RESPONSES, [dict(response)], "twin-response")
    elif corruption == "response-knowledge":
        forged = _drifted(
            response, {"knowledge_time": lambda r: r["knowledge_time"] + timedelta(hours=1)}
        )
        _replace(h, RESPONSES, response, forged)
    elif corruption == "response-policy":
        _replace(h, RESPONSES, response, _drifted(response, {"availability_policy_hash": "0" * 64}))
    elif corruption == "response-batch-replayed":
        _replace(h, RESPONSES, response, None)
        h.forge_snapshot(RESPONSES, [dict(response)], batch_id=batch_id)
    else:
        h.object_path(response["object_key"]).unlink()
    _refused(h, match)


@pytest.mark.parametrize("corruption", ["renatived", "dropped", "added"])
def test_a_rest_element_batch_that_no_longer_reproduces_writes_no_edge(
    h: RestHarness, corruption: str
) -> None:
    """Every row is lawful on its own; only the committed element batch proves the tamper.

    ``renatived``: element 101 swapped for a self-consistent competitor (same lineage, index and
    arrival number) — without the batch check it would be a plain mismatch finding.
    ``dropped``: element 101 removed. ``added``: a lawful-looking fourth element appended.
    """
    _pair(h, 3)
    rows = sorted(h.rows(REST_AGGS), key=lambda row: row["element_index"])
    if corruption == "renatived":
        _replace(h, REST_AGGS, rows[1], _renatived(rows[1], "rest"))
    elif corruption == "dropped":
        _replace(h, REST_AGGS, rows[1], None)
    else:
        extra = _renatived(rows[2], "rest")
        h.forge_rows(REST_AGGS, [extra], "hostile-element")
    _refused(h, _BATCH_TAMPER["element", corruption])


# ------------------------------------------------------------------ archive element rows


@pytest.mark.parametrize(
    ("drift", "match"),
    [
        ({"arrival_seq": lambda r: r["arrival_seq"] + 1}, r"\['arrival_seq'\]"),
        (
            {"archive_line_number": 2, "arrival_seq": lambda r: r["arrival_seq"] + 1},
            # D3E-R3: the re-parsed object has one line, so line 2 cannot exist at all.
            "is not a line of the 1-line object",
        ),
        (
            {"knowledge_time": lambda r: r["knowledge_time"] + timedelta(hours=1)},
            r"\['knowledge_time'\]",
        ),
        (
            {
                "ingest_time": lambda r: r["ingest_time"] - timedelta(seconds=1),
                "available_time": lambda r: r["available_time"] - timedelta(seconds=1),
            },
            r"\['available_time', 'ingest_time'\]",
        ),
        ({"availability_policy_hash": "0" * 64}, r"\['availability_policy_hash'\]"),
        ({"availability_policy_version": "9.9.9"}, r"\['availability_policy_version'\]"),
        ({"availability_evidence_gap": "made up"}, r"\['availability_evidence_gap'\]"),
        ({"declared_latency_us": 5}, r"\['declared_latency_us'\]"),
        ({"parser_hash": "0" * 64}, r"\['parser_hash'\]"),
        ({"contract_schema_version": "9.9.9"}, r"\['contract_schema_version'\]"),
        ({"supersedes": ["rev1-" + "a" * 64]}, r"\['supersedes'\]"),
        ({"precedence_evidence": [PRECEDENCE_ITEM]}, r"\['precedence_evidence'\]"),
        ({"source_revision_time": EARLY}, r"\['source_revision_time'\]"),
        ({"event_time": lambda r: r["event_time"] + TICK}, "event_time"),
    ],
)
def test_an_archive_row_whose_lineage_does_not_hold_writes_no_edge(
    h: RestHarness, drift: dict[str, Any], match: str
) -> None:
    _pair(h)
    [row] = h.rows(ARCHIVE_AGGS)
    _replace(h, ARCHIVE_AGGS, row, _drifted(row, drift))
    _refused(h, match)


@pytest.mark.parametrize(
    ("line", "match"),
    [
        (9, r"is not line 1 of archive revision .*: \['quantity'\]"),  # sorts after line 1
        (0, "row .* \\(line 0\\) is not a line of the 3-line object"),  # sorts before line 1
    ],
)
def test_g3p_the_first_failing_archive_row_decides_the_refusal(
    h: RestHarness, line: int, match: str
) -> None:
    """G3-P: the lines are taken from the object in one read; still, in line order, the first
    row that fails (another content, or no line of the object) decides the refusal."""
    _pair(h, 3)
    rows = sorted(h.rows(ARCHIVE_AGGS), key=lambda row: row["archive_line_number"])
    _replace(h, ARCHIVE_AGGS, rows[0], _renatived(rows[0], "archive"), batch_id="corruption-1")
    _replace(h, ARCHIVE_AGGS, rows[2], dict(rows[2], archive_line_number=line), batch_id="c-3")
    _refused(h, match)


@pytest.mark.parametrize(
    ("corruption", "match"),
    [
        ("twin-archive", "committed 2 time"),
        ("archive-knowledge", "committed with other content"),
        ("archive-policy", r"\['availability_policy_hash'\]"),
        ("archive-object-size", r"\['object_size_bytes'\]"),
        ("archive-collector", "collector"),
        ("archive-batch-replayed", "2 snapshots committing it"),
        ("archive-object-missing", "is not published"),
        ("archive-other-symbol", "not committed for this data type"),
    ],
)
def test_an_archive_row_whose_archive_revision_is_not_lawful_writes_no_edge(
    h: RestHarness, corruption: str, match: str
) -> None:
    _pair(h)
    [archive] = h.rows(ARCHIVES)
    batch_id = f"{archive['revision_id']}.archive.{archive['arrival_seq']}"
    replace: dict[str, dict[str, Any]] = {
        "archive-knowledge": {"knowledge_time": lambda r: r["knowledge_time"] + timedelta(hours=1)},
        "archive-policy": {"availability_policy_hash": "0" * 64},
        "archive-object-size": {"object_size_bytes": 1},
        "archive-collector": {"collector_version": "9.9.9"},
        "archive-other-symbol": {"symbol": "ETHUSDT"},
    }
    if corruption == "twin-archive":
        h.forge_rows(ARCHIVES, [dict(archive)], "twin-archive")
    elif corruption in replace:
        _replace(h, ARCHIVES, archive, _drifted(archive, replace[corruption]))
    elif corruption == "archive-batch-replayed":
        _replace(h, ARCHIVES, archive, None)
        h.forge_snapshot(ARCHIVES, [dict(archive)], batch_id=batch_id)
    else:
        h.object_path(archive["object_key"]).unlink()
    _refused(h, match)


@pytest.mark.parametrize("corruption", ["renatived", "dropped", "added"])
def test_an_archive_row_batch_that_no_longer_reproduces_writes_no_edge(
    h: RestHarness, corruption: str
) -> None:
    _pair(h, 3)
    rows = sorted(h.rows(ARCHIVE_AGGS), key=lambda row: row["archive_line_number"])
    if corruption == "renatived":
        _replace(h, ARCHIVE_AGGS, rows[1], _renatived(rows[1], "archive"))
    elif corruption == "dropped":
        _replace(h, ARCHIVE_AGGS, rows[1], None)
    else:
        # A self-consistent competitor of line 2 (same key, line and arrival number).
        h.forge_rows(ARCHIVE_AGGS, [_renatived(rows[1], "archive")], "hostile-archive-row")
    _refused(h, _BATCH_TAMPER["row", corruption])


def test_a_lawful_pair_still_yields_its_edge_after_verification(h: RestHarness) -> None:
    """Control: nothing tampered — the verified pair is equal and gets exactly one edge."""
    _pair(h, 3)
    out = h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)
    assert len(out.edges) == 3 and out.findings == ()
    assert len(h.rows(EVIDENCE)) == 3


# ------------------------------------------------------------------ one fixed view


@dataclass
class _ReadHook(ProxyCatalog):
    """Runs ``hook(n)`` after the n-th archive element read of a pinned read — i.e. between the
    element rows and every lineage / holder / batch verification of that attempt.

    E1 bounding: the pinned read's current-head scans stream through ``scan_column_batches``
    (no ``scan_columns`` is left on this path). A streamed reader is pinned to the head current
    when it opens, so a commit made by ``hook`` right after opening is never seen by that read —
    exactly as after the old whole-table read. Reads at an explicit snapshot (the verified-edge
    stream) are not pinned-read attempts and are not counted, as before."""

    hook: Any = None
    attempts: int = 0

    def scan_columns(self, table: str, **kwargs: Any) -> Any:
        raise AssertionError(f"{table}: a reconcile read must stream")

    def scan_column_batches(self, table: str, **kwargs: Any) -> Any:
        result = self.inner.scan_column_batches(table, **kwargs)
        text = repr(kwargs.get("row_filter"))
        if (
            table == ARCHIVE_AGGS.table
            and "observation_key" in text
            and kwargs.get("snapshot_id") is None
        ):
            self.attempts += 1
            if self.hook is not None:
                self.hook(self.attempts)
        return result


def _mover(h: RestHarness, table: str) -> Any:
    """Commits an unrelated row (never read by this partition's reconcile) to ``table``."""
    templates = {
        ARCHIVES.table: (ARCHIVES, h.rows(ARCHIVES)[0]),
        ARCHIVE_AGGS.table: (ARCHIVE_AGGS, h.rows(ARCHIVE_AGGS)[0]),
        RESPONSES.table: (RESPONSES, h.rows(RESPONSES)[0]),
        REST_AGGS.table: (REST_AGGS, h.rows(REST_AGGS)[0]),
    }
    edge = _edge_row(h)

    def commit(index: int) -> None:
        digest = hashlib.sha256(f"mover-{table}-{index}".encode()).hexdigest()
        if table == EVIDENCE.table:
            row = dict(edge, observation_key=f"binance:spot:agg_trade:ETHUSDT:{index}")
            row["edge_id"] = "edge1-" + digest
            h.forge_rows(EVIDENCE, [row], f"mover-{index}")
            return
        definition, template = templates[table]
        row = dict(template, revision_id="rev1-" + digest, symbol="ETHUSDT")
        row["arrival_seq"] = (
            BASE + (500 + index) * STRIDE
            if table in (RESPONSES.table, REST_AGGS.table)
            else (5000 + index) * STRIDE
        )
        if table == ARCHIVE_AGGS.table:
            row["archive_revision_id"] = "rev1-" + "8" * 64
        if table == REST_AGGS.table:
            row["response_revision_id"] = "rev1-" + "8" * 64
        h.forge_rows(definition, [row], f"mover-{index}")

    return commit


@pytest.mark.parametrize(
    "table", [ARCHIVES.table, ARCHIVE_AGGS.table, RESPONSES.table, REST_AGGS.table, EVIDENCE.table]
)
def test_a_head_moved_mid_read_is_read_again_and_judged_once(h: RestHarness, table: str) -> None:
    _pair(h)
    mover = _mover(h, table)
    proxy = _ReadHook(h.adapter, hook=lambda n: mover(n) if n == 1 else None)

    out = h.reconciler(clock=StepClock(start=K_EDGE), adapter=proxy).reconcile(
        "agg_trades", SYMBOL, DAY
    )

    assert proxy.attempts >= 2  # the first view mixed two snapshots and was discarded
    [edge] = out.edges
    assert out.findings == () and not edge.reused
    [row] = [item for item in h.rows(EVIDENCE) if item["observation_key"].split(":")[3] == SYMBOL]
    assert row["edge_id"] == edge.edge_id


@pytest.mark.parametrize("twin", ["archive", "response"])
def test_a_twin_landing_mid_read_is_never_judged_on_a_mixed_view(h: RestHarness, twin: str) -> None:
    _pair(h)
    definition = ARCHIVES if twin == "archive" else RESPONSES
    [original] = h.rows(definition)

    def land(count: int) -> None:
        if count == 1:
            h.forge_rows(definition, [dict(original)], "hostile-twin")

    proxy = _ReadHook(h.adapter, hook=land)
    clock = StepClock(start=K_EDGE)
    with pytest.raises(CatalogIntegrityError, match="committed 2 time"):
        h.reconciler(clock=clock, adapter=proxy).reconcile("agg_trades", SYMBOL, DAY)
    assert proxy.attempts == 2 and clock.calls == 0
    assert h.rows(EVIDENCE) == []


def test_heads_that_keep_moving_end_in_a_bounded_conflict_without_an_edge(
    h: RestHarness,
) -> None:
    _pair(h)
    mover = _mover(h, ARCHIVES.table)
    proxy = _ReadHook(h.adapter, hook=mover)
    clock = StepClock(start=K_EDGE)
    with pytest.raises(ChannelReconcileConflict, match="kept moving"):
        h.reconciler(clock=clock, adapter=proxy).reconcile("agg_trades", SYMBOL, DAY)
    assert proxy.attempts == 8 and clock.calls == 0
    assert h.rows(EVIDENCE) == []


# =========================================================================================
# D3E-R3: rows are bound to their immutable sources (independent review, 2026-09-25)
# =========================================================================================


def test_r3_an_archive_row_batch_beyond_the_object_is_refused(h: RestHarness) -> None:
    """Review A-1: a hostile ``<A>.rows.00000001`` with line 4 of a 3-line archive."""
    items = ss.agg_items(4)
    _archive(h, "agg_trades", ss.archive_agg_lines(items[:3]), knowledge=EARLY)
    _rest(h, "agg_trades", items, knowledge=LATE)
    [archive] = h.rows(ARCHIVES)
    rows = sorted(h.rows(ARCHIVE_AGGS), key=lambda r: r["archive_line_number"])
    item = items[3]
    native = {name: rows[2][name] for name in AGG_TRADES_ROW_SCHEMA.names}
    native.update(
        agg_trade_id=item["a"],
        price=Decimal(item["p"]),
        quantity=Decimal(item["q"]),
        first_trade_id=item["f"],
        last_trade_id=item["l"],
        timestamp_raw=item["T"],
        is_buyer_maker=item["m"],
        is_best_match=item["M"],
        archive_line_number=4,
        event_time=ss.at_ms(item["T"]),
    )
    chunk = pa.Table.from_pylist([native], schema=AGG_TRADES_ROW_SCHEMA)
    records = _row_records(
        chunk,
        data_type="agg_trades",
        symbol=SYMBOL,
        time_unit=time_unit_for("agg_trades", archive["coverage_start"]).value,
        source_identity=archive_identity.row_source_identity(archive["revision_id"]),
        base=archive["arrival_seq"],
        times=_times_from_row(archive),
        subject=AvailabilitySubject.AGG_TRADE,
        binding=AVAILABILITY_BINDING,
    )
    forged = _row_batch(ARCHIVE_AGGS, records, chunk, "agg_trades").to_pylist()
    h.forge_rows(ARCHIVE_AGGS, forged, _row_batch_id(archive["revision_id"], 1))
    _refused(h, "is not a line of the 3-line object")


def test_r3_a_rest_element_in_a_slot_its_body_does_not_hold_is_refused(h: RestHarness) -> None:
    """Review A-2: lineage L2's elements were all first delivered by L1; a forged batch gives
    L2 an element 0 = trade 103, which L2's body does not hold at index 0."""
    items = ss.agg_items(4)
    _archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=EARLY)
    _rest(h, "agg_trades", items[:3], knowledge=LATE, request_id="req-a", t0=T0)
    _rest(h, "agg_trades", items[:3], knowledge=LATE, request_id="req-b", t0=T0 - 1)
    owners = {row["response_revision_id"] for row in h.rows(REST_AGGS)}
    [l2] = [row for row in h.rows(RESPONSES) if row["revision_id"] not in owners]
    item = items[3]
    template = h.rows(REST_AGGS)[0]
    native = {name: template[name] for name in ELEMENT_NATIVE_COLUMNS["agg_trades"]}
    native.update(
        agg_trade_id=item["a"],
        price=Decimal(item["p"]),
        quantity=Decimal(item["q"]),
        first_trade_id=item["f"],
        last_trade_id=item["l"],
        timestamp_raw=item["T"],
        is_buyer_maker=item["m"],
        is_best_match=item["M"],
    )
    schema = pa.schema([REST_AGGS.arrow_schema.field(name) for name in native])
    native = pa.Table.from_pylist([native], schema=schema).to_pylist()[0]
    *_, row = element_columns(
        REST_AGGS,
        "agg_trades",
        SYMBOL,
        native,
        element_index=0,
        response_revision_id=l2["revision_id"],
        base=l2["arrival_seq"],
        ingest_time=l2["ingest_time"],
        knowledge_time=l2["knowledge_time"],
    )
    h.forge_rows(REST_AGGS, [dict(row)], element_batch_id(l2["revision_id"], 0))
    _refused(h, "is not element 0 of its lineage response's body")


def test_r3_an_edge_recommitted_with_an_earlier_time_is_refused(h: RestHarness) -> None:
    """Review C-1: delete the edge row, re-commit it outside its edge batch, K_E moved earlier."""
    _pair(h)
    h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)
    [edge] = h.rows(EVIDENCE)
    h.delete_rows(EVIDENCE, EqualTo("edge_id", edge["edge_id"]))  # type: ignore[call-arg, arg-type]
    h.forge_rows(EVIDENCE, [dict(edge, knowledge_time=LATE)], "forged-edge")
    with pytest.raises(CatalogIntegrityError, match="not exactly what an edge batch"):
        h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)


def test_r3_a_deleted_edge_is_refused(h: RestHarness) -> None:
    _pair(h)
    h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)
    [edge] = h.rows(EVIDENCE)
    h.delete_rows(EVIDENCE, EqualTo("edge_id", edge["edge_id"]))  # type: ignore[call-arg, arg-type]
    with pytest.raises(CatalogIntegrityError, match="is gone"):
        h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)


# =========================================================================================
# D3E-R3 cross-day: one aggTrade key whose REST revisions fall on two UTC days
# =========================================================================================

#: 2023-11-15T00:00:00Z: the midnight between ``DAY`` and ``NEXT_DAY``.
MIDNIGHT_MS = 1_700_006_400_000
NEXT_DAY = DAY + timedelta(days=1)
ONLY_DAY = f"binance:spot:agg_trade:{SYMBOL}:99"  # a REST revision on DAY only
SPANNING = f"binance:spot:agg_trade:{SYMBOL}:100"  # REST revisions on DAY and on NEXT_DAY
ONLY_NEXT = f"binance:spot:agg_trade:{SYMBOL}:101"  # a REST revision on NEXT_DAY only
K_SECOND = K_EDGE + timedelta(hours=1)


def _cross_midnight(h: RestHarness) -> None:
    """aggTrade 100 is delivered twice by REST: at 23:59:59.999 of DAY and at 00:00 of
    NEXT_DAY (another event time, so another payload and revision). Each day's archive holds
    the equal counterpart of its own REST revision; 99 / 101 live on one day only.

    Partitions DAY and NEXT_DAY therefore both read every revision of key 100 and derive the
    same two key-100 edges, while their edge batches carry different day prefixes.
    """
    before = ss.agg_item(99, MIDNIGHT_MS - 2)
    late = ss.agg_item(100, MIDNIGHT_MS - 1)
    early = ss.agg_item(100, MIDNIGHT_MS)
    after = ss.agg_item(101, MIDNIGHT_MS + 1)
    _archive(
        h,
        "agg_trades",
        ss.archive_agg_lines([before, late]),
        knowledge=EARLY,
        request_id="archive-day",
    )
    _archive(
        h,
        "agg_trades",
        ss.archive_agg_lines([early, after]),
        knowledge=EARLY,
        day=NEXT_DAY,
        retrieved_at=utc(2023, 11, 17),
        request_id="archive-next",
    )
    _rest(
        h,
        "agg_trades",
        [before, late],
        knowledge=LATE,
        request_id="req-day",
        t0=MIDNIGHT_MS - MINUTE_MS,
    )
    _rest(
        h,
        "agg_trades",
        [early, after],
        knowledge=LATE + timedelta(hours=1),
        request_id="req-next",
        t0=MIDNIGHT_MS - MINUTE_MS - 1,
    )
    days: dict[str, set[Any]] = {}
    for row in h.rows(REST_AGGS):
        days.setdefault(row["observation_key"], set()).add(row["event_time"].date())
    assert days == {ONLY_DAY: {DAY}, SPANNING: {DAY, NEXT_DAY}, ONLY_NEXT: {NEXT_DAY}}


def _keys_of(out: ChannelReconciled) -> list[str]:
    return sorted(item.edge.evidence.observation_key for item in out.edges)


def _pinned_view(h: RestHarness, evidence_snapshot: str | None = None) -> PinnedCatalogView:
    """A read-only catalog view pinned like a PIT manifest (what the PIT selector reads)."""
    bindings = {table.table: h.head(table.table) for table in _ALL_TABLES}
    if evidence_snapshot is not None:
        bindings[EVIDENCE.table] = evidence_snapshot
    return PinnedCatalogView(
        h.adapter, {table: head for table, head in bindings.items() if head is not None}
    )


def _pinned_edges(h: RestHarness, day: Any, evidence_snapshot: str | None = None) -> list[Any]:
    view = _pinned_view(h, evidence_snapshot)
    return list(ChannelReconciler(view, h.storage).verified_edges("agg_trades", SYMBOL, day))


@pytest.mark.parametrize("early_close", [False, True], ids=["exhaust", "early_close"])
def test_verified_edge_run_matches_compatibility_and_closes_reader(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch, early_close: bool
) -> None:
    _cross_midnight(h)
    h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)
    reconciler = ChannelReconciler(h.adapter, h.storage)
    expected = reconciler.verified_edges("agg_trades", SYMBOL, DAY)
    opened: list[Any] = []
    closed: list[Any] = []
    original = cast(Any, channel_reconcile).iter_run

    @contextmanager
    def tracked_iter_run(storage: Any, root: Any) -> Iterator[Any]:
        with original(storage, root) as rows:
            opened.append(root)
            try:
                yield rows
            finally:
                closed.append(root)

    monkeypatch.setattr(channel_reconcile, "iter_run", tracked_iter_run)
    params = channel_reconcile.VerifiedEdgeRunParams(
        row_capacity=2,
        merge_fanout=2,
        limits=RunLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2),
    )
    actual: tuple[ChannelEdge, ...]
    with reconciler.iter_verified_edges("agg_trades", SYMBOL, DAY, params=params) as stream:
        if early_close:
            actual = (next(iter(stream)),)
        else:
            actual = tuple(stream)

    assert opened
    assert len(closed) == len(opened)

    def semantic_edge(edge: Any) -> tuple[Any, ...]:
        evidence = edge.evidence.model_dump(
            mode="python",
            exclude={"schema_version": True, "policy": {"schema_version": True}},
        )
        return (
            edge.edge_id,
            evidence,
            edge.revision_table,
            edge.superseded_table,
            edge.revision_snapshot_id,
            edge.superseded_snapshot_id,
            edge.projection_sha256,
        )

    if early_close:
        assert semantic_edge(actual[0]) == semantic_edge(expected[0])
    else:
        assert [semantic_edge(edge) for edge in actual] == [
            semantic_edge(edge) for edge in sorted(expected, key=lambda edge: edge.edge_id)
        ]


@pytest.mark.parametrize("first_day", [DAY, NEXT_DAY], ids=["day_first", "next_first"])
def test_r3_cross_day_a_key_spanning_midnight_reconciles_from_both_days(
    h: RestHarness, first_day: Any
) -> None:
    _cross_midnight(h)
    second_day = NEXT_DAY if first_day == DAY else DAY
    own = {DAY: ONLY_DAY, NEXT_DAY: ONLY_NEXT}

    first = h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, first_day)
    first_head = h.head(EVIDENCE.table)
    second = h.reconciler(clock=StepClock(start=K_SECOND)).reconcile(
        "agg_trades", SYMBOL, second_day
    )

    # First run: both key-100 edges and its own single-day key, in one batch of its prefix.
    assert _keys_of(first) == sorted([SPANNING, SPANNING, own[first_day]])
    assert all(not item.reused for item in first.edges) and len(first.commits) == 1
    assert len(first.findings) == 2  # key 100's cross pairs differ in event time
    # Second run: key 100's edges were committed by the other day's batch; re-verified, reused.
    assert _keys_of(second) == sorted([SPANNING, SPANNING, own[second_day]])
    assert {item.edge.evidence.observation_key for item in second.edges if item.reused} == {
        SPANNING
    }
    assert [item.edge.evidence.observation_key for item in second.edges if not item.reused] == [
        own[second_day]
    ]
    prefix = f"{DELIVERY_CHANNEL_BINDING.policy_id}@{DELIVERY_CHANNEL_BINDING.version}.edges."
    [first_commit], [second_commit] = first.commits, second.commits
    assert first_commit.batch_id.startswith(f"{prefix}agg_trades.{SYMBOL}.{first_day}.")
    assert second_commit.batch_id.startswith(f"{prefix}agg_trades.{SYMBOL}.{second_day}.")
    rows = h.rows(EVIDENCE)
    assert len(rows) == len({row["edge_id"] for row in rows}) == 4  # 99, 101 and both of key 100
    times: dict[str, set[datetime]] = {}
    for row in rows:
        times.setdefault(row["observation_key"], set()).add(row["knowledge_time"])
    # The first commit of each edge is authoritative: key 100 keeps the first run's time.
    assert times == {SPANNING: {K_EDGE}, own[first_day]: {K_EDGE}, own[second_day]: {K_SECOND}}

    # Re-runs of both days, in both orders, are idempotent: no clock, no commit, all reused.
    head = h.head(EVIDENCE.table)
    edges: dict[Any, tuple[str, ...]] = {}
    for day in (first_day, second_day, first_day):
        later = StepClock(start=K_EDGE + timedelta(days=30))
        again = h.reconciler(clock=later).reconcile("agg_trades", SYMBOL, day)
        assert later.calls == 0 and again.commits == () and h.head(EVIDENCE.table) == head
        assert again.edges and all(item.reused for item in again.edges)
        edges[day] = again.edge_ids
    assert edges[first_day] == first.edge_ids and edges[second_day] == second.edge_ids
    assert h.rows(EVIDENCE) == rows

    # verified_edges (the PIT selector's call) agrees live and on a pinned view, for both days.
    for day in (DAY, NEXT_DAY):
        clock = StepClock(start=FAR)
        live = h.reconciler(clock=clock).verified_edges("agg_trades", SYMBOL, day)
        assert tuple(edge.edge_id for edge in live) == edges[day] and clock.calls == 0
        assert tuple(edge.edge_id for edge in _pinned_edges(h, day)) == edges[day]
    # A manifest pinned between the two edge batches: the second day already sees key 100's
    # edges (committed by the first day's batch) and not its own, still uncommitted one.
    assert first_head is not None
    early = {day: _pinned_edges(h, day, first_head) for day in (DAY, NEXT_DAY)}
    assert tuple(edge.edge_id for edge in early[first_day]) == first.edge_ids
    assert sorted(edge.evidence.observation_key for edge in early[second_day]) == [
        SPANNING,
        SPANNING,
    ]

    # PIT over the pinned verified edges: each single-day key selects its archive revision;
    # key 100's two REST revisions are superseded, its two archive revisions stay competing.
    evidence = {
        edge.edge_id: edge.evidence for day in (DAY, NEXT_DAY) for edge in _pinned_edges(h, day)
    }
    assert len(evidence) == 4
    for key, status in (
        (ONLY_DAY, PointInTimeStatus.SELECTED),
        (ONLY_NEXT, PointInTimeStatus.SELECTED),
        (SPANNING, PointInTimeStatus.CONFLICT),
    ):
        archive, rest = _records(h, key)
        graph = assemble_channel_graph(
            archive, rest, [item for item in evidence.values() if item.observation_key == key]
        )
        pit = _select(list(graph.revisions), list(graph.precedence_evidence), key, FAR)
        assert pit.status is status
        assert set(pit.maximal_heads) == {record.revision_id for record in archive}


def test_r3_cross_day_both_orders_converge_to_the_same_edge_set(tmp_path: Path) -> None:
    edge_sets = []
    for label, order in (("day", (DAY, NEXT_DAY)), ("next", (NEXT_DAY, DAY))):
        with ss.sqlite_harness(tmp_path / label) as h:
            _cross_midnight(h)
            for day in order:
                h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, day)
            edge_sets.append(
                sorted(
                    (row["edge_id"], row["revision_id"], row["superseded_revision_id"])
                    for row in h.rows(EVIDENCE)
                )
            )
    assert len(edge_sets[0]) == 4 and edge_sets[0] == edge_sets[1]


def _spanning_edge(h: RestHarness) -> dict[str, Any]:
    """Both days reconciled, DAY first: a key-100 edge that DAY's edge batch committed."""
    _cross_midnight(h)
    h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, DAY)
    h.reconciler(clock=StepClock(start=K_SECOND)).reconcile("agg_trades", SYMBOL, NEXT_DAY)
    return sorted(
        (row for row in h.rows(EVIDENCE) if row["observation_key"] == SPANNING),
        key=lambda row: row["edge_id"],
    )[0]


def _cross_refused(h: RestHarness, day: Any, match: str) -> None:
    """``reconcile`` and ``verified_edges`` (live and pinned) of ``day`` fail closed."""
    before = _state(h)
    clock = StepClock(start=K_EDGE)
    with pytest.raises(CatalogIntegrityError, match=match):
        h.reconciler(clock=clock).reconcile("agg_trades", SYMBOL, day)
    with pytest.raises(CatalogIntegrityError, match=match):
        h.reconciler(clock=clock).verified_edges("agg_trades", SYMBOL, day)
    with pytest.raises(CatalogIntegrityError, match=match):
        _pinned_edges(h, day)
    assert clock.calls == 0 and _state(h) == before


def _drop(h: RestHarness, edge: dict[str, Any]) -> None:
    h.delete_rows(EVIDENCE, EqualTo("edge_id", edge["edge_id"]))  # type: ignore[call-arg, arg-type]


@pytest.mark.parametrize("day", [DAY, NEXT_DAY], ids=["from_day", "from_next"])
def test_r3_cross_day_an_edge_recommitted_with_an_earlier_time_is_refused(
    h: RestHarness, day: Any
) -> None:
    edge = _spanning_edge(h)
    _drop(h, edge)
    h.forge_rows(EVIDENCE, [dict(edge, knowledge_time=LATE + timedelta(hours=2))], "forged-edge")
    _cross_refused(h, day, "not exactly what an edge batch")


@pytest.mark.parametrize("day", [DAY, NEXT_DAY], ids=["from_day", "from_next"])
def test_r3_cross_day_a_forged_second_row_is_refused(h: RestHarness, day: Any) -> None:
    edge = _spanning_edge(h)
    h.forge_rows(EVIDENCE, [dict(edge)], "forged-duplicate")
    _cross_refused(h, day, "committed twice")


@pytest.mark.parametrize("day", [DAY, NEXT_DAY], ids=["from_day", "from_next"])
def test_r3_cross_day_a_deleted_edge_is_refused(h: RestHarness, day: Any) -> None:
    edge = _spanning_edge(h)
    _drop(h, edge)
    _cross_refused(h, day, "is gone")


@pytest.mark.parametrize("day", [DAY, NEXT_DAY], ids=["from_day", "from_next"])
def test_r3_cross_day_a_recommit_as_the_other_days_batch_is_refused(
    h: RestHarness, day: Any
) -> None:
    """Delete key 100's edge and re-commit the identical row as a reproducible NEXT_DAY edge
    batch (content-derived id, count and fingerprint all right): DAY's batch committed it."""
    edge = _spanning_edge(h)
    _drop(h, edge)
    partition = ("agg_trades", SYMBOL, NEXT_DAY)
    batch_id = channel_reconcile._edge_batch_id(partition, [edge["edge_id"]])
    h.forge_rows(EVIDENCE, [dict(edge)], batch_id)
    _cross_refused(h, day, "committed twice")


@pytest.mark.parametrize("day", [DAY, NEXT_DAY], ids=["from_day", "from_next"])
def test_r3_cross_day_a_batch_claiming_the_other_days_prefix_is_refused(
    h: RestHarness, day: Any
) -> None:
    """A re-timed key-100 row committed under a DAY edge-batch id its content does not derive."""
    edge = _spanning_edge(h)
    _drop(h, edge)
    prefix = channel_reconcile._edge_batch_prefix(("agg_trades", SYMBOL, DAY))
    forged = dict(edge, knowledge_time=LATE + timedelta(hours=2))
    h.forge_rows(EVIDENCE, [forged], prefix + "0" * 64)
    _cross_refused(h, day, "no longer reproduces")

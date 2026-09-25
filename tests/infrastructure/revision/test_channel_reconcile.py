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
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

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
from core.domain.base import canonical_json
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
from infrastructure.revision import channel_reconcile, rest_identity
from infrastructure.revision import identity as archive_identity
from infrastructure.revision.channel_precedence import (
    AGG_TRADE_PROJECTION,
    DELIVERY_CHANNEL_BINDING,
    DELIVERY_CHANNEL_HASH,
    KLINE_1M_PROJECTION,
    POLICY_STATEMENT,
    Channel,
    ChannelComparison,
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
    assemble_channel_graph,
    check_arrival_seq,
    evidence_from_row,
    revision_record_from_row,
)
from infrastructure.revision.precedence import maximal_heads
from infrastructure.revision.rest_availability import RestAvailabilitySubject
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
        "contract_schema_version": "2.0.0",
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
    ("element", "renatived"): "committed with other content",
    ("element", "dropped"): r"has 2 element row\(s\) but its element batches committed 3",
    ("element", "added"): "arrival_seq .* is not unique",
    ("row", "renatived"): "committed with other content",
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
            "not committed by any batch",
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
    element rows and every lineage / holder / batch verification of that attempt."""

    hook: Any = None
    attempts: int = 0

    def scan_columns(self, table: str, **kwargs: Any) -> Any:
        result = self.inner.scan_columns(table, **kwargs)
        text = repr(kwargs.get("row_filter"))
        if table == ARCHIVE_AGGS.table and "observation_key" in text:
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

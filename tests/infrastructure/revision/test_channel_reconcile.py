"""D3E cross-channel reconciler and graph guard (ADR-0027 §4 / §9 / §11; acceptance #6 ~ #8,
#15 crash point 3 (r1), #21).

Archive rows come from the real D2 store over real ZIP bytes; REST rows from the real D3D
collector plus the D3E store. The reconciler under test is then the only writer of the evidence
table. The four ``knowledge_cutoff`` segments are evaluated with the real contracts
(``RevisionGraph``, ``PointInTimeSelection``) and the accepted maximal-head helper; the expected
selection in each segment is written down by hand from ADR-0027 §4.7.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

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
    BINANCE_SPOT_KLINES_1M,
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_KLINES_1M,
)
from infrastructure.revision import identity as archive_identity
from infrastructure.revision import rest_identity
from infrastructure.revision.channel_precedence import (
    AGG_TRADE_PROJECTION,
    DELIVERY_CHANNEL_BINDING,
    DELIVERY_CHANNEL_HASH,
    KLINE_1M_PROJECTION,
    POLICY_STATEMENT,
    Channel,
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
    assert not isinstance(h.collect(request, start_ms=retrieved_ms), Exception)
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


def test_an_incomparable_pair_yields_a_finding_next_to_an_equal_one(h: RestHarness) -> None:
    items = ss.kline_items(1)
    _archive(h, "klines_1m", ss.archive_kline_lines(items), knowledge=EARLY)
    _rest(h, "klines_1m", items, knowledge=LATE)
    # A lawful second REST revision of the same minute whose "ignore" field is empty.
    [real] = h.rows(REST_KLINES)
    odd = dict(real)
    odd["ignore_raw"] = ""
    odd["payload_hash"] = rest_identity.kline_1m_payload_hash(SYMBOL, odd)
    odd["revision_id"] = rest_identity.revision_id(
        odd["observation_key"], odd["source_id"], odd["payload_hash"]
    )
    odd["arrival_seq"] = BASE + 7 * STRIDE + 1
    h.forge_rows(REST_KLINES, [odd], "second-rest-revision")

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

    with pytest.raises(CatalogIntegrityError, match="contradict"):
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

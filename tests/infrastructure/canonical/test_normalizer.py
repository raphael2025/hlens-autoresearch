"""E1 Canonical normalizer (ADR-0028; roadmap #15; ADR-0028 E1 acceptance #1 ~ #12).

Raw rows come from the accepted D2 store and the D3E REST store; the D-33 edge from the real
reconciler. Expected values are written down from ADR-0028 by hand, not from the builder.
"""

from __future__ import annotations

import hashlib
from collections.abc import Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast

import pytest
from pyiceberg.expressions import AlwaysFalse, AlwaysTrue, And, EqualTo, GreaterThanOrEqual, In

from core.contracts.catalog import CommitRequest, CommitResult, SnapshotNotFound
from core.contracts.revision import PointInTimeStatus, PrecedenceEvidence
from core.domain.base import CONTRACT_SCHEMA_VERSION, canonical_json
from infrastructure.canonical import normalizer as nz
from infrastructure.canonical import rules
from infrastructure.canonical.normalizer import (
    CanonicalNormalizeConflict,
    CanonicalNormalizeError,
    CanonicalNormalizer,
    CanonicalUnitIncomplete,
    unit_batch_id,
)
from infrastructure.catalog import iceberg_adapter as iceberg_adapter_module
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError, CommitLayout
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision import ArchiveIngested, RawRevisionStore
from infrastructure.revision import identity as archive_identity
from infrastructure.revision.channel_reconcile import evidence_from_row, revision_record_from_row
from infrastructure.revision.row_integrity import PersistedRowVerifier
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.collector import rest_support as cs
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision import revision_support as rs
from tests.infrastructure.revision.rest_store_support import (
    SYMBOL,
    Crash,
    ProxyCatalog,
    RestHarness,
    StepClock,
    utc,
)

K_ARCHIVE = utc(2023, 12, 1)
K_REST = utc(2023, 12, 5)
K_NORM = utc(2023, 12, 6)
K_EDGE = utc(2023, 12, 10)
KEY = f"binance:spot:agg_trade:{SYMBOL}:100"


class _BatchStream(Protocol):
    """The closable stream a catalog's ``scan_column_batches`` hands back."""

    _closed: bool

    @property
    def schema(self) -> Any: ...

    def __iter__(self) -> Iterator[Any]: ...

    def __next__(self) -> Any: ...

    def close(self) -> None: ...


def _sha(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def _pair(h: RestHarness, count: int = 3) -> tuple[str, str, list[dict[str, Any]]]:
    items = ss.agg_items(count)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE)
    [response] = c.ingest_rest(h, "agg_trades", items, knowledge=K_REST)
    return archive, response, items


# =========================================================================================
# the rules
# =========================================================================================


def test_spec_hashes_are_derived_from_their_specs() -> None:
    assert rules.IDENTITY_HASH == _sha(rules.IDENTITY_SPEC)
    assert rules.NORMALIZER_BINDING.policy_hash == _sha(rules.NORMALIZER_SPEC)
    assert rules.AVAILABILITY_BINDING.policy_hash == _sha(rules.AVAILABILITY_SPEC)
    assert rules.PRECEDENCE_MAP_BINDING.policy_hash == _sha(rules.PRECEDENCE_MAP_SPEC)
    ids = {
        rules.NORMALIZER_BINDING.policy_id,
        rules.AVAILABILITY_BINDING.policy_id,
        rules.PRECEDENCE_MAP_BINDING.policy_id,
        rules.IDENTITY_SPEC["rule"],
    }
    assert ids == {
        "hlens.canonical.binance-spot.normalizer",
        "hlens.canonical.availability",
        "hlens.canonical.precedence-map",
        "hlens.canonical.revision-identity",
    }
    # Separate from both Raw identity rules (ADR-0027 §11 trap 1).
    assert rules.IDENTITY_HASH != archive_identity.IDENTITY_HASH


def test_the_revision_id_is_exactly_the_documented_digest() -> None:
    key, source, payload = KEY, "hlens.canonical.binance-spot.normalizer@1.0.0|t|r", "a" * 64
    document = {
        "rule": "hlens.canonical.revision-identity",
        "rule_version": "1.0.0",
        "rule_hash": rules.IDENTITY_HASH,
        "observation_key": key,
        "source_id": source,
        "payload_hash": payload,
    }
    assert rules.revision_id(key, source, payload) == f"crev1-{_sha(document)}"


def test_iter_revision_ids_rejects_a_tampered_result_revision_count(h: RestHarness) -> None:
    archive, _, _ = _pair(h, 3)
    n = c.normalizer(h, clock=StepClock(start=K_NORM), microbatch_rows=1)
    out = n.normalize_unit(c.ARCHIVE_AGGS.table, archive)
    tampered = replace(out, revision_count=out.revision_count + 1)

    with pytest.raises(
        CatalogIntegrityError, match="no longer matches its committed Canonical unit"
    ):
        list(n.iter_revision_ids(tampered))


def test_iter_revision_ids_early_close_releases_disk_backed_position_index(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, _, _ = _pair(h, 3)
    n = c.normalizer(h, clock=StepClock(start=K_NORM), microbatch_rows=1)
    created: list[nz._PositionIndex] = []
    original_init = nz._PositionIndex.__init__

    def track_index(index: nz._PositionIndex, scratch_directory: Path) -> None:
        original_init(index, scratch_directory)
        created.append(index)

    monkeypatch.setattr(nz._PositionIndex, "__init__", track_index)
    out = n.normalize_unit(c.ARCHIVE_AGGS.table, archive)
    created.clear()

    ids = n.iter_revision_ids(out)
    assert isinstance(ids, Generator)
    assert next(ids)
    assert created and any(not index._closed for index in created)
    ids.close()
    assert created and all(index._closed for index in created)


def test_normalizer_pin_uses_one_stable_bounded_pointer_and_streams_exact_history(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, _, _ = _pair(h, 3)
    channel = rules.raw_channel_of(c.ARCHIVE_AGGS.table)
    normalizer = c.normalizer(
        h, clock=StepClock(start=K_NORM), metadata_limits=nz.NORMALIZER_METADATA_LIMITS
    )
    normalizer.normalize_unit(c.ARCHIVE_AGGS.table, archive)
    expected_head = h.head(c.TRADES.table)
    assert expected_head is not None
    expected_history = list(h.adapter.history(c.TRADES.table, expected_head))
    expected_rows = h.adapter.scan_columns(
        c.ARCHIVE_AGGS.table, columns=("archive_line_number", "symbol")
    ).to_pylist()

    def reject_eager_load(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the bounded normalizer pin loaded eager table metadata")

    pinned_pointers: list[tuple[str, str | None]] = []
    pin_metadata = h.adapter.pin_bounded_metadata

    def capture_pin(table: str, *, storage: Any, limits: Any) -> Any:
        handle = pin_metadata(table, storage=storage, limits=limits)
        pinned_pointers.append((handle.metadata_location, handle.selected_snapshot_id))
        return handle

    monkeypatch.setattr(h.adapter, "pin_bounded_metadata", capture_pin)
    monkeypatch.setattr(h.adapter._catalog, "load_table", reject_eager_load)
    pin = normalizer._pin(channel, archive)

    assert pin.canonical_head == expected_head
    assert list(pin.catalog.history(c.TRADES.table, expected_head)) == expected_history
    handles = pin.catalog._bounded_metadata
    assert len(pinned_pointers) == 6
    assert pinned_pointers[:3] == pinned_pointers[3:]
    assert set(handles) == {channel.element.table, channel.source.table, channel.canonical.table}
    assert all(handle.metadata_location for handle in handles.values())
    assert all(handle.snapshot_count <= handle._limits.max_snapshots for handle in handles.values())
    assert all(not handle.metadata.snapshots for handle in handles.values())

    batches = cast(
        _BatchStream,
        pin.catalog.scan_column_batches(
            channel.element.table, columns=("archive_line_number", "symbol")
        ),
    )
    try:
        assert [row for batch in batches for row in batch.to_pylist()] == expected_rows
    finally:
        batches.close()

    early = cast(
        _BatchStream,
        pin.catalog.scan_column_batches(
            channel.element.table, columns=("archive_line_number", "symbol")
        ),
    )
    next(early)
    stream = early
    assert not stream._closed
    stream.close()
    assert stream._closed


def test_normalizer_bounded_pin_rejects_a_missing_exact_snapshot(h: RestHarness) -> None:
    archive, _, _ = _pair(h, 3)
    channel = rules.raw_channel_of(c.ARCHIVE_AGGS.table)
    bindings = {
        channel.element.table: "999999999999999999",
        channel.source.table: h.head(channel.source.table),
        channel.canonical.table: h.head(channel.canonical.table),
    }
    view = PinnedCatalogView(h.adapter, bindings)
    normalizer = c.normalizer(
        h,
        clock=StepClock(start=K_NORM),
        adapter=view,
        metadata_limits=nz.NORMALIZER_METADATA_LIMITS,
    )

    with pytest.raises(SnapshotNotFound):
        normalizer._pin(channel, archive)


def test_bounded_pinned_view_can_be_nested_without_eager_reads(h: RestHarness) -> None:
    archive, _, _ = _pair(h, 3)
    channel = rules.raw_channel_of(c.ARCHIVE_AGGS.table)
    writer = c.normalizer(h, clock=StepClock(start=K_NORM))
    writer.normalize_unit(c.ARCHIVE_AGGS.table, archive)
    tables = (channel.element.table, channel.source.table, channel.canonical.table)
    bindings = {table: h.head(table) for table in tables}
    inner = PinnedCatalogView(h.adapter, bindings)
    reader = c.normalizer(
        h,
        clock=StepClock(start=K_NORM),
        adapter=inner,
        metadata_limits=nz.NORMALIZER_METADATA_LIMITS,
    )
    trades_head = h.head(c.TRADES.table)
    assert trades_head is not None
    expected = list(h.adapter.history(c.TRADES.table, trades_head))

    pin = reader._pin(channel, archive)
    assert pin.catalog.load_table(channel.element.table) is not None
    assert pin.canonical_head is not None
    assert list(pin.catalog.history(c.TRADES.table, pin.canonical_head)) == expected
    batches = cast(
        _BatchStream,
        pin.catalog.scan_column_batches(
            channel.element.table, columns=("archive_line_number", "symbol")
        ),
    )
    try:
        assert sum(batch.num_rows for batch in batches) == 3
    finally:
        batches.close()


def test_bounded_scan_columns_keeps_pointer_limit_and_empty_schema_after_metadata_moves(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, _, _ = _pair(h, 3)
    channel = rules.raw_channel_of(c.ARCHIVE_AGGS.table)
    normalizer = c.normalizer(
        h, clock=StepClock(start=K_NORM), metadata_limits=nz.NORMALIZER_METADATA_LIMITS
    )
    normalizer.normalize_unit(c.ARCHIVE_AGGS.table, archive)
    pin = normalizer._pin(channel, archive)
    columns = ("arrival_seq", "revision_id")

    before_batches = cast(
        _BatchStream, pin.catalog.scan_column_batches(c.TRADES.table, columns=columns)
    )
    try:
        expected = [row for batch in before_batches for row in batch.to_pylist()]
    finally:
        before_batches.close()
    assert expected

    empty_inner = PinnedCatalogView(h.adapter, {c.TRADES.table: None})
    empty_handle = empty_inner.pin_bounded_metadata(
        c.TRADES.table, storage=h.storage, limits=normalizer._metadata_limits
    )
    empty_view = PinnedCatalogView(
        h.adapter, {c.TRADES.table: None}, bounded_metadata={c.TRADES.table: empty_handle}
    )
    empty_expected = h.adapter.scan_columns(
        c.TRADES.table, columns=columns, row_filter=AlwaysFalse()
    )
    empty_bounded = empty_view.scan_columns(c.TRADES.table, columns=columns)
    assert empty_bounded.num_rows == 0
    assert empty_bounded.schema == empty_expected.schema

    second_items = ss.agg_items(1, first_id=200, first_ms=ss.T0 + 60_000)
    second_archive = c.ingest_archive(
        h,
        "agg_trades",
        ss.archive_agg_lines(second_items),
        knowledge=K_ARCHIVE + timedelta(days=1),
        request_id="archive-after-pin",
    )
    normalizer.normalize_unit(c.ARCHIVE_AGGS.table, second_archive)

    def reject_current_pointer(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("bounded scan_columns reloaded the moved catalog pointer")

    monkeypatch.setattr(h.adapter, "scan_columns", reject_current_pointer)
    bounded_all = pin.catalog.scan_columns(c.TRADES.table, columns=columns)
    assert bounded_all.to_pylist() == expected
    bounded_limited = pin.catalog.scan_columns(c.TRADES.table, columns=columns, limit=2)
    assert bounded_limited.to_pylist() == expected[:2]

    no_rows = pin.catalog.scan_columns(
        c.TRADES.table,
        columns=columns,
        row_filter=EqualTo("arrival_seq", -1),  # type: ignore[call-arg, arg-type]
    )
    assert no_rows.num_rows == 0
    empty_stream = cast(
        _BatchStream,
        pin.catalog.scan_column_batches(
            c.TRADES.table,
            columns=columns,
            row_filter=EqualTo("arrival_seq", -1),  # type: ignore[call-arg, arg-type]
        ),
    )
    try:
        assert no_rows.schema == empty_stream.schema
        assert list(empty_stream) == []
    finally:
        empty_stream.close()


def test_bounded_scan_closes_reader_and_source_after_read_error() -> None:
    closed: list[str] = []

    class BrokenReader:
        def __init__(self, source: Any) -> None:
            self.source = source

        def __iter__(self) -> BrokenReader:
            return self

        def __next__(self) -> Any:
            next(self.source)
            raise RuntimeError("scan failed")

        def close(self) -> None:
            closed.append("reader")

    def source() -> Any:
        try:
            yield
        finally:
            closed.append("source")

    iterator = source()
    stream = iceberg_adapter_module._SnapshotBatchStream(BrokenReader(iterator), iterator)
    with pytest.raises(RuntimeError, match="scan failed"):
        next(stream)
    assert stream._closed
    assert closed == ["reader", "source"]


def test_the_trade_payload_is_exactly_the_documented_document() -> None:
    columns = {
        "venue": "binance",
        "instrument_type": "spot",
        "symbol": "BTC-USDT",
        "venue_symbol": "BTCUSDT",
        "venue_trade_id": "100",
        "price": Decimal("92792.05"),
        "quantity": Decimal("0.0015"),
        "buyer_is_maker": True,
        "event_time": utc(2023, 11, 14, 22, 14),
    }
    document = {
        "kind": "hlens.canonical.trade/1",
        "venue": "binance",
        "instrument_type": "spot",
        "symbol": "BTC-USDT",
        "venue_symbol": "BTCUSDT",
        "venue_trade_id": "100",
        "price": "92792.050000000000000000",
        "quantity": "0.001500000000000000",
        "buyer_is_maker": True,
        "event_time_us": 1_700_000_040_000_000,
    }
    assert rules.payload_hash("agg_trades", columns) == _sha(document)
    with pytest.raises(rules.CanonicalRuleViolation, match="18 fractional"):
        rules.payload_hash("agg_trades", dict(columns, price=Decimal("0.1000000000000000001")))
    with pytest.raises(rules.CanonicalRuleViolation, match="non-negative"):
        rules.payload_hash("agg_trades", dict(columns, quantity=Decimal("-1")))


@pytest.mark.parametrize(
    ("value", "ok"),
    [(0, True), (rules.ARRIVAL_SEQ_STRIDE, True), (5, False), (1 << 62, False), (-1 << 32, False),
     (True, False)],
)  # fmt: skip
def test_block_bases(value: Any, ok: bool) -> None:
    if ok:
        assert rules.check_block_base(value) == value
    else:
        with pytest.raises(rules.CanonicalRuleViolation):
            rules.check_block_base(value)


# =========================================================================================
# #2 / #5: one Canonical revision per Raw revision, lineage in the identity
# =========================================================================================


def test_archive_and_rest_units_map_one_to_one_with_every_frozen_column(h: RestHarness) -> None:
    archive, response, items = _pair(h)
    [archive_row] = [row for row in h.rows(c.ARCHIVE_AGGS) if row["agg_trade_id"] == 100]
    [rest_row] = [row for row in h.rows(c.REST_AGGS) if row["agg_trade_id"] == 100]
    clock = StepClock(start=K_NORM)
    n = c.normalizer(h, clock=clock)

    first = n.normalize_unit(c.ARCHIVE_AGGS.table, archive)
    second = n.normalize_unit(c.REST_AGGS.table, response)

    assert clock.calls == 2  # one reading per unit
    assert (first.arrival_seq_base, second.arrival_seq_base) == (0, c.STRIDE)
    rows = {row["lineage_raw_revision_id"]: row for row in h.rows(c.TRADES)}
    assert len(rows) == 6
    a, r = rows[archive_row["revision_id"]], rows[rest_row["revision_id"]]
    # Same market content, same payload hash, two revisions (lineage is identity, #5).
    assert a["payload_hash"] == r["payload_hash"] and a["revision_id"] != r["revision_id"]
    assert a["observation_key"] == r["observation_key"] == KEY
    assert a["source_id"] == (
        f"hlens.canonical.binance-spot.normalizer@1.0.0|raw.binance_spot_agg_trades|"
        f"{archive_row['revision_id']}"
    )
    assert (a["lineage_source_table"], a["lineage_source_revision_id"]) == (
        "raw.binance_spot_archives",
        archive,
    )
    assert (r["lineage_source_table"], r["lineage_source_revision_id"]) == (
        "raw.binance_spot_rest_responses",
        response,
    )
    assert a["arrival_seq"] == 0 + 1 and r["arrival_seq"] == c.STRIDE + 0 + 1
    item = items[0]
    assert (a["venue"], a["instrument_type"], a["symbol"], a["venue_symbol"]) == (
        "binance",
        "spot",
        "BTC-USDT",
        "BTCUSDT",
    )
    assert a["venue_trade_id"] == str(item["a"]) and a["buyer_is_maker"] is item["m"]
    assert a["price"] == Decimal(item["p"]) and a["quantity"] == Decimal(item["q"])
    # Times (ADR-0023 §3): Raw ingest / available, knowledge = the unit's one reading.
    assert a["ingest_time"] == archive_row["ingest_time"]
    assert a["available_time"] == archive_row["available_time"] == archive_row["ingest_time"]
    assert a["knowledge_time"] == K_NORM >= archive_row["knowledge_time"]
    assert r["knowledge_time"] == K_NORM + timedelta(seconds=1)  # the clock's second reading
    assert a["availability_policy_id"] == "hlens.canonical.availability"
    assert a["availability_evidence"] == []
    assert a["availability_evidence_gap"].startswith(
        f"inherited from raw.binance_spot_agg_trades/{archive_row['revision_id']} under "
        "binance.spot.publication@1.0.0: "
    )
    assert a["availability_evidence_gap"].endswith(archive_row["availability_evidence_gap"])
    assert a["supersedes"] == a["precedence_evidence"] == r["supersedes"] == []
    assert a["contract_schema_version"] == CONTRACT_SCHEMA_VERSION  # a new unit (ADR-0052 V2)
    assert a["declared_latency_us"] == 0 and a["source_time"] is None
    # Every Canonical row maps back to a lawful contract record.
    for row in rows.values():
        revision_record_from_row(row)


def test_klines_become_one_minute_bars(h: RestHarness) -> None:
    items = ss.kline_items(2)
    archive = c.ingest_archive(h, "klines_1m", ss.archive_kline_lines(items), knowledge=K_ARCHIVE)
    [response] = c.ingest_rest(h, "klines_1m", items, knowledge=K_REST)
    n = c.normalizer(h, clock=StepClock(start=K_NORM))
    n.normalize_unit(c.ARCHIVE_KLINES.table, archive)
    n.normalize_unit(c.REST_KLINES.table, response)
    rows = h.rows(c.BARS)
    assert len(rows) == 4
    [raw] = [row for row in h.rows(c.ARCHIVE_KLINES) if row["open_time_raw"] == items[0][0]]
    [bar] = [row for row in rows if row["lineage_raw_revision_id"] == raw["revision_id"]]
    assert bar["interval_end"] - bar["interval_start"] == timedelta(minutes=1)
    assert (bar["trade_count"], bar["quote_volume"]) == (
        raw["number_of_trades"],
        raw["quote_asset_volume"],
    )
    assert bar["available_time"] == raw["ingest_time"]  # >= interval_end, gap branch
    by_key: dict[str, set[str]] = {}
    for row in rows:
        by_key.setdefault(row["observation_key"], set()).add(row["payload_hash"])
    assert all(len(hashes) == 1 for hashes in by_key.values())  # equal content across channels


def test_equal_canonical_content_from_different_raw_content_is_never_folded(
    h: RestHarness,
) -> None:
    """#6: archive and REST differ only in ``first_trade_id`` (dropped by Canonical)."""
    items = ss.agg_items(1)
    archive_items = [dict(items[0], f=items[0]["f"] - 1)]
    archive = c.ingest_archive(
        h, "agg_trades", ss.archive_agg_lines(archive_items), knowledge=K_ARCHIVE
    )
    [response] = c.ingest_rest(h, "agg_trades", items, knowledge=K_REST)
    out = h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, ss.DAY)
    assert out.edges == () and len(out.findings) == 1  # D-33: mismatch, no Raw edge
    n = c.normalizer(h, clock=StepClock(start=K_NORM))
    n.normalize_unit(c.ARCHIVE_AGGS.table, archive)
    n.normalize_unit(c.REST_AGGS.table, response)
    a, r = c.records(h, KEY)
    assert a.payload_hash == r.payload_hash and a.revision_id != r.revision_id
    status, heads = c.select([a, r], c.mapped_edges(h, KEY), utc(2030, 1, 1))
    assert status is PointInTimeStatus.CONFLICT and set(heads) == {a.revision_id, r.revision_id}


# =========================================================================================
# #1 / #2: both arrival orders × edge before / after normalization × four cutoffs
# =========================================================================================


@pytest.mark.parametrize("order", ["archive_first", "rest_first"])
@pytest.mark.parametrize("edge", ["before_normalization", "after_normalization"])
def test_four_cutoffs_over_mapped_edges(h: RestHarness, order: str, edge: str) -> None:
    items = ss.agg_items(1)
    k_first, k_second = utc(2023, 12, 1), utc(2023, 12, 5)
    if order == "archive_first":
        archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=k_first)
        [response] = c.ingest_rest(h, "agg_trades", items, knowledge=k_second)
    else:
        [response] = c.ingest_rest(h, "agg_trades", items, knowledge=k_first)
        archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=k_second)
    k_norm_a, k_norm_r = utc(2023, 12, 6), utc(2023, 12, 7)
    # Before normalization: after both Raw knowledge times, before either Canonical row exists.
    k_edge = utc(2023, 12, 5, 12) if edge == "before_normalization" else utc(2023, 12, 10)

    def reconcile() -> None:
        h.reconciler(clock=StepClock(start=k_edge)).reconcile("agg_trades", SYMBOL, ss.DAY)

    if edge == "before_normalization":
        reconcile()
    c.normalizer(h, clock=StepClock(start=k_norm_a)).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    c.normalizer(h, clock=StepClock(start=k_norm_r)).normalize_unit(c.REST_AGGS.table, response)
    if edge == "after_normalization":
        reconcile()

    revisions = c.records(h, KEY)
    [a] = [item for item in revisions if "raw.binance_spot_agg_trades|" in item.source_id]
    [r] = [item for item in revisions if "raw.binance_spot_rest_agg_trades|" in item.source_id]
    [mapped] = c.mapped_edges(h, KEY)
    k_mapped = max(k_edge, k_norm_a, k_norm_r)
    assert mapped.knowledge_time == k_mapped
    assert (mapped.revision_id, mapped.superseded_revision_id) == (a.revision_id, r.revision_id)
    table = {
        "before both": c.select(revisions, [mapped], k_norm_a - c.TICK),
        "first": c.select(revisions, [mapped], k_norm_a),
        "both, edge unknown": c.select(revisions, [mapped], k_norm_r),
        "edge known": c.select(revisions, [mapped], k_mapped),
    }
    assert table["before both"][0] is PointInTimeStatus.ABSENT
    assert table["first"] == (PointInTimeStatus.SELECTED, (a.revision_id,))
    if edge == "before_normalization":
        # K_E' = max(...) = the later normalization: the conflict window is empty.
        assert k_mapped == k_norm_r
        assert table["both, edge unknown"] == (PointInTimeStatus.SELECTED, (a.revision_id,))
    else:
        assert table["both, edge unknown"][0] is PointInTimeStatus.CONFLICT
        assert c.select(revisions, [mapped], k_mapped - c.TICK)[0] is PointInTimeStatus.CONFLICT
    assert table["edge known"] == (PointInTimeStatus.SELECTED, (a.revision_id,))
    # Nothing was materialised in Canonical rows: the edge lives only in the Raw evidence table.
    assert all(row["supersedes"] == [] for row in h.rows(c.TRADES))


def test_the_mapped_edge_is_exact_and_refuses_wrong_endpoints(h: RestHarness) -> None:
    archive, response, _ = _pair(h, 1)
    n = c.normalizer(h, clock=StepClock(start=K_NORM))
    n.normalize_unit(c.ARCHIVE_AGGS.table, archive)
    n.normalize_unit(c.REST_AGGS.table, response)
    h.reconciler(clock=StepClock(start=K_EDGE)).reconcile("agg_trades", SYMBOL, ss.DAY)
    [raw_row] = h.rows(c.EVIDENCE)
    raw_edge = evidence_from_row(raw_row)
    [a_row] = [
        row for row in h.rows(c.TRADES) if row["lineage_raw_revision_id"] == raw_edge.revision_id
    ]
    [r_row] = [
        row
        for row in h.rows(c.TRADES)
        if row["lineage_raw_revision_id"] == raw_edge.superseded_revision_id
    ]
    a, r = revision_record_from_row(a_row), revision_record_from_row(r_row)
    snapshot = h.head(c.EVIDENCE.table)
    assert snapshot is not None
    mapped = rules.map_channel_edge(raw_edge, raw_row["edge_id"], snapshot, a, r)
    assert mapped == PrecedenceEvidence(
        observation_key=KEY,
        revision_id=a.revision_id,
        superseded_revision_id=r.revision_id,
        policy=rules.PRECEDENCE_MAP_BINDING,
        evidence=(
            rules.PRECEDENCE_MAP_STATEMENT,
            f"raw_edge_id={raw_row['edge_id']}",
            f"raw_evidence_table=raw.binance_spot_precedence_evidence@snapshot:{snapshot}",
            *raw_edge.evidence,
        ),
        knowledge_time=K_EDGE,
    )
    with pytest.raises(rules.CanonicalRuleViolation, match="not the Canonical image"):
        rules.map_channel_edge(raw_edge, raw_row["edge_id"], snapshot, r, a)
    other = a.model_copy(update={"observation_key": f"binance:spot:agg_trade:{SYMBOL}:999"})
    with pytest.raises(rules.CanonicalRuleViolation, match="another observation_key"):
        rules.map_channel_edge(raw_edge, raw_row["edge_id"], snapshot, other, r)
    foreign = a.model_copy(update={"source_id": f"elsewhere|x|{raw_edge.revision_id}"})
    with pytest.raises(rules.CanonicalRuleViolation, match="not the Canonical image"):
        rules.map_channel_edge(raw_edge, raw_row["edge_id"], snapshot, foreign, r)
    with pytest.raises(rules.CanonicalRuleViolation, match="snapshot"):
        rules.map_channel_edge(raw_edge, raw_row["edge_id"], "", a, r)


def test_same_channel_competition_stays_a_conflict(h: RestHarness) -> None:
    """#4: two REST bodies disagree on one element → two Canonical revisions, no edge."""
    items = ss.agg_items(1)
    changed = [dict(items[0], q="0.00250000")]
    cs.queue_agg_chain(h.venue, SYMBOL, ss.T0, [items])
    cs.queue_agg_chain(h.venue, SYMBOL, ss.T0, [changed])
    first = h.collect(ss.agg_request("req-a"))
    second = h.collect(ss.agg_request("req-b"), start_ms=cs.RETRIEVED_AT_MS + 60_000)
    assert not isinstance(first, Exception) and not isinstance(second, Exception)
    store = h.store(clock=StepClock(start=K_REST))
    pages = [store.ingest_collection(ss.agg_request(name)).pages[0] for name in ("req-a", "req-b")]
    n = c.normalizer(h, clock=StepClock(start=K_NORM))
    for page in pages:
        n.normalize_unit(c.REST_AGGS.table, page.response_revision_id)
    revisions = c.records(h, KEY)
    assert len(revisions) == 2 and revisions[0].payload_hash != revisions[1].payload_hash
    assert c.select(revisions, c.mapped_edges(h, KEY), utc(2030, 1, 1))[0] is (
        PointInTimeStatus.CONFLICT
    )


# =========================================================================================
# idempotency, crash recovery, concurrency (#7 / #8)
# =========================================================================================


def test_a_replay_changes_nothing_and_reads_no_clock(h: RestHarness) -> None:
    archive, _, _ = _pair(h)
    c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    before = (h.head(c.TRADES.table), h.rows(c.TRADES))
    later = StepClock(start=K_NORM + timedelta(days=9))
    again = c.normalizer(h, clock=later).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert again.replayed and later.calls == 0
    assert (h.head(c.TRADES.table), h.rows(c.TRADES)) == before


@pytest.mark.parametrize("crash_after", [1, 2])
def test_a_crash_between_batches_resumes_with_the_first_base_and_time(
    h: RestHarness, crash_after: int
) -> None:
    archive, _, _ = _pair(h)
    proxy = ProxyCatalog(h.adapter, after=ss.crash_after_commits(crash_after, table=c.TRADES.table))
    # Only the pre-ADR-0108 per-batch layout can stop between batches: old history (legacy).
    with pytest.raises(Crash):
        c.normalizer(h, clock=StepClock(start=K_NORM), adapter=proxy, microbatch_rows=1,
                     legacy=True).normalize_unit(c.ARCHIVE_AGGS.table, archive)  # fmt: skip
    assert len(h.rows(c.TRADES)) == crash_after
    later = StepClock(start=K_NORM + timedelta(hours=5))
    out = c.normalizer(h, clock=later, microbatch_rows=1).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    assert later.calls == 0 and out.knowledge_time == K_NORM and out.arrival_seq_base == 0
    rows = h.rows(c.TRADES)
    assert len(rows) == 3 and {row["knowledge_time"] for row in rows} == {K_NORM}
    assert out.batch_count == 3 and out.replayed_batch_count == crash_after
    # Same result as one uninterrupted run in an independent catalog.
    with ss.sqlite_harness(h.tmp_path / "reference") as ref:
        ref_archive, _, _ = _pair(ref)
        c.normalizer(ref, clock=StepClock(start=K_NORM), microbatch_rows=1).normalize_unit(
            c.ARCHIVE_AGGS.table, ref_archive
        )
        ref_rows = ref.rows(c.TRADES)
    key = lambda row: row["revision_id"]  # noqa: E731
    assert sorted(rows, key=key) == sorted(ref_rows, key=key)


def test_a_crash_before_the_first_batch_starts_over_with_a_new_reading(h: RestHarness) -> None:
    archive, _, _ = _pair(h)

    def die(request: CommitRequest) -> None:
        raise Crash("before the first commit")

    with pytest.raises(Crash):
        c.normalizer(h, clock=StepClock(start=K_NORM), adapter=ProxyCatalog(h.adapter, before=die))\
            .normalize_unit(c.ARCHIVE_AGGS.table, archive)  # fmt: skip
    assert h.rows(c.TRADES) == []
    later = K_NORM + timedelta(hours=1)
    out = c.normalizer(h, clock=StepClock(start=later)).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    assert out.knowledge_time == later  # the earlier reading was never persisted


def test_a_concurrent_unit_takes_the_block_and_the_loser_reallocates(h: RestHarness) -> None:
    archive, response, _ = _pair(h)
    rival = c.normalizer(h, clock=StepClock(start=K_NORM + timedelta(minutes=1)))
    fired: list[bool] = []

    def interleave(request: CommitRequest) -> None:
        if not fired:
            fired.append(True)
            rival.normalize_unit(c.REST_AGGS.table, response)

    proxy = ProxyCatalog(h.adapter, before=interleave)
    out = c.normalizer(h, clock=StepClock(start=K_NORM), adapter=proxy).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    assert fired and out.arrival_seq_base == c.STRIDE  # the rival committed block 0 first
    seqs = [row["arrival_seq"] for row in h.rows(c.TRADES)]
    assert len(seqs) == len(set(seqs)) == 6


def test_the_same_unit_raced_is_committed_once(h: RestHarness) -> None:
    archive, _, _ = _pair(h)
    rival = c.normalizer(h, clock=StepClock(start=K_NORM + timedelta(minutes=1)))
    fired: list[bool] = []

    def interleave(request: CommitRequest) -> None:
        if not fired:
            fired.append(True)
            rival.normalize_unit(c.ARCHIVE_AGGS.table, archive)

    proxy = ProxyCatalog(h.adapter, before=interleave)
    out = c.normalizer(h, clock=StepClock(start=K_NORM), adapter=proxy).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    assert out.replayed and out.knowledge_time == K_NORM + timedelta(minutes=1)
    assert len(h.rows(c.TRADES)) == 3


# =========================================================================================
# fail closed (#10 / #12)
# =========================================================================================


def _state(h: RestHarness) -> tuple[Any, ...]:
    return (h.head(c.TRADES.table), h.rows(c.TRADES))


def test_a_forged_raw_row_is_never_normalized(h: RestHarness) -> None:
    archive, _, _ = _pair(h, 1)
    [row] = h.rows(c.ARCHIVE_AGGS)
    forged = dict(row, knowledge_time=row["knowledge_time"] + timedelta(hours=1))
    h.delete_rows(c.ARCHIVE_AGGS, EqualTo("revision_id", row["revision_id"]))  # type: ignore[call-arg, arg-type]
    h.forge_rows(c.ARCHIVE_AGGS, [forged], "corruption")
    clock = StepClock(start=K_NORM)
    with pytest.raises(CatalogIntegrityError, match=r"\['knowledge_time'\]"):
        c.normalizer(h, clock=clock).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert clock.calls == 0 and h.rows(c.TRADES) == []


@pytest.mark.parametrize(
    ("drift", "match"),
    [
        # A unit's ready time is recovered from its committed rows, so a one-row unit whose time
        # drifted re-normalizes consistently: only its committed batch fingerprint exposes it.
        (
            {"knowledge_time": lambda r: r["knowledge_time"] + timedelta(hours=1)},
            "committed with other content",
        ),
        ({"symbol": "BTCUSDT"}, "disagrees"),
        ({"lineage_source_table": "raw.binance_spot_rest_responses"}, "disagrees|not part"),
        ({"supersedes": ["crev1-" + "a" * 64]}, "disagrees"),
        ({"payload_hash": "0" * 64}, "disagrees"),
    ],
)
def test_a_committed_canonical_row_that_drifts_fails_closed(
    h: RestHarness, drift: dict[str, Any], match: str
) -> None:
    archive, _, _ = _pair(h, 1)
    c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    [row] = h.rows(c.TRADES)
    forged = {name: (value(row) if callable(value) else value) for name, value in drift.items()}
    h.delete_rows(c.TRADES, EqualTo("revision_id", row["revision_id"]))  # type: ignore[call-arg, arg-type]
    h.forge_rows(c.TRADES, [dict(row, **forged)], "corruption")
    before = _state(h)
    with pytest.raises(CatalogIntegrityError, match=match):
        c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert _state(h) == before


@pytest.mark.parametrize("pinned", [False, True])
def test_a_narrow_proof_of_a_unit_layout_rejects_a_reappended_row(
    h: RestHarness, pinned: bool
) -> None:
    """ADR-0108 unit layout: a narrow (PIT) proof must catch a delete + re-append of a unit row.

    The forged rows keep their revision ids and arrival_seqs but all move ``knowledge_time``, so
    the recovered unit facts and the window's planned rows move with them; only the unit
    snapshot's whole-unit fingerprint, re-checked on the narrow path, still tells them apart.
    """
    archive, _, _ = _pair(h, 3)
    c.normalizer(h, clock=StepClock(start=K_NORM), microbatch_rows=1).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    rows = sorted(h.rows(c.TRADES), key=lambda item: item["arrival_seq"])
    h.delete_rows(c.TRADES, AlwaysTrue())
    shifted = rows[0]["knowledge_time"] + timedelta(hours=1)
    h.forge_rows(c.TRADES, [dict(row, knowledge_time=shifted) for row in rows], "corruption")
    adapter: Any = h.adapter
    if pinned:
        channel = rules.raw_channel_of(c.ARCHIVE_AGGS.table)
        tables = (channel.element.table, channel.source.table, channel.canonical.table)
        adapter = PinnedCatalogView(h.adapter, {table: h.head(table) for table in tables})
    n = c.normalizer(h, clock=StepClock(start=K_NORM), adapter=adapter)
    with pytest.raises(CatalogIntegrityError):
        n.verify_unit(c.ARCHIVE_AGGS.table, archive, arrival_seqs={rows[2]["arrival_seq"]})


def test_a_committed_batch_that_no_longer_reproduces_fails_closed(h: RestHarness) -> None:
    archive, _, _ = _pair(h)
    c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    row = sorted(h.rows(c.TRADES), key=lambda item: item["arrival_seq"])[1]
    h.delete_rows(c.TRADES, EqualTo("revision_id", row["revision_id"]))  # type: ignore[call-arg, arg-type]
    before = _state(h)
    with pytest.raises(CatalogIntegrityError, match="rows deleted or added"):
        c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert _state(h) == before


@pytest.mark.parametrize("microbatch_rows", [None, 1])
def test_a_rest_unit_the_store_has_not_finished_is_refused_until_it_has(
    h: RestHarness, microbatch_rows: int | None
) -> None:
    """G2-R1a / RT-3 (was: a Raw unit that grows after normalization fails closed).

    Before G2-R1a the crash-partial REST unit was normalized as a one-row unit and failed
    closed forever once the store finished it ("the Raw unit changed"). Now its missing
    elements — held by no other page — make it incomplete: nothing is committed and no clock
    is read until the store's rerun completes the page; then the whole page is normalized.
    """
    items = ss.agg_items(3)
    cs.queue_agg_chain(h.venue, SYMBOL, ss.T0, [items])
    collected = h.collect(ss.agg_request("req-a"))
    assert not isinstance(collected, Exception), collected
    proxy = ProxyCatalog(h.adapter, after=ss.crash_after_commits(1, table=c.REST_AGGS.table))
    with pytest.raises(Crash):
        h.store(clock=StepClock(start=K_REST), adapter=proxy, element_microbatch_rows=1)\
            .ingest_collection(ss.agg_request("req-a"))  # fmt: skip
    [response] = h.rows(c.RESPONSES)
    assert len(h.rows(c.REST_AGGS)) == 1
    clock = StepClock(start=K_NORM)
    n = c.normalizer(h, clock=clock, microbatch_rows=microbatch_rows)
    before = _state(h)
    with pytest.raises(CanonicalUnitIncomplete, match=r"element\(s\) \[1, 2\] are committed"):
        n.normalize_unit(c.REST_AGGS.table, response["revision_id"])
    with pytest.raises(CanonicalUnitIncomplete):
        n.verify_unit(c.REST_AGGS.table, response["revision_id"])
    assert clock.calls == 0 and _state(h) == before == (None, [])
    h.store(clock=StepClock(start=K_REST), element_microbatch_rows=1).ingest_collection(
        ss.agg_request("req-a")
    )
    out = n.normalize_unit(c.REST_AGGS.table, response["revision_id"])
    revision_ids = tuple(n.iter_revision_ids(out))
    assert len(revision_ids) == 3 and clock.calls == 1
    assert sorted(row["venue_trade_id"] for row in h.rows(c.TRADES)) == ["100", "101", "102"]
    assert [
        row["revision_id"] for row in n.verify_unit(c.REST_AGGS.table, response["revision_id"])
    ] == list(revision_ids)


def test_a_plan_recording_another_unit_size_fails_closed(h: RestHarness) -> None:
    """E1-R1: the unit size in every batch id; a Raw unit of another size no longer matches."""
    archive, _, _ = _pair(h)
    h.forge_rows(c.TRADES, [_forged_row(h, 0)], unit_batch_id(archive, 4, 1, 0))
    clock = StepClock(start=K_NORM)
    before = _state(h)
    with pytest.raises(CatalogIntegrityError, match="the Raw unit changed"):
        c.normalizer(h, clock=clock).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert clock.calls == 0 and _state(h) == before


# =========================================================================================
# E1-R3: the committed batch ids are the plan (review B)
# =========================================================================================


def _unit_batches(h: RestHarness, source: str) -> list[tuple[str | None, int | None]]:
    return [
        (s.batch_id, s.added_rows)
        for s in h.history(c.TRADES.table)
        if s.batch_id is not None and source in s.batch_id
    ]


def _crash_partial(h: RestHarness, count: int, microbatch_rows: int) -> str:
    """A unit stopped after its first batch: pre-ADR-0108 per-batch history (``legacy``)."""
    items = ss.agg_items(count)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE)
    proxy = ProxyCatalog(h.adapter, after=ss.crash_after_commits(1, table=c.TRADES.table))
    with pytest.raises(Crash):
        c.normalizer(
            h,
            clock=StepClock(start=K_NORM),
            adapter=proxy,
            microbatch_rows=microbatch_rows,
            legacy=True,
        ).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    return archive


@pytest.mark.parametrize("microbatch_rows", [None, 1, 2, 3])
def test_recovery_follows_the_committed_plan_whatever_the_configuration(
    h: RestHarness, microbatch_rows: int | None
) -> None:
    archive = _crash_partial(h, 5, 2)
    assert len(h.rows(c.TRADES)) == 2
    clock = StepClock(start=K_NORM + timedelta(hours=1))
    out = c.normalizer(h, clock=clock, microbatch_rows=microbatch_rows).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    assert clock.calls == 0 and out.knowledge_time == K_NORM  # recovered, not re-read
    assert _unit_batches(h, archive) == [
        (unit_batch_id(archive, 5, 2, 0), 2),
        (unit_batch_id(archive, 5, 2, 1), 2),
        (unit_batch_id(archive, 5, 2, 2), 1),
    ]
    assert unit_batch_id(archive, 5, 2, 1).endswith(".0000000005.000002.00000001")
    assert len(h.rows(c.TRADES)) == 5
    n = c.normalizer(h, clock=StepClock(start=K_NORM))
    verified = n.verify_unit(c.ARCHIVE_AGGS.table, archive)
    assert [row["revision_id"] for row in verified] == list(n.iter_revision_ids(out))


@pytest.mark.parametrize("delete", ["all", "one"])
def test_batches_whose_rows_were_deleted_fail_closed_without_a_clock_reading(
    h: RestHarness, delete: str
) -> None:
    archive, _, _ = _pair(h)
    n = c.normalizer(h, clock=StepClock(start=K_NORM))
    n.normalize_unit(c.ARCHIVE_AGGS.table, archive)
    rows = sorted(h.rows(c.TRADES), key=lambda item: item["arrival_seq"])
    column, value = (
        ("lineage_source_revision_id", archive)
        if delete == "all"
        else ("revision_id", rows[-1]["revision_id"])
    )
    h.delete_rows(c.TRADES, EqualTo(column, value))  # type: ignore[call-arg, arg-type]
    match = "rows are gone" if delete == "all" else "rows deleted or added"
    before = _state(h)
    with pytest.raises(CatalogIntegrityError, match=match):
        n.verify_unit(c.ARCHIVE_AGGS.table, archive)
    clock = StepClock(start=K_NORM + timedelta(days=3))
    with pytest.raises(CatalogIntegrityError, match=match):
        c.normalizer(h, clock=clock).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert clock.calls == 0 and _state(h) == before


def _forged_row(h: RestHarness, index: int) -> dict[str, Any]:
    channel = rules.raw_channel_of(c.ARCHIVE_AGGS.table)
    raw = sorted(h.rows(c.ARCHIVE_AGGS), key=lambda row: row["archive_line_number"])
    return rules.canonical_row(
        channel,
        raw[index],
        base=0,
        ready_time=K_NORM,
        contract_schema_version=CONTRACT_SCHEMA_VERSION,
    )


@pytest.mark.parametrize(
    ("chunk", "match"),
    [
        (1, "more than one plan"),  # another microbatch size under the same unit
        (3, "committed with other content"),  # the plan's own id, 1 row where 3 belong
    ],
)
def test_a_batch_off_the_committed_plan_fails_closed(
    h: RestHarness, chunk: int, match: str
) -> None:
    archive = _crash_partial(h, 7, 3)
    h.forge_rows(c.TRADES, [_forged_row(h, 3)], unit_batch_id(archive, 7, chunk, 1))
    before = _state(h)
    with pytest.raises(CatalogIntegrityError, match=match):
        c.normalizer(h, clock=StepClock(start=K_NORM)).verify_unit(c.ARCHIVE_AGGS.table, archive)
    for microbatch_rows in (1, 3, None):
        clock = StepClock(start=K_NORM)
        with pytest.raises(CatalogIntegrityError, match=match):
            c.normalizer(h, clock=clock, microbatch_rows=microbatch_rows).normalize_unit(
                c.ARCHIVE_AGGS.table, archive
            )
        assert clock.calls == 0
    assert _state(h) == before


@pytest.mark.parametrize(
    "suffix", ["0000000007.000003", "7.000003.00000001", "0000000007.000003.0000000x"]
)
def test_a_malformed_batch_id_of_the_unit_fails_closed(h: RestHarness, suffix: str) -> None:
    archive = _crash_partial(h, 7, 3)
    prefix = unit_batch_id(archive, 7, 3, 0).rsplit(".", 3)[0]
    h.forge_rows(c.TRADES, [_forged_row(h, 3)], f"{prefix}.{suffix}")
    with pytest.raises(CatalogIntegrityError, match="malformed batch id"):
        c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(c.ARCHIVE_AGGS.table, archive)


def test_committed_rows_without_a_batch_fail_closed(h: RestHarness) -> None:
    archive, _, _ = _pair(h, 1)
    h.forge_rows(c.TRADES, [_forged_row(h, 0)], "not-a-normalizer-batch")
    clock = StepClock(start=K_NORM)
    with pytest.raises(CatalogIntegrityError, match="no committed batch"):
        c.normalizer(h, clock=clock).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert clock.calls == 0


def test_a_recovered_batch_committed_with_other_content_is_corruption(h: RestHarness) -> None:
    """Every writer recovers the same plan, so a batch conflict then is never contention."""
    archive = _crash_partial(h, 5, 2)
    fired: list[bool] = []

    def forge(request: CommitRequest) -> None:
        if not fired:
            fired.append(True)
            h.forge_rows(c.TRADES, [_forged_row(h, 4)], request.batch_id)

    clock = StepClock(start=K_NORM)
    proxy = ProxyCatalog(h.adapter, before=forge)
    with pytest.raises(CatalogIntegrityError, match="committed with other content"):
        c.normalizer(h, clock=clock, adapter=proxy).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert clock.calls == 0 and fired


def test_a_fresh_unit_raced_reads_the_clock_once(h: RestHarness) -> None:
    archive, _, _ = _pair(h)
    rival = c.normalizer(h, clock=StepClock(start=K_NORM + timedelta(minutes=1)))

    def interleave(request: CommitRequest) -> None:
        if not h.rows(c.TRADES):
            rival.normalize_unit(c.ARCHIVE_AGGS.table, archive)

    clock = StepClock(start=K_NORM)
    out = c.normalizer(h, clock=clock, adapter=ProxyCatalog(h.adapter, before=interleave))\
        .normalize_unit(c.ARCHIVE_AGGS.table, archive)  # fmt: skip
    assert clock.calls == 1 and out.knowledge_time == K_NORM + timedelta(minutes=1)


def test_a_clock_behind_raw_knowledge_or_naive_is_refused(h: RestHarness) -> None:
    archive, _, _ = _pair(h)
    with pytest.raises(CanonicalNormalizeConflict, match="backfill"):
        c.normalizer(h, clock=StepClock(start=K_ARCHIVE - c.TICK)).normalize_unit(
            c.ARCHIVE_AGGS.table, archive
        )
    with pytest.raises(CanonicalNormalizeError, match="timezone-aware"):
        c.normalizer(h, clock=lambda: datetime(2024, 1, 1)).normalize_unit(
            c.ARCHIVE_AGGS.table, archive
        )
    assert h.rows(c.TRADES) == []


def test_unknown_units_and_tables_fail_closed(h: RestHarness) -> None:
    _pair(h, 1)
    n = c.normalizer(h, clock=StepClock(start=K_NORM))
    with pytest.raises(CanonicalNormalizeError, match="Raw element table"):
        n.normalize_unit("canonical.trades", "x")
    with pytest.raises(CanonicalNormalizeError, match="not one committed revision"):
        n.normalize_unit(c.ARCHIVE_AGGS.table, "rev1-" + "9" * 64)


def test_an_empty_rest_page_is_a_unit_without_rows(h: RestHarness) -> None:
    cs.queue_agg_chain(h.venue, SYMBOL, ss.T0, [[]])
    collected = h.collect(ss.agg_request("req-empty"))
    assert not isinstance(collected, Exception), collected
    stored = h.store(clock=StepClock(start=K_REST)).ingest_collection(ss.agg_request("req-empty"))
    out = c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(
        c.REST_AGGS.table, stored.pages[0].response_revision_id
    )
    assert out.revision_count == out.batch_count == out.replayed_batch_count == 0
    assert tuple(c.normalizer(h, clock=StepClock(start=K_NORM)).iter_revision_ids(out)) == ()
    assert h.rows(c.TRADES) == []


# =========================================================================================
# #8: one fixed view
# =========================================================================================


@dataclass
class _ReadHook(ProxyCatalog):
    """Runs ``hook(n)`` after the n-th Raw element read of the unit (before verification)."""

    hook: Any = None
    reads: int = 0
    table: str = field(default=c.ARCHIVE_AGGS.table)

    def scan_column_batches(self, table: str, **kwargs: Any) -> Any:
        result = self.inner.scan_column_batches(table, **kwargs)
        if (
            table == self.table
            and "archive_revision_id" in repr(kwargs.get("row_filter"))
            and (kwargs.get("columns") and len(kwargs["columns"]) > 2)
        ):
            self.reads += 1
            if self.hook is not None:
                self.hook(self.reads)
        return result


def test_a_head_moved_mid_read_does_not_move_the_call(h: RestHarness) -> None:
    """G3-S: every read of a call time-travels to the heads pinned at its start."""
    archive, _, _ = _pair(h, 1)
    [template] = h.rows(c.ARCHIVES)

    def move(count: int) -> None:
        if count == 1:
            row = dict(template, revision_id="rev1-" + "8" * 64, arrival_seq=900 * c.STRIDE)
            h.forge_rows(c.ARCHIVES, [row], "mover")

    proxy = _ReadHook(h.adapter, hook=move)
    out = c.normalizer(h, clock=StepClock(start=K_NORM), adapter=proxy).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    assert (
        proxy.reads >= 2
        and len(tuple(c.normalizer(h, clock=StepClock(start=K_NORM)).iter_revision_ids(out))) == 1
    )


# =========================================================================================
# G3-S: bounded windows over pinned snapshots
# =========================================================================================


@dataclass
class _ScanLog(ProxyCatalog):
    """Records (table, column count, rows returned) of every scan; ``hook`` runs before it."""

    scans: list[tuple[str, int, int]] = field(default_factory=list)
    hook: Any = None

    def scan_column_batches(self, table: str, **kwargs: Any) -> Any:
        if self.hook is not None:
            self.hook(table, kwargs)
        batches = self.inner.scan_column_batches(table, **kwargs)

        def tracked() -> Any:
            count = 0
            try:
                for batch in batches:
                    count += batch.num_rows
                    yield batch
            finally:
                close = getattr(batches, "close", None)
                if callable(close):
                    close()
                self.scans.append((table, len(kwargs["columns"]), count))

        return tracked()


def test_a_unit_is_proven_and_written_in_bounded_windows(h: RestHarness) -> None:
    """The pre-ADR-0108 per-batch writer (``legacy``): still the path that completes a stopped
    old unit, so its bounded per-window reads stay pinned here."""
    items = ss.agg_items(7)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE)
    log = _ScanLog(h.adapter)
    out = c.normalizer(h, clock=StepClock(start=K_NORM), adapter=log, microbatch_rows=2,
                       legacy=True).normalize_unit(c.ARCHIVE_AGGS.table, archive)  # fmt: skip
    assert out.batch_count == 4
    assert [count for _, count in _unit_batches(h, archive)] == [2, 2, 2, 1]
    wide = [(table, rows) for table, width, rows in log.scans if width > 3]
    # Canonical rows are only ever read one window (or its block slice) at a time.
    assert max(rows for table, rows in wide if table == c.TRADES.table) <= 2
    # Raw rows: the first proving window is one filtered scan (<= 2 rows); the second window
    # spools the unit once (one 7-row scan) and the remaining proving and all four writing
    # windows read that spool (E1-RAW-WINDOW-REUSE). The other 7-row read is the verifier's
    # proof of the Raw unit's one snapshot (ADR-0108: once per pinned head, then cached).
    assert [rows for table, rows in wide if table == c.ARCHIVE_AGGS.table] == [2, 7, 7]
    rows = sorted(h.rows(c.TRADES), key=lambda row: row["arrival_seq"])
    assert [row["revision_id"] for row in rows] == list(
        c.normalizer(h, clock=StepClock(start=K_NORM)).iter_revision_ids(out)
    )
    assert [row["arrival_seq"] for row in rows] == [1, 2, 3, 4, 5, 6, 7]


@pytest.mark.parametrize("microbatch_rows", [1, 2, 7, None])
def test_a_new_unit_is_one_snapshot_whatever_its_windows(
    h: RestHarness, microbatch_rows: int | None
) -> None:
    """ADR-0108 §9(a)/(b): a new unit is exactly one Canonical snapshot, whatever N and M, and its
    rows are bit for bit the rows the pre-ADR-0108 per-batch layout writes."""
    items = ss.agg_items(7)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE)
    before = len(h.history(c.TRADES.table))
    out = c.normalizer(h, clock=StepClock(start=K_NORM), microbatch_rows=microbatch_rows)\
        .normalize_unit(c.ARCHIVE_AGGS.table, archive)  # fmt: skip
    chunk = microbatch_rows or nz.DEFAULT_MICROBATCH_ROWS
    assert out.revision_count == 7 and out.batch_count == -(-7 // chunk)
    assert not out.replayed
    added = h.history(c.TRADES.table)[before:]  # oldest first
    assert [(s.batch_id, s.added_rows) for s in added] == [
        (nz.unit_commit_id(archive, 7, chunk), 7)
    ]
    rows = sorted(h.rows(c.TRADES), key=lambda row: row["arrival_seq"])
    assert [row["revision_id"] for row in rows] == list(
        c.normalizer(h, clock=StepClock(start=K_NORM)).iter_revision_ids(out)
    )
    again = c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    assert again.replayed and again.replayed_batch_count == out.batch_count
    assert len(h.history(c.TRADES.table)) == before + 1
    with ss.sqlite_harness(h.tmp_path / "legacy") as old:
        old_archive = c.ingest_archive(
            old, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE
        )
        c.normalizer(
            old, clock=StepClock(start=K_NORM), microbatch_rows=microbatch_rows, legacy=True
        ).normalize_unit(c.ARCHIVE_AGGS.table, old_archive)
        assert len(_unit_batches(old, old_archive)) == out.batch_count
        old_rows = sorted(old.rows(c.TRADES), key=lambda row: row["arrival_seq"])
    assert rows == old_rows


@dataclass
class _StagedThenCrash(ProxyCatalog):
    """Dies once a unit commit has staged every file, before its snapshot (ADR-0108 §9(c))."""

    def commit_unit(self, request: CommitRequest, batches: Any, **kwargs: Any) -> Any:
        if request.table != c.TRADES.table:
            return super().commit_unit(request, batches, **kwargs)
        passes = [0]

        def staged_then_crash() -> Any:
            passes[0] += 1
            yield from batches()
            if passes[0] >= 2:
                raise Crash("staged, not committed")

        return self.inner.commit_unit(request, staged_then_crash, **kwargs)


def test_a_crash_after_staging_leaves_nothing_and_the_rerun_commits_once(
    h: RestHarness,
) -> None:
    """ADR-0108 §9(c): staged files without their snapshot are invisible; the rerun reads a new
    clock (nothing was committed) and equals one clean run."""
    items = ss.agg_items(7)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE)
    before = (_state(h), len(h.history(c.TRADES.table)))
    with pytest.raises(Crash):
        c.normalizer(
            h, clock=StepClock(start=K_NORM), adapter=_StagedThenCrash(h.adapter), microbatch_rows=2
        ).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert (_state(h), len(h.history(c.TRADES.table))) == before
    later = StepClock(start=K_NORM + timedelta(hours=1))
    out = c.normalizer(h, clock=later, microbatch_rows=2).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    assert (
        later.calls == 1 and not out.replayed and out.knowledge_time == K_NORM + timedelta(hours=1)
    )
    assert len(h.history(c.TRADES.table)) == before[1] + 1
    with ss.sqlite_harness(h.tmp_path / "clean") as ref:
        ref_archive = c.ingest_archive(
            ref, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE
        )
        c.normalizer(
            ref, clock=StepClock(start=K_NORM + timedelta(hours=1)), microbatch_rows=2
        ).normalize_unit(c.ARCHIVE_AGGS.table, ref_archive)
        ref_rows = sorted(ref.rows(c.TRADES), key=lambda row: row["arrival_seq"])
    assert sorted(h.rows(c.TRADES), key=lambda row: row["arrival_seq"]) == ref_rows


def test_a_crash_after_the_unit_commit_replays_on_rerun(h: RestHarness) -> None:
    """ADR-0108 §9(c): a unit committed just before the process died is adopted, not rewritten."""
    items = ss.agg_items(7)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE)

    def die(request: CommitRequest, result: CommitResult) -> None:
        if request.table == c.TRADES.table:
            raise Crash("committed, not returned")

    with pytest.raises(Crash):
        c.normalizer(
            h,
            clock=StepClock(start=K_NORM),
            adapter=ProxyCatalog(h.adapter, after=die),
            microbatch_rows=2,
        ).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    committed = _state(h)
    later = StepClock(start=K_NORM + timedelta(hours=1))
    out = c.normalizer(h, clock=later, microbatch_rows=3).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    assert later.calls == 0 and out.replayed and out.knowledge_time == K_NORM
    assert out.batch_count == 4  # the committed plan's windows (M = 2), not the new setting
    assert _state(h) == committed


def test_a_unit_layout_beside_per_batch_commits_fails_closed(h: RestHarness) -> None:
    """ADR-0108 §7: one unit never mixes the two commit layouts."""
    archive = _crash_partial(h, 5, 2)
    h.forge_rows(c.TRADES, [_forged_row(h, 4)], nz.unit_commit_id(archive, 5, 2))
    before = _state(h)
    for read in ("verify_unit", "normalize_unit"):
        clock = StepClock(start=K_NORM)
        with pytest.raises(CatalogIntegrityError, match="mixes the one-snapshot unit layout"):
            getattr(c.normalizer(h, clock=clock), read)(c.ARCHIVE_AGGS.table, archive)
        assert clock.calls == 0
    assert _state(h) == before


def test_windows_and_one_window_normalize_identically(h: RestHarness) -> None:
    items = ss.agg_items(5)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE)
    [response] = c.ingest_rest(h, "agg_trades", items, knowledge=K_REST)
    small = c.normalizer(h, clock=StepClock(start=K_NORM), microbatch_rows=2)
    large = c.normalizer(h, clock=StepClock(start=K_NORM))
    a = small.normalize_unit(c.ARCHIVE_AGGS.table, archive)
    r = large.normalize_unit(c.REST_AGGS.table, response)
    rows = {row["revision_id"]: row for row in h.rows(c.TRADES)}
    for out in (a, r):
        assert (
            out.revision_count
            == len(tuple(c.normalizer(h, clock=StepClock(start=K_NORM)).iter_revision_ids(out)))
            == 5
        )
    # Same market content per key, lineage and block differ only (ADR-0028 §1).
    by_key: dict[str, set[str]] = {}
    for row in rows.values():
        by_key.setdefault(row["observation_key"], set()).add(row["payload_hash"])
    assert all(len(hashes) == 1 for hashes in by_key.values()) and len(by_key) == 5
    assert small.verify_unit(c.ARCHIVE_AGGS.table, archive) == large.verify_unit(
        c.ARCHIVE_AGGS.table, archive
    )


def test_another_writer_mid_proof_restarts_the_unit_without_double_writes(h: RestHarness) -> None:
    items = ss.agg_items(3)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE)
    [response] = c.ingest_rest(h, "agg_trades", items, knowledge=K_REST)
    rival = c.normalizer(h, clock=StepClock(start=K_NORM))
    fired: list[bool] = []

    def interleave(table: str, kwargs: Mapping[str, Any]) -> None:
        if table == c.ARCHIVE_AGGS.table and not fired and len(kwargs["columns"]) > 3:
            fired.append(True)
            rival.normalize_unit(c.REST_AGGS.table, response)  # moves canonical.trades

    clock = StepClock(start=K_NORM)
    out = c.normalizer(h, clock=clock, adapter=_ScanLog(h.adapter, hook=interleave))\
        .normalize_unit(c.ARCHIVE_AGGS.table, archive)  # fmt: skip
    assert fired and out.revision_count == 3 and out.arrival_seq_base == c.STRIDE
    assert len(h.rows(c.TRADES)) == 6
    # The first commit lost its expected parent before anything of the unit was committed, so
    # the whole unit restarted with a new block and a new reading (ADR-0028 §6: the old reading
    # was never persisted); the rival's rows are untouched and nothing is written twice.
    assert clock.calls == 2


def test_proof_windows_never_split_a_position_and_cover_all() -> None:
    assert list(nz._proof_windows([1, 2, 2, 3, 4, 5], 2)) == [
        (1, 2, 3),
        (3, 4, 2),
        (5, 5, 1),
    ]
    assert list(nz._proof_windows([1, 1, 1], 2)) == [(1, 1, 3)]
    assert list(nz._proof_windows([], 2)) == []
    assert list(nz._proof_windows([3, 9, 40], 25_000)) == [(3, 40, 3)]


@pytest.mark.parametrize(
    ("values", "expected", "ok"),
    [
        ([1, 2, 3], [1, 2, 3], True),
        ([3, 1, 2], [1, 2, 3], True),
        ([4, 2], [2, 4], True),  # a REST unit's positions may have gaps
        ([1, 2, 2], [1, 2, 3], False),
        ([1, 2, 4], [1, 2, 3], False),
        ([1, 2], [1, 2, 3], False),
        ([], [], True),
        ([None, 2, 3], [1, 2, 3], False),
    ],
)
def test_same_index_numbers(
    values: list[int | None], expected: list[int], ok: bool, tmp_path: Path
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    index = nz._PositionIndex(scratch)
    try:
        index.add_batch(value for value in values if value is not None)
        index.finalize()
        assert (
            nz._same_index_numbers(
                index, len(values), any(value is None for value in values), expected
            )
            is ok
        )
    finally:
        index.close()


def test_position_index_keeps_database_and_ordered_read_under_explicit_scratch(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    index = nz._PositionIndex(scratch)
    child = Path(index._temporary.name)
    try:
        index.add_batch((9, 2, 5, 1))
        plan = index._connection.execute(
            "EXPLAIN QUERY PLAN SELECT position FROM positions ORDER BY position"
        ).fetchone()
        assert child.parent == scratch
        assert sorted(path.name for path in child.iterdir()) == ["positions.sqlite3"]
        assert plan is not None and "USING COVERING INDEX positions_position" in plan[3]
        index.finalize()
        assert sorted(path.name for path in child.iterdir()) == [
            "positions.sqlite3",
            "ranks.bin",
        ]
        assert list(index) == [1, 2, 5, 9]
    finally:
        index.close()
    assert list(scratch.iterdir()) == []


def _raw_spool_batch(lines: list[int], revisions: list[str]) -> Any:
    import pyarrow as pa  # type: ignore[import-untyped]

    return pa.record_batch(
        [pa.array(lines, pa.int64()), pa.array(revisions, pa.string())],
        names=["archive_line_number", "revision_id"],
    )


def test_raw_window_spool_reuses_one_scan_across_resliced_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel = rules.raw_channel_of(c.ARCHIVE_AGGS.table)
    monkeypatch.setattr(nz, "_RAW_SPOOL_BATCH_ROWS", 2)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    batch = _raw_spool_batch([5, 1, 4, 2, 3, 3], ["e", "a", "d", "b", "c2", "c1"])
    spool = nz._RawWindowSpool(scratch, batch.schema, "archive_line_number")
    child = Path(spool._temporary.name)
    try:
        spool.add_batch(batch)
        spool.add_batch(_raw_spool_batch([6], ["f"]))
        spool.finalize()
        # One 6-row scan batch is spooled as three 2-row IPC batches, plus the 1-row batch.
        assert spool._batch_count == 4
        assert child.parent == scratch
        window = spool.window(2, 4, expected_rows=4, channel=channel, display_low=2, display_high=4)
        assert [(row["archive_line_number"], row["revision_id"]) for row in window] == [
            (2, "b"),
            (3, "c1"),
            (3, "c2"),
            (4, "d"),
        ]
        # Windows are reread from disk, in any order, without another source scan.
        assert [
            row["revision_id"]
            for row in spool.window(
                5, 6, expected_rows=2, channel=channel, display_low=5, display_high=6
            )
        ] == ["e", "f"]
        with pytest.raises(CatalogIntegrityError, match="Raw window 2..4 contains extra rows"):
            spool.window(2, 4, expected_rows=3, channel=channel, display_low=2, display_high=4)
        with pytest.raises(CatalogIntegrityError, match="has 1 rows; expected 2"):
            spool.window(6, 7, expected_rows=2, channel=channel, display_low=6, display_high=7)
    finally:
        spool.close()
    assert not child.exists()
    assert list(scratch.iterdir()) == []
    with pytest.raises(RuntimeError, match="not readable"):
        spool.window(1, 1, expected_rows=1, channel=channel, display_low=1, display_high=1)


def test_raw_windows_scan_one_window_then_spool_the_unit_once(h: RestHarness) -> None:
    items = ss.agg_items(7, ms_step=ss.MINUTE_MS)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE)
    channel = rules.raw_channel_of(c.ARCHIVE_AGGS.table)
    n = c.normalizer(h, clock=StepClock(start=K_NORM), microbatch_rows=2)
    pin = n._pin(channel, archive)
    log = _ScanLog(h.adapter)
    logged = nz._Pin(log, pin.canonical_head, pin.verifier)  # type: ignore[arg-type]
    scratch = h.canonical_scratch_directory
    baseline = set(scratch.iterdir())
    positions, windows, _ = n._positions(logged, channel, archive)
    try:
        before = set(scratch.iterdir())
        first = windows.window(logged, 3, 4, expected_rows=2)
        assert not windows.spooled and set(scratch.iterdir()) == before
        later = [windows.window(logged, low, low + 1, expected_rows=2) for low in (1, 5, 3)]
        assert windows.spooled
        assert [row["archive_line_number"] for row in first] == [3, 4]
        assert [[row["archive_line_number"] for row in rows] for rows in later] == [
            [1, 2],
            [5, 6],
            [3, 4],
        ]
        # Spooled rows are the rows the filtered window scan returns.
        assert later[2] == first
        assert windows.window(logged, 7, 7, expected_rows=1)[0]["archive_line_number"] == 7
        with pytest.raises(CatalogIntegrityError, match="has 1 rows; expected 2"):
            windows.window(logged, 7, 8, expected_rows=2)
        wide = [rows for table, width, rows in log.scans if width > 3]
        # One 2-row window scan, then one 7-row unit spool; no scan per later window.
        assert wide == [2, 7]
    finally:
        positions.close()
        windows.close()
        windows.close()
    assert set(scratch.iterdir()) == baseline
    with pytest.raises(RuntimeError, match="closed"):
        windows.window(logged, 1, 2, expected_rows=2)


def test_raw_windows_release_the_spool_when_the_unit_scan_fails(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = ss.agg_items(5, ms_step=ss.MINUTE_MS)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE)
    channel = rules.raw_channel_of(c.ARCHIVE_AGGS.table)
    n = c.normalizer(h, clock=StepClock(start=K_NORM), microbatch_rows=2)
    pin = n._pin(channel, archive)
    positions, windows, _ = n._positions(pin, channel, archive)
    scratch = h.canonical_scratch_directory
    before = set(scratch.iterdir())

    def broken(self: Any, record_batch: Any) -> None:
        raise OSError("scratch full")

    monkeypatch.setattr(nz._RawWindowSpool, "add_batch", broken)
    try:
        windows.window(pin, 1, 2, expected_rows=2)
        with pytest.raises(OSError, match="scratch full"):
            windows.window(pin, 3, 4, expected_rows=2)
        assert not windows.spooled
        assert set(scratch.iterdir()) == before
    finally:
        positions.close()
        windows.close()


def test_per_call_pins_close_their_verifiers_on_success_and_failure(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = ss.agg_items(5, ms_step=ss.MINUTE_MS)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE)
    opened: list[Any] = []
    closed: list[Any] = []

    class Tracked(PersistedRowVerifier):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            opened.append(self)

        def close(self) -> None:
            closed.append(self)
            super().close()

    monkeypatch.setattr(nz, "PersistedRowVerifier", Tracked)
    n = c.normalizer(h, clock=StepClock(start=K_NORM), microbatch_rows=2)
    out = n.normalize_unit(c.ARCHIVE_AGGS.table, archive)
    list(n.iter_revision_ids(out))
    n.verify_unit(c.ARCHIVE_AGGS.table, archive)
    n.verify_unit(c.ARCHIVE_AGGS.table, archive, arrival_seqs={1})
    assert opened and {id(v) for v in opened} == {id(v) for v in closed}

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("survey failed")

    monkeypatch.setattr(nz.CanonicalNormalizer, "_survey", fail)
    monkeypatch.setattr(nz.CanonicalNormalizer, "_unit_facts", fail)
    with pytest.raises(OSError, match="survey failed"):
        n.normalize_unit(c.ARCHIVE_AGGS.table, archive)
    with pytest.raises(OSError, match="survey failed"):
        n.verify_unit(c.ARCHIVE_AGGS.table, archive)
    with pytest.raises(OSError, match="survey failed"):
        n.verify_unit(c.ARCHIVE_AGGS.table, archive, arrival_seqs={1})
    assert len(opened) > 4 and {id(v) for v in opened} == {id(v) for v in closed}


def test_raw_window_spool_rejects_null_positions_and_releases_scratch(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    batch = _raw_spool_batch([1], ["a"])
    spool = nz._RawWindowSpool(scratch, batch.schema, "archive_line_number")
    try:
        with pytest.raises(CatalogIntegrityError, match="Raw position is null"):
            spool.add_batch(_raw_spool_batch([None], ["b"]))  # type: ignore[list-item]
        with pytest.raises(RuntimeError, match="not readable"):
            spool.window(
                1,
                1,
                expected_rows=1,
                channel=rules.raw_channel_of(c.ARCHIVE_AGGS.table),
                display_low=1,
                display_high=1,
            )
    finally:
        spool.close()
        spool.close()
    assert list(scratch.iterdir()) == []
    with pytest.raises(RuntimeError, match="finalized"):
        spool.add_batch(batch)


def test_normalizer_refuses_unusable_scratch_before_any_catalog_access(tmp_path: Path) -> None:
    not_a_directory = tmp_path / "scratch-file"
    not_a_directory.write_text("occupied", encoding="utf-8")
    with pytest.raises(CanonicalNormalizeError, match="scratch directory is not usable"):
        CanonicalNormalizer(object(), object(), scratch_directory=not_a_directory)  # type: ignore[arg-type]

    first = tmp_path / "loop-a"
    second = tmp_path / "loop-b"
    first.symlink_to(second)
    second.symlink_to(first)
    with pytest.raises(CanonicalNormalizeError, match="scratch directory is not usable"):
        CanonicalNormalizer(object(), object(), scratch_directory=first)  # type: ignore[arg-type]


def test_positions_closes_reader_when_scratch_index_initialization_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Reader:
        closed = False

        def __iter__(self) -> Any:
            raise AssertionError("reader must not be consumed if index initialization fails")

        def close(self) -> None:
            self.closed = True

    reader = Reader()

    class Catalog:
        def scan_column_batches(self, *_args: Any, **_kwargs: Any) -> Reader:
            return reader

    def fail_index(_scratch_directory: Path) -> Any:
        raise OSError("scratch index initialization failed")

    monkeypatch.setattr(nz, "_PositionIndex", fail_index)
    normalizer = CanonicalNormalizer(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        scratch_directory=tmp_path / "scratch",
    )
    pin = type("Pin", (), {"catalog": Catalog()})()

    with pytest.raises(OSError, match="scratch index initialization failed"):
        normalizer._positions(pin, rules.raw_channel_of(c.ARCHIVE_AGGS.table), "unit")

    assert reader.closed


def test_batch_windows_are_rank_slices_of_the_positions() -> None:
    positions = [2, 3, 5, 8, 9]
    assert [nz._batch_window(positions, 2, i) for i in range(3)] == [
        (2, 3, 2),
        (5, 8, 4),
        (9, 9, 5),
    ]


# =========================================================================================
# G3-S-R1: review E
# =========================================================================================


def test_a_rest_unit_lacking_positions_another_page_delivered_is_normalized(
    h: RestHarness,
) -> None:
    """Review E-1: page B re-delivers trades 101..102 first delivered by page A and owns only
    trade 103 (element_index 2); its positions have a gap and it is still lawful."""
    cs.queue_agg_chain(h.venue, SYMBOL, ss.T0, [ss.agg_items(3, first_id=100)])
    cs.queue_agg_chain(
        h.venue, SYMBOL, ss.T0 + 1, [ss.agg_items(3, first_id=101, first_ms=ss.T0 + 1)]
    )
    h.collect(ss.agg_request("req-a"))
    h.collect(ss.agg_request("req-b", start_ms=ss.T0 + 1))
    store = h.store(clock=StepClock(start=K_REST))
    a = store.ingest_collection(ss.agg_request("req-a")).pages[0].response_revision_id
    b = store.ingest_collection(ss.agg_request("req-b", start_ms=ss.T0 + 1)).pages[0]
    owned = [
        r["element_index"]
        for r in h.rows(c.REST_AGGS)
        if r["response_revision_id"] == b.response_revision_id
    ]
    assert owned == [2]
    n = c.normalizer(h, clock=StepClock(start=K_NORM), microbatch_rows=1)
    assert n.normalize_unit(c.REST_AGGS.table, a).revision_count == 3
    out = n.normalize_unit(c.REST_AGGS.table, b.response_revision_id)
    [revision_id] = n.iter_revision_ids(out)
    [row] = [r for r in h.rows(c.TRADES) if r["revision_id"] == revision_id]
    assert out.arrival_seq_base is not None
    assert row["arrival_seq"] == out.arrival_seq_base + 3 and row["venue_trade_id"] == "103"
    assert n.normalize_unit(c.REST_AGGS.table, b.response_revision_id).replayed
    assert [r["revision_id"] for r in n.verify_unit(c.REST_AGGS.table, b.response_revision_id)] == [
        revision_id
    ]


@pytest.mark.parametrize(
    ("legacy", "match"),
    [
        (True, "not exactly the 5 lines of its object"),
        # ADR-0108: a one-snapshot Raw unit is proven whole first (its unit fingerprint).
        (False, "committed with other content"),
    ],
)
def test_an_archive_unit_missing_its_last_lines_is_truncated(
    h: RestHarness, legacy: bool, match: str
) -> None:
    """Review E-3: an archive revision's rows are all the lines of its object."""
    arc = rs.archive(
        h.storage,
        data_type="agg_trades",
        symbol=SYMBOL,
        day=ss.DAY,
        rows=ss.archive_agg_lines(ss.agg_items(5)),
        retrieved_at=c.ARCHIVE_RETRIEVED,
        request_id="archive-1",
    )
    ingested = RawRevisionStore(
        h.adapter,
        h.storage,
        clock=StepClock(start=K_ARCHIVE),
        microbatch_rows=2,
        _legacy_batch_commits=legacy,
    ).ingest(arc.collected, arc.context)
    assert isinstance(ingested, ArchiveIngested), ingested
    archive = ingested.archive_revision_id
    h.delete_rows(c.ARCHIVE_AGGS, GreaterThanOrEqual("archive_line_number", 5))  # type: ignore[call-arg, arg-type]
    clock = StepClock(start=K_NORM)
    with pytest.raises(CatalogIntegrityError, match=match):
        c.normalizer(h, clock=clock).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert clock.calls == 0 and h.rows(c.TRADES) == []


def test_a_replay_reads_the_committed_block_once_after_its_first_window(h: RestHarness) -> None:
    """E1-CANONICAL-WINDOW-REUSE: the first window keeps its two filtered Canonical reads;
    the block slice and the symbol's (revision_id, time) span are then read once for the rest."""
    items = ss.agg_items(7, ms_step=ss.MINUTE_MS)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE)
    c.normalizer(h, clock=StepClock(start=K_NORM), microbatch_rows=2).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    columns: list[tuple[str, ...]] = []
    log = _ScanLog(
        h.adapter,
        hook=lambda table, kwargs: (
            columns.append(tuple(kwargs["columns"])) if table == c.TRADES.table else None
        ),
    )
    scratch = h.canonical_scratch_directory
    before = set(scratch.iterdir())
    replay = c.normalizer(h, clock=StepClock(start=K_NORM), adapter=log, microbatch_rows=2)
    out = replay.normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert out.replayed_batch_count == out.batch_count == 4
    wide = len(c.TRADES.arrow_schema)
    trades = [rows for table, _, rows in log.scans if table == c.TRADES.table]
    full = [rows for cols, rows in zip(columns, trades, strict=True) if len(cols) == wide]
    # ADR-0108 unit layout: windows are proven in row order (the unit fingerprint is re-derived
    # as they go), so one 2-row window read (window 0), then one 7-row block read for 1, 2, 3
    assert full == [2, 7]
    assert columns.count(("revision_id",)) == 1  # batch 3's own uniqueness read
    assert columns.count(("revision_id", "event_time")) == 1  # the indexed span, once
    replay.close()
    assert set(scratch.iterdir()) == before


@pytest.mark.parametrize("index", [6, 1], ids=["first-checked-window", "spooled-window"])
def test_a_replay_refuses_a_copy_in_any_window(h: RestHarness, index: int) -> None:
    """A copy of a committed row is refused whether its window is read directly or from the
    spooled block / indexed span (batches are checked newest first)."""
    items = ss.agg_items(7, ms_step=ss.MINUTE_MS)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE)
    n = c.normalizer(h, clock=StepClock(start=K_NORM), microbatch_rows=2)
    n.normalize_unit(c.ARCHIVE_AGGS.table, archive)
    row = sorted(h.rows(c.TRADES), key=lambda item: item["arrival_seq"])[index]
    copy = dict(row, arrival_seq=row["arrival_seq"] + 9 * c.STRIDE, lineage_source_revision_id="x")
    h.forge_rows(c.TRADES, [copy], "copy")
    with pytest.raises(CatalogIntegrityError, match="not held by exactly one row"):
        n.normalize_unit(c.ARCHIVE_AGGS.table, archive)


@pytest.mark.parametrize("index", [6, 1], ids=["first-checked-window", "spooled-window"])
def test_a_replay_refuses_a_changed_row_in_any_window(h: RestHarness, index: int) -> None:
    items = ss.agg_items(7, ms_step=ss.MINUTE_MS)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE)
    n = c.normalizer(h, clock=StepClock(start=K_NORM), microbatch_rows=2)
    n.normalize_unit(c.ARCHIVE_AGGS.table, archive)
    row = sorted(h.rows(c.TRADES), key=lambda item: item["arrival_seq"])[index]
    h.delete_rows(c.TRADES, EqualTo("revision_id", row["revision_id"]))  # type: ignore[call-arg, arg-type]
    h.forge_rows(c.TRADES, [dict(row, price=row["price"] + 1)], "changed")
    with pytest.raises(CatalogIntegrityError, match="disagrees with its re-normalized row"):
        n.normalize_unit(c.ARCHIVE_AGGS.table, archive)


def test_a_replay_rechecks_revision_id_uniqueness(h: RestHarness) -> None:
    """Review E-2: an exact copy of a committed row (another block, same time) is refused on
    replay, as on the first run's read-back."""
    archive, _, _ = _pair(h)
    n = c.normalizer(h, clock=StepClock(start=K_NORM), microbatch_rows=2)
    n.normalize_unit(c.ARCHIVE_AGGS.table, archive)
    row = sorted(h.rows(c.TRADES), key=lambda item: item["arrival_seq"])[1]
    copy = dict(row, arrival_seq=row["arrival_seq"] + 9 * c.STRIDE, lineage_source_revision_id="x")
    h.forge_rows(c.TRADES, [copy], "copy")
    with pytest.raises(CatalogIntegrityError, match="not held by exactly one row"):
        n.normalize_unit(c.ARCHIVE_AGGS.table, archive)


# =========================================================================================
# G3-S2: proving only the committed batches a reader reads
# =========================================================================================


def _seven(h: RestHarness) -> tuple[str, list[dict[str, Any]]]:
    """A 7-row archive unit normalized as batches of 2 (positions 1-2, 3-4, 5-6, 7)."""
    items = ss.agg_items(7, ms_step=ss.MINUTE_MS)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE)
    c.normalizer(h, clock=StepClock(start=K_NORM), microbatch_rows=2).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    return archive, sorted(h.rows(c.TRADES), key=lambda row: row["arrival_seq"])


def test_a_restricted_verification_returns_the_windows_it_proves(h: RestHarness) -> None:
    archive, rows = _seven(h)
    n = c.normalizer(h, clock=StepClock(start=K_NORM))
    full = n.verify_unit(c.ARCHIVE_AGGS.table, archive)
    assert [row["revision_id"] for row in full] == [row["revision_id"] for row in rows]
    # arrival_seq 3 lies in the second batch (positions 3-4); 7 in the last one.
    part = n.verify_unit(c.ARCHIVE_AGGS.table, archive, arrival_seqs={3, 7})
    assert [row["arrival_seq"] for row in part] == [3, 4, 7]
    assert all(row == full[row["arrival_seq"] - 1] for row in part)
    # A number outside the unit's block proves nothing and returns nothing.
    assert n.verify_unit(c.ARCHIVE_AGGS.table, archive, arrival_seqs={c.STRIDE + 1}) == ()


def test_restricted_verification_reuses_builtin_set_without_mutating_it(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, _ = _seven(h)
    n = c.normalizer(h, clock=StepClock(start=K_NORM))
    requested = {3, 7}
    before = set(requested)
    seen: list[Any] = []
    original = n._verify_batches

    def observe(pin: Any, channel: Any, source_revision_id: str, seqs: Any) -> Any:
        seen.append(seqs)
        return original(pin, channel, source_revision_id, seqs)

    monkeypatch.setattr(n, "_verify_batches", observe)
    result = n.verify_unit(c.ARCHIVE_AGGS.table, archive, arrival_seqs=requested)

    assert seen == [requested]
    assert seen[0] is requested
    assert requested == before
    assert [row["arrival_seq"] for row in result] == [3, 4, 7]

    iterable_result = n.verify_unit(
        c.ARCHIVE_AGGS.table, archive, arrival_seqs=(seq for seq in (3, 7))
    )
    assert type(seen[1]) is frozenset
    assert seen[1] == before
    assert [row["arrival_seq"] for row in iterable_result] == [3, 4, 7]


def test_a_restricted_verification_still_checks_the_whole_unit(h: RestHarness) -> None:
    archive, rows = _seven(h)
    # Delete a committed row of the last batch; a reader of the first batch must still refuse.
    h.delete_rows(c.TRADES, EqualTo("revision_id", rows[-1]["revision_id"]))  # type: ignore[call-arg, arg-type]
    n = c.normalizer(h, clock=StepClock(start=K_NORM))
    with pytest.raises(CatalogIntegrityError, match="rows deleted or added"):
        n.verify_unit(c.ARCHIVE_AGGS.table, archive, arrival_seqs={1})


def test_a_restricted_verification_proves_the_raw_rows_it_reads(h: RestHarness) -> None:
    archive, _ = _seven(h)
    raw = sorted(h.rows(c.ARCHIVE_AGGS), key=lambda row: row["archive_line_number"])
    forged = dict(raw[3], price=raw[3]["price"] + 1)  # line 4: the second batch
    h.delete_rows(c.ARCHIVE_AGGS, EqualTo("revision_id", raw[3]["revision_id"]))  # type: ignore[call-arg, arg-type]
    h.forge_rows(c.ARCHIVE_AGGS, [forged], "corruption")
    n = c.normalizer(h, clock=StepClock(start=K_NORM))
    with pytest.raises(CatalogIntegrityError):
        n.verify_unit(c.ARCHIVE_AGGS.table, archive, arrival_seqs={4})
    with pytest.raises(CatalogIntegrityError):
        n.verify_unit(c.ARCHIVE_AGGS.table, archive)


# =========================================================================================
# G2-R1a: readers take a normalized unit only whole (RT-1)
# =========================================================================================


def test_readers_refuse_a_unit_whose_normalization_stopped_half_way(h: RestHarness) -> None:
    """RT-1: a committed prefix of the plan is a normalization that stopped, not a smaller unit;
    the full and the restricted reader refuse it until a rerun commits the rest."""
    archive = _crash_partial(h, 5, 2)
    committed = sorted(h.rows(c.TRADES), key=lambda row: row["arrival_seq"])
    assert len(committed) == 2
    n = c.normalizer(h, clock=StepClock(start=K_NORM))
    with pytest.raises(CanonicalUnitIncomplete, match="1 of the 3 batches"):
        n.verify_unit(c.ARCHIVE_AGGS.table, archive)
    with pytest.raises(CanonicalUnitIncomplete, match="1 of the 3 batches"):
        n.verify_unit(c.ARCHIVE_AGGS.table, archive, arrival_seqs={committed[0]["arrival_seq"]})
    out = c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    full = n.verify_unit(c.ARCHIVE_AGGS.table, archive)
    assert [row["revision_id"] for row in full] == list(n.iter_revision_ids(out))
    part = n.verify_unit(c.ARCHIVE_AGGS.table, archive, arrival_seqs={committed[0]["arrival_seq"]})
    assert part == full[:2]


# =========================================================================================
# G2-R3a: the page completeness check reads narrow columns in chunks
# =========================================================================================


def _unbounded_check_rest_unit(
    pin: Any, channel: rules.RawChannel, source_revision_id: str, positions: Sequence[int]
) -> None:
    """The pre-G2-R3a ``_check_rest_unit`` verbatim: full rows of every history row per key."""
    table = channel.element.table
    response, elements = pin.verifier.page_elements(channel.data_type, source_revision_id)
    own = {position - 1 for position in positions}
    if not own <= {element.element_index for element in elements}:
        raise CatalogIntegrityError(
            f"{table}: unit {source_revision_id} holds positions its body does not"
        )
    missing = [element for element in elements if element.element_index not in own]
    if not missing:
        return
    held: dict[str, list[Mapping[str, Any]]] = {}
    columns = tuple(field.name for field in channel.element.arrow_schema)
    keys = sorted({element.observation_key for element in missing})
    for start in range(0, len(keys), nz._KEY_CHUNK):
        for row in pin.catalog.scan_columns(
            table,
            columns=columns,
            row_filter=And(
                nz._equals("symbol", response["symbol"]),
                In("observation_key", keys[start : start + nz._KEY_CHUNK]),  # type: ignore[call-arg, arg-type]
            ),
        ).to_pylist():
            held.setdefault(row["revision_id"], []).append(row)
    unheld = [
        element.element_index
        for element in missing
        if len(held.get(element.revision_id, ())) != 1
        or held[element.revision_id][0]["response_revision_id"] == source_revision_id
    ]
    if unheld:
        raise CanonicalUnitIncomplete(
            f"{table}: response revision {source_revision_id} holds {len(elements)} "
            f"element(s) but element(s) {unheld[:8]} are committed neither under it nor "
            "under another page: its element batches stopped half-way (rerun the store)"
        )
    pin.verifier.verify_rest_elements(
        channel.element,
        channel.data_type,
        [held[element.revision_id][0] for element in missing],
    )


@dataclass
class _SpyVerifier:
    """Delegates to the real verifier; records the rows each ``verify_rest_elements`` proves."""

    inner: Any
    proved: list[list[Mapping[str, Any]]] = field(default_factory=list)

    def page_elements(self, data_type: str, response_revision_id: str) -> Any:
        return self.inner.page_elements(data_type, response_revision_id)

    def verify_rest_elements(self, definition: Any, data_type: str, rows: Any) -> None:
        self.proved.append([dict(row) for row in rows])
        self.inner.verify_rest_elements(definition, data_type, rows)


def _page_verdicts(h: RestHarness, units: list[str]) -> list[tuple[Any, ...]]:
    """``(unit, check, outcome, rows proven)`` of the bounded and the unbounded check."""
    channel = rules.raw_channel_of(c.REST_AGGS.table)
    n = c.normalizer(h, clock=StepClock(start=K_NORM))
    verdicts: list[tuple[Any, ...]] = []
    for unit in units:
        pin = n._pin(channel, unit)
        positions, raw_rows, _ = n._positions(pin, channel, unit)
        for check in ("bounded", "unbounded"):
            spy = _SpyVerifier(pin.verifier)
            spied = nz._Pin(pin.catalog, pin.canonical_head, spy)  # type: ignore[arg-type]
            try:
                if check == "bounded":
                    n._check_rest_unit(spied, channel, unit, positions)
                else:
                    _unbounded_check_rest_unit(spied, channel, unit, positions)
                outcome: tuple[str, str] = ("ok", "")
            except CatalogIntegrityError as exc:
                outcome = (type(exc).__name__, str(exc))
            verdicts.append((unit, check, outcome, spy.proved))
        positions.close()
        raw_rows.close()
    return verdicts


def _same_verdicts(verdicts: list[tuple[Any, ...]]) -> dict[str, tuple[Any, ...]]:
    by_unit: dict[str, dict[str, tuple[Any, ...]]] = {}
    for unit, check, outcome, proved in verdicts:
        by_unit.setdefault(unit, {})[check] = (outcome, proved)
    for unit, checks in by_unit.items():
        assert checks["bounded"] == checks["unbounded"], unit
    return {unit: checks["bounded"] for unit, checks in by_unit.items()}


@pytest.mark.parametrize("key_chunk", [1, 2, nz._KEY_CHUNK])
def test_the_bounded_page_check_keeps_the_unbounded_verdicts(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch, key_chunk: int
) -> None:
    """G2-R3a: narrow columns and chunked read-backs give the old verdict and prove the same
    rows — on a complete page, a page two of whose elements another page delivered, a page
    that stopped half-way, other history rows of the keys, and a holder committed twice."""
    monkeypatch.setattr(nz, "_KEY_CHUNK", key_chunk)
    cs.queue_agg_chain(h.venue, SYMBOL, ss.T0, [ss.agg_items(3, first_id=100)])
    cs.queue_agg_chain(
        h.venue, SYMBOL, ss.T0 + 1, [ss.agg_items(3, first_id=101, first_ms=ss.T0 + 1)]
    )
    cs.queue_agg_chain(
        h.venue, SYMBOL, ss.T0 + 10, [ss.agg_items(3, first_id=110, first_ms=ss.T0 + 10)]
    )
    h.collect(ss.agg_request("req-a"))
    h.collect(ss.agg_request("req-b", start_ms=ss.T0 + 1))
    h.collect(ss.agg_request("req-c", start_ms=ss.T0 + 10))
    store = h.store(clock=StepClock(start=K_REST))
    a = store.ingest_collection(ss.agg_request("req-a")).pages[0].response_revision_id
    b = store.ingest_collection(ss.agg_request("req-b", start_ms=ss.T0 + 1))
    b_id = b.pages[0].response_revision_id
    proxy = ProxyCatalog(h.adapter, after=ss.crash_after_commits(1, table=c.REST_AGGS.table))
    with pytest.raises(Crash):
        h.store(clock=StepClock(start=K_REST), adapter=proxy, element_microbatch_rows=1)\
            .ingest_collection(ss.agg_request("req-c", start_ms=ss.T0 + 10))  # fmt: skip
    [partial] = {row["revision_id"] for row in h.rows(c.RESPONSES)} - {a, b_id}
    # Other revisions of B's foreign keys (never holders of its elements) sit in the history.
    foreign = sorted(
        (row for row in h.rows(c.REST_AGGS) if row["response_revision_id"] == a),
        key=lambda row: row["element_index"],
    )[1:]
    noise = [
        dict(
            row,
            revision_id=f"noise-{i}",
            response_revision_id="elsewhere",
            arrival_seq=row["arrival_seq"] + 9 * c.STRIDE,
        )
        for i, row in enumerate(foreign)
    ]
    h.forge_rows(c.REST_AGGS, noise, "noise")

    verdicts = _same_verdicts(_page_verdicts(h, [a, b_id, partial]))
    assert verdicts[a] == (("ok", ""), [])
    outcome, proved = verdicts[b_id]
    assert outcome == ("ok", "")
    assert [[row["revision_id"] for row in rows] for rows in proved] == [
        [row["revision_id"] for row in foreign]
    ]
    assert verdicts[partial][0][0] == "CanonicalUnitIncomplete"
    assert verdicts[partial][1] == []

    # A holder committed twice (a copy under another lineage) leaves B's element unheld.
    h.forge_rows(c.REST_AGGS, [dict(foreign[0], response_revision_id="elsewhere")], "copy")
    outcome, proved = _same_verdicts(_page_verdicts(h, [b_id]))[b_id]
    assert outcome[0] == "CanonicalUnitIncomplete" and proved == []


@dataclass
class _ForgedLayout(ProxyCatalog):
    """Reports another commit layout for the Canonical trades table's snapshots."""

    layout: CommitLayout = field(default_factory=lambda: CommitLayout(unit=False, window_rows=None))

    def commit_layout(self, table: str, snapshot_id: str) -> CommitLayout:
        if table == c.TRADES.table:
            return self.layout
        return super().commit_layout(table, snapshot_id)


@pytest.mark.parametrize(
    "layout",
    [CommitLayout(unit=False, window_rows=None), CommitLayout(unit=True, window_rows=3)],
    ids=["no-layout-marker", "other-window"],
)
def test_a_unit_id_without_its_layout_marker_fails_closed(
    h: RestHarness, layout: CommitLayout
) -> None:
    """ADR-0108 §8: a ``.unit`` batch id must be backed by the summary's unit layout and the
    window size the id records."""
    items = ss.agg_items(5)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE)
    c.normalizer(h, clock=StepClock(start=K_NORM), microbatch_rows=2).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    before = _state(h)
    for read in ("verify_unit", "normalize_unit"):
        clock = StepClock(start=K_NORM)
        forged = _ForgedLayout(h.adapter, layout=layout)
        with pytest.raises(CatalogIntegrityError, match="does not record the unit commit layout"):
            getattr(c.normalizer(h, clock=clock, adapter=forged), read)(
                c.ARCHIVE_AGGS.table, archive
            )
        assert clock.calls == 0
    assert _state(h) == before

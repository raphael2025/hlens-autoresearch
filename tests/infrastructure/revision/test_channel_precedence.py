"""D-33 option A: ``binance.spot.delivery-channel@1.0.0`` (Phase 1 D3B; ADR-0027 §4).

Pure evidence for the archive ↔ REST content comparison: exact projections, every field,
missing / out-of-domain values, unit normalisation (millisecond ↔ microsecond), storage
integrity, the time-free edge identity, the caller-supplied edge knowledge time and the evidence
contents. Rows are built with the real identity rules of each channel, so "equal" here means the
two persisted revisions really re-derive their own identities.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from core.contracts.revision import (
    AvailabilityDecision,
    ObservationTimes,
    PolicyRole,
    RevisionGraph,
    RevisionRecord,
)
from core.domain.base import canonical_json
from infrastructure.catalog.phase1_tables import BINANCE_SPOT_PRECEDENCE_EVIDENCE
from infrastructure.revision import channel_precedence, rest_identity
from infrastructure.revision import identity as archive_identity
from infrastructure.revision.channel_precedence import (
    AGG_TRADE_PROJECTION,
    DELIVERY_CHANNEL_BINDING,
    DELIVERY_CHANNEL_HASH,
    DELIVERY_CHANNEL_SPEC,
    KLINE_1M_PROJECTION,
    POLICY_STATEMENT,
    Channel,
    ChannelComparison,
    ChannelPrecedenceViolation,
    ChannelRevision,
    ComparisonOutcome,
    build_channel_edge,
    compare_channels,
    project,
)
from infrastructure.revision.precedence import maximal_heads
from infrastructure.revision.rest_availability import (
    RestAvailabilitySubject,
    decide_rest_availability,
)
from tests.infrastructure.catalog.phase1_support import batch_for, policy

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
T_2024 = datetime(2024, 12, 31, 23, 58, tzinfo=UTC)  # archive declared in milliseconds
T_2025 = datetime(2025, 1, 1, 0, 5, tzinfo=UTC)  # archive declared in microseconds
K_ARCHIVE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
K_REST = datetime(2026, 9, 25, 6, 0, tzinfo=UTC)
ARCHIVE_REVISION = "rev1-" + "d" * 64  # the archive file revision the rows were parsed from
FACTOR = {"millisecond": 1000, "microsecond": 1}


def us(value: datetime) -> int:
    return (value - EPOCH) // timedelta(microseconds=1)


def at_us(micros: int) -> datetime:
    return EPOCH + timedelta(microseconds=micros)


def agg_natives(when: datetime, unit: str, **changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "agg_trade_id": 26129,
        "price": Decimal("93712.01"),
        "quantity": Decimal("0.0001"),
        "first_trade_id": 27781,
        "last_trade_id": 27783,
        "timestamp_raw": us(when) // FACTOR[unit],
        "is_buyer_maker": True,
        "is_best_match": True,
    }
    fields.update(changes)
    return fields


def kline_natives(start: datetime, unit: str, **changes: Any) -> dict[str, Any]:
    open_raw = us(start) // FACTOR[unit]
    minute = 60_000_000 // FACTOR[unit]
    fields: dict[str, Any] = {
        "open_time_raw": open_raw,
        "open": Decimal("93700.00"),
        "high": Decimal("93750.50"),
        "low": Decimal("93690.10"),
        "close": Decimal("93712.01"),
        "volume": Decimal("12.345678"),
        "close_time_raw": open_raw + minute - 1,
        "quote_asset_volume": Decimal("1157000.12345678"),
        "number_of_trades": 321,
        "taker_buy_base_asset_volume": Decimal("6.1"),
        "taker_buy_quote_asset_volume": Decimal("571600.5"),
        "ignore_raw": "0",
    }
    fields.update(changes)
    return fields


def archive_scale(natives: dict[str, Any]) -> dict[str, Any]:
    """Decimals as read back from ``decimal(38, 18)`` Iceberg columns (18 fractional digits)."""
    return {
        name: value.quantize(Decimal(1).scaleb(-18)) if isinstance(value, Decimal) else value
        for name, value in natives.items()
    }


def revision(
    channel: Channel,
    data_type: str,
    natives: dict[str, Any],
    *,
    unit: str,
    symbol: str = "BTCUSDT",
    times: dict[str, Any] | None = None,
    **overrides: Any,
) -> ChannelRevision:
    """A persisted element revision whose identity really derives from its row."""
    row: dict[str, Any] = {"symbol": symbol, **natives}
    factor = FACTOR[unit]
    if data_type == "agg_trades":
        row["event_time"] = at_us(natives["timestamp_raw"] * factor)
        key = rest_identity.agg_trade_observation_key(symbol, natives["agg_trade_id"])
    else:
        row["interval_start"] = at_us(natives["open_time_raw"] * factor)
        row["interval_end"] = at_us((natives["close_time_raw"] + 1) * factor)
        key = rest_identity.kline_1m_observation_key(symbol, row["interval_start"])
    row.update(times or {})
    if channel is Channel.ARCHIVE:
        row["archive_revision_id"] = ARCHIVE_REVISION
        source = archive_identity.row_source_identity(ARCHIVE_REVISION)
        payload = (
            archive_identity.agg_trade_payload_hash(symbol, unit, row)
            if data_type == "agg_trades"
            else archive_identity.kline_1m_payload_hash(symbol, unit, row)
        )
        revision_id = archive_identity.revision_id(key, source, payload)
        knowledge, snapshot = K_ARCHIVE, "4411"
    else:
        row["response_revision_id"] = "rev1-" + "e" * 64
        source = rest_identity.rest_source_identity()
        payload = (
            rest_identity.agg_trade_payload_hash(symbol, row)
            if data_type == "agg_trades"
            else rest_identity.kline_1m_payload_hash(symbol, row)
        )
        revision_id = rest_identity.revision_id(key, source, payload)
        knowledge, snapshot = K_REST, "8822"
    fields: dict[str, Any] = {
        "channel": channel,
        "data_type": data_type,
        "observation_key": key,
        "revision_id": revision_id,
        "source_id": source,
        "payload_hash": payload,
        "knowledge_time": knowledge,
        "snapshot_id": snapshot,
        "time_unit": unit,
        "row": row,
    }
    fields.update(overrides)
    return ChannelRevision(**fields)


def agg_pair(
    *, when: datetime = T_2024, archive_unit: str = "millisecond", **rest_changes: Any
) -> tuple[ChannelRevision, ChannelRevision]:
    archive = revision(
        Channel.ARCHIVE,
        "agg_trades",
        archive_scale(agg_natives(when, archive_unit)),
        unit=archive_unit,
    )
    rest = revision(
        Channel.REST,
        "agg_trades",
        agg_natives(when.replace(microsecond=when.microsecond // 1000 * 1000), "millisecond",
                    **rest_changes),
        unit="millisecond",
    )  # fmt: skip
    return archive, rest


def kline_pair(
    *, start: datetime = T_2024, archive_unit: str = "millisecond", **rest_changes: Any
) -> tuple[ChannelRevision, ChannelRevision]:
    archive = revision(
        Channel.ARCHIVE,
        "klines_1m",
        archive_scale(kline_natives(start, archive_unit)),
        unit=archive_unit,
    )
    rest = revision(
        Channel.REST, "klines_1m", kline_natives(start, "millisecond", **rest_changes),
        unit="millisecond",
    )  # fmt: skip
    return archive, rest


# --------------------------------------------------------------------------- policy identity


def test_policy_identity_and_nature() -> None:
    assert DELIVERY_CHANNEL_BINDING.role is PolicyRole.PRECEDENCE
    assert DELIVERY_CHANNEL_BINDING.policy_id == "binance.spot.delivery-channel"
    assert DELIVERY_CHANNEL_BINDING.version == "1.0.0"
    digest = hashlib.sha256(canonical_json(DELIVERY_CHANNEL_SPEC).encode("utf-8")).hexdigest()
    assert (
        DELIVERY_CHANNEL_HASH
        == digest
        == ("399513973e6bb22e9e2c74a84ad226a220adf3b6035e8ca4290d26c19cdf1a85")
    )
    assert "not a source-declared revision order" in DELIVERY_CHANNEL_SPEC["nature"]
    assert "not a source-declared revision order" in POLICY_STATEMENT
    assert DELIVERY_CHANNEL_SPEC["edge"]["direction"] == "archive -> REST"
    assert set(DELIVERY_CHANNEL_SPEC["projections"]) == {AGG_TRADE_PROJECTION, KLINE_1M_PROJECTION}


def test_module_is_pure() -> None:
    source = inspect.getsource(channel_precedence)
    tree = ast.parse(source)
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not {"now", "utcnow", "today"} & attributes
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
    assert not {"httpx", "urllib", "requests", "socket", "time", "pyiceberg", "pyarrow"} & imported


# --------------------------------------------------------------------------- equal


def test_equal_agg_trade_content_across_decimal_scales() -> None:
    archive, rest = agg_pair()
    assert archive.payload_hash != rest.payload_hash  # different channel renderings
    comparison = compare_channels(archive, rest)
    assert comparison.outcome is ComparisonOutcome.EQUAL and comparison.equal
    assert comparison.reasons == ()
    assert comparison.archive_projection_sha256 == comparison.rest_projection_sha256
    projection = project(rest)
    assert projection.kind == AGG_TRADE_PROJECTION
    assert projection.document["price"] == "93712.010000000000000000"
    assert projection.document["event_time_us"] == us(T_2024)
    assert projection.sha256 == comparison.rest_projection_sha256
    assert set(projection.document) == {
        "kind", *DELIVERY_CHANNEL_SPEC["projections"][AGG_TRADE_PROJECTION]["fields"]
    }  # fmt: skip


def test_equal_kline_content() -> None:
    comparison = compare_channels(*kline_pair())
    assert comparison.outcome is ComparisonOutcome.EQUAL
    document = project(comparison.rest).document
    assert document["interval_end_us"] - document["interval_start_us"] == 60_000_000
    assert set(document) == {
        "kind", *DELIVERY_CHANNEL_SPEC["projections"][KLINE_1M_PROJECTION]["fields"]
    }  # fmt: skip


def test_millisecond_and_microsecond_klines_are_equivalent() -> None:
    archive, rest = kline_pair(start=T_2025, archive_unit="microsecond")
    assert archive.row["close_time_raw"] % 1000 == 999  # …59999999 µs
    assert rest.row["close_time_raw"] % 1000 == 999  # …59999 ms
    assert archive.row["close_time_raw"] != rest.row["close_time_raw"] * 1000
    assert compare_channels(archive, rest).outcome is ComparisonOutcome.EQUAL


def test_microsecond_aggtrade_with_sub_millisecond_digits_never_equals_millisecond_rest() -> None:
    exact = compare_channels(*agg_pair(when=T_2025, archive_unit="microsecond"))
    assert exact.outcome is ComparisonOutcome.EQUAL  # sub-millisecond digits are zero
    sub_ms = T_2025 + timedelta(microseconds=123)
    archive, rest = agg_pair(when=sub_ms, archive_unit="microsecond")
    assert rest.row["timestamp_raw"] * 1000 == us(sub_ms) - 123  # the ms rendering truncates
    comparison = compare_channels(archive, rest)
    assert comparison.outcome is ComparisonOutcome.MISMATCH
    assert comparison.reasons == ("differs: event_time_us",)
    with pytest.raises(ChannelPrecedenceViolation):
        build_channel_edge(comparison, knowledge_time=K_REST)


# --------------------------------------------------------------------------- mismatch


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price", Decimal("93712.02")),
        ("quantity", Decimal("0.0002")),
        ("first_trade_id", 27780),
        ("last_trade_id", 27784),
        ("timestamp_raw", us(T_2024) // 1000 + 1),
        ("is_buyer_maker", False),
        ("is_best_match", False),
    ],
)
def test_each_agg_trade_field_difference_is_a_mismatch(field: str, value: Any) -> None:
    comparison = compare_channels(*agg_pair(**{field: value}))
    expected = "event_time_us" if field == "timestamp_raw" else field
    assert comparison.outcome is ComparisonOutcome.MISMATCH
    assert comparison.reasons == (f"differs: {expected}",)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("open", Decimal("93700.01")),
        ("high", Decimal("93750.51")),
        ("low", Decimal("93690.00")),
        ("close", Decimal("93712.00")),
        ("volume", Decimal("12.345679")),
        ("quote_asset_volume", Decimal("1157000.12345679")),
        ("number_of_trades", 322),
        ("taker_buy_base_asset_volume", Decimal("6.2")),
        ("taker_buy_quote_asset_volume", Decimal("571600.6")),
        ("ignore_raw", "1"),
    ],
)
def test_each_kline_field_difference_is_a_mismatch(field: str, value: Any) -> None:
    comparison = compare_channels(*kline_pair(**{field: value}))
    expected = "ignore" if field == "ignore_raw" else field
    assert comparison.outcome is ComparisonOutcome.MISMATCH
    assert comparison.reasons == (f"differs: {expected}",)


def test_trailing_zeros_are_the_same_decimal() -> None:
    assert compare_channels(*agg_pair(price=Decimal("93712.0100"))).equal
    assert compare_channels(*agg_pair(quantity=Decimal("1E-4"))).equal


# --------------------------------------------------------------------------- incomparable


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price", Decimal("93712.0100000000000000001")),  # 19 fractional digits
        ("price", Decimal("100000000000000000000")),  # 21 integer digits
        ("price", Decimal("-93712.01")),
        ("price", Decimal("NaN")),
        ("quantity", Decimal("-0")),
    ],
)
def test_decimals_outside_decimal_38_18_are_incomparable_not_rounded(
    field: str, value: Decimal
) -> None:
    archive, stored = agg_pair()
    row = dict(stored.row)
    row[field] = value  # as a decoder would have produced it; never rounded into range
    rest = ChannelRevision(**{**_fields(stored), "row": row})
    comparison = compare_channels(archive, rest)
    assert comparison.outcome is ComparisonOutcome.INCOMPARABLE
    assert comparison.reasons and comparison.reasons[0].startswith(f"rest: {field}")
    assert comparison.rest_projection_sha256 is None


def test_missing_null_and_empty_fields_are_incomparable() -> None:
    archive, rest = agg_pair()
    row = dict(archive.row)
    row["is_best_match"] = None  # the frozen archive column is optional
    nulled = compare_channels(ChannelRevision(**{**_fields(archive), "row": row}), rest)
    assert nulled.outcome is ComparisonOutcome.INCOMPARABLE
    assert nulled.reasons == ("archive: missing is_best_match",)

    row = dict(rest.row)
    del row["quantity"]
    missing = compare_channels(archive, ChannelRevision(**{**_fields(rest), "row": row}))
    assert missing.outcome is ComparisonOutcome.INCOMPARABLE
    assert missing.reasons == ("rest: missing quantity",)

    k_archive, k_rest = kline_pair()
    row = dict(k_rest.row)
    row["ignore_raw"] = ""
    empty = compare_channels(k_archive, ChannelRevision(**{**_fields(k_rest), "row": row}))
    assert empty.outcome is ComparisonOutcome.INCOMPARABLE
    assert empty.reasons == ("rest: ignore_raw is empty",)


def test_a_kline_that_is_not_one_minute_is_incomparable() -> None:
    archive, rest = kline_pair()
    row = dict(rest.row)
    row["close_time_raw"] += 1
    comparison = compare_channels(archive, ChannelRevision(**{**_fields(rest), "row": row}))
    assert comparison.outcome is ComparisonOutcome.INCOMPARABLE


def _fields(item: ChannelRevision) -> dict[str, Any]:
    return {name: getattr(item, name) for name in ChannelRevision.__slots__}


# --------------------------------------------------------------------------- integrity


@pytest.mark.parametrize(
    ("side", "change"),
    [
        ("archive", {"payload_hash": "0" * 64}),
        ("rest", {"payload_hash": "0" * 64}),
        ("archive", {"revision_id": "rev1-" + "0" * 64}),
        ("rest", {"source_id": "binance.public.spot.rest@1.0.1"}),
        ("archive", {"time_unit": "microsecond"}),  # stored event_time contradicts the unit
    ],
)
def test_stored_rows_that_do_not_rederive_are_integrity_violations(
    side: str, change: dict[str, Any]
) -> None:
    archive, rest = agg_pair()
    if side == "archive":
        archive = ChannelRevision(**{**_fields(archive), **change})
    else:
        rest = ChannelRevision(**{**_fields(rest), **change})
    comparison = compare_channels(archive, rest)
    assert comparison.outcome is ComparisonOutcome.INTEGRITY_VIOLATION
    assert comparison.reasons[0].startswith(f"{side}: ")
    with pytest.raises(ChannelPrecedenceViolation):
        build_channel_edge(comparison, knowledge_time=K_REST)


def test_a_stored_utc_time_that_contradicts_the_raw_value_is_an_integrity_violation() -> None:
    archive, rest = kline_pair()
    row = dict(archive.row)
    row["interval_end"] = row["interval_end"] + timedelta(microseconds=1)
    comparison = compare_channels(ChannelRevision(**{**_fields(archive), "row": row}), rest)
    assert comparison.outcome is ComparisonOutcome.INTEGRITY_VIOLATION


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda a, r: compare_channels(r, a), id="swapped-channels"),
        pytest.param(lambda a, r: compare_channels(a, agg_pair(agg_trade_id=1)[1]), id="other-key"),
        pytest.param(lambda a, r: compare_channels(a, kline_pair()[1]), id="other-data-type"),
        pytest.param(
            lambda a, r: ChannelRevision(**{**_fields(r), "time_unit": "microsecond"}),
            id="rest-not-millisecond",
        ),
        pytest.param(lambda a, r: ChannelRevision(**{**_fields(r), "snapshot_id": ""}), id="snap"),
        pytest.param(
            lambda a, r: ChannelRevision(**{**_fields(r), "knowledge_time": datetime(2026, 1, 1)}),
            id="naive",
        ),
    ],
)
def test_unlawful_inputs_fail_closed(build: Any) -> None:
    archive, rest = agg_pair()
    with pytest.raises(ChannelPrecedenceViolation):
        build(archive, rest)


# --------------------------------------------------------------------------- edge


def test_edge_is_evidence_only_archive_to_rest_with_complete_evidence() -> None:
    archive, rest = agg_pair()
    comparison = compare_channels(archive, rest)
    edge = build_channel_edge(comparison, knowledge_time=K_REST + timedelta(seconds=3))
    item = edge.evidence
    assert (item.revision_id, item.superseded_revision_id) == (
        archive.revision_id,
        rest.revision_id,
    )
    assert item.observation_key == archive.observation_key == rest.observation_key
    assert item.policy == DELIVERY_CHANNEL_BINDING
    assert item.knowledge_time == K_REST + timedelta(seconds=3)
    digest = comparison.archive_projection_sha256
    assert item.evidence == (
        POLICY_STATEMENT,
        f"projection={AGG_TRADE_PROJECTION}",
        f"projection_sha256={digest}",
        f"archive_payload_hash={archive.payload_hash}",
        f"rest_payload_hash={rest.payload_hash}",
        "archive_revision_table=raw.binance_spot_agg_trades@snapshot:4411",
        "rest_revision_table=raw.binance_spot_rest_agg_trades@snapshot:8822",
    )
    assert (edge.revision_table, edge.superseded_table) == (
        "raw.binance_spot_agg_trades",
        "raw.binance_spot_rest_agg_trades",
    )
    assert (edge.revision_snapshot_id, edge.superseded_snapshot_id) == ("4411", "8822")
    assert edge.projection_sha256 == digest


def test_edge_identity_is_time_free_and_repeat_comparisons_are_stable() -> None:
    archive, rest = agg_pair()
    first = build_channel_edge(compare_channels(archive, rest), knowledge_time=K_REST)
    later = build_channel_edge(
        compare_channels(archive, rest), knowledge_time=K_REST + timedelta(days=3)
    )
    assert first.edge_id == later.edge_id  # replays must reuse the first committed record
    assert first.edge_id == rest_identity.edge_id(
        DELIVERY_CHANNEL_BINDING, archive.observation_key, archive.revision_id, rest.revision_id
    )
    assert first.evidence.knowledge_time != later.evidence.knowledge_time
    assert compare_channels(archive, rest) == compare_channels(archive, rest)


def test_edge_knowledge_time_comes_from_the_caller_and_is_never_backfilled() -> None:
    comparison = compare_channels(*agg_pair())
    floor = max(K_ARCHIVE, K_REST)
    assert build_channel_edge(comparison, knowledge_time=floor).evidence.knowledge_time == floor
    for early in (K_ARCHIVE, floor - timedelta(microseconds=1)):
        with pytest.raises(ChannelPrecedenceViolation, match="backfilled"):
            build_channel_edge(comparison, knowledge_time=early)
    with pytest.raises(ChannelPrecedenceViolation):
        build_channel_edge(comparison, knowledge_time=datetime(2026, 9, 26))  # naive
    parameters = inspect.signature(build_channel_edge).parameters
    assert parameters["knowledge_time"].default is inspect.Parameter.empty  # never defaulted


def test_edge_row_fits_the_evidence_table_exactly() -> None:
    edge = build_channel_edge(compare_channels(*kline_pair()), knowledge_time=K_REST)
    row = edge.row()
    stored = batch_for(BINANCE_SPOT_PRECEDENCE_EVIDENCE, [row]).to_pylist()[0]
    assert stored == row
    assert stored["edge_id"] == edge.edge_id and stored["revision_table"].startswith("raw.")


def _record(item: ChannelRevision, arrival_seq: int) -> RevisionRecord:
    if item.channel is Channel.REST:
        decision = decide_rest_availability(
            RestAvailabilitySubject.AGG_TRADE,
            event_time=item.row["event_time"],
            ingest_time=item.knowledge_time,
            knowledge_time=item.knowledge_time,
        )
    else:
        times = ObservationTimes(
            event_time=item.row["event_time"],
            available_time=item.knowledge_time,
            ingest_time=item.knowledge_time,
            knowledge_time=item.knowledge_time,
            declared_latency=timedelta(0),
        )
        decision = AvailabilityDecision(
            times=times,
            policy=policy(PolicyRole.AVAILABILITY, "binance.spot.publication"),
            evidence_gap="gap",
        )
    return RevisionRecord(
        observation_key=item.observation_key,
        revision_id=item.revision_id,
        source_id=item.source_id,
        payload_hash=item.payload_hash,
        arrival_seq=arrival_seq,
        availability=decision,
    )


def test_the_edge_resolves_the_two_heads_in_one_revision_graph() -> None:
    archive, rest = agg_pair()
    records = (
        _record(archive, rest_identity.check_archive_interval_arrival_seq(1 << 32)),
        _record(rest, rest_identity.element_arrival_seq(1 << 62, 0)),
    )
    # Without the edge: two competing heads.
    RevisionGraph(revisions=records)
    assert maximal_heads(records) == tuple(sorted((archive.revision_id, rest.revision_id)))
    # With the evidence-only edge (no record declares it in supersedes): only the archive head.
    edge = build_channel_edge(compare_channels(archive, rest), knowledge_time=K_REST)
    RevisionGraph(revisions=records, precedence_evidence=(edge.evidence,))
    assert all(record.supersedes == () for record in records)
    assert maximal_heads(records, (edge.evidence,)) == (archive.revision_id,)


# --------------------------------------------------------------------------- D3B-R1 boundaries


@pytest.mark.parametrize(
    "value",
    [
        Decimal("1e100"),
        Decimal("9" * 50),
        Decimal("1E+999999"),
        Decimal("1e-100"),
        Decimal("1E-999999"),
        Decimal("0." + "1" * 200),
    ],
    ids=["1e100", "50-digits", "1e+999999", "1e-100", "1e-999999", "200-fraction-digits"],
)
def test_extreme_finite_decimals_are_incomparable_never_an_exception(value: Decimal) -> None:
    archive, stored = agg_pair()
    row = dict(stored.row)
    row["price"] = value
    rest = ChannelRevision(**{**_fields(stored), "row": row})
    comparison = compare_channels(archive, rest)  # must not raise decimal.InvalidOperation
    assert comparison.outcome is ComparisonOutcome.INCOMPARABLE
    assert comparison.reasons[0].startswith("rest: price")
    with pytest.raises(ValueError, match="price"):
        project(rest)
    with pytest.raises(ChannelPrecedenceViolation):
        build_channel_edge(comparison, knowledge_time=K_REST)


def _forged(**changes: Any) -> ChannelComparison:
    honest = compare_channels(*agg_pair())
    fields: dict[str, Any] = {
        "outcome": honest.outcome,
        "projection_kind": honest.projection_kind,
        "archive": honest.archive,
        "rest": honest.rest,
        "archive_projection_sha256": honest.archive_projection_sha256,
        "rest_projection_sha256": honest.rest_projection_sha256,
        "reasons": honest.reasons,
    }
    fields.update(changes)
    return ChannelComparison(**fields)


def test_a_forged_cross_key_equal_comparison_mints_no_edge() -> None:
    archive, _ = agg_pair()
    _, other_key = agg_pair(agg_trade_id=1)
    assert archive.observation_key != other_key.observation_key
    digest = project(archive).sha256
    forged = _forged(
        rest=other_key, archive_projection_sha256=digest, rest_projection_sha256=digest
    )
    with pytest.raises(ChannelPrecedenceViolation, match="observation_key"):
        build_channel_edge(forged, knowledge_time=K_REST)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"archive_projection_sha256": "0" * 64, "rest_projection_sha256": "0" * 64},
                     id="both-digests"),
        pytest.param({"rest_projection_sha256": "0" * 64}, id="one-digest"),
        pytest.param({"archive_projection_sha256": None, "rest_projection_sha256": None},
                     id="no-digest"),
        pytest.param({"projection_kind": KLINE_1M_PROJECTION}, id="kind"),
        pytest.param({"reasons": ("differs: price",)}, id="reasons"),
        pytest.param({"rest": agg_pair(price=Decimal("1"))[1]}, id="equal-claimed-for-mismatch"),
        pytest.param({"archive": ChannelRevision(**{**_fields(agg_pair()[0]),
                                                  "payload_hash": "0" * 64})},
                     id="equal-claimed-for-integrity-violation"),
    ],
)  # fmt: skip
def test_forged_projection_digests_or_outcomes_mint_no_edge(changes: dict[str, Any]) -> None:
    with pytest.raises(ChannelPrecedenceViolation):
        build_channel_edge(_forged(**changes), knowledge_time=K_REST)


def test_rows_are_snapshotted_so_later_mutation_cannot_change_the_edge() -> None:
    archive, stored = agg_pair()
    caller_row = dict(stored.row)
    rest = ChannelRevision(**{**_fields(stored), "row": caller_row})
    comparison = compare_channels(archive, rest)
    caller_row["price"] = Decimal("1")  # the caller's mapping changes after the comparison
    edge = build_channel_edge(comparison, knowledge_time=K_REST)
    assert rest.row["price"] == Decimal("93712.01")
    assert edge.projection_sha256 == comparison.rest_projection_sha256
    with pytest.raises(TypeError):
        rest.row["price"] = Decimal("1")  # type: ignore[index]


def test_a_stale_comparison_whose_row_was_swapped_mints_no_edge() -> None:
    comparison = compare_channels(*agg_pair())
    mutated = dict(comparison.rest.row)
    mutated["price"] = Decimal("93712.02")
    # Bypass the frozen dataclass the way a buggy caller could: the edge must still fail closed.
    object.__setattr__(comparison.rest, "row", mutated)
    with pytest.raises(ChannelPrecedenceViolation):
        build_channel_edge(comparison, knowledge_time=K_REST)


def test_edge_safety_does_not_depend_on_assert_statements() -> None:
    tree = ast.parse(inspect.getsource(channel_precedence))
    assert not [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)]


def test_forged_comparisons_fail_closed_under_python_optimize() -> None:
    script = (
        "import sys\n"
        "assert sys.flags.optimize == 1\n"
        "from tests.infrastructure.revision import test_channel_precedence as t\n"
        "from infrastructure.revision.channel_precedence import "
        "ChannelPrecedenceViolation, build_channel_edge\n"
        "_, other = t.agg_pair(agg_trade_id=1)\n"
        "for forged in (t._forged(rest=other), t._forged(rest_projection_sha256='0' * 64)):\n"
        "    try:\n"
        "        build_channel_edge(forged, knowledge_time=t.K_REST)\n"
        "    except ChannelPrecedenceViolation:\n"
        "        continue\n"
        "    raise SystemExit('edge minted under -O')\n"
        "print('ok')\n"
    )
    repo = Path(__file__).resolve().parents[3]
    env = {**os.environ, "PYTHONPATH": str(repo)}
    result = subprocess.run(
        [sys.executable, "-O", "-c", script],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == "ok"

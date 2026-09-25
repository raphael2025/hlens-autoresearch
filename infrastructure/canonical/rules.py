"""Canonical rules of ADR-0028 (Phase 1 E1) — pure functions, no I/O, no clock.

Four versioned identifiers (``03-data.md`` §7.3), each a spec whose SHA-256 of canonical JSON is its
hash (derived, never written down, so any rule change changes it):

- ``hlens.canonical.revision-identity@1.0.0`` — Canonical ``source_id`` (normalizer version + Raw
  table + Raw revision id: **lineage is identity**), payload documents and ``crev1-`` revision ids.
  Physically separate from the archive and REST identity rules (ADR-0027 §11 trap 1);
- ``hlens.canonical.binance-spot.normalizer@1.0.0`` (parser role) — Raw element row → Canonical
  row, one to one: symbol map, unit conversion, empty in-row edges, the arrival block layout;
- ``hlens.canonical.availability@1.0.0`` — ADR-0023 §3 propagation:
  ``available = max(spec constraint, raw.available + 0)``,
  ``knowledge = max(ready, raw.knowledge)``, evidence **or** gap inherited from the Raw decision;
- ``hlens.canonical.precedence-map@1.0.0`` — a verified Raw ``archive → REST`` edge mapped one to
  one onto the two Canonical revisions of those lineages, ``knowledge_time`` = the latest of the
  three persisted facts. It is a join, never a comparison: Canonical payload equality is never
  precedence evidence (ADR-0028 §2 / §3).

Every builder goes through the contract (``RevisionRecord`` / ``AvailabilityDecision`` /
``PrecedenceEvidence``), so whatever the contracts refuse can never be written or mapped.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Final

from core.contracts.revision import (
    AvailabilityDecision,
    ObservationTimes,
    PolicyBinding,
    PolicyRole,
    PrecedenceEvidence,
    RevisionRecord,
)
from core.domain.base import CONTRACT_SCHEMA_VERSION, canonical_json
from infrastructure.catalog.definitions import RegisteredTableDefinition
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_AGG_TRADES,
    BINANCE_SPOT_ARCHIVES,
    BINANCE_SPOT_KLINES_1M,
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_KLINES_1M,
    BINANCE_SPOT_REST_RESPONSES,
    CANONICAL_BARS_1M,
    CANONICAL_TRADES,
)

__all__ = [
    "ARRIVAL_SEQ_LIMIT",
    "ARRIVAL_SEQ_STRIDE",
    "AVAILABILITY_BINDING",
    "AVAILABILITY_SPEC",
    "CANONICAL_TABLES",
    "IDENTITY_HASH",
    "IDENTITY_SPEC",
    "NORMALIZER_BINDING",
    "NORMALIZER_SPEC",
    "PRECEDENCE_MAP_BINDING",
    "PRECEDENCE_MAP_SPEC",
    "SYMBOLS",
    "CanonicalRuleViolation",
    "RawChannel",
    "canonical_row",
    "canonical_source_identity",
    "check_block_base",
    "decide_availability",
    "map_channel_edge",
    "next_block_base",
    "payload_hash",
    "position_of",
    "raw_channel_of",
    "revision_id",
]

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MICRO: Final = timedelta(microseconds=1)
_MINUTE: Final = timedelta(minutes=1)
_SCALE: Final = Decimal("0.000000000000000001")

#: Canonical ``arrival_seq``: one block of ``2**32`` numbers per normalization unit, in
#: ``[0, 2**62)`` of each Canonical table; row ``p`` of a unit takes ``base + p`` (ADR-0028 §5).
ARRIVAL_SEQ_STRIDE: Final = 1 << 32
ARRIVAL_SEQ_LIMIT: Final = 1 << 62


class CanonicalRuleViolation(ValueError):
    """The inputs cannot lawfully become a Canonical revision / mapped edge (fail closed)."""


def _digest(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(document)).encode("utf-8")).hexdigest()


# =========================================================================================
# first-slice instruments (frozen with the normalizer version)
# =========================================================================================


@dataclass(frozen=True, slots=True)
class _Instrument:
    venue: str
    instrument_type: str
    symbol: str
    venue_symbol: str


#: venue-native symbol → Canonical instrument. Canonical ``symbol`` is ``<base>-<quote>``; the
#: listing history (E2) must use the same ``Instrument``.
SYMBOLS: Final[Mapping[str, _Instrument]] = {
    "BTCUSDT": _Instrument("binance", "spot", "BTC-USDT", "BTCUSDT"),
    "ETHUSDT": _Instrument("binance", "spot", "ETH-USDT", "ETHUSDT"),
}


# =========================================================================================
# Raw channels: which Raw tables feed which Canonical table
# =========================================================================================


@dataclass(frozen=True, slots=True)
class RawChannel:
    """One Raw element table and everything the normalizer needs to know about it."""

    name: str
    data_type: str
    element: RegisteredTableDefinition
    source: RegisteredTableDefinition
    lineage_column: str
    canonical: RegisteredTableDefinition


_CHANNELS: Final[tuple[RawChannel, ...]] = (
    RawChannel(
        "archive",
        "agg_trades",
        BINANCE_SPOT_AGG_TRADES,
        BINANCE_SPOT_ARCHIVES,
        "archive_revision_id",
        CANONICAL_TRADES,
    ),
    RawChannel(
        "archive",
        "klines_1m",
        BINANCE_SPOT_KLINES_1M,
        BINANCE_SPOT_ARCHIVES,
        "archive_revision_id",
        CANONICAL_BARS_1M,
    ),
    RawChannel(
        "rest",
        "agg_trades",
        BINANCE_SPOT_REST_AGG_TRADES,
        BINANCE_SPOT_REST_RESPONSES,
        "response_revision_id",
        CANONICAL_TRADES,
    ),
    RawChannel(
        "rest",
        "klines_1m",
        BINANCE_SPOT_REST_KLINES_1M,
        BINANCE_SPOT_REST_RESPONSES,
        "response_revision_id",
        CANONICAL_BARS_1M,
    ),
)
CANONICAL_TABLES: Final[Mapping[str, RegisteredTableDefinition]] = {
    "agg_trades": CANONICAL_TRADES,
    "klines_1m": CANONICAL_BARS_1M,
}


def raw_channel_of(raw_table: str) -> RawChannel:
    """The channel of a Raw element table name; anything else fails closed."""
    for channel in _CHANNELS:
        if channel.element.table == raw_table:
            return channel
    raise CanonicalRuleViolation(f"{raw_table!r} is not a Raw element table of the first slice")


# =========================================================================================
# the four specs
# =========================================================================================

NORMALIZER_ID: Final = "hlens.canonical.binance-spot.normalizer"
NORMALIZER_VERSION: Final = "1.0.0"
NORMALIZER_SPEC: Final[dict[str, Any]] = {
    "normalizer": NORMALIZER_ID,
    "version": NORMALIZER_VERSION,
    "adr": "ADR-0028",
    "mapping": "one Canonical revision per verified Raw element revision (no merge, no choice)",
    "channels": [
        {
            "channel": channel.name,
            "data_type": channel.data_type,
            "raw_table": channel.element.table,
            "source_table": channel.source.table,
            "lineage_column": channel.lineage_column,
            "canonical_table": channel.canonical.table,
        }
        for channel in _CHANNELS
    ],
    "instruments": {
        venue_symbol: {
            "venue": item.venue,
            "instrument_type": item.instrument_type,
            "symbol": item.symbol,
            "venue_symbol": item.venue_symbol,
        }
        for venue_symbol, item in SYMBOLS.items()
    },
    "trade": {
        "venue_trade_id": "aggTradeId as decimal text",
        "price": "price",
        "quantity": "quantity",
        "buyer_is_maker": "is_buyer_maker",
        "event_time": "the Raw row's event_time (declared-unit conversion, already proven)",
        "dropped": ["first_trade_id", "last_trade_id", "is_best_match", "timestamp_raw"],
    },
    "bar_1m": {
        "open/high/low/close/volume": "same-named Raw fields",
        "quote_volume": "quote_asset_volume",
        "trade_count": "number_of_trades",
        "taker_buy_base_volume": "taker_buy_base_asset_volume",
        "taker_buy_quote_volume": "taker_buy_quote_asset_volume",
        "interval": "the Raw row's [interval_start, interval_end), exactly one minute",
        "dropped": ["open_time_raw", "close_time_raw", "ignore_raw"],
    },
    "in_row_edges": "image of the Raw row's own in-row edges; 1.0.0 requires and writes none",
    "arrival_seq": {
        "interval": [0, ARRIVAL_SEQ_LIMIT],
        "block_stride": ARRIVAL_SEQ_STRIDE,
        "unit": "one Raw source revision x data type",
        "position": "archive_line_number (archive) | element_index + 1 (REST)",
        "base": "reserved, never a row",
    },
}
NORMALIZER_HASH: Final = _digest(NORMALIZER_SPEC)
NORMALIZER_BINDING: Final = PolicyBinding(
    role=PolicyRole.PARSER,
    policy_id=NORMALIZER_ID,
    version=NORMALIZER_VERSION,
    policy_hash=NORMALIZER_HASH,
)
_NORMALIZER_IDENTITY: Final = f"{NORMALIZER_ID}@{NORMALIZER_VERSION}"

IDENTITY_RULE_ID: Final = "hlens.canonical.revision-identity"
IDENTITY_RULE_VERSION: Final = "1.0.0"
IDENTITY_SPEC: Final[dict[str, Any]] = {
    "rule": IDENTITY_RULE_ID,
    "version": IDENTITY_RULE_VERSION,
    "separate_from": [
        "hlens.binance.spot.raw-revision-identity@1.0.0",
        "hlens.binance.spot.rest-revision-identity@1.0.0",
    ],
    "observation_key": "the Raw element observation_key, character for character",
    "source_id": "<normalizer>@<version>|<lineage_raw_table>|<lineage_raw_revision_id>",
    "payload": {
        "trade": [
            "kind=hlens.canonical.trade/1",
            "venue",
            "instrument_type",
            "symbol",
            "venue_symbol",
            "venue_trade_id",
            "price",
            "quantity",
            "buyer_is_maker",
            "event_time_us",
        ],
        "bar_1m": [
            "kind=hlens.canonical.bar_1m/1",
            "venue",
            "instrument_type",
            "symbol",
            "venue_symbol",
            "interval_start_us",
            "interval_end_us",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_volume",
            "trade_count",
            "taker_buy_base_volume",
            "taker_buy_quote_volume",
        ],
        "decimal": "decimal(38, 18) as fixed text with exactly 18 fractional digits",
        "time": "exact UTC epoch microseconds",
        "excluded": "lineage, both time axes, arrival_seq, bindings",
    },
    "revision_id": "crev1-sha256(canonical_json({rule, rule_version, rule_hash, observation_key, "
    "source_id, payload_hash})); rule_hash is the SHA-256 of this spec",
    "canonical_json": "sorted keys, compact separators, UTF-8, no NaN",
}
IDENTITY_HASH: Final = _digest(IDENTITY_SPEC)

AVAILABILITY_ID: Final = "hlens.canonical.availability"
AVAILABILITY_VERSION: Final = "1.0.0"
AVAILABILITY_SPEC: Final[dict[str, Any]] = {
    "policy": AVAILABILITY_ID,
    "version": AVAILABILITY_VERSION,
    "adr": ["ADR-0023 §3", "ADR-0028 §4"],
    "available_time": "max(spec constraint, raw.available_time + declared_latency), latency 0; "
    "trade constraint = event_time, bar_1m constraint = interval_end",
    "knowledge_time": "max(actual_ready_time, raw.knowledge_time); a ready time before the Raw "
    "knowledge_time is refused, never raised",
    "ingest_time": "the Raw row's ingest_time",
    "evidence_or_gap": "exactly one, inherited: gap -> 'inherited from <raw_table>/<raw_revision>"
    " under <policy>@<version>: <raw gap>'; evidence -> ['input <raw_table>/<raw_revision> under "
    "<policy>@<version>', *raw evidence]",
}
AVAILABILITY_HASH: Final = _digest(AVAILABILITY_SPEC)
AVAILABILITY_BINDING: Final = PolicyBinding(
    role=PolicyRole.AVAILABILITY,
    policy_id=AVAILABILITY_ID,
    version=AVAILABILITY_VERSION,
    policy_hash=AVAILABILITY_HASH,
)

PRECEDENCE_MAP_ID: Final = "hlens.canonical.precedence-map"
PRECEDENCE_MAP_VERSION: Final = "1.0.0"
PRECEDENCE_MAP_STATEMENT: Final = (
    "canonical image of a persisted, verified Raw precedence edge along the lineage columns; "
    "no new comparison or policy judgement"
)
PRECEDENCE_MAP_SPEC: Final[dict[str, Any]] = {
    "policy": PRECEDENCE_MAP_ID,
    "version": PRECEDENCE_MAP_VERSION,
    "adr": "ADR-0028 §3.2",
    "input": f"one verified row of {BINANCE_SPOT_PRECEDENCE_EVIDENCE.table} of a bound snapshot",
    "endpoints": "exactly one Canonical revision per Raw endpoint under the bound normalizer "
    "(lineage_raw_table, lineage_raw_revision_id); zero -> no Canonical edge; more -> fail",
    "knowledge_time": "max(raw edge knowledge_time, both Canonical knowledge_time); no clock",
    "evidence": [
        PRECEDENCE_MAP_STATEMENT,
        "raw_edge_id=<edge_id>",
        f"raw_evidence_table={BINANCE_SPOT_PRECEDENCE_EVIDENCE.table}@snapshot:<bound snapshot id>",
        "*raw edge evidence",
    ],
    "materialised": False,
}
PRECEDENCE_MAP_HASH: Final = _digest(PRECEDENCE_MAP_SPEC)
PRECEDENCE_MAP_BINDING: Final = PolicyBinding(
    role=PolicyRole.PRECEDENCE,
    policy_id=PRECEDENCE_MAP_ID,
    version=PRECEDENCE_MAP_VERSION,
    policy_hash=PRECEDENCE_MAP_HASH,
)


# =========================================================================================
# identity
# =========================================================================================


def canonical_source_identity(raw_table: str, raw_revision_id: str) -> str:
    """``<normalizer>@<version>|<raw table>|<raw revision id>``: the lineage is the identity."""
    raw_channel_of(raw_table)
    if not isinstance(raw_revision_id, str) or not raw_revision_id or "|" in raw_revision_id:
        raise CanonicalRuleViolation("raw_revision_id must be a non-empty id without '|'")
    return f"{_NORMALIZER_IDENTITY}|{raw_table}|{raw_revision_id}"


def revision_id(observation_key: str, source_id: str, payload_hash: str) -> str:
    """Stable Canonical revision id: same inputs → same id, whenever they arrive."""
    for label, value in (("observation_key", observation_key), ("source_id", source_id)):
        if not isinstance(value, str) or not value:
            raise CanonicalRuleViolation(f"{label} must be a non-empty string")
    if not isinstance(payload_hash, str) or len(payload_hash) != 64:
        raise CanonicalRuleViolation("payload_hash must be SHA-256 hex")
    document = {
        "rule": IDENTITY_RULE_ID,
        "rule_version": IDENTITY_RULE_VERSION,
        "rule_hash": IDENTITY_HASH,
        "observation_key": observation_key,
        "source_id": source_id,
        "payload_hash": payload_hash,
    }
    return f"crev1-{_digest(document)}"


def _decimal_text(value: object, label: str) -> str:
    if not isinstance(value, Decimal) or not value.is_finite() or value.is_signed():
        raise CanonicalRuleViolation(f"{label} is not a finite non-negative Decimal")
    if value.adjusted() >= 20:
        raise CanonicalRuleViolation(f"{label} exceeds decimal(38, 18)")
    with localcontext() as context:
        context.prec = 80
        try:
            quantized = value.quantize(_SCALE)
        except InvalidOperation:
            raise CanonicalRuleViolation(
                f"{label} is not representable in decimal(38, 18)"
            ) from None
    if quantized != value:
        raise CanonicalRuleViolation(f"{label} needs more than 18 fractional digits")
    return format(quantized, "f")


def _micros(value: object, label: str) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise CanonicalRuleViolation(f"{label} is not a UTC timestamp")
    return (value - _EPOCH) // _MICRO


def _int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CanonicalRuleViolation(f"{label} is not a non-negative int")
    return value


def _market_columns(data_type: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    """The Canonical market columns of one Raw element row (normalizer 1.0.0 mapping)."""
    instrument = SYMBOLS.get(raw["symbol"])
    if instrument is None:
        raise CanonicalRuleViolation(f"symbol {raw['symbol']!r} is not a first-slice instrument")
    columns: dict[str, Any] = {
        "venue": instrument.venue,
        "instrument_type": instrument.instrument_type,
        "symbol": instrument.symbol,
        "venue_symbol": instrument.venue_symbol,
    }
    if data_type == "agg_trades":
        columns.update(
            {
                "event_time": raw["event_time"],
                "venue_trade_id": str(_int(raw["agg_trade_id"], "agg_trade_id")),
                "price": raw["price"],
                "quantity": raw["quantity"],
                "buyer_is_maker": raw["is_buyer_maker"],
            }
        )
    else:
        if raw["interval_end"] - raw["interval_start"] != _MINUTE:
            raise CanonicalRuleViolation("a 1m bar must span exactly one minute")
        columns.update(
            {
                "interval_start": raw["interval_start"],
                "interval_end": raw["interval_end"],
                "open": raw["open"],
                "high": raw["high"],
                "low": raw["low"],
                "close": raw["close"],
                "volume": raw["volume"],
                "quote_volume": raw["quote_asset_volume"],
                "trade_count": _int(raw["number_of_trades"], "number_of_trades"),
                "taker_buy_base_volume": raw["taker_buy_base_asset_volume"],
                "taker_buy_quote_volume": raw["taker_buy_quote_asset_volume"],
            }
        )
    return columns


def payload_hash(data_type: str, columns: Mapping[str, Any]) -> str:
    """SHA-256 of the Canonical payload document (market content only)."""
    base = {
        "venue": columns["venue"],
        "instrument_type": columns["instrument_type"],
        "symbol": columns["symbol"],
        "venue_symbol": columns["venue_symbol"],
    }
    if data_type == "agg_trades":
        maker = columns["buyer_is_maker"]
        if not isinstance(maker, bool):
            raise CanonicalRuleViolation("buyer_is_maker is not a boolean")
        document = {
            "kind": "hlens.canonical.trade/1",
            **base,
            "venue_trade_id": columns["venue_trade_id"],
            "price": _decimal_text(columns["price"], "price"),
            "quantity": _decimal_text(columns["quantity"], "quantity"),
            "buyer_is_maker": maker,
            "event_time_us": _micros(columns["event_time"], "event_time"),
        }
    else:
        document = {
            "kind": "hlens.canonical.bar_1m/1",
            **base,
            "interval_start_us": _micros(columns["interval_start"], "interval_start"),
            "interval_end_us": _micros(columns["interval_end"], "interval_end"),
            "trade_count": _int(columns["trade_count"], "trade_count"),
            **{
                name: _decimal_text(columns[name], name)
                for name in (
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "quote_volume",
                    "taker_buy_base_volume",
                    "taker_buy_quote_volume",
                )
            },
        }
    return _digest(document)


# =========================================================================================
# availability (ADR-0023 §3 / ADR-0028 §4)
# =========================================================================================


def _inherited(raw_table: str, raw: Mapping[str, Any]) -> tuple[tuple[str, ...], str | None]:
    policy = f"{raw['availability_policy_id']}@{raw['availability_policy_version']}"
    source = f"{raw_table}/{raw['revision_id']}"
    evidence = tuple(raw["availability_evidence"])
    gap = raw["availability_evidence_gap"]
    if (gap is None) == (not evidence):
        raise CanonicalRuleViolation(f"{source} has neither or both evidence and gap")
    if gap is not None:
        return (), f"inherited from {source} under {policy}: {gap}"
    return (f"input {source} under {policy}", *evidence), None


def decide_availability(
    data_type: str, raw_table: str, raw: Mapping[str, Any], *, ready_time: datetime
) -> AvailabilityDecision:
    """The Canonical decision of one Raw row, given the normalizer's one clock reading."""
    if (
        not isinstance(ready_time, datetime)
        or ready_time.tzinfo is None
        or ready_time.utcoffset() != timedelta(0)
    ):
        raise CanonicalRuleViolation("the normalizer clock must be timezone-aware UTC")
    if ready_time < raw["knowledge_time"]:
        raise CanonicalRuleViolation(
            "the normalizer ready time precedes the Raw knowledge_time: refusing to backfill"
        )
    latency = timedelta(microseconds=raw["declared_latency_us"])
    if data_type == "agg_trades":
        event_time, event_end = raw["event_time"], None
        constraint = event_time
    else:
        event_time, event_end = raw["interval_start"], raw["interval_end"]
        constraint = event_end
    evidence, gap = _inherited(raw_table, raw)
    try:
        times = ObservationTimes(
            event_time=event_time,
            event_end_time=event_end,
            source_time=raw["source_time"],
            available_time=max(constraint, raw["available_time"] + latency),
            ingest_time=raw["ingest_time"],
            knowledge_time=max(ready_time, raw["knowledge_time"]),
            declared_latency=latency,
        )
        return AvailabilityDecision(
            times=times, policy=AVAILABILITY_BINDING, evidence=evidence, evidence_gap=gap
        )
    except ValueError as exc:
        raise CanonicalRuleViolation(f"no lawful Canonical availability: {exc}") from None


# =========================================================================================
# arrival blocks
# =========================================================================================


def check_block_base(value: object) -> int:
    """A Canonical block base: an int multiple of the stride inside ``[0, 2**62)``."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise CanonicalRuleViolation("a Canonical arrival block base is not an int")
    if not 0 <= value < ARRIVAL_SEQ_LIMIT or value % ARRIVAL_SEQ_STRIDE:
        raise CanonicalRuleViolation(f"{value} is not a Canonical arrival block base")
    return value


def next_block_base(largest: int | None) -> int:
    """The block after every committed Canonical ``arrival_seq`` (gaps allowed, never reused)."""
    if largest is None:
        return 0
    if not isinstance(largest, int) or isinstance(largest, bool) or not 0 <= largest:
        raise CanonicalRuleViolation("corrupt Canonical arrival anchor")
    base = (largest // ARRIVAL_SEQ_STRIDE + 1) * ARRIVAL_SEQ_STRIDE
    if base >= ARRIVAL_SEQ_LIMIT:
        raise CanonicalRuleViolation("Canonical arrival sequence space exhausted")
    return base


def position_of(channel: RawChannel, raw: Mapping[str, Any]) -> int:
    """The row's position inside its unit's block (never 0: the base is reserved)."""
    if channel.name == "archive":
        position = raw["archive_line_number"]
    else:
        position = raw["element_index"] + 1
    if not isinstance(position, int) or isinstance(position, bool):
        raise CanonicalRuleViolation("a Raw position is not an int")
    if not 1 <= position < ARRIVAL_SEQ_STRIDE:
        raise CanonicalRuleViolation(f"Raw position {position} does not fit one block")
    return position


# =========================================================================================
# the single row builder
# =========================================================================================


def canonical_row(
    channel: RawChannel, raw: Mapping[str, Any], *, base: int, ready_time: datetime
) -> dict[str, Any]:
    """Every column of the Canonical revision of one verified Raw element row.

    ``raw`` must already be proven (``PersistedRowVerifier``); this only maps it. A Raw row with
    an in-row edge cannot be mapped by normalizer 1.0.0 (ADR-0028 §3.1) and fails closed.
    """
    if raw["supersedes"] or raw["precedence_evidence"]:
        raise CanonicalRuleViolation(
            f"{channel.element.table}/{raw['revision_id']} carries an in-row edge: normalizer "
            "1.0.0 cannot map it (new normalizer version + ADR required)"
        )
    check_block_base(base)
    raw_table = channel.element.table
    columns = _market_columns(channel.data_type, raw)
    digest = payload_hash(channel.data_type, columns)
    source = canonical_source_identity(raw_table, raw["revision_id"])
    key = raw["observation_key"]
    decision = decide_availability(channel.data_type, raw_table, raw, ready_time=ready_time)
    try:
        record = RevisionRecord(
            observation_key=key,
            revision_id=revision_id(key, source, digest),
            source_id=source,
            payload_hash=digest,
            arrival_seq=base + position_of(channel, raw),
            source_revision_id=raw["source_revision_id"],
            source_revision_time=raw["source_revision_time"],
            availability=decision,
        )
    except ValueError as exc:
        raise CanonicalRuleViolation(f"no lawful Canonical revision: {exc}") from None
    times = record.availability.times
    row: dict[str, Any] = {
        "observation_key": record.observation_key,
        "revision_id": record.revision_id,
        "source_id": record.source_id,
        "payload_hash": record.payload_hash,
        "arrival_seq": record.arrival_seq,
        "supersedes": [],
        "source_revision_id": record.source_revision_id,
        "source_revision_time": record.source_revision_time,
        "source_time": times.source_time,
        "available_time": times.available_time,
        "ingest_time": times.ingest_time,
        "knowledge_time": times.knowledge_time,
        "declared_latency_us": times.declared_latency // _MICRO,
        "availability_policy_id": decision.policy.policy_id,
        "availability_policy_version": decision.policy.version,
        "availability_policy_hash": decision.policy.policy_hash,
        "availability_evidence": list(decision.evidence),
        "availability_evidence_gap": decision.evidence_gap,
        "precedence_evidence": [],
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "lineage_raw_table": raw_table,
        "lineage_raw_revision_id": raw["revision_id"],
        "lineage_source_table": channel.source.table,
        "lineage_source_revision_id": raw[channel.lineage_column],
        **columns,
    }
    names = {field.name for field in channel.canonical.arrow_schema}
    drift = sorted(names ^ set(row))
    if drift:  # pragma: no cover - a builder / table mismatch is a code defect
        raise CanonicalRuleViolation(
            f"Canonical row drifts from {channel.canonical.table}: {drift}"
        )
    return row


# =========================================================================================
# precedence map (ADR-0028 §3.2) — used by the PIT executor (F1)
# =========================================================================================


def map_channel_edge(
    raw_edge: PrecedenceEvidence,
    raw_edge_id: str,
    raw_evidence_snapshot_id: str,
    archive_canonical: RevisionRecord,
    rest_canonical: RevisionRecord,
) -> PrecedenceEvidence:
    """The Canonical image of one **already verified** Raw ``archive → REST`` edge.

    The callers must have located ``archive_canonical`` / ``rest_canonical`` as the unique
    Canonical revisions whose lineage is the edge's two Raw revisions under the bound normalizer;
    this function re-checks what it can see (same key, lineage in the source identity) and stamps
    ``knowledge_time`` with the latest of the three persisted facts — never a clock.
    """
    if not isinstance(raw_edge, PrecedenceEvidence):
        raise CanonicalRuleViolation("raw_edge must be a PrecedenceEvidence")
    for label, value in (("raw_edge_id", raw_edge_id), ("snapshot", raw_evidence_snapshot_id)):
        if not isinstance(value, str) or not value:
            raise CanonicalRuleViolation(f"{label} must be a non-empty string")
    pairs = (
        (archive_canonical, raw_edge.revision_id, "archive"),
        (rest_canonical, raw_edge.superseded_revision_id, "rest"),
    )
    for record, raw_revision, name in pairs:
        if not isinstance(record, RevisionRecord):
            raise CanonicalRuleViolation(f"{name} endpoint must be a RevisionRecord")
        if record.observation_key != raw_edge.observation_key:
            raise CanonicalRuleViolation(f"{name} endpoint belongs to another observation_key")
        tail = f"|{raw_revision}"
        if not record.source_id.startswith(f"{_NORMALIZER_IDENTITY}|") or not (
            record.source_id.endswith(tail)
        ):
            raise CanonicalRuleViolation(
                f"{name} endpoint {record.revision_id} is not the Canonical image of {raw_revision}"
            )
        channel = raw_channel_of(record.source_id.split("|")[1])
        if channel.name != name:
            raise CanonicalRuleViolation(f"{name} endpoint comes from the {channel.name} channel")
    knowledge = max(
        raw_edge.knowledge_time,
        archive_canonical.availability.times.knowledge_time,
        rest_canonical.availability.times.knowledge_time,
    )
    try:
        return PrecedenceEvidence(
            observation_key=raw_edge.observation_key,
            revision_id=archive_canonical.revision_id,
            superseded_revision_id=rest_canonical.revision_id,
            policy=PRECEDENCE_MAP_BINDING,
            evidence=(
                PRECEDENCE_MAP_STATEMENT,
                f"raw_edge_id={raw_edge_id}",
                f"raw_evidence_table={BINANCE_SPOT_PRECEDENCE_EVIDENCE.table}"
                f"@snapshot:{raw_evidence_snapshot_id}",
                *raw_edge.evidence,
            ),
            knowledge_time=knowledge,
        )
    except ValueError as exc:
        raise CanonicalRuleViolation(f"no lawful mapped edge: {exc}") from None

"""Precedence policy ``binance.spot.delivery-channel@1.0.0`` — D-33 option A (Phase 1 D3B).

ADR-0027 §4. This is a **project-defined, versioned** policy: "canonical market content equal"
plus "ADR-0022 designates the official archive as the authoritative backfill channel". It is
**not** a revision order declared by Binance and says nothing about which channel published
first or corrected later.

For one observation key that has an archive element revision and a REST element revision, this
module decides, from the two persisted rows alone:

- ``EQUAL`` — both versioned market-content projections are complete, in domain and their
  canonical JSON is byte-identical → exactly one evidence-only edge
  ``archive revision supersedes REST revision`` may be persisted (``build_channel_edge``);
- ``MISMATCH`` — the projections differ → no edge; competing heads stay; fail closed;
- ``INCOMPARABLE`` — a field is missing / empty / outside the projection domain (for example a
  decimal that ``decimal(38, 18)`` cannot hold exactly, or a kline that is not one minute) →
  no edge;
- ``INTEGRITY_VIOLATION`` — a stored row does not re-derive its own payload hash, revision id,
  source identity, observation key or stored UTC times → no edge, and the caller must abort
  with ``CatalogIntegrityError`` (storage corruption is never "just a mismatch").

Time normalisation is exact: ``raw × unit factor`` epoch microseconds, never truncated or
rounded. A millisecond REST aggTrade therefore never equals a microsecond archive aggTrade whose
timestamp has non-zero sub-millisecond digits (ADR-0027 §4.8), while a 1m kline is equal across
units because its end is normalised to the exclusive interval end.

Pure functions only: no I/O and **no clock**. The edge's ``knowledge_time`` comes from the
caller (the local time the comparison completed, stamped before the edge is committed) and must
not precede either revision's ``knowledge_time``; its identity ``edge_id`` contains no time.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Any, Final

from core.contracts.revision import PolicyBinding, PolicyRole, PrecedenceEvidence
from core.domain.base import canonical_json
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_AGG_TRADES,
    BINANCE_SPOT_KLINES_1M,
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_KLINES_1M,
)
from infrastructure.revision import identity as archive_identity
from infrastructure.revision import rest_identity

__all__ = [
    "AGG_TRADE_PROJECTION",
    "CHANNEL_TABLES",
    "DELIVERY_CHANNEL_BINDING",
    "DELIVERY_CHANNEL_HASH",
    "DELIVERY_CHANNEL_POLICY_ID",
    "DELIVERY_CHANNEL_POLICY_VERSION",
    "DELIVERY_CHANNEL_SPEC",
    "KLINE_1M_PROJECTION",
    "POLICY_STATEMENT",
    "Channel",
    "ChannelComparison",
    "ChannelEdge",
    "ChannelPrecedenceViolation",
    "ChannelRevision",
    "ComparisonOutcome",
    "Projection",
    "build_channel_edge",
    "compare_channels",
    "project",
]

DELIVERY_CHANNEL_POLICY_ID: Final = "binance.spot.delivery-channel"
DELIVERY_CHANNEL_POLICY_VERSION: Final = "1.0.0"

AGG_TRADE_PROJECTION: Final = "binance.spot.agg_trade.market-content/1"
KLINE_1M_PROJECTION: Final = "binance.spot.kline_1m.market-content/1"

POLICY_STATEMENT: Final = (
    "binance.spot.delivery-channel@1.0.0: canonical market content equal; archive is the "
    "ADR-0022 authoritative backfill channel; not a source-declared revision order"
)

_INT64_MAX: Final = (1 << 63) - 1
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND: Final = timedelta(microseconds=1)
_MINUTE_US: Final = 60_000_000
_SCALE: Final = Decimal(1).scaleb(-18)
_INTEGER_DIGITS: Final = 20  # decimal(38, 18)
_SYMBOL_RE: Final = re.compile(r"^[A-Z0-9]{1,32}$")
_SNAPSHOT_RE: Final = re.compile(r"^[0-9]{1,20}$")
#: Declared unit → microseconds per tick.
_UNIT_FACTORS: Final[dict[str, int]] = {"millisecond": 1000, "microsecond": 1}


class ChannelPrecedenceViolation(ValueError):
    """The inputs cannot be evaluated under this policy at all (programming error; fail closed)."""


class Channel(StrEnum):
    ARCHIVE = "archive"
    REST = "rest"


#: ``(channel, data_type)`` → the Raw element table whose rows this policy compares.
CHANNEL_TABLES: Final[dict[tuple[Channel, str], str]] = {
    (Channel.ARCHIVE, "agg_trades"): BINANCE_SPOT_AGG_TRADES.table,
    (Channel.ARCHIVE, "klines_1m"): BINANCE_SPOT_KLINES_1M.table,
    (Channel.REST, "agg_trades"): BINANCE_SPOT_REST_AGG_TRADES.table,
    (Channel.REST, "klines_1m"): BINANCE_SPOT_REST_KLINES_1M.table,
}


class ComparisonOutcome(StrEnum):
    EQUAL = "equal"
    MISMATCH = "mismatch"
    INCOMPARABLE = "incomparable"
    INTEGRITY_VIOLATION = "integrity_violation"


def _check_utc(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ChannelPrecedenceViolation(f"{label} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ChannelPrecedenceViolation(f"{label} must be UTC")
    return value


@dataclass(frozen=True, slots=True)
class ChannelRevision:
    """One persisted element revision as read back from its Raw table at a fixed snapshot.

    ``row`` holds that table's columns (native fields, ``symbol``, the stored UTC times and the
    lineage column). ``time_unit`` is the revision's declared unit: the D1 parser rule for an
    archive row, ``millisecond`` for REST 1.0.0.
    """

    channel: Channel
    data_type: str
    observation_key: str
    revision_id: str
    source_id: str
    payload_hash: str
    knowledge_time: datetime
    snapshot_id: str
    time_unit: str
    row: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.channel, Channel):
            raise ChannelPrecedenceViolation("channel must be a Channel")
        if (self.channel, self.data_type) not in CHANNEL_TABLES:
            raise ChannelPrecedenceViolation(f"unsupported data_type {self.data_type!r}")
        for label, value in (
            ("observation_key", self.observation_key),
            ("revision_id", self.revision_id),
            ("source_id", self.source_id),
            ("payload_hash", self.payload_hash),
        ):
            if not isinstance(value, str) or not value:
                raise ChannelPrecedenceViolation(f"{label} must be a non-empty string")
        _check_utc(self.knowledge_time, "knowledge_time")
        snapshot = self.snapshot_id
        if not isinstance(snapshot, str) or _SNAPSHOT_RE.fullmatch(snapshot) is None:
            raise ChannelPrecedenceViolation("snapshot_id must be an Iceberg snapshot id")
        if self.time_unit not in _UNIT_FACTORS:
            raise ChannelPrecedenceViolation(f"unsupported time unit {self.time_unit!r}")
        if self.channel is Channel.REST and self.time_unit != rest_identity.DECLARED_TIME_UNIT:
            raise ChannelPrecedenceViolation("REST 1.0.0 revisions are declared in milliseconds")
        if not isinstance(self.row, Mapping):
            raise ChannelPrecedenceViolation("row must be a mapping of column values")

    @property
    def table(self) -> str:
        return CHANNEL_TABLES[(self.channel, self.data_type)]


class _Incomparable(Exception):
    """Internal signal: the projection cannot be formed; ``args[0]`` is the reason."""


class _Integrity(Exception):
    """Internal signal: the stored row contradicts itself; ``args[0]`` is the reason."""


def _value(row: Mapping[str, Any], column: str) -> Any:
    if column not in row or row[column] is None:
        raise _Incomparable(f"missing {column}")
    return row[column]


def _int(row: Mapping[str, Any], column: str) -> int:
    value = _value(row, column)
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _INT64_MAX:
        raise _Incomparable(f"{column} is not a non-negative int64")
    return value


def _bool(row: Mapping[str, Any], column: str) -> bool:
    value = _value(row, column)
    if not isinstance(value, bool):
        raise _Incomparable(f"{column} is not a boolean")
    return value


def _decimal(row: Mapping[str, Any], column: str) -> str:
    """Exact ``decimal(38, 18)`` rendering with 18 fractional digits; never rounds."""
    value = _value(row, column)
    if not isinstance(value, Decimal) or not value.is_finite() or value.is_signed():
        raise _Incomparable(f"{column} is not a finite non-negative Decimal")
    with localcontext() as context:
        context.prec = 80
        quantized = value.quantize(_SCALE)
        if quantized != value:
            raise _Incomparable(f"{column} needs more than 18 fractional digits")
    if quantized.adjusted() >= _INTEGER_DIGITS:
        raise _Incomparable(f"{column} exceeds decimal(38, 18)")
    return format(quantized, "f")


def _text(row: Mapping[str, Any], column: str) -> str:
    value = _value(row, column)
    if not isinstance(value, str) or not value:
        raise _Incomparable(f"{column} is empty")
    return value


def _symbol(row: Mapping[str, Any]) -> str:
    value = _text(row, "symbol")
    if _SYMBOL_RE.fullmatch(value) is None:
        raise _Incomparable("symbol is not a venue-native upper-case symbol")
    return value


def _micros_of(row: Mapping[str, Any], column: str) -> int:
    value = _value(row, column)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise _Incomparable(f"{column} is not a UTC timestamp")
    return (value - _EPOCH) // _MICROSECOND


def _scaled(raw: int, factor: int, label: str) -> int:
    micros = raw * factor
    if micros > _INT64_MAX:
        raise _Incomparable(f"{label} overflows int64 microseconds")
    return micros


@dataclass(frozen=True, slots=True)
class Projection:
    """A complete, in-domain market-content projection and its canonical digest."""

    kind: str
    document: Mapping[str, Any] = field(repr=False)
    canonical: str = field(repr=False)
    sha256: str = ""


def _projection(kind: str, document: dict[str, Any]) -> Projection:
    text = canonical_json({"kind": kind, **document})
    return Projection(
        kind=kind,
        document={"kind": kind, **document},
        canonical=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _project(revision: ChannelRevision) -> Projection:
    row = revision.row
    factor = _UNIT_FACTORS[revision.time_unit]
    if revision.data_type == "agg_trades":
        event_time_us = _scaled(_int(row, "timestamp_raw"), factor, "timestamp")
        if _micros_of(row, "event_time") != event_time_us:
            raise _Integrity("stored event_time is not timestamp_raw in the declared unit")
        return _projection(
            AGG_TRADE_PROJECTION,
            {
                "symbol": _symbol(row),
                "agg_trade_id": _int(row, "agg_trade_id"),
                "price": _decimal(row, "price"),
                "quantity": _decimal(row, "quantity"),
                "first_trade_id": _int(row, "first_trade_id"),
                "last_trade_id": _int(row, "last_trade_id"),
                "event_time_us": event_time_us,
                "is_buyer_maker": _bool(row, "is_buyer_maker"),
                "is_best_match": _bool(row, "is_best_match"),
            },
        )
    start_us = _scaled(_int(row, "open_time_raw"), factor, "open time")
    close_raw = _int(row, "close_time_raw")
    if close_raw >= _INT64_MAX:
        raise _Incomparable("close_time_raw has no successor tick")
    end_us = _scaled(close_raw + 1, factor, "close time")
    if end_us - start_us != _MINUTE_US:
        raise _Incomparable("kline is not exactly one minute (close != open + 1m - 1 tick)")
    if _micros_of(row, "interval_start") != start_us:
        raise _Integrity("stored interval_start is not open_time_raw in the declared unit")
    if _micros_of(row, "interval_end") != end_us:
        raise _Integrity("stored interval_end is not the exclusive end of the kline")
    return _projection(
        KLINE_1M_PROJECTION,
        {
            "symbol": _symbol(row),
            "interval": "1m",
            "interval_start_us": start_us,
            "interval_end_us": end_us,
            "open": _decimal(row, "open"),
            "high": _decimal(row, "high"),
            "low": _decimal(row, "low"),
            "close": _decimal(row, "close"),
            "volume": _decimal(row, "volume"),
            "quote_asset_volume": _decimal(row, "quote_asset_volume"),
            "number_of_trades": _int(row, "number_of_trades"),
            "taker_buy_base_asset_volume": _decimal(row, "taker_buy_base_asset_volume"),
            "taker_buy_quote_asset_volume": _decimal(row, "taker_buy_quote_asset_volume"),
            "ignore": _text(row, "ignore_raw"),
        },
    )


def project(revision: ChannelRevision) -> Projection:
    """The market-content projection of ``revision``, or ``ValueError`` explaining why not."""
    if not isinstance(revision, ChannelRevision):
        raise ChannelPrecedenceViolation("revision must be a ChannelRevision")
    try:
        return _project(revision)
    except (_Incomparable, _Integrity) as exc:
        raise ValueError(str(exc.args[0])) from None


def _verify_identity(revision: ChannelRevision) -> None:
    """The stored row must re-derive its own identity under its own channel's rule."""
    row = revision.row
    symbol = row["symbol"]
    try:
        if revision.data_type == "agg_trades":
            key = rest_identity.agg_trade_observation_key(symbol, row["agg_trade_id"])
        else:
            key = rest_identity.kline_1m_observation_key(symbol, row["interval_start"])
        if revision.channel is Channel.ARCHIVE:
            source = archive_identity.row_source_identity(_text(row, "archive_revision_id"))
            if revision.data_type == "agg_trades":
                payload = archive_identity.agg_trade_payload_hash(
                    symbol, revision.time_unit, dict(row)
                )
            else:
                payload = archive_identity.kline_1m_payload_hash(
                    symbol, revision.time_unit, dict(row)
                )
            derived_id = archive_identity.revision_id(key, source, payload)
        else:
            source = rest_identity.rest_source_identity()
            if revision.data_type == "agg_trades":
                payload = rest_identity.agg_trade_payload_hash(symbol, dict(row))
            else:
                payload = rest_identity.kline_1m_payload_hash(symbol, dict(row))
            derived_id = rest_identity.revision_id(key, source, payload)
    except (ValueError, KeyError, _Incomparable) as exc:
        raise _Integrity(f"{revision.table}: identity cannot be re-derived ({exc})") from None
    for label, stored, derived in (
        ("observation_key", revision.observation_key, key),
        ("source_id", revision.source_id, source),
        ("payload_hash", revision.payload_hash, payload),
        ("revision_id", revision.revision_id, derived_id),
    ):
        if stored != derived:
            raise _Integrity(f"{revision.table}: stored {label} does not re-derive from the row")


@dataclass(frozen=True, slots=True)
class ChannelComparison:
    """The policy judgement for one (archive, REST) pair of one observation key."""

    outcome: ComparisonOutcome
    projection_kind: str
    archive: ChannelRevision
    rest: ChannelRevision
    archive_projection_sha256: str | None = None
    rest_projection_sha256: str | None = None
    reasons: tuple[str, ...] = ()

    @property
    def observation_key(self) -> str:
        return self.archive.observation_key

    @property
    def equal(self) -> bool:
        return self.outcome is ComparisonOutcome.EQUAL


def compare_channels(archive: ChannelRevision, rest: ChannelRevision) -> ChannelComparison:
    """Apply ``binance.spot.delivery-channel@1.0.0`` to one archive / REST pair.

    Order: form both projections (missing / out-of-domain → ``INCOMPARABLE``); prove each row
    re-derives its own identity and UTC times (→ ``INTEGRITY_VIOLATION``); compare canonical
    JSON byte for byte (→ ``EQUAL`` / ``MISMATCH`` listing the differing fields).
    """
    if not isinstance(archive, ChannelRevision) or archive.channel is not Channel.ARCHIVE:
        raise ChannelPrecedenceViolation("archive must be an archive-channel ChannelRevision")
    if not isinstance(rest, ChannelRevision) or rest.channel is not Channel.REST:
        raise ChannelPrecedenceViolation("rest must be a REST-channel ChannelRevision")
    if archive.data_type != rest.data_type:
        raise ChannelPrecedenceViolation("both revisions must have the same data_type")
    if archive.observation_key != rest.observation_key:
        raise ChannelPrecedenceViolation("the policy only relates revisions of one observation_key")
    if archive.revision_id == rest.revision_id:
        raise ChannelPrecedenceViolation("an archive and a REST revision cannot share an id")
    kind = AGG_TRADE_PROJECTION if archive.data_type == "agg_trades" else KLINE_1M_PROJECTION

    projections: dict[Channel, Projection] = {}
    incomparable: list[str] = []
    integrity: list[str] = []
    for revision in (archive, rest):
        try:
            projections[revision.channel] = _project(revision)
        except _Incomparable as exc:
            incomparable.append(f"{revision.channel.value}: {exc.args[0]}")
        except _Integrity as exc:
            integrity.append(f"{revision.channel.value}: {exc.args[0]}")
    if not integrity:
        for revision in (archive, rest):
            if revision.channel in projections:
                try:
                    _verify_identity(revision)
                except _Integrity as exc:
                    integrity.append(f"{revision.channel.value}: {exc.args[0]}")

    def result(outcome: ComparisonOutcome, reasons: list[str]) -> ChannelComparison:
        left, right = projections.get(Channel.ARCHIVE), projections.get(Channel.REST)
        return ChannelComparison(
            outcome=outcome,
            projection_kind=kind,
            archive=archive,
            rest=rest,
            archive_projection_sha256=None if left is None else left.sha256,
            rest_projection_sha256=None if right is None else right.sha256,
            reasons=tuple(reasons),
        )

    if integrity:
        return result(ComparisonOutcome.INTEGRITY_VIOLATION, integrity)
    if incomparable:
        return result(ComparisonOutcome.INCOMPARABLE, incomparable)
    left, right = projections[Channel.ARCHIVE], projections[Channel.REST]
    if left.canonical == right.canonical:
        return result(ComparisonOutcome.EQUAL, [])
    differing = sorted(
        name for name in left.document if left.document[name] != right.document.get(name)
    )
    return result(ComparisonOutcome.MISMATCH, [f"differs: {name}" for name in differing])


@dataclass(frozen=True, slots=True)
class ChannelEdge:
    """One evidence-only edge and the columns of ``raw.binance_spot_precedence_evidence``."""

    edge_id: str
    evidence: PrecedenceEvidence
    revision_table: str
    superseded_table: str
    revision_snapshot_id: str
    superseded_snapshot_id: str
    projection_sha256: str

    def row(self) -> dict[str, Any]:
        """The evidence-table row (column name → value); a pure mapping, no write."""
        item = self.evidence
        return {
            "edge_id": self.edge_id,
            "observation_key": item.observation_key,
            "revision_id": item.revision_id,
            "revision_table": self.revision_table,
            "superseded_revision_id": item.superseded_revision_id,
            "superseded_table": self.superseded_table,
            "policy_id": item.policy.policy_id,
            "policy_version": item.policy.version,
            "policy_hash": item.policy.policy_hash,
            "evidence": list(item.evidence),
            "knowledge_time": item.knowledge_time,
            "revision_snapshot_id": self.revision_snapshot_id,
            "superseded_snapshot_id": self.superseded_snapshot_id,
            "projection_sha256": self.projection_sha256,
            "contract_schema_version": item.schema_version,
        }


def build_channel_edge(comparison: ChannelComparison, *, knowledge_time: datetime) -> ChannelEdge:
    """The evidence-only edge ``archive supersedes REST`` for an ``EQUAL`` comparison.

    ``knowledge_time`` is supplied by the caller: the local time the comparison completed. It
    must not precede either revision's ``knowledge_time`` (never backfilled, ADR-0027 §4.6).
    """
    if not isinstance(comparison, ChannelComparison):
        raise ChannelPrecedenceViolation("comparison must be a ChannelComparison")
    if comparison.outcome is not ComparisonOutcome.EQUAL:
        raise ChannelPrecedenceViolation(
            f"no edge for a {comparison.outcome.value} comparison: competing heads stay"
        )
    _check_utc(knowledge_time, "knowledge_time")
    archive, rest = comparison.archive, comparison.rest
    floor = max(archive.knowledge_time, rest.knowledge_time)
    if knowledge_time < floor:
        raise ChannelPrecedenceViolation(
            "edge knowledge_time precedes a revision's knowledge_time: an edge is never "
            "backfilled before both sides were known"
        )
    digest = comparison.archive_projection_sha256
    assert digest is not None and digest == comparison.rest_projection_sha256
    evidence = PrecedenceEvidence(
        observation_key=comparison.observation_key,
        revision_id=archive.revision_id,
        superseded_revision_id=rest.revision_id,
        policy=DELIVERY_CHANNEL_BINDING,
        evidence=(
            POLICY_STATEMENT,
            f"projection={comparison.projection_kind}",
            f"projection_sha256={digest}",
            f"archive_payload_hash={archive.payload_hash}",
            f"rest_payload_hash={rest.payload_hash}",
            f"archive_revision_table={archive.table}@snapshot:{archive.snapshot_id}",
            f"rest_revision_table={rest.table}@snapshot:{rest.snapshot_id}",
        ),
        knowledge_time=knowledge_time,
    )
    return ChannelEdge(
        edge_id=rest_identity.edge_id(
            DELIVERY_CHANNEL_BINDING,
            comparison.observation_key,
            archive.revision_id,
            rest.revision_id,
        ),
        evidence=evidence,
        revision_table=archive.table,
        superseded_table=rest.table,
        revision_snapshot_id=archive.snapshot_id,
        superseded_snapshot_id=rest.snapshot_id,
        projection_sha256=digest,
    )


_DECIMAL_RULE: Final = "exact value as decimal(38, 18) fixed-point text with 18 fractional digits"

#: The complete, versioned policy document; ``DELIVERY_CHANNEL_HASH`` is its JSON SHA-256.
DELIVERY_CHANNEL_SPEC: Final[dict[str, Any]] = {
    "policy_id": DELIVERY_CHANNEL_POLICY_ID,
    "version": DELIVERY_CHANNEL_POLICY_VERSION,
    "role": PolicyRole.PRECEDENCE.value,
    "nature": (
        "project-defined policy: canonical market content equality plus the ADR-0022 "
        "designation of the official archive as the authoritative backfill channel; not a "
        "source-declared revision order"
    ),
    "decision": "ADR-0027 section 4 (D-33 option A, selected by Codex)",
    "scope": {
        "venue": "binance",
        "market": "spot",
        "pairs": {
            "agg_trades": [BINANCE_SPOT_AGG_TRADES.table, BINANCE_SPOT_REST_AGG_TRADES.table],
            "klines_1m": [BINANCE_SPOT_KLINES_1M.table, BINANCE_SPOT_REST_KLINES_1M.table],
        },
        "out_of_scope": ["archive replacements", "REST-internal revisions"],
    },
    "evidence_document": "docs/architecture/evidence/binance-spot-rest-market-data.md",
    "projections": {
        AGG_TRADE_PROJECTION: {
            "fields": [
                "symbol",
                "agg_trade_id",
                "price",
                "quantity",
                "first_trade_id",
                "last_trade_id",
                "event_time_us",
                "is_buyer_maker",
                "is_best_match",
            ],
            "event_time_us": "timestamp_raw x unit factor, exact",
        },
        KLINE_1M_PROJECTION: {
            "fields": [
                "symbol",
                "interval",
                "interval_start_us",
                "interval_end_us",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "quote_asset_volume",
                "number_of_trades",
                "taker_buy_base_asset_volume",
                "taker_buy_quote_asset_volume",
                "ignore",
            ],
            "interval_start_us": "open_time_raw x unit factor, exact",
            "interval_end_us": "(close_time_raw + 1 tick) x unit factor; must be start + 1m",
            "ignore": "source text compared byte for byte",
        },
    },
    "normalisation": {
        "integer": "base-10, non-negative, int64",
        "decimal": _DECIMAL_RULE,
        "decimal_out_of_domain": "more than 18 fractional digits, more than 20 integer digits, "
        "negative or non-finite: incomparable, never rounded",
        "boolean": "JSON true / false after each channel's strict parse",
        "time": "exact UTC epoch microseconds = raw x factor (millisecond 1000, microsecond 1)",
        "text": "symbol as-is; ignore as source text",
        "excluded": [
            "time_unit",
            "timestamp_raw",
            "open_time_raw",
            "close_time_raw",
            "identity, lineage and time-axis columns",
            "arrival_seq",
            "archive_line_number",
            "element_index",
            "parser and decoder bindings",
        ],
    },
    "equality": "both projections complete and in domain; canonical JSON byte-identical",
    "integrity": "each row must re-derive its observation key, source identity, payload hash, "
    "revision id and stored UTC times under its own channel's identity rule",
    "outcomes": {
        "equal": "one evidence-only edge: archive revision supersedes REST revision",
        "mismatch": "no edge; competing heads; fail closed",
        "incomparable": "no edge; competing heads; fail closed",
        "integrity_violation": "no edge; the caller aborts with CatalogIntegrityError",
        "no_counterpart": "nothing to record",
    },
    "edge": {
        "direction": "archive -> REST",
        "carrier": "raw.binance_spot_precedence_evidence (never a revision row's supersedes)",
        "identity": "edge1-<sha256> without knowledge_time (rest identity rule)",
        "evidence_items": [
            "policy statement",
            "projection kind",
            "projection sha256",
            "archive payload hash",
            "REST payload hash",
            "archive table and snapshot",
            "REST table and snapshot",
        ],
        "knowledge_time": "supplied by the caller after the comparison completed; >= both "
        "revisions' knowledge_time; never backfilled; first committed record is authoritative",
    },
    "never_evidence": [
        "retrieved_at",
        "ingest_time order",
        "knowledge_time order",
        "arrival order",
        "arrival_seq",
        "http headers",
        "payload hash order",
        "local wall clock",
    ],
    "known_consequence": "a millisecond REST aggTrade never equals a microsecond archive aggTrade "
    "whose timestamp has non-zero sub-millisecond digits (fail closed)",
}
DELIVERY_CHANNEL_HASH: Final = hashlib.sha256(
    canonical_json(DELIVERY_CHANNEL_SPEC).encode("utf-8")
).hexdigest()
DELIVERY_CHANNEL_BINDING: Final = PolicyBinding(
    role=PolicyRole.PRECEDENCE,
    policy_id=DELIVERY_CHANNEL_POLICY_ID,
    version=DELIVERY_CHANNEL_POLICY_VERSION,
    policy_hash=DELIVERY_CHANNEL_HASH,
)

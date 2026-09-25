"""The fourteen Phase 1 production Iceberg tables (03-data.md §7.1; roadmap #9, C3+D3B+E2+QG-1).

Single entry point for the production layout: ``PHASE1_TABLES`` (fourteen definitions, frozen
logical names, ``version = 1.0.0``, ``definition_id`` = table name), ``PHASE1_REGISTRY`` and the
idempotent ``ensure_phase1_tables``. The first eight are the C3 first slice and are unchanged by
D3B; the next four are the ADR-0027 REST additions (three REST Raw tables + the independent
precedence-evidence table); the thirteenth is the ADR-0029 additive exchangeInfo snapshot table
(E2); the fourteenth is the ADR-0031 additive quality evidence-gap table (QG-1). Appending never
changes an earlier definition or its hash. C2 test-only definitions live under ``tests/`` and
never enter this registry.

Every field ID is written out below and equals the ID Iceberg assigns on table creation (top-level
fields first, then nested fields depth-first); the module refuses to import otherwise. Field docs
are Iceberg column docs and part of the hashed definition document. Types: timestamps are
``timestamptz`` (microseconds, UTC); exchange decimals are ``decimal(38, 18)``; durations are
integer microseconds; no floating point. All batches are fingerprinted with
``hlens.pyarrow-batch-sha256@1.0.0``.

What these tables carry (C3 freezes only the physical format; the semantics come from the
Accepted ADRs and contracts, the producers come in later batches):

- **Revision block** (Raw rows, Canonical rows, listings): ``RevisionRecord`` +
  ``AvailabilityDecision`` + ``ObservationTimes`` (ADR-0023). ``supersedes`` / evidence lists are
  sets in canonical (sorted) order. ``precedence_evidence`` holds the ``PrecedenceEvidence`` edges
  persisted with this revision at ingest, i.e. those whose newer side is this row's
  ``revision_id`` under this row's ``observation_key``. ``contract_schema_version`` is the
  contract envelope version of the nested contracts, so rows map back to contracts with the same
  content hash. Interval tables name the historical start / end ``interval_start`` /
  ``interval_end`` (= ``ObservationTimes.event_time`` / ``event_end_time``); instantaneous tables
  have no end column (``event_end_time`` is null by definition).
- **REST tables (ADR-0027)**: ``raw.binance_spot_rest_responses`` is the Raw source payload
  revision of one HTTP response page; the two REST element tables bind ``response_revision_id`` +
  ``element_index`` + the ``PolicyBinding(role=parser)`` of the REST decoder instead of the archive
  lineage columns; their ``precedence_evidence`` column has the frozen shape (newer side = this row)
  and carries same-channel edges only. ``raw.binance_spot_precedence_evidence`` holds one complete
  ``PrecedenceEvidence`` per row with **both** ends explicit (the D-33 cross-channel edges).
- **exchangeInfo snapshots (ADR-0029)**: ``raw.binance_spot_exchange_info`` is the Raw source
  revision of one successful ``GET /api/v3/exchangeInfo`` answer (request identity + body bytes);
  it carries the requested symbols and, per requested symbol present in the answer, the four
  native fields the listing derivation reads. ``canonical.instrument_listings`` rows derived from
  it name it as both lineage hops.
- **Raw lineage**: parsed Raw rows bind ``archive_revision_id`` (the ``raw.binance_spot_archives``
  revision) + line number + the ``PolicyBinding(role=parser)``; Canonical rows and listings carry
  the ``SelectedRevisionLineage`` hops as ``lineage_*`` columns.
- **Bindings** (03-data.md §7.3): source binding on archive rows, parser binding on parsed rows,
  availability / precedence policy bindings per revision / edge, universe spec binding on manifests.
- **Manifests**: queryable identity columns plus ``manifest_json``, the contract canonical JSON
  (``core.domain.base.canonical_json``: sorted keys, compact separators, non-ASCII kept, no NaN)
  of ``ResearchDatasetManifest.model_dump(mode="json")`` encoded as UTF-8. Its SHA-256 equals
  ``manifest_content_hash`` (the manifest has no non-semantic fields), and
  ``ResearchDatasetManifest.model_validate_json(manifest_json)`` rebuilds the manifest.
- **Quality reports**: no quality-report contract exists yet; the table is a generic carrier keyed
  by ``report_id`` (the ID referenced by manifests and ``AvailabilityEvidenceGap``). The event
  taxonomy and the quality rules are proposed with batch E.
- **Quality evidence gaps (ADR-0031, QG-1)**: ``quality.availability_evidence_gaps`` is an
  additive, append-only table carrying one row per ``AvailabilityEvidenceGap`` referenced by a
  quality report (``quality_report_id`` + ``table`` + ``revision_id`` + ``gap``), partitioned by
  ``identity(subject_symbol) + day(subject_start)`` so a report's gaps can be written in bounded,
  time-sliced batches instead of one unbounded report row. The report row itself is the only
  reference to a batch of gap rows; nothing here changes ``AvailabilityEvidenceGap`` or
  ``quality.data_quality_reports``.

This module creates no data and derives no semantics: revision IDs, observation keys, policy
evidence, parsing, Canonical conversion and PIT selection belong to batches D ~ F.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from pyiceberg.partitioning import UNPARTITIONED_PARTITION_SPEC, PartitionField, PartitionSpec
from pyiceberg.schema import Schema, assign_fresh_schema_ids
from pyiceberg.transforms import DayTransform, IdentityTransform
from pyiceberg.types import (
    BooleanType,
    DecimalType,
    IcebergType,
    ListType,
    LongType,
    NestedField,
    StringType,
    StructType,
    TimestamptzType,
)

from core.contracts.catalog import TableDefinition
from infrastructure.catalog.definitions import RegisteredTableDefinition, TableDefinitionRegistry
from infrastructure.catalog.fingerprint import PYARROW_BATCH_FINGERPRINT
from infrastructure.catalog.iceberg_adapter import PyIcebergCatalogAdapter

__all__ = [
    "BINANCE_SPOT_AGG_TRADES",
    "BINANCE_SPOT_ARCHIVES",
    "BINANCE_SPOT_EXCHANGE_INFO",
    "BINANCE_SPOT_KLINES_1M",
    "BINANCE_SPOT_PRECEDENCE_EVIDENCE",
    "BINANCE_SPOT_REST_AGG_TRADES",
    "BINANCE_SPOT_REST_KLINES_1M",
    "BINANCE_SPOT_REST_RESPONSES",
    "CANONICAL_BARS_1M",
    "CANONICAL_INSTRUMENT_LISTINGS",
    "CANONICAL_TRADES",
    "DATASET_MANIFESTS",
    "DATA_QUALITY_REPORTS",
    "EXCHANGE_DECIMAL",
    "PHASE1_DEFINITION_VERSION",
    "PHASE1_REGISTRY",
    "PHASE1_TABLES",
    "PHASE1_TABLE_PROPERTIES",
    "Phase1TableState",
    "QUALITY_EVIDENCE_GAPS",
    "describe_partition_spec",
    "ensure_phase1_tables",
]

PHASE1_DEFINITION_VERSION: Final = "1.0.0"
#: Exchange prices / quantities / volumes (Binance spot uses at most 8 decimals).
EXCHANGE_DECIMAL: Final = DecimalType(38, 18)
#: Non-binding table properties shared by all Phase 1 tables (part of each definition hash).
PHASE1_TABLE_PROPERTIES: Final[Mapping[str, str]] = {"write.parquet.compression-codec": "zstd"}

_S: Final = StringType()
_L: Final = LongType()
_T: Final = TimestamptzType()
_B: Final = BooleanType()
_D: Final = EXCHANGE_DECIMAL


def _req(field_id: int, name: str, field_type: IcebergType, doc: str) -> NestedField:
    return NestedField(field_id, name, field_type, required=True, doc=doc)


def _opt(field_id: int, name: str, field_type: IcebergType, doc: str) -> NestedField:
    return NestedField(field_id, name, field_type, required=False, doc=doc)


def _strings(element_id: int) -> ListType:
    return ListType(element_id, _S, element_required=True)


def _precedence_evidence(
    field_id: int, element_id: int, child_ids: tuple[int, int, int, int, int, int, int]
) -> NestedField:
    """``PrecedenceEvidence`` edges persisted with this revision (newer side = this row)."""
    superseded, policy_id, policy_version, policy_hash, evidence, knowledge, evidence_item = (
        child_ids
    )
    edge = StructType(
        _req(superseded, "superseded_revision_id", _S, "PrecedenceEvidence.superseded_revision_id"),
        _req(policy_id, "policy_id", _S, "precedence PolicyBinding.policy_id"),
        _req(policy_version, "policy_version", _S, "precedence PolicyBinding.version"),
        _req(policy_hash, "policy_hash", _S, "precedence PolicyBinding.policy_hash (SHA-256 hex)"),
        _req(evidence, "evidence", _strings(evidence_item), "PrecedenceEvidence.evidence"),
        _req(knowledge, "knowledge_time", _T, "PrecedenceEvidence.knowledge_time"),
    )
    return _req(
        field_id,
        "precedence_evidence",
        ListType(element_id, edge, element_required=True),
        "ingest-time PrecedenceEvidence for this revision's supersedes edges",
    )


# Docs shared by the revision block (ADR-0023; core/contracts/revision.py).
_OBSERVATION_KEY = "RevisionRecord.observation_key: stable key of the business observation"
_REVISION_ID = "RevisionRecord.revision_id: stable revision identity"
_SOURCE_ID = "RevisionRecord.source_id: identity of the source object / message"
_PAYLOAD_HASH = "RevisionRecord.payload_hash: SHA-256 hex of this revision's payload"
_ARRIVAL_SEQ = "RevisionRecord.arrival_seq: local append order; audit only, never precedence"
_SUPERSEDES = "RevisionRecord.supersedes: revision_ids this revision supersedes (set, sorted)"
_SOURCE_REVISION_ID = "RevisionRecord.source_revision_id: source-declared revision id"
_SOURCE_REVISION_TIME = "RevisionRecord.source_revision_time: source-declared revision time"
_EVENT_TIME = "ObservationTimes.event_time: when the market event happened (UTC)"
_EVENT_END_TIME = "ObservationTimes.event_end_time: end of an interval observation (UTC)"
_INTERVAL_START = "ObservationTimes.event_time: interval start, inclusive (UTC)"
_INTERVAL_END = "ObservationTimes.event_end_time: interval end, exclusive (UTC)"
_SOURCE_TIME = "ObservationTimes.source_time: source-declared publish time; null if none"
_AVAILABLE_TIME = "ObservationTimes.available_time: historical availability (policy)"
_INGEST_TIME = "ObservationTimes.ingest_time: first local receipt of this payload"
_KNOWLEDGE_TIME = "ObservationTimes.knowledge_time: local time the revision became selectable"
_DECLARED_LATENCY = "ObservationTimes.declared_latency in microseconds (>= 0)"
_AVAILABILITY_ID = "AvailabilityDecision.policy.policy_id (role=availability)"
_AVAILABILITY_VERSION = "AvailabilityDecision.policy.version"
_AVAILABILITY_HASH = "AvailabilityDecision.policy.policy_hash (SHA-256 hex)"
_AVAILABILITY_EVIDENCE = "AvailabilityDecision.evidence (empty iff a gap is recorded)"
_AVAILABILITY_GAP = "AvailabilityDecision.evidence_gap (null iff evidence is given)"
_CONTRACT_VERSION = "contract envelope schema_version of the nested revision contracts"
_SYMBOL = "venue-native instrument symbol, e.g. BTCUSDT"
_ARCHIVE_REVISION = "revision_id of the raw.binance_spot_archives revision this row was parsed from"
_ARCHIVE_LINE = "1-based line number of this row in the decompressed archive CSV"
_PARSER_ID = "parser PolicyBinding.policy_id (role=parser)"
_PARSER_VERSION = "parser PolicyBinding.version"
_PARSER_HASH = "parser PolicyBinding.policy_hash (SHA-256 hex)"
_LINEAGE_RAW_TABLE = "SelectedRevisionLineage.raw_table (raw namespace)"
_LINEAGE_RAW_REVISION = "SelectedRevisionLineage.raw_revision_id"
_LINEAGE_SOURCE_TABLE = "SelectedRevisionLineage.source_table (raw namespace)"
_LINEAGE_SOURCE_REVISION = "SelectedRevisionLineage.source_revision_id"
_RESPONSE_REVISION = (
    "revision_id of the raw.binance_spot_rest_responses revision that first delivered this element"
)
_ELEMENT_INDEX = "0-based position of this element in the delivering response array"
_DECODER_ID = "decoder PolicyBinding.policy_id (role=parser), e.g. binance.spot.rest.decoder"
_DECODER_VERSION = "decoder PolicyBinding.version"
_DECODER_HASH = "decoder PolicyBinding.policy_hash (SHA-256 hex)"


def _definition(
    table: str, schema: Schema, spec: PartitionSpec = UNPARTITIONED_PARTITION_SPEC
) -> RegisteredTableDefinition:
    fresh = assign_fresh_schema_ids(schema)
    if fresh.model_dump_json() != schema.model_dump_json():
        raise ValueError(f"{table}: declared field IDs differ from Iceberg's creation-time IDs")
    definition = RegisteredTableDefinition(
        table=table,
        definition_id=table,
        version=PHASE1_DEFINITION_VERSION,
        schema=schema,
        fingerprint_rule=PYARROW_BATCH_FINGERPRINT,
        partition_spec=spec,
        properties=PHASE1_TABLE_PROPERTIES,
    )
    if definition.partition_spec.model_dump_json() != spec.model_dump_json():
        raise ValueError(f"{table}: declared partition IDs differ from Iceberg's creation-time IDs")
    return definition


def _symbol_day_spec(symbol_id: int, time_id: int, time_column: str) -> PartitionSpec:
    return PartitionSpec(
        PartitionField(
            source_id=symbol_id, field_id=1000, transform=IdentityTransform(), name="symbol"
        ),
        PartitionField(
            source_id=time_id, field_id=1001, transform=DayTransform(), name=f"{time_column}_day"
        ),
    )


# --------------------------------------------------------------------------- raw


BINANCE_SPOT_ARCHIVES: Final = _definition(
    "raw.binance_spot_archives",
    Schema(
        _req(1, "observation_key", _S, _OBSERVATION_KEY),
        _req(2, "revision_id", _S, _REVISION_ID),
        _req(3, "source_id", _S, _SOURCE_ID),
        _req(4, "payload_hash", _S, _PAYLOAD_HASH),
        _req(5, "arrival_seq", _L, _ARRIVAL_SEQ),
        _req(6, "supersedes", _strings(40), _SUPERSEDES),
        _opt(7, "source_revision_id", _S, _SOURCE_REVISION_ID),
        _opt(8, "source_revision_time", _T, _SOURCE_REVISION_TIME),
        _req(9, "event_time", _T, _EVENT_TIME),
        _opt(10, "event_end_time", _T, _EVENT_END_TIME),
        _opt(11, "source_time", _T, _SOURCE_TIME),
        _req(12, "available_time", _T, _AVAILABLE_TIME),
        _req(13, "ingest_time", _T, _INGEST_TIME),
        _req(14, "knowledge_time", _T, _KNOWLEDGE_TIME),
        _req(15, "declared_latency_us", _L, _DECLARED_LATENCY),
        _req(16, "availability_policy_id", _S, _AVAILABILITY_ID),
        _req(17, "availability_policy_version", _S, _AVAILABILITY_VERSION),
        _req(18, "availability_policy_hash", _S, _AVAILABILITY_HASH),
        _req(19, "availability_evidence", _strings(41), _AVAILABILITY_EVIDENCE),
        _opt(20, "availability_evidence_gap", _S, _AVAILABILITY_GAP),
        _precedence_evidence(21, 42, (43, 44, 45, 46, 47, 48, 49)),
        _req(22, "contract_schema_version", _S, _CONTRACT_VERSION),
        _req(
            23, "source_binding_id", _S, "SourceBinding.source_id, e.g. binance.public.spot.archive"
        ),
        _req(24, "source_binding_version", _S, "SourceBinding.version, e.g. 1.0.0"),
        _req(25, "collector_id", _S, "CollectionResult.collector_id"),
        _req(26, "collector_version", _S, "CollectionResult.collector_version"),
        _req(27, "collection_request_id", _S, "CollectionRequest.request_id"),
        _req(28, "data_type", _S, "CollectionRequest.data_type (archive data type)"),
        _req(29, "symbol", _S, _SYMBOL),
        _req(
            30, "coverage_start", _T, "CollectedObject.coverage_start: archive coverage, inclusive"
        ),
        _req(31, "coverage_end", _T, "CollectedObject.coverage_end: archive coverage, exclusive"),
        _req(32, "source_uri", _S, "CollectedObject.source_uri: download URI of the archive"),
        _req(33, "retrieved_at", _T, "CollectedObject.retrieved_at (knowledge-axis evidence)"),
        _req(34, "source_sha256", _S, "CollectedObject.source_sha256: SHA-256 from .CHECKSUM"),
        _req(35, "object_key", _S, "ObjectRef.key of the published archive object"),
        _req(36, "object_uri", _S, "ObjectRef.uri: warehouse URI of the archive bytes"),
        _req(37, "object_sha256", _S, "ObjectRef.sha256: SHA-256 computed over the stored bytes"),
        _req(38, "object_size_bytes", _L, "ObjectRef.size in bytes"),
        _req(
            39,
            "source_metadata",
            ListType(
                50,
                StructType(
                    _req(51, "name", _S, "metadata key (lower-case token, e.g. HTTP header)"),
                    _req(52, "value", _S, "metadata value as received"),
                ),
                element_required=True,
            ),
            "CollectedObject.source_metadata as (name, value) pairs sorted by name",
        ),
    ),
)

BINANCE_SPOT_AGG_TRADES: Final = _definition(
    "raw.binance_spot_agg_trades",
    Schema(
        _req(1, "observation_key", _S, _OBSERVATION_KEY),
        _req(2, "revision_id", _S, _REVISION_ID),
        _req(3, "source_id", _S, _SOURCE_ID),
        _req(4, "payload_hash", _S, _PAYLOAD_HASH),
        _req(5, "arrival_seq", _L, _ARRIVAL_SEQ),
        _req(6, "supersedes", _strings(36), _SUPERSEDES),
        _opt(7, "source_revision_id", _S, _SOURCE_REVISION_ID),
        _opt(8, "source_revision_time", _T, _SOURCE_REVISION_TIME),
        _req(9, "event_time", _T, _EVENT_TIME),
        _opt(10, "source_time", _T, _SOURCE_TIME),
        _req(11, "available_time", _T, _AVAILABLE_TIME),
        _req(12, "ingest_time", _T, _INGEST_TIME),
        _req(13, "knowledge_time", _T, _KNOWLEDGE_TIME),
        _req(14, "declared_latency_us", _L, _DECLARED_LATENCY),
        _req(15, "availability_policy_id", _S, _AVAILABILITY_ID),
        _req(16, "availability_policy_version", _S, _AVAILABILITY_VERSION),
        _req(17, "availability_policy_hash", _S, _AVAILABILITY_HASH),
        _req(18, "availability_evidence", _strings(37), _AVAILABILITY_EVIDENCE),
        _opt(19, "availability_evidence_gap", _S, _AVAILABILITY_GAP),
        _precedence_evidence(20, 38, (39, 40, 41, 42, 43, 44, 45)),
        _req(21, "contract_schema_version", _S, _CONTRACT_VERSION),
        _req(22, "symbol", _S, _SYMBOL),
        _req(23, "archive_revision_id", _S, _ARCHIVE_REVISION),
        _req(24, "archive_line_number", _L, _ARCHIVE_LINE),
        _req(25, "parser_id", _S, _PARSER_ID),
        _req(26, "parser_version", _S, _PARSER_VERSION),
        _req(27, "parser_hash", _S, _PARSER_HASH),
        _req(28, "agg_trade_id", _L, "Binance aggregate trade id"),
        _req(29, "price", _D, "Binance price"),
        _req(30, "quantity", _D, "Binance quantity"),
        _req(31, "first_trade_id", _L, "Binance first trade id"),
        _req(32, "last_trade_id", _L, "Binance last trade id"),
        _req(33, "timestamp_raw", _L, "Binance timestamp as in the file (unit set by parser)"),
        _req(34, "is_buyer_maker", _B, "Binance: was the buyer the maker"),
        _opt(35, "is_best_match", _B, "Binance: was the trade the best price match"),
    ),
    _symbol_day_spec(22, 9, "event_time"),
)

BINANCE_SPOT_KLINES_1M: Final = _definition(
    "raw.binance_spot_klines_1m",
    Schema(
        _req(1, "observation_key", _S, _OBSERVATION_KEY),
        _req(2, "revision_id", _S, _REVISION_ID),
        _req(3, "source_id", _S, _SOURCE_ID),
        _req(4, "payload_hash", _S, _PAYLOAD_HASH),
        _req(5, "arrival_seq", _L, _ARRIVAL_SEQ),
        _req(6, "supersedes", _strings(41), _SUPERSEDES),
        _opt(7, "source_revision_id", _S, _SOURCE_REVISION_ID),
        _opt(8, "source_revision_time", _T, _SOURCE_REVISION_TIME),
        _req(9, "interval_start", _T, _INTERVAL_START),
        _req(10, "interval_end", _T, _INTERVAL_END),
        _opt(11, "source_time", _T, _SOURCE_TIME),
        _req(12, "available_time", _T, _AVAILABLE_TIME),
        _req(13, "ingest_time", _T, _INGEST_TIME),
        _req(14, "knowledge_time", _T, _KNOWLEDGE_TIME),
        _req(15, "declared_latency_us", _L, _DECLARED_LATENCY),
        _req(16, "availability_policy_id", _S, _AVAILABILITY_ID),
        _req(17, "availability_policy_version", _S, _AVAILABILITY_VERSION),
        _req(18, "availability_policy_hash", _S, _AVAILABILITY_HASH),
        _req(19, "availability_evidence", _strings(42), _AVAILABILITY_EVIDENCE),
        _opt(20, "availability_evidence_gap", _S, _AVAILABILITY_GAP),
        _precedence_evidence(21, 43, (44, 45, 46, 47, 48, 49, 50)),
        _req(22, "contract_schema_version", _S, _CONTRACT_VERSION),
        _req(23, "symbol", _S, _SYMBOL),
        _req(24, "archive_revision_id", _S, _ARCHIVE_REVISION),
        _req(25, "archive_line_number", _L, _ARCHIVE_LINE),
        _req(26, "parser_id", _S, _PARSER_ID),
        _req(27, "parser_version", _S, _PARSER_VERSION),
        _req(28, "parser_hash", _S, _PARSER_HASH),
        _req(29, "open_time_raw", _L, "Binance kline open time as in the file"),
        _req(30, "open", _D, "Binance open price"),
        _req(31, "high", _D, "Binance high price"),
        _req(32, "low", _D, "Binance low price"),
        _req(33, "close", _D, "Binance close price"),
        _req(34, "volume", _D, "Binance base asset volume"),
        _req(35, "close_time_raw", _L, "Binance kline close time as in the file"),
        _req(36, "quote_asset_volume", _D, "Binance quote asset volume"),
        _req(37, "number_of_trades", _L, "Binance number of trades"),
        _req(38, "taker_buy_base_asset_volume", _D, "Binance taker buy base asset volume"),
        _req(39, "taker_buy_quote_asset_volume", _D, "Binance taker buy quote asset volume"),
        _opt(40, "ignore_raw", _S, "Binance 'ignore' column text as in the file"),
    ),
    _symbol_day_spec(23, 9, "interval_start"),
)

# --------------------------------------------------------------------------- raw / REST (ADR-0027)


BINANCE_SPOT_REST_RESPONSES: Final = _definition(
    "raw.binance_spot_rest_responses",
    Schema(
        _req(1, "observation_key", _S, "binance:spot:rest:<page_identity_sha256>"),
        _req(2, "revision_id", _S, _REVISION_ID),
        _req(3, "source_id", _S, "binance.public.spot.rest@1.0.0 (channel-level source identity)"),
        _req(4, "payload_hash", _S, "SHA-256 hex of the response entity body"),
        _req(5, "arrival_seq", _L, "REST block base in [2**62, 2**63); audit only"),
        _req(6, "supersedes", _strings(54), _SUPERSEDES),
        _opt(7, "source_revision_id", _S, _SOURCE_REVISION_ID),
        _opt(8, "source_revision_time", _T, _SOURCE_REVISION_TIME),
        _req(9, "event_time", _T, "ObservationTimes.event_time = requested_at (UTC)"),
        _req(10, "event_end_time", _T, "ObservationTimes.event_end_time = ingest_time (UTC)"),
        _opt(11, "source_time", _T, _SOURCE_TIME),
        _req(12, "available_time", _T, _AVAILABLE_TIME),
        _req(13, "ingest_time", _T, "ObservationTimes.ingest_time: last response body byte"),
        _req(14, "knowledge_time", _T, _KNOWLEDGE_TIME),
        _req(15, "declared_latency_us", _L, _DECLARED_LATENCY),
        _req(16, "availability_policy_id", _S, _AVAILABILITY_ID),
        _req(17, "availability_policy_version", _S, _AVAILABILITY_VERSION),
        _req(18, "availability_policy_hash", _S, _AVAILABILITY_HASH),
        _req(19, "availability_evidence", _strings(55), _AVAILABILITY_EVIDENCE),
        _opt(20, "availability_evidence_gap", _S, _AVAILABILITY_GAP),
        _precedence_evidence(21, 56, (57, 58, 59, 60, 61, 62, 63)),
        _req(22, "contract_schema_version", _S, _CONTRACT_VERSION),
        _req(23, "source_binding_id", _S, "SourceBinding.source_id, e.g. binance.public.spot.rest"),
        _req(24, "source_binding_version", _S, "SourceBinding.version, e.g. 1.0.0"),
        _req(25, "collector_id", _S, "CollectionResult.collector_id of the first delivery"),
        _req(26, "collector_version", _S, "CollectionResult.collector_version"),
        _req(27, "collection_request_id", _S, "logical CollectionRequest.request_id (first seen)"),
        _req(28, "page_index", _L, "0-based page position in its chain (first delivery)"),
        _req(29, "data_type", _S, "agg_trades | klines_1m"),
        _req(30, "symbol", _S, _SYMBOL),
        _req(31, "request_origin", _S, "page identity origin: configured https://host[:port]"),
        _req(32, "request_path", _S, "page identity path, e.g. /api/v3/aggTrades"),
        _req(33, "request_query", _S, "canonical name-sorted query string of the page"),
        _req(34, "declared_time_unit", _S, "declared unit of the response times (millisecond)"),
        _req(35, "page_limit", _L, "page identity limit (rule constant)"),
        _req(36, "page_identity_sha256", _S, "SHA-256 of the canonical page identity document"),
        _req(37, "source_uri", _S, "CollectedObject.source_uri: the exact request URI"),
        _req(38, "requested_at", _T, "local UTC time the request was handed to the transport"),
        _req(39, "retrieved_at", _T, "CollectedObject.retrieved_at: last body byte = ingest_time"),
        _req(40, "http_status", _L, "HTTP status of the response (200 for every revision)"),
        _req(
            41,
            "source_metadata",
            ListType(
                64,
                StructType(
                    _req(65, "name", _S, "metadata key (lower-case token, e.g. HTTP header)"),
                    _req(66, "value", _S, "metadata value as received"),
                ),
                element_required=True,
            ),
            "CollectedObject.source_metadata as (name, value) pairs sorted by name",
        ),
        _req(42, "object_key", _S, "ObjectRef.key of the published response body"),
        _req(43, "object_uri", _S, "ObjectRef.uri: warehouse URI of the response body"),
        _req(44, "object_sha256", _S, "ObjectRef.sha256: SHA-256 computed over the stored bytes"),
        _req(45, "object_size_bytes", _L, "ObjectRef.size in bytes"),
        _req(46, "decoder_id", _S, _DECODER_ID),
        _req(47, "decoder_version", _S, _DECODER_VERSION),
        _req(48, "decoder_hash", _S, _DECODER_HASH),
        _req(49, "decode_outcome", _S, "strict decode of the first delivery: accepted | rejected"),
        _opt(50, "decode_rejection_code", _S, "decoder rejection code; null iff accepted"),
        _opt(51, "element_count", _L, "decoded element count; null iff rejected"),
        _opt(52, "answered_start", _T, "page answered interval start, inclusive; null if empty"),
        _opt(53, "answered_end", _T, "page answered interval end, exclusive; null if empty"),
    ),
)

BINANCE_SPOT_REST_AGG_TRADES: Final = _definition(
    "raw.binance_spot_rest_agg_trades",
    Schema(
        _req(1, "observation_key", _S, _OBSERVATION_KEY),
        _req(2, "revision_id", _S, _REVISION_ID),
        _req(3, "source_id", _S, "binance.public.spot.rest@1.0.0 (channel-level source identity)"),
        _req(4, "payload_hash", _S, _PAYLOAD_HASH),
        _req(5, "arrival_seq", _L, "REST block base + element_index + 1; audit only"),
        _req(6, "supersedes", _strings(36), _SUPERSEDES),
        _opt(7, "source_revision_id", _S, _SOURCE_REVISION_ID),
        _opt(8, "source_revision_time", _T, _SOURCE_REVISION_TIME),
        _req(9, "event_time", _T, _EVENT_TIME),
        _opt(10, "source_time", _T, _SOURCE_TIME),
        _req(11, "available_time", _T, _AVAILABLE_TIME),
        _req(12, "ingest_time", _T, _INGEST_TIME),
        _req(13, "knowledge_time", _T, _KNOWLEDGE_TIME),
        _req(14, "declared_latency_us", _L, _DECLARED_LATENCY),
        _req(15, "availability_policy_id", _S, _AVAILABILITY_ID),
        _req(16, "availability_policy_version", _S, _AVAILABILITY_VERSION),
        _req(17, "availability_policy_hash", _S, _AVAILABILITY_HASH),
        _req(18, "availability_evidence", _strings(37), _AVAILABILITY_EVIDENCE),
        _opt(19, "availability_evidence_gap", _S, _AVAILABILITY_GAP),
        _precedence_evidence(20, 38, (39, 40, 41, 42, 43, 44, 45)),
        _req(21, "contract_schema_version", _S, _CONTRACT_VERSION),
        _req(22, "symbol", _S, _SYMBOL),
        _req(23, "response_revision_id", _S, _RESPONSE_REVISION),
        _req(24, "element_index", _L, _ELEMENT_INDEX),
        _req(25, "decoder_id", _S, _DECODER_ID),
        _req(26, "decoder_version", _S, _DECODER_VERSION),
        _req(27, "decoder_hash", _S, _DECODER_HASH),
        _req(28, "agg_trade_id", _L, "Binance aggregate trade id (a)"),
        _req(29, "price", _D, "Binance price (p)"),
        _req(30, "quantity", _D, "Binance quantity (q)"),
        _req(31, "first_trade_id", _L, "Binance first trade id (f)"),
        _req(32, "last_trade_id", _L, "Binance last trade id (l)"),
        _req(33, "timestamp_raw", _L, "Binance timestamp T in the declared unit (millisecond)"),
        _req(34, "is_buyer_maker", _B, "Binance: was the buyer the maker (m)"),
        _req(35, "is_best_match", _B, "Binance: was the trade the best price match (M)"),
    ),
    _symbol_day_spec(22, 9, "event_time"),
)

BINANCE_SPOT_REST_KLINES_1M: Final = _definition(
    "raw.binance_spot_rest_klines_1m",
    Schema(
        _req(1, "observation_key", _S, _OBSERVATION_KEY),
        _req(2, "revision_id", _S, _REVISION_ID),
        _req(3, "source_id", _S, "binance.public.spot.rest@1.0.0 (channel-level source identity)"),
        _req(4, "payload_hash", _S, _PAYLOAD_HASH),
        _req(5, "arrival_seq", _L, "REST block base + element_index + 1; audit only"),
        _req(6, "supersedes", _strings(41), _SUPERSEDES),
        _opt(7, "source_revision_id", _S, _SOURCE_REVISION_ID),
        _opt(8, "source_revision_time", _T, _SOURCE_REVISION_TIME),
        _req(9, "interval_start", _T, _INTERVAL_START),
        _req(10, "interval_end", _T, _INTERVAL_END),
        _opt(11, "source_time", _T, _SOURCE_TIME),
        _req(12, "available_time", _T, _AVAILABLE_TIME),
        _req(13, "ingest_time", _T, _INGEST_TIME),
        _req(14, "knowledge_time", _T, _KNOWLEDGE_TIME),
        _req(15, "declared_latency_us", _L, _DECLARED_LATENCY),
        _req(16, "availability_policy_id", _S, _AVAILABILITY_ID),
        _req(17, "availability_policy_version", _S, _AVAILABILITY_VERSION),
        _req(18, "availability_policy_hash", _S, _AVAILABILITY_HASH),
        _req(19, "availability_evidence", _strings(42), _AVAILABILITY_EVIDENCE),
        _opt(20, "availability_evidence_gap", _S, _AVAILABILITY_GAP),
        _precedence_evidence(21, 43, (44, 45, 46, 47, 48, 49, 50)),
        _req(22, "contract_schema_version", _S, _CONTRACT_VERSION),
        _req(23, "symbol", _S, _SYMBOL),
        _req(24, "response_revision_id", _S, _RESPONSE_REVISION),
        _req(25, "element_index", _L, _ELEMENT_INDEX),
        _req(26, "decoder_id", _S, _DECODER_ID),
        _req(27, "decoder_version", _S, _DECODER_VERSION),
        _req(28, "decoder_hash", _S, _DECODER_HASH),
        _req(29, "open_time_raw", _L, "Binance kline open time in the declared unit"),
        _req(30, "open", _D, "Binance open price"),
        _req(31, "high", _D, "Binance high price"),
        _req(32, "low", _D, "Binance low price"),
        _req(33, "close", _D, "Binance close price"),
        _req(34, "volume", _D, "Binance base asset volume"),
        _req(35, "close_time_raw", _L, "Binance kline close time in the declared unit"),
        _req(36, "quote_asset_volume", _D, "Binance quote asset volume"),
        _req(37, "number_of_trades", _L, "Binance number of trades"),
        _req(38, "taker_buy_base_asset_volume", _D, "Binance taker buy base asset volume"),
        _req(39, "taker_buy_quote_asset_volume", _D, "Binance taker buy quote asset volume"),
        _req(40, "ignore_raw", _S, "Binance 12th ('unused, ignore') field text as received"),
    ),
    _symbol_day_spec(23, 9, "interval_start"),
)

BINANCE_SPOT_EXCHANGE_INFO: Final = _definition(
    "raw.binance_spot_exchange_info",
    Schema(
        _req(1, "observation_key", _S, "binance:spot:exchange-info:<request_identity_sha256>"),
        _req(2, "revision_id", _S, _REVISION_ID),
        _req(
            3,
            "source_id",
            _S,
            "binance.public.spot.exchange-info@1.0.0 (channel-level source identity)",
        ),
        _req(4, "payload_hash", _S, "SHA-256 hex of the response entity body"),
        _req(5, "arrival_seq", _L, "previous largest arrival_seq + 1 (0 first); audit only"),
        _req(6, "supersedes", _strings(47), _SUPERSEDES),
        _opt(7, "source_revision_id", _S, _SOURCE_REVISION_ID),
        _opt(8, "source_revision_time", _T, _SOURCE_REVISION_TIME),
        _req(9, "event_time", _T, "ObservationTimes.event_time = requested_at (UTC)"),
        _req(10, "event_end_time", _T, "ObservationTimes.event_end_time = ingest_time (UTC)"),
        _opt(11, "source_time", _T, _SOURCE_TIME),
        _req(12, "available_time", _T, _AVAILABLE_TIME),
        _req(13, "ingest_time", _T, "ObservationTimes.ingest_time: last response body byte"),
        _req(14, "knowledge_time", _T, _KNOWLEDGE_TIME),
        _req(15, "declared_latency_us", _L, _DECLARED_LATENCY),
        _req(16, "availability_policy_id", _S, _AVAILABILITY_ID),
        _req(17, "availability_policy_version", _S, _AVAILABILITY_VERSION),
        _req(18, "availability_policy_hash", _S, _AVAILABILITY_HASH),
        _req(19, "availability_evidence", _strings(48), _AVAILABILITY_EVIDENCE),
        _opt(20, "availability_evidence_gap", _S, _AVAILABILITY_GAP),
        _precedence_evidence(21, 49, (50, 51, 52, 53, 54, 55, 56)),
        _req(22, "contract_schema_version", _S, _CONTRACT_VERSION),
        _req(
            23,
            "source_binding_id",
            _S,
            "SourceBinding.source_id, e.g. binance.public.spot.exchange-info",
        ),
        _req(24, "source_binding_version", _S, "SourceBinding.version, e.g. 1.0.0"),
        _req(25, "collector_id", _S, "collector id of the first delivery"),
        _req(26, "collector_version", _S, "collector version of the first delivery"),
        _req(27, "collection_request_id", _S, "logical snapshot request id (first delivery)"),
        _req(28, "request_origin", _S, "request identity origin: configured https://host[:port]"),
        _req(29, "request_path", _S, "request identity path: /api/v3/exchangeInfo"),
        _req(30, "request_query", _S, "canonical query string (the symbols parameter only)"),
        _req(
            31, "request_identity_sha256", _S, "SHA-256 of the canonical request identity document"
        ),
        _req(32, "source_uri", _S, "the exact request URI"),
        _req(33, "requested_at", _T, "local UTC time the request was handed to the transport"),
        _req(34, "retrieved_at", _T, "last response body byte = ingest_time (first delivery)"),
        _req(35, "http_status", _L, "HTTP status of the response (200 for every revision)"),
        _req(
            36,
            "source_metadata",
            ListType(
                57,
                StructType(
                    _req(58, "name", _S, "metadata key (lower-case token, e.g. HTTP header)"),
                    _req(59, "value", _S, "metadata value as received"),
                ),
                element_required=True,
            ),
            "allow-listed HTTP response headers as (name, value) pairs sorted by name",
        ),
        _req(37, "object_key", _S, "ObjectRef.key of the published response body"),
        _req(38, "object_uri", _S, "ObjectRef.uri: warehouse URI of the response body"),
        _req(39, "object_sha256", _S, "ObjectRef.sha256: SHA-256 computed over the stored bytes"),
        _req(40, "object_size_bytes", _L, "ObjectRef.size in bytes"),
        _req(41, "decoder_id", _S, "decoder PolicyBinding.policy_id (role=parser)"),
        _req(42, "decoder_version", _S, _DECODER_VERSION),
        _req(43, "decoder_hash", _S, _DECODER_HASH),
        _req(
            44,
            "server_time_raw",
            _L,
            "response-level serverTime as received; unit not interpreted, never a status time",
        ),
        _req(45, "requested_symbols", _strings(60), "symbols of the request (set, sorted)"),
        _req(
            46,
            "symbols",
            ListType(
                61,
                StructType(
                    _req(62, "symbol", _S, "exchangeInfo symbols[].symbol as received"),
                    _req(63, "status", _S, "exchangeInfo symbols[].status as received"),
                    _req(64, "base_asset", _S, "exchangeInfo symbols[].baseAsset as received"),
                    _req(65, "quote_asset", _S, "exchangeInfo symbols[].quoteAsset as received"),
                ),
                element_required=True,
            ),
            "requested symbols present in the response, sorted by symbol (absent = missing)",
        ),
    ),
)

BINANCE_SPOT_PRECEDENCE_EVIDENCE: Final = _definition(
    "raw.binance_spot_precedence_evidence",
    Schema(
        _req(1, "edge_id", _S, "edge1-<sha256>: policy + observation_key + both ends; no time"),
        _req(2, "observation_key", _S, "PrecedenceEvidence.observation_key (both ends)"),
        _req(3, "revision_id", _S, "PrecedenceEvidence.revision_id: the superseding revision"),
        _req(4, "revision_table", _S, "Raw table (namespace.table) of revision_id"),
        _req(
            5,
            "superseded_revision_id",
            _S,
            "PrecedenceEvidence.superseded_revision_id: the superseded revision",
        ),
        _req(6, "superseded_table", _S, "Raw table (namespace.table) of superseded_revision_id"),
        _req(7, "policy_id", _S, "precedence PolicyBinding.policy_id"),
        _req(8, "policy_version", _S, "precedence PolicyBinding.version"),
        _req(9, "policy_hash", _S, "precedence PolicyBinding.policy_hash (SHA-256 hex)"),
        _req(10, "evidence", _strings(16), "PrecedenceEvidence.evidence (non-empty)"),
        _req(
            11,
            "knowledge_time",
            _T,
            "PrecedenceEvidence.knowledge_time: first successful comparison, never backfilled",
        ),
        _req(12, "revision_snapshot_id", _S, "snapshot of revision_table read by the comparison"),
        _req(
            13,
            "superseded_snapshot_id",
            _S,
            "snapshot of superseded_table read by the comparison",
        ),
        _req(14, "projection_sha256", _S, "SHA-256 of the equal canonical content projection"),
        _req(15, "contract_schema_version", _S, "contract envelope schema_version of the edge"),
    ),
)

# --------------------------------------------------------------------------- canonical


CANONICAL_TRADES: Final = _definition(
    "canonical.trades",
    Schema(
        _req(1, "observation_key", _S, _OBSERVATION_KEY),
        _req(2, "revision_id", _S, _REVISION_ID),
        _req(3, "source_id", _S, _SOURCE_ID),
        _req(4, "payload_hash", _S, _PAYLOAD_HASH),
        _req(5, "arrival_seq", _L, _ARRIVAL_SEQ),
        _req(6, "supersedes", _strings(34), _SUPERSEDES),
        _opt(7, "source_revision_id", _S, _SOURCE_REVISION_ID),
        _opt(8, "source_revision_time", _T, _SOURCE_REVISION_TIME),
        _req(9, "event_time", _T, _EVENT_TIME),
        _opt(10, "source_time", _T, _SOURCE_TIME),
        _req(11, "available_time", _T, _AVAILABLE_TIME),
        _req(12, "ingest_time", _T, _INGEST_TIME),
        _req(13, "knowledge_time", _T, _KNOWLEDGE_TIME),
        _req(14, "declared_latency_us", _L, _DECLARED_LATENCY),
        _req(15, "availability_policy_id", _S, _AVAILABILITY_ID),
        _req(16, "availability_policy_version", _S, _AVAILABILITY_VERSION),
        _req(17, "availability_policy_hash", _S, _AVAILABILITY_HASH),
        _req(18, "availability_evidence", _strings(35), _AVAILABILITY_EVIDENCE),
        _opt(19, "availability_evidence_gap", _S, _AVAILABILITY_GAP),
        _precedence_evidence(20, 36, (37, 38, 39, 40, 41, 42, 43)),
        _req(21, "contract_schema_version", _S, _CONTRACT_VERSION),
        _req(22, "venue", _S, "Instrument.venue"),
        _req(23, "instrument_type", _S, "Instrument.instrument_type"),
        _req(24, "symbol", _S, "canonical Instrument.symbol"),
        _req(25, "venue_symbol", _S, "venue-native symbol (kept next to the canonical symbol)"),
        _req(26, "venue_trade_id", _S, "venue-native trade id"),
        _req(27, "price", _D, "trade price"),
        _req(28, "quantity", _D, "trade quantity (base asset)"),
        _opt(29, "buyer_is_maker", _B, "venue-reported maker side; null if not reported"),
        _req(30, "lineage_raw_table", _S, _LINEAGE_RAW_TABLE),
        _req(31, "lineage_raw_revision_id", _S, _LINEAGE_RAW_REVISION),
        _req(32, "lineage_source_table", _S, _LINEAGE_SOURCE_TABLE),
        _req(33, "lineage_source_revision_id", _S, _LINEAGE_SOURCE_REVISION),
    ),
    _symbol_day_spec(24, 9, "event_time"),
)

CANONICAL_BARS_1M: Final = _definition(
    "canonical.bars_1m",
    Schema(
        _req(1, "observation_key", _S, _OBSERVATION_KEY),
        _req(2, "revision_id", _S, _REVISION_ID),
        _req(3, "source_id", _S, _SOURCE_ID),
        _req(4, "payload_hash", _S, _PAYLOAD_HASH),
        _req(5, "arrival_seq", _L, _ARRIVAL_SEQ),
        _req(6, "supersedes", _strings(40), _SUPERSEDES),
        _opt(7, "source_revision_id", _S, _SOURCE_REVISION_ID),
        _opt(8, "source_revision_time", _T, _SOURCE_REVISION_TIME),
        _req(9, "interval_start", _T, _INTERVAL_START),
        _req(10, "interval_end", _T, _INTERVAL_END),
        _opt(11, "source_time", _T, _SOURCE_TIME),
        _req(12, "available_time", _T, _AVAILABLE_TIME),
        _req(13, "ingest_time", _T, _INGEST_TIME),
        _req(14, "knowledge_time", _T, _KNOWLEDGE_TIME),
        _req(15, "declared_latency_us", _L, _DECLARED_LATENCY),
        _req(16, "availability_policy_id", _S, _AVAILABILITY_ID),
        _req(17, "availability_policy_version", _S, _AVAILABILITY_VERSION),
        _req(18, "availability_policy_hash", _S, _AVAILABILITY_HASH),
        _req(19, "availability_evidence", _strings(41), _AVAILABILITY_EVIDENCE),
        _opt(20, "availability_evidence_gap", _S, _AVAILABILITY_GAP),
        _precedence_evidence(21, 42, (43, 44, 45, 46, 47, 48, 49)),
        _req(22, "contract_schema_version", _S, _CONTRACT_VERSION),
        _req(23, "venue", _S, "Instrument.venue"),
        _req(24, "instrument_type", _S, "Instrument.instrument_type"),
        _req(25, "symbol", _S, "canonical Instrument.symbol"),
        _req(26, "venue_symbol", _S, "venue-native symbol (kept next to the canonical symbol)"),
        _req(27, "open", _D, "open price"),
        _req(28, "high", _D, "high price"),
        _req(29, "low", _D, "low price"),
        _req(30, "close", _D, "close price"),
        _req(31, "volume", _D, "base asset volume"),
        _req(32, "quote_volume", _D, "quote asset volume"),
        _req(33, "trade_count", _L, "number of trades"),
        _req(34, "taker_buy_base_volume", _D, "taker buy base asset volume"),
        _req(35, "taker_buy_quote_volume", _D, "taker buy quote asset volume"),
        _req(36, "lineage_raw_table", _S, _LINEAGE_RAW_TABLE),
        _req(37, "lineage_raw_revision_id", _S, _LINEAGE_RAW_REVISION),
        _req(38, "lineage_source_table", _S, _LINEAGE_SOURCE_TABLE),
        _req(39, "lineage_source_revision_id", _S, _LINEAGE_SOURCE_REVISION),
    ),
    _symbol_day_spec(25, 9, "interval_start"),
)

CANONICAL_INSTRUMENT_LISTINGS: Final = _definition(
    "canonical.instrument_listings",
    Schema(
        _req(1, "observation_key", _S, "episode.observation_key() (ADR-0024 §2)"),
        _req(2, "revision_id", _S, _REVISION_ID),
        _req(3, "source_id", _S, _SOURCE_ID),
        _req(4, "payload_hash", _S, _PAYLOAD_HASH),
        _req(5, "arrival_seq", _L, _ARRIVAL_SEQ),
        _req(6, "supersedes", _strings(43), _SUPERSEDES),
        _opt(7, "source_revision_id", _S, _SOURCE_REVISION_ID),
        _opt(8, "source_revision_time", _T, _SOURCE_REVISION_TIME),
        _req(9, "event_time", _T, _EVENT_TIME),
        _opt(10, "event_end_time", _T, _EVENT_END_TIME),
        _opt(11, "source_time", _T, _SOURCE_TIME),
        _req(12, "available_time", _T, _AVAILABLE_TIME),
        _req(13, "ingest_time", _T, _INGEST_TIME),
        _req(14, "knowledge_time", _T, _KNOWLEDGE_TIME),
        _req(15, "declared_latency_us", _L, _DECLARED_LATENCY),
        _req(16, "availability_policy_id", _S, _AVAILABILITY_ID),
        _req(17, "availability_policy_version", _S, _AVAILABILITY_VERSION),
        _req(18, "availability_policy_hash", _S, _AVAILABILITY_HASH),
        _req(19, "availability_evidence", _strings(44), _AVAILABILITY_EVIDENCE),
        _opt(20, "availability_evidence_gap", _S, _AVAILABILITY_GAP),
        _precedence_evidence(21, 45, (46, 47, 48, 49, 50, 51, 52)),
        _req(22, "contract_schema_version", _S, _CONTRACT_VERSION),
        _req(
            23, "episode_basis", _S, "episode key basis: stable_product_id | degraded_symbol_start"
        ),
        _req(24, "episode_venue", _S, "episode key venue"),
        _req(25, "episode_instrument_type", _S, "episode key instrument_type"),
        _opt(26, "episode_venue_product_id", _S, "StableEpisodeKey.venue_product_id"),
        _opt(27, "episode_symbol", _S, "DegradedEpisodeKey.symbol"),
        _opt(28, "episode_tradable_from", _T, "DegradedEpisodeKey.tradable_from"),
        _req(29, "venue", _S, "Instrument.venue"),
        _req(30, "symbol", _S, "Instrument.symbol"),
        _req(31, "instrument_type", _S, "Instrument.instrument_type"),
        _req(32, "base", _S, "Instrument.base"),
        _req(33, "quote", _S, "Instrument.quote"),
        _req(
            34,
            "tradable_intervals",
            ListType(
                53,
                StructType(
                    _req(54, "tradable_from", _T, "TradableInterval.tradable_from, inclusive"),
                    _opt(55, "tradable_until", _T, "TradableInterval.tradable_until; null = open"),
                ),
                element_required=True,
            ),
            "ListingRevision.tradable_intervals sorted by tradable_from",
        ),
        _req(35, "status", _S, "ListingRevision.status: listed | suspended | delisted"),
        _opt(36, "source_status", _S, "ListingRevision.source_status (source text)"),
        _opt(37, "status_reason", _S, "ListingRevision.status_reason (source text)"),
        _opt(
            38,
            "renamed_from",
            StructType(
                _req(56, "venue", _S, "DegradedEpisodeKey.venue"),
                _req(57, "instrument_type", _S, "DegradedEpisodeKey.instrument_type"),
                _req(58, "symbol", _S, "DegradedEpisodeKey.symbol"),
                _req(59, "tradable_from", _T, "DegradedEpisodeKey.tradable_from"),
            ),
            "ListingRevision.renamed_from (degraded key of the previous episode)",
        ),
        _req(39, "lineage_raw_table", _S, _LINEAGE_RAW_TABLE),
        _req(40, "lineage_raw_revision_id", _S, _LINEAGE_RAW_REVISION),
        _req(41, "lineage_source_table", _S, _LINEAGE_SOURCE_TABLE),
        _req(42, "lineage_source_revision_id", _S, _LINEAGE_SOURCE_REVISION),
    ),
)

# --------------------------------------------------------------------------- quality / research


DATA_QUALITY_REPORTS: Final = _definition(
    "quality.data_quality_reports",
    Schema(
        _req(1, "report_id", _S, "stable report id (manifest quality_report_ids)"),
        _req(2, "quality_rule_id", _S, "id of the versioned quality rule set that produced it"),
        _req(3, "quality_rule_version", _S, "SemVer of that rule set"),
        _req(4, "quality_rule_hash", _S, "SHA-256 hex of that rule set"),
        _req(5, "subject_table", _S, "namespace.table the report is about"),
        _opt(6, "subject_snapshot_id", _S, "snapshot of subject_table the report checked"),
        _opt(7, "subject_symbol", _S, "partition symbol the report covers"),
        _opt(8, "subject_start", _T, "covered UTC interval start, inclusive"),
        _opt(9, "subject_end", _T, "covered UTC interval end, exclusive"),
        _req(10, "knowledge_time", _T, "local time the report became usable"),
        _req(
            11,
            "events",
            ListType(
                13,
                StructType(
                    _req(14, "event_id", _S, "event id, unique within the report"),
                    _req(15, "event_type", _S, "event category defined by the quality rule set"),
                    _opt(16, "table", _S, "namespace.table the event concerns"),
                    _opt(17, "observation_key", _S, "observation_key the event concerns"),
                    _req(18, "revision_ids", _strings(22), "revision_ids involved (set, sorted)"),
                    _opt(19, "event_start", _T, "affected UTC interval start, inclusive"),
                    _opt(20, "event_end", _T, "affected UTC interval end, exclusive"),
                    _req(21, "detail", _S, "non-empty evidence description"),
                ),
                element_required=True,
            ),
            "quality events (gaps, duplicates, checksum / time failures, competing heads, ...)",
        ),
        _req(
            12,
            "evidence_gaps",
            ListType(
                23,
                StructType(
                    _req(24, "table", _S, "AvailabilityEvidenceGap.table"),
                    _req(25, "revision_id", _S, "AvailabilityEvidenceGap.revision_id"),
                    _req(26, "gap", _S, "AvailabilityEvidenceGap.gap"),
                ),
                element_required=True,
            ),
            "AvailabilityEvidenceGap records whose quality_report_id is report_id",
        ),
    ),
)

DATASET_MANIFESTS: Final = _definition(
    "research.dataset_manifests",
    Schema(
        _req(1, "manifest_content_hash", _S, "ResearchDatasetManifest.content_hash()"),
        _req(2, "contract_schema_version", _S, "ResearchDatasetManifest.schema_version"),
        _req(3, "dataset_zone", _S, "dataset.zone (research_dataset)"),
        _req(4, "dataset_table", _S, "dataset.table"),
        _req(5, "dataset_snapshot_id", _S, "dataset.snapshot_id"),
        _req(6, "dataset_time_range_start", _T, "dataset.time_range_start"),
        _req(7, "dataset_time_range_end", _T, "dataset.time_range_end"),
        _req(8, "point_in_time_name", _S, "point_in_time.name"),
        _req(9, "point_in_time_version", _S, "point_in_time.version"),
        _req(10, "point_in_time_hash", _S, "point_in_time.content_hash()"),
        _opt(11, "simulation_time", _T, "point_in_time.simulation_time"),
        _opt(12, "simulation_start", _T, "point_in_time.simulation_start"),
        _opt(13, "simulation_end", _T, "point_in_time.simulation_end"),
        _req(14, "knowledge_cutoff", _T, "point_in_time.knowledge_cutoff"),
        _req(15, "universe_spec_name", _S, "universe_spec.name"),
        _req(16, "universe_spec_version", _S, "universe_spec.version"),
        _req(17, "universe_spec_hash", _S, "universe_spec.spec_hash"),
        _req(
            18,
            "snapshot_bindings",
            ListType(
                21,
                StructType(
                    _req(22, "table", _S, "upstream namespace.table"),
                    _req(23, "snapshot_id", _S, "upstream snapshot_id"),
                ),
                element_required=True,
            ),
            "point_in_time.snapshot_bindings sorted by table",
        ),
        _req(19, "quality_report_ids", _strings(24), "quality_report_ids (set, sorted)"),
        _req(
            20,
            "manifest_json",
            _S,
            "contract canonical JSON of the full manifest (UTF-8); sha256 = manifest_content_hash",
        ),
    ),
)

QUALITY_EVIDENCE_GAPS: Final = _definition(
    "quality.availability_evidence_gaps",
    Schema(
        _req(1, "quality_report_id", _S, "AvailabilityEvidenceGap.quality_report_id"),
        _req(2, "table", _S, "AvailabilityEvidenceGap.table"),
        _req(3, "revision_id", _S, "AvailabilityEvidenceGap.revision_id"),
        _req(4, "gap", _S, "AvailabilityEvidenceGap.gap"),
        _req(5, "subject_symbol", _S, "partition symbol of the time-sliced batch this row is in"),
        _req(6, "subject_start", _T, "covered UTC day start, inclusive (partition key)"),
    ),
    _symbol_day_spec(5, 6, "subject_start"),
)

#: The fourteen production tables in 03-data.md §7.1 order: the C3 first slice, then ADR-0027,
#: then the ADR-0029 exchangeInfo snapshot table (E2), then the ADR-0031 quality evidence-gap
#: table (QG-1).
PHASE1_TABLES: Final[tuple[RegisteredTableDefinition, ...]] = (
    BINANCE_SPOT_ARCHIVES,
    BINANCE_SPOT_AGG_TRADES,
    BINANCE_SPOT_KLINES_1M,
    CANONICAL_TRADES,
    CANONICAL_BARS_1M,
    CANONICAL_INSTRUMENT_LISTINGS,
    DATA_QUALITY_REPORTS,
    DATASET_MANIFESTS,
    BINANCE_SPOT_REST_RESPONSES,
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_KLINES_1M,
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    BINANCE_SPOT_EXCHANGE_INFO,
    QUALITY_EVIDENCE_GAPS,
)
PHASE1_REGISTRY: Final = TableDefinitionRegistry(PHASE1_TABLES)


def describe_partition_spec(definition: RegisteredTableDefinition) -> str:
    """Human-readable partition spec, e.g. ``identity(symbol), day(event_time)``."""
    parts = []
    for field in definition.partition_spec.fields:
        column = definition.schema.find_column_name(field.source_id)
        parts.append(f"{field.transform}({column})")
    return ", ".join(parts) if parts else "unpartitioned"


@dataclass(frozen=True)
class Phase1TableState:
    """Result of ``ensure_phase1_tables`` for one table."""

    table: str
    definition: TableDefinition
    partition: str
    created: bool
    current_snapshot_id: str | None


def ensure_phase1_tables(
    adapter: PyIcebergCatalogAdapter,
    definitions: tuple[RegisteredTableDefinition, ...] = PHASE1_TABLES,
) -> tuple[Phase1TableState, ...]:
    """Idempotently create the Phase 1 tables through ``adapter``.

    Existing tables with the same binding are verified and left unchanged (their data is never
    touched); any drift raises (``TableDefinitionConflict`` / ``CatalogIntegrityError`` /
    ``UnknownTableDefinition``). The adapter's registry must contain these definitions.
    """
    states = []
    for definition in definitions:
        existed = adapter.load_table(definition.table) is not None
        info = adapter.create_table(definition.binding)
        current = info.current_snapshot
        states.append(
            Phase1TableState(
                table=definition.table,
                definition=info.definition,
                partition=describe_partition_spec(definition),
                created=not existed,
                current_snapshot_id=None if current is None else current.snapshot_id,
            )
        )
    return tuple(states)

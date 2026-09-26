"""The physical Phase 3 Event table ``event.events`` (ADR-0056; 03-data.md §8).

One row per event of one event run (``EventResult``): the nine columns of the logical Event table
(``infrastructure.event.table.EVENT_TABLE_COLUMNS``, same names, same order, field IDs 1-10; the
optional ``subject`` is field 10, ADR-0057) plus
the **run block** (``event_index``, ``event_count``, ``request_hash``, ``provider_hash``,
``as_of``; IDs 10-14) that lets the table alone rebuild and re-verify the ``EventResult``. There
is no ingest-time column (rows are a pure function of the run, so the batch fingerprint is
deterministic and a rewrite is an idempotent replay; write time is the snapshot's
``committed_at``) and no revision block (a run is immutable; a new run has a new
``result_hash``). Partitioned by ``month(event_time)``: events are sparse, so ``day`` would write
one tiny file per day a run spans.

The definition reuses the Phase 1 catalog machinery read-only (``RegisteredTableDefinition``,
``TableDefinitionRegistry``, the frozen ``hlens.pyarrow-batch-sha256@1.0.0`` fingerprint rule and
the idempotent ``ensure_phase1_tables``) and is **not** part of ``PHASE1_TABLES``: the Phase 1
registry and its hashes are unchanged. Callers compose a registry that contains
``PHASE3_TABLES`` for their ``PyIcebergCatalogAdapter``. ``ensure_event_tables`` is the only
creation path; nothing wires it into the Phase 1 provisioning scripts.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema, assign_fresh_schema_ids
from pyiceberg.transforms import MonthTransform
from pyiceberg.types import (
    IcebergType,
    ListType,
    LongType,
    NestedField,
    StringType,
    TimestamptzType,
)

from infrastructure.catalog.definitions import RegisteredTableDefinition, TableDefinitionRegistry
from infrastructure.catalog.fingerprint import PYARROW_BATCH_FINGERPRINT
from infrastructure.catalog.iceberg_adapter import PyIcebergCatalogAdapter
from infrastructure.catalog.phase1_tables import Phase1TableState, ensure_phase1_tables
from infrastructure.event.table import EVENT_TABLE_COLUMNS

__all__ = [
    "EVENT_EVENTS",
    "EVENT_RUN_COLUMNS",
    "PHASE3_DEFINITION_VERSION",
    "PHASE3_REGISTRY",
    "PHASE3_TABLES",
    "PHASE3_TABLE_PROPERTIES",
    "ensure_event_tables",
]

PHASE3_DEFINITION_VERSION: Final = "1.0.0"
#: Non-binding table properties (part of the definition hash); same value as Phase 1.
PHASE3_TABLE_PROPERTIES: Final[Mapping[str, str]] = {"write.parquet.compression-codec": "zstd"}
#: The run block: constant per run except ``event_index``.
EVENT_RUN_COLUMNS: Final = ("event_index", "event_count", "request_hash", "provider_hash", "as_of")

_S: Final = StringType()
_L: Final = LongType()
_T: Final = TimestamptzType()


def _req(field_id: int, name: str, field_type: IcebergType, doc: str) -> NestedField:
    return NestedField(field_id, name, field_type, required=True, doc=doc)


def _hashes(element_id: int) -> ListType:
    return ListType(element_id, _S, element_required=True)


_SCHEMA: Final = Schema(
    _req(1, "event_id", _S, "Event.event_id: content hash of the event"),
    _req(2, "event", _S, "Event.event as the canonical ref string event:{name}@{semver}"),
    _req(3, "spec_hash", _S, "Event.spec_hash: content hash of the event definition"),
    _req(4, "event_time", _T, "Event.event_time: observable time (UTC)"),
    _req(5, "attributes_json", _S, "contract canonical JSON of Event.attributes"),
    _req(6, "input_ids", _hashes(16), "Event.input_ids (strictly ascending)"),
    _req(7, "upstream_event_ids", _hashes(17), "Event.upstream_event_ids (strictly ascending)"),
    _req(8, "provider", _S, "EventResult.provider (plugin key)"),
    _req(9, "result_hash", _S, "EventResult.result_hash: the run; one batch per value"),
    NestedField(
        10, "subject", _S, required=False, doc="Event.subject (ADR-0057); null when unbound"
    ),
    _req(11, "event_index", _L, "0-based position in the run's (event_time, event_id) order"),
    _req(12, "event_count", _L, "number of events in the run"),
    _req(13, "request_hash", _S, "EventResult.request_hash"),
    _req(14, "provider_hash", _S, "EventResult.provider_hash"),
    _req(15, "as_of", _T, "EventResult.as_of (UTC)"),
)

_SPEC: Final = PartitionSpec(
    PartitionField(source_id=4, field_id=1000, transform=MonthTransform(), name="event_time_month")
)


def _definition() -> RegisteredTableDefinition:
    if tuple(field.name for field in _SCHEMA.fields) != EVENT_TABLE_COLUMNS + EVENT_RUN_COLUMNS:
        raise ValueError("event.events columns must be the logical Event table plus the run block")
    if assign_fresh_schema_ids(_SCHEMA).model_dump_json() != _SCHEMA.model_dump_json():
        raise ValueError("event.events: declared field IDs differ from Iceberg's creation-time IDs")
    definition = RegisteredTableDefinition(
        table="event.events",
        definition_id="event.events",
        version=PHASE3_DEFINITION_VERSION,
        schema=_SCHEMA,
        fingerprint_rule=PYARROW_BATCH_FINGERPRINT,
        partition_spec=_SPEC,
        properties=PHASE3_TABLE_PROPERTIES,
    )
    if definition.partition_spec.model_dump_json() != _SPEC.model_dump_json():
        raise ValueError("event.events: declared partition IDs differ from Iceberg's creation IDs")
    return definition


EVENT_EVENTS: Final = _definition()
PHASE3_TABLES: Final[tuple[RegisteredTableDefinition, ...]] = (EVENT_EVENTS,)
PHASE3_REGISTRY: Final = TableDefinitionRegistry(PHASE3_TABLES)


def ensure_event_tables(adapter: PyIcebergCatalogAdapter) -> tuple[Phase1TableState, ...]:
    """Idempotently create ``event.events`` through ``adapter`` (explicit call only).

    Existing tables are verified and left unchanged; drift raises. The adapter's registry must
    contain ``PHASE3_TABLES``.
    """
    return ensure_phase1_tables(adapter, PHASE3_TABLES)

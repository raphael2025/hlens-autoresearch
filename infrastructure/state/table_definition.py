"""The additive physical Phase 2 State table ``state.states`` (ADR-0089)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema, assign_fresh_schema_ids
from pyiceberg.transforms import DayTransform
from pyiceberg.types import IcebergType, LongType, NestedField, StringType, TimestamptzType

from infrastructure.catalog.definitions import RegisteredTableDefinition, TableDefinitionRegistry
from infrastructure.catalog.fingerprint import PYARROW_BATCH_FINGERPRINT
from infrastructure.catalog.iceberg_adapter import PyIcebergCatalogAdapter
from infrastructure.catalog.phase1_tables import Phase1TableState, ensure_phase1_tables
from infrastructure.state.table import STATE_TABLE_SCHEMA

__all__ = [
    "PHASE2_DEFINITION_VERSION",
    "PHASE2_REGISTRY",
    "PHASE2_TABLES",
    "PHASE2_TABLE_PROPERTIES",
    "RUN_COLUMNS",
    "STATE_STATES",
    "ensure_state_tables",
]

PHASE2_DEFINITION_VERSION: Final = "1.0.0"
PHASE2_TABLE_PROPERTIES: Final[Mapping[str, str]] = {"write.parquet.compression-codec": "zstd"}
RUN_COLUMNS: Final = (
    "provider_hash",
    "evaluation_index",
    "evaluation_count",
    "run_schema_version",
)

_S: Final = StringType()
_L: Final = LongType()
_T: Final = TimestamptzType()


def _req(field_id: int, name: str, field_type: IcebergType, doc: str) -> NestedField:
    return NestedField(field_id, name, field_type, required=True, doc=doc)


_SCHEMA: Final = Schema(
    _req(1, "state_ref", _S, "StateSpec.ref as its canonical reference string"),
    _req(2, "spec_hash", _S, "StateRequest.spec_hash"),
    _req(3, "provider", _S, "StateResult.provider"),
    _req(4, "request_hash", _S, "StateResult.request_hash"),
    _req(5, "result_hash", _S, "StateResult.result_hash; run identity"),
    _req(6, "evaluation_time", _T, "StateValue.evaluation_time (UTC)"),
    NestedField(7, "state", _S, required=False, doc="StateValue.state; null means not computable"),
    _req(8, "inputs_used", _L, "StateValue.inputs_used"),
    NestedField(
        9, "latest_input_time", _T, required=False, doc="StateValue.latest_input_time (UTC)"
    ),
    _req(10, "provider_hash", _S, "StateResult.provider_hash"),
    _req(11, "evaluation_index", _L, "0-based index in increasing evaluation_time order"),
    _req(12, "evaluation_count", _L, "number of values in this StateResult"),
    _req(13, "run_schema_version", _S, "recorded StateResult and StateValue contract envelope"),
)

_SPEC: Final = PartitionSpec(
    PartitionField(
        source_id=6,
        field_id=1000,
        transform=DayTransform(),
        name="evaluation_time_day",
    )
)


def _definition() -> RegisteredTableDefinition:
    logical = tuple(field.name for field in STATE_TABLE_SCHEMA)
    if tuple(field.name for field in _SCHEMA.fields) != logical + RUN_COLUMNS:
        raise ValueError("state.states must retain the logical State table plus its run envelope")
    if assign_fresh_schema_ids(_SCHEMA).model_dump_json() != _SCHEMA.model_dump_json():
        raise ValueError("state.states: declared field IDs differ from Iceberg creation-time IDs")
    definition = RegisteredTableDefinition(
        table="state.states",
        definition_id="state.states",
        version=PHASE2_DEFINITION_VERSION,
        schema=_SCHEMA,
        fingerprint_rule=PYARROW_BATCH_FINGERPRINT,
        partition_spec=_SPEC,
        properties=PHASE2_TABLE_PROPERTIES,
    )
    if definition.partition_spec.model_dump_json() != _SPEC.model_dump_json():
        raise ValueError("state.states: declared partition IDs differ from Iceberg creation IDs")
    return definition


STATE_STATES: Final = _definition()
PHASE2_TABLES: Final = (STATE_STATES,)
PHASE2_REGISTRY: Final = TableDefinitionRegistry(PHASE2_TABLES)


def ensure_state_tables(
    adapter: PyIcebergCatalogAdapter,
) -> tuple[Phase1TableState, ...]:
    """Idempotently create or verify only ``state.states`` (explicit call, no real catalog)."""
    return ensure_phase1_tables(adapter, PHASE2_TABLES)

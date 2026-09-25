"""The materialized PIT selection of a Research Dataset — **proposed** table shape (Phase 1 F3).

ADR-0023 §6 and ``03-data.md`` §3 / §7.5 require a Research Dataset to have its **own** Iceberg
``snapshot_id`` (``ResearchDatasetManifest.dataset``), but no Research Dataset table is frozen:
``03-data.md`` §7.1 leaves "the naming of materialized Research Dataset tables" to the PIT batch
to *propose*. This module is that proposal and nothing more: ``SELECTION_SCHEMA`` is the shape a
dataset build writes, and ``selection_table_definition`` builds a definition of it for a caller
that **has** an approved (or, in tests, a test-only) table name. It registers nothing, creates no
table and does not touch ``PHASE1_TABLES``.

One row = one Canonical revision selected for one observation key, for the simulation span in
which it is the selection and its symbol a universe member (point simulation: no span). Rows
reference the Canonical revision instead of copying its payload: the Canonical snapshot is bound
by the manifest and immutable, so ``(canonical_table, revision_id)`` at that snapshot *is* the
content, and a copy would duplicate every trade of the window. All rows of one build share its
``selection_id`` (the batch id), so a later build appending to the same table never mixes in.
"""

from __future__ import annotations

from typing import Final

from pyiceberg.schema import Schema
from pyiceberg.types import NestedField, StringType, TimestamptzType

from infrastructure.catalog.definitions import RegisteredTableDefinition
from infrastructure.catalog.fingerprint import PYARROW_BATCH_FINGERPRINT
from infrastructure.catalog.phase1_tables import PHASE1_TABLE_PROPERTIES

__all__ = [
    "SELECTION_DEFINITION_VERSION",
    "SELECTION_NAMESPACE",
    "SELECTION_SCHEMA",
    "selection_table_definition",
]

SELECTION_NAMESPACE: Final = "research"
SELECTION_DEFINITION_VERSION: Final = "1.0.0"

_S: Final = StringType()
_T: Final = TimestamptzType()

SELECTION_SCHEMA: Final = Schema(
    NestedField(1, "selection_id", _S, required=True, doc="dataset build id (= batch id)"),
    NestedField(2, "canonical_table", _S, required=True, doc="selected revision's table"),
    NestedField(3, "symbol", _S, required=True, doc="Canonical symbol, e.g. BTC-USDT"),
    NestedField(4, "observation_key", _S, required=True, doc="RevisionRecord.observation_key"),
    NestedField(5, "revision_id", _S, required=True, doc="the selected Canonical revision"),
    NestedField(6, "event_time", _T, required=True, doc="its event_time / interval_start (UTC)"),
    NestedField(7, "effective_from", _T, required=False, doc="simulation span start; null = point"),
    NestedField(8, "effective_until", _T, required=False, doc="simulation span end (exclusive)"),
)


def selection_table_definition(
    table: str, *, version: str = SELECTION_DEFINITION_VERSION
) -> RegisteredTableDefinition:
    """A definition of ``SELECTION_SCHEMA`` under ``table`` (``research`` namespace only)."""
    if table.split(".", 1)[0] != SELECTION_NAMESPACE:
        raise ValueError(f"a Research Dataset table must be in the {SELECTION_NAMESPACE} namespace")
    return RegisteredTableDefinition(
        table=table,
        definition_id=table,
        version=version,
        schema=SELECTION_SCHEMA,
        fingerprint_rule=PYARROW_BATCH_FINGERPRINT,
        properties=PHASE1_TABLE_PROPERTIES,
    )

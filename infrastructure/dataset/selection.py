"""The materialized PIT selection of a Research Dataset — production table shape (ADR-0033, DS-1).

ADR-0023 §6 and ``03-data.md`` §3 / §7.5 require a Research Dataset to have its **own** Iceberg
``snapshot_id`` (``ResearchDatasetManifest.dataset``). This module proposed that table's shape for
Phase 1 F3; ADR-0033 (DS-1) accepted it and registered it as the fifteenth production table,
``research.dataset_selections`` (``infrastructure.catalog.phase1_tables.DATASET_SELECTIONS``).
The field definitions now live there — the single source of truth, since ``dataset`` already
depends on ``catalog`` and the reverse would cycle — and this module re-exports them under their
original names so ``infrastructure.dataset.builder`` (and any caller building an alternate,
same-shape ``research.*`` table) is unaffected.

One row = one Canonical revision selected for one observation key, for the simulation span in
which it is the selection and its symbol a universe member (point simulation: no span). Rows
reference the Canonical revision instead of copying its payload: the Canonical snapshot is bound
by the manifest and immutable, so ``(canonical_table, revision_id)`` at that snapshot *is* the
content, and a copy would duplicate every trade of the window. All rows of one build share its
``selection_id`` (the batch id), so a later build appending to the same table never mixes in.
"""

from __future__ import annotations

from typing import Final

from infrastructure.catalog.definitions import RegisteredTableDefinition
from infrastructure.catalog.fingerprint import PYARROW_BATCH_FINGERPRINT
from infrastructure.catalog.phase1_tables import DATASET_SELECTIONS, PHASE1_TABLE_PROPERTIES

__all__ = [
    "SELECTION_DEFINITION_VERSION",
    "SELECTION_NAMESPACE",
    "SELECTION_SCHEMA",
    "selection_table_definition",
]

SELECTION_NAMESPACE: Final = "research"
SELECTION_DEFINITION_VERSION: Final = DATASET_SELECTIONS.version
#: The frozen production schema (``research.dataset_selections``, ADR-0033); single source of
#: truth lives in ``infrastructure.catalog.phase1_tables``.
SELECTION_SCHEMA: Final = DATASET_SELECTIONS.schema


def selection_table_definition(
    table: str, *, version: str = SELECTION_DEFINITION_VERSION
) -> RegisteredTableDefinition:
    """A definition of ``SELECTION_SCHEMA`` under ``table`` (``research`` namespace only).

    ``DATASET_SELECTIONS`` is the frozen, partitioned production definition of this schema under
    ``research.dataset_selections``; this constructor stays generic (unpartitioned, arbitrary
    ``research.*`` name) for a caller that needs its own, differently-named table of the same
    shape.
    """
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

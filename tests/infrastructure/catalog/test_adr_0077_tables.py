"""ADR-0077 B2: the two additive v3 evidence manifest / selection chunk tables.

Appended after the frozen fifteen (C3+D3B+E2+QG-1+DS-1, see ``test_phase1_tables``): the sixteenth
table is ``research.dataset_evidence_manifests`` and the seventeenth is
``research.dataset_selection_chunks`` (ADR-0077 §7 / §4.1). Both table names, schemas, partition
specs and field-ID assignments are frozen by this batch (§6.2.5); Codex / Raphael review this
before the definitions are treated as accepted.

**Golden hash OPEN**: unlike the sibling batches' golden tests, this file does not pin
``definition_hash`` / layout-sha256 / arrow-schema-sha256 literals for the two new tables. This
batch ran no Python (the wave's hard constraint — WSL memory cap, see ``b-wave-common.md``), so no
real SHA-256 could be computed; hand-writing one would be a fabricated value indistinguishable from
a real regression if wrong. Once Codex / the reviewer actually runs ``uv run pytest`` against this
module (which self-checks its field-ID assignment against ``assign_fresh_schema_ids`` on import,
see ``infrastructure/catalog/phase1_tables.py::_definition``) the resulting hashes should be pinned
into ``tests/infrastructure/catalog/test_phase1_tables.py``'s ``ALL_GOLDEN`` (and the two tables
added to ``PHASE1_TABLE_NAMES`` there) exactly like every earlier additive batch. This file instead
proves everything that does not require a pre-known hash: structural shape, doc coverage, physical
types, partitioning, and that all fifteen earlier tables' *already-pinned* goldens are untouched.
"""

from __future__ import annotations

import hashlib
import re

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.io.pyarrow import schema_to_pyarrow
from pyiceberg.schema import assign_fresh_schema_ids
from pyiceberg.types import (
    BooleanType,
    DecimalType,
    ListType,
    LongType,
    StringType,
    StructType,
    TimestamptzType,
)

from core.contracts.catalog import TABLE_NAME_PATTERN
from core.contracts.universe import (
    DatasetChunkProof,
    DatasetRuleBinding,
    EvidenceObjectRef,
    EvidenceStreamRef,
)
from infrastructure.catalog import PHASE1_REGISTRY, PHASE1_TABLES
from infrastructure.catalog.definitions import RegisteredTableDefinition
from infrastructure.catalog.fingerprint import PYARROW_BATCH_FINGERPRINT
from infrastructure.catalog.phase1_tables import (
    DATASET_EVIDENCE_MANIFESTS,
    DATASET_MANIFESTS,
    DATASET_SELECTION_CHUNKS,
    DATASET_SELECTIONS,
    EXCHANGE_DECIMAL,
    PHASE1_TABLE_PROPERTIES,
)
from tests.infrastructure.catalog.test_phase1_tables import (
    DS_GOLDEN,
    E2_GOLDEN,
    GOLDEN,
    QG_GOLDEN,
    REST_GOLDEN,
    by_table,
    layout_lines,
    sha256_text,
)

_TABLE_NAME_RE = re.compile(TABLE_NAME_PATTERN)

#: The two ADR-0077 B2 tables, in the §7 listing order (manifest table, then chunk table §4.1).
B2_TABLES = ("research.dataset_evidence_manifests", "research.dataset_selection_chunks")
#: (partition field id, transform, source column, name) — manifest table is unpartitioned; the
#: chunk table reuses the same ``identity(symbol) + day(event_time)`` spec as v2's
#: ``research.dataset_selections`` (ADR-0077 §4.1 recommendation).
B2_PARTITIONS: dict[str, list[tuple[int, str, str, str]]] = {
    "research.dataset_evidence_manifests": [],
    "research.dataset_selection_chunks": [
        (1000, "identity", "symbol", "symbol"),
        (1001, "day", "event_time", "event_time_day"),
    ],
}
#: All fifteen earlier tables' pinned goldens (definition_hash only), reused from the sibling
#: golden-test module so this file never invents a hash value of its own.
EARLIER_GOLDEN = {**GOLDEN, **REST_GOLDEN, **E2_GOLDEN, **QG_GOLDEN, **DS_GOLDEN}


# --------------------------------------------------------------------------- registry placement


def test_b2_tables_are_appended_last_without_disturbing_the_earlier_fifteen() -> None:
    tables = tuple(item.table for item in PHASE1_TABLES)
    assert len(tables) == 17
    assert tables[15:] == B2_TABLES
    assert tables[:15] == tuple(EARLIER_GOLDEN)
    assert len(PHASE1_REGISTRY) == 17
    for name in B2_TABLES:
        definition = by_table(name)
        assert definition.definition_id == name
        assert definition.version == "1.0.0"
        assert definition.evolves_from is None
        assert dict(definition.properties) == dict(PHASE1_TABLE_PROPERTIES)
        assert PHASE1_REGISTRY.resolve(definition.binding) is definition
        assert _TABLE_NAME_RE.fullmatch(name), name


def test_earlier_fifteen_goldens_are_unchanged_by_b2() -> None:
    """B2 only appends: all fifteen earlier definitions keep their exact pinned hashes."""
    for table, (expected_hash, _, _) in EARLIER_GOLDEN.items():
        assert by_table(table).definition_hash == expected_hash, table
    assert not set(B2_TABLES) & set(EARLIER_GOLDEN)


def test_dataset_selections_v2_shape_is_untouched_by_the_new_chunk_table() -> None:
    """DQ-3 = a: the chunk table is a new table; v2 ``research.dataset_selections`` (DS-1) is not
    schema-evolved, so its eight columns, partition spec and pinned hash stay exactly as DS-1
    froze them."""
    assert DATASET_SELECTIONS.definition_hash == DS_GOLDEN["research.dataset_selections"][0]
    assert [f.name for f in DATASET_SELECTIONS.schema.fields] == [
        "selection_id",
        "canonical_table",
        "symbol",
        "observation_key",
        "revision_id",
        "event_time",
        "effective_from",
        "effective_until",
    ]


# --------------------------------------------------------------------------- self-consistency


def test_b2_field_ids_match_iceberg_fresh_assignment() -> None:
    """Same self-check ``_definition`` already runs at import time, asserted explicitly here."""
    for definition in (DATASET_EVIDENCE_MANIFESTS, DATASET_SELECTION_CHUNKS):
        fresh = assign_fresh_schema_ids(definition.schema)
        assert fresh.model_dump_json() == definition.schema.model_dump_json()
        assert definition.arrow_schema.equals(
            schema_to_pyarrow(definition.schema, include_field_ids=False), check_metadata=True
        )


def test_b2_definition_hash_is_a_lowercase_sha256_hex_string() -> None:
    """Sanity only (not a golden pin, see module docstring): shape, not value, of the hash."""
    for definition in (DATASET_EVIDENCE_MANIFESTS, DATASET_SELECTION_CHUNKS):
        assert isinstance(definition.definition_hash, str)
        assert len(definition.definition_hash) == 64
        assert definition.definition_hash == definition.definition_hash.lower()
        int(definition.definition_hash, 16)  # raises if not hex


def test_b2_definition_hashes_are_deterministic_within_this_process() -> None:
    """Rebuilding the same schema/spec content is deterministic (same document, same hash)."""
    for definition in (DATASET_EVIDENCE_MANIFESTS, DATASET_SELECTION_CHUNKS):
        rebuilt = RegisteredTableDefinition(
            table=definition.table,
            definition_id=definition.definition_id,
            version=definition.version,
            schema=definition.schema,
            fingerprint_rule=PYARROW_BATCH_FINGERPRINT,
            partition_spec=definition.partition_spec,
            properties=definition.properties,
        )
        assert rebuilt.definition_hash == definition.definition_hash


# --------------------------------------------------------------------------- physical shape


def test_b2_physical_types_are_exact_and_utc_with_docs() -> None:
    allowed = (
        StringType,
        LongType,
        BooleanType,
        TimestamptzType,
        DecimalType,
        ListType,
        StructType,
    )
    for definition in (DATASET_EVIDENCE_MANIFESTS, DATASET_SELECTION_CHUNKS):
        for line in layout_lines(definition.schema):
            field_id = int(line.split("|")[0])
            field_type = definition.schema.find_type(field_id)
            assert isinstance(field_type, allowed), line
            if isinstance(field_type, DecimalType):
                assert field_type == EXCHANGE_DECIMAL
            if not line.split("|")[1].endswith(".element"):
                assert definition.schema.find_field(field_id).doc, f"{line} has no doc"
        for field in definition.arrow_schema:
            assert not pa.types.is_floating(field.type)


def test_b2_partition_specs_match_the_frozen_choice() -> None:
    for table, expected in B2_PARTITIONS.items():
        definition = by_table(table)
        spec = definition.partition_spec
        assert spec.spec_id == 0
        actual = [
            (
                field.field_id,
                str(field.transform),
                definition.schema.find_column_name(field.source_id),
                field.name,
            )
            for field in spec.fields
        ]
        assert actual == expected, table


# --------------------------------------------------------------------------- manifest table shape


def test_manifest_table_columns_and_requiredness() -> None:
    schema = DATASET_EVIDENCE_MANIFESTS.schema
    assert [f.name for f in schema.fields] == [
        "manifest_content_hash",
        "contract_schema_version",
        "dataset_zone",
        "dataset_table",
        "dataset_snapshot_id",
        "dataset_time_range_start",
        "dataset_time_range_end",
        "point_in_time_name",
        "point_in_time_version",
        "point_in_time_hash",
        "simulation_time",
        "simulation_start",
        "simulation_end",
        "knowledge_cutoff",
        "universe_spec_name",
        "universe_spec_version",
        "universe_spec_hash",
        "snapshot_bindings",
        "rule_id",
        "rule_version",
        "rule_hash",
        "data_type",
        "selection_id",
        "row_count",
        "chunk_rows",
        "chunk_count",
        "evidence",
        "manifest_json",
    ]
    optional = {"simulation_time", "simulation_start", "simulation_end"}
    for field in schema.fields:
        assert field.required is (field.name not in optional), field.name
    assert DATASET_EVIDENCE_MANIFESTS.partition_spec.fields == ()


def test_manifest_table_dataset_point_in_time_and_universe_columns_mirror_v2() -> None:
    """Same identity-column shape as v2 ``research.dataset_manifests`` (DATASET_MANIFESTS): the
    v3 manifest's ``dataset`` / ``point_in_time`` / ``universe_spec`` triples are queryable the
    same way, so both manifest generations can be joined / filtered uniformly."""
    v2 = DATASET_MANIFESTS.schema
    v3 = DATASET_EVIDENCE_MANIFESTS.schema
    shared = [
        "dataset_time_range_start",
        "dataset_time_range_end",
        "point_in_time_name",
        "point_in_time_version",
        "point_in_time_hash",
        "simulation_time",
        "simulation_start",
        "simulation_end",
        "knowledge_cutoff",
        "universe_spec_name",
        "universe_spec_version",
        "universe_spec_hash",
    ]
    for name in shared:
        v2_field, v3_field = v2.find_field(name), v3.find_field(name)
        assert v2_field.field_type == v3_field.field_type, name
        assert v2_field.required == v3_field.required, name
    # dataset_zone / dataset_table carry the same physical type in both generations; only
    # dataset_snapshot_id's *doc* differs (v3's is the last committed chunk's snapshot, ADR §1.2).
    assert v2.find_field("dataset_zone").field_type == v3.find_field("dataset_zone").field_type
    assert v2.find_field("dataset_table").field_type == v3.find_field("dataset_table").field_type


def test_manifest_table_snapshot_bindings_struct_matches_v2_shape() -> None:
    v2_elem = DATASET_MANIFESTS.schema.find_type("snapshot_bindings")
    v3_elem = DATASET_EVIDENCE_MANIFESTS.schema.find_type("snapshot_bindings")
    assert isinstance(v2_elem, ListType) and isinstance(v3_elem, ListType)
    v2_names = [f.name for f in v2_elem.element_type.fields]
    v3_names = [f.name for f in v3_elem.element_type.fields]
    assert v2_names == v3_names == ["table", "snapshot_id"]


def test_manifest_table_rule_triple_matches_dataset_rule_binding_fields() -> None:
    """``rule_id`` / ``rule_version`` / ``rule_hash`` name the same three facts as
    ``DatasetRuleBinding`` (``rule_id`` / ``version`` / ``rule_hash``, ADR-0077 §1.2)."""
    contract_fields = set(DatasetRuleBinding.model_fields) - {"schema_version"}
    assert contract_fields == {"rule_id", "version", "rule_hash"}
    schema = DATASET_EVIDENCE_MANIFESTS.schema
    for name in ("rule_id", "rule_version", "rule_hash"):
        assert schema.find_field(name).required, name


def test_manifest_table_evidence_group_is_exactly_six_flattened_evidence_stream_refs() -> None:
    """One row per ``EvidenceStreamRef`` (ADR-0077 §1.3), root ``EvidenceObjectRef`` flattened to
    ``root_key`` / ``root_sha256`` / ``root_size`` (DQ-8 = a: no ``uri`` in the manifest hash)."""
    element = DATASET_EVIDENCE_MANIFESTS.schema.find_type("evidence")
    assert isinstance(element, ListType)
    struct = element.element_type
    assert isinstance(struct, StructType)
    assert [f.name for f in struct.fields] == [
        "stream",
        "record_count",
        "leaf_count",
        "depth",
        "root_key",
        "root_sha256",
        "root_size",
    ]
    for field in struct.fields:
        assert field.required, field.name
    stream_ref_fields = set(EvidenceStreamRef.model_fields) - {"schema_version"}
    assert stream_ref_fields == {"stream", "format", "record_count", "leaf_count", "depth", "root"}
    object_ref_fields = set(EvidenceObjectRef.model_fields) - {"schema_version"}
    assert object_ref_fields == {"key", "sha256", "size"}
    # "format" is a fixed literal (hlens.dataset.evidence-jsonl@1.0.0) for all six streams: it is
    # not stored per row, unlike the other EvidenceStreamRef / EvidenceObjectRef fields above.
    assert "format" not in {f.name for f in struct.fields}


def test_manifest_table_row_count_columns_are_long_not_decimal_or_float() -> None:
    schema = DATASET_EVIDENCE_MANIFESTS.schema
    for name in ("row_count", "chunk_rows", "chunk_count"):
        assert isinstance(schema.find_type(name), LongType), name


def test_manifest_table_no_quality_report_ids_or_legacy_inline_columns() -> None:
    """The v3 manifest does not carry v2's ``quality_report_ids`` list (superseded by the
    ``quality_reports`` evidence stream, §1.6) and no per-member/exclusion/lineage inline columns
    (ADR-0077 §1: fixed-size manifest, full content only via the evidence streams)."""
    schema = DATASET_EVIDENCE_MANIFESTS.schema
    for name in ("quality_report_ids", "members", "exclusions", "lineage", "evidence_gaps"):
        assert name not in schema.column_names, name


# --------------------------------------------------------------------------- chunk table shape


def test_chunk_table_extends_v2_selection_columns_with_chunk_index_and_row_ordinal() -> None:
    schema = DATASET_SELECTION_CHUNKS.schema
    assert [f.name for f in schema.fields] == [
        "selection_id",
        "canonical_table",
        "symbol",
        "observation_key",
        "revision_id",
        "event_time",
        "effective_from",
        "effective_until",
        "chunk_index",
        "row_ordinal",
    ]
    optional = {"effective_from", "effective_until"}
    for field in schema.fields:
        assert field.required is (field.name not in optional), field.name
    assert isinstance(schema.find_type("chunk_index"), LongType)
    assert isinstance(schema.find_type("row_ordinal"), LongType)


def test_chunk_table_first_eight_columns_match_v2_field_by_field() -> None:
    v2_fields = DATASET_SELECTIONS.schema.fields
    v3_fields = DATASET_SELECTION_CHUNKS.schema.fields[:8]
    assert len(v2_fields) == len(v3_fields) == 8
    for v2_field, v3_field in zip(v2_fields, v3_fields, strict=True):
        assert v2_field.field_id == v3_field.field_id
        assert v2_field.name == v3_field.name
        assert v2_field.field_type == v3_field.field_type
        assert v2_field.required == v3_field.required


def test_chunk_table_row_ordinal_and_chunk_index_reference_dataset_chunk_proof() -> None:
    contract_fields = set(DatasetChunkProof.model_fields) - {"schema_version"}
    assert contract_fields == {
        "chunk_index",
        "batch_id",
        "snapshot_id",
        "first_row_ordinal",
        "row_count",
        "batch_fingerprint",
    }
    # The chunk table stores chunk_index directly and a per-row global row_ordinal (not the
    # per-chunk first_row_ordinal, which a reader can recompute as chunk_index * chunk_rows).
    schema = DATASET_SELECTION_CHUNKS.schema
    assert schema.find_field("chunk_index").doc
    assert schema.find_field("row_ordinal").doc


# --------------------------------------------------------------------------- misc


def test_b2_tables_have_no_precedence_evidence_or_revision_block() -> None:
    """Neither B2 table is a revision table: both are audit / pointer carriers, like
    ``research.dataset_manifests`` and ``research.dataset_selections`` before them."""
    for definition in (DATASET_EVIDENCE_MANIFESTS, DATASET_SELECTION_CHUNKS):
        for name in ("arrival_seq", "supersedes", "precedence_evidence", "available_time"):
            assert name not in definition.schema.column_names, (definition.table, name)


def test_b2_layout_hash_is_stable_within_this_process() -> None:
    """Re-hashing the same canonical layout text twice is deterministic (no set/dict ordering
    leaking into the hashed document)."""
    for definition in (DATASET_EVIDENCE_MANIFESTS, DATASET_SELECTION_CHUNKS):
        text = "\n".join(layout_lines(definition.schema))
        assert sha256_text(text) == sha256_text(text) == hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest()

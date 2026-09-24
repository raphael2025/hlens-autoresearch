"""C3 production table definitions: exact layout, stable hashes, registry and evolution rules.

No catalog here (pure definitions); PostgreSQL evidence is in ``test_phase1_tables_postgres``.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from pyiceberg.io.pyarrow import schema_to_pyarrow
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema, assign_fresh_schema_ids, index_name_by_id
from pyiceberg.transforms import DayTransform, IdentityTransform, MonthTransform
from pyiceberg.types import (
    BooleanType,
    DecimalType,
    ListType,
    LongType,
    NestedField,
    StringType,
    StructType,
    TimestamptzType,
)

import infrastructure.catalog as catalog_package
from core.contracts.universe import ResearchDatasetManifest
from infrastructure.catalog import (
    PHASE1_REGISTRY,
    PHASE1_TABLES,
    PYARROW_BATCH_FINGERPRINT,
    RegisteredTableDefinition,
    TableDefinitionRegistry,
    describe_partition_spec,
)
from infrastructure.catalog.phase1_tables import (
    CANONICAL_BARS_1M,
    EXCHANGE_DECIMAL,
    PHASE1_TABLE_PROPERTIES,
)
from tests.infrastructure.catalog import catalog_support
from tests.infrastructure.catalog.phase1_support import (
    BARS_MONTH,
    ROW_BUILDERS,
    bars_month_target,
    batch_for,
    listing_from_row,
    listing_revision,
    manifest,
    manifest_row,
    revision_from_row,
)

FROZEN_TABLES = (
    "raw.binance_spot_archives",
    "raw.binance_spot_agg_trades",
    "raw.binance_spot_klines_1m",
    "canonical.trades",
    "canonical.bars_1m",
    "canonical.instrument_listings",
    "quality.data_quality_reports",
    "research.dataset_manifests",
)

#: 03-data.md §7.1 initial partitions: (partition field id, transform, source column, name).
FROZEN_PARTITIONS: dict[str, list[tuple[int, str, str, str]]] = {
    "raw.binance_spot_archives": [],
    "raw.binance_spot_agg_trades": [
        (1000, "identity", "symbol", "symbol"),
        (1001, "day", "event_time", "event_time_day"),
    ],
    "raw.binance_spot_klines_1m": [
        (1000, "identity", "symbol", "symbol"),
        (1001, "day", "interval_start", "interval_start_day"),
    ],
    "canonical.trades": [
        (1000, "identity", "symbol", "symbol"),
        (1001, "day", "event_time", "event_time_day"),
    ],
    "canonical.bars_1m": [
        (1000, "identity", "symbol", "symbol"),
        (1001, "day", "interval_start", "interval_start_day"),
    ],
    "canonical.instrument_listings": [],
    "quality.data_quality_reports": [],
    "research.dataset_manifests": [],
}

#: Golden SHA-256 values. Any change of schema (IDs, names, types, requiredness, docs), partition
#: spec, properties or fingerprint rule changes them and needs a new definition version.
GOLDEN: dict[str, tuple[str, str, str]] = {
    # table: (definition_hash, layout sha256, arrow schema sha256)
    "raw.binance_spot_archives": (
        "5a42b574cb196576757b883a0f33550efc3672d42ee365cd8724747002c061ff",
        "069e834bfb3cad9bff39189f0b200782bbc3b96a404023acfe2fc0a9acb6813d",
        "cbf35064f895af737525b5afe9c2b5feff9dcd2b09f351f30d471c61c622d34f",
    ),
    "raw.binance_spot_agg_trades": (
        "0df01b43022b30875342fcfe9f2a0c77f1af9a20e2a14e56c3a5c70cf3565309",
        "c5abc813e57759fa7d863eb0d1b2e2872d5753fbfa65b32d94cc10d1f8220119",
        "9a457fde2fa666e0bc74d795ad93b3d1ab7b639b178a7169531d8e176c6ad077",
    ),
    "raw.binance_spot_klines_1m": (
        "95d7047ff5160faa547db31f1628281f074f949007824660716c32cac65878ee",
        "e39f04fca8e3a3411e21f6c0a414cb2726acf6c877eb51f9cc20e6ba8448b028",
        "600b5138e6787be9c5e2351ddf1c03981d8a0934e196160a68f01f001eabf5a3",
    ),
    "canonical.trades": (
        "ae42d6594c9a8470fffdd38fef2f3f38a2a0dd52a05348620505a55e2afb4838",
        "6c9907856f3182d2303ce8f85d8042d736577e41fe4e1da2040cddd3fdb64ac5",
        "cd27449d42779338d7d9a07fc40f05645d790fedc31ef05deb2594ab24e44f47",
    ),
    "canonical.bars_1m": (
        "2e8ceca3de0c62d3af8aa59beaa21ddb223680c217663527c6497d5f0a43530f",
        "6730e67a8a9e6440b7ddae348fcc25cc9fc229bb3580781539a4b894743ce132",
        "336d59ffb56570bd2e1f725df928059658960577696a32689dcec4ad26055e41",
    ),
    "canonical.instrument_listings": (
        "e04260f8ca257213c0536531f244d06fe60c2f4c1f3a72ae43ce37802a7ad78e",
        "37fa92a674f1ccef38fab73876ef4ccbf3d2122b184ea9c12b96da73ef29f624",
        "0ce29e547f40826696deeaa8c4823d677e7f183528b1869d14346969fbdfa504",
    ),
    "quality.data_quality_reports": (
        "21c620530d19073082acf8abdeb72d5801e72919aabea50ce84319c7f1ce798d",
        "7e542c97bda85c0ccb7b544c6ccaad84f0f326ea9231aa22d0470834ce9e68c8",
        "85ddc18db12993d11283e2ef99d1a172b2a682bfbf61a0d432e837e738a04118",
    ),
    "research.dataset_manifests": (
        "d9dffebfa701bd245e8c299a024046abe7246755f5c5c5b42f73569ca4b53c75",
        "4eaf9e4472f5c47dcbdc73cbfe5c1416862edf25b8ce1cbc3d678ada5d272c52",
        "bd0d4f5771543c180d34c4e256ff57bc4760f5289c426efac007f9c389ad4942",
    ),
}

REVISION_TABLES = FROZEN_TABLES[:6]
INTERVAL_TABLES = ("raw.binance_spot_klines_1m", "canonical.bars_1m")
INSTANT_TABLES = ("raw.binance_spot_agg_trades", "canonical.trades")
LINEAGE_TABLES = ("canonical.trades", "canonical.bars_1m", "canonical.instrument_listings")
REVISION_BLOCK = {
    "observation_key": True,
    "revision_id": True,
    "source_id": True,
    "payload_hash": True,
    "arrival_seq": True,
    "supersedes": True,
    "source_revision_id": False,
    "source_revision_time": False,
    "source_time": False,
    "available_time": True,
    "ingest_time": True,
    "knowledge_time": True,
    "declared_latency_us": True,
    "availability_policy_id": True,
    "availability_policy_version": True,
    "availability_policy_hash": True,
    "availability_evidence": True,
    "availability_evidence_gap": False,
    "precedence_evidence": True,
    "contract_schema_version": True,
}


def layout_lines(schema: Schema) -> list[str]:
    """``field_id|full.name|type|required`` for every field, nested included."""
    names = index_name_by_id(schema)
    return [
        f"{field_id}|{names[field_id]}|{schema.find_type(field_id)}|"
        f"{schema.find_field(field_id).required}"
        for field_id in sorted(names)
    ]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def arrow_text(schema: pa.Schema) -> str:
    return str(schema.to_string(show_field_metadata=False, show_schema_metadata=False))


def by_table(table: str) -> RegisteredTableDefinition:
    return next(item for item in PHASE1_TABLES if item.table == table)


# --------------------------------------------------------------------------- registry


def test_registry_holds_exactly_the_eight_frozen_tables() -> None:
    assert tuple(item.table for item in PHASE1_TABLES) == FROZEN_TABLES
    assert len(PHASE1_REGISTRY) == 8
    assert [item.table for item in PHASE1_REGISTRY] == list(FROZEN_TABLES)
    for definition in PHASE1_TABLES:
        assert definition.definition_id == definition.table
        assert definition.version == "1.0.0"
        assert definition.evolves_from is None
        assert definition.fingerprint_rule is PYARROW_BATCH_FINGERPRINT
        assert dict(definition.properties) == dict(PHASE1_TABLE_PROPERTIES)
        assert PHASE1_REGISTRY.resolve(definition.binding) is definition
    c2_tables = {item.table for item in (catalog_support.ALPHA, catalog_support.BETA)}
    assert not c2_tables & set(FROZEN_TABLES)
    for c2 in (catalog_support.ALPHA, catalog_support.BETA):
        with pytest.raises(Exception, match="not registered"):
            PHASE1_REGISTRY.resolve(c2.binding)


def test_production_entry_points_are_exported() -> None:
    for name in (
        "PHASE1_TABLES",
        "PHASE1_REGISTRY",
        "ensure_phase1_tables",
        "PYARROW_BATCH_FINGERPRINT",
        "PYARROW_BATCH_FINGERPRINT_RULE_ID",
    ):
        assert name in catalog_package.__all__
    assert catalog_package.PYARROW_BATCH_FINGERPRINT_RULE_ID == "hlens.pyarrow-batch-sha256@1.0.0"


@pytest.mark.parametrize("table", FROZEN_TABLES)
def test_layout_matches_golden_and_creation_ids(table: str) -> None:
    definition = by_table(table)
    fresh = assign_fresh_schema_ids(definition.schema)
    assert fresh.model_dump_json() == definition.schema.model_dump_json()
    assert definition.arrow_schema.equals(
        schema_to_pyarrow(definition.schema, include_field_ids=False), check_metadata=True
    )
    expected_hash, expected_layout, expected_arrow = GOLDEN[table]
    assert definition.definition_hash == expected_hash
    assert sha256_text("\n".join(layout_lines(definition.schema))) == expected_layout
    assert sha256_text(arrow_text(definition.arrow_schema)) == expected_arrow


@pytest.mark.parametrize("table", FROZEN_TABLES)
def test_initial_partition_specs_are_the_frozen_ones(table: str) -> None:
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
    assert actual == FROZEN_PARTITIONS[table]
    expected_text = ", ".join(f"{t}({c})" for _, t, c, _ in FROZEN_PARTITIONS[table])
    assert describe_partition_spec(definition) == (expected_text or "unpartitioned")


@pytest.mark.parametrize("table", FROZEN_TABLES)
def test_physical_types_are_exact_and_utc(table: str) -> None:
    allowed = (
        StringType,
        LongType,
        BooleanType,
        TimestamptzType,
        DecimalType,
        ListType,
        StructType,
    )
    definition = by_table(table)
    for line in layout_lines(definition.schema):
        field_id = int(line.split("|")[0])
        field_type = definition.schema.find_type(field_id)
        assert isinstance(field_type, allowed), line
        if isinstance(field_type, DecimalType):
            assert field_type == EXCHANGE_DECIMAL
        if not line.split("|")[1].endswith(".element"):  # list elements carry no doc
            assert definition.schema.find_field(field_id).doc, f"{line} has no doc"
    for field in definition.arrow_schema:
        assert not pa.types.is_floating(field.type)


@pytest.mark.parametrize("table", REVISION_TABLES)
def test_revision_tables_carry_the_revision_block(table: str) -> None:
    schema = by_table(table).schema
    for name, required in REVISION_BLOCK.items():
        assert schema.find_field(name).required is required, name
    if table in INTERVAL_TABLES:
        assert schema.find_field("interval_start").required
        assert schema.find_field("interval_end").required
        assert "event_time" not in schema.column_names
    elif table in INSTANT_TABLES:
        assert schema.find_field("event_time").required
        assert "event_end_time" not in schema.column_names
    else:
        assert schema.find_field("event_time").required
        assert not schema.find_field("event_end_time").required
    lineage = [f"lineage_{hop}" for hop in ("raw_table", "raw_revision_id", "source_table")]
    lineage.append("lineage_source_revision_id")
    for name in lineage:
        assert (name in schema.column_names) is (table in LINEAGE_TABLES), name


# --------------------------------------------------------------------------- hashes


@dataclass(frozen=True)
class _OtherRule:
    rule_id: str = "hlens.pyarrow-batch-sha256@1.0.1"

    def fingerprint(self, batch: pa.Table) -> str:
        return PYARROW_BATCH_FINGERPRINT.fingerprint(batch)


def _variant(**changes: Any) -> RegisteredTableDefinition:
    base = CANONICAL_BARS_1M
    fields: dict[str, Any] = {
        "table": base.table,
        "definition_id": base.definition_id,
        "version": base.version,
        "schema": base.schema,
        "fingerprint_rule": base.fingerprint_rule,
        "partition_spec": base.partition_spec,
        "properties": base.properties,
    }
    fields.update(changes)
    return RegisteredTableDefinition(**fields)


def _schema_with(field_id: int, replacement: NestedField) -> Schema:
    base = CANONICAL_BARS_1M.schema
    return Schema(
        *(replacement if f.field_id == field_id else f for f in base.fields),
        schema_id=base.schema_id,
    )


def test_definition_hash_is_sensitive_to_every_part() -> None:
    base = CANONICAL_BARS_1M
    close = base.schema.find_field("close")
    start = base.schema.find_field("interval_start").field_id
    symbol = base.schema.find_field("symbol").field_id
    variants = {
        "doc": _variant(schema=_schema_with(close.field_id, NestedField(
            close.field_id, "close", close.field_type, required=True, doc="changed"))),
        "type": _variant(schema=_schema_with(close.field_id, NestedField(
            close.field_id, "close", DecimalType(38, 8), required=True, doc=close.doc))),
        "required": _variant(schema=_schema_with(close.field_id, NestedField(
            close.field_id, "close", close.field_type, required=False, doc=close.doc))),
        "spec": _variant(partition_spec=PartitionSpec(
            PartitionField(symbol, 1000, IdentityTransform(), "symbol"),
            PartitionField(start, 1001, MonthTransform(), "interval_start_month"))),
        "rule": _variant(fingerprint_rule=_OtherRule()),
        "properties": _variant(properties={"write.parquet.compression-codec": "snappy"}),
        "version": _variant(version="1.0.1"),
    }  # fmt: skip
    hashes = {name: item.definition_hash for name, item in variants.items()}
    assert base.definition_hash not in hashes.values()
    assert len(set(hashes.values())) == len(hashes)
    # Rebuilding the same content is deterministic.
    assert _variant().definition_hash == base.definition_hash
    same_spec = _variant(partition_spec=PartitionSpec(
        PartitionField(symbol, 1000, IdentityTransform(), "symbol"),
        PartitionField(start, 1001, DayTransform(), "interval_start_day")))  # fmt: skip
    assert same_spec.definition_hash == base.definition_hash


def test_definition_hashes_are_stable_across_processes() -> None:
    script = (
        "from infrastructure.catalog import PHASE1_TABLES\n"
        "print(','.join(d.definition_hash for d in PHASE1_TABLES))\n"
    )
    outputs = {
        subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env={"PYTHONHASHSEED": seed, "PATH": ""},
        ).stdout.strip()
        for seed in ("0", "1", "random")
    }
    assert outputs == {",".join(item.definition_hash for item in PHASE1_TABLES)}


def test_c2_initial_definition_documents_are_unchanged() -> None:
    """C3's ``evolves_from`` key is only added for targets: C2 hashes stay byte-identical."""
    assert catalog_support.ALPHA.definition_hash == (
        "afe4254cdff3579d74ce7a04fc6efe81333cca450ea211fd0d1831edbe6765b5"
    )
    assert catalog_support.BETA.definition_hash == (
        "09a66245f16d9f6615ba30c524021fa25de3d4d63c53e78d765def083fd98ea4"
    )
    assert catalog_support.ALPHA_V110.definition_hash == (
        "7f700bf8183ab0b327f7b3ec7b2616cf0f54590f75f73d8d31bf4c2d54f79774"
    )


# --------------------------------------------------------------------------- evolution targets


def test_evolution_target_keeps_declared_ids_and_binds_its_source() -> None:
    assert BARS_MONTH.evolves_from == CANONICAL_BARS_1M.binding
    assert BARS_MONTH.is_evolution_target
    assert [f.field_id for f in BARS_MONTH.partition_spec.fields] == [1000, 1002]
    assert BARS_MONTH.partition_spec.spec_id == 1
    assert BARS_MONTH.schema.model_dump_json() == CANONICAL_BARS_1M.schema.model_dump_json()
    # The source binding is part of the hashed document.
    unbound = RegisteredTableDefinition(
        table=BARS_MONTH.table,
        definition_id=BARS_MONTH.definition_id,
        version=BARS_MONTH.version,
        schema=BARS_MONTH.schema,
        fingerprint_rule=BARS_MONTH.fingerprint_rule,
        partition_spec=BARS_MONTH.partition_spec,
        properties=BARS_MONTH.properties,
    )
    assert unbound.definition_hash != BARS_MONTH.definition_hash
    assert [f.field_id for f in unbound.partition_spec.fields] == [1000, 1001]


def _target(**changes: Any) -> RegisteredTableDefinition:
    base = bars_month_target()
    fields: dict[str, Any] = {
        "table": base.table,
        "definition_id": base.definition_id,
        "version": base.version,
        "schema": base.schema,
        "fingerprint_rule": base.fingerprint_rule,
        "partition_spec": base.partition_spec,
        "properties": base.properties,
        "evolves_from": base.evolves_from,
    }
    fields.update(changes)
    return RegisteredTableDefinition(**fields)


def test_registry_rejects_targets_that_change_more_than_the_spec() -> None:
    close = CANONICAL_BARS_1M.schema.find_field("close")
    bad = {
        "schema": _target(schema=_schema_with(close.field_id, NestedField(
            close.field_id, "close", close.field_type, required=True, doc="other"))),
        "rule": _target(fingerprint_rule=_OtherRule()),
        "properties": _target(properties={"write.parquet.compression-codec": "snappy"}),
        "same spec": _target(partition_spec=PartitionSpec(
            *CANONICAL_BARS_1M.partition_spec.fields, spec_id=0)),
        "same version": _target(version="1.0.0"),
        "older version": _target(version="0.9.0"),
    }  # fmt: skip
    for label, target in bad.items():
        with pytest.raises(ValueError):
            TableDefinitionRegistry((*PHASE1_TABLES, target))
        assert label
    with pytest.raises(ValueError, match="unregistered"):
        TableDefinitionRegistry((BARS_MONTH,))
    with pytest.raises(ValueError, match="same table"):
        _target(table="canonical.trades")
    with pytest.raises(ValueError, match="below the Iceberg range"):
        bars_month_target(field_id=999)


# --------------------------------------------------------------------------- contract carriage


@pytest.mark.parametrize("table", REVISION_TABLES)
def test_revision_rows_round_trip_to_contracts(table: str) -> None:
    definition = by_table(table)
    row = ROW_BUILDERS[table]("rt")
    stored = batch_for(definition, [row]).to_pylist()[0]
    record, evidence = revision_from_row(stored)
    expected, expected_evidence = revision_from_row(row)
    assert record.content_hash() == expected.content_hash()
    assert [e.content_hash() for e in evidence] == [e.content_hash() for e in expected_evidence]
    assert record.revision_id == row["revision_id"]


def test_listing_and_manifest_rows_rebuild_identical_contracts() -> None:
    listing, _ = listing_revision("rt")
    stored = batch_for(by_table("canonical.instrument_listings"), [ROW_BUILDERS[
        "canonical.instrument_listings"]("rt")]).to_pylist()[0]  # fmt: skip
    assert listing_from_row(stored).content_hash() == listing.content_hash()

    original = manifest("rt")
    stored = batch_for(by_table("research.dataset_manifests"), [manifest_row("rt")]).to_pylist()[0]
    rebuilt = ResearchDatasetManifest.model_validate_json(stored["manifest_json"])
    assert rebuilt == original
    assert rebuilt.content_hash() == stored["manifest_content_hash"] == original.content_hash()
    assert sha256_text(stored["manifest_json"]) == stored["manifest_content_hash"]
    assert stored["point_in_time_hash"] == original.point_in_time.content_hash()
    assert [b["table"] for b in stored["snapshot_bindings"]] == sorted(
        original.point_in_time.snapshot_bindings
    )

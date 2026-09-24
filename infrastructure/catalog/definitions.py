"""Implementation-side table definition registry for the PyIceberg catalog (Phase 1 C2).

A ``TableDefinition`` DTO only carries a binding (`definition_id` + SemVer + `definition_hash`).
This module binds each registered binding to the concrete PyIceberg ``Schema``, partition spec,
table properties and the versioned batch fingerprint rule. The ``definition_hash`` is **derived**
here as the SHA-256 of a canonical JSON document of that content; it is never taken from a caller.

C2 ships only the mechanism. The eight production tables, their schemas / partitions and the
frozen PyArrow fingerprint rule belong to C3 and are injected through the same registry.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Protocol

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.io.pyarrow import schema_to_pyarrow
from pyiceberg.partitioning import (
    UNPARTITIONED_PARTITION_SPEC,
    PartitionSpec,
    assign_fresh_partition_spec_ids,
)
from pyiceberg.schema import Schema, assign_fresh_schema_ids

from core.contracts.catalog import (
    TableDefinition,
    UnknownTableDefinition,
    validate_table_name,
)
from core.contracts.revision import BINDING_ID_PATTERN
from core.domain.base import SEMVER_PATTERN

__all__ = [
    "DEFINITION_DOCUMENT_KIND",
    "ICEBERG_FORMAT_VERSION",
    "PROPERTY_COMMIT_RETRIES",
    "PROPERTY_DEFINITION_HASH",
    "PROPERTY_DEFINITION_ID",
    "PROPERTY_DEFINITION_VERSION",
    "PROPERTY_FINGERPRINT_RULE",
    "RESERVED_PROPERTY_PREFIXES",
    "BatchFingerprintRule",
    "RegisteredTableDefinition",
    "TableDefinitionRegistry",
]

#: Identifies the canonical definition document format hashed into ``definition_hash``.
DEFINITION_DOCUMENT_KIND: Final = "hlens.iceberg-table-definition/1"
#: Iceberg table format version pinned for every C2 table.
ICEBERG_FORMAT_VERSION: Final = 2

PROPERTY_DEFINITION_ID: Final = "hlens.definition.id"
PROPERTY_DEFINITION_VERSION: Final = "hlens.definition.version"
PROPERTY_DEFINITION_HASH: Final = "hlens.definition.hash"
PROPERTY_FINGERPRINT_RULE: Final = "hlens.batch.fingerprint-rule"
#: PyIceberg 0.12 retries a failed commit by rebasing onto the new head and *dropping* the
#: ``AssertRefSnapshotId`` requirement. That would turn a stale ``expected_parent_snapshot_id``
#: into a silent success, so every table pins retries to zero and the adapter verifies it.
PROPERTY_COMMIT_RETRIES: Final = "commit.retry.num-retries"

#: Property keys owned by the adapter; registered definitions must not set them.
RESERVED_PROPERTY_PREFIXES: Final = ("hlens.", "commit.retry.", "format-version")

_BINDING_ID_RE = re.compile(BINDING_ID_PATTERN)
_SEMVER_RE = re.compile(SEMVER_PATTERN)
_RULE_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]*@[0-9A-Za-z.+-]+$")


class BatchFingerprintRule(Protocol):
    """A registered, versioned rule computing a batch content fingerprint.

    ``rule_id`` names the rule and its version (``name@semver``) and is part of the hashed
    definition document; ``fingerprint`` returns lowercase SHA-256 hex computed only from the
    actual batch. C3 freezes the production PyArrow rule; C2 tests inject a minimal rule.
    """

    @property
    def rule_id(self) -> str: ...

    def fingerprint(self, batch: pa.Table) -> str: ...


def _canonical_json(document: Mapping[str, Any]) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


@dataclass(frozen=True, eq=False)
class RegisteredTableDefinition:
    """One registered table definition: binding + concrete Iceberg layout + fingerprint rule.

    Schema and partition field IDs are normalised the same way Iceberg assigns them on table
    creation, so the stored table can be compared with the registered layout after a restart.
    """

    table: str
    definition_id: str
    version: str
    schema: Schema
    fingerprint_rule: BatchFingerprintRule
    partition_spec: PartitionSpec = UNPARTITIONED_PARTITION_SPEC
    properties: Mapping[str, str] = field(default_factory=dict)
    _definition_hash: str = field(init=False, repr=False)
    _arrow_schema: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        validate_table_name(self.table)
        if _BINDING_ID_RE.fullmatch(self.definition_id) is None:
            raise ValueError(f"definition_id {self.definition_id!r} is not a binding id")
        if _SEMVER_RE.fullmatch(self.version) is None:
            raise ValueError(f"version {self.version!r} is not SemVer")
        if _RULE_ID_RE.fullmatch(self.fingerprint_rule.rule_id) is None:
            raise ValueError(f"fingerprint rule id {self.fingerprint_rule.rule_id!r} is invalid")
        properties = dict(self.properties)
        for key, value in properties.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("table properties must map str to str")
            if key.startswith(RESERVED_PROPERTY_PREFIXES):
                raise ValueError(f"table property {key!r} is reserved for the catalog adapter")
        fresh_schema = assign_fresh_schema_ids(self.schema)
        fresh_spec = assign_fresh_partition_spec_ids(self.partition_spec, self.schema, fresh_schema)
        object.__setattr__(self, "schema", fresh_schema)
        object.__setattr__(self, "partition_spec", fresh_spec)
        object.__setattr__(self, "properties", MappingProxyType(properties))
        object.__setattr__(
            self, "_arrow_schema", schema_to_pyarrow(fresh_schema, include_field_ids=False)
        )
        document = {
            "kind": DEFINITION_DOCUMENT_KIND,
            "table": self.table,
            "definition_id": self.definition_id,
            "version": self.version,
            "format_version": ICEBERG_FORMAT_VERSION,
            "schema": json.loads(fresh_schema.model_dump_json()),
            "partition_spec": json.loads(fresh_spec.model_dump_json()),
            "properties": properties,
            "batch_fingerprint_rule": self.fingerprint_rule.rule_id,
        }
        digest = hashlib.sha256(_canonical_json(document)).hexdigest()
        object.__setattr__(self, "_definition_hash", digest)

    @property
    def definition_hash(self) -> str:
        """SHA-256 of the canonical definition document (derived, never declared)."""
        return self._definition_hash

    @property
    def arrow_schema(self) -> pa.Schema:
        """The exact Arrow schema a batch for this table must have."""
        return self._arrow_schema

    @property
    def binding(self) -> TableDefinition:
        return TableDefinition(
            table=self.table,
            definition_id=self.definition_id,
            version=self.version,
            definition_hash=self.definition_hash,
        )

    def table_properties(self) -> dict[str, str]:
        """Properties written at table creation and verified on every load."""
        return {
            **self.properties,
            PROPERTY_DEFINITION_ID: self.definition_id,
            PROPERTY_DEFINITION_VERSION: self.version,
            PROPERTY_DEFINITION_HASH: self.definition_hash,
            PROPERTY_FINGERPRINT_RULE: self.fingerprint_rule.rule_id,
            PROPERTY_COMMIT_RETRIES: "0",
        }


class TableDefinitionRegistry:
    """Immutable registry keyed by ``(definition_id, version)``."""

    def __init__(self, definitions: Iterable[RegisteredTableDefinition]) -> None:
        entries: dict[tuple[str, str], RegisteredTableDefinition] = {}
        for definition in definitions:
            if not isinstance(definition, RegisteredTableDefinition):
                raise TypeError("registry entries must be RegisteredTableDefinition")
            key = (definition.definition_id, definition.version)
            if key in entries:
                raise ValueError(f"definition {key!r} is registered twice")
            entries[key] = definition
        self._entries = MappingProxyType(entries)

    def __len__(self) -> int:
        return len(self._entries)

    def resolve(self, binding: TableDefinition) -> RegisteredTableDefinition:
        """The registered definition for ``binding``; otherwise ``UnknownTableDefinition``."""
        entry = self._entries.get((binding.definition_id, binding.version))
        if (
            entry is None
            or entry.table != binding.table
            or entry.definition_hash != binding.definition_hash
        ):
            raise UnknownTableDefinition(
                f"table definition {binding.definition_id}@{binding.version} for "
                f"{binding.table} is not registered with that hash"
            )
        return entry

"""Append-only catalog persistence for fixed-size ADR-0093 quality report manifests.

This primitive validates and commits one manifest row. It does not derive report projections,
prove source completeness, read stream objects, or integrate a reporter. Existing v1/v2 report rows
remain on their legacy table and path.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.expressions import EqualTo

from core.contracts.catalog import (
    BATCH_ID_PATTERN,
    SNAPSHOT_ID_PATTERN,
    BatchConflict,
    CommitRequest,
    TableNotFound,
    validate_table_name,
)
from core.domain.base import SEMVER_PATTERN, parse_semver
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import DATA_QUALITY_REPORT_MANIFESTS
from infrastructure.quality.report_streams import (
    QUALITY_REPORT_STREAM_FORMAT,
    QualityReportStreamLimits,
    QualityReportStreamRef,
)

__all__ = ["QualityReportManifestStore"]

_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_RULE_ID_RE: Final = re.compile(r"^[a-z][a-z0-9_.-]*$")
_VERSION_RE: Final = re.compile(SEMVER_PATTERN)
_BATCH_ID_RE: Final = re.compile(BATCH_ID_PATTERN)
_SNAPSHOT_ID_RE: Final = re.compile(SNAPSHOT_ID_PATTERN)
_STREAM_NAMES: Final = ("events", "event_revisions", "evidence_gaps")
_STREAM_REF_FIELDS: Final = frozenset(
    {"format_id", "record_count", "leaf_count", "depth", "root_key", "root_sha256", "root_size"}
)
_STREAM_ROOT_RE: Final = re.compile(r"^quality/report-evidence/v1/(?P<sha256>[0-9a-f]{64})\.jsonl$")
_UTC = UTC


def _text(name: str, value: object, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not any(not character.isspace() for character in value):
        qualifier = " or null" if optional else ""
        raise CatalogIntegrityError(f"manifest {name} must be non-empty text{qualifier}")
    try:
        for start in range(0, len(value), 1024):
            value[start : start + 1024].encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CatalogIntegrityError(f"manifest {name} must be valid UTF-8") from exc
    return value


def _integer(name: str, value: object, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CatalogIntegrityError(f"manifest {name} must be an integer >= {minimum}")
    return value


def _canonical_jsonl_size(value: object, *, maximum: int) -> int:
    """Count canonical JSON UTF-8 bytes plus LF without materializing the whole row."""
    size = 0

    def add(data: bytes) -> None:
        nonlocal size
        size += len(data)
        if size > maximum:
            raise CatalogIntegrityError(
                "manifest canonical JSONL row exceeds max_manifest_record_bytes"
            )

    def add_text(text: str) -> None:
        add(b'"')
        for offset in range(0, len(text), 256):
            chunk = text[offset : offset + 256]
            try:
                escaped = json.dumps(chunk, ensure_ascii=False)[1:-1].encode("utf-8")
            except UnicodeEncodeError as exc:
                raise CatalogIntegrityError("manifest text must be valid UTF-8") from exc
            add(escaped)
        add(b'"')

    def visit(item: object) -> None:
        if isinstance(item, str):
            add_text(item)
        elif isinstance(item, datetime):
            add_text(item.isoformat())
        elif item is None or isinstance(item, (bool, int)):
            add(json.dumps(item, allow_nan=False).encode("ascii"))
        elif isinstance(item, list):
            add(b"[")
            for index, child in enumerate(item):
                if index:
                    add(b",")
                visit(child)
            add(b"]")
        elif isinstance(item, Mapping):
            add(b"{")
            for index, key in enumerate(sorted(item)):
                if not isinstance(key, str):
                    raise CatalogIntegrityError("manifest JSON object keys must be text")
                if index:
                    add(b",")
                add_text(key)
                add(b":")
                visit(item[key])
            add(b"}")
        else:
            raise CatalogIntegrityError("manifest row contains a non-canonical JSON value")

    visit(value)
    add(b"\n")
    return size


def _utc_datetime(name: str, value: object, *, optional: bool = False) -> datetime | None:
    if optional and value is None:
        return None
    if not isinstance(value, datetime):
        raise CatalogIntegrityError(f"manifest {name} must be an aware UTC datetime")
    try:
        offset = value.utcoffset()
    except Exception as exc:
        raise CatalogIntegrityError(f"manifest {name} has an invalid UTC offset") from exc
    if value.tzinfo is None or offset is None or offset != timedelta(0):
        raise CatalogIntegrityError(f"manifest {name} must have UTC offset +00:00")
    return value.astimezone(_UTC)


def _stream_ref_row(value: object, *, expected_stream: str, fanout: int) -> dict[str, Any]:
    if isinstance(value, QualityReportStreamRef):
        if value.stream != expected_stream:
            raise CatalogIntegrityError(
                f"manifest {expected_stream} reference names stream {value.stream!r}"
            )
        reference: Mapping[str, Any] = {
            "format_id": value.format,
            "record_count": value.record_count,
            "leaf_count": value.leaf_count,
            "depth": value.depth,
            "root_key": value.root_key,
            "root_sha256": value.root_sha256,
            "root_size": value.root_size,
        }
    elif isinstance(value, Mapping):
        if set(value) != _STREAM_REF_FIELDS:
            raise CatalogIntegrityError(
                f"manifest {expected_stream} reference has an invalid field set"
            )
        reference = value
    else:
        raise CatalogIntegrityError(
            f"manifest {expected_stream} must be a QualityReportStreamRef or reference mapping"
        )

    format_id = _text(f"{expected_stream}.format_id", reference.get("format_id"))
    if format_id != QUALITY_REPORT_STREAM_FORMAT:
        raise CatalogIntegrityError(f"manifest {expected_stream} has an unsupported stream format")
    record_count = _integer(f"{expected_stream}.record_count", reference.get("record_count"))
    leaf_count = _integer(f"{expected_stream}.leaf_count", reference.get("leaf_count"))
    depth = _integer(f"{expected_stream}.depth", reference.get("depth"), minimum=1)
    root_size = _integer(f"{expected_stream}.root_size", reference.get("root_size"), minimum=1)
    root_sha256 = _text(f"{expected_stream}.root_sha256", reference.get("root_sha256"))
    assert root_sha256 is not None
    if _SHA256_RE.fullmatch(root_sha256) is None:
        raise CatalogIntegrityError(f"manifest {expected_stream} root SHA-256 is invalid")
    root_key = _text(f"{expected_stream}.root_key", reference.get("root_key"))
    assert root_key is not None
    match = _STREAM_ROOT_RE.fullmatch(root_key)
    if match is None or match.group("sha256") != root_sha256:
        raise CatalogIntegrityError(
            f"manifest {expected_stream} root key is not derived from SHA-256"
        )

    if record_count == 0:
        if leaf_count != 0 or depth != 1:
            raise CatalogIntegrityError(f"manifest {expected_stream} empty stream shape is invalid")
    else:
        if leaf_count < 1 or leaf_count > record_count:
            raise CatalogIntegrityError(f"manifest {expected_stream} leaf count is inconsistent")
        expected_depth = 1
        capacity = fanout
        while capacity < leaf_count:
            capacity *= fanout
            expected_depth += 1
        if depth != expected_depth:
            raise CatalogIntegrityError(f"manifest {expected_stream} depth is not canonical")
    max_root_size = 512 * (fanout + 1)
    if root_size > max_root_size:
        raise CatalogIntegrityError(
            f"manifest {expected_stream} root size exceeds tree shape bound"
        )
    return {
        "format_id": format_id,
        "record_count": record_count,
        "leaf_count": leaf_count,
        "depth": depth,
        "root_key": root_key,
        "root_sha256": root_sha256,
        "root_size": root_size,
    }


class QualityReportManifestStore:
    """Read and append one canonical v3 manifest per report ID.

    ``allowed_snapshot_tables`` is a finite collection supplied by the report rule. Its size is the
    runtime maximum binding count; ``required_snapshot_tables`` optionally names the subset every
    manifest must include. Stream limits are explicit and validate the canonical root depth and
    object shape.
    """

    def __init__(
        self,
        adapter: Any,
        *,
        quality_rule_id: str,
        quality_rule_version: str,
        quality_rule_hash: str,
        allowed_snapshot_tables: Collection[str],
        stream_limits: QualityReportStreamLimits,
        max_manifest_record_bytes: int,
        required_snapshot_tables: Collection[str] = (),
    ) -> None:
        if not isinstance(quality_rule_id, str) or _RULE_ID_RE.fullmatch(quality_rule_id) is None:
            raise CatalogIntegrityError("quality_rule_id is invalid")
        if (
            not isinstance(quality_rule_version, str)
            or _VERSION_RE.fullmatch(quality_rule_version) is None
        ):
            raise CatalogIntegrityError("quality_rule_version must be SemVer")
        try:
            parse_semver(quality_rule_version)
        except ValueError as exc:
            raise CatalogIntegrityError("quality_rule_version must be SemVer") from exc
        if (
            not isinstance(quality_rule_hash, str)
            or _SHA256_RE.fullmatch(quality_rule_hash) is None
        ):
            raise CatalogIntegrityError("quality_rule_hash must be lowercase SHA-256 hex")
        allowed = self._table_collection("allowed_snapshot_tables", allowed_snapshot_tables)
        required = self._table_collection(
            "required_snapshot_tables", required_snapshot_tables, allow_empty=True
        )
        if not required.issubset(allowed):
            raise CatalogIntegrityError("required snapshot tables must be in the allowed set")
        if not isinstance(stream_limits, QualityReportStreamLimits):
            raise CatalogIntegrityError("stream_limits must be QualityReportStreamLimits")
        if (
            isinstance(max_manifest_record_bytes, bool)
            or not isinstance(max_manifest_record_bytes, int)
            or max_manifest_record_bytes < 1
        ):
            raise CatalogIntegrityError("max_manifest_record_bytes must be a positive integer")
        self._adapter = adapter
        self._quality_rule_id = quality_rule_id
        self._quality_rule_version = quality_rule_version
        self._quality_rule_hash = quality_rule_hash
        self._allowed_snapshot_tables = allowed
        self._required_snapshot_tables = required
        self._stream_limits = stream_limits
        self._max_manifest_record_bytes = max_manifest_record_bytes
        self._definition = DATA_QUALITY_REPORT_MANIFESTS
        self._schema = self._definition.arrow_schema
        self._columns = tuple(field.name for field in self._schema)

    @staticmethod
    def _table_collection(
        name: str, values: Collection[str], *, allow_empty: bool = False
    ) -> frozenset[str]:
        if isinstance(values, str) or not isinstance(values, Collection):
            raise CatalogIntegrityError(f"{name} must be a finite collection of table names")
        tables: list[str] = []
        for value in values:
            try:
                table = validate_table_name(value)
            except (TypeError, ValueError) as exc:
                raise CatalogIntegrityError(f"{name} contains an invalid table name") from exc
            tables.append(table)
        if len(tables) != len(set(tables)):
            raise CatalogIntegrityError(f"{name} contains duplicate table names")
        if not tables and not allow_empty:
            raise CatalogIntegrityError(f"{name} must not be empty")
        return frozenset(tables)

    def _report_id(self, value: object) -> str:
        report_id = _text("report_id", value)
        assert report_id is not None
        if _BATCH_ID_RE.fullmatch(report_id) is None:
            raise CatalogIntegrityError("manifest report_id is not a valid catalog batch id")
        expected_prefix = f"{self._quality_rule_id}@{self._quality_rule_version}."
        if not report_id.startswith(expected_prefix):
            raise CatalogIntegrityError(
                "manifest report_id does not match the configured rule identity"
            )
        return report_id

    def _snapshot_bindings(self, value: object) -> list[dict[str, str]]:
        if not isinstance(value, list):
            raise CatalogIntegrityError("manifest snapshot_bindings must be a list")
        if len(value) > len(self._allowed_snapshot_tables):
            raise CatalogIntegrityError("manifest snapshot bindings exceed the finite allowlist")
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        previous: str | None = None
        for index, raw in enumerate(value):
            if not isinstance(raw, Mapping) or set(raw) != {"table", "snapshot_id"}:
                raise CatalogIntegrityError(f"snapshot_bindings[{index}] has an invalid shape")
            try:
                table = validate_table_name(raw.get("table"))
            except (TypeError, ValueError) as exc:
                raise CatalogIntegrityError(f"snapshot_bindings[{index}].table is invalid") from exc
            if table not in self._allowed_snapshot_tables:
                raise CatalogIntegrityError(f"snapshot binding table {table} is not allowed")
            if table in seen:
                raise CatalogIntegrityError(f"snapshot_bindings repeats table {table}")
            if previous is not None and table < previous:
                raise CatalogIntegrityError("manifest snapshot_bindings must be sorted by table")
            snapshot_id = _text(f"snapshot_bindings[{index}].snapshot_id", raw.get("snapshot_id"))
            assert snapshot_id is not None
            if _SNAPSHOT_ID_RE.fullmatch(snapshot_id) is None:
                raise CatalogIntegrityError(f"snapshot_bindings[{index}].snapshot_id is invalid")
            result.append({"table": table, "snapshot_id": snapshot_id})
            previous = table
            seen.add(table)
        if not self._required_snapshot_tables.issubset(seen):
            missing = sorted(self._required_snapshot_tables - seen)
            raise CatalogIntegrityError(
                f"manifest is missing required snapshot bindings: {missing}"
            )
        return result

    def _normalize(self, row: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(row, Mapping):
            raise CatalogIntegrityError("manifest row must be a mapping")
        expected = set(self._columns)
        if set(row) != expected:
            missing = sorted(expected - set(row))
            extra = sorted(set(row) - expected)
            raise CatalogIntegrityError(
                f"manifest row has an invalid schema (missing={missing}, extra={extra})"
            )

        report_id = self._report_id(row.get("report_id"))
        if row.get("quality_rule_id") != self._quality_rule_id:
            raise CatalogIntegrityError("manifest quality_rule_id differs from store identity")
        if row.get("quality_rule_version") != self._quality_rule_version:
            raise CatalogIntegrityError("manifest quality_rule_version differs from store identity")
        if row.get("quality_rule_hash") != self._quality_rule_hash:
            raise CatalogIntegrityError("manifest quality_rule_hash differs from store identity")
        subject_table_value = _text("subject_table", row.get("subject_table"))
        try:
            subject_table = validate_table_name(subject_table_value)
        except (TypeError, ValueError) as exc:
            raise CatalogIntegrityError("manifest subject_table is invalid") from exc
        if subject_table not in self._allowed_snapshot_tables:
            raise CatalogIntegrityError(
                "manifest subject_table is not in the allowed snapshot tables"
            )
        subject_snapshot_id = _text(
            "subject_snapshot_id", row.get("subject_snapshot_id"), optional=True
        )
        if (
            subject_snapshot_id is not None
            and _SNAPSHOT_ID_RE.fullmatch(subject_snapshot_id) is None
        ):
            raise CatalogIntegrityError("manifest subject_snapshot_id is invalid")
        subject_symbol = _text("subject_symbol", row.get("subject_symbol"), optional=True)
        subject_start = _utc_datetime("subject_start", row.get("subject_start"), optional=True)
        subject_end = _utc_datetime("subject_end", row.get("subject_end"), optional=True)
        if (subject_start is None) != (subject_end is None):
            raise CatalogIntegrityError(
                "manifest subject interval requires both endpoints or neither"
            )
        if subject_start is not None and subject_end is not None and subject_start >= subject_end:
            raise CatalogIntegrityError("manifest subject interval must have positive duration")
        knowledge_time = _utc_datetime("knowledge_time", row.get("knowledge_time"))
        assert knowledge_time is not None
        bindings = self._snapshot_bindings(row.get("snapshot_bindings"))
        subject_binding = next(
            (binding for binding in bindings if binding["table"] == subject_table), None
        )
        if (subject_binding is None) != (subject_snapshot_id is None) or (
            subject_binding is not None and subject_binding["snapshot_id"] != subject_snapshot_id
        ):
            raise CatalogIntegrityError(
                "manifest subject snapshot and its table binding must be present and equal"
            )

        normalized: dict[str, Any] = {
            "report_id": report_id,
            "quality_rule_id": self._quality_rule_id,
            "quality_rule_version": self._quality_rule_version,
            "quality_rule_hash": self._quality_rule_hash,
            "subject_table": subject_table,
            "subject_snapshot_id": subject_snapshot_id,
            "subject_symbol": subject_symbol,
            "subject_start": subject_start,
            "subject_end": subject_end,
            "knowledge_time": knowledge_time,
            "snapshot_bindings": bindings,
        }
        for stream_name in _STREAM_NAMES:
            normalized[stream_name] = _stream_ref_row(
                row.get(stream_name),
                expected_stream=stream_name,
                fanout=self._stream_limits.fanout,
            )
        _canonical_jsonl_size(normalized, maximum=self._max_manifest_record_bytes)
        try:
            arrow_table = pa.Table.from_pylist([normalized], schema=self._schema)
        except (pa.ArrowException, TypeError, ValueError) as exc:
            raise CatalogIntegrityError(
                f"manifest row does not match its Arrow schema: {exc}"
            ) from exc
        if not arrow_table.schema.equals(self._schema, check_metadata=False):
            raise CatalogIntegrityError("manifest Arrow schema differs from registered schema")
        result = arrow_table.to_pylist()
        if len(result) != 1:
            raise CatalogIntegrityError("manifest Arrow normalization did not return one row")
        return cast(dict[str, Any], result[0])

    def lookup(self, report_id: str) -> dict[str, Any] | None:
        """Read one manifest by report ID; never writes or reads a clock."""
        report_id = self._report_id(report_id)
        try:
            table = self._adapter.scan_columns(
                self._definition.table,
                columns=self._columns,
                row_filter=EqualTo("report_id", report_id),  # type: ignore[call-arg, arg-type]
                limit=2,
            )
        except TableNotFound:
            return None
        rows = table.to_pylist()
        if len(rows) > 1:
            raise CatalogIntegrityError(f"quality report manifest {report_id} is committed twice")
        if not rows:
            return None
        normalized = self._normalize(rows[0])
        if normalized["report_id"] != report_id:
            raise CatalogIntegrityError("manifest lookup returned a different report ID")
        return normalized

    def commit(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Append, replay idempotently, then read back and compare the exact normalized row.

        A concurrent parent-snapshot conflict is deliberately propagated as ``CommitConflict`` so
        the caller can retry with the same report ID. A batch ID reused for other content is an
        integrity violation; no overwrite or repair path exists.
        """
        normalized = self._normalize(row)
        report_id = normalized["report_id"]
        existing = self.lookup(report_id)
        if existing is not None:
            if existing != normalized:
                raise CatalogIntegrityError(
                    f"quality report manifest {report_id} already has different content"
                )
            return existing

        batch = pa.Table.from_pylist([normalized], schema=self._schema)
        if not batch.schema.equals(self._schema, check_metadata=False):
            raise CatalogIntegrityError("manifest batch schema differs from registered schema")
        fingerprint = self._definition.fingerprint_rule.fingerprint(batch)
        info = self._adapter.load_table(self._definition.table)
        if info is None:
            raise TableNotFound(f"table {self._definition.table} does not exist")
        request = CommitRequest(
            table=self._definition.table,
            batch_id=report_id,
            batch_fingerprint=fingerprint,
            row_count=1,
            expected_parent_snapshot_id=(
                None if info.current_snapshot is None else info.current_snapshot.snapshot_id
            ),
        )
        try:
            self._adapter.commit_batch(request, batch)
        except BatchConflict as exc:
            raise CatalogIntegrityError(
                f"quality report manifest {report_id} batch ID has different content"
            ) from exc
        read_back = self.lookup(report_id)
        if read_back != normalized:
            raise CatalogIntegrityError(
                f"quality report manifest {report_id} reads back differently after commit"
            )
        return read_back

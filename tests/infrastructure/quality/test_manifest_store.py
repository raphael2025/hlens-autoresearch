"""ADR-0093 quality-manifest catalog commit/read primitive on SQLite RestHarness."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest

from core.contracts.catalog import CommitConflict, CommitRequest
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import DATA_QUALITY_REPORT_MANIFESTS
from infrastructure.quality.manifest_store import (
    QualityReportManifestStore,
    _canonical_jsonl_size,
    derive_quality_report_id,
)
from infrastructure.quality.report_projection import CANONICAL_PARTITION_V3_RULE_HASH
from infrastructure.quality.report_streams import (
    QUALITY_REPORT_STREAM_FORMAT,
    QualityReportStreamLimits,
    QualityReportStreamRef,
    QualityReportStreamWriter,
)
from tests.infrastructure.revision.rest_store_support import RestHarness, sqlite_harness

RULE_ID = "hlens.quality.canonical-partition"
RULE_VERSION = "3.0.0"
RULE_HASH = CANONICAL_PARTITION_V3_RULE_HASH
IDENTITY_RULE_HASHES = {
    "quality": RULE_HASH,
    "pit.maximal-head@1.0.0": "b" * 64,
    "policy.required@1.0.0": "c" * 64,
}
MAX_IDENTITY_RULE_HASHES = 4
STREAM_LIMITS = QualityReportStreamLimits(leaf_max_records=3, leaf_max_bytes=4096, fanout=2)
ALLOWED_TABLES = ("canonical.bars_1m", "raw.binance_spot_klines_1m")
REQUIRED_TABLES = ("canonical.bars_1m",)
MAX_MANIFEST_RECORD_BYTES = 32_768


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[RestHarness]:
    with sqlite_harness(tmp_path) as opened:
        yield opened


def store(
    harness: RestHarness, *, max_record_bytes: int = MAX_MANIFEST_RECORD_BYTES
) -> QualityReportManifestStore:
    return QualityReportManifestStore(
        harness.adapter,
        quality_rule_id=RULE_ID,
        quality_rule_version=RULE_VERSION,
        quality_rule_hash=RULE_HASH,
        allowed_snapshot_tables=ALLOWED_TABLES,
        required_snapshot_tables=REQUIRED_TABLES,
        identity_rule_hashes=IDENTITY_RULE_HASHES,
        max_identity_rule_hashes=MAX_IDENTITY_RULE_HASHES,
        stream_limits=STREAM_LIMITS,
        max_manifest_record_bytes=max_record_bytes,
    )


def fake_report_id(digest: str) -> str:
    return f"{RULE_ID}@{RULE_VERSION}.canonical.bars_1m.BTCUSDT.2024-12-31.{digest}"


def stream_ref(harness: RestHarness, name: str) -> QualityReportStreamRef:
    writer = QualityReportStreamWriter(harness.storage, name, limits=STREAM_LIMITS)
    return writer.finish()


def manifest_row(
    harness: RestHarness,
    *,
    report_id: str | None = None,
    knowledge_time: datetime | None = None,
) -> dict[str, Any]:
    day = datetime(2024, 12, 31, tzinfo=UTC)
    row: dict[str, Any] = {
        "quality_rule_id": RULE_ID,
        "quality_rule_version": RULE_VERSION,
        "quality_rule_hash": RULE_HASH,
        "subject_table": "canonical.bars_1m",
        "subject_snapshot_id": "4242",
        "subject_symbol": "BTCUSDT",
        "subject_start": day,
        "subject_end": day + timedelta(days=1),
        "knowledge_time": knowledge_time or day + timedelta(days=601),
        "snapshot_bindings": [
            {"table": "canonical.bars_1m", "snapshot_id": "4242"},
            {"table": "raw.binance_spot_klines_1m", "snapshot_id": "4241"},
        ],
        "events": stream_ref(harness, "events"),
        "event_revisions": stream_ref(harness, "event_revisions"),
        "evidence_gaps": stream_ref(harness, "evidence_gaps"),
    }
    row["report_id"] = report_id or derive_quality_report_id(
        quality_rule_id=RULE_ID,
        quality_rule_version=RULE_VERSION,
        quality_rule_hash=RULE_HASH,
        identity_rule_hashes=IDENTITY_RULE_HASHES,
        max_identity_rule_hashes=MAX_IDENTITY_RULE_HASHES,
        subject_table=row["subject_table"],
        subject_snapshot_id=row["subject_snapshot_id"],
        subject_symbol=row["subject_symbol"],
        subject_start=row["subject_start"],
        subject_end=row["subject_end"],
        snapshot_bindings=row["snapshot_bindings"],
        max_identity_bytes=MAX_MANIFEST_RECORD_BYTES,
    )
    return row


def test_commit_readback_and_same_row_idempotency(harness: RestHarness) -> None:
    manifest_store = store(harness)
    row = manifest_row(harness)
    stored = manifest_store.commit(row)
    assert stored == manifest_store.lookup(row["report_id"])
    assert stored["events"]["format_id"] == QUALITY_REPORT_STREAM_FORMAT
    assert stored["events"]["record_count"] == 0
    assert stored["snapshot_bindings"] == row["snapshot_bindings"]
    assert stored["knowledge_time"].utcoffset() == timedelta(0)

    before = harness.adapter.load_table(DATA_QUALITY_REPORT_MANIFESTS.table)
    assert before is not None and before.current_snapshot is not None
    snapshot_before = before.current_snapshot.snapshot_id
    repeated = manifest_store.commit(row)
    after = harness.adapter.load_table(DATA_QUALITY_REPORT_MANIFESTS.table)
    assert after is not None and after.current_snapshot is not None
    assert repeated == stored
    assert after.current_snapshot.snapshot_id == snapshot_before


def test_lookup_existing_only_is_read_only_and_missing_returns_none(
    harness: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_store = store(harness)
    assert manifest_store.lookup(fake_report_id("0" * 64)) is None
    row = manifest_row(harness)
    expected = manifest_store.commit(row)

    def reject_write(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("lookup attempted to write")

    monkeypatch.setattr(harness.adapter, "commit_batch", reject_write)
    assert manifest_store.lookup(row["report_id"]) == expected
    assert manifest_store.lookup(fake_report_id("1" * 64)) is None


def test_same_report_id_with_different_content_is_integrity_error(harness: RestHarness) -> None:
    manifest_store = store(harness)
    row = manifest_row(harness)
    manifest_store.commit(row)
    writer = QualityReportStreamWriter(harness.storage, "events", limits=STREAM_LIMITS)
    writer.append({"changed": True})
    changed = dict(row, events=writer.finish())
    with pytest.raises(CatalogIntegrityError, match="already has different content"):
        manifest_store.commit(changed)


def test_report_id_binds_rule_hash_subject_and_exact_snapshot_bindings(
    harness: RestHarness,
) -> None:
    row = manifest_row(harness)
    with pytest.raises(CatalogIntegrityError, match="does not match its rule, subject"):
        store(harness).commit(dict(row, report_id=fake_report_id("f" * 64)))

    changed_bindings = [dict(binding) for binding in row["snapshot_bindings"]]
    changed_bindings[1]["snapshot_id"] = "4243"
    with pytest.raises(CatalogIntegrityError, match="does not match its rule, subject"):
        store(harness).commit(dict(row, snapshot_bindings=changed_bindings))

    def derived(
        *,
        rule_hash: str,
        symbol: str,
        bindings: list[dict[str, str]],
        hashes: dict[str, str] | None = None,
    ) -> str:
        return derive_quality_report_id(
            quality_rule_id=RULE_ID,
            quality_rule_version=RULE_VERSION,
            quality_rule_hash=rule_hash,
            identity_rule_hashes=hashes or {**IDENTITY_RULE_HASHES, "quality": rule_hash},
            max_identity_rule_hashes=MAX_IDENTITY_RULE_HASHES,
            subject_table=row["subject_table"],
            subject_snapshot_id=row["subject_snapshot_id"],
            subject_symbol=symbol,
            subject_start=row["subject_start"],
            subject_end=row["subject_end"],
            snapshot_bindings=bindings,
            max_identity_bytes=MAX_MANIFEST_RECORD_BYTES,
        )

    original = derived(rule_hash=RULE_HASH, symbol="BTCUSDT", bindings=row["snapshot_bindings"])
    assert (
        derived(
            rule_hash=RULE_HASH,
            symbol="BTCUSDT",
            bindings=row["snapshot_bindings"],
            hashes=dict(reversed(tuple(IDENTITY_RULE_HASHES.items()))),
        )
        == original
    )
    assert (
        derived(rule_hash="b" * 64, symbol="BTCUSDT", bindings=row["snapshot_bindings"]) != original
    )
    assert (
        derived(rule_hash=RULE_HASH, symbol="ETHUSDT", bindings=row["snapshot_bindings"])
        != original
    )
    assert derived(rule_hash=RULE_HASH, symbol="BTCUSDT", bindings=changed_bindings) != original
    changed_pit_hashes = dict(IDENTITY_RULE_HASHES, **{"pit.maximal-head@1.0.0": "d" * 64})
    changed_policy_hashes = dict(IDENTITY_RULE_HASHES, **{"policy.required@1.0.0": "e" * 64})
    assert (
        derived(
            rule_hash=RULE_HASH,
            symbol="BTCUSDT",
            bindings=row["snapshot_bindings"],
            hashes=changed_pit_hashes,
        )
        != original
    )
    assert (
        derived(
            rule_hash=RULE_HASH,
            symbol="BTCUSDT",
            bindings=row["snapshot_bindings"],
            hashes=changed_policy_hashes,
        )
        != original
    )
    with pytest.raises(CatalogIntegrityError, match="max_identity_bytes"):
        derive_quality_report_id(
            quality_rule_id=RULE_ID,
            quality_rule_version=RULE_VERSION,
            quality_rule_hash=RULE_HASH,
            identity_rule_hashes=IDENTITY_RULE_HASHES,
            max_identity_rule_hashes=MAX_IDENTITY_RULE_HASHES,
            subject_table=row["subject_table"],
            subject_snapshot_id=row["subject_snapshot_id"],
            subject_symbol=row["subject_symbol"],
            subject_start=row["subject_start"],
            subject_end=row["subject_end"],
            snapshot_bindings=row["snapshot_bindings"],
            max_identity_bytes=1,
        )


def test_lookup_rejects_duplicate_manifest_rows(harness: RestHarness) -> None:
    manifest_store = store(harness)
    row = manifest_store.commit(manifest_row(harness))
    definition = DATA_QUALITY_REPORT_MANIFESTS
    batch = pa.Table.from_pylist([row], schema=definition.arrow_schema)
    info = harness.adapter.load_table(definition.table)
    assert info is not None
    request = CommitRequest(
        table=definition.table,
        batch_id="out-of-band-copy",
        batch_fingerprint=definition.fingerprint_rule.fingerprint(batch),
        row_count=1,
        expected_parent_snapshot_id=(
            None if info.current_snapshot is None else info.current_snapshot.snapshot_id
        ),
    )
    harness.adapter.commit_batch(request, batch)
    with pytest.raises(CatalogIntegrityError, match="committed twice"):
        manifest_store.lookup(row["report_id"])


def test_commit_conflict_is_left_for_caller_retry(
    harness: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_store = store(harness)

    def conflict(*args: Any, **kwargs: Any) -> Any:
        raise CommitConflict("simulated parent-snapshot race")

    monkeypatch.setattr(harness.adapter, "commit_batch", conflict)
    with pytest.raises(CommitConflict, match="simulated parent-snapshot race"):
        manifest_store.commit(manifest_row(harness))


@pytest.mark.parametrize(
    ("binding_mutation", "message"),
    [
        (
            lambda bindings: [
                *bindings,
                {"table": "raw.binance_spot_agg_trades", "snapshot_id": "42"},
            ],
            "exceed the finite allowlist",
        ),
        (
            lambda bindings: [bindings[0], dict(bindings[0])],
            "repeats table",
        ),
        (
            lambda bindings: list(reversed(bindings)),
            "sorted by table",
        ),
        (lambda bindings: [bindings[1]], "required snapshot bindings"),
    ],
)
def test_snapshot_bindings_must_be_allowed_unique_sorted_and_complete(
    harness: RestHarness, binding_mutation: Any, message: str
) -> None:
    row = manifest_row(harness)
    row["snapshot_bindings"] = binding_mutation(row["snapshot_bindings"])
    with pytest.raises(CatalogIntegrityError, match=message):
        store(harness).commit(row)


def test_subject_table_must_be_allowed_and_required_subject_binding_present(
    harness: RestHarness,
) -> None:
    manifest_store = store(harness)
    row = manifest_row(harness)
    row["subject_table"] = "raw.binance_spot_agg_trades"
    row["subject_snapshot_id"] = None
    with pytest.raises(CatalogIntegrityError, match="subject_table is not in the allowed"):
        manifest_store.commit(row)

    row = manifest_row(harness)
    row["subject_snapshot_id"] = None
    row["snapshot_bindings"] = [row["snapshot_bindings"][1]]
    with pytest.raises(CatalogIntegrityError, match="required snapshot bindings"):
        manifest_store.commit(row)


def test_subject_snapshot_and_binding_must_be_bidirectionally_consistent(
    harness: RestHarness,
) -> None:
    row = manifest_row(harness)
    row["subject_snapshot_id"] = None
    with pytest.raises(CatalogIntegrityError, match="subject snapshot and its table binding"):
        store(harness).commit(row)

    row = manifest_row(harness)
    row["subject_snapshot_id"] = "4241"
    with pytest.raises(CatalogIntegrityError, match="subject snapshot and its table binding"):
        store(harness).commit(row)


def test_manifest_jsonl_byte_limit_accepts_exact_cap_and_rejects_one_byte_less(
    harness: RestHarness,
) -> None:
    row = manifest_row(harness)
    broad_store = store(harness)
    normalized = broad_store._normalize(row)
    exact_size = _canonical_jsonl_size(normalized, maximum=MAX_MANIFEST_RECORD_BYTES)

    with pytest.raises(CatalogIntegrityError, match="exceeds max_manifest_record_bytes"):
        store(harness, max_record_bytes=exact_size - 1).commit(row)
    stored = store(harness, max_record_bytes=exact_size).commit(row)
    assert stored["report_id"] == row["report_id"]


def test_oversized_manifest_text_is_rejected_before_catalog_append(harness: RestHarness) -> None:
    manifest_store = store(harness, max_record_bytes=4096)
    row = manifest_row(harness)
    row["subject_symbol"] = "X" * 20_000
    with pytest.raises(CatalogIntegrityError, match="exceeds max_manifest_record_bytes"):
        manifest_store.commit(row)
    table = harness.adapter.load_table(DATA_QUALITY_REPORT_MANIFESTS.table)
    assert table is not None and table.current_snapshot is None


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda row: row.update(extra="field"), "invalid schema"),
        (lambda row: row.pop("subject_end"), "invalid schema"),
        (lambda row: row.update(quality_rule_hash="B" * 64), "quality_rule_hash"),
        (lambda row: row.update(quality_rule_version="3"), "quality_rule_version"),
        (lambda row: row.update(report_id="invalid/id"), "batch id"),
        (
            lambda row: row.update(report_id="other.rule@2.0.0.canonical.bars.BTCUSDT.2024-12-31"),
            "identity prefix or digest",
        ),
        (lambda row: row.update(subject_table="not-a-table"), "subject_table"),
        (
            lambda row: row.update(subject_table="raw.binance_spot_klines_1m"),
            "subject snapshot and its table binding must be present and equal",
        ),
        (lambda row: row.update(subject_snapshot_id="bad/id"), "subject_snapshot_id"),
        (lambda row: row.update(subject_start=datetime(2024, 1, 1)), "UTC offset"),
        (
            lambda row: row.update(subject_end=row["subject_start"] - timedelta(seconds=1)),
            "positive duration",
        ),
        (lambda row: row.update(knowledge_time=datetime(2024, 1, 1)), "UTC offset"),
    ],
)
def test_manifest_metadata_subject_and_schema_are_strict(
    harness: RestHarness, mutation: Any, message: str
) -> None:
    row = manifest_row(harness)
    mutation(row)
    with pytest.raises(CatalogIntegrityError, match=message):
        store(harness).commit(row)


@pytest.mark.parametrize(
    ("stream_name", "mutation", "message"),
    [
        (
            "events",
            lambda ref: replace(ref, stream="event_revisions"),
            "names stream",
        ),
        ("events", lambda ref: replace(ref, format="wrong@1.0.0"), "unsupported stream format"),
        (
            "events",
            lambda ref: replace(ref, root_sha256="b" * 64),
            "root key is not derived",
        ),
        (
            "events",
            lambda ref: replace(ref, record_count=1),
            "leaf count is inconsistent",
        ),
        (
            "events",
            lambda ref: replace(ref, depth=0),
            "depth must be an integer",
        ),
        (
            "events",
            lambda ref: replace(ref, root_size=0),
            "root_size must be an integer",
        ),
    ],
)
def test_stream_refs_require_correct_name_format_counts_and_root_shape(
    harness: RestHarness, stream_name: str, mutation: Any, message: str
) -> None:
    row = manifest_row(harness)
    row[stream_name] = mutation(row[stream_name])
    with pytest.raises(CatalogIntegrityError, match=message):
        store(harness).commit(row)


def test_stream_reference_mapping_shape_is_strict(harness: RestHarness) -> None:
    row = manifest_row(harness)
    row["events"] = {"format_id": QUALITY_REPORT_STREAM_FORMAT}
    with pytest.raises(CatalogIntegrityError, match="invalid field set"):
        store(harness).commit(row)


def test_constructor_requires_consistent_rule_and_finite_snapshot_allowlists(
    harness: RestHarness,
) -> None:
    with pytest.raises(CatalogIntegrityError, match="quality_rule_hash"):
        QualityReportManifestStore(
            harness.adapter,
            quality_rule_id=RULE_ID,
            quality_rule_version=RULE_VERSION,
            quality_rule_hash="bad",
            allowed_snapshot_tables=ALLOWED_TABLES,
            required_snapshot_tables=(),
            identity_rule_hashes=IDENTITY_RULE_HASHES,
            max_identity_rule_hashes=MAX_IDENTITY_RULE_HASHES,
            stream_limits=STREAM_LIMITS,
            max_manifest_record_bytes=MAX_MANIFEST_RECORD_BYTES,
        )
    with pytest.raises(CatalogIntegrityError, match="finite collection"):
        QualityReportManifestStore(
            harness.adapter,
            quality_rule_id=RULE_ID,
            quality_rule_version=RULE_VERSION,
            quality_rule_hash=RULE_HASH,
            allowed_snapshot_tables="canonical.bars_1m",
            required_snapshot_tables=(),
            identity_rule_hashes=IDENTITY_RULE_HASHES,
            max_identity_rule_hashes=MAX_IDENTITY_RULE_HASHES,
            stream_limits=STREAM_LIMITS,
            max_manifest_record_bytes=MAX_MANIFEST_RECORD_BYTES,
        )
    with pytest.raises(CatalogIntegrityError, match="required snapshot tables"):
        QualityReportManifestStore(
            harness.adapter,
            quality_rule_id=RULE_ID,
            quality_rule_version=RULE_VERSION,
            quality_rule_hash=RULE_HASH,
            allowed_snapshot_tables=ALLOWED_TABLES,
            required_snapshot_tables=("raw.not_allowed",),
            identity_rule_hashes=IDENTITY_RULE_HASHES,
            max_identity_rule_hashes=MAX_IDENTITY_RULE_HASHES,
            stream_limits=STREAM_LIMITS,
            max_manifest_record_bytes=MAX_MANIFEST_RECORD_BYTES,
        )
    with pytest.raises(CatalogIntegrityError, match="max_manifest_record_bytes"):
        QualityReportManifestStore(
            harness.adapter,
            quality_rule_id=RULE_ID,
            quality_rule_version=RULE_VERSION,
            quality_rule_hash=RULE_HASH,
            allowed_snapshot_tables=ALLOWED_TABLES,
            required_snapshot_tables=(),
            identity_rule_hashes=IDENTITY_RULE_HASHES,
            max_identity_rule_hashes=MAX_IDENTITY_RULE_HASHES,
            stream_limits=STREAM_LIMITS,
            max_manifest_record_bytes=0,
        )
    with pytest.raises(CatalogIntegrityError, match="max_identity_rule_hashes"):
        QualityReportManifestStore(
            harness.adapter,
            quality_rule_id=RULE_ID,
            quality_rule_version=RULE_VERSION,
            quality_rule_hash=RULE_HASH,
            allowed_snapshot_tables=ALLOWED_TABLES,
            required_snapshot_tables=(),
            identity_rule_hashes=IDENTITY_RULE_HASHES,
            max_identity_rule_hashes=2,
            stream_limits=STREAM_LIMITS,
            max_manifest_record_bytes=MAX_MANIFEST_RECORD_BYTES,
        )
    with pytest.raises(CatalogIntegrityError, match="contain quality equal"):
        QualityReportManifestStore(
            harness.adapter,
            quality_rule_id=RULE_ID,
            quality_rule_version=RULE_VERSION,
            quality_rule_hash=RULE_HASH,
            allowed_snapshot_tables=ALLOWED_TABLES,
            required_snapshot_tables=(),
            identity_rule_hashes={**IDENTITY_RULE_HASHES, "quality": "f" * 64},
            max_identity_rule_hashes=MAX_IDENTITY_RULE_HASHES,
            stream_limits=STREAM_LIMITS,
            max_manifest_record_bytes=MAX_MANIFEST_RECORD_BYTES,
        )


def test_arrow_schema_is_normalized_and_validated_on_readback(harness: RestHarness) -> None:
    manifest_store = store(harness)
    row = manifest_row(harness, knowledge_time=datetime(2026, 8, 23, 0, 0, tzinfo=UTC))
    stored = manifest_store.commit(row)
    assert tuple(stored) == tuple(DATA_QUALITY_REPORT_MANIFESTS.arrow_schema.names)
    assert stored["knowledge_time"] == datetime(2026, 8, 23, tzinfo=UTC)
    assert set(stored["events"]) == {
        "format_id",
        "record_count",
        "leaf_count",
        "depth",
        "root_key",
        "root_sha256",
        "root_size",
    }

"""ADR-0094: versioned PIT conflict evidence contract shapes and replay boundaries."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.contracts.registry import CONTRACT_MODELS, export_json_schemas
from core.contracts.universe import (
    ADR_0094_VERSION,
    DATASET_EVIDENCE_FORMAT,
    EvidenceStream,
    EvidenceStreamRef,
    PITConflictEvent,
    PITConflictEvidenceRef,
    PITConflictHeadRecord,
)
from core.domain.base import (
    CONTRACT_SCHEMA_VERSION,
    PUBLISHED_CONTRACT_SCHEMA_VERSIONS,
    Contract,
    canonical_json,
    contract_schema_version_scope,
)
from tests.test_adr_0077_evidence_manifest import (
    REPO,
    obj,
    pit_conflict_event,
    pit_conflict_record,
    stream_ref,
    v3_at,
    wire,
)

CURRENT_SCHEMA_DIR = REPO / "schemas"

PIT_CONFLICT_MODELS: tuple[type[Contract], ...] = (
    PITConflictHeadRecord,
    PITConflictEvidenceRef,
    PITConflictEvent,
)

LEGACY_MANIFEST_GOLDEN_SHA256 = {
    "2.3.0": "40899f7552ed35bbb5f505b2ba30c7007c5400190ed65664f947954e291c1da5",
    "2.4.0": "a2b89b7e5444399748d91e3d51021ec82970bfdc2edd29f39a2216fcf3a9930c",
}


def test_contract_250_is_published_and_new_models_are_appended() -> None:
    assert ADR_0094_VERSION == CONTRACT_SCHEMA_VERSION == "2.5.0"
    assert PUBLISHED_CONTRACT_SCHEMA_VERSIONS == (
        "2.0.0",
        "2.1.0",
        "2.2.0",
        "2.3.0",
        "2.4.0",
        "2.5.0",
    )
    assert CONTRACT_MODELS[-3:] == PIT_CONFLICT_MODELS
    assert all(model._MODEL_SINCE == ADR_0094_VERSION for model in PIT_CONFLICT_MODELS)


def test_pit_conflict_schemas_match_exported_and_committed_files(tmp_path: Path) -> None:
    exported = export_json_schemas(tmp_path)
    for model in PIT_CONFLICT_MODELS:
        committed = (CURRENT_SCHEMA_DIR / f"{model.__name__}.schema.json").read_bytes()
        assert committed == exported[model.__name__].read_bytes()
        schema = json.loads(committed)
        assert schema["properties"]["schema_version"]["default"] == "2.5.0"
        assert schema["additionalProperties"] is False


def test_old_dataset_manifest_golden_bytes_and_hashes_replay_unchanged() -> None:
    for version, expected_hash in LEGACY_MANIFEST_GOLDEN_SHA256.items():
        path = (
            REPO / "tests" / "golden" / f"v{version.replace('.', '_')}" / "dataset_v3_manifest.json"
        )
        original = path.read_bytes().rstrip(b"\n")
        assert hashlib.sha256(original).hexdigest() == expected_hash
        with contract_schema_version_scope(version):
            replayed = v3_at(version)
        assert replayed.schema_version == version
        assert canonical_json(replayed.model_dump(mode="json")).encode("utf-8") == original
        assert replayed.content_hash() == expected_hash
        assert type(replayed).model_validate_json(original).content_hash() == expected_hash


def test_current_2_5_manifest_and_conflict_event_replay_their_canonical_bytes() -> None:
    manifest = v3_at("2.5.0")
    event = pit_conflict_event()
    for model in (manifest, event):
        canonical = canonical_json(model.model_dump(mode="json"))
        replayed = type(model).model_validate_json(canonical)
        assert replayed.schema_version == "2.5.0"
        assert canonical_json(replayed.model_dump(mode="json")) == canonical
        assert replayed.content_hash() == model.content_hash()


def test_new_enum_value_and_models_are_rejected_by_pre_250_envelopes() -> None:
    conflict_stream = stream_ref(EvidenceStream.PIT_CONFLICTS, record_count=2)
    stream_payload = wire(conflict_stream)
    for old in ("2.3.0", "2.4.0"):
        with pytest.raises(ValidationError, match="2.5.0"):
            EvidenceStreamRef.model_validate({**stream_payload, "schema_version": old})
        with pytest.raises(ValidationError, match="2.5.0"):
            PITConflictHeadRecord.model_validate(
                {**wire(pit_conflict_record()), "schema_version": old}
            )
        with pytest.raises(ValidationError, match="2.5.0"):
            PITConflictEvidenceRef.model_validate(
                {**wire(pit_conflict_event().evidence), "schema_version": old}
            )
        with pytest.raises(ValidationError, match="2.5.0"):
            PITConflictEvent.model_validate({**wire(pit_conflict_event()), "schema_version": old})


def test_conflict_record_event_and_root_ref_are_fixed_shape_without_head_truncation() -> None:
    large = pit_conflict_record(head_count=2**63, ordinal=2**63 - 1)
    assert large.head_count == 2**63
    assert large.ordinal == large.head_count - 1
    assert "maximal_heads" not in wire(large)

    event = pit_conflict_event()
    assert event.evidence.stream.stream is EvidenceStream.PIT_CONFLICTS
    assert event.evidence.stream.record_count == event.head_count
    assert "maximal_heads" not in wire(event)
    assert "maximal_heads" not in wire(event.evidence)

    with pytest.raises(ValidationError, match="ordinal"):
        pit_conflict_record(head_count=2, ordinal=2)
    with pytest.raises(ValidationError, match="ordinal"):
        pit_conflict_record(ordinal=True)
    with pytest.raises(ValidationError, match="record_count"):
        pit_conflict_event(head_count=3)
    with pytest.raises(ValidationError, match="pit_conflicts"):
        PITConflictEvidenceRef(stream=stream_ref(EvidenceStream.LINEAGE, record_count=2))


def test_conflict_stream_ref_uses_the_existing_fixed_size_evidence_ref_shape() -> None:
    root = obj(8)
    evidence = stream_ref(EvidenceStream.PIT_CONFLICTS, record_count=2, root=root)
    assert evidence.format == DATASET_EVIDENCE_FORMAT
    assert evidence.root == root

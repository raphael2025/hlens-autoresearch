"""ADR-0094 contract boundary and versioned Dataset manifest shapes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.contracts.universe import (
    ADR_0094_VERSION,
    EvidenceStream,
    EvidenceStreamRef,
    PitConflictEvidenceResult,
    PitConflictHeadEvidence,
)
from core.domain.base import (
    CONTRACT_SCHEMA_VERSION,
    PUBLISHED_CONTRACT_SCHEMA_VERSIONS,
    contract_schema_version_scope,
)
from infrastructure.dataset.manifests import evidence_manifest_row
from tests.test_adr_0077_evidence_manifest import evidence, stream_ref, v3


def test_contract_250_registers_only_the_new_pit_conflict_models_and_value() -> None:
    assert CONTRACT_SCHEMA_VERSION == ADR_0094_VERSION == "2.5.0"
    assert PUBLISHED_CONTRACT_SCHEMA_VERSIONS == (
        "2.0.0",
        "2.1.0",
        "2.2.0",
        "2.3.0",
        "2.4.0",
        "2.5.0",
    )
    assert PitConflictHeadEvidence._MODEL_SINCE == "2.5.0"
    assert PitConflictEvidenceResult._MODEL_SINCE == "2.5.0"
    assert EvidenceStreamRef._VALUES_SINCE == {"stream": {EvidenceStream.PIT_CONFLICTS: "2.5.0"}}


def test_old_envelopes_reject_the_new_stream_and_record_model() -> None:
    with contract_schema_version_scope("2.4.0"), pytest.raises(ValidationError, match="2.5.0"):
        stream_ref(EvidenceStream.PIT_CONFLICTS, 0)
    with contract_schema_version_scope("2.4.0"), pytest.raises(ValidationError, match="2.5.0"):
        PitConflictHeadEvidence(
            rule_id="hlens.pit.maximal-head",
            rule_version="1.0.0",
            rule_hash="a" * 64,
            observation_key="key-1",
            simulation_time="2024-01-01T00:00:00Z",  # type: ignore[arg-type]
            knowledge_cutoff="2024-01-01T00:00:00Z",  # type: ignore[arg-type]
            head_count=2,
            ordinal=0,
            revision_id="r-1",
        )


def test_manifest_shapes_are_selected_by_recorded_envelope() -> None:
    for version in ("2.3.0", "2.4.0"):
        manifest = v3(schema_version=version)
        row = evidence_manifest_row(manifest)
        assert manifest.schema_version == version
        assert len(manifest.evidence) == 6
        assert len(row["evidence"]) == 6
        assert EvidenceStream.PIT_CONFLICTS not in {ref.stream for ref in manifest.evidence}

    refs = (*evidence(3), stream_ref(EvidenceStream.PIT_CONFLICTS, 0))
    legacy_pit = v3(schema_version="2.4.0").point_in_time
    current_pit = legacy_pit.model_copy(
        update={
            "snapshot_bindings": {
                **legacy_pit.snapshot_bindings,
                "quality.data_quality_report_manifests": "9104",
            }
        }
    )
    current = v3(schema_version="2.5.0", evidence=refs, point_in_time=current_pit)
    current_row = evidence_manifest_row(current)
    assert len(current.evidence) == len(current_row["evidence"]) == 7
    assert current.evidence_for(EvidenceStream.PIT_CONFLICTS).record_count == 0

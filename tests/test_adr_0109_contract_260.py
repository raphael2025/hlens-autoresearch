"""ADR-0109: contract 2.6.0 -- the v3 Dataset manifest no longer requires the legacy Quality table.

* the version registry: 2.6.0 is current and every earlier minor stays published;
* a manifest recorded at >= 2.6.0 must bind ``canonical.instrument_listings`` and
  ``quality.data_quality_report_manifests``; the legacy ``quality.data_quality_reports`` binding
  is optional (still a legal binding when present);
* a manifest recorded at < 2.6.0 keeps its rule: without the legacy binding it is refused, and a
  payload refused at its recorded version is not made valid by the 2.6.0 rule;
* the 2.5.0 seven-stream v3 manifest golden (generated before the bump, at its own envelope)
  replays byte for byte with its pinned hash.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from pydantic import ValidationError

from core.contracts.revision import PointInTimeSpec
from core.contracts.universe import (
    ADR_0094_VERSION,
    ADR_0109_VERSION,
    LISTINGS_TABLE,
    QUALITY_REPORT_MANIFESTS_TABLE,
    QUALITY_REPORTS_TABLE,
    EvidenceStream,
    ResearchDatasetEvidenceManifest,
)
from core.domain.base import (
    CONTRACT_SCHEMA_VERSION,
    PUBLISHED_CONTRACT_SCHEMA_VERSIONS,
    canonical_json,
    contract_schema_version_scope,
)
from tests.test_adr_0077_evidence_manifest import REPO, evidence, stream_ref, v3
from tests.test_universe_contracts import pit

#: The 2.5.0 seven-stream v3 manifest written under ``contract_schema_version_scope("2.5.0")``
#: by the pre-ADR-0109 code (phase/1 b0707d2) and reproduced byte for byte by the 2.6.0 code.
V3_MANIFEST_AT_2_5_0_SHA256 = "63c5bd7322d8cc7c36b4cc8194e5702569956bac35bd789aa35c70f972eac605"
V3_MANIFEST_AT_2_5_0 = REPO / "tests" / "golden" / "v2_5_0" / "dataset_v3_manifest.json"


def bindings(*, legacy: bool, manifests: bool = True, listings: bool = True) -> PointInTimeSpec:
    """The fixture PIT spec with the three Quality / listing bindings chosen explicitly."""
    base = pit()
    bound = dict(base.snapshot_bindings)
    if manifests:
        bound[QUALITY_REPORT_MANIFESTS_TABLE] = "9104"
    if not legacy:
        bound.pop(QUALITY_REPORTS_TABLE)
    if not listings:
        bound.pop(LISTINGS_TABLE)
    return base.model_copy(update={"snapshot_bindings": bound})


def seven_stream(version: str, **spec: bool) -> ResearchDatasetEvidenceManifest:
    """A 2.5.0+ manifest built entirely inside its recorded version (every nested envelope too)."""
    with contract_schema_version_scope(version):
        refs = (*evidence(3), stream_ref(EvidenceStream.PIT_CONFLICTS, 0))
        point_in_time = bindings(**spec)
    return v3(schema_version=version, evidence=refs, point_in_time=point_in_time)


def wire(instance: ResearchDatasetEvidenceManifest) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(instance.model_dump_json())
    return payload


# ======================================================================================
# version registry
# ======================================================================================


def test_contract_260_is_current_and_every_earlier_minor_stays_published() -> None:
    assert ADR_0109_VERSION == CONTRACT_SCHEMA_VERSION == "2.6.0"
    assert ADR_0094_VERSION == "2.5.0"
    assert PUBLISHED_CONTRACT_SCHEMA_VERSIONS == (
        "2.0.0",
        "2.1.0",
        "2.2.0",
        "2.3.0",
        "2.4.0",
        "2.5.0",
        "2.6.0",
    )
    assert PUBLISHED_CONTRACT_SCHEMA_VERSIONS[-1] == CONTRACT_SCHEMA_VERSION


# ======================================================================================
# >= 2.6.0: the legacy table is optional, listings and report manifests are required
# ======================================================================================


def test_a_260_manifest_without_the_legacy_table_is_valid() -> None:
    built = seven_stream("2.6.0", legacy=False)
    assert built.schema_version == "2.6.0"
    assert QUALITY_REPORTS_TABLE not in built.point_in_time.snapshot_bindings
    again = ResearchDatasetEvidenceManifest.model_validate_json(built.model_dump_json())
    assert again.content_hash() == built.content_hash()


def test_a_260_manifest_with_the_legacy_table_is_still_valid() -> None:
    built = seven_stream("2.6.0", legacy=True)
    assert built.point_in_time.snapshot_bindings[QUALITY_REPORTS_TABLE] == "7002"
    without = seven_stream("2.6.0", legacy=False)
    assert built.content_hash() != without.content_hash()  # the binding is part of the identity


def test_the_current_envelope_is_260_and_needs_no_legacy_binding() -> None:
    refs = (*evidence(3), stream_ref(EvidenceStream.PIT_CONFLICTS, 0))
    built = v3(
        schema_version=CONTRACT_SCHEMA_VERSION, evidence=refs, point_in_time=bindings(legacy=False)
    )
    assert built.schema_version == CONTRACT_SCHEMA_VERSION == "2.6.0"
    assert built.point_in_time.schema_version == "2.6.0"


@pytest.mark.parametrize(
    ("missing", "spec"),
    [
        (QUALITY_REPORT_MANIFESTS_TABLE, {"legacy": True, "manifests": False}),
        (QUALITY_REPORT_MANIFESTS_TABLE, {"legacy": False, "manifests": False}),
        (LISTINGS_TABLE, {"legacy": True, "listings": False}),
        (LISTINGS_TABLE, {"legacy": False, "listings": False}),
    ],
)
def test_a_260_manifest_still_requires_listings_and_report_manifests(
    missing: str, spec: dict[str, bool]
) -> None:
    with pytest.raises(ValidationError, match=missing):
        seven_stream("2.6.0", **spec)


# ======================================================================================
# < 2.6.0: unchanged
# ======================================================================================


def test_a_250_manifest_without_the_legacy_table_is_refused() -> None:
    with pytest.raises(ValidationError, match=QUALITY_REPORTS_TABLE):
        seven_stream("2.5.0", legacy=False)


@pytest.mark.parametrize("old", ["2.3.0", "2.4.0"])
def test_a_six_stream_manifest_without_the_legacy_table_is_refused(old: str) -> None:
    with contract_schema_version_scope(old):
        spec = bindings(legacy=False, manifests=False)
    with pytest.raises(ValidationError, match=QUALITY_REPORTS_TABLE):
        v3(schema_version=old, point_in_time=spec)


def test_a_refused_250_payload_is_not_rescued_by_the_260_rule() -> None:
    valid_at_260 = json.dumps(wire(seven_stream("2.6.0", legacy=False)))
    as_250 = json.loads(valid_at_260.replace('"2.6.0"', '"2.5.0"'))
    assert '"2.6.0"' not in json.dumps(as_250)
    with pytest.raises(ValidationError, match=QUALITY_REPORTS_TABLE):
        ResearchDatasetEvidenceManifest.model_validate(as_250)


# ======================================================================================
# the 2.5.0 golden replays byte for byte
# ======================================================================================


def test_the_250_v3_manifest_golden_replays_at_its_recorded_version() -> None:
    original = V3_MANIFEST_AT_2_5_0.read_bytes().rstrip(b"\n")
    assert hashlib.sha256(original).hexdigest() == V3_MANIFEST_AT_2_5_0_SHA256

    parsed = ResearchDatasetEvidenceManifest.model_validate_json(original)
    assert parsed.schema_version == "2.5.0"
    assert parsed.content_hash() == V3_MANIFEST_AT_2_5_0_SHA256
    assert canonical_json(parsed.model_dump(mode="json")).encode("utf-8") == original
    assert len(parsed.evidence) == 7
    assert QUALITY_REPORTS_TABLE in parsed.point_in_time.snapshot_bindings

    with contract_schema_version_scope("2.5.0"):
        rebuilt = ResearchDatasetEvidenceManifest.model_validate(json.loads(original))
    assert rebuilt.content_hash() == V3_MANIFEST_AT_2_5_0_SHA256

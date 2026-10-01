"""ADR-0081: the per-kind report payload DTO registry (``apps/api/report_dto.py``).

``decode_report_payload`` is exercised directly here (its own unit boundary: version resolution,
required-field checks, the ``paper_deviation`` 2.0.0 scope-bound rules) and through
``ReportStore``/the ``/reports/...`` endpoints (the integration boundary: a DTO failure is a 422
before any kind-specific identity check runs, and an *unsupported* version is served opaquely
without that identity check being assumed at all -- ADR-0081 §2). Nothing here imports
``research/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api import create_app
from apps.api.report_dto import (
    DEGRADATION_AUTHORITY_FORMATS,
    REPORT_DTOS,
    decode_report_payload,
)
from apps.api.store import ReportKind, ReportMalformed, ReportStore
from core.domain.base import content_hash
from tests.factories import validation_report

Payload = dict[str, Any]


def _write(root: Path, kind: ReportKind, report_id: str, payload: Payload) -> None:
    directory = root / kind.value
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{report_id}.json").write_text(json.dumps(payload), encoding="utf-8")


# --- registry shape --------------------------------------------------------------------------


def test_every_report_kind_has_a_dto_registration() -> None:
    """No ``ReportKind`` is servable without an explicit ADR-0081 registration (fail closed)."""
    assert set(REPORT_DTOS) == set(ReportKind)
    assert all(spec.required for spec in REPORT_DTOS.values())


@pytest.mark.parametrize("kind", list(ReportKind))
def test_the_baseline_version_is_itself_supported(kind: ReportKind) -> None:
    spec = REPORT_DTOS[kind]
    assert spec.baseline in spec.supported_versions


# --- version resolution -----------------------------------------------------------------------


def test_a_kind_without_schema_version_resolves_to_the_virtual_baseline() -> None:
    """A historic payload with no ``schema_version`` key uses the registered baseline (ADR-0081
    §1), and that inferred version is never injected back into the payload."""
    spec = REPORT_DTOS[ReportKind.ROUTER_PAPER_RUN]
    payload: dict[str, Any] = {field: "x" for field in spec.required}
    payload["decisions"] = []  # required field, but a list (not a string placeholder)
    dto = decode_report_payload(ReportKind.ROUTER_PAPER_RUN, payload)
    assert dto.supported is True
    assert dto.schema_version == spec.baseline
    assert "schema_version" not in dto.payload


@pytest.mark.parametrize(
    "kind",
    [ReportKind.RESEARCH_LOOP_ROUND, ReportKind.ROUTER_PAPER_RUN, ReportKind.ROUTER_STOP],
)
def test_virtual_version_kinds_forbid_a_payload_schema_version(kind: ReportKind) -> None:
    spec = REPORT_DTOS[kind]
    assert spec.has_payload_schema_version is False
    payload: Payload = {field: "x" for field in spec.required}
    if kind is ReportKind.ROUTER_PAPER_RUN:
        payload["decisions"] = []

    dto = decode_report_payload(kind, payload)
    assert dto.supported is True
    assert dto.schema_version == spec.baseline
    assert "schema_version" not in dto.payload

    with pytest.raises(ReportMalformed, match="does not carry schema_version"):
        decode_report_payload(kind, {**payload, "schema_version": spec.baseline})


def test_an_explicit_known_version_is_used_as_is() -> None:
    spec = REPORT_DTOS[ReportKind.STATE_DIAGNOSTICS]
    other = next(v for v in spec.supported_versions if v != spec.baseline)
    payload = {field: "x" for field in spec.required}
    payload["schema_version"] = other
    dto = decode_report_payload(ReportKind.STATE_DIAGNOSTICS, payload)
    assert dto.supported is True
    assert dto.schema_version == other


def test_schema_version_must_be_a_string() -> None:
    payload: dict[str, Any] = {
        field: "x" for field in REPORT_DTOS[ReportKind.EVENT_STATISTICS].required
    }
    payload["schema_version"] = 1
    with pytest.raises(ReportMalformed, match="schema_version must be a string"):
        decode_report_payload(ReportKind.EVENT_STATISTICS, payload, path=Path("<test>"))


def test_a_version_outside_the_registration_is_opaque_and_never_raises() -> None:
    """ADR-0081 §2: an unsupported version is read-only pass-through, not malformed -- even
    though it lacks every required field a supported version would need."""
    payload = {"schema_version": "99.0.0"}
    dto = decode_report_payload(ReportKind.EVENT_STATISTICS, payload)
    assert dto.supported is False
    assert dto.schema_version == "99.0.0"
    assert dto.payload == payload


@pytest.mark.parametrize("kind", [kind for kind, spec in REPORT_DTOS.items() if spec.required])
def test_a_known_version_missing_a_required_field_is_malformed(kind: ReportKind) -> None:
    spec = REPORT_DTOS[kind]
    payload = {field: "x" for field in spec.required if field != spec.required[-1]}
    if spec.has_payload_schema_version:
        payload["schema_version"] = spec.baseline
    with pytest.raises(ReportMalformed, match=f"lacks required field {spec.required[-1]}"):
        decode_report_payload(kind, payload, path=Path("<test>"))


# --- paper_deviation 2.0.0 declared scope (ADR-0079 / ADR-0081 §5) ----------------------------


def _scope(**overrides: Any) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "scope_schema_version": "1.0.0",
        "validation_profile": "profile@1.0.0",
        "validation_profile_hash": "a" * 64,
        "validation_report_hash": "b" * 64,
        "venue": "binance_spot",
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "research_class": "trend",
    }
    scope.update(overrides)
    body = {key: value for key, value in scope.items() if key != "scope_hash"}
    scope.setdefault("scope_hash", content_hash(body))
    return scope


def _deviation_payload(**overrides: Any) -> Payload:
    payload: Payload = {
        "kind": "paper_deviation",
        "deviation_hash": "c" * 64,
        "schema_version": "2.0.0",
        "declared_scope": _scope(),
        "summary": {},
        "marks": [],
    }
    payload.update(overrides)
    return payload


def test_paper_deviation_kind_field_must_match_the_report_kind() -> None:
    payload = _deviation_payload(kind="not_paper_deviation")
    with pytest.raises(ReportMalformed, match="kind does not match report kind"):
        decode_report_payload(ReportKind.PAPER_DEVIATION, payload, path=Path("<test>"))


def test_paper_deviation_2_0_0_requires_the_scope_bound_fields() -> None:
    payload = _deviation_payload()
    del payload["declared_scope"]
    with pytest.raises(ReportMalformed, match="lacks scope-bound DTO fields"):
        decode_report_payload(ReportKind.PAPER_DEVIATION, payload, path=Path("<test>"))


def test_paper_deviation_2_0_0_well_formed_scope_is_supported() -> None:
    dto = decode_report_payload(ReportKind.PAPER_DEVIATION, _deviation_payload())
    assert dto.supported is True
    assert dto.schema_version == "2.0.0"


@pytest.mark.parametrize(
    "edit",
    [
        lambda scope: {**scope, "scope_schema_version": "2.0.0"},
        lambda scope: {**scope, "venue": 1},
        lambda scope: {**scope, "validation_profile_hash": "not-hex"},
        lambda scope: {**scope, "validation_report_hash": "0" * 63},  # too short
        lambda scope: {k: v for k, v in scope.items() if k != "scope_hash"},
    ],
)
def test_paper_deviation_2_0_0_malformed_scope_is_rejected(edit: Any) -> None:
    scope = edit(_scope())
    payload = _deviation_payload(declared_scope=scope)
    with pytest.raises(ReportMalformed, match="declared_scope is invalid"):
        decode_report_payload(ReportKind.PAPER_DEVIATION, payload, path=Path("<test>"))


def test_paper_deviation_2_0_0_scope_hash_must_match_the_recomputed_scope_body() -> None:
    scope = _scope()
    scope["scope_hash"] = "f" * 64  # well-formed hex, but not the hash of the scope body
    payload = _deviation_payload(declared_scope=scope)
    with pytest.raises(ReportMalformed, match="scope_hash does not match declared_scope"):
        decode_report_payload(ReportKind.PAPER_DEVIATION, payload, path=Path("<test>"))


def test_paper_deviation_1_0_0_legacy_is_supported_without_scope_fields() -> None:
    """ADR-0081 §5: 1.0.0 remains readable as descriptive legacy material -- the 2.0.0 scope
    rules are not retroactively required of it."""
    payload = {"kind": "paper_deviation", "deviation_hash": "c" * 64, "schema_version": "1.0.0"}
    dto = decode_report_payload(ReportKind.PAPER_DEVIATION, payload)
    assert dto.supported is True
    assert dto.schema_version == "1.0.0"


# --- ADR-0094: the default Contract envelope bumped to 2.5.0 (validation_report only) ---------
#
# validation_report is the one ReportKind whose payload is a direct ``Contract.model_dump()``
# (research/reports/validation.py); every other kind's schema_version is an independent,
# domain-specific number defined by its own writer module (research/reports/*.py,
# research/router/deviation.py, research/synthetic_lab/gate_calibration.py), unrelated to
# core.domain.base.CONTRACT_SCHEMA_VERSION. ADR-0088 is an additive minor (composed strategies,
# event bar spec, peak equity, synthetic effects, volatility-scaling barrier) that does not touch
# ValidationReport's own fields, so 2.5.0 is registered with the same required-field shape as
# 2.3.0 (itself unchanged from 2.2.0, ADR-0077).


def test_validation_report_baseline_tracks_the_current_contract_envelope() -> None:
    assert REPORT_DTOS[ReportKind.VALIDATION_REPORT].baseline == "2.5.0"
    assert {"2.0.0", "2.1.0", "2.2.0", "2.3.0", "2.4.0", "2.5.0"} <= REPORT_DTOS[
        ReportKind.VALIDATION_REPORT
    ].supported_versions


def test_validation_report_2_5_0_is_a_known_version_with_the_2_3_0_shape(tmp_path: Path) -> None:
    """A freshly built ValidationReport now carries the bumped default envelope (ADR-0094); it
    must be served as a supported DTO, not fall back to raw-JSON "unknown version" display."""
    report = validation_report()
    assert report.schema_version == "2.5.0"  # core/domain/base.py's new Contract default
    payload = report.model_dump(mode="json")

    dto = decode_report_payload(ReportKind.VALIDATION_REPORT, payload)
    assert dto.supported is True
    assert dto.schema_version == "2.5.0"

    report_id = report.content_hash()
    _write(tmp_path, ReportKind.VALIDATION_REPORT, report_id, payload)
    envelope = ReportStore(tmp_path).get(ReportKind.VALIDATION_REPORT, report_id)
    assert envelope.payload["schema_version"] == "2.5.0"

    client = TestClient(create_app(reports_root=tmp_path))
    detail = client.get(f"/reports/validation_report/{report_id}")
    assert detail.status_code == 200
    listing = client.get("/reports/validation_report").json()
    assert listing["invalid"] == []
    assert [item["id"] for item in listing["reports"]] == [report_id]


def test_validation_report_2_3_0_legacy_payload_remains_supported(tmp_path: Path) -> None:
    """A pre-ADR-0088 payload persisted with the prior default envelope (2.3.0) must keep
    resolving as a supported DTO -- registering 2.5.0 must not drop 2.3.0 read access."""
    report = validation_report()
    payload = report.model_dump(mode="json")
    payload["schema_version"] = "2.3.0"

    dto = decode_report_payload(ReportKind.VALIDATION_REPORT, payload)
    assert dto.supported is True
    assert dto.schema_version == "2.3.0"


# --- integration: ReportStore / the API apply the DTO check before identity ---------------------


def test_a_dto_failure_is_422_before_any_identity_check_runs(tmp_path: Path) -> None:
    """A DTO-owned required field is checked before the kind's identity hash rule."""
    report_id = "not-a-real-hash"
    payload = {"schema_version": "1.1.0", "kind": "state_diagnostics"}  # no state_space
    _write(tmp_path, ReportKind.STATE_DIAGNOSTICS, report_id, payload)
    with pytest.raises(ReportMalformed, match="lacks required field state_space"):
        ReportStore(tmp_path).get(ReportKind.STATE_DIAGNOSTICS, report_id)
    response = TestClient(create_app(reports_root=tmp_path)).get(
        f"/reports/state_diagnostics/{report_id}"
    )
    assert response.status_code == 422
    assert "lacks required field state_space" in response.json()["detail"]


def test_an_unsupported_version_is_served_opaquely_without_the_identity_check(
    tmp_path: Path,
) -> None:
    """ADR-0081 §2: a version outside the registration is opaque read-only pass-through -- the
    file is served even though its ``report_hash`` does not match the payload (the kind's
    identity rule is not assumed to hold for a version this API does not understand)."""
    report_id = "0" * 64
    payload = {"schema_version": "99.0.0", "report_hash": "definitely-not-the-real-hash"}
    _write(tmp_path, ReportKind.EVENT_STATISTICS, report_id, payload)
    envelope = ReportStore(tmp_path).get(ReportKind.EVENT_STATISTICS, report_id)
    assert envelope.payload == payload
    listing = TestClient(create_app(reports_root=tmp_path)).get("/reports/event_statistics")
    body = listing.json()
    assert body["invalid"] == []
    assert [item["id"] for item in body["reports"]] == [report_id]


# --- degradation_check 1.1.0: optional evidence.authority (ADR-0098 §4, additive) ---------------
#
# The shapes below follow research/operations/degradation.py ``DegradationEvidence.as_mapping``
# and research/operations/authority.py ``AuthorityProvenance.payload`` (restated: nothing here
# imports research/). Hash values are syntactic placeholders; ``check_hash`` is recomputed only in
# the store test, where ReportStore's identity rule needs it.

_HISTORY, _MANIFEST, _PROFILE, _REPORT = ("1" * 64, "2" * 64, "3" * 64, "4" * 64)
_START, _END = "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"


def _evidence(**overrides: Any) -> Payload:
    evidence: Payload = {
        "lifecycle_history_hash": _HISTORY,
        "lifecycle_scope": "scope text",
        "profile_ref": "validation_profile:p@1.0.0",
        "profile_hash": _PROFILE,
        "validation_report_hash": _REPORT,
        "recent_observation_set_id": "obs-1",
        "recent_observation_set_hash": _MANIFEST,
        "metric_method_id": "method@1",
        "window_start": _START,
        "window_end": _END,
    }
    evidence.update(overrides)
    return evidence


def _lifecycle(**overrides: Any) -> Payload:
    lifecycle: Payload = {
        "head": {"record_count": 3, "last_record_hash": "5" * 64},
        "anchor": "absent",
        "history_hash": _HISTORY,
        "record_hashes": ["6" * 64, "7" * 64],
    }
    lifecycle.update(overrides)
    return lifecycle


def _authority(**overrides: Any) -> Payload:
    authority: Payload = {
        "format": "hlens.p11.authority-provenance@1.2.0",
        "subject": "strategy:trend_a@1.0.0",
        "lifecycle": _lifecycle(),
        "source": {"dataset_id": "ds-btc-1h", "manifest_hash": "8" * 64},
        "baseline": {
            "run_id": "run-1",
            "manifest_hash": "9" * 64,
            "baseline_set_hash": "a" * 64,
            "validation_report_hash": _REPORT,
            "profile_ref": "validation_profile:p@1.0.0",
            "profile_hash": _PROFILE,
        },
        "metrics": {
            "registry": "hlens.p11.monitoring-metrics@2.0.0",
            "definitions": [{"metric": "sharpe", "definition": "p11.sharpe@1.0.0"}],
        },
        "execution": {},
        "recent_manifest_hash": _MANIFEST,
        "as_of": "2026-02-02T00:00:00Z",
        "window_start": _START,
        "window_end": _END,
    }
    authority.update(overrides)
    return authority


def _degradation(evidence: Payload | None) -> Payload:
    payload: Payload = {
        "kind": "degradation_check",
        "schema_version": "1.1.0",
        "check_hash": "b" * 64,
        "metrics": [],
    }
    if evidence is not None:
        payload["evidence"] = evidence
    return payload


def test_the_current_authority_format_is_registered() -> None:
    assert "hlens.p11.authority-provenance@1.2.0" in DEGRADATION_AUTHORITY_FORMATS


def test_a_caller_declared_1_1_0_report_without_authority_is_supported() -> None:
    dto = decode_report_payload(ReportKind.DEGRADATION_CHECK, _degradation(_evidence()))
    assert dto.supported is True
    assert "authority" not in dto.payload["evidence"]


def test_a_legacy_1_0_0_report_without_evidence_is_unchanged() -> None:
    payload = {**_degradation(None), "schema_version": "1.0.0"}
    assert decode_report_payload(ReportKind.DEGRADATION_CHECK, payload).supported is True


def test_a_known_format_authority_bound_to_its_evidence_is_supported() -> None:
    evidence = _evidence(authority=_authority())
    dto = decode_report_payload(ReportKind.DEGRADATION_CHECK, _degradation(evidence))
    assert dto.supported is True
    assert dto.payload["evidence"]["authority"]["lifecycle"]["anchor"] == "absent"


def test_an_unknown_authority_format_is_kept_opaque() -> None:
    """ADR-0081 §6: an additive object of a format this API does not know is not interpreted."""
    evidence = _evidence(authority={"format": "hlens.p11.authority-provenance@9.0.0"})
    assert decode_report_payload(ReportKind.DEGRADATION_CHECK, _degradation(evidence)).supported


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        ({}, "evidence must be a non-empty object"),
        (_evidence(authority=[]), "must be an object with a string format"),
        (_evidence(authority={"lifecycle": {}}), "must be an object with a string format"),
        (_evidence(authority=_authority(source={"dataset_id": "ds"})), "source is not a"),
        (
            _evidence(authority=_authority(lifecycle=_lifecycle(anchor="maybe"))),
            "anchor must be present or absent",
        ),
        (
            _evidence(authority=_authority(lifecycle=_lifecycle(head={"record_count": True}))),
            "lifecycle.head is not",
        ),
        (
            _evidence(authority=_authority(metrics={"registry": "r", "definitions": [1]})),
            "entries must name their definition",
        ),
        (
            _evidence(authority=_authority(window_end="2026-03-01T00:00:00Z")),
            "describes another window end",
        ),
        (
            _evidence(authority=_authority(recent_manifest_hash="c" * 64)),
            "describes another recent manifest",
        ),
        (
            _evidence(lifecycle_history_hash="d" * 64, authority=_authority()),
            "describes another lifecycle history",
        ),
        (
            _evidence(profile_hash="e" * 64, authority=_authority()),
            "describes another Profile",
        ),
        (
            _evidence(authority=_authority(as_of="2026-01-15T00:00:00Z")),
            "as_of is before the window end",
        ),
        (
            _evidence(authority=_authority(as_of="yesterday")),
            "are not comparable UTC times",
        ),
    ],
)
def test_a_malformed_or_unbound_known_authority_is_malformed(
    evidence: Payload, reason: str
) -> None:
    with pytest.raises(ReportMalformed, match=reason):
        decode_report_payload(
            ReportKind.DEGRADATION_CHECK, _degradation(evidence), path=Path("<test>")
        )


def test_the_store_serves_an_authority_report_and_lists_an_unbound_one_as_invalid(
    tmp_path: Path,
) -> None:
    """Integration: the DTO check runs before ``check_hash``; a bound report is served."""

    def self_hashed(evidence: Payload) -> tuple[str, Payload]:
        body = {k: v for k, v in _degradation(evidence).items() if k != "check_hash"}
        check_hash = content_hash(body)
        return check_hash, {**body, "check_hash": check_hash}

    good_id, good = self_hashed(_evidence(authority=_authority()))
    bad_id, bad = self_hashed(_evidence(authority=_authority(window_start="2025-12-01T00:00:00Z")))
    _write(tmp_path, ReportKind.DEGRADATION_CHECK, good_id, good)
    _write(tmp_path, ReportKind.DEGRADATION_CHECK, bad_id, bad)
    listing = ReportStore(tmp_path).listing(ReportKind.DEGRADATION_CHECK)
    assert [envelope.id for envelope in listing.reports] == [good_id]
    assert [item.id for item in listing.invalid] == [bad_id]
    assert "describes another window start" in listing.invalid[0].reason

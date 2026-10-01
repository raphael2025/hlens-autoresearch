"""Version registry and structural DTO checks for report payloads (ADR-0081).

This module intentionally belongs to the API plane and does not import ``research``. Report
payloads are persisted public data, so each kind has an explicit baseline version and required
identity/display fields. Unknown versions remain available as opaque, read-only JSON for forward
compatibility; known versions with an invalid shape are rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from apps.api.store import ReportKind, ReportMalformed
from core.domain.base import content_hash

__all__ = [
    "DEGRADATION_AUTHORITY_FORMATS",
    "DEGRADATION_AUTHORITY_FORMAT_PREFIX",
    "ReportDTO",
    "ReportPayloadDTO",
    "decode_report_payload",
]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

#: ``degradation_check`` 1.1.0 ``evidence.authority`` (ADR-0098 §4 ``AuthorityProvenance``): an
#: optional, additive object inside the hash-bound evidence. Its ``format`` names the provenance
#: payload version. Only these known formats get the structural / cross-binding check below; any
#: other ``format`` is kept as opaque JSON (ADR-0081 §6: an additive field an older reader does
#: not understand is ignored, never interpreted). Restated here because ``apps/`` never imports
#: ``research/`` (``research.operations.authority.AUTHORITY_FORMAT``; tests keep them in step).
DEGRADATION_AUTHORITY_FORMAT_PREFIX: Final = "hlens.p11.authority-provenance@"
DEGRADATION_AUTHORITY_FORMATS: Final = frozenset(
    f"{DEGRADATION_AUTHORITY_FORMAT_PREFIX}{version}" for version in ("1.0.0", "1.1.0", "1.2.0")
)
#: ``authority.lifecycle.anchor`` (ADR-0098 修订 1): verified against an external anchor, or not.
_AUTHORITY_ANCHORS: Final = frozenset({"present", "absent"})


@dataclass(frozen=True)
class ReportPayloadDTO:
    kind: ReportKind
    schema_version: str
    payload: dict[str, Any]
    supported: bool


@dataclass(frozen=True)
class ReportDTO:
    baseline: str
    supported_versions: frozenset[str]
    required: tuple[str, ...]
    has_payload_schema_version: bool = True


# Some long-lived payloads predate an in-payload schema_version. Their DTO version is virtual:
# it is assigned by kind here and is not injected into payload bytes or identity hashes. For kinds
# with a domain or identity validator in ReportStore, keep this layer to version dispatch; the
# validator owns required-field errors so malformed data gets the relevant reason.
REPORT_DTOS: Final[dict[ReportKind, ReportDTO]] = {
    ReportKind.VALIDATION_REPORT: ReportDTO(
        "2.5.0",
        frozenset({"2.0.0", "2.1.0", "2.2.0", "2.3.0", "2.4.0", "2.5.0"}),
        ("schema_version", "gates", "verdict"),
    ),
    ReportKind.RESEARCH_LOOP_ROUND: ReportDTO(
        "1.0.0", frozenset({"1.0.0"}), ("round_index", "loop_id", "stages"), False
    ),
    ReportKind.STATE_STRATEGY_MATRIX: ReportDTO(
        "1.0.0", frozenset({"1.0.0"}), ("matrix_hash", "cells", "strategy", "state")
    ),
    ReportKind.ROUTER_PAPER_RUN: ReportDTO(
        "1.0.0", frozenset({"1.0.0"}), ("run_hash", "router", "decisions", "charges"), False
    ),
    ReportKind.GATE_CALIBRATION: ReportDTO(
        "1.0.0", frozenset({"1.0.0"}), ("schema_version", "report_hash", "candidates")
    ),
    ReportKind.ROUTER_STOP: ReportDTO(
        "1.0.0", frozenset({"1.0.0"}), ("stop_hash", "router", "reason"), False
    ),
    ReportKind.STATE_DIAGNOSTICS: ReportDTO(
        "1.1.0", frozenset({"1.0.0", "1.1.0"}), ("schema_version", "kind", "state_space")
    ),
    ReportKind.EVENT_STATISTICS: ReportDTO(
        "1.0.0", frozenset({"1.0.0"}), ("schema_version", "report_hash", "statistics")
    ),
    ReportKind.PAPER_DEVIATION: ReportDTO(
        "2.0.0", frozenset({"1.0.0", "2.0.0"}), ("kind", "deviation_hash")
    ),
    ReportKind.DEGRADATION_CHECK: ReportDTO(
        "1.1.0", frozenset({"1.0.0", "1.1.0"}), ("schema_version", "check_hash", "metrics")
    ),
    ReportKind.RETRO_AUDIT: ReportDTO(
        "1.1.0", frozenset({"1.0.0", "1.1.0"}), ("schema_version", "report_hash", "kind")
    ),
}


def decode_report_payload(
    kind: ReportKind, payload: dict[str, Any], *, path: Path | str = "<report>"
) -> ReportPayloadDTO:
    """Resolve a known DTO version and check its required fields; preserve unknown versions."""
    source_path = Path(path)
    spec = REPORT_DTOS[kind]
    if not spec.has_payload_schema_version and "schema_version" in payload:
        reason = (
            "research_loop_round payload does not carry schema_version (LoopRoundRecord contract)"
            if kind is ReportKind.RESEARCH_LOOP_ROUND
            else f"{kind.value} payload does not carry schema_version"
        )
        raise ReportMalformed(source_path, reason)
    raw_version = payload.get("schema_version")
    version = spec.baseline if "schema_version" not in payload else raw_version
    if not isinstance(version, str):
        raise ReportMalformed(source_path, "schema_version must be a string")
    if version not in spec.supported_versions:
        return ReportPayloadDTO(kind, version, payload, supported=False)
    if any(field not in payload for field in spec.required):
        missing = next(field for field in spec.required if field not in payload)
        raise ReportMalformed(
            source_path, f"{kind.value} {version} payload lacks required field {missing}"
        )
    if kind is ReportKind.PAPER_DEVIATION:
        if payload.get("kind") != kind.value:
            raise ReportMalformed(source_path, "paper_deviation kind does not match report kind")
        if version == "2.0.0":
            if not all(field in payload for field in ("declared_scope", "summary", "marks")):
                raise ReportMalformed(
                    source_path, "paper_deviation 2.0.0 payload lacks scope-bound DTO fields"
                )
            scope = payload["declared_scope"]
            scope_fields = (
                "validation_profile",
                "validation_profile_hash",
                "validation_report_hash",
                "venue",
                "symbol",
                "timeframe",
                "research_class",
            )
            if (
                not isinstance(scope, dict)
                or scope.get("scope_schema_version") != "1.0.0"
                or any(not isinstance(scope.get(field), str) for field in scope_fields)
                or not isinstance(scope.get("scope_hash"), str)
                or any(
                    _SHA256.fullmatch(scope[field]) is None
                    for field in ("validation_profile_hash", "validation_report_hash")
                    if isinstance(scope.get(field), str)
                )
            ):
                raise ReportMalformed(
                    source_path, "paper_deviation 2.0.0 declared_scope is invalid"
                )
            scope_body = {key: value for key, value in scope.items() if key != "scope_hash"}
            if scope.get("scope_hash") != content_hash(scope_body):
                raise ReportMalformed(
                    source_path, "paper_deviation 2.0.0 scope_hash does not match declared_scope"
                )
    if kind is ReportKind.DEGRADATION_CHECK and version == "1.1.0" and "evidence" in payload:
        _check_degradation_evidence(source_path, payload["evidence"])
    return ReportPayloadDTO(kind, version, payload, supported=True)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _check_degradation_evidence(path: Path, evidence: object) -> None:
    """The 1.1.0 ``evidence`` object and, when present, its known-format ``authority`` block.

    A report without ``evidence.authority`` is the caller-declared path (ADR-0067) and is not
    touched. With a known format the authority block must have the ``AuthorityProvenance``
    payload shape and describe the same lifecycle history, recent manifest, Profile, baseline
    report and window as the evidence around it (``run_degradation_check`` enforces exactly these
    bindings before the writer runs), so a reader can never see an authority block that belongs
    to other evidence. This checks shape and agreement only; it does not re-resolve anything.
    """
    if not isinstance(evidence, dict) or not evidence:
        raise ReportMalformed(path, "degradation_check 1.1.0 evidence must be a non-empty object")
    if "authority" not in evidence:
        return
    authority = evidence["authority"]
    if not isinstance(authority, dict) or not isinstance(authority.get("format"), str):
        raise ReportMalformed(
            path, "degradation_check evidence.authority must be an object with a string format"
        )
    if authority["format"] not in DEGRADATION_AUTHORITY_FORMATS:
        return  # an unknown provenance format: opaque JSON, never read as authority evidence

    def invalid(reason: str) -> ReportMalformed:
        return ReportMalformed(path, f"degradation_check evidence.authority {reason}")

    lifecycle = authority.get("lifecycle")
    source = authority.get("source")
    baseline = authority.get("baseline")
    metrics = authority.get("metrics")
    if not (
        isinstance(lifecycle, dict)
        and isinstance(source, dict)
        and isinstance(baseline, dict)
        and isinstance(metrics, dict)
    ):
        raise invalid("lacks a lifecycle / source / baseline / metrics object")
    head = lifecycle.get("head")
    if (
        not isinstance(head, dict)
        or not isinstance(head.get("record_count"), int)
        or isinstance(head.get("record_count"), bool)
        or head["record_count"] < 0
        or not _is_sha256(head.get("last_record_hash"))
    ):
        raise invalid("lifecycle.head is not a (record_count, last_record_hash) identity")
    if lifecycle.get("anchor") not in _AUTHORITY_ANCHORS:
        raise invalid("lifecycle.anchor must be present or absent")
    if not _is_sha256(lifecycle.get("history_hash")) or not isinstance(
        lifecycle.get("record_hashes"), list
    ):
        raise invalid("lifecycle lacks its history_hash / record_hashes")
    if not isinstance(source.get("dataset_id"), str) or not _is_sha256(source.get("manifest_hash")):
        raise invalid("source is not a (dataset_id, manifest_hash) identity")
    if not all(
        _is_sha256(baseline.get(field))
        for field in ("baseline_set_hash", "validation_report_hash", "profile_hash")
    ):
        raise invalid("baseline lacks its baseline_set_hash / report / Profile hashes")
    if not isinstance(metrics.get("registry"), str) or not isinstance(
        metrics.get("definitions"), list
    ):
        raise invalid("metrics lacks its registry / definitions")
    if not all(
        isinstance(item, dict) and isinstance(item.get("definition"), str)
        for item in metrics["definitions"]
    ):
        raise invalid("metrics.definitions entries must name their definition")
    if not all(
        isinstance(authority.get(field), str) for field in ("as_of", "window_start", "window_end")
    ):
        raise invalid("lacks its as_of / window_start / window_end")
    bindings = (
        (lifecycle["history_hash"], evidence.get("lifecycle_history_hash"), "lifecycle history"),
        (
            authority.get("recent_manifest_hash"),
            evidence.get("recent_observation_set_hash"),
            "recent manifest",
        ),
        (baseline["profile_hash"], evidence.get("profile_hash"), "Profile"),
        (
            baseline["validation_report_hash"],
            evidence.get("validation_report_hash"),
            "validation report",
        ),
        (authority["window_start"], evidence.get("window_start"), "window start"),
        (authority["window_end"], evidence.get("window_end"), "window end"),
    )
    for bound, declared, what in bindings:
        if not isinstance(bound, str) or bound != declared:
            raise invalid(f"describes another {what} than the evidence")
    try:  # ADR-0098 修订 2 §1: the evaluation time is never before the window end
        as_of = datetime.fromisoformat(authority["as_of"])
        window_end = datetime.fromisoformat(authority["window_end"])
        too_early = as_of < window_end
    except (TypeError, ValueError):  # unparseable, or naive vs aware
        raise invalid("as_of / window_end are not comparable UTC times") from None
    if too_early:
        raise invalid("as_of is before the window end")

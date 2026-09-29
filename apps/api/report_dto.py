"""Version registry and structural DTO checks for report payloads (ADR-0081).

This module intentionally belongs to the API plane and does not import ``research``. Report
payloads are persisted public data, so each kind has an explicit baseline version and required
identity/display fields. Unknown versions remain available as opaque, read-only JSON for forward
compatibility; known versions with an invalid shape are rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from apps.api.store import ReportKind, ReportMalformed
from core.domain.base import content_hash

__all__ = ["ReportDTO", "ReportPayloadDTO", "decode_report_payload"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


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
        "2.4.0",
        frozenset({"2.0.0", "2.1.0", "2.2.0", "2.3.0", "2.4.0"}),
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
    return ReportPayloadDTO(kind, version, payload, supported=True)

"""Serializable validation report view (Phase 8, ADR-0041) for a later apps/web visualization.

``report_view`` turns a ``ValidationReport`` (the contract object; its verdict is
``derive_verdict``) plus the optional G4 ``RobustnessResult`` into a plain JSON-ready ``dict``:
gates grouped by stage (G0 … G5), every threshold with its Profile field (or ``param:`` source),
each robustness check with its status, missing fields and tables (walk-forward windows, states,
instruments, neighbours) — the rows a chart needs. ``to_json`` is canonical (sorted keys), so
the same inputs give byte-identical text. This module draws nothing and has no web dependency;
the view is a research artifact (``apps/`` must not import ``research/``: a later API layer
serves the JSON, not this module).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final

from core.domain.research import ValidationReport
from research.validation.g4 import RobustnessResult
from research.validation.gates import PARAM_SOURCE_PREFIX, PROFILE_FIELD_MISSING

__all__ = ["VIEW_SCHEMA", "VIEW_SCHEMA_VERSION", "report_view", "to_json"]

VIEW_SCHEMA: Final = "hlens.research.validation_report_view"
VIEW_SCHEMA_VERSION: Final = "1.0.0"
STATUS: Final = "FRAMEWORK_IMPLEMENTED / NOT_VALIDATED"
STAGES: Final = ("G0", "G1", "G2", "G3", "G4", "G5")


def report_view(
    report: ValidationReport,
    robustness: RobustnessResult | None = None,
    *,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """A JSON-ready view of one report (see module docs)."""
    gates = [gate.model_dump(mode="json") for gate in report.gates]
    stages: dict[str, list[dict[str, object]]] = {stage: [] for stage in STAGES}
    for gate in gates:
        stage = str(gate["gate_id"]).split(".")[0]
        stages.setdefault(stage, []).append(gate)
    counts = {
        verdict: sum(1 for gate in gates if gate["verdict"] == verdict)
        for verdict in ("PASS", "FAIL", "INCONCLUSIVE")
    }
    found = {str(g["threshold_source"]) for g in gates if g["threshold_source"]}
    if robustness is not None:  # thresholds a check used without a gate carrying them
        found |= {use.source for check in robustness.checks for use in check.thresholds}
    sources = sorted(found)
    view: dict[str, object] = {
        "schema": VIEW_SCHEMA,
        "schema_version": VIEW_SCHEMA_VERSION,
        "status": STATUS,
        "report_id": report.report_id,
        "run_id": report.run_id,
        "subject": str(report.subject),
        "experiment_hash": report.experiment_hash,
        "constitution_version": report.constitution_version,
        "validation_profile": str(report.validation_profile),
        "validation_profile_hash": report.validation_profile_hash,
        "verdict": report.verdict.value,
        "created_at": report.created_at.isoformat(),
        "gate_counts": counts,
        "stages": {stage: rows for stage, rows in stages.items()},
        "stages_run": [stage for stage, rows in stages.items() if rows],
        "threshold_sources": {
            "profile": [s for s in sources if not s.startswith(PARAM_SOURCE_PREFIX)],
            "explicit_params": [s for s in sources if s.startswith(PARAM_SOURCE_PREFIX)],
        },
        "profile_fields_missing": sorted(
            str(g["metric"]).split(":", 1)[1]
            for g in gates
            if str(g["metric"]).startswith(f"{PROFILE_FIELD_MISSING}:")
        ),
        "robustness": None if robustness is None else robustness.to_dict(),
    }
    if extra:
        view["extra"] = dict(extra)
    return view


def to_json(view: Mapping[str, object]) -> str:
    """Canonical JSON text of a view (sorted keys, no NaN / Infinity)."""
    return json.dumps(view, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)

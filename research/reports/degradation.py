"""Writer for Phase 11 degradation checks (``apps/worker/degradation.py``, ADR-0049).

``DegradationMonitor.check`` compares a strategy's recent metrics with its validation baseline
and returns a ``DegradationCheck`` (subject, breaches, missing) — until now nothing reported it.
This writer records one check together with everything needed to read it: the monitor's rules
(metric, direction, allowed decline and the threshold's source), the baseline and recent value of
every ruled metric, and the window the recent values describe.

Before anything is written the check is recomputed: ``monitor.check(check.subject, baseline,
recent)`` must equal ``check`` (a check that is not what this monitor gives for these metrics is
refused, ``ValueError``), so the report cannot pair a result with inputs it did not come from.

A check in which every ruled metric is missing is ``insufficient_evidence``: the payload then
carries ``"insufficient_evidence": true`` (the key is added **only** in that case, so every other
payload and its ``check_hash`` are unchanged) next to ``"degraded": false`` — a reader must not
show it as healthy.

The payload is deterministic and JSON-ready (``Decimal`` as exact text); ``check_hash`` is the
content hash of the rest of it and the report id. ``apps/api``'s ``ReportStore`` serves it as the
``degradation_check`` kind and recomputes ``check_hash``. Evidence only: a degradation check never
changes lifecycle state (``ACTIVE -> DEGRADED`` is a Control Plane transition citing the event,
ADR-0006) and this writer holds no threshold of its own. Not wired into the research loop here.

Legacy calls retain schema 1.0.0 and their original payload/hash. The explicit P11 operation can
write additive evidence under schema 1.1.0 (ADR-0067); the evidence becomes part of ``check_hash``.

Code completion (2026-09-27, CODE_COMPLETE / DEBUG_PENDING).
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from apps.worker.degradation import DegradationCheck, DegradationMonitor
from core.domain.base import content_hash
from research.operations.degradation import DegradationOperationResult
from research.reports.envelope import WrittenReport, write_report_file

__all__ = [
    "KIND",
    "degradation_check_payload",
    "degradation_operation_payload",
    "write_degradation_check",
    "write_degradation_operation",
]

#: Directory name under the report root (``apps.api.store.ReportKind.DEGRADATION_CHECK``); equal
#: to the payload's own ``kind``.
KIND: Final = "degradation_check"
LEGACY_SCHEMA_VERSION: Final = "1.0.0"
EVIDENCE_SCHEMA_VERSION: Final = "1.1.0"
STATUS: Final = "FRAMEWORK_IMPLEMENTED / NOT_VALIDATED"
NOTE: Final = (
    "evidence only; never changes lifecycle state (ACTIVE -> DEGRADED is a Control Plane "
    "transition citing the event); thresholds come only from the named source"
)

Metrics = Mapping[str, Decimal | float | int]


def _decimal(value: Decimal | float | int, name: str) -> Decimal:
    """The monitor's own conversion (``apps/worker/degradation.py``): exact for ``Decimal`` /
    ``int``, ``repr`` for ``float``; non-finite refused."""
    number = value if isinstance(value, Decimal) else Decimal(repr(value))
    if not number.is_finite():
        raise ValueError(f"{name} must be finite")
    return number


def _degradation_check_payload(
    check: DegradationCheck,
    *,
    monitor: DegradationMonitor,
    baseline: Metrics,
    recent: Metrics,
    window: str,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The report payload (module docs), after recomputing ``check`` from its inputs."""
    if not isinstance(check, DegradationCheck) or not isinstance(monitor, DegradationMonitor):
        raise ValueError("a degradation check report needs a DegradationCheck and its monitor")
    if not isinstance(window, str) or not window.strip():
        raise ValueError("window must name the data the recent metrics describe")
    if monitor.check(check.subject, baseline, recent) != check:
        raise ValueError("the check is not what this monitor gives for these metrics; not written")
    breached = {breach["metric"] for breach in check.breaches}
    missing = set(check.missing)
    metrics: list[dict[str, Any]] = []
    for rule in monitor.rules:
        base = _decimal(baseline[rule.metric], rule.metric)
        now = None if rule.metric in missing else _decimal(recent[rule.metric], rule.metric)
        decline = None
        if now is not None:
            decline = now - base if rule.lower_is_better else base - now
        metrics.append(
            {
                "metric": rule.metric,
                "direction": "lower_is_better" if rule.lower_is_better else "higher_is_better",
                "baseline": str(base),
                "recent": None if now is None else str(now),
                "decline": None if decline is None else str(decline),
                "max_decline": str(rule.max_decline),
                "threshold_source": rule.source,
                "breached": rule.metric in breached,
                "missing": rule.metric in missing,
            }
        )
    body: dict[str, Any] = {
        "kind": KIND,
        "schema_version": LEGACY_SCHEMA_VERSION if evidence is None else EVIDENCE_SCHEMA_VERSION,
        "status": STATUS,
        "note": NOTE,
        "subject": str(check.subject),
        "window": window,
        "degraded": check.degraded,
        "metrics": metrics,
        "breaches": [dict(sorted(breach.items())) for breach in check.breaches],
        "missing": list(check.missing),
    }
    if check.insufficient_evidence:
        body["insufficient_evidence"] = True
    if evidence is not None:
        evidence_payload = dict(evidence)
        if not evidence_payload:
            raise ValueError("an evidence-bearing degradation check needs a non-empty evidence map")
        body["evidence"] = evidence_payload
    return {**body, "check_hash": content_hash(body)}


def degradation_check_payload(
    check: DegradationCheck,
    *,
    monitor: DegradationMonitor,
    baseline: Metrics,
    recent: Metrics,
    window: str,
) -> dict[str, Any]:
    """Build the legacy 1.0.0 payload; provenance can only come from a validated operation."""
    return _degradation_check_payload(
        check, monitor=monitor, baseline=baseline, recent=recent, window=window
    )


def degradation_operation_payload(result: DegradationOperationResult) -> dict[str, Any]:
    """The schema 1.1.0 report for a validated explicit operation result (ADR-0067)."""
    if not isinstance(result, DegradationOperationResult):
        raise ValueError("an evidence-bearing report needs a DegradationOperationResult")
    manifest = result.evidence.recent_observation_manifest
    if (
        manifest.content_hash() != result.evidence.recent_observation_set_hash
        or manifest.observation_set_id != result.evidence.recent_observation_set_id
        or manifest.method_id != result.evidence.metric_method_id
        or manifest.window != result.window
        or manifest.metrics != result.recent
        or manifest.subject.target_identity() != result.check.subject.target_identity()
        or result.evidence.window_start != result.window.start
        or result.evidence.window_end != result.window.end
    ):
        raise ValueError("the operation result's evidence is not bound to its check inputs")
    return _degradation_check_payload(
        result.check,
        monitor=result.monitor,
        baseline=result.baseline_map(),
        recent=result.recent_map(),
        window=result.window.label,
        evidence=result.evidence.as_mapping(),
    )


def write_degradation_check(
    root: Path,
    check: DegradationCheck,
    *,
    monitor: DegradationMonitor,
    baseline: Metrics,
    recent: Metrics,
    window: str,
) -> WrittenReport:
    """Write at ``<root>/degradation_check/<check_hash>.json`` (append-only)."""
    payload = degradation_check_payload(
        check, monitor=monitor, baseline=baseline, recent=recent, window=window
    )
    return write_report_file(root, KIND, payload["check_hash"], payload)


def write_degradation_operation(root: Path, result: DegradationOperationResult) -> WrittenReport:
    """Write an ADR-0067 operation result with hash-bound evidence and schema 1.1.0."""
    payload = degradation_operation_payload(result)
    return write_report_file(root, KIND, payload["check_hash"], payload)

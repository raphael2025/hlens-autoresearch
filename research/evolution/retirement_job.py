"""Explicit, caller-invoked strategy retirement (Phase 12; ADR-0045 / ADR-0086).

This job is deliberately outside every continuous loop. A human reviewer invokes it with the
strategy, one or more hash-bound degradation reports, and an explicitly opened
``RetirementRegistry``. It verifies each report against its ``DegradationCheck`` before asking
``retire`` to build the record and persisting it. It never makes an autonomous retirement choice.

The existing ``RetirementRecord`` has no reviewer field. The named reviewer is therefore retained
as a ``human_review:<name>`` evidence reference alongside every verified degradation report. This
records the declaration; it does not authenticate the person's identity or authority (ADR-0019).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from apps.worker.degradation import DegradationCheck
from core.domain.base import Ref, content_hash
from core.domain.research import RetirementRecord
from core.domain.specs import StrategySpec
from infrastructure.registry.retirement import RetirementRegistry
from research.evolution.operators import retire

__all__ = ["DegradationEvidence", "DegradationReportResolver", "retire_strategy"]

DegradationReportResolver = Callable[[str], Mapping[str, Any] | None]
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_AUTOMATION_IDENTITY: Final = re.compile(
    r"(?:^|[\s@._:/-])(?:agent|assistant|automated|automation|autonomous|bot|ci|claude|codex|copilot|daemon|gpt|llm|model|service|system)(?:$|[\s@._:/-])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DegradationEvidence:
    """A monitor result and the content identity of its persisted report."""

    check: DegradationCheck
    report_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.check, DegradationCheck):
            raise TypeError("degradation evidence needs a DegradationCheck")
        if not isinstance(self.report_hash, str) or _SHA256.fullmatch(self.report_hash) is None:
            raise ValueError("a degradation report hash must be a lowercase SHA-256")


def _reviewer_name(reviewer: str) -> str:
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("retirement requires a named human reviewer")
    name = reviewer.strip()
    if _AUTOMATION_IDENTITY.search(name):
        raise ValueError("an automated identity cannot review a retirement")
    return name


def _verify_report(item: DegradationEvidence, resolver: DegradationReportResolver) -> str | None:
    """Return a refusal reason, or ``None`` when the report binds this degraded check."""
    try:
        payload = resolver(item.report_hash)
    except Exception as exc:
        return f"report {item.report_hash} could not be resolved: {type(exc).__name__}: {exc}"
    if not isinstance(payload, Mapping):
        return f"report {item.report_hash} is missing or is not a report object"
    if payload.get("kind") != "degradation_check":
        return f"report {item.report_hash} is not a degradation_check"
    if payload.get("schema_version") not in {"1.0.0", "1.1.0"}:
        return f"report {item.report_hash} has an unsupported schema version"
    if payload.get("status") != "FRAMEWORK_IMPLEMENTED / NOT_VALIDATED":
        return f"report {item.report_hash} has an invalid status marker"
    if not isinstance(payload.get("window"), str) or not payload["window"].strip():
        return f"report {item.report_hash} does not name its observation window"
    metrics = payload.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        return f"report {item.report_hash} has no metric evidence"
    if any(not isinstance(metric, Mapping) for metric in metrics):
        return f"report {item.report_hash} has malformed metric evidence"
    names = tuple(metric.get("metric") for metric in metrics)
    if any(not isinstance(name, str) or not name for name in names):
        return f"report {item.report_hash} has malformed metric evidence"
    metric_names = set(names)
    claimed_hash = payload.get("check_hash")
    body = {key: value for key, value in payload.items() if key != "check_hash"}
    if claimed_hash != item.report_hash or content_hash(body) != item.report_hash:
        return f"report {item.report_hash} has a mismatched content hash"

    check = item.check
    if payload.get("subject") != str(check.subject):
        return f"report {item.report_hash} belongs to a different subject"
    if payload.get("breaches") != [dict(sorted(breach.items())) for breach in check.breaches]:
        return f"report {item.report_hash} does not match the supplied degradation breaches"
    if payload.get("missing") != list(check.missing):
        return f"report {item.report_hash} does not match the supplied missing metrics"
    if payload.get("degraded") is not check.degraded:
        return f"report {item.report_hash} does not match the supplied degradation status"
    if payload.get("insufficient_evidence", False) is not check.insufficient_evidence:
        return f"report {item.report_hash} does not match the supplied evidence sufficiency"
    if not check.degraded or check.insufficient_evidence:
        return f"report {item.report_hash} does not establish degradation"
    if any(breach.get("metric") not in metric_names for breach in check.breaches):
        return f"report {item.report_hash} is missing metric evidence for a reported breach"
    if any(metric not in metric_names for metric in check.missing):
        return f"report {item.report_hash} is missing a ruled metric"
    return None


def retire_strategy(
    *,
    subject: StrategySpec,
    reason: str,
    degradation_evidence: Sequence[DegradationEvidence],
    report_resolver: DegradationReportResolver,
    reviewer: str,
    registry: RetirementRegistry,
) -> RetirementRecord:
    """Verify and persist a single human-invoked retirement.

    Missing, incomplete, malformed, mismatched, or healthy degradation evidence is refused before
    ``retire`` or the registry is called. Duplicate subjects are refused by the append-only
    registry. A closed or corrupt registry propagates its own refusal unchanged.
    """
    if not isinstance(subject, StrategySpec):
        raise TypeError("retirement subject must be a StrategySpec")
    if not isinstance(registry, RetirementRegistry):
        raise TypeError("retirement requires a caller-provided RetirementRegistry")
    reviewer_name = _reviewer_name(reviewer)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("retirement requires a non-empty reason")
    if not degradation_evidence:
        raise ValueError("retirement requires at least one verified degradation report")
    refs: list[str] = []
    seen_hashes: set[str] = set()
    for item in degradation_evidence:
        if not isinstance(item, DegradationEvidence):
            raise TypeError("every degradation evidence item must include a check and report hash")
        if item.report_hash in seen_hashes:
            raise ValueError(f"degradation report {item.report_hash} was supplied more than once")
        seen_hashes.add(item.report_hash)
        if item.check.subject.target_identity() != subject.ref.target_identity():
            raise ValueError(f"degradation check {item.report_hash} belongs to a different subject")
        refusal = _verify_report(item, report_resolver)
        if refusal is not None:
            raise ValueError(refusal)
        refs.append(f"degradation_check:{item.report_hash}")

    record: RetirementRecord = retire(
        subject,
        reason.strip(),
        evidence=(*refs, f"human_review:{reviewer_name}"),
    )
    return registry.register_retirement(record)

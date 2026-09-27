"""Router eligibility bound to validation evidence (P10-ELIG, research level; paper only).

Code completion (2026-09-26, CODE_COMPLETE / DEBUG_PENDING; Claude, under Raphael's 2026-09-26
autonomous-decision instruction). No core / contract / Schema change.

In **trust mode** (the default) ``StrategyRouter`` takes the caller's lifecycle map at its word.
In **evidence mode** (``StrategyRouter(spec, lifecycle, evidence=EligibilityEvidence(...))``)
every strategy the spec can route to must also be backed by the actual ``ValidationReport``:

- ``report_hashes``: strategy ref -> the claimed report content hash (``ValidationReport
  .content_hash()``, the id ``research/reports/validation.py`` writes it under);
- ``reports``: either the report objects (strategy ref -> ``ValidationReport``) or a resolver
  ``report hash -> ValidationReport | None`` — ``report_store_resolver(root)`` reads
  ``<root>/validation_report/<hash>.json`` (the report store's file layout; ``apps`` is never
  imported);
- ``profiles``: the ``ValidationProfile`` objects the reports were produced under (ADR-0060 C-T4
  below needs the Profile's benchmark rule; a report cannot state it by itself).

``check_report`` then requires, per routed strategy, in this order (the first failure is the
refusal reason): a claimed hash (``report_hash_missing``), a report for it
(``report_not_found``), a well-formed ``ValidationReport`` (``report_invalid``), its content hash
equal to the claimed one (``report_hash_mismatch``), its ``subject`` equal to the routed strategy
ref (``subject_mismatch``), verdict PASS (``verdict_not_pass``) and at least one G5 (sealed OOS)
gate (``sealed_oos_not_evaluated``; ``research.validation.report.promotion_blocked_reason``),
every G5 gate PASS (``sealed_oos_not_passed``; implied by a PASS verdict for a validated report,
checked anyway), then the report's Profile among ``profiles`` (content hash = the report's
``validation_profile_hash``, same ref; ``profile_not_found``) and, when that Profile's
``benchmark.market_benchmark_rule`` is not ``none``, the ADR-0060 item it calls for in the report
(``G2.market_benchmark.<rule>`` for a registered rule, the bare ``G2.market_benchmark`` for an
unregistered one; ``market_benchmark_missing``) — the validator's ``ValidatorSetup
.market_benchmark`` stays opt-in (default ``False``), evidence mode does not accept a report made
without it. A PASS report including G5 is the research-level prerequisite of the routable
lifecycle states (in-sample + sealed OOS passed); the human / Control-Plane review steps that
make a strategy PRODUCTION_CANDIDATE or ACTIVE stay a caller claim — production eligibility
belongs to the Control Plane, not to this module. Nothing here has a threshold.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from core.contracts.validation_profile import ValidationProfile
from core.domain.base import SHA256_PATTERN, Ref
from core.domain.research import ValidationReport, Verdict
from research.validation.benchmark import MARKET_BENCHMARK_GATE, resolve_market_benchmark
from research.validation.report import (
    SEALED_OOS_NOT_EVALUATED,
    VERDICT_NOT_PASS,
    promotion_blocked_reason,
)

__all__ = [
    "SEALED_OOS_GATE_PREFIX",
    "VALIDATION_REPORT_KIND",
    "EligibilityCheck",
    "EligibilityEvidence",
    "EligibilityRefusal",
    "ReportResolver",
    "ReportUnreadable",
    "check_report",
    "report_store_resolver",
]

type EligibilityRefusal = Literal[
    "report_hash_missing",
    "report_not_found",
    "report_invalid",
    "report_hash_mismatch",
    "subject_mismatch",
    "verdict_not_pass",
    "sealed_oos_not_evaluated",
    "sealed_oos_not_passed",
    "profile_not_found",
    "market_benchmark_missing",
]
#: report content hash -> the report, or ``None`` when the source has no such report.
type ReportResolver = Callable[[str], ValidationReport | None]

#: Directory of validation reports under a report root; equals ``research.reports.validation
#: .KIND`` (not imported: ``research.reports`` imports this package; a test pins the equality).
VALIDATION_REPORT_KIND: Final = "validation_report"
#: A G5 (sealed OOS) gate id starts with this, as in ``research.validation.report``.
SEALED_OOS_GATE_PREFIX: Final = "G5."
_SHA256: Final = re.compile(SHA256_PATTERN)


class ReportUnreadable(ValueError):
    """A report source holds something for the hash that is not a well-formed report."""


@dataclass(frozen=True, slots=True)
class EligibilityEvidence:
    """What evidence mode verifies the lifecycle claims against (module docs)."""

    report_hashes: Mapping[Ref, str] | Mapping[str, str]
    reports: Mapping[Ref, ValidationReport] | Mapping[str, ValidationReport] | ReportResolver
    #: The Validation Profiles the reports were produced under (no default: absence is refused).
    profiles: Sequence[ValidationProfile]


@dataclass(frozen=True, slots=True)
class EligibilityCheck:
    """The outcome of checking one routed strategy's claimed eligibility against its report."""

    strategy: str
    #: the lifecycle state the caller claimed for it (its value)
    lifecycle: str
    #: the claimed report content hash (``None``: none was claimed)
    report_hash: str | None
    #: the report's ``subject`` ref, once a report was found
    subject: str | None
    #: the report's verdict, once a report was found
    verdict: str | None
    #: the report's G5 (sealed OOS) gate ids, once a report was found
    sealed_oos_gates: tuple[str, ...]
    #: ``None``: verified; otherwise why the claim is refused
    refusal: EligibilityRefusal | None
    detail: str

    @property
    def verified(self) -> bool:
        return self.refusal is None

    def to_dict(self) -> dict[str, object]:
        """JSON-ready record (hashed into run / stop hashes; written into report payloads)."""
        return {
            "strategy": self.strategy,
            "lifecycle": self.lifecycle,
            "report_hash": self.report_hash,
            "subject": self.subject,
            "verdict": self.verdict,
            "sealed_oos_gates": list(self.sealed_oos_gates),
            "refusal": self.refusal,
            "detail": self.detail,
        }


def report_store_resolver(root: Path) -> ReportResolver:
    """Resolve a report hash to ``<root>/validation_report/<hash>.json`` (``None``: no file).

    The file is parsed as a ``ValidationReport`` (the payload ``write_validation_report``
    writes); an unparsable file raises ``ReportUnreadable``. Whether its content really hashes to
    the file name is checked by ``check_report`` (``report_hash_mismatch``), not here.
    """
    directory = Path(root) / VALIDATION_REPORT_KIND

    def resolve(report_hash: str) -> ValidationReport | None:
        if not isinstance(report_hash, str) or _SHA256.fullmatch(report_hash) is None:
            return None
        path = directory / f"{report_hash}.json"
        if not path.is_file():
            return None
        try:
            return ValidationReport.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except ValueError as error:  # JSONDecodeError and pydantic's ValidationError
            raise ReportUnreadable(f"{path.name}: not a ValidationReport") from error

    return resolve


def _refused(
    strategy: str,
    lifecycle: str,
    report_hash: str | None,
    refusal: EligibilityRefusal,
    detail: str,
    report: ValidationReport | None = None,
) -> EligibilityCheck:
    return EligibilityCheck(
        strategy=strategy,
        lifecycle=lifecycle,
        report_hash=report_hash,
        subject=None if report is None else str(report.subject),
        verdict=None if report is None else report.verdict.value,
        sealed_oos_gates=() if report is None else _sealed_oos_gates(report),
        refusal=refusal,
        detail=detail,
    )


def _sealed_oos_gates(report: ValidationReport) -> tuple[str, ...]:
    return tuple(g.gate_id for g in report.gates if g.gate_id.startswith(SEALED_OOS_GATE_PREFIX))


def _market_benchmark_item(profile: ValidationProfile) -> str | None:
    """The ADR-0060 gate id ``profile``'s benchmark rule calls for (``None``: rule ``none``)."""
    name = profile.benchmark.market_benchmark_rule
    if name == "none":
        return None
    registered = resolve_market_benchmark(name) is not None
    return f"{MARKET_BENCHMARK_GATE}.{name}" if registered else MARKET_BENCHMARK_GATE


def check_report(
    strategy: str,
    lifecycle: str,
    report_hash: str | None,
    reports: Mapping[str, ValidationReport] | ReportResolver,
    *,
    profiles: Sequence[ValidationProfile],
) -> EligibilityCheck:
    """Check one routed strategy's claim (module docs; the first failing check is the refusal).

    ``reports`` is either keyed by strategy ref string or a resolver by report hash; ``profiles``
    are the Profiles the reports were produced under.
    """
    if report_hash is None:
        return _refused(strategy, lifecycle, None, "report_hash_missing", "no report hash claimed")
    try:
        report = reports.get(strategy) if isinstance(reports, Mapping) else reports(report_hash)
    except ReportUnreadable as error:
        return _refused(strategy, lifecycle, report_hash, "report_invalid", str(error))
    if report is None:
        return _refused(
            strategy, lifecycle, report_hash, "report_not_found", "no report for the claimed hash"
        )
    if not isinstance(report, ValidationReport):
        return _refused(
            strategy, lifecycle, report_hash, "report_invalid", "not a ValidationReport"
        )
    actual = report.content_hash()
    if actual != report_hash:
        return _refused(
            strategy,
            lifecycle,
            report_hash,
            "report_hash_mismatch",
            f"the report's content hash is {actual}",
            report,
        )
    if str(report.subject) != strategy:
        return _refused(
            strategy,
            lifecycle,
            report_hash,
            "subject_mismatch",
            f"the report's subject is {report.subject}",
            report,
        )
    blocked = promotion_blocked_reason(report)
    if blocked == VERDICT_NOT_PASS:
        return _refused(
            strategy,
            lifecycle,
            report_hash,
            "verdict_not_pass",
            f"the report's verdict is {report.verdict.value}",
            report,
        )
    if blocked == SEALED_OOS_NOT_EVALUATED:
        return _refused(
            strategy,
            lifecycle,
            report_hash,
            "sealed_oos_not_evaluated",
            "the report has no G5 (sealed OOS) gate",
            report,
        )
    failed = sorted(
        g.gate_id
        for g in report.gates
        if g.gate_id.startswith(SEALED_OOS_GATE_PREFIX) and g.verdict is not Verdict.PASS
    )
    if blocked is not None or failed:
        return _refused(
            strategy,
            lifecycle,
            report_hash,
            "sealed_oos_not_passed",
            f"G5 gates not PASS: {failed}" if failed else f"promotion blocked: {blocked}",
            report,
        )
    profile = next(
        (
            p
            for p in profiles
            if isinstance(p, ValidationProfile)
            and p.content_hash() == report.validation_profile_hash
            and p.ref.target_identity() == report.validation_profile.target_identity()
        ),
        None,
    )
    if profile is None:
        return _refused(
            strategy,
            lifecycle,
            report_hash,
            "profile_not_found",
            f"no given Profile is {report.validation_profile} / {report.validation_profile_hash}",
            report,
        )
    item = _market_benchmark_item(profile)
    if item is not None and not any(g.gate_id == item for g in report.gates):
        return _refused(
            strategy,
            lifecycle,
            report_hash,
            "market_benchmark_missing",
            f"benchmark.market_benchmark_rule={profile.benchmark.market_benchmark_rule} "
            f"calls for {item}, which the report lacks (ADR-0060)",
            report,
        )
    return EligibilityCheck(
        strategy=strategy,
        lifecycle=lifecycle,
        report_hash=report_hash,
        subject=str(report.subject),
        verdict=report.verdict.value,
        sealed_oos_gates=_sealed_oos_gates(report),
        refusal=None,
        detail="PASS including G5 (sealed OOS)",
    )

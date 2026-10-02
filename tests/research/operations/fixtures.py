"""TEST ONLY fixtures for the P11 operations tests (ADR-0105 §3 / §4): one complete explicit-mode
(caller-declared) degradation case written to disk — a frozen Profile in a temporary freeze
registry, a PASS baseline report, its run, the exported baseline set, an ACTIVE lifecycle history
and a recent-metric manifest. Every number is fabricated; nothing is evidence about a strategy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from core.contracts.validation_profile import LifecycleParams, ValidationProfile
from core.domain.base import Ref, canonical_json
from core.domain.research import ExperimentRun, GateResult, ValidationReport, Verdict
from core.lifecycle.strategy import LifecycleState
from infrastructure.registry import ProfileFreezeRegistry
from research.operations.baseline_export import export_baseline_set
from research.operations.degradation import (
    ObservationSource,
    ObservationWindow,
    RecentMetricManifest,
)
from tests.factories import experiment_run, strategy_ref, validation_report
from tests.promotion.fixtures import (
    PATH_TO_PRODUCTION_CANDIDATE,
    TOY_FREEZE_APPROVER,
    TOY_FREEZE_TIME,
    history,
    toy_calibration_report,
    toy_profile,
)

#: The one metric of the case: a registry metric (``G4.cost_stress.breakeven``), TEST ONLY numbers.
METRIC = "breakeven_cost_multiple"
GATE_ID = "G4.cost_stress.breakeven"
GATE_LABEL = "breakeven_cost_multiple[>=]"
WINDOW_START = "2026-02-01T00:00:00Z"
WINDOW_END = "2026-03-01T00:00:00Z"


@dataclass(frozen=True, slots=True)
class Case:
    """The paths (and objects) of one written case."""

    subject: Ref
    profile: ValidationProfile
    report: ValidationReport
    run: ExperimentRun
    profile_path: Path
    report_path: Path
    run_path: Path
    baseline_set_path: Path
    lifecycle_path: Path
    recent_path: Path
    freeze_registry: Path
    freeze_anchor: Path

    def cli_args(self, reports_root: Path) -> list[str]:
        """The explicit-mode degradation CLI arguments for this case."""
        return [
            "--subject", str(self.subject),
            "--profile", str(self.profile_path),
            "--baseline-report", str(self.report_path),
            "--baseline-set", str(self.baseline_set_path),
            "--lifecycle", str(self.lifecycle_path),
            "--recent-manifest", str(self.recent_path),
            "--window-start", WINDOW_START,
            "--window-end", WINDOW_END,
            "--window-label", "TEST ONLY window",
            "--freeze-registry", str(self.freeze_registry),
            "--freeze-anchor", str(self.freeze_anchor),
            "--reports-root", str(reports_root),
        ]  # fmt: skip


def write_json(path: Path, payload: object) -> Path:
    path.write_text(canonical_json(payload), encoding="utf-8")
    return path


def toy_gate(value: str = "3.5", *, gate_id: str = GATE_ID, label: str = GATE_LABEL) -> GateResult:
    return GateResult(
        gate_id=gate_id,
        metric=label,
        value=float(Decimal(value)),
        value_exact=Decimal(value),
        verdict=Verdict.PASS,
    )


def toy_report_and_run(
    profile: ValidationProfile, subject: Ref, *, gates: tuple[GateResult, ...] | None = None
) -> tuple[ValidationReport, ExperimentRun]:
    run = experiment_run(run_id="run-ops-1")
    report = validation_report(
        Verdict.PASS,
        report_id="rep-ops-1",
        run_id=run.run_id,
        subject=subject,
        experiment_hash=run.experiment_hash,
        validation_profile=profile.ref,
        validation_profile_hash=profile.content_hash(),
        gates=(toy_gate(),) if gates is None else gates,
    )
    return report, run


def write_case(root: Path, *, recent_value: str = "3.2", threshold: str = "0.5") -> Case:
    """Write a complete case under ``root`` (its own directory per case)."""
    root.mkdir(parents=True, exist_ok=True)
    subject = strategy_ref()
    report_bytes, report_hash = toy_calibration_report()
    thresholds = LifecycleParams.model_validate(
        {
            "paper_period": toy_profile().lifecycle.paper_period,
            "paper_acceptance_rule": "test-rule",
            "degradation_thresholds": {METRIC: float(Decimal(threshold))},
            "degradation_thresholds_exact": {METRIC: Decimal(threshold)},
        }
    )
    profile = toy_profile(calibration_report=report_hash, lifecycle=thresholds)
    freeze_registry, freeze_anchor = root / "freezes", root / "freezes.anchor.jsonl"
    with ProfileFreezeRegistry(freeze_registry, anchor=freeze_anchor) as freezes:
        freezes.register_freeze(
            profile, report_bytes, approved_by=TOY_FREEZE_APPROVER, approved_at=TOY_FREEZE_TIME
        )
    report, run = toy_report_and_run(profile, subject)
    baseline = export_baseline_set(report, run, {METRIC: GATE_ID})
    active = history(subject, (*PATH_TO_PRODUCTION_CANDIDATE, LifecycleState.ACTIVE))
    window = ObservationWindow(
        start=datetime(2026, 2, 1, tzinfo=UTC),
        end=datetime(2026, 3, 1, tzinfo=UTC),
        label="TEST ONLY window",
    )
    manifest = RecentMetricManifest(
        subject=subject,
        profile_ref=profile.ref,
        profile_hash=profile.content_hash(),
        window=window,
        observation_set_id="test-only-observations",
        method_id="test-only-method@1",
        sources=(
            ObservationSource(
                source_id="test-only-source",
                source_hash="a" * 64,
                event_time=datetime(2026, 2, 15, tzinfo=UTC),
                observed_time=datetime(2026, 2, 28, tzinfo=UTC),
            ),
        ),
        metrics={METRIC: Decimal(recent_value)},
    )
    return Case(
        subject=subject,
        profile=profile,
        report=report,
        run=run,
        profile_path=write_json(root / "profile.json", profile.model_dump(mode="json")),
        report_path=write_json(root / "report.json", report.model_dump(mode="json")),
        run_path=write_json(root / "run.json", run.model_dump(mode="json")),
        baseline_set_path=write_json(root / "baseline_set.json", baseline.payload()),
        lifecycle_path=write_json(root / "lifecycle.json", active.model_dump(mode="json")),
        recent_path=write_json(
            root / "recent.json",
            {"manifest": manifest.payload(), "manifest_hash": manifest.content_hash()},
        ),
        freeze_registry=freeze_registry,
        freeze_anchor=freeze_anchor,
    )


def read_json(path: Path) -> dict[str, object]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded

"""The explicit retirement job verifies human supplied, hash-bound degradation evidence."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from apps.worker.degradation import DegradationMonitor
from core.domain.base import FrozenMapping, Kind, Ref
from core.domain.specs import StrategySpec
from infrastructure.registry.registry import DuplicateRecord
from infrastructure.registry.retirement import RetirementRegistry
from research.evolution.retirement_job import DegradationEvidence, retire_strategy
from research.reports.degradation import degradation_check_payload

SUBJECT = Ref(kind=Kind.STRATEGY, name="tsmom", version="1.0.0")
BASELINE = {"sharpe": Decimal("1.0")}
RECENT = {"sharpe": Decimal("0.1")}


def _strategy() -> StrategySpec:
    return StrategySpec(
        name=SUBJECT.name,
        version=SUBJECT.version,
        signals=(Ref(kind=Kind.FEATURE, name="return", version="1.0.0"),),
        params=FrozenMapping[str, str | int | float | bool]({}),
        param_search_space=FrozenMapping[str, tuple[str | int | float | bool, ...]]({}),
    )


def _evidence() -> tuple[DegradationEvidence, dict[str, Any]]:
    monitor = DegradationMonitor({"sharpe": Decimal("0.5")}, source="profile#degradation")
    check = monitor.check(SUBJECT, BASELINE, RECENT)
    payload = degradation_check_payload(
        check, monitor=monitor, baseline=BASELINE, recent=RECENT, window="validation-window"
    )
    return DegradationEvidence(check, payload["check_hash"]), payload


def _invoke(
    registry: RetirementRegistry,
    evidence: Sequence[DegradationEvidence],
    resolver: Any,
    *,
    reviewer: str = "Raphael Example",
) -> Any:
    return retire_strategy(
        subject=_strategy(),
        reason="degradation evidence reviewed",
        degradation_evidence=evidence,
        report_resolver=resolver,
        reviewer=reviewer,
        registry=registry,
    )


def test_retirement_job_verifies_each_report_and_persists_the_human_review(tmp_path: Path) -> None:
    evidence, payload = _evidence()
    with RetirementRegistry(tmp_path / "retirements") as registry:
        record = _invoke(registry, (evidence,), lambda report_hash: payload)
        assert record.subject_ref == SUBJECT
        assert record.evidence == (
            f"degradation_check:{evidence.report_hash}",
            "human_review:Raphael Example",
        )
        assert registry.retirement_of(SUBJECT) == record


def test_retirement_job_refuses_missing_degradation_evidence(tmp_path: Path) -> None:
    with RetirementRegistry(tmp_path / "retirements") as registry:
        with pytest.raises(ValueError, match="at least one"):
            _invoke(registry, (), lambda _: None)
        assert len(registry) == 0


def test_retirement_job_refuses_missing_or_mismatched_report(tmp_path: Path) -> None:
    evidence, payload = _evidence()
    with RetirementRegistry(tmp_path / "retirements") as registry:
        with pytest.raises(ValueError, match="missing"):
            _invoke(registry, (evidence,), lambda _: None)
        altered = {**payload, "degraded": False}
        with pytest.raises(ValueError, match="mismatched content hash"):
            _invoke(registry, (evidence,), lambda _: altered)
        assert len(registry) == 0


@pytest.mark.parametrize("reviewer", ["", "   ", "Codex", "automation service"])
def test_retirement_job_requires_a_named_human_reviewer(tmp_path: Path, reviewer: str) -> None:
    evidence, payload = _evidence()
    with RetirementRegistry(tmp_path / "retirements") as registry:
        with pytest.raises(ValueError, match="reviewer|automated"):
            _invoke(registry, (evidence,), lambda _: payload, reviewer=reviewer)
        assert len(registry) == 0


def test_retirement_job_refuses_non_degraded_check(tmp_path: Path) -> None:
    monitor = DegradationMonitor({"sharpe": Decimal("0.5")}, source="profile#degradation")
    recent = {"sharpe": Decimal("1.0")}
    check = monitor.check(SUBJECT, BASELINE, recent)
    payload = degradation_check_payload(
        check, monitor=monitor, baseline=BASELINE, recent=recent, window="validation-window"
    )
    evidence = DegradationEvidence(check, payload["check_hash"])
    with RetirementRegistry(tmp_path / "retirements") as registry:
        with pytest.raises(ValueError, match="does not establish degradation"):
            _invoke(registry, (evidence,), lambda _: payload)
        assert len(registry) == 0


def test_retirement_job_refuses_duplicate_retirement(tmp_path: Path) -> None:
    evidence, payload = _evidence()
    with RetirementRegistry(tmp_path / "retirements") as registry:
        _invoke(registry, (evidence,), lambda _: payload)
        with pytest.raises(DuplicateRecord, match="already retired"):
            _invoke(registry, (evidence,), lambda _: payload)

"""Phase 14 framework: golden reruns and adapter conformance (ADR-0047)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from infrastructure.migration import compare_golden, record_golden, run_conformance
from infrastructure.migration.golden import GoldenError
from plugins.knowledge import LocalKnowledgeProvider
from tests.contract_suites import knowledge as knowledge_suite


def test_a_bit_identical_rerun_passes_and_a_drift_is_reported() -> None:
    golden = record_golden("exp", lambda: {"sharpe": Decimal("1.25"), "trades": Decimal(40)})
    same = compare_golden(
        golden, lambda: {"sharpe": Decimal("1.25"), "trades": Decimal(40)}, Decimal(0)
    )
    assert same.passed and same.bit_identical
    drift = compare_golden(
        golden, lambda: {"sharpe": Decimal("1.2501"), "trades": Decimal(40)}, Decimal(0)
    )
    assert not drift.passed and drift.differences == {
        "sharpe": (Decimal("1.25"), Decimal("1.2501"))
    }
    tolerated = compare_golden(
        golden, lambda: {"sharpe": Decimal("1.2501"), "trades": Decimal(40)}, Decimal("0.001")
    )
    assert tolerated.passed and not tolerated.bit_identical


def test_missing_and_extra_outputs_are_differences() -> None:
    golden = record_golden("exp", lambda: {"a": Decimal(1)})
    diff = compare_golden(golden, lambda: {"b": Decimal(1)}, Decimal(0))
    assert diff.differences == {"a": (Decimal(1), None), "b": (None, Decimal(1))}


@pytest.mark.parametrize("bad", [{"x": 1.0}, {"x": Decimal("NaN")}, {"": Decimal(1)}])
def test_outputs_must_be_finite_decimals(bad: dict[str, object]) -> None:
    with pytest.raises(GoldenError):
        record_golden("exp", lambda: bad)  # type: ignore[arg-type, return-value]


def test_a_conformance_run_reports_every_failure() -> None:
    report = run_conformance(
        "hlens_knowledge_local@1.0.0", LocalKnowledgeProvider, knowledge_suite.KNOWLEDGE_CHECKS
    )
    assert report.passed and len(report.checks) == len(knowledge_suite.KNOWLEDGE_CHECKS)

    def broken(_: object) -> None:
        raise AssertionError("nope")

    failed = run_conformance("x@1.0.0", LocalKnowledgeProvider, (broken,))
    assert not failed.passed and failed.failures == (("broken", "AssertionError: nope"),)

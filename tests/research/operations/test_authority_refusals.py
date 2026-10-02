"""ADR-0098 §4: the refusals ``resolve_degradation_inputs`` decides **before** it reads the source
— the pins' form, the catalog's evidence verifier, the lifecycle authority, the closed metric
registry (ADR-0105 D-P11-WINDOW included), the baseline report's gates, the evaluation time — each
with its named code and without running the declared pipeline.

The catalog is a stand-in that is never read (each refusal here comes before the first catalog
access; a read would fail the test, and the builder is not a ``DatasetBuilder``). The resolution
over a real v3 dataset, and the source refusals, are in ``test_authority_resolver.py``. Every
number is TEST ONLY.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from infrastructure.registry.lifecycle import LifecycleRegistry
from research.loop.dataset_source import DatasetCatalog
from research.operations.authority import (
    BASELINE_BINDING_MISMATCH,
    LIFECYCLE_NOT_ACTIVE,
    LIFECYCLE_UNAVAILABLE,
    METRIC_INPUTS_UNAVAILABLE,
    METRIC_REGISTRY_ID,
    METRIC_UNDEFINED,
    SOURCE_IDENTITY_MISMATCH,
    SOURCE_UNAVAILABLE,
)
from research.operations.degradation import DegradationOperationRefused, ObservationWindow
from tests import factories
from tests.research.operations import authority_fixtures as af
from tests.research.operations.authority_fixtures import WINDOW, ResolverCase, Source


class _NeverRead:
    """A catalog part the refusals below must never touch."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the catalog was read ({name}) before the refusal")


SOURCE = Source(
    catalog=DatasetCatalog(
        adapter=_NeverRead(),  # type: ignore[arg-type]
        storage=_NeverRead(),  # type: ignore[arg-type]
        builder=_NeverRead(),  # type: ignore[arg-type]
        evidence_verifier=_NeverRead(),  # type: ignore[arg-type]
    ),
    dataset_id="hlens.dataset.pit-selection@2.1.0." + "a" * 64,
    manifest_hash="b" * 64,
    dataset=factories.dataset_ref(),
)


@pytest.fixture
def case(tmp_path: Path) -> ResolverCase:
    return af.resolver_case(SOURCE, tmp_path / "case")


# ---- pins and catalog ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"dataset_id": "not a selection id"},
        {"dataset_id": 7},
        {"manifest_hash": "ABC"},
        {"manifest_hash": "B" * 64},  # not lowercase
        {"baseline_manifest_hash": "f" * 63},
    ],
)
def test_malformed_pins_are_an_identity_mismatch(case: ResolverCase, overrides: dict) -> None:  # type: ignore[type-arg]
    case.refused(SOURCE_IDENTITY_MISMATCH, **overrides)
    assert case.calls == []


def test_a_catalog_without_the_v3_evidence_verifier_is_unavailable(case: ResolverCase) -> None:
    plain = replace(SOURCE.catalog, evidence_verifier=None)
    assert "evidence verifier" in case.refused(SOURCE_UNAVAILABLE, catalog=plain)
    case.refused(SOURCE_UNAVAILABLE, catalog=object())


# ---- lifecycle ----------------------------------------------------------------------------


def test_a_subject_that_is_not_active_is_refused_before_anything_else(tmp_path: Path) -> None:
    paper = af.resolver_case(SOURCE, tmp_path / "paper", lifecycle_path=af.PATH_TO_ACTIVE[:5])
    assert "PAPER" in paper.refused(LIFECYCLE_NOT_ACTIVE)


def test_a_writer_lifecycle_instance_is_unavailable(case: ResolverCase) -> None:
    with LifecycleRegistry(case.registry) as writer:
        case.refused(LIFECYCLE_UNAVAILABLE, lifecycle=writer, head=writer.head)


# ---- the closed metric registry -----------------------------------------------------------


def test_an_unknown_ruled_metric_is_undefined(tmp_path: Path) -> None:
    other = af.resolver_case(
        SOURCE,
        tmp_path / "unknown",
        thresholds={"invented_sharpe": "0.5"},
        gate_ids={"invented_sharpe": af.GATE_ID},
    )
    message = other.refused(METRIC_UNDEFINED)
    assert METRIC_REGISTRY_ID in message and "'invented_sharpe'" in message


def test_a_sealed_gate_metric_is_undefined(tmp_path: Path) -> None:
    gate_id = "G5.sealed_oos.breakeven"
    other = af.resolver_case(
        SOURCE,
        tmp_path / "g5",
        gates=(af.gate("3.5", gate_id=gate_id),),
        gate_ids={af.METRIC: gate_id},
    )
    assert "sealed OOS gate" in other.refused(METRIC_UNDEFINED)


def test_a_research_window_metric_outside_the_research_window_cites_adr_0105(
    tmp_path: Path,
) -> None:
    gate_id, label = "G4.walk_forward.positive_fraction", "positive_window_fraction[>=]"
    other = af.resolver_case(
        SOURCE,
        tmp_path / "walk",
        thresholds={"positive_window_fraction": "0.1"},
        gates=(af.gate("0.75", gate_id=gate_id, label=label),),
        gate_ids={"positive_window_fraction": gate_id},
        lifecycle_start=datetime(2026, 1, 1, tzinfo=UTC),
    )
    recent = ObservationWindow(
        start=datetime(2026, 2, 1, tzinfo=UTC), end=datetime(2026, 3, 1, tzinfo=UTC), label="w"
    )
    message = other.refused(METRIC_UNDEFINED, window=recent, as_of=datetime(2026, 3, 2, tzinfo=UTC))
    assert "ADR-0105" in message and "D-P11-WINDOW" in message
    assert "'positive_window_fraction'" in message
    assert other.calls == []


def test_a_research_window_independent_metric_on_the_same_window_is_not_undefined(
    tmp_path: Path,
) -> None:
    """D-P11-WINDOW: a metric that does not depend on the research window passes the window
    rule on a recent window; the next authority (the baseline dataset, which the stand-in
    catalog cannot supply) is then reached."""
    other = af.resolver_case(
        SOURCE, tmp_path / "recent", lifecycle_start=datetime(2026, 1, 1, tzinfo=UTC)
    )
    recent = ObservationWindow(
        start=datetime(2026, 2, 1, tzinfo=UTC), end=datetime(2026, 3, 1, tzinfo=UTC), label="w"
    )
    message = other.refused(
        SOURCE_UNAVAILABLE, window=recent, as_of=datetime(2026, 3, 2, tzinfo=UTC)
    )
    assert "the baseline manifest does not load" in message


def test_a_validation_metric_without_its_binding_is_inputs_unavailable(tmp_path: Path) -> None:
    gate_id = "G2.breakeven_cost_multiple"
    other = af.resolver_case(
        SOURCE,
        tmp_path / "g2",
        gates=(af.gate("3.5", gate_id=gate_id),),
        gate_ids={af.METRIC: gate_id},
    )
    assert "WindowValidationBinding" in other.refused(METRIC_INPUTS_UNAVAILABLE)


def test_a_baseline_gate_missing_from_the_report_is_a_binding_mismatch(tmp_path: Path) -> None:
    other = af.resolver_case(
        SOURCE,
        tmp_path / "nogate",
        gates=(
            af.gate(
                "3.5", gate_id="G4.cost_stress.0", label="breakeven_cost_multiple_vs_stress[>=]"
            ),
        ),
        gate_ids={af.METRIC: af.GATE_ID},
    )
    other.refused(BASELINE_BINDING_MISMATCH)


# ---- the evaluation time and the inputs' types --------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"as_of": WINDOW.end - timedelta(seconds=1)},  # before the window end
        {"as_of": datetime(2023, 11, 15)},  # naive
        {"subject": "strategy:s_example@1.0.0"},  # not a Ref
        {"window": (WINDOW.start, WINDOW.end)},
        {"baseline": {"breakeven_cost_multiple": "3.5"}},
    ],
)
def test_invalid_inputs_are_refused_before_any_authority(
    case: ResolverCase,
    overrides: dict,  # type: ignore[type-arg]
) -> None:
    with pytest.raises(DegradationOperationRefused):
        case.resolve(**overrides)
    assert case.calls == []

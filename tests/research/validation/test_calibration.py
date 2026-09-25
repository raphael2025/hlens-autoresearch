"""Phase 4: the null-model calibration report skeleton on RandomWalkMarket (ADR-0037 / ADR-0042).

Only the framework is tested: the report counts false positives on pure noise and detections on
planted effects, deterministically. The rates here come from the TEST ONLY profile and a handful
of seeds; they are not calibration results and propose no Profile number.
"""

from __future__ import annotations

import json

from core.contracts.outcome import OutcomeEvent
from core.contracts.synthetic import SyntheticMarket
from plugins.synthetic import RandomWalkMarket
from research.outcomes import OutcomeTable
from research.validation import InSampleInput
from research.validation.calibration import (
    STATUS,
    MomentumSignStudy,
    NullCalibrationReport,
    run_null_calibration,
)
from tests.research.validation.fixtures import (
    LABEL_SPEC,
    MANIFEST,
    PROVIDER,
    TEST_ONLY_PROFILE,
    in_sample_input,
    market_spec,
    research_events,
)


def _build(
    market: SyntheticMarket, table: OutcomeTable, events: tuple[OutcomeEvent, ...]
) -> InSampleInput:
    return in_sample_input(
        table, MomentumSignStudy(market, events), seed=int(market.market_hash[:8], 16)
    )


def _report() -> NullCalibrationReport:
    specs = [market_spec(seed, minutes=1440) for seed in (21, 22, 23, 24)] + [
        market_spec(seed, strength="0.6", minutes=1440) for seed in (31, 32)
    ]
    return run_null_calibration(
        profile=TEST_ONLY_PROFILE,
        generator=RandomWalkMarket(),
        market_specs=specs,
        outcome_provider=PROVIDER,
        label_spec=LABEL_SPEC,
        manifest_content_hash=MANIFEST,
        events_for=research_events,
        build_input=_build,
    )


def test_the_report_counts_false_positives_and_detections() -> None:
    report = _report()
    assert report.status == STATUS
    assert "TBD" in report.note
    assert (report.null_trials, report.planted_trials) == (4, 2)
    assert report.false_positive_rate == report.false_positives / 4
    assert report.detection_rate == report.detections / 2
    assert report.detections == 2  # a strong planted effect is found
    assert report.profile == str(TEST_ONLY_PROFILE.ref)
    assert report.generator == RandomWalkMarket().descriptor.plugin_key


def test_the_report_is_deterministic_and_serializable() -> None:
    first, second = _report(), _report()
    assert first.to_json() == second.to_json()
    payload = json.loads(first.to_json())
    assert payload["status"] == STATUS
    assert len(payload["trials"]) == 6

"""Null-model calibration report — framework skeleton (ADR-0037 §6; Phase 4 two-step freeze).

Runs the in-sample pipeline (G0 – G3) on synthetic markets with known truth (ADR-0042) and counts
how often it says ``PASS``:

- on pure-noise markets (``truth == ()``) a PASS is a **false positive**;
- on markets with a planted effect a PASS is a **detection** (power).

This module only produces the report. It proposes no Profile number and freezes nothing: choosing
thresholds from these rates is the Step 2 freeze and needs its own ADR and approval (Constitution
unchanged; ADR-0007). Synthetic results never support a real-market conclusion (roadmap P9).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal

from core.contracts.outcome import (
    OutcomeEvent,
    OutcomeLabelSpec,
    OutcomeProvider,
    OutcomeRequest,
)
from core.contracts.synthetic import SyntheticMarket, SyntheticMarketProvider, SyntheticMarketSpec
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import Kind, Ref
from core.domain.research import Verdict, derive_verdict
from research.outcomes.sources import bars_from_synthetic
from research.outcomes.table import OutcomeTable, materialize
from research.validation.pipeline import InSampleInput, run_in_sample

__all__ = [
    "CalibrationTrial",
    "MomentumSignStudy",
    "NullCalibrationReport",
    "run_null_calibration",
    "spaced_events",
]

STATUS = "FRAMEWORK_ONLY_NOT_CALIBRATED"
NOTE = "Profile numbers remain TBD; this report proposes and freezes nothing (ADR-0037)."


def spaced_events(
    market: SyntheticMarket, every: timedelta, warmup: int
) -> tuple[OutcomeEvent, ...]:
    """One event at the end of every ``every``-long block, after ``warmup`` bars."""
    if every <= timedelta(0) or warmup < 1:
        raise ValueError("every must be positive and warmup >= 1")
    events: list[OutcomeEvent] = []
    first = market.bars[warmup - 1].interval_end
    t = first
    last = market.bars[-1].interval_end
    while t <= last:
        events.append(OutcomeEvent(event_key=f"e{len(events):06d}", event_time=t))
        t += every
    return tuple(events)


class MomentumSignStudy:
    """Side = sign of the last completed bar's return before the event (a causal toy signal)."""

    def __init__(self, market: SyntheticMarket, events: Sequence[OutcomeEvent]) -> None:
        closes = {bar.interval_end: (bar.open, bar.close) for bar in market.bars}
        self._sides: dict[str, int] = {}
        for event in events:
            bar = closes.get(event.event_time)
            if bar is None:
                self._sides[event.event_key] = 0
                continue
            opened, closed = bar
            self._sides[event.event_key] = (closed > opened) - (closed < opened)

    @property
    def signal_refs(self) -> tuple[Ref, ...]:
        return (Ref(kind=Kind.FEATURE, name="bar_log_return", version="1.0.0"),)

    def sides(self, event_keys: Sequence[str], label_values: Sequence[Decimal]) -> tuple[int, ...]:
        return tuple(self._sides.get(key, 0) for key in event_keys)


@dataclass(frozen=True)
class CalibrationTrial:
    seed: int
    market_hash: str
    planted: bool
    verdict: Verdict
    failing_gates: tuple[str, ...]
    inconclusive_gates: tuple[str, ...]


@dataclass(frozen=True)
class NullCalibrationReport:
    status: str
    note: str
    profile: str
    profile_hash: str
    generator: str
    outcome: str
    null_trials: int
    false_positives: int
    false_positive_rate: float | None
    planted_trials: int
    detections: int
    detection_rate: float | None
    trials: tuple[CalibrationTrial, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False, indent=2)


TrialBuilder = Callable[[SyntheticMarket, OutcomeTable, tuple[OutcomeEvent, ...]], InSampleInput]


def run_null_calibration(
    *,
    profile: ValidationProfile,
    generator: SyntheticMarketProvider,
    market_specs: Sequence[SyntheticMarketSpec],
    outcome_provider: OutcomeProvider,
    label_spec: OutcomeLabelSpec,
    manifest_content_hash: str,
    events_for: Callable[[SyntheticMarket], tuple[OutcomeEvent, ...]],
    build_input: TrialBuilder,
) -> NullCalibrationReport:
    """Run G0 – G3 on every market and report false-positive and detection rates."""
    if not market_specs:
        raise ValueError("calibration needs at least one market")
    trials: list[CalibrationTrial] = []
    for spec in market_specs:
        market = generator.generate(spec)
        bars = bars_from_synthetic(market)
        events = events_for(market)
        request = OutcomeRequest(
            label_spec=label_spec,
            manifest_content_hash=manifest_content_hash,
            price_cutoff=bars[-1].available_time,
            events=events,
            bars=bars,
        )
        table = materialize(outcome_provider, request)
        gates = run_in_sample(build_input(market, table, request.events))
        trials.append(
            CalibrationTrial(
                seed=spec.seed,
                market_hash=market.market_hash,
                planted=bool(market.truth),
                verdict=derive_verdict(gates),
                failing_gates=tuple(g.gate_id for g in gates if g.verdict is Verdict.FAIL),
                inconclusive_gates=tuple(
                    g.gate_id for g in gates if g.verdict is Verdict.INCONCLUSIVE
                ),
            )
        )
    null = [trial for trial in trials if not trial.planted]
    planted = [trial for trial in trials if trial.planted]
    false_positives = sum(trial.verdict is Verdict.PASS for trial in null)
    detections = sum(trial.verdict is Verdict.PASS for trial in planted)
    return NullCalibrationReport(
        status=STATUS,
        note=NOTE,
        profile=str(profile.ref),
        profile_hash=profile.content_hash(),
        generator=generator.descriptor.plugin_key,
        outcome=str(label_spec.outcome),
        null_trials=len(null),
        false_positives=false_positives,
        false_positive_rate=false_positives / len(null) if null else None,
        planted_trials=len(planted),
        detections=detections,
        detection_rate=detections / len(planted) if planted else None,
        trials=tuple(trials),
    )

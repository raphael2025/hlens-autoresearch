"""Shared fixtures for the Phase 4 validation tests.

!!! TEST ONLY !!!  ``TEST_ONLY_PROFILE`` holds arbitrary, **uncalibrated** numbers chosen to make
the smoke tests fast and readable. They are not a proposal, not a calibration result and must never
be used for research or copied into a real Validation Profile (Profile numbers remain TBD until the
Phase 4 Step 2 freeze, ADR-0007 / ADR-0037).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from core.contracts.cost_model import CostModelSpec
from core.contracts.outcome import (
    OutcomeEvent,
    OutcomeLabelSpec,
    OutcomeMethod,
    OutcomeRequest,
)
from core.contracts.synthetic import PlantedEffect, SyntheticMarket, SyntheticMarketSpec
from core.contracts.validation_profile import (
    BenchmarkParams,
    CostStressParams,
    DataSplitParams,
    SampleSizeParams,
    SignificanceParams,
    ValidationProfile,
    WalkForwardParams,
)
from core.domain.base import Kind, Ref
from core.domain.research import ExperimentRun, RunState
from core.domain.specs import OutcomeSpec
from plugins.outcomes import ForwardReturnOutcome
from plugins.synthetic import RandomWalkMarket
from research.outcomes import OutcomeTable, bars_from_synthetic, materialize
from research.validation import InSampleInput, ValidationContext
from research.validation.controls import SignalStudy
from tests import factories

T0 = datetime(2024, 1, 1, tzinfo=UTC)
BOUNDARY = datetime(2024, 1, 2, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HORIZON = 2 * MINUTE
MANIFEST = "7" * 64

#: TEST ONLY — arbitrary, uncalibrated numbers (see module docstring).
TEST_ONLY_PROFILE = factories.validation_profile(
    name="test_only_uncalibrated",
    data_split=DataSplitParams(
        research_window_start=date(2024, 1, 1),
        sealed_oos_boundary=date(2024, 1, 2),
        sealed_oos_length=timedelta(days=1),
        sealed_oos_max_extension=timedelta(0),
        embargo=timedelta(minutes=30),
        walk_forward=WalkForwardParams(
            train_window=timedelta(hours=6),
            test_window=timedelta(hours=2),
            step=timedelta(hours=2),
            min_positive_window_fraction=0.5,
            max_single_window_pnl_share=0.5,
        ),
    ),
    sample_size=SampleSizeParams(
        min_effective_trades_in_sample=50,
        min_effective_trades_out_of_sample=20,
        min_effective_trades_per_state=10,
        effective_sample_method="non-overlapping-greedy",
        min_regime_coverage="test-only",
    ),
    significance=SignificanceParams(
        multiple_testing_method="bonferroni",
        multiple_testing_threshold=0.01,
        overfitting_metric="not-implemented",
        overfitting_threshold=0.5,
        trial_count_scope="family",
    ),
    benchmark=BenchmarkParams(
        null_model="random-entry",
        null_model_simulations=100,
        null_model_percentile=90.0,
        market_benchmark_rule="test-only",
        inverse_control_reported=False,
    ),
    cost_stress=CostStressParams(
        cost_model=Ref(kind=Kind.COST_MODEL, name="cost_v1", version="1.0.0"),
        fill_assumption="next-bar-open",
        stress_multipliers=(2.0,),
        reported_only_multipliers=(3.0,),
        delay_stress_bars=1,
        min_breakeven_cost_multiple=1.5,
    ),
)

COST_MODEL = CostModelSpec(
    name="cost_v1",
    version="1.0.0",
    created_at=T0,
    fee_rate_per_side=Decimal("0.00005"),
    slippage_rate_per_side=Decimal("0.00005"),
)

OUTCOME_SPEC = OutcomeSpec(
    name="fwd_2m", version="1.0.0", created_at=T0, horizon=HORIZON, label_definition="2m fwd"
)
LABEL_SPEC = OutcomeLabelSpec.bind(OUTCOME_SPEC, OutcomeMethod.FORWARD_RETURN)
PROVIDER = ForwardReturnOutcome((LABEL_SPEC,))


def market_spec(seed: int, strength: str | None = None, minutes: int = 2880) -> SyntheticMarketSpec:
    effects = (
        (PlantedEffect(lag_minutes=1, strength=Decimal(strength)),) if strength is not None else ()
    )
    return SyntheticMarketSpec(
        name="calibration_market",
        version="1.0.0",
        symbol="SYN-USDT",
        start=T0,
        minutes=minutes,
        seed=seed,
        initial_price=Decimal(100),
        volatility=Decimal("0.001"),
        effects=effects,
    )


def research_events(market: SyntheticMarket) -> tuple[OutcomeEvent, ...]:
    """Events every horizon in the research day, with labels known before the boundary."""
    events: list[OutcomeEvent] = []
    t = market.bars[0].interval_end
    while t + HORIZON < BOUNDARY:
        events.append(OutcomeEvent(event_key=f"e{len(events):05d}", event_time=t))
        t += HORIZON
    return tuple(events)


def sealed_events(market: SyntheticMarket) -> tuple[OutcomeEvent, ...]:
    events: list[OutcomeEvent] = []
    t = BOUNDARY
    end = BOUNDARY + TEST_ONLY_PROFILE.data_split.sealed_oos_length
    while t + HORIZON <= end and t + HORIZON <= market.bars[-1].interval_end:
        events.append(OutcomeEvent(event_key=f"s{len(events):05d}", event_time=t))
        t += HORIZON
    return tuple(events)


def outcome_table(market: SyntheticMarket, events: Sequence[OutcomeEvent]) -> OutcomeTable:
    bars = bars_from_synthetic(market)
    request = OutcomeRequest(
        label_spec=LABEL_SPEC,
        manifest_content_hash=MANIFEST,
        price_cutoff=bars[-1].available_time,
        events=tuple(events),
        bars=bars,
    )
    return materialize(PROVIDER, request)


def bound_run(profile: ValidationProfile = TEST_ONLY_PROFILE) -> ExperimentRun:
    refs = {
        "hypothesis_ref": factories.hypothesis_ref(),
        "strategy_ref": factories.strategy_ref(),
        "risk_policy_ref": factories.risk_ref(),
    }
    repro = factories.repro_tuple(
        **refs,
        outcome_ref=LABEL_SPEC.outcome,
        cost_model_ref=COST_MODEL.ref,
        validation_profile=profile.ref,
        validation_profile_hash=profile.content_hash(),
        dependency_hashes={
            str(refs["hypothesis_ref"]): factories.HASH_A,
            str(refs["strategy_ref"]): factories.HASH_B,
            str(refs["risk_policy_ref"]): factories.HASH_C,
            str(LABEL_SPEC.outcome): LABEL_SPEC.outcome_spec_hash,
            str(COST_MODEL.ref): COST_MODEL.content_hash(),
        },
    )
    return factories.experiment_run(repro=repro, state=RunState.COMPLETED)


def context(
    profile: ValidationProfile = TEST_ONLY_PROFILE, run: ExperimentRun | None = None
) -> ValidationContext:
    use_run = bound_run(profile) if run is None else run
    metadata = factories.experiment_metadata(
        experiment_hash=use_run.experiment_hash,
        validation_profile=profile.ref,
        validation_profile_hash=profile.content_hash(),
    )
    return ValidationContext(
        report_id="rep-p4",
        subject=factories.strategy_ref(),
        run=use_run,
        metadata=metadata,
        profile=profile,
        cost_model=COST_MODEL,
        label_spec=LABEL_SPEC,
    )


def in_sample_input(
    table: OutcomeTable,
    study: SignalStudy,
    *,
    ctx: ValidationContext | None = None,
    reproduced_hash: str | None = None,
    seed: int = 11,
) -> InSampleInput:
    return InSampleInput(
        context=context() if ctx is None else ctx,
        outcomes=table,
        study=study,
        seed=seed,
        reproduce=lambda: table.result_hash if reproduced_hash is None else reproduced_hash,
        recorded_result_hash=table.result_hash,
    )


def generate(seed: int, strength: str | None = None) -> SyntheticMarket:
    return RandomWalkMarket().generate(market_spec(seed, strength))

"""Shared fixtures of the W2 loop e2e tests (ADR-0049 implementation note, 2026-09-25).

!!! TEST ONLY !!!  the Profile of ``loop_profile``, ``LOOP_TEST_ONLY_PARAMS``,
the budgets, cost units, decision grid, state-model parameters and strategy points below are
arbitrary, **uncalibrated** numbers chosen to keep the smoke tests small and fast. They are not a
proposal, not a calibration result and must never be used for research (Profile numbers remain
TBD). Synthetic results support no claim about real markets (roadmap Phase 9).

Calendar: each round ingests a new 3-day segment ``[as_of - 3 days, as_of)`` of a fresh seeded
random walk; round ``i`` covers days ``3i .. 3i + 3`` after ``T0``. The Profile's research window
starts at ``T0`` and its sealed OOS window starts after the last round's data (``boundary_day``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from apps.worker import LoopBudget, ResearchLoop
from core.contracts.cost_model import CostModelSpec
from core.contracts.outcome import OutcomeLabelSpec, OutcomeMethod
from core.contracts.synthetic import PlantedEffect, SyntheticMarketSpec
from core.contracts.validation_profile import (
    BenchmarkParams,
    CostStressParams,
    DataSplitParams,
    ParameterStabilityParams,
    SampleSizeParams,
    SignificanceParams,
    ValidationProfile,
    WalkForwardParams,
)
from core.domain.base import Kind, Ref
from core.domain.research import EvidenceLevel, KnowledgeItem
from core.domain.selection import ProfileSelection, ProfileSelectionKey
from core.domain.specs import OutcomeSpec
from infrastructure.event_bus import InMemoryEventBus
from plugins.backtest import BarBacktester
from plugins.features import BarLogReturnProvider
from plugins.llm import ScriptedLLMProvider
from plugins.outcomes import ForwardReturnOutcome
from plugins.states import TrendRangeProvider
from plugins.synthetic import RandomWalkMarket
from research.loop import (
    EvolutionPlan,
    LoopWiring,
    OosUnsealBudget,
    ResearchMemory,
    SyntheticLoopConfig,
    build_synthetic_loop,
)
from research.strategies.failure_registry import FailureRegistry
from research.strategies.library import library_entries
from research.strategies.time_series_momentum import TimeSeriesMomentumProvider
from research.validation import RobustnessParams
from tests import factories

T0 = datetime(2024, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
DAYS_PER_ROUND = 3
SYMBOL = "SYN-USDT"
FAMILY = "loop_tsmom"
#: A fake but well-formed full Git OID (TEST ONLY).
CODE_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def loop_profile(boundary_day: int = 10) -> ValidationProfile:
    """TEST ONLY — arbitrary, uncalibrated numbers (see module docstring)."""
    return factories.validation_profile(
        name="test_only_loop_uncalibrated",
        data_split=DataSplitParams(
            research_window_start=date(2024, 1, 1),
            sealed_oos_boundary=date(2024, 1, 1) + timedelta(days=boundary_day),
            sealed_oos_length=timedelta(days=1),
            sealed_oos_max_extension=timedelta(0),
            embargo=HOUR,
            walk_forward=WalkForwardParams(
                train_window=timedelta(hours=12),
                test_window=timedelta(hours=6),
                step=timedelta(hours=6),
                min_positive_window_fraction=0.5,
                max_single_window_pnl_share=0.6,
            ),
        ),
        sample_size=SampleSizeParams(
            min_effective_trades_in_sample=30,
            min_effective_trades_out_of_sample=10,
            min_effective_trades_per_state=10,
            effective_sample_method="overlap-clusters",
            min_regime_coverage="test-only",
        ),
        significance=SignificanceParams(
            multiple_testing_method="bonferroni",
            multiple_testing_threshold=0.05,
            overfitting_metric="pbo_cscv",
            overfitting_threshold=0.3,
            trial_count_scope="family",
        ),
        benchmark=BenchmarkParams(
            null_model="random-entry",
            null_model_simulations=200,
            null_model_percentile=90.0,
            market_benchmark_rule="test-only",
            inverse_control_reported=False,
        ),
        parameter_stability=ParameterStabilityParams(
            neighborhood_definition="adjacent_grid",
            min_neighborhood_performance_ratio=0.3,
            min_positive_neighbor_fraction=0.5,
            time_alignment_offsets=(MINUTE,),
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


#: TEST ONLY — explicit parameters for rules without a Profile field.
LOOP_TEST_ONLY_PARAMS = RobustnessParams(
    cscv_partitions=8,
    max_participation_rate=0.01,
    min_capacity=None,
    impact_coefficient=0.1,
    cross_asset_min_positive_fraction=None,
)
COST_MODEL = CostModelSpec(
    name="cost_v1",
    version="1.0.0",
    created_at=T0,
    fee_rate_per_side=Decimal("0.00005"),
    slippage_rate_per_side=Decimal("0.00005"),
)
LABEL_SPEC = OutcomeLabelSpec.bind(
    OutcomeSpec(
        name="fwd_1h", version="1.0.0", created_at=T0, horizon=HOUR, label_definition="1h fwd"
    ),
    OutcomeMethod.FORWARD_RETURN,
)
LOG_RETURN = BarLogReturnProvider.spec()
#: TEST ONLY state-model parameters (a model parameter, not a validation threshold).
TREND = TrendRangeProvider.spec(LOG_RETURN.ref, window=30, threshold="0.3")
SELECTION = ProfileSelection(
    selection_rule=Ref(kind=Kind.PROFILE_SELECTION_RULE, name="test_only_rule", version="1.0.0"),
    selection_rule_hash="f" * 64,
    key=ProfileSelectionKey(
        venue="synthetic", symbol="SYN_USDT", timeframe="1m", research_class="intraday"
    ),
)
#: TEST ONLY budget: generous except where a test narrows it.
TEST_ONLY_BUDGET = LoopBudget(
    max_trials_per_round=3,
    max_trials_total=10,
    max_llm_cost_units=Decimal(10),
    max_compute_seconds=Decimal(1000),
)
#: The planted effect: a 60-minute return autocorrelation a 60-bar TSMOM can capture.
PLANTED = (PlantedEffect(lag_minutes=60, strength=Decimal("0.5")),)


def knowledge(lookback: int) -> KnowledgeItem:
    return KnowledgeItem(
        name=f"k_tsmom_lookback_{lookback}",
        version="1.0.0",
        created_at=T0,
        source="test://synthetic",
        license="test-only",
        claim=f"a {lookback}-bar time-series momentum beats costs on minute bars",
        conditions=("strategy = tsmom_bars@1.0.0", f"param lookback = {lookback}"),
        evidence_level=EvidenceLevel.E0_ANECDOTE,
    )


def llm_output(index: int, lookback: int | None) -> dict[str, Any]:
    conditions = (
        [] if lookback is None else ["strategy = tsmom_bars@1.0.0", f"param lookback = {lookback}"]
    )
    return {
        "name": f"h_llm_{index}",
        "statement": f"LLM suggestion {index}",
        "expected_direction": "higher",
        "minimum_meaningful_effect": "declared before running",
        "conditions": conditions,
    }


def wiring(
    *,
    evolution: bool = True,
    oos_unseal: OosUnsealBudget | None = None,
) -> LoopWiring:
    tsmom = library_entries()[0].candidate()
    return LoopWiring(
        feature_provider=BarLogReturnProvider((LOG_RETURN,)),
        feature_spec=LOG_RETURN,
        feature_chunk_bars=15,
        state_provider=TrendRangeProvider((TREND,)),
        state_spec=TREND,
        decision_step=HOUR,
        decision_warmup=61 * MINUTE,
        strategies=(tsmom,),
        backtester=BarBacktester(),
        cost_model=COST_MODEL,
        initial_equity=Decimal(1_000_000),
        outcome_provider=ForwardReturnOutcome((LABEL_SPEC,)),
        label_spec=LABEL_SPEC,
        robustness=LOOP_TEST_ONLY_PARAMS,
        profile_selection=SELECTION,
        declared_research_class="intraday",
        code_commit=CODE_COMMIT,
        environment_lock="test-only-lock",
        evolution=EvolutionPlan(
            every_rounds=1,
            parents_per_round=1,
            compute_seconds=Decimal(1),
            provider_for=lambda spec: TimeSeriesMomentumProvider((spec,)),
            minimum_meaningful_effect="net mean return above costs (test only)",
        )
        if evolution
        else None,
        oos_unseal=oos_unseal,
        sealed_decision_step=None if oos_unseal is None else HOUR,
    )


def config(
    *,
    seed: int = 11,
    planted: bool = True,
    budget: LoopBudget = TEST_ONLY_BUDGET,
    lookbacks: tuple[int, ...] = (60, 240, 1440),
    loop_wiring: LoopWiring | None = None,
    profile: ValidationProfile | None = None,
    days_per_round: int = DAYS_PER_ROUND,
) -> SyntheticLoopConfig:
    return SyntheticLoopConfig(
        loop_id="synthetic_loop",
        seed=seed,
        epoch=T0 + timedelta(days=days_per_round),
        cadence=timedelta(days=days_per_round),
        budget=budget,
        market=SyntheticMarketSpec(
            name="loop_market",
            version="1.0.0",
            symbol=SYMBOL,
            start=T0,
            minutes=1,
            seed=0,
            initial_price=Decimal(100),
            volatility=Decimal("0.001"),
            effects=PLANTED if planted else (),
        ),
        minutes_per_round=days_per_round * 1440,
        compute_seconds_per_bar=Decimal("0.001"),
        wiring=loop_wiring or wiring(),
        family_id=FAMILY,
        knowledge=tuple(knowledge(lookback) for lookback in lookbacks),
        max_new_hypotheses_per_round=1,
        hypothesis_compute_seconds=Decimal("0.5"),
        compute_seconds_per_trial=Decimal(1),
        validation_compute_seconds=Decimal(5),
        state_compute_seconds=Decimal(1),
        profile=profile or loop_profile(),
        constitution_version="1.0.0",
        llm_prompt="propose one falsifiable hypothesis about minute returns",
        llm_cost_units_per_call=Decimal(1),
    )


def build(
    tmp: Path,
    cfg: SyntheticLoopConfig | None = None,
    *,
    llm_lookbacks: tuple[int | None, ...] = (240, 1440, None),
) -> tuple[ResearchLoop, ResearchMemory, InMemoryEventBus]:
    bus = InMemoryEventBus()
    memory = ResearchMemory(failures=FailureRegistry(tmp / "failures.jsonl"))
    llm = ScriptedLLMProvider(
        [llm_output(i, lookback) for i, lookback in enumerate(llm_lookbacks)], clock=lambda: T0
    )
    loop = build_synthetic_loop(
        cfg or config(), provider=RandomWalkMarket(), bus=bus, memory=memory, llm=llm
    )
    return loop, memory, bus


__all__ = [
    "CODE_COMMIT",
    "FAMILY",
    "SYMBOL",
    "T0",
    "TEST_ONLY_BUDGET",
    "build",
    "config",
    "knowledge",
    "loop_profile",
    "wiring",
]

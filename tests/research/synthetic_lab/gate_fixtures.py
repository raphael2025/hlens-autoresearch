"""Fixtures for the Phase 9 gate calibration harness tests.

!!! TEST ONLY !!!  ``LAX_TEST_ONLY_PROFILE`` and ``STRICT_TEST_ONLY_PROFILE`` are deliberately
extreme, **uncalibrated** candidates built to exercise the harness: the lax one lets almost
anything through, the strict one almost nothing. They are not a proposal, not a calibration result
and must never be used for research or copied into a real Validation Profile (D-09 TBD-1..5 are
frozen by Raphael after calibration). ``TEST_ONLY_PARAMS`` and ``TEST_ONLY_ALPHA`` are likewise
arbitrary TEST ONLY values.

The detector is the full G0 → G4 pipeline through ``PipelineBacktestValidator`` on a 60-bar TSMOM
(the first library entry) over the research window of a two-day ``RandomWalkMarket``. The
multi-instrument detector (``multi_detector``) runs the same candidate over one such market per
symbol through the Phase 8 multi-instrument path (``ValidatorSetup.instruments``), with the TEST
ONLY ``MULTI_TEST_ONLY_PARAMS``; ``multi_arms`` are TEST ONLY arms, not a calibration design.

ADR-0060 enforcement (2026-09-26): both Profiles name the registered C-T4 rule
``buy_and_hold_equal_weight`` with ``inverse_control_reported=True`` (the former placeholder
``"test-only"`` is unregistered, hence ``G2.market_benchmark`` INCONCLUSIVE), and both detector
setups set ``ValidatorSetup.market_benchmark=True`` (the detectors refuse False).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from core.contracts.cost_model import CostModelSpec
from core.contracts.outcome import OutcomeLabelSpec, OutcomeMethod
from core.contracts.strategy import BacktestCostModel, PriceBar
from core.contracts.synthetic import (
    PlantedEffect,
    SyntheticBar,
    SyntheticMarket,
    SyntheticMarketSpec,
)
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
from core.domain.research import RunState
from core.domain.specs import OutcomeSpec
from plugins.backtest import BarBacktester
from plugins.outcomes import ForwardReturnOutcome
from plugins.synthetic import RandomWalkMarket
from research.strategies.library import library_entries
from research.strategies.pipeline import EvaluationInputs, StrategyCandidate
from research.strategies.signals import bar_signals
from research.strategies.validation import TrialRunner, ValidatorSetup
from research.synthetic_lab.gate_calibration import (
    GateCalibrationSetup,
    MultiInstrumentArm,
    MultiInstrumentCalibrationSetup,
    MultiInstrumentValidatorDetector,
    SealedInputsFactory,
    StrategyValidatorDetector,
)
from research.validation import RobustnessParams, ValidationContext
from tests import factories

T0 = datetime(2024, 1, 1, tzinfo=UTC)
BOUNDARY = datetime(2024, 1, 3, tzinfo=UTC)
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
SYMBOL = "SYN-USDT"
CHOSEN = {"lookback": 60}

BASE_SPEC = SyntheticMarketSpec(
    name="gate_calibration_market",
    version="1.0.0",
    symbol=SYMBOL,
    start=T0,
    minutes=2 * 1440,  # the research window only; nothing at or after BOUNDARY is generated
    seed=0,
    initial_price=Decimal(100),
    volatility=Decimal("0.001"),
)
#: G5 mode: the research window plus the one-day sealed window of both TEST ONLY Profiles.
G5_BASE_SPEC = BASE_SPEC.model_copy(
    update={"name": "gate_calibration_market_g5", "minutes": 3 * 1440}
)
SEALED_END = BOUNDARY + timedelta(days=1)
STRONG = PlantedEffect(lag_minutes=60, strength=Decimal("0.5"))
WEAK = PlantedEffect(lag_minutes=60, strength=Decimal("0.2"))


def _split() -> DataSplitParams:
    return DataSplitParams(
        research_window_start=date(2024, 1, 1),
        sealed_oos_boundary=date(2024, 1, 3),
        sealed_oos_length=timedelta(days=1),
        sealed_oos_max_extension=timedelta(0),
        embargo=HOUR,
        walk_forward=WalkForwardParams(
            train_window=timedelta(hours=12),
            test_window=timedelta(hours=6),
            step=timedelta(hours=6),
            min_positive_window_fraction=0.0,
            max_single_window_pnl_share=1.0,
        ),
    )


def _cost_stress(min_breakeven: float, delay: int) -> CostStressParams:
    return CostStressParams(
        cost_model=Ref(kind=Kind.COST_MODEL, name="cost_v1", version="1.0.0"),
        fill_assumption="next-bar-open",
        stress_multipliers=(1.0,),
        delay_stress_bars=delay,
        min_breakeven_cost_multiple=min_breakeven,
    )


#: TEST ONLY — deliberately lax (see module docstring).
LAX_TEST_ONLY_PROFILE = factories.validation_profile(
    name="test_only_lax_uncalibrated",
    data_split=_split(),
    sample_size=SampleSizeParams(
        min_effective_trades_in_sample=1,
        min_effective_trades_out_of_sample=1,
        min_effective_trades_per_state=1,
        effective_sample_method="overlap-clusters",
        min_regime_coverage="test-only",
    ),
    significance=SignificanceParams(
        multiple_testing_method="bonferroni",
        # Not 1.0: the pipeline reads this one level both for G3 (adjusted p <= level) and for
        # the G1 negative controls (control p >= level), so a "lax" 1.0 fails every G1 control.
        multiple_testing_threshold=0.05,
        overfitting_metric="pbo_cscv",
        overfitting_threshold=1.0,
        trial_count_scope="family",
    ),
    benchmark=BenchmarkParams(
        null_model="random-entry",
        null_model_simulations=20,
        null_model_percentile=0.0,
        # ADR-0060 enforcement: a registered rule (the "test-only" placeholder is INCONCLUSIVE)
        market_benchmark_rule="buy_and_hold_equal_weight",
        inverse_control_reported=True,
    ),
    parameter_stability=ParameterStabilityParams(
        neighborhood_definition="adjacent_grid",
        min_neighborhood_performance_ratio=-1000.0,
        min_positive_neighbor_fraction=0.0,
        time_alignment_offsets=(MINUTE,),
    ),
    cost_stress=_cost_stress(1e-9, 1),
)

#: TEST ONLY — deliberately strict (see module docstring).
STRICT_TEST_ONLY_PROFILE = factories.validation_profile(
    name="test_only_strict_uncalibrated",
    data_split=_split().model_copy(
        update={
            "walk_forward": _split().walk_forward.model_copy(
                update={"min_positive_window_fraction": 1.0, "max_single_window_pnl_share": 0.3}
            )
        }
    ),
    sample_size=SampleSizeParams(
        min_effective_trades_in_sample=40,
        min_effective_trades_out_of_sample=20,
        min_effective_trades_per_state=15,
        effective_sample_method="overlap-clusters",
        min_regime_coverage="test-only",
    ),
    significance=SignificanceParams(
        multiple_testing_method="bonferroni",
        multiple_testing_threshold=0.0001,
        overfitting_metric="pbo_cscv",
        overfitting_threshold=0.05,
        trial_count_scope="family",
    ),
    benchmark=BenchmarkParams(
        null_model="random-entry",
        null_model_simulations=20,
        null_model_percentile=99.0,
        # ADR-0060 enforcement: a registered rule (the "test-only" placeholder is INCONCLUSIVE)
        market_benchmark_rule="buy_and_hold_equal_weight",
        inverse_control_reported=True,
    ),
    parameter_stability=ParameterStabilityParams(
        neighborhood_definition="adjacent_grid",
        min_neighborhood_performance_ratio=0.9,
        min_positive_neighbor_fraction=1.0,
        time_alignment_offsets=(MINUTE,),
    ),
    cost_stress=_cost_stress(5.0, 1),
)

#: TEST ONLY — explicit parameters for rules without a Profile field. ``min_capacity=0.0`` states
#: "no capacity requirement in this smoke test" explicitly: since the ADR-0041 review fix a missing
#: ``min_capacity`` is INCONCLUSIVE (it used to be silently skipped), and this harness exercises the
#: other gates' pass rates, not capacity.
TEST_ONLY_PARAMS = RobustnessParams(
    cscv_partitions=4,
    max_participation_rate=1.0,
    min_capacity=0.0,
    impact_coefficient=0.0,
    cross_asset_min_positive_fraction=None,
    max_undersampled_pnl_share=None,
)
#: TEST ONLY — the reported interval level (a reporting parameter).
TEST_ONLY_ALPHA = Decimal("0.05")

FEE, SLIPPAGE = Decimal("0.00001"), Decimal("0.00001")
COST_MODEL = CostModelSpec(
    name="cost_v1",
    version="1.0.0",
    created_at=T0,
    fee_rate_per_side=FEE,
    slippage_rate_per_side=SLIPPAGE,
)
BACKTEST_COSTS = BacktestCostModel(
    name="cost_v1", version="1.0.0", fee_rate=FEE, slippage_rate=SLIPPAGE
)
OUTCOME_SPEC = OutcomeSpec(
    name="fwd_1h", version="1.0.0", created_at=T0, horizon=HOUR, label_definition="1h fwd"
)
LABEL_SPEC = OutcomeLabelSpec.bind(OUTCOME_SPEC, OutcomeMethod.FORWARD_RETURN)


def candidate() -> StrategyCandidate:
    return library_entries()[0].candidate()


def _price_bars(bars: Sequence[SyntheticBar]) -> tuple[PriceBar, ...]:
    return tuple(
        PriceBar(
            instrument=SYMBOL,
            interval_start=bar.interval_start,
            interval_end=bar.interval_end,
            available_time=bar.interval_end,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
        )
        for bar in bars
    )


def inputs(market: SyntheticMarket) -> EvaluationInputs:
    bars = _price_bars([bar for bar in market.bars if bar.interval_end <= BOUNDARY])
    decisions: list[datetime] = []
    t = T0 + 61 * MINUTE
    while t + HOUR < BOUNDARY:  # every label ends before the sealed window
        decisions.append(t)
        t += HOUR
    return EvaluationInputs(
        instruments=(SYMBOL,),
        bars=bars,
        decision_times=tuple(decisions),
        knowledge_cutoff=BOUNDARY,
        cost_model=BACKTEST_COSTS,
        initial_equity=Decimal(1_000_000),
        signals=bar_signals(bars),
        params=CHOSEN,
    )


def sealed_inputs(
    research: EvaluationInputs, sealed_bars: tuple[SyntheticBar, ...]
) -> EvaluationInputs:
    """G5 inputs: research + released sealed bars, hourly decisions inside the sealed window."""
    bars = (*research.bars, *_price_bars(sealed_bars))
    last = sealed_bars[-1].interval_end if sealed_bars else BOUNDARY
    decisions: list[datetime] = []
    t = BOUNDARY + 61 * MINUTE
    while t + HOUR < last:  # every label ends inside the released sealed bars
        decisions.append(t)
        t += HOUR
    return replace(
        research,
        bars=bars,
        decision_times=tuple(decisions),
        knowledge_cutoff=last,
        signals=bar_signals(bars),
    )


def context(strategy: StrategyCandidate, profile: ValidationProfile) -> ValidationContext:
    spec = strategy.spec
    refs = {"hypothesis_ref": factories.hypothesis_ref(), "risk_policy_ref": None}
    repro = factories.repro_tuple(
        **refs,
        strategy_ref=spec.ref,
        outcome_ref=LABEL_SPEC.outcome,
        cost_model_ref=COST_MODEL.ref,
        validation_profile=profile.ref,
        validation_profile_hash=profile.content_hash(),
        dependency_hashes={
            str(refs["hypothesis_ref"]): factories.HASH_A,
            str(spec.ref): spec.content_hash(),
            str(LABEL_SPEC.outcome): LABEL_SPEC.outcome_spec_hash,
            str(COST_MODEL.ref): COST_MODEL.content_hash(),
        },
    )
    run = factories.experiment_run(repro=repro, state=RunState.COMPLETED)
    trials = 1
    for values in spec.param_search_space.values():
        trials *= len(values)
    metadata = factories.experiment_metadata(
        experiment_hash=run.experiment_hash,
        validation_profile=profile.ref,
        validation_profile_hash=profile.content_hash(),
        hypothesis_family_id=strategy.hypothesis_family_id,
        family_trial_count=trials,
    )
    return ValidationContext(
        report_id="rep-gate-calibration",
        subject=spec.ref,
        run=run,
        metadata=metadata,
        profile=profile,
        cost_model=COST_MODEL,
        label_spec=LABEL_SPEC,
    )


def detector(*, sealed_inputs_for: SealedInputsFactory | None = None) -> StrategyValidatorDetector:
    """``sealed_inputs_for`` (e.g. ``sealed_inputs``) enables ``detect_sealed`` (G5 mode)."""
    strategy = candidate()

    def setup_for(
        market: SyntheticMarket, profile: ValidationProfile, runner: TrialRunner
    ) -> ValidatorSetup:
        return ValidatorSetup(
            context=context(strategy, profile),
            outcome_provider=ForwardReturnOutcome((LABEL_SPEC,)),
            manifest_content_hash="7" * 64,
            instrument=SYMBOL,
            trials=runner,
            chosen_params=CHOSEN,
            seed=11,
            robustness=TEST_ONLY_PARAMS,
            state_of=lambda t: "am" if t.hour < 12 else "pm",
            bar_volume={(SYMBOL, bar.interval_start): bar.volume for bar in market.bars},
            declared_instruments=(SYMBOL,),
            market_benchmark=True,  # ADR-0060 enforced (the detector refuses False)
        )

    return StrategyValidatorDetector(
        name="tsmom60_full_pipeline",
        candidate=strategy,
        backtester=BarBacktester(),
        inputs_for=inputs,
        setup_for=setup_for,
        sealed_inputs_for=sealed_inputs_for,
    )


def setup(
    seeds: int,
    *,
    candidates: tuple[ValidationProfile, ...] = (LAX_TEST_ONLY_PROFILE, STRICT_TEST_ONLY_PROFILE),
    planted: tuple[PlantedEffect, ...] = (STRONG, WEAK),
    sealed_oos_g5: bool = False,
) -> GateCalibrationSetup:
    """``sealed_oos_g5=True``: G5 mode over ``G5_BASE_SPEC`` (research + sealed window)."""
    if not sealed_oos_g5:  # exactly the pre-G5 setup (its report hashes are pinned)
        return GateCalibrationSetup(
            provider=RandomWalkMarket(),
            base=BASE_SPEC,
            detector=detector(),
            candidates=candidates,
            noise_seeds=tuple(range(seeds)),
            planted=planted,
            planted_seeds=tuple(range(100, 100 + seeds)),
            alpha=TEST_ONLY_ALPHA,
        )
    return GateCalibrationSetup(
        provider=RandomWalkMarket(),
        base=G5_BASE_SPEC,
        detector=detector(sealed_inputs_for=sealed_inputs),
        candidates=candidates,
        noise_seeds=tuple(range(seeds)),
        planted=planted,
        planted_seeds=tuple(range(100, 100 + seeds)),
        alpha=TEST_ONLY_ALPHA,
        sealed_oos_g5=True,
    )


def cli_setup() -> GateCalibrationSetup:
    """The ``--setup`` factory the CLI test loads (one seed, one planted arm, lax only)."""
    return setup(1, candidates=(LAX_TEST_ONLY_PROFILE,), planted=(STRONG,))


# ======================================================================================
# Multi-instrument mode (TEST ONLY; mirrors tests/research/strategies/
# test_multi_instrument_validation.py)
# ======================================================================================

MULTI_SYMBOLS = ("S0-USDT", "S1-USDT")
#: TEST ONLY — the lax parameters with an explicit cross-asset fraction (otherwise the C-R3
#: consistency gate is ``profile_field_missing`` and no multi-instrument run can PASS).
MULTI_TEST_ONLY_PARAMS = replace(TEST_ONLY_PARAMS, cross_asset_min_positive_fraction=0.5)


def _instrument_bars(symbol: str, bars: Sequence[SyntheticBar]) -> tuple[PriceBar, ...]:
    return tuple(
        PriceBar(
            instrument=symbol,
            interval_start=bar.interval_start,
            interval_end=bar.interval_end,
            available_time=bar.interval_end,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
        )
        for bar in bars
        if bar.interval_end <= BOUNDARY
    )


def multi_inputs(markets: Mapping[str, SyntheticMarket]) -> EvaluationInputs:
    """Research-window inputs over every instrument of the book (hourly decisions)."""
    names = tuple(sorted(markets))
    bars = tuple(bar for name in names for bar in _instrument_bars(name, markets[name].bars))
    return replace(
        inputs(next(iter(markets.values()))),
        instruments=names,
        bars=bars,
        signals=bar_signals(bars),
    )


def multi_detector() -> MultiInstrumentValidatorDetector:
    strategy = candidate()

    def setup_for(
        markets: Mapping[str, SyntheticMarket], profile: ValidationProfile, runner: TrialRunner
    ) -> ValidatorSetup:
        names = tuple(sorted(markets))
        return ValidatorSetup(
            context=context(strategy, profile),
            outcome_provider=ForwardReturnOutcome((LABEL_SPEC,)),
            manifest_content_hash="7" * 64,
            instrument=names[0],
            trials=runner,
            chosen_params=CHOSEN,
            seed=11,
            robustness=MULTI_TEST_ONLY_PARAMS,
            state_of=lambda t: "am" if t.hour < 12 else "pm",
            bar_volume={
                (name, bar.interval_start): bar.volume
                for name, market in markets.items()
                for bar in market.bars
            },
            declared_instruments=names,
            instruments=names,
            market_benchmark=True,  # ADR-0060 enforced (the detector refuses False)
        )

    return MultiInstrumentValidatorDetector(
        name="tsmom60_multi_instrument_pipeline",
        candidate=strategy,
        backtester=BarBacktester(),
        inputs_for=multi_inputs,
        setup_for=setup_for,
    )


def multi_arms(
    seeds: int, *, symbols: tuple[str, ...] = MULTI_SYMBOLS
) -> tuple[MultiInstrumentArm, ...]:
    """TEST ONLY arms: all noise, all planted (``STRONG``), mixed (first planted, rest noise)."""
    k = len(symbols)
    return (
        MultiInstrumentArm("all_noise", "all_noise", (None,) * k, tuple(range(seeds))),
        MultiInstrumentArm(
            "all_planted", "all_planted", (STRONG,) * k, tuple(range(100, 100 + seeds))
        ),
        MultiInstrumentArm(
            "mixed", "mixed", (STRONG, *(None,) * (k - 1)), tuple(range(200, 200 + seeds))
        ),
    )


def multi_setup(
    seeds: int,
    *,
    symbols: tuple[str, ...] = MULTI_SYMBOLS,
    candidates: tuple[ValidationProfile, ...] = (LAX_TEST_ONLY_PROFILE,),
    arms: tuple[MultiInstrumentArm, ...] | None = None,
) -> MultiInstrumentCalibrationSetup:
    return MultiInstrumentCalibrationSetup(
        provider=RandomWalkMarket(),
        base=BASE_SPEC,
        detector=multi_detector(),
        candidates=candidates,
        symbols=symbols,
        arms=multi_arms(seeds, symbols=symbols) if arms is None else arms,
        alpha=TEST_ONLY_ALPHA,
    )


def multi_cli_setup() -> MultiInstrumentCalibrationSetup:
    """The ``--setup`` factory of the multi-instrument CLI test (one seed per arm, lax only)."""
    return multi_setup(1)

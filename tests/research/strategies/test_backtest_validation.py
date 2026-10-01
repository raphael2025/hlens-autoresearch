"""End to end: synthetic market → TSMOM → BarBacktester → PipelineBacktestValidator → report.

Phase 5 wiring of the Phase 4 gates (G0 – G3) and the Phase 8 robustness gates (G4), ADR-0041.

!!! TEST ONLY !!!  ``E2E_TEST_ONLY_PROFILE`` and ``E2E_TEST_ONLY_PARAMS`` hold arbitrary,
**uncalibrated** numbers chosen to make this smoke test readable; they are not a proposal, not a
calibration result and must never be used for research (Profile numbers remain TBD).

The market is ``plugins/synthetic`` ``RandomWalkMarket`` with a planted 60-minute return
autocorrelation (a trailing-hour trend that persists into the next hour, which a 60-bar TSMOM can
capture) or pure noise. Only the research window (before the sealed boundary) is ever simulated.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from core.contracts.cost_model import CostModelSpec
from core.contracts.outcome import OutcomeLabelSpec, OutcomeMethod
from core.contracts.strategy import BacktestCostModel, BacktestProvider, BacktestResult, PriceBar
from core.contracts.synthetic import PlantedEffect, SyntheticMarket, SyntheticMarketSpec
from core.contracts.validation_profile import (
    BenchmarkParams,
    CostStressParams,
    DataSplitParams,
    ParameterStabilityParams,
    SampleSizeParams,
    SignificanceParams,
    WalkForwardParams,
)
from core.domain.base import CONTRACT_SCHEMA_VERSION, Kind, Ref
from core.domain.research import GateResult, RunState, Verdict, derive_verdict
from core.domain.specs import OutcomeSpec
from core.errors import ReasonCode
from infrastructure.bars import DatasetPriceBars, ManifestPair, pair_hash_of
from plugins.backtest import BarBacktester, ExecutionModel
from plugins.outcomes import ForwardReturnOutcome
from plugins.synthetic import RandomWalkMarket
from research.strategies.failure_registry import FailureRegistry
from research.strategies.library import library_entries
from research.strategies.pipeline import (
    CandidateTrialRunner,
    EvaluationInputs,
    EvaluationStatus,
    StrategyCandidate,
    StrategyEvaluation,
    evaluate_strategy,
)
from research.strategies.signals import bar_signals
from research.strategies.validation import (
    DIAGNOSTIC_MODE,
    PRICE_BINDING_DATASET,
    PRICE_BINDING_SYNTHETIC,
    PipelineBacktestValidator,
    TrialRunner,
    ValidatorSetup,
    binding_mismatches,
)
from research.validation import RobustnessParams, ValidationContext, to_json
from research.validation.g4 import run_robustness
from research.validation.robustness import RobustnessCheck
from tests import factories
from tests.contract_version_support import PINNED_CONTRACT_VERSION, at_contract_version

T0 = datetime(2024, 1, 1, tzinfo=UTC)
BOUNDARY = datetime(2024, 1, 5, tzinfo=UTC)
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
SYMBOL = "SYN-USDT"

#: TEST ONLY — arbitrary, uncalibrated numbers (see module docstring).
E2E_TEST_ONLY_PROFILE = factories.validation_profile(
    name="test_only_e2e_uncalibrated",
    data_split=DataSplitParams(
        research_window_start=date(2024, 1, 1),
        sealed_oos_boundary=date(2024, 1, 5),
        sealed_oos_length=timedelta(days=1),
        sealed_oos_max_extension=timedelta(0),
        embargo=HOUR,
        walk_forward=WalkForwardParams(
            train_window=timedelta(days=1),
            test_window=timedelta(hours=12),
            step=timedelta(hours=12),
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
        # ADR-0060 enforcement: a registered rule (the "test-only" placeholder is INCONCLUSIVE)
        market_benchmark_rule="buy_and_hold_equal_weight",
        inverse_control_reported=True,
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
E2E_TEST_ONLY_PARAMS = RobustnessParams(
    cscv_partitions=8,
    max_participation_rate=0.01,
    min_capacity=None,
    impact_coefficient=0.1,
    cross_asset_min_positive_fraction=None,
    max_undersampled_pnl_share=None,
)
FEE, SLIPPAGE = Decimal("0.00005"), Decimal("0.00005")
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
CHOSEN = {"lookback": 60}


def _market(seed: int, planted: bool) -> SyntheticMarket:
    effects = (PlantedEffect(lag_minutes=60, strength=Decimal("0.5")),) if planted else ()
    return RandomWalkMarket().generate(
        SyntheticMarketSpec(
            name="e2e_market",
            version="1.0.0",
            symbol=SYMBOL,
            start=T0,
            minutes=5 * 1440,
            seed=seed,
            initial_price=Decimal(100),
            volatility=Decimal("0.001"),
            effects=effects,
        )
    )


def _bars(market: SyntheticMarket, *, with_volume: bool = False) -> tuple[PriceBar, ...]:
    """Research-window bars only: nothing at or after the sealed boundary is simulated.

    ``with_volume``: the bars as ``backtest_bars_from_dataset`` produces them since B61 (each
    carries its volume); the synthetic path keeps the volume-less bars (its hashes unchanged).
    """
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
            volume=bar.volume if with_volume else None,
        )
        for bar in market.bars
        if bar.interval_end <= BOUNDARY
    )


def _inputs(market: SyntheticMarket, *, with_volume: bool = False) -> EvaluationInputs:
    bars = _bars(market, with_volume=with_volume)
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


def _context(candidate: StrategyCandidate) -> ValidationContext:
    spec, profile = candidate.spec, E2E_TEST_ONLY_PROFILE
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
    metadata = factories.experiment_metadata(
        experiment_hash=run.experiment_hash,
        validation_profile=profile.ref,
        validation_profile_hash=profile.content_hash(),
        hypothesis_family_id=candidate.hypothesis_family_id,
        family_trial_count=len(spec.param_search_space["lookback"])
        * len(spec.param_search_space["long_only"]),
    )
    return ValidationContext(
        report_id="rep-e2e",
        subject=spec.ref,
        run=run,
        metadata=metadata,
        profile=profile,
        cost_model=COST_MODEL,
        label_spec=LABEL_SPEC,
    )


def _setup(
    market: SyntheticMarket,
    candidate: StrategyCandidate,
    *,
    context: ValidationContext | None = None,
    manifest: str = "7" * 64,
    dataset_bars: DatasetPriceBars | None = None,
    manifest_pair: ManifestPair | None = None,
    feature_manifest_hashes: tuple[str, ...] = (),
    trials: TrialRunner | None = None,
    robustness: RobustnessParams = E2E_TEST_ONLY_PARAMS,
    backtester: BacktestProvider | None = None,
    execution: ExecutionModel | None = None,
    bar_volume: dict[tuple[str, datetime], Decimal] | None = None,
) -> ValidatorSetup:
    return ValidatorSetup(
        context=context or _context(candidate),
        outcome_provider=ForwardReturnOutcome((LABEL_SPEC,)),
        manifest_content_hash=manifest,
        dataset_bars=dataset_bars,
        manifest_pair=manifest_pair,
        feature_manifest_hashes=feature_manifest_hashes,
        instrument=SYMBOL,
        # the dataset path trades on the proven bars themselves (which carry volume since B61)
        trials=trials
        or CandidateTrialRunner(
            candidate, _inputs(market, with_volume=dataset_bars is not None), BarBacktester()
        ),
        chosen_params=CHOSEN,
        seed=11,
        robustness=robustness,
        state_of=lambda t: "am" if t.hour < 12 else "pm",
        bar_volume=_volumes_of(market) if bar_volume is None else bar_volume,
        declared_instruments=(SYMBOL,),
        backtester=backtester,
        execution=execution,
    )


def _evaluate(
    market: SyntheticMarket,
    tmp_path: Path,
    *,
    run_backtester: BacktestProvider | None = None,
    **setup: object,
) -> tuple[StrategyEvaluation, FailureRegistry]:
    candidate = library_entries()[0].candidate()
    registry = FailureRegistry(tmp_path / "failures.jsonl")
    result = evaluate_strategy(
        candidate,
        _inputs(market, with_volume=setup.get("dataset_bars") is not None),
        backtester=run_backtester or BarBacktester(),
        registry=registry,
        validator=PipelineBacktestValidator(_setup(market, candidate, **setup)),  # type: ignore[arg-type]
    )
    return result, registry


_STATUS = {
    Verdict.PASS: EvaluationStatus.PASSED,
    Verdict.INCONCLUSIVE: EvaluationStatus.INCONCLUSIVE,
    Verdict.FAIL: EvaluationStatus.REJECTED,
}


@pytest.fixture(scope="module")
def planted(tmp_path_factory: pytest.TempPathFactory) -> tuple[StrategyEvaluation, FailureRegistry]:
    return _evaluate(_market(seed=7, planted=True), tmp_path_factory.mktemp("planted"))


def test_end_to_end_report_covers_g0_to_g4(
    planted: tuple[StrategyEvaluation, FailureRegistry],
) -> None:
    result, registry = planted
    assert result.validation is not None and result.backtest is not None
    report = result.validation.report
    assert report.subject == library_entries()[0].spec.ref
    assert report.verdict is derive_verdict(report.gates)
    assert result.status is _STATUS[report.verdict]
    stages = {gate.gate_id.split(".")[0] for gate in report.gates}
    assert stages == {"G0", "G1", "G2", "G3", "G4"}, sorted(g.gate_id for g in report.gates)
    by_id = {gate.gate_id: gate for gate in report.gates}
    assert by_id["G0.reproducibility"].verdict is Verdict.PASS
    assert by_id["G0.backtest_cost_model"].verdict is Verdict.PASS
    assert by_id["G1.label_blind_sides"].verdict is Verdict.PASS
    assert by_id["G4.overfitting"].threshold_source == "significance.overfitting_threshold"
    assert "G4.delay_stress" in by_id and "G4.time_alignment.0" in by_id
    if report.verdict is Verdict.FAIL:
        (record,) = registry.records()
        assert record.gate_id == next(g.gate_id for g in report.gates if g.verdict is Verdict.FAIL)
    else:
        assert registry.records() == ()


def test_end_to_end_view_is_serializable(
    planted: tuple[StrategyEvaluation, FailureRegistry],
) -> None:
    result, _ = planted
    assert result.validation is not None and result.backtest is not None
    view = result.validation.view
    assert view is not None
    loaded = json.loads(to_json(view))
    assert loaded["extra"]["backtest_result_hash"] == result.backtest.result_hash
    checks = {c["check_id"]: c for c in loaded["robustness"]["checks"]}
    assert len(checks["parameter_neighborhood"]["details"]["neighbors"]) == 2
    assert checks["overfitting"]["details"]["trials_evaluated"] == 6  # the declared space
    assert checks["state_decomposition"]["details"]["states"]
    assert checks["capacity"]["details"]["capacity"] > 0
    assert checks["cross_asset"]["status"] == "PASS"


def test_pure_noise_is_not_passed(tmp_path: Path) -> None:
    result, _ = _evaluate(_market(seed=5, planted=False), tmp_path)
    assert result.status in {EvaluationStatus.REJECTED, EvaluationStatus.INCONCLUSIVE}
    assert result.validation is not None
    assert result.validation.report.verdict is not Verdict.PASS


def test_a_cost_model_mismatch_fails_g0(tmp_path: Path) -> None:
    market = _market(seed=7, planted=True)
    candidate = library_entries()[0].candidate()
    other = COST_MODEL.model_copy(update={"fee_rate_per_side": Decimal("0.001")})
    ctx = _context(candidate)
    ctx = ValidationContext(**{**ctx.__dict__, "cost_model": other})
    result, registry = _evaluate(market, tmp_path, context=ctx)
    assert result.status is EvaluationStatus.REJECTED
    (record,) = registry.records()
    assert (record.gate_id, record.reason_code) == (
        "G0.backtest_cost_model",
        ReasonCode.CONTRACT_VIOLATION,
    )


class _OtherBacktestValidator(PipelineBacktestValidator):
    """Hands the validator a genuine backtest of another parameter point (not reproducible)."""

    def __init__(self, setup: ValidatorSetup) -> None:
        super().__init__(setup)
        self._other = setup.trials.run({"lookback": 240}).backtest

    def validate(self, subject, spec, backtest):  # type: ignore[no-untyped-def]
        return super().validate(subject, spec, self._other)


def test_a_non_reproducible_backtest_is_failed_not_rejected(tmp_path: Path) -> None:
    market = _market(seed=7, planted=True)
    candidate = library_entries()[0].candidate()
    registry = FailureRegistry(tmp_path / "failures.jsonl")
    result = evaluate_strategy(
        candidate,
        _inputs(market),
        backtester=BarBacktester(),
        registry=registry,
        validator=_OtherBacktestValidator(_setup(market, candidate)),
    )
    assert result.status is EvaluationStatus.FAILED
    (record,) = registry.records()
    assert (record.terminal_state, record.reason_code) == ("FAILED", ReasonCode.NOT_REPRODUCIBLE)


def test_the_trial_runner_delays_and_restricts_trials() -> None:
    market = _market(seed=7, planted=True)
    runner = CandidateTrialRunner(
        library_entries()[0].candidate(), _inputs(market), BarBacktester()
    )
    base = runner.run(CHOSEN)
    late = runner.run(CHOSEN, delay_bars=1)
    by_time = {t.decision_time: t.target_weight for t in base.targets}
    assert late.targets and all(
        by_time[t.decision_time - MINUTE] == t.target_weight for t in late.targets
    )
    assert late.backtest.result_hash != base.backtest.result_hash
    shifted = runner.run(CHOSEN, decision_offset=MINUTE)
    assert {t.decision_time for t in shifted.targets} == {
        t.decision_time + MINUTE for t in base.targets
    }
    with pytest.raises(ValueError):
        runner.run(CHOSEN, instruments=("NOPE",))


def test_an_evaluation_is_never_promotable_without_a_sealed_oos_result(
    planted: tuple[StrategyEvaluation, FailureRegistry],
) -> None:
    """ADR-0041 R18: a research-window result (G0–G4) never unblocks promotion by itself."""
    result, _ = planted
    assert result.validation is not None
    assert not any(g.gate_id.startswith("G5.") for g in result.validation.report.gates)
    assert result.promotion_blocked_reason is not None
    if result.status is EvaluationStatus.PASSED:
        assert result.promotion_blocked_reason == "sealed_oos_not_evaluated"
    unvalidated = StrategyEvaluation(result.subject, EvaluationStatus.NOT_VALIDATED)
    assert unvalidated.promotion_blocked_reason == "status_not_validated"


# =========================================================================================
# backlog E5: the labels' manifest is verified to be the backtest bars' (G0.manifest_binding)
# =========================================================================================

MANIFEST = "7" * 64


def _proven(market: SyntheticMarket, manifest: str = MANIFEST) -> DatasetPriceBars:
    """Stands in for ``backtest_bars_from_dataset`` (its only producer) over these bars: since
    B61 every such bar carries its proven volume."""
    bars = _bars(market, with_volume=True)
    return DatasetPriceBars(manifest, max(bar.available_time for bar in bars), bars)


def _extra(result: StrategyEvaluation) -> dict[str, object]:
    assert result.validation is not None and result.validation.view is not None
    extra = result.validation.view["extra"]
    assert isinstance(extra, dict)
    return extra


def test_the_synthetic_path_is_labelled_unverified(
    planted: tuple[StrategyEvaluation, FailureRegistry],
) -> None:
    result, _ = planted
    assert _extra(result)["price_binding"] == {
        "mode": PRICE_BINDING_SYNTHETIC,
        "manifest_content_hash": MANIFEST,
        "verified": False,
    }
    assert result.validation is not None
    assert "G0.manifest_binding" not in {g.gate_id for g in result.validation.report.gates}


def test_a_matching_manifest_passes_the_g0_binding_and_changes_nothing_else(
    planted: tuple[StrategyEvaluation, FailureRegistry], tmp_path: Path
) -> None:
    market = _market(seed=7, planted=True)
    proven = _proven(market)
    result, _ = _evaluate(market, tmp_path, dataset_bars=proven)
    assert result.validation is not None
    gates = result.validation.report.gates
    binding = next(g for g in gates if g.gate_id == "G0.manifest_binding")
    assert (binding.verdict, binding.value) == (Verdict.PASS, 0.0)
    synthetic, _ = planted
    assert synthetic.validation is not None
    assert tuple(g for g in gates if g is not binding) == synthetic.validation.report.gates
    assert _extra(result)["price_binding"] == {
        "mode": PRICE_BINDING_DATASET,
        "manifest_content_hash": MANIFEST,
        "price_cutoff": proven.price_cutoff.isoformat(),
        "verified": True,
        "mismatches": [],
    }


def test_a_mismatched_manifest_hash_is_refused_at_g0(tmp_path: Path) -> None:
    market = _market(seed=7, planted=True)
    result, registry = _evaluate(market, tmp_path, dataset_bars=_proven(market, "8" * 64))
    assert result.status is EvaluationStatus.REJECTED
    assert result.validation is not None
    report = result.validation.report
    assert report.verdict is Verdict.FAIL
    binding = next(g for g in report.gates if g.gate_id == "G0.manifest_binding")
    assert (binding.verdict, binding.value) == (Verdict.FAIL, 1.0)
    # Refused before any label is computed: only the adapter gates exist.
    assert {g.gate_id.split(".")[0] for g in report.gates} == {"G0"}
    (record,) = registry.records()
    assert (record.gate_id, record.terminal_state, record.reason_code) == (
        "G0.manifest_binding",
        "REJECTED",
        ReasonCode.CONTRACT_VIOLATION,
    )
    binding_view = _extra(result)["price_binding"]
    assert isinstance(binding_view, dict)
    assert (binding_view["verified"], binding_view["mismatches"]) == (False, ["manifest_hash"])


def test_bars_outside_the_manifest_are_refused() -> None:
    market = _market(seed=7, planted=True)
    candidate = library_entries()[0].candidate()
    proven = _proven(market)
    setup = _setup(market, candidate, dataset_bars=proven)
    assert binding_mismatches(setup, proven.bars) == []
    first = proven.bars[0]
    moved = first.model_copy(
        update={name: getattr(first, name) + 1 for name in ("open", "high", "low", "close")}
    )
    assert binding_mismatches(setup, (moved, *proven.bars[1:])) == ["bars_in_manifest"]
    other = _setup(market, candidate, dataset_bars=proven, manifest="8" * 64)
    assert binding_mismatches(other, proven.bars) == ["manifest_hash"]
    early = DatasetPriceBars(MANIFEST, proven.bars[-1].interval_start, proven.bars)
    assert binding_mismatches(_setup(market, candidate, dataset_bars=early), proven.bars) == [
        "price_cutoff"
    ]
    foreign = tuple(bar.model_copy(update={"instrument": "OTHER-USDT"}) for bar in proven.bars)
    assert "instrument_bars" in binding_mismatches(setup, foreign)
    # The synthetic path has nothing to verify against; it is labelled in the view instead.
    assert binding_mismatches(_setup(market, candidate), (moved,)) == []


# =========================================================================================
# backlog E1 follow-up: G0.manifest_binding checks the chain's feature / price manifest pair
# =========================================================================================

FEATURE_MANIFEST = "6" * 64


def _pair(feature: str = FEATURE_MANIFEST, price: str = MANIFEST) -> ManifestPair:
    """Stands in for ``pair_manifests`` (its only producer) over these two hashes."""
    return ManifestPair(feature, price, pair_hash_of(feature, price))


def _binding(result: StrategyEvaluation) -> tuple[Verdict, dict[str, object]]:
    assert result.validation is not None
    gate = next(g for g in result.validation.report.gates if g.gate_id == "G0.manifest_binding")
    view = _extra(result)["price_binding"]
    assert isinstance(view, dict)
    return gate.verdict, view


def _assert_refused_at_g0(
    result: StrategyEvaluation, registry: FailureRegistry, mismatches: list[str]
) -> None:
    assert result.status is EvaluationStatus.REJECTED
    assert result.validation is not None
    report = result.validation.report
    assert report.verdict is Verdict.FAIL
    binding = next(g for g in report.gates if g.gate_id == "G0.manifest_binding")
    assert (binding.verdict, binding.value) == (Verdict.FAIL, float(len(mismatches)))
    # Refused before any label is computed: only the adapter gates exist.
    assert {g.gate_id.split(".")[0] for g in report.gates} == {"G0"}
    (record,) = registry.records()
    assert (record.gate_id, record.terminal_state, record.reason_code) == (
        "G0.manifest_binding",
        "REJECTED",
        ReasonCode.CONTRACT_VIOLATION,
    )
    _, view = _binding(result)
    assert (view["verified"], view["mismatches"]) == (False, mismatches)


def test_a_matching_manifest_pair_passes_and_changes_nothing_else(tmp_path: Path) -> None:
    market = _market(seed=7, planted=True)
    proven, pair = _proven(market), _pair()
    result, _ = _evaluate(
        market,
        tmp_path / "pair",
        dataset_bars=proven,
        manifest_pair=pair,
        feature_manifest_hashes=(FEATURE_MANIFEST, FEATURE_MANIFEST),
    )
    verdict, view = _binding(result)
    assert verdict is Verdict.PASS
    assert view == {
        "mode": PRICE_BINDING_DATASET,
        "manifest_content_hash": MANIFEST,
        "price_cutoff": proven.price_cutoff.isoformat(),
        "verified": True,
        "mismatches": [],
        "manifest_pair": {
            "feature_manifest_hash": FEATURE_MANIFEST,
            "price_manifest_hash": MANIFEST,
            "pair_hash": pair.pair_hash,
        },
        "feature_manifest_hashes": [FEATURE_MANIFEST, FEATURE_MANIFEST],
    }
    # The pair adds checks, never gates: the report equals the pair-less dataset path's (E5).
    without, _ = _evaluate(market, tmp_path / "without", dataset_bars=proven)
    assert result.validation is not None and without.validation is not None
    assert result.validation.report.gates == without.validation.report.gates
    assert result.validation.report.verdict is without.validation.report.verdict
    assert result.status is without.status


def test_a_pair_for_other_prices_is_refused_at_g0(tmp_path: Path) -> None:
    market = _market(seed=7, planted=True)
    result, registry = _evaluate(
        market,
        tmp_path,
        dataset_bars=_proven(market),
        manifest_pair=_pair(price="8" * 64),  # a genuine pair hash, but of another price view
        feature_manifest_hashes=(FEATURE_MANIFEST,),
    )
    _assert_refused_at_g0(result, registry, ["pair_price_manifest"])


def test_features_from_another_manifest_are_refused_at_g0(tmp_path: Path) -> None:
    market = _market(seed=7, planted=True)
    result, registry = _evaluate(
        market,
        tmp_path,
        dataset_bars=_proven(market),
        manifest_pair=_pair(),
        feature_manifest_hashes=(FEATURE_MANIFEST, "5" * 64),  # one request off the pair
    )
    _assert_refused_at_g0(result, registry, ["pair_feature_manifest"])


def test_a_forged_pair_hash_is_refused_at_g0(tmp_path: Path) -> None:
    market = _market(seed=7, planted=True)
    forged = _pair()
    with pytest.raises(ValueError, match="pair_hash"):  # refused at construction ...
        ManifestPair(FEATURE_MANIFEST, MANIFEST, "0" * 64)
    object.__setattr__(forged, "pair_hash", "0" * 64)  # ... so forge one past it
    result, registry = _evaluate(
        market,
        tmp_path,
        dataset_bars=_proven(market),
        manifest_pair=forged,
        feature_manifest_hashes=(FEATURE_MANIFEST,),
    )
    _assert_refused_at_g0(result, registry, ["pair_hash"])


def test_a_pair_without_dataset_bars_is_refused_at_g0(tmp_path: Path) -> None:
    market = _market(seed=7, planted=True)
    result, registry = _evaluate(
        market, tmp_path, manifest_pair=_pair(), feature_manifest_hashes=(FEATURE_MANIFEST,)
    )
    _assert_refused_at_g0(result, registry, ["pair_without_dataset_bars"])
    _, view = _binding(result)
    assert (view["mode"], view["price_cutoff"]) == (PRICE_BINDING_DATASET, None)


def test_pair_checks_are_each_detected() -> None:
    market = _market(seed=7, planted=True)
    candidate = library_entries()[0].candidate()
    proven = _proven(market)

    def check(**fields: object) -> list[str]:
        setup = _setup(market, candidate, **fields)  # type: ignore[arg-type]
        return binding_mismatches(setup, proven.bars)

    features = (FEATURE_MANIFEST,)
    assert check(dataset_bars=proven, manifest_pair=_pair(), feature_manifest_hashes=features) == []
    # A pair needs the feature requests' hashes: none given is a mismatch, not a skip.
    assert check(dataset_bars=proven, manifest_pair=_pair()) == ["pair_feature_manifest"]
    # Feature hashes with nothing to compare them to are an inconsistent setup.
    assert check(dataset_bars=proven, feature_manifest_hashes=features) == [
        "feature_hashes_without_pair"
    ]
    assert check(feature_manifest_hashes=features) == ["feature_hashes_without_pair"]
    # A pair about two other manifests: both sides refused.
    other = _pair(feature="5" * 64, price="8" * 64)
    assert check(dataset_bars=proven, manifest_pair=other, feature_manifest_hashes=features) == [
        "pair_feature_manifest",
        "pair_price_manifest",
    ]
    # Without a pair, E5 is unchanged; the synthetic path has nothing to verify.
    assert check(dataset_bars=proven) == []
    assert check() == []


def test_the_synthetic_path_is_unchanged_by_the_pair_fields(
    planted: tuple[StrategyEvaluation, FailureRegistry],
) -> None:
    market = _market(seed=7, planted=True)
    setup = _setup(market, library_entries()[0].candidate())
    assert (setup.manifest_pair, setup.feature_manifest_hashes) == (None, ())
    result, _ = planted
    assert result.validation is not None
    assert "G0.manifest_binding" not in {g.gate_id for g in result.validation.report.gates}
    assert _extra(result)["price_binding"] == {
        "mode": PRICE_BINDING_SYNTHETIC,
        "manifest_content_hash": MANIFEST,
        "verified": False,
    }


# =========================================================================================
# backlog E4: public G4 input builder; a report-only diagnostic after an earlier FAIL
# =========================================================================================


def test_the_public_g4_builder_is_what_validate_runs(
    planted: tuple[StrategyEvaluation, FailureRegistry],
) -> None:
    result, _ = planted
    assert result.validation is not None and result.backtest is not None
    market = _market(seed=7, planted=True)
    candidate = library_entries()[0].candidate()
    validator = PipelineBacktestValidator(_setup(market, candidate))
    inp = validator.robustness_input(candidate.spec, result.backtest)
    g4 = tuple(g for g in result.validation.report.gates if g.gate_id.startswith("G4."))
    assert g4 and run_robustness(inp).gates == g4
    other = CandidateTrialRunner(candidate, _inputs(market), BarBacktester())
    with pytest.raises(ValueError, match="do not reproduce"):
        validator.robustness_input(candidate.spec, other.run({"lookback": 240}).backtest)


def test_a_diagnostic_g4_after_a_fail_is_report_only() -> None:
    market = _market(seed=7, planted=True)
    candidate = library_entries()[0].candidate()
    other = COST_MODEL.model_copy(update={"fee_rate_per_side": Decimal("0.001")})
    ctx = _context(candidate)
    ctx = ValidationContext(**{**ctx.__dict__, "cost_model": other})
    validator = PipelineBacktestValidator(_setup(market, candidate, context=ctx))
    backtest = CandidateTrialRunner(candidate, _inputs(market), BarBacktester()).run(CHOSEN)
    before = validator.validate(candidate.spec.ref, candidate.spec, backtest.backtest)
    assert before.report.verdict is Verdict.FAIL
    assert not any(g.gate_id.startswith("G4.") for g in before.report.gates)

    diagnostic = validator.robustness_diagnostic(candidate.spec, backtest.backtest)
    assert diagnostic.mode == DIAGNOSTIC_MODE
    assert diagnostic.backtest_result_hash == backtest.backtest.result_hash
    assert diagnostic.gates and all(g.gate_id.startswith("G4.") for g in diagnostic.gates)
    assert diagnostic.to_dict()["verdict_effect"] == "none"
    with pytest.raises(ValueError):
        type(diagnostic)(diagnostic.result, diagnostic.backtest_result_hash, mode="validated")
    # The verdict is untouched: validating again gives the same gates and verdict.
    after = validator.validate(candidate.spec.ref, candidate.spec, backtest.backtest)
    assert (after.report.gates, after.report.verdict) == (
        before.report.gates,
        before.report.verdict,
    )


# =========================================================================================
# implementation note (2026-09-26): the validator's declared execution model
# (``ValidatorSetup.backtester`` / ``.execution``, ``G0.execution_model``, G4 capacity coefficient)
# =========================================================================================


def _volumes(market: SyntheticMarket) -> dict[tuple[str, datetime], Decimal]:
    return {(SYMBOL, bar.interval_start): bar.volume for bar in market.bars}


def _variant(market: SyntheticMarket, coefficient: str = "0.1") -> BarBacktester:
    """A ``BarBacktester`` with an opt-in square-root impact model (TEST ONLY coefficients).

    The participation cap keeps every fill's participation small (bar volume is tiny relative to
    the strategy's equity here): without it the impact could push the fill price to zero or below.
    """
    return BarBacktester(
        execution=ExecutionModel(
            max_participation_rate=Decimal("0.01"),
            impact_coefficient=Decimal(coefficient),
            bar_volume=_volumes(market),
        )
    )


def test_no_declared_execution_model_adds_no_gate_and_the_report_is_reproducible(
    tmp_path: Path,
) -> None:
    """The default path (neither ``backtester`` nor ``execution`` declared) is byte-identical,
    including the report hash: it gets no ``G0.execution_model`` gate, and — with a fixed report
    timestamp (backlog E3) — the same inputs give the same report content hash every time."""
    market = _market(seed=7, planted=True)
    candidate = library_entries()[0].candidate()
    ctx = replace(_context(candidate), created_at=T0)
    first, _ = _evaluate(market, tmp_path / "a", context=ctx)
    second, _ = _evaluate(market, tmp_path / "b", context=ctx)
    assert first.validation is not None and second.validation is not None
    assert first.validation.report.content_hash() == second.validation.report.content_hash()
    assert "G0.execution_model" not in {g.gate_id for g in first.validation.report.gates}


def test_a_matching_execution_model_passes_g0_execution_model(tmp_path: Path) -> None:
    market = _market(seed=7, planted=True)
    candidate = library_entries()[0].candidate()
    variant = _variant(market)
    trials = CandidateTrialRunner(candidate, _inputs(market), variant)
    result, _ = _evaluate(
        market, tmp_path, run_backtester=variant, trials=trials, execution=variant.execution
    )
    assert result.validation is not None
    gates = {g.gate_id: g for g in result.validation.report.gates}
    assert gates["G0.execution_model"].verdict is Verdict.PASS
    assert gates["G0.reproducibility"].verdict is Verdict.PASS
    # Declaring the equivalent full provider instead of just the execution model agrees.
    same_provider = _setup(market, candidate, trials=trials, backtester=variant)
    assert same_provider.execution is None and same_provider.backtester is variant


def test_a_mismatched_execution_model_is_refused_at_g0(tmp_path: Path) -> None:
    """A candidate actually backtested with the variant, but validated against the plain v1
    backtester, is refused at ``G0.execution_model`` — before G0.reproducibility even runs."""
    market = _market(seed=7, planted=True)
    variant = _variant(market)
    result, registry = _evaluate(
        market, tmp_path, run_backtester=variant, backtester=BarBacktester()
    )
    assert result.status is EvaluationStatus.REJECTED
    assert result.validation is not None
    report = result.validation.report
    assert report.verdict is Verdict.FAIL
    gate = next(g for g in report.gates if g.gate_id == "G0.execution_model")
    assert (gate.verdict, gate.value) == (Verdict.FAIL, 0.0)
    # Refused before any re-run-dependent gate is even computed: only the adapter gates exist.
    assert {g.gate_id.split(".")[0] for g in report.gates} == {"G0"}
    (record,) = registry.records()
    assert (record.gate_id, record.terminal_state, record.reason_code) == (
        "G0.execution_model",
        "REJECTED",
        ReasonCode.CONTRACT_VIOLATION,
    )


def _carry_over_model(market: SyntheticMarket) -> ExecutionModel:
    """The ``_variant`` parameters with the remainder carried over (ADR-0054; TEST ONLY)."""
    return ExecutionModel(
        max_participation_rate=Decimal("0.01"),
        impact_coefficient=Decimal("0.1"),
        bar_volume=_volumes(market),
        carry_over=True,
    )


def _inputs_with_volume(market: SyntheticMarket) -> EvaluationInputs:
    """``_inputs`` whose bars carry the market's volume (``PriceBar.volume``, ADR-0054 §4)."""
    inputs = _inputs(market)
    volumes = _volumes(market)
    bars = tuple(
        bar.model_copy(update={"volume": volumes[(bar.instrument, bar.interval_start)]})
        for bar in inputs.bars
    )
    return replace(inputs, bars=bars)


def test_g0_execution_model_tells_carry_over_from_truncation(tmp_path: Path) -> None:
    """ADR-0054: the same cap and impact with and without carry-over are two execution models —
    two providers, told apart by ``G0.execution_model``: a truncating backtest validated against a
    declared carry-over model is refused before any re-run-dependent gate."""
    market = _market(seed=7, planted=True)
    truncating = _variant(market)
    carry = BarBacktester(execution=_carry_over_model(market))
    assert carry.descriptor.execution_model == "next_bar_open_participation"
    assert truncating.descriptor.execution_model == "next_bar_open"
    assert carry.descriptor.content_hash() != truncating.descriptor.content_hash()
    result, registry = _evaluate(
        market, tmp_path, run_backtester=truncating, execution=carry.execution
    )
    assert result.status is EvaluationStatus.REJECTED
    assert result.validation is not None
    gate = next(g for g in result.validation.report.gates if g.gate_id == "G0.execution_model")
    assert (gate.verdict, gate.value) == (Verdict.FAIL, 0.0)
    assert {g.gate_id.split(".")[0] for g in result.validation.report.gates} == {"G0"}
    (record,) = registry.records()
    assert (record.gate_id, record.reason_code) == (
        "G0.execution_model",
        ReasonCode.CONTRACT_VIOLATION,
    )


def test_a_carry_over_backtest_passes_g0_against_its_declared_model(tmp_path: Path) -> None:
    """A candidate backtested with carry-over (bars carrying volume) and validated against the
    same declared model passes ``G0.execution_model`` and reproduces (ADR-0054)."""
    market = _market(seed=7, planted=True)
    candidate = library_entries()[0].candidate()
    model = _carry_over_model(market)
    carry = BarBacktester(execution=model)
    inputs = _inputs_with_volume(market)
    trials = CandidateTrialRunner(candidate, inputs, carry)
    registry = FailureRegistry(tmp_path / "failures.jsonl")
    result = evaluate_strategy(
        candidate,
        inputs,
        backtester=carry,
        registry=registry,
        validator=PipelineBacktestValidator(
            _setup(market, candidate, trials=trials, execution=model)
        ),
    )
    assert result.backtest is not None and result.backtest.remainders
    assert result.validation is not None
    gates = {g.gate_id: g for g in result.validation.report.gates}
    assert gates["G0.execution_model"].verdict is Verdict.PASS
    assert gates["G0.reproducibility"].verdict is Verdict.PASS


def test_a_setup_cannot_declare_both_backtester_and_execution(tmp_path: Path) -> None:
    market = _market(seed=7, planted=True)
    candidate = library_entries()[0].candidate()
    variant = _variant(market)
    with pytest.raises(ValueError, match="either backtester or execution"):
        _setup(market, candidate, backtester=variant, execution=variant.execution)


def test_robustness_input_refuses_a_declared_execution_model_mismatch() -> None:
    """``robustness_input`` refuses the mismatch itself, before re-running the parameter grid."""
    market = _market(seed=7, planted=True)
    candidate = library_entries()[0].candidate()
    variant = _variant(market)
    backtest = CandidateTrialRunner(candidate, _inputs(market), variant).run(CHOSEN).backtest
    validator = PipelineBacktestValidator(_setup(market, candidate, backtester=BarBacktester()))
    with pytest.raises(ValueError, match="execution model does not match"):
        validator.robustness_input(candidate.spec, backtest)


def test_an_impact_coefficient_conflict_is_inconclusive() -> None:
    """A backtest's execution model impact coefficient disagreeing with an explicit
    ``RobustnessParams.impact_coefficient`` is never resolved silently (implementation note,
    2026-09-26): the capacity check reports the conflict and is ``INCONCLUSIVE``."""
    market = _market(seed=7, planted=True)
    candidate = library_entries()[0].candidate()
    variant = _variant(market, coefficient="0.1")
    trials = CandidateTrialRunner(candidate, _inputs(market), variant)
    conflicting = replace(E2E_TEST_ONLY_PARAMS, impact_coefficient=0.5)
    setup = _setup(
        market, candidate, trials=trials, execution=variant.execution, robustness=conflicting
    )
    validator = PipelineBacktestValidator(setup)
    backtest = trials.run(CHOSEN).backtest
    result = run_robustness(validator.robustness_input(candidate.spec, backtest))
    gate = next(g for g in result.gates if g.gate_id == "G4.capacity.impact_estimated")
    assert gate.verdict is Verdict.INCONCLUSIVE
    assert gate.metric == "impact_coefficient_mismatch"
    checks = {c.check_id: c for c in result.checks}
    assert checks["capacity"].details["impact_coefficient_conflict"] == {
        "param_capacity_impact_coefficient": 0.5,
        "execution_model_impact_coefficient": 0.1,
    }
    assert checks["capacity"].details["impact_cost_per_period_at_capacity"] is None


@pytest.mark.parametrize("coefficient", ["0.1", "0.10"])
def test_an_equal_float_param_and_decimal_model_coefficient_agree(coefficient: str) -> None:
    """Review fixes 3 (2026-09-26): ``RobustnessParams.impact_coefficient = 0.1`` (a float) and
    the execution model's ``Decimal("0.1")`` are the same number, compared exactly (no ``float``
    round trip): no mismatch, the model's value is used."""
    market = _market(seed=7, planted=True)
    candidate = library_entries()[0].candidate()
    variant = _variant(market, coefficient=coefficient)
    trials = CandidateTrialRunner(candidate, _inputs(market), variant)
    same = replace(E2E_TEST_ONLY_PARAMS, impact_coefficient=0.1)
    setup = _setup(market, candidate, trials=trials, execution=variant.execution, robustness=same)
    validator = PipelineBacktestValidator(setup)
    inp = validator.robustness_input(candidate.spec, trials.run(CHOSEN).backtest)
    assert inp.execution_impact_coefficient == Decimal(coefficient)
    assert isinstance(inp.execution_impact_coefficient, Decimal)
    result = run_robustness(inp)
    assert "G4.capacity.impact_estimated" not in {g.gate_id for g in result.gates}
    details = {c.check_id: c for c in result.checks}["capacity"].details
    assert details["impact_coefficient_source"] == "execution_model"
    assert "impact_coefficient_conflict" not in details


def test_the_capacity_check_uses_the_execution_models_coefficient() -> None:
    """With no explicit ``impact_coefficient`` (``None``), the capacity check reads the
    execution model's coefficient instead of leaving the impact estimate unreported."""
    market = _market(seed=7, planted=True)
    candidate = library_entries()[0].candidate()
    variant = _variant(market, coefficient="0.2")
    trials = CandidateTrialRunner(candidate, _inputs(market), variant)
    no_explicit = replace(E2E_TEST_ONLY_PARAMS, impact_coefficient=None)
    setup = _setup(
        market, candidate, trials=trials, execution=variant.execution, robustness=no_explicit
    )
    validator = PipelineBacktestValidator(setup)
    backtest = trials.run(CHOSEN).backtest
    result = run_robustness(validator.robustness_input(candidate.spec, backtest))
    checks = {c.check_id: c for c in result.checks}
    details = checks["capacity"].details
    assert details["impact_coefficient_source"] == "execution_model"
    assert details["impact_cost_per_period_at_capacity"] is not None
    # A successfully resolved coefficient is never gated (an estimate is reported, not a pass).
    assert "G4.capacity.impact_estimated" not in {g.gate_id for g in result.gates}


# =========================================================================================
# ADR-0064 (B66): on the dataset path, bar_volume must equal the executed bars' proven volume
# =========================================================================================


def _volumes_of(market: SyntheticMarket) -> dict[tuple[str, datetime], Decimal]:
    """The separately bound ``bar_volume`` the validator is given (the market's own volumes)."""
    return {(SYMBOL, bar.interval_start): bar.volume for bar in market.bars}


def _capacity(
    market: SyntheticMarket, proven: DatasetPriceBars | None, **fields: object
) -> tuple[RobustnessCheck, tuple[GateResult, ...], BacktestResult]:
    """The C-R5 check of the chosen run under ``proven`` (``None``: the synthetic path);
    ``fields`` go to ``_setup`` (e.g. ``bar_volume``)."""
    candidate = library_entries()[0].candidate()
    setup = _setup(market, candidate, dataset_bars=proven, **fields)  # type: ignore[arg-type]
    backtest = setup.trials.run(CHOSEN).backtest
    validator = PipelineBacktestValidator(setup)
    result = run_robustness(validator.robustness_input(candidate.spec, backtest))
    capacity = {c.check_id: c for c in result.checks}["capacity"]
    gates = tuple(g for g in result.gates if g.gate_id.startswith("G4.capacity."))
    return capacity, gates, backtest


def _traded(backtest: BacktestResult) -> list[datetime]:
    return sorted({fill.fill_time for fill in backtest.fills if fill.quantity != 0})


def _changed(
    volumes: dict[tuple[str, datetime], Decimal], changes: dict[datetime, Decimal | None]
) -> dict[tuple[str, datetime], Decimal]:
    """``volumes`` with the values at ``changes``' times replaced (``None``: removed)."""
    out = {key: value for key, value in volumes.items() if key[1] not in changes}
    out |= {(SYMBOL, t): v for t, v in changes.items() if v is not None}
    return out


def _gates(gates: tuple[GateResult, ...]) -> list[tuple[str, str]]:
    return [(g.gate_id, g.metric) for g in gates]


ESTIMATED_MISMATCH = [("G4.capacity.estimated", "bar_volume_source_mismatch")]
ESTIMATED_MISSING = [("G4.capacity.estimated", "bar_volume_missing")]


def test_equal_volume_sources_keep_the_capacity_result() -> None:
    market = _market(seed=7, planted=True)
    synthetic = _capacity(market, None)
    dataset = _capacity(market, _proven(market))
    assert dataset[:2] == synthetic[:2]  # details and gates
    assert "capacity" in dataset[0].details
    assert "bar_volume_source_mismatch" not in dataset[0].details
    # numeric Decimal equality: another representation of the same value is not a conflict
    rewritten = {key: value * Decimal("1.00") for key, value in _volumes_of(market).items()}
    first = _traded(dataset[2])[0]
    assert str(rewritten[(SYMBOL, first)]) != str(_volumes_of(market)[(SYMBOL, first)])
    assert _capacity(market, _proven(market), bar_volume=rewritten)[:2] == synthetic[:2]


def test_a_volume_source_mismatch_is_inconclusive_and_computes_nothing() -> None:
    market = _market(seed=7, planted=True)
    proven = _proven(market)
    first, *_ = _traded(_capacity(market, proven)[2])
    executed = _volumes_of(market)[(SYMBOL, first)]
    supplied = _changed(_volumes_of(market), {first: executed + 1})
    capacity, gates, _ = _capacity(market, proven, bar_volume=supplied)
    assert _gates(gates) == ESTIMATED_MISMATCH  # no .required / .impact_estimated
    [estimated] = gates
    assert (estimated.verdict, estimated.value) == (Verdict.INCONCLUSIVE, 1.0)
    assert "capacity" not in capacity.details
    assert "impact_cost_per_period_at_capacity" not in capacity.details
    assert capacity.details["bar_volume_source_mismatch"] == {
        "fills": 1,
        "missing": 0,
        "first": {
            "instrument": SYMBOL,
            "time": first.isoformat(),
            "bar_volume": str(executed + 1),
            "dataset_bars_volume": str(executed),
        },
    }
    # the same supplied volumes on the synthetic path: nothing to compare against, unchanged
    synthetic = _capacity(market, None, bar_volume=supplied)
    assert (
        "capacity" in synthetic[0].details
        and "bar_volume_source_mismatch" not in synthetic[0].details
    )


def test_only_the_executed_bar_at_the_fill_time_is_compared() -> None:
    market = _market(seed=7, planted=True)
    proven = _proven(market)
    reference = _capacity(market, proven)
    traded = set(_traded(reference[2]))
    untraded = [bar.interval_start for bar in proven.bars if bar.interval_start not in traded]
    assert untraded
    elsewhere = _changed(_volumes_of(market), {untraded[0]: Decimal("123456789")})
    assert _capacity(market, proven, bar_volume=elsewhere)[:2] == reference[:2]
    # a value keyed at another instrument is not the fill's: missing, never compared
    other = {("OTHER-USDT", t): v for (_, t), v in _volumes_of(market).items()}
    assert _gates(_capacity(market, proven, bar_volume=other)[1]) == ESTIMATED_MISSING


def test_a_volume_missing_from_either_source_stays_bar_volume_missing() -> None:
    market = _market(seed=7, planted=True)
    proven = _proven(market)
    first, *_ = _traded(_capacity(market, proven)[2])
    # missing from bar_volume
    supplied = _changed(_volumes_of(market), {first: None})
    _, gates, _ = _capacity(market, proven, bar_volume=supplied)
    assert _gates(gates) == ESTIMATED_MISSING
    # missing from the executed bars (a bar without volume)
    bars = tuple(
        bar.model_copy(update={"volume": None}) if bar.interval_start == first else bar
        for bar in proven.bars
    )
    no_volume = DatasetPriceBars(proven.manifest_content_hash, proven.price_cutoff, bars)
    capacity, gates, _ = _capacity(market, no_volume)
    assert _gates(gates) == ESTIMATED_MISSING
    assert "bar_volume_source_mismatch" not in capacity.details


def test_a_mismatch_takes_precedence_over_a_missing_volume() -> None:
    market = _market(seed=7, planted=True)
    proven = _proven(market)
    first, second, *_ = _traded(_capacity(market, proven)[2])
    executed = _volumes_of(market)[(SYMBOL, second)]
    supplied = _changed(_volumes_of(market), {first: None, second: executed + 1})
    capacity, gates, _ = _capacity(market, proven, bar_volume=supplied)
    assert _gates(gates) == ESTIMATED_MISMATCH
    record = capacity.details["bar_volume_source_mismatch"]
    assert isinstance(record, dict) and (record["fills"], record["missing"]) == (1, 1)


def test_a_mismatch_reaches_the_report_as_an_inconclusive_g4_gate(tmp_path: Path) -> None:
    market = _market(seed=7, planted=True)
    proven = _proven(market)
    shifted = {key: value + 1 for key, value in _volumes_of(market).items()}  # every bar differs
    result, _ = _evaluate(market, tmp_path / "mismatch", dataset_bars=proven, bar_volume=shifted)
    assert result.validation is not None
    report = result.validation.report
    binding = next(g for g in report.gates if g.gate_id == "G0.manifest_binding")
    assert binding.verdict is Verdict.PASS  # the executed bars are the proven ones
    estimated = next(g for g in report.gates if g.gate_id == "G4.capacity.estimated")
    assert (estimated.verdict, estimated.metric) == (
        Verdict.INCONCLUSIVE,
        "bar_volume_source_mismatch",
    )
    assert report.verdict is Verdict.INCONCLUSIVE
    # equal sources: the report is the one without the conflict
    equal, _ = _evaluate(market, tmp_path / "equal", dataset_bars=proven)
    assert equal.validation is not None
    assert "G4.capacity.estimated" not in {g.gate_id for g in equal.validation.report.gates}


# =========================================================================================
# ADR-0065 (B67): carry-over remainders left unfilled on the dataset path
# =========================================================================================


def _carry_over(market: SyntheticMarket, rate: str = "0.01") -> dict[str, object]:
    """``_setup`` fields for a carry-over run (ADR-0054) on the proven, volume-carrying bars."""
    model = ExecutionModel(max_participation_rate=Decimal(rate), carry_over=True)
    candidate = library_entries()[0].candidate()
    trials = CandidateTrialRunner(
        candidate, _inputs(market, with_volume=True), BarBacktester(execution=model)
    )
    return {"trials": trials, "execution": model}


def test_an_unfilled_carry_over_remainder_makes_capacity_inconclusive() -> None:
    market = _market(seed=7, planted=True)
    capacity, gates, backtest = _capacity(market, _proven(market), **_carry_over(market))
    unfilled = [item for item in backtest.remainders if item.remaining_quantity > 0]
    assert unfilled, "the TEST ONLY cap must leave a remainder"
    assert _gates(gates) == [("G4.capacity.estimated", "carry_over_unfilled")]
    assert gates[0].value == float(len(unfilled))
    assert "capacity" not in capacity.details
    record = capacity.details["carry_over_unfilled"]
    assert isinstance(record, dict)
    first = unfilled[0]
    assert record["remainders"] == len(unfilled)
    # no cross-instrument total: quantities of different instruments do not add
    assert set(record) == {"remainders", "first"}
    assert record["first"] == {
        "instrument": first.instrument,
        "decision_time": first.decision_time.isoformat(),
        "requested_quantity": str(first.requested_quantity),
        "filled_quantity": str(first.filled_quantity),
        "remaining_quantity": str(first.remaining_quantity),
        "ended_by": first.ended_by,
        "ended_at": first.ended_at.isoformat(),
    }


def test_remainders_are_read_on_the_dataset_path_only() -> None:
    market = _market(seed=7, planted=True)
    fields = _carry_over(market)
    # the same carry-over run on the synthetic path: not read, the check is as before
    synthetic, gates, backtest = _capacity(market, None, **fields)
    assert any(item.remaining_quantity > 0 for item in backtest.remainders)
    assert "carry_over_unfilled" not in synthetic.details
    assert ("G4.capacity.estimated", "carry_over_unfilled") not in _gates(gates)


#: The contract version the research report / run hashes pinned here and in the strategy, router
#: and synthetic-lab tests were recorded at (2026-09-26/27, ADR-0055 2.2.0; ``a8490bf`` /
#: ``6d887b7``). The 2.3.0 – 2.5.0 minors changed only the envelope every content hash includes,
#: so these pins are checked on what the 2.2.0 code built (``at_contract_version``); any other
#: change still breaks them.
def _envelope_free(document: Any) -> Any:
    """``document`` without any ``schema_version``."""
    if isinstance(document, dict):
        return {k: _envelope_free(v) for k, v in document.items() if k != "schema_version"}
    if isinstance(document, list):
        return [_envelope_free(item) for item in document]
    return document


#: ``ValidationReport.content_hash()`` of the full evaluation on the code before B67 (``255ce1a``),
#: computed twice on that source: the dataset path (default ``next_bar_open``, no remainders) and
#: the synthetic path. B67 must reproduce them bit for bit. Recorded at contract 2.2.0
#: (``PINNED_CONTRACT_VERSION``).
PRE_B67_DATASET_REPORT_HASH = "8ed6bf10ce3a1a2b1cab21d383468f92aece3603c74fcfb23006739d566b3a6a"
PRE_B67_SYNTHETIC_REPORT_HASH = "f46de6b1b9c047e5743ff9d5676f3e640aea7f2f47b05f00092d7e43acdbbd76"
#: Report fields that bind a contract object by its content hash (which includes its envelope),
#: or that are not part of the report's content (``created_at``).
_ENVELOPE_BOUND = ("created_at", "experiment_hash", "validation_profile_hash")


def _default_model_reports(tmp: str) -> dict[str, Any]:
    """The dataset- and synthetic-path reports of the planted market: hashes and JSON dumps."""
    market = _market(seed=7, planted=True)
    dataset, _ = _evaluate(market, Path(tmp) / "dataset", dataset_bars=_proven(market))
    synthetic, _ = _evaluate(market, Path(tmp) / "synthetic")
    assert dataset.validation is not None and synthetic.validation is not None
    reports = (dataset.validation.report, synthetic.validation.report)
    return {
        "hashes": [report.content_hash() for report in reports],
        "reports": [report.model_dump(mode="json") for report in reports],
    }


def test_the_default_model_reports_are_the_pre_b67_ones(tmp_path: Path) -> None:
    # the pins were recorded at contract 2.2.0: reproduced bit for bit when every contract
    # object is built at 2.2.0
    then = at_contract_version(
        PINNED_CONTRACT_VERSION, f"{__name__}:_default_model_reports", str(tmp_path / "then")
    )
    assert then["hashes"] == [PRE_B67_DATASET_REPORT_HASH, PRE_B67_SYNTHETIC_REPORT_HASH]
    # at the current contract the reports differ from those only by the envelope (and the
    # hashes binding envelope-carrying objects)
    now = _default_model_reports(str(tmp_path / "now"))
    for current, old in zip(now["reports"], then["reports"], strict=True):
        assert current["schema_version"] == CONTRACT_SCHEMA_VERSION
        assert old["schema_version"] == PINNED_CONTRACT_VERSION
        for key in _ENVELOPE_BOUND:
            del current[key], old[key]
        assert _envelope_free(current) == _envelope_free(old)


def test_the_default_model_has_no_remainders_and_is_unchanged() -> None:
    market = _market(seed=7, planted=True)
    synthetic = _capacity(market, None)
    dataset = _capacity(market, _proven(market))
    assert dataset[2].remainders == ()  # next_bar_open: no carry-over records
    assert dataset[:2] == synthetic[:2]
    assert "carry_over_unfilled" not in dataset[0].details
    candidate = library_entries()[0].candidate()
    setup = _setup(market, candidate, dataset_bars=_proven(market))
    inp = PipelineBacktestValidator(setup).robustness_input(
        candidate.spec, setup.trials.run(CHOSEN).backtest
    )
    assert inp.capacity_remainders == ()
    synthetic_setup = _setup(market, candidate)
    synthetic_inp = PipelineBacktestValidator(synthetic_setup).robustness_input(
        candidate.spec, synthetic_setup.trials.run(CHOSEN).backtest
    )
    assert synthetic_inp.capacity_remainders is None


# =========================================================================================
# debugging pass (2026-09-26): optional multi-seed G1 negative controls through ValidatorSetup
# =========================================================================================

_CONTROLS = ("G1.shuffle_control", "G1.shift_control")


def test_the_default_setup_keeps_single_seed_controls() -> None:
    market = _market(seed=7, planted=True)
    setup = _setup(market, library_entries()[0].candidate())
    assert setup.control_seeds is None


def test_control_seeds_reach_the_in_sample_controls() -> None:
    market = _market(seed=7, planted=True)
    candidate = library_entries()[0].candidate()
    backtest = CandidateTrialRunner(candidate, _inputs(market), BarBacktester()).run(CHOSEN)
    plain = PipelineBacktestValidator(_setup(market, candidate))
    seeded = PipelineBacktestValidator(replace(_setup(market, candidate), control_seeds=(11, 12)))
    before = plain.validate(candidate.spec.ref, candidate.spec, backtest.backtest).report.gates
    after = seeded.validate(candidate.spec.ref, candidate.spec, backtest.backtest).report.gates
    old, new = {g.gate_id: g for g in before}, {g.gate_id: g for g in after}
    assert not any(".seed." in gate_id for gate_id in old)
    # the setup's seed 11 is the legacy shuffle seed, 12 the legacy shift seed
    for gate_id, seed in zip(_CONTROLS, (11, 12), strict=True):
        per_seed = new[f"{gate_id}.seed.{seed}"]
        assert (per_seed.value, per_seed.verdict) == (old[gate_id].value, old[gate_id].verdict)
        assert new[gate_id].metric.endswith("_min_over_seeds[>=]")
    assert set(new) - set(old) == {f"{g}.seed.{s}" for g in _CONTROLS for s in (11, 12)}
    for gate_id in old:
        if not gate_id.startswith(_CONTROLS):
            assert new[gate_id] == old[gate_id]

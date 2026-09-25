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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from core.contracts.cost_model import CostModelSpec
from core.contracts.outcome import OutcomeLabelSpec, OutcomeMethod
from core.contracts.strategy import BacktestCostModel, PriceBar
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
from core.domain.base import Kind, Ref
from core.domain.research import RunState, Verdict, derive_verdict
from core.domain.specs import OutcomeSpec
from core.errors import ReasonCode
from infrastructure.bars import DatasetPriceBars, ManifestPair, pair_hash_of
from plugins.backtest import BarBacktester
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
    ValidatorSetup,
    binding_mismatches,
)
from research.validation import RobustnessParams, ValidationContext, to_json
from research.validation.g4 import run_robustness
from tests import factories

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


def _bars(market: SyntheticMarket) -> tuple[PriceBar, ...]:
    """Research-window bars only: nothing at or after the sealed boundary is simulated."""
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
        for bar in market.bars
        if bar.interval_end <= BOUNDARY
    )


def _inputs(market: SyntheticMarket) -> EvaluationInputs:
    bars = _bars(market)
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
) -> ValidatorSetup:
    return ValidatorSetup(
        context=context or _context(candidate),
        outcome_provider=ForwardReturnOutcome((LABEL_SPEC,)),
        manifest_content_hash=manifest,
        dataset_bars=dataset_bars,
        manifest_pair=manifest_pair,
        feature_manifest_hashes=feature_manifest_hashes,
        instrument=SYMBOL,
        trials=CandidateTrialRunner(candidate, _inputs(market), BarBacktester()),
        chosen_params=CHOSEN,
        seed=11,
        robustness=E2E_TEST_ONLY_PARAMS,
        state_of=lambda t: "am" if t.hour < 12 else "pm",
        bar_volume={(SYMBOL, bar.interval_start): bar.volume for bar in market.bars},
        declared_instruments=(SYMBOL,),
    )


def _evaluate(
    market: SyntheticMarket, tmp_path: Path, **setup: object
) -> tuple[StrategyEvaluation, FailureRegistry]:
    candidate = library_entries()[0].candidate()
    registry = FailureRegistry(tmp_path / "failures.jsonl")
    result = evaluate_strategy(
        candidate,
        _inputs(market),
        backtester=BarBacktester(),
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
    """Stands in for ``backtest_bars_from_dataset`` (its only producer) over these bars."""
    bars = _bars(market)
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

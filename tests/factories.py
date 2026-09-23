"""测试用的最小合法对象构造器。

⚠️ 这里出现的所有数字都是**测试数据**，不是被批准的验证阈值。
真正的阈值在 Phase 4 校准后写入 Validation Profile 版本（ADR-0007 两步冻结 Step 2）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from core.contracts.profile_selection import (
    ProfileSelectionKey,
    ProfileSelectionRule,
    SelectionEntry,
)
from core.contracts.validation_profile import (
    BenchmarkParams,
    CostStressParams,
    DataSplitParams,
    LifecycleParams,
    ParameterStabilityParams,
    ProfileScope,
    Provenance,
    SampleSizeParams,
    SignificanceParams,
    ValidationProfile,
    WalkForwardParams,
)
from core.domain.base import Kind, Ref
from core.domain.research import (
    GateResult,
    ReproducibilityTuple,
    ValidationReport,
    Verdict,
)
from core.domain.specs import DatasetRef, Zone

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def dataset_ref() -> DatasetRef:
    return DatasetRef(
        zone=Zone.CANONICAL,
        table="binance_btcusdt_1h",
        snapshot_id="snap-1",
        time_range_start=T0,
        time_range_end=T0 + timedelta(days=30),
    )


def hypothesis_ref(version: str = "1.0.0") -> Ref:
    return Ref(kind=Kind.HYPOTHESIS, name="h_example", version=version)


def repro_tuple(**overrides: object) -> ReproducibilityTuple:
    payload: dict[str, object] = {
        "hypothesis_ref": hypothesis_ref(),
        "dataset_snapshots": (dataset_ref(),),
        "code_commit": "0123456789abcdef",
        "environment_lock": "lock-hash",
        "constitution_version": "0.2.0-draft",
        "validation_profile_version": "vp:test_scope@1.0.0",
        "validation_profile_hash": "profile-hash",
        "split_spec": "train/validation/sealed-oos",
        "cost_model_ref": Ref(kind=Kind.COST_MODEL, name="cost_v1", version="1.0.0"),
    }
    payload.update(overrides)
    return ReproducibilityTuple(**payload)  # type: ignore[arg-type]


def validation_profile(**overrides: object) -> ValidationProfile:
    payload: dict[str, object] = {
        "name": "test_scope",
        "version": "1.0.0",
        "scope": ProfileScope(
            venue="testvenue", symbol="TESTPAIR", timeframe="1h", research_class="swing"
        ),
        "data_split": DataSplitParams(
            research_window_start=date(2020, 1, 1),
            sealed_oos_boundary=date(2024, 1, 1),
            sealed_oos_length=timedelta(days=365),
            sealed_oos_max_extension=timedelta(days=365),
            embargo=timedelta(hours=72),
            walk_forward=WalkForwardParams(
                train_window=timedelta(days=720),
                test_window=timedelta(days=180),
                step=timedelta(days=180),
                min_positive_window_fraction=0.5,
                max_single_window_pnl_share=0.5,
            ),
        ),
        "sample_size": SampleSizeParams(
            min_effective_trades_in_sample=1,
            min_effective_trades_out_of_sample=1,
            min_effective_trades_per_state=1,
            effective_sample_method="overlap-adjusted",
            min_regime_coverage="test",
        ),
        "significance": SignificanceParams(
            multiple_testing_method="test-method",
            multiple_testing_threshold=0.5,
            overfitting_metric="test-metric",
            overfitting_threshold=0.5,
            trial_count_scope="family",
        ),
        "benchmark": BenchmarkParams(
            null_model="random-entry",
            null_model_simulations=1,
            null_model_percentile=50.0,
            market_benchmark_rule="by-class",
            inverse_control_reported=True,
        ),
        "parameter_stability": ParameterStabilityParams(
            neighborhood_definition="one-grid-step",
            min_neighborhood_performance_ratio=0.5,
            min_positive_neighbor_fraction=0.5,
        ),
        "cost_stress": CostStressParams(
            cost_model=Ref(kind=Kind.COST_MODEL, name="cost_v1", version="1.0.0"),
            fill_assumption="next-bar-open",
            stress_multipliers=(2.0,),
            delay_stress_bars=1,
            min_breakeven_cost_multiple=2.0,
        ),
        "lifecycle": LifecycleParams(
            paper_period=timedelta(days=30),
            paper_acceptance_rule="test-rule",
        ),
    }
    payload.update(overrides)
    return ValidationProfile(**payload)  # type: ignore[arg-type]


def selection_key(research_class: str = "swing") -> ProfileSelectionKey:
    return ProfileSelectionKey(
        venue="testvenue", symbol="TESTPAIR", timeframe="1h", research_class=research_class
    )


def selection_rule() -> ProfileSelectionRule:
    return ProfileSelectionRule(
        name="test_rule",
        version="1.0.0",
        entries=(
            SelectionEntry(
                key=selection_key(),
                profile=Ref(kind=Kind.PROFILE, name="test_scope", version="1.0.0"),
                profile_version="1.0.0",
            ),
        ),
    )


def gate_result(verdict: Verdict = Verdict.PASS, *, with_threshold: bool = True) -> GateResult:
    return GateResult(
        gate_id="G0",
        metric="reproducible",
        value=1.0,
        threshold=1.0 if with_threshold else None,
        threshold_source="significance.multiple_testing_threshold" if with_threshold else None,
        verdict=verdict,
    )


def validation_report(verdict: Verdict = Verdict.PASS, **overrides: object) -> ValidationReport:
    payload: dict[str, object] = {
        "report_id": "rep-1",
        "subject": Ref(kind=Kind.STRATEGY, name="s_example", version="1.0.0"),
        "experiment_hash": "exp-hash",
        "constitution_version": "0.2.0-draft",
        "validation_profile_version": "vp:test_scope@1.0.0",
        "validation_profile_hash": "profile-hash",
        "gates": (gate_result(verdict),),
        "verdict": verdict,
    }
    payload.update(overrides)
    return ValidationReport(**payload)  # type: ignore[arg-type]


def frozen_profile() -> ValidationProfile:
    return validation_profile(
        status="frozen",
        provenance=Provenance(calibration_report="calib-1", approval_adr="ADR-XXXX"),
    )

"""测试用的最小合法对象构造器。

⚠️ 这里出现的所有数字都是**测试数据**，不是被批准的验证阈值。
真正的阈值在 Phase 4 校准后写入 Validation Profile 版本（ADR-0007 两步冻结 Step 2）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from core.contracts.profile_selection import (
    ExperimentMetadata,
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
from core.domain.artifact import (
    DeploymentRecord,
    EquivalenceCheck,
    GoldenOutputs,
    StrategyArtifact,
)
from core.domain.base import GitCodeRevision, Kind, Ref
from core.domain.research import (
    ExperimentRun,
    ExperimentSpec,
    GateResult,
    ReproducibilityTuple,
    ValidationReport,
    Verdict,
)
from core.domain.selection import ProfileSelection
from core.domain.specs import DatasetRef, Zone
from core.lifecycle.strategy import LifecycleHistory

T0 = datetime(2024, 1, 1, tzinfo=UTC)

#: 测试用的假内容哈希：形状必须合法（64 位小写十六进制），取值无语义。
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_RULE = "f" * 64
#: Profile 与实验身份用的假 `ContentHash`（ADR-0015 §D-21.1 起这些槽位必须是 SHA-256 形状）。
HASH_PROFILE = "1" * 64
HASH_EXPERIMENT = "2" * 64
HASH_CONFIG = "3" * 64

#: 测试用的假 Git OID：40 位小写十六进制（ADR-0015 §D-21.2 起短 SHA 被拒绝）。
GIT_COMMIT_OID = "0123456789abcdef0123456789abcdef01234567"
GIT_TREE_OID = "fedcba9876543210fedcba9876543210fedcba98"
#: 另一个合法 OID：供"只改一项就换身份"的实验哈希敏感性测试使用。
OTHER_GIT_COMMIT_OID = "89abcdef0123456789abcdef0123456789abcdef"
#: 64 位形式（SHA-256 仓库）同样合法。
GIT_COMMIT_OID_SHA256 = "0123456789abcdef" * 4


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


def strategy_ref(name: str = "s_example", version: str = "1.0.0") -> Ref:
    return Ref(kind=Kind.STRATEGY, name=name, version=version)


def risk_ref(name: str = "r_example", version: str = "1.0.0") -> Ref:
    return Ref(kind=Kind.RISK, name=name, version=version)


def outcome_ref(name: str = "o_example", version: str = "1.0.0") -> Ref:
    return Ref(kind=Kind.OUTCOME, name=name, version=version)


def cost_model_ref(name: str = "cost_v1", version: str = "1.0.0") -> Ref:
    return Ref(kind=Kind.COST_MODEL, name=name, version=version)


def selection_rule_ref(version: str = "1.0.0") -> Ref:
    return Ref(kind=Kind.PROFILE_SELECTION_RULE, name="test_rule", version=version)


def profile_ref(name: str = "test_scope", version: str = "1.0.0") -> Ref:
    """Validation Profile 的引用（ADR-0015 §D-22.2 起三处绑定都用它，不再用自由字符串）。"""
    return Ref(kind=Kind.PROFILE, name=name, version=version)


def profile_selection(**overrides: object) -> ProfileSelection:
    payload: dict[str, object] = {
        "selection_rule": selection_rule_ref(),
        "selection_rule_hash": HASH_RULE,
        "key": selection_key(),
    }
    payload.update(overrides)
    return ProfileSelection(**payload)  # type: ignore[arg-type]


def dependency_hashes(*refs: Ref, **extra: str) -> dict[str, str]:
    """把直接引用映射成 `kind:name@version → content_hash`（ADR-0009 §5 的覆盖规则）。"""
    fixed = (HASH_A, HASH_B, HASH_C, HASH_D, HASH_E)
    mapping = {str(ref): fixed[index % len(fixed)] for index, ref in enumerate(refs)}
    mapping.update(extra)
    return mapping


def repro_tuple(**overrides: object) -> ReproducibilityTuple:
    """默认构造一个**完整绑定**的复现元组：直接引用全部出现在 dependency_hashes 中。"""
    refs: dict[str, Ref | None] = {
        "hypothesis_ref": hypothesis_ref(),
        "strategy_ref": strategy_ref(),
        "risk_policy_ref": risk_ref(),
        "outcome_ref": outcome_ref(),
        "cost_model_ref": cost_model_ref(),
    }
    for name in refs:
        if name in overrides:
            refs[name] = overrides[name]  # type: ignore[assignment]

    payload: dict[str, object] = {
        **refs,
        "dataset_snapshots": (dataset_ref(),),
        "code_commit": GIT_COMMIT_OID,
        "dependency_hashes": dependency_hashes(*(r for r in refs.values() if r is not None)),
        "environment_lock": "lock-hash",
        "constitution_version": "0.2.0-draft",
        "validation_profile": profile_ref(),
        "validation_profile_hash": HASH_PROFILE,
        "profile_selection": profile_selection(),
        "split_spec": "train/validation/sealed-oos",
    }
    payload.update(overrides)
    return ReproducibilityTuple(**payload)  # type: ignore[arg-type]


def experiment_spec(**overrides: object) -> ExperimentSpec:
    payload: dict[str, object] = {
        "name": "e_example",
        "version": "1.0.0",
        "created_at": T0,
        "repro": repro_tuple(),
    }
    payload.update(overrides)
    return ExperimentSpec(**payload)  # type: ignore[arg-type]


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
        entries=(SelectionEntry(key=selection_key(), profile=profile_ref()),),
    )


def gate_result(
    verdict: Verdict = Verdict.PASS, *, gate_id: str = "G0", with_threshold: bool = True
) -> GateResult:
    return GateResult(
        gate_id=gate_id,
        metric="reproducible",
        value=1.0,
        threshold=1.0 if with_threshold else None,
        threshold_source="significance.multiple_testing_threshold" if with_threshold else None,
        verdict=verdict,
    )


def validation_report(verdict: Verdict = Verdict.PASS, **overrides: object) -> ValidationReport:
    payload: dict[str, object] = {
        "report_id": "rep-1",
        "run_id": "run-1",
        "subject": strategy_ref(),
        "experiment_hash": HASH_EXPERIMENT,
        "constitution_version": "0.2.0-draft",
        "validation_profile": profile_ref(),
        "validation_profile_hash": HASH_PROFILE,
        "gates": (gate_result(verdict),),
        "verdict": verdict,
    }
    payload.update(overrides)
    return ValidationReport(**payload)  # type: ignore[arg-type]


def experiment_metadata(**overrides: object) -> ExperimentMetadata:
    """每个实验的规则绑定记录（ADR-0015 §D-22.2、§D-22.3 起用引用 + 完整选择依据）。"""
    payload: dict[str, object] = {
        "experiment_hash": HASH_EXPERIMENT,
        "constitution_version": "0.2.0-draft",
        "validation_profile": profile_ref(),
        "validation_profile_hash": HASH_PROFILE,
        "profile_selection": profile_selection(),
        "hypothesis_family_id": "family-1",
        "trial_index": 1,
        "family_trial_count": 1,
        "declared_research_class": "swing",
    }
    payload.update(overrides)
    return ExperimentMetadata(**payload)  # type: ignore[arg-type]


def experiment_run(**overrides: object) -> ExperimentRun:
    payload: dict[str, object] = {
        "run_id": "run-1",
        "experiment": Ref(kind=Kind.EXPERIMENT, name="e_example", version="1.0.0"),
        "repro": repro_tuple(),
    }
    payload.update(overrides)
    return ExperimentRun(**payload)  # type: ignore[arg-type]


def golden_outputs(**overrides: object) -> GoldenOutputs:
    payload: dict[str, object] = {
        "dataset_snapshot_id": "snap-1",
        "signals_uri": "s3://bucket/signals",
        "signals_hash": HASH_A,
        "positions_uri": "s3://bucket/positions",
        "positions_hash": HASH_B,
    }
    payload.update(overrides)
    return GoldenOutputs(**payload)  # type: ignore[arg-type]


def strategy_artifact(**overrides: object) -> StrategyArtifact:
    spec_ref = overrides.get("strategy_spec", strategy_ref())
    assert isinstance(spec_ref, Ref)
    payload: dict[str, object] = {
        "name": "a_example",
        "version": "1.0.0",
        "created_at": T0,
        "strategy_spec": spec_ref,
        "dependencies": dependency_hashes(spec_ref),
        "research_code_commit": GIT_COMMIT_OID,
        "research_code_tree_hash": GIT_TREE_OID,
        "experiment_hashes": (HASH_EXPERIMENT,),
        "validation_reports": ("rep-1",),
        "golden_outputs": golden_outputs(),
    }
    payload.update(overrides)
    return StrategyArtifact(**payload)  # type: ignore[arg-type]


def git_code_revision(**overrides: object) -> GitCodeRevision:
    """生产代码修订：commit + tree 的结构化身份（ADR-0015 §D-21.2）。"""
    payload: dict[str, object] = {"commit_oid": GIT_COMMIT_OID, "tree_oid": GIT_TREE_OID}
    payload.update(overrides)
    return GitCodeRevision(**payload)  # type: ignore[arg-type]


def equivalence_check(**overrides: object) -> EquivalenceCheck:
    payload: dict[str, object] = {
        "artifact_id": HASH_C,
        "production_code_hash": git_code_revision(),
        "signals_match": True,
        "positions_match": True,
    }
    payload.update(overrides)
    return EquivalenceCheck(**payload)  # type: ignore[arg-type]


def deployment_record(**overrides: object) -> DeploymentRecord:
    """默认构造一份**自洽**的部署记录：与 Equivalence 检查的身份完全一致。"""
    equivalence = overrides.pop("equivalence", equivalence_check())
    assert isinstance(equivalence, EquivalenceCheck)
    payload: dict[str, object] = {
        "deployment_id": "deploy-1",
        "artifact_id": equivalence.artifact_id,
        "production_code_hash": equivalence.production_code_hash,
        "config_hash": HASH_CONFIG,
        "equivalence": equivalence,
    }
    payload.update(overrides)
    return DeploymentRecord(**payload)  # type: ignore[arg-type]


def lifecycle_history(**overrides: object) -> LifecycleHistory:
    payload: dict[str, object] = {"subject": strategy_ref(), "transitions": ()}
    payload.update(overrides)
    return LifecycleHistory(**payload)  # type: ignore[arg-type]


def frozen_profile() -> ValidationProfile:
    return validation_profile(
        status="frozen",
        provenance=Provenance(calibration_report="calib-1", approval_adr="ADR-XXXX"),
    )

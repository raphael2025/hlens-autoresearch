"""契约注册表与 JSON Schema 导出（02-domain.md §3、roadmap Phase 0）。

注册表是"所有核心实体都有契约与 Schema 导出"这一验收标准的唯一来源：
新增核心实体必须登记到 `CONTRACT_MODELS`，否则契约测试会失败。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from core.contracts.catalog import (
    CommitRequest,
    CommitResult,
    SnapshotInfo,
    TableDefinition,
    TableInfo,
)
from core.contracts.collector import (
    CollectedObject,
    CollectionRequest,
    CollectionResult,
    CollectorDescriptor,
    CoverageGap,
    SourceBinding,
)
from core.contracts.cost_model import CostModelSpec
from core.contracts.event import (
    Event,
    EventInputPoint,
    EventProviderDescriptor,
    EventRequest,
    EventResult,
)
from core.contracts.event_bus import BusMessage
from core.contracts.feature import (
    FeatureObservation,
    FeatureRequest,
    FeatureResult,
    FeatureValue,
    ProviderDescriptor,
)
from core.contracts.knowledge import (
    KnowledgeProviderDescriptor,
    KnowledgeQuery,
    KnowledgeResult,
)
from core.contracts.llm import LlmProviderDescriptor, LlmRequest, LlmResponse
from core.contracts.loop_audit import (
    LoopBudgetLimits,
    LoopBudgetUsage,
    LoopOverrun,
    LoopRoundRecord,
    LoopRoundRecorded,
    LoopRoundStarted,
    LoopStageRecord,
    LoopTransitionRecord,
)
from core.contracts.outcome import (
    OutcomeEvent,
    OutcomeLabel,
    OutcomeLabelSpec,
    OutcomePriceBar,
    OutcomeProviderDescriptor,
    OutcomeRequest,
    OutcomeResult,
)
from core.contracts.profile_selection import (
    ExperimentMetadata,
    OosUnsealing,
    ProfileSelection,
    ProfileSelectionKey,
    ProfileSelectionRule,
    SelectionEntry,
)
from core.contracts.revision import (
    AvailabilityDecision,
    ObservationTimes,
    PointInTimeSelection,
    PointInTimeSpec,
    PolicyBinding,
    PrecedenceEvidence,
    RevisionGraph,
    RevisionRecord,
)
from core.contracts.state import (
    StateInput,
    StateProviderDescriptor,
    StateRequest,
    StateResult,
    StateValue,
)
from core.contracts.storage import ObjectRef, PublishResult, StagedObject, StageRequest
from core.contracts.strategy import (
    BacktestCostModel,
    BacktestProviderDescriptor,
    BacktestRequest,
    BacktestResult,
    ConstrainedPosition,
    EquityPoint,
    Fill,
    FillRemainder,
    PortfolioState,
    PriceBar,
    RiskProviderDescriptor,
    RiskRequest,
    RiskResult,
    SignalObservation,
    StrategyProviderDescriptor,
    StrategyRequest,
    StrategyResult,
    TargetPosition,
)
from core.contracts.synthetic import (
    JumpEffect,
    PlantedEffect,
    SyntheticBar,
    SyntheticMarket,
    SyntheticMarketSpec,
    SyntheticProviderDescriptor,
    VolatilityClusteringEffect,
)
from core.contracts.universe import (
    AvailabilityEvidenceGap,
    DatasetChunkProof,
    DatasetQualityReportRef,
    DatasetRuleBinding,
    DegradedEpisodeKey,
    EvidenceObjectRef,
    EvidenceStreamRef,
    ListingHistory,
    ListingRevision,
    PitConflictEvidenceResult,
    PitConflictHeadEvidence,
    ResearchDatasetEvidenceManifest,
    ResearchDatasetManifest,
    SelectedRevisionLineage,
    StableEpisodeKey,
    TradableInterval,
    UniverseExclusion,
    UniverseFilter,
    UniverseMember,
    UniverseSelectionSpec,
    UniverseSpecBinding,
)
from core.contracts.validation_profile import ValidationProfile
from core.domain.artifact import (
    DeploymentRecord,
    EquivalenceCheck,
    GoldenOutputs,
    StrategyArtifact,
)
from core.domain.base import ContentBlobRef, GitCodeRevision, Ref
from core.domain.research import (
    ExperimentRun,
    ExperimentSpec,
    FailureRecord,
    GateResult,
    Hypothesis,
    KnowledgeItem,
    LlmCall,
    ReproducibilityTuple,
    RetirementRecord,
    ValidationReport,
)
from core.domain.specs import (
    ConditionedStrategy,
    DatasetRef,
    EnsembleStrategy,
    EventSpec,
    FeatureSpec,
    Instrument,
    NegatedStrategy,
    OutcomeSpec,
    RepresentationSpec,
    RiskPolicy,
    StateSpec,
    StrategySpec,
)
from core.lifecycle.strategy import (
    AuthorizationRecord,
    ExecutionModeChange,
    LifecycleHistory,
    LifecycleTransition,
    RiskGateRecord,
)

if TYPE_CHECKING:
    from core.domain.base import Contract

__all__ = ["CONTRACT_MODELS", "export_json_schemas"]

#: 全部核心契约模型。顺序即导出顺序。
CONTRACT_MODELS: tuple[type[Contract], ...] = (
    # 基础
    Ref,
    GitCodeRevision,
    ContentBlobRef,
    Instrument,
    DatasetRef,
    # 研究对象规格
    RepresentationSpec,
    FeatureSpec,
    StateSpec,
    EventSpec,
    OutcomeSpec,
    StrategySpec,
    RiskPolicy,
    # 知识与假设
    KnowledgeItem,
    Hypothesis,
    # 实验
    LlmCall,
    ReproducibilityTuple,
    ExperimentSpec,
    ExperimentRun,
    # 验证与终态记录
    GateResult,
    ValidationReport,
    FailureRecord,
    RetirementRecord,
    # 三层验证架构
    ValidationProfile,
    ProfileSelectionKey,
    ProfileSelection,
    SelectionEntry,
    ProfileSelectionRule,
    OosUnsealing,
    ExperimentMetadata,
    # 生命周期
    LifecycleTransition,
    LifecycleHistory,
    RiskGateRecord,
    AuthorizationRecord,
    ExecutionModeChange,
    # 研究 / 生产边界
    GoldenOutputs,
    StrategyArtifact,
    EquivalenceCheck,
    DeploymentRecord,
    # 双时间与 revision DAG（ADR-0023，Phase 1 B1）
    PolicyBinding,
    ObservationTimes,
    AvailabilityDecision,
    RevisionRecord,
    PrecedenceEvidence,
    RevisionGraph,
    PointInTimeSpec,
    PointInTimeSelection,
    # 历史可交易 universe 与 Research Dataset manifest（ADR-0024 / ADR-0023 §6，Phase 1 B2）
    TradableInterval,
    StableEpisodeKey,
    DegradedEpisodeKey,
    ListingRevision,
    ListingHistory,
    UniverseFilter,
    UniverseSelectionSpec,
    UniverseSpecBinding,
    UniverseMember,
    UniverseExclusion,
    SelectedRevisionLineage,
    AvailabilityEvidenceGap,
    ResearchDatasetManifest,
    # Data Plane Adapter 的 DTO（ADR-0017 / ADR-0021 / ADR-0022，Phase 1 B3）
    StageRequest,
    StagedObject,
    ObjectRef,
    PublishResult,
    TableDefinition,
    SnapshotInfo,
    TableInfo,
    CommitRequest,
    CommitResult,
    SourceBinding,
    CollectorDescriptor,
    CollectionRequest,
    CollectedObject,
    CoverageGap,
    CollectionResult,
    # FeatureProvider 的 DTO（ADR-0030，Phase 1 F4）
    FeatureObservation,
    FeatureRequest,
    FeatureValue,
    FeatureResult,
    ProviderDescriptor,
    # KnowledgeProvider 的 DTO（ADR-0034，Phase 0.5）
    KnowledgeQuery,
    KnowledgeProviderDescriptor,
    KnowledgeResult,
    # SyntheticMarketProvider 的 DTO（ADR-0042，Phase 9）
    PlantedEffect,
    SyntheticMarketSpec,
    SyntheticBar,
    SyntheticMarket,
    SyntheticProviderDescriptor,
    # EventBusAdapter 的消息（ADR-0044，Phase 11 地基）
    BusMessage,
    # LLMProvider 的 DTO（ADR-0040，Phase 7）
    LlmRequest,
    LlmResponse,
    LlmProviderDescriptor,
    # StateProvider 的 DTO（ADR-0035，Phase 2）
    StateInput,
    StateRequest,
    StateValue,
    StateResult,
    StateProviderDescriptor,
    # Strategy / Risk / Backtest Provider 的 DTO（ADR-0038，Phase 5）
    SignalObservation,
    StrategyRequest,
    TargetPosition,
    StrategyResult,
    StrategyProviderDescriptor,
    PortfolioState,
    RiskRequest,
    ConstrainedPosition,
    RiskResult,
    RiskProviderDescriptor,
    BacktestCostModel,
    PriceBar,
    BacktestRequest,
    Fill,
    EquityPoint,
    BacktestResult,
    BacktestProviderDescriptor,
    # EventProvider 的 DTO（ADR-0036，Phase 3）
    EventInputPoint,
    Event,
    EventRequest,
    EventResult,
    EventProviderDescriptor,
    # OutcomeProvider 的 DTO 与成本模型 v1（ADR-0037，Phase 4）
    OutcomeLabelSpec,
    OutcomePriceBar,
    OutcomeEvent,
    OutcomeRequest,
    OutcomeLabel,
    OutcomeProviderDescriptor,
    OutcomeResult,
    CostModelSpec,
    # 持续研究循环的审计记录（ADR-0050，Phase 11；描述既有字节，只追加）
    LoopBudgetUsage,
    LoopBudgetLimits,
    LoopStageRecord,
    LoopTransitionRecord,
    LoopOverrun,
    LoopRoundRecord,
    LoopRoundStarted,
    LoopRoundRecorded,
    # 回测剩余量跨 bar 结转（ADR-0054；additive，只追加）
    FillRemainder,
    # 有界 Research Dataset evidence manifest（ADR-0077，契约 2.3.0；additive，只追加）
    DatasetRuleBinding,
    EvidenceObjectRef,
    EvidenceStreamRef,
    DatasetQualityReportRef,
    DatasetChunkProof,
    ResearchDatasetEvidenceManifest,
    # 组合策略与新合成效应（ADR-0088，契约 2.4.0；additive，只追加）
    ConditionedStrategy,
    EnsembleStrategy,
    NegatedStrategy,
    VolatilityClusteringEffect,
    JumpEffect,
    # 有界 PIT 冲突 heads 证据（ADR-0094，契约 2.5.0；additive，只追加）
    PitConflictHeadEvidence,
    PitConflictEvidenceResult,
)


def export_json_schemas(out_dir: Path) -> dict[str, Path]:
    """把全部契约导出为 JSON Schema，返回 {模型名: 文件路径}。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for model in CONTRACT_MODELS:
        schema = model.model_json_schema(mode="serialization")
        path = out_dir / f"{model.__name__}.schema.json"
        path.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written[model.__name__] = path
    return written


if __name__ == "__main__":  # pragma: no cover - 手动导出入口
    target = Path(__file__).resolve().parents[2] / "schemas"
    files = export_json_schemas(target)
    print(f"exported {len(files)} schemas to {target}")

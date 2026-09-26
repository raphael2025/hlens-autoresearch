"""Continuous research loop, research side (Phase 11, ADR-0049): stages, memory, composition."""

from research.loop.compose import (
    DurableLoop,
    LoopSettings,
    LoopWiring,
    SyntheticLoopConfig,
    build_synthetic_loop,
    check_round_bus,
    compose_loop,
    loop_fingerprint,
    open_synthetic_loop,
    run_unattended_and_report,
)
from research.loop.dataset_compose import (
    DatasetLoopConfig,
    build_dataset_loop,
    dataset_loop_fingerprint,
    open_dataset_loop,
)
from research.loop.dataset_source import (
    DatasetCatalog,
    DatasetIngestStage,
    DatasetRound,
    DatasetRoundRefused,
    DatasetSegment,
)
from research.loop.durable import FileAnchor, LoopStateInconsistent, StateAnchor, StateHead
from research.loop.evolution import EvolutionPlan, EvolutionStage
from research.loop.memory import ResearchMemory, ReviewApproval, ReviewQueue
from research.loop.segment import RoundData
from research.loop.stages import (
    HypothesisStage,
    IngestStage,
    MemoryStage,
    StateStage,
)
from research.loop.trials import (
    ExperimentStage,
    OosUnsealBudget,
    TrialComponents,
    TrialOutcome,
    ValidationOutcome,
    ValidationStage,
)

__all__ = [
    "DatasetCatalog",
    "DatasetIngestStage",
    "DatasetLoopConfig",
    "DatasetRound",
    "DatasetRoundRefused",
    "DatasetSegment",
    "DurableLoop",
    "EvolutionPlan",
    "EvolutionStage",
    "ExperimentStage",
    "FileAnchor",
    "HypothesisStage",
    "IngestStage",
    "LoopSettings",
    "LoopStateInconsistent",
    "LoopWiring",
    "MemoryStage",
    "OosUnsealBudget",
    "ResearchMemory",
    "ReviewApproval",
    "ReviewQueue",
    "RoundData",
    "StateAnchor",
    "StateHead",
    "StateStage",
    "SyntheticLoopConfig",
    "TrialComponents",
    "TrialOutcome",
    "ValidationOutcome",
    "ValidationStage",
    "build_dataset_loop",
    "build_synthetic_loop",
    "check_round_bus",
    "compose_loop",
    "dataset_loop_fingerprint",
    "loop_fingerprint",
    "open_dataset_loop",
    "open_synthetic_loop",
    "run_unattended_and_report",
]

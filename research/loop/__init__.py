"""Continuous research loop, research side (Phase 11, ADR-0049): stages, memory, composition."""

from research.loop.compose import (
    LoopWiring,
    SyntheticLoopConfig,
    build_synthetic_loop,
    run_unattended_and_report,
)
from research.loop.evolution import EvolutionPlan, EvolutionStage
from research.loop.memory import ResearchMemory, ReviewApproval, ReviewQueue
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
    "EvolutionPlan",
    "EvolutionStage",
    "ExperimentStage",
    "HypothesisStage",
    "IngestStage",
    "LoopWiring",
    "MemoryStage",
    "OosUnsealBudget",
    "ResearchMemory",
    "ReviewApproval",
    "ReviewQueue",
    "StateStage",
    "SyntheticLoopConfig",
    "TrialComponents",
    "TrialOutcome",
    "ValidationOutcome",
    "ValidationStage",
    "build_synthetic_loop",
    "run_unattended_and_report",
]

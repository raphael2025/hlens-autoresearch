"""Continuous research loop, research side (Phase 11, ADR-0049): stages, memory, composition."""

from research.loop.compose import SyntheticLoopConfig, build_synthetic_loop
from research.loop.memory import ResearchMemory, ReviewQueue
from research.loop.stages import (
    ExperimentStage,
    HypothesisStage,
    IngestStage,
    MemoryStage,
    StateStage,
    ValidationStage,
)

__all__ = [
    "ExperimentStage",
    "HypothesisStage",
    "IngestStage",
    "MemoryStage",
    "ResearchMemory",
    "ReviewQueue",
    "StateStage",
    "SyntheticLoopConfig",
    "ValidationStage",
    "build_synthetic_loop",
]

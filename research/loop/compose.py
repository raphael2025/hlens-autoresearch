"""Research-side composition root of the continuous loop (Phase 11; ADR-0049).

``build_synthetic_loop`` wires the research stages of ``research/loop/stages.py`` into the generic
``apps.worker.loop.ResearchLoop``. The dependency points research → apps/worker (the worker is the
runtime host and exposes the stage Protocol); apps/ never imports research/ (01-system.md §3).
Every number comes from ``SyntheticLoopConfig``; there are no defaults for budgets or thresholds.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from apps.worker.loop import LoopBudget, ResearchLoop
from core.contracts.event_bus import EventBusAdapter
from core.contracts.llm import LLMProvider
from core.contracts.synthetic import SyntheticMarketProvider, SyntheticMarketSpec
from core.contracts.validation_profile import ValidationProfile
from core.domain.research import KnowledgeItem
from research.loop.memory import ResearchMemory
from research.loop.stages import (
    ExperimentStage,
    HypothesisStage,
    IngestStage,
    MemoryStage,
    StateStage,
    ValidationStage,
)

__all__ = ["SyntheticLoopConfig", "build_synthetic_loop"]


@dataclass(frozen=True, slots=True)
class SyntheticLoopConfig:
    loop_id: str
    seed: int
    epoch: datetime
    cadence: timedelta
    budget: LoopBudget
    market: SyntheticMarketSpec
    minutes_per_round: int
    compute_seconds_per_bar: Decimal
    state_window: int
    trend_threshold: Decimal
    family_id: str
    knowledge: Sequence[KnowledgeItem]
    max_new_hypotheses_per_round: int
    hypothesis_compute_seconds: Decimal
    compute_seconds_per_trial: Decimal
    validation_compute_seconds: Decimal
    state_compute_seconds: Decimal
    profile: ValidationProfile
    constitution_version: str
    llm_prompt: str | None = None
    llm_cost_units_per_call: Decimal = Decimal(0)


def build_synthetic_loop(
    config: SyntheticLoopConfig,
    *,
    provider: SyntheticMarketProvider,
    bus: EventBusAdapter,
    memory: ResearchMemory,
    llm: LLMProvider | None = None,
) -> ResearchLoop:
    stages = (
        IngestStage(
            provider,
            config.market,
            minutes_per_round=config.minutes_per_round,
            compute_seconds_per_bar=config.compute_seconds_per_bar,
        ),
        StateStage(
            memory,
            window=config.state_window,
            trend_threshold=config.trend_threshold,
            compute_seconds=config.state_compute_seconds,
        ),
        HypothesisStage(
            memory,
            family_id=config.family_id,
            knowledge=config.knowledge,
            max_new_per_round=config.max_new_hypotheses_per_round,
            compute_seconds=config.hypothesis_compute_seconds,
            llm=llm,
            llm_prompt=config.llm_prompt if llm is not None else None,
            llm_cost_units_per_call=config.llm_cost_units_per_call,
        ),
        ExperimentStage(memory, compute_seconds_per_trial=config.compute_seconds_per_trial),
        ValidationStage(
            memory,
            config.profile,
            constitution_version=config.constitution_version,
            compute_seconds=config.validation_compute_seconds,
        ),
        MemoryStage(memory),
    )
    return ResearchLoop(
        loop_id=config.loop_id,
        stages=stages,
        budget=config.budget,
        bus=bus,
        seed=config.seed,
        epoch=config.epoch,
        cadence=config.cadence,
    )

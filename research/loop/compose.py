"""Research-side composition root of the continuous loop (Phase 11; ADR-0049).

``build_synthetic_loop`` wires the research stages of ``research/loop/stages.py`` into the generic
``apps.worker.loop.ResearchLoop``. The dependency points research → apps/worker (the worker is the
runtime host and exposes the stage Protocol); apps/ never imports research/ (01-system.md §3).
Every number comes from ``SyntheticLoopConfig``; there are no defaults for budgets or thresholds.

``run_unattended_and_report`` optionally feeds each round's audit record to the research console
(ADR-0048): a thin wrapper around ``ResearchLoop.run_unattended`` that also writes every
``LoopRecord`` it produces to a report root via ``research.reports.write_research_loop_round``,
when one is given. Nothing here changes what a round does or its content hash; the report root is
purely an additional, optional sink for the same records the loop already returns.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from apps.worker.loop import LoopBudget, LoopRecord, ResearchLoop
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
from research.reports import write_research_loop_rounds

__all__ = ["SyntheticLoopConfig", "build_synthetic_loop", "run_unattended_and_report"]


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


def run_unattended_and_report(
    loop: ResearchLoop, rounds: int, *, reports_root: Path | None = None
) -> tuple[LoopRecord, ...]:
    """``loop.run_unattended(rounds)``, also writing every record when ``reports_root`` is given.

    ``reports_root is None`` (the default) behaves exactly like calling ``run_unattended``
    directly: no filesystem write happens. When set, every ``LoopRecord`` the loop produces is
    also written to ``<reports_root>/research_loop_round/<record_hash>.json`` (append-only;
    re-running the same rounds under the same seed is a no-op, see
    ``research.reports.write_research_loop_round``).
    """
    records = loop.run_unattended(rounds)
    if reports_root is not None:
        write_research_loop_rounds(reports_root, records)
    return records

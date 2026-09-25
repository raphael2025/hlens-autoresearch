"""Research-side composition root of the continuous loop (Phase 11; ADR-0049).

``build_synthetic_loop`` wires the research stages of ``research/loop/`` into the generic
``apps.worker.loop.ResearchLoop``. The dependency points research → apps/worker (the worker is the
runtime host and exposes the stage Protocol); apps/ never imports research/ (01-system.md §3).
Every number comes from ``SyntheticLoopConfig`` / ``LoopWiring``; there are no defaults for budgets
or thresholds (validation thresholds live in the bound ``ValidationProfile`` only).

``LoopWiring`` (W2) carries the real components the stages run on: the F4 feature and P2 state
providers, the strategy catalog (P5 candidates), backtester, cost model, outcome provider and label
spec, the G4 explicit parameters, the reproducibility bindings (Profile selection, code commit,
environment lock), the optional evolution plan (P12) and the optional sealed-OOS unseal budget —
without one the sealed window stays sealed.

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

from apps.worker.loop import LoopBudget, LoopRecord, LoopStage, ResearchLoop
from core.contracts.cost_model import CostModelSpec
from core.contracts.event_bus import EventBusAdapter
from core.contracts.feature import FeatureProvider
from core.contracts.llm import LLMProvider
from core.contracts.outcome import OutcomeLabelSpec, OutcomeProvider
from core.contracts.state import StateProvider
from core.contracts.strategy import BacktestProvider
from core.contracts.synthetic import SyntheticMarketProvider, SyntheticMarketSpec
from core.contracts.validation_profile import ValidationProfile
from core.domain.research import KnowledgeItem
from core.domain.selection import ProfileSelection
from core.domain.specs import FeatureSpec, StateSpec
from research.loop.memory import ResearchMemory
from research.loop.stages import (
    EvolutionPlan,
    EvolutionStage,
    ExperimentStage,
    HypothesisStage,
    IngestStage,
    MemoryStage,
    OosUnsealBudget,
    StateStage,
    TrialComponents,
    ValidationStage,
)
from research.reports import write_research_loop_rounds
from research.strategies.pipeline import StrategyCandidate
from research.validation import RobustnessParams

__all__ = [
    "LoopWiring",
    "SyntheticLoopConfig",
    "build_synthetic_loop",
    "run_unattended_and_report",
]


@dataclass(frozen=True)
class LoopWiring:
    """The real Phase 1 / 2 / 4 / 5 / 6 / 8 / 12 components of the stages (see module docs)."""

    feature_provider: FeatureProvider
    feature_spec: FeatureSpec
    feature_chunk_bars: int
    state_provider: StateProvider
    state_spec: StateSpec
    decision_step: timedelta
    decision_warmup: timedelta
    strategies: Sequence[StrategyCandidate]
    backtester: BacktestProvider
    cost_model: CostModelSpec
    initial_equity: Decimal
    outcome_provider: OutcomeProvider
    label_spec: OutcomeLabelSpec
    robustness: RobustnessParams
    profile_selection: ProfileSelection
    declared_research_class: str
    code_commit: str
    environment_lock: str
    evolution: EvolutionPlan | None
    #: ``None`` (the default): the sealed OOS window is never unsealed by the loop. Otherwise only
    #: the families it lists (each with its approving human) may be unsealed.
    oos_unseal: OosUnsealBudget | None = None
    sealed_decision_step: timedelta | None = None


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
    wiring: LoopWiring
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
    wiring = config.wiring
    for candidate in wiring.strategies:
        memory.add_strategy(candidate)
    components = TrialComponents(
        backtester=wiring.backtester,
        cost_model=wiring.cost_model,
        initial_equity=wiring.initial_equity,
        outcome_provider=wiring.outcome_provider,
        label_spec=wiring.label_spec,
        robustness=wiring.robustness,
        profile=config.profile,
        profile_selection=wiring.profile_selection,
        constitution_version=config.constitution_version,
        declared_research_class=wiring.declared_research_class,
        code_commit=wiring.code_commit,
        environment_lock=wiring.environment_lock,
    )
    evolution: tuple[LoopStage, ...] = (
        () if wiring.evolution is None else (EvolutionStage(memory, wiring.evolution),)
    )
    stages: tuple[LoopStage, ...] = (
        IngestStage(
            provider,
            config.market,
            config.profile,
            minutes_per_round=config.minutes_per_round,
            compute_seconds_per_bar=config.compute_seconds_per_bar,
            decision_step=wiring.decision_step,
            decision_warmup=wiring.decision_warmup,
            label_horizon=wiring.label_spec.horizon,
        ),
        StateStage(
            memory,
            feature_provider=wiring.feature_provider,
            feature_spec=wiring.feature_spec,
            state_provider=wiring.state_provider,
            state_spec=wiring.state_spec,
            feature_chunk_bars=wiring.feature_chunk_bars,
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
        *evolution,
        ExperimentStage(
            memory, components, compute_seconds_per_trial=config.compute_seconds_per_trial
        ),
        ValidationStage(
            memory,
            components,
            compute_seconds_per_validation=config.validation_compute_seconds,
            oos_unseal=wiring.oos_unseal,
            sealed_decision_step=wiring.sealed_decision_step,
        ),
        MemoryStage(memory),
    )
    loop = ResearchLoop(
        loop_id=config.loop_id,
        stages=stages,
        budget=config.budget,
        bus=bus,
        seed=config.seed,
        epoch=config.epoch,
        cadence=config.cadence,
    )
    memory.reviews.bind_loop_actor(loop.guard.actor)  # the loop can never approve its own drafts
    return loop


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

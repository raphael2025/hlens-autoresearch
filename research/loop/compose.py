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

Durable composition (ADR-0049 implementation note, durable composition, 2026-09-26):
``open_synthetic_loop(config, state_dir=...)`` (or ``build_synthetic_loop(..., state_dir=...)``)
keeps the audit and every part of ``ResearchMemory`` the stages read across rounds under one
directory, restores them on reopening and refuses to start when they disagree
(``research.loop.durable``). Without a state directory nothing changes: all in memory, same record
hashes (a restarted durable run reproduces the uninterrupted run's hashes as well).

Durable review fixes (ADR-0049 implementation note, 2026-09-26): the configuration fingerprint
binds the ``LoopBudget``, the whole ``OosUnsealBudget`` (``max_unsealings``, approved families and
approvers) and the exact cadence (microseconds), so a directory reopened with any other budget or
unseal quota is refused — raising a budget is a human decision and takes a new ``state_dir`` or
``loop_id``. ``anchor=`` (a path outside ``state_dir`` or any ``StateAnchor``) keeps the
directory's head after every recorded round and refuses a directory that was rolled back or
diverged (``research.loop.durable``, **External anchor**).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from os import PathLike
from pathlib import Path
from typing import Any

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
from research.loop.durable import DurableState, FileAnchor, StateAnchor, open_state
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
    "DurableLoop",
    "LoopWiring",
    "SyntheticLoopConfig",
    "build_synthetic_loop",
    "loop_fingerprint",
    "open_synthetic_loop",
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
    #: Still-open (VALIDATION / INCONCLUSIVE) hypotheses re-evaluated per round on the grown
    #: accumulated research data, each as a new registered trial (0: never re-evaluate).
    max_reevaluations_per_round: int
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
    memory: ResearchMemory | None = None,
    llm: LLMProvider | None = None,
    state_dir: Path | None = None,
    anchor: StateAnchor | Path | None = None,
) -> ResearchLoop:
    """Compose the loop over ``memory`` (in memory), or over ``state_dir`` (durable).

    Exactly one of ``memory`` / ``state_dir``. ``state_dir`` is ``open_synthetic_loop(...).loop``;
    use ``open_synthetic_loop`` directly when the caller needs the restored memory (e.g. to
    approve LLM drafts between rounds). ``anchor`` only with ``state_dir``.
    """
    if (memory is None) == (state_dir is None):
        raise ValueError("pass exactly one of memory (in memory) or state_dir (durable)")
    if state_dir is not None:
        return open_synthetic_loop(
            config, state_dir=state_dir, provider=provider, bus=bus, llm=llm, anchor=anchor
        ).loop
    if anchor is not None:
        raise ValueError("an anchor keeps a state directory's head: it needs state_dir")
    assert memory is not None
    return _compose(config, provider, bus, memory, llm, None)


def _compose(
    config: SyntheticLoopConfig,
    provider: SyntheticMarketProvider,
    bus: EventBusAdapter,
    memory: ResearchMemory,
    llm: LLMProvider | None,
    state: DurableState | None,
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
            memory,
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
            max_reevaluations_per_round=config.max_reevaluations_per_round,
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
        audit=None if state is None else state.audit,
        checkpoint=None if state is None else state.checkpoint,
        after_record=None if state is None or state.anchor is None else state.publish_anchor,
    )
    memory.reviews.bind_loop_actor(loop.guard.actor)  # the loop can never approve its own drafts
    if state is not None:
        state.verify_guard(loop.guard)
        state.publish_anchor()  # every check passed: the anchor catches up with the directory
    return loop


@dataclass(frozen=True)
class DurableLoop:
    """A loop composed over a state directory, with the research memory restored from it."""

    loop: ResearchLoop
    memory: ResearchMemory
    state_dir: Path


def open_synthetic_loop(
    config: SyntheticLoopConfig,
    *,
    state_dir: Path,
    provider: SyntheticMarketProvider,
    bus: EventBusAdapter,
    llm: LLMProvider | None = None,
    anchor: StateAnchor | Path | None = None,
) -> DurableLoop:
    """Compose the loop over ``state_dir`` (created when missing), restoring every stateful part.

    The audit, trial ledger, sealed-OOS unsealing ledger, lineage, failure registry, review queue
    and the per-round memory checkpoint all live under ``state_dir``
    (``research.loop.durable``). Reopening restores them, cross-checks them against each other and
    against this configuration, and refuses (``LoopStateInconsistent``) on any disagreement; the
    loop then continues after the last recorded round. ``llm`` is external: resuming its own state
    (e.g. a scripted provider's position) is the caller's job. Human review approvals go through
    ``DurableLoop.memory.reviews.approve`` (journaled).

    The budgets are part of the configuration: reopening with another ``LoopBudget`` or
    ``OosUnsealBudget`` (a larger quota, another approved family or approver — or a smaller one)
    is refused; raising a budget is a human decision and takes a new ``state_dir`` or ``loop_id``.
    ``anchor``: an optional external anchor — a path **outside** ``state_dir`` (a ``FileAnchor``)
    or any ``StateAnchor``; it receives the directory's head after every recorded round, and a
    reopened directory behind it (rolled back) or diverged from it is refused. Without it a
    consistent truncation of every file to an earlier round boundary is not detectable (the
    documented limit of ``research.loop.durable``).
    """
    wiring = config.wiring
    state = open_state(
        state_dir,
        fingerprint=loop_fingerprint(config),
        strategies=wiring.strategies,
        provider=provider,
        provider_for=None if wiring.evolution is None else wiring.evolution.provider_for,
        anchor=FileAnchor(anchor) if isinstance(anchor, str | PathLike) else anchor,
    )
    loop = _compose(config, provider, bus, state.memory, llm, state)
    return DurableLoop(loop=loop, memory=state.memory, state_dir=state.root)


def _unseal_payload(budget: OosUnsealBudget | None) -> dict[str, Any] | None:
    if budget is None:
        return None
    return {
        "max_unsealings": budget.max_unsealings,
        "approved_families": dict(sorted(budget.approved_families.items())),
    }


def loop_fingerprint(config: SyntheticLoopConfig) -> dict[str, Any]:
    """What a restored research memory depends on (the header of a state directory).

    Includes the budgets (the ``LoopBudget`` payload and the whole ``OosUnsealBudget``): a state
    directory is bound to the budgets it was opened with, and changing them on reopening is
    refused (a new budget is a human decision: a new ``state_dir`` / ``loop_id``). The cadence is
    exact (whole microseconds, ``timedelta``'s resolution). Compute declarations and the LLM
    provider are not part of it.
    """
    wiring = config.wiring
    return {
        "loop_id": config.loop_id,
        "seed": config.seed,
        "epoch": config.epoch.isoformat(),
        "cadence_microseconds": config.cadence // timedelta(microseconds=1),
        "budget": config.budget.payload(),
        "oos_unseal": _unseal_payload(wiring.oos_unseal),
        "family_id": config.family_id,
        "profile": config.profile.content_hash(),
        "constitution_version": config.constitution_version,
        "market": config.market.content_hash(),
        "minutes_per_round": config.minutes_per_round,
        "knowledge": [item.content_hash() for item in config.knowledge],
        "strategies": [c.spec.content_hash() for c in wiring.strategies],
        "feature_spec": wiring.feature_spec.content_hash(),
        "state_spec": wiring.state_spec.content_hash(),
        "label_spec": wiring.label_spec.content_hash(),
        "cost_model": wiring.cost_model.content_hash(),
        "code_commit": wiring.code_commit,
        "environment_lock": wiring.environment_lock,
        "evolution": wiring.evolution is not None,
    }


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

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

Durable unsealing (ADR-0049 implementation note, review fixes 4, 2026-09-26): an
``OosUnsealBudget`` runs only against a ``DurableUnsealingLedger`` — a ``state_dir``'s
``sealed_oos.jsonl``, or one the caller passes as ``ResearchMemory(oos_ledger=...)`` — because an
in-memory ledger forgets its unsealings on a restart (``ValidationStage`` refuses the
combination). The TEST-ONLY ``OosUnsealBudget(ephemeral_unseal_for_tests=True)`` is the one
exception for an in-memory loop; it is fingerprinted and marked in every G5 status it touches, and
a ``state_dir`` refuses it before writing anything.

Approvals between rounds (ADR-0049 implementation note, 2026-09-26): a human approval on the
restored memory (``DurableLoop.memory.reviews.approve``) is refused while a round runs and, once
journaled, immediately writes a between-rounds checkpoint line and moves the anchor; reopening
refuses an approval no such line names (``research.loop.durable``, **Approvals between rounds**).

Durable bus (ADR-0044 / ADR-0049 implementation notes, durable jobs and bus wiring, 2026-09-26):
with a ``state_dir`` and no ``bus`` the composition opens its own ``FileEventBus(state_dir /
"bus")`` (``DurableLoop.bus``; ``DurableLoop.close()`` or dropping the loop releases its lock) and,
before composing, cross-checks it against the verified audit (``check_round_bus``): the bus's
``research_loop.round`` messages must be exactly the audit's recorded rounds
(``apps.worker.loop.round_message``: same key, ``record_hash`` and record), in order. A bus
**ahead** of the audit (more messages, e.g. an audit rolled back) or holding any foreign message is
refused (``LoopStateInconsistent``). A bus **behind** by exactly the last recorded round is caught
up from the audit -- the only state a crash can leave (the loop publishes a round only after the
audit fsync'd it, stops when that publish fails, and the composition catches up before any new
round runs), and the message is a pure function of the verified record. Behind by more than one
round (a truncated or replaced bus, or one that never saw this directory) is refused.

Injected buses (ADR-0049 implementation note, review fixes 3, 2026-09-26): a bus the caller passes
with a ``state_dir`` is cross-checked exactly like the automatic one (same refusals, same one-round
catch-up) — it is never trusted unchecked; closing it stays the caller's job. The one exception is
an ``InMemoryEventBus``: it is **not a durable bus** (a new process always starts it empty), so
being behind is expected, not evidence of truncation. The audit's missing rounds are **replayed**
into it (every one, in order), so it too holds exactly the audit's rounds before the loop runs; a
foreign, reordered or ahead message is still refused. Any other bus type is treated as durable
(fail closed).

LLM content verification (Phase 7, 2026-09-26): whether ``llm`` is a
``research.loop.llm_content.ContentVerifiedLLM`` is part of a state directory's identity.
``open_synthetic_loop`` / ``open_dataset_loop`` add ``llm_content_verified: true`` to the
fingerprint in that case only (``llm_content_fingerprint``; every other fingerprint is
byte-identical to before), so a directory opened with verification is refused when reopened with a
plain LLM or none — the reviewed drafts it holds would otherwise be taken unverified — and a
directory opened without it is refused when reopened with it. ``compose_durable`` checks the
recorded header against its ``llm`` as well (a caller composing an ``open_state`` directly).
"""

from __future__ import annotations

import weakref
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from os import PathLike
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

from apps.worker.loop import (
    ROUND_TOPIC,
    LoopBudget,
    LoopRecord,
    LoopStage,
    ResearchLoop,
    round_message,
)
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
from infrastructure.event_bus import FileEventBus, InMemoryEventBus
from research.hypotheses import HypothesisBatch, KnowledgeSource
from research.loop.durable import (
    LOOP_STATE_OPENED,
    DurableState,
    FileAnchor,
    LoopStateInconsistent,
    StateAnchor,
    StateLock,
    open_state,
)
from research.loop.llm_content import ContentVerifiedLLM
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
from research.loop.trials import ConditionalPlan
from research.reports import write_research_loop_rounds, write_state_strategy_matrix
from research.strategies.pipeline import StrategyCandidate
from research.validation import RobustnessParams

__all__ = [
    "BUS_AUDIT_CONSUMER",
    "BUS_DIR",
    "DurableLoop",
    "LoopSettings",
    "LoopWiring",
    "SyntheticLoopConfig",
    "build_synthetic_loop",
    "check_round_bus",
    "compose_durable",
    "compose_loop",
    "llm_content_fingerprint",
    "loop_fingerprint",
    "open_synthetic_loop",
    "refuse_ephemeral_unseal",
    "run_unattended_and_report",
    "settings_fingerprint",
]


#: The composition's own bus, under the state directory (durable mode without a caller's bus).
BUS_DIR = "bus"
#: The consumer that reads ``research_loop.round`` to cross-check it; it never acknowledges, so
#: every check reads the whole topic from the start (other consumers are independent).
BUS_AUDIT_CONSUMER = "research_loop_bus_audit"
#: The fingerprint key of the LLM content-verification mode (present, ``True``, only when on).
LLM_CONTENT_VERIFIED = "llm_content_verified"


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
    #: ``None`` (the default): no conditional hypothesis is formed and every record, fingerprint
    #: and outcome is byte-identical to a loop without this field. A ``ConditionalPlan``: every
    #: cell of each trial's State × Strategy matrix is pre-registered as a trial of the trial's
    #: family (``research.loop.trials``, **Conditional hypotheses**); fingerprinted. With
    #: ``validate_cells=True`` the validation stage also runs in-sample G0 – G3 on every supported
    #: cell (**Per-cell validation**; no lifecycle move).
    conditional: ConditionalPlan | None = None
    #: ``None`` (the default): no batch, every record and fingerprint byte-identical. A declared
    #: ``HypothesisBatch`` (``research.hypotheses.batch``): pre-registered as a whole by the
    #: hypothesis stage the first round it runs (every cell a trial of the family); fingerprinted.
    hypothesis_batch: HypothesisBatch | None = None
    #: ``None`` (the default): no knowledge search, every record and fingerprint byte-identical. A
    #: ``KnowledgeSource`` (declared ``KnowledgeProvider`` + ``KnowledgeQuery``): searched once per
    #: round by the hypothesis stage, the query / result hashes recorded as the origin of the
    #: hypotheses it yields; the provider identity and the query are fingerprinted.
    knowledge_source: KnowledgeSource | None = None


class LoopSettings(Protocol):
    """What every composition of the loop binds, whatever its round data source.

    ``SyntheticLoopConfig`` (synthetic markets) and ``DatasetLoopConfig``
    (``research.loop.dataset_compose``, verified Research Dataset manifests) both provide these;
    only the ingest stage and the source's part of the fingerprint differ.
    """

    @property
    def loop_id(self) -> str: ...
    @property
    def seed(self) -> int: ...
    @property
    def epoch(self) -> datetime: ...
    @property
    def cadence(self) -> timedelta: ...
    @property
    def budget(self) -> LoopBudget: ...
    @property
    def wiring(self) -> LoopWiring: ...
    @property
    def family_id(self) -> str: ...
    @property
    def knowledge(self) -> Sequence[KnowledgeItem]: ...
    @property
    def max_new_hypotheses_per_round(self) -> int: ...
    @property
    def max_reevaluations_per_round(self) -> int: ...
    @property
    def hypothesis_compute_seconds(self) -> Decimal: ...
    @property
    def compute_seconds_per_trial(self) -> Decimal: ...
    @property
    def validation_compute_seconds(self) -> Decimal: ...
    @property
    def state_compute_seconds(self) -> Decimal: ...
    @property
    def profile(self) -> ValidationProfile: ...
    @property
    def constitution_version(self) -> str: ...
    @property
    def llm_prompt(self) -> str | None: ...
    @property
    def llm_cost_units_per_call(self) -> Decimal: ...


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
    bus: EventBusAdapter | None = None,
    memory: ResearchMemory | None = None,
    llm: LLMProvider | None = None,
    state_dir: Path | None = None,
    anchor: StateAnchor | Path | None = None,
) -> ResearchLoop:
    """Compose the loop over ``memory`` (in memory), or over ``state_dir`` (durable).

    Exactly one of ``memory`` / ``state_dir``. ``state_dir`` is ``open_synthetic_loop(...).loop``;
    use ``open_synthetic_loop`` directly when the caller needs the restored memory (e.g. to
    approve LLM drafts between rounds) or to close the automatic bus. ``anchor`` only with
    ``state_dir``. ``bus``: required in memory; with ``state_dir`` it may be omitted (the
    composition's own ``FileEventBus(state_dir / "bus")``, cross-checked against the audit; its
    lock is released when the returned loop is garbage-collected or the process exits).
    """
    if (memory is None) == (state_dir is None):
        raise ValueError("pass exactly one of memory (in memory) or state_dir (durable)")
    if state_dir is not None:
        return open_synthetic_loop(
            config, state_dir=state_dir, provider=provider, bus=bus, llm=llm, anchor=anchor
        ).loop
    if anchor is not None:
        raise ValueError("an anchor keeps a state directory's head: it needs state_dir")
    if bus is None:
        raise ValueError("an in-memory loop needs a bus (only a state_dir provides its own)")
    assert memory is not None
    return compose_loop(config, _synthetic_ingest(config, provider, memory), bus, memory, llm, None)


def _synthetic_ingest(
    config: SyntheticLoopConfig, provider: SyntheticMarketProvider, memory: ResearchMemory
) -> IngestStage:
    wiring = config.wiring
    return IngestStage(
        memory,
        provider,
        config.market,
        config.profile,
        minutes_per_round=config.minutes_per_round,
        compute_seconds_per_bar=config.compute_seconds_per_bar,
        decision_step=wiring.decision_step,
        decision_warmup=wiring.decision_warmup,
        label_horizon=wiring.label_spec.horizon,
    )


def compose_loop(
    config: LoopSettings,
    ingest: LoopStage,
    bus: EventBusAdapter,
    memory: ResearchMemory,
    llm: LLMProvider | None,
    state: DurableState | None,
) -> ResearchLoop:
    """The loop over ``memory`` with ``ingest`` as its round data source (shared composition).

    ``ingest`` is the round's data source stage (named ``ingest``; its ``segment`` artifact is a
    ``research.loop.segment.RoundData``); every other stage, the budget, the lifecycle guard, the
    audit and the durable hooks are the same for every source.
    """
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
        ingest,
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
            llm_content=llm.resolver if isinstance(llm, ContentVerifiedLLM) else None,
            batch=wiring.hypothesis_batch,
            knowledge_source=wiring.knowledge_source,
        ),
        *evolution,
        ExperimentStage(
            memory,
            components,
            compute_seconds_per_trial=config.compute_seconds_per_trial,
            conditional=wiring.conditional,
            state_spec=wiring.state_spec,
        ),
        ValidationStage(
            memory,
            components,
            compute_seconds_per_validation=config.validation_compute_seconds,
            oos_unseal=wiring.oos_unseal,
            sealed_decision_step=wiring.sealed_decision_step,
            conditional=wiring.conditional,
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
    """A loop composed over a state directory, with the research memory restored from it.

    ``bus`` is the bus the loop publishes on; ``owned_bus`` is set when the composition opened it
    (``state_dir / "bus"``): ``close()`` (or leaving a ``with`` block) releases its lock, as does
    dropping the loop. Closing a caller's bus is the caller's job."""

    loop: ResearchLoop
    memory: ResearchMemory
    state_dir: Path
    bus: EventBusAdapter
    owned_bus: FileEventBus | None = None
    #: The state directory's single-writer lock (``research.loop.durable.StateLock``).
    state_lock: StateLock | None = None

    def close(self) -> None:
        """Release the composition's own bus and the directory lock (idempotent; a caller's bus
        stays open)."""
        if self.owned_bus is not None:
            self.owned_bus.close()
        if self.state_lock is not None:
            self.state_lock.release()

    def __enter__(self) -> DurableLoop:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def check_round_bus(
    bus: EventBusAdapter,
    loop_id: str,
    records: Sequence[LoopRecord],
    *,
    durable: bool = True,
) -> int:
    """Cross-check ``research_loop.round`` on ``bus`` against the verified audit ``records``;
    return how many rounds were caught up from the audit. See the module docs, **Durable bus**:
    ahead, foreign or reordered messages, or a durable bus more than one round behind, are
    ``LoopStateInconsistent``; nothing is published unless every present message matches.
    ``durable=False`` (an ``InMemoryEventBus``, module docs, **Injected buses**): every missing
    round is replayed from the audit instead of refusing a gap of more than one."""
    expected = [round_message(loop_id, record) for record in records]
    published = bus.poll(BUS_AUDIT_CONSUMER, ROUND_TOPIC, len(expected) + 1)
    if len(published) > len(expected):
        raise LoopStateInconsistent(
            f"the bus holds more {ROUND_TOPIC} messages than the {len(expected)} rounds the "
            "audit records: the bus is ahead of the audit (audit rolled back, or another bus)"
        )
    for index, message in enumerate(published):
        if message.message_id != expected[index].message_id:
            raise LoopStateInconsistent(
                f"{ROUND_TOPIC} message {index} ({message.key!r}, record_hash "
                f"{str(message.payload.get('record_hash'))[:12]}) is not the audit's round "
                f"{index} (record_hash {records[index].record_hash[:12]}): a foreign or "
                "reordered record"
            )
    missing = expected[len(published) :]
    if durable and len(missing) > 1:
        raise LoopStateInconsistent(
            f"the bus is {len(missing)} rounds behind the audit (it holds {len(published)} of "
            f"{len(expected)}): only the last recorded round can be missing after a crash, so "
            "the bus was truncated, replaced or never used for this directory"
        )
    for message in missing:
        bus.publish(message)
    return len(missing)


def open_synthetic_loop(
    config: SyntheticLoopConfig,
    *,
    state_dir: Path,
    provider: SyntheticMarketProvider,
    bus: EventBusAdapter | None = None,
    llm: LLMProvider | None = None,
    anchor: StateAnchor | Path | None = None,
    bus_anchor: Path | None = None,
) -> DurableLoop:
    """Compose the loop over ``state_dir`` (created when missing), restoring every stateful part.

    The audit, trial ledger, sealed-OOS unsealing ledger, lineage, failure registry, review queue
    and the per-round memory checkpoint all live under ``state_dir``
    (``research.loop.durable``). Reopening restores them, cross-checks them against each other and
    against this configuration, and refuses (``LoopStateInconsistent``) on any disagreement; the
    loop then continues after the last recorded round. ``llm`` is external: resuming its own state
    (e.g. a scripted provider's position) is the caller's job. Human review approvals go through
    ``DurableLoop.memory.reviews.approve`` between rounds: journaled, then checkpointed (a
    ``between_rounds`` line) and anchored at once; an approval written any other way is refused
    on reopening.

    The budgets are part of the configuration: reopening with another ``LoopBudget`` or
    ``OosUnsealBudget`` (a larger quota, another approved family or approver — or a smaller one)
    is refused; raising a budget is a human decision and takes a new ``state_dir`` or ``loop_id``.
    ``anchor``: an optional external anchor — a path **outside** ``state_dir`` (a ``FileAnchor``)
    or any ``StateAnchor``; it receives the directory's head after every recorded round, and a
    reopened directory behind it (rolled back) or diverged from it is refused. Without it a
    consistent truncation of every file to an earlier round boundary is not detectable (the
    documented limit of ``research.loop.durable``).

    ``bus``: omitted -> the composition's own ``FileEventBus(state_dir / "bus")``, cross-checked
    against the audit before the loop is composed (``check_round_bus``: the last round is caught
    up after a crash, anything else inconsistent is refused) and released by
    ``DurableLoop.close()``. A caller's bus is cross-checked the same way (an ``InMemoryEventBus``
    is not durable: the audit's rounds are replayed into it; module docs, **Injected buses**) and
    stays open. ``bus_anchor``: only with the composition's own bus — a path **outside**
    ``state_dir`` given to ``FileEventBus(..., anchor=)``, so lines dropped from the end of *any*
    bus topic (not only ``research_loop.round``, which the audit already covers) are refused on
    reopening (``BusCorrupted``).
    """
    if bus is not None and bus_anchor is not None:  # before anything under state_dir is touched
        raise ValueError(
            "bus_anchor anchors the composition's own bus; anchor a caller's bus there"
        )
    refuse_ephemeral_unseal(config)
    wiring = config.wiring
    state = open_state(
        state_dir,
        fingerprint={**loop_fingerprint(config), **llm_content_fingerprint(llm)},
        strategies=wiring.strategies,
        provider=provider,
        provider_for=None if wiring.evolution is None else wiring.evolution.provider_for,
        anchor=FileAnchor(anchor) if isinstance(anchor, str | PathLike) else anchor,
    )
    return compose_durable(
        config, state, _synthetic_ingest(config, provider, state.memory), bus, llm, bus_anchor
    )


def compose_durable(
    config: LoopSettings,
    state: DurableState,
    ingest: LoopStage,
    bus: EventBusAdapter | None,
    llm: LLMProvider | None,
    bus_anchor: Path | None = None,
) -> DurableLoop:
    """``compose_loop`` over an opened state directory, with its bus (shared by every source).

    ``bus`` given: cross-checked against the verified audit (``check_round_bus``; an
    ``InMemoryEventBus`` is not durable and gets the audit's rounds replayed, module docs,
    **Injected buses**), then used; closing it is the caller's job. Omitted: the composition's own
    ``FileEventBus(state_dir / "bus")``, cross-checked the same way and released by
    ``DurableLoop.close()`` or when the loop is dropped (module docs, **Durable bus**);
    ``bus_anchor`` (own bus only) is that bus's external topic-head anchor. The directory's
    recorded LLM content-verification mode must be ``llm``'s (module docs, **LLM content
    verification**; ``LoopStateInconsistent`` otherwise).
    """
    if bus is not None and bus_anchor is not None:
        raise ValueError(
            "bus_anchor anchors the composition's own bus; anchor a caller's bus there"
        )
    _check_llm_content_mode(state, llm)
    if bus is not None:
        durable = not isinstance(bus, InMemoryEventBus)
        check_round_bus(bus, config.loop_id, state.audit.records, durable=durable)
        loop = compose_loop(config, ingest, bus, state.memory, llm, state)
        return DurableLoop(
            loop=loop, memory=state.memory, state_dir=state.root, bus=bus, state_lock=state.lock
        )
    owned = FileEventBus(state.root / BUS_DIR, anchor=bus_anchor)
    try:
        check_round_bus(owned, config.loop_id, state.audit.records)
        loop = compose_loop(config, ingest, owned, state.memory, llm, state)
    except BaseException:
        owned.close()
        raise
    weakref.finalize(loop, owned.close)  # a dropped loop releases the bus lock
    return DurableLoop(
        loop=loop,
        memory=state.memory,
        state_dir=state.root,
        bus=owned,
        owned_bus=owned,
        state_lock=state.lock,
    )


def llm_content_fingerprint(llm: LLMProvider | None) -> dict[str, Any]:
    """The fingerprint component of the LLM content-verification mode.

    ``{"llm_content_verified": True}`` for a ``ContentVerifiedLLM``, else nothing (so every
    unverified fingerprint stays exactly as it was)."""
    return {LLM_CONTENT_VERIFIED: True} if isinstance(llm, ContentVerifiedLLM) else {}


def _check_llm_content_mode(state: DurableState, llm: LLMProvider | None) -> None:
    """The header's recorded verification mode is ``llm``'s (module docs)."""
    header = state.checkpoint.journal.entries[0]
    recorded = header.payload.get("fingerprint") if header.type == LOOP_STATE_OPENED else None
    if not isinstance(recorded, Mapping):
        raise LoopStateInconsistent(f"{state.root} has no readable configuration fingerprint")
    verified = recorded.get(LLM_CONTENT_VERIFIED, False) is True
    if verified != isinstance(llm, ContentVerifiedLLM):
        raise LoopStateInconsistent(
            f"{state.root} was opened with LLM content verification "
            f"{'on' if verified else 'off'}; this composition has it "
            f"{'off' if verified else 'on'}: the verification mode is part of the state "
            "directory (reviewed drafts would be taken under another check)"
        )


def _unseal_payload(budget: OosUnsealBudget | None) -> dict[str, Any] | None:
    """The budget as fingerprinted; the TEST-ONLY ``ephemeral_unseal_for_tests`` flag appears
    only when set (so every durable fingerprint stays as it was)."""
    if budget is None:
        return None
    payload: dict[str, Any] = {
        "max_unsealings": budget.max_unsealings,
        "approved_families": dict(sorted(budget.approved_families.items())),
    }
    if budget.ephemeral_unseal_for_tests:
        payload["ephemeral_unseal_for_tests"] = True
    return payload


def refuse_ephemeral_unseal(config: LoopSettings) -> None:
    """A state directory's unsealing ledger is durable: the TEST-ONLY ephemeral flag is refused
    before anything is written to the directory (review fixes 4)."""
    budget = config.wiring.oos_unseal
    if budget is not None and budget.ephemeral_unseal_for_tests:
        raise ValueError(
            "ephemeral_unseal_for_tests is a TEST-ONLY flag for an in-memory loop; a state_dir "
            "keeps a durable unsealing ledger: drop the flag"
        )


def loop_fingerprint(config: SyntheticLoopConfig) -> dict[str, Any]:
    """What a restored research memory depends on (the header of a state directory).

    Includes the budgets (the ``LoopBudget`` payload and the whole ``OosUnsealBudget``): a state
    directory is bound to the budgets it was opened with, and changing them on reopening is
    refused (a new budget is a human decision: a new ``state_dir`` / ``loop_id``). The cadence is
    exact (whole microseconds, ``timedelta``'s resolution), and so are the decision / warm-up /
    sealed decision steps; the evolution plan's numbers, the initial equity, the feature chunk size
    and the explicit G4 parameters are bound too. Compute declarations and the LLM provider are not
    (its content-verification mode is: the opening functions add ``llm_content_fingerprint``).
    """
    return {
        **settings_fingerprint(config),
        "market": config.market.content_hash(),
        "minutes_per_round": config.minutes_per_round,
    }


def settings_fingerprint(config: LoopSettings) -> dict[str, Any]:
    """The part of a state directory's fingerprint every round data source shares.

    The opt-in ``ConditionalPlan`` appears only when set (``conditional``: its payload), so every
    fingerprint of a configuration without one stays exactly as it was; so does the opt-in
    ``HypothesisBatch`` (``hypothesis_batch``: its payload) and the opt-in ``KnowledgeSource``
    (``knowledge_source``: provider identity and query hash)."""
    wiring = config.wiring
    opt_in: dict[str, Any] = (
        {} if wiring.conditional is None else {"conditional": wiring.conditional.payload()}
    )
    if wiring.hypothesis_batch is not None:
        opt_in["hypothesis_batch"] = wiring.hypothesis_batch.payload()
    if wiring.knowledge_source is not None:
        opt_in["knowledge_source"] = wiring.knowledge_source.payload()
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
        "knowledge": [item.content_hash() for item in config.knowledge],
        "strategies": [c.spec.content_hash() for c in wiring.strategies],
        "feature_spec": wiring.feature_spec.content_hash(),
        "state_spec": wiring.state_spec.content_hash(),
        "label_spec": wiring.label_spec.content_hash(),
        "cost_model": wiring.cost_model.content_hash(),
        "code_commit": wiring.code_commit,
        "environment_lock": wiring.environment_lock,
        "evolution": _evolution_payload(wiring.evolution),
        "decision_step_microseconds": wiring.decision_step // timedelta(microseconds=1),
        "decision_warmup_microseconds": wiring.decision_warmup // timedelta(microseconds=1),
        "sealed_decision_step_microseconds": (
            None
            if wiring.sealed_decision_step is None
            else wiring.sealed_decision_step // timedelta(microseconds=1)
        ),
        "feature_chunk_bars": wiring.feature_chunk_bars,
        "initial_equity": str(wiring.initial_equity),
        "robustness": {  # explicit G4 parameters, as exact text (no float in the fingerprint)
            name: None if value is None else repr(value)
            for name, value in sorted(asdict(wiring.robustness).items())
        },
        **opt_in,
    }


def _evolution_payload(plan: EvolutionPlan | None) -> dict[str, Any] | None:
    """The evolution plan's declared numbers (``provider_for`` is code: ``code_commit``)."""
    if plan is None:
        return None
    return {
        "every_rounds": plan.every_rounds,
        "parents_per_round": plan.parents_per_round,
        "compute_seconds": str(plan.compute_seconds),
        "minimum_meaningful_effect": plan.minimum_meaningful_effect,
    }


def run_unattended_and_report(
    loop: ResearchLoop, rounds: int, *, reports_root: Path | None = None
) -> tuple[LoopRecord, ...]:
    """Run rounds and write their loop records and generated P6 matrices when requested.

    ``reports_root is None`` (the default) behaves exactly like calling ``run_unattended``
    directly: no filesystem write happens. When set, every ``LoopRecord`` the loop produces is
    written to ``<reports_root>/research_loop_round/<record_hash>.json`` and every complete
    ``StateStrategyMatrix`` produced by its experiment stages is written to
    ``<reports_root>/state_strategy_matrix/<matrix_hash>.json``. Both writers are append-only and
    idempotent for identical content. Matrix reports use their existing ``matrix_hash`` identity;
    they are an additional sink and do not change any ``LoopRecord`` payload or hash.
    """
    matrix_starts = (
        {
            id(stage): len(stage.matrices)
            for stage in loop.stages
            if isinstance(stage, ExperimentStage)
        }
        if reports_root is not None
        else {}
    )
    records = loop.run_unattended(rounds)
    if reports_root is not None:
        for stage in loop.stages:
            if isinstance(stage, ExperimentStage):
                for matrix in stage.matrices[matrix_starts[id(stage)] :]:
                    write_state_strategy_matrix(reports_root, matrix)
        write_research_loop_rounds(reports_root, records)
    return records

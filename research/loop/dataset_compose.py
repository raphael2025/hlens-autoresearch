"""Dataset-backed composition of the continuous loop (ADR-0049 implementation note, dataset-backed
loop, 2026-09-26).

``build_dataset_loop`` / ``open_dataset_loop`` compose the same loop as ``build_synthetic_loop`` /
``open_synthetic_loop`` (``research.loop.compose.compose_loop`` / ``compose_durable``: the same
state, hypothesis, evolution, experiment, validation and memory stages, the same ``LoopBudget``,
``LifecycleGuard``, audit, durable state directory, anchor and automatic durable bus) with one
difference: the round data source is ``research.loop.dataset_source.DatasetIngestStage``, which
reads every round's data from the verified Research Dataset manifests a ``DatasetRound`` declares,
instead of generating a synthetic market. What that changes downstream:

- every validation report carries ``G0.manifest_binding`` (the proven bars, the round's
  ``ManifestPair`` and the manifest of every feature request behind the signals), where the
  synthetic path is labelled ``synthetic_unverified``;
- the reproducibility tuple's dataset snapshots are the pair's two ``DatasetRef``;
- the durable state keeps no ingest memory (``open_state(provider=None)``): each round re-reads its
  declared manifests, and on reopening every recorded ingest must name exactly the manifests the
  configuration declares for that round (``LoopStateInconsistent`` otherwise).

``DatasetLoopConfig`` binds everything ``SyntheticLoopConfig`` binds except the market: the
Canonical ``symbol``, the declared ``rounds`` (round ``i`` reads ``rounds[i]``; a round beyond them
fails its ingest stage, recorded) and the ingest's declared compute. The fingerprint of a state
directory binds the symbol and every declared manifest hash (the sealed pairs included).

Sealed OOS (ADR-0049 implementation note, dataset G5, 2026-09-26). An ``OosUnsealBudget`` (with
its ``sealed_decision_step``) is accepted only when at least one declared round has a sealed
manifest pair (``DatasetRound.sealed_feature_manifest_hash`` / ``sealed_price_manifest_hash``);
without one, G5 has nothing to run on and the budget is refused. G5 then runs exactly as on the
synthetic path (``research.loop.trials.ValidationStage``): only for a family on the budget's
approved list with its human approver, only after an in-sample PASS, at most once per family (the
evaluation is claimed — recorded as consumed — before any sealed manifest, bar or feature is read;
an early end is ``consumed_without_result``), bounded by ``max_unsealings``; the unsealing ledger
is the durable ``sealed_oos.jsonl`` of the state directory, so a restart never unseals again, and
the budget is part of the fingerprint. An in-memory dataset loop with a budget needs an explicitly
passed ``DurableUnsealingLedger`` (or the TEST-ONLY ``ephemeral_unseal_for_tests`` flag; review
fixes 4, ``research.loop.trials.OosUnsealBudget``). What is read and proven after the claim:
``research.loop.dataset_source.SealedDatasetPair``.

v3 evidence manifests (ADR-0077; C1-CONSUMERS, data access only). A ``DatasetRound`` may declare
``ResearchDatasetEvidenceManifest`` hashes when the ``DatasetCatalog`` carries the builder
catalog's ``StreamingEvidenceVerifier`` (``evidence_verifier``; without it such a round's ingest
is refused by the store). The composition is unchanged: a round's source identity is its declared
manifest content hashes, which the fingerprint and the recorded-ingest check already bind, so a
state directory's fingerprint and records do not depend on the manifest form. Nothing here resolves
ACTIVE strategies, source authority or metrics (ADR-0080 BLOCKED), schedules rounds or reaches the
ADR-0074 operator (synthetic-only).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from os import PathLike
from pathlib import Path
from typing import Any

from apps.worker.loop import LoopBudget, LoopRecord, ResearchLoop
from core.contracts.event_bus import EventBusAdapter
from core.contracts.llm import LLMProvider
from core.contracts.validation_profile import ValidationProfile
from core.domain.research import KnowledgeItem
from research.loop.compose import (
    DurableLoop,
    LoopWiring,
    compose_durable,
    compose_loop,
    llm_content_fingerprint,
    refuse_ephemeral_unseal,
    settings_fingerprint,
)
from research.loop.dataset_source import (
    DATASET_SOURCE,
    DatasetCatalog,
    DatasetIngestStage,
    DatasetRound,
)
from research.loop.durable import FileAnchor, LoopStateInconsistent, StateAnchor, open_state
from research.loop.memory import ResearchMemory

__all__ = [
    "DatasetLoopConfig",
    "build_dataset_loop",
    "dataset_loop_fingerprint",
    "open_dataset_loop",
]


@dataclass(frozen=True, slots=True)
class DatasetLoopConfig:
    """``SyntheticLoopConfig`` with declared dataset rounds instead of a market (module docs)."""

    loop_id: str
    seed: int
    epoch: datetime
    cadence: timedelta
    budget: LoopBudget
    #: Canonical symbol (e.g. ``BTC-USDT``) the loop researches.
    symbol: str
    #: Round ``i`` reads ``rounds[i]``.
    rounds: Sequence[DatasetRound]
    ingest_compute_seconds: Decimal
    wiring: LoopWiring
    family_id: str
    knowledge: Sequence[KnowledgeItem]
    max_new_hypotheses_per_round: int
    max_reevaluations_per_round: int
    hypothesis_compute_seconds: Decimal
    compute_seconds_per_trial: Decimal
    validation_compute_seconds: Decimal
    state_compute_seconds: Decimal
    profile: ValidationProfile
    constitution_version: str
    llm_prompt: str | None = None
    llm_cost_units_per_call: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        rounds = tuple(self.rounds)
        if not rounds or not all(isinstance(item, DatasetRound) for item in rounds):
            raise ValueError("a dataset loop needs at least one declared DatasetRound")
        object.__setattr__(self, "rounds", rounds)
        unsealing = (
            self.wiring.oos_unseal is not None or self.wiring.sealed_decision_step is not None
        )
        if unsealing and not any(item.has_sealed_pair for item in rounds):
            raise ValueError(
                "G5 over dataset rounds is not wired without a sealed manifest pair: an "
                "OosUnsealBudget needs at least one DatasetRound declaring "
                "sealed_feature_manifest_hash and sealed_price_manifest_hash (otherwise the "
                "sealed OOS window stays sealed)"
            )


def _ingest(config: DatasetLoopConfig, catalog: DatasetCatalog) -> DatasetIngestStage:
    wiring = config.wiring
    return DatasetIngestStage(
        catalog,
        config.profile,
        symbol=config.symbol,
        rounds=config.rounds,
        compute_seconds=config.ingest_compute_seconds,
        decision_step=wiring.decision_step,
        decision_warmup=wiring.decision_warmup,
        label_horizon=wiring.label_spec.horizon,
    )


def build_dataset_loop(
    config: DatasetLoopConfig,
    *,
    catalog: DatasetCatalog,
    bus: EventBusAdapter | None = None,
    memory: ResearchMemory | None = None,
    llm: LLMProvider | None = None,
    state_dir: Path | None = None,
    anchor: StateAnchor | Path | None = None,
) -> ResearchLoop:
    """Compose the dataset-backed loop over ``memory`` (in memory) or ``state_dir`` (durable).

    Exactly one of ``memory`` / ``state_dir``; ``anchor`` only with ``state_dir``; ``bus`` required
    in memory, optional with ``state_dir`` (as ``build_synthetic_loop``).
    """
    if (memory is None) == (state_dir is None):
        raise ValueError("pass exactly one of memory (in memory) or state_dir (durable)")
    if state_dir is not None:
        return open_dataset_loop(
            config, state_dir=state_dir, catalog=catalog, bus=bus, llm=llm, anchor=anchor
        ).loop
    if anchor is not None:
        raise ValueError("an anchor keeps a state directory's head: it needs state_dir")
    if bus is None:
        raise ValueError("an in-memory loop needs a bus (only a state_dir provides its own)")
    assert memory is not None
    return compose_loop(config, _ingest(config, catalog), bus, memory, llm, None)


def open_dataset_loop(
    config: DatasetLoopConfig,
    *,
    state_dir: Path,
    catalog: DatasetCatalog,
    bus: EventBusAdapter | None = None,
    llm: LLMProvider | None = None,
    anchor: StateAnchor | Path | None = None,
    bus_anchor: Path | None = None,
) -> DurableLoop:
    """The dataset-backed loop over ``state_dir`` (``open_synthetic_loop``'s contract).

    Besides every cross-check of ``research.loop.durable``, each recorded ingest must name the
    manifests ``config.rounds`` declares for its round. ``bus`` omitted: the composition's own
    ``FileEventBus(state_dir / "bus")``, cross-checked against the audit (``compose_durable``).
    """
    if bus is not None and bus_anchor is not None:  # before anything under state_dir is touched
        raise ValueError(
            "bus_anchor anchors the composition's own bus; anchor a caller's bus there"
        )
    refuse_ephemeral_unseal(config)
    wiring = config.wiring
    state = open_state(
        state_dir,
        fingerprint={**dataset_loop_fingerprint(config), **llm_content_fingerprint(llm)},
        strategies=wiring.strategies,
        provider=None,
        provider_for=None if wiring.evolution is None else wiring.evolution.provider_for,
        anchor=FileAnchor(anchor) if isinstance(anchor, str | PathLike) else anchor,
    )
    for record in state.audit.records:
        _check_recorded_ingest(state.root, config, record)
    return compose_durable(config, state, _ingest(config, catalog), bus, llm, bus_anchor)


def _check_recorded_ingest(root: Path, config: DatasetLoopConfig, record: LoopRecord) -> None:
    stage = next((s for s in record.stages if s.name == "ingest"), None)
    if stage is None or stage.summary is None or "manifest_pair_hash" not in stage.summary:
        return  # the ingest failed (or never ran): nothing was read
    summary = stage.summary
    declared = (
        config.rounds[record.round_index] if record.round_index < len(config.rounds) else None
    )
    if declared is None or (
        summary.get("source"),
        summary.get("symbol"),
        summary.get("feature_manifest_hash"),
        summary.get("price_manifest_hash"),
        summary.get("sealed_manifest_hash"),
        summary.get("sealed_feature_manifest_hash"),
        summary.get("sealed_price_manifest_hash"),
    ) != (
        DATASET_SOURCE,
        config.symbol,
        declared.feature_manifest_hash,
        declared.price_manifest_hash,
        declared.sealed_manifest_hash,
        declared.sealed_feature_manifest_hash,
        declared.sealed_price_manifest_hash,
    ):
        raise LoopStateInconsistent(
            f"{root}: round {record.round_index} read other manifests than the configuration "
            "declares for it"
        )


def dataset_loop_fingerprint(config: DatasetLoopConfig) -> dict[str, Any]:
    """``settings_fingerprint`` plus the source: the symbol and every declared round's manifests."""
    return {
        **settings_fingerprint(config),
        "source": DATASET_SOURCE,
        "symbol": config.symbol,
        "rounds": [item.payload() for item in config.rounds],
    }

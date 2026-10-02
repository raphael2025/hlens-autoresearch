"""Strict TOML v1 configuration of the Dataset-sourced loop operator (ADR-0105 §5; amends
ADR-0074 §9).

The parallel of ``research.loop.operator_config`` for ``research.loop.dataset_operator``: the same
strict reading (``load_operator_config``'s rules — UTF-8 TOML, closed tables, every key required,
no TOML float, no placeholder, no TEST ONLY content, hash-bound artifacts loaded through their
contract models), the same ``[paths]``, ``[loop.wiring]`` and cross-checks, the same static
provider allowlist and the same ADR-0062 freeze-registry check — **without a frozen Profile every
configuration is refused** (ADR-0074 §2.8, unchanged). Only the round data source differs: instead
of a synthetic market, the rounds read declared Research Dataset manifests
(``research.loop.dataset_compose.DatasetLoopConfig``). The readers of the common tables and
scalars (``[paths]``, ``[loop.wiring]``, artifacts, the cross-checks, the freeze check, the
``wiring`` identity, the provider construction) are the synthetic configuration's own, imported;
only the ``[source]`` / ``[loop]`` tables, the provider role list and the identity payload are
this module's.

**Schema** (``schema_version = "1.0.0"``; every table closed, every key required):

- ``[operator]``: ``llm_enabled = false``.
- ``[paths]``: exactly the synthetic operator's six paths and rules.
- ``[providers.<role>]`` for each of ``DATASET_PROVIDER_ROLES`` (the synthetic roles without
  ``synthetic_market_provider``): ``id``, ``version``, ``descriptor_hash``.
- ``[source]``: ``kind = "research_dataset"``; ``symbol`` (a Canonical symbol, e.g.
  ``BTC-USDT``); ``ingest_compute_seconds`` (canonical decimal string); ``rounds``, a non-empty
  array of ``{ feature_manifest_hash, price_manifest_hash, sealed_manifest_hash }`` tables (round
  ``i`` reads ``rounds[i]``; each hash a SHA-256; ``sealed_manifest_hash`` a withheld-only
  declaration or ``false``). A sealed manifest **pair** is not accepted in v1 (``oos_unseal`` is
  disabled, as in the synthetic schema).
- ``[loop]``: the ``DatasetLoopConfig`` field names that are not source fields (the synthetic
  ``[loop]`` keys without ``market``, ``minutes_per_round`` and ``compute_seconds_per_bar``), with
  ``[loop.budget]``; ``profile`` and each ``knowledge`` element are artifact references.
- ``[loop.wiring]``: exactly the synthetic schema (every disabled mode an explicit ``false``).

``DatasetOperatorConfig.operator_identity`` is the SHA-256 of the semantic configuration
(``hlens.dataset-loop-operator-identity@1.0.0``: schema version, provider identities, the source,
every loop value or content hash and the synthetic ``wiring`` identity); paths are not part of it.
The live ``DatasetCatalog`` is not configuration (``research.loop.dataset_operator``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from apps.worker.loop import LoopBudget
from core.contracts.validation_profile import ValidationProfile
from core.domain.base import (
    NAME_PATTERN,
    SEMVER_PATTERN,
    SHA256_PATTERN,
    content_hash,
    exact_decimal_text,
)
from core.domain.research import KnowledgeItem
from infrastructure.canonical import rules
from research.loop.dataset_compose import DatasetLoopConfig
from research.loop.dataset_source import DATASET_SOURCE, DatasetRound
from research.loop.operator_config import (
    _PATH_KEYS,
    _WIRING_KEYS,
    CONFIG_SCHEMA_VERSION,
    PROVIDER_ROLES,
    FreezeEvidence,
    OperatorConfigError,
    OperatorPaths,
    ProviderIdentity,
    WiringValues,
    _artifact,
    _artifacts,
    _decimal,
    _duration,
    _freeze_evidence,
    _int,
    _paths,
    _read_toml,
    _scan_strings,
    _str,
    _table,
    _utc,
    _wiring,
    compile_wiring,
    cross_check_bindings,
    wiring_identity,
)
from research.loop.operator_providers import OperatorProviderError, build_core_providers

__all__ = [
    "DATASET_PROVIDER_ROLES",
    "CompiledDatasetOperatorConfig",
    "DatasetInjectedProviders",
    "DatasetLoopValues",
    "DatasetOperatorConfig",
    "DatasetSourceValues",
    "build_dataset_providers",
    "load_dataset_operator_config",
]

#: ``[providers.<role>]`` roles of the Dataset-sourced operator (no synthetic market provider).
DATASET_PROVIDER_ROLES: Final = tuple(
    role for role in PROVIDER_ROLES if role != "synthetic_market_provider"
)
_IDENTITY_FORMAT: Final = "hlens.dataset-loop-operator-identity@1.0.0"
_TOP_KEYS: Final = frozenset({"schema_version", "operator", "paths", "providers", "source", "loop"})
_PROVIDER_KEYS: Final = frozenset({"id", "version", "descriptor_hash"})
_SOURCE_KEYS: Final = frozenset({"kind", "symbol", "ingest_compute_seconds", "rounds"})
_ROUND_KEYS: Final = frozenset(
    {"feature_manifest_hash", "price_manifest_hash", "sealed_manifest_hash"}
)
_BUDGET_KEYS: Final = frozenset(
    {"max_trials_per_round", "max_trials_total", "max_llm_cost_units", "max_compute_seconds"}
)
_LOOP_KEYS: Final = frozenset(
    {
        "loop_id",
        "seed",
        "epoch",
        "cadence",
        "budget",
        "wiring",
        "family_id",
        "knowledge",
        "max_new_hypotheses_per_round",
        "max_reevaluations_per_round",
        "hypothesis_compute_seconds",
        "compute_seconds_per_trial",
        "validation_compute_seconds",
        "state_compute_seconds",
        "profile",
        "constitution_version",
        "llm_prompt",
        "llm_cost_units_per_call",
    }
)
_SHA256_RE: Final = re.compile(SHA256_PATTERN)
_SEMVER_RE: Final = re.compile(SEMVER_PATTERN)
_NAME_RE: Final = re.compile(NAME_PATTERN)
#: Canonical symbols (``infrastructure.canonical.rules``): what ``DatasetIngestStage`` reads.
_CANONICAL_SYMBOLS: Final = frozenset(item.symbol for item in rules.SYMBOLS.values())
# Process-local marker set only by ``build_dataset_providers`` (as the synthetic operator's
# allowlist seal: an accidental-misuse boundary, not a security boundary).
_DATASET_ALLOWLIST_SEAL: Final = object()


# ---- result types ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatasetSourceValues:
    """The ``[source]`` table: the Canonical symbol, the declared rounds and the ingest compute."""

    symbol: str
    rounds: tuple[DatasetRound, ...]
    ingest_compute_seconds: Decimal

    def payload(self) -> dict[str, Any]:
        return {
            "kind": DATASET_SOURCE,
            "symbol": self.symbol,
            "ingest_compute_seconds": exact_decimal_text(self.ingest_compute_seconds),
            "rounds": [item.payload() for item in self.rounds],
        }


@dataclass(frozen=True, slots=True)
class DatasetLoopValues:
    """Every ``DatasetLoopConfig`` value that is neither a source field nor ``wiring``."""

    loop_id: str
    seed: int
    epoch: datetime
    cadence: timedelta
    budget: LoopBudget
    family_id: str
    knowledge: tuple[KnowledgeItem, ...]
    max_new_hypotheses_per_round: int
    max_reevaluations_per_round: int
    hypothesis_compute_seconds: Decimal
    compute_seconds_per_trial: Decimal
    validation_compute_seconds: Decimal
    state_compute_seconds: Decimal
    profile: ValidationProfile
    constitution_version: str
    llm_prompt: None
    llm_cost_units_per_call: Decimal


@dataclass(frozen=True, slots=True)
class DatasetInjectedProviders:
    """The five allowlisted providers ``build_dataset_providers`` built and verified."""

    identities: tuple[ProviderIdentity, ...]
    feature_provider: Any
    state_provider: Any
    strategy_provider: Any
    backtester: Any
    outcome_provider: Any
    _allowlist_seal: object = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class CompiledDatasetOperatorConfig:
    """What the Dataset-sourced operator hands to ``open_dataset_loop`` (with its catalog)."""

    loop_config: DatasetLoopConfig
    operator_identity: str
    paths: OperatorPaths


@dataclass(frozen=True, slots=True)
class DatasetOperatorConfig:
    """A fully parsed and verified v1 configuration, without provider objects (module docs)."""

    schema_version: str
    paths: OperatorPaths
    providers: tuple[ProviderIdentity, ...]
    source: DatasetSourceValues
    loop: DatasetLoopValues
    wiring: WiringValues
    freeze: FreezeEvidence
    operator_identity: str

    def build(self, providers: DatasetInjectedProviders) -> CompiledDatasetOperatorConfig:
        """Compile exactly one ``DatasetLoopConfig`` (every field set) from these values and the
        verified providers. One-way: nothing here reads files or mutates the configuration."""
        if not isinstance(providers, DatasetInjectedProviders):
            raise OperatorConfigError("providers must be a DatasetInjectedProviders")
        if providers.identities != self.providers:
            raise OperatorConfigError(
                "the injected providers were verified against other identities than the "
                "configuration's [providers] tables"
            )
        if providers._allowlist_seal is not _DATASET_ALLOWLIST_SEAL:
            raise OperatorConfigError(
                "providers were not constructed and verified by the static allowlist"
            )
        wiring = compile_wiring(
            self.wiring,
            feature_provider=providers.feature_provider,
            state_provider=providers.state_provider,
            strategy_provider=providers.strategy_provider,
            backtester=providers.backtester,
            outcome_provider=providers.outcome_provider,
        )
        v, source = self.loop, self.source
        loop_config = DatasetLoopConfig(
            loop_id=v.loop_id,
            seed=v.seed,
            epoch=v.epoch,
            cadence=v.cadence,
            budget=v.budget,
            symbol=source.symbol,
            rounds=source.rounds,
            ingest_compute_seconds=source.ingest_compute_seconds,
            wiring=wiring,
            family_id=v.family_id,
            knowledge=v.knowledge,
            max_new_hypotheses_per_round=v.max_new_hypotheses_per_round,
            max_reevaluations_per_round=v.max_reevaluations_per_round,
            hypothesis_compute_seconds=v.hypothesis_compute_seconds,
            compute_seconds_per_trial=v.compute_seconds_per_trial,
            validation_compute_seconds=v.validation_compute_seconds,
            state_compute_seconds=v.state_compute_seconds,
            profile=v.profile,
            constitution_version=v.constitution_version,
            llm_prompt=v.llm_prompt,
            llm_cost_units_per_call=v.llm_cost_units_per_call,
        )
        return CompiledDatasetOperatorConfig(
            loop_config=loop_config, operator_identity=self.operator_identity, paths=self.paths
        )


def build_dataset_providers(config: DatasetOperatorConfig) -> DatasetInjectedProviders:
    """The five statically allowlisted providers (``research.loop.operator_providers
    .build_core_providers``), verified against the configuration before any state is opened."""
    if not isinstance(config, DatasetOperatorConfig):
        raise OperatorProviderError("config must be a DatasetOperatorConfig")
    if tuple(identity.role for identity in config.providers) != DATASET_PROVIDER_ROLES:
        raise OperatorProviderError(
            "provider identities must occur exactly once in DATASET_PROVIDER_ROLES order"
        )
    core = build_core_providers(config.providers, config.wiring)
    return DatasetInjectedProviders(
        identities=config.providers,
        feature_provider=core.feature_provider,
        state_provider=core.state_provider,
        strategy_provider=core.strategy_provider,
        backtester=core.backtester,
        outcome_provider=core.outcome_provider,
        _allowlist_seal=_DATASET_ALLOWLIST_SEAL,
    )


# ---- entry point ----------------------------------------------------------------------------


def load_dataset_operator_config(path: Path) -> DatasetOperatorConfig:
    """Parse, type, resolve and verify one v1 configuration file (module docs).

    Raises ``OperatorConfigError`` for every refusal — including a missing or empty freeze
    registry and a Profile without an exact freeze record — and ``FreezeRegistryFault`` when the
    configured registry cannot be read, is corrupted or locked (the synthetic schema's rules)."""
    config_file = Path(path).resolve()
    raw = _read_toml(config_file)
    _scan_strings(raw, "configuration")
    _table(raw, "configuration", _TOP_KEYS)
    if raw["schema_version"] != CONFIG_SCHEMA_VERSION:
        raise OperatorConfigError(
            f"unsupported schema_version {raw['schema_version']!r} "
            f"(supported: {CONFIG_SCHEMA_VERSION!r})"
        )
    base = config_file.parent
    operator = _table(raw["operator"], "[operator]", frozenset({"llm_enabled"}))
    if operator["llm_enabled"] is not False:
        raise OperatorConfigError("[operator] llm_enabled must be false (v1 runs without an LLM)")
    paths = _paths(_table(raw["paths"], "[paths]", frozenset(_PATH_KEYS)), base, config_file)
    providers = _providers(raw["providers"])
    source = _source(_table(raw["source"], "[source]", _SOURCE_KEYS))
    loop = _table(raw["loop"], "[loop]", _LOOP_KEYS)
    wiring = _wiring(_table(loop["wiring"], "[loop.wiring]", _WIRING_KEYS), base)
    values = _loop_values(loop, base)
    cross_check_bindings(values.profile, values.family_id, wiring)
    freeze = _freeze_evidence(paths, values.profile)
    return DatasetOperatorConfig(
        schema_version=CONFIG_SCHEMA_VERSION,
        paths=paths,
        providers=providers,
        source=source,
        loop=values,
        wiring=wiring,
        freeze=freeze,
        operator_identity=_operator_identity(providers, source, values, wiring),
    )


# ---- tables ---------------------------------------------------------------------------------


def _providers(value: Any) -> tuple[ProviderIdentity, ...]:
    table = _table(value, "[providers]", frozenset(DATASET_PROVIDER_ROLES))
    identities = []
    for role in DATASET_PROVIDER_ROLES:
        label = f"[providers.{role}]"
        entry = _table(table[role], label, _PROVIDER_KEYS)
        identities.append(
            ProviderIdentity(
                role=role,
                id=_str(entry["id"], f"{label} id", _NAME_RE),
                version=_str(entry["version"], f"{label} version", _SEMVER_RE),
                descriptor_hash=_str(
                    entry["descriptor_hash"], f"{label} descriptor_hash", _SHA256_RE
                ),
            )
        )
    return tuple(identities)


def _source(table: dict[str, Any]) -> DatasetSourceValues:
    label = "[source]"
    if table["kind"] != DATASET_SOURCE:
        raise OperatorConfigError(f'{label} kind must be "{DATASET_SOURCE}"')
    symbol = _str(table["symbol"], f"{label} symbol")
    if symbol not in _CANONICAL_SYMBOLS:
        raise OperatorConfigError(f"{label} symbol {symbol!r} is not a Canonical symbol")
    raw_rounds = table["rounds"]
    if not isinstance(raw_rounds, list) or not raw_rounds:
        raise OperatorConfigError(f"{label} rounds must be a non-empty array")
    rounds: list[DatasetRound] = []
    for index, item in enumerate(raw_rounds):
        where = f"{label} rounds[{index}]"
        entry = _table(item, where, _ROUND_KEYS)
        sealed = entry["sealed_manifest_hash"]
        rounds.append(
            DatasetRound(
                feature_manifest_hash=_str(
                    entry["feature_manifest_hash"], f"{where} feature_manifest_hash", _SHA256_RE
                ),
                price_manifest_hash=_str(
                    entry["price_manifest_hash"], f"{where} price_manifest_hash", _SHA256_RE
                ),
                sealed_manifest_hash=(
                    None
                    if sealed is False
                    else _str(sealed, f"{where} sealed_manifest_hash", _SHA256_RE)
                ),
            )
        )
    return DatasetSourceValues(
        symbol=symbol,
        rounds=tuple(rounds),
        ingest_compute_seconds=_decimal(
            table["ingest_compute_seconds"], f"{label} ingest_compute_seconds", minimum=Decimal(0)
        ),
    )


def _loop_values(table: dict[str, Any], base: Path) -> DatasetLoopValues:
    label = "[loop]"
    if table["llm_prompt"] != "":
        raise OperatorConfigError(f'{label} llm_prompt must be "" while llm_enabled = false')
    llm_cost = _decimal(
        table["llm_cost_units_per_call"], f"{label} llm_cost_units_per_call", minimum=None
    )
    if llm_cost != 0:
        raise OperatorConfigError(
            f'{label} llm_cost_units_per_call must be "0" while llm_enabled = false'
        )
    budget = _table(table["budget"], f"{label}.budget", _BUDGET_KEYS)
    knowledge = _artifacts(table["knowledge"], f"{label} knowledge", base, KnowledgeItem)
    if not knowledge:
        raise OperatorConfigError(f"{label} knowledge must be a non-empty array")
    if len({item.ref.target_identity() for item in knowledge}) != len(knowledge):
        raise OperatorConfigError(f"{label} knowledge lists one KnowledgeItem twice")

    def compute(name: str) -> Decimal:
        return _decimal(table[name], f"{label} {name}", minimum=Decimal(0))

    try:
        loop_budget = LoopBudget(
            max_trials_per_round=_int(
                budget["max_trials_per_round"], f"{label}.budget max_trials_per_round", minimum=0
            ),
            max_trials_total=_int(
                budget["max_trials_total"], f"{label}.budget max_trials_total", minimum=0
            ),
            max_llm_cost_units=_decimal(
                budget["max_llm_cost_units"],
                f"{label}.budget max_llm_cost_units",
                minimum=Decimal(0),
            ),
            max_compute_seconds=_decimal(
                budget["max_compute_seconds"],
                f"{label}.budget max_compute_seconds",
                minimum=Decimal(0),
            ),
        )
    except ValueError as exc:
        raise OperatorConfigError(f"{label}.budget is invalid: {exc}") from exc
    return DatasetLoopValues(
        loop_id=_str(table["loop_id"], f"{label} loop_id"),
        seed=_int(table["seed"], f"{label} seed", minimum=0),
        epoch=_utc(table["epoch"], f"{label} epoch"),
        cadence=_duration(table["cadence"], f"{label} cadence", positive=True),
        budget=loop_budget,
        family_id=_str(table["family_id"], f"{label} family_id"),
        knowledge=knowledge,
        max_new_hypotheses_per_round=_int(
            table["max_new_hypotheses_per_round"],
            f"{label} max_new_hypotheses_per_round",
            minimum=0,
        ),
        max_reevaluations_per_round=_int(
            table["max_reevaluations_per_round"], f"{label} max_reevaluations_per_round", minimum=0
        ),
        hypothesis_compute_seconds=compute("hypothesis_compute_seconds"),
        compute_seconds_per_trial=compute("compute_seconds_per_trial"),
        validation_compute_seconds=compute("validation_compute_seconds"),
        state_compute_seconds=compute("state_compute_seconds"),
        profile=_artifact(table["profile"], f"{label} profile", base, ValidationProfile),
        constitution_version=_str(
            table["constitution_version"], f"{label} constitution_version", _SEMVER_RE
        ),
        llm_prompt=None,
        llm_cost_units_per_call=llm_cost,
    )


def _operator_identity(
    providers: tuple[ProviderIdentity, ...],
    source: DatasetSourceValues,
    values: DatasetLoopValues,
    wiring: WiringValues,
) -> str:
    """SHA-256 of the semantic configuration (module docs); no path and no ``--rounds``."""
    budget: Mapping[str, Any] = {
        "max_trials_per_round": values.budget.max_trials_per_round,
        "max_trials_total": values.budget.max_trials_total,
        "max_llm_cost_units": exact_decimal_text(values.budget.max_llm_cost_units),
        "max_compute_seconds": exact_decimal_text(values.budget.max_compute_seconds),
    }
    payload = {
        "format": _IDENTITY_FORMAT,
        "schema_version": CONFIG_SCHEMA_VERSION,
        "operator": {"llm_enabled": False},
        "providers": {identity.role: identity.payload() for identity in providers},
        "source": source.payload(),
        "loop": {
            "loop_id": values.loop_id,
            "seed": values.seed,
            "epoch": values.epoch.isoformat(),
            "cadence_microseconds": values.cadence // timedelta(microseconds=1),
            "budget": dict(budget),
            "family_id": values.family_id,
            "knowledge": [item.content_hash() for item in values.knowledge],
            "max_new_hypotheses_per_round": values.max_new_hypotheses_per_round,
            "max_reevaluations_per_round": values.max_reevaluations_per_round,
            "hypothesis_compute_seconds": exact_decimal_text(values.hypothesis_compute_seconds),
            "compute_seconds_per_trial": exact_decimal_text(values.compute_seconds_per_trial),
            "validation_compute_seconds": exact_decimal_text(values.validation_compute_seconds),
            "state_compute_seconds": exact_decimal_text(values.state_compute_seconds),
            "profile": values.profile.content_hash(),
            "constitution_version": values.constitution_version,
            "llm_prompt": None,
            "llm_cost_units_per_call": exact_decimal_text(values.llm_cost_units_per_call),
        },
        "wiring": wiring_identity(wiring),
    }
    return content_hash(payload)

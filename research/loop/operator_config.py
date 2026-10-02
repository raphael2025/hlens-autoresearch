"""Strict TOML v1 configuration of the synthetic-loop operator (ADR-0074 §2 / §4.1 / §4.5).

``load_operator_config(path)`` reads one UTF-8 TOML file and returns an ``OperatorConfig``: every
value typed, every relative path resolved against the file's directory, every hash-bound artifact
loaded through its existing contract model and checked against its declared ``content_hash``, and
the Profile checked against the configured ADR-0062 ``ProfileFreezeRegistry``. Any deviation is an
``OperatorConfigError`` (ADR-0074 §8: exit code 2); nothing is defaulted, merged, interpolated or
searched for. The one exception is a freeze registry that cannot be read, is corrupted or is locked
by another writer: that is a ``FreezeRegistryFault`` (exit code 5), kept apart so the operator can
map it; an empty registry or a Profile without a matching freeze stays an ``OperatorConfigError``.

**What this module does not do.** It holds no provider objects and imports no provider code: the
six ``[providers.<role>]`` tables are parsed into ``ProviderIdentity`` values only. The static
allowlist (ADR-0074 §3) lives in ``research.loop.operator_providers``, which recomputes each
descriptor and hands the verified objects back as ``InjectedProviders``;
``OperatorConfig.build(providers)`` then compiles exactly one existing ``SyntheticLoopConfig`` /
``LoopWiring``. There is no dynamic import, entry point, ``module:factory`` or path-to-code lookup
here. It does not open the loop state directory, check anchors or locks, run Git, or read the
environment lock file (``code_commit`` / ``environment_lock`` are format-checked only; binding
them to the trusted worktree is the operator's job, ADR-0074 §3.3).

**Freeze registry.** ``freeze_registry_dir`` / ``freeze_registry_anchor`` must already exist (the
journal, the blob directory and the anchor file); the registry is opened read-only in intent —
never ``register_freeze`` — its records replayed, and the Profile must have an exact
``frozen_record`` (ref, content hash and cited calibration report). ``ProfileFreezeRegistry`` may
create its ``.lock`` file, and its own open may append the single missing anchor tail record of the
ADR-0062 crash window (journal record fsync'd, anchor record not yet written); this module adds no
other recovery and writes nothing else (ADR-0074 §4.5). Today the registry is empty and no
production Profile is frozen, so every configuration is refused: that is the intended state, not a
defect.

**Schema** (``schema_version = "1.0.0"``; every table closed, every key required):

- ``[operator]``: ``llm_enabled = false``.
- ``[paths]``: ``state_dir``, ``state_anchor``, ``bus_anchor``, ``reports_root``,
  ``freeze_registry_dir``, ``freeze_registry_anchor`` — pairwise distinct and non-nested, and no
  two existing entries (nor an anchor and the freeze journal) the same file (hardlink / alias).
- ``[providers.<role>]`` for each of ``PROVIDER_ROLES``: ``id``, ``version``, ``descriptor_hash``.
- ``[loop]``: the ``SyntheticLoopConfig`` field names (``wiring`` excluded), with ``[loop.budget]``
  holding the four ``LoopBudget`` fields; ``market``, ``profile`` and each ``knowledge`` element
  are artifact references; ``knowledge`` is a non-empty array.
- ``[loop.wiring]``: the ``LoopWiring`` field names except the four provider-object fields (which
  come from ``[providers]``), plus the compile-only ``profile_selection_rule`` and ``outcome_spec``
  references (loaded and cross-checked, never passed to ``LoopWiring``); ``strategies`` is an array
  of ``{ spec = <artifact ref>, hypothesis_family_id = "..." }`` whose family is ``loop.family_id``;
  ``[loop.wiring.robustness]`` holds the six ``RobustnessParams`` fields, each required, where an
  explicit ``false`` means ``None`` (no value given; ``true`` is refused); ``evolution``,
  ``oos_unseal``, ``sealed_decision_step``, ``conditional``, ``hypothesis_batch`` and
  ``knowledge_source`` must each be ``false`` (compiled to ``None``).

An artifact reference is exactly ``{ path = "...", content_hash = "<sha256>" }``; the file is
strict JSON (no duplicate keys, no NaN / Infinity) that must be the model's own
``model_dump(mode="json")`` (so no field is left to a model default) and whose ``content_hash()``
equals the declared hash. Times are strings with a zero UTC offset; ``timedelta`` values are
``"<canonical decimal> seconds"`` with at most six fractional digits; ``Decimal`` values are
canonical decimal strings (TOML floats are refused everywhere). ``RobustnessParams`` float fields
are canonical decimal strings whose shortest float repr denotes the same value (or ``false``);
``cscv_partitions`` is a positive TOML integer (or ``false``). Strings wrapped in
``<...>`` are placeholders and strings marking TEST ONLY content are refused, in the TOML and in
every artifact.

**Cross-checks** (ADR-0074 §2.6 / §3.2): ``outcome_spec`` is exactly what ``label_spec`` binds
(``outcome`` ref, ``outcome_spec_hash``, ``horizon``); ``profile_selection_rule`` is exactly the
selection's rule and selects the configured Profile; the selection key's venue / symbol / timeframe
/ research_class equal the Profile's ``scope``; ``declared_research_class`` equals both; the
Profile's ``cost_stress.cost_model`` is the configured ``cost_model``; every strategy's
``hypothesis_family_id`` is ``loop.family_id``.

``OperatorConfig.operator_identity`` is the SHA-256 of the ADR-0074 §5 semantic configuration
(schema version, provider identities, every loop / wiring value or content hash — including the
compile-only ``profile_selection_rule`` and ``outcome_spec`` — and the explicit disables); paths
are not part of it.
"""

from __future__ import annotations

import json
import math
import os
import re
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from apps.worker.loop import LoopBudget
from core.contracts.cost_model import CostModelSpec
from core.contracts.feature import FeatureProvider
from core.contracts.outcome import OutcomeLabelSpec, OutcomeProvider
from core.contracts.profile_selection import ProfileSelectionRule
from core.contracts.state import StateProvider
from core.contracts.strategy import BacktestProvider, StrategyProvider
from core.contracts.synthetic import SyntheticMarketProvider, SyntheticMarketSpec
from core.contracts.validation_profile import ProfileStatus, ValidationProfile
from core.domain.base import (
    GIT_OID_PATTERN,
    NAME_PATTERN,
    RESEARCH_CLASS_PATTERN,
    SEMVER_PATTERN,
    SHA256_PATTERN,
    Contract,
    canonical_json,
    content_hash,
    exact_decimal,
    exact_decimal_text,
)
from core.domain.research import KnowledgeItem
from core.domain.selection import ProfileSelection
from core.domain.specs import FeatureSpec, OutcomeSpec, StateSpec, StrategySpec
from core.errors import ProfileViolation
from infrastructure.event_bus.journal import JournalCorrupted
from infrastructure.registry.blobs import BlobCorrupted
from infrastructure.registry.profile_freeze import ProfileFreezeRegistry
from infrastructure.registry.registry import RegistryError
from research.loop.compose import LoopWiring, SyntheticLoopConfig
from research.strategies.pipeline import StrategyCandidate
from research.strategies.signals import LOG_RETURN_SIGNAL
from research.validation import RobustnessParams

__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "DISABLED_WIRING_FIELDS",
    "PROVIDER_ROLES",
    "CompiledOperatorConfig",
    "FreezeEvidence",
    "FreezeRegistryFault",
    "InjectedProviders",
    "LoopValues",
    "OperatorConfig",
    "OperatorConfigError",
    "OperatorPaths",
    "ProviderIdentity",
    "StrategyEntry",
    "WiringValues",
    "compile_wiring",
    "cross_check_bindings",
    "load_operator_config",
    "wiring_identity",
]

#: The one supported configuration schema (ADR-0074 §2.1); any other value is refused.
CONFIG_SCHEMA_VERSION: Final = "1.0.0"
#: The identity payload's own format tag (changes whenever the payload's shape changes).
_IDENTITY_FORMAT: Final = "hlens.loop-operator-identity@1.0.0"
# Process-local marker set only by operator_providers after static allowlist checks. It is not a
# security boundary against code already executing in this Python process; it prevents ordinary
# callers from accidentally treating a hand-constructed InjectedProviders as verified.
_ALLOWLIST_VALIDATION_SEAL: Final = object()
#: ``[providers.<role>]`` roles, in the order they are reported and hashed (ADR-0074 §2.2).
PROVIDER_ROLES: Final = (
    "synthetic_market_provider",
    "feature_provider",
    "state_provider",
    "strategy_provider",
    "backtester",
    "outcome_provider",
)
#: ``[loop.wiring]`` modes that v1 accepts only as an explicit ``false`` (ADR-0074 §2.4).
DISABLED_WIRING_FIELDS: Final = (
    "evolution",
    "oos_unseal",
    "sealed_decision_step",
    "conditional",
    "hypothesis_batch",
    "knowledge_source",
)

_PATH_KEYS: Final = (
    "state_dir",
    "state_anchor",
    "bus_anchor",
    "reports_root",
    "freeze_registry_dir",
    "freeze_registry_anchor",
)
_TOP_KEYS: Final = frozenset({"schema_version", "operator", "paths", "providers", "loop"})
_PROVIDER_KEYS: Final = frozenset({"id", "version", "descriptor_hash"})
_REF_KEYS: Final = frozenset({"path", "content_hash"})
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
        "market",
        "minutes_per_round",
        "compute_seconds_per_bar",
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
_WIRING_KEYS: Final = frozenset(
    {
        "feature_spec",
        "feature_chunk_bars",
        "state_spec",
        "decision_step",
        "decision_warmup",
        "strategies",
        "cost_model",
        "initial_equity",
        "label_spec",
        "robustness",
        "profile_selection",
        "profile_selection_rule",
        "outcome_spec",
        "declared_research_class",
        "code_commit",
        "environment_lock",
        *DISABLED_WIRING_FIELDS,
    }
)
_STRATEGY_KEYS: Final = frozenset({"spec", "hypothesis_family_id"})
_ROBUSTNESS_FLOAT_KEYS: Final = (
    "max_participation_rate",
    "min_capacity",
    "impact_coefficient",
    "cross_asset_min_positive_fraction",
    "max_undersampled_pnl_share",
)
_ROBUSTNESS_KEYS: Final = frozenset({"cscv_partitions", *_ROBUSTNESS_FLOAT_KEYS})

_SHA256_RE: Final = re.compile(SHA256_PATTERN)
_NAME_RE: Final = re.compile(NAME_PATTERN)
_SEMVER_RE: Final = re.compile(SEMVER_PATTERN)
_RESEARCH_CLASS_RE: Final = re.compile(RESEARCH_CLASS_PATTERN)
_GIT_OID_RE: Final = re.compile(GIT_OID_PATTERN)
_DURATION_RE: Final = re.compile(r"(0|[1-9][0-9]*)(?:\.([0-9]{0,5}[1-9]))? seconds")
_PLACEHOLDER_RE: Final = re.compile(r"\s*<.*>\s*", re.DOTALL)
_TEST_ONLY_RE: Final = re.compile(r"test[\s_-]*only", re.IGNORECASE)


class OperatorConfigError(ValueError):
    """The configuration is refused (ADR-0074 §8: exit code 2, nothing was run or opened)."""


class FreezeRegistryFault(RuntimeError):
    """The configured freeze registry cannot be read, is corrupted (journal, blobs or anchor) or is
    locked by another writer (ADR-0074 §8: exit code 5). Not an ``OperatorConfigError``: an empty
    registry or a Profile without an exact freeze record is a configuration refusal instead."""


# ---- result types ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OperatorPaths:
    """The six ``[paths]`` entries, absolute and resolved (plus the config file itself)."""

    config_file: Path
    state_dir: Path
    state_anchor: Path
    bus_anchor: Path
    reports_root: Path
    freeze_registry_dir: Path
    freeze_registry_anchor: Path


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """One ``[providers.<role>]`` table: which allowlisted provider, as declared."""

    role: str
    id: str
    version: str
    descriptor_hash: str

    def payload(self) -> dict[str, str]:
        return {"id": self.id, "version": self.version, "descriptor_hash": self.descriptor_hash}


@dataclass(frozen=True, slots=True)
class StrategyEntry:
    """One ``strategies`` element: a hash-bound ``StrategySpec`` and its hypothesis family. The
    ``StrategyProvider`` is injected at ``build`` time; v1 registers no risk policy / provider."""

    spec: StrategySpec
    hypothesis_family_id: str


@dataclass(frozen=True, slots=True)
class FreezeEvidence:
    """The verified ADR-0062 record freezing the configured Profile, and the registry's anchor
    snapshot (record count, head) at load time. A snapshot, not a lease: the registry is closed
    again before ``load_operator_config`` returns."""

    freeze_id: str
    profile_ref: str
    profile_hash: str
    calibration_report_hash: str
    approved_by: str
    approved_at: datetime
    anchor_length: int
    anchor_head: str


@dataclass(frozen=True, slots=True)
class WiringValues:
    """Every ``LoopWiring`` value except the four provider objects, typed and verified.

    ``profile_selection_rule`` and ``outcome_spec`` are compile-only (ADR-0074 §2.2) and never
    reach ``LoopWiring``; the six ``DISABLED_WIRING_FIELDS`` were each an explicit ``false`` and
    compile to ``None``."""

    feature_spec: FeatureSpec
    feature_chunk_bars: int
    state_spec: StateSpec
    decision_step: timedelta
    decision_warmup: timedelta
    strategies: tuple[StrategyEntry, ...]
    cost_model: CostModelSpec
    initial_equity: Decimal
    label_spec: OutcomeLabelSpec
    outcome_spec: OutcomeSpec
    robustness: RobustnessParams
    profile_selection: ProfileSelection
    profile_selection_rule: ProfileSelectionRule
    declared_research_class: str
    code_commit: str
    environment_lock: str


@dataclass(frozen=True, slots=True)
class LoopValues:
    """Every ``SyntheticLoopConfig`` value except ``wiring``, typed and verified.

    ``llm_prompt`` is ``None``: v1 accepts only ``llm_prompt = ""`` with ``llm_enabled = false``."""

    loop_id: str
    seed: int
    epoch: datetime
    cadence: timedelta
    budget: LoopBudget
    market: SyntheticMarketSpec
    minutes_per_round: int
    compute_seconds_per_bar: Decimal
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
class InjectedProviders:
    """The six provider objects ``research.loop.operator_providers`` created from its static
    allowlist, with the identities it verified them against.

    This module does not check that an object *is* the provider its identity names (descriptor
    recomputation and spec agreement are ``operator_providers``' job, ADR-0074 §3.1); ``build``
    only refuses a set verified against identities other than the configuration's."""

    identities: tuple[ProviderIdentity, ...]
    synthetic_market_provider: SyntheticMarketProvider
    feature_provider: FeatureProvider
    state_provider: StateProvider
    strategy_provider: StrategyProvider
    backtester: BacktestProvider
    outcome_provider: OutcomeProvider
    _allowlist_seal: object = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class CompiledOperatorConfig:
    """What the operator hands to ``research.loop.compose.open_synthetic_loop``."""

    loop_config: SyntheticLoopConfig
    market_provider: SyntheticMarketProvider
    operator_identity: str
    paths: OperatorPaths


@dataclass(frozen=True, slots=True)
class OperatorConfig:
    """A fully parsed and verified v1 configuration, without provider objects (module docs)."""

    schema_version: str
    paths: OperatorPaths
    providers: tuple[ProviderIdentity, ...]
    loop: LoopValues
    wiring: WiringValues
    freeze: FreezeEvidence
    operator_identity: str

    def provider(self, role: str) -> ProviderIdentity:
        for identity in self.providers:
            if identity.role == role:
                return identity
        raise KeyError(role)

    def build(self, providers: InjectedProviders) -> CompiledOperatorConfig:
        """Compile exactly one ``SyntheticLoopConfig`` (every field set) from these values and the
        injected providers. One-way: nothing here reads files or mutates the configuration."""
        if not isinstance(providers, InjectedProviders):
            raise OperatorConfigError("providers must be an InjectedProviders")
        if providers.identities != self.providers:
            raise OperatorConfigError(
                "the injected providers were verified against other identities than the "
                "configuration's [providers] tables"
            )
        if providers._allowlist_seal is not _ALLOWLIST_VALIDATION_SEAL:
            raise OperatorConfigError(
                "providers were not constructed and verified by the static allowlist"
            )
        for role in PROVIDER_ROLES:
            if getattr(providers, role) is None:
                raise OperatorConfigError(f"no {role} was injected")
        wiring = compile_wiring(
            self.wiring,
            feature_provider=providers.feature_provider,
            state_provider=providers.state_provider,
            strategy_provider=providers.strategy_provider,
            backtester=providers.backtester,
            outcome_provider=providers.outcome_provider,
        )
        v = self.loop
        loop_config = SyntheticLoopConfig(
            loop_id=v.loop_id,
            seed=v.seed,
            epoch=v.epoch,
            cadence=v.cadence,
            budget=v.budget,
            market=v.market,
            minutes_per_round=v.minutes_per_round,
            compute_seconds_per_bar=v.compute_seconds_per_bar,
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
        return CompiledOperatorConfig(
            loop_config=loop_config,
            market_provider=providers.synthetic_market_provider,
            operator_identity=self.operator_identity,
            paths=self.paths,
        )


def compile_wiring(
    w: WiringValues,
    *,
    feature_provider: FeatureProvider,
    state_provider: StateProvider,
    strategy_provider: StrategyProvider,
    backtester: BacktestProvider,
    outcome_provider: OutcomeProvider,
) -> LoopWiring:
    """The one ``LoopWiring`` of verified wiring values and allowlist-verified providers (every
    ``DISABLED_WIRING_FIELDS`` mode ``None``). Shared by ``OperatorConfig.build`` and the
    Dataset-sourced operator configuration (ADR-0105 §5)."""
    try:
        strategies = tuple(
            StrategyCandidate(
                spec=entry.spec,
                strategy=strategy_provider,
                hypothesis_family_id=entry.hypothesis_family_id,
            )
            for entry in w.strategies
        )
    except ValueError as exc:
        raise OperatorConfigError(f"a strategy cannot be wired: {exc}") from exc
    return LoopWiring(
        feature_provider=feature_provider,
        feature_spec=w.feature_spec,
        feature_chunk_bars=w.feature_chunk_bars,
        state_provider=state_provider,
        state_spec=w.state_spec,
        decision_step=w.decision_step,
        decision_warmup=w.decision_warmup,
        strategies=strategies,
        backtester=backtester,
        cost_model=w.cost_model,
        initial_equity=w.initial_equity,
        outcome_provider=outcome_provider,
        label_spec=w.label_spec,
        robustness=w.robustness,
        profile_selection=w.profile_selection,
        declared_research_class=w.declared_research_class,
        code_commit=w.code_commit,
        environment_lock=w.environment_lock,
        evolution=None,
        oos_unseal=None,
        sealed_decision_step=None,
        conditional=None,
        hypothesis_batch=None,
        knowledge_source=None,
    )


# ---- entry point ----------------------------------------------------------------------------


def load_operator_config(path: Path) -> OperatorConfig:
    """Parse, type, resolve and verify one v1 configuration file (module docs).

    Raises ``OperatorConfigError`` for every refusal, including an unreadable file, a missing
    freeze registry, an empty one and a Profile without an exact freeze record; raises
    ``FreezeRegistryFault`` when the configured registry cannot be read, is corrupted or locked."""
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

    loop = _table(raw["loop"], "[loop]", _LOOP_KEYS)
    wiring = _wiring(_table(loop["wiring"], "[loop.wiring]", _WIRING_KEYS), base)
    values = _loop_values(loop, base)
    _cross_check(values, wiring)
    freeze = _freeze_evidence(paths, values.profile)
    return OperatorConfig(
        schema_version=CONFIG_SCHEMA_VERSION,
        paths=paths,
        providers=providers,
        loop=values,
        wiring=wiring,
        freeze=freeze,
        operator_identity=_operator_identity(providers, values, wiring),
    )


# ---- reading --------------------------------------------------------------------------------


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise OperatorConfigError(f"cannot read the configuration {path}: {exc}") from exc
    if data.startswith(b"\xef\xbb\xbf"):
        raise OperatorConfigError("the configuration must be UTF-8 without a byte-order mark")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise OperatorConfigError("the configuration is not valid UTF-8") from exc
    try:
        # tomllib refuses duplicate keys and redefined tables; floats are refused below.
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise OperatorConfigError(f"the configuration is not valid TOML: {exc}") from exc


def _scan_strings(value: Any, label: str) -> None:
    """Refuse placeholders, TEST ONLY markers and floats anywhere in a parsed document."""
    if isinstance(value, str):
        if _PLACEHOLDER_RE.fullmatch(value) is not None:
            raise OperatorConfigError(f"{label} contains a placeholder value {value!r}")
        if _TEST_ONLY_RE.search(value) is not None:
            raise OperatorConfigError(f"{label} contains TEST ONLY content")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _scan_strings(key, label)
            _scan_strings(item, label)
    elif isinstance(value, list | tuple):
        for item in value:
            _scan_strings(item, label)


def _reject_floats(value: Any, label: str) -> None:
    if isinstance(value, float):
        raise OperatorConfigError(f"{label}: TOML floats are not accepted (use decimal strings)")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_floats(item, f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_floats(item, f"{label}[{index}]")


def _reject_nonfinite_json_numbers(value: Any, label: str) -> None:
    """Contracts may serialize finite float fields as JSON numbers; reject only NaN / infinity."""
    if isinstance(value, float) and not math.isfinite(value):
        raise OperatorConfigError(f"{label} contains a non-finite JSON number")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_nonfinite_json_numbers(item, f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_nonfinite_json_numbers(item, f"{label}[{index}]")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OperatorConfigError("an artifact JSON object has a duplicate key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise OperatorConfigError("an artifact contains a non-finite number")


def _read_json(path: Path, label: str) -> Any:
    if not path.is_file():
        raise OperatorConfigError(f"{label}: {path} is not an existing file")
    try:
        text = path.read_bytes().decode("utf-8", errors="strict")
    except OSError as exc:
        raise OperatorConfigError(f"{label}: cannot read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise OperatorConfigError(f"{label}: {path} is not valid UTF-8") from exc
    try:
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise OperatorConfigError(f"{label}: {path} is not valid JSON") from exc


# ---- scalar types ---------------------------------------------------------------------------


def _table(value: Any, label: str, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OperatorConfigError(f"{label} must be a table")
    missing = keys - value.keys()
    unknown = value.keys() - keys
    if missing or unknown:
        raise OperatorConfigError(
            f"{label}: missing keys {sorted(missing)}, unknown keys {sorted(unknown)}"
        )
    _reject_floats(value, label)
    return value


def _str(value: Any, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OperatorConfigError(f"{label} must be a non-empty string without surrounding space")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise OperatorConfigError(f"{label} does not have the required format")
    return value


def _int(value: Any, label: str, *, minimum: int) -> int:
    if type(value) is not int:  # bool is an int subclass: refused here
        raise OperatorConfigError(f"{label} must be an integer")
    if value < minimum:
        raise OperatorConfigError(f"{label} must be >= {minimum}")
    return value


def _decimal(value: Any, label: str, *, minimum: Decimal | None, strict: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise OperatorConfigError(f"{label} must be a canonical decimal string")
    try:
        result = exact_decimal(value)
    except ValueError as exc:
        raise OperatorConfigError(f"{label} must be a canonical decimal string") from exc
    if minimum is not None and (result <= minimum if strict else result < minimum):
        raise OperatorConfigError(f"{label} must be {'>' if strict else '>='} {minimum}")
    return result


def _duration(value: Any, label: str, *, positive: bool) -> timedelta:
    if not isinstance(value, str):
        raise OperatorConfigError(f'{label} must be a string "<decimal> seconds"')
    match = _DURATION_RE.fullmatch(value)
    if match is None:
        raise OperatorConfigError(
            f'{label} must be "<canonical decimal> seconds" with at most six fractional digits'
        )
    whole, fraction = match.group(1), match.group(2) or ""
    microseconds = int(whole) * 1_000_000 + int(fraction.ljust(6, "0") or "0")
    try:
        result = timedelta(microseconds=microseconds)
    except OverflowError as exc:
        raise OperatorConfigError(f"{label} is out of range") from exc
    if positive and result <= timedelta(0):
        raise OperatorConfigError(f"{label} must be positive")
    return result


def _utc(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise OperatorConfigError(f"{label} must be a UTC ISO-8601 string")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise OperatorConfigError(f"{label} must be a UTC ISO-8601 string") from exc
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise OperatorConfigError(f"{label} must carry an explicit zero UTC offset")
    return result.astimezone(UTC)


def _float_param(value: Any, label: str) -> float:
    """A ``RobustnessParams`` float, written as a canonical decimal whose float is that value."""
    exact = _decimal(value, label, minimum=None)
    result = float(exact)
    if Decimal(repr(result)) != exact:
        raise OperatorConfigError(f"{label} is not exactly representable as its float repr")
    return result


def _nullable[T](value: Any, label: str, parse: Callable[[Any, str], T]) -> T | None:
    """A nullable ``RobustnessParams`` value: TOML ``false`` is ``None``; ``true`` is refused."""
    if value is False:
        return None
    if value is True:
        raise OperatorConfigError(f"{label}: true is not accepted (use false for no value)")
    return parse(value, label)


def _disabled(value: Any, label: str) -> None:
    if value is not False:
        raise OperatorConfigError(f"{label} must be false in schema v1 (ADR-0074 §2.4)")


# ---- paths ----------------------------------------------------------------------------------


def _resolve(value: Any, label: str, base: Path) -> Path:
    text = _str(value, label)
    if "\x00" in text:
        raise OperatorConfigError(f"{label} contains a NUL byte")
    return (base / Path(text)).resolve()


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _same_file(a: Path, b: Path) -> bool:
    """Whether two paths that both exist are one file (hardlink, bind mount or other alias)."""
    if not (os.path.lexists(a) and os.path.lexists(b)):
        return False
    try:
        return a.samefile(b)
    except OSError as exc:
        raise OperatorConfigError(f"[paths] cannot compare {a} and {b}: {exc}") from exc


def _no_aliases(resolved: Mapping[str, Path]) -> None:
    """Refuse existing ``[paths]`` entries that are one file under two names, and an anchor that
    is the freeze registry's own journal (ADR-0074 §4.1: no hardlink / alias bypass)."""
    for index, first in enumerate(_PATH_KEYS):
        for second in _PATH_KEYS[index + 1 :]:
            if _same_file(resolved[first], resolved[second]):
                raise OperatorConfigError(f"[paths] {first} and {second} are the same file")
    for key in ("state_anchor", "bus_anchor", "freeze_registry_anchor"):
        path = resolved[key]
        if os.path.lexists(path):
            try:
                if path.stat().st_nlink > 1:
                    raise OperatorConfigError(f"[paths] {key} is a hardlink with another file")
            except OSError as exc:
                raise OperatorConfigError(f"[paths] cannot inspect {key}: {exc}") from exc
    journal = resolved["freeze_registry_dir"] / "freezes.jsonl"
    for key in ("state_anchor", "bus_anchor", "freeze_registry_anchor"):
        if _same_file(resolved[key], journal):
            raise OperatorConfigError(f"[paths] {key} is the freeze registry journal")


def _paths(table: dict[str, Any], base: Path, config_file: Path) -> OperatorPaths:
    resolved = {key: _resolve(table[key], f"[paths] {key}", base) for key in _PATH_KEYS}
    for index, first in enumerate(_PATH_KEYS):
        for second in _PATH_KEYS[index + 1 :]:
            if _overlap(resolved[first], resolved[second]):
                raise OperatorConfigError(
                    f"[paths] {first} and {second} are the same path or nest in one another"
                )
    _no_aliases(resolved)
    for key in ("state_anchor", "bus_anchor", "freeze_registry_anchor"):
        if _same_file(resolved[key], config_file):
            raise OperatorConfigError(f"[paths] {key} is the operator configuration file")
    for key in ("state_dir", "reports_root"):
        if resolved[key].exists() and not resolved[key].is_dir():
            raise OperatorConfigError(f"[paths] {key} exists and is not a directory")
    for key in ("state_anchor", "bus_anchor"):
        if resolved[key].exists() and not resolved[key].is_file():
            raise OperatorConfigError(f"[paths] {key} exists and is not a regular file")
    registry = resolved["freeze_registry_dir"]
    if not registry.is_dir():
        raise OperatorConfigError("[paths] freeze_registry_dir must be an existing directory")
    # Refused before ProfileFreezeRegistry could create an empty registry in their place.
    if not (registry / "freezes.jsonl").is_file() or not (registry / "blobs").is_dir():
        raise OperatorConfigError(
            "[paths] freeze_registry_dir holds no existing registry journal and blob directory"
        )
    if not resolved["freeze_registry_anchor"].is_file():
        raise OperatorConfigError("[paths] freeze_registry_anchor must be an existing file")
    return OperatorPaths(config_file=config_file, **resolved)


# ---- providers ------------------------------------------------------------------------------


def _providers(value: Any) -> tuple[ProviderIdentity, ...]:
    table = _table(value, "[providers]", frozenset(PROVIDER_ROLES))
    identities = []
    for role in PROVIDER_ROLES:
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


# ---- artifacts ------------------------------------------------------------------------------


def _artifact[M: Contract](value: Any, label: str, base: Path, model: type[M]) -> M:
    """Load one ``{ path, content_hash }`` reference through its contract model (module docs)."""
    ref = _table(value, label, _REF_KEYS)
    path = _resolve(ref["path"], f"{label} path", base)
    declared = _str(ref["content_hash"], f"{label} content_hash", _SHA256_RE)
    raw = _read_json(path, label)
    _scan_strings(raw, label)
    _reject_nonfinite_json_numbers(raw, label)
    try:
        loaded = model.model_validate_json(canonical_json(raw))
    except (ValidationError, TypeError, ValueError) as exc:
        raise OperatorConfigError(f"{label} does not validate as {model.__name__}: {exc}") from exc
    if canonical_json(loaded.model_dump(mode="json")) != canonical_json(raw):
        raise OperatorConfigError(
            f"{label} is not its own canonical {model.__name__} JSON (a field is missing, "
            "defaulted or not in canonical form)"
        )
    actual = loaded.content_hash()
    if actual != declared:
        raise OperatorConfigError(f"{label}: content_hash {actual} is not the declared {declared}")
    return loaded


def _artifacts[M: Contract](value: Any, label: str, base: Path, model: type[M]) -> tuple[M, ...]:
    if not isinstance(value, list):
        raise OperatorConfigError(f"{label} must be an array")
    return tuple(_artifact(item, f"{label}[{i}]", base, model) for i, item in enumerate(value))


def _strategies(value: Any, base: Path) -> tuple[StrategyEntry, ...]:
    if not isinstance(value, list) or not value:
        raise OperatorConfigError("[loop.wiring] strategies must be a non-empty array")
    entries = []
    for index, item in enumerate(value):
        label = f"[loop.wiring] strategies[{index}]"
        table = _table(item, label, _STRATEGY_KEYS)
        spec = _artifact(table["spec"], f"{label} spec", base, StrategySpec)
        if spec.risk_policy is not None:
            raise OperatorConfigError(f"{label}: v1 registers no risk policy / risk provider")
        entries.append(
            StrategyEntry(
                spec=spec,
                hypothesis_family_id=_str(
                    table["hypothesis_family_id"], f"{label} hypothesis_family_id"
                ),
            )
        )
    refs = [entry.spec.ref.target_identity() for entry in entries]
    if len(set(refs)) != len(refs):
        raise OperatorConfigError("[loop.wiring] strategies lists one StrategySpec twice")
    return tuple(entries)


# ---- loop / wiring --------------------------------------------------------------------------


def _wiring(table: dict[str, Any], base: Path) -> WiringValues:
    label = "[loop.wiring]"
    for name in DISABLED_WIRING_FIELDS:
        _disabled(table[name], f"{label} {name}")
    robustness = _table(table["robustness"], f"{label}.robustness", _ROBUSTNESS_KEYS)
    return WiringValues(
        feature_spec=_artifact(table["feature_spec"], f"{label} feature_spec", base, FeatureSpec),
        feature_chunk_bars=_int(
            table["feature_chunk_bars"], f"{label} feature_chunk_bars", minimum=1
        ),
        state_spec=_artifact(table["state_spec"], f"{label} state_spec", base, StateSpec),
        decision_step=_duration(table["decision_step"], f"{label} decision_step", positive=True),
        decision_warmup=_duration(
            table["decision_warmup"], f"{label} decision_warmup", positive=False
        ),
        strategies=_strategies(table["strategies"], base),
        cost_model=_artifact(table["cost_model"], f"{label} cost_model", base, CostModelSpec),
        initial_equity=_decimal(
            table["initial_equity"], f"{label} initial_equity", minimum=Decimal(0), strict=True
        ),
        label_spec=_artifact(table["label_spec"], f"{label} label_spec", base, OutcomeLabelSpec),
        outcome_spec=_artifact(table["outcome_spec"], f"{label} outcome_spec", base, OutcomeSpec),
        robustness=RobustnessParams(
            cscv_partitions=_nullable(
                robustness["cscv_partitions"],
                f"{label}.robustness cscv_partitions",
                _cscv_partitions,
            ),
            **{
                key: _nullable(robustness[key], f"{label}.robustness {key}", _float_param)
                for key in _ROBUSTNESS_FLOAT_KEYS
            },
        ),
        profile_selection=_artifact(
            table["profile_selection"], f"{label} profile_selection", base, ProfileSelection
        ),
        profile_selection_rule=_artifact(
            table["profile_selection_rule"],
            f"{label} profile_selection_rule",
            base,
            ProfileSelectionRule,
        ),
        declared_research_class=_str(
            table["declared_research_class"],
            f"{label} declared_research_class",
            _RESEARCH_CLASS_RE,
        ),
        code_commit=_str(table["code_commit"], f"{label} code_commit", _GIT_OID_RE),
        environment_lock=_str(table["environment_lock"], f"{label} environment_lock"),
    )


def _loop_values(table: dict[str, Any], base: Path) -> LoopValues:
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
    return LoopValues(
        loop_id=_str(table["loop_id"], f"{label} loop_id"),
        seed=_int(table["seed"], f"{label} seed", minimum=0),
        epoch=_utc(table["epoch"], f"{label} epoch"),
        cadence=_duration(table["cadence"], f"{label} cadence", positive=True),
        budget=loop_budget,
        market=_artifact(table["market"], f"{label} market", base, SyntheticMarketSpec),
        minutes_per_round=_int(table["minutes_per_round"], f"{label} minutes_per_round", minimum=1),
        compute_seconds_per_bar=compute("compute_seconds_per_bar"),
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


def _cross_check(values: LoopValues, wiring: WiringValues) -> None:
    """The outcome, Profile / ProfileSelection / ProfileSelectionRule, cost model and strategy
    family bindings (ADR-0074 §2.6 / §2.8 / §3.2; module docs, **Cross-checks**)."""
    cross_check_bindings(values.profile, values.family_id, wiring)


def cross_check_bindings(profile: ValidationProfile, family_id: str, wiring: WiringValues) -> None:
    """``_cross_check`` over the Profile and the family id alone (shared with the Dataset-sourced
    operator configuration, ADR-0105 §5): the Profile must be FROZEN, among the other bindings."""
    label_spec = wiring.label_spec
    outcome = wiring.outcome_spec
    if label_spec.outcome.target_identity() != outcome.ref.target_identity():
        raise OperatorConfigError(
            f"outcome_spec is {outcome.ref}, label_spec binds {label_spec.outcome}"
        )
    if label_spec.outcome_spec_hash != outcome.content_hash():
        raise OperatorConfigError("outcome_spec's content hash is not label_spec.outcome_spec_hash")
    if label_spec.horizon != outcome.horizon:
        raise OperatorConfigError("outcome_spec's horizon is not label_spec.horizon")
    if wiring.state_spec.features != (wiring.feature_spec.ref,):
        raise OperatorConfigError("state_spec.features must be the configured feature_spec ref")
    if any(entry.spec.signals != (LOG_RETURN_SIGNAL,) for entry in wiring.strategies):
        raise OperatorConfigError(
            "every v1 StrategySpec must use exactly the allowlisted bar-log-return signal"
        )

    selection = wiring.profile_selection
    rule = wiring.profile_selection_rule
    if profile.status is not ProfileStatus.FROZEN:
        raise OperatorConfigError(f"{profile.ref} is not FROZEN")
    if rule.ref.target_identity() != selection.selection_rule.target_identity():
        raise OperatorConfigError(
            f"profile_selection_rule is {rule.ref}, the selection names {selection.selection_rule}"
        )
    if rule.content_hash() != selection.selection_rule_hash:
        raise OperatorConfigError(
            "profile_selection_rule's content hash is not profile_selection.selection_rule_hash"
        )
    try:
        entry = rule.select(selection.key)
    except ProfileViolation as exc:
        raise OperatorConfigError(f"profile_selection_rule selects no Profile: {exc}") from exc
    if entry.profile.target_identity() != profile.ref.target_identity():
        raise OperatorConfigError(
            f"the selection rule selects {entry.profile}, the configured Profile is {profile.ref}"
        )
    scope = profile.scope
    if (scope.venue, scope.symbol, scope.timeframe, scope.research_class) != (
        selection.key.venue,
        selection.key.symbol,
        selection.key.timeframe,
        selection.key.research_class,
    ):
        raise OperatorConfigError(
            "the profile_selection key's venue / symbol / timeframe / research_class are not "
            f"{profile.ref}'s scope"
        )
    if wiring.declared_research_class != selection.key.research_class:
        raise OperatorConfigError(
            "declared_research_class is not the profile_selection key's research_class"
        )
    if wiring.declared_research_class != scope.research_class:
        raise OperatorConfigError(
            f"declared_research_class is not {profile.ref}'s scope research_class"
        )
    stressed = profile.cost_stress.cost_model
    if stressed.target_identity() != wiring.cost_model.ref.target_identity():
        raise OperatorConfigError(
            f"{profile.ref} stresses {stressed}, the configured cost_model is "
            f"{wiring.cost_model.ref}"
        )
    for index, strategy_entry in enumerate(wiring.strategies):
        if strategy_entry.hypothesis_family_id != family_id:
            raise OperatorConfigError(
                f"[loop.wiring] strategies[{index}] hypothesis_family_id is not [loop] family_id"
            )


def _cscv_partitions(value: Any, label: str) -> int:
    parsed = _int(value, label, minimum=2)
    if parsed % 2:
        raise OperatorConfigError(f"{label} must be an even number of partitions")
    return parsed


# ---- freeze registry ------------------------------------------------------------------------


def _freeze_evidence(paths: OperatorPaths, profile: ValidationProfile) -> FreezeEvidence:
    """Replay the configured ADR-0062 registry and require an exact record for ``profile``.

    Registry I/O, corruption and lock failures are ``FreezeRegistryFault``; an empty registry or no
    exact record is an ``OperatorConfigError``. Opening the registry may append the one missing
    anchor tail record ADR-0062 recovers; nothing else is recovered or written here."""
    faults = (RegistryError, JournalCorrupted, BlobCorrupted, OSError)
    try:
        registry = ProfileFreezeRegistry(
            paths.freeze_registry_dir, anchor=paths.freeze_registry_anchor
        )
    except faults as exc:
        raise FreezeRegistryFault(f"the freeze registry cannot be opened: {exc}") from exc
    except ValueError as exc:  # the registry's own anchor-placement guard
        raise OperatorConfigError(f"the freeze registry is misconfigured: {exc}") from exc
    try:
        with registry:
            if len(registry) == 0:
                raise OperatorConfigError(
                    "the freeze registry is empty: no Validation Profile is frozen, so no "
                    "operator configuration can run (ADR-0074 §2.8)"
                )
            record = registry.frozen_record(profile)
            if record is None:
                raise OperatorConfigError(
                    f"{profile.ref} (hash {profile.content_hash()}) has no exact freeze record"
                )
            length, head = registry.anchor_snapshot
    except faults as exc:
        raise FreezeRegistryFault(f"the freeze registry cannot be verified: {exc}") from exc
    return FreezeEvidence(
        freeze_id=record.freeze_id,
        profile_ref=record.profile_ref,
        profile_hash=record.profile_hash,
        calibration_report_hash=record.report_hash,
        approved_by=record.approved_by,
        approved_at=record.approved_at,
        anchor_length=length,
        anchor_head=head,
    )


# ---- operator identity ----------------------------------------------------------------------


def _microseconds(value: timedelta) -> int:
    return value // timedelta(microseconds=1)


def _float_text(value: float | None) -> str | None:
    return None if value is None else repr(value)


def _hashes(items: Sequence[Any], key: Callable[[Any], str]) -> list[str]:
    return [key(item) for item in items]


def _operator_identity(
    providers: tuple[ProviderIdentity, ...], values: LoopValues, wiring: WiringValues
) -> str:
    """SHA-256 of the ADR-0074 §5 semantic configuration; no path and no ``--rounds``."""
    payload = {
        "format": _IDENTITY_FORMAT,
        "schema_version": CONFIG_SCHEMA_VERSION,
        "operator": {"llm_enabled": False},
        "providers": {identity.role: identity.payload() for identity in providers},
        "loop": {
            "loop_id": values.loop_id,
            "seed": values.seed,
            "epoch": values.epoch.isoformat(),
            "cadence_microseconds": _microseconds(values.cadence),
            "budget": {
                "max_trials_per_round": values.budget.max_trials_per_round,
                "max_trials_total": values.budget.max_trials_total,
                "max_llm_cost_units": exact_decimal_text(values.budget.max_llm_cost_units),
                "max_compute_seconds": exact_decimal_text(values.budget.max_compute_seconds),
            },
            "market": values.market.content_hash(),
            "minutes_per_round": values.minutes_per_round,
            "compute_seconds_per_bar": exact_decimal_text(values.compute_seconds_per_bar),
            "family_id": values.family_id,
            "knowledge": _hashes(values.knowledge, lambda item: item.content_hash()),
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


def wiring_identity(wiring: WiringValues) -> dict[str, Any]:
    """The ``wiring`` part of the operator identity payload (ADR-0074 §5): every wiring value or
    content hash and the explicit disables. Shared with the Dataset-sourced operator (ADR-0105
    §5)."""
    robustness = wiring.robustness
    return {
        "feature_spec": wiring.feature_spec.content_hash(),
        "feature_chunk_bars": wiring.feature_chunk_bars,
        "state_spec": wiring.state_spec.content_hash(),
        "decision_step_microseconds": _microseconds(wiring.decision_step),
        "decision_warmup_microseconds": _microseconds(wiring.decision_warmup),
        "strategies": [
            {
                "spec": entry.spec.content_hash(),
                "hypothesis_family_id": entry.hypothesis_family_id,
            }
            for entry in wiring.strategies
        ],
        "cost_model": wiring.cost_model.content_hash(),
        "initial_equity": exact_decimal_text(wiring.initial_equity),
        "label_spec": wiring.label_spec.content_hash(),
        "outcome_spec": wiring.outcome_spec.content_hash(),
        "robustness": {
            "cscv_partitions": robustness.cscv_partitions,
            **{key: _float_text(getattr(robustness, key)) for key in _ROBUSTNESS_FLOAT_KEYS},
        },
        "profile_selection": wiring.profile_selection.content_hash(),
        "profile_selection_rule": wiring.profile_selection_rule.content_hash(),
        "declared_research_class": wiring.declared_research_class,
        "code_commit": wiring.code_commit,
        "environment_lock": wiring.environment_lock,
        "disabled": list(DISABLED_WIRING_FIELDS),
    }

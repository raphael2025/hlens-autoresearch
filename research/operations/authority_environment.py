"""Default ``AuthorityEnvironment`` factory for the P11 authority mode (ADR-0100 item 4).

Use it as ``--authority-environment research.operations.authority_environment:default_environment
--authority-evidence-verifier MODULE:CALLABLE`` (``degradation_cli``; ADR-0098 修订 1) or embed it
as ``with default_environment(evidence_verifier="MODULE:CALLABLE") as env: ...``.
``default_environment(...)`` returns a context manager; entering it builds everything below and
yields one ``research.operations.authority.AuthorityEnvironment``; leaving it closes the catalog
adapter and the storage adapter. Nothing here reads a clock, schedules, writes the catalog, the
lifecycle or a report; the resolver still checks every identity against the baseline run.

**What comes from where.**

- *Data plane* — ``infrastructure.settings.Settings`` (``HLENS_*``, ``.env``): the PostgreSQL SQL
  catalog + ``file://`` warehouse (``open_postgres_catalog_adapter(settings, PHASE1_REGISTRY)``),
  the ``LocalFileStorageAdapter`` (``from_settings``) and the ``DatasetBuilder`` (canonical scratch
  directory, market-data origin, ``DATASET_SELECTIONS``), assembled into the ``DatasetCatalog``.
- *v3 evidence verifier* — the resolver reads v3 manifests only through the builder catalog's
  ``StreamingEvidenceVerifier``, which needs the accepted Dataset rule, the PIT / Universe run
  bounds and a Quality evidence factory (``infrastructure.dataset.pipeline``: "every resource limit
  is supplied by the caller"). None of that is a ``Settings`` field and nothing is guessed: the
  **explicit** ``evidence_verifier`` argument (the CLI's ``--authority-evidence-verifier``) names
  a deployment-trusted ``MODULE:CALLABLE`` (resolved like the CLI's own factory,
  ``apps.worker.serve.load_factory``) called with ``(adapter, storage)`` that must return a
  ``StreamingEvidenceVerifier`` for that adapter. It is never read from the environment or
  ``.env`` (integrity fixes 2026-09-30: code chosen by an environment variable is not an explicit
  deployment decision); without the argument the factory refuses.
- *What the baseline pins* — ``AuthorityEnvironmentSettings`` (``HLENS_AUTHORITY_*``): the
  baseline ``ExperimentRun`` JSON (``BASELINE_RUN``), the baseline dataset manifest hash
  (``BASELINE_MANIFEST_HASH``; one of the run's ``dataset_snapshots``, checked by the resolver),
  the ``StrategySpec`` JSON (``STRATEGY_SPEC``) and, when the spec names one, the ``RiskPolicy``
  JSON (``RISK_POLICY``), the ``CostModelSpec`` JSON (``COST_MODEL``), the ``OutcomeLabelSpec``
  JSON (``LABEL_SPEC``; its outcome ref + spec hash must be the run's recorded ``outcome_ref``),
  the initial equity (``INITIAL_EQUITY``) and the decision grid (``DECISION_STEP``,
  ``DECISION_WARMUP``; the grid's label horizon is the label spec's ``horizon``, exactly as the
  research loop's ``decision_grid``).
- *Backtest provider* — resolved from the plugin registry (``infrastructure.plugins``:
  ``PluginRegistry(discover(PluginKind.BACKTEST))``) by the provider identity the baseline run
  recorded in ``plugin_versions`` (exactly one backtest ``name@version``), constructed by this
  composition root's explicit table (``BACKTEST_CONSTRUCTORS``: ``hlens_bar_backtest@1.0.0`` →
  ``BarBacktester()``) and required to have the recorded descriptor hash. A variant with an
  execution model (fingerprinted at construction) has no constructor here and is refused.
- *Decision pipeline* — ``StrategyWindowTargetSource``: the strategy (and risk) provider is chosen
  by the provider identity the run recorded (``STRATEGY_PROVIDERS`` / ``RISK_PROVIDERS``: the
  research strategy / risk provider classes by descriptor name), constructed with exactly the
  baseline spec (policy) and required to have the recorded ``name@version`` + descriptor hash (a
  provider the loop built with other specs has another hash and is refused, never re-hashed).
  It runs ``research.strategies.pipeline.CandidateTrialRunner`` (strategy → risk → backtest) on the
  window's proven bars with signals computed from those bars only
  (``research.strategies.signals.bar_signals`` / ``price_signals.bar_price_signals`` — the
  definitions the feature providers use; any other signal ref is refused), no risk signals (as the
  research loop's trials), the request params of the run's params, and the decision grid over the
  window's bars. Its instruments are the baseline manifest's universe members
  (``authority.universe_symbols``, the resolver's own rule). No data outside the window is read:
  a strategy that needs history before ``window.start`` sees only what the window holds.
- *Validation binding* (optional; only the metric definitions that re-run the baseline validator
  need it) — enabled by ``HLENS_AUTHORITY_VALIDATION_METADATA`` (the baseline
  ``ExperimentMetadata`` JSON); then ``VALIDATION_SEED`` and ``ROBUSTNESS_PARAMS`` (a JSON object
  with exactly the six ``RobustnessParams`` fields) are required, ``CONTROL_SEEDS`` (JSON list),
  ``MARKET_BENCHMARK`` and ``BAR_VOLUME`` (booleans, default ``false``) optional. The outcome
  provider is resolved like the backtest provider (``OUTCOME_CONSTRUCTORS``; the
  ``vol_scaled_triple_barrier`` provider needs a volatility channel and is refused). The trial
  runner is the decision pipeline's own; no state labeller and no declared execution model are
  built (the resolver refuses the binding when the baseline report shows either was used).

**Refusals.** Every missing or invalid setting is collected and refused at once with
``authority_environment_unavailable`` (``AuthorityEnvironmentUnavailable.missing_settings`` names
the environment variables, never their values); a pinned artifact that does not match the run, or
a provider that cannot be constructed with the recorded identity, is refused with the same code.
Nothing is connected before every file-level check has passed.

Code completion (2026-09-30, CODE_COMPLETE / DEBUG_PENDING; not run, not tested).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from apps.worker.serve import load_factory
from core.contracts.catalog import CatalogError
from core.contracts.cost_model import CostModelSpec
from core.contracts.feature import ObservationScalar
from core.contracts.outcome import OutcomeLabelSpec, OutcomeProvider
from core.contracts.profile_selection import ExperimentMetadata
from core.contracts.strategy import (
    BacktestCostModel,
    BacktestProvider,
    BacktestProviderError,
    PriceBar,
    RiskProvider,
    RiskProviderError,
    SignalObservation,
    StrategyProvider,
    StrategyProviderError,
    TargetPosition,
)
from core.domain.base import Kind
from core.domain.research import ExperimentRun
from core.domain.specs import RiskPolicy, StrategySpec
from infrastructure.catalog import PHASE1_REGISTRY
from infrastructure.catalog.iceberg_adapter import open_postgres_catalog_adapter
from infrastructure.catalog.phase1_tables import DATASET_SELECTIONS
from infrastructure.dataset.builder import DatasetBuilder
from infrastructure.dataset.verify_v3 import StreamingEvidenceVerifier
from infrastructure.plugins.discovery import discover
from infrastructure.plugins.manifest import PluginKind
from infrastructure.plugins.registry import PluginRegistry, PluginRegistryError
from infrastructure.settings import Settings
from infrastructure.storage import LocalFileStorageAdapter
from plugins.backtest import BarBacktester
from plugins.outcomes import ForwardReturnOutcome, TripleBarrierOutcome
from research.experiments.run_inputs import strategy_params
from research.loop.dataset_source import DatasetCatalog
from research.loop.segment import decision_grid
from research.operations.authority import (
    AuthorityEnvironment,
    AuthorityRefused,
    TargetSourceIdentity,
    WindowExecution,
    WindowValidationBinding,
    universe_symbols,
)
from research.operations.degradation import ObservationWindow
from research.operations.degradation_cli import AUTHORITY_ENVIRONMENT_UNAVAILABLE
from research.strategies.cross_sectional_momentum import CrossSectionalMomentumProvider
from research.strategies.donchian_breakout import DonchianBreakoutProvider
from research.strategies.drawdown_control import DrawdownControlRiskProvider
from research.strategies.dual_momentum import DualMomentumProvider
from research.strategies.pipeline import CandidateTrialRunner, EvaluationInputs, StrategyCandidate
from research.strategies.price_signals import (
    BAR_CLOSE_SIGNAL,
    BAR_HIGH_SIGNAL,
    BAR_LOW_SIGNAL,
    bar_price_signals,
)
from research.strategies.signals import LOG_RETURN_SIGNAL, bar_signals, realized_vol_signal
from research.strategies.time_series_momentum import TimeSeriesMomentumProvider
from research.strategies.volatility_target import VolatilityTargetRiskProvider
from research.strategies.zscore_reversion import ZScoreReversionProvider
from research.validation.g4 import RobustnessParams

__all__ = [
    "BACKTEST_CONSTRUCTORS",
    "ENV_PREFIX",
    "OUTCOME_CONSTRUCTORS",
    "RISK_PROVIDERS",
    "STRATEGY_PROVIDERS",
    "AuthorityEnvironmentSettings",
    "AuthorityEnvironmentUnavailable",
    "StrategyWindowTargetSource",
    "default_environment",
]

#: Environment prefix of the authority-specific settings (``Settings`` keeps ``HLENS_``).
ENV_PREFIX: Final = "HLENS_AUTHORITY_"
_SETTINGS_PREFIX: Final = "HLENS_"

#: Composition-root constructors of the backtest providers a baseline run may record, by exact
#: ``name@version`` (the plugin registry resolves manifests only; ADR-0087).
BACKTEST_CONSTRUCTORS: Final[Mapping[str, Callable[[], BacktestProvider]]] = {
    "hlens_bar_backtest@1.0.0": BarBacktester,
}
#: ... of the outcome providers (built with exactly the baseline label spec).
OUTCOME_CONSTRUCTORS: Final[
    Mapping[str, Callable[[Sequence[OutcomeLabelSpec]], OutcomeProvider]]
] = {
    "hlens_forward_return@1.0.0": ForwardReturnOutcome,
    "hlens_triple_barrier@1.0.0": TripleBarrierOutcome,
}
#: Research strategy providers by descriptor name (built with exactly the baseline spec).
STRATEGY_PROVIDERS: Final[Mapping[str, Callable[[Sequence[StrategySpec]], StrategyProvider]]] = {
    "research_tsmom": TimeSeriesMomentumProvider,
    "research_xsmom": CrossSectionalMomentumProvider,
    "research_donchian_breakout": DonchianBreakoutProvider,
    "research_dual_momentum": DualMomentumProvider,
    "research_zscore_reversion": ZScoreReversionProvider,
}
#: Research risk providers by descriptor name (built with exactly the baseline policy).
RISK_PROVIDERS: Final[Mapping[str, Callable[[Sequence[RiskPolicy]], RiskProvider]]] = {
    "research_vol_target": VolatilityTargetRiskProvider,
    "research_drawdown_control": DrawdownControlRiskProvider,
}

_PRICE_SIGNALS: Final = (BAR_CLOSE_SIGNAL, BAR_HIGH_SIGNAL, BAR_LOW_SIGNAL)
_VOL_SIGNAL: Final = re.compile(r"^bar_realized_vol_([1-9][0-9]*)$")
_ROBUSTNESS_FIELDS: Final = (
    "cscv_partitions",
    "max_participation_rate",
    "min_capacity",
    "impact_coefficient",
    "cross_asset_min_positive_fraction",
    "max_undersampled_pnl_share",
)


class AuthorityEnvironmentUnavailable(AuthorityRefused):
    """``authority_environment_unavailable``; ``missing_settings`` names the environment variables
    that are missing or invalid (names only, never values)."""

    def __init__(self, message: str, missing_settings: Sequence[str] = ()) -> None:
        self.missing_settings: tuple[str, ...] = tuple(missing_settings)
        detail = message
        if self.missing_settings:
            detail = f"{message}: {', '.join(self.missing_settings)}"
        super().__init__(AUTHORITY_ENVIRONMENT_UNAVAILABLE, detail)


class AuthorityEnvironmentSettings(BaseSettings):
    """The authority-specific settings (module docs, **What comes from where**). Every field is
    optional here so that every missing one can be named at once; ``default_environment`` decides
    which are required."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    baseline_run: Path | None = None
    baseline_manifest_hash: str | None = None
    strategy_spec: Path | None = None
    risk_policy: Path | None = None
    cost_model: Path | None = None
    label_spec: Path | None = None
    initial_equity: Decimal | None = None
    decision_step: timedelta | None = None
    decision_warmup: timedelta | None = None
    validation_metadata: Path | None = None
    validation_seed: int | None = None
    robustness_params: Path | None = None
    control_seeds: tuple[int, ...] | None = None
    market_benchmark: bool = False
    bar_volume: bool = False


def _env(name: str, prefix: str = ENV_PREFIX) -> str:
    return f"{prefix}{name.upper()}"


def _invalid_names(exc: ValidationError, prefix: str) -> list[str]:
    names = sorted({_env(str(error["loc"][0]), prefix) for error in exc.errors() if error["loc"]})
    return names or [f"{prefix}*"]


def _load_settings() -> tuple[Settings, AuthorityEnvironmentSettings]:
    missing: list[str] = []
    settings: Settings | None = None
    authority: AuthorityEnvironmentSettings | None = None
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        missing.extend(_invalid_names(exc, _SETTINGS_PREFIX))
    try:
        authority = AuthorityEnvironmentSettings()
    except ValidationError as exc:
        missing.extend(_invalid_names(exc, ENV_PREFIX))
    if missing or settings is None or authority is None:
        raise AuthorityEnvironmentUnavailable("invalid or missing settings", missing)
    return settings, authority


def _required(config: AuthorityEnvironmentSettings) -> list[str]:
    """The names of every required authority setting that is not set (module docs)."""
    names = [
        "baseline_run",
        "baseline_manifest_hash",
        "strategy_spec",
        "cost_model",
        "label_spec",
        "initial_equity",
        "decision_step",
        "decision_warmup",
    ]
    if config.validation_metadata is not None:
        names += ["validation_seed", "robustness_params"]
    return [_env(name) for name in names if getattr(config, name) is None]


def _read(path: Path, setting: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise AuthorityEnvironmentUnavailable(
            f"the file named by {setting} does not read as JSON", (setting,)
        ) from exc


def _model[M](path: Path | None, model: type[M], name: str) -> M:
    setting = _env(name)
    if path is None:
        raise AuthorityEnvironmentUnavailable("a required setting is missing", (setting,))
    try:
        validate: Callable[[Any], M] = model.model_validate  # type: ignore[attr-defined]
        return validate(_read(path, setting))
    except (ValidationError, ValueError, TypeError) as exc:
        raise AuthorityEnvironmentUnavailable(
            f"the file named by {setting} does not match its contract", (setting,)
        ) from exc


def _plugin_key(descriptor: Any) -> str:
    return f"{descriptor.name}@{descriptor.version}"


def _check_recorded(provider: Any, run: ExperimentRun, what: str) -> None:
    descriptor = provider.descriptor
    if run.repro.plugin_versions.get(_plugin_key(descriptor)) != descriptor.content_hash():
        raise AuthorityEnvironmentUnavailable(
            f"the constructed {what} {_plugin_key(descriptor)} does not have the identity the "
            "baseline run recorded (constructed with other inputs than the baseline's)"
        )


def _registered(run: ExperimentRun, kind: PluginKind) -> str:
    """The one ``name@version`` of ``kind`` that the baseline run recorded and the plugin registry
    resolves (``discover`` of the installed entry points)."""
    try:
        registry = PluginRegistry(discover(kind))
    except (PluginRegistryError, RuntimeError) as exc:
        raise AuthorityEnvironmentUnavailable(
            f"the {kind.value} plugin registry does not load ({type(exc).__name__})"
        ) from exc
    found: list[str] = []
    for key in sorted(run.repro.plugin_versions):
        try:
            plugin = registry.resolve(key)
        except (PluginRegistryError, ValueError):
            continue
        if plugin.manifest.kind is kind:
            found.append(key)
    if len(found) != 1:
        raise AuthorityEnvironmentUnavailable(
            f"the baseline run records {len(found)} {kind.value} plugins the registry resolves "
            f"({found}); exactly one is required"
        )
    return found[0]


def _backtest_provider(run: ExperimentRun) -> BacktestProvider:
    key = _registered(run, PluginKind.BACKTEST)
    constructor = BACKTEST_CONSTRUCTORS.get(key)
    if constructor is None:
        raise AuthorityEnvironmentUnavailable(
            f"no composition-root constructor for the recorded backtest provider {key}"
        )
    provider = constructor()
    _check_recorded(provider, run, "backtest provider")
    return provider


def _outcome_provider(run: ExperimentRun, label_spec: OutcomeLabelSpec) -> OutcomeProvider:
    key = _registered(run, PluginKind.OUTCOME)
    constructor = OUTCOME_CONSTRUCTORS.get(key)
    if constructor is None:
        raise AuthorityEnvironmentUnavailable(
            f"no composition-root constructor for the recorded outcome provider {key}"
        )
    try:
        provider = constructor((label_spec,))
    except ValueError as exc:
        raise AuthorityEnvironmentUnavailable(
            f"the recorded outcome provider {key} does not serve the label spec"
        ) from exc
    _check_recorded(provider, run, "outcome provider")
    return provider


def _recorded_provider[S, P](
    run: ExperimentRun,
    table: Mapping[str, Callable[[Sequence[S]], P]],
    spec: S,
    what: str,
) -> P:
    names = sorted({key.partition("@")[0] for key in run.repro.plugin_versions} & set(table))
    if len(names) != 1:
        raise AuthorityEnvironmentUnavailable(
            f"the baseline run records {len(names)} known {what} providers ({names}); exactly one "
            "is required"
        )
    try:
        provider = table[names[0]]((spec,))
    except ValueError as exc:
        raise AuthorityEnvironmentUnavailable(
            f"the recorded {what} provider {names[0]} does not accept the baseline {what}"
        ) from exc
    _check_recorded(provider, run, f"{what} provider")
    return provider


def _check_label_spec(label_spec: OutcomeLabelSpec, run: ExperimentRun) -> None:
    outcome = run.repro.outcome_ref
    if (
        outcome is None
        or label_spec.outcome.target_identity() != outcome.target_identity()
        or label_spec.outcome_spec_hash != run.repro.dependency_hashes.get(str(outcome))
    ):
        raise AuthorityEnvironmentUnavailable(
            "the label spec is not the Outcome definition the baseline run recorded",
            (_env("label_spec"),),
        )


def _request_params(
    spec: StrategySpec, params: Mapping[str, str | int | float | bool]
) -> dict[str, ObservationScalar]:
    """The request params of a point: the declared ``param_search_space`` keys only; a float
    cannot be requested (the rule of ``research.strategies.validation`` and the research loop)."""
    out: dict[str, ObservationScalar] = {}
    for name, value in params.items():
        if name not in spec.param_search_space:
            continue
        if isinstance(value, float):
            raise AuthorityEnvironmentUnavailable(
                f"{spec.ref}: float parameter {name}={value!r} cannot be requested"
            )
        out[name] = value
    return out


def _signals(spec: StrategySpec, bars: Sequence[PriceBar]) -> tuple[SignalObservation, ...]:
    """The spec's signals computed from ``bars`` only (module docs); another ref is refused."""
    wanted = set(spec.signals)
    windows: list[int] = []
    unknown: list[str] = []
    for ref in spec.signals:
        if ref == LOG_RETURN_SIGNAL or ref in _PRICE_SIGNALS:
            continue
        match = _VOL_SIGNAL.fullmatch(ref.name)
        if match is not None and ref == realized_vol_signal(int(match.group(1))):
            windows.append(int(match.group(1)))
        else:
            unknown.append(str(ref))
    if unknown:
        raise ValueError(f"no window signal constructor for {unknown}")
    out: list[SignalObservation] = []
    if LOG_RETURN_SIGNAL in wanted or windows:
        out.extend(bar_signals(bars, vol_windows=tuple(sorted(set(windows)))))
    prices = [ref for ref in spec.signals if ref in _PRICE_SIGNALS]
    if prices:
        out.extend(bar_price_signals(bars, prices))
    return tuple(item for item in out if item.signal in wanted)


def _check_signal_refs(spec: StrategySpec) -> None:
    for ref in spec.signals:
        if ref.kind is not Kind.FEATURE:
            raise AuthorityEnvironmentUnavailable(
                f"{spec.ref} reads the non-feature signal {ref}; no window constructor exists"
            )
    try:
        _signals(spec, ())
    except ValueError as exc:
        raise AuthorityEnvironmentUnavailable(f"{spec.ref}: {exc}") from exc


@dataclass(frozen=True)
class StrategyWindowTargetSource:
    """The admitted strategy's decision pipeline over the window's proven bars (module docs): a
    ``research.operations.authority.WindowTargetSource`` whose ``runner`` is also the validation
    binding's trial runner, so both re-runs are one pipeline."""

    identity: TargetSourceIdentity
    candidate: StrategyCandidate
    backtester: BacktestProvider
    cost_model: BacktestCostModel
    initial_equity: Decimal
    request_params: Mapping[str, ObservationScalar]
    decision_step: timedelta
    decision_warmup: timedelta
    label_horizon: timedelta

    def inputs(self, bars: tuple[PriceBar, ...], window: ObservationWindow) -> EvaluationInputs:
        ordered = tuple(sorted(bars, key=lambda bar: (bar.interval_start, bar.instrument)))
        if not ordered:
            raise ValueError(f"no bar in the window {window.label}")
        decisions = decision_grid(
            ordered,
            step=self.decision_step,
            warmup=self.decision_warmup,
            horizon=self.label_horizon,
        )
        return EvaluationInputs(
            instruments=tuple(sorted(self.identity.instruments)),
            bars=ordered,
            decision_times=decisions,
            knowledge_cutoff=max(bar.available_time for bar in ordered),
            cost_model=self.cost_model,
            initial_equity=self.initial_equity,
            signals=_signals(self.candidate.spec, ordered),
            params=dict(self.request_params),
        )

    def runner(self, bars: tuple[PriceBar, ...], window: ObservationWindow) -> CandidateTrialRunner:
        return CandidateTrialRunner(self.candidate, self.inputs(bars, window), self.backtester)

    def targets(
        self, bars: tuple[PriceBar, ...], window: ObservationWindow
    ) -> Sequence[TargetPosition]:
        try:
            return self.runner(bars, window).run(dict(self.request_params)).targets
        except (StrategyProviderError, RiskProviderError, BacktestProviderError) as exc:
            raise ValueError(f"the decision pipeline refused: {exc}") from exc


def _robustness(path: Path) -> RobustnessParams:
    setting = _env("robustness_params")
    payload = _read(path, setting)
    if not isinstance(payload, dict) or set(payload) != set(_ROBUSTNESS_FIELDS):
        raise AuthorityEnvironmentUnavailable(
            f"{setting} must be a JSON object with exactly {list(_ROBUSTNESS_FIELDS)}", (setting,)
        )
    values: dict[str, Any] = {}
    for name in _ROBUSTNESS_FIELDS:
        value = payload[name]
        if value is None:
            values[name] = None
        elif name == "cscv_partitions":
            if isinstance(value, bool) or not isinstance(value, int):
                raise AuthorityEnvironmentUnavailable(
                    f"{setting}: {name} must be an int", (setting,)
                )
            values[name] = value
        elif isinstance(value, bool) or not isinstance(value, int | float):
            raise AuthorityEnvironmentUnavailable(f"{setting}: {name} must be a number", (setting,))
        else:
            values[name] = float(value)
    return RobustnessParams(**values)


#: The explicit argument (CLI flag) naming the trusted v3 evidence verifier factory.
EVIDENCE_VERIFIER_ARGUMENT: Final = "--authority-evidence-verifier"


def _evidence_verifier(spec: str, adapter: Any, storage: Any) -> StreamingEvidenceVerifier:
    setting = EVIDENCE_VERIFIER_ARGUMENT
    try:
        factory = cast(Callable[[Any, Any], object], load_factory(spec))
        verifier = factory(adapter, storage)
    except Exception as exc:  # a trusted factory that cannot be loaded or fails
        raise AuthorityEnvironmentUnavailable(
            f"the evidence verifier factory failed ({type(exc).__name__})", (setting,)
        ) from exc
    if not isinstance(verifier, StreamingEvidenceVerifier) or verifier.adapter is not adapter:
        raise AuthorityEnvironmentUnavailable(
            "the evidence verifier factory did not return a StreamingEvidenceVerifier of the "
            "settings' catalog",
            (setting,),
        )
    return verifier


@dataclass(frozen=True)
class _Pinned:
    """Everything read from files and the plugin registry before anything is connected."""

    run: ExperimentRun
    manifest_hash: str
    spec: StrategySpec
    cost_model: CostModelSpec
    label_spec: OutcomeLabelSpec
    candidate: StrategyCandidate
    backtester: BacktestProvider
    plugins: Mapping[str, str]
    initial_equity: Decimal
    decision_step: timedelta
    decision_warmup: timedelta
    validation: Callable[[StrategyWindowTargetSource], WindowValidationBinding] | None


def _pinned(config: AuthorityEnvironmentSettings) -> _Pinned:
    missing = _required(config)
    if missing:
        raise AuthorityEnvironmentUnavailable("required settings are missing", missing)
    run = _model(config.baseline_run, ExperimentRun, "baseline_run")
    spec = _model(config.strategy_spec, StrategySpec, "strategy_spec")
    cost_model = _model(config.cost_model, CostModelSpec, "cost_model")
    label_spec = _model(config.label_spec, OutcomeLabelSpec, "label_spec")
    _check_label_spec(label_spec, run)
    _check_signal_refs(spec)
    manifest_hash = config.baseline_manifest_hash
    assert manifest_hash is not None
    equity, step, warmup = config.initial_equity, config.decision_step, config.decision_warmup
    assert equity is not None and step is not None and warmup is not None
    risk_policy: RiskPolicy | None = None
    risk: RiskProvider | None = None
    if spec.risk_policy is not None:
        risk_policy = _model(config.risk_policy, RiskPolicy, "risk_policy")
        risk = _recorded_provider(run, RISK_PROVIDERS, risk_policy, "risk")
    strategy = _recorded_provider(run, STRATEGY_PROVIDERS, spec, "strategy")
    try:
        candidate = StrategyCandidate(
            spec=spec,
            strategy=strategy,
            hypothesis_family_id=f"authority:{spec.ref}",
            risk_policy=risk_policy,
            risk=risk,
        )
    except ValueError as exc:
        raise AuthorityEnvironmentUnavailable(
            f"the risk policy is not the spec's ({exc})", (_env("risk_policy"),)
        ) from exc
    plugins = {_plugin_key(strategy.descriptor): strategy.descriptor.content_hash()}
    if risk is not None:
        plugins[_plugin_key(risk.descriptor)] = risk.descriptor.content_hash()
    validation: Callable[[StrategyWindowTargetSource], WindowValidationBinding] | None = None
    if config.validation_metadata is not None:
        metadata = _model(config.validation_metadata, ExperimentMetadata, "validation_metadata")
        assert config.robustness_params is not None and config.validation_seed is not None
        robustness = _robustness(config.robustness_params)
        outcome_provider = _outcome_provider(run, label_spec)
        seed, control_seeds = config.validation_seed, config.control_seeds
        market_benchmark, bar_volume = config.market_benchmark, config.bar_volume

        def binding_for(source: StrategyWindowTargetSource) -> WindowValidationBinding:
            return WindowValidationBinding(
                spec=spec,
                metadata=metadata,
                label_spec=label_spec,
                outcome_provider=outcome_provider,
                trials=source.runner,
                seed=seed,
                robustness=robustness,
                declared_instruments=tuple(sorted(source.identity.instruments)),
                state_labels=None,
                bar_volume=bar_volume,
                control_seeds=control_seeds,
                market_benchmark=market_benchmark,
            )

        validation = binding_for

    return _Pinned(
        run=run,
        manifest_hash=manifest_hash,
        spec=spec,
        cost_model=cost_model,
        label_spec=label_spec,
        candidate=candidate,
        backtester=_backtest_provider(run),
        plugins=plugins,
        initial_equity=equity,
        decision_step=step,
        decision_warmup=warmup,
        validation=validation,
    )


def _target_source(pinned: _Pinned, instruments: tuple[str, ...]) -> StrategyWindowTargetSource:
    run, spec = pinned.run, pinned.spec
    policy = pinned.candidate.risk_policy
    identity = TargetSourceIdentity(
        strategy_ref=spec.ref,
        strategy_spec_hash=spec.content_hash(),
        risk_policy_ref=None if policy is None else policy.ref,
        risk_policy_hash=None if policy is None else policy.content_hash(),
        # the strategy's parameters, never the run inputs record (ADR-0100 修订 2)
        params=strategy_params(run.repro.params),
        plugins=dict(pinned.plugins),
        instruments=instruments,
        decision_step=pinned.decision_step,
        decision_warmup=pinned.decision_warmup,
        initial_equity=pinned.initial_equity,
    )
    cost = pinned.cost_model
    return StrategyWindowTargetSource(
        identity=identity,
        candidate=pinned.candidate,
        backtester=pinned.backtester,
        cost_model=BacktestCostModel(
            name=cost.name,
            version=cost.version,
            fee_rate=cost.fee_rate_per_side,
            slippage_rate=cost.slippage_rate_per_side,
        ),
        initial_equity=pinned.initial_equity,
        request_params=_request_params(spec, strategy_params(run.repro.params)),
        decision_step=pinned.decision_step,
        decision_warmup=pinned.decision_warmup,
        label_horizon=pinned.label_spec.horizon,
    )


@contextmanager
def _open(evidence_verifier: str) -> Iterator[AuthorityEnvironment]:
    settings, config = _load_settings()
    pinned = _pinned(config)  # every file / registry check before anything is connected
    with ExitStack() as stack:
        try:
            storage = stack.enter_context(LocalFileStorageAdapter.from_settings(settings))
            adapter = stack.enter_context(open_postgres_catalog_adapter(settings, PHASE1_REGISTRY))
            builder = DatasetBuilder(
                adapter,
                storage,
                canonical_scratch_directory=settings.canonical_scratch_path,
                market_data_base_url=str(settings.binance_market_data_base_url),
                dataset_table=DATASET_SELECTIONS,
            )
        except (OSError, ValueError, RuntimeError, CatalogError) as exc:
            raise AuthorityEnvironmentUnavailable(
                f"the data plane does not open from the settings ({type(exc).__name__})"
            ) from exc
        catalog = DatasetCatalog(
            adapter=adapter,
            storage=storage,
            builder=builder,
            evidence_verifier=_evidence_verifier(evidence_verifier, adapter, storage),
        )
        instruments = universe_symbols(catalog, pinned.manifest_hash)
        if not instruments:
            raise AuthorityEnvironmentUnavailable("the baseline manifest's universe is empty")
        source = _target_source(pinned, instruments)
        yield AuthorityEnvironment(
            catalog=catalog,
            execution=WindowExecution(
                backtester=pinned.backtester,
                cost_model=pinned.cost_model,
                initial_equity=pinned.initial_equity,
                targets=source,
            ),
            baseline_run=pinned.run,
            baseline_manifest_hash=pinned.manifest_hash,
            validation=None if pinned.validation is None else pinned.validation(source),
        )


def default_environment(
    *, evidence_verifier: str | None = None
) -> AbstractContextManager[AuthorityEnvironment]:
    """The default ``AuthorityEnvironment`` (module docs) as a context manager: entering builds it
    (or refuses with ``authority_environment_unavailable``), leaving closes the catalog and the
    storage adapters. ``evidence_verifier`` is the explicit ``MODULE:CALLABLE`` of the trusted v3
    evidence verifier factory (``--authority-evidence-verifier``); it is required and never read
    from the environment."""
    if not isinstance(evidence_verifier, str) or not evidence_verifier:
        raise AuthorityEnvironmentUnavailable(
            "the v3 evidence verifier factory must be given explicitly (never from the "
            "environment)",
            (EVIDENCE_VERIFIER_ARGUMENT,),
        )
    return _open(evidence_verifier)

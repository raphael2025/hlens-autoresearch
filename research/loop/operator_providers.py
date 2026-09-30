"""Closed provider allowlist for the synthetic-loop operator (ADR-0074 §3).

This module imports each approved implementation directly. Provider identities in the TOML select
only these fixed implementations; they are never interpreted as Python import paths or factories.
Every provider is built against the exact hash-bound specs held by ``OperatorConfig`` and its
descriptor is checked before the caller can open loop state.
"""

from __future__ import annotations

from collections.abc import Mapping

from core.contracts.outcome import OutcomeLabelSpec, OutcomeMethod
from core.contracts.strategy import BacktestProviderDescriptor
from plugins.backtest.bar import BarBacktester
from plugins.features.bars import BarLogReturnProvider
from plugins.outcomes.forward_return import ForwardReturnOutcome
from plugins.states.regimes import TrendRangeProvider
from plugins.synthetic.random_walk import RandomWalkMarket
from research.loop.operator_config import (
    _ALLOWLIST_VALIDATION_SEAL,
    PROVIDER_ROLES,
    InjectedProviders,
    OperatorConfig,
    ProviderIdentity,
)
from research.strategies.time_series_momentum import (
    TimeSeriesMomentumProvider,
    tsmom_spec,
    tsmom_vol_scaled_spec,
)

__all__ = ["OperatorProviderError", "build_providers"]


class OperatorProviderError(ValueError):
    """The configured provider selection or its recomputed identity is not allowlisted."""


# These are deliberately literals: changing an implementation identity requires an explicit
# reviewed change to this closed registry. The descriptor hash is spec-bound for all roles except
# the synthetic market provider, so it is recomputed from the exact configured spec(s) below.
_STATIC_IDENTITIES: dict[str, tuple[str, str]] = {
    "synthetic_market_provider": ("hlens_synthetic_random_walk", "1.0.0"),
    "feature_provider": ("bar_log_return", "1.0.0"),
    "state_provider": ("trend_range", "1.0.0"),
    "strategy_provider": ("research_tsmom", "0.1.0"),
    "backtester": ("hlens_bar_backtest", "1.0.0"),
    "outcome_provider": ("hlens_forward_return", "1.0.0"),
}


def _refuse(message: str) -> OperatorProviderError:
    return OperatorProviderError(message)


def _identity(config: OperatorConfig, role: str) -> ProviderIdentity:
    matches = tuple(item for item in config.providers if item.role == role)
    if len(matches) != 1:
        raise _refuse(f"configuration must contain exactly one {role} identity")
    identity = matches[0]
    expected = _STATIC_IDENTITIES.get(role)
    if expected is None or (identity.id, identity.version) != expected:
        raise _refuse(
            f"{role} identity {identity.id}@{identity.version} is not in the static allowlist"
        )
    return identity


def _verify_descriptor(
    config: OperatorConfig,
    role: str,
    descriptor: object,
) -> None:
    identity = _identity(config, role)
    try:
        name = descriptor.name  # type: ignore[attr-defined]
        version = descriptor.version  # type: ignore[attr-defined]
        actual_hash = descriptor.content_hash()  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError) as exc:
        raise _refuse(f"{role} produced an invalid descriptor: {exc}") from exc
    if (name, version) != _STATIC_IDENTITIES[role]:
        raise _refuse(
            f"{role} descriptor is {name}@{version}; expected "
            f"{_STATIC_IDENTITIES[role][0]}@{_STATIC_IDENTITIES[role][1]}"
        )
    if (identity.id, identity.version, identity.descriptor_hash) != (
        name,
        version,
        actual_hash,
    ):
        raise _refuse(
            f"{role} descriptor identity/hash does not exactly match the configured identity"
        )


def _verify_supported_specs(
    role: str,
    descriptor: object,
    field: str,
    expected: Mapping[str, str],
) -> None:
    try:
        actual = getattr(descriptor, field)
        actual_map = dict(actual.items())
    except (AttributeError, TypeError, ValueError) as exc:
        raise _refuse(f"{role} descriptor has an invalid {field}: {exc}") from exc
    if actual_map != dict(expected):
        raise _refuse(
            f"{role} descriptor {field} does not exactly match the configured spec "
            "references/hashes"
        )


def _expected(spec: object) -> dict[str, str]:
    return {str(spec.ref): spec.content_hash()}  # type: ignore[attr-defined]


def build_providers(config: OperatorConfig) -> InjectedProviders:
    """Construct the six statically registered providers and verify them before loop opening.

    The only supported provider set is ADR-0074 §3. Constructor inputs come from the already
    loaded, hash-bound artifacts in ``config``; no state directory or execution phase is touched.
    """
    if not isinstance(config, OperatorConfig):
        raise _refuse("config must be an OperatorConfig")
    if tuple(identity.role for identity in config.providers) != PROVIDER_ROLES:
        raise _refuse("provider identities must occur exactly once in PROVIDER_ROLES order")

    wiring = config.wiring
    try:
        synthetic = RandomWalkMarket()
        _verify_descriptor(config, "synthetic_market_provider", synthetic.descriptor)

        feature = BarLogReturnProvider(specs=(wiring.feature_spec,))
        _verify_descriptor(config, "feature_provider", feature.descriptor)
        _verify_supported_specs(
            "feature_provider",
            feature.descriptor,
            "supported_features",
            _expected(wiring.feature_spec),
        )

        state = TrendRangeProvider(specs=(wiring.state_spec,))
        _verify_descriptor(config, "state_provider", state.descriptor)
        _verify_supported_specs(
            "state_provider",
            state.descriptor,
            "supported_states",
            _expected(wiring.state_spec),
        )

        allowed_strategy_specs = {
            tsmom_spec().content_hash(): tsmom_spec(),
            tsmom_vol_scaled_spec().content_hash(): tsmom_vol_scaled_spec(),
        }
        strategy_specs = tuple(entry.spec for entry in wiring.strategies)
        if not strategy_specs:
            raise _refuse("strategy_provider requires at least one configured StrategySpec")
        for spec in strategy_specs:
            if (
                spec.risk_policy is not None
                or spec.content_hash() not in allowed_strategy_specs
                or spec.model_dump(mode="json")
                != allowed_strategy_specs[spec.content_hash()].model_dump(mode="json")
            ):
                raise _refuse(
                    f"strategy spec {spec.ref} is not exactly an allowlisted TSMOM spec or "
                    "declares risk"
                )
        strategy = TimeSeriesMomentumProvider(specs=strategy_specs)
        _verify_descriptor(config, "strategy_provider", strategy.descriptor)
        _verify_supported_specs(
            "strategy_provider",
            strategy.descriptor,
            "supported_strategies",
            {str(spec.ref): spec.content_hash() for spec in strategy_specs},
        )

        backtester = BarBacktester(execution=None)
        _verify_descriptor(config, "backtester", backtester.descriptor)
        descriptor = backtester.descriptor
        if (
            backtester.execution is not None
            or not isinstance(descriptor, BacktestProviderDescriptor)
            or descriptor.simulation_only is not True
            or descriptor.execution_model != "next_bar_open"
        ):
            raise _refuse("backtester must be simulation-only with execution_model=next_bar_open")

        label_spec = wiring.label_spec
        if (
            not isinstance(label_spec, OutcomeLabelSpec)
            or label_spec.method is not OutcomeMethod.FORWARD_RETURN
        ):
            raise _refuse("outcome_provider only allows a forward_return OutcomeLabelSpec")
        outcome = ForwardReturnOutcome(label_specs=(label_spec,))
        _verify_descriptor(config, "outcome_provider", outcome.descriptor)
        _verify_supported_specs(
            "outcome_provider",
            outcome.descriptor,
            "supported_outcomes",
            {str(label_spec.outcome): label_spec.content_hash()},
        )
    except OperatorProviderError:
        raise
    except Exception as exc:
        raise _refuse(f"provider construction or verification failed: {exc}") from exc

    return InjectedProviders(
        identities=tuple(config.providers),
        synthetic_market_provider=synthetic,
        feature_provider=feature,
        state_provider=state,
        strategy_provider=strategy,
        backtester=backtester,
        outcome_provider=outcome,
        _allowlist_seal=_ALLOWLIST_VALIDATION_SEAL,
    )

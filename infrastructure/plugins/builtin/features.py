"""Static `PluginManifest` for the built-in `FeatureProvider`s (`plugins/features/bars.py`).

Each manifest's `name` / `version` / `deterministic` must equal the corresponding provider class's
`NAME` / `VERSION` (a class attribute — every instance's `descriptor.name` / `descriptor.version`
comes straight from it) — checked one by one in
`tests/infrastructure/plugins/test_builtin_manifests.py`.

`inputs` is the one representation the module's own convenience `spec()` factories default to
(`plugins.features.bars.BAR_1M_INPUT`, `representation:canonical_bar_1m@1.0.0`) — a provider will
in fact serve any `FeatureSpec` whose single input Ref it can rebuild itself from, but this is
what the shipped providers are documented and built to run against. `available_lag` is left unset:
it is a per-`FeatureSpec` parameter with no default, not a plugin-level constant (see
`infrastructure.plugins.manifest` module docstring).
"""

from __future__ import annotations

from infrastructure.plugins.manifest import PluginKind, PluginManifest

__all__ = ["BAR_LOG_RETURN", "BAR_REALIZED_VOLATILITY", "BAR_VOLUME_SUM"]

_BAR_1M_INPUT = "representation:canonical_bar_1m@1.0.0"

BAR_LOG_RETURN = PluginManifest(
    name="bar_log_return",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {"scale": {"type": "integer", "minimum": 1}},
        "required": ["scale"],
        "additionalProperties": False,
    },
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

BAR_REALIZED_VOLATILITY = PluginManifest(
    name="bar_realized_volatility",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {
            "window": {"type": "integer", "minimum": 1},
            "scale": {"type": "integer", "minimum": 1},
        },
        "required": ["window", "scale"],
        "additionalProperties": False,
    },
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

BAR_VOLUME_SUM = PluginManifest(
    name="bar_volume_sum",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {"window": {"type": "integer", "minimum": 1}},
        "required": ["window"],
        "additionalProperties": False,
    },
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

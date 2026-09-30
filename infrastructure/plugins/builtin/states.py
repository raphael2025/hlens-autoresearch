"""Static `PluginManifest` for the built-in `StateProvider`s (`plugins/states/regimes.py`,
`volatility_events.py`).

`name` / `version` / `deterministic` are checked one by one against the provider classes' `NAME` /
`VERSION` in `tests/infrastructure/plugins/test_builtin_manifests.py`.

`inputs` is left empty: a `StateSpec` names its input features by `Ref` at spec-construction
time (e.g. `bar_realized_vol_<n>@...` for `VolatilityRegimeProvider`, any log-return feature for
`TrendRangeProvider`; `volatility_squeeze` / `return_shock` each take two, per their own
`spec()` factory) — the provider class itself does not fix one. `params_schema` reflects the
parameters `StateSpec.method` actually encodes for each model (`plugins.states.regimes`
docstring): `trailing_quantile_buckets` (`cuts`, `min_history` — `training_window` / `seed` are
dedicated `StateSpec` fields, not `method` params) for the two quantile regimes, and
`efficiency_ratio` (`window`, `threshold`) for `TrendRangeProvider`.

`volatility_squeeze` (`vol_ratio_bands`, ADR-0085 `MST-SQUEEZE-001` / `MST-EXPANSION-001`) and
`return_shock` (`abs_return_vol_multiple`, `MST-SHOCK-001`), from
`plugins/states/volatility_events.py`, follow the same shape: no `method` parameter has a default
(ADR-0085 §"通用规则" #2), so every one
of them is `required` here too.
"""

from __future__ import annotations

from infrastructure.plugins.manifest import PluginKind, PluginManifest

__all__ = [
    "LIQUIDITY_REGIME",
    "RETURN_SHOCK",
    "TREND_RANGE",
    "VOLATILITY_REGIME",
    "VOLATILITY_SQUEEZE",
]

_QUANTILE_PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "cuts": {"type": "string", "description": "comma-separated decimal quantile cut points"},
        "min_history": {"type": "integer", "minimum": 1},
    },
    "required": ["cuts", "min_history"],
    "additionalProperties": False,
}

VOLATILITY_REGIME = PluginManifest(
    name="volatility_regime",
    kind=PluginKind.STATE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_QUANTILE_PARAMS_SCHEMA,
    inputs=(),
    outputs=("state:string",),
)

LIQUIDITY_REGIME = PluginManifest(
    name="liquidity_regime",
    kind=PluginKind.STATE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_QUANTILE_PARAMS_SCHEMA,
    inputs=(),
    outputs=("state:string",),
)

TREND_RANGE = PluginManifest(
    name="trend_range",
    kind=PluginKind.STATE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {
            "window": {"type": "integer", "minimum": 1},
            "threshold": {"type": "string", "description": "decimal in (0, 1]"},
        },
        "required": ["window", "threshold"],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=("state:string",),
)

VOLATILITY_SQUEEZE = PluginManifest(
    name="volatility_squeeze",
    kind=PluginKind.STATE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {
            "squeeze_below": {"type": "string", "description": "positive decimal ratio cut"},
            "expansion_above": {
                "type": "string",
                "description": "positive decimal ratio cut, > squeeze_below",
            },
        },
        "required": ["squeeze_below", "expansion_above"],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=("state:string",),
)

RETURN_SHOCK = PluginManifest(
    name="return_shock",
    kind=PluginKind.STATE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {"k": {"type": "string", "description": "positive decimal multiplier"}},
        "required": ["k"],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=("state:string",),
)

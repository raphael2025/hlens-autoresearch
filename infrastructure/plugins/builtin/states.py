"""Static `PluginManifest` for the built-in `StateProvider`s (`plugins/states/regimes.py`).

`name` / `version` / `deterministic` are checked one by one against the provider classes' `NAME` /
`VERSION` in `tests/infrastructure/plugins/test_builtin_manifests.py`.

`inputs` is left empty: a `StateSpec` names its one input feature by `Ref` at spec-construction
time (e.g. `bar_realized_vol_<n>@...` for `VolatilityRegimeProvider`, any log-return feature for
`TrendRangeProvider`) — the provider class itself does not fix one. `params_schema` reflects the
parameters `StateSpec.method` actually encodes for each model (`plugins.states.regimes`
docstring): `trailing_quantile_buckets` (`cuts`, `min_history` — `training_window` / `seed` are
dedicated `StateSpec` fields, not `method` params) for the two quantile regimes, and
`efficiency_ratio` (`window`, `threshold`) for `TrendRangeProvider`.
"""

from __future__ import annotations

from infrastructure.plugins.manifest import PluginKind, PluginManifest

__all__ = ["LIQUIDITY_REGIME", "TREND_RANGE", "VOLATILITY_REGIME"]

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

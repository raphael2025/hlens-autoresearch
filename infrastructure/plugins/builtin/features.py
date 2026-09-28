"""Static `PluginManifest` for the built-in `FeatureProvider`s (`plugins/features/bars.py`,
`indicators.py`, `range_volatility.py`, `microstructure.py`).

Each manifest's `name` / `version` / `deterministic` must equal the corresponding provider class's
`NAME` / `VERSION` (a class attribute — every instance's `descriptor.name` / `descriptor.version`
comes straight from it) — checked one by one in
`tests/infrastructure/plugins/test_builtin_manifests.py`. As with the three `bars.py` manifests
below, `name` is the provider's class-level `NAME` (e.g. `"atr"`, `"parkinson_vol"`), never the
per-spec parameterized identity a `spec()` factory builds (e.g. `atr_14@1.0.0`): a manifest
describes the plugin, and one plugin instance serves many differently-parameterized specs.

`inputs` is the one representation the module's own convenience `spec()` factories default to
(`plugins.features.bars.BAR_1M_INPUT`, `representation:canonical_bar_1m@1.0.0`) — a provider will
in fact serve any `FeatureSpec` whose single input Ref it can rebuild itself from, but this is
what the shipped providers are documented and built to run against. `available_lag` is left unset:
it is a per-`FeatureSpec` parameter with no default, not a plugin-level constant (see
`infrastructure.plugins.manifest` module docstring).

ADR-0087's implementation record: `indicators.py` / `range_volatility.py` / `microstructure.py`
Providers have no `available_lag` default either (same rule), and their `params_schema` mirrors
each `spec()` factory's required keyword-only parameters exactly (ADR-0085 §"通用规则" #2: no
parameter has a library default, so every one of them is `required`).
"""

from __future__ import annotations

from infrastructure.plugins.manifest import PluginKind, PluginManifest

__all__ = [
    "ADX",
    "AMIHUD_ILLIQUIDITY",
    "ATR",
    "BAR_CLOSE",
    "BAR_HIGH",
    "BAR_LOG_RETURN",
    "BAR_LOW",
    "BAR_REALIZED_VOLATILITY",
    "BAR_VOLUME_SUM",
    "BBANDS_BANDWIDTH",
    "BBANDS_PERCENT_B",
    "CORWIN_SCHULTZ_SPREAD",
    "GARMAN_KLASS_VOLATILITY",
    "JUMP_VARIANCE",
    "MACD_LINE",
    "MACD_SIGNAL",
    "PARKINSON_VOLATILITY",
    "RSI",
    "TAKER_FLOW_IMBALANCE",
    "VWAP",
    "YANG_ZHANG_VOLATILITY",
]

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


# ---------------------------------------------------------------------------------------
# indicators.py (ADR-0085; IMPL-FEAT-IND) — no default value for any of these params
# ---------------------------------------------------------------------------------------

_PERIOD_SCALE_SCHEMA = {
    "type": "object",
    "properties": {
        "period": {"type": "integer", "minimum": 1},
        "scale": {"type": "integer", "minimum": 0},
    },
    "required": ["period", "scale"],
    "additionalProperties": False,
}

_WINDOW_SCALE_SCHEMA = {
    "type": "object",
    "properties": {
        "window": {"type": "integer", "minimum": 1},
        "scale": {"type": "integer", "minimum": 0},
    },
    "required": ["window", "scale"],
    "additionalProperties": False,
}

_MACD_PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "fast": {"type": "integer", "minimum": 1},
        "slow": {"type": "integer", "minimum": 1},
        "signal": {"type": "integer", "minimum": 1},
        "scale": {"type": "integer", "minimum": 0},
    },
    "required": ["fast", "slow", "signal", "scale"],
    "additionalProperties": False,
}

_BBANDS_PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "window": {"type": "integer", "minimum": 1},
        "k": {"type": "string", "description": "positive finite decimal"},
        "scale": {"type": "integer", "minimum": 0},
    },
    "required": ["window", "k", "scale"],
    "additionalProperties": False,
}

_NO_PARAMS_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

ATR = PluginManifest(
    name="atr",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_PERIOD_SCALE_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

RSI = PluginManifest(
    name="rsi",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_PERIOD_SCALE_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

MACD_LINE = PluginManifest(
    name="macd_line",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_MACD_PARAMS_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

MACD_SIGNAL = PluginManifest(
    name="macd_signal",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_MACD_PARAMS_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

BBANDS_PERCENT_B = PluginManifest(
    name="bbands_percent_b",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_BBANDS_PARAMS_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

BBANDS_BANDWIDTH = PluginManifest(
    name="bbands_bandwidth",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_BBANDS_PARAMS_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

VWAP = PluginManifest(
    name="vwap",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_WINDOW_SCALE_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

ADX = PluginManifest(
    name="adx",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_PERIOD_SCALE_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

BAR_CLOSE = PluginManifest(
    name="bar_close",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_NO_PARAMS_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

BAR_HIGH = PluginManifest(
    name="bar_high",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_NO_PARAMS_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

BAR_LOW = PluginManifest(
    name="bar_low",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_NO_PARAMS_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)


# ---------------------------------------------------------------------------------------
# range_volatility.py (ADR-0085; IMPL-FEAT-VOL) — `window` / `scale` required, no default
# in the spec's own params (the `spec()` factory's `scale: int = DEFAULT_SCALE` is caller
# convenience only; the built spec always carries an explicit `scale` param, and rebuilding
# it from `spec.params` via `_canonical` requires the key to be present).
# ---------------------------------------------------------------------------------------

_VOL_WINDOW_SCALE_SCHEMA = {
    "type": "object",
    "properties": {
        "window": {"type": "integer", "minimum": 1},
        "scale": {"type": "integer", "minimum": 1},
    },
    "required": ["window", "scale"],
    "additionalProperties": False,
}

PARKINSON_VOLATILITY = PluginManifest(
    name="parkinson_vol",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_VOL_WINDOW_SCALE_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

GARMAN_KLASS_VOLATILITY = PluginManifest(
    name="garman_klass_vol",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_VOL_WINDOW_SCALE_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

YANG_ZHANG_VOLATILITY = PluginManifest(
    name="yang_zhang_vol",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_VOL_WINDOW_SCALE_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

JUMP_VARIANCE = PluginManifest(
    name="jump_qv",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_VOL_WINDOW_SCALE_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)


# ---------------------------------------------------------------------------------------
# microstructure.py (ADR-0085; IMPL-FEAT-VOL) — same `window` / `scale` shape as above.
# ---------------------------------------------------------------------------------------

TAKER_FLOW_IMBALANCE = PluginManifest(
    name="taker_flow",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_VOL_WINDOW_SCALE_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

AMIHUD_ILLIQUIDITY = PluginManifest(
    name="amihud_illiq",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_VOL_WINDOW_SCALE_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

CORWIN_SCHULTZ_SPREAD = PluginManifest(
    name="cs_spread",
    kind=PluginKind.FEATURE,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema=_VOL_WINDOW_SCALE_SCHEMA,
    inputs=(_BAR_1M_INPUT,),
    outputs=("value:decimal",),
)

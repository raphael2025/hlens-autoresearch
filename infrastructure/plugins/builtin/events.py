"""Static `PluginManifest` for the built-in `EventProvider`s (`plugins/events/`).

`name` / `version` / `deterministic` are checked one by one against the provider classes' `NAME` /
`VERSION` in `tests/infrastructure/plugins/test_builtin_manifests.py`.

An `EventSpec` has no `params` field: every definition parameter lives in `EventSpec.trigger`'s
canonical JSON (`plugins.events._base.trigger_of`), so `params_schema` here documents the trigger
shape for each operator (05-plugin.md's `params_schema` is documentation of a plugin's parameters,
not a literal request payload validator). `inputs` is left empty: a spec names its own upstream
feature / state / event refs at construction time — no provider fixes one.
"""

from __future__ import annotations

from infrastructure.plugins.manifest import PluginKind, PluginManifest

__all__ = [
    "EVENT_ABSENCE",
    "EVENT_CO_OCCURRENCE",
    "EVENT_COUNT",
    "EVENT_SEQUENCE",
    "EVENT_WINDOW_END",
    "FEATURE_THRESHOLD_CROSS",
    "STATE_SWITCH",
    "VOLATILITY_BREAKOUT",
]

_EVENT_OUTPUTS = ("event_time:timestamp", "attributes:object")

FEATURE_THRESHOLD_CROSS = PluginManifest(
    name="feature_threshold_cross",
    kind=PluginKind.EVENT,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {
            "feature": {"type": "string", "description": "feature:name@version Ref"},
            "level": {"type": "string", "description": "decimal crossing level"},
            "direction": {"type": "string", "enum": ["up", "down"]},
        },
        "required": ["feature", "level", "direction"],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=_EVENT_OUTPUTS,
)

VOLATILITY_BREAKOUT = PluginManifest(
    name="volatility_breakout",
    kind=PluginKind.EVENT,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {
            "feature": {"type": "string", "description": "feature:name@version Ref"},
            "window": {"type": "integer", "minimum": 1},
            "multiplier": {"type": "string", "description": "decimal multiplier of the baseline"},
        },
        "required": ["feature", "window", "multiplier"],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=_EVENT_OUTPUTS,
)

STATE_SWITCH = PluginManifest(
    name="state_switch",
    kind=PluginKind.EVENT,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {
            "state": {"type": "string", "description": "state:name@version Ref"},
            "from_state": {"type": "string"},
            "to_state": {"type": "string"},
        },
        "required": ["state"],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=_EVENT_OUTPUTS,
)

_PAIR_PARAMS_SCHEMA_PROPERTIES = {
    "window_us": {"type": "integer", "minimum": 1},
}

EVENT_SEQUENCE = PluginManifest(
    name="event_sequence",
    kind=PluginKind.EVENT,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {
            "first": {"type": "string", "description": "event:name@version Ref"},
            "first_hash": {"type": "string"},
            "then": {"type": "string", "description": "event:name@version Ref"},
            "then_hash": {"type": "string"},
            **_PAIR_PARAMS_SCHEMA_PROPERTIES,
        },
        "required": ["first", "first_hash", "then", "then_hash", "window_us"],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=("event_time:timestamp", "attributes:object", "upstream_event_ids:array(string)"),
)

EVENT_CO_OCCURRENCE = PluginManifest(
    name="event_co_occurrence",
    kind=PluginKind.EVENT,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {
            "left": {"type": "string", "description": "event:name@version Ref"},
            "left_hash": {"type": "string"},
            "right": {"type": "string", "description": "event:name@version Ref"},
            "right_hash": {"type": "string"},
            **_PAIR_PARAMS_SCHEMA_PROPERTIES,
        },
        "required": ["left", "left_hash", "right", "right_hash", "window_us"],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=("event_time:timestamp", "attributes:object", "upstream_event_ids:array(string)"),
)

EVENT_WINDOW_END = PluginManifest(
    name="event_window_end",
    kind=PluginKind.EVENT,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {"window_us": {"type": "integer", "minimum": 1}},
        "required": ["window_us"],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=("event_time:timestamp", "upstream_event_ids:array(string)"),
)

EVENT_ABSENCE = PluginManifest(
    name="event_absence",
    kind=PluginKind.EVENT,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {"window_us": {"type": "integer", "minimum": 1}},
        "required": ["window_us"],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=("event_time:timestamp", "upstream_event_ids:array(string)"),
)

EVENT_COUNT = PluginManifest(
    name="event_count",
    kind=PluginKind.EVENT,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {
            "window_us": {"type": "integer", "minimum": 1},
            "at_least": {"type": "integer", "minimum": 1},
        },
        "required": ["window_us", "at_least"],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=("event_time:timestamp", "attributes:object", "upstream_event_ids:array(string)"),
)

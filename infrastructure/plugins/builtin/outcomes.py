"""Static `PluginManifest` for the built-in `OutcomeProvider`s (`plugins/outcomes/`).

`name` / `version` / `deterministic` are checked one by one against the provider classes' `NAME` /
`VERSION` (`plugins.outcomes._window.LabelProviderBase`) in
`tests/infrastructure/plugins/test_builtin_manifests.py`. `params_schema` reflects
`OutcomeLabelSpec`'s executable label parameters (`core.contracts.outcome.OutcomeLabelSpec`):
`horizon` for all three, plus `upper_barrier` / `lower_barrier` for `triple_barrier`, and
`volatility_feature` / `barrier_multiplier` for `vol_scaled_triple_barrier` (ADR-0088 decision 5,
contract 2.4.0) only. `inputs` is left empty: the price bars (and, for
`vol_scaled_triple_barrier`, the resolved per-event volatility values) a label is computed over are
supplied per `OutcomeRequest` / the provider's constructor, not fixed by the manifest — see
`plugins/outcomes/vol_scaled_triple_barrier.py`'s module docstring for why the volatility channel
is a constructor argument rather than a request field.
"""

from __future__ import annotations

from infrastructure.plugins.manifest import PluginKind, PluginManifest

__all__ = ["HLENS_FORWARD_RETURN", "HLENS_TRIPLE_BARRIER", "HLENS_VOL_SCALED_TRIPLE_BARRIER"]

HLENS_FORWARD_RETURN = PluginManifest(
    name="hlens_forward_return",
    kind=PluginKind.OUTCOME,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {"horizon_seconds": {"type": "integer", "minimum": 1}},
        "required": ["horizon_seconds"],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=("value:decimal",),
)

HLENS_TRIPLE_BARRIER = PluginManifest(
    name="hlens_triple_barrier",
    kind=PluginKind.OUTCOME,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {
            "horizon_seconds": {"type": "integer", "minimum": 1},
            "upper_barrier": {"type": "string", "description": "finite decimal, e.g. 0.02"},
            "lower_barrier": {"type": "string", "description": "finite decimal in (0, 1)"},
        },
        "required": ["horizon_seconds", "upper_barrier", "lower_barrier"],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=("value:decimal", "barrier:integer"),
)

HLENS_VOL_SCALED_TRIPLE_BARRIER = PluginManifest(
    name="hlens_vol_scaled_triple_barrier",
    kind=PluginKind.OUTCOME,
    version="1.0.0",
    contract_version="2.4.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {
            "horizon_seconds": {"type": "integer", "minimum": 1},
            "volatility_feature": {
                "type": "string",
                "description": "Ref[FEATURE] string feature:name@version",
            },
            "barrier_multiplier": {"type": "string", "description": "finite decimal, > 0"},
        },
        "required": ["horizon_seconds", "volatility_feature", "barrier_multiplier"],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=("value:decimal", "barrier:integer"),
)

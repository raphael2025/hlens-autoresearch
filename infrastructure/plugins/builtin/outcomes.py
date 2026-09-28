"""Static `PluginManifest` for the built-in `OutcomeProvider`s (`plugins/outcomes/`).

`name` / `version` / `deterministic` are checked one by one against the provider classes' `NAME` /
`VERSION` (`plugins.outcomes._window.LabelProviderBase`) in
`tests/infrastructure/plugins/test_builtin_manifests.py`. `params_schema` reflects
`OutcomeLabelSpec`'s executable label parameters (`core.contracts.outcome.OutcomeLabelSpec`):
`horizon` for both, plus `upper_barrier` / `lower_barrier` for `triple_barrier` only. `inputs` is
left empty: the price bars a label is computed over are supplied per `OutcomeRequest`, not fixed
by the provider.
"""

from __future__ import annotations

from infrastructure.plugins.manifest import PluginKind, PluginManifest

__all__ = ["HLENS_FORWARD_RETURN", "HLENS_TRIPLE_BARRIER"]

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

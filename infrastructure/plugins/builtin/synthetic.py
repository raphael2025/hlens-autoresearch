"""Static `PluginManifest` for the built-in `SyntheticMarketProvider`
(`plugins/synthetic/random_walk.py`).

`name` / `version` / `deterministic` are checked against `RandomWalkMarket()`'s own `descriptor`
in `tests/infrastructure/plugins/test_builtin_manifests.py`.
"""

from __future__ import annotations

from infrastructure.plugins.manifest import PluginKind, PluginManifest

__all__ = ["HLENS_SYNTHETIC_RANDOM_WALK"]

HLENS_SYNTHETIC_RANDOM_WALK = PluginManifest(
    name="hlens_synthetic_random_walk",
    kind=PluginKind.SYNTHETIC,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {
            "seed": {"type": "integer"},
            "minutes": {"type": "integer", "minimum": 1},
            "initial_price": {"type": "string", "description": "finite positive decimal"},
            "volatility": {"type": "string", "description": "finite decimal"},
        },
        "required": ["seed", "minutes", "initial_price", "volatility"],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=("close:decimal", "volume:decimal"),
)

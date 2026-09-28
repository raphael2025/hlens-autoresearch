"""Static `PluginManifest` for the built-in `BacktestProvider` (`plugins/backtest/bar.py`).

`BarBacktester`'s descriptor version depends on its constructor argument (`execution=None` gives
`1.0.0`; an explicit `ExecutionModel` bumps it to `1.1.0+exec.<fingerprint>` or, with
`carry_over=True`, `1.2.0+exec.<fingerprint>` — `plugins.backtest.bar` module docstring). Those
variants are only known at construction time (the fingerprint binds the actual `ExecutionModel`
parameters), so they cannot be declared as a second, separate static manifest without a real
instance to fingerprint. This manifest covers the default, always-available v1 model
(`execution=None`, `_VERSION = "1.0.0"`) that `BarBacktester()` constructs with no arguments —
the one entry-point discovery can register with no side channel. `name` / `version` /
`deterministic` are checked against `BarBacktester()`'s own `descriptor` in
`tests/infrastructure/plugins/test_builtin_manifests.py`.
"""

from __future__ import annotations

from infrastructure.plugins.manifest import PluginKind, PluginManifest

__all__ = ["HLENS_BAR_BACKTEST"]

HLENS_BAR_BACKTEST = PluginManifest(
    name="hlens_bar_backtest",
    kind=PluginKind.BACKTEST,
    version="1.0.0",
    contract_version="2.0.0",
    deterministic=True,
    params_schema={
        "type": "object",
        "properties": {
            "fee_rate": {"type": "string", "description": "decimal cost model fee rate"},
            "slippage_rate": {"type": "string", "description": "decimal cost model slippage rate"},
        },
        "required": [],
        "additionalProperties": False,
    },
    inputs=(),
    outputs=("equity_close:decimal", "fill_price:decimal", "fee:decimal", "slippage_cost:decimal"),
)

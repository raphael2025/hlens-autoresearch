"""Static `PluginManifest` for every existing built-in Provider (ADR-0087, task 2).

These cover the Provider implementations that already existed before ADR-0087 (`plugins/{features,
states,events,outcomes,backtest,synthetic,llm,knowledge}`), one module per kind. `strategy` and
`risk` have no built-in `PluginManifest` here: those Providers live in `research/strategies/` and
`research/risk/` (still research-stage, not promoted — ADR-0005), and `infrastructure/` must not
import `research/` (`tests/test_architecture_boundaries.py`,
`test_plugins_and_infrastructure_do_not_import_research`).

Each submodule exports plain module-level `PluginManifest` constants — no side effects, no
`Provider` import (`infrastructure/` is not allowed to depend on `plugins/`:
`docs/architecture/01-system.md` §4 only allows `Infrastructure -> Domain`). Every field is a
literal copied from the provider's own `NAME` / `VERSION` / descriptor at the time this module was
written; `tests/infrastructure/plugins/test_builtin_manifests.py` imports both sides and compares
`name` / `version` / `kind` one by one so the two cannot silently drift (ADR-0087 "后果").

`BUILTIN_MANIFESTS` is a flat tuple of every manifest here, for discovery-adjacent tooling and
tests that want to iterate all of them without hand-listing every submodule.
"""

from __future__ import annotations

from infrastructure.plugins.builtin.backtest import HLENS_BAR_BACKTEST
from infrastructure.plugins.builtin.events import (
    EVENT_ABSENCE,
    EVENT_CO_OCCURRENCE,
    EVENT_COUNT,
    EVENT_SEQUENCE,
    EVENT_WINDOW_END,
    FEATURE_THRESHOLD_CROSS,
    STATE_SWITCH,
    VOLATILITY_BREAKOUT,
)
from infrastructure.plugins.builtin.features import (
    BAR_LOG_RETURN,
    BAR_REALIZED_VOLATILITY,
    BAR_VOLUME_SUM,
)
from infrastructure.plugins.builtin.knowledge import HLENS_KNOWLEDGE_LOCAL
from infrastructure.plugins.builtin.llm import HLENS_LLM_SCRIPTED
from infrastructure.plugins.builtin.outcomes import HLENS_FORWARD_RETURN, HLENS_TRIPLE_BARRIER
from infrastructure.plugins.builtin.states import LIQUIDITY_REGIME, TREND_RANGE, VOLATILITY_REGIME
from infrastructure.plugins.builtin.synthetic import HLENS_SYNTHETIC_RANDOM_WALK
from infrastructure.plugins.manifest import PluginManifest

__all__ = [
    "BAR_LOG_RETURN",
    "BAR_REALIZED_VOLATILITY",
    "BAR_VOLUME_SUM",
    "BUILTIN_MANIFESTS",
    "EVENT_ABSENCE",
    "EVENT_CO_OCCURRENCE",
    "EVENT_COUNT",
    "EVENT_SEQUENCE",
    "EVENT_WINDOW_END",
    "FEATURE_THRESHOLD_CROSS",
    "HLENS_BAR_BACKTEST",
    "HLENS_FORWARD_RETURN",
    "HLENS_KNOWLEDGE_LOCAL",
    "HLENS_LLM_SCRIPTED",
    "HLENS_SYNTHETIC_RANDOM_WALK",
    "HLENS_TRIPLE_BARRIER",
    "LIQUIDITY_REGIME",
    "STATE_SWITCH",
    "TREND_RANGE",
    "VOLATILITY_BREAKOUT",
    "VOLATILITY_REGIME",
]

#: Every built-in manifest, flattened (order matches the table in this module's docstring).
BUILTIN_MANIFESTS: tuple[PluginManifest, ...] = (
    BAR_LOG_RETURN,
    BAR_REALIZED_VOLATILITY,
    BAR_VOLUME_SUM,
    VOLATILITY_REGIME,
    LIQUIDITY_REGIME,
    TREND_RANGE,
    FEATURE_THRESHOLD_CROSS,
    VOLATILITY_BREAKOUT,
    STATE_SWITCH,
    EVENT_SEQUENCE,
    EVENT_CO_OCCURRENCE,
    EVENT_WINDOW_END,
    EVENT_ABSENCE,
    EVENT_COUNT,
    HLENS_FORWARD_RETURN,
    HLENS_TRIPLE_BARRIER,
    HLENS_BAR_BACKTEST,
    HLENS_SYNTHETIC_RANDOM_WALK,
    HLENS_LLM_SCRIPTED,
    HLENS_KNOWLEDGE_LOCAL,
)

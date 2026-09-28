"""Plugin manifest and discovery (ADR-0087; docs/architecture/05-plugin.md §4-§6).

`manifest.py` defines `PluginManifest` (strict data class + parsing); `discovery.py` discovers and
validates `hlens.plugins.<kind>` entry points, fail closed, without touching any registry;
`builtin/` holds the static `PluginManifest` for every existing built-in Provider (the ones this
package does not itself register anywhere — see `builtin/README` note in `builtin/__init__.py`).
"""

from __future__ import annotations

from infrastructure.plugins.discovery import (
    DiscoveredPlugin,
    DiscoveryFailure,
    EntryPointLike,
    EntryPointSource,
    PluginDiscoveryError,
    default_entry_point_source,
    discover,
    entry_point_group,
)
from infrastructure.plugins.manifest import (
    PluginKind,
    PluginManifest,
    PluginManifestError,
    SUPPORTED_CONTRACT_MAJOR,
    validate_params_schema,
)

__all__ = [
    "DiscoveredPlugin",
    "DiscoveryFailure",
    "EntryPointLike",
    "EntryPointSource",
    "PluginDiscoveryError",
    "PluginKind",
    "PluginManifest",
    "PluginManifestError",
    "SUPPORTED_CONTRACT_MAJOR",
    "default_entry_point_source",
    "discover",
    "entry_point_group",
    "validate_params_schema",
]

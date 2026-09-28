"""Explicit, process-local lookup for validated plugin discovery results.

The registry accepts only the data returned by ``discover``. It neither discovers plugins nor
imports or instantiates their implementations; the composition root remains responsible for
choosing and wiring providers (ADR-0087, ``docs/architecture/05-plugin.md`` §5).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from infrastructure.plugins.discovery import DiscoveredPlugin

__all__ = [
    "AmbiguousPluginError",
    "DuplicatePluginError",
    "PluginNotFoundError",
    "PluginRegistry",
    "PluginRegistryError",
]


class PluginRegistryError(LookupError):
    """Base error for invalid registration data or an unsuccessful lookup."""


class DuplicatePluginError(PluginRegistryError):
    """The input contains the same kind and name@version more than once."""


class PluginNotFoundError(PluginRegistryError):
    """No discovered plugin matches the requested name@version."""


class AmbiguousPluginError(PluginRegistryError):
    """The requested name@version identifies plugins from more than one kind."""


class PluginRegistry:
    """Resolve validated discovery results by exact ``name@version``.

    Pass results from one or more explicit ``discover`` calls. Repeated identities within a kind
    are rejected during construction. The same name@version in different kinds is retained as an
    ambiguity and rejected when selected, since this API intentionally has no implicit kind or
    precedence rule. No discovery, import, provider construction, or global mutation occurs here.
    """

    __slots__ = ("_plugins_by_key",)

    _plugins_by_key: Mapping[str, tuple[DiscoveredPlugin, ...]]

    def __init__(self, discovered: Iterable[DiscoveredPlugin]) -> None:
        grouped: defaultdict[str, list[DiscoveredPlugin]] = defaultdict(list)
        seen_by_kind: set[tuple[object, str]] = set()

        for plugin in discovered:
            if not isinstance(plugin, DiscoveredPlugin):
                raise TypeError(
                    "PluginRegistry accepts only DiscoveredPlugin results; "
                    f"got {type(plugin).__name__}"
                )
            manifest = plugin.manifest
            key = manifest.plugin_key
            identity = (manifest.kind, key)
            if identity in seen_by_kind:
                raise DuplicatePluginError(
                    f"duplicate discovered plugin for kind={manifest.kind.value!r}: {key}"
                )
            seen_by_kind.add(identity)
            grouped[key].append(plugin)

        # Keep an immutable snapshot: later changes to a caller-owned input collection cannot
        # change which plugin a lookup selects.
        snapshot = {key: tuple(plugins) for key, plugins in grouped.items()}
        self._plugins_by_key = MappingProxyType(snapshot)

    def resolve(self, plugin_key: str) -> DiscoveredPlugin:
        """Return the sole discovered plugin for an exact ``name@version`` selector."""
        if not isinstance(plugin_key, str) or not plugin_key:
            raise ValueError("plugin_key must be a non-empty name@version string")
        name, separator, version = plugin_key.partition("@")
        if not separator or not name or not version or "@" in version:
            raise ValueError(f"plugin_key must use exact name@version syntax: {plugin_key!r}")

        matches = self._plugins_by_key.get(plugin_key)
        if not matches:
            raise PluginNotFoundError(f"no discovered plugin matches {plugin_key!r}")
        if len(matches) != 1:
            kinds = sorted(plugin.manifest.kind.value for plugin in matches)
            raise AmbiguousPluginError(
                f"{plugin_key!r} matches multiple plugin kinds: {', '.join(kinds)}"
            )
        return matches[0]

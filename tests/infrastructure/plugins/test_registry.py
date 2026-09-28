"""Explicit plugin lookup by name@version (ADR-0087; 05-plugin.md §5)."""

from __future__ import annotations

import pytest

from infrastructure.plugins.discovery import DiscoveredPlugin
from infrastructure.plugins.manifest import PluginKind, PluginManifest
from infrastructure.plugins.registry import (
    AmbiguousPluginError,
    DuplicatePluginError,
    PluginNotFoundError,
    PluginRegistry,
)


def _discovered(
    name: str,
    *,
    version: str = "1.0.0",
    kind: PluginKind = PluginKind.FEATURE,
    entry_point_name: str | None = None,
) -> DiscoveredPlugin:
    manifest = PluginManifest(
        name=name,
        kind=kind,
        version=version,
        contract_version="2.0.0",
        deterministic=True,
        params_schema={"type": "object", "properties": {}, "required": []},
        inputs=(),
        outputs=("value:decimal",),
    )
    return DiscoveredPlugin(entry_point_name or name, manifest)


def test_registry_resolves_an_exact_name_at_version_without_loading_classes() -> None:
    plugin = _discovered("bar_log_return", version="1.2.0")
    registry = PluginRegistry((plugin,))

    assert registry.resolve("bar_log_return@1.2.0") is plugin


def test_registry_rejects_a_missing_name_at_version() -> None:
    registry = PluginRegistry((_discovered("bar_log_return"),))

    with pytest.raises(PluginNotFoundError, match="bar_log_return@2.0.0"):
        registry.resolve("bar_log_return@2.0.0")


def test_registry_rejects_duplicate_identity_within_a_kind() -> None:
    first = _discovered("bar_log_return", entry_point_name="first")
    second = _discovered("bar_log_return", entry_point_name="second")

    with pytest.raises(DuplicatePluginError, match="bar_log_return@1.0.0"):
        PluginRegistry((first, second))


def test_registry_rejects_ambiguous_name_at_version_across_kinds() -> None:
    feature = _discovered("shared_name", kind=PluginKind.FEATURE)
    state = _discovered("shared_name", kind=PluginKind.STATE)
    registry = PluginRegistry((feature, state))

    with pytest.raises(AmbiguousPluginError, match="multiple plugin kinds"):
        registry.resolve("shared_name@1.0.0")


def test_registry_takes_a_snapshot_of_discovery_results() -> None:
    discovered = [_discovered("bar_log_return")]
    registry = PluginRegistry(discovered)
    discovered.clear()

    assert registry.resolve("bar_log_return@1.0.0").manifest.name == "bar_log_return"


def test_registry_rejects_malformed_selector() -> None:
    registry = PluginRegistry(())

    with pytest.raises(ValueError, match="name@version"):
        registry.resolve("bar_log_return")

"""Plugin discovery over `importlib.metadata` entry points (ADR-0087; 05-plugin.md §5).

`discover(kind)` reads every entry point declared under the group `hlens.plugins.<kind>`, loads
it, and turns it into a `PluginManifest` — validating, per entry point: the manifest is
structurally legal (`PluginManifest`'s own `__post_init__` / `from_mapping`), `contract_version`'s
major is compatible with the current contract major, and `params_schema` is a legal JSON Schema
subset (all three checks live in `infrastructure.plugins.manifest`; this module does not
re-implement them). Every failure across the whole group is collected — a single bad entry point
fails the *entire* `discover(kind)` call, not just itself, so a caller never registers a partial,
silently-degraded set of plugins (ADR-0087: "任一插件校验失败，整体 fail closed").

`discover` never registers anything into any registry; it only returns the validated manifests
(paired with their entry point name) for the caller to register explicitly.

The entry point *source* is injectable (`source=`) so tests do not depend on an installed
distribution (`pyproject.toml`'s `[project.entry-points."hlens.plugins.<kind>"]` only takes effect
after `uv sync`, which this implementation batch deliberately does not run — see
`docs/adr/0087-plugin-manifest-discovery.md`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from typing import Protocol

from infrastructure.plugins.manifest import PluginKind, PluginManifest, PluginManifestError

__all__ = [
    "DiscoveredPlugin",
    "DiscoveryFailure",
    "EntryPointLike",
    "EntryPointSource",
    "PluginDiscoveryError",
    "default_entry_point_source",
    "discover",
    "entry_point_group",
]


def entry_point_group(kind: PluginKind) -> str:
    """The `importlib.metadata` entry point group name for `kind` (05-plugin.md §5)."""
    return f"hlens.plugins.{kind.value}"


class EntryPointLike(Protocol):
    """The structural shape `discover` needs from an entry point: real, or an injected fake."""

    @property
    def name(self) -> str: ...

    def load(self) -> object: ...


#: Injectable entry point source: `group name` -> the entry points declared under it.
EntryPointSource = Callable[[str], Iterable[EntryPointLike]]


def default_entry_point_source(group: str) -> Iterable[EntryPointLike]:
    """`importlib.metadata.entry_points(group=...)`: what an installed distribution declares."""
    return importlib_metadata.entry_points(group=group)


@dataclass(frozen=True, slots=True)
class DiscoveredPlugin:
    """One entry point that passed every ADR-0087 check, ready for the caller to register."""

    entry_point_name: str
    manifest: PluginManifest


@dataclass(frozen=True, slots=True)
class DiscoveryFailure:
    """One entry point that failed a check; `discover` reports every failure, never just one."""

    entry_point_name: str
    reason: str


class PluginDiscoveryError(RuntimeError):
    """Fail closed: at least one `kind` entry point failed validation (ADR-0087)."""

    def __init__(self, kind: PluginKind, failures: Sequence[DiscoveryFailure]) -> None:
        self.kind = kind
        self.failures: tuple[DiscoveryFailure, ...] = tuple(failures)
        detail = "; ".join(
            f"{failure.entry_point_name}: {failure.reason}" for failure in self.failures
        )
        super().__init__(
            f"{entry_point_group(kind)}: {len(self.failures)} plugin(s) failed discovery: {detail}"
        )


def discover(
    kind: PluginKind, *, source: EntryPointSource = default_entry_point_source
) -> tuple[DiscoveredPlugin, ...]:
    """Discover and validate every `hlens.plugins.<kind>` entry point; fail closed as a whole.

    Never mutates any registry — the caller explicitly registers what is returned.
    """
    entry_points = list(source(entry_point_group(kind)))
    discovered: list[DiscoveredPlugin] = []
    failures: list[DiscoveryFailure] = []
    seen_keys: set[str] = set()
    for entry_point in entry_points:
        try:
            manifest = _load_manifest(entry_point)
            _check_kind(manifest, kind)
        except PluginManifestError as exc:
            failures.append(DiscoveryFailure(entry_point.name, str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 - any load-time failure fails closed, not silently
            failures.append(DiscoveryFailure(entry_point.name, f"failed to load: {exc}"))
            continue
        if manifest.plugin_key in seen_keys:
            failures.append(
                DiscoveryFailure(entry_point.name, f"duplicate name@version: {manifest.plugin_key}")
            )
            continue
        seen_keys.add(manifest.plugin_key)
        discovered.append(DiscoveredPlugin(entry_point.name, manifest))
    if failures:
        raise PluginDiscoveryError(kind, failures)
    return tuple(discovered)


def _load_manifest(entry_point: EntryPointLike) -> PluginManifest:
    loaded = entry_point.load()
    if isinstance(loaded, PluginManifest):
        return loaded
    if isinstance(loaded, Mapping):
        return PluginManifest.from_mapping(loaded)
    raise PluginManifestError(
        f"entry point must resolve to a PluginManifest or an equivalent mapping, "
        f"got {type(loaded).__name__}"
    )


def _check_kind(manifest: PluginManifest, kind: PluginKind) -> None:
    if manifest.kind is not kind:
        raise PluginManifestError(
            f"entry point is declared under {entry_point_group(kind)!r} but "
            f"manifest.kind={manifest.kind.value!r}"
        )

"""`infrastructure.plugins.discovery.discover`: fail-closed entry point discovery (ADR-0087).

The entry point source is injected throughout (`EntryPointSource`), so none of this depends on
`pyproject.toml`'s `[project.entry-points."hlens.plugins.<kind>"]` being installed (ADR-0087
defers `uv sync` to the debugging pass). Three things this suite pins down, one test each:

- a group of entirely legal entry points discovers cleanly and registers nothing itself;
- a **single** bad entry point (unloadable, illegal manifest, wrong kind, duplicate key) fails the
  *whole* `discover(kind)` call and is reported by name — good entries next to it are never
  returned as a silent partial result (ADR-0087: "任一插件校验失败，整体 fail closed");
- every failure in a group is collected, not just the first one found.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from infrastructure.plugins.discovery import (
    EntryPointLike,
    PluginDiscoveryError,
    default_entry_point_source,
    discover,
    entry_point_group,
)
from infrastructure.plugins.manifest import PluginKind, PluginManifest


@dataclass(frozen=True, slots=True)
class _FakeEntryPoint:
    """A minimal `EntryPointLike`: a name plus a zero-arg loader (may raise)."""

    name: str
    loader: Callable[[], object]

    def load(self) -> object:
        return self.loader()


def _source(
    entry_points: dict[str, list[_FakeEntryPoint]],
) -> Callable[[str], list[_FakeEntryPoint]]:
    def source(group: str) -> list[_FakeEntryPoint]:
        return entry_points.get(group, [])

    return source


def _feature_manifest(name: str, *, version: str = "1.0.0") -> PluginManifest:
    return PluginManifest(
        name=name,
        kind=PluginKind.FEATURE,
        version=version,
        contract_version="2.0.0",
        deterministic=True,
        params_schema={"type": "object", "properties": {}, "required": []},
        inputs=(),
        outputs=("value:decimal",),
    )


def test_entry_point_group_name_follows_05_plugin_md() -> None:
    assert entry_point_group(PluginKind.FEATURE) == "hlens.plugins.feature"
    assert entry_point_group(PluginKind.BACKTEST) == "hlens.plugins.backtest"


def test_default_entry_point_source_is_the_real_importlib_metadata_lookup() -> None:
    # No installed distribution declares `hlens.plugins.*` in this batch (ADR-0087 defers `uv
    # sync`), so this only pins that the default source runs and returns something iterable.
    assert list(default_entry_point_source("hlens.plugins.feature")) == []


def test_a_group_of_legal_entry_points_discovers_cleanly() -> None:
    manifest_a = _feature_manifest("bar_log_return")
    manifest_b = _feature_manifest("bar_volume_sum")
    source = _source(
        {
            "hlens.plugins.feature": [
                _FakeEntryPoint("a", lambda: manifest_a),
                _FakeEntryPoint("b", lambda: manifest_b),
            ]
        }
    )
    discovered = discover(PluginKind.FEATURE, source=source)
    assert {item.manifest.name for item in discovered} == {"bar_log_return", "bar_volume_sum"}
    assert {item.entry_point_name for item in discovered} == {"a", "b"}


def test_a_mapping_entry_point_is_parsed_through_from_mapping() -> None:
    payload = {
        "name": "bar_log_return",
        "kind": "feature",
        "version": "1.0.0",
        "contract_version": "2.0.0",
        "deterministic": True,
        "params_schema": {"type": "object", "properties": {}, "required": []},
        "inputs": [],
        "outputs": ["value:decimal"],
    }
    source = _source({"hlens.plugins.feature": [_FakeEntryPoint("a", lambda: payload)]})
    [discovered] = discover(PluginKind.FEATURE, source=source)
    assert discovered.manifest.name == "bar_log_return"


def test_an_empty_group_discovers_nothing_and_does_not_fail() -> None:
    assert discover(PluginKind.FEATURE, source=_source({})) == ()


# ---------------------------------------------------------------- fail closed as a whole


def test_one_entry_point_that_fails_to_load_fails_the_whole_group() -> None:
    good = _feature_manifest("bar_log_return")

    def boom() -> object:
        raise RuntimeError("cannot import this plugin's module")

    source = _source(
        {
            "hlens.plugins.feature": [
                _FakeEntryPoint("good", lambda: good),
                _FakeEntryPoint("bad", boom),
            ]
        }
    )
    with pytest.raises(PluginDiscoveryError) as excinfo:
        discover(PluginKind.FEATURE, source=source)
    error = excinfo.value
    assert error.kind is PluginKind.FEATURE
    assert [failure.entry_point_name for failure in error.failures] == ["bad"]
    assert "cannot import" in error.failures[0].reason


def test_an_illegal_manifest_fails_the_whole_group() -> None:
    good = _feature_manifest("bar_log_return")
    bad_payload = {"name": "not valid"}  # missing every other required field
    source = _source(
        {
            "hlens.plugins.feature": [
                _FakeEntryPoint("good", lambda: good),
                _FakeEntryPoint("bad", lambda: bad_payload),
            ]
        }
    )
    with pytest.raises(PluginDiscoveryError) as excinfo:
        discover(PluginKind.FEATURE, source=source)
    assert [failure.entry_point_name for failure in excinfo.value.failures] == ["bad"]


def test_a_manifest_declared_under_the_wrong_kind_group_fails_the_whole_group() -> None:
    wrong_kind = PluginManifest(
        name="volatility_regime",
        kind=PluginKind.STATE,
        version="1.0.0",
        contract_version="2.0.0",
        deterministic=True,
        params_schema={"type": "object", "properties": {}, "required": []},
        inputs=(),
        outputs=("state:string",),
    )
    source = _source({"hlens.plugins.feature": [_FakeEntryPoint("mismatched", lambda: wrong_kind)]})
    with pytest.raises(PluginDiscoveryError) as excinfo:
        discover(PluginKind.FEATURE, source=source)
    [failure] = excinfo.value.failures
    assert failure.entry_point_name == "mismatched"
    assert "hlens.plugins.feature" in failure.reason


def test_a_duplicate_name_at_version_across_entry_points_fails_the_whole_group() -> None:
    first = _feature_manifest("bar_log_return")
    second = _feature_manifest("bar_log_return")  # same name@version, different entry point
    source = _source(
        {
            "hlens.plugins.feature": [
                _FakeEntryPoint("first", lambda: first),
                _FakeEntryPoint("second", lambda: second),
            ]
        }
    )
    with pytest.raises(PluginDiscoveryError) as excinfo:
        discover(PluginKind.FEATURE, source=source)
    [failure] = excinfo.value.failures
    assert failure.entry_point_name == "second"
    assert "duplicate" in failure.reason


def test_every_failure_in_the_group_is_reported_not_just_the_first() -> None:
    def boom_a() -> object:
        raise RuntimeError("a is broken")

    def boom_b() -> object:
        raise RuntimeError("b is broken")

    source = _source(
        {
            "hlens.plugins.feature": [
                _FakeEntryPoint("a", boom_a),
                _FakeEntryPoint("b", boom_b),
            ]
        }
    )
    with pytest.raises(PluginDiscoveryError) as excinfo:
        discover(PluginKind.FEATURE, source=source)
    assert {failure.entry_point_name for failure in excinfo.value.failures} == {"a", "b"}


def test_discover_never_registers_anything_it_only_returns_manifests() -> None:
    # There is no registry object anywhere in this module's imports: `discover`'s signature takes
    # only a `kind` and an entry point source, and returns a plain tuple. This test pins that
    # contract by checking the return type carries no side-effecting handle, only data.
    manifest = _feature_manifest("bar_log_return")
    source = _source({"hlens.plugins.feature": [_FakeEntryPoint("a", lambda: manifest)]})
    result = discover(PluginKind.FEATURE, source=source)
    assert isinstance(result, tuple)
    assert result[0].manifest is manifest


def test_entry_point_like_protocol_is_satisfied_by_the_fake() -> None:
    fake: EntryPointLike = _FakeEntryPoint("x", lambda: object())
    assert fake.name == "x"

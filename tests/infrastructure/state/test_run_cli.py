"""State run CLI (ADR-0102): compute / show / list, zero side effects by default, redaction."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from core.contracts.catalog import TableNotFound
from core.contracts.state import StateResult
from infrastructure.catalog.iceberg_adapter import CatalogUnavailable
from infrastructure.plugins.builtin import states as builtin_states
from infrastructure.state import run_cli
from infrastructure.state.iceberg import StateTable
from infrastructure.state.runner import run_state, state_inputs, state_request
from infrastructure.state.store import StateResultStore
from infrastructure.state.table_definition import PHASE2_REGISTRY, ensure_state_tables
from plugins.features import BarRealizedVolatilityProvider
from plugins.states import VolatilityRegimeProvider
from tests.infrastructure.catalog.catalog_support import SqliteCatalogHarness
from tests.infrastructure.state.cli_support import (
    PROVIDER,
    TIMES,
    Files,
    feature_run,
    make_files,
    write_json,
)


def _expected(files: Files) -> StateResult:
    vol = BarRealizedVolatilityProvider.spec(5)
    request, result = feature_run(vol)
    provider = VolatilityRegimeProvider((files.spec,))
    return run_state(
        provider, files.spec, state_request(files.spec, TIMES, state_inputs([(request, result)]))
    )


def _forbid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args: object) -> None:
        raise AssertionError("this invocation must not read settings or open a catalog")

    monkeypatch.setattr(run_cli, "Settings", unexpected)
    monkeypatch.setattr(run_cli, "open_postgres_catalog_adapter", unexpected)


@pytest.fixture
def files(tmp_path: Path) -> Files:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    return make_files(inputs)


def test_default_compute_prints_a_summary_and_has_no_side_effects(
    files: Files, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _forbid_environment(monkeypatch)
    before = sorted(tmp_path.rglob("*"))
    assert run_cli.main(files.compute_args()) == 0
    out = capsys.readouterr()
    expected = _expected(files)
    assert f"result_hash\t{expected.result_hash}" in out.out
    assert f"evaluations\t{len(TIMES)}" in out.out
    prefixes = {line.split("\t")[0] for line in out.out.splitlines()}
    assert not prefixes & {"stored", "table"}
    assert out.err == ""
    assert sorted(tmp_path.rglob("*")) == before


def test_store_round_trip_show_and_list(files: Files, tmp_path: Path, capsys: Any) -> None:
    store_dir = tmp_path / "store"
    assert run_cli.main(files.compute_args("--store", str(store_dir))) == 0
    expected = _expected(files)
    assert f"stored\t{store_dir / (expected.result_hash + '.json')}" in capsys.readouterr().out
    assert StateResultStore(store_dir).get(expected.result_hash) == expected

    assert run_cli.main(["list", "--store", str(store_dir)]) == 0
    assert capsys.readouterr().out.split() == [expected.result_hash]

    assert run_cli.main(["show", "--store", str(store_dir), expected.result_hash]) == 0
    shown = capsys.readouterr().out
    assert "verified\tyes" in shown and f"provider\t{PROVIDER}" in shown
    assert "value\t" not in shown

    assert run_cli.main(["show", "--store", str(store_dir), "--values", expected.result_hash]) == 0
    values = [line for line in capsys.readouterr().out.splitlines() if line.startswith("value\t")]
    assert len(values) == len(TIMES)

    # the same run again is an idempotent no-op
    assert run_cli.main(files.compute_args("--store", str(store_dir))) == 0
    assert StateResultStore(store_dir).hashes() == (expected.result_hash,)


def test_show_and_list_never_create_a_missing_store(tmp_path: Path, capsys: Any) -> None:
    missing = tmp_path / "nowhere"
    assert run_cli.main(["list", "--store", str(missing)]) == 1
    assert run_cli.main(["show", "--store", str(missing), "0" * 64]) == 1
    assert not missing.exists()
    assert "not an existing state store" in capsys.readouterr().err


def test_show_rejects_unknown_malformed_and_tampered_runs(
    files: Files, tmp_path: Path, capsys: Any
) -> None:
    store_dir = tmp_path / "store"
    assert run_cli.main(files.compute_args("--store", str(store_dir))) == 0
    capsys.readouterr()
    assert run_cli.main(["show", "--store", str(store_dir), "f" * 64]) == 1
    assert "no state run" in capsys.readouterr().err
    assert run_cli.main(["show", "--store", str(store_dir), "not-a-hash"]) == 1
    assert "64 lowercase hexadecimal" in capsys.readouterr().err

    (path,) = store_dir.glob("*.json")
    path.write_text(path.read_text() + "\n", encoding="utf-8")  # no longer the canonical bytes
    assert run_cli.main(["show", "--store", str(store_dir), path.stem]) == 1
    assert "canonical form" in capsys.readouterr().err


def test_apply_table_appends_to_an_existing_table_and_is_idempotent(
    files: Files, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    harness = SqliteCatalogHarness(tmp_path / "catalog", registry=PHASE2_REGISTRY)
    (tmp_path / "catalog").mkdir()
    adapter = harness.open_adapter()
    ensure_state_tables(adapter)
    seen: dict[str, Any] = {}

    @contextmanager
    def open_adapter(settings: object, registry: Any) -> Iterator[Any]:
        seen["settings"], seen["registry"] = settings, registry
        yield adapter

    monkeypatch.setattr(run_cli, "Settings", lambda: "settings")
    monkeypatch.setattr(run_cli, "open_postgres_catalog_adapter", open_adapter)
    try:
        assert run_cli.main(files.compute_args("--apply-table")) == 0
        first = capsys.readouterr().out
        expected = _expected(files)
        assert "table\tstate.states" in first and "appended" in first
        assert seen["settings"] == "settings"
        loaded = StateTable(adapter).load(expected.result_hash, expected_spec=files.spec)
        assert loaded == expected

        assert run_cli.main(files.compute_args("--apply-table")) == 0
        assert "already present (verified)" in capsys.readouterr().out
    finally:
        harness.cleanup()


def test_apply_table_does_not_create_a_missing_table(
    files: Files, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    (tmp_path / "catalog").mkdir()
    harness = SqliteCatalogHarness(tmp_path / "catalog", registry=PHASE2_REGISTRY)
    adapter = harness.open_adapter()

    @contextmanager
    def open_adapter(_settings: object, _registry: Any) -> Iterator[Any]:
        yield adapter

    monkeypatch.setattr(run_cli, "Settings", lambda: object())
    monkeypatch.setattr(run_cli, "open_postgres_catalog_adapter", open_adapter)
    try:
        assert run_cli.main(files.compute_args("--apply-table")) == 1
        assert f"failed: {TableNotFound.__name__}" in capsys.readouterr().err
        assert adapter.load_table("state.states") is None
    finally:
        harness.cleanup()


def test_catalog_and_settings_errors_are_redacted(
    files: Files, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    secret = "postgresql://state-test:secret@example.invalid/catalog"

    @contextmanager
    def open_adapter(_settings: object, _registry: Any) -> Iterator[Any]:
        raise CatalogUnavailable(secret)
        yield  # pragma: no cover

    monkeypatch.setattr(run_cli, "Settings", lambda: object())
    monkeypatch.setattr(run_cli, "open_postgres_catalog_adapter", open_adapter)
    assert run_cli.main(files.compute_args("--apply-table")) == 1
    err = capsys.readouterr().err
    assert "CatalogUnavailable" in err and secret not in err and "secret" not in err

    def bad_settings() -> object:
        raise ValueError(secret)

    monkeypatch.setattr(run_cli, "Settings", bad_settings)
    assert run_cli.main(files.compute_args("--apply-table")) == 1
    err = capsys.readouterr().err
    assert "ValueError" in err and secret not in err


@pytest.mark.parametrize(
    "provider",
    [
        "nonexistent@1.0.0",
        "volatility_regime",
        "volatility_regime@",
        "@1.0.0",
        "volatility_regime@9.9.9",
    ],
)
def test_unknown_or_unavailable_provider_is_refused(
    files: Files, provider: str, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    _forbid_environment(monkeypatch)
    assert run_cli.main(files.compute_args(provider=provider)) == 1
    out = capsys.readouterr()
    assert "provider" in out.err and out.out == ""


def test_a_provider_that_does_not_serve_the_spec_is_refused(files: Files, capsys: Any) -> None:
    assert run_cli.main(files.compute_args(provider="trend_range@1.0.0")) == 1
    assert "cannot be served by trend_range" in capsys.readouterr().err


def test_a_trained_spec_without_a_seed_is_refused_before_running(
    tmp_path: Path, capsys: Any
) -> None:
    unseeded = make_files(tmp_path, seed=None)
    assert unseeded.spec.training_window is not None and unseeded.spec.seed is None
    store_dir = tmp_path / "store"
    assert run_cli.main(unseeded.compute_args("--store", str(store_dir))) == 1
    out = capsys.readouterr()
    assert "does not fix its seed" in out.err and out.out == ""
    assert not store_dir.exists()


def test_unreadable_and_invalid_inputs_are_refused(
    files: Files, tmp_path: Path, capsys: Any
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    broken = Files(files.spec, bad, files.request_path, files.result_path)
    assert run_cli.main(broken.compute_args()) == 1
    assert "not a readable JSON file" in capsys.readouterr().err

    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    assert (
        run_cli.main(Files(files.spec, array, files.request_path, files.result_path).compute_args())
        == 1
    )
    assert "not a JSON object" in capsys.readouterr().err

    # a result is not a spec: the file parses but is not a StateSpec
    swapped = Files(files.spec, files.result_path, files.request_path, files.result_path)
    assert run_cli.main(swapped.compute_args()) == 1
    assert "not a valid StateSpec" in capsys.readouterr().err

    missing = Files(files.spec, tmp_path / "gone.json", files.request_path, files.result_path)
    assert run_cli.main(missing.compute_args()) == 1
    assert "not a readable JSON file" in capsys.readouterr().err


def test_feature_pairs_must_answer_each_other_and_share_times(
    files: Files, tmp_path: Path, capsys: Any
) -> None:
    vol = BarRealizedVolatilityProvider.spec(5)
    request, _ = feature_run(vol)
    shifted = request.model_copy(update={"evaluation_times": TIMES[:-1]})
    shifted_path = write_json(tmp_path / "shifted.json", shifted)
    mismatch = Files(files.spec, files.spec_path, shifted_path, files.result_path)
    assert run_cli.main(mismatch.compute_args()) == 1
    assert "does not answer its request" in capsys.readouterr().err

    short_request, short_result = feature_run(vol, TIMES[:-1])
    args = files.compute_args()
    args += [
        "--feature",
        str(write_json(tmp_path / "short_request.json", short_request)),
        str(write_json(tmp_path / "short_result.json", short_result)),
    ]
    assert run_cli.main(args) == 1
    assert "same evaluation times" in capsys.readouterr().err


def test_inputs_outside_the_spec_features_are_refused(
    files: Files, tmp_path: Path, capsys: Any
) -> None:
    other_spec = BarRealizedVolatilityProvider.spec(3)
    request, result = feature_run(other_spec)
    request_path = write_json(tmp_path / "other_request.json", request)
    result_path = write_json(tmp_path / "other_result.json", result)
    stray = Files(files.spec, files.spec_path, request_path, result_path)
    assert run_cli.main(stray.compute_args()) == 1
    assert "outside" in capsys.readouterr().err


def test_provider_table_matches_the_builtin_state_manifests() -> None:
    manifest_names = {
        manifest.name
        for manifest in vars(builtin_states).values()
        if type(manifest).__name__ == "PluginManifest"
    }
    assert set(run_cli.PROVIDER_PATHS) == manifest_names
    for name, path in run_cli.PROVIDER_PATHS.items():
        module_name, _, class_name = path.partition(":")
        cls = getattr(__import__(module_name, fromlist=[class_name]), class_name)
        assert cls.NAME == name


def test_run_cli_has_no_static_plugin_or_research_import() -> None:
    tree = ast.parse(Path(run_cli.__file__).read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    assert not roots & {"plugins", "research"}

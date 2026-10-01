"""State read-back CLI (ADR-0102): show / list; read-only, never creates a store."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from core.contracts.state import StateResult
from core.domain.specs import StateSpec
from infrastructure.state import run_cli
from infrastructure.state.store import StateResultStore
from tests.infrastructure.state.cli_support import PROVIDER, TIMES, expected_result, make_files


@pytest.fixture
def stored(tmp_path: Path) -> tuple[Path, StateResult]:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    result = expected_result(make_files(inputs))
    store_dir = tmp_path / "store"
    StateResultStore(store_dir).put(result)
    return store_dir, result


def test_show_and_list_round_trip(stored: tuple[Path, StateResult], capsys: Any) -> None:
    store_dir, result = stored
    assert run_cli.main(["list", "--store", str(store_dir)]) == 0
    assert capsys.readouterr().out.split() == [result.result_hash]

    assert run_cli.main(["show", "--store", str(store_dir), result.result_hash]) == 0
    shown = capsys.readouterr().out
    assert "verified\tyes" in shown and f"provider\t{PROVIDER}" in shown
    assert f"evaluations\t{len(TIMES)}" in shown and "value\t" not in shown

    args = ["show", "--store", str(store_dir), "--values", result.result_hash]
    assert run_cli.main(args) == 0
    values = [line for line in capsys.readouterr().out.splitlines() if line.startswith("value\t")]
    assert len(values) == len(TIMES)


def test_show_and_list_never_create_a_missing_store(tmp_path: Path, capsys: Any) -> None:
    missing = tmp_path / "nowhere"
    assert run_cli.main(["list", "--store", str(missing)]) == 1
    assert run_cli.main(["show", "--store", str(missing), "0" * 64]) == 1
    assert not missing.exists()
    assert "not an existing state store" in capsys.readouterr().err


def test_show_rejects_unknown_malformed_and_tampered_runs(
    stored: tuple[Path, StateResult], capsys: Any
) -> None:
    store_dir, result = stored
    assert run_cli.main(["show", "--store", str(store_dir), "f" * 64]) == 1
    assert "no state run" in capsys.readouterr().err
    assert run_cli.main(["show", "--store", str(store_dir), "not-a-hash"]) == 1
    assert "64 lowercase hexadecimal" in capsys.readouterr().err

    path = store_dir / f"{result.result_hash}.json"
    path.write_text(path.read_text() + "\n", encoding="utf-8")  # no longer the canonical bytes
    assert run_cli.main(["show", "--store", str(store_dir), result.result_hash]) == 1
    assert "canonical form" in capsys.readouterr().err


def test_unexpected_errors_print_only_the_exception_type(
    stored: tuple[Path, StateResult], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    store_dir, result = stored
    secret = "postgresql://user:secret@example.invalid/db"

    def boom(*_args: object) -> None:
        raise RuntimeError(secret)

    monkeypatch.setattr(run_cli, "summary_lines", boom)
    assert run_cli.main(["show", "--store", str(store_dir), result.result_hash]) == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "secret" not in err


def test_read_contract_refuses_unreadable_and_invalid_files(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(run_cli.CliError, match="not a readable JSON file"):
        run_cli.read_contract(StateSpec, bad, "state spec")
    with pytest.raises(run_cli.CliError, match="not a readable JSON file"):
        run_cli.read_contract(StateSpec, tmp_path / "gone.json", "state spec")
    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(run_cli.CliError, match="not a JSON object"):
        run_cli.read_contract(StateSpec, array, "state spec")
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    with pytest.raises(run_cli.CliError, match="not a valid StateSpec"):
        run_cli.read_contract(StateSpec, empty, "state spec")


def test_run_cli_has_no_plugin_or_research_import_of_any_kind() -> None:
    source = Path(run_cli.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    assert not roots & {"plugins", "research", "importlib"}
    assert "import_module" not in source and "PROVIDER" not in source

"""State report CLI (ADR-0102): stored run -> diagnostics; writes only with --out."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.contracts.state import StateResult
from core.domain.specs import StateSpec
from infrastructure.state.store import StateResultStore
from research.reports.state_diagnostics import KIND
from research.states import report_cli, run_cli
from research.states.diagnostics import StateDiagnostics, diagnose
from tests.infrastructure.state.cli_support import Files, make_files, write_json

MIN_RUN = 3  # test-only flicker parameter


@pytest.fixture
def stored(tmp_path: Path, capsys: Any) -> tuple[Files, Path, StateResult]:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    files = make_files(inputs)
    store_dir = tmp_path / "store"
    assert run_cli.main(files.compute_args("--store", str(store_dir))) == 0
    capsys.readouterr()
    (result_hash,) = StateResultStore(store_dir).hashes()
    return files, store_dir, StateResultStore(store_dir).get(result_hash)


def _args(files: Files, store_dir: Path, result_hash: str, *extra: str) -> list[str]:
    return [
        "--store",
        str(store_dir),
        "--spec",
        str(files.spec_path),
        "--min-run",
        str(MIN_RUN),
        *extra,
        result_hash,
    ]


def test_default_prints_markdown_and_writes_nothing(
    stored: tuple[Files, Path, StateResult], tmp_path: Path, capsys: Any
) -> None:
    files, store_dir, result = stored
    before = sorted(tmp_path.rglob("*"))
    assert report_cli.main(_args(files, store_dir, result.result_hash)) == 0
    out = capsys.readouterr()
    assert "flicker" in out.out and out.err == ""
    assert "report\t" not in out.out
    assert sorted(tmp_path.rglob("*")) == before


def test_out_writes_the_deterministic_report_and_repeats_as_a_no_op(
    stored: tuple[Files, Path, StateResult], tmp_path: Path, capsys: Any
) -> None:
    files, store_dir, result = stored
    out_dir = tmp_path / "reports"
    expected = diagnose(result, files.spec.state_space, min_run=MIN_RUN)
    args = _args(files, store_dir, result.result_hash, "--out", str(out_dir))
    assert report_cli.main(args) == 0
    path = out_dir / KIND / f"{expected.diagnostics_hash}.json"
    assert f"report\t{path}\twritten" in capsys.readouterr().out
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["source_result_hash"] == result.result_hash
    assert StateDiagnostics.from_payload(payload, expected_hash=expected.diagnostics_hash)

    assert report_cli.main(args) == 0
    assert "already present (identical)" in capsys.readouterr().out
    assert [p.name for p in (out_dir / KIND).iterdir()] == [path.name]


def test_missing_store_unknown_or_tampered_run_is_refused_without_writing(
    stored: tuple[Files, Path, StateResult], tmp_path: Path, capsys: Any
) -> None:
    files, store_dir, result = stored
    out_dir = tmp_path / "reports"
    missing = tmp_path / "nowhere"
    assert report_cli.main(_args(files, missing, result.result_hash, "--out", str(out_dir))) == 1
    assert "not an existing state store" in capsys.readouterr().err
    assert not missing.exists()

    assert report_cli.main(_args(files, store_dir, "f" * 64, "--out", str(out_dir))) == 1
    assert "no state run" in capsys.readouterr().err

    path = store_dir / f"{result.result_hash}.json"
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    assert report_cli.main(_args(files, store_dir, result.result_hash, "--out", str(out_dir))) == 1
    assert "canonical form" in capsys.readouterr().err
    assert not out_dir.exists()


def test_a_spec_whose_state_space_lacks_the_stored_labels_is_refused(
    stored: tuple[Files, Path, StateResult], tmp_path: Path, capsys: Any
) -> None:
    files, store_dir, result = stored
    narrow = StateSpec.model_validate(
        {**files.spec.model_dump(mode="json"), "state_space": ["low_vol", "mid_vol"]}
    )
    narrow_path = write_json(tmp_path / "narrow.json", narrow)
    args = _args(files, store_dir, result.result_hash)
    args[args.index("--spec") + 1] = str(narrow_path)
    assert report_cli.main(args) == 1
    assert "outside the state space" in capsys.readouterr().err


def test_invalid_min_run_and_unreadable_spec_are_refused(
    stored: tuple[Files, Path, StateResult], tmp_path: Path, capsys: Any
) -> None:
    files, store_dir, result = stored
    args = _args(files, store_dir, result.result_hash)
    args[args.index("--min-run") + 1] = "0"
    assert report_cli.main(args) == 1
    assert "min_run must be a positive int" in capsys.readouterr().err

    bad = tmp_path / "bad.json"
    bad.write_text("nope", encoding="utf-8")
    args = _args(files, store_dir, result.result_hash)
    args[args.index("--spec") + 1] = str(bad)
    assert report_cli.main(args) == 1
    assert "not a readable JSON file" in capsys.readouterr().err


def test_unexpected_errors_print_only_the_exception_type(
    stored: tuple[Files, Path, StateResult], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    files, store_dir, result = stored
    secret = "postgresql://user:secret@example.invalid/db"

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(secret)

    monkeypatch.setattr(report_cli, "diagnose", boom)
    assert report_cli.main(_args(files, store_dir, result.result_hash)) == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "secret" not in err

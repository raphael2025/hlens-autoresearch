"""ADR-0105 §4: the explicit degradation batch driver (`research.operations.degradation_batch`).

Entries run through the real degradation CLI logic over TEST ONLY cases
(`tests/research/operations/fixtures.py`: frozen Profile in a temporary freeze registry, PASS
baseline, ACTIVE history, recent manifest); only the failure-isolation tests substitute the CLI
callable, to raise what a real run would not.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from research.operations import degradation_batch, degradation_cli
from research.operations.degradation_batch import (
    MANIFEST_FORMAT,
    BatchManifestError,
    load_manifest,
    main,
    run_batch,
)
from tests.research.operations.fixtures import (
    WINDOW_END,
    WINDOW_START,
    Case,
    read_json,
    write_case,
)

LABEL = "TEST ONLY window"


def _q(value: object) -> str:
    return json.dumps(str(value))  # a TOML basic string for these ASCII paths


def _entry(case: Case, **overrides: str) -> str:
    return _entry_at(case, None, overrides)


def _entry_at(case: Case, relative_to: Path | None, overrides: Mapping[str, str]) -> str:
    def path(p: Path) -> str:
        return _q(p.relative_to(relative_to) if relative_to is not None else p)

    fields: dict[str, str] = {
        "subject": _q(case.subject),
        "profile": path(case.profile_path),
        "baseline_report": path(case.report_path),
        "baseline_set": path(case.baseline_set_path),
        "window_start": _q(WINDOW_START),
        "window_end": _q(WINDOW_END),
        "window_label": _q(LABEL),
        "freeze_registry": path(case.freeze_registry),
        "freeze_anchor": path(case.freeze_anchor),
        "lifecycle": path(case.lifecycle_path),
        "recent_manifest": path(case.recent_path),
    }
    fields.update(overrides)
    body = "\n".join(f"{key} = {value}" for key, value in fields.items() if value != "")
    return f"[[entry]]\n{body}\n"


def _manifest(tmp_path: Path, *entries: str, header: str | None = None) -> Path:
    head = f'format = "{MANIFEST_FORMAT}"\n\n' if header is None else header
    path = tmp_path / "batch.toml"
    path.write_text(head + "\n".join(entries), encoding="utf-8")
    return path


@pytest.fixture
def reports(tmp_path: Path) -> Path:
    root = tmp_path / "reports"
    root.mkdir()
    return root


def _report_files(reports: Path) -> list[Path]:
    return sorted(reports.glob("degradation_check/*.json"))


def test_every_entry_runs_in_manifest_order_and_a_degraded_result_is_a_success(
    tmp_path: Path, reports: Path
) -> None:
    healthy = write_case(tmp_path / "a", recent_value="3.2")
    degraded = write_case(tmp_path / "b", recent_value="0.1")  # decline 3.4 > allowed 0.5
    manifest = _manifest(tmp_path, _entry(healthy), _entry(degraded))
    result = run_batch(manifest, reports_root=reports)
    assert [item.index for item in result.entries] == [0, 1]
    assert [dict(item.fields)["status"] for item in result.entries] == [
        "not_degraded",
        "degraded",
    ]
    assert all(item.succeeded for item in result.entries) and result.exit_code == 0
    assert result.failed == ()
    files = _report_files(reports)
    assert len(files) == 2
    assert {dict(item.fields)["check_hash"] for item in result.entries} == {
        read_json(file)["check_hash"] for file in files
    }


def test_a_failing_entry_is_recorded_and_the_batch_continues(tmp_path: Path, reports: Path) -> None:
    good = write_case(tmp_path / "good", recent_value="3.2")
    other = write_case(tmp_path / "other", recent_value="3.1")
    broken = _entry(good, recent_manifest=_q(tmp_path / "missing_recent.json"))
    refused = _entry(good, window_end=_q("2026-03-01T00:00:00"))  # no UTC offset
    manifest = _manifest(tmp_path, _entry(good), broken, refused, _entry(other))
    result = run_batch(manifest, reports_root=reports)
    assert [item.exit_code for item in result.entries] == [0, 3, 1, 0]
    assert [item.index for item in result.failed] == [1, 2]
    assert result.exit_code == 1
    assert "refused" in result.entries[2].error or "failed" in result.entries[1].error
    assert len(_report_files(reports)) == 2  # the entries around the failures still wrote


def test_the_same_manifest_run_again_is_idempotent(tmp_path: Path, reports: Path) -> None:
    manifest = _manifest(tmp_path, _entry(write_case(tmp_path / "a")))
    first = run_batch(manifest, reports_root=reports)
    again = run_batch(manifest, reports_root=reports)
    assert first.exit_code == again.exit_code == 0
    assert first.entries[0].fields == again.entries[0].fields
    assert len(_report_files(reports)) == 1


def test_relative_paths_resolve_against_the_manifest_directory_not_the_cwd(
    tmp_path: Path, reports: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = write_case(tmp_path / "case")
    manifest = _manifest(tmp_path, _entry_at(case, tmp_path, {}))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert run_batch(manifest, reports_root=reports).exit_code == 0


def test_a_missing_reports_root_fails_every_entry(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, _entry(write_case(tmp_path / "a")))
    result = run_batch(manifest, reports_root=tmp_path / "no_such_reports")
    assert result.exit_code == 1 and result.entries[0].exit_code == 1
    assert "reports root" in result.entries[0].error or "refused" in result.entries[0].error


def test_an_unexpected_exception_or_usage_exit_of_one_entry_does_not_stop_the_batch(
    tmp_path: Path, reports: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = write_case(tmp_path / "a")
    manifest = _manifest(tmp_path, _entry(case), _entry(case), _entry(case))
    real_main = degradation_cli.main
    calls: list[int] = []

    def flaky(argv: list[str] | None = None, **kwargs: object) -> int:
        calls.append(len(calls))
        if calls[-1] == 0:
            raise RuntimeError("boom")
        if calls[-1] == 1:
            raise SystemExit(2)  # an argparse usage error
        return real_main(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(degradation_cli, "main", flaky)
    result = run_batch(manifest, reports_root=reports)
    assert calls == [0, 1, 2]
    assert [item.exit_code for item in result.entries] == [3, 2, 0]
    assert "RuntimeError" in result.entries[0].error and "boom" not in result.entries[0].error
    assert result.exit_code == 1


def test_the_batch_never_expands_or_discovers_entries(
    tmp_path: Path, reports: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = write_case(tmp_path / "a")
    manifest = _manifest(tmp_path, _entry(case), _entry(case, window_label=_q("second")))
    seen: list[list[str]] = []
    real_main = degradation_cli.main

    def spy(argv: list[str] | None = None, **kwargs: object) -> int:
        assert argv is not None
        seen.append(list(argv))
        return real_main(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(degradation_cli, "main", spy)
    run_batch(manifest, reports_root=reports)
    assert len(seen) == 2  # exactly the manifest's entries, in order
    assert all(argv[-2:] == ["--reports-root", str(reports)] for argv in seen)
    assert seen[0][seen[0].index("--window-label") + 1] == LABEL
    assert seen[1][seen[1].index("--window-label") + 1] == "second"
    assert "--authority-environment" not in seen[0]


def test_an_authority_entry_becomes_the_authority_mode_arguments(tmp_path: Path) -> None:
    case = write_case(tmp_path / "a")
    authority = {
        "lifecycle": "",
        "recent_manifest": "",
        "authority_registry": _q(tmp_path / "registry"),
        "authority_head": _q("latest"),
        "dataset_id": _q("dataset-1"),
        "manifest_hash": _q("a" * 64),
        "authority_as_of": _q("2026-03-02T00:00:00Z"),
        "authority_environment": _q("pkg.module:factory"),
        "authority_anchor": _q(tmp_path / "anchor.jsonl"),
    }
    manifest = _manifest(tmp_path, _entry(case, **authority))
    [entry] = load_manifest(manifest)
    assert entry.authority
    argv = list(entry.argv)
    for flag, value in (
        ("--authority-head", "latest"),
        ("--dataset-id", "dataset-1"),
        ("--authority-as-of", "2026-03-02T00:00:00Z"),
        ("--authority-environment", "pkg.module:factory"),
    ):
        assert argv[argv.index(flag) + 1] == value
    assert "--lifecycle" not in argv and "--recent-manifest" not in argv


def test_an_authority_entry_without_a_registry_is_a_recorded_failure(
    tmp_path: Path, reports: Path
) -> None:
    case = write_case(tmp_path / "a")
    authority = {
        "lifecycle": "",
        "recent_manifest": "",
        "authority_registry": _q(tmp_path / "no_registry"),
        "authority_head": _q("latest"),
        "dataset_id": _q("dataset-1"),
        "manifest_hash": _q("a" * 64),
        "authority_as_of": _q("2026-03-02T00:00:00Z"),
    }
    result = run_batch(_manifest(tmp_path, _entry(case, **authority)), reports_root=reports)
    assert result.exit_code == 1 and "refused at input" in result.entries[0].error
    assert _report_files(reports) == []


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("not toml", "format = [unterminated"),
        ("duplicate key", f'format = "{MANIFEST_FORMAT}"\nformat = "x"\n'),
        ("no format", "[[entry]]\nsubject = 'x'\n"),
        ("another format", 'format = "other@1"\n[[entry]]\nsubject = "x"\n'),
        ("no entries", f'format = "{MANIFEST_FORMAT}"\n'),
        ("empty entry list", f'format = "{MANIFEST_FORMAT}"\nentry = []\n'),
        ("unknown top-level key", f'format = "{MANIFEST_FORMAT}"\nreports_root = "x"\n'),
    ],
)
def test_a_malformed_manifest_is_refused_before_anything_runs(
    tmp_path: Path, reports: Path, name: str, text: str
) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(BatchManifestError):
        run_batch(path, reports_root=reports)
    assert main(["--manifest", str(path), "--reports-root", str(reports)]) == 2, name
    assert _report_files(reports) == []


def test_a_missing_manifest_file_is_refused(tmp_path: Path, reports: Path) -> None:
    with pytest.raises(BatchManifestError, match="cannot be read"):
        run_batch(tmp_path / "absent.toml", reports_root=reports)


def test_entries_are_parsed_strictly(tmp_path: Path, reports: Path) -> None:
    case = write_case(tmp_path / "a")
    authority = {
        "authority_registry": _q("r"),
        "authority_head": _q("latest"),
        "dataset_id": _q("d"),
        "manifest_hash": _q("a" * 64),
        "authority_as_of": _q("2026-03-02T00:00:00Z"),
    }
    cases: dict[str, str] = {
        "unknown key": _entry(case, surprise=_q("x")),
        "reports_root in an entry": _entry(case, reports_root=_q(reports)),
        "missing subject": _entry(case, subject=""),
        "missing window_end": _entry(case, window_end=""),
        "missing recent_manifest": _entry(case, recent_manifest=""),
        "both modes": _entry(case, **authority),
        "partial authority": _entry(
            case, lifecycle="", recent_manifest="", authority_registry=_q("r")
        ),
        "authority-only key in the explicit mode": _entry(case, authority_anchor=_q("a")),
        "integer value": _entry(case, window_label="7"),
        "native TOML datetime": _entry(case, window_start="2026-02-01T00:00:00Z"),
        "empty string": _entry(case, window_label=_q("")),
        "padded string": _entry(case, window_label=_q(" x ")),
    }
    for name, entry in cases.items():
        path = _manifest(tmp_path, entry)
        with pytest.raises(BatchManifestError):
            load_manifest(path)
        assert name  # (the loop variable names the failing case in a traceback)
    assert _report_files(reports) == []


def test_the_command_line_summarizes_and_exits_non_zero_when_any_entry_failed(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case = write_case(tmp_path / "a")
    ok_manifest = _manifest(tmp_path, _entry(case))
    assert main(["--manifest", str(ok_manifest), "--reports-root", str(reports)]) == 0
    out = capsys.readouterr().out
    assert "entry=1" in out and "status=not_degraded" in out and "entries=1 failed=0" in out
    mixed = _manifest(
        tmp_path, _entry(case), _entry(case, recent_manifest=_q(tmp_path / "nope.json"))
    )
    assert main(["--manifest", str(mixed), "--reports-root", str(reports)]) == 1
    captured = capsys.readouterr()
    assert "entries=2 failed=1" in captured.out and "failed exit=3" in captured.out
    assert "entry failed" in captured.err or "failed at" in captured.err


def test_the_module_reads_no_clock_and_starts_no_subprocess() -> None:
    path = Path(degradation_batch.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not imported & {"subprocess", "time", "os", "multiprocessing"}
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not calls & {"now", "utcnow", "today", "time", "system", "run", "Popen"}

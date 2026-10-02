"""``python -m infrastructure.dataset.cli`` (ADR-0101 D2): build, show, verify and exit codes."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from core.contracts.catalog import CatalogError
from infrastructure.catalog.iceberg_adapter import CatalogUnavailable
from infrastructure.catalog.phase1_tables import (
    DATASET_EVIDENCE_MANIFESTS,
    DATASET_SELECTION_CHUNKS,
)
from infrastructure.dataset import cli, factory
from infrastructure.dataset.builder import DatasetBuilder
from infrastructure.pit.selector import PitSelector
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE, UniverseBuilder
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import World
from tests.infrastructure.dataset.entry_support import (
    SECRET_DSN,
    make_profile,
    patch_catalog,
    profile_document,
    settings_for,
    v3_ready,
    write_profile,
)

REPO = Path(__file__).resolve().parents[3]
UNIVERSE = f"{FIRST_SLICE_UNIVERSE.name}@{FIRST_SLICE_UNIVERSE.version}"
REQUEST = [
    "--data-type",
    "agg_trades",
    "--start",
    "2023-11-14T21:00:00Z",
    "--end",
    "2023-11-14T23:00:00+00:00",
    "--simulation-time",
    "2023-12-20T00:00:00Z",
    "--knowledge-cutoff",
    "2023-12-20T00:00:00Z",
    "--universe",
    UNIVERSE,
]


def _argv(command: str, profile: Path, *extra: str, request: bool = True) -> list[str]:
    return [command, "--profile", str(profile), *(REQUEST if request else []), *extra]


def _run(
    capsys: pytest.CaptureFixture[str], w: World, argv: list[str]
) -> tuple[int, dict[str, Any] | None, str]:
    code = cli.main(argv, settings=settings_for(w))
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out else None), captured.err


# ------------------------------------------------------------------------- usage errors


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["build"],
        ["bogus"],
        ["verify", "--profile", "p.json"],
        ["build", "--profile", "p.json"],
        ["show", "--profile", "p.json", *REQUEST[:-2]],  # no --universe
    ],
)
def test_missing_or_unknown_arguments_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        cli.main(argv)
    assert exit_.value.code == cli.EXIT_USAGE


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--universe", "hlens.universe.other@1.0.0"),
        ("--universe", FIRST_SLICE_UNIVERSE.name),
        ("--universe", f"{FIRST_SLICE_UNIVERSE.name}@9.9.9"),
        ("--universe", f"@{FIRST_SLICE_UNIVERSE.version}"),
        ("--data-type", "trades_raw"),
        ("--start", "2023-11-14T21:00:00"),  # naive
        ("--end", "not-a-time"),
        ("--simulation-time", "2023-12-20"),
        ("--knowledge-cutoff", "2023-12-20T00:00:00"),
    ],
)
def test_a_bad_request_value_exits_2(
    flag: str, value: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _argv("show", tmp_path / "p.json")
    argv[argv.index(flag) + 1] = value
    with pytest.raises(SystemExit) as exit_:
        cli.main(argv)
    assert exit_.value.code == cli.EXIT_USAGE
    assert capsys.readouterr().err.strip()


def test_only_registered_universes_are_accepted(tmp_path: Path) -> None:
    args = cli._parser().parse_args(_argv("show", tmp_path / "p.json"))
    assert args.universe is FIRST_SLICE_UNIVERSE
    assert args.listing_assumption is False
    assert args.start.utcoffset().total_seconds() == 0


def test_timestamps_with_any_offset_are_normalised_to_utc(tmp_path: Path) -> None:
    argv = _argv("show", tmp_path / "p.json")
    argv[argv.index("--start") + 1] = "2023-11-14T23:00:00+02:00"
    args = cli._parser().parse_args(argv)
    assert args.start.isoformat() == "2023-11-14T21:00:00+00:00"


# ------------------------------------------------------------------------- profile / settings


def test_an_invalid_profile_exits_3_before_anything_is_opened(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened = patch_catalog(monkeypatch, w)
    document = profile_document()
    del document["rule"]
    path = write_profile(tmp_path / "bad.json", document)
    code, out, err = _run(capsys, w, _argv("build", path))
    assert (code, out) == (cli.EXIT_PROFILE, None)
    assert "invalid profile" in err and "rule" in err
    assert opened == []
    code, _, err = _run(capsys, w, _argv("show", tmp_path / "absent.json"))
    assert code == cli.EXIT_PROFILE and "cannot be read" in err


def test_invalid_settings_exit_4_naming_fields_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = make_profile(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HLENS_CATALOG_URI", raising=False)
    monkeypatch.setenv("HLENS_HTTP_USER_AGENT", "   ")
    code = cli.main(_argv("show", path))
    err = capsys.readouterr().err
    assert code == cli.EXIT_ENVIRONMENT
    assert "settings are invalid" in err and "catalog_uri" in err
    assert "   " not in err.split("invalid:")[1].strip().replace(", ", "")


def test_a_catalog_failure_exits_4_without_leaking_credentials(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = make_profile(tmp_path)

    def failing(settings: Any, registry: Any) -> Any:
        raise CatalogUnavailable(f"cannot connect to {SECRET_DSN} (password fake-s3cr3t-pw)")

    monkeypatch.setattr(factory, "open_postgres_catalog_adapter", failing)
    code, out, err = _run(capsys, w, _argv("show", path))
    assert (code, out) == (cli.EXIT_ENVIRONMENT, None)
    assert "did not open" in err and "CatalogUnavailable" in err
    for secret in ("fake-s3cr3t-pw", "hlens_user", SECRET_DSN):
        assert secret not in err


def test_redaction_removes_the_dsn_and_url_credentials() -> None:
    text = f"boom {SECRET_DSN}; also https://user:tok@host/x and mysql://a@b"
    cleaned = cli._redact(text, [SECRET_DSN])
    assert "fake-s3cr3t-pw" not in cleaned and "tok" not in cleaned and "user:" not in cleaned
    assert "<redacted>" in cleaned and "host/x" in cleaned
    assert cli._redact("nothing here", [""]) == "nothing here"


def test_secrets_cover_the_dsn_and_its_user_and_password(w: World) -> None:
    assert cli._secrets(settings_for(w)) == [SECRET_DSN, "fake-s3cr3t-pw", "hlens_user"]


def test_an_unexpected_failure_prints_only_its_type(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = make_profile(tmp_path)
    patch_catalog(monkeypatch, w)

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"driver said {SECRET_DSN}")

    monkeypatch.setattr(cli, "pin_dataset_pit_spec", explode)
    code, out, err = _run(capsys, w, _argv("build", path))
    assert (code, out) == (cli.EXIT_FAILED, None)
    assert err.strip() == "error: build failed (RuntimeError)"


def test_a_known_failure_is_reported_redacted(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = make_profile(tmp_path)
    patch_catalog(monkeypatch, w)

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise CatalogError(f"catalog said {SECRET_DSN}")

    monkeypatch.setattr(cli, "pin_dataset_pit_spec", explode)
    code, _, err = _run(capsys, w, _argv("show", path))
    assert code == cli.EXIT_FAILED
    assert "CatalogError" in err and "fake-s3cr3t-pw" not in err and "<redacted>" in err


# ------------------------------------------------------------------------- the real path


@pytest.fixture
def ready(w: World, tmp_path: Path) -> World:
    v3_ready(w, tmp_path)
    return w


def _heads(w: World) -> tuple[str | None, str | None]:
    return (
        w.h.head(DATASET_EVIDENCE_MANIFESTS.table),
        w.h.head(DATASET_SELECTION_CHUNKS.table),
    )


def test_show_prints_the_plan_and_writes_nothing(
    ready: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, profile = make_profile(tmp_path)
    patch_catalog(monkeypatch, w := ready)
    before = _heads(w)
    code, shown, err = _run(capsys, w, _argv("show", path))
    assert code == cli.EXIT_OK and err == ""
    assert shown is not None
    assert shown["command"] == "show"
    assert shown["profile_hash"] == profile.profile_hash()
    assert shown["capacity_evidence"] == "none"
    assert shown["rule_hash"] == profile.rule.rule_hash
    assert shown["manifest_exists"] is False
    assert shown["listing_assumption"] is False
    assert shown["universe"] == UNIVERSE and shown["data_type"] == "agg_trades"
    assert shown["start"] == "2023-11-14T21:00:00+00:00"
    assert shown["end"] == "2023-11-14T23:00:00+00:00"
    assert "canonical.trades" in shown["snapshot_bindings"]
    assert _heads(w) == before == (None, None)
    code, assumed, _ = _run(capsys, w, _argv("show", path, "--listing-assumption"))
    assert code == cli.EXIT_OK and assumed is not None
    assert assumed["listing_assumption"] is True
    assert assumed["pit_content_hash"] != shown["pit_content_hash"]
    assert assumed["selection_id"] != shown["selection_id"]


def test_build_then_rebuild_then_show_then_verify(
    ready: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    w = ready
    path, profile = make_profile(tmp_path)
    patch_catalog(monkeypatch, w)
    code, built, err = _run(capsys, w, _argv("build", path))
    assert code == cli.EXIT_OK and err == "" and built is not None
    assert built["command"] == "build"
    assert built["profile_hash"] == profile.profile_hash()
    assert built["capacity_evidence"] == "none"
    assert built["row_count"] > 0 and built["chunk_count"] >= 1
    assert built["manifest_replayed"] is False and built["replayed"] is False
    assert {item["stream"] for item in built["evidence"]} >= {"members", "chunk_proofs"}
    assert built["dataset"]["table"] == DATASET_SELECTION_CHUNKS.table

    code, again, _ = _run(capsys, w, _argv("build", path))
    assert code == cli.EXIT_OK and again is not None
    assert again["manifest_hash"] == built["manifest_hash"]
    assert again["replayed"] is True and again["manifest_replayed"] is True

    code, shown, _ = _run(capsys, w, _argv("show", path))
    assert code == cli.EXIT_OK and shown is not None
    assert shown["selection_id"] == built["selection_id"]
    assert shown["pit_content_hash"] == built["pit_content_hash"]
    assert shown["manifest_exists"] is True

    code, verified, err = _run(
        capsys, w, _argv("verify", path, "--manifest-hash", built["manifest_hash"], request=False)
    )
    assert code == cli.EXIT_OK and err == "" and verified is not None
    assert verified["verified"] is True
    assert verified["selection_id"] == built["selection_id"]
    assert verified["row_count"] == built["row_count"]
    assert verified["chunk_count"] == built["chunk_count"]
    assert verified["profile_hash"] == profile.profile_hash()


def test_verify_of_an_unknown_manifest_exits_5(
    ready: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, _ = make_profile(tmp_path)
    patch_catalog(monkeypatch, ready)
    code, out, err = _run(
        capsys, ready, _argv("verify", path, "--manifest-hash", "0" * 64, request=False)
    )
    assert (code, out) == (cli.EXIT_NOT_FOUND, None)
    assert "no v3 manifest" in err


def test_verify_under_another_rule_fails_with_exit_1(
    ready: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    w = ready
    path, _ = make_profile(tmp_path)
    patch_catalog(monkeypatch, w)
    _, built, _ = _run(capsys, w, _argv("build", path))
    assert built is not None
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_path, _ = make_profile(other_dir, rule={**profile_document()["rule"], "chunk_rows": 3})
    code, out, err = _run(
        capsys,
        w,
        _argv("verify", other_path, "--manifest-hash", built["manifest_hash"], request=False),
    )
    assert (code, out) == (cli.EXIT_FAILED, None)
    assert "verify failed" in err and "not the verifier's rule" in err


def test_a_request_the_builder_rejects_exits_2(
    ready: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, _ = make_profile(tmp_path)
    patch_catalog(monkeypatch, ready)
    argv = _argv("show", path)
    argv[argv.index("--end") + 1] = "2023-11-14T21:00:00Z"  # an empty window
    code, out, err = _run(capsys, ready, argv)
    assert (code, out) == (cli.EXIT_USAGE, None)
    assert "invalid request" in err


def test_capacity_evidence_is_echoed_when_the_profile_carries_it(
    ready: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, profile = make_profile(tmp_path, capacity_evidence="capacity-run-2026-10")
    patch_catalog(monkeypatch, ready)
    code, shown, _ = _run(capsys, ready, _argv("show", path))
    assert code == cli.EXIT_OK and shown is not None
    assert shown["capacity_evidence"] == "capacity-run-2026-10"
    assert shown["profile_hash"] == profile.profile_hash()


def test_the_entry_never_touches_the_v2_or_unbounded_paths(
    ready: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    w = ready
    path, _ = make_profile(tmp_path)
    patch_catalog(monkeypatch, w)

    def forbidden(name: str) -> Any:
        def fail(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(f"{name} is not part of the v3 entry")

        return fail

    monkeypatch.setattr(DatasetBuilder, "select", forbidden("DatasetBuilder.select"))
    monkeypatch.setattr(DatasetBuilder, "build", forbidden("DatasetBuilder.build"))
    monkeypatch.setattr(PitSelector, "select", forbidden("PitSelector.select"))
    monkeypatch.setattr(UniverseBuilder, "build", forbidden("UniverseBuilder.build"))
    for command in ("show", "build", "build"):  # a fresh build, then its replay
        code, _, err = _run(capsys, w, _argv(command, path))
        assert code == cli.EXIT_OK, err


# ------------------------------------------------------------------------- static guard

_FORBIDDEN_NAMES = {"DatasetBuilder", "UniverseBuilder", "PitSelector", "ManifestStore"}


def _tree(name: str) -> ast.Module:
    return ast.parse((REPO / "infrastructure" / "dataset" / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("module", ["cli.py", "factory.py"])
def test_entry_modules_do_not_import_the_v2_builders(module: str) -> None:
    tree = _tree(module)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
    assert not imported & _FORBIDDEN_NAMES, sorted(imported & _FORBIDDEN_NAMES)


@pytest.mark.parametrize("module", ["cli.py", "factory.py"])
def test_entry_modules_call_only_the_bounded_pipeline_methods(module: str) -> None:
    """No ``.select(...)`` call at all; ``.build(...)`` only on the ``DatasetBuildPipeline``."""
    for node in ast.walk(_tree(module)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        assert node.func.attr != "select", f"{module}:{node.lineno} calls .select()"
        if node.func.attr == "build":
            receiver = node.func.value
            name = (
                receiver.attr
                if isinstance(receiver, ast.Attribute)
                else getattr(receiver, "id", "")
            )
            assert name == "pipeline", f"{module}:{node.lineno} calls .build() on {name!r}"


def test_the_cli_module_is_runnable_as_a_script_entry() -> None:
    tree = _tree("cli.py")
    guards = [
        node
        for node in tree.body
        if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test)
    ]
    assert guards and "SystemExit(main())" in ast.unparse(guards[0])


def test_ds_world_origin_matches_the_pipeline_origin(w: World) -> None:
    assert factory.market_data_origin(settings_for(w)) == ds.ORIGIN

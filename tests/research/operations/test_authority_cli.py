"""ADR-0098 §4 / 修订 1: the authority mode of ``research.operations.degradation_cli`` — usage
errors (argparse exit 2), input and authority refusals (exit 1, the refusal code on stderr, no
report written) and I/O faults (exit 3). A successful authority run needs a real v3 dataset and is
in ``test_authority_resolver.py``.

The caller-declared files are the TEST ONLY case of ``tests/research/operations/fixtures.py``;
the Lifecycle Registry is real. Where an ``AuthorityEnvironment`` is needed only to reach the
lifecycle authority (which refuses first), its catalog is a stand-in that is never read: the
resolver checks the lifecycle before it touches the catalog.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.lifecycle.strategy import LifecycleState
from infrastructure.registry.lifecycle import JOURNAL_NAME, LifecycleRegistry
from research.loop.dataset_source import DatasetCatalog
from research.operations import degradation_cli
from research.operations.authority import AuthorityEnvironment
from tests.research.operations import authority_fixtures as af
from tests.research.operations.fixtures import Case, write_case

S = LifecycleState
FACTORY = "tests.research.operations.authority_fixtures:environment_factory"
CONTEXT = "tests.research.operations.authority_fixtures:environment_context"
FAILING = "tests.research.operations.authority_fixtures:failing_factory"
DEFAULT = "research.operations.authority_environment:default_environment"


@pytest.fixture
def case(tmp_path: Path) -> Case:
    return write_case(tmp_path / "case")


@pytest.fixture
def reports(tmp_path: Path) -> Path:
    root = tmp_path / "reports"
    root.mkdir()
    return root


def _registry(tmp_path: Path, path: tuple[LifecycleState, ...] = af.PATH_TO_ACTIVE) -> Path:
    root = tmp_path / "lifecycle"
    af.write_registry(
        root,
        af.transitions(af.SUBJECT, path, datetime(2026, 1, 1, tzinfo=UTC), step=timedelta(hours=1)),
        anchor=tmp_path / "lifecycle.anchor.jsonl",
    )
    return root


def _args(case: Case, reports: Path, registry: Path, **changes: str) -> list[str]:
    """The authority-mode arguments of ``case`` (``changes``: flag → value; "" drops a flag)."""
    base = case.cli_args(reports)
    explicit = {"--lifecycle", "--recent-manifest"}
    out: list[str] = []
    pairs = list(zip(base[::2], base[1::2], strict=True))
    values = {flag: value for flag, value in pairs if flag not in explicit}
    values.update(
        {
            "--authority-registry": str(registry),
            "--authority-head": "latest",
            "--dataset-id": "hlens.dataset.pit-selection@2.1.0." + "a" * 64,
            "--manifest-hash": "b" * 64,
            "--authority-as-of": "2026-03-02T00:00:00Z",
        }
    )
    values.update(changes)
    for flag, value in values.items():
        if value != "":
            out += [flag, value]
    return out


def _stand_in_environment(case: Case) -> AuthorityEnvironment:
    catalog = DatasetCatalog(
        adapter=object(),  # type: ignore[arg-type]
        storage=object(),  # type: ignore[arg-type]
        builder=object(),  # type: ignore[arg-type]
        evidence_verifier=object(),  # type: ignore[arg-type]
    )
    run = af.baseline_run(case.profile)
    return AuthorityEnvironment(
        catalog=catalog,
        execution=af.execution(run, ("BTC-USDT",)),
        baseline_run=run,
        baseline_manifest_hash="c" * 64,
    )


def _refused(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    reports: Path,
    *,
    code: int = 1,
    **kwargs: Any,
) -> str:
    assert degradation_cli.main(argv, **kwargs) == code
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing reported as a result
    assert sorted(reports.rglob("*.json")) == []  # no report written
    return captured.err


# ---- usage errors (exit 2) ----------------------------------------------------------------


def test_mixing_or_splitting_the_modes_is_a_usage_error(
    case: Case, reports: Path, tmp_path: Path
) -> None:
    registry = _registry(tmp_path)
    authority = _args(case, reports, registry)
    for argv in (
        authority + ["--lifecycle", str(case.lifecycle_path)],  # both modes
        _args(case, reports, registry, **{"--manifest-hash": ""}),  # part of the authority mode
        case.cli_args(reports) + ["--authority-anchor", str(tmp_path / "a")],
        case.cli_args(reports) + ["--authority-environment", FACTORY],
        _args(case, reports, registry, **{"--authority-evidence-verifier": FACTORY}),
    ):
        with pytest.raises(SystemExit) as usage:
            degradation_cli.main(argv)
        assert usage.value.code == 2


def test_an_embedded_environment_is_for_the_authority_mode_only(
    case: Case, reports: Path, tmp_path: Path
) -> None:
    environment = _stand_in_environment(case)
    with pytest.raises(SystemExit):
        degradation_cli.main(case.cli_args(reports), authority_environment=environment)
    argv = _args(case, reports, _registry(tmp_path), **{"--authority-environment": FACTORY})
    with pytest.raises(SystemExit):  # an embedded environment and a factory: never both
        degradation_cli.main(argv, authority_environment=environment)


# ---- input refusals (exit 1) --------------------------------------------------------------


def test_a_missing_or_shared_registry_is_refused_before_anything_opens(
    case: Case, reports: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _registry(tmp_path)
    empty = tmp_path / "empty_registry"
    empty.mkdir()
    for changes in (
        {"--authority-registry": str(tmp_path / "missing")},
        {"--authority-registry": str(empty)},  # no journal: nothing is ever created
        {"--authority-registry": str(reports)},  # shares the reports root
        {"--authority-anchor": str(tmp_path / "missing.anchor")},
        {"--authority-anchor": str(registry / JOURNAL_NAME)},  # inside the registry
        {"--authority-as-of": "2026-02-28T00:00:00Z"},  # before the window end
        {"--authority-as-of": "2026-03-02T00:00:00"},  # no UTC offset
    ):
        err = _refused(_args(case, reports, registry, **changes), capsys, reports)
        assert "refused at input" in err
    assert sorted(empty.iterdir()) == []


# ---- authority refusals after the pinned head (exit 1) -----------------------------------


def test_without_an_environment_the_mode_refuses_after_verifying_the_head(
    case: Case, reports: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    err = _refused(_args(case, reports, _registry(tmp_path)), capsys, reports)
    assert "refused at authority (AuthorityRefused: authority_environment_unavailable)" in err


@pytest.mark.parametrize("head", ["a" * 64, "not-a-hash", "LATEST"])
def test_a_head_not_on_the_chain_is_unknown(
    case: Case, reports: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], head: str
) -> None:
    argv = _args(case, reports, _registry(tmp_path), **{"--authority-head": head})
    err = _refused(argv, capsys, reports, authority_environment=_stand_in_environment(case))
    assert "AuthorityRefused: lifecycle_head_unknown" in err


def test_a_pinned_head_of_this_chain_is_accepted_until_the_next_authority(
    case: Case, reports: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _registry(tmp_path, af.PATH_TO_ACTIVE[:5])  # PAPER: not ACTIVE
    with LifecycleRegistry.open_snapshot(registry) as snapshot:
        head = snapshot.head.last_record_hash
    argv = _args(case, reports, registry, **{"--authority-head": head})
    err = _refused(argv, capsys, reports, authority_environment=_stand_in_environment(case))
    assert "AuthorityRefused: lifecycle_not_active" in err


def test_a_corrupted_registry_journal_is_unavailable(
    case: Case, reports: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _registry(tmp_path)
    with (registry / JOURNAL_NAME).open("a", encoding="utf-8") as journal:
        journal.write('{"seq": 99}\n')
    err = _refused(_args(case, reports, registry), capsys, reports)
    assert "AuthorityRefused: lifecycle_unavailable" in err


def test_an_anchor_that_disagrees_with_the_registry_is_unavailable(
    case: Case, reports: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _registry(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    af.write_registry(
        other / "lifecycle",
        af.transitions(af.SUBJECT, af.PATH_TO_ACTIVE[:3], datetime(2025, 1, 1, tzinfo=UTC)),
        anchor=other / "other.anchor.jsonl",
    )
    argv = _args(
        case, reports, registry, **{"--authority-anchor": str(other / "other.anchor.jsonl")}
    )
    err = _refused(argv, capsys, reports)
    assert "AuthorityRefused: lifecycle_unavailable" in err


# ---- the trusted factory ------------------------------------------------------------------


def test_a_trusted_factory_and_its_context_are_used_and_left(
    case: Case,
    reports: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(tmp_path, af.PATH_TO_ACTIVE[:5])  # PAPER: the resolver refuses
    monkeypatch.setattr(af, "FACTORY_ENVIRONMENT", [_stand_in_environment(case)])
    for factory in (FACTORY, CONTEXT):
        argv = _args(case, reports, registry, **{"--authority-environment": factory})
        err = _refused(argv, capsys, reports)
        assert "AuthorityRefused: lifecycle_not_active" in err  # the factory's env reached it
    assert af.FACTORY_ENVIRONMENT[-1] == "exited"


@pytest.mark.parametrize(
    "factory",
    [
        "not a factory",
        "tests.research.operations.no_such_module:factory",
        "tests.research.operations.authority_fixtures:no_such_callable",
        "tests.research.operations.authority_fixtures:METRIC",  # not callable
    ],
)
def test_a_factory_that_cannot_be_loaded_is_unavailable(
    case: Case, reports: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], factory: str
) -> None:
    argv = _args(case, reports, _registry(tmp_path), **{"--authority-environment": factory})
    assert "authority_environment_unavailable" in _refused(argv, capsys, reports)


def test_a_failing_or_wrong_factory_is_unavailable_without_its_details(
    case: Case,
    reports: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(tmp_path)
    err = _refused(
        _args(case, reports, registry, **{"--authority-environment": FAILING}), capsys, reports
    )
    assert "authority_environment_unavailable" in err and "secret" not in err
    monkeypatch.setattr(af, "FACTORY_ENVIRONMENT", ["not an environment"])
    err = _refused(
        _args(case, reports, registry, **{"--authority-environment": FACTORY}), capsys, reports
    )
    assert "authority_environment_unavailable" in err


def test_an_unloadable_evidence_verifier_is_unavailable(
    case: Case, reports: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _args(
        case,
        reports,
        _registry(tmp_path),
        **{
            "--authority-environment": FACTORY,
            "--authority-evidence-verifier": "tests.research.operations.nothing:here",
        },
    )
    assert "authority_environment_unavailable" in _refused(argv, capsys, reports)


def test_the_default_factory_names_what_it_lacks(
    case: Case,
    reports: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(tmp_path)
    err = _refused(
        _args(case, reports, registry, **{"--authority-environment": DEFAULT}), capsys, reports
    )
    assert "authority_environment_unavailable" in err
    assert "missing settings: --authority-evidence-verifier" in err
    # with the verifier argument, every missing setting is named (names, never values)
    monkeypatch.chdir(tmp_path)  # no .env here
    for name in list(os.environ):
        if name.startswith("HLENS_"):
            monkeypatch.delenv(name)
    argv = _args(
        case,
        reports,
        registry,
        **{"--authority-environment": DEFAULT, "--authority-evidence-verifier": FACTORY},
    )
    err = _refused(argv, capsys, reports)
    assert "authority_environment_unavailable" in err and "missing settings: HLENS_" in err

"""ADR-0105 §5: the Dataset-sourced operator's durable path over a real dataset (offline).

``research.loop.dataset_operator`` runs ``research.loop.operator``'s own pre-open checks and bounded
round execution over ``open_dataset_loop(..., operator_identity=...)``. No production Profile is
frozen, so no operator configuration can be loaded (``test_dataset_operator.py``); this module
drives the same shared functions directly with the compiled configuration a loaded file would
give: a new state is created as operator state v5 bound to the identity, the round runs with its
report, the read-only pre-open check then recognises the directory as this configuration's, and
another identity — or a v4 dataset directory — is refused.

Data and configuration: the Binance-format fixture and the TEST ONLY loop configuration of
``test_research_loop_real_data`` on the SQLite test catalog (``w`` fixture); only round 0 runs.
The identity is an arbitrary TEST ONLY hash. Nothing here is a research result.
"""

from __future__ import annotations

import gc
from pathlib import Path

import pytest

from research.loop.dataset_compose import open_dataset_loop
from research.loop.dataset_operator import _expected_fingerprint
from research.loop.dataset_operator_config import CompiledDatasetOperatorConfig
from research.loop.durable import OPERATOR_STATE_VERSION, LoopStateInconsistent
from research.loop.operator import (
    EXIT_OK,
    OperatorRefused,
    StopRequest,
    check_anchor_paths,
    check_state_dir,
    run_durable,
)
from research.loop.operator_config import OperatorPaths
from research.loop.state_export import read_durable_state
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.e2e import test_research_loop_real_data as base

IDENTITY = "ab" * 32
OTHER_IDENTITY = "cd" * 32


def _paths(root: Path) -> OperatorPaths:
    anchors = root / "anchors"
    anchors.mkdir(parents=True)
    return OperatorPaths(
        config_file=root / "operator.toml",
        state_dir=root / "state",
        state_anchor=anchors / "state.jsonl",
        bus_anchor=anchors / "bus.jsonl",
        reports_root=root / "reports",
        freeze_registry_dir=root / "freezes",
        freeze_registry_anchor=anchors / "freezes.jsonl",
    )


def test_the_dataset_operator_creates_and_recognises_its_v5_state(
    w: ds.World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base._ingest(w)
    config = base._config(base._build(w).rounds)
    paths = _paths(tmp_path)
    compiled = CompiledDatasetOperatorConfig(
        loop_config=config, operator_identity=IDENTITY, paths=paths
    )
    expected = _expected_fingerprint(compiled)
    assert expected["operator_identity"] == IDENTITY

    check_anchor_paths(paths)
    assert check_state_dir(paths, expected) is False  # new

    def open_loop(identity: str = IDENTITY) -> object:
        return open_dataset_loop(
            config,
            state_dir=paths.state_dir,
            catalog=base._catalog(w),
            anchor=paths.state_anchor,
            bus_anchor=paths.bus_anchor,
            operator_identity=identity,
        )

    code = run_durable(
        open_loop,  # type: ignore[arg-type]
        loop_id=config.loop_id,
        operator_identity=IDENTITY,
        paths=paths,
        existing=False,
        rounds=1,
        stop=StopRequest(),
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK, out
    assert "state=new" in out and "rounds_run=1" in out and f"exit_code={EXIT_OK}" in out
    assert len(list(paths.reports_root.rglob("*.json"))) == 1
    gc.collect()

    # the state is operator v5, bound to the identity; the pre-open check recognises it
    view = read_durable_state(paths.state_dir)
    assert view.state_version == OPERATOR_STATE_VERSION
    assert view.fingerprint["operator_identity"] == IDENTITY
    assert dict(view.fingerprint) == dict(expected)
    assert check_state_dir(paths, expected) is True

    # another identity is another configuration: refused read-only and at opening
    other = {**expected, "operator_identity": OTHER_IDENTITY}
    with pytest.raises(OperatorRefused, match="operator_identity"):
        check_state_dir(paths, other)
    with pytest.raises(LoopStateInconsistent):
        open_loop(OTHER_IDENTITY)
    gc.collect()
    with pytest.raises(ValueError, match="canonical lowercase SHA-256"):
        open_loop("AB" * 32)

    # the same identity reopens it; a stop requested before the first boundary runs no round
    stop = StopRequest()
    stop.signal_name = "SIGTERM"
    code = run_durable(
        open_loop,  # type: ignore[arg-type]
        loop_id=config.loop_id,
        operator_identity=IDENTITY,
        paths=paths,
        existing=True,
        rounds=1,
        stop=stop,
    )
    out = capsys.readouterr().out
    assert code == EXIT_OK, out
    assert "state=existing" in out and "rounds_recorded_before=1" in out
    assert "rounds_run=0" in out and "outcome=stopped_by_SIGTERM" in out


def test_a_v4_dataset_state_is_never_taken_over(w: ds.World, tmp_path: Path) -> None:
    base._ingest(w)
    config = base._config(base._build(w).rounds)
    paths = _paths(tmp_path)
    with open_dataset_loop(config, state_dir=paths.state_dir, catalog=base._catalog(w)):
        pass  # header only: an ordinary (v4) dataset state
    gc.collect()
    compiled = CompiledDatasetOperatorConfig(
        loop_config=config, operator_identity=IDENTITY, paths=paths
    )
    with pytest.raises(OperatorRefused, match="never takes over"):
        check_state_dir(paths, _expected_fingerprint(compiled))

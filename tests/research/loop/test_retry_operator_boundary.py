"""ADR-0083: the ADR-0074 v5 operator must fail closed on retry state v6."""

from pathlib import Path

import pytest

from research.loop.durable import LOOP_STATE_OPENED, MEMORY_FILE, RETRY_STATE_VERSION
from research.loop.operator import _check_state_dir, _StateFault
from research.loop.operator_config import OperatorPaths
from research.persistence import AppendOnlyJournal


def test_synthetic_operator_refuses_v6_retry_state_as_unknown(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    AppendOnlyJournal(state_dir / MEMORY_FILE).append(
        LOOP_STATE_OPENED,
        {"state_version": RETRY_STATE_VERSION, "fingerprint": {}},
    )
    paths = OperatorPaths(
        config_file=tmp_path / "operator.toml",
        state_dir=state_dir,
        state_anchor=tmp_path / "state.anchor",
        bus_anchor=tmp_path / "bus.anchor",
        reports_root=tmp_path / "reports",
        freeze_registry_dir=tmp_path / "freeze",
        freeze_registry_anchor=tmp_path / "freeze.anchor",
    )

    with pytest.raises(_StateFault, match="unknown state version 6"):
        _check_state_dir(paths, {})

    # This is a read-only preflight: refusal must not create operator files or mutate the v6 log.
    assert sorted(path.name for path in state_dir.iterdir()) == [MEMORY_FILE]

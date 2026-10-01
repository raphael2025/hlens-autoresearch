"""``infrastructure.state`` exports (ADR-0102): the lightweight runner / table / store surface."""

from __future__ import annotations

import subprocess
import sys

import infrastructure.state as state_package
from infrastructure.state.runner import run_state
from infrastructure.state.store import StateResultStore, StateStoreCorrupted


def test_package_exports_the_runner_table_and_store_names() -> None:
    assert set(state_package.__all__) == {
        "STATE_TABLE_SCHEMA",
        "StateResultStore",
        "StateRunnerError",
        "StateStoreCorrupted",
        "run_state",
        "state_inputs",
        "state_request",
        "state_table",
    }
    for name in state_package.__all__:
        assert hasattr(state_package, name)
    assert state_package.run_state is run_state
    assert state_package.StateResultStore is StateResultStore
    assert state_package.StateStoreCorrupted is StateStoreCorrupted


def test_importing_the_package_loads_neither_pyiceberg_nor_plugins_nor_research() -> None:
    code = (
        "import sys, infrastructure.state;"
        "bad = [m for m in ('pyiceberg', 'plugins', 'research') if m in sys.modules];"
        "print(','.join(bad))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""

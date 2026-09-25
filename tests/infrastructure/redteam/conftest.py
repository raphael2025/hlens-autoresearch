"""Fixtures for the G2 cross-stage red-team suite (one tiny SQLite world per test)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.infrastructure.dataset import dataset_support as ds


@pytest.fixture
def w(tmp_path: Path) -> Iterator[ds.World]:
    with ds.sqlite_world(tmp_path) as opened:
        yield opened

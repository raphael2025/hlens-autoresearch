"""Fixtures for the F2 universe and F3 Research Dataset tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from infrastructure.storage import LocalFileStorageAdapter
from tests.infrastructure.dataset import dataset_support as ds


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "postgres: needs the dedicated PostgreSQL test catalog (HLENS_TEST_CATALOG_URI)"
    )


@pytest.fixture
def w(tmp_path: Path) -> Iterator[ds.World]:
    with ds.sqlite_world(tmp_path) as opened:
        yield opened


@pytest.fixture
def evidence_store(tmp_path: Path) -> LocalFileStorageAdapter:
    """A real object store for v3 evidence trees (ADR-0077), outside any catalog."""
    return ds.evidence_storage(tmp_path)

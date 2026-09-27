"""Fixtures for the C2 catalog tests (see ``catalog_support``)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.infrastructure.catalog.catalog_support import (
    PostgresCatalogHarness,
    SqliteCatalogHarness,
    postgres_test_catalog_uri,
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "postgres: needs the dedicated PostgreSQL test catalog (HLENS_TEST_CATALOG_URI)"
    )


@pytest.fixture
def sqlite_harness(tmp_path: Path) -> Iterator[SqliteCatalogHarness]:
    harness = SqliteCatalogHarness(tmp_path)
    yield harness
    harness.cleanup()


@pytest.fixture
def pg_harness(tmp_path: Path) -> Iterator[PostgresCatalogHarness]:
    harness = PostgresCatalogHarness(tmp_path, uri=postgres_test_catalog_uri())
    yield harness
    harness.cleanup()

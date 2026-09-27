"""Fixtures for the F4 feature pipeline tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.infrastructure.revision import rest_store_support as ss


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "postgres: needs the dedicated PostgreSQL test catalog (HLENS_TEST_CATALOG_URI)"
    )


@pytest.fixture
def h(tmp_path: Path) -> Iterator[ss.RestHarness]:
    with ss.sqlite_harness(tmp_path) as opened:
        yield opened

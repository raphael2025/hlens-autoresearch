"""Fixtures for the D2 revision store tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.infrastructure.revision.revision_support import StoreHarness, store_harness


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "postgres: needs the dedicated PostgreSQL test catalog (HLENS_TEST_CATALOG_URI)"
    )


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[StoreHarness]:
    with store_harness(tmp_path) as opened:
        yield opened

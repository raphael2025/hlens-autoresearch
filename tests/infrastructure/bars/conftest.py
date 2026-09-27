"""Fixtures for the dataset-bars and manifest-pairing tests (SQLite + PostgreSQL)."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "postgres: needs the dedicated PostgreSQL test catalog (HLENS_TEST_CATALOG_URI)"
    )

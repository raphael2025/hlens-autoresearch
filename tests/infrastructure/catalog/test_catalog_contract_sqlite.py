"""B3 Catalog contract suite against the PyIceberg adapter on an injected SQLite catalog.

Fast unit coverage only: per ADR-0021 D-01 this is **not** PostgreSQL integration evidence.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.contract_suites.catalog import CatalogAdapterContract, CatalogSubject
from tests.infrastructure.catalog.catalog_support import SqliteCatalogHarness


class TestPyIcebergCatalogContractSqlite(CatalogAdapterContract):
    @pytest.fixture
    def catalog_subject(self, sqlite_harness: SqliteCatalogHarness) -> CatalogSubject[Any]:
        return sqlite_harness.subject()

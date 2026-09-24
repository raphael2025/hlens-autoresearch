"""B3 Catalog contract suite against the PyIceberg adapter on the PostgreSQL test catalog."""

from __future__ import annotations

from typing import Any

import pytest

from tests.contract_suites.catalog import CatalogAdapterContract, CatalogSubject
from tests.infrastructure.catalog.catalog_support import PostgresCatalogHarness

pytestmark = pytest.mark.postgres


class TestPyIcebergCatalogContractPostgres(CatalogAdapterContract):
    @pytest.fixture
    def catalog_subject(self, pg_harness: PostgresCatalogHarness) -> CatalogSubject[Any]:
        return pg_harness.subject()

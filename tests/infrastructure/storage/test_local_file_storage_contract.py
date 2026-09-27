"""B3 Storage contract suite against ``LocalFileStorageAdapter`` (Phase 1 C1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.storage import LocalFileStorageAdapter
from tests.contract_suites.storage import StorageAdapterContract, StorageSubject


def _subject(tmp_path: Path) -> StorageSubject:
    warehouse = tmp_path / "warehouse"
    staging = warehouse / "staging"
    warehouse.mkdir()
    staging.mkdir()

    def open_adapter() -> LocalFileStorageAdapter:
        return LocalFileStorageAdapter(warehouse.as_uri(), staging.as_uri())

    probe = open_adapter()
    return StorageSubject(open=open_adapter, forbidden_keys=probe.forbidden_object_keys)


class TestLocalFileStorageContract(StorageAdapterContract):
    @pytest.fixture
    def storage_subject(self, tmp_path: Path) -> StorageSubject:
        return _subject(tmp_path)

"""CollectorAdapter contract suite against BinanceSpotArchiveCollector (Phase 1 D0)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.contracts.collector import CollectionRequest, SourceBinding
from infrastructure.collector import ARCHIVE_SOURCE, BinanceSpotArchiveCollector
from tests.contract_suites.collector import CollectorAdapterContract, CollectorSubject
from tests.infrastructure.collector.conftest import (
    ARCHIVE_BASE,
    ArchiveFixture,
    make_collector,
    make_storage,
)


def _subject(tmp_path: Path) -> CollectorSubject:
    fixture = ArchiveFixture()
    # One published object + one SOURCE_ABSENT gap (checksum 404).
    fixture.put_zip(
        "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip",
        b"zip-day-1-payload",
    )
    storage = make_storage(tmp_path)

    def open_collector() -> BinanceSpotArchiveCollector:
        return make_collector(storage, fixture)

    request = CollectionRequest(
        request_id="d0-contract-agg-2024-01-01-02",
        source=ARCHIVE_SOURCE,
        data_type="agg_trades",
        symbols=("BTCUSDT",),
        coverage_start=datetime(2024, 1, 1, tzinfo=UTC),
        coverage_end=datetime(2024, 1, 3, tzinfo=UTC),
    )
    return CollectorSubject(storage=storage, open=open_collector, request=request)


class TestBinanceArchiveCollectorContract(CollectorAdapterContract):
    @pytest.fixture
    def collector_subject(self, tmp_path: Path) -> CollectorSubject:
        return _subject(tmp_path)


def test_contract_fixture_uses_real_local_storage_and_gap(tmp_path: Path) -> None:
    subject = _subject(tmp_path)
    result = subject.open().collect(subject.request)
    assert len(result.objects) == 1
    assert len(result.gaps) == 1
    assert result.gaps[0].reason.value == "source_absent"
    assert result.objects[0].source_uri.startswith(ARCHIVE_BASE)
    assert subject.storage.lookup(result.objects[0].ref.key) == result.objects[0].ref
    assert isinstance(subject.request.source, SourceBinding)

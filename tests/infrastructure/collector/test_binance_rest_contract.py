"""CollectorAdapter contract suite against BinanceSpotRestCollector (Phase 1 D3D, ADR-0027 §14).

The subject is fully offline: one mock venue, one real local ``file://`` storage, injected
clocks. Re-opening the collector models a restart, which is exactly what the replay check needs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.collector import BinanceSpotRestCollector
from infrastructure.storage import LocalFileStorageAdapter
from tests.contract_suites.collector import CollectorAdapterContract, CollectorSubject
from tests.infrastructure.collector.rest_support import (
    ORIGIN,
    SYMBOL,
    T0,
    RestVenue,
    agg_page,
    agg_request,
    make_collector,
    make_storage,
    queue_agg_chain,
)


def _subject(tmp_path: Path) -> CollectorSubject:
    venue = RestVenue()
    # One committed page (an object) plus the honest tail gap after a short page.
    queue_agg_chain(venue, SYMBOL, T0, [agg_page(4, first_id=500, first_ms=T0)])
    storage = make_storage(tmp_path)

    def open_collector() -> BinanceSpotRestCollector:
        return make_collector(storage, venue)

    return CollectorSubject(
        storage=storage,
        open=open_collector,
        request=agg_request(request_id="d3d-contract-agg"),
    )


class TestBinanceRestCollectorContract(CollectorAdapterContract):
    @pytest.fixture
    def collector_subject(self, tmp_path: Path) -> CollectorSubject:
        return _subject(tmp_path)


def test_the_contract_fixture_really_produces_an_object_a_gap_and_a_replay(
    tmp_path: Path,
) -> None:
    subject = _subject(tmp_path)
    assert isinstance(subject.storage, LocalFileStorageAdapter)
    first = subject.open().collect(subject.request)
    assert len(first.objects) == 1
    assert len(first.gaps) == 1
    assert first.gaps[0].reason.value == "source_absent"
    assert first.objects[0].source_uri.startswith(f"{ORIGIN}/api/v3/aggTrades?")
    assert first.objects[0].source_sha256 is None
    assert subject.storage.lookup(first.objects[0].ref.key) == first.objects[0].ref
    # Replaying after a "restart" costs nothing and returns the very same commitments.
    again = subject.open().collect(subject.request)
    assert again.objects == first.objects
    assert again.gaps == first.gaps

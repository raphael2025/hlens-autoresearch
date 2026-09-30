"""Bounded listing observation inputs preserve the proved Raw snapshot membership."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from infrastructure.canonical.listing_runs import listing_observations_run
from infrastructure.revision.exchange_info_store import ExchangeInfoRowVerifier
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.streaming.runs import RunLimits, iter_run
from tests.infrastructure.revision import exchange_info_support as xs
from tests.infrastructure.revision.exchange_info_support import T1, Harness


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    with xs.harness(tmp_path) as opened:
        yield opened


def test_listing_observations_run_keeps_snapshot_membership_and_orders_high_cardinality(
    h: Harness,
) -> None:
    for index in range(9):
        at = T1 + timedelta(seconds=index)
        h.observe(
            f"listing-{index}",
            {"BTCUSDT": f"UNKNOWN_{index}", "ETHUSDT": "TRADING"},
            at,
        )
    scratch = LocalFileStorageAdapter(
        (h.tmp_path / "scratch-warehouse").as_uri(),
        (h.tmp_path / "scratch-stage").as_uri(),
    )
    verifier = ExchangeInfoRowVerifier(h.adapter, h.storage, xs.ORIGIN)
    try:
        raw_root = verifier.verify_table_bounded(
            h.head(xs.EXCHANGE_INFO.table),
            scratch_storage=scratch,
            capacity=2,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=4, leaf_max_bytes=32768, fanout=2),
            max_record_bytes=16384,
        )
        assert raw_root is not None and raw_root.record_count == 9
        root = listing_observations_run(
            raw_root,
            storage=scratch,
            capacity=2,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=4, leaf_max_bytes=32768, fanout=2),
            max_record_bytes=4096,
        )
        assert root is not None and root.record_count == 18
        with iter_run(scratch, root) as reader:
            observations = list(reader)
        keys = [
            (row["venue_symbol"], row["retrieved_at"], row["snapshot_revision_id"])
            for row in observations
        ]
        assert keys == sorted(keys)
        assert {row["snapshot_ordinal"] for row in observations} == set(range(9))
        assert all(row["snapshot_id"] for row in observations)
    finally:
        verifier.close()
        scratch.close()


def test_listing_observations_run_preserves_missing_symbol_as_unresolved(h: Harness) -> None:
    h.observe("missing-eth", {"BTCUSDT": "TRADING", "ETHUSDT": None}, T1)
    scratch = LocalFileStorageAdapter(
        (h.tmp_path / "scratch-warehouse").as_uri(),
        (h.tmp_path / "scratch-stage").as_uri(),
    )
    verifier = ExchangeInfoRowVerifier(h.adapter, h.storage, xs.ORIGIN)
    try:
        raw_root = verifier.verify_table_bounded(
            h.head(xs.EXCHANGE_INFO.table),
            scratch_storage=scratch,
            capacity=1,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=2, leaf_max_bytes=32768, fanout=2),
            max_record_bytes=16384,
        )
        assert raw_root is not None
        root = listing_observations_run(
            raw_root,
            storage=scratch,
            capacity=1,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=2, leaf_max_bytes=32768, fanout=2),
            max_record_bytes=4096,
        )
        assert root is not None
        with iter_run(scratch, root) as reader:
            by_symbol = {row["venue_symbol"]: row for row in reader}
        assert by_symbol["ETHUSDT"]["status"] is None
        assert by_symbol["ETHUSDT"]["snapshot_ordinal"] == 0
    finally:
        verifier.close()
        scratch.close()

"""Persistent prefix indexes preserve exact Raw snapshot history with bounded runs."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from infrastructure.canonical.listing_prefix_index import (
    ListingPrefixIndex,
    build_listing_prefix_index,
)
from infrastructure.revision.exchange_info_store import ExchangeInfoRowVerifier
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.streaming.runs import RunLimits, iter_run
from tests.infrastructure.revision import exchange_info_support as xs
from tests.infrastructure.revision.exchange_info_support import T1, Harness


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    with xs.harness(tmp_path) as opened:
        yield opened


def test_prefix_index_sorts_raw_ordinals_and_spills_each_root(h: Harness) -> None:
    for snapshot_number in range(7):
        h.observe(
            f"prefix-{snapshot_number}",
            {"BTCUSDT": "TRADING", "ETHUSDT": "HALT"},
            T1.replace(microsecond=0) + timedelta(seconds=snapshot_number),
            server_time=snapshot_number,
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
            limits=RunLimits(leaf_max_records=3, leaf_max_bytes=32768, fanout=2),
            max_record_bytes=16384,
        )
        assert raw_root is not None
        index, root_refs = build_listing_prefix_index(
            raw_root,
            storage=scratch,
            raw_sort_capacity=2,
            raw_sort_merge_fanout=2,
            raw_sort_limits=RunLimits(leaf_max_records=3, leaf_max_bytes=32768, fanout=2),
            raw_max_record_bytes=16384,
            root_refs_capacity=2,
            root_refs_merge_fanout=2,
            root_refs_limits=RunLimits(leaf_max_records=3, leaf_max_bytes=32768, fanout=2),
            leaf_max_records=2,
            fanout=3,
            max_node_bytes=10000,
            max_record_bytes=4096,
        )
        with iter_run(scratch, root_refs) as reader:
            prefixes = list(reader)
        assert [item["snapshot_ordinal"] for item in prefixes] == list(range(7))
        assert index.stats.raw_record_count == 7
        assert index.stats.root_ref_count == 7
        assert index.stats.observation_count == 14
        assert index.stats.node_write_count > 7
        assert index.stats.node_write_bytes > 0
        for ordinal, prefix in enumerate(prefixes):
            ref = None if prefix["root"] is None else ListingPrefixIndex._parse_ref(prefix["root"])
            assert len(list(index.iter_rows(ref))) == 2 * (ordinal + 1)
        assert len(list(index.iter_rows())) == 14
    finally:
        verifier.close()
        scratch.close()


def test_prefix_index_uses_byte_valid_leaf_splits_for_uneven_rows(tmp_path: Path) -> None:
    storage = LocalFileStorageAdapter(
        (tmp_path / "index-warehouse").as_uri(),
        (tmp_path / "index-stage").as_uri(),
    )
    index = ListingPrefixIndex(
        storage,
        leaf_max_records=2,
        fanout=3,
        max_node_bytes=3312,
        max_record_bytes=1400,
    )
    instant = datetime(2026, 9, 1, tzinfo=UTC)
    base = {
        "snapshot_id": "snapshot",
        "snapshot_ordinal": 0,
        "requested_at": instant,
        "retrieved_at": instant,
        "raw_knowledge_time": instant,
        "status": "TRADING",
        "base_asset": "A",
        "quote_asset": "B",
    }
    try:
        assert list(index.iter_rows(None)) == []
        index.insert({**base, "venue_symbol": "A", "snapshot_revision_id": "small"})
        index.insert(
            {
                **base,
                "venue_symbol": "B" * 200,
                "snapshot_revision_id": "x" * 200,
                "status": "S" * 650,
            }
        )
        index.insert(
            {
                **base,
                "venue_symbol": "B" * 200,
                "snapshot_revision_id": "y" * 200,
                "status": "S" * 650,
            }
        )
        assert [row["snapshot_revision_id"] for row in index.iter_rows()] == [
            "small",
            "x" * 200,
            "y" * 200,
        ]
    finally:
        storage.close()


def test_prefix_index_splits_internal_nodes_with_variable_key_sizes(tmp_path: Path) -> None:
    storage = LocalFileStorageAdapter(
        (tmp_path / "internal-warehouse").as_uri(),
        (tmp_path / "internal-stage").as_uri(),
    )
    index = ListingPrefixIndex(
        storage,
        leaf_max_records=1,
        fanout=3,
        max_node_bytes=3000,
        max_record_bytes=1200,
    )
    instant = datetime(2026, 9, 1, tzinfo=UTC)
    try:
        for number, symbol in enumerate(("A", "B" * 250, "C", "D" * 250, "E", "F" * 250, "G")):
            index.insert(
                {
                    "snapshot_id": f"snap-{number}",
                    "snapshot_ordinal": number,
                    "venue_symbol": symbol,
                    "snapshot_revision_id": f"revision-{number}-"
                    + "r" * (20 if len(symbol) == 1 else 250),
                    "requested_at": instant,
                    "retrieved_at": instant,
                    "raw_knowledge_time": instant,
                    "status": "TRADING",
                    "base_asset": "A",
                    "quote_asset": "B",
                }
            )
        assert index.stats.max_tree_depth >= 2
        assert len(list(index.iter_rows())) == 7
    finally:
        storage.close()

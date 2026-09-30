"""Persistent prefix indexes preserve exact Raw snapshot history with bounded runs."""

from __future__ import annotations

import tracemalloc
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from infrastructure.canonical.listing_prefix_index import (
    ListingPrefixIndex,
    ListingPrefixIndexError,
    build_listing_prefix_index,
)
from infrastructure.revision.exchange_info_store import ExchangeInfoRowVerifier
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.streaming.runs import RunLimits, iter_run, write_sorted_run
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
            raw_max_run_object_bytes=32768,
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


def test_prefix_leaf_split_scratch_stays_linear_at_large_fan_in(tmp_path: Path) -> None:
    storage = LocalFileStorageAdapter(
        (tmp_path / "stress-warehouse").as_uri(), (tmp_path / "stress-stage").as_uri()
    )
    index = ListingPrefixIndex(
        storage,
        leaf_max_records=1200,
        fanout=3,
        max_node_bytes=308_000,
        max_record_bytes=200,
    )
    instant = datetime(2026, 9, 1, tzinfo=UTC)
    entries: list[Mapping[str, object]] = [
        index._entry(
            ("BTCUSDT", instant, f"revision-{number:04d}"),
            {"payload": "x" * 80},
        )
        for number in range(1201)
    ]
    try:
        tracemalloc.start()
        left, right = index._split_leaf_if_needed(entries)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert left.size <= index._max_node_bytes
        assert right is not None and right.size <= index._max_node_bytes
        assert peak < 5 * index._max_node_bytes
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        storage.close()


def test_prefix_builder_rejects_ordinal_gaps_and_duplicate_snapshot_ids(
    tmp_path: Path,
) -> None:
    for name, entries, expected in (
        ("gap", [(0, "snap-0"), (2, "snap-2")], "ordinals are missing"),
        ("duplicate", [(0, "same"), (1, "same")], "repeats a snapshot id"),
    ):
        storage = LocalFileStorageAdapter(
            (tmp_path / f"{name}-warehouse").as_uri(),
            (tmp_path / f"{name}-stage").as_uri(),
        )
        raw_run = write_sorted_run(
            storage,
            [
                {
                    "snapshot_id": snapshot_id,
                    "snapshot_ordinal": ordinal,
                    "row": {
                        "revision_id": f"raw-{ordinal}",
                        "requested_symbols": [],
                        "symbols": [],
                        "requested_at": datetime(2026, 9, 1, tzinfo=UTC),
                        "retrieved_at": datetime(2026, 9, 1, tzinfo=UTC),
                        "knowledge_time": datetime(2026, 9, 1, tzinfo=UTC),
                    },
                }
                for ordinal, snapshot_id in entries
            ],
            RunLimits(leaf_max_records=2, leaf_max_bytes=8192, fanout=2),
        )
        try:
            with pytest.raises(ListingPrefixIndexError, match=expected):
                build_listing_prefix_index(
                    raw_run,
                    storage=storage,
                    raw_sort_capacity=1,
                    raw_sort_merge_fanout=2,
                    raw_sort_limits=RunLimits(leaf_max_records=2, leaf_max_bytes=8192, fanout=2),
                    raw_max_record_bytes=4096,
                    raw_max_run_object_bytes=8192,
                    root_refs_capacity=1,
                    root_refs_merge_fanout=2,
                    root_refs_limits=RunLimits(leaf_max_records=2, leaf_max_bytes=8192, fanout=2),
                    leaf_max_records=2,
                    fanout=2,
                    max_node_bytes=8192,
                    max_record_bytes=1024,
                )
        finally:
            storage.close()


def test_prefix_index_rejects_tampered_or_missing_nodes_and_closes_early_reader(
    tmp_path: Path,
) -> None:
    storage = LocalFileStorageAdapter(
        (tmp_path / "tamper-warehouse").as_uri(), (tmp_path / "tamper-stage").as_uri()
    )
    index = ListingPrefixIndex(
        storage,
        leaf_max_records=4,
        fanout=3,
        max_node_bytes=8192,
        max_record_bytes=1024,
    )
    instant = datetime(2026, 9, 1, tzinfo=UTC)
    try:
        revisions = (
            ("tie-b", instant),
            ("backdated", instant - timedelta(days=1)),
            ("tie-a", instant),
        )
        for revision, at in revisions:
            index.insert(
                {
                    "snapshot_id": revision,
                    "snapshot_ordinal": 0,
                    "venue_symbol": "BTCUSDT",
                    "snapshot_revision_id": revision,
                    "requested_at": at - timedelta(hours=1),
                    "retrieved_at": at,
                    "raw_knowledge_time": at,
                    "status": None,
                    "base_asset": None,
                    "quote_asset": None,
                }
            )
        reader = index.iter_rows()
        first = next(reader)
        assert first["snapshot_revision_id"] == "backdated"
        reader.close()
        ordered = list(index.iter_rows())
        assert [row["snapshot_revision_id"] for row in ordered] == [
            "backdated",
            "tie-a",
            "tie-b",
        ]
        assert len(ordered) == 3
        assert all(row["status"] is None for row in index.iter_rows())
        root = index.root
        assert root is not None
        tampered = replace(root, sha256="0" * 64)
        with pytest.raises(ListingPrefixIndexError, match="malformed or out of bounds"):
            list(index.iter_rows(tampered))
        absent = replace(
            root, key=f"quality/listing-prefix-index/v1/{'f' * 64}.json", sha256="f" * 64
        )
        with pytest.raises(ListingPrefixIndexError, match="missing or has a mismatched ref"):
            list(index.iter_rows(absent))
    finally:
        storage.close()

"""Bounded joins map each persisted listing batch to its exact Raw prefix."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from infrastructure.canonical.listing_bounded_verify import verify_listing_history_bounded
from infrastructure.canonical.listing_history_runs import listing_history_prefix_run
from infrastructure.canonical.listing_rules import parse_batch_id
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.revision.row_integrity import history_from
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.streaming.runs import RunLimits, RunRef, RunSetBuilder, iter_run
from tests.infrastructure.revision import exchange_info_support as xs
from tests.infrastructure.revision.exchange_info_support import LISTINGS, T1, Harness


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    with xs.harness(tmp_path) as opened:
        yield opened


def _prefixes(h: Harness, storage: LocalFileStorageAdapter, *, omit: str | None = None) -> RunRef:
    builder = RunSetBuilder(
        storage,
        key=lambda row: row["snapshot_ordinal"],
        capacity=2,
        merge_fanout=2,
        limits=RunLimits(leaf_max_records=4, leaf_max_bytes=32768, fanout=2),
    )
    with builder:
        snapshots = list(
            history_from(h.adapter, xs.EXCHANGE_INFO.table, h.head(xs.EXCHANGE_INFO.table))
        )
        for snapshot in reversed(snapshots):
            if snapshot.snapshot_id != omit:
                builder.add(
                    {
                        "snapshot_ordinal": snapshot.total_rows - 1,
                        "snapshot_id": snapshot.snapshot_id,
                        "root": None,
                        "observation_count": snapshot.total_rows,
                    }
                )
        ref = builder.finish()
    assert ref is not None
    return ref


def _scratch(h: Harness) -> LocalFileStorageAdapter:
    return LocalFileStorageAdapter(
        (h.tmp_path / "listing-join-warehouse").as_uri(),
        (h.tmp_path / "listing-join-stage").as_uri(),
    )


def test_listing_history_join_is_ordinal_ordered_and_names_exact_raw_prefixes(
    h: Harness,
) -> None:
    for index, status in enumerate(("TRADING", "HALT", "TRADING")):
        h.observe(f"listing-history-{index}", {"BTCUSDT": status}, T1.replace(microsecond=index))
        deriver = h.deriver()
        try:
            deriver.derive()
        finally:
            deriver.close()

    scratch = _scratch(h)
    try:
        roots = _prefixes(h, scratch)
        joined = listing_history_prefix_run(
            h.adapter,
            h.head(LISTINGS.table),
            roots,
            storage=scratch,
            capacity=2,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=4, leaf_max_bytes=32768, fanout=2),
            max_record_bytes=16384,
            max_run_object_bytes=32768,
        )
        assert joined is not None
        with iter_run(scratch, joined) as reader:
            rows = list(reader)
        assert [row["snapshot_ordinal"] for row in rows] == sorted(
            row["snapshot_ordinal"] for row in rows
        )
        assert len(rows) == len(
            list(history_from(h.adapter, LISTINGS.table, h.head(LISTINGS.table)))
        )
        assert len(rows) >= 2
        assert [parse_batch_id(row["snapshot"]["batch_id"]) for row in rows] == [
            row["raw_snapshot_id"] for row in rows
        ]
        assert [row["observation_count"] for row in rows] == [
            row["snapshot_ordinal"] + 1 for row in rows
        ]
    finally:
        scratch.close()


def test_listing_history_join_rejects_missing_raw_snapshot_prefix(h: Harness) -> None:
    for index, status in enumerate(("TRADING", "HALT")):
        h.observe(
            f"listing-history-missing-{index}", {"BTCUSDT": status}, T1.replace(microsecond=index)
        )
        deriver = h.deriver()
        try:
            deriver.derive()
        finally:
            deriver.close()
    listing_history = list(history_from(h.adapter, LISTINGS.table, h.head(LISTINGS.table)))
    named = parse_batch_id(listing_history[0].batch_id)
    assert named is not None
    raw_history = list(
        history_from(h.adapter, xs.EXCHANGE_INFO.table, h.head(xs.EXCHANGE_INFO.table))
    )
    omitted = next(
        snapshot.snapshot_id for snapshot in raw_history if snapshot.snapshot_id == named
    )

    scratch = _scratch(h)
    try:
        with pytest.raises(CatalogIntegrityError, match="absent from its pinned history"):
            listing_history_prefix_run(
                h.adapter,
                h.head(LISTINGS.table),
                _prefixes(h, scratch, omit=omitted),
                storage=scratch,
                capacity=2,
                merge_fanout=2,
                limits=RunLimits(leaf_max_records=4, leaf_max_bytes=32768, fanout=2),
                max_record_bytes=16384,
                max_run_object_bytes=32768,
            )
    finally:
        scratch.close()


def test_deriver_prepares_bounded_replay_inputs_at_pinned_heads(h: Harness) -> None:
    for index, status in enumerate(("TRADING", "HALT", "TRADING")):
        h.observe(
            f"listing-inputs-{index}",
            {"BTCUSDT": status},
            T1.replace(microsecond=index),
        )
        deriver = h.deriver()
        try:
            deriver.derive()
        finally:
            deriver.close()
    scratch = _scratch(h)
    deriver = h.deriver()
    try:
        inputs = deriver.bounded_replay_inputs(
            scratch_storage=scratch,
            capacity=2,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=4, leaf_max_bytes=32768, fanout=2),
            max_record_bytes=16384,
            max_run_object_bytes=32768,
            prefix_leaf_max_records=3,
            prefix_fanout=3,
            prefix_max_node_bytes=16384,
            prefix_max_record_bytes=4096,
        )
        assert inputs.raw_snapshot_id == h.head(xs.EXCHANGE_INFO.table)
        assert inputs.listing_snapshot_id == h.head(LISTINGS.table)
        raw_count = len(
            list(
                history_from(
                    h.adapter,
                    xs.EXCHANGE_INFO.table,
                    h.head(xs.EXCHANGE_INFO.table),
                )
            )
        )
        assert inputs.raw_rows is not None and inputs.raw_rows.record_count == raw_count
        assert inputs.observations is not None and inputs.observations.record_count >= raw_count
        assert inputs.prefix_roots is not None and inputs.prefix_roots.record_count == raw_count
        assert inputs.listing_batches is not None and inputs.listing_batches.record_count >= 2
        assert inputs.prefix_index is not None
        with iter_run(scratch, inputs.listing_batches) as batches:
            last = None
            for batch in batches:
                last = batch
            assert last is not None
            prefix = list(inputs.prefix_index.iter_rows_for_root_document(last["root"]))
        assert sorted({row["snapshot_ordinal"] for row in prefix}) == list(range(raw_count))
    finally:
        deriver.close()
        scratch.close()


def test_bounded_deriver_replays_rows_and_c3_fingerprints_exactly(h: Harness) -> None:
    for index, status in enumerate(("TRADING", "HALT", "TRADING")):
        h.observe(
            f"listing-proof-{index}",
            {"BTCUSDT": status},
            T1.replace(microsecond=index),
        )
        deriver = h.deriver()
        try:
            deriver.derive()
        finally:
            deriver.close()
    scratch = _scratch(h)
    seen_findings: list[tuple[str, tuple[str, ...]]] = []

    def consume_finding(
        code: str,
        _symbol: str,
        _instant: object,
        revision_ids: Iterable[str],
        _detail: Callable[[int], str],
    ) -> None:
        seen_findings.append((code, tuple(revision_ids)))

    deriver = h.deriver()
    try:
        proof = deriver.verify_bounded(
            scratch_storage=scratch,
            capacity=2,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=4, leaf_max_bytes=32768, fanout=2),
            max_record_bytes=16384,
            max_run_object_bytes=32768,
            prefix_leaf_max_records=3,
            prefix_fanout=3,
            prefix_max_node_bytes=16384,
            prefix_max_record_bytes=4096,
            row_chunk_capacity=2,
            max_hash_chunk_bytes=64,
            finding_sink=consume_finding,
        )
        assert proof.committed_rows is not None
        with iter_run(scratch, proof.committed_rows) as reader:
            rows = list(reader)
        expected = sorted(h.rows(LISTINGS.table), key=lambda row: row["revision_id"])
        assert rows == expected
        assert proof.committed_row_count == len(expected)
        assert proof.diverged_count == 0
        assert proof.diverged_revision_ids is None
        assert proof.revision_index_node_write_count >= len(expected)
        assert proof.revision_index_node_write_bytes > 0
    finally:
        deriver.close()
        scratch.close()


def test_bounded_replay_rejects_a_tampered_persisted_batch_fingerprint(h: Harness) -> None:
    for index, status in enumerate(("TRADING", "HALT")):
        h.observe(
            f"listing-fingerprint-{index}",
            {"BTCUSDT": status},
            T1.replace(microsecond=index),
        )
        deriver = h.deriver()
        try:
            deriver.derive()
        finally:
            deriver.close()
    scratch = _scratch(h)
    deriver = h.deriver()
    try:
        inputs = deriver.bounded_replay_inputs(
            scratch_storage=scratch,
            capacity=2,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=4, leaf_max_bytes=32768, fanout=2),
            max_record_bytes=16384,
            max_run_object_bytes=32768,
            prefix_leaf_max_records=3,
            prefix_fanout=3,
            prefix_max_node_bytes=16384,
            prefix_max_record_bytes=4096,
        )
        assert inputs.listing_batches is not None
        corrupt_builder = RunSetBuilder(
            scratch,
            key=lambda row: row["snapshot_ordinal"],
            capacity=2,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=4, leaf_max_bytes=32768, fanout=2),
        )
        with corrupt_builder, iter_run(scratch, inputs.listing_batches) as batches:
            for batch in batches:
                corrupt = dict(batch)
                snapshot = dict(corrupt["snapshot"])
                snapshot["batch_fingerprint"] = "0" * 64
                corrupt["snapshot"] = snapshot
                corrupt_builder.add(corrupt)
            corrupt_ref = corrupt_builder.finish()
        assert corrupt_ref is not None
        corrupted = replace(inputs, listing_batches=corrupt_ref)
        with pytest.raises(CatalogIntegrityError, match="different fingerprint"):
            verify_listing_history_bounded(
                h.adapter,
                corrupted,
                evidence_storage=h.storage,
                scratch_storage=scratch,
                capacity=2,
                merge_fanout=2,
                limits=RunLimits(leaf_max_records=4, leaf_max_bytes=32768, fanout=2),
                max_record_bytes=16384,
                max_run_object_bytes=32768,
                row_chunk_capacity=2,
                max_hash_chunk_bytes=64,
                prefix_leaf_max_records=3,
                prefix_fanout=3,
                prefix_max_node_bytes=16384,
                prefix_max_record_bytes=4096,
                finding_sink=lambda *_args: None,
            )
    finally:
        deriver.close()
        scratch.close()

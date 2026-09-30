"""Bounded joins map each persisted listing batch to its exact Raw prefix."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pyarrow as pa  # type: ignore[import-untyped]
import pytest

from core.domain.base import canonical_json
from infrastructure.canonical.listing_bounded_verify import (
    _preflight_arrow_jsonl_row,
    _scan_current_rows,
    verify_listing_history_bounded,
)
from infrastructure.canonical.listing_history_runs import listing_history_prefix_run
from infrastructure.canonical.listing_rules import parse_batch_id
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import CANONICAL_INSTRUMENT_LISTINGS
from infrastructure.revision.exchange_info_store import ExchangeInfoRowVerifier
from infrastructure.revision.row_integrity import history_from
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.streaming.runs import RunLimits, RunRef, RunSetBuilder, _encode_row, iter_run
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


def test_arrow_row_preflight_matches_existing_jsonl_byte_cap(h: Harness) -> None:
    h.observe("listing-preflight", {"BTCUSDT": "TRADING"}, T1)
    deriver = h.deriver()
    try:
        deriver.derive()
    finally:
        deriver.close()
    [stored] = h.rows(LISTINGS.table)
    row = dict(stored)
    row["symbol"] = 'BTC"\\\n☃'
    row["availability_evidence"] = ["é\\\n", "\u0001"]
    row["precedence_evidence"] = [
        {
            "superseded_revision_id": "previous",
            "policy_id": "policy",
            "policy_version": "1.0.0",
            "policy_hash": "hash",
            "evidence": ["é\\\n", "\u0001"],
            "knowledge_time": T1,
        }
    ]
    schema = CANONICAL_INSTRUMENT_LISTINGS.arrow_schema
    batch = pa.RecordBatch.from_pylist([row], schema=schema)
    size = _preflight_arrow_jsonl_row(batch, 0, 1_000_000)
    [round_tripped] = batch.to_pylist()
    assert size == len(canonical_json(_encode_row(round_tripped)).encode("utf-8")) + 1
    ExchangeInfoRowVerifier._check_bounded_row(round_tripped, size)
    with pytest.raises(CatalogIntegrityError, match="max_record_bytes"):
        _preflight_arrow_jsonl_row(batch, 0, size - 1)


def test_oversized_arrow_row_is_rejected_before_to_pylist(h: Harness) -> None:
    schema = CANONICAL_INSTRUMENT_LISTINGS.arrow_schema
    arrays = [
        pa.array(
            ["x" * 100_000] if field.name == "observation_key" else [None],
            type=field.type,
        )
        for field in schema
    ]
    batch = pa.RecordBatch.from_arrays(arrays, schema=schema)
    conversions = {"count": 0}

    class SliceSpy:
        def __init__(self, sliced: pa.RecordBatch) -> None:
            self._sliced = sliced

        def to_pylist(self) -> list[dict[str, Any]]:
            conversions["count"] += 1
            return cast(list[dict[str, Any]], self._sliced.to_pylist())

    class BatchSpy:
        schema = batch.schema
        num_rows = batch.num_rows

        def column(self, index: int) -> pa.Array:
            return batch.column(index)

        def slice(self, offset: int, length: int) -> SliceSpy:
            return SliceSpy(batch.slice(offset, length))

    class Reader:
        closed = False

        def __iter__(self) -> Iterator[BatchSpy]:
            yield BatchSpy()

        def close(self) -> None:
            self.closed = True

    reader = Reader()

    class Catalog:
        def scan_column_batches(
            self, _table: str, *, columns: tuple[str, ...], snapshot_id: str
        ) -> Reader:
            assert columns
            assert snapshot_id == "pinned"
            return reader

    scratch = LocalFileStorageAdapter(
        (h.tmp_path / "preflight-warehouse").as_uri(),
        (h.tmp_path / "preflight-stage").as_uri(),
    )
    try:
        with pytest.raises(CatalogIntegrityError, match="max_record_bytes=256"):
            _scan_current_rows(
                Catalog(),  # type: ignore[arg-type]
                "pinned",
                scratch,
                capacity=2,
                merge_fanout=2,
                limits=RunLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2),
                max_record_bytes=256,
                max_run_object_bytes=4096,
            )
        assert conversions["count"] == 0
        assert reader.closed
    finally:
        scratch.close()


def test_recomputed_c3_fingerprint_does_not_hide_changed_listing_content(h: Harness) -> None:
    h.observe("listing-replay-tamper", {"BTCUSDT": "TRADING"}, T1)
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
        [changed_row] = h.rows(LISTINGS.table)
        changed_row = dict(changed_row)
        changed_row["status_reason"] += " altered"
        schema = CANONICAL_INSTRUMENT_LISTINGS.arrow_schema
        changed_batch = pa.Table.from_pylist([changed_row], schema=schema)
        changed_fingerprint = CANONICAL_INSTRUMENT_LISTINGS.fingerprint_rule.fingerprint(
            changed_batch
        )
        changed_batches = RunSetBuilder(
            scratch,
            key=lambda row: row["snapshot_ordinal"],
            capacity=2,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=4, leaf_max_bytes=32768, fanout=2),
        )
        with changed_batches, iter_run(scratch, inputs.listing_batches) as batches:
            for batch in batches:
                changed = dict(batch)
                snapshot = dict(changed["snapshot"])
                snapshot["batch_fingerprint"] = changed_fingerprint
                changed["snapshot"] = snapshot
                changed_batches.add(changed)
            changed_ref = changed_batches.finish()
        assert changed_ref is not None
        changed_inputs = replace(inputs, listing_batches=changed_ref)

        class ChangedCatalog:
            def __getattr__(self, name: str) -> Any:
                return getattr(h.adapter, name)

            def scan_column_batches(
                self,
                table: str,
                *,
                columns: tuple[str, ...],
                snapshot_id: str,
            ) -> Iterator[pa.RecordBatch]:
                source = h.adapter.scan_column_batches(
                    table, columns=columns, snapshot_id=snapshot_id
                )
                try:
                    for batch in source:
                        rows = batch.to_pylist()
                        rows[0]["status_reason"] += " altered"
                        yield pa.RecordBatch.from_pylist(rows, schema=batch.schema)
                finally:
                    close = getattr(source, "close", None)
                    if callable(close):
                        close()

        with pytest.raises(CatalogIntegrityError, match="differs from its replay"):
            verify_listing_history_bounded(
                ChangedCatalog(),  # type: ignore[arg-type]
                changed_inputs,
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

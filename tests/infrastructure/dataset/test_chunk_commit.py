"""``IcebergChunkWriter`` (B3, ADR-0077 §4 / §4.4): batch commit and checkpoint replay.

Exercises ``infrastructure.dataset.chunks.IcebergChunkWriter`` directly against the real
sqlite-backed Iceberg catalog the ``w`` fixture opens (``research.dataset_selection_chunks`` is
created empty by ``ensure_phase1_tables``, as for every other Phase 1 table) — no in-memory
stand-in. Rows are hand-built to match the frozen ``DATASET_SELECTION_CHUNKS`` schema exactly (the
same ten fields ``infrastructure.dataset.builder._EvidenceBuildSink`` emits); nothing here goes
through the v3 generator (B1/B-BUILD's own tests already cover that against ``FakeChunkWriter``).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from pyiceberg.expressions import EqualTo

from core.contracts.catalog import CommitRequest
from core.contracts.universe import dataset_chunk_batch_id
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    DATA_QUALITY_REPORTS,
    DATASET_SELECTION_CHUNKS,
    DATASET_SELECTIONS,
)
from infrastructure.dataset.builder import ChunkCommitted, DatasetSpecError
from infrastructure.dataset.chunks import IcebergChunkWriter
from tests.infrastructure.dataset import dataset_support as ds

_CANONICAL = "canonical.trades"
_EVENT = datetime(2024, 1, 1, tzinfo=UTC)
_SNAPSHOT_ID_RE = re.compile(r"^[0-9]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _row(
    selection_id: str, chunk_index: int, row_ordinal: int, *, revision: str | None = None
) -> dict[str, Any]:
    return {
        "selection_id": selection_id,
        "canonical_table": _CANONICAL,
        "symbol": "BTC-USDT",
        "observation_key": f"key-{row_ordinal}",
        "revision_id": revision or f"rev-{row_ordinal}",
        "event_time": _EVENT + timedelta(minutes=row_ordinal),
        "effective_from": None,
        "effective_until": None,
        "chunk_index": chunk_index,
        "row_ordinal": row_ordinal,
    }


def _writer(world: ds.World) -> IcebergChunkWriter:
    return IcebergChunkWriter(world.h.adapter, DATASET_SELECTION_CHUNKS)


def _raw_commit(
    world: ds.World, selection_id: str, *, chunk_index: int, rows: Sequence[dict[str, Any]]
) -> None:
    """Commit a chunk batch straight through the catalog, bypassing ``IcebergChunkWriter``.

    Simulates another writer (or corruption) landing a chunk this test's writer never committed —
    the only way a hole or an extra chunk can appear, since ``IcebergChunkWriter`` itself never
    calls ``commit_chunk`` out of order.
    """
    table = DATASET_SELECTION_CHUNKS
    batch = pa.Table.from_pylist(list(rows), schema=table.arrow_schema)
    info = world.h.adapter.load_table(table.table)
    parent = (
        None if info is None or info.current_snapshot is None else info.current_snapshot.snapshot_id
    )
    request = CommitRequest(
        table=table.table,
        batch_id=dataset_chunk_batch_id(selection_id, chunk_index),
        batch_fingerprint=table.fingerprint_rule.fingerprint(batch),
        row_count=batch.num_rows,
        expected_parent_snapshot_id=parent,
    )
    world.h.adapter.commit_batch(request, batch)


# ------------------------------------------------------------------ construction / shape


def test_table_property_and_binding_checks(w: ds.World) -> None:
    writer = _writer(w)
    assert writer.table == DATASET_SELECTION_CHUNKS.table == "research.dataset_selection_chunks"
    with pytest.raises(DatasetSpecError, match="schema"):
        IcebergChunkWriter(w.h.adapter, DATASET_SELECTIONS)  # v2 shape: no chunk_index/row_ordinal
    with pytest.raises(DatasetSpecError, match="namespace"):
        IcebergChunkWriter(w.h.adapter, DATA_QUALITY_REPORTS)  # not a research.* table


# ------------------------------------------------------------------ commit + seal happy path


def test_commit_chunk_then_seal(w: ds.World) -> None:
    writer = _writer(w)
    selection = "sel-happy-1"
    rows0 = [_row(selection, 0, 0), _row(selection, 0, 1)]
    rows1 = [_row(selection, 1, 2)]

    first = writer.commit_chunk(selection, 0, rows0)
    assert isinstance(first, ChunkCommitted)
    assert first.replayed is False
    assert first.proof.chunk_index == 0
    assert first.proof.batch_id == dataset_chunk_batch_id(selection, 0)
    assert first.proof.first_row_ordinal == 0
    assert first.proof.row_count == 2
    assert _SNAPSHOT_ID_RE.fullmatch(first.proof.snapshot_id)
    assert _SHA256_RE.fullmatch(first.proof.batch_fingerprint)

    second = writer.commit_chunk(selection, 1, rows1)
    assert second.replayed is False
    assert second.proof.chunk_index == 1
    assert second.proof.first_row_ordinal == 2
    assert second.proof.row_count == 1
    assert second.proof.snapshot_id != first.proof.snapshot_id

    writer.seal(selection, 2)  # must not raise

    persisted = w.h.adapter.scan_columns(
        DATASET_SELECTION_CHUNKS.table,
        columns=("chunk_index", "row_ordinal"),
        row_filter=EqualTo("selection_id", selection),  # type: ignore[call-arg, arg-type]
        snapshot_id=second.proof.snapshot_id,
    ).to_pylist()
    assert sorted((r["chunk_index"], r["row_ordinal"]) for r in persisted) == [
        (0, 0),
        (0, 1),
        (1, 2),
    ]


# ------------------------------------------------------------------ replay


def test_replay_same_writer_instance_is_idempotent(w: ds.World) -> None:
    writer = _writer(w)
    selection = "sel-replay-1"
    rows = [_row(selection, 0, 0)]
    first = writer.commit_chunk(selection, 0, rows)
    again = writer.commit_chunk(selection, 0, rows)
    assert again.replayed is True
    assert again.proof == first.proof


def test_replay_after_a_new_writer_instance_resumes_from_a_committed_prefix(w: ds.World) -> None:
    """Checkpoint replay: a fresh writer (a new process) re-derives from scratch, proves the
    already-committed prefix identical and continues past it (ADR-0077 §4.4)."""
    selection = "sel-restart-1"
    rows0 = [_row(selection, 0, 0), _row(selection, 0, 1)]
    rows1 = [_row(selection, 1, 2)]

    run_a = _writer(w)
    committed0 = run_a.commit_chunk(selection, 0, rows0)
    # run_a "crashes" here: chunk 1 is never committed, no seal happens.

    run_b = _writer(w)
    replay0 = run_b.commit_chunk(selection, 0, rows0)
    assert replay0.replayed is True
    assert replay0.proof == committed0.proof

    fresh1 = run_b.commit_chunk(selection, 1, rows1)
    assert fresh1.replayed is False
    run_b.seal(selection, 2)


# ------------------------------------------------------------------ tamper / integrity


def test_conflicting_content_at_the_same_chunk_is_rejected(w: ds.World) -> None:
    writer = _writer(w)
    selection = "sel-tamper-1"
    writer.commit_chunk(selection, 0, [_row(selection, 0, 0)])
    tampered = [_row(selection, 0, 0, revision="rev-evil")]
    with pytest.raises(CatalogIntegrityError, match="committed with other rows"):
        writer.commit_chunk(selection, 0, tampered)


def test_hole_beyond_the_first_missing_chunk_is_rejected(w: ds.World) -> None:
    writer = _writer(w)
    selection = "sel-hole-1"
    writer.commit_chunk(selection, 0, [_row(selection, 0, 0)])
    # Simulate a bypassed writer / tampering: chunk 2 lands without chunk 1 ever existing.
    _raw_commit(w, selection, chunk_index=2, rows=[_row(selection, 2, 2)])
    with pytest.raises(CatalogIntegrityError, match="hole"):
        writer.commit_chunk(selection, 1, [_row(selection, 1, 1)])


def test_seal_rejects_a_chunk_at_or_beyond_chunk_count(w: ds.World) -> None:
    writer = _writer(w)
    selection = "sel-extra-1"
    writer.commit_chunk(selection, 0, [_row(selection, 0, 0)])
    writer.commit_chunk(selection, 1, [_row(selection, 1, 1)])
    _raw_commit(w, selection, chunk_index=2, rows=[_row(selection, 2, 2)])
    with pytest.raises(CatalogIntegrityError, match="at or beyond chunk_count"):
        writer.seal(selection, 2)


def test_seal_rejects_a_non_positive_chunk_count(w: ds.World) -> None:
    writer = _writer(w)
    with pytest.raises(CatalogIntegrityError):
        writer.seal("sel-zero-1", 0)


# ------------------------------------------------------------------ malformed input


def test_commit_chunk_rejects_empty_rows(w: ds.World) -> None:
    writer = _writer(w)
    with pytest.raises(CatalogIntegrityError, match="no rows"):
        writer.commit_chunk("sel-empty-1", 0, [])


def test_commit_chunk_rejects_rows_carrying_another_chunk_index(w: ds.World) -> None:
    writer = _writer(w)
    selection = "sel-badindex-1"
    bad = [_row(selection, 0, 0), _row(selection, 1, 1)]
    with pytest.raises(CatalogIntegrityError, match="another chunk_index"):
        writer.commit_chunk(selection, 0, bad)


def test_commit_chunk_rejects_rows_carrying_another_selection_id(w: ds.World) -> None:
    writer = _writer(w)
    selection = "sel-badsel-1"
    bad = [_row(selection, 0, 0), _row("some-other-selection", 0, 1)]
    with pytest.raises(CatalogIntegrityError, match="another selection_id"):
        writer.commit_chunk(selection, 0, bad)


def test_commit_chunk_rejects_noncontiguous_row_ordinals(w: ds.World) -> None:
    writer = _writer(w)
    selection = "sel-badordinal-1"
    bad = [_row(selection, 0, 0), _row(selection, 0, 2)]
    with pytest.raises(CatalogIntegrityError, match="not contiguous"):
        writer.commit_chunk(selection, 0, bad)


def test_commit_chunk_rejects_a_malformed_chunk_index(w: ds.World) -> None:
    writer = _writer(w)
    with pytest.raises(ValueError):
        writer.commit_chunk("sel-invalid-1", -1, [_row("sel-invalid-1", -1, 0)])

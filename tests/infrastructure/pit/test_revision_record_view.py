"""The bounded selector re-iterates per-key RevisionRecords without storing a duplicate tuple."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

from core.contracts.revision import PointInTimeSpec, PointInTimeStatus
from core.contracts.storage import StorageAdapter
from infrastructure.pit.runs import RunLimits
from infrastructure.pit.selector import PitRunParams, _evaluate, _RevisionRecordView
from infrastructure.revision.channel_reconcile import revision_record_from_row
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.pit.test_selector import FAR, _chain
from tests.infrastructure.revision.rest_store_support import RestHarness


def test_record_view_reiterates_and_multitime_selector_matches_materialized_records(
    h: RestHarness,
) -> None:
    _chain(h)
    base = h.rows(c.TRADES)[0]
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = start + timedelta(seconds=20)
    offsets = [((index * 7) % 17) + 1 for index in range(257)]
    rows: dict[str, dict[str, Any]] = {}
    available: dict[str, datetime] = {}

    for index, offset in enumerate(offsets):
        revision_id = f"synthetic-{index:04d}"
        at = start + timedelta(seconds=offset)
        row = dict(base)
        row.update(
            revision_id=revision_id,
            source_id=f"source-{index:04d}",
            payload_hash=hashlib.sha256(f"payload-{index}".encode()).hexdigest(),
            arrival_seq=index,
            source_revision_id=None,
            source_revision_time=None,
            supersedes=(),
            available_time=at,
            ingest_time=at,
            knowledge_time=FAR,
            source_time=None,
        )
        rows[revision_id] = row
        available[revision_id] = at

    sorted_rows = {revision_id: rows[revision_id] for revision_id in sorted(rows)}
    view = _RevisionRecordView(sorted_rows)
    expected_records = tuple(revision_record_from_row(row) for row in sorted_rows.values())
    assert view._rows is sorted_rows
    assert not hasattr(view, "__dict__")
    assert tuple(view) == expected_records
    assert tuple(view) == expected_records

    spec = cast(
        PointInTimeSpec,
        SimpleNamespace(
            knowledge_cutoff=FAR,
            simulation_time=None,
            simulation_start=start,
            simulation_end=end,
        ),
    )
    params = PitRunParams(
        row_batch_rows=8,
        edge_batch_rows=8,
        merge_fanout=2,
        key_history_buffer=1,
        limits=RunLimits(leaf_max_records=8, leaf_max_bytes=1 << 16, fanout=2),
    )
    external = list(
        _evaluate(
            "one-key",
            view,
            (),
            spec,
            available,
            run_storage=cast(StorageAdapter, h.storage),
            run_params=params,
        )
    )
    materialized = list(_evaluate("one-key", expected_records, (), spec, available))

    assert external == materialized
    assert len(external) > 1
    assert external[0].status is PointInTimeStatus.ABSENT
    assert all(item.status is PointInTimeStatus.CONFLICT for item in external[1:])

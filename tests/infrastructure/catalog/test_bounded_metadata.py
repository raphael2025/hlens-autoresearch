"""Actual local SqlCatalog coverage for the private bounded metadata reader."""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

import pytest
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import ValidationError as IcebergValidationError
from pyiceberg.expressions import AlwaysTrue
from pyiceberg.table.metadata import TableMetadataUtil

from core.contracts.catalog import CommitRequest, SnapshotNotFound
from infrastructure.catalog.bounded_metadata import (
    BoundedMetadataError,
    BoundedMetadataLimits,
    _ByteReader,
)
from infrastructure.catalog.iceberg_adapter import (
    CatalogIntegrityError,
    PyIcebergCatalogAdapter,
)
from infrastructure.storage.local import LocalFileStorageAdapter
from infrastructure.streaming.content_key_tree import KeyTreeParams
from infrastructure.streaming.runs import RunLimits
from tests.infrastructure.catalog.catalog_support import (
    ALPHA,
    REGISTRY,
    RULE,
    SqliteCatalogHarness,
    make_batch,
)


def _limits(
    *,
    max_snapshots: int = 5000,
    max_metadata_bytes: int = 16 * 1024 * 1024,
    max_retained_json_bytes: int = 2 * 1024 * 1024,
) -> BoundedMetadataLimits:
    return BoundedMetadataLimits(
        max_metadata_bytes=max_metadata_bytes,
        max_item_bytes=256 * 1024,
        max_retained_json_bytes=max_retained_json_bytes,
        read_chunk_bytes=16 * 1024,
        max_small_array_items=512,
        max_map_items=512,
        max_snapshots=max_snapshots,
        run_capacity=64,
        run_limits=RunLimits(leaf_max_records=32, leaf_max_bytes=1024 * 1024, fanout=8),
        run_merge_fanout=8,
        key_tree_params=KeyTreeParams(page_max_bytes=1024 * 1024, leaf_max_records=64, fanout=8),
    )


def test_byte_reader_keeps_tokens_across_chunk_boundaries() -> None:
    payload = b" " * 16_383 + b'{"alpha": [1, {"beta": "x\\"y"}], "omega": true}'
    reader = _ByteReader(
        io.BytesIO(payload),
        max_bytes=len(payload),
        max_item_bytes=len(payload),
        chunk_size=16 * 1024,
    )
    value = reader.json_value(max_bytes=len(payload))
    assert value == {"alpha": [1, {"beta": 'x"y'}], "omega": True}

    split_payload = b"[" + b" " * 16_384 + b'{"id": 1}]'
    split_reader = _ByteReader(
        io.BytesIO(split_payload),
        max_bytes=len(split_payload),
        max_item_bytes=128,
        chunk_size=7,
    )
    seen: list[object] = []
    assert split_reader.array(lambda item, _, _size: seen.append(item), max_items=2) == 1
    assert seen == [{"id": 1}]


def test_bounded_history_and_compact_scan_view_match_real_sql_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = SqliteCatalogHarness(tmp_path)
    catalog = harness.sql_catalog()
    adapter = PyIcebergCatalogAdapter(catalog, REGISTRY)
    adapter.create_table(ALPHA.binding)
    for index in range(4):
        batch = make_batch(1, f"bounded-{index}")
        catalog_table = catalog.load_table(ALPHA.table)
        current_snapshot = catalog_table.current_snapshot()
        parent_id = None if current_snapshot is None else str(current_snapshot.snapshot_id)
        adapter.commit_batch(
            CommitRequest(
                table=ALPHA.table,
                batch_id=f"bounded-{index}",
                batch_fingerprint=RULE.fingerprint(batch),
                row_count=1,
                expected_parent_snapshot_id=None if index == 0 else parent_id,
            ),
            batch,
        )

    storage = LocalFileStorageAdapter(
        (tmp_path / "warehouse").as_uri(), (tmp_path / "staging").as_uri()
    )
    eager = catalog.load_table(ALPHA.table)
    eager_head = eager.current_snapshot()
    assert eager_head is not None
    head = str(eager_head.snapshot_id)

    original_loader = catalog.load_table

    def reject_eager_load(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("bounded pinning called eager SqlCatalog.load_table")

    monkeypatch.setattr(catalog, "load_table", reject_eager_load)
    bounded = adapter.pin_bounded_metadata(ALPHA.table, storage=storage, limits=_limits())
    monkeypatch.setattr(catalog, "load_table", original_loader)

    expected = list(adapter.history(ALPHA.table, head))
    actual = [adapter._snapshot_info(ALPHA.table, item) for item in bounded.iter_history(head)]
    assert actual == expected
    assert bounded.metadata_location == eager.metadata_location
    assert bounded.snapshot_count == len(eager.metadata.snapshots) == 4
    metadata_path = Path(unquote(urlparse(eager.metadata_location).path))
    eager_parsed = TableMetadataUtil.parse_raw(metadata_path.read_text(encoding="utf-8"))
    assert bounded.metadata == eager_parsed.model_copy(update={"snapshots": []})

    compact = bounded.compact_table(head)
    assert len(compact.metadata.snapshots) == 1
    assert adapter._verified(ALPHA.table, compact)[1] == ALPHA.binding

    # The fixed-snapshot reader keeps the planner's bound-method evaluators, which in PyIceberg
    # retain its planner. Assert that retained planner metadata is the one-snapshot projection.
    def reject_catalog_reload(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("pinned scan reloaded SqlCatalog metadata")

    monkeypatch.setattr(catalog, "load_table", reject_catalog_reload)
    reader = adapter.scan_pinned_batches(
        bounded,
        snapshot_id=head,
        columns=("seq",),
        row_filter=AlwaysTrue(),
    )
    try:
        source_frame = cast(Any, reader._source).gi_frame
        assert source_frame is not None
        plan = source_frame.f_locals["plan"]
        planner = plan.manifest_evaluators.default_factory.__self__
        assert planner.table_metadata is not bounded.metadata
        assert len(planner.table_metadata.snapshots) == 1
        assert planner.table_metadata.snapshots[0].snapshot_id == int(head)
    finally:
        reader.close()
        storage.close()
        harness.cleanup()


def test_branch_skipped_parent_and_cycle_match_eager_history(tmp_path: Path) -> None:
    harness = SqliteCatalogHarness(tmp_path)
    catalog = harness.sql_catalog()
    adapter = PyIcebergCatalogAdapter(catalog, REGISTRY)
    adapter.create_table(ALPHA.binding)
    for index in range(4):
        batch = make_batch(1, f"branch-{index}")
        current = catalog.load_table(ALPHA.table).current_snapshot()
        adapter.commit_batch(
            CommitRequest(
                table=ALPHA.table,
                batch_id=f"branch-{index}",
                batch_fingerprint=RULE.fingerprint(batch),
                row_count=1,
                expected_parent_snapshot_id=None if current is None else str(current.snapshot_id),
            ),
            batch,
        )

    eager = catalog.load_table(ALPHA.table)
    source_snapshots = eager.metadata.snapshots
    assert len(source_snapshots) == 4
    a, b, c, d = source_snapshots
    branched = [
        a.model_copy(update={"parent_snapshot_id": None}),
        b.model_copy(update={"parent_snapshot_id": a.snapshot_id}),
        c.model_copy(update={"parent_snapshot_id": a.snapshot_id}),
        d.model_copy(update={"parent_snapshot_id": b.snapshot_id}),
    ]
    raw = eager.metadata.model_dump(mode="json", by_alias=True, exclude_none=True)
    raw["snapshots"] = [
        item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in branched
    ]
    raw["refs"]["side"] = {"snapshot-id": c.snapshot_id, "type": "branch"}
    _replace_metadata_pointer(catalog, eager.metadata_location, raw)
    eager_branch = catalog.load_table(ALPHA.table)
    bounded_storage = LocalFileStorageAdapter(
        (tmp_path / "warehouse").as_uri(), (tmp_path / "staging").as_uri()
    )
    bounded = adapter.pin_bounded_metadata(ALPHA.table, storage=bounded_storage, limits=_limits())
    head_id = str(d.snapshot_id)
    assert (
        [item.snapshot_id for item in bounded.iter_history(head_id)]
        == [int(item.snapshot_id) for item in adapter.history(ALPHA.table, head_id)]
        == [d.snapshot_id, b.snapshot_id, a.snapshot_id]
    )
    assert eager_branch.metadata.refs["side"].snapshot_id == c.snapshot_id

    with pytest.raises(BoundedMetadataError, match="max_snapshots"):
        adapter.pin_bounded_metadata(
            ALPHA.table,
            storage=bounded_storage,
            limits=_limits(max_snapshots=3),
        )

    # A -> B -> A cycle reached through D -> B must yield the same distinct snapshots before
    # both implementations fail at the repeated pointer.
    cyclic = [
        a.model_copy(update={"parent_snapshot_id": b.snapshot_id}),
        b.model_copy(update={"parent_snapshot_id": a.snapshot_id}),
        c.model_copy(update={"parent_snapshot_id": a.snapshot_id}),
        d.model_copy(update={"parent_snapshot_id": b.snapshot_id}),
    ]
    cycle_raw = eager_branch.metadata.model_dump(mode="json", by_alias=True, exclude_none=True)
    cycle_raw["snapshots"] = [
        item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in cyclic
    ]
    _replace_metadata_pointer(catalog, eager_branch.metadata_location, cycle_raw)
    eager_yielded: list[int] = []
    eager_iterator = adapter.history(ALPHA.table, head_id)
    with pytest.raises(CatalogIntegrityError, match="cycle"):
        while True:
            eager_yielded.append(int(next(eager_iterator).snapshot_id))
    bounded_cycle = adapter.pin_bounded_metadata(
        ALPHA.table, storage=bounded_storage, limits=_limits()
    )
    bounded_yielded: list[int] = []
    bounded_iterator = bounded_cycle.iter_history(head_id)
    with pytest.raises(CatalogIntegrityError, match="cycle"):
        while True:
            bounded_yielded.append(next(bounded_iterator).snapshot_id)
    assert (
        bounded_yielded
        == eager_yielded
        == [
            d.snapshot_id,
            b.snapshot_id,
            a.snapshot_id,
        ]
    )

    with pytest.raises(SnapshotNotFound):
        next(bounded_cycle.iter_history("9223372036854775807"))
    bounded_storage.close()
    harness.cleanup()


def test_snapshot_history_at_increasing_sizes_matches_eager_reader(tmp_path: Path) -> None:
    harness = SqliteCatalogHarness(tmp_path)
    catalog = harness.sql_catalog()
    adapter = PyIcebergCatalogAdapter(catalog, REGISTRY)
    adapter.create_table(ALPHA.binding)
    batch = make_batch(1, "history-size")
    adapter.commit_batch(
        CommitRequest(
            table=ALPHA.table,
            batch_id="history-size",
            batch_fingerprint=RULE.fingerprint(batch),
            row_count=1,
            expected_parent_snapshot_id=None,
        ),
        batch,
    )
    storage = LocalFileStorageAdapter(
        (tmp_path / "warehouse").as_uri(), (tmp_path / "staging").as_uri()
    )
    for count in (1, 16, 64):
        eager = catalog.load_table(ALPHA.table)
        base = eager.metadata.snapshots[0]
        snapshots = [
            base.model_copy(
                update={
                    "snapshot_id": 987_654_321_000_000_000 + index,
                    "parent_snapshot_id": (
                        None if index == 0 else 987_654_321_000_000_000 + index - 1
                    ),
                    "sequence_number": index + 1,
                }
            )
            for index in range(count)
        ]
        raw = eager.metadata.model_dump(mode="json", by_alias=True, exclude_none=True)
        raw["snapshots"] = [
            item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in snapshots
        ]
        raw["current-snapshot-id"] = snapshots[-1].snapshot_id
        raw["refs"]["main"]["snapshot-id"] = snapshots[-1].snapshot_id
        raw["last-sequence-number"] = count
        location = _replace_metadata_pointer(catalog, eager.metadata_location, raw)

        bounded = adapter.pin_bounded_metadata(ALPHA.table, storage=storage, limits=_limits())
        assert bounded.metadata_location == location
        assert bounded.snapshot_count == count
        head = str(snapshots[-1].snapshot_id)
        expected = list(adapter.history(ALPHA.table, head))
        actual = [adapter._snapshot_info(ALPHA.table, item) for item in bounded.iter_history(head)]
        assert actual == expected
        assert len(actual) == count
    storage.close()
    harness.cleanup()


def test_pinned_history_matches_exception_semantics_and_first_duplicate(tmp_path: Path) -> None:
    harness = SqliteCatalogHarness(tmp_path)
    catalog = harness.sql_catalog()
    adapter = PyIcebergCatalogAdapter(catalog, REGISTRY)
    adapter.create_table(ALPHA.binding)
    batch = make_batch(1, "one")
    adapter.commit_batch(
        CommitRequest(
            table=ALPHA.table,
            batch_id="one",
            batch_fingerprint=RULE.fingerprint(batch),
            row_count=1,
            expected_parent_snapshot_id=None,
        ),
        batch,
    )
    eager = catalog.load_table(ALPHA.table)
    original = eager.metadata.snapshots[0]
    # TableMetadata.snapshot_by_id returns the first listed duplicate; make the duplicate's
    # summary visibly different while keeping the metadata valid.
    duplicate = original.model_copy(update={"summary": None})
    raw_metadata = eager.metadata.model_dump(mode="json", by_alias=True, exclude_none=True)
    raw_metadata["snapshots"] = [
        original.model_dump(mode="json", by_alias=True),
        duplicate.model_dump(mode="json", by_alias=True),
    ]
    _replace_metadata_pointer(catalog, eager.metadata_location, raw_metadata)

    storage = LocalFileStorageAdapter(
        (tmp_path / "warehouse").as_uri(), (tmp_path / "staging").as_uri()
    )
    bounded = adapter.pin_bounded_metadata(ALPHA.table, storage=storage, limits=_limits())
    assert bounded.snapshot_count == 2
    assert bounded.snapshot_by_id(original.snapshot_id) == original

    # Self-parent is rejected by both paths before yielding a DTO.
    self_parent = dict(raw_metadata)
    self_parent["snapshots"] = [
        original.model_copy(update={"parent_snapshot_id": original.snapshot_id}).model_dump(
            mode="json", by_alias=True
        )
    ]
    _replace_metadata_pointer(catalog, bounded.metadata_location, self_parent)
    self_bounded = adapter.pin_bounded_metadata(ALPHA.table, storage=storage, limits=_limits())
    with pytest.raises(CatalogIntegrityError):
        next(self_bounded.iter_history(str(original.snapshot_id)))

    # A valid but dangling parent yields the current row, then both readers fail on the parent.
    dangling = dict(raw_metadata)
    dangling["snapshots"] = [
        original.model_copy(
            update={"parent_snapshot_id": original.snapshot_id + 10_000}
        ).model_dump(mode="json", by_alias=True)
    ]
    _replace_metadata_pointer(catalog, self_bounded.metadata_location, dangling)
    dangling_bounded = adapter.pin_bounded_metadata(ALPHA.table, storage=storage, limits=_limits())
    iterator = dangling_bounded.iter_history(str(original.snapshot_id))
    assert next(iterator).snapshot_id == original.snapshot_id
    with pytest.raises(SnapshotNotFound):
        next(iterator)

    malformed = dict(dangling)
    malformed_snapshot = dict(malformed["snapshots"][0])
    malformed_snapshot["snapshot-id"] = "not-an-integer"
    malformed["snapshots"] = [malformed_snapshot]
    bad_location = _replace_metadata_bytes(
        catalog,
        dangling_bounded.metadata_location,
        json.dumps(malformed, separators=(",", ":")).encode("utf-8"),
    )
    with pytest.raises(IcebergValidationError):
        catalog.load_table(ALPHA.table)
    with pytest.raises(BoundedMetadataError, match="Snapshot validation"):
        adapter.pin_bounded_metadata(ALPHA.table, storage=storage, limits=_limits())
    assert bad_location
    storage.close()
    harness.cleanup()


def test_metadata_caps_and_duplicate_top_level_fields_fail_closed(tmp_path: Path) -> None:
    harness = SqliteCatalogHarness(tmp_path)
    catalog = harness.sql_catalog()
    adapter = PyIcebergCatalogAdapter(catalog, REGISTRY)
    adapter.create_table(ALPHA.binding)
    eager = catalog.load_table(ALPHA.table)
    raw_path = Path(unquote(urlparse(eager.metadata_location).path))
    body = raw_path.read_bytes()

    tiny_storage = LocalFileStorageAdapter(
        (tmp_path / "warehouse").as_uri(), (tmp_path / "staging").as_uri()
    )
    with pytest.raises(BoundedMetadataError, match="max_metadata_bytes"):
        adapter.pin_bounded_metadata(
            ALPHA.table,
            storage=tiny_storage,
            limits=_limits(max_metadata_bytes=max(1, len(body) - 1)),
        )
    tiny_storage.close()

    retained_storage = LocalFileStorageAdapter(
        (tmp_path / "warehouse").as_uri(), (tmp_path / "staging").as_uri()
    )
    with pytest.raises(BoundedMetadataError, match="max_retained_json_bytes"):
        adapter.pin_bounded_metadata(
            ALPHA.table,
            storage=retained_storage,
            limits=_limits(max_retained_json_bytes=1),
        )
    retained_storage.close()

    duplicate_document = body[:-1] + b',"format-version":2}'
    raw_path.write_bytes(duplicate_document)
    with pytest.raises(BoundedMetadataError, match="duplicate"):
        storage = LocalFileStorageAdapter(
            (tmp_path / "warehouse").as_uri(), (tmp_path / "staging").as_uri()
        )
        try:
            adapter.pin_bounded_metadata(ALPHA.table, storage=storage, limits=_limits())
        finally:
            storage.close()
    harness.cleanup()


def _replace_metadata_pointer(
    catalog: SqlCatalog, old_location: str, raw_metadata: Mapping[str, object]
) -> str:
    from pyiceberg.catalog.sql import IcebergTables
    from sqlalchemy import update
    from sqlalchemy.orm import Session

    metadata = TableMetadataUtil.parse_obj(dict(raw_metadata))
    old_path = Path(unquote(urlparse(old_location).path))
    new_location = (old_path.parent / f"bounded-{uuid.uuid4().hex}.metadata.json").as_uri()
    Path(unquote(urlparse(new_location).path)).write_text(
        metadata.model_dump_json(by_alias=True, exclude_none=True), encoding="utf-8"
    )
    with Session(catalog.engine) as session:
        session.execute(
            update(IcebergTables)
            .where(IcebergTables.metadata_location == old_location)
            .values(metadata_location=new_location, previous_metadata_location=old_location)
        )
        session.commit()
    return new_location


def _replace_metadata_bytes(catalog: SqlCatalog, old_location: str, body: bytes) -> str:
    from pyiceberg.catalog.sql import IcebergTables
    from sqlalchemy import update
    from sqlalchemy.orm import Session

    old_path = Path(unquote(urlparse(old_location).path))
    new_location = (old_path.parent / f"bounded-{uuid.uuid4().hex}.metadata.json").as_uri()
    Path(unquote(urlparse(new_location).path)).write_bytes(body)
    with Session(catalog.engine) as session:
        session.execute(
            update(IcebergTables)
            .where(IcebergTables.metadata_location == old_location)
            .values(metadata_location=new_location, previous_metadata_location=old_location)
        )
        session.commit()
    return new_location

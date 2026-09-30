"""Canonical-partition v3 event/revision JSONL projection and RunSet lifecycle tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.contracts.storage import StorageAdapter
from core.domain.base import canonical_json
from infrastructure.quality.report_projection import (
    CANONICAL_PARTITION_V3_RULE_HASH,
    CanonicalPartitionProjectionError,
    CanonicalPartitionV3Projector,
)
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.streaming.runs import RunLimits


@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorageAdapter:
    return LocalFileStorageAdapter(
        (tmp_path / "warehouse").as_uri(), (tmp_path / "staging").as_uri()
    )


def limits() -> RunLimits:
    return RunLimits(leaf_max_records=8, leaf_max_bytes=4096, fanout=3)


def projector(
    storage: StorageAdapter, *, capacity: int = 3, merge_fanout: int = 2
) -> CanonicalPartitionV3Projector:
    return CanonicalPartitionV3Projector(
        storage, capacity=capacity, merge_fanout=merge_fanout, limits=limits()
    )


def project_one(
    storage: StorageAdapter,
    *,
    revision_ids: Any,
    event_type: str = "bar_1m_gap",
    table: str | None = "canonical.bars_1m",
    observation_key: str | None = "binance:spot:bar:BTC-USDT:1m:1700000000000",
    event_start: datetime | None = datetime(2023, 11, 14, tzinfo=UTC),
    event_end: datetime | None = datetime(2023, 11, 14, 0, 1, tzinfo=UTC),
    detail: str = "gap",
) -> dict[str, Any]:
    instance = projector(storage)
    with instance.project_event(
        event_type=event_type,
        table=table,
        observation_key=observation_key,
        revision_ids=revision_ids,
        event_start=event_start,
        event_end=event_end,
        detail=detail,
    ) as projected:
        revisions = list(projected.revision_records)
        record = dict(projected.event_record)
    assert instance.next_event_ordinal == 1
    assert instance.next_revision_ordinal == len(revisions)
    return record


def test_empty_revision_ids_are_supported_with_zero_range(storage: LocalFileStorageAdapter) -> None:
    instance = projector(storage)
    with instance.project_event(
        event_type="report_inputs",
        table=None,
        observation_key=None,
        revision_ids=(),
        event_start=None,
        event_end=None,
        detail="pinned input snapshots",
    ) as projected:
        assert list(projected.revision_records) == []
        assert projected.event_record["revision_first_ordinal"] == 0
        assert projected.event_record["revision_count"] == 0
        assert projected.event_record["event_start"] is None
        assert projected.event_record["event_end"] is None
    assert instance.next_event_ordinal == 1
    assert instance.next_revision_ordinal == 0


def test_revisions_are_sorted_deduplicated_and_bound_to_event_ordinal(
    storage: LocalFileStorageAdapter,
) -> None:
    instance = projector(storage, capacity=2)
    with instance.project_event(
        event_type="competing_heads",
        table="canonical.trades",
        observation_key="key-a",
        revision_ids=(item for item in ["rev-c", "rev-a", "rev-b", "rev-a"]),
        event_start=None,
        event_end=None,
        detail="conflicting heads",
    ) as first:
        assert first.event_record["revision_first_ordinal"] == 0
        assert first.event_record["revision_count"] == 3
        assert list(first.revision_records) == [
            {"event_ordinal": 0, "revision_id": "rev-a"},
            {"event_ordinal": 0, "revision_id": "rev-b"},
            {"event_ordinal": 0, "revision_id": "rev-c"},
        ]
    with instance.project_event(
        event_type="evidence_gaps",
        table="canonical.trades",
        observation_key="key-b",
        revision_ids=["rev-y", "rev-x"],
        event_start=None,
        event_end=None,
        detail="missing evidence",
    ) as second:
        assert second.event_record["revision_first_ordinal"] == 3
        assert second.event_record["revision_count"] == 2
        assert list(second.revision_records) == [
            {"event_ordinal": 1, "revision_id": "rev-x"},
            {"event_ordinal": 1, "revision_id": "rev-y"},
        ]
    assert instance.next_event_ordinal == 2
    assert instance.next_revision_ordinal == 5


def test_event_id_is_stable_for_different_input_orders(storage: LocalFileStorageAdapter) -> None:
    first = project_one(storage, revision_ids=["rev-c", "rev-a", "rev-b", "rev-a"])
    second = project_one(storage, revision_ids=["rev-b", "rev-c", "rev-a"])
    assert first == second


def test_high_cardinality_event_streams_without_materializing_revision_ids(
    storage: LocalFileStorageAdapter,
) -> None:
    count = 1_200
    ids = (f"rev-{index:05d}" for index in range(count - 1, -1, -1) for _ in (0, 1))
    instance = projector(storage, capacity=13, merge_fanout=3)
    with instance.project_event(
        event_type="competing_heads",
        table="canonical.trades",
        observation_key="key-high-cardinality",
        revision_ids=ids,
        event_start=None,
        event_end=None,
        detail="many heads",
    ) as projected:
        assert projected.event_record["revision_count"] == count
        for expected_index, actual in enumerate(projected.revision_records):
            assert actual == {
                "event_ordinal": 0,
                "revision_id": f"rev-{expected_index:05d}",
            }
        assert expected_index == count - 1
    assert instance.next_revision_ordinal == count


def test_digest_uses_the_versioned_domain_and_length_prefixed_fields(
    storage: LocalFileStorageAdapter,
) -> None:
    revision_ids = ["rev-A", "rev-é", "rev-😀"]
    event = project_one(storage, revision_ids=revision_ids)
    fields = {key: value for key, value in event.items() if key != "event_id"}
    identity = {
        "rule_id": "hlens.quality.canonical-partition",
        "rule_version": "3.0.0",
        "rule_hash": CANONICAL_PARTITION_V3_RULE_HASH,
        "event_fields": fields,
    }
    encoded = canonical_json(identity).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(b"hlens.quality.canonical-partition@3.0.0/event-id/v1\x00")
    for value in [encoded, *(revision_id.encode("utf-8") for revision_id in sorted(revision_ids))]:
        digest.update(len(value).to_bytes(8, "big", signed=False))
        digest.update(value)
    assert event["event_id"] == f"qevt3-{digest.hexdigest()}"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_type", "other"),
        ("table", "canonical.other"),
        ("observation_key", "other-key"),
        ("event_start", datetime(2023, 11, 14, 0, 1, tzinfo=UTC)),
        ("event_end", datetime(2023, 11, 14, 0, 2, tzinfo=UTC)),
        ("detail", "different detail"),
    ],
)
def test_event_id_changes_when_any_fixed_text_or_time_field_changes(
    storage: LocalFileStorageAdapter, field: str, value: Any
) -> None:
    kwargs: dict[str, Any] = {"revision_ids": ["rev-a", "rev-b"]}
    original = project_one(storage, **kwargs)
    kwargs[field] = value
    changed = project_one(storage, **kwargs)
    assert original["event_id"] != changed["event_id"]


def test_event_id_changes_when_revision_identity_changes_but_count_does_not(
    storage: LocalFileStorageAdapter,
) -> None:
    first = project_one(storage, revision_ids=["rev-a", "rev-b"])
    changed = project_one(storage, revision_ids=["rev-a", "rev-c"])
    assert first["revision_count"] == changed["revision_count"] == 2
    assert first["event_id"] != changed["event_id"]


def test_event_id_changes_when_the_revision_count_changes(storage: LocalFileStorageAdapter) -> None:
    first = project_one(storage, revision_ids=["rev-a"])
    changed = project_one(storage, revision_ids=["rev-a", "rev-b"])
    assert first["revision_count"] == 1
    assert changed["revision_count"] == 2
    assert first["event_id"] != changed["event_id"]


def test_revision_first_ordinal_is_part_of_event_identity(storage: LocalFileStorageAdapter) -> None:
    first = projector(storage)
    with first.project_event(
        event_type="report_inputs",
        table=None,
        observation_key=None,
        revision_ids=["prior-revision"],
        event_start=None,
        event_end=None,
        detail="prefix",
    ) as projected:
        list(projected.revision_records)
    with first.project_event(
        event_type="bar_1m_gap",
        table="canonical.bars_1m",
        observation_key="key",
        revision_ids=["rev-a"],
        event_start=None,
        event_end=None,
        detail="gap",
    ) as projected:
        later = dict(projected.event_record)
        list(projected.revision_records)
    early = project_one(storage, revision_ids=["rev-a"])
    assert later["revision_first_ordinal"] == 1
    assert early["revision_first_ordinal"] == 0
    assert later["event_id"] != early["event_id"]


def test_naive_or_non_utc_times_are_rejected(storage: LocalFileStorageAdapter) -> None:
    instance = projector(storage)
    for invalid in (
        datetime(2023, 11, 14),
        datetime(2023, 11, 14, tzinfo=timezone(timedelta(hours=3))),
    ):
        with pytest.raises(CanonicalPartitionProjectionError, match="timezone-aware|UTC offset"):
            with instance.project_event(
                event_type="bar_1m_gap",
                table="canonical.bars_1m",
                observation_key="key",
                revision_ids=[],
                event_start=invalid,
                event_end=None,
                detail="gap",
            ):
                pytest.fail("invalid time was accepted")


def test_required_text_fields_and_ordinal_parameters_are_strict(
    storage: LocalFileStorageAdapter,
) -> None:
    with pytest.raises(CanonicalPartitionProjectionError, match="event_type"):
        project_one(storage, revision_ids=[], event_type=" ")
    with pytest.raises(CanonicalPartitionProjectionError, match="detail"):
        project_one(storage, revision_ids=[], detail="")
    with pytest.raises(CanonicalPartitionProjectionError, match="revision_id"):
        project_one(storage, revision_ids=[""])
    for capacity, merge_fanout in ((True, 2), (2, True), (0, 2), (2, 1)):
        with pytest.raises(CanonicalPartitionProjectionError):
            CanonicalPartitionV3Projector(
                storage,
                capacity=capacity,
                merge_fanout=merge_fanout,
                limits=limits(),
            )


def test_early_close_releases_run_readers_and_prevents_ordinal_gap(
    storage: LocalFileStorageAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual_open = storage.open_read
    opened: list[Any] = []

    def tracked(ref: Any) -> Any:
        handle = actual_open(ref)
        opened.append(handle)
        return handle

    monkeypatch.setattr(storage, "open_read", tracked)
    instance = projector(storage, capacity=2)
    with pytest.raises(RuntimeError, match="stop early"):
        with instance.project_event(
            event_type="competing_heads",
            table="canonical.trades",
            observation_key="key",
            revision_ids=[f"rev-{index:03d}" for index in range(12)],
            event_start=None,
            event_end=None,
            detail="many heads",
        ) as projected:
            next(projected.revision_records)
            raise RuntimeError("stop early")
    assert opened
    assert all(handle.closed for handle in opened)
    assert instance.next_event_ordinal == 0
    assert instance.next_revision_ordinal == 0
    with pytest.raises(CanonicalPartitionProjectionError, match="failed after an incomplete event"):
        with instance.project_event(
            event_type="another",
            table="canonical.trades",
            observation_key="key-2",
            revision_ids=[],
            event_start=None,
            event_end=None,
            detail="must not continue",
        ):
            pytest.fail("failed projector accepted another event")

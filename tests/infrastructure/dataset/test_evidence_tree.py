"""Evidence streams (ADR-0077 §2 / §3 / §5 / §6.2.3 / §6.2.4): writer, reader and fail-closed paths.

Every limit below is an arbitrary small value chosen to exercise the tree shape; none is a DQ-9
choice (DQ-9 stays OPEN).
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pytest

from core.contracts.storage import StorageAdapter
from core.contracts.universe import (
    DatasetQualityReportRef,
    DatasetQualitySubject,
    EvidenceObjectRef,
    EvidenceStream,
    EvidenceStreamRef,
    SelectedRevisionLineage,
)
from core.domain.base import CONTRACT_SCHEMA_VERSION, Contract
from infrastructure.dataset.evidence import (
    EvidenceIntegrityError,
    EvidenceLimitError,
    EvidenceRecordTooLarge,
    EvidenceTreeLimits,
    EvidenceTreeWriter,
    canonical_depth,
    evidence_record_bytes,
    evidence_record_from_bytes,
    iter_evidence_stream,
)
from infrastructure.storage import LocalFileStorageAdapter
from tests.infrastructure.dataset import dataset_support as ds

REPORTS = EvidenceStream.QUALITY_REPORTS
LINEAGE = EvidenceStream.LINEAGE
VERSION = CONTRACT_SCHEMA_VERSION


def limits(records: int, size: int, fanout: int) -> EvidenceTreeLimits:
    return EvidenceTreeLimits(leaf_max_records=records, leaf_max_bytes=size, fanout=fanout)


def report(i: int) -> DatasetQualityReportRef:
    return DatasetQualityReportRef(
        report_id=f"report-{i:04d}",
        subject=DatasetQualitySubject.SYMBOL_DAY,
        symbol="BTCUSDT",
        day=date(2023, 11, 1) + timedelta(days=i),
    )


def write(
    storage: StorageAdapter,
    records: list[DatasetQualityReportRef] | list[SelectedRevisionLineage],
    tree: EvidenceTreeLimits,
    stream: EvidenceStream = REPORTS,
) -> EvidenceStreamRef:
    writer = EvidenceTreeWriter(storage, stream, limits=tree, schema_version=VERSION)
    for record in records:
        writer.append(record)
    return writer.finish()


def read(
    storage: StorageAdapter, ref: EvidenceStreamRef, tree: EvidenceTreeLimits
) -> list[Contract]:
    with iter_evidence_stream(storage, ref, limits=tree, schema_version=VERSION) as records:
        return list(records)


@pytest.fixture
def storage(evidence_store: LocalFileStorageAdapter) -> LocalFileStorageAdapter:
    return evidence_store


# ------------------------------------------------------------------ shape and round trip


@pytest.mark.parametrize(
    ("leaves", "fanout", "depth"),
    [(0, 2, 1), (1, 2, 1), (2, 2, 1), (3, 2, 2), (4, 2, 2), (5, 2, 3), (9, 3, 2), (10, 3, 3)],
)
def test_canonical_depth(leaves: int, fanout: int, depth: int) -> None:
    assert canonical_depth(leaves, fanout) == depth


@pytest.mark.parametrize("count", [1, 2, 3, 5, 7, 8, 9])
def test_round_trip_keeps_order_counts_and_canonical_shape(
    storage: LocalFileStorageAdapter, count: int
) -> None:
    tree = limits(1, 4096, 2)
    records = [report(i) for i in range(count)]
    ref = write(storage, records, tree)
    assert (ref.stream, ref.record_count, ref.leaf_count) == (REPORTS, count, count)
    assert ref.depth == canonical_depth(count, 2)
    assert read(storage, ref, tree) == records


def test_leaves_close_on_record_count_and_on_bytes(storage: LocalFileStorageAdapter) -> None:
    records = [report(i) for i in range(6)]
    line = len(evidence_record_bytes(records[0]))
    by_count = write(storage, records, limits(4, 100 * line, 3))
    assert by_count.leaf_count == 2  # 4 + 2
    by_bytes = write(storage, records, limits(100, 2 * line, 3))
    assert by_bytes.leaf_count == 3  # 2 + 2 + 2
    assert read(storage, by_bytes, limits(100, 2 * line, 3)) == records


def test_empty_stream_is_a_childless_level_one_root(storage: LocalFileStorageAdapter) -> None:
    tree = limits(2, 4096, 2)
    ref = write(storage, [], tree)
    assert (ref.record_count, ref.leaf_count, ref.depth) == (0, 0, 1)
    assert ds.evidence_object_bytes(storage, ref.root.key) == ds.evidence_index(REPORTS, 1, [])
    assert read(storage, ref, tree) == []


def test_same_records_same_root_and_republishing_is_idempotent(
    storage: LocalFileStorageAdapter, tmp_path: Path
) -> None:
    tree = limits(2, 4096, 2)
    records = [report(i) for i in range(5)]
    first = write(storage, records, tree)
    assert write(storage, records, tree) == first
    elsewhere = ds.evidence_storage(tmp_path / "other")
    assert write(elsewhere, records, tree) == first  # content identity, no uri in it (DQ-8)
    assert write(storage, records[:4], tree).root != first.root
    assert write(storage, [*records[1:], records[0]], tree).root != first.root


def test_object_keys_are_content_keys(storage: LocalFileStorageAdapter) -> None:
    ref = write(storage, [report(0)], limits(2, 4096, 2))
    assert ref.root.key == f"research/dataset-evidence/v1/{ref.root.sha256}.jsonl"


# ------------------------------------------------------------------ limits and records


def test_limits_have_no_defaults_and_are_validated() -> None:
    parameters = inspect.signature(EvidenceTreeLimits).parameters.values()
    assert all(item.default is inspect.Parameter.empty for item in parameters)
    with pytest.raises(TypeError):
        EvidenceTreeLimits()  # type: ignore[call-arg]
    for bad in (
        {"leaf_max_records": 0, "leaf_max_bytes": 10, "fanout": 2},
        {"leaf_max_records": 1, "leaf_max_bytes": 0, "fanout": 2},
        {"leaf_max_records": 1, "leaf_max_bytes": 10, "fanout": 1},
        {"leaf_max_records": True, "leaf_max_bytes": 10, "fanout": 2},
        {"leaf_max_records": 1, "leaf_max_bytes": 10.0, "fanout": 2},
    ):
        with pytest.raises(EvidenceLimitError):
            EvidenceTreeLimits(**bad)


def test_a_record_longer_than_a_leaf_fails_closed(storage: LocalFileStorageAdapter) -> None:
    line = len(evidence_record_bytes(report(0)))
    writer = EvidenceTreeWriter(
        storage, REPORTS, limits=limits(4, line - 1, 2), schema_version=VERSION
    )
    with pytest.raises(EvidenceRecordTooLarge):
        writer.append(report(0))


def test_writer_refuses_other_record_types_and_versions(storage: LocalFileStorageAdapter) -> None:
    tree = limits(2, 4096, 2)
    writer = EvidenceTreeWriter(storage, REPORTS, limits=tree, schema_version=VERSION)
    with pytest.raises(EvidenceIntegrityError):
        writer.append(ds.trade_lineage("r1"))  # a lineage record in the report stream
    lineage = EvidenceTreeWriter(storage, LINEAGE, limits=tree, schema_version=VERSION)
    fields = ds.trade_lineage("r1").model_dump(exclude={"schema_version"})
    old = SelectedRevisionLineage(**fields, schema_version="2.2.0")
    with pytest.raises(EvidenceIntegrityError):
        lineage.append(old)  # one write group, one version (ADR-0052 V1)


def test_projection_carries_no_envelope_at_any_depth() -> None:
    member = ds.v3_member("BTCUSDT", "listing-btc")
    line = evidence_record_bytes(member)
    assert b"schema_version" not in line
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    again = evidence_record_from_bytes(EvidenceStream.MEMBERS, line, schema_version=VERSION)
    assert again == member


def test_records_are_rebuilt_at_the_recorded_version(storage: LocalFileStorageAdapter) -> None:
    tree = limits(2, 4096, 2)
    old = [
        SelectedRevisionLineage(
            **ds.trade_lineage(f"r{i}").model_dump(exclude={"schema_version"}),
            schema_version="2.2.0",
        )
        for i in range(3)
    ]
    writer = EvidenceTreeWriter(storage, LINEAGE, limits=tree, schema_version="2.2.0")
    for record in old:
        writer.append(record)
    ref = writer.finish()
    current = [ds.trade_lineage(f"r{i}") for i in range(3)]
    assert write(storage, current, tree, LINEAGE).root == ref.root  # no envelope in the bytes
    with iter_evidence_stream(storage, ref, limits=tree, schema_version="2.2.0") as records:
        assert list(records) == old
    assert read(storage, ref, tree) == current


@pytest.mark.parametrize(
    "mutate",
    [
        lambda line: line.replace(b",", b", ", 1),  # not canonical whitespace
        lambda line: b'{"schema_version":"2.3.0",' + line[1:],  # an envelope in the bytes
        lambda line: line[:-1],  # no LF
        lambda line: line.replace(b"BTCUSDT", b" BTCUSDT"),  # stripped on parse: not canonical
    ],
)
def test_only_the_canonical_projection_parses(mutate: Callable[[bytes], bytes]) -> None:
    line = evidence_record_bytes(report(0))
    bad = mutate(line)
    with pytest.raises(EvidenceIntegrityError):
        evidence_record_from_bytes(REPORTS, bad, schema_version=VERSION)


# ------------------------------------------------------------------ reader: fail closed


def test_a_missing_object_fails_closed(storage: LocalFileStorageAdapter, tmp_path: Path) -> None:
    tree = limits(2, 4096, 2)
    ref = write(ds.evidence_storage(tmp_path / "elsewhere"), [report(0)], tree)
    with pytest.raises(EvidenceIntegrityError, match="missing"):
        read(storage, ref, tree)


def test_lookup_identity_is_checked_before_open_read(storage: LocalFileStorageAdapter) -> None:
    tree = limits(2, 4096, 2)
    ref = write(storage, [report(0)], tree)
    with pytest.raises(EvidenceIntegrityError, match="another identity"):
        read(ds.LookupLies(storage), ref, tree)


def test_tampered_object_bytes_fail_closed(storage: LocalFileStorageAdapter) -> None:
    tree = limits(1, 4096, 2)
    records = [report(i) for i in range(3)]
    ref = write(storage, records, tree)
    # Rewrite the first leaf in place (same length, other content).
    leaf_key = _first_leaf_key(storage, ref.root.key)
    path = ds.evidence_object_path(storage, leaf_key)
    data = path.read_bytes()
    path.write_bytes(data.replace(b"report-0000", b"report-9999"))
    with pytest.raises(EvidenceIntegrityError):
        read(storage, ref, tree)


def _first_leaf_key(storage: LocalFileStorageAdapter, key: str) -> str:
    while True:
        lines = ds.evidence_object_bytes(storage, key).split(b"\n")
        header = json.loads(lines[0])
        if header["node"] == "leaf":
            return key
        key = json.loads(lines[1])["key"]


def _leaf(
    storage: LocalFileStorageAdapter, first: int, records: list[DatasetQualityReportRef]
) -> tuple[EvidenceObjectRef, int, int]:
    data = ds.evidence_leaf(REPORTS, first, [evidence_record_bytes(item) for item in records])
    return ds.publish_bytes(storage, data), first, len(records)


def test_reordered_children_fail_closed(storage: LocalFileStorageAdapter) -> None:
    tree = limits(1, 4096, 2)
    a = _leaf(storage, 0, [report(0)])
    b = _leaf(storage, 1, [report(1)])
    good = ds.publish_bytes(storage, ds.evidence_index(REPORTS, 1, [a, b]))
    ref = ds.stream_ref(REPORTS, good, record_count=2, leaf_count=2, depth=1)
    assert read(storage, ref, tree) == [report(0), report(1)]
    swapped = ds.publish_bytes(storage, ds.evidence_index(REPORTS, 1, [b, a], first=0, count=2))
    bad = ds.stream_ref(REPORTS, swapped, record_count=2, leaf_count=2, depth=1)
    with pytest.raises(EvidenceIntegrityError, match="contiguous"):
        read(storage, bad, tree)


def test_truncation_is_found_at_the_parent(storage: LocalFileStorageAdapter) -> None:
    tree = limits(1, 4096, 2)
    a = _leaf(storage, 0, [report(0)])
    b = _leaf(storage, 1, [report(1)])
    # The root claims two records but lists one child.
    root = ds.publish_bytes(storage, ds.evidence_index(REPORTS, 1, [a], count=2))
    ref = ds.stream_ref(REPORTS, root, record_count=2, leaf_count=2, depth=1)
    with pytest.raises(EvidenceIntegrityError, match="sum"):
        read(storage, ref, tree)
    # The stream reference claims fewer records than the root commits.
    full = ds.publish_bytes(storage, ds.evidence_index(REPORTS, 1, [a, b]))
    short = ds.stream_ref(REPORTS, full, record_count=1, leaf_count=1, depth=1)
    with pytest.raises(EvidenceIntegrityError):
        read(storage, short, tree)


def test_a_header_naming_another_stream_fails_closed(storage: LocalFileStorageAdapter) -> None:
    tree = limits(1, 4096, 2)
    data = ds.evidence_leaf(LINEAGE, 0, [evidence_record_bytes(report(0))])
    leaf = (ds.publish_bytes(storage, data), 0, 1)
    root = ds.publish_bytes(storage, ds.evidence_index(REPORTS, 1, [leaf]))
    ref = ds.stream_ref(REPORTS, root, record_count=1, leaf_count=1, depth=1)
    with pytest.raises(EvidenceIntegrityError, match="another stream"):
        read(storage, ref, tree)


def test_a_non_full_inner_node_fails_closed(storage: LocalFileStorageAdapter) -> None:
    tree = limits(1, 4096, 2)
    leaves = [_leaf(storage, i, [report(i)]) for i in range(3)]
    # Canonical: [[0, 1], [2]]. Forged: [[0], [1, 2]] has a short node off the rightmost path.
    left = ds.publish_bytes(storage, ds.evidence_index(REPORTS, 1, leaves[:1]))
    right = ds.publish_bytes(storage, ds.evidence_index(REPORTS, 1, leaves[1:]))
    root = ds.publish_bytes(storage, ds.evidence_index(REPORTS, 2, [(left, 0, 1), (right, 1, 2)]))
    ref = ds.stream_ref(REPORTS, root, record_count=3, leaf_count=3, depth=2)
    with pytest.raises(EvidenceIntegrityError, match="not full"):
        read(storage, ref, tree)


def test_a_leaf_closed_early_fails_closed(storage: LocalFileStorageAdapter) -> None:
    tree = limits(2, 4096, 2)
    a = _leaf(storage, 0, [report(0)])  # room for another record: not canonical
    b = _leaf(storage, 1, [report(1), report(2)])
    root = ds.publish_bytes(storage, ds.evidence_index(REPORTS, 1, [a, b]))
    ref = ds.stream_ref(REPORTS, root, record_count=3, leaf_count=2, depth=1)
    with pytest.raises(EvidenceIntegrityError, match="closed early"):
        read(storage, ref, tree)


def test_depth_and_leaf_count_must_be_the_canonical_ones(storage: LocalFileStorageAdapter) -> None:
    tree = limits(1, 4096, 2)
    ref = write(storage, [report(i) for i in range(3)], tree)
    deeper = ds.stream_ref(REPORTS, ref.root, record_count=3, leaf_count=3, depth=3)
    with pytest.raises(EvidenceIntegrityError, match="canonical depth"):
        read(storage, deeper, tree)
    fewer = ds.stream_ref(REPORTS, ref.root, record_count=3, leaf_count=2, depth=1)
    with pytest.raises(EvidenceIntegrityError):
        read(storage, fewer, tree)


def test_reading_with_other_limits_fails_closed(storage: LocalFileStorageAdapter) -> None:
    ref = write(storage, [report(i) for i in range(4)], limits(2, 4096, 2))
    with pytest.raises(EvidenceIntegrityError):
        read(storage, ref, limits(1, 4096, 2))


def test_the_iterator_is_released_on_early_exit(storage: LocalFileStorageAdapter) -> None:
    tree = limits(1, 4096, 2)
    records = [report(i) for i in range(5)]
    ref = write(storage, records, tree)
    with iter_evidence_stream(storage, ref, limits=tree, schema_version=VERSION) as it:
        assert next(it) == records[0]
    with pytest.raises(StopIteration):
        next(it)  # closed with the block
    assert read(storage, ref, tree) == records

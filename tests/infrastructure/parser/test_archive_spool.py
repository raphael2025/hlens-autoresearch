"""The disk-spooled D1 parse (E1-CAP-1): same verdicts, bounded reads, nothing left behind.

Every parse of the other parser tests is already checked against the spool (``conftest.py``);
these cover what only the spool has: real storage, block reads, lifecycle and tampering.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pytest

from core.contracts.storage import ObjectNotFound, ObjectRef, StageRequest
from infrastructure.parser import (
    ArchiveParseRequest,
    ParsedArchive,
    RejectionCode,
    SpooledArchive,
    archive_spool,
    parse_archive,
    spool_archive,
)
from infrastructure.storage import LocalFileStorageAdapter
from tests.infrastructure.parser.parser_support import (
    US_DAY,
    BytesStorage,
    archive_for,
    csv_bytes,
    day_start,
    expect_rejection,
    kline_rows,
    make_case,
    object_key,
)

KLINES = "klines_1m"


def _published(tmp_path: Path, rows: int) -> tuple[LocalFileStorageAdapter, ArchiveParseRequest]:
    warehouse = tmp_path / "warehouse"
    (warehouse / "staging").mkdir(parents=True)
    storage = LocalFileStorageAdapter(warehouse.as_uri(), (warehouse / "staging").as_uri())
    data = archive_for(KLINES, "BTCUSDT", US_DAY, csv_bytes(kline_rows(US_DAY, rows)))
    staged = storage.stage(
        StageRequest(
            key=object_key(KLINES, "BTCUSDT", US_DAY),
            expected_sha256=hashlib.sha256(data).hexdigest(),
        ),
        [data],
    )
    ref = storage.publish(staged).ref
    return storage, ArchiveParseRequest(
        archive_revision_id="archive-rev-1",
        data_type=KLINES,
        symbol="BTCUSDT",
        coverage_start=day_start(US_DAY),
        coverage_end=day_start(US_DAY) + timedelta(days=1),
        object_ref=ref,
    )


def test_a_spool_of_a_published_object_holds_its_parse(tmp_path: Path) -> None:
    storage, request = _published(tmp_path, 50)
    spools = tmp_path / "spools"
    spools.mkdir()
    parsed = parse_archive(request, storage)
    assert isinstance(parsed, ParsedArchive)
    spooled = spool_archive(request, storage, directory=spools, chunk_rows=7)
    assert isinstance(spooled, SpooledArchive)
    with spooled:
        assert spooled.row_count == 50
        assert spooled.take(range(50)).equals(parsed.rows)
        # Arbitrary, repeated and cross-block indices come back in the asked order.
        wanted = [49, 0, 6, 7, 7, 13, 48]
        assert spooled.take(wanted).equals(parsed.rows.take(pa.array(wanted)))
        assert spooled.take([]).num_rows == 0
        with pytest.raises(IndexError):
            spooled.take([50])
        with pytest.raises(IndexError):
            spooled.take([-1])
        [directory] = spools.iterdir()
        # Only the rows and their block index stay: the object copy is gone.
        assert sorted(item.name for item in directory.iterdir()) == ["rows.arrows", "rows.index"]
    assert not any(spools.iterdir())
    assert spooled.closed
    with pytest.raises(ValueError, match="closed"):
        spooled.take([0])


def test_a_spool_reads_only_the_blocks_it_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``take`` of a window reads the blocks the window falls in, each once."""
    storage, request = _published(tmp_path, 50)
    spooled = spool_archive(request, storage, directory=tmp_path, chunk_rows=8)
    assert isinstance(spooled, SpooledArchive)
    read: list[int] = []
    original = SpooledArchive._chunk

    def logged(self: SpooledArchive, chunk: int) -> pa.Table:
        read.append(chunk)
        table = original(self, chunk)
        assert table.num_rows <= 8
        return table

    monkeypatch.setattr(SpooledArchive, "_chunk", logged)
    assert spooled.take(range(10, 20)).num_rows == 10
    assert read == [1, 2]
    spooled.close()


def test_a_tampered_spool_block_is_refused(tmp_path: Path) -> None:
    storage, request = _published(tmp_path, 20)
    spooled = spool_archive(request, storage, directory=tmp_path, chunk_rows=8)
    assert isinstance(spooled, SpooledArchive)
    [directory] = [item for item in tmp_path.iterdir() if item.name.startswith("hlens-archive")]
    index = directory / "rows.index"
    entries = bytearray(index.read_bytes())
    size = len(entries) // 3
    # Block 0's entry now names block 1's bytes (same row count, same schema, other rows).
    entries[0:size] = entries[size : 2 * size]
    index.write_bytes(bytes(entries))
    with pytest.raises(ValueError, match="does not hold its spooled bytes"):
        spooled.take([0])
    assert spooled.take([8]).num_rows == 1  # other blocks still read
    rows = directory / "rows.arrows"
    data = bytearray(rows.read_bytes())
    data[-5] ^= 0xFF
    rows.write_bytes(bytes(data))
    with pytest.raises(ValueError, match="does not hold its spooled bytes"):
        spooled.take([19])
    spooled.close()


def test_a_refused_or_failing_spool_leaves_nothing(tmp_path: Path) -> None:
    storage, request = _published(tmp_path, 5)
    spools = tmp_path / "spools"
    spools.mkdir()
    forged = ArchiveParseRequest(
        archive_revision_id=request.archive_revision_id,
        data_type=request.data_type,
        symbol=request.symbol,
        coverage_start=request.coverage_start,
        coverage_end=request.coverage_end,
        object_ref=request.object_ref.model_copy(update={"sha256": "0" * 64}),
    )
    assert expect_rejection(parse_archive(forged, storage)) == expect_rejection(
        spool_archive(forged, storage, directory=spools)  # type: ignore[arg-type]
    )
    assert not any(spools.iterdir())
    empty = tmp_path / "empty"
    (empty / "staging").mkdir(parents=True)
    nothing = LocalFileStorageAdapter(empty.as_uri(), (empty / "staging").as_uri())
    missing = ObjectRef(
        key=request.object_ref.key,
        uri=(empty / "missing.zip").as_uri(),
        sha256="a" * 64,
        size=10,
    )
    with pytest.raises(ObjectNotFound):
        spool_archive(
            ArchiveParseRequest(
                archive_revision_id="archive-rev-1",
                data_type=KLINES,
                symbol="BTCUSDT",
                coverage_start=request.coverage_start,
                coverage_end=request.coverage_end,
                object_ref=missing,
            ),
            nothing,
            directory=spools,
        )
    assert not any(spools.iterdir())


def test_the_spool_rehashes_what_storage_returns(tmp_path: Path) -> None:
    good = archive_for(KLINES, "BTCUSDT", US_DAY, csv_bytes(kline_rows(US_DAY, 5)))
    evil = archive_for(KLINES, "BTCUSDT", US_DAY, csv_bytes(kline_rows(US_DAY, 6)))
    case = make_case(KLINES, US_DAY, good)
    for served in (evil, good + b"x", good[:-1]):
        rejection = expect_rejection(
            spool_archive(case.request, BytesStorage(served), directory=tmp_path)  # type: ignore[arg-type]
        )
        assert rejection.code is RejectionCode.OBJECT_INTEGRITY_MISMATCH
    assert not any(tmp_path.iterdir())
    oversized = make_case(KLINES, US_DAY, b"x", size=(1 << 30) + 1)
    storage = BytesStorage(b"x")
    rejection = expect_rejection(
        spool_archive(oversized.request, storage, directory=tmp_path)  # type: ignore[arg-type]
    )
    assert rejection.code is RejectionCode.ARCHIVE_TOO_LARGE and storage.opened == 0


def test_the_spool_block_size_must_be_a_positive_int(tmp_path: Path) -> None:
    case = make_case(KLINES, US_DAY, b"x")
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="chunk_rows"):
            spool_archive(
                case.request,
                BytesStorage(b"x"),  # type: ignore[arg-type]
                directory=tmp_path,
                chunk_rows=bad,  # type: ignore[arg-type]
            )
    assert archive_spool.DEFAULT_SPOOL_CHUNK_ROWS > 0

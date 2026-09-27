"""D1 parser over real ``LocalFileStorageAdapter`` objects, D0 → D1 hand-off and C3 column fit."""

from __future__ import annotations

import ast
import hashlib
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import httpx
import pyarrow as pa  # type: ignore[import-untyped]
import pytest
from pyiceberg.io.pyarrow import schema_to_pyarrow

from core.contracts.collector import CollectionRequest
from core.contracts.storage import (
    ObjectNotFound,
    ObjectRef,
    PublishResult,
    StagedObject,
    StageRequest,
)
from infrastructure.catalog.definitions import RegisteredTableDefinition
from infrastructure.catalog.phase1_tables import BINANCE_SPOT_AGG_TRADES, BINANCE_SPOT_KLINES_1M
from infrastructure.collector import ARCHIVE_SOURCE, BinanceSpotArchiveCollector
from infrastructure.parser import (
    AGG_TRADES_ROW_SCHEMA,
    KLINES_1M_ROW_SCHEMA,
    ArchiveParseRequest,
    ParsedArchive,
    RejectionCode,
    parse_archive,
    parse_archive_bytes,
)
from infrastructure.parser import binance_archive as parser_mod
from infrastructure.storage import LocalFileStorageAdapter
from tests.infrastructure.parser.parser_support import (
    MS_DAY,
    US_DAY,
    agg_rows,
    archive_for,
    csv_bytes,
    day_start,
    expect_rejection,
    kline_rows,
    make_case,
    object_key,
)

KLINES = "klines_1m"


def _storage(tmp_path: Path) -> LocalFileStorageAdapter:
    warehouse = tmp_path / "warehouse"
    (warehouse / "staging").mkdir(parents=True)
    return LocalFileStorageAdapter(warehouse.as_uri(), (warehouse / "staging").as_uri())


def _publish(storage: LocalFileStorageAdapter, key: str, data: bytes) -> ObjectRef:
    staged = storage.stage(
        StageRequest(key=key, expected_sha256=hashlib.sha256(data).hexdigest()), [data]
    )
    return storage.publish(staged).ref


def _request(ref: ObjectRef) -> ArchiveParseRequest:
    return ArchiveParseRequest(
        archive_revision_id="archive-rev-1",
        data_type=KLINES,
        symbol="BTCUSDT",
        coverage_start=day_start(US_DAY),
        coverage_end=day_start(US_DAY) + timedelta(days=1),
        object_ref=ref,
    )


def test_parse_from_storage_equals_parse_from_bytes(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    data = archive_for(KLINES, "BTCUSDT", US_DAY, csv_bytes(kline_rows(US_DAY, 5)))
    ref = _publish(storage, object_key(KLINES, "BTCUSDT", US_DAY), data)
    request = _request(ref)
    from_storage = parse_archive(request, storage)
    assert isinstance(from_storage, ParsedArchive)
    assert from_storage == parse_archive_bytes(request, data)
    assert from_storage == parse_archive(request, storage)
    assert from_storage.object_ref == ref


def test_ref_disagreeing_with_stored_object_is_an_integrity_rejection(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    data = archive_for(KLINES, "BTCUSDT", US_DAY, csv_bytes(kline_rows(US_DAY, 5)))
    ref = _publish(storage, object_key(KLINES, "BTCUSDT", US_DAY), data)
    forged = ref.model_copy(update={"sha256": "0" * 64})
    rejection = expect_rejection(parse_archive(_request(forged), storage))
    assert rejection.code is RejectionCode.OBJECT_INTEGRITY_MISMATCH


def test_missing_object_is_a_storage_error_not_a_quality_event(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    ref = ObjectRef(
        key=object_key(KLINES, "BTCUSDT", US_DAY),
        uri=(tmp_path / "warehouse" / "missing.zip").as_uri(),
        sha256="a" * 64,
        size=10,
    )
    with pytest.raises(ObjectNotFound):
        parse_archive(_request(ref), storage)


class _LyingStorage:
    """``open_read`` returns bytes other than those the ref describes (broken adapter)."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.opened = 0

    def stage(self, request: StageRequest, content: object) -> StagedObject:
        raise NotImplementedError

    def publish(self, staged: StagedObject) -> PublishResult:
        raise NotImplementedError

    def lookup(self, key: str) -> ObjectRef | None:
        raise NotImplementedError

    def open_read(self, ref: ObjectRef) -> BinaryIO:
        self.opened += 1
        return io.BytesIO(self.payload)


def test_parser_rehashes_what_storage_returns() -> None:
    good = archive_for(KLINES, "BTCUSDT", US_DAY, csv_bytes(kline_rows(US_DAY, 5)))
    evil = archive_for(KLINES, "BTCUSDT", US_DAY, csv_bytes(kline_rows(US_DAY, 6)))
    case = make_case(KLINES, US_DAY, good)
    storage = _LyingStorage(evil)
    rejection = expect_rejection(parse_archive(case.request, storage))
    assert rejection.code is RejectionCode.OBJECT_INTEGRITY_MISMATCH
    longer = _LyingStorage(good + b"x")
    assert (
        expect_rejection(parse_archive(case.request, longer)).code
        is RejectionCode.OBJECT_INTEGRITY_MISMATCH
    )


def test_oversized_ref_is_rejected_without_opening_storage() -> None:
    case = make_case(KLINES, US_DAY, b"x", size=(1 << 30) + 1)
    storage = _LyingStorage(b"x")
    rejection = expect_rejection(parse_archive(case.request, storage))
    assert rejection.code is RejectionCode.ARCHIVE_TOO_LARGE
    assert storage.opened == 0


def test_d0_collected_object_hands_off_to_parser(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    content = csv_bytes(agg_rows(MS_DAY, 4))
    payload = archive_for("agg_trades", "ETHUSDT", MS_DAY, content)
    filename = f"ETHUSDT-aggTrades-{MS_DAY.isoformat()}.zip"
    path = f"data/spot/daily/aggTrades/ETHUSDT/{filename}"
    files = {
        path: payload,
        f"{path}.CHECKSUM": f"{hashlib.sha256(payload).hexdigest()}  {filename}\n".encode(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = files.get(request.url.path.lstrip("/"))
        return httpx.Response(404 if body is None else 200, content=body or b"")

    with BinanceSpotArchiveCollector(
        storage,
        archive_base_url="https://archive.test",
        http_connect_timeout_seconds=1.0,
        http_read_timeout_seconds=1.0,
        http_max_retries=0,
        http_user_agent="hlens-d1-test/0.0.0",
        http_transport=httpx.MockTransport(handler),
        clock=lambda: datetime(2026, 9, 24, tzinfo=UTC),
    ) as collector:
        result = collector.collect(
            CollectionRequest(
                request_id="d1-handoff",
                source=ARCHIVE_SOURCE,
                data_type="agg_trades",
                symbols=("ETHUSDT",),
                coverage_start=day_start(MS_DAY),
                coverage_end=day_start(US_DAY),
            )
        )
    (collected,) = result.objects
    request = ArchiveParseRequest.for_collected_object(
        collected, data_type=result.request.data_type, archive_revision_id="archive-rev-7"
    )
    parsed = parse_archive(request, storage)
    assert isinstance(parsed, ParsedArchive)
    assert (parsed.symbol, parsed.row_count, parsed.archive_revision_id) == (
        "ETHUSDT",
        4,
        "archive-rev-7",
    )
    assert parsed.object_ref == collected.ref


@pytest.mark.parametrize(
    ("row_schema", "definition"),
    [
        (AGG_TRADES_ROW_SCHEMA, BINANCE_SPOT_AGG_TRADES),
        (KLINES_1M_ROW_SCHEMA, BINANCE_SPOT_KLINES_1M),
    ],
)
def test_row_columns_match_the_frozen_c3_raw_tables(
    row_schema: pa.Schema, definition: RegisteredTableDefinition
) -> None:
    """Every parser column exists in the C3 Raw table with the same Arrow type (no cast in D2)."""
    table_schema = schema_to_pyarrow(definition.schema)
    for row_field in row_schema:
        table_field = table_schema.field(row_field.name)
        assert row_field.type == table_field.type, row_field.name
        assert not row_field.nullable


def test_parser_module_has_no_network_clock_or_randomness() -> None:
    tree = ast.parse(Path(parser_mod.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {"httpx", "socket", "urllib", "random", "secrets", "time", "os", "requests"}
    assert not {name.split(".")[0] for name in imported} & forbidden
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attributes & {"now", "utcnow", "today", "time_ns", "monotonic"}

"""Binance 公共现货日归档 fail-closed parser（Phase 1 D1；ADR-0022 §2 / §3；03-data.md §7.3）。

``binance.spot.archive.parser@1.0.0``：把 D0 已校验并发布的日归档 ZIP 解析为与 C3 Raw 表列同名、
同类型的原生字段列（``pyarrow.Table``），外加原始行号与按单位换算后的时间列。

规则（全部写入 ``PARSER_SPEC``，其规范 JSON 的 SHA-256 即 ``PARSER_BINDING.policy_hash``）：

- 只支持 ``BTCUSDT`` / ``ETHUSDT`` × ``agg_trades`` / ``klines_1m`` × 一个 UTC 整日半开区间。
- 时间单位只由 ``data_type`` + 覆盖日 + parser 版本一次性决定：覆盖日早于
  ``2025-01-01T00:00:00Z`` 为毫秒，自该日起为微秒；不逐值猜测数量级。
- aggTrades 事件时间与 kline 开盘时间必须落在 ``[coverage_start, coverage_end)``，零容差；
  kline 开盘对齐整分钟，收盘时间 = 开盘 + 1 分钟 - 1 tick。
- ZIP / CSV 都是不可信输入：结构、成员、编码、换行、列数、字段文法、行内与跨行不变量任一
  不符，整个文件拒绝（``ArchiveRejection``），不返回任何行。
- 只有整个成员读到 EOF（CRC 已校验）且全部检查通过后才构造 ``ParsedArchive``。

**诚实边界（本批不做）**：不构造 revision / ``observation_key`` / ``arrival_seq``、不写 Iceberg、
不持久化质量报告（``ArchiveRejection.quality_event()`` 只产出可持久化的结构）、不做缺口
（缺失分钟 / aggTrade id 间隙）判定、不访问网络。
"""

from __future__ import annotations

import hashlib
import io
import re
import stat
import struct
import tempfile
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import IO, Any, Final, Self, cast

import pyarrow as pa  # type: ignore[import-untyped]

from core.contracts.collector import CollectedObject
from core.contracts.revision import PolicyBinding, PolicyRole
from core.contracts.storage import IntegrityViolation, ObjectRef, StorageAdapter, StorageError
from core.domain.base import canonical_json
from infrastructure.contract_version import PHASE1_PUBLICATION_VERSION

__all__ = [
    "AGG_TRADES_ROW_SCHEMA",
    "ARCHIVE_EVENT_TABLE",
    "ArchiveParseRequest",
    "ArchiveRejection",
    "KLINES_1M_ROW_SCHEMA",
    "MICROSECOND_FROM",
    "PARSER_BINDING",
    "PARSER_ID",
    "PARSER_SPEC",
    "PARSER_VERSION",
    "ParseOutcome",
    "ParsedArchive",
    "ParserQualityEvent",
    "RejectionCode",
    "SUPPORTED_DATA_TYPES",
    "SUPPORTED_SYMBOLS",
    "TimeUnit",
    "UnsupportedArchiveRequest",
    "archive_filename",
    "member_filename",
    "parse_archive",
    "parse_archive_bytes",
    "time_unit_for",
]

PARSER_ID: Final = "binance.spot.archive.parser"
PARSER_VERSION: Final = "1.0.0"
SUPPORTED_SYMBOLS: Final[tuple[str, ...]] = ("BTCUSDT", "ETHUSDT")
SUPPORTED_DATA_TYPES: Final[tuple[str, ...]] = ("agg_trades", "klines_1m")
#: 覆盖起点不早于此时刻的归档使用微秒；更早的使用毫秒（Binance 官方说明）。
MICROSECOND_FROM: Final = datetime(2025, 1, 1, tzinfo=UTC)
#: 质量事件所关注的表：被拒绝的是 ``raw.binance_spot_archives`` 的一个归档 revision。
ARCHIVE_EVENT_TABLE: Final = "raw.binance_spot_archives"

# 资源上限（属于 parser 版本；变化 = 新 parser 版本）。
_MAX_ARCHIVE_BYTES: Final = 1 << 30  # 压缩归档 1 GiB
_MAX_MEMBER_BYTES: Final = 3 << 30  # 解压后 CSV 3 GiB
_MAX_COMPRESSION_RATIO: Final = 50  # 解压字节 / 压缩字节
_MAX_LINE_BYTES: Final = 1024  # 不含结尾 LF
_READ_CHUNK_BYTES: Final = 1 << 20
_MAX_DETAIL_CHARS: Final = 240
_CHUNK_ROWS: Final = 65_536

_INT64_MAX: Final = (1 << 63) - 1
_DECIMAL_PRECISION: Final = 38
_DECIMAL_SCALE: Final = 18
_INTEGER_PATTERN: Final = r"0|[1-9][0-9]*"
_DECIMAL_PATTERN: Final = (
    rf"(?:0|[1-9][0-9]{{0,{_DECIMAL_PRECISION - _DECIMAL_SCALE - 1}}})"
    rf"(?:\.[0-9]{{1,{_DECIMAL_SCALE}}})?"
)
_INTEGER_RE: Final = re.compile(_INTEGER_PATTERN)
_DECIMAL_RE: Final = re.compile(_DECIMAL_PATTERN)
_DECIMAL_SHAPE_RE: Final = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_BOOLEANS: Final[dict[str, bool]] = {"True": True, "False": False}

# ZIP：允许的通用位标志（压缩选项 1/2、data descriptor、UTF-8 文件名）与压缩方法。
_ALLOWED_ZIP_FLAGS: Final = 0x0002 | 0x0004 | 0x0008 | 0x0800
_ENCRYPTION_FLAGS: Final = 0x0001 | 0x0040 | 0x2000
_ALLOWED_COMPRESSION: Final[frozenset[int]] = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_LOCAL_HEADER_SIG: Final = b"PK\x03\x04"
_EOCD_SIG: Final = b"PK\x05\x06"
_EOCD_SIZE: Final = 22
_LOCAL_HEADER_SIZE: Final = 30
_DATA_DESCRIPTOR_SIG: Final = b"PK\x07\x08"
_FORBIDDEN_EXTRA_IDS: Final[frozenset[int]] = frozenset({0x0001, 0x9901})  # zip64, AES


class TimeUnit(StrEnum):
    """归档原生时间戳单位；每 tick 的微秒数由 ``micros_per_tick`` 给出。"""

    MILLISECOND = "millisecond"
    MICROSECOND = "microsecond"

    @property
    def ticks_per_second(self) -> int:
        return 1_000 if self is TimeUnit.MILLISECOND else 1_000_000

    @property
    def micros_per_tick(self) -> int:
        return 1_000 if self is TimeUnit.MILLISECOND else 1


class RejectionCode(StrEnum):
    """稳定的拒绝原因；值是持久化的 token，只增不改。"""

    OBJECT_KEY_MISMATCH = "object_key_mismatch"
    OBJECT_INTEGRITY_MISMATCH = "object_integrity_mismatch"
    ARCHIVE_TOO_LARGE = "archive_too_large"
    ZIP_CORRUPT = "zip_corrupt"
    ZIP_LAYOUT_UNSUPPORTED = "zip_layout_unsupported"
    ZIP_MEMBER_COUNT = "zip_member_count"
    ZIP_MEMBER_NAME_MISMATCH = "zip_member_name_mismatch"
    ZIP_MEMBER_NOT_REGULAR_FILE = "zip_member_not_regular_file"
    ZIP_MEMBER_ENCRYPTED = "zip_member_encrypted"
    ZIP_MEMBER_FLAGS_UNSUPPORTED = "zip_member_flags_unsupported"
    ZIP_COMPRESSION_UNSUPPORTED = "zip_compression_unsupported"
    ZIP_MEMBER_TOO_LARGE = "zip_member_too_large"
    ZIP_COMPRESSION_RATIO_EXCEEDED = "zip_compression_ratio_exceeded"
    ZIP_MEMBER_CORRUPT = "zip_member_corrupt"
    NON_ASCII_CONTENT = "non_ascii_content"
    INVALID_LINE_ENDING = "invalid_line_ending"
    EMPTY_LINE = "empty_line"
    LINE_TOO_LONG = "line_too_long"
    NO_ROWS = "no_rows"
    COLUMN_COUNT = "column_count"
    INVALID_INTEGER = "invalid_integer"
    INTEGER_OUT_OF_RANGE = "integer_out_of_range"
    INVALID_DECIMAL = "invalid_decimal"
    DECIMAL_OUT_OF_RANGE = "decimal_out_of_range"
    INVALID_BOOLEAN = "invalid_boolean"
    NON_POSITIVE_PRICE = "non_positive_price"
    NON_POSITIVE_QUANTITY = "non_positive_quantity"
    TRADE_ID_RANGE_INVALID = "trade_id_range_invalid"
    OHLC_INVARIANT = "ohlc_invariant"
    TAKER_VOLUME_INVARIANT = "taker_volume_invariant"
    TIMESTAMP_OUT_OF_COVERAGE = "timestamp_out_of_coverage"
    KLINE_OPEN_NOT_ALIGNED = "kline_open_not_aligned"
    KLINE_CLOSE_TIME_MISMATCH = "kline_close_time_mismatch"
    DUPLICATE_ROW = "duplicate_row"
    OUT_OF_ORDER = "out_of_order"
    TIMESTAMP_DECREASING = "timestamp_decreasing"
    TRADE_ID_OVERLAP = "trade_id_overlap"


class UnsupportedArchiveRequest(ValueError):
    """调用方请求超出 parser 1.0.0 的支持范围（不是归档质量事件）。"""


# --------------------------------------------------------------------------- layouts


_TS = pa.timestamp("us", tz="UTC")
_DEC = pa.decimal128(_DECIMAL_PRECISION, _DECIMAL_SCALE)

#: aggTrades CSV 固定 8 列（官方 spot 布局）；列名与 ``raw.binance_spot_agg_trades`` 相同。
_AGG_TRADES_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("agg_trade_id", "integer"),
    ("price", "decimal"),
    ("quantity", "decimal"),
    ("first_trade_id", "integer"),
    ("last_trade_id", "integer"),
    ("timestamp_raw", "integer"),
    ("is_buyer_maker", "boolean"),
    ("is_best_match", "boolean"),
)
#: klines CSV 固定 12 列（官方 spot 布局）；列名与 ``raw.binance_spot_klines_1m`` 相同。
_KLINES_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("open_time_raw", "integer"),
    ("open", "decimal"),
    ("high", "decimal"),
    ("low", "decimal"),
    ("close", "decimal"),
    ("volume", "decimal"),
    ("close_time_raw", "integer"),
    ("quote_asset_volume", "decimal"),
    ("number_of_trades", "integer"),
    ("taker_buy_base_asset_volume", "decimal"),
    ("taker_buy_quote_asset_volume", "decimal"),
    ("ignore_raw", "decimal_text"),
)

_KIND_TYPES: Final[dict[str, pa.DataType]] = {
    "integer": pa.int64(),
    "decimal": _DEC,
    "boolean": pa.bool_(),
    "decimal_text": pa.large_string(),
}


def _row_schema(
    columns: tuple[tuple[str, str], ...], derived: tuple[tuple[str, pa.DataType], ...]
) -> pa.Schema:
    fields = [pa.field("archive_line_number", pa.int64(), nullable=False)]
    fields += [pa.field(name, _KIND_TYPES[kind], nullable=False) for name, kind in columns]
    fields += [pa.field(name, data_type, nullable=False) for name, data_type in derived]
    return pa.schema(fields)


#: 成功结果的列：原始行号 + 原生字段（C3 列名 / 类型）+ 按单位换算的 UTC 时间。
AGG_TRADES_ROW_SCHEMA: Final = _row_schema(_AGG_TRADES_COLUMNS, (("event_time", _TS),))
KLINES_1M_ROW_SCHEMA: Final = _row_schema(
    _KLINES_COLUMNS, (("interval_start", _TS), ("interval_end", _TS))
)


def archive_filename(data_type: str, symbol: str, day: date) -> str:
    """官方日归档文件名（亦为 D0 object key 的 basename）。"""
    return f"{_file_stem(data_type, symbol, day)}.zip"


def member_filename(data_type: str, symbol: str, day: date) -> str:
    """归档内唯一 CSV 成员的文件名。"""
    return f"{_file_stem(data_type, symbol, day)}.csv"


def _file_stem(data_type: str, symbol: str, day: date) -> str:
    if data_type == "agg_trades":
        return f"{symbol}-aggTrades-{day.isoformat()}"
    if data_type == "klines_1m":
        return f"{symbol}-1m-{day.isoformat()}"
    raise UnsupportedArchiveRequest(f"unsupported data_type {data_type!r}")


def time_unit_for(data_type: str, coverage_start: datetime) -> TimeUnit:
    """parser 1.0.0 的单位规则：只看数据类型与覆盖起点，不看任何数值。"""
    if data_type not in SUPPORTED_DATA_TYPES:
        raise UnsupportedArchiveRequest(f"unsupported data_type {data_type!r}")
    if coverage_start.tzinfo is None or coverage_start.utcoffset() != timedelta(0):
        raise UnsupportedArchiveRequest("coverage_start must be timezone-aware UTC")
    return TimeUnit.MICROSECOND if coverage_start >= MICROSECOND_FROM else TimeUnit.MILLISECOND


# --------------------------------------------------------------------------- spec / binding


PARSER_SPEC: Final[dict[str, Any]] = {
    "parser_id": PARSER_ID,
    "version": PARSER_VERSION,
    "source": {"source_id": "binance.public.spot.archive", "version": "1.0.0"},
    "symbols": list(SUPPORTED_SYMBOLS),
    "coverage": {"granularity": "utc_day", "interval": "half_open", "tolerance_ticks": 0},
    "time_unit": {
        "decided_by": ["data_type", "coverage_start", "parser_version"],
        "before": TimeUnit.MILLISECOND.value,
        "from": TimeUnit.MICROSECOND.value,
        "switch_at": "2025-01-01T00:00:00Z",
        "per_value_magnitude_guessing": False,
    },
    "archive": {
        "max_archive_bytes": _MAX_ARCHIVE_BYTES,
        "max_member_bytes": _MAX_MEMBER_BYTES,
        "max_compression_ratio": _MAX_COMPRESSION_RATIO,
        "members": 1,
        "member": "regular file named {stem}.csv (unix file type 0 or S_IFREG, no DOS dir bit)",
        "member_integrity": "local header, data descriptor and directory agree; "
        "byte count and CRC-32 re-checked independently",
        "compression_methods": sorted(_ALLOWED_COMPRESSION),
        "allowed_general_purpose_flags": _ALLOWED_ZIP_FLAGS,
        "layout": "local header at 0; data (+ optional descriptor) then central directory; "
        "22-byte EOCD at end; no comment, zip64 record, prefix or trailing bytes",
        "forbidden_extra_field_ids": sorted(_FORBIDDEN_EXTRA_IDS),
    },
    "csv": {
        "encoding": "ascii",
        "line_terminator": "LF",
        "final_line_terminator": "required",
        "carriage_return": "reject",
        "empty_lines": "reject",
        "header": "reject",
        "quoting": "none",
        "max_line_bytes": _MAX_LINE_BYTES,
        "min_rows": 1,
    },
    "grammar": {
        "integer": _INTEGER_PATTERN,
        "integer_max": _INT64_MAX,
        "decimal": _DECIMAL_PATTERN,
        "decimal_type": f"decimal({_DECIMAL_PRECISION},{_DECIMAL_SCALE})",
        "boolean": sorted(_BOOLEANS),
    },
    "data_types": {
        "agg_trades": {
            "archive": "{symbol}-aggTrades-{yyyy-mm-dd}.zip",
            "columns": [list(column) for column in _AGG_TRADES_COLUMNS],
            "row_rules": [
                "price > 0",
                "quantity > 0",
                "first_trade_id <= last_trade_id",
                "coverage_start <= timestamp < coverage_end",
            ],
            "cross_row_rules": [
                "agg_trade_id strictly increasing (equal = duplicate_row)",
                "timestamp non-decreasing",
                "first_trade_id > previous last_trade_id",
            ],
            "derived": {"event_time": "timestamp_raw in time_unit"},
        },
        "klines_1m": {
            "archive": "{symbol}-1m-{yyyy-mm-dd}.zip",
            "columns": [list(column) for column in _KLINES_COLUMNS],
            "row_rules": [
                "open, high, low, close > 0",
                "low <= min(open, close) and high >= max(open, close)",
                "taker_buy_base_asset_volume <= volume",
                "taker_buy_quote_asset_volume <= quote_asset_volume",
                "coverage_start <= open_time < coverage_end",
                "open_time aligned to a whole minute",
                "close_time == open_time + 1 minute - 1 tick",
            ],
            "cross_row_rules": ["open_time strictly increasing (equal = duplicate_row)"],
            "derived": {
                "interval_start": "open_time_raw in time_unit",
                "interval_end": "interval_start + 1 minute (exclusive)",
            },
        },
    },
}

#: ``PolicyBinding(role=parser)``：哈希由 ``PARSER_SPEC`` 的规范 JSON 派生。
PARSER_BINDING: Final = PolicyBinding(
    schema_version=PHASE1_PUBLICATION_VERSION,
    role=PolicyRole.PARSER,
    policy_id=PARSER_ID,
    version=PARSER_VERSION,
    policy_hash=hashlib.sha256(canonical_json(PARSER_SPEC).encode("utf-8")).hexdigest(),
)


# --------------------------------------------------------------------------- request / outcome


@dataclass(frozen=True, slots=True)
class ArchiveParseRequest:
    """一次解析请求：要解析哪个已发布对象，以及它声称代表哪个归档 revision。"""

    archive_revision_id: str
    data_type: str
    symbol: str
    coverage_start: datetime
    coverage_end: datetime
    object_ref: ObjectRef

    def __post_init__(self) -> None:
        revision = self.archive_revision_id
        if (
            not isinstance(revision, str)
            or not revision
            or len(revision) > 256
            or not revision.isascii()
            or not revision.isprintable()
            or revision != revision.strip()
        ):
            raise UnsupportedArchiveRequest(
                "archive_revision_id must be 1-256 printable ASCII characters without "
                "surrounding whitespace"
            )
        if self.data_type not in SUPPORTED_DATA_TYPES:
            raise UnsupportedArchiveRequest(f"unsupported data_type {self.data_type!r}")
        if self.symbol not in SUPPORTED_SYMBOLS:
            raise UnsupportedArchiveRequest(f"unsupported symbol {self.symbol!r}")
        for label, value in (
            ("coverage_start", self.coverage_start),
            ("coverage_end", self.coverage_end),
        ):
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise UnsupportedArchiveRequest(f"{label} must be timezone-aware UTC")
            if value.utcoffset() != timedelta(0):
                raise UnsupportedArchiveRequest(f"{label} must be UTC")
        start = self.coverage_start
        if (start.hour, start.minute, start.second, start.microsecond) != (0, 0, 0, 0):
            raise UnsupportedArchiveRequest("coverage_start must be a UTC midnight")
        if self.coverage_end - start != timedelta(days=1):
            raise UnsupportedArchiveRequest("coverage must be exactly one UTC day")
        if not isinstance(self.object_ref, ObjectRef):
            raise UnsupportedArchiveRequest("object_ref must be an ObjectRef")

    @classmethod
    def for_collected_object(
        cls, collected: CollectedObject, *, data_type: str, archive_revision_id: str
    ) -> Self:
        """由 D0 ``CollectedObject`` 机械构造（D2 负责给出 ``archive_revision_id``）。"""
        return cls(
            archive_revision_id=archive_revision_id,
            data_type=data_type,
            symbol=collected.symbol,
            coverage_start=collected.coverage_start,
            coverage_end=collected.coverage_end,
            object_ref=collected.ref,
        )

    @property
    def coverage_day(self) -> date:
        return self.coverage_start.astimezone(UTC).date()

    @property
    def time_unit(self) -> TimeUnit:
        return time_unit_for(self.data_type, self.coverage_start)


@dataclass(frozen=True, slots=True)
class ParserQualityEvent:
    """与 ``quality.data_quality_reports.events`` 元素逐字段对应的可持久化事件（E3 写入）。"""

    event_id: str
    event_type: str
    table: str
    observation_key: str | None
    revision_ids: tuple[str, ...]
    event_start: datetime
    event_end: datetime
    detail: str


@dataclass(frozen=True, slots=True)
class ArchiveRejection:
    """整文件拒绝：稳定原因码 + 定位信息；不含任何原始行内容。"""

    parser: PolicyBinding
    archive_revision_id: str
    data_type: str
    symbol: str
    coverage_start: datetime
    coverage_end: datetime
    object_key: str
    object_sha256: str
    code: RejectionCode
    detail: str
    line_number: int | None = None
    column: str | None = None

    def quality_event(self) -> ParserQualityEvent:
        location = ""
        if self.line_number is not None:
            location = f" at line {self.line_number}"
            if self.column is not None:
                location += f" column {self.column}"
        detail = f"{self.code.value}{location}: {self.detail}"[:_MAX_DETAIL_CHARS]
        identity = {
            "parser_id": self.parser.policy_id,
            "parser_version": self.parser.version,
            "parser_hash": self.parser.policy_hash,
            "archive_revision_id": self.archive_revision_id,
            "object_sha256": self.object_sha256,
            "code": self.code.value,
            "line_number": self.line_number,
            "column": self.column,
        }
        digest = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
        return ParserQualityEvent(
            event_id=f"{PARSER_ID}.rejection.{digest}",
            event_type="archive_parse_rejected",
            table=ARCHIVE_EVENT_TABLE,
            observation_key=None,
            revision_ids=(self.archive_revision_id,),
            event_start=self.coverage_start,
            event_end=self.coverage_end,
            detail=detail,
        )


@dataclass(frozen=True, eq=False)
class ParsedArchive:
    """整文件成功解析的结果；只在所有行与跨行检查通过后构造。

    相等 = 全部绑定字段相等且 ``rows`` 的 Schema 与逐列值相等（``pyarrow.Table.equals``）。
    """

    parser: PolicyBinding
    archive_revision_id: str
    data_type: str
    symbol: str
    coverage_start: datetime
    coverage_end: datetime
    object_ref: ObjectRef
    member_name: str
    time_unit: TimeUnit
    rows: pa.Table

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ParsedArchive):
            return NotImplemented
        return (
            self._identity() == other._identity()
            and self.rows.schema.equals(other.rows.schema, check_metadata=True)
            and self.rows.equals(other.rows)
        )

    __hash__ = None  # type: ignore[assignment]

    def _identity(self) -> tuple[object, ...]:
        return (
            self.parser,
            self.archive_revision_id,
            self.data_type,
            self.symbol,
            self.coverage_start,
            self.coverage_end,
            self.object_ref,
            self.member_name,
            self.time_unit,
        )

    @property
    def row_count(self) -> int:
        return int(self.rows.num_rows)


type ParseOutcome = ParsedArchive | ArchiveRejection


class _Reject(Exception):
    """内部控制流：携带拒绝原因离开解析循环。"""

    def __init__(
        self,
        code: RejectionCode,
        detail: str,
        *,
        line_number: int | None = None,
        column: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.line_number = line_number
        self.column = column


# --------------------------------------------------------------------------- entry points


def parse_archive(request: ArchiveParseRequest, storage: StorageAdapter) -> ParseOutcome:
    """经 ``StorageAdapter.open_read`` 读取已发布对象并解析。

    对象缺失等存储故障，以及本地输入 / 临时 spool I/O 故障，均作为可重试的 ``StorageError``
    抛出，不是质量事件；对象与 ``ObjectRef`` 不一致（``IntegrityViolation``）作为
    ``object_integrity_mismatch`` 拒绝。
    """
    try:
        _check_request_identity(request)
        # ZIP needs random access. Avoid keeping a second full Python ``bytes`` object alongside
        # the complete ParsedArchive table: copy the verified stream into a seekable temporary
        # file in fixed-size reads. The configured temporary filesystem may still be memory-backed.
        try:
            handle = storage.open_read(request.object_ref)
        except IntegrityViolation as exc:
            raise _Reject(
                RejectionCode.OBJECT_INTEGRITY_MISMATCH,
                f"storage refused the object: {type(exc).__name__}",
            ) from exc
        with handle:
            with tempfile.TemporaryFile(mode="w+b") as spool:
                byte_count, digest = _spool_bounded(
                    handle, spool, request.object_ref.size + 1
                )
                if byte_count != request.object_ref.size:
                    raise _Reject(
                        RejectionCode.OBJECT_INTEGRITY_MISMATCH,
                        f"byte length {byte_count} != ObjectRef.size {request.object_ref.size}",
                    )
                if digest != request.object_ref.sha256:
                    raise _Reject(
                        RejectionCode.OBJECT_INTEGRITY_MISMATCH,
                        "SHA-256 != ObjectRef.sha256",
                    )
                # Do not expose parsed rows until object identity and the complete member have
                # passed validation, including ZIP CRC and all row invariants.
                spool.seek(0)
                rows, member_name = _parse_zip(
                    cast(IO[bytes], _RetryableSpoolReader(spool)), request
                )
    except _Reject as reject:
        return _rejection(request, reject)
    except OSError as exc:
        raise StorageError(
            f"archive parser input or temporary spool I/O failed ({type(exc).__name__})"
        ) from exc
    return _success(request, member_name, rows)


def parse_archive_bytes(request: ArchiveParseRequest, data: bytes) -> ParseOutcome:
    """解析内存中的归档字节；字节必须与 ``request.object_ref`` 的 SHA-256 / 长度一致。"""
    try:
        _check_request_identity(request)
        if len(data) != request.object_ref.size:
            raise _Reject(
                RejectionCode.OBJECT_INTEGRITY_MISMATCH,
                f"byte length {len(data)} != ObjectRef.size {request.object_ref.size}",
            )
        if hashlib.sha256(data).hexdigest() != request.object_ref.sha256:
            raise _Reject(RejectionCode.OBJECT_INTEGRITY_MISMATCH, "SHA-256 != ObjectRef.sha256")
        # This convenience entry point receives a complete bytes object from its caller;
        # the storage entry point uses a temporary seekable spool to avoid another full copy.
        rows, member_name = _parse_zip(io.BytesIO(data), request)
    except _Reject as reject:
        return _rejection(request, reject)
    return _success(request, member_name, rows)


def _rejection(request: ArchiveParseRequest, reject: _Reject) -> ArchiveRejection:
    return ArchiveRejection(
        parser=PARSER_BINDING,
        archive_revision_id=request.archive_revision_id,
        data_type=request.data_type,
        symbol=request.symbol,
        coverage_start=request.coverage_start,
        coverage_end=request.coverage_end,
        object_key=request.object_ref.key,
        object_sha256=request.object_ref.sha256,
        code=reject.code,
        detail=reject.detail[:_MAX_DETAIL_CHARS],
        line_number=reject.line_number,
        column=reject.column,
    )


def _success(request: ArchiveParseRequest, member_name: str, rows: pa.Table) -> ParsedArchive:
    return ParsedArchive(
        parser=PARSER_BINDING,
        archive_revision_id=request.archive_revision_id,
        data_type=request.data_type,
        symbol=request.symbol,
        coverage_start=request.coverage_start,
        coverage_end=request.coverage_end,
        object_ref=request.object_ref,
        member_name=member_name,
        time_unit=request.time_unit,
        rows=rows,
    )


def _check_request_identity(request: ArchiveParseRequest) -> None:
    ref = request.object_ref
    expected = archive_filename(request.data_type, request.symbol, request.coverage_day)
    if ref.key.rsplit("/", 1)[-1] != expected:
        raise _Reject(
            RejectionCode.OBJECT_KEY_MISMATCH,
            f"object key basename is not {expected}",
        )
    if ref.size > _MAX_ARCHIVE_BYTES:
        raise _Reject(
            RejectionCode.ARCHIVE_TOO_LARGE,
            f"archive size {ref.size} exceeds {_MAX_ARCHIVE_BYTES} bytes",
        )


class _RetryableSpoolReader:
    """Preserve local spool I/O failures through zipfile's OSError-to-BadZip mapping."""

    def __init__(self, handle: IO[bytes]) -> None:
        self._handle = handle

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)

    def read(self, size: int = -1) -> bytes:
        try:
            return self._handle.read(size)
        except OSError as exc:
            raise StorageError(
                f"archive parser temporary spool read failed ({type(exc).__name__})"
            ) from exc

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        try:
            return self._handle.seek(offset, whence)
        except OSError as exc:
            raise StorageError(
                f"archive parser temporary spool seek failed ({type(exc).__name__})"
            ) from exc

    def tell(self) -> int:
        try:
            return self._handle.tell()
        except OSError as exc:
            raise StorageError(
                f"archive parser temporary spool tell failed ({type(exc).__name__})"
            ) from exc


def _spool_bounded(source: IO[bytes], destination: IO[bytes], limit: int) -> tuple[int, str]:
    """按固定块复制至 seekable spool 并同步哈希，最多读取 ``limit`` 字节。"""
    hasher = hashlib.sha256()
    total = 0
    while total < limit and (chunk := source.read(min(_READ_CHUNK_BYTES, limit - total))):
        destination.write(chunk)
        hasher.update(chunk)
        total += len(chunk)
    return total, hasher.hexdigest()


# --------------------------------------------------------------------------- ZIP container


def _parse_zip(handle: IO[bytes], request: ArchiveParseRequest) -> tuple[pa.Table, str]:
    expected_member = member_filename(request.data_type, request.symbol, request.coverage_day)
    handle.seek(0, io.SEEK_END)
    total = handle.tell()
    handle.seek(0)
    offset_cd = _check_container_layout(handle, total)
    try:
        archive = zipfile.ZipFile(handle, mode="r")
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError) as exc:
        raise _Reject(RejectionCode.ZIP_CORRUPT, f"unreadable ZIP: {type(exc).__name__}") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) != 1:
            raise _Reject(RejectionCode.ZIP_MEMBER_COUNT, f"expected 1 member, found {len(infos)}")
        info = infos[0]
        _check_member(info, expected_member)
        _check_member_layout(info, handle, offset_cd)
        try:
            member = archive.open(info, mode="r")
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, ValueError) as exc:
            raise _Reject(
                RejectionCode.ZIP_MEMBER_CORRUPT, f"cannot open member: {type(exc).__name__}"
            ) from exc
        with member:
            parser = (
                _AggTradesParser(request)
                if request.data_type == "agg_trades"
                else _KlinesParser(request)
            )
            for line_number, text in _csv_lines(member, size=info.file_size, crc=info.CRC):
                parser.feed(line_number, text)
            return parser.finish(), expected_member


def _check_container_layout(handle: IO[bytes], total: int) -> int:
    """拒绝前缀 / 尾随字节、注释、zip64 与多卷；恰好一个条目；返回中央目录偏移。"""
    if total < _EOCD_SIZE:
        raise _Reject(RejectionCode.ZIP_CORRUPT, f"archive too short ({total} bytes)")
    handle.seek(total - _EOCD_SIZE)
    eocd = handle.read(_EOCD_SIZE)
    handle.seek(0)
    head = handle.read(4)
    handle.seek(0)
    if eocd[:4] != _EOCD_SIG:
        raise _Reject(
            RejectionCode.ZIP_LAYOUT_UNSUPPORTED,
            "no end-of-central-directory record at the end (comment or trailing bytes)",
        )
    _, disk, cd_disk, disk_entries, entries, size_cd, offset_cd, comment_len = struct.unpack(
        "<4s4H2LH", eocd
    )
    if (disk, cd_disk, comment_len) != (0, 0, 0):
        raise _Reject(RejectionCode.ZIP_LAYOUT_UNSUPPORTED, "multi-disk archive or comment")
    if disk_entries != entries:
        raise _Reject(RejectionCode.ZIP_LAYOUT_UNSUPPORTED, "inconsistent entry counts")
    if entries != 1:
        raise _Reject(RejectionCode.ZIP_MEMBER_COUNT, f"expected 1 member, found {entries}")
    if offset_cd + size_cd != total - _EOCD_SIZE:
        raise _Reject(
            RejectionCode.ZIP_LAYOUT_UNSUPPORTED,
            "central directory is not immediately followed by the end record (zip64 or gap)",
        )
    if total < _LOCAL_HEADER_SIZE + _EOCD_SIZE or head != _LOCAL_HEADER_SIG:
        raise _Reject(RejectionCode.ZIP_LAYOUT_UNSUPPORTED, "archive does not start with a member")
    return int(offset_cd)


def _check_member(info: zipfile.ZipInfo, expected: str) -> None:
    """中央目录条目：普通文件、名字精确匹配、未加密、已知标志 / 方法、大小与压缩比上限。"""
    name = info.orig_filename
    if info.is_dir() or name.endswith("/"):
        raise _Reject(RejectionCode.ZIP_MEMBER_NOT_REGULAR_FILE, "member is a directory")
    if name != expected or info.filename != expected:
        raise _Reject(RejectionCode.ZIP_MEMBER_NAME_MISMATCH, f"member name is not {expected}")
    file_type = stat.S_IFMT(info.external_attr >> 16)
    if info.create_system == 3 and file_type not in {0, stat.S_IFREG}:
        raise _Reject(RejectionCode.ZIP_MEMBER_NOT_REGULAR_FILE, f"unix file type 0o{file_type:o}")
    if info.external_attr & 0x10:
        raise _Reject(RejectionCode.ZIP_MEMBER_NOT_REGULAR_FILE, "DOS directory attribute")
    if info.flag_bits & _ENCRYPTION_FLAGS:
        raise _Reject(RejectionCode.ZIP_MEMBER_ENCRYPTED, "member is encrypted")
    if info.flag_bits & ~_ALLOWED_ZIP_FLAGS:
        raise _Reject(
            RejectionCode.ZIP_MEMBER_FLAGS_UNSUPPORTED,
            f"general purpose flags 0x{info.flag_bits:04x} not allowed",
        )
    if info.compress_type not in _ALLOWED_COMPRESSION:
        raise _Reject(
            RejectionCode.ZIP_COMPRESSION_UNSUPPORTED,
            f"compression method {info.compress_type} not allowed",
        )
    if info.file_size > _MAX_MEMBER_BYTES:
        raise _Reject(
            RejectionCode.ZIP_MEMBER_TOO_LARGE,
            f"member size {info.file_size} exceeds {_MAX_MEMBER_BYTES} bytes",
        )
    if info.file_size == 0:
        raise _Reject(RejectionCode.NO_ROWS, "member is empty")
    if info.compress_type == zipfile.ZIP_STORED and info.compress_size != info.file_size:
        raise _Reject(RejectionCode.ZIP_MEMBER_CORRUPT, "stored member sizes disagree")
    if info.file_size > info.compress_size * _MAX_COMPRESSION_RATIO:
        raise _Reject(
            RejectionCode.ZIP_COMPRESSION_RATIO_EXCEEDED,
            f"compression ratio exceeds {_MAX_COMPRESSION_RATIO}",
        )


def _check_member_layout(info: zipfile.ZipInfo, handle: IO[bytes], offset_cd: int) -> None:
    """本地头与中央目录一致；数据（+ 可选 descriptor）之后紧接中央目录。"""
    if info.header_offset != 0:
        raise _Reject(RejectionCode.ZIP_LAYOUT_UNSUPPORTED, "member does not start at offset 0")
    handle.seek(0)
    local = handle.read(_LOCAL_HEADER_SIZE)
    handle.seek(0)
    (_, _, flags, method, _, _, crc, compress_size, file_size, name_len, extra_len) = struct.unpack(
        "<4s5H3L2H", local
    )
    handle.seek(_LOCAL_HEADER_SIZE + name_len)
    local_extra = handle.read(extra_len)
    handle.seek(0)
    _check_extra_fields(local_extra)
    _check_extra_fields(info.extra)
    if (flags, method) != (info.flag_bits, info.compress_type):
        raise _Reject(
            RejectionCode.ZIP_MEMBER_CORRUPT, "local header flags / method disagree with directory"
        )
    declared = (info.CRC, info.compress_size, info.file_size)
    data_end = _LOCAL_HEADER_SIZE + name_len + extra_len + info.compress_size
    gap = offset_cd - data_end
    if info.flag_bits & 0x0008:
        if gap not in {12, 16}:
            raise _Reject(RejectionCode.ZIP_LAYOUT_UNSUPPORTED, "unexpected data descriptor")
        handle.seek(data_end)
        descriptor = handle.read(gap)
        handle.seek(0)
        if gap == 16:
            if descriptor[:4] != _DATA_DESCRIPTOR_SIG:
                raise _Reject(RejectionCode.ZIP_LAYOUT_UNSUPPORTED, "unexpected data descriptor")
            descriptor = descriptor[4:]
        if struct.unpack("<3L", descriptor) != declared:
            raise _Reject(
                RejectionCode.ZIP_MEMBER_CORRUPT, "data descriptor disagrees with directory"
            )
    else:
        if gap != 0:
            raise _Reject(
                RejectionCode.ZIP_LAYOUT_UNSUPPORTED,
                "bytes between member data and central directory",
            )
        if (crc, compress_size, file_size) != declared:
            raise _Reject(
                RejectionCode.ZIP_MEMBER_CORRUPT, "local header sizes / CRC disagree with directory"
            )


def _check_extra_fields(extra: bytes) -> None:
    """extra 字段必须是完整的 (id, size, data) 块序列，且不含 zip64 / AES 块。"""
    offset = 0
    while offset < len(extra):
        if offset + 4 > len(extra):
            raise _Reject(RejectionCode.ZIP_LAYOUT_UNSUPPORTED, "truncated extra field")
        header_id, size = struct.unpack_from("<2H", extra, offset)
        offset += 4 + size
        if offset > len(extra):
            raise _Reject(RejectionCode.ZIP_LAYOUT_UNSUPPORTED, "truncated extra field")
        if header_id in _FORBIDDEN_EXTRA_IDS:
            raise _Reject(
                RejectionCode.ZIP_LAYOUT_UNSUPPORTED, f"extra field 0x{header_id:04x} not allowed"
            )


# --------------------------------------------------------------------------- CSV lines


def _csv_lines(member: IO[bytes], *, size: int, crc: int) -> Iterator[tuple[int, str]]:
    """逐行产出 ``(1-based 行号, ASCII 文本)``；读到 EOF 后独立核对字节数与 CRC-32。

    自行按 LF 切分并限制行长：``ZipExtFile.readline(limit)`` 在换行已缓冲时不遵守 ``limit``；
    ``zipfile`` 只在压缩流耗尽时核对 CRC，声明大小偏小 / 偏大时不会报错，故在此自行核对。
    """
    line_number = 0
    pending = b""
    seen = 0
    running_crc = 0
    while True:
        try:
            chunk = member.read(_READ_CHUNK_BYTES)
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError) as exc:
            raise _Reject(
                RejectionCode.ZIP_MEMBER_CORRUPT,
                f"member stream failed: {type(exc).__name__}",
                line_number=line_number + 1,
            ) from exc
        if not chunk:
            break
        seen += len(chunk)
        running_crc = zlib.crc32(chunk, running_crc)
        if seen > size:
            raise _Reject(RejectionCode.ZIP_MEMBER_CORRUPT, "member longer than declared")
        data = pending + chunk
        start = 0
        while (newline := data.find(b"\n", start)) >= 0:
            line_number += 1
            yield line_number, _line_text(data[start:newline], line_number)
            start = newline + 1
        pending = data[start:]
        if len(pending) > _MAX_LINE_BYTES:
            raise _Reject(
                RejectionCode.LINE_TOO_LONG,
                f"line exceeds {_MAX_LINE_BYTES} bytes",
                line_number=line_number + 1,
            )
    if seen != size or running_crc != crc:
        raise _Reject(
            RejectionCode.ZIP_MEMBER_CORRUPT, "member size or CRC-32 differs from the directory"
        )
    if pending:
        raise _Reject(
            RejectionCode.INVALID_LINE_ENDING,
            "last line is not terminated by LF",
            line_number=line_number + 1,
        )


def _line_text(body: bytes, line_number: int) -> str:
    if len(body) > _MAX_LINE_BYTES:
        raise _Reject(
            RejectionCode.LINE_TOO_LONG,
            f"line exceeds {_MAX_LINE_BYTES} bytes",
            line_number=line_number,
        )
    if not body.isascii():
        raise _Reject(RejectionCode.NON_ASCII_CONTENT, "non-ASCII byte", line_number=line_number)
    if b"\r" in body:
        raise _Reject(RejectionCode.INVALID_LINE_ENDING, "carriage return", line_number=line_number)
    if not body:
        raise _Reject(RejectionCode.EMPTY_LINE, "empty line", line_number=line_number)
    return body.decode("ascii")


# --------------------------------------------------------------------------- field grammar


def _parse_int(text: str, line: int, column: str) -> int:
    if _INTEGER_RE.fullmatch(text) is None:
        raise _Reject(
            RejectionCode.INVALID_INTEGER,
            "not a canonical non-negative integer",
            line_number=line,
            column=column,
        )
    value = int(text)
    if value > _INT64_MAX:
        raise _Reject(
            RejectionCode.INTEGER_OUT_OF_RANGE, "exceeds int64", line_number=line, column=column
        )
    return value


def _check_decimal_text(text: str, line: int, column: str) -> None:
    if _DECIMAL_RE.fullmatch(text) is not None:
        return
    if _DECIMAL_SHAPE_RE.fullmatch(text) is not None and not (
        len(text) > 1 and text[0] == "0" and text[1] != "."
    ):
        raise _Reject(
            RejectionCode.DECIMAL_OUT_OF_RANGE,
            f"exceeds decimal({_DECIMAL_PRECISION},{_DECIMAL_SCALE})",
            line_number=line,
            column=column,
        )
    raise _Reject(
        RejectionCode.INVALID_DECIMAL,
        "not a plain non-negative decimal",
        line_number=line,
        column=column,
    )


def _parse_decimal(text: str, line: int, column: str) -> Decimal:
    _check_decimal_text(text, line, column)
    return Decimal(text)


def _parse_bool(text: str, line: int, column: str) -> bool:
    try:
        return _BOOLEANS[text]
    except KeyError:
        raise _Reject(
            RejectionCode.INVALID_BOOLEAN,
            "expected True or False",
            line_number=line,
            column=column,
        ) from None


# --------------------------------------------------------------------------- row parsers


class _ColumnBuffer:
    """按列缓冲，每 ``_CHUNK_ROWS`` 行转换为 Arrow 数组以限制 Python 对象峰值。"""

    def __init__(self, schema: pa.Schema) -> None:
        self._schema = schema
        self._pending: list[list[Any]] = [[] for _ in schema]
        self._batches: list[pa.RecordBatch] = []

    def append(self, values: tuple[Any, ...]) -> None:
        for column, value in zip(self._pending, values, strict=True):
            column.append(value)
        if len(self._pending[0]) >= _CHUNK_ROWS:
            self._flush()

    def _flush(self) -> None:
        if not self._pending[0]:
            return
        arrays = [
            pa.array(values, type=schema_field.type)
            for values, schema_field in zip(self._pending, self._schema, strict=True)
        ]
        self._batches.append(pa.RecordBatch.from_arrays(arrays, schema=self._schema))
        self._pending = [[] for _ in self._schema]

    def table(self) -> pa.Table:
        self._flush()
        # Keep the bounded RecordBatch chunks: combine_chunks would allocate a second full
        # column set while the batches are still live, even though ParsedArchive must return a
        # complete Table. Consumers that truly need contiguous arrays can combine explicitly.
        return pa.Table.from_batches(self._batches, schema=self._schema)


class _RowParser:
    columns: tuple[tuple[str, str], ...]
    schema: pa.Schema

    def __init__(self, request: ArchiveParseRequest) -> None:
        unit = request.time_unit
        self.unit = unit
        self.start_ticks = _epoch_ticks(request.coverage_start, unit)
        self.end_ticks = _epoch_ticks(request.coverage_end, unit)
        self.minute_ticks = 60 * unit.ticks_per_second
        self.buffer = _ColumnBuffer(self.schema)
        self.rows = 0

    def fields(self, line: int, text: str) -> list[str]:
        values = text.split(",")
        if len(values) != len(self.columns):
            raise _Reject(
                RejectionCode.COLUMN_COUNT,
                f"expected {len(self.columns)} columns, found {len(values)}",
                line_number=line,
            )
        return values

    def check_in_coverage(self, ticks: int, line: int, column: str) -> None:
        if not self.start_ticks <= ticks < self.end_ticks:
            raise _Reject(
                RejectionCode.TIMESTAMP_OUT_OF_COVERAGE,
                f"{ticks} not in [{self.start_ticks}, {self.end_ticks}) {self.unit.value}s",
                line_number=line,
                column=column,
            )

    def feed(self, line: int, text: str) -> None:
        raise NotImplementedError

    def finish(self) -> pa.Table:
        if self.rows == 0:
            raise _Reject(RejectionCode.NO_ROWS, "archive contains no data rows")
        return self.buffer.table()


class _AggTradesParser(_RowParser):
    columns = _AGG_TRADES_COLUMNS
    schema = AGG_TRADES_ROW_SCHEMA

    def __init__(self, request: ArchiveParseRequest) -> None:
        super().__init__(request)
        self.previous: tuple[int, int, int] | None = None  # (agg id, timestamp, last trade id)

    def feed(self, line: int, text: str) -> None:
        raw = self.fields(line, text)
        agg_id = _parse_int(raw[0], line, "agg_trade_id")
        price = _parse_decimal(raw[1], line, "price")
        quantity = _parse_decimal(raw[2], line, "quantity")
        first_id = _parse_int(raw[3], line, "first_trade_id")
        last_id = _parse_int(raw[4], line, "last_trade_id")
        ticks = _parse_int(raw[5], line, "timestamp_raw")
        buyer_maker = _parse_bool(raw[6], line, "is_buyer_maker")
        best_match = _parse_bool(raw[7], line, "is_best_match")
        if price <= 0:
            raise _Reject(
                RejectionCode.NON_POSITIVE_PRICE, "price <= 0", line_number=line, column="price"
            )
        if quantity <= 0:
            raise _Reject(
                RejectionCode.NON_POSITIVE_QUANTITY,
                "quantity <= 0",
                line_number=line,
                column="quantity",
            )
        if first_id > last_id:
            raise _Reject(
                RejectionCode.TRADE_ID_RANGE_INVALID,
                "first_trade_id > last_trade_id",
                line_number=line,
                column="first_trade_id",
            )
        self.check_in_coverage(ticks, line, "timestamp_raw")
        if self.previous is not None:
            prev_id, prev_ticks, prev_last = self.previous
            if agg_id == prev_id:
                raise _Reject(
                    RejectionCode.DUPLICATE_ROW,
                    "agg_trade_id repeats the previous row",
                    line_number=line,
                    column="agg_trade_id",
                )
            if agg_id < prev_id:
                raise _Reject(
                    RejectionCode.OUT_OF_ORDER,
                    "agg_trade_id decreases",
                    line_number=line,
                    column="agg_trade_id",
                )
            if ticks < prev_ticks:
                raise _Reject(
                    RejectionCode.TIMESTAMP_DECREASING,
                    "timestamp decreases",
                    line_number=line,
                    column="timestamp_raw",
                )
            if first_id <= prev_last:
                raise _Reject(
                    RejectionCode.TRADE_ID_OVERLAP,
                    "first_trade_id overlaps the previous row",
                    line_number=line,
                    column="first_trade_id",
                )
        self.previous = (agg_id, ticks, last_id)
        self.rows += 1
        self.buffer.append(
            (
                line,
                agg_id,
                price,
                quantity,
                first_id,
                last_id,
                ticks,
                buyer_maker,
                best_match,
                ticks * self.unit.micros_per_tick,
            )
        )


class _KlinesParser(_RowParser):
    columns = _KLINES_COLUMNS
    schema = KLINES_1M_ROW_SCHEMA

    def __init__(self, request: ArchiveParseRequest) -> None:
        super().__init__(request)
        self.previous_open: int | None = None

    def feed(self, line: int, text: str) -> None:
        raw = self.fields(line, text)
        open_ticks = _parse_int(raw[0], line, "open_time_raw")
        open_, high, low, close = (
            _parse_decimal(raw[index], line, name)
            for index, name in ((1, "open"), (2, "high"), (3, "low"), (4, "close"))
        )
        volume = _parse_decimal(raw[5], line, "volume")
        close_ticks = _parse_int(raw[6], line, "close_time_raw")
        quote_volume = _parse_decimal(raw[7], line, "quote_asset_volume")
        trades = _parse_int(raw[8], line, "number_of_trades")
        taker_base = _parse_decimal(raw[9], line, "taker_buy_base_asset_volume")
        taker_quote = _parse_decimal(raw[10], line, "taker_buy_quote_asset_volume")
        _check_decimal_text(raw[11], line, "ignore_raw")
        for name, price in (("open", open_), ("high", high), ("low", low), ("close", close)):
            if price <= 0:
                raise _Reject(
                    RejectionCode.NON_POSITIVE_PRICE, f"{name} <= 0", line_number=line, column=name
                )
        if not (low <= min(open_, close) and high >= max(open_, close) and low <= high):
            raise _Reject(
                RejectionCode.OHLC_INVARIANT,
                "low <= min(open, close) <= max(open, close) <= high violated",
                line_number=line,
            )
        if taker_base > volume:
            raise _Reject(
                RejectionCode.TAKER_VOLUME_INVARIANT,
                "taker buy base volume > volume",
                line_number=line,
                column="taker_buy_base_asset_volume",
            )
        if taker_quote > quote_volume:
            raise _Reject(
                RejectionCode.TAKER_VOLUME_INVARIANT,
                "taker buy quote volume > quote asset volume",
                line_number=line,
                column="taker_buy_quote_asset_volume",
            )
        self.check_in_coverage(open_ticks, line, "open_time_raw")
        if (open_ticks - self.start_ticks) % self.minute_ticks:
            raise _Reject(
                RejectionCode.KLINE_OPEN_NOT_ALIGNED,
                "open time is not on a whole minute",
                line_number=line,
                column="open_time_raw",
            )
        if close_ticks != open_ticks + self.minute_ticks - 1:
            raise _Reject(
                RejectionCode.KLINE_CLOSE_TIME_MISMATCH,
                f"close time != open time + 1 minute - 1 {self.unit.value}",
                line_number=line,
                column="close_time_raw",
            )
        if self.previous_open is not None:
            if open_ticks == self.previous_open:
                raise _Reject(
                    RejectionCode.DUPLICATE_ROW,
                    "open time repeats the previous row",
                    line_number=line,
                    column="open_time_raw",
                )
            if open_ticks < self.previous_open:
                raise _Reject(
                    RejectionCode.OUT_OF_ORDER,
                    "open time decreases",
                    line_number=line,
                    column="open_time_raw",
                )
        self.previous_open = open_ticks
        self.rows += 1
        start_micros = open_ticks * self.unit.micros_per_tick
        self.buffer.append(
            (
                line,
                open_ticks,
                open_,
                high,
                low,
                close,
                volume,
                close_ticks,
                quote_volume,
                trades,
                taker_base,
                taker_quote,
                raw[11],
                start_micros,
                start_micros + 60_000_000,
            )
        )


def _epoch_ticks(value: datetime, unit: TimeUnit) -> int:
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    return micros // unit.micros_per_tick

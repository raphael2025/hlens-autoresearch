"""Builders for D1 parser tests: synthetic Binance archives, requests and ZIP header patching."""

from __future__ import annotations

import hashlib
import io
import struct
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from core.contracts.storage import ObjectRef
from infrastructure.parser import ArchiveParseRequest, ArchiveRejection, ParseOutcome
from infrastructure.parser.binance_archive import archive_filename, member_filename

MS_DAY = date(2024, 12, 31)
US_DAY = date(2025, 1, 1)
_KEY_DIRS = {"agg_trades": "aggTrades", "klines_1m": "klines"}


def day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def ticks_per_second(day: date) -> int:
    return 1_000_000 if day >= US_DAY else 1_000


def start_ticks(day: date, *, tps: int | None = None) -> int:
    seconds = int(day_start(day).timestamp())
    return seconds * (tps or ticks_per_second(day))


def object_key(data_type: str, symbol: str, day: date) -> str:
    """A legal warehouse key for a parser fixture.

    The parser only requires the official basename, so this stays a plain path; production
    keys are content-addressed (``revisions/<sha256>/...``, see infrastructure/README.md).
    """
    interval = "/1m" if data_type == "klines_1m" else ""
    filename = archive_filename(data_type, symbol, day)
    return f"raw/binance/spot/archive/daily/{_KEY_DIRS[data_type]}/{symbol}{interval}/{filename}"


# --------------------------------------------------------------------------- CSV rows


def agg_rows(day: date, count: int = 3, *, tps: int | None = None, step: int = 1) -> list[str]:
    """``count`` valid aggTrades rows starting at the coverage start, ``step`` ticks apart."""
    base = start_ticks(day, tps=tps)
    rows = []
    for index in range(count):
        first = 1000 + index * 3
        rows.append(
            f"{500 + index},92792.05000000,0.00150000,{first},{first + 2},"
            f"{base + index * step},{'True' if index % 2 else 'False'},True"
        )
    return rows


def kline_row(
    day: date,
    minute: int,
    *,
    tps: int | None = None,
    open_: str = "92792.05000000",
    high: str = "92832.25000000",
    low: str = "92782.12000000",
    close: str = "92782.13000000",
    volume: str = "6.45789000",
    quote_volume: str = "599298.29174060",
    trades: str = "1660",
    taker_base: str = "4.36611000",
    taker_quote: str = "405183.17952120",
    ignore: str = "0",
    open_ticks: int | None = None,
    close_ticks: int | None = None,
) -> str:
    per_second = tps or ticks_per_second(day)
    opened = start_ticks(day, tps=per_second) + minute * 60 * per_second
    if open_ticks is not None:
        opened = open_ticks
    closed = opened + 60 * per_second - 1 if close_ticks is None else close_ticks
    return (
        f"{opened},{open_},{high},{low},{close},{volume},{closed},{quote_volume},{trades},"
        f"{taker_base},{taker_quote},{ignore}"
    )


def kline_rows(day: date, count: int = 3, *, tps: int | None = None) -> list[str]:
    return [kline_row(day, minute, tps=tps) for minute in range(count)]


def csv_bytes(rows: list[str]) -> bytes:
    return "".join(f"{row}\n" for row in rows).encode("utf-8")


# --------------------------------------------------------------------------- ZIP


def make_zip(
    members: list[tuple[str, bytes]],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
    comment: bytes = b"",
    streamed: bool = False,
) -> bytes:
    """Build a ZIP; ``streamed`` writes through a non-seekable sink (data descriptors)."""
    sink: io.BytesIO | _Unseekable = _Unseekable() if streamed else io.BytesIO()
    with zipfile.ZipFile(sink, mode="w", compression=compression) as archive:
        for name, payload in members:
            info = zipfile.ZipInfo(name, date_time=(2025, 1, 2, 0, 0, 0))
            info.compress_type = compression
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payload)
        archive.comment = comment
    return sink.getvalue()


class _Unseekable(io.RawIOBase):
    def __init__(self) -> None:
        self._buffer = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, data: object) -> int:
        chunk = bytes(data)  # type: ignore[call-overload]
        self._buffer.extend(chunk)
        return len(chunk)

    def getvalue(self) -> bytes:
        return bytes(self._buffer)


def archive_for(data_type: str, symbol: str, day: date, content: bytes) -> bytes:
    return make_zip([(member_filename(data_type, symbol, day), content)])


def central_offset(data: bytes) -> int:
    return int(struct.unpack("<L", data[-6:-2])[0])


def patch_member(
    data: bytes,
    *,
    flag_bits: int | None = None,
    crc: int | None = None,
    file_size: int | None = None,
    external_attr: int | None = None,
    local_name: bytes | None = None,
    central_name: bytes | None = None,
    central_file_size: int | None = None,
) -> bytes:
    """Rewrite single-member header fields in both the local header and the central directory."""
    out = bytearray(data)
    cd = central_offset(data)
    assert out[:4] == b"PK\x03\x04" and out[cd : cd + 4] == b"PK\x01\x02"
    if flag_bits is not None:
        struct.pack_into("<H", out, 6, flag_bits)
        struct.pack_into("<H", out, cd + 8, flag_bits)
    if crc is not None:
        struct.pack_into("<L", out, 14, crc)
        struct.pack_into("<L", out, cd + 16, crc)
    if file_size is not None:
        struct.pack_into("<L", out, 22, file_size)
        struct.pack_into("<L", out, cd + 24, file_size)
    if external_attr is not None:
        struct.pack_into("<L", out, cd + 38, external_attr)
    if local_name is not None:
        (name_len,) = struct.unpack_from("<H", out, 26)
        assert len(local_name) == name_len
        out[30 : 30 + name_len] = local_name
    if central_name is not None:
        (name_len,) = struct.unpack_from("<H", out, cd + 28)
        assert len(central_name) == name_len
        out[cd + 46 : cd + 46 + name_len] = central_name
    if central_file_size is not None:
        struct.pack_into("<L", out, cd + 24, central_file_size)
    return bytes(out)


# --------------------------------------------------------------------------- requests


@dataclass(frozen=True)
class Case:
    request: ArchiveParseRequest
    data: bytes


def make_case(
    data_type: str,
    day: date,
    data: bytes,
    *,
    symbol: str = "BTCUSDT",
    key: str | None = None,
    revision: str = "archive-rev-1",
    size: int | None = None,
    sha256: str | None = None,
) -> Case:
    ref = ObjectRef(
        key=key or object_key(data_type, symbol, day),
        uri=f"file:///warehouse/{key or object_key(data_type, symbol, day)}",
        sha256=sha256 or hashlib.sha256(data).hexdigest(),
        size=len(data) if size is None else size,
    )
    request = ArchiveParseRequest(
        archive_revision_id=revision,
        data_type=data_type,
        symbol=symbol,
        coverage_start=day_start(day),
        coverage_end=day_start(day) + timedelta(days=1),
        object_ref=ref,
    )
    return Case(request, data)


def csv_case(data_type: str, day: date, content: bytes, *, symbol: str = "BTCUSDT") -> Case:
    return make_case(data_type, day, archive_for(data_type, symbol, day, content), symbol=symbol)


def expect_rejection(outcome: ParseOutcome) -> ArchiveRejection:
    assert isinstance(outcome, ArchiveRejection), f"expected rejection, got {outcome!r}"
    assert not hasattr(outcome, "rows")
    return outcome

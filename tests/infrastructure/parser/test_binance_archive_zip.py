"""Hostile ZIP containers for the D1 parser (task counterexample 5): all fail closed, no rows."""

from __future__ import annotations

import io
import struct
import warnings
import zipfile

import pytest

from infrastructure.parser import ParsedArchive, RejectionCode, parse_archive_bytes
from infrastructure.parser.binance_archive import member_filename
from tests.infrastructure.parser.parser_support import (
    US_DAY,
    central_offset,
    csv_bytes,
    expect_rejection,
    kline_rows,
    make_case,
    make_zip,
    patch_member,
)

KLINES = "klines_1m"
NAME = member_filename(KLINES, "BTCUSDT", US_DAY)
CONTENT = csv_bytes(kline_rows(US_DAY, 50))


def _outcome(data: bytes) -> object:
    case = make_case(KLINES, US_DAY, data)
    return parse_archive_bytes(case.request, case.data)


def _code(data: bytes) -> RejectionCode:
    return expect_rejection(_outcome(data)).code  # type: ignore[arg-type]


def _valid() -> bytes:
    return make_zip([(NAME, CONTENT)])


def test_reference_archive_is_accepted() -> None:
    outcome = _outcome(_valid())
    assert isinstance(outcome, ParsedArchive) and outcome.row_count == 50


def test_stored_member_and_data_descriptor_layout_are_accepted() -> None:
    stored = _outcome(make_zip([(NAME, CONTENT)], compression=zipfile.ZIP_STORED))
    assert isinstance(stored, ParsedArchive) and stored.row_count == 50
    streamed = make_zip([(NAME, CONTENT)], streamed=True)
    assert zipfile.ZipFile(io.BytesIO(streamed)).infolist()[0].flag_bits & 0x08
    outcome = _outcome(streamed)
    assert isinstance(outcome, ParsedArchive) and outcome.row_count == 50


# --------------------------------------------------------------------------- members


def test_extra_member_rejected() -> None:
    data = make_zip([(NAME, CONTENT), ("README.txt", b"hello\n")])
    assert _code(data) is RejectionCode.ZIP_MEMBER_COUNT


def test_duplicate_member_rejected() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # zipfile warns on duplicate names
        data = make_zip([(NAME, CONTENT), (NAME, CONTENT)])
    assert _code(data) is RejectionCode.ZIP_MEMBER_COUNT


def test_no_member_rejected() -> None:
    assert _code(make_zip([])) is RejectionCode.ZIP_MEMBER_COUNT


def test_directory_member_rejected() -> None:
    assert _code(make_zip([(f"{NAME}/", b"")])) is RejectionCode.ZIP_MEMBER_NOT_REGULAR_FILE


@pytest.mark.parametrize(
    "name",
    [
        f"../{NAME}",
        f"../../etc/{NAME}",
        f"sub/{NAME}",
        f"/{NAME}",
        f"..\\{NAME}",
        f"{NAME}.csv",
        NAME.upper(),
    ],
)
def test_path_traversal_and_foreign_member_names_rejected(name: str) -> None:
    assert _code(make_zip([(name, CONTENT)])) is RejectionCode.ZIP_MEMBER_NAME_MISMATCH


def test_nul_truncated_member_name_rejected() -> None:
    forged_name = f"{NAME}\x00../evil".encode()
    placeholder = "X" * len(forged_name)
    data = make_zip([(placeholder, CONTENT)])
    data = patch_member(data, local_name=forged_name, central_name=forged_name)
    assert _code(data) is RejectionCode.ZIP_MEMBER_NAME_MISMATCH


def test_permission_only_unix_mode_is_a_regular_file() -> None:
    outcome = _outcome(patch_member(_valid(), external_attr=0o600 << 16))
    assert isinstance(outcome, ParsedArchive)


def test_local_header_name_differs_from_central_directory() -> None:
    forged = patch_member(_valid(), local_name=b"BTCUSDT-1m-2025-01-02.csv")
    assert _code(forged) is RejectionCode.ZIP_MEMBER_CORRUPT


@pytest.mark.parametrize("mode", [0o120777, 0o040755, 0o010644])
def test_non_regular_unix_member_rejected(mode: int) -> None:
    data = patch_member(_valid(), external_attr=mode << 16)
    assert _code(data) is RejectionCode.ZIP_MEMBER_NOT_REGULAR_FILE


def test_dos_directory_attribute_rejected() -> None:
    data = patch_member(_valid(), external_attr=(0o100644 << 16) | 0x10)
    assert _code(data) is RejectionCode.ZIP_MEMBER_NOT_REGULAR_FILE


# --------------------------------------------------------------------------- flags / methods


@pytest.mark.parametrize("flags", [0x0001, 0x0041, 0x2000])
def test_encrypted_member_rejected(flags: int) -> None:
    assert _code(patch_member(_valid(), flag_bits=flags)) is RejectionCode.ZIP_MEMBER_ENCRYPTED


@pytest.mark.parametrize("flags", [0x0010, 0x0020, 0x1000])
def test_unknown_flags_rejected(flags: int) -> None:
    data = patch_member(_valid(), flag_bits=flags)
    assert _code(data) is RejectionCode.ZIP_MEMBER_FLAGS_UNSUPPORTED


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_unsupported_compression_rejected(method: int) -> None:
    data = make_zip([(NAME, CONTENT)], compression=method)
    assert _code(data) is RejectionCode.ZIP_COMPRESSION_UNSUPPORTED


# --------------------------------------------------------------------------- corruption


def test_crc_mismatch_after_valid_rows_rejected() -> None:
    info = zipfile.ZipFile(io.BytesIO(_valid())).infolist()[0]
    data = patch_member(_valid(), crc=info.CRC ^ 0xFFFFFFFF)
    assert _code(data) is RejectionCode.ZIP_MEMBER_CORRUPT


def test_corrupt_deflate_stream_rejected() -> None:
    data = bytearray(_valid())
    start = 30 + len(NAME)
    for offset in range(start + 10, start + 40):
        data[offset] ^= 0x5A
    assert _code(bytes(data)) is RejectionCode.ZIP_MEMBER_CORRUPT


def test_corrupt_central_directory_rejected() -> None:
    data = bytearray(_valid())
    data[central_offset(bytes(data))] ^= 0xFF
    assert _code(bytes(data)) is RejectionCode.ZIP_CORRUPT


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"", RejectionCode.ZIP_CORRUPT),
        (b"PK\x03\x04", RejectionCode.ZIP_CORRUPT),
        (b"not a zip archive at all, but long enough to be looked at", None),
    ],
)
def test_non_zip_bytes_rejected(data: bytes, code: RejectionCode | None) -> None:
    got = _code(data)
    assert got is (code or RejectionCode.ZIP_LAYOUT_UNSUPPORTED)


def test_truncated_archive_rejected() -> None:
    data = _valid()
    for cut in (len(data) - 1, len(data) // 2, 40):
        assert _code(data[:cut]) in {
            RejectionCode.ZIP_LAYOUT_UNSUPPORTED,
            RejectionCode.ZIP_CORRUPT,
        }


# --------------------------------------------------------------------------- layout


def test_prefixed_bytes_rejected() -> None:
    assert _code(b"MZ-stub" + _valid()) is RejectionCode.ZIP_LAYOUT_UNSUPPORTED


def test_trailing_bytes_rejected() -> None:
    assert _code(_valid() + b"\x00") is RejectionCode.ZIP_LAYOUT_UNSUPPORTED


def test_archive_comment_rejected() -> None:
    data = make_zip([(NAME, CONTENT)], comment=b"hi")
    assert _code(data) is RejectionCode.ZIP_LAYOUT_UNSUPPORTED


def test_hidden_bytes_before_central_directory_rejected() -> None:
    data = _valid()
    offset = central_offset(data)
    hidden = b"PK\x03\x04hidden-member-bytes"
    forged = bytearray(data[:offset] + hidden + data[offset:])
    struct.pack_into("<L", forged, len(forged) - 6, offset + len(hidden))
    assert _code(bytes(forged)) is RejectionCode.ZIP_LAYOUT_UNSUPPORTED


def test_zip64_extra_field_rejected() -> None:
    sink = io.BytesIO()
    with (
        zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED) as archive,
        archive.open(zipfile.ZipInfo(NAME), "w", force_zip64=True) as member,
    ):
        member.write(CONTENT)
    assert _code(sink.getvalue()) is RejectionCode.ZIP_LAYOUT_UNSUPPORTED


# --------------------------------------------------------------------------- resource limits


def test_compression_bomb_rejected_before_decompression() -> None:
    bomb = make_zip([(NAME, b"0" * (8 << 20))])
    assert len(bomb) < 64 << 10
    assert _code(bomb) is RejectionCode.ZIP_COMPRESSION_RATIO_EXCEEDED


def test_declared_member_size_over_cap_rejected() -> None:
    data = patch_member(_valid(), file_size=(3 << 30) + 1)
    assert _code(data) is RejectionCode.ZIP_MEMBER_TOO_LARGE


def test_local_header_disagreeing_with_directory_rejected() -> None:
    data = patch_member(_valid(), central_file_size=len(CONTENT) - 1)
    assert _code(data) is RejectionCode.ZIP_MEMBER_CORRUPT


def test_data_descriptor_disagreeing_with_directory_rejected() -> None:
    data = bytearray(make_zip([(NAME, CONTENT)], streamed=True))
    descriptor_crc = central_offset(bytes(data)) - 12  # signature + crc + sizes (16 bytes)
    data[descriptor_crc] ^= 0xFF
    assert _code(bytes(data)) is RejectionCode.ZIP_MEMBER_CORRUPT


def test_declared_size_lie_is_caught_by_stream_checks() -> None:
    smaller = patch_member(_valid(), file_size=len(CONTENT) - 10)
    assert _code(smaller) is RejectionCode.ZIP_MEMBER_CORRUPT
    larger = patch_member(_valid(), file_size=len(CONTENT) + 10)
    assert _code(larger) is RejectionCode.ZIP_MEMBER_CORRUPT


def test_empty_member_rejected() -> None:
    assert _code(make_zip([(NAME, b"")])) is RejectionCode.NO_ROWS

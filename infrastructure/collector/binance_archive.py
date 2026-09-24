"""Binance 公共现货日归档下载壳（Phase 1 D0；ADR-0021 / 0022 / 0023）。

只做：配置 archive base 下的官方 ZIP + `.CHECKSUM` 获取、SHA-256 先验校验、经
``StorageAdapter`` 流式 staging → 原子 publish。不做归档解析、不构造 revision、
不写表存储 / Raw、不访问 market-data 或账户 / 交易端点。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Final, Self
from urllib.parse import urlparse

import httpx

from core.contracts.collector import (
    CollectedObject,
    CollectionFailed,
    CollectionRequest,
    CollectionResult,
    CollectorDescriptor,
    CoverageGap,
    GapReason,
    SourceBinding,
    UnsupportedRequest,
)
from core.contracts.storage import (
    IntegrityViolation,
    ObjectConflict,
    StageRequest,
    StorageAdapter,
    StorageError,
)
from core.domain.base import FrozenMapping
from infrastructure.settings import Settings

__all__ = [
    "ARCHIVE_SOURCE",
    "BinanceSpotArchiveCollector",
    "COLLECTOR_ID",
    "COLLECTOR_VERSION",
    "SUPPORTED_DATA_TYPES",
    "SUPPORTED_SYMBOLS",
]

COLLECTOR_ID: Final[str] = "binance.spot.public-archive"
COLLECTOR_VERSION: Final[str] = "1.0.0"
ARCHIVE_SOURCE: Final[SourceBinding] = SourceBinding(
    source_id="binance.public.spot.archive",
    version="1.0.0",
)
SUPPORTED_SYMBOLS: Final[frozenset[str]] = frozenset({"BTCUSDT", "ETHUSDT"})
SUPPORTED_DATA_TYPES: Final[frozenset[str]] = frozenset({"agg_trades", "klines_1m"})

_CHECKSUM_MAX_BYTES: Final[int] = 4096
_STREAM_CHUNK_SIZE: Final[int] = 64 * 1024
_SHA256_HEX: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_METADATA_HEADERS: Final[frozenset[str]] = frozenset(
    {"etag", "last-modified", "content-length", "content-type"}
)
_RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({429}) | frozenset(range(500, 600))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _https_origin(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        msg = f"archive base URL 必须是 https://host[:port]/…：{base_url!r}"
        raise ValueError(msg)
    host = parsed.hostname.lower()
    if parsed.port is not None:
        return f"https://{host}:{parsed.port}"
    return f"https://{host}"


def _normalize_base(base_url: str) -> str:
    text = str(base_url).strip()
    if not text:
        msg = "archive base URL must not be blank"
        raise ValueError(msg)
    origin = _https_origin(text)
    parsed = urlparse(text)
    path = parsed.path.rstrip("/")
    if path in {"", "/"}:
        return origin
    if any(segment in {".", "..", ""} for segment in path.split("/")):
        msg = f"archive base URL 路径不得含空段或 . / ..：{base_url!r}"
        raise ValueError(msg)
    return f"{origin}{path}"


def _is_utc_midnight(value: datetime) -> bool:
    return (
        value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
        and value.hour == 0
        and value.minute == 0
        and value.second == 0
        and value.microsecond == 0
    )


def _day_iter(start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime, date]]:
    cursor = start
    while cursor < end:
        nxt = cursor + timedelta(days=1)
        yield cursor, nxt, cursor.date()
        cursor = nxt


def _parse_checksum_body(body: bytes, *, expected_filename: str) -> str:
    if len(body) > _CHECKSUM_MAX_BYTES:
        raise CollectionFailed("checksum body exceeds 4096 bytes")
    try:
        text = body.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CollectionFailed("checksum body must be ASCII") from exc
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    if not text or "\n" in text or "\r" in text:
        raise CollectionFailed("checksum body must contain exactly one sha256sum record")
    if len(text) < 66:
        raise CollectionFailed("checksum record too short")
    digest = text[:64]
    sep = text[64:66]
    filename = text[66:]
    if _SHA256_HEX.fullmatch(digest) is None:
        raise CollectionFailed("checksum digest must be 64 hex characters")
    if sep not in {"  ", " *"}:
        raise CollectionFailed("checksum separator must be two spaces or space+asterisk")
    if filename != expected_filename:
        raise CollectionFailed(
            f"checksum filename {filename!r} does not match expected {expected_filename!r}"
        )
    if "/" in filename or "\\" in filename or " " in filename or "\t" in filename:
        raise CollectionFailed("checksum filename must be a bare basename")
    return digest.lower()


def _trusted_content_length(headers: httpx.Headers) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    if not raw.isdigit():
        return None
    return int(raw)


def _read_bounded_body(response: httpx.Response, *, limit: int) -> bytes:
    buf = bytearray()
    for chunk in response.iter_bytes(chunk_size=1024):
        if not chunk:
            continue
        if len(buf) + len(chunk) > limit:
            raise CollectionFailed(f"response body exceeds {limit} bytes")
        buf.extend(chunk)
    return bytes(buf)


def _safe_metadata(headers: httpx.Headers) -> FrozenMapping[str, str]:
    out: dict[str, str] = {}
    for name in _SAFE_METADATA_HEADERS:
        value = headers.get(name)
        if value is not None:
            out[name] = value
    return FrozenMapping(out)


def _is_retryable_transport(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.WriteError,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.RemoteProtocolError,
        ),
    )


class BinanceSpotArchiveCollector:
    """同步 ``CollectorAdapter``：Binance 公共 spot 日归档下载与官方 checksum 校验。"""

    def __init__(
        self,
        storage: StorageAdapter,
        *,
        archive_base_url: str,
        http_connect_timeout_seconds: float,
        http_read_timeout_seconds: float,
        http_max_retries: int,
        http_user_agent: str,
        http_client: httpx.Client | None = None,
        http_transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if http_connect_timeout_seconds <= 0 or http_read_timeout_seconds <= 0:
            msg = "HTTP timeouts must be positive"
            raise ValueError(msg)
        if http_max_retries < 0:
            msg = "http_max_retries must be >= 0"
            raise ValueError(msg)
        if not http_user_agent.strip():
            msg = "http_user_agent must not be blank"
            raise ValueError(msg)
        if http_client is not None and http_transport is not None:
            msg = "provide at most one of http_client or http_transport"
            raise ValueError(msg)

        self._storage = storage
        self._archive_base = _normalize_base(archive_base_url)
        self._origin = _https_origin(self._archive_base)
        self._max_retries = http_max_retries
        self._timeout = httpx.Timeout(
            connect=http_connect_timeout_seconds,
            read=http_read_timeout_seconds,
            write=http_read_timeout_seconds,
            pool=http_connect_timeout_seconds,
        )
        self._user_agent = http_user_agent.strip()
        self._clock = clock or _utc_now
        self._owns_client = http_client is None
        if http_client is not None:
            self._client = http_client
        else:
            self._client = httpx.Client(
                transport=http_transport,
                timeout=self._timeout,
                follow_redirects=False,
                headers={"User-Agent": self._user_agent},
            )
        self._descriptor = CollectorDescriptor(
            collector_id=COLLECTOR_ID,
            version=COLLECTOR_VERSION,
            sources=(ARCHIVE_SOURCE,),
            network_origins=(self._origin,),
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        storage: StorageAdapter,
        *,
        http_client: httpx.Client | None = None,
        http_transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> Self:
        """从已有 ``Settings`` 机械构造；不新增设置字段，也不读取 market-data base。"""
        return cls(
            storage,
            archive_base_url=str(settings.binance_archive_base_url),
            http_connect_timeout_seconds=settings.http_connect_timeout_seconds,
            http_read_timeout_seconds=settings.http_read_timeout_seconds,
            http_max_retries=settings.http_max_retries,
            http_user_agent=settings.http_user_agent,
            http_client=http_client,
            http_transport=http_transport,
            clock=clock,
        )

    @property
    def descriptor(self) -> CollectorDescriptor:
        return self._descriptor

    def close(self) -> None:
        """关闭本实例创建的 HTTP 客户端；注入的客户端不关闭。"""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def collect(self, request: CollectionRequest) -> CollectionResult:
        self._validate_request(request)
        objects: list[CollectedObject] = []
        gaps: list[CoverageGap] = []
        try:
            for symbol in request.symbols:
                for day_start, day_end, day in _day_iter(
                    request.coverage_start, request.coverage_end
                ):
                    item = self._collect_day(request.data_type, symbol, day_start, day_end, day)
                    if isinstance(item, CoverageGap):
                        gaps.append(item)
                    else:
                        objects.append(item)
        except (CollectionFailed, UnsupportedRequest):
            raise
        except StorageError as exc:
            raise CollectionFailed(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise CollectionFailed(f"HTTP transport failure: {exc}") from exc

        return CollectionResult(
            request=request,
            collector_id=self._descriptor.collector_id,
            collector_version=self._descriptor.version,
            objects=tuple(objects),
            gaps=tuple(gaps),
        )

    def _validate_request(self, request: CollectionRequest) -> None:
        if request.source != ARCHIVE_SOURCE:
            raise UnsupportedRequest(
                f"unsupported source {request.source.source_id}@{request.source.version}"
            )
        if request.data_type not in SUPPORTED_DATA_TYPES:
            raise UnsupportedRequest(f"unsupported data_type {request.data_type!r}")
        unsupported = [symbol for symbol in request.symbols if symbol not in SUPPORTED_SYMBOLS]
        if unsupported:
            raise UnsupportedRequest(f"unsupported symbols: {unsupported!r}")
        if not _is_utc_midnight(request.coverage_start) or not _is_utc_midnight(
            request.coverage_end
        ):
            raise UnsupportedRequest("coverage must be aligned to UTC midnight day boundaries")

    def _relative_paths(self, data_type: str, symbol: str, day: date) -> tuple[str, str, str]:
        day_text = day.isoformat()
        if data_type == "agg_trades":
            filename = f"{symbol}-aggTrades-{day_text}.zip"
            relative = f"data/spot/daily/aggTrades/{symbol}/{filename}"
            key = f"raw/binance/spot/archive/daily/aggTrades/{symbol}/{filename}"
            return relative, filename, key
        if data_type == "klines_1m":
            filename = f"{symbol}-1m-{day_text}.zip"
            relative = f"data/spot/daily/klines/{symbol}/1m/{filename}"
            key = f"raw/binance/spot/archive/daily/klines/{symbol}/1m/{filename}"
            return relative, filename, key
        raise UnsupportedRequest(f"unsupported data_type {data_type!r}")

    def _url_for(self, relative_path: str) -> str:
        if (
            not relative_path
            or relative_path.startswith("/")
            or "\\" in relative_path
            or any(part in {".", "..", ""} for part in relative_path.split("/"))
        ):
            raise CollectionFailed(f"refusing unsafe archive relative path: {relative_path!r}")
        url = f"{self._archive_base}/{relative_path}"
        parsed = urlparse(url)
        if parsed.scheme != "https" or _https_origin(url) != self._origin:
            raise CollectionFailed(f"constructed URL escaped archive origin: {url!r}")
        if not url.startswith(f"{self._archive_base}/"):
            raise CollectionFailed(f"constructed URL escaped archive base: {url!r}")
        return url

    def _collect_day(
        self,
        data_type: str,
        symbol: str,
        day_start: datetime,
        day_end: datetime,
        day: date,
    ) -> CollectedObject | CoverageGap:
        relative, filename, object_key = self._relative_paths(data_type, symbol, day)
        zip_url = self._url_for(relative)
        checksum_url = self._url_for(f"{relative}.CHECKSUM")

        checksum_outcome = self._get_checksum(checksum_url, expected_filename=filename)
        if checksum_outcome is None:
            return CoverageGap(
                symbol=symbol,
                coverage_start=day_start,
                coverage_end=day_end,
                reason=GapReason.SOURCE_ABSENT,
                detail=f"checksum absent for {symbol} {day.isoformat()} ({data_type})",
            )
        source_sha256, _checksum_headers = checksum_outcome

        zip_headers, stream = self._open_zip_stream(zip_url)
        expected_size = _trusted_content_length(zip_headers)
        try:
            staged = self._storage.stage(
                StageRequest(
                    key=object_key,
                    expected_sha256=source_sha256,
                    expected_size=expected_size,
                ),
                stream,
            )
            published = self._storage.publish(staged)
        except IntegrityViolation as exc:
            raise CollectionFailed(f"ZIP integrity check failed: {exc}") from exc
        except ObjectConflict as exc:
            raise CollectionFailed(f"immutable object conflict: {exc}") from exc
        finally:
            stream.close()

        return CollectedObject(
            ref=published.ref,
            symbol=symbol,
            coverage_start=day_start,
            coverage_end=day_end,
            source_uri=zip_url,
            retrieved_at=self._clock(),
            source_sha256=source_sha256,
            source_metadata=_safe_metadata(zip_headers),
        )

    def _get_checksum(
        self, url: str, *, expected_filename: str
    ) -> tuple[str, httpx.Headers] | None:
        response = self._request(url)
        try:
            status = response.status_code
            if status in {404, 410}:
                return None
            if status >= 300 and status < 400:
                raise CollectionFailed(f"checksum redirect forbidden: HTTP {status}")
            if status != 200:
                raise CollectionFailed(f"checksum HTTP {status}")
            body = _read_bounded_body(response, limit=_CHECKSUM_MAX_BYTES)
            digest = _parse_checksum_body(body, expected_filename=expected_filename)
            return digest, response.headers
        finally:
            response.close()

    def _open_zip_stream(self, url: str) -> tuple[httpx.Headers, _ZipByteStream]:
        response = self._request(url)
        status = response.status_code
        if status in {404, 410}:
            response.close()
            raise CollectionFailed(
                f"ZIP absent while checksum present (source inconsistency): HTTP {status}"
            )
        if status >= 300 and status < 400:
            response.close()
            raise CollectionFailed(f"ZIP redirect forbidden: HTTP {status}")
        if status != 200:
            response.close()
            raise CollectionFailed(f"ZIP HTTP {status}")
        return response.headers, _ZipByteStream(response)

    def _request(self, url: str) -> httpx.Response:
        self._assert_url_allowed(url)
        attempts = 1 + self._max_retries
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                request = self._client.build_request(
                    "GET",
                    url,
                    headers={"User-Agent": self._user_agent},
                    timeout=self._timeout,
                )
                response = self._client.send(request, stream=True, follow_redirects=False)
            except httpx.HTTPError as exc:
                last_error = exc
                if _is_retryable_transport(exc) and attempt + 1 < attempts:
                    continue
                raise CollectionFailed(f"HTTP transport failure: {exc}") from exc

            status = response.status_code
            if status in _RETRYABLE_STATUS and attempt + 1 < attempts:
                response.close()
                last_error = CollectionFailed(f"retryable HTTP {status}")
                continue
            if status in _RETRYABLE_STATUS:
                response.close()
                raise CollectionFailed(f"exhausted retries for HTTP {status}")
            return response

        assert last_error is not None
        raise CollectionFailed(f"exhausted retries: {last_error}") from last_error

    def _assert_url_allowed(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise CollectionFailed(f"refusing non-HTTPS URL: {url!r}")
        if _https_origin(url) != self._origin:
            raise CollectionFailed(f"refusing URL outside archive origin: {url!r}")
        if not url.startswith(f"{self._archive_base}/") and url != self._archive_base:
            raise CollectionFailed(f"refusing URL outside archive base: {url!r}")
        if parsed.username is not None or parsed.password is not None:
            raise CollectionFailed(f"refusing URL with credentials: {url!r}")


class _ZipByteStream:
    """单遍、分块消费 ``httpx`` 流式响应；暴露 ``close`` 供 ``finally`` 释放连接。"""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self._closed = False
        self._iter = response.iter_bytes(chunk_size=_STREAM_CHUNK_SIZE)

    def __iter__(self) -> Iterator[bytes]:
        return self

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        try:
            while True:
                chunk = next(self._iter)
                if chunk:
                    return chunk
        except StopIteration:
            self.close()
            raise

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._response.close()

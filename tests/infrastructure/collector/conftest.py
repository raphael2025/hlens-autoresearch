"""Shared fixtures for Binance archive collector tests."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from infrastructure.collector import BinanceSpotArchiveCollector
from infrastructure.storage import LocalFileStorageAdapter

ARCHIVE_ORIGIN = "https://archive.test"
ARCHIVE_BASE = f"{ARCHIVE_ORIGIN}"


@dataclass
class ArchiveFixture:
    """In-memory archive site driven by ``httpx.MockTransport``."""

    files: dict[str, bytes] = field(default_factory=dict)
    checksum_override: dict[str, bytes | int] = field(default_factory=dict)
    zip_status: dict[str, int] = field(default_factory=dict)
    redirects: set[str] = field(default_factory=set)
    requests: list[str] = field(default_factory=list)
    fail_connect_times: dict[str, int] = field(default_factory=dict)
    status_sequence: dict[str, list[int]] = field(default_factory=dict)
    chunk_sizes: dict[str, int] = field(default_factory=dict)
    _connect_failures_done: dict[str, int] = field(default_factory=dict)

    def put_zip(self, path: str, payload: bytes, *, checksum: str | None = None) -> str:
        digest = checksum or hashlib.sha256(payload).hexdigest()
        filename = path.rsplit("/", 1)[-1]
        self.files[path] = payload
        self.files[f"{path}.CHECKSUM"] = f"{digest}  {filename}\n".encode("ascii")
        return digest

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        path = request.url.path.lstrip("/")

        remaining = self.fail_connect_times.get(path, 0) - self._connect_failures_done.get(path, 0)
        if remaining > 0:
            self._connect_failures_done[path] = self._connect_failures_done.get(path, 0) + 1
            raise httpx.ConnectError("simulated connect failure", request=request)

        sequence = self.status_sequence.get(path)
        if sequence:
            status = sequence.pop(0)
            if status != 200:
                return httpx.Response(status, request=request)

        if path in self.redirects or url in self.redirects:
            return httpx.Response(
                302,
                headers={"Location": f"{ARCHIVE_ORIGIN}/evil/{path}"},
                request=request,
            )

        if path in self.checksum_override:
            override = self.checksum_override[path]
            if isinstance(override, int):
                return httpx.Response(override, request=request)
            return httpx.Response(200, content=override, request=request)

        if path in self.zip_status:
            return httpx.Response(self.zip_status[path], request=request)

        if path not in self.files:
            return httpx.Response(404, request=request)

        payload = self.files[path]
        chunk = self.chunk_sizes.get(path)
        etag = f'"{hashlib.sha256(payload).hexdigest()[:16]}"'
        headers = {"content-length": str(len(payload)), "etag": etag}
        if chunk is None:
            return httpx.Response(200, content=payload, headers=headers, request=request)

        def body() -> Iterator[bytes]:
            for i in range(0, len(payload), chunk):
                yield payload[i : i + chunk]

        return httpx.Response(200, content=body(), headers=headers, request=request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def make_storage(tmp_path: Path) -> LocalFileStorageAdapter:
    warehouse = tmp_path / "warehouse"
    staging = warehouse / "staging"
    warehouse.mkdir()
    staging.mkdir()
    return LocalFileStorageAdapter(warehouse.as_uri(), staging.as_uri())


def make_collector(
    storage: LocalFileStorageAdapter,
    fixture: ArchiveFixture,
    *,
    max_retries: int = 2,
    archive_base_url: str = ARCHIVE_BASE,
    clock: Callable[[], datetime] | None = None,
) -> BinanceSpotArchiveCollector:
    return BinanceSpotArchiveCollector(
        storage,
        archive_base_url=archive_base_url,
        http_connect_timeout_seconds=1.0,
        http_read_timeout_seconds=1.0,
        http_max_retries=max_retries,
        http_user_agent="hlens-d0-test/0.0.0",
        http_transport=fixture.transport(),
        clock=clock or (lambda: datetime(2026, 9, 24, 12, 0, tzinfo=UTC)),
    )


@pytest.fixture
def archive_fixture() -> ArchiveFixture:
    return ArchiveFixture()

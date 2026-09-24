"""Behavior tests for BinanceSpotArchiveCollector (Phase 1 D0 / acceptance #10)."""

from __future__ import annotations

import ast
import hashlib
import inspect
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from core.contracts.collector import (
    CollectionFailed,
    CollectionRequest,
    GapReason,
    SourceBinding,
    UnsupportedRequest,
)
from core.contracts.storage import ObjectConflict
from infrastructure.collector import (
    ARCHIVE_SOURCE,
    COLLECTOR_ID,
    BinanceSpotArchiveCollector,
)
from infrastructure.collector import binance_archive as archive_mod
from infrastructure.settings import Settings
from tests.infrastructure.collector.conftest import (
    ARCHIVE_BASE,
    ARCHIVE_ORIGIN,
    ArchiveFixture,
    make_collector,
    make_storage,
)

MODULE_PATH = Path(archive_mod.__file__).resolve()


def _request(
    *,
    data_type: str = "agg_trades",
    symbols: tuple[str, ...] = ("BTCUSDT",),
    start: datetime = datetime(2024, 1, 1, tzinfo=UTC),
    end: datetime = datetime(2024, 1, 2, tzinfo=UTC),
    request_id: str = "d0-req",
    source: SourceBinding = ARCHIVE_SOURCE,
) -> CollectionRequest:
    return CollectionRequest(
        request_id=request_id,
        source=source,
        data_type=data_type,
        symbols=symbols,
        coverage_start=start,
        coverage_end=end,
    )


def test_url_key_mapping_for_both_data_types_and_multi_day(
    tmp_path: Path, archive_fixture: ArchiveFixture
) -> None:
    payloads = {
        ("agg_trades", "BTCUSDT", "2024-01-01"): b"agg-btc-1",
        ("agg_trades", "ETHUSDT", "2024-01-01"): b"agg-eth-1",
        ("agg_trades", "BTCUSDT", "2024-01-02"): b"agg-btc-2",
        ("klines_1m", "BTCUSDT", "2024-01-01"): b"kline-btc-1",
    }
    expected = {
        ("agg_trades", "BTCUSDT", "2024-01-01"): (
            "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip",
            "raw/binance/spot/archive/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip",
        ),
        ("agg_trades", "ETHUSDT", "2024-01-01"): (
            "data/spot/daily/aggTrades/ETHUSDT/ETHUSDT-aggTrades-2024-01-01.zip",
            "raw/binance/spot/archive/daily/aggTrades/ETHUSDT/ETHUSDT-aggTrades-2024-01-01.zip",
        ),
        ("agg_trades", "BTCUSDT", "2024-01-02"): (
            "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-02.zip",
            "raw/binance/spot/archive/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-02.zip",
        ),
        ("klines_1m", "BTCUSDT", "2024-01-01"): (
            "data/spot/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2024-01-01.zip",
            "raw/binance/spot/archive/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2024-01-01.zip",
        ),
    }
    for (data_type, symbol, day), payload in payloads.items():
        rel, _key = expected[(data_type, symbol, day)]
        archive_fixture.put_zip(rel, payload)

    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture)

    agg = collector.collect(
        _request(
            data_type="agg_trades",
            symbols=("BTCUSDT", "ETHUSDT"),
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 1, 3, tzinfo=UTC),
            request_id="agg-multi",
        )
    )
    assert {item.ref.key for item in agg.objects} == {
        expected[("agg_trades", "BTCUSDT", "2024-01-01")][1],
        expected[("agg_trades", "ETHUSDT", "2024-01-01")][1],
        expected[("agg_trades", "BTCUSDT", "2024-01-02")][1],
    }
    assert len(agg.gaps) == 1
    assert agg.gaps[0].symbol == "ETHUSDT"
    assert agg.gaps[0].coverage_start == datetime(2024, 1, 2, tzinfo=UTC)

    klines = collector.collect(
        _request(
            data_type="klines_1m",
            request_id="kline-one",
            end=datetime(2024, 1, 2, tzinfo=UTC),
        )
    )
    assert len(klines.objects) == 1
    obj = klines.objects[0]
    rel, key = expected[("klines_1m", "BTCUSDT", "2024-01-01")]
    assert obj.ref.key == key
    assert obj.source_uri == f"{ARCHIVE_BASE}/{rel}"
    assert obj.source_sha256 == hashlib.sha256(b"kline-btc-1").hexdigest()


def test_checksum_precedes_zip_and_404_skips_zip(
    tmp_path: Path, archive_fixture: ArchiveFixture
) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    archive_fixture.put_zip(rel, b"present")
    missing = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-02.zip"
    archive_fixture.checksum_override[f"{missing}.CHECKSUM"] = 404

    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture)
    result = collector.collect(
        _request(start=datetime(2024, 1, 1, tzinfo=UTC), end=datetime(2024, 1, 3, tzinfo=UTC))
    )
    assert len(result.objects) == 1
    assert result.gaps[0].reason is GapReason.SOURCE_ABSENT
    assert f"{ARCHIVE_BASE}/{missing}" not in archive_fixture.requests
    # checksum always before matching ZIP for the present day
    present_checksum = f"{ARCHIVE_BASE}/{rel}.CHECKSUM"
    present_zip = f"{ARCHIVE_BASE}/{rel}"
    assert archive_fixture.requests.index(present_checksum) < archive_fixture.requests.index(
        present_zip
    )


@pytest.mark.parametrize(
    "bad_body",
    [
        b"not-a-hash  BTCUSDT-aggTrades-2024-01-01.zip\n",
        b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef  WRONG.zip\n",
        (
            b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            b"  BTCUSDT-aggTrades-2024-01-01.zip\n"
            b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            b"  BTCUSDT-aggTrades-2024-01-01.zip\n"
        ),
        "café".encode(),
        b"x" * 4097,
    ],
)
def test_bad_checksum_publishes_nothing(
    tmp_path: Path, archive_fixture: ArchiveFixture, bad_body: bytes
) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    archive_fixture.files[rel] = b"payload"
    archive_fixture.checksum_override[f"{rel}.CHECKSUM"] = bad_body
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture)
    with pytest.raises(CollectionFailed):
        collector.collect(_request())
    assert (
        storage.lookup(
            "raw/binance/spot/archive/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
        )
        is None
    )
    assert f"{ARCHIVE_BASE}/{rel}" not in archive_fixture.requests


def test_zip_hash_mismatch_publishes_nothing(
    tmp_path: Path, archive_fixture: ArchiveFixture
) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    wrong = hashlib.sha256(b"other").hexdigest()
    archive_fixture.files[rel] = b"payload"
    archive_fixture.files[f"{rel}.CHECKSUM"] = (
        f"{wrong}  BTCUSDT-aggTrades-2024-01-01.zip\n".encode("ascii")
    )
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture)
    with pytest.raises(CollectionFailed):
        collector.collect(_request())
    assert (
        storage.lookup(
            "raw/binance/spot/archive/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
        )
        is None
    )


def test_zip_size_mismatch_publishes_nothing(
    tmp_path: Path, archive_fixture: ArchiveFixture
) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    payload = b"payload"
    digest = archive_fixture.put_zip(rel, payload)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        archive_fixture.requests.append(str(request.url))
        if path.endswith(".CHECKSUM"):
            return httpx.Response(
                200,
                content=f"{digest}  BTCUSDT-aggTrades-2024-01-01.zip\n".encode("ascii"),
                request=request,
            )
        if path == rel:
            return httpx.Response(
                200,
                content=payload,
                headers={"content-length": str(len(payload) + 10)},
                request=request,
            )
        return httpx.Response(404, request=request)

    archive_fixture.handler = handler  # type: ignore[method-assign]
    storage = make_storage(tmp_path)
    collector = BinanceSpotArchiveCollector(
        storage,
        archive_base_url=ARCHIVE_BASE,
        http_connect_timeout_seconds=1.0,
        http_read_timeout_seconds=1.0,
        http_max_retries=0,
        http_user_agent="hlens-d0-test/0.0.0",
        http_transport=httpx.MockTransport(handler),
        clock=lambda: datetime(2026, 9, 24, tzinfo=UTC),
    )
    with pytest.raises(CollectionFailed):
        collector.collect(_request())
    assert (
        storage.lookup(
            "raw/binance/spot/archive/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
        )
        is None
    )


def test_redirect_not_followed(tmp_path: Path, archive_fixture: ArchiveFixture) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    archive_fixture.put_zip(rel, b"payload")
    archive_fixture.redirects.add(f"{rel}.CHECKSUM")
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture, max_retries=0)
    with pytest.raises(CollectionFailed, match="redirect"):
        collector.collect(_request())
    assert all("/evil/" not in url for url in archive_fixture.requests)
    assert (
        storage.lookup(
            "raw/binance/spot/archive/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
        )
        is None
    )


def test_requests_stay_inside_configured_archive_base(
    tmp_path: Path, archive_fixture: ArchiveFixture
) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    archive_fixture.put_zip(rel, b"payload")
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture)
    collector.collect(_request())
    for url in archive_fixture.requests:
        assert url.startswith(f"{ARCHIVE_BASE}/")
        assert "data-api" not in url
        assert "api.binance" not in url
        assert "/order" not in url
        assert "/account" not in url


def test_malicious_base_path_rejected(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    with pytest.raises(ValueError):
        BinanceSpotArchiveCollector(
            storage,
            archive_base_url="https://archive.test/../evil",
            http_connect_timeout_seconds=1.0,
            http_read_timeout_seconds=1.0,
            http_max_retries=0,
            http_user_agent="x",
        )


@pytest.mark.parametrize(
    "bad_base",
    [
        "https://u:p@archive.test",
        "https://archive.test?evil=1",
        "https://archive.test#frag",
        "https://archive.test:99999",
        "https://archive.test/%2e%2e/evil",
        "https://archive.test/ok%2fescape",
        "https://archive.test/ok%5cescape",
        "https://archive.test//double",
        "http://archive.test",
        " https://archive.test",
        "https://archive.test ",
        " https://archive.test ",
        "\thttps://archive.test",
        "https://archive.test\n",
    ],
)
def test_archive_base_url_rejected_without_silent_rewrite(tmp_path: Path, bad_base: str) -> None:
    storage = make_storage(tmp_path)
    with pytest.raises(ValueError):
        BinanceSpotArchiveCollector(
            storage,
            archive_base_url=bad_base,
            http_connect_timeout_seconds=1.0,
            http_read_timeout_seconds=1.0,
            http_max_retries=0,
            http_user_agent="x",
        )


def test_safe_archive_base_prefix_keeps_requests_under_prefix(
    tmp_path: Path, archive_fixture: ArchiveFixture
) -> None:
    prefix_base = "https://archive.test/mirror"
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    # Fixture paths are host-relative after stripping leading slash from request URL path.
    archive_fixture.put_zip(f"mirror/{rel}", b"prefixed-payload")
    storage = make_storage(tmp_path)
    collector = make_collector(
        storage, archive_fixture, archive_base_url=prefix_base, max_retries=0
    )
    result = collector.collect(_request())
    assert len(result.objects) == 1
    assert result.objects[0].source_uri == f"{prefix_base}/{rel}"
    for url in archive_fixture.requests:
        assert url.startswith(f"{prefix_base}/")
        assert not url.startswith(f"{ARCHIVE_ORIGIN}/data/")


def test_transient_retry_count_exact(tmp_path: Path, archive_fixture: ArchiveFixture) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    digest = archive_fixture.put_zip(rel, b"payload")
    archive_fixture.status_sequence[f"{rel}.CHECKSUM"] = [503, 503, 200]
    # After retries succeed, put checksum body back via files (200 uses files)
    archive_fixture.files[f"{rel}.CHECKSUM"] = (
        f"{digest}  BTCUSDT-aggTrades-2024-01-01.zip\n".encode("ascii")
    )
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture, max_retries=2)
    result = collector.collect(_request())
    assert len(result.objects) == 1
    checksum_calls = [u for u in archive_fixture.requests if u.endswith(".CHECKSUM")]
    assert len(checksum_calls) == 3  # 1 + 2 retries


def test_transient_retry_exhausted(tmp_path: Path, archive_fixture: ArchiveFixture) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    archive_fixture.put_zip(rel, b"payload")
    archive_fixture.status_sequence[f"{rel}.CHECKSUM"] = [503, 503, 503]
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture, max_retries=2)
    with pytest.raises(CollectionFailed):
        collector.collect(_request())
    checksum_calls = [u for u in archive_fixture.requests if u.endswith(".CHECKSUM")]
    assert len(checksum_calls) == 3


def test_ordinary_4xx_not_retried(tmp_path: Path, archive_fixture: ArchiveFixture) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    archive_fixture.put_zip(rel, b"payload")
    archive_fixture.status_sequence[f"{rel}.CHECKSUM"] = [400]
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture, max_retries=5)
    with pytest.raises(CollectionFailed, match="HTTP 400"):
        collector.collect(_request())
    checksum_calls = [u for u in archive_fixture.requests if u.endswith(".CHECKSUM")]
    assert len(checksum_calls) == 1


def test_connect_error_retries(tmp_path: Path, archive_fixture: ArchiveFixture) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    archive_fixture.put_zip(rel, b"payload")
    archive_fixture.fail_connect_times[f"{rel}.CHECKSUM"] = 2
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture, max_retries=2)
    result = collector.collect(_request())
    assert len(result.objects) == 1


def test_zip_404_after_checksum_is_collection_failed(
    tmp_path: Path, archive_fixture: ArchiveFixture
) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    digest = hashlib.sha256(b"missing-zip").hexdigest()
    archive_fixture.files[f"{rel}.CHECKSUM"] = (
        f"{digest}  BTCUSDT-aggTrades-2024-01-01.zip\n".encode("ascii")
    )
    archive_fixture.zip_status[rel] = 404
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture, max_retries=0)
    with pytest.raises(CollectionFailed, match="inconsistency"):
        collector.collect(_request())


def test_replay_and_reopen_idempotent(tmp_path: Path, archive_fixture: ArchiveFixture) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    archive_fixture.put_zip(rel, b"stable-bytes")
    storage = make_storage(tmp_path)
    clock_values = [
        datetime(2026, 9, 24, 1, tzinfo=UTC),
        datetime(2026, 9, 24, 2, tzinfo=UTC),
        datetime(2026, 9, 24, 3, tzinfo=UTC),
    ]
    clock_iter = iter(clock_values)

    def clock() -> datetime:
        return next(clock_iter)

    first = make_collector(storage, archive_fixture, clock=clock)
    req = _request()
    a = first.collect(req)
    b = first.collect(req)
    second = make_collector(storage, archive_fixture, clock=clock)
    c = second.collect(req)
    assert [(o.ref.key, o.ref.sha256, o.ref.size, o.source_uri) for o in a.objects] == [
        (o.ref.key, o.ref.sha256, o.ref.size, o.source_uri) for o in b.objects
    ]
    assert [(o.ref.key, o.ref.sha256, o.ref.size, o.source_uri) for o in a.objects] == [
        (o.ref.key, o.ref.sha256, o.ref.size, o.source_uri) for o in c.objects
    ]
    assert a.gaps == b.gaps == c.gaps
    assert a.objects[0].retrieved_at != b.objects[0].retrieved_at


def test_same_key_different_content_conflicts_and_keeps_old(
    tmp_path: Path, archive_fixture: ArchiveFixture
) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    key = "raw/binance/spot/archive/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    archive_fixture.put_zip(rel, b"original-bytes")
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture, max_retries=0)
    first = collector.collect(_request())
    old_ref = first.objects[0].ref

    archive_fixture.put_zip(rel, b"mutated-bytes")
    with pytest.raises(CollectionFailed):
        collector.collect(_request(request_id="replay-mutated"))
    assert storage.lookup(key) == old_ref
    with storage.open_read(old_ref) as handle:
        assert handle.read() == b"original-bytes"


def test_zip_streamed_in_chunks_without_content_aggregation(
    tmp_path: Path, archive_fixture: ArchiveFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    payload = b"A" * 200_000
    archive_fixture.put_zip(rel, payload)
    archive_fixture.chunk_sizes[rel] = 8192
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture)

    seen_chunks: list[int] = []
    original_iter = httpx.Response.iter_bytes

    def tracking_iter(self: httpx.Response, chunk_size: int | None = None) -> Iterator[bytes]:
        for chunk in original_iter(self, chunk_size=chunk_size):
            if str(self.request.url).endswith(".zip"):
                seen_chunks.append(len(chunk))
            yield chunk

    monkeypatch.setattr(httpx.Response, "iter_bytes", tracking_iter)

    opened: list[archive_mod._ZipByteStream] = []
    original_stream = archive_mod._ZipByteStream

    class TrackingStream(original_stream):  # type: ignore[valid-type,misc]
        def __init__(self, response: httpx.Response) -> None:
            super().__init__(response)
            opened.append(self)

    monkeypatch.setattr(archive_mod, "_ZipByteStream", TrackingStream)

    result = collector.collect(_request())
    assert result.objects[0].ref.size == len(payload)
    assert len(opened) == 1
    assert sum(seen_chunks) == len(payload)
    assert len(seen_chunks) > 1
    # Production ZIP path must not aggregate via response.content / .read().
    zip_helpers = inspect.getsource(archive_mod._ZipByteStream) + inspect.getsource(
        archive_mod.BinanceSpotArchiveCollector._fetch_and_publish_zip
    )
    assert ".content" not in zip_helpers
    assert ".read(" not in zip_helpers


def test_unsupported_requests(tmp_path: Path, archive_fixture: ArchiveFixture) -> None:
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture)
    with pytest.raises(UnsupportedRequest):
        collector.collect(_request(symbols=("BNBUSDT",)))
    with pytest.raises(UnsupportedRequest):
        collector.collect(_request(data_type="trades"))
    with pytest.raises(UnsupportedRequest):
        collector.collect(
            _request(source=SourceBinding(source_id="binance.public.spot.archive", version="9.0.0"))
        )
    with pytest.raises(UnsupportedRequest):
        collector.collect(
            _request(
                start=datetime(2024, 1, 1, 12, tzinfo=UTC),
                end=datetime(2024, 1, 2, tzinfo=UTC),
            )
        )


def test_from_settings_uses_archive_base_only(
    tmp_path: Path, archive_fixture: ArchiveFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HLENS_CATALOG_URI", "postgresql://u:p@127.0.0.1:5432/db")
    monkeypatch.setenv("HLENS_BINANCE_ARCHIVE_BASE_URL", ARCHIVE_BASE)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    archive_fixture.put_zip(rel, b"from-settings")
    storage = make_storage(tmp_path)
    collector = BinanceSpotArchiveCollector.from_settings(
        settings,
        storage,
        http_transport=archive_fixture.transport(),
        clock=lambda: datetime(2026, 9, 24, tzinfo=UTC),
    )
    assert collector.descriptor.collector_id == COLLECTOR_ID
    assert collector.descriptor.network_origins == (ARCHIVE_ORIGIN,)
    result = collector.collect(_request())
    assert len(result.objects) == 1


def test_partial_failure_does_not_return_result(
    tmp_path: Path, archive_fixture: ArchiveFixture
) -> None:
    day1 = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    day2 = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-02.zip"
    archive_fixture.put_zip(day1, b"day1")
    archive_fixture.put_zip(day2, b"day2")
    archive_fixture.status_sequence[f"{day2}.CHECKSUM"] = [500]
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture, max_retries=0)
    with pytest.raises(CollectionFailed):
        collector.collect(
            _request(start=datetime(2024, 1, 1, tzinfo=UTC), end=datetime(2024, 1, 3, tzinfo=UTC))
        )
    # Day1 may already be published (idempotent), but collect itself must not return success.
    assert (
        storage.lookup(
            "raw/binance/spot/archive/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
        )
        is not None
    )
    assert (
        storage.lookup(
            "raw/binance/spot/archive/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-02.zip"
        )
        is None
    )


def test_source_static_guards() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    lowered = source.lower()
    forbidden = (
        "data-api.binance",
        "api.binance.com",
        "/api/v3/order",
        "/api/v3/account",
        "private api",
        "pyiceberg",
        "revision_id",
        "zipfile",
        "binance_market_data_base_url",
    )
    for token in forbidden:
        assert token not in lowered, token
    tree = ast.parse(source)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    assert not any("iceberg" in name or "parser" in name for name in imports)
    stream_src = inspect.getsource(archive_mod._ZipByteStream)
    assert ".content" not in stream_src
    assert ".read(" not in stream_src


def test_checksum_binary_mode_separator_accepted(
    tmp_path: Path, archive_fixture: ArchiveFixture
) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    payload = b"bin-mode"
    digest = hashlib.sha256(payload).hexdigest()
    archive_fixture.files[rel] = payload
    archive_fixture.files[f"{rel}.CHECKSUM"] = (
        f"{digest} *BTCUSDT-aggTrades-2024-01-01.zip\n".encode("ascii")
    )
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture)
    result = collector.collect(_request())
    assert result.objects[0].source_sha256 == digest


def test_object_conflict_type_surfaces_as_collection_failed(
    tmp_path: Path, archive_fixture: ArchiveFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    archive_fixture.put_zip(rel, b"payload")
    storage = make_storage(tmp_path)

    def boom(staged):  # type: ignore[no-untyped-def]
        raise ObjectConflict("conflict")

    monkeypatch.setattr(storage, "publish", boom)
    collector = make_collector(storage, archive_fixture)
    with pytest.raises(CollectionFailed, match="conflict"):
        collector.collect(_request())


def _count_urls(requests: list[str], *, suffix: str) -> int:
    return sum(1 for url in requests if url.endswith(suffix))


def test_checksum_midstream_read_error_retries_once(
    tmp_path: Path, archive_fixture: ArchiveFixture
) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    payload = b"0123456789abcdefghij"
    archive_fixture.put_zip(rel, payload)
    checksum_path = f"{rel}.CHECKSUM"
    archive_fixture.stream_fail_after[checksum_path] = [10, None]
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture, max_retries=1)
    result = collector.collect(_request())
    assert len(result.objects) == 1
    assert _count_urls(archive_fixture.requests, suffix=".CHECKSUM") == 2
    assert _count_urls(archive_fixture.requests, suffix=".zip") == 1


def test_checksum_midstream_exhausted_never_fetches_zip(
    tmp_path: Path, archive_fixture: ArchiveFixture
) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    payload = b"0123456789abcdefghij"
    archive_fixture.put_zip(rel, payload)
    checksum_path = f"{rel}.CHECKSUM"
    archive_fixture.stream_fail_after[checksum_path] = [10, 10]
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture, max_retries=1)
    with pytest.raises(CollectionFailed):
        collector.collect(_request())
    assert _count_urls(archive_fixture.requests, suffix=".CHECKSUM") == 2
    assert _count_urls(archive_fixture.requests, suffix=".zip") == 0
    assert (
        storage.lookup(
            "raw/binance/spot/archive/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
        )
        is None
    )


def test_zip_midstream_read_error_retries_without_regetting_checksum(
    tmp_path: Path, archive_fixture: ArchiveFixture
) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    payload = b"Z" * 40
    archive_fixture.put_zip(rel, payload)
    archive_fixture.stream_fail_after[rel] = [10, None]
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture, max_retries=1)
    result = collector.collect(_request())
    assert len(result.objects) == 1
    assert result.objects[0].ref.size == len(payload)
    assert _count_urls(archive_fixture.requests, suffix=".CHECKSUM") == 1
    assert _count_urls(archive_fixture.requests, suffix=".zip") == 2
    with storage.open_read(result.objects[0].ref) as handle:
        assert handle.read() == payload


def test_zip_midstream_exhausted_leaves_no_object(
    tmp_path: Path, archive_fixture: ArchiveFixture
) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    key = "raw/binance/spot/archive/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    payload = b"Z" * 40
    archive_fixture.put_zip(rel, payload)
    archive_fixture.stream_fail_after[rel] = [10, 10]
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture, max_retries=1)
    with pytest.raises(CollectionFailed):
        collector.collect(_request())
    assert _count_urls(archive_fixture.requests, suffix=".CHECKSUM") == 1
    assert _count_urls(archive_fixture.requests, suffix=".zip") == 2
    assert storage.lookup(key) is None
    published = [
        path
        for path in Path(str(tmp_path)).rglob("*")
        if path.is_file() and "BTCUSDT-aggTrades-2024-01-01.zip" in path.name
    ]
    assert published == []


def test_parse_integrity_and_4xx_still_not_retried(
    tmp_path: Path, archive_fixture: ArchiveFixture
) -> None:
    rel = "data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-01-01.zip"
    archive_fixture.files[rel] = b"payload"
    archive_fixture.checksum_override[f"{rel}.CHECKSUM"] = b"not-valid-checksum\n"
    storage = make_storage(tmp_path)
    collector = make_collector(storage, archive_fixture, max_retries=5)
    with pytest.raises(CollectionFailed):
        collector.collect(_request())
    assert _count_urls(archive_fixture.requests, suffix=".CHECKSUM") == 1
    assert _count_urls(archive_fixture.requests, suffix=".zip") == 0

    archive_fixture.requests.clear()
    archive_fixture.checksum_override.clear()
    wrong = hashlib.sha256(b"other").hexdigest()
    archive_fixture.files[f"{rel}.CHECKSUM"] = (
        f"{wrong}  BTCUSDT-aggTrades-2024-01-01.zip\n".encode("ascii")
    )
    with pytest.raises(CollectionFailed):
        collector.collect(_request(request_id="integrity"))
    assert _count_urls(archive_fixture.requests, suffix=".CHECKSUM") == 1
    assert _count_urls(archive_fixture.requests, suffix=".zip") == 1

    archive_fixture.requests.clear()
    archive_fixture.status_sequence[f"{rel}.CHECKSUM"] = [400]
    with pytest.raises(CollectionFailed, match="HTTP 400"):
        collector.collect(_request(request_id="fourxx"))
    assert _count_urls(archive_fixture.requests, suffix=".CHECKSUM") == 1

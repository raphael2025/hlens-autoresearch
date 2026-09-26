"""D-NET capability-run tool: every step offline (mock archive site, temporary SQLite catalog).

No network: the archive site is an ``httpx.MockTransport`` that serves a synthetic, real-format
klines ZIP and its ``.CHECKSUM``; the catalog is a throwaway SQLite catalog. The real run uses the
PostgreSQL catalog from ``.env.catalog`` (``docs/reviews/2026-09-26-dnet-real-data-capability.md``).
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest

from infrastructure.catalog import PHASE1_REGISTRY, PyIcebergCatalogAdapter, ensure_phase1_tables
from infrastructure.parser.binance_archive import member_filename
from infrastructure.settings import Settings
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.tools import dnet_capability_run as tool
from tests.infrastructure.catalog.catalog_support import SqliteCatalogHarness

ARCHIVE_BASE = "https://archive.test"
DAY = date(2025, 6, 1)  # microsecond ticks from 2025 on (D1 parser)
BARS = 30  # the first 30 minutes only: the quality report must show the rest as a gap


def _kline_lines(base: int) -> list[str]:
    start_us = int(datetime.combine(DAY, datetime.min.time(), tzinfo=UTC).timestamp()) * 10**6
    minute_us = 60 * 10**6
    lines = []
    for index in range(BARS):
        opened, closed = base + index, base + index + (1 if index % 2 else -1)
        high, low = max(opened, closed) + 2, min(opened, closed) - 2
        open_us = start_us + index * minute_us
        lines.append(
            f"{open_us},{opened}.00000000,{high}.00000000,{low}.00000000,{closed}.00000000,"
            f"1.50000000,{open_us + minute_us - 1},{closed * 3 // 2}.00000000,7,"
            f"0.50000000,{closed // 2}.00000000,0"
        )
    return lines


def _zip(symbol: str, lines: list[str]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            member_filename("klines_1m", symbol, DAY), "".join(f"{x}\n" for x in lines)
        )
    return buffer.getvalue()


class _Site:
    """A mock archive site: the ZIP and ``.CHECKSUM`` of each symbol's day; records requests."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.files: dict[str, bytes] = {}
        for symbol, base in (("BTCUSDT", 60_000), ("ETHUSDT", 3_000)):
            name = f"{symbol}-1m-{DAY.isoformat()}.zip"
            path = f"/data/spot/daily/klines/{symbol}/1m/{name}"
            body = _zip(symbol, _kline_lines(base))
            self.files[path] = body
            digest = hashlib.sha256(body).hexdigest()
            self.files[f"{path}.CHECKSUM"] = f"{digest}  {name}\n".encode()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        body = self.files.get(request.url.path)
        if body is None:
            return httpx.Response(404, request=request)
        return httpx.Response(200, content=body, request=request)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[PyIcebergCatalogAdapter, LocalFileStorageAdapter]]:
    harness = SqliteCatalogHarness(tmp_path, registry=PHASE1_REGISTRY)
    staging = harness.warehouse / "staging"
    staging.mkdir(parents=True)
    storage = LocalFileStorageAdapter(harness.warehouse_uri, staging.as_uri())
    try:
        adapter = harness.open_adapter()
        ensure_phase1_tables(adapter)
        yield adapter, storage
    finally:
        storage.close()
        harness.cleanup()


def _round_trip(payload: Any) -> Any:
    """What the next step reads back from the state file."""
    return json.loads(json.dumps(payload, default=str))


def test_every_step_runs_offline_and_stops_at_the_universe(
    world: tuple[PyIcebergCatalogAdapter, LocalFileStorageAdapter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, storage = world
    monkeypatch.setenv("HLENS_CATALOG_URI", "postgresql://u:p@127.0.0.1:5432/db")
    monkeypatch.setenv("HLENS_BINANCE_ARCHIVE_BASE_URL", ARCHIVE_BASE)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    site = _Site()

    collected = _round_trip(
        tool._collect(settings, storage, [DAY], http_transport=httpx.MockTransport(site.handler))
    )
    assert collected["gaps"] == [] and len(collected["objects"]) == 2
    # D0 only: each checksum is fetched before its archive, nothing outside the archive base.
    assert all(url.startswith(f"{ARCHIVE_BASE}/data/spot/daily/klines/") for url in site.requests)
    assert [u.endswith(".CHECKSUM") for u in site.requests] == [True, False, True, False]

    ingested = _round_trip(tool._ingest(adapter, storage, collected))
    assert [u["row_count"] for u in ingested["units"]] == [BARS, BARS]
    assert not any(u["replayed"] for u in ingested["units"])
    assert all(u["maximal_heads"] == 1 for u in ingested["units"])

    normalized = tool._normalize(adapter, storage, ingested)
    assert [u["canonical_revisions"] for u in normalized["units"]] == [BARS, BARS]

    reports = tool._report(adapter, storage, [DAY])["reports"]
    assert len(reports) == 2
    for report in reports:
        assert report["event_counts"]["bar_1m_gap"] == 1  # minutes 30 .. 1439 are missing
        assert "competing_heads" not in report["event_counts"]
        assert report["event_counts"]["evidence_gaps"] == 1

    selections = tool._pit(adapter, storage, [DAY])["selections"]
    by_bound = {(s["symbol"], s["adr_0032_bound"]): s for s in selections}
    for symbol in ("BTCUSDT", "ETHUSDT"):
        # Conservative (D-HIST): nothing is visible at the end of a day ingested later.
        assert by_bound[(symbol, False)]["statuses"] == {"absent": BARS}
        # ADR-0032 bound: every bar is visible 5 s after its close, well before the day ends.
        assert by_bound[(symbol, True)]["statuses"] == {"selected": BARS}
        assert by_bound[(symbol, True)]["assumed_revisions"] == BARS
        assert by_bound[(symbol, True)]["conflicts"] == 0

    stopped = tool._f2("https://market-data.test", adapter, storage, [DAY])
    assert stopped["outcome"] == "stopped"
    assert stopped["error_type"].endswith("UniverseSpecError")
    assert "canonical.instrument_listings" in stopped["tables_without_snapshot"]
    assert "raw.binance_spot_exchange_info" in stopped["tables_without_snapshot"]
    assert len(site.requests) == 4  # the dataset step made no request at all

    # Rerun = replay (08-deployment.md §6.2): nothing new is committed.
    again = tool._ingest(adapter, storage, collected)
    assert all(u["replayed"] for u in again["units"])
    assert all(r["reused"] for r in tool._report(adapter, storage, [DAY])["reports"])


def test_days_must_be_consecutive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HLENS_CATALOG_URI", "postgresql://u:p@127.0.0.1:5432/db")
    monkeypatch.setenv("HLENS_WAREHOUSE_URI", (tmp_path / "warehouse").as_uri())
    day, later = DAY.isoformat(), (DAY + timedelta(days=2)).isoformat()
    with pytest.raises(SystemExit):
        tool.main(["--state-dir", str(tmp_path / "state"), "--day", day, "--day", later, "f2"])

"""D-NET committed data replayed after the 2.1.0 bump (ADR-0052 versioned replay, M2 gate).

The D-NET capability steps (collect, ingest, normalize, report, pit, f2) run offline — the mock
archive site of ``test_dnet_capability_run`` and a temporary SQLite catalog; no download, no REST
or exchangeInfo call, never the real catalog or warehouse — as the 2.0.0 code
(``written_at("2.0.0")``). The steps are then run again by the current code on the same catalog:
every write step is a replay (nothing committed, every snapshot head and row unchanged, every row
still 2.0.0) and the read steps (quality reports, PIT, f2) answer exactly as before.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from core.domain.base import CONTRACT_SCHEMA_VERSION
from infrastructure.catalog import PHASE1_REGISTRY, PyIcebergCatalogAdapter, ensure_phase1_tables
from infrastructure.catalog.phase1_tables import PHASE1_TABLES
from infrastructure.settings import Settings
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.tools import dnet_capability_run as tool
from tests.infrastructure.catalog.catalog_support import SqliteCatalogHarness
from tests.infrastructure.contract_era import written_at
from tests.infrastructure.tools.test_dnet_capability_run import (
    ARCHIVE_BASE,
    BARS,
    DAY,
    _round_trip,
    _Site,
)

OLD = "2.0.0"


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


def _state(adapter: PyIcebergCatalogAdapter) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for table in PHASE1_TABLES:
        info = adapter.load_table(table.table)
        assert info is not None
        head = None if info.current_snapshot is None else info.current_snapshot.snapshot_id
        rows = (
            []
            if head is None
            else adapter.scan_columns(
                table.table, columns=tuple(field.name for field in table.arrow_schema)
            ).to_pylist()
        )
        state[table.table] = (
            head,
            sorted(json.dumps(row, default=repr, sort_keys=True) for row in rows),
        )
    return state


def _versions(adapter: PyIcebergCatalogAdapter) -> set[str]:
    found: set[str] = set()
    for table in PHASE1_TABLES:
        if "contract_schema_version" not in table.arrow_schema.names:
            continue
        info = adapter.load_table(table.table)
        if info is None or info.current_snapshot is None:
            continue
        found |= set(
            adapter.scan_columns(table.table, columns=("contract_schema_version",))
            .column("contract_schema_version")
            .to_pylist()
        )
    return found


def _without_timings(value: Any) -> Any:
    """The step outputs minus their measured ``wall_seconds`` (a timing, not an answer)."""
    if isinstance(value, dict):
        return {k: _without_timings(v) for k, v in value.items() if k != "wall_seconds"}
    if isinstance(value, list):
        return [_without_timings(item) for item in value]
    return value


def _reads(adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter) -> Any:
    reports = tool._report(adapter, storage, [DAY])["reports"]
    selections = tool._pit(adapter, storage, [DAY])["selections"]
    stopped = tool._f2("https://market-data.test", adapter, storage, [DAY])
    answers = json.loads(json.dumps([reports, selections, stopped], default=str, sort_keys=True))
    return _without_timings(answers)


def test_dnet_data_committed_at_2_0_0_replays_unchanged_at_2_1_0(
    world: tuple[PyIcebergCatalogAdapter, LocalFileStorageAdapter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert CONTRACT_SCHEMA_VERSION == "2.1.0"
    adapter, storage = world
    monkeypatch.setenv("HLENS_CATALOG_URI", "postgresql://u:p@127.0.0.1:5432/db")
    monkeypatch.setenv("HLENS_BINANCE_ARCHIVE_BASE_URL", ARCHIVE_BASE)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    site = _Site()
    with written_at(OLD):
        collected = _round_trip(
            tool._collect(
                settings, storage, [DAY], http_transport=httpx.MockTransport(site.handler)
            )
        )
        ingested = _round_trip(tool._ingest(adapter, storage, collected))
        normalized = tool._normalize(adapter, storage, ingested)
        assert [u["canonical_revisions"] for u in normalized["units"]] == [BARS, BARS]
        written = tool._report(adapter, storage, [DAY])["reports"]
        assert not any(report["reused"] for report in written)
        first_reads = _reads(adapter, storage)  # the reports now exist: every read reuses
    assert _versions(adapter) == {OLD}
    committed = _state(adapter)
    requests = len(site.requests)

    # the current code, as a fresh process would open the same catalog
    adapter = PyIcebergCatalogAdapter(adapter._catalog, PHASE1_REGISTRY)  # noqa: SLF001
    again = tool._ingest(adapter, storage, collected)
    assert all(unit["replayed"] for unit in again["units"])
    renormalized = tool._normalize(adapter, storage, _round_trip(again))
    assert [u["canonical_revisions"] for u in renormalized["units"]] == [BARS, BARS]
    assert _reads(adapter, storage) == first_reads
    assert all(report["reused"] for report in tool._report(adapter, storage, [DAY])["reports"])
    assert _state(adapter) == committed  # no snapshot, no row, no version changed
    assert _versions(adapter) == {OLD}
    assert len(site.requests) == requests  # nothing downloaded again

"""Operator capacity probe for the Phase 1 local data pipeline (G3-T; PROJECT_STATUS.md §7).

``python -m infrastructure.tools.capacity_probe --rows N`` builds ``N`` synthetic aggTrades rows
spread evenly over one UTC day, publishes them as one archive object into a temporary SQLite
catalog + local warehouse (the same shape ``tests.infrastructure.revision.rest_store_support``
and ``tests.infrastructure.catalog.catalog_support`` build for tests), then runs the unmodified
production pipeline over that unit:

1. **ingest** — ``RawRevisionStore.ingest`` (D1 parse + D2 persist) of the synthetic archive;
2. **normalize** — ``CanonicalNormalizer.normalize_unit`` of that archive revision (E1);
3. **pit_select_1h** — ``PitSelector.select`` over one UTC hour of the day (F1);
4. **quality_report_day** — ``QualityReporter.report`` over the whole UTC day (E3), which itself
   re-runs ``PitSelector`` an hour at a time (G3-S3).

Each stage is timed and memory-profiled; the tool prints one JSON summary to stdout. Nothing here
is imported by, or imports, the ``tests/`` tree: it reimplements the small amount of fixture
plumbing (a synthetic archive ZIP, a throwaway SQLite catalog + local warehouse) directly against
production modules, so it stays runnable outside pytest and never becomes a load-bearing test
double.

**Scope, read honestly**:

- this bypasses the real D0 archive collector and the D3D REST collector: the synthetic object is
  built and published in-process, not downloaded, so ``ingest_archive`` measures D1 parse + D2
  persist only, never network or checksum-verification-against-a-remote-source cost;
- only the archive channel is exercised (no REST tail, no cross-channel reconciliation), so the
  precedence-evidence table is never written and ``pit_select_1h`` / ``quality_report_day`` never
  pay for cross-channel edge mapping;
- ``tracemalloc`` only tracks Python-level allocations; PyArrow and PyIceberg buffers are native
  and mostly invisible to it, so ``tracemalloc_peak_mb`` understates real memory pressure.
  ``ru_maxrss_delta_mb`` (the process' resident-set high-water mark) is the more trustworthy
  number, but it is cumulative and non-decreasing across the whole process: a later stage that
  never exceeds an earlier stage's peak reports a delta of ``0.0``, not its own usage.

Refuses ``--rows`` above 50 000 unless ``--i-know-memory`` is passed (WSL has crashed from memory
exhaustion at multi-million-row scale; see PROJECT_STATUS.md §7).
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import tempfile
import time
import tracemalloc
import uuid
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any, Final

from pyiceberg.catalog.sql import SqlCatalog

from core.contracts.collector import CollectedObject
from core.contracts.revision import PointInTimeSpec, PointInTimeStatus
from core.contracts.storage import StageRequest
from core.domain.base import FrozenMapping
from infrastructure.canonical import rules
from infrastructure.canonical.normalizer import CanonicalNormalizer, CanonicalUnitNormalized
from infrastructure.catalog import PHASE1_REGISTRY, PyIcebergCatalogAdapter, ensure_phase1_tables
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_AGG_TRADES,
    BINANCE_SPOT_ARCHIVES,
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    CANONICAL_TRADES,
)
from infrastructure.collector.binance_archive import ARCHIVE_SOURCE, COLLECTOR_ID, COLLECTOR_VERSION
from infrastructure.parser.binance_archive import member_filename
from infrastructure.pit.selector import PIT_BINDING, REQUIRED_BINDINGS, PitSelection, PitSelector
from infrastructure.quality.reporter import QualityReported, QualityReporter
from infrastructure.revision import ArchiveContext, ArchiveIngested, RawRevisionStore
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from infrastructure.revision.identity import archive_object_key, archive_relative_path
from infrastructure.storage import LocalFileStorageAdapter

__all__ = ["main", "run_probe"]

#: Above this the tool refuses to run without an explicit override (WSL memory exhaustion).
MAX_ROWS_WITHOUT_OVERRIDE: Final = 50_000
ARCHIVE_BASE: Final = "https://data.binance.vision"
SYMBOL: Final = "BTCUSDT"
DATA_TYPE: Final = "agg_trades"
#: On or after this day the D1 parser expects microsecond ticks (infrastructure/parser
#: /binance_archive.py ``MICROSECOND_FROM``); a fixed, arbitrary probe day.
DAY: Final = date(2025, 6, 1)
TICKS_PER_SECOND: Final = 1_000_000
_SECONDS_PER_DAY: Final = 86_400

_DAY_START: Final = datetime(DAY.year, DAY.month, DAY.day, tzinfo=UTC)
#: The knowledge axis: retrieval, then each stage's clock reading, strictly in production order.
_RETRIEVED_AT: Final = _DAY_START + timedelta(days=1)
_KNOWLEDGE_INGEST: Final = _RETRIEVED_AT + timedelta(hours=1)
_KNOWLEDGE_NORMALIZE: Final = _KNOWLEDGE_INGEST + timedelta(hours=1)
_KNOWLEDGE_REPORT: Final = _KNOWLEDGE_NORMALIZE + timedelta(hours=1)
_FAR: Final = _KNOWLEDGE_REPORT + timedelta(days=3650)
#: The UTC hour selected by the ``pit_select_1h`` stage.
_SELECT_START: Final = _DAY_START + timedelta(hours=12)
_SELECT_END: Final = _SELECT_START + timedelta(hours=1)


# ============================================================================================
# measurement
# ============================================================================================


@dataclass(frozen=True, slots=True)
class StageMeasurement:
    wall_seconds: float
    tracemalloc_peak_mb: float
    ru_maxrss_delta_mb: float

    def as_dict(self) -> dict[str, float]:
        return {
            "wall_seconds": round(self.wall_seconds, 6),
            "tracemalloc_peak_mb": round(self.tracemalloc_peak_mb, 3),
            "ru_maxrss_delta_mb": round(self.ru_maxrss_delta_mb, 3),
        }


def _measure[T](fn: Callable[[], T]) -> tuple[T, StageMeasurement]:
    """Time ``fn`` and profile it; ``ru_maxrss`` is the process' cumulative high-water mark."""
    before_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    tracemalloc.start()
    start = time.perf_counter()
    try:
        result = fn()
    finally:
        elapsed = time.perf_counter() - start
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    after_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    measurement = StageMeasurement(
        wall_seconds=elapsed,
        tracemalloc_peak_mb=peak / (1024 * 1024),
        ru_maxrss_delta_mb=(after_rss_kb - before_rss_kb) / 1024,
    )
    return result, measurement


# ============================================================================================
# synthetic archive (reimplemented here, never imported from tests/)
# ============================================================================================


def _agg_trade_lines(rows: int) -> list[str]:
    """``rows`` structurally valid aggTrades CSV lines spread evenly over ``DAY`` (UTC)."""
    day_start_ticks = int(_DAY_START.timestamp()) * TICKS_PER_SECOND
    step = max(1, (_SECONDS_PER_DAY * TICKS_PER_SECOND) // rows)
    lines: list[str] = []
    for index in range(rows):
        agg_id = 1_000 + index
        first_id = 2_000 + index * 2
        last_id = first_id + 1
        ticks = day_start_ticks + index * step
        is_buyer_maker = "True" if index % 2 else "False"
        is_best_match = "True" if index % 3 else "False"
        lines.append(
            f"{agg_id},50000.00000000,0.01000000,{first_id},{last_id},{ticks},"
            f"{is_buyer_maker},{is_best_match}"
        )
    return lines


def _build_archive_bytes(rows: int) -> bytes:
    """One in-memory ZIP shaped exactly like a daily Binance archive (one CSV member)."""
    content = "".join(f"{line}\n" for line in _agg_trade_lines(rows)).encode("utf-8")
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_filename(DATA_TYPE, SYMBOL, DAY), content)
    return buffer.getvalue()


def _publish_archive(storage: LocalFileStorageAdapter, rows: int) -> CollectedObject:
    """Build and publish the synthetic archive object (fixture setup, not a measured stage)."""
    data = _build_archive_bytes(rows)
    digest = sha256(data).hexdigest()
    key = archive_object_key(DATA_TYPE, SYMBOL, DAY, digest)
    staged = storage.stage(StageRequest(key=key, expected_sha256=digest), [data])
    published = storage.publish(staged)
    relative = archive_relative_path(DATA_TYPE, SYMBOL, DAY)
    return CollectedObject(
        ref=published.ref,
        symbol=SYMBOL,
        coverage_start=_DAY_START,
        coverage_end=_DAY_START + timedelta(days=1),
        source_uri=f"{ARCHIVE_BASE}/{relative}",
        retrieved_at=_RETRIEVED_AT,
        source_sha256=digest,
    )


# ============================================================================================
# throwaway catalog + warehouse (same shape as tests.infrastructure.catalog.catalog_support)
# ============================================================================================


@contextmanager
def _harness(tmp_path: Path) -> Iterator[tuple[PyIcebergCatalogAdapter, LocalFileStorageAdapter]]:
    warehouse = tmp_path / "warehouse"
    staging = warehouse / "staging"
    warehouse.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    catalog = SqlCatalog(
        f"capacity_probe_{uuid.uuid4().hex[:16]}",
        uri=f"sqlite:///{tmp_path / 'catalog.sqlite'}",
        warehouse=warehouse.as_uri(),
    )
    storage = LocalFileStorageAdapter(warehouse.as_uri(), staging.as_uri())
    try:
        adapter = PyIcebergCatalogAdapter(catalog, PHASE1_REGISTRY)
        ensure_phase1_tables(adapter)
        yield adapter, storage
    finally:
        storage.close()
        catalog.close()


def _head(adapter: PyIcebergCatalogAdapter, table: str) -> str | None:
    info = adapter.load_table(table)
    if info is None or info.current_snapshot is None:
        return None
    return info.current_snapshot.snapshot_id


# ============================================================================================
# the four stages
# ============================================================================================


def _ingest_archive(
    adapter: PyIcebergCatalogAdapter,
    storage: LocalFileStorageAdapter,
    collected: CollectedObject,
    rows: int,
) -> ArchiveIngested:
    context = ArchiveContext(
        request_id=f"capacity-probe-{rows}",
        data_type=DATA_TYPE,
        collector_id=COLLECTOR_ID,
        collector_version=COLLECTOR_VERSION,
        source=ARCHIVE_SOURCE,
    )
    store = RawRevisionStore(adapter, storage, clock=lambda: _KNOWLEDGE_INGEST)
    outcome = store.ingest(collected, context)
    if not isinstance(outcome, ArchiveIngested):
        raise RuntimeError(f"the synthetic archive was rejected, not ingested: {outcome!r}")
    return outcome


def _normalize(
    adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter, archive_revision_id: str
) -> CanonicalUnitNormalized:
    normalizer = CanonicalNormalizer(adapter, storage, clock=lambda: _KNOWLEDGE_NORMALIZE)
    return normalizer.normalize_unit(BINANCE_SPOT_AGG_TRADES.table, archive_revision_id)


def _pit_spec(adapter: PyIcebergCatalogAdapter) -> PointInTimeSpec:
    tables = (
        BINANCE_SPOT_ARCHIVES.table,
        BINANCE_SPOT_AGG_TRADES.table,
        CANONICAL_TRADES.table,
        BINANCE_SPOT_PRECEDENCE_EVIDENCE.table,
    )
    bindings = {table: head for table in tables if (head := _head(adapter, table)) is not None}
    return PointInTimeSpec(
        name="hlens.tools.capacity-probe",
        version="1.0.0",
        simulation_time=_FAR,
        knowledge_cutoff=_FAR,
        snapshot_bindings=FrozenMapping(bindings),
        point_in_time_binding=PIT_BINDING,
        availability_bindings=REQUIRED_BINDINGS["availability_bindings"],
        precedence_bindings=(DELIVERY_CHANNEL_BINDING, rules.PRECEDENCE_MAP_BINDING),
        parser_bindings=REQUIRED_BINDINGS["parser_bindings"],
    )


def _pit_select_one_hour(
    adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter
) -> PitSelection:
    spec = _pit_spec(adapter)
    return PitSelector(adapter, storage).select(spec, DATA_TYPE, SYMBOL, _SELECT_START, _SELECT_END)


def _quality_report(
    adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter
) -> QualityReported:
    reporter = QualityReporter(adapter, storage, clock=lambda: _KNOWLEDGE_REPORT)
    return reporter.report(DATA_TYPE, SYMBOL, DAY)


# ============================================================================================
# orchestration
# ============================================================================================

_NOTES: Final[tuple[str, ...]] = (
    "ingest_archive times RawRevisionStore.ingest only (D1 parse + D2 persist); building and "
    "publishing the synthetic archive object happens before the measured window, so this never "
    "includes the real D0/D3D network collector.",
    "pit_select_1h and quality_report_day run the unmodified production PitSelector / "
    "QualityReporter over a single archive-only channel (no REST tail, no cross-channel "
    "reconciliation), so they exclude REST ingestion and reconciler cost.",
    "tracemalloc_peak_mb only sees Python-level allocations; PyArrow / PyIceberg buffers are "
    "native and mostly invisible to it, so it understates real memory pressure.",
    "ru_maxrss_delta_mb is the process' resident-set high-water mark, which only grows: it is "
    "cumulative across stages, so a later stage that never exceeds an earlier stage's peak can "
    "report 0.0 even though it allocated real memory that was then freed.",
)


def run_probe(rows: int) -> dict[str, Any]:
    """Run all four stages for ``rows`` synthetic aggTrades and return the JSON-able summary."""
    stages: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="hlens-capacity-probe-") as raw_tmp:
        with _harness(Path(raw_tmp)) as (adapter, storage):
            collected = _publish_archive(storage, rows)

            ingested, measurement = _measure(
                lambda: _ingest_archive(adapter, storage, collected, rows)
            )
            stages["ingest_archive"] = measurement.as_dict() | {
                "archive_revision_id": ingested.archive_revision_id,
                "row_count": ingested.row_count,
            }

            normalized, measurement = _measure(
                lambda: _normalize(adapter, storage, ingested.archive_revision_id)
            )
            stages["normalize"] = measurement.as_dict() | {
                "canonical_rows": len(normalized.revision_ids),
            }

            selection, measurement = _measure(lambda: _pit_select_one_hour(adapter, storage))
            selected = sum(
                1 for item in selection.selections if item.status is PointInTimeStatus.SELECTED
            )
            stages["pit_select_1h"] = measurement.as_dict() | {
                "window_start": _SELECT_START.isoformat(),
                "window_end": _SELECT_END.isoformat(),
                "keys_evaluated": len(selection.records),
                "keys_selected": selected,
            }

            report, measurement = _measure(lambda: _quality_report(adapter, storage))
            stages["quality_report_day"] = measurement.as_dict() | {
                "event_count": len(report.row["events"]),
                "reused": report.reused,
            }

    return {
        "rows": rows,
        "symbol": SYMBOL,
        "data_type": DATA_TYPE,
        "day": DAY.isoformat(),
        "stages": stages,
        "notes": list(_NOTES),
    }


# ============================================================================================
# CLI
# ============================================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m infrastructure.tools.capacity_probe",
        description=(
            "Generate N synthetic aggTrades spread over one UTC day into a temporary "
            "SQLite-catalog harness, then run ingest -> normalize -> a one-hour PIT selection "
            "-> a day quality report, timing and memory-profiling each stage."
        ),
    )
    parser.add_argument(
        "--rows", type=int, required=True, help="number of synthetic aggTrades rows to generate"
    )
    parser.add_argument(
        "--i-know-memory",
        action="store_true",
        help=(
            f"allow --rows above {MAX_ROWS_WITHOUT_OVERRIDE}; WSL has crashed from memory "
            "exhaustion at this scale, use with care"
        ),
    )
    args = parser.parse_args(argv)
    if args.rows < 1:
        parser.error("--rows must be at least 1")
    if args.rows > MAX_ROWS_WITHOUT_OVERRIDE and not args.i_know_memory:
        parser.error(
            f"--rows {args.rows} exceeds {MAX_ROWS_WITHOUT_OVERRIDE}; pass --i-know-memory to "
            "run it anyway (WSL has crashed from memory exhaustion at this scale; "
            "PROJECT_STATUS.md §7)"
        )
    summary = run_probe(args.rows)
    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

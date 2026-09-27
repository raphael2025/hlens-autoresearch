"""D-NET real-archive capability run: klines_1m, BTCUSDT / ETHUSDT, a few UTC days (operator tool).

Raphael approved D-NET on 2026-09-26: download the official Binance spot **public daily
archives** (no keys), write to the local machine only. This tool walks
``docs/architecture/08-deployment.md`` §6.1 over the **real** catalog configured by ``Settings``
(``.env.catalog``), one step per process so each step's wall time and peak RSS are its own:

- ``collect``   D0 ``BinanceSpotArchiveCollector`` (``.CHECKSUM`` first, then the ZIP);
- ``ingest``    D2 ``RawRevisionStore.ingest`` of every collected object;
- ``normalize`` E1 ``CanonicalNormalizer.normalize_unit`` of every ingested archive revision;
- ``report``    E3 ``QualityReporter.report`` of every ``(klines_1m, symbol, day)`` partition;
- ``pit``       F1 ``PitSelector.select`` (read-only) per partition, once conservative and once with
  the ADR-0032 assumption bound, at the end of each day with the knowledge cutoff = now;
- ``f2``        F2 / F3 ``DatasetBuilder.select`` (read-only, **no network**) at the current heads,
  to record where the dataset path stops (the universe needs a listing history, E2).

Scope and red lines: ``klines_1m`` only; the only network origin is the configured archive base
(D0); no REST / ``exchangeInfo`` call is made by any step (``f2`` passes the market-data base only
as the origin string the universe builder checks stored rows against). Nothing here writes to the
repository; step records go to ``--state-dir`` (use a directory under the git-ignored ``data/``).
Every command must run under a ``systemd-run`` memory cap (08-deployment.md §6.3). This is a
capability check, **never a market conclusion**.

Usage (from the repository root, never echo the env file)::

    systemd-run --user --scope --quiet -p MemoryMax=3G -p MemorySwapMax=0 \\
      uv run --env-file <project>/.env.catalog python -m infrastructure.tools.dnet_capability_run \\
      --state-dir <project>/data/dnet-run --day 2026-09-21 --day 2026-09-22 collect
"""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import time
import traceback
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any, Final

import httpx

from core.contracts.collector import CollectedObject, CollectionRequest
from core.contracts.revision import PointInTimeSpec
from core.domain.base import FrozenMapping
from infrastructure.canonical import listing_rules as lr
from infrastructure.canonical import rules
from infrastructure.canonical.normalizer import CanonicalNormalizer
from infrastructure.catalog import PHASE1_REGISTRY, PyIcebergCatalogAdapter
from infrastructure.catalog.iceberg_adapter import open_postgres_catalog_adapter
from infrastructure.catalog.phase1_tables import (
    DATASET_SELECTIONS,
    PHASE1_TABLES,
)
from infrastructure.collector.binance_archive import ARCHIVE_SOURCE, BinanceSpotArchiveCollector
from infrastructure.dataset.builder import DatasetBuilder
from infrastructure.pit.assumption import ASSUMPTION_BINDING
from infrastructure.pit.selector import PIT_BINDING, PitSelector
from infrastructure.quality.reporter import QualityReporter
from infrastructure.revision import ArchiveContext, ArchiveIngested, RawRevisionStore
from infrastructure.revision.channel_precedence import DELIVERY_CHANNEL_BINDING
from infrastructure.revision.exchange_info_availability import EXCHANGE_INFO_AVAILABILITY_BINDING
from infrastructure.settings import Settings, local_file_uri_to_path
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE

__all__ = ["main"]

DATA_TYPE: Final = "klines_1m"
SYMBOLS: Final = ("BTCUSDT", "ETHUSDT")
_DAY: Final = timedelta(days=1)
STEPS: Final = ("collect", "ingest", "normalize", "report", "pit", "f2")
_REPO_ROOT: Final = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class _StepOutcome:
    result: Any
    snapshot_heads_before: dict[str, str] | None
    snapshot_heads_after: dict[str, str] | None


def _day_start(day: date) -> datetime:
    return datetime.combine(day, dtime(), tzinfo=UTC)


def _peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _days_iso(days: list[date]) -> list[str]:
    return [d.isoformat() for d in sorted(set(days))]


def _code_revision(repo_root: Path = _REPO_ROOT) -> dict[str, Any]:
    """Git HEAD sha and dirty flag; nulls if git is unavailable (never raises)."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
        return {"commit": commit or None, "dirty": bool(porcelain.strip())}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}


def _load(state_dir: Path, name: str) -> Any:
    return json.loads((state_dir / f"{name}.json").read_text(encoding="utf-8"))


def _load_prior(state_dir: Path, name: str, days: list[date]) -> Any:
    """Load a prior step record; refuse stale, failed, or day-mismatched state (fail closed)."""
    ok_path = state_dir / f"{name}.json"
    failed_path = state_dir / f"{name}.failed.json"
    if not ok_path.is_file():
        raise SystemExit(f"missing prior step state: {ok_path.name} (refusing to proceed)")
    if failed_path.is_file() and failed_path.stat().st_mtime_ns >= ok_path.stat().st_mtime_ns:
        raise SystemExit(
            f"prior step {name!r} has a failed record newer than or equal to {ok_path.name}; "
            "refusing to consume stale or superseded state"
        )
    record = _load(state_dir, name)
    if record.get("status") != "ok":
        raise SystemExit(
            f"prior step {name!r} record status is {record.get('status')!r}, expected 'ok'"
        )
    expected = _days_iso(days)
    actual = record.get("days")
    if actual != expected:
        raise SystemExit(
            f"prior step {name!r} days {actual!r} do not match current --day set {expected!r}"
        )
    return record


def _save(state_dir: Path, name: str, payload: Any) -> None:
    (state_dir / f"{name}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )


def _invalidate_ok_state(state_dir: Path, step: str) -> None:
    """Remove a previous successful ``<step>.json`` so later steps cannot read it."""
    path = state_dir / f"{step}.json"
    if path.is_file():
        path.unlink()


def _clear_failed_state(state_dir: Path, step: str) -> None:
    path = state_dir / f"{step}.failed.json"
    if path.is_file():
        path.unlink()


def _heads(adapter: PyIcebergCatalogAdapter, *, exclude: tuple[str, ...] = ()) -> dict[str, str]:
    """The current snapshot of every Phase 1 table that has one (``exclude`` left out)."""
    heads: dict[str, str] = {}
    for definition in PHASE1_TABLES:
        if definition.table in exclude:
            continue
        info = adapter.load_table(definition.table)
        if info is not None and info.current_snapshot is not None:
            heads[definition.table] = info.current_snapshot.snapshot_id
    return heads


# ============================================================================================
# steps
# ============================================================================================


def _collect(
    settings: Settings,
    storage: LocalFileStorageAdapter,
    days: list[date],
    *,
    http_transport: httpx.BaseTransport | None = None,
) -> Any:
    out: dict[str, Any] = {"objects": [], "gaps": [], "per_request": []}
    with BinanceSpotArchiveCollector.from_settings(
        settings, storage, http_transport=http_transport
    ) as collector:
        for symbol in SYMBOLS:
            request = CollectionRequest(
                request_id=f"dnet-{DATA_TYPE}-{symbol.lower()}-{days[0]}-{days[-1]}",
                source=ARCHIVE_SOURCE,
                data_type=DATA_TYPE,
                symbols=(symbol,),
                coverage_start=_day_start(days[0]),
                coverage_end=_day_start(days[-1]) + _DAY,
            )
            started = time.perf_counter()
            result = collector.collect(request)
            out["per_request"].append(
                {
                    "request_id": request.request_id,
                    "wall_seconds": round(time.perf_counter() - started, 3),
                    "objects": len(result.objects),
                    "gaps": len(result.gaps),
                }
            )
            for obj in result.objects:
                out["objects"].append(
                    {
                        "request_id": request.request_id,
                        "collector_id": result.collector_id,
                        "collector_version": result.collector_version,
                        "object": json.loads(obj.model_dump_json()),
                    }
                )
            out["gaps"].extend(json.loads(gap.model_dump_json()) for gap in result.gaps)
    return out


def _ingest(adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter, state: Any) -> Any:
    store = RawRevisionStore(adapter, storage)
    units = []
    for item in state["objects"]:
        collected = CollectedObject.model_validate(item["object"])
        context = ArchiveContext(
            request_id=item["request_id"],
            data_type=DATA_TYPE,
            collector_id=item["collector_id"],
            collector_version=item["collector_version"],
            source=ARCHIVE_SOURCE,
        )
        started = time.perf_counter()
        outcome = store.ingest(collected, context)
        elapsed = round(time.perf_counter() - started, 3)
        if not isinstance(outcome, ArchiveIngested):
            units.append(
                {
                    "symbol": collected.symbol,
                    "day": collected.coverage_start.date().isoformat(),
                    "rejected": repr(outcome.quality_event),
                    "wall_seconds": elapsed,
                }
            )
            continue
        units.append(
            {
                "symbol": collected.symbol,
                "day": collected.coverage_start.date().isoformat(),
                "archive_revision_id": outcome.archive_revision_id,
                "rows_table": outcome.rows_table,
                "row_count": outcome.row_count,
                "row_revision_count": outcome.row_revision_count,
                "row_batches": len(outcome.row_commits),
                "replayed": outcome.replayed,
                "availability_gaps": [
                    {"table": g.table, "gap": g.gap, "revisions": g.revision_count}
                    for g in outcome.availability_gaps
                ],
                "maximal_heads": len(outcome.maximal_heads),
                "competing_revision_ids": list(outcome.competing_revision_ids),
                "supersedes": list(outcome.supersedes),
                "wall_seconds": elapsed,
            }
        )
    return {"units": units}


def _normalize(
    adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter, state: Any
) -> Any:
    normalizer = CanonicalNormalizer(adapter, storage)
    units = []
    for unit in state["units"]:
        if "archive_revision_id" not in unit:
            continue
        started = time.perf_counter()
        result = normalizer.normalize_unit(unit["rows_table"], unit["archive_revision_id"])
        units.append(
            {
                "symbol": unit["symbol"],
                "day": unit["day"],
                "source_revision_id": result.source_revision_id,
                "canonical_table": result.canonical_table,
                "canonical_revisions": result.row_count,
                "batches": len(result.commits),
                "replayed": result.replayed,
                "knowledge_time": result.knowledge_time,
                "wall_seconds": round(time.perf_counter() - started, 3),
            }
        )
    return {"units": units}


def _report(
    adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter, days: list[date]
) -> Any:
    reporter = QualityReporter(adapter, storage)
    reports = []
    for symbol in SYMBOLS:
        for day in days:
            started = time.perf_counter()
            reported = reporter.report(DATA_TYPE, symbol, day)
            row = reported.row
            events = list(row["events"])
            by_type = Counter(event["event_type"] for event in events)
            summary: dict[str, Any] = {
                "symbol": symbol,
                "day": day.isoformat(),
                "report_id": reported.report_id,
                "reused": reported.reused,
                "subject_table": row["subject_table"],
                "subject_snapshot_id": row["subject_snapshot_id"],
                "event_counts": dict(sorted(by_type.items())),
                "wall_seconds": round(time.perf_counter() - started, 3),
            }
            for event in events:
                if event["event_type"] in {"bar_1m_gap", "competing_heads", "evidence_gaps"}:
                    summary.setdefault("event_details", []).append(
                        {
                            "type": event["event_type"],
                            "start": event.get("event_start"),
                            "end": event.get("event_end"),
                            "detail": event.get("detail"),
                        }
                    )
            reports.append(summary)
    return {"reports": reports}


def _pit_spec(
    heads: dict[str, str], *, at: datetime, cutoff: datetime, assumed: bool
) -> PointInTimeSpec:
    availability = (rules.AVAILABILITY_BINDING, *((ASSUMPTION_BINDING,) if assumed else ()))
    return PointInTimeSpec(
        name="hlens.tools.dnet-capability",
        version="1.0.0",
        simulation_time=at,
        knowledge_cutoff=cutoff,
        snapshot_bindings=FrozenMapping(heads),
        point_in_time_binding=PIT_BINDING,
        availability_bindings=availability,
        precedence_bindings=(DELIVERY_CHANNEL_BINDING, rules.PRECEDENCE_MAP_BINDING),
        parser_bindings=(rules.NORMALIZER_BINDING,),
    )


def _pit(
    adapter: PyIcebergCatalogAdapter, storage: LocalFileStorageAdapter, days: list[date]
) -> Any:
    heads = _heads(adapter, exclude=(DATASET_SELECTIONS.table,))
    cutoff = datetime.now(UTC)
    selector = PitSelector(adapter, storage)
    out = []
    for symbol in SYMBOLS:
        for day in days:
            start = _day_start(day)
            for assumed in (False, True):
                spec = _pit_spec(heads, at=start + _DAY, cutoff=cutoff, assumed=assumed)
                started = time.perf_counter()
                selection = selector.select(spec, DATA_TYPE, symbol, start, start + _DAY)
                statuses = Counter(s.status.value for s in selection.selections)
                out.append(
                    {
                        "symbol": symbol,
                        "day": day.isoformat(),
                        "adr_0032_bound": assumed,
                        "keys": len(selection.records),
                        "statuses": dict(sorted(statuses.items())),
                        "conflicts": len(selection.conflicts),
                        "evidence_gaps": len(selection.evidence_gaps),
                        "assumed_revisions": len(selection.assumed),
                        "wall_seconds": round(time.perf_counter() - started, 3),
                    }
                )
    return {"simulation_time": "end of each UTC day", "knowledge_cutoff": cutoff, "selections": out}


def _f2(
    market_data_base_url: str,
    adapter: PyIcebergCatalogAdapter,
    storage: LocalFileStorageAdapter,
    days: list[date],
) -> Any:
    """Read-only dataset selection at the current heads; records where it stops (no network)."""
    heads = _heads(adapter, exclude=(DATASET_SELECTIONS.table,))
    start, end = _day_start(days[0]), _day_start(days[-1]) + _DAY
    spec = PointInTimeSpec(
        name="hlens.tools.dnet-capability.dataset",
        version="1.0.0",
        simulation_start=start,
        simulation_end=end,
        knowledge_cutoff=datetime.now(UTC),
        snapshot_bindings=FrozenMapping(heads),
        point_in_time_binding=PIT_BINDING,
        availability_bindings=(
            rules.AVAILABILITY_BINDING,
            EXCHANGE_INFO_AVAILABILITY_BINDING,
            ASSUMPTION_BINDING,
        ),
        precedence_bindings=(
            DELIVERY_CHANNEL_BINDING,
            rules.PRECEDENCE_MAP_BINDING,
            lr.LISTING_OBSERVATION_BINDING,
        ),
        parser_bindings=(rules.NORMALIZER_BINDING, lr.LISTING_STATUS_BINDING),
    )
    builder = DatasetBuilder(
        adapter,
        storage,
        market_data_base_url=market_data_base_url,
        dataset_table=DATASET_SELECTIONS,
    )
    unbound = sorted(d.table for d in PHASE1_TABLES if d.table not in heads)
    try:
        selection = builder.select(FIRST_SLICE_UNIVERSE, spec, DATA_TYPE, start, end)
    except Exception as exc:  # noqa: BLE001 - the stop point is the finding
        return {
            "outcome": "stopped",
            "error_type": f"{type(exc).__module__}.{type(exc).__name__}",
            "error": str(exc),
            "tables_without_snapshot": unbound,
        }
    return {"outcome": "selected", "rows": len(selection.rows), "tables_without_snapshot": unbound}


# ============================================================================================
# entry point
# ============================================================================================


def _run(args: argparse.Namespace) -> _StepOutcome:
    settings = Settings()  # type: ignore[call-arg]
    days: list[date] = sorted(set(args.day))
    if [d - days[0] for d in days] != [timedelta(days=i) for i in range(len(days))]:
        raise SystemExit("--day values must be consecutive UTC days")
    state_dir: Path = args.state_dir
    storage = LocalFileStorageAdapter.from_settings(settings)
    try:
        if args.step == "collect":
            return _StepOutcome(_collect(settings, storage, days), None, None)
        # Validate prior state before opening the catalog (fail closed, no DB on bad state).
        prior_result: Any | None = None
        if args.step == "ingest":
            prior_result = _load_prior(state_dir, "collect", days)["result"]
        elif args.step == "normalize":
            prior_result = _load_prior(state_dir, "ingest", days)["result"]
        with open_postgres_catalog_adapter(settings, PHASE1_REGISTRY) as adapter:
            before = _heads(adapter)
            steps: dict[str, Callable[[], Any]] = {
                "ingest": lambda: _ingest(adapter, storage, prior_result),
                "normalize": lambda: _normalize(adapter, storage, prior_result),
                "report": lambda: _report(adapter, storage, days),
                "pit": lambda: _pit(adapter, storage, days),
                "f2": lambda: _f2(
                    str(settings.binance_market_data_base_url), adapter, storage, days
                ),
            }
            result = steps[args.step]()
            after = _heads(adapter)
            return _StepOutcome(result, before, after)
    finally:
        storage.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m infrastructure.tools.dnet_capability_run")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--day", type=date.fromisoformat, action="append", required=True)
    parser.add_argument("step", choices=STEPS)
    args = parser.parse_args(argv)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings()  # type: ignore[call-arg]
    warehouse = local_file_uri_to_path(settings.warehouse_uri, field_name="warehouse_uri")
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    heads_before: dict[str, str] | None = None
    heads_after: dict[str, str] | None = None
    try:
        outcome = _run(args)
        payload = outcome.result
        heads_before = outcome.snapshot_heads_before
        heads_after = outcome.snapshot_heads_after
        status = "ok"
    except Exception as exc:  # noqa: BLE001 - reported, then a non-zero exit
        payload = {
            "error_type": f"{type(exc).__module__}.{type(exc).__name__}",
            "error": str(exc),
            "traceback": traceback.format_exc(limit=8),
        }
        status = "failed"
    record = {
        "step": args.step,
        "status": status,
        "started_at": started_at,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mb": round(_peak_rss_mb(), 1),
        "warehouse": str(warehouse),
        "days": _days_iso(list(args.day)),
        "code_revision": _code_revision(),
        "snapshot_heads_before": heads_before,
        "snapshot_heads_after": heads_after,
        "result": payload,
    }
    if status == "ok":
        _clear_failed_state(args.state_dir, args.step)
        _save(args.state_dir, args.step, record)
    else:
        _invalidate_ok_state(args.state_dir, args.step)
        _save(args.state_dir, f"{args.step}.failed", record)
    with (args.state_dir / "steps.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True, default=str))
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())

"""Isolated-process capacity probe of the Canonical normalizer (E1-CAP-1; review 2026-09-27-e1).

``python -m infrastructure.tools.normalizer_memory_probe --rows 10000 100000 500000 \
--i-know-memory`` (the defaults: M = 256, narrow window 65 536, D2 row batch 4 096)

For each unit size ``N`` (fixed microbatch ``M``, narrow window and D2 row batch for every ``N``)
it builds one synthetic aggTrades archive in a throwaway SQLite catalog + local warehouse on
disk, ingests it through the real D1 parser + D2 store, and then runs each measured stage of the
unmodified production code **in its own fresh Python process**:

- ``verify_archive`` — strict archive verification: ``PersistedRowVerifier`` over the pinned
  heads proves every Raw row of the unit window by window (``M`` rows) against the spooled D1
  re-parse of its object, its D2 row batches and its archive revision (D3E-R3);
- ``write_crash`` — ``normalize_unit`` writing the unit until a catalog proxy raises right after
  its ``crash_after``-th Canonical commit (half the plan): the write path, then a crash;
- ``resume`` — ``normalize_unit`` in a new process over that committed prefix: the survey of
  the committed batches, the missing batches' write + read-back and the close (recovery);
- ``replay`` — ``normalize_unit`` again over the complete unit (the proving survey, no write);
- ``read_batch`` — a reader on a ``PinnedCatalogView`` (what PIT does) proving the batch that
  holds one committed row, ``verify_unit(..., arrival_seqs=...)``. The batch is the middle one
  of the batch ids actually committed in the Canonical history and the row is read from the
  table inside that batch's range — nothing assumes where a batch or a number lies;
- ``metadata`` — only loading both tables and walking their snapshot histories: the Iceberg
  metadata term (one snapshot and one manifest per commit) that every read above also pays.

Measurement: the parent samples the child's resident set (``VmRSS`` in ``/proc/<pid>/status``)
every ``--interval`` seconds for the child's whole life. The child reports phase markers on its
``CLOCK_MONOTONIC`` (shared with the parent on Linux): ``ready`` (imports done, catalog open, the
stage prepared, ``gc`` run), a settle pause, ``start`` and ``end`` of the measured work. The
stage's **baseline** is the median sample of the settle window, its **peak** the largest sample
between ``start`` and ``end``, its **delta** the difference. ``VmHWM`` at ``ready`` and at
``end`` is reported too, labelled process-level: it is the process's high-water mark since it
started, not the stage's. A sampled peak can miss a spike shorter than the interval; the output
records the interval, the sample counts and both ``VmHWM`` values so that gap is visible.

Pass criterion (fixed before any run, ``GROWTH_LIMIT_MIB``): at least three distinct ``N`` values
must be measured; every stage needs at least three settle and three in-stage RSS samples at every
``N``; and, for every stage, the full range ``max(delta(N)) - min(delta(N))`` across all measured
sizes is at most 32 MiB. Missing or duplicate ``N`` measurements and insufficient samples fail
closed, and a failed capacity verdict returns a nonzero exit status. A per-row working set fails it
by far (0.7 KB/row at 500k rows is ~330 MiB); the output also carries a least-squares slope per
stage, in bytes per row and per batch, and the ``metadata`` stage's own growth, so any remaining
linear term is visible and attributable.

``--runtime controlled`` (default) fixes ``ARROW_DEFAULT_MEMORY_POOL=system``, ``OMP_NUM_THREADS``
and ``PYICEBERG_MAX_WORKERS`` to 1 (thread-count allocator retention, not data); ``default``
leaves the libraries' defaults. The work directory must be on disk (the archive spool and the
warehouse are files; on tmpfs they would be memory): a tmpfs work directory is refused. Run it
under a memory cap (``systemd-run --user --scope -p MemoryMax=3G -p MemorySwapMax=0 ...``).
Nothing here imports ``tests/``. One JSON document is printed.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any, Final

from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.expressions import And, EqualTo, GreaterThanOrEqual, LessThanOrEqual

from core.contracts.catalog import CommitRequest, CommitResult
from infrastructure.canonical import rules
from infrastructure.canonical.normalizer import (
    DEFAULT_NARROW_ROWS,
    CanonicalNormalizer,
    unit_batch_id,
)
from infrastructure.catalog import PHASE1_REGISTRY, PyIcebergCatalogAdapter, ensure_phase1_tables
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_AGG_TRADES,
    BINANCE_SPOT_ARCHIVES,
    CANONICAL_TRADES,
)
from infrastructure.collector.binance_archive import ARCHIVE_SOURCE, COLLECTOR_ID, COLLECTOR_VERSION
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision import ArchiveContext, ArchiveIngested, RawRevisionStore
from infrastructure.revision.row_integrity import PersistedRowVerifier, history_from
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.tools.capacity_probe import (
    DATA_TYPE,
    SYMBOL,
    _agg_trade_lines,
    _publish_archive,
)

__all__ = ["GROWTH_LIMIT_MIB", "main", "run_probe"]

STAGES: Final = ("verify_archive", "write_crash", "resume", "replay", "read_batch", "metadata")
RUNTIMES: Final = {
    "controlled": {
        "ARROW_DEFAULT_MEMORY_POOL": "system",
        "OMP_NUM_THREADS": "1",
        "PYICEBERG_MAX_WORKERS": "1",
    },
    "default": {},
}
#: Pass criterion, fixed before any run: stage delta at the largest N minus at the smallest N.
GROWTH_LIMIT_MIB: Final = 32
DEFAULT_SIZES: Final = (10_000, 100_000, 500_000)
DEFAULT_MICROBATCH: Final = 256
#: D2's own row batch: one fixed value for every N (the verifier re-reads the touched one).
DEFAULT_D2_BATCH: Final = 4_096
#: Refused without ``--i-know-memory`` (WSL has crashed from memory exhaustion before).
MAX_ROWS_WITHOUT_OVERRIDE: Final = 100_000
_SETTLE_SECONDS: Final = 0.5
#: Minimum duration of the metadata stage (repeated loads), so it is sampled like the others.
_METADATA_SECONDS: Final = 0.3
_MIN_SETTLE_SAMPLES: Final = 3
_MIN_STAGE_SAMPLES: Final = 3
_CATALOG_NAME: Final = "e1_cap1_probe"
_KNOWLEDGE_INGEST: Final = datetime(2025, 6, 2, 1, tzinfo=UTC)
_KNOWLEDGE_NORMALIZE: Final = _KNOWLEDGE_INGEST + timedelta(hours=1)
_RAW: Final = BINANCE_SPOT_AGG_TRADES.table
_TABLES: Final = (_RAW, BINANCE_SPOT_ARCHIVES.table, CANONICAL_TRADES.table)


class _ProbeCrash(Exception):
    """The simulated process death right after a Canonical commit."""


class _CrashAfter:
    """Delegates to the adapter; raises right after its ``limit``-th Canonical commit."""

    def __init__(self, inner: PyIcebergCatalogAdapter, limit: int) -> None:
        self._inner = inner
        self._limit = limit
        self._count = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def commit_batch(self, request: CommitRequest, batch: Any) -> CommitResult:
        result = self._inner.commit_batch(request, batch)
        if request.table == CANONICAL_TRADES.table:
            self._count += 1
            if self._count >= self._limit:
                raise _ProbeCrash(f"crashed after {self._count} Canonical commits")
        return result


class _Clock:
    """A UTC clock: ``start``, then one microsecond later per reading."""

    def __init__(self, start: datetime) -> None:
        self._now = start
        self.readings = 0

    def __call__(self) -> datetime:
        value = self._now
        self._now = value + timedelta(microseconds=1)
        self.readings += 1
        return value


@contextmanager
def _opened(
    workdir: Path, *, create: bool = False
) -> Iterator[tuple[PyIcebergCatalogAdapter, LocalFileStorageAdapter]]:
    warehouse = workdir / "warehouse"
    staging = warehouse / "staging"
    if create:
        staging.mkdir(parents=True, exist_ok=True)
    catalog = SqlCatalog(
        _CATALOG_NAME,
        uri=f"sqlite:///{workdir / 'catalog.sqlite'}",
        warehouse=warehouse.as_uri(),
    )
    storage = LocalFileStorageAdapter(warehouse.as_uri(), staging.as_uri())
    try:
        adapter = PyIcebergCatalogAdapter(catalog, PHASE1_REGISTRY)
        if create:
            ensure_phase1_tables(adapter)
        yield adapter, storage
    finally:
        storage.close()
        catalog.close()


def _heads(adapter: PyIcebergCatalogAdapter) -> dict[str, str]:
    heads: dict[str, str] = {}
    for table in _TABLES:
        info = adapter.load_table(table)
        if info is not None and info.current_snapshot is not None:
            heads[table] = info.current_snapshot.snapshot_id
    return heads


def _status_kb(field: str, pid: int | str = "self") -> int | None:
    try:
        text = Path(f"/proc/{pid}/status").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    for line in text.splitlines():
        if line.startswith(f"{field}:"):
            return int(line.split()[1])
    return None


def _emit(event: str, **facts: Any) -> None:
    print(json.dumps({"event": event, "t": time.monotonic(), **facts}), flush=True)


# ============================================================================================
# child: fixture setup and measured stages
# ============================================================================================


def _setup(workdir: Path, rows: int, d2_batch: int) -> dict[str, Any]:
    with _opened(workdir, create=True) as (adapter, storage):
        collected = _publish_archive(storage, _agg_trade_lines(rows))
        context = ArchiveContext(
            request_id="e1-cap1-probe",
            data_type=DATA_TYPE,
            collector_id=COLLECTOR_ID,
            collector_version=COLLECTOR_VERSION,
            source=ARCHIVE_SOURCE,
        )
        store = RawRevisionStore(
            adapter, storage, clock=_Clock(_KNOWLEDGE_INGEST), microbatch_rows=d2_batch
        )
        outcome = store.ingest(collected, context)
        if not isinstance(outcome, ArchiveIngested):
            raise RuntimeError(f"the synthetic archive was not ingested: {outcome!r}")
        return {"unit": outcome.archive_revision_id}


def _committed_batch_entries(
    adapter: PyIcebergCatalogAdapter, unit: str
) -> Iterator[tuple[str, int]]:
    """Yield the unit's committed Canonical batches in the history walk's newest-first order."""
    head = _heads(adapter).get(CANONICAL_TRADES.table)
    prefix = unit_batch_id(unit, 0, 0, 0).rsplit(".", 3)[0] + "."
    for snapshot in history_from(adapter, CANONICAL_TRADES.table, head):
        if snapshot.batch_id is not None and snapshot.batch_id.startswith(prefix):
            yield snapshot.batch_id, snapshot.added_rows


def _middle_committed_batch(adapter: PyIcebergCatalogAdapter, unit: str) -> tuple[int, str, int]:
    """Return the middle batch's (count, id, row count) using two streaming history walks."""
    count = sum(1 for _ in _committed_batch_entries(adapter, unit))
    if count == 0:
        raise RuntimeError(f"unit {unit} has no committed Canonical batches")
    # History is newest-first; this is the index corresponding to the middle of an oldest-first
    # sequence, without materializing/reversing the entire batch list.
    middle = (count - 1) // 2
    for index, (batch_id, added) in enumerate(_committed_batch_entries(adapter, unit)):
        if index == middle:
            return count, batch_id, added
    raise RuntimeError(f"unit {unit} lost its middle Canonical batch during preparation")


def _stage(
    stage: str,
    adapter: PyIcebergCatalogAdapter,
    storage: LocalFileStorageAdapter,
    unit: str,
    rows: int,
    config: dict[str, int],
    spool: Path,
) -> Callable[[], dict[str, Any]]:
    """A closure doing only the measured work (catalog open, everything else prepared)."""
    chunk, narrow = config["microbatch"], config["narrow"]

    def normalizer(catalog: Any, clock: _Clock | None = None) -> CanonicalNormalizer:
        return CanonicalNormalizer(
            catalog,
            storage,
            clock=clock,
            microbatch_rows=chunk,
            narrow_rows=narrow,
            spool_dir=spool,
        )

    if stage == "verify_archive":
        view = PinnedCatalogView(adapter, _heads(adapter))

        def verify() -> dict[str, Any]:
            with PersistedRowVerifier(
                view, storage, cache_archives=True, spool_dir=spool, spool_rows=chunk
            ) as verifier:
                lines = verifier.archive_row_count(DATA_TYPE, SYMBOL, unit)
                proven = 0
                for low in range(1, lines + 1, chunk):
                    high = min(low + chunk - 1, lines)
                    window = view.scan_columns(
                        _RAW,
                        columns=tuple(f.name for f in BINANCE_SPOT_AGG_TRADES.arrow_schema),
                        row_filter=And(
                            EqualTo("archive_revision_id", unit),  # type: ignore[call-arg, arg-type]
                            And(
                                GreaterThanOrEqual("archive_line_number", low),  # type: ignore[call-arg, arg-type]
                                LessThanOrEqual("archive_line_number", high),  # type: ignore[call-arg, arg-type]
                            ),
                        ),
                        limit=high - low + 2,
                    ).to_pylist()
                    verifier.verify_archive_elements(
                        BINANCE_SPOT_AGG_TRADES, DATA_TYPE, SYMBOL, window
                    )
                    proven += len(window)
            return {"lines": lines, "rows_proven": proven}

        return verify
    if stage == "write_crash":
        crash_after = max(1, -(-rows // chunk) // 2)
        clock = _Clock(_KNOWLEDGE_NORMALIZE)
        writer = normalizer(_CrashAfter(adapter, crash_after), clock)

        def write_crash() -> dict[str, Any]:
            try:
                writer.normalize_unit(_RAW, unit)
            except _ProbeCrash:
                return {"crash_after": crash_after, "clock_readings": clock.readings}
            raise RuntimeError("the write finished before its simulated crash")

        return write_crash
    if stage in ("resume", "replay"):
        clock = _Clock(_KNOWLEDGE_NORMALIZE + timedelta(hours=1))
        writer = normalizer(adapter, clock)

        def normalize() -> dict[str, Any]:
            out = writer.normalize_unit(_RAW, unit)
            return {
                "row_count": out.row_count,
                "batch_count": out.batch_count,
                "committed_batches": out.committed_batches,
                "replayed": out.replayed,
                "clock_readings": clock.readings,
            }

        return normalize
    if stage == "read_batch":
        committed_count, batch_id, added = _middle_committed_batch(adapter, unit)
        index = int(batch_id.rsplit(".", 1)[1])
        sample = (
            adapter.scan_columns(
                CANONICAL_TRADES.table,
                columns=("arrival_seq",),
                row_filter=EqualTo("lineage_source_revision_id", unit),  # type: ignore[call-arg, arg-type]
                limit=1,
            )
            .column("arrival_seq")[0]
            .as_py()
        )
        base = (sample // rules.ARRIVAL_SEQ_STRIDE) * rules.ARRIVAL_SEQ_STRIDE
        # The archive's position p is arrival_seq base + p; batch i holds ranks [i*M, i*M+M).
        row = (
            adapter.scan_columns(
                CANONICAL_TRADES.table,
                columns=("arrival_seq",),
                row_filter=And(
                    EqualTo("lineage_source_revision_id", unit),  # type: ignore[call-arg, arg-type]
                    And(
                        GreaterThanOrEqual("arrival_seq", base + index * chunk + 1),  # type: ignore[call-arg, arg-type]
                        LessThanOrEqual("arrival_seq", base + index * chunk + added),  # type: ignore[call-arg, arg-type]
                    ),
                ),
                limit=1,
            )
            .column("arrival_seq")[0]
            .as_py()
        )
        reader = normalizer(PinnedCatalogView(adapter, _heads(adapter)))

        def read_batch() -> dict[str, Any]:
            with reader:
                found = reader.verify_unit(_RAW, unit, arrival_seqs={row})
            if row not in {item["arrival_seq"] for item in found} or len(found) != added:
                raise RuntimeError("the proven batch is not the committed batch holding the row")
            return {
                "committed_batches_seen": committed_count,
                "batch_id": batch_id,
                "arrival_seq": row,
                "rows_read": len(found),
            }

        return read_batch
    if stage == "metadata":

        def walk() -> dict[str, Any]:
            # One load + walk takes milliseconds: repeat it for a minimum duration so the sampler
            # sees the stage (same memory shape: each round loads the metadata afresh).
            snapshots: dict[str, int] = {}
            rounds = 0
            started = time.monotonic()
            while rounds == 0 or time.monotonic() - started < _METADATA_SECONDS:
                for table, head in _heads(adapter).items():
                    snapshots[table] = sum(1 for _ in history_from(adapter, table, head))
                rounds += 1
            return {"snapshots": snapshots, "rounds": rounds}

        return walk
    raise ValueError(f"unknown stage {stage!r}")


def _child_stage(stage: str, workdir: Path, unit: str, rows: int, config: dict[str, int]) -> None:
    spool = workdir / "spool"
    spool.mkdir(exist_ok=True)
    with _opened(workdir) as (adapter, storage):
        body = _stage(stage, adapter, storage, unit, rows, config, spool)
        gc.collect()
        _emit("ready", vmhwm_kb=_status_kb("VmHWM"), vmrss_kb=_status_kb("VmRSS"))
        time.sleep(_SETTLE_SECONDS)
        _emit("start")
        started = time.perf_counter()
        facts = body()
        wall = time.perf_counter() - started
        _emit(
            "end",
            wall_seconds=round(wall, 3),
            vmhwm_kb=_status_kb("VmHWM"),
            spool_left=sorted(item.name for item in spool.iterdir()),
            **facts,
        )


# ============================================================================================
# parent: isolation, sampling, attribution, verdicts
# ============================================================================================


def _run_child(
    args: list[str], runtime: str, interval: float
) -> tuple[list[dict[str, Any]], list[tuple[float, int]]]:
    """Run one child; sample its ``VmRSS`` until it exits. Returns its events and samples."""
    env = {name: value for name, value in os.environ.items() if name not in RUNTIMES["controlled"]}
    env.update(RUNTIMES[runtime])
    process = subprocess.Popen(
        [sys.executable, "-m", "infrastructure.tools.normalizer_memory_probe", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    samples: list[tuple[float, int]] = []

    def sample() -> None:
        while process.poll() is None:
            rss = _status_kb("VmRSS", process.pid)
            if rss is not None:
                samples.append((time.monotonic(), rss))
            time.sleep(interval)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    stdout, stderr = process.communicate()
    sampler.join()
    if process.returncode != 0:
        raise RuntimeError(f"probe child {args[:2]} failed:\n{stderr[-4000:]}")
    events = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    return events, samples


def _attribute(
    events: list[dict[str, Any]], samples: list[tuple[float, int]], interval: float
) -> dict[str, Any]:
    by_event = {event["event"]: event for event in events}
    ready, start, end = by_event["ready"], by_event["start"], by_event["end"]
    settle = [rss for t, rss in samples if ready["t"] <= t <= start["t"]]
    during = [rss for t, rss in samples if start["t"] <= t <= end["t"]]
    if len(settle) < _MIN_SETTLE_SAMPLES or len(during) < _MIN_STAGE_SAMPLES:
        raise RuntimeError(
            "too few samples: need at least "
            f"{_MIN_SETTLE_SAMPLES} settle and {_MIN_STAGE_SAMPLES} stage samples; "
            "lower --interval or increase the measured stage duration"
        )
    baseline = statistics.median(settle)
    peak = max(during)
    facts = {k: v for k, v in end.items() if k not in ("event", "t", "vmhwm_kb")}
    return {
        "baseline_mib": round(baseline / 1024, 1),
        "peak_mib": round(peak / 1024, 1),
        "delta_mib": round((peak - baseline) / 1024, 1),
        "settle_samples": len(settle),
        "stage_samples": len(during),
        "interval_seconds": interval,
        "process_vmhwm_at_ready_mib": round(ready["vmhwm_kb"] / 1024, 1),
        "process_vmhwm_at_end_mib": round(end["vmhwm_kb"] / 1024, 1),
        **facts,
    }


def _slope(points: list[tuple[int, float]]) -> float | None:
    """Least-squares slope of ``(x, MiB)``, in bytes per unit of ``x``."""
    if len(points) < 2:
        return None
    xs, ys = [x for x, _ in points], [y * 2**20 for _, y in points]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mean_x) ** 2 for x in xs)
    return (
        None
        if den == 0
        else sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / den
    )


def _validate_sizes(sizes: list[int] | tuple[int, ...]) -> None:
    """Require a genuine multi-scale measurement before doing any capacity work."""
    if len(sizes) < 3 or len(set(sizes)) < 3:
        raise ValueError("capacity probe requires at least three distinct N sizes")
    if len(set(sizes)) != len(sizes):
        raise ValueError("capacity probe sizes must not contain duplicates")
    if any(size < 1 for size in sizes):
        raise ValueError("capacity probe sizes must be positive")


def _stage_verdict(
    points: list[tuple[int, float]],
    expected_sizes: list[int] | tuple[int, ...],
    microbatch: int,
) -> dict[str, Any]:
    """Fail closed on incomplete/duplicate data; gate the full observed range, not endpoints."""
    _validate_sizes(expected_sizes)
    measured = [size for size, _ in points]
    if len(measured) != len(expected_sizes) or len(set(measured)) != len(measured):
        raise RuntimeError("capacity probe has missing or duplicate stage measurements")
    if set(measured) != set(expected_sizes):
        raise RuntimeError("capacity probe measurements do not match the requested N sizes")
    if any(not math.isfinite(delta) for _, delta in points):
        raise RuntimeError("capacity probe has a non-finite RSS measurement")
    ordered = sorted(points)
    deltas = [delta for _, delta in ordered]
    growth = max(deltas) - min(deltas)
    rows_slope = _slope(ordered)
    return {
        "delta_mib_by_rows": dict(ordered),
        "growth_mib": round(growth, 1),
        "slope_bytes_per_row": None if rows_slope is None else round(rows_slope, 2),
        "slope_bytes_per_batch": (
            None if rows_slope is None else round(rows_slope * microbatch, 1)
        ),
        "limit_mib": GROWTH_LIMIT_MIB,
        "pass": growth <= GROWTH_LIMIT_MIB,
    }


def _filesystem(path: Path) -> str:
    """The filesystem type holding ``path`` (longest ``/proc/mounts`` prefix)."""
    resolved = str(path.resolve())
    best, kind = "", "unknown"
    for line in Path("/proc/mounts").read_text().splitlines():
        parts = line.split()
        mount = parts[1]
        if (resolved == mount or resolved.startswith(mount.rstrip("/") + "/")) and len(mount) > len(
            best
        ):
            best, kind = mount, parts[2]
    return kind


def _git_head() -> str | None:
    done = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return done.stdout.strip() or None


def run_probe(
    sizes: list[int],
    config: dict[str, int],
    *,
    base: Path,
    runtime: str = "controlled",
    interval: float = 0.01,
) -> dict[str, Any]:
    _validate_sizes(sizes)
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("RSS sample interval must be finite and greater than zero")
    if min(config.values()) < 1:
        raise ValueError("microbatch, narrow and D2 batch sizes must be positive")
    results: list[dict[str, Any]] = []
    common = [f"--{name.replace('_', '-')}={value}" for name, value in config.items()]
    for rows in sizes:
        workdir = Path(tempfile.mkdtemp(prefix=f"e1-cap1-{rows}-", dir=base))
        try:
            events, _ = _run_child(
                ["--stage", "setup", "--workdir", str(workdir), f"--unit-rows={rows}", *common],
                runtime,
                interval,
            )
            unit = next(event for event in events if event["event"] == "setup")["unit"]
            for stage in STAGES:
                events, samples = _run_child(
                    [
                        "--stage",
                        stage,
                        "--workdir",
                        str(workdir),
                        f"--unit={unit}",
                        f"--unit-rows={rows}",
                        *common,
                    ],
                    runtime,
                    interval,
                )
                record = {"rows": rows, "batches": -(-rows // config["microbatch"]), "stage": stage}
                record.update(_attribute(events, samples, interval))
                results.append(record)
                print(json.dumps(record), file=sys.stderr, flush=True)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    verdicts: dict[str, Any] = {}
    for stage in STAGES:
        points = [(r["rows"], r["delta_mib"]) for r in results if r["stage"] == stage]
        verdicts[stage] = _stage_verdict(points, sizes, config["microbatch"])
    return {
        "probe": "infrastructure.tools.normalizer_memory_probe",
        "config": {
            "sizes": sizes,
            **config,
            "spool_rows": min(8_192, config["microbatch"]),
            "runtime": runtime,
            "runtime_env": RUNTIMES[runtime],
            "stages": list(STAGES),
            "sample_interval_seconds": interval,
            "settle_seconds": _SETTLE_SECONDS,
            "growth_limit_mib": GROWTH_LIMIT_MIB,
            "workdir_filesystem": _filesystem(base),
            "git_head": _git_head(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "pyarrow": metadata.version("pyarrow"),
            "pyiceberg": metadata.version("pyiceberg"),
            "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        "results": results,
        "verdicts": verdicts,
        "pass": all(v["pass"] is True for v in verdicts.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, nargs="+", default=list(DEFAULT_SIZES))
    parser.add_argument("--microbatch", type=int, default=DEFAULT_MICROBATCH)
    parser.add_argument("--narrow", type=int, default=DEFAULT_NARROW_ROWS)
    parser.add_argument("--d2-batch", type=int, default=DEFAULT_D2_BATCH)
    parser.add_argument("--interval", type=float, default=0.01, help="RSS sample interval (s)")
    parser.add_argument("--runtime", choices=tuple(RUNTIMES), default="controlled")
    parser.add_argument(
        "--base",
        type=Path,
        default=Path(os.environ.get("HLENS_PROBE_DIR", Path.home() / ".cache" / "hlens-probe")),
        help="work directory (on disk; tmpfs is refused)",
    )
    parser.add_argument("--i-know-memory", action="store_true")
    parser.add_argument("--stage", choices=("setup", *STAGES), help=argparse.SUPPRESS)
    parser.add_argument("--workdir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--unit", help=argparse.SUPPRESS)
    parser.add_argument("--unit-rows", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    config = {"microbatch": args.microbatch, "narrow": args.narrow, "d2_batch": args.d2_batch}
    if args.stage == "setup":
        _emit("setup", **_setup(args.workdir, args.unit_rows, args.d2_batch))
        return 0
    if args.stage is not None:
        _child_stage(args.stage, args.workdir, args.unit, args.unit_rows, config)
        return 0
    try:
        _validate_sizes(args.rows)
    except ValueError as exc:
        parser.error(str(exc))
    if min(config.values()) < 1:
        parser.error("--microbatch, --narrow and --d2-batch must be positive")
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval must be finite and greater than zero")
    if max(args.rows) > MAX_ROWS_WITHOUT_OVERRIDE and not args.i_know_memory:
        parser.error(f"--rows above {MAX_ROWS_WITHOUT_OVERRIDE} needs --i-know-memory")
    if _filesystem(args.base) == "tmpfs":
        parser.error(f"{args.base} is on tmpfs: a spool or warehouse there is memory")
    args.base.mkdir(parents=True, exist_ok=True)
    result = run_probe(
        sorted(args.rows), config, base=args.base, runtime=args.runtime, interval=args.interval
    )
    print(json.dumps(result, indent=2))
    return 0 if result["pass"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())

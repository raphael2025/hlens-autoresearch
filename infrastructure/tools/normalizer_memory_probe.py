"""Isolated-process RSS probe of the Canonical normalizer on the **main** code line (E1-CAP-1).

``python -m infrastructure.tools.normalizer_memory_probe --i-know-memory`` (defaults: ``M = 256``,
``N = 10 000 / 100 000 / 500 000``, 3 repeats, D2 row batch 4 096), run under a memory cap, e.g.
``systemd-run --user --scope -p MemoryMax=6G -p MemorySwapMax=0 uv run python -m ...``.

This is an **operator diagnostic**, not a test: tests import only its pure validation helpers and
never run a capacity experiment. It changes no production code or data: every
catalog and warehouse it writes is a throwaway SQLite catalog + local ``file://`` warehouse under
``--base``, deleted afterwards.

Which code line it measures
===========================

It imports the unmodified production modules of the checkout it is started from (every child runs
with that checkout as its working directory and reports the path of the normalizer module it
imported; a path outside the checkout aborts the run). The output records ``git HEAD``, the local
``main`` ref, and whether ``core/``, ``infrastructure/`` (except this file), ``plugins/``,
``pyproject.toml`` or
``uv.lock`` differ from ``main`` or are dirty. The probe's own SHA-256 and clean/dirty state
relative to ``HEAD`` are recorded separately, and an uncommitted probe cannot satisfy
``e1_cap1_evidence``. Only when the compared production paths match ``main`` is
``code_line.matches_main`` true.

The earlier candidate numbers (``resume`` 59.9 MiB / ``replay`` 63.9 MiB cross-scale growth at
500k) came from the candidate probe on ``fix/e1-cap1@a75278e`` — a different normalizer (spooled
archive re-parse, narrow proof windows) that is **not** in ``main`` — with **one sample per N**.
They are not main results, and nothing here reuses or extrapolates them. This probe adapts that
candidate probe's protocol to main's APIs and adds repeats.

Stages (each in its own fresh Python process)
=============================================

For every ``N`` and every repeat a fresh fixture is built: one synthetic aggTrades archive of ``N``
lines (the fixture of ``infrastructure.tools.capacity_probe``), ingested through the real D1 parser
+ D2 ``RawRevisionStore`` in a ``setup`` child. Setup is sampled for safety / logging, but its RSS
is excluded from the per-stage growth verdict. Then, in order, each in a new child:

- ``verify_archive`` — strict archive verification only: ``PersistedRowVerifier`` over a
  ``PinnedCatalogView`` of the heads with ``cache_archives=True`` (exactly how main's normalizer
  builds it), ``archive_row_count`` then every Raw line proven in windows of ``M`` lines via
  ``verify_archive_elements``. Main has no archive spool: its verifier caches the strict D1
  re-parse of the archive in memory, and that is what this stage measures. It is the verifier's
  path, not the normalizer's whole proving pass (that is inside ``replay``);
- ``write_crash`` — ``normalize_unit`` writing the unit through a catalog proxy that raises right
  after its ``crash_after``-th Canonical commit (half the plan): the write path, then an
  interrupted write;
- ``resume`` — ``normalize_unit`` in a new process over that committed prefix: the proving survey,
  the missing batches' commit + read-back and the close;
- ``replay`` — ``normalize_unit`` again over the complete unit (the proving survey, no commit);
- ``read_batch`` — a normalizer on a ``PinnedCatalogView`` (what PIT does) proving one committed
  batch with ``verify_unit(..., arrival_seqs={row})``: the middle batch of the batch ids actually
  committed in the Canonical history, and a row read from the table inside that batch's range;
- ``metadata`` — only ``load_table`` + a ``history_from`` walk of the three tables, repeated for at
  least 0.3 s: the Iceberg snapshot-metadata term (one snapshot per commit) every read also pays.

Optional ADR-0077 v3 Dataset measurement
========================================

``--dataset-v3`` adds a separate ``dataset_v3_build`` stage at every requested N and repeat. It
uses the same N-row synthetic aggTrades archive, prepares the first-slice listing and quality
reports in an unmeasured fixture child, then measures a v3 ``DatasetEvidenceBuilder.build`` plus
its streaming manifest verifier over the full UTC day ``[2025-06-01T00:00Z, 2025-06-02T00:00Z)``.
The four DQ-9 rule parameters are mandatory CLI inputs (``--dataset-chunk-rows``,
``--dataset-leaf-max-records``, ``--dataset-leaf-max-bytes`` and ``--dataset-fanout``); the probe
chooses no DQ-9 values. Their exact values, fixed upstream sorted-run bounds, fixture identity,
full-day window, result IDs/counts, code line and RSS samples are recorded. The v3 stage gets its
own full-range ``growth_mib`` verdict against the same fixed 32 MiB limit; it does not alter the
normalizer E1-CAP-1 verdict or mark DQ-9 accepted.

Each stage's result is checked (rows proven, crash point, commits already committed / replayed,
the proven batch holds the row) and a wrong fixture state fails the run closed. Every stage's API
result object (e.g. ``CanonicalUnitNormalized``) stays referenced for a
``0.2 s`` hold inside the measured window, so it is sampled (the full-process working set counts
it: E1-HIST).

No stage is omitted relative to the candidate probe: each maps onto a public main API. Main-API
differences handled here: ``CanonicalNormalizer`` takes only ``clock`` and ``microbatch_rows``
(no ``narrow_rows`` / ``spool_dir``); ``PersistedRowVerifier`` takes no spool and is not a
context manager; ``CanonicalUnitNormalized`` exposes fixed-size row / batch counts and replay
summaries directly.

Measurement
===========

The parent samples the child's ``VmRSS`` (``/proc/<pid>/status``) every ``--interval`` seconds for
the child's whole life. The child writes phase markers on ``CLOCK_MONOTONIC`` (shared with the
parent on Linux): ``ready`` (catalog open, stage prepared, ``gc`` run), a 0.5 s settle pause,
``start`` and ``end`` of the measured work (+ hold). **Baseline** = median ``VmRSS`` sample of the
settle window (its min / max are reported as drift); **peak** = largest sample in
``[start, end]``; **delta** = peak - baseline. ``VmHWM`` at ``ready`` and at ``end`` is reported,
labelled process-level (a high-water mark since process start, not the stage's). A sampled peak
can miss a spike shorter than the interval; the interval and sample counts are recorded. Stage
preparation (heads, history walks to locate the batch) runs before ``ready`` and may warm
process-wide caches (e.g. PyIceberg's manifest LRU); ``VmRSS`` at ``ready`` is reported.

Optional staged allocation diagnostics
=======================================

``--staged-diagnostics`` instruments the probe child to count public CatalogAdapter calls,
manifest-list entries, live manifest entries and data-file reads visited by the bounded snapshot
scanner, plus any remaining high-level planner calls and planned tasks. Manifest and entry counts
include both scanner passes; early termination can make the second pass partial. It also reports
retained ``tracemalloc`` deltas by source path and a reachable-Python-size estimate for the held
result.
Scanner counters count yielded entries / started reads, not bytes; manifest-list bytes are not
counted separately.
This mode changes child memory and timing, so its RSS is diagnostic only and can never satisfy
``e1_cap1_evidence``. ``tracemalloc`` does not include native Arrow buffers and is not an RSS
measurement. The default probe path does not enable this instrumentation.

Criterion (fixed, not configurable: ``GROWTH_LIMIT_MIB = 32``)
==============================================================

For every stage, ``growth = max(delta) - min(delta)`` over **every** repeat at **every** ``N``
(run-to-run noise is counted against the stage, never averaged away) must be at most 32 MiB. Every
stage needs at least 3 settle and 3 in-stage samples per child; missing, duplicate or non-finite
measurements fail closed. Per-``N`` min / median / max and least-squares slopes (bytes per row and
per ``M``-row batch, on the per-``N`` medians and maxima) are reported for attribution.

The result is labelled E1-CAP-1 evidence (``e1_cap1_evidence``) only when ``M = 256``, the sizes
include 10k / 100k / 500k, there are at least 3 repeats, and ``code_line.matches_main``. Any other
run is a diagnostic only. Even an evidence-grade PASS is only the RSS part of the E1 closure
criteria (``docs/reviews/2026-09-27-e1-review.md``): structural cardinality assertions, targeted
tests and independent review are separate.

Exit status: 0 = evidence-grade PASS; 1 = some stage exceeds the limit; 3 = all stages within the
limit but not evidence-grade; 4 = the probe failed (partial JSON, ``status = "error"``); 2 = usage.

Safety
======

- ``N`` above 50 000 needs ``--i-know-memory`` (main still keeps O(N) positions, time columns,
  revision ids and the parsed archive; WSL has crashed from memory exhaustion before), and also a
  finite cgroup memory limit on this process (``systemd-run ... -p MemoryMax=...``) unless
  ``--allow-uncapped`` is given;
- a child whose sampled ``VmRSS`` exceeds ``--child-rss-limit-mib`` (default 4096) or that runs
  longer than ``--child-timeout`` is killed and the run fails closed (never counted as a pass);
- the work directory must be on disk: a ``tmpfs`` / ``ramfs`` ``--base`` (where the warehouse and
  child temporary files would be memory) is refused. Every child uses its per-run work directory
  as ``TMPDIR`` so parser spools and disk-backed indexes are covered by that refusal.

``--runtime controlled`` (default) sets ``ARROW_DEFAULT_MEMORY_POOL=system``, ``OMP_NUM_THREADS=1``
and ``PYICEBERG_MAX_WORKERS=1`` in the children; compatibility of these settings with the exact
locked dependency versions has not been independently verified, so the environment values are
reported and must not be treated as proof that each library honored them. ``default`` removes these
variables and uses library defaults. One JSON document goes to stdout (and ``--json-out``);
``--samples-out`` writes every child's raw ``VmRSS`` series as JSON lines.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import inspect
import json
import math
import os
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any, Final, NoReturn, cast

from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.expressions import And, EqualTo, GreaterThanOrEqual, LessThanOrEqual

from core.contracts.catalog import CommitRequest, CommitResult
from infrastructure.canonical import rules
from infrastructure.canonical.normalizer import CanonicalNormalizer, unit_batch_id
from infrastructure.catalog import PHASE1_REGISTRY, PyIcebergCatalogAdapter, ensure_phase1_tables
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_AGG_TRADES,
    BINANCE_SPOT_ARCHIVES,
    CANONICAL_TRADES,
    DATASET_SELECTION_CHUNKS,
)
from infrastructure.collector.binance_archive import ARCHIVE_SOURCE, COLLECTOR_ID, COLLECTOR_VERSION
from infrastructure.dataset.builder import (
    DatasetEvidenceBuilder,
    DatasetEvidenceRequest,
    dataset_evidence_rule,
)
from infrastructure.dataset.chunks import IcebergChunkWriter
from infrastructure.dataset.sources import UniverseRunParams, dataset_evidence_sources
from infrastructure.dataset.verify_v3 import StreamingEvidenceVerifier
from infrastructure.pit.runs import RunLimits
from infrastructure.pit.selector import PitRunParams
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision import ArchiveContext, ArchiveIngested, RawRevisionStore
from infrastructure.revision.row_integrity import PersistedRowVerifier, history_from
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.tools.capacity_probe import (
    _KNOWLEDGE_INGEST,
    _KNOWLEDGE_NORMALIZE,
    DATA_TYPE,
    REST_BASE,
    SYMBOL,
    _agg_trade_lines,
    _dataset_pit_spec,
    _prepare_dataset_quality,
    _prepare_listings,
    _publish_archive,
)
from infrastructure.tools.capacity_probe import (
    DAY as PROBE_DAY,
)
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE

__all__ = ["GROWTH_LIMIT_MIB", "main", "run_probe"]

PROBE: Final = "infrastructure.tools.normalizer_memory_probe"
STAGES: Final = ("verify_archive", "write_crash", "resume", "replay", "read_batch", "metadata")
DATASET_V3_STAGE: Final = "dataset_v3_build"
DATASET_V3_RULE_KEYS: Final = (
    "chunk_rows",
    "leaf_max_records",
    "leaf_max_bytes",
    "fanout",
)
DATASET_V3_DAY_START: Final = datetime(PROBE_DAY.year, PROBE_DAY.month, PROBE_DAY.day, tzinfo=UTC)
DATASET_V3_DAY_END: Final = DATASET_V3_DAY_START + timedelta(days=1)
# ADR-0077 leaves these source-run sizes to callers. Keep a separately reported, fixed probe
# configuration; do not substitute these for the four explicit DQ-9 DatasetRule parameters.
DATASET_V3_SOURCE_RUN_RECORDS: Final = 256
DATASET_V3_SOURCE_RUN_BYTES: Final = 4 * 1024 * 1024
DATASET_V3_SOURCE_RUN_FANOUT: Final = 8
RUNTIMES: Final[dict[str, dict[str, str]]] = {
    "controlled": {
        "ARROW_DEFAULT_MEMORY_POOL": "system",
        "OMP_NUM_THREADS": "1",
        "PYICEBERG_MAX_WORKERS": "1",
    },
    "default": {},
}
#: The E1-CAP-1 pass criterion (review 2026-09-27-e1): fixed here, deliberately not a flag.
GROWTH_LIMIT_MIB: Final = 32
#: The protocol the E1 review fixes: M = 256 and at least these unit sizes.
PROTOCOL_MICROBATCH: Final = 256
PROTOCOL_SIZES: Final = (10_000, 100_000, 500_000)
PROTOCOL_MIN_REPEATS: Final = 3
DEFAULT_SIZES: Final = PROTOCOL_SIZES
DEFAULT_MICROBATCH: Final = PROTOCOL_MICROBATCH
DEFAULT_REPEATS: Final = PROTOCOL_MIN_REPEATS
#: D2's own row batch: one fixed value for every N (the verifier re-reads the touched ones).
DEFAULT_D2_BATCH: Final = 4_096
#: Same guard as ``capacity_probe``: main keeps O(N) state, so above this a cap is required.
MAX_ROWS_WITHOUT_OVERRIDE: Final = 50_000
DEFAULT_CHILD_RSS_LIMIT_MIB: Final = 4_096
DEFAULT_CHILD_TIMEOUT_SECONDS: Final = 7_200.0
DEFAULT_INTERVAL_SECONDS: Final = 0.01
_REFUSED_FILESYSTEMS: Final = frozenset({"tmpfs", "ramfs"})
_SETTLE_SECONDS: Final = 0.5
#: The stage's result object stays referenced this long inside the measured window.
_HOLD_SECONDS: Final = 0.2
#: Minimum duration of the metadata stage (repeated loads), so it is sampled like the others.
_METADATA_SECONDS: Final = 0.3
_MIN_SETTLE_SAMPLES: Final = 3
_MIN_STAGE_SAMPLES: Final = 3
_EVENT_PREFIX: Final = "@@e1-probe "
_STDERR_TAIL: Final = 8_000
_CATALOG_NAME: Final = "e1_main_probe"
_RAW: Final = BINANCE_SPOT_AGG_TRADES.table
_TABLES: Final = (_RAW, BINANCE_SPOT_ARCHIVES.table, CANONICAL_TRADES.table)
_ROOT: Final = Path(__file__).resolve().parents[2]
_SELF: Final = Path(__file__).resolve().relative_to(_ROOT).as_posix()
#: What decides the measured code line: compared with ``main`` (this file excluded).
_CODE_PATHS: Final = ("core", "infrastructure", "plugins", "pyproject.toml", "uv.lock")
_DEPENDENCIES: Final = (
    "pyarrow",
    "pyiceberg",
    "pyiceberg-core",
    "pydantic",
    "sqlalchemy",
    "httpx",
)


class ProbeError(Exception):
    """The probe could not produce a trustworthy measurement; the run fails closed."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class ProbeInterrupted(Exception):
    """A parent-process interruption that must never be reported as an RSS verdict."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        super().__init__(f"probe interrupted by {name}")


@contextmanager
def _parent_interrupt_handlers() -> Iterator[None]:
    """Turn parent SIGINT/SIGTERM into catchable interruptions while children are active."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous: dict[int, Any] = {}

    def interrupt(signum: int, _frame: Any) -> NoReturn:
        raise ProbeInterrupted(signum)

    signals = (signal.SIGINT, signal.SIGTERM)
    try:
        for signum in signals:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupt)
        yield
    finally:
        for previous_signum, handler in previous.items():
            signal.signal(previous_signum, handler)


class _ProbeCrash(Exception):
    """The simulated process death right after a Canonical commit."""


class _CrashAfter:
    """Delegates to the adapter; raises right after its ``limit``-th Canonical commit."""

    def __init__(self, inner: PyIcebergCatalogAdapter, limit: int) -> None:
        self._inner = inner
        self._limit = limit
        self.count = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def commit_batch(self, request: CommitRequest, batch: Any) -> CommitResult:
        result = self._inner.commit_batch(request, batch)
        if request.table == CANONICAL_TRADES.table:
            self.count += 1
            if self.count >= self._limit:
                raise _ProbeCrash(f"crashed after {self.count} Canonical commits")
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


@dataclass
class _StageDiagnostics:
    """Probe-only call and allocation counters; never changes production decisions."""

    adapter_calls: Counter[str] = field(default_factory=Counter)
    manifest_list_entries_read: int = 0
    live_manifest_entries_read: int = 0
    data_file_reads_started: int = 0
    high_level_plan_calls: int = 0
    high_level_manifests_considered: int = 0
    high_level_file_scan_tasks_planned: int = 0


class _CountingAdapter:
    """Count public CatalogAdapter operations made during the measured stage."""

    def __init__(self, inner: PyIcebergCatalogAdapter, diagnostics: _StageDiagnostics) -> None:
        self._inner = inner
        self._diagnostics = diagnostics

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def load_table(self, table: str) -> Any:
        self._diagnostics.adapter_calls["load_table"] += 1
        return self._inner.load_table(table)

    def scan_columns(self, *args: Any, **kwargs: Any) -> Any:
        self._diagnostics.adapter_calls["scan_columns"] += 1
        return self._inner.scan_columns(*args, **kwargs)

    def scan_column_batches(self, *args: Any, **kwargs: Any) -> Any:
        self._diagnostics.adapter_calls["scan_column_batches"] += 1
        return self._inner.scan_column_batches(*args, **kwargs)

    def commit_batch(self, *args: Any, **kwargs: Any) -> Any:
        self._diagnostics.adapter_calls["commit_batch"] += 1
        return self._inner.commit_batch(*args, **kwargs)

    def max_int64(self, *args: Any, **kwargs: Any) -> Any:
        self._diagnostics.adapter_calls["max_int64"] += 1
        return self._inner.max_int64(*args, **kwargs)


@contextmanager
def _count_scan_work(diagnostics: _StageDiagnostics) -> Iterator[None]:
    """Count actual bounded-scanner work and any remaining high-level planner work."""
    from pyiceberg.table import ManifestGroupPlanner

    from infrastructure.catalog import iceberg_adapter

    original_manifests = iceberg_adapter._manifest_files
    original_entries = iceberg_adapter._live_entries
    original_data_files = iceberg_adapter._data_file_batches
    original_plan_files = ManifestGroupPlanner.plan_files

    def counted_manifests(*args: Any, **kwargs: Any) -> Iterator[Any]:
        for manifest in original_manifests(*args, **kwargs):
            diagnostics.manifest_list_entries_read += 1
            yield manifest

    def counted_entries(*args: Any, **kwargs: Any) -> Iterator[Any]:
        for entry in original_entries(*args, **kwargs):
            diagnostics.live_manifest_entries_read += 1
            yield entry

    def counted_data_files(*args: Any, **kwargs: Any) -> Iterator[Any]:
        diagnostics.data_file_reads_started += 1
        yield from original_data_files(*args, **kwargs)

    def counted_plan_files(
        planner: Any,
        manifests: Any,
        manifest_entry_filter: Callable[[Any], bool] = lambda _: True,
    ) -> Any:
        diagnostics.high_level_plan_calls += 1
        considered = 0

        def count_inputs() -> Iterator[Any]:
            nonlocal considered
            for manifest in manifests:
                considered += 1
                yield manifest

        tasks = original_plan_files(planner, count_inputs(), manifest_entry_filter)
        diagnostics.high_level_manifests_considered += considered
        if isinstance(tasks, Sequence):
            diagnostics.high_level_file_scan_tasks_planned += len(tasks)
        return tasks

    iceberg_adapter._manifest_files = counted_manifests
    iceberg_adapter._live_entries = counted_entries
    iceberg_adapter._data_file_batches = counted_data_files
    ManifestGroupPlanner.plan_files = counted_plan_files  # type: ignore[assignment]
    try:
        yield
    finally:
        iceberg_adapter._manifest_files = original_manifests
        iceberg_adapter._live_entries = original_entries
        iceberg_adapter._data_file_batches = original_data_files
        ManifestGroupPlanner.plan_files = original_plan_files  # type: ignore[method-assign]


def _allocation_category(filename: str) -> str:
    normalized = filename.replace("\\", "/")
    if "/pyiceberg/" in normalized:
        return "pyiceberg"
    if "/infrastructure/canonical/" in normalized:
        return "infrastructure/canonical"
    if "/infrastructure/revision/" in normalized:
        return "infrastructure/revision"
    return "other"


def _allocation_deltas(before: Any, after: Any) -> dict[str, dict[str, int]]:
    """Return retained tracemalloc deltas by source path, not process RSS."""
    totals = {
        name: {"size_bytes": 0, "allocation_count": 0}
        for name in (
            "pyiceberg",
            "infrastructure/canonical",
            "infrastructure/revision",
            "other",
        )
    }
    for statistic in after.compare_to(before, "filename"):
        category = _allocation_category(statistic.traceback[0].filename)
        totals[category]["size_bytes"] += statistic.size_diff
        totals[category]["allocation_count"] += statistic.count_diff
    return totals


def _reachable_python_size(root: object) -> int:
    """Estimate Python bytes reachable from the held result; excludes native buffers."""
    seen: set[int] = set()
    pending = [root]
    total = 0
    while pending:
        value = pending.pop()
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        try:
            total += sys.getsizeof(value)
        except (TypeError, ValueError):
            continue
        if isinstance(value, Mapping):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (tuple, list, set, frozenset)):
            pending.extend(value)
        elif is_dataclass(value) and not isinstance(value, type):
            pending.extend(getattr(value, item.name) for item in fields(value))
        elif hasattr(value, "__dict__"):
            pending.extend(vars(value).values())
    return total


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


def _status_kb(name: str, pid: int | str = "self") -> int | None:
    try:
        text = Path(f"/proc/{pid}/status").read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    for line in text.splitlines():
        if line.startswith(f"{name}:"):
            return int(line.split()[1])
    return None


def _mib(kb: float | None) -> float | None:
    return None if kb is None else round(kb / 1024, 1)


def _emit(event: str, **facts: Any) -> None:
    print(_EVENT_PREFIX + json.dumps({"event": event, "t": time.monotonic(), **facts}), flush=True)


# ============================================================================================
# child: fixture setup and measured stages
# ============================================================================================


def _setup(workdir: Path, rows: int, d2_batch: int) -> dict[str, Any]:
    with _opened(workdir, create=True) as (adapter, storage):
        collected = _publish_archive(storage, _agg_trade_lines(rows))
        context = ArchiveContext(
            request_id="e1-main-probe",
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
            raise ProbeError(f"the synthetic archive was not ingested: {outcome!r}")
        return {"unit": outcome.archive_revision_id}


def _validate_dataset_v3_config(config: Mapping[str, int]) -> dict[str, int]:
    """Validate explicit DQ-9 inputs without choosing or defaulting any parameter."""
    if set(config) != set(DATASET_V3_RULE_KEYS):
        missing = sorted(set(DATASET_V3_RULE_KEYS) - set(config))
        extra = sorted(set(config) - set(DATASET_V3_RULE_KEYS))
        raise ValueError(f"Dataset v3 rule parameters incomplete: missing={missing}, extra={extra}")
    if any(
        isinstance(config[key], bool) or not isinstance(config[key], int) or config[key] < 1
        for key in DATASET_V3_RULE_KEYS
    ):
        raise ValueError("Dataset v3 DQ-9 parameters must be positive integers")
    # The factory owns the parameter constraints (including fanout >= 2 and a usable evidence
    # leaf size). Its values are supplied by the caller and become the measured rule hash.
    dataset_evidence_rule(**dict(config))
    return {key: config[key] for key in DATASET_V3_RULE_KEYS}


def _prepare_dataset_v3_fixture(workdir: Path) -> dict[str, Any]:
    """Prepare the full-day Dataset's listing and quality inputs outside the measured child."""
    with _opened(workdir) as (adapter, storage):
        _prepare_listings(adapter, storage)
        _prepare_dataset_quality(adapter, storage)
        pit = _dataset_pit_spec(adapter)
        return {
            "window_start": DATASET_V3_DAY_START.isoformat(),
            "window_end_exclusive": DATASET_V3_DAY_END.isoformat(),
            "data_type": DATA_TYPE,
            "universe_symbols": sorted(FIRST_SLICE_UNIVERSE.symbols),
            "pit_spec_hash": pit.content_hash(),
            "listing_and_quality_inputs_prepared": True,
        }


def _dataset_v3_source_params(microbatch: int) -> tuple[PitRunParams, UniverseRunParams]:
    """The upstream source-run bounds are fixed and reported separately from DQ-9."""
    limits = RunLimits(
        leaf_max_records=DATASET_V3_SOURCE_RUN_RECORDS,
        leaf_max_bytes=DATASET_V3_SOURCE_RUN_BYTES,
        fanout=DATASET_V3_SOURCE_RUN_FANOUT,
    )
    pit = PitRunParams(
        row_batch_rows=microbatch,
        edge_batch_rows=microbatch,
        merge_fanout=DATASET_V3_SOURCE_RUN_FANOUT,
        key_history_buffer=microbatch,
        limits=limits,
    )
    universe = UniverseRunParams(
        capacity=DATASET_V3_SOURCE_RUN_RECORDS,
        merge_fanout=DATASET_V3_SOURCE_RUN_FANOUT,
        limits=limits,
    )
    return pit, universe


def _unit_prefix(unit: str) -> str:
    # ``unit_batch_id`` is ``<prefix><rows>.<chunk>.<index>``; strip the three numeric fields.
    return unit_batch_id(unit, 0, 0, 0).rsplit(".", 3)[0] + "."


def _committed_batch_entries(
    adapter: PyIcebergCatalogAdapter, unit: str
) -> Iterator[tuple[str, int]]:
    """The unit's committed Canonical batches, newest first, straight from the history walk."""
    head = _heads(adapter).get(CANONICAL_TRADES.table)
    prefix = _unit_prefix(unit)
    for snapshot in history_from(adapter, CANONICAL_TRADES.table, head):
        if snapshot.batch_id is not None and snapshot.batch_id.startswith(prefix):
            yield snapshot.batch_id, snapshot.added_rows


def _middle_committed_batch(adapter: PyIcebergCatalogAdapter, unit: str) -> tuple[int, str, int]:
    """(committed batch count, middle batch id, its row count) from two streaming walks."""
    count = sum(1 for _ in _committed_batch_entries(adapter, unit))
    if count == 0:
        raise ProbeError(f"unit {unit} has no committed Canonical batches")
    middle = (count - 1) // 2
    for index, (batch_id, added) in enumerate(_committed_batch_entries(adapter, unit)):
        if index == middle:
            return count, batch_id, added
    raise ProbeError(f"unit {unit} lost its middle Canonical batch during preparation")


def _parse_batch_id(unit: str, batch_id: str) -> tuple[int, int, int]:
    """(unit rows, chunk, index) of one of the unit's batch ids."""
    fields = batch_id[len(_unit_prefix(unit)) :].split(".")
    if len(fields) != 3 or not all(part.isascii() and part.isdigit() for part in fields):
        raise ProbeError(f"unexpected Canonical batch id {batch_id!r}")
    unit_rows, chunk, index = (int(part) for part in fields)
    return unit_rows, chunk, index


def _crash_after(rows: int, microbatch: int) -> int:
    return max(1, -(-rows // microbatch) // 2)


def _first_scanned_value(adapter: Any, table: str, column: str, row_filter: Any) -> Any | None:
    """Read one projected value through the bounded interface and close on the first batch."""
    reader = adapter.scan_column_batches(table, columns=(column,), row_filter=row_filter)
    try:
        for record_batch in reader:
            if record_batch.num_rows:
                return record_batch.column(0)[0].as_py()
    finally:
        close = getattr(reader, "close", None)
        if callable(close):
            close()
    return None


def _stage(
    stage: str,
    adapter: PyIcebergCatalogAdapter,
    storage: LocalFileStorageAdapter,
    unit: str,
    rows: int,
    microbatch: int,
    dataset_v3_config: Mapping[str, int] | None = None,
) -> Callable[[], tuple[dict[str, Any], object]]:
    """A closure doing only the measured work; returns (facts, the API result to hold)."""
    batches = -(-rows // microbatch)

    if stage == DATASET_V3_STAGE:
        if dataset_v3_config is None:
            raise ProbeError("dataset_v3_build requires explicit DQ-9 parameters")
        rule = dataset_evidence_rule(**_validate_dataset_v3_config(dataset_v3_config))
        pit_params, universe_params = _dataset_v3_source_params(microbatch)

        def build_dataset() -> tuple[dict[str, Any], object]:
            pit = _dataset_pit_spec(adapter)
            request = DatasetEvidenceRequest(
                universe=FIRST_SLICE_UNIVERSE,
                pit=pit,
                data_type=DATA_TYPE,
                start=DATASET_V3_DAY_START,
                end=DATASET_V3_DAY_END,
            )

            def sources_for(req: DatasetEvidenceRequest) -> Any:
                return dataset_evidence_sources(
                    adapter,
                    storage,
                    req,
                    market_data_base_url=REST_BASE,
                    pit_params=pit_params,
                    universe_params=universe_params,
                )

            sources = sources_for(request)
            chunks = IcebergChunkWriter(adapter, DATASET_SELECTION_CHUNKS)
            builder = DatasetEvidenceBuilder(adapter, storage, rule=rule)
            verifier = StreamingEvidenceVerifier(
                adapter, builder=builder, chunks=chunks, sources=sources_for
            )
            summary = builder.build(
                request,
                sources=sources,
                chunks=chunks,
                manifests=verifier.store(),
            )
            if summary.row_count != rows:
                raise ProbeError(
                    f"full-day Dataset selected {summary.row_count} rows, expected {rows}"
                )
            return {
                "window_start": request.start.isoformat(),
                "window_end_exclusive": request.end.isoformat(),
                "data_type": request.data_type,
                "universe_symbols": sorted(request.universe.symbols),
                "selection_id": summary.selection_id,
                "manifest_hash": summary.manifest_hash,
                "dataset_snapshot_id": summary.dataset.snapshot_id,
                "row_count": summary.row_count,
                "chunk_count": summary.chunk_count,
                "replayed_chunk_count": summary.replayed_chunk_count,
                "manifest_replayed": summary.manifest_replayed,
                "evidence_streams": {
                    ref.stream.value: {
                        "record_count": ref.record_count,
                        "leaf_count": ref.leaf_count,
                        "depth": ref.depth,
                        "root_sha256": ref.root.sha256,
                        "root_size": ref.root.size,
                    }
                    for ref in summary.evidence
                },
            }, summary

        return build_dataset

    if stage == "verify_archive":
        view = PinnedCatalogView(adapter, _heads(adapter))
        columns = tuple(f.name for f in BINANCE_SPOT_AGG_TRADES.arrow_schema)

        def verify() -> tuple[dict[str, Any], object]:
            # Built as main's normalizer builds it (``_pin``): pinned view, archive cache on.
            verifier = PersistedRowVerifier(view, storage, cache_archives=True)
            lines = verifier.archive_row_count(DATA_TYPE, SYMBOL, unit)
            proven = 0
            for low in range(1, lines + 1, microbatch):
                high = min(low + microbatch - 1, lines)
                reader = view.scan_column_batches(
                    _RAW,
                    columns=columns,
                    row_filter=And(
                        EqualTo("archive_revision_id", unit),  # type: ignore[call-arg, arg-type]
                        And(
                            GreaterThanOrEqual("archive_line_number", low),  # type: ignore[call-arg, arg-type]
                            LessThanOrEqual("archive_line_number", high),  # type: ignore[call-arg, arg-type]
                        ),
                    ),
                )
                window: list[dict[str, Any]] = []
                try:
                    for record_batch in reader:
                        window.extend(record_batch.to_pylist())
                finally:
                    close = getattr(reader, "close", None)
                    if callable(close):
                        close()
                verifier.verify_archive_elements(BINANCE_SPOT_AGG_TRADES, DATA_TYPE, SYMBOL, window)
                proven += len(window)
            if lines != rows or proven != rows:
                raise ProbeError(
                    f"verify_archive proved {proven} of {lines} lines, expected {rows}"
                )
            return {"lines": lines, "rows_proven": proven}, verifier

        return verify

    if stage == "write_crash":
        crash_after = _crash_after(rows, microbatch)
        if batches < 2:
            raise ProbeError("write_crash needs a plan of at least two batches (N > M)")
        clock = _Clock(_KNOWLEDGE_NORMALIZE)
        proxy = _CrashAfter(adapter, crash_after)
        writer = CanonicalNormalizer(proxy, storage, clock=clock, microbatch_rows=microbatch)

        def write_crash() -> tuple[dict[str, Any], object]:
            try:
                writer.normalize_unit(_RAW, unit)
            except _ProbeCrash:
                if proxy.count != crash_after:
                    raise ProbeError(
                        f"crashed after {proxy.count} commits, expected {crash_after}"
                    ) from None
                return {
                    "crash_after": crash_after,
                    "batches": batches,
                    "clock_readings": clock.readings,
                }, writer
            raise ProbeError("the write finished before its simulated crash")

        return write_crash

    if stage in ("resume", "replay"):
        clock = _Clock(_KNOWLEDGE_NORMALIZE + timedelta(hours=1))
        writer = CanonicalNormalizer(adapter, storage, clock=clock, microbatch_rows=microbatch)
        crash_after = _crash_after(rows, microbatch)

        def normalize() -> tuple[dict[str, Any], object]:
            out = writer.normalize_unit(_RAW, unit)
            already = out.replayed_batch_count
            facts = {
                "revision_ids": out.revision_count,
                "commits": out.batch_count,
                "already_committed": already,
                "replayed": out.replayed,
                "clock_readings": clock.readings,
            }
            expected_already = crash_after if stage == "resume" else batches
            if (
                out.revision_count != rows
                or out.batch_count != batches
                or already != expected_already
                or out.replayed is not (stage == "replay")
                or clock.readings != 0
            ):
                raise ProbeError(f"{stage} returned an unexpected result: {facts}")
            return facts, out

        return normalize

    if stage == "read_batch":
        committed_count, batch_id, added = _middle_committed_batch(adapter, unit)
        unit_rows, chunk, index = _parse_batch_id(unit, batch_id)
        if unit_rows != rows or committed_count != batches:
            raise ProbeError(
                f"read_batch found plan ({unit_rows} rows, {committed_count} batches), "
                f"expected ({rows}, {batches})"
            )
        sample = _first_scanned_value(
            adapter,
            CANONICAL_TRADES.table,
            "arrival_seq",
            EqualTo("lineage_source_revision_id", unit),  # type: ignore[call-arg, arg-type]
        )
        if sample is None:
            raise ProbeError(f"no Canonical row found for unit {unit}")
        base = (sample // rules.ARRIVAL_SEQ_STRIDE) * rules.ARRIVAL_SEQ_STRIDE
        # An archive's position p is arrival_seq base + p; batch i holds ranks [i*chunk, +chunk).
        found_row = _first_scanned_value(
            adapter,
            CANONICAL_TRADES.table,
            "arrival_seq",
            row_filter=And(
                EqualTo("lineage_source_revision_id", unit),  # type: ignore[call-arg, arg-type]
                And(
                    GreaterThanOrEqual("arrival_seq", base + index * chunk + 1),  # type: ignore[call-arg, arg-type]
                    LessThanOrEqual("arrival_seq", base + index * chunk + added),  # type: ignore[call-arg, arg-type]
                ),
            ),
        )
        if found_row is None:
            raise ProbeError(f"no committed row inside batch {batch_id}")
        row = found_row
        reader = CanonicalNormalizer(
            PinnedCatalogView(adapter, _heads(adapter)), storage, microbatch_rows=microbatch
        )

        def read_batch() -> tuple[dict[str, Any], object]:
            found = reader.verify_unit(_RAW, unit, arrival_seqs={row})
            if row not in {item["arrival_seq"] for item in found} or len(found) != added:
                raise ProbeError("the proven batch is not the committed batch holding the row")
            return {
                "committed_batches_seen": committed_count,
                "batch_id": batch_id,
                "arrival_seq": row,
                "rows_read": len(found),
            }, found

        return read_batch

    if stage == "metadata":

        def walk() -> tuple[dict[str, Any], object]:
            # One load + walk takes milliseconds: repeat it for a minimum duration so the sampler
            # sees the stage (same memory shape: each round loads the metadata afresh).
            snapshots: dict[str, int] = {}
            rounds = 0
            started = time.monotonic()
            while rounds == 0 or time.monotonic() - started < _METADATA_SECONDS:
                for table, head in _heads(adapter).items():
                    snapshots[table] = sum(1 for _ in history_from(adapter, table, head))
                rounds += 1
            return {"snapshots": snapshots, "rounds": rounds}, snapshots

        return walk

    raise ProbeError(f"unknown stage {stage!r}")


def _child_stage(
    stage: str,
    workdir: Path,
    unit: str,
    rows: int,
    microbatch: int,
    *,
    staged_diagnostics: bool,
    dataset_v3_config: Mapping[str, int] | None = None,
) -> None:
    with _opened(workdir) as (inner_adapter, storage):
        diagnostics = _StageDiagnostics()
        adapter = (
            _CountingAdapter(inner_adapter, diagnostics) if staged_diagnostics else inner_adapter
        )
        body = _stage(
            stage,
            cast(PyIcebergCatalogAdapter, adapter),
            storage,
            unit,
            rows,
            microbatch,
            dataset_v3_config,
        )
        gc.collect()
        _emit("ready", vmhwm_kb=_status_kb("VmHWM"), vmrss_kb=_status_kb("VmRSS"))
        time.sleep(_SETTLE_SECONDS)
        if staged_diagnostics:
            import tracemalloc

            tracemalloc.start(1)
            before = tracemalloc.take_snapshot()
        _emit("start")
        started = time.perf_counter()
        counter_scope = _count_scan_work(diagnostics) if staged_diagnostics else nullcontext()
        with counter_scope:
            facts, held = body()
        wall = time.perf_counter() - started
        time.sleep(_HOLD_SECONDS)  # ``held`` is still referenced: its residency is sampled
        _emit(
            "end",
            stage_seconds=round(wall, 3),
            hold_seconds=_HOLD_SECONDS,
            vmhwm_kb=_status_kb("VmHWM"),
            facts=facts,
        )
        if staged_diagnostics:
            after = tracemalloc.take_snapshot()
            _emit(
                "diagnostics",
                adapter_calls=dict(diagnostics.adapter_calls),
                manifest_list_entries_read=diagnostics.manifest_list_entries_read,
                live_manifest_entries_read=diagnostics.live_manifest_entries_read,
                data_file_reads_started=diagnostics.data_file_reads_started,
                high_level_plan_calls=diagnostics.high_level_plan_calls,
                high_level_manifests_considered=diagnostics.high_level_manifests_considered,
                high_level_file_scan_tasks_planned=(diagnostics.high_level_file_scan_tasks_planned),
                tracemalloc_retained_deltas=_allocation_deltas(before, after),
                held_result={
                    "type": f"{type(held).__module__}.{type(held).__qualname__}",
                    "reachable_python_bytes_estimate": _reachable_python_size(held),
                    "native_buffer_bytes_included": False,
                },
                tracemalloc_is_rss=False,
            )
            tracemalloc.stop()
        del held


def _child_main(args: argparse.Namespace) -> int:
    _emit(
        "boot",
        pid=os.getpid(),
        normalizer_file=str(Path(inspect.getfile(CanonicalNormalizer)).resolve()),
        vmhwm_kb=_status_kb("VmHWM"),
        vmrss_kb=_status_kb("VmRSS"),
    )
    try:
        if args.stage == "setup":
            _emit("setup", **_setup(args.workdir, args.unit_rows, args.d2_batch))
        elif args.stage == "dataset_setup":
            _emit("dataset_setup", **_prepare_dataset_v3_fixture(args.workdir))
        else:
            dataset_v3_config = None
            if args.dataset_v3_config_json is not None:
                decoded = json.loads(args.dataset_v3_config_json)
                if not isinstance(decoded, dict):
                    raise ProbeError("Dataset v3 config must be a JSON object")
                dataset_v3_config = _validate_dataset_v3_config(decoded)
            _child_stage(
                args.stage,
                args.workdir,
                args.unit,
                args.unit_rows,
                args.microbatch,
                staged_diagnostics=args.staged_diagnostics,
                dataset_v3_config=dataset_v3_config,
            )
    except BaseException as exc:  # noqa: BLE001 - reported to the parent, then re-signalled
        traceback.print_exc(file=sys.stderr)
        _emit("error", type=type(exc).__name__, message=str(exc)[:2_000])
        return 1
    return 0


# ============================================================================================
# parent: isolation, sampling, attribution, verdicts
# ============================================================================================


@dataclass
class _ChildRun:
    args: list[str]
    events: list[dict[str, Any]]
    samples: list[tuple[float, int]]
    returncode: int
    stderr_tail: str
    wall_seconds: float
    rss_guard_tripped_kb: int | None = None
    timed_out: bool = False
    by_event: dict[str, dict[str, Any]] = field(default_factory=dict)


def _run_child(
    args: list[str],
    *,
    temp_dir: Path,
    runtime: str,
    interval: float,
    timeout: float,
    rss_limit_kb: int,
) -> _ChildRun:
    """Run one child from the repository root; sample its ``VmRSS`` until it exits."""
    env = {name: value for name, value in os.environ.items() if name not in RUNTIMES["controlled"]}
    env.update(RUNTIMES[runtime])
    env["TMPDIR"] = str(temp_dir.resolve())
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "-m", PROBE, *args],
        cwd=_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    samples: list[tuple[float, int]] = []
    state: dict[str, Any] = {"guard": None, "timed_out": False}
    stop_sampler = threading.Event()

    def sample() -> None:
        while not stop_sampler.is_set() and process.poll() is None:
            now = time.monotonic()
            rss = _status_kb("VmRSS", process.pid)
            if rss is not None:
                samples.append((now, rss))
                if rss > rss_limit_kb and state["guard"] is None:
                    state["guard"] = rss
                    process.kill()
            if now - started > timeout and not state["timed_out"]:
                state["timed_out"] = True
                process.kill()
            if stop_sampler.wait(interval):
                break

    sampler = threading.Thread(target=sample, daemon=True)
    try:
        sampler.start()
        stdout, stderr = process.communicate()
        stop_sampler.set()
        sampler.join()
    except BaseException:
        # SIGINT/SIGTERM and KeyboardInterrupt must not orphan the active measurement child.
        stop_sampler.set()
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            pass
        try:
            process.communicate()
        except BaseException:
            try:
                process.wait()
            except BaseException:
                pass
        if sampler.ident is not None:
            sampler.join()
        raise
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if line.startswith(_EVENT_PREFIX):
            try:
                events.append(json.loads(line[len(_EVENT_PREFIX) :]))
            except json.JSONDecodeError as exc:
                raise ProbeError(f"unparseable child event line: {line[:200]!r}") from exc
    run = _ChildRun(
        args=args,
        events=events,
        samples=samples,
        returncode=process.returncode,
        stderr_tail=stderr[-_STDERR_TAIL:],
        wall_seconds=round(time.monotonic() - started, 3),
        rss_guard_tripped_kb=state["guard"],
        timed_out=state["timed_out"],
        by_event={event["event"]: event for event in events},
    )
    if run.returncode != 0 or run.rss_guard_tripped_kb is not None or run.timed_out:
        raise ProbeError(
            f"probe child {args[:2]} failed",
            {
                "args": args,
                "returncode": run.returncode,
                "error_event": run.by_event.get("error"),
                "rss_guard_tripped_mib": _mib(run.rss_guard_tripped_kb),
                "rss_limit_mib": _mib(rss_limit_kb),
                "timed_out": run.timed_out,
                "timeout_seconds": timeout,
                "max_sampled_rss_mib": _mib(max((kb for _, kb in samples), default=0)),
                "stderr_tail": run.stderr_tail,
            },
        )
    boot = run.by_event.get("boot")
    imported = None if boot is None else Path(boot["normalizer_file"])
    if imported is None or not imported.is_relative_to(_ROOT):
        raise ProbeError(
            "the child did not import the production code of this checkout",
            {"expected_root": str(_ROOT), "imported": None if imported is None else str(imported)},
        )
    return run


def _attribute(run: _ChildRun, interval: float) -> dict[str, Any]:
    missing = [name for name in ("ready", "start", "end") if name not in run.by_event]
    if missing:
        raise ProbeError(f"child {run.args[:2]} did not report {missing}")
    ready, start, end = run.by_event["ready"], run.by_event["start"], run.by_event["end"]
    settle = [rss for t, rss in run.samples if ready["t"] <= t <= start["t"]]
    during = [(t, rss) for t, rss in run.samples if start["t"] <= t <= end["t"]]
    if len(settle) < _MIN_SETTLE_SAMPLES or len(during) < _MIN_STAGE_SAMPLES:
        raise ProbeError(
            f"too few samples for {run.args[:2]}: {len(settle)} settle / {len(during)} stage; "
            f"need at least {_MIN_SETTLE_SAMPLES} / {_MIN_STAGE_SAMPLES} (lower --interval)"
        )
    baseline_kb = statistics.median(settle)
    peak_t, peak_kb = max(during, key=lambda item: item[1])
    delta_kb = peak_kb - baseline_kb
    diagnostics = run.by_event.get("diagnostics")
    return {
        "baseline_kb": baseline_kb,
        "peak_kb": peak_kb,
        "delta_kb": delta_kb,
        "baseline_mib": round(baseline_kb / 1024, 1),
        "peak_mib": round(peak_kb / 1024, 1),
        "delta_mib": round(delta_kb / 1024, 1),
        "settle_min_mib": round(min(settle) / 1024, 1),
        "settle_max_mib": round(max(settle) / 1024, 1),
        "settle_samples": len(settle),
        "stage_samples": len(during),
        "peak_at_seconds_after_start": round(peak_t - start["t"], 3),
        "stage_window_seconds": round(end["t"] - start["t"], 3),
        "interval_seconds": interval,
        "vmrss_at_ready_mib": _mib(ready["vmrss_kb"]),
        "process_vmhwm_at_ready_mib": _mib(ready["vmhwm_kb"]),
        "process_vmhwm_at_end_mib": _mib(end["vmhwm_kb"]),
        "stage_seconds": end["stage_seconds"],
        "hold_seconds": end["hold_seconds"],
        "child_wall_seconds": run.wall_seconds,
        "facts": end["facts"],
        "diagnostics": (
            None
            if diagnostics is None
            else {key: value for key, value in diagnostics.items() if key != "event"}
        ),
    }


def _slope(points: Sequence[tuple[int, float]]) -> float | None:
    """Least-squares slope of ``(x, KiB)``, in bytes per unit of ``x``."""
    if len({x for x, _ in points}) < 2:
        return None
    xs, ys = [x for x, _ in points], [y * 1024 for _, y in points]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mean_x) ** 2 for x in xs)
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / den


def _validate_sizes(sizes: Sequence[int]) -> None:
    if len(sizes) < 3 or len(set(sizes)) != len(sizes):
        raise ValueError("the probe needs at least three distinct N sizes, without duplicates")
    if any(size < 1 for size in sizes):
        raise ValueError("N sizes must be positive")


def _stage_verdict(
    records: Sequence[dict[str, Any]],
    sizes: Sequence[int],
    repeats: int,
    microbatch: int,
) -> dict[str, Any]:
    """Fail closed on incomplete data; gate the full range of every delta at every N."""
    by_size: dict[int, list[float]] = {size: [] for size in sizes}
    seen_repeats: dict[int, set[int]] = {size: set() for size in sizes}
    for record in records:
        rows = record["rows"]
        repeat = record["repeat"]
        if rows not in by_size:
            raise ProbeError(f"measurement at unrequested N {rows}")
        if repeat not in range(repeats) or repeat in seen_repeats[rows]:
            raise ProbeError(f"invalid or duplicate repeat {repeat} at N={rows}")
        seen_repeats[rows].add(repeat)
        by_size[rows].append(float(record["delta_kb"]))
    if any(indices != set(range(repeats)) for indices in seen_repeats.values()):
        raise ProbeError("missing or duplicate stage measurements")
    every = [delta for deltas in by_size.values() for delta in deltas]
    if not all(math.isfinite(delta) for delta in every):
        raise ProbeError("non-finite RSS measurement")
    growth_kb = max(every) - min(every)
    medians = [(size, statistics.median(deltas)) for size, deltas in sorted(by_size.items())]
    maxima = [(size, max(deltas)) for size, deltas in sorted(by_size.items())]
    median_slope, max_slope = _slope(medians), _slope(maxima)
    return {
        "delta_mib_by_rows": {
            str(size): {
                "samples": [round(d / 1024, 1) for d in deltas],
                "min": round(min(deltas) / 1024, 1),
                "median": round(statistics.median(deltas) / 1024, 1),
                "max": round(max(deltas) / 1024, 1),
            }
            for size, deltas in sorted(by_size.items())
        },
        "growth_mib": round(growth_kb / 1024, 1),
        "growth_definition": "max(delta) - min(delta) over every repeat at every N",
        "median_range_mib": _mib(max(m for _, m in medians) - min(m for _, m in medians)),
        "slope_bytes_per_row_median": None if median_slope is None else round(median_slope, 2),
        "slope_bytes_per_batch_median": (
            None if median_slope is None else round(median_slope * microbatch, 1)
        ),
        "slope_bytes_per_row_max": None if max_slope is None else round(max_slope, 2),
        "limit_mib": GROWTH_LIMIT_MIB,
        "pass": growth_kb <= GROWTH_LIMIT_MIB * 1024,
    }


# ------------------------------------------------------------------ environment facts


def _filesystem(path: Path) -> str:
    """The filesystem type holding ``path`` (longest ``/proc/mounts`` prefix)."""
    resolved = str(path.resolve())
    best, kind = "", "unknown"
    for line in Path("/proc/mounts").read_text().splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mount = parts[1]
        inside = resolved == mount or resolved.startswith(mount.rstrip("/") + "/")
        if inside and len(mount) > len(best):
            best, kind = mount, parts[2]
    return kind


def _cgroup_memory_limit() -> dict[str, Any]:
    """This process's cgroup memory limit (v2 ``memory.max``, v1 ``memory.limit_in_bytes``)."""
    try:
        lines = Path("/proc/self/cgroup").read_text().splitlines()
    except OSError:
        return {"limit_bytes": None, "source": None}
    for line in lines:
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        controllers, rel = parts[1], parts[2].lstrip("/")
        if controllers == "":
            candidates = [Path("/sys/fs/cgroup") / rel / "memory.max"]
        elif "memory" in controllers.split(","):
            candidates = [Path("/sys/fs/cgroup/memory") / rel / "memory.limit_in_bytes"]
        else:
            continue
        for candidate in candidates:
            try:
                text = candidate.read_text().strip()
            except OSError:
                continue
            if text == "max" or not text.isdigit() or int(text) >= 1 << 60:
                return {"limit_bytes": None, "source": str(candidate)}
            return {"limit_bytes": int(text), "source": str(candidate)}
    return {"limit_bytes": None, "source": None}


def _git(*args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(_ROOT), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _probe_identity(sha256: str | None, status: str | None) -> dict[str, Any]:
    """Describe this probe source and whether the working file differs from ``HEAD``."""
    relative_to_head = (
        "unknown" if sha256 is None or status is None else ("dirty" if status else "clean")
    )
    return {
        "path": _SELF,
        "sha256": sha256,
        "relative_to_head": relative_to_head,
        "matches_head": relative_to_head == "clean",
    }


def _code_line() -> dict[str, Any]:
    """Whether the imported production code is the local ``main`` code line (this file aside)."""
    pathspec = ["--", *_CODE_PATHS, f":(exclude){_SELF}"]
    head = _git("rev-parse", "HEAD")
    main_ref = _git("rev-parse", "--verify", "--quiet", "refs/heads/main")
    diff = None if main_ref is None else _git("diff", "--name-only", main_ref, *pathspec)
    dirty = _git("status", "--porcelain", "--untracked-files=all", *pathspec)
    probe_dirty = _git("status", "--porcelain", "--untracked-files=all", "--", _SELF)
    differing = None if diff is None else [line for line in diff.splitlines() if line]
    dirty_lines = None if dirty is None else [line for line in dirty.splitlines() if line]
    probe = _probe_identity(_file_sha256(_ROOT / _SELF), probe_dirty)
    return {
        "root": str(_ROOT),
        "head": head,
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "main": main_ref,
        "head_is_main": head is not None and head == main_ref,
        "head_contains_main": (
            None
            if main_ref is None
            else _git("merge-base", "--is-ancestor", main_ref, "HEAD") is not None
        ),
        "compared_paths": list(_CODE_PATHS),
        "excluded": _SELF,
        "probe_source": probe,
        "differs_from_main": differing,
        "dirty": dirty_lines,
        "matches_main": differing == [] and dirty_lines == [],
    }


def _versions() -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    for name in _DEPENDENCIES:
        try:
            found[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            found[name] = None
    return found


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _mem_total_kb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1])
    except OSError:
        return None
    return None


def _environment(base: Path, runtime: str) -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "executable": sys.executable,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "mem_total_mib": (None if (kb := _mem_total_kb()) is None else round(kb / 1024)),
        "cgroup_memory": _cgroup_memory_limit(),
        "workdir_base": str(base.resolve()),
        "workdir_filesystem": _filesystem(base),
        "child_temporary_directory": "per-run work directory via TMPDIR",
        "runtime": runtime,
        "runtime_env": RUNTIMES[runtime],
        "dependencies": _versions(),
        "uv_lock_sha256": _file_sha256(_ROOT / "uv.lock"),
    }


# ------------------------------------------------------------------ the run


def _child_args(
    stage: str,
    workdir: Path,
    rows: int,
    config: dict[str, int],
    unit: str | None,
    *,
    staged_diagnostics: bool,
    dataset_v3_config: Mapping[str, int] | None = None,
) -> list[str]:
    args = [
        "--stage",
        stage,
        "--workdir",
        str(workdir),
        f"--unit-rows={rows}",
        f"--microbatch={config['microbatch']}",
        f"--d2-batch={config['d2_batch']}",
    ]
    if unit is not None:
        args.append(f"--unit={unit}")
    if dataset_v3_config is not None:
        args.append("--dataset-v3-config-json")
        args.append(json.dumps(_validate_dataset_v3_config(dataset_v3_config), sort_keys=True))
    if staged_diagnostics and stage != "setup":
        args.append("--stage-diagnostics")
    return args


def _write_samples(handle: Any, rows: int, repeat: int, stage: str, run: _ChildRun) -> None:
    if handle is None:
        return
    origin = run.samples[0][0] if run.samples else 0.0
    handle.write(
        json.dumps(
            {
                "rows": rows,
                "repeat": repeat,
                "stage": stage,
                "events": [{**event, "t": round(event["t"] - origin, 4)} for event in run.events],
                "samples": [[round(t - origin, 4), kb] for t, kb in run.samples],
            }
        )
        + "\n"
    )
    handle.flush()


def run_probe(
    sizes: Sequence[int],
    config: dict[str, int],
    *,
    base: Path,
    repeats: int = DEFAULT_REPEATS,
    runtime: str = "controlled",
    interval: float = DEFAULT_INTERVAL_SECONDS,
    child_timeout: float = DEFAULT_CHILD_TIMEOUT_SECONDS,
    child_rss_limit_mib: int = DEFAULT_CHILD_RSS_LIMIT_MIB,
    samples_out: Path | None = None,
    keep_workdirs: bool = False,
    staged_diagnostics: bool = False,
    dataset_v3_config: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Measure every stage at every N, ``repeats`` times; never raises for a failed child."""
    _validate_sizes(sizes)
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("the RSS sample interval must be finite and greater than zero")
    if min(config.values()) < 1:
        raise ValueError("microbatch and D2 batch sizes must be positive")
    if dataset_v3_config is not None:
        dataset_v3_config = _validate_dataset_v3_config(dataset_v3_config)
    code_line = _code_line()
    conformance = {
        "microbatch_is_256": config["microbatch"] == PROTOCOL_MICROBATCH,
        "sizes_include_10k_100k_500k": set(PROTOCOL_SIZES) <= set(sizes),
        "repeats_at_least_3": repeats >= PROTOCOL_MIN_REPEATS,
        "code_line_matches_main": code_line["matches_main"] is True,
        "probe_matches_head": code_line["probe_source"]["matches_head"] is True,
        "staged_diagnostics_disabled": not staged_diagnostics,
    }
    document: dict[str, Any] = {
        "probe": PROBE,
        "measures": "the checkout's own production code; see code_line.matches_main",
        "status": "running",
        "config": {
            "sizes": list(sizes),
            "repeats": repeats,
            **config,
            "stages": list(STAGES),
            "dataset_v3_measurement": (
                {"status": "not_requested"}
                if dataset_v3_config is None
                else {
                    "status": "requested",
                    "stage": DATASET_V3_STAGE,
                    "rule_parameters": dict(dataset_v3_config),
                    "day_window": {
                        "start": DATASET_V3_DAY_START.isoformat(),
                        "end_exclusive": DATASET_V3_DAY_END.isoformat(),
                    },
                    "source_run_parameters": {
                        "leaf_max_records": DATASET_V3_SOURCE_RUN_RECORDS,
                        "leaf_max_bytes": DATASET_V3_SOURCE_RUN_BYTES,
                        "fanout": DATASET_V3_SOURCE_RUN_FANOUT,
                        "pit_row_batch_rows": config["microbatch"],
                        "pit_edge_batch_rows": config["microbatch"],
                        "pit_key_history_buffer": config["microbatch"],
                        "universe_capacity": DATASET_V3_SOURCE_RUN_RECORDS,
                        "universe_merge_fanout": DATASET_V3_SOURCE_RUN_FANOUT,
                    },
                }
            ),
            "sample_interval_seconds": interval,
            "settle_seconds": _SETTLE_SECONDS,
            "hold_seconds": _HOLD_SECONDS,
            "metadata_min_seconds": _METADATA_SECONDS,
            "min_settle_samples": _MIN_SETTLE_SAMPLES,
            "min_stage_samples": _MIN_STAGE_SAMPLES,
            "growth_limit_mib": GROWTH_LIMIT_MIB,
            "child_timeout_seconds": child_timeout,
            "child_rss_limit_mib": child_rss_limit_mib,
            "fixture": "infrastructure.tools.capacity_probe synthetic aggTrades archive",
            "staged_diagnostics": staged_diagnostics,
            "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        "code_line": code_line,
        "environment": _environment(base, runtime),
        "protocol_conformance": conformance,
        "results": [],
        "setups": [],
        "cleanup_warnings": [],
    }
    results: list[dict[str, Any]] = document["results"]
    rss_limit_kb = child_rss_limit_mib * 1024
    handle = None if samples_out is None else samples_out.open("w", encoding="utf-8")
    try:
        for repeat in range(repeats):
            for rows in sizes:
                workdir = Path(tempfile.mkdtemp(prefix=f"e1-main-{rows}-r{repeat}-", dir=base))
                try:
                    if _filesystem(workdir) in _REFUSED_FILESYSTEMS:
                        raise ProbeError(f"work directory {workdir} is on a memory filesystem")
                    setup = _run_child(
                        _child_args(
                            "setup",
                            workdir,
                            rows,
                            config,
                            None,
                            staged_diagnostics=False,
                        ),
                        temp_dir=workdir,
                        runtime=runtime,
                        interval=interval,
                        timeout=child_timeout,
                        rss_limit_kb=rss_limit_kb,
                    )
                    if "setup" not in setup.by_event:
                        raise ProbeError(f"setup child for N={rows} reported no unit")
                    unit = setup.by_event["setup"]["unit"]
                    document["setups"].append(
                        {
                            "rows": rows,
                            "repeat": repeat,
                            "unit": unit,
                            "wall_seconds": setup.wall_seconds,
                            "max_sampled_rss_mib": round(
                                max((kb for _, kb in setup.samples), default=0) / 1024, 1
                            ),
                        }
                    )
                    for stage in STAGES:
                        run = _run_child(
                            _child_args(
                                stage,
                                workdir,
                                rows,
                                config,
                                unit,
                                staged_diagnostics=staged_diagnostics,
                            ),
                            temp_dir=workdir,
                            runtime=runtime,
                            interval=interval,
                            timeout=child_timeout,
                            rss_limit_kb=rss_limit_kb,
                        )
                        _write_samples(handle, rows, repeat, stage, run)
                        record = {
                            "rows": rows,
                            "batches": -(-rows // config["microbatch"]),
                            "repeat": repeat,
                            "stage": stage,
                            **_attribute(run, interval),
                        }
                        results.append(record)
                        progress = {k: v for k, v in record.items() if not k.endswith("_kb")}
                        print(json.dumps(progress), file=sys.stderr, flush=True)
                    if dataset_v3_config is not None:
                        fixture = _run_child(
                            _child_args(
                                "dataset_setup",
                                workdir,
                                rows,
                                config,
                                unit,
                                staged_diagnostics=False,
                            ),
                            temp_dir=workdir,
                            runtime=runtime,
                            interval=interval,
                            timeout=child_timeout,
                            rss_limit_kb=rss_limit_kb,
                        )
                        if "dataset_setup" not in fixture.by_event:
                            raise ProbeError(
                                f"Dataset fixture child for N={rows} reported no setup facts"
                            )
                        fixture_facts = fixture.by_event["dataset_setup"]
                        dataset_run = _run_child(
                            _child_args(
                                DATASET_V3_STAGE,
                                workdir,
                                rows,
                                config,
                                unit,
                                staged_diagnostics=staged_diagnostics,
                                dataset_v3_config=dataset_v3_config,
                            ),
                            temp_dir=workdir,
                            runtime=runtime,
                            interval=interval,
                            timeout=child_timeout,
                            rss_limit_kb=rss_limit_kb,
                        )
                        _write_samples(handle, rows, repeat, DATASET_V3_STAGE, dataset_run)
                        dataset_record = {
                            "rows": rows,
                            "repeat": repeat,
                            "stage": DATASET_V3_STAGE,
                            "fixture": fixture_facts,
                            "rule_parameters": dict(dataset_v3_config),
                            **_attribute(dataset_run, interval),
                        }
                        document.setdefault("dataset_v3_results", []).append(dataset_record)
                        progress = {
                            k: v for k, v in dataset_record.items() if not k.endswith("_kb")
                        }
                        print(json.dumps(progress), file=sys.stderr, flush=True)
                finally:
                    if not keep_workdirs:
                        try:
                            shutil.rmtree(workdir)
                        except (ProbeInterrupted, KeyboardInterrupt):
                            raise
                        except Exception as cleanup_exc:  # noqa: BLE001 - report failed cleanup
                            document["cleanup_warnings"].append(
                                {
                                    "workdir": str(workdir),
                                    "type": type(cleanup_exc).__name__,
                                    "message": str(cleanup_exc),
                                }
                            )
        verdicts = {
            stage: _stage_verdict(
                [r for r in results if r["stage"] == stage], sizes, repeats, config["microbatch"]
            )
            for stage in STAGES
        }
        if dataset_v3_config is not None:
            document["dataset_v3_verdict"] = _stage_verdict(
                document["dataset_v3_results"], sizes, repeats, config["microbatch"]
            )
            document["dataset_v3_measurement"]["status"] = "complete"
            document["dataset_v3_measurement"]["verdict"] = (
                "PASS" if document["dataset_v3_verdict"]["pass"] else "FAIL"
            )
    except (ProbeInterrupted, KeyboardInterrupt) as exc:
        signum = exc.signum if isinstance(exc, ProbeInterrupted) else signal.SIGINT
        try:
            signal_name = signal.Signals(signum).name
        except ValueError:
            signal_name = str(signum)
        document["status"] = "interrupted"
        document["error"] = {
            "type": "ProbeInterrupted",
            "message": str(exc) or f"probe interrupted by {signal_name}",
            "signal_number": signum,
            "signal_name": signal_name,
            "partial_stage_results": len(results),
            "partial_dataset_results": len(document.get("dataset_v3_results", [])),
        }
        document["capacity_verdict"] = "ERROR"
        document["e1_cap1_evidence"] = False
        if dataset_v3_config is not None:
            document["dataset_v3_measurement"]["status"] = "interrupted"
        return document
    except Exception as exc:  # noqa: BLE001 - any failure is reported, never a verdict
        details = exc.details if isinstance(exc, ProbeError) else {}
        document["status"] = "error"
        document["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            **details,
            "traceback": traceback.format_exc()[-_STDERR_TAIL:],
        }
        document["capacity_verdict"] = "ERROR"
        document["e1_cap1_evidence"] = False
        if dataset_v3_config is not None:
            document["dataset_v3_measurement"]["status"] = "failed"
        return document
    finally:
        if handle is not None:
            handle.close()
    numeric_pass = all(verdict["pass"] is True for verdict in verdicts.values())
    evidence = all(conformance.values())
    document["status"] = "complete"
    document["verdicts"] = verdicts
    document["capacity_verdict"] = "PASS" if numeric_pass else "FAIL"
    document["e1_cap1_evidence"] = evidence
    document["note"] = "RSS growth only. " + (
        "Evidence-grade configuration; E1-CAP-1 closure still needs the structural "
        "assertions, targeted tests and independent review of the E1 review."
        if evidence
        else "Diagnostic only: the configuration or code line is not the E1-CAP-1 protocol "
        "on main (see protocol_conformance); not E1-CAP-1 evidence either way."
    )
    return document


def _exit_status(document: dict[str, Any]) -> int:
    if document["status"] == "interrupted":
        return 128 + int(document.get("error", {}).get("signal_number", signal.SIGINT))
    if document["status"] != "complete":
        return 4
    if document["capacity_verdict"] != "PASS" or (
        "dataset_v3_verdict" in document and document["dataset_v3_verdict"]["pass"] is not True
    ):
        return 1
    return 0 if document["e1_cap1_evidence"] else 3


def _interrupted_document(
    exc: ProbeInterrupted | KeyboardInterrupt,
    dataset_v3_config: Mapping[str, int] | None,
) -> dict[str, Any]:
    """Fallback report for an interruption before ``run_probe`` can create its report."""
    signum = exc.signum if isinstance(exc, ProbeInterrupted) else signal.SIGINT
    try:
        signal_name = signal.Signals(signum).name
    except ValueError:
        signal_name = str(signum)
    document: dict[str, Any] = {
        "probe": PROBE,
        "status": "interrupted",
        "error": {
            "type": "ProbeInterrupted",
            "message": str(exc) or f"probe interrupted by {signal_name}",
            "signal_number": signum,
            "signal_name": signal_name,
            "partial_stage_results": 0,
            "partial_dataset_results": 0,
        },
        "capacity_verdict": "ERROR",
        "e1_cap1_evidence": False,
        "results": [],
        "setups": [],
        "cleanup_warnings": [],
    }
    if dataset_v3_config is not None:
        document["dataset_v3_measurement"] = {
            "status": "interrupted",
            "stage": DATASET_V3_STAGE,
            "rule_parameters": dict(dataset_v3_config),
        }
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=f"python -m {PROBE}",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--rows", type=int, nargs="+", default=list(DEFAULT_SIZES))
    parser.add_argument(
        "--microbatch",
        type=int,
        default=DEFAULT_MICROBATCH,
        help="Canonical microbatch M (E1-CAP-1 protocol: 256; anything else is diagnostic only)",
    )
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--d2-batch", type=int, default=DEFAULT_D2_BATCH)
    parser.add_argument(
        "--interval", type=float, default=DEFAULT_INTERVAL_SECONDS, help="RSS sample interval (s)"
    )
    parser.add_argument("--runtime", choices=tuple(RUNTIMES), default="controlled")
    parser.add_argument(
        "--base",
        type=Path,
        default=Path(os.environ.get("HLENS_PROBE_DIR", Path.home() / ".cache" / "hlens-probe")),
        help="work directory root (on disk; tmpfs / ramfs is refused)",
    )
    parser.add_argument("--child-timeout", type=float, default=DEFAULT_CHILD_TIMEOUT_SECONDS)
    parser.add_argument(
        "--child-rss-limit-mib",
        type=int,
        default=DEFAULT_CHILD_RSS_LIMIT_MIB,
        help="kill a child (and fail the run) once its sampled VmRSS exceeds this",
    )
    parser.add_argument("--json-out", type=Path, help="also write the JSON document here")
    parser.add_argument("--samples-out", type=Path, help="raw VmRSS series, JSON lines")
    parser.add_argument(
        "--staged-diagnostics",
        action="store_true",
        help="collect instrumented call/allocation attribution; diagnostic only, never E1 evidence",
    )
    parser.add_argument("--keep-workdirs", action="store_true")
    parser.add_argument("--i-know-memory", action="store_true")
    parser.add_argument(
        "--allow-uncapped",
        action="store_true",
        help="allow N above the guard without a cgroup memory limit on this process",
    )
    parser.add_argument(
        "--stage",
        choices=("setup", "dataset_setup", *STAGES, DATASET_V3_STAGE),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--workdir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--unit", help=argparse.SUPPRESS)
    parser.add_argument("--unit-rows", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--stage-diagnostics", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dataset-v3-config-json", help=argparse.SUPPRESS)
    parser.add_argument(
        "--dataset-v3",
        action="store_true",
        help="also measure ADR-0077 v3 Dataset over a full UTC day (requires all four DQ-9 values)",
    )
    for key in DATASET_V3_RULE_KEYS:
        parser.add_argument(
            f"--dataset-{key.replace('_', '-')}",
            type=int,
            help=f"explicit ADR-0077 Dataset rule input {key}; no DQ-9 defaults are selected",
        )
    args = parser.parse_args(argv)
    if args.stage is not None:
        return _child_main(args)
    supplied_dataset_values = {key: getattr(args, f"dataset_{key}") for key in DATASET_V3_RULE_KEYS}
    provided = {key: value for key, value in supplied_dataset_values.items() if value is not None}
    if args.dataset_v3:
        if len(provided) != len(DATASET_V3_RULE_KEYS):
            parser.error("--dataset-v3 requires all four --dataset-* DQ-9 values explicitly")
        try:
            dataset_v3_config = _validate_dataset_v3_config(provided)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        if provided:
            parser.error("--dataset-* values require --dataset-v3")
        dataset_v3_config = None
    try:
        _validate_sizes(args.rows)
    except ValueError as exc:
        parser.error(str(exc))
    if args.microbatch < 1 or args.d2_batch < 1:
        parser.error("--microbatch and --d2-batch must be positive")
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval must be finite and greater than zero")
    if not math.isfinite(args.child_timeout) or args.child_timeout <= 0:
        parser.error("--child-timeout must be finite and greater than zero")
    if args.child_rss_limit_mib < 1:
        parser.error("--child-rss-limit-mib must be positive")
    if max(args.rows) > MAX_ROWS_WITHOUT_OVERRIDE:
        if not args.i_know_memory:
            parser.error(f"--rows above {MAX_ROWS_WITHOUT_OVERRIDE} needs --i-know-memory")
        if _cgroup_memory_limit()["limit_bytes"] is None and not args.allow_uncapped:
            parser.error(
                "no cgroup memory limit on this process: run under "
                "`systemd-run --user --scope -p MemoryMax=... -p MemorySwapMax=0` "
                "or pass --allow-uncapped"
            )
    try:
        with _parent_interrupt_handlers():
            kind = _filesystem(args.base)
            if kind in _REFUSED_FILESYSTEMS:
                parser.error(f"{args.base} is on {kind}: a warehouse there is memory")
            args.base.mkdir(parents=True, exist_ok=True)
            config = {"microbatch": args.microbatch, "d2_batch": args.d2_batch}
            document = run_probe(
                sorted(args.rows),
                config,
                base=args.base,
                repeats=args.repeats,
                runtime=args.runtime,
                interval=args.interval,
                child_timeout=args.child_timeout,
                child_rss_limit_mib=args.child_rss_limit_mib,
                samples_out=args.samples_out,
                keep_workdirs=args.keep_workdirs,
                staged_diagnostics=args.staged_diagnostics,
                dataset_v3_config=dataset_v3_config,
            )
    except (ProbeInterrupted, KeyboardInterrupt) as exc:
        document = _interrupted_document(exc, dataset_v3_config)
    text = json.dumps(document, indent=2, default=str)
    if args.json_out is not None:
        args.json_out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return _exit_status(document)


if __name__ == "__main__":
    raise SystemExit(main())

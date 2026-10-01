"""Bounded, reproducible capacity diagnostics for :class:`SQLitePitGraph`.

This is an engineering diagnostic, not the E1-CAP-1 acceptance gate. Inputs are
generated lazily and hard-capped so the harness itself does not retain a graph-sized
Python fixture. Run with ``python -m infrastructure.pit.diagnostics``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Any

from core.contracts.revision import (
    AvailabilityDecision,
    ObservationTimes,
    PrecedenceEvidence,
    RevisionRecord,
)
from infrastructure.pit.sqlite_graph import SQLitePitGraph
from infrastructure.revision.availability import AVAILABILITY_BINDING
from infrastructure.revision.precedence import PRECEDENCE_BINDING

_BASE = datetime(2025, 3, 1, tzinfo=UTC)
_MAX_VERTICES = 512
_MAX_FANOUT = 8
_MAX_CUTOFFS = 16
_RID = re.compile(r"^diag-(\d{6})$")


def _revision_id(index: int) -> str:
    return f"diag-{index:06d}"


@dataclass(frozen=True)
class _Availability(Mapping[str, datetime]):
    scenario: str
    vertices: int

    def __getitem__(self, key: str) -> datetime:
        match = _RID.fullmatch(key)
        if match is None:
            raise KeyError(key)
        index = int(match.group(1))
        if index >= self.vertices:
            raise KeyError(key)
        if self.scenario == "repeated_cutoff":
            return _BASE + timedelta(seconds=index + 1)
        return _BASE

    def __iter__(self) -> Iterator[str]:
        # SQLitePitGraph only uses indexed lookups. This iterator is for Mapping conformance.
        for index in range(self.vertices):
            yield _revision_id(index)

    def __len__(self) -> int:
        return self.vertices


def _record(index: int, older: tuple[int, ...]) -> RevisionRecord:
    rid = _revision_id(index)
    times = ObservationTimes(
        event_time=_BASE - timedelta(days=60),
        available_time=_BASE,
        ingest_time=_BASE,
        knowledge_time=_BASE,
        declared_latency=timedelta(0),
    )
    decision = AvailabilityDecision(
        times=times,
        policy=AVAILABILITY_BINDING,
        evidence_gap="diagnostic synthetic availability",
    )
    return RevisionRecord(
        observation_key="diagnostic-observation",
        revision_id=rid,
        source_id="diagnostic-source",
        payload_hash=sha256(rid.encode("ascii")).hexdigest(),
        arrival_seq=index,
        supersedes=tuple(_revision_id(item) for item in older),
        availability=decision,
    )


def _records(scenario: str, vertices: int, fanout: int) -> Iterator[RevisionRecord]:
    for index in range(vertices):
        older: tuple[int, ...]
        if scenario in {"long_chain", "repeated_cutoff"}:
            older = () if index == 0 else (index - 1,)
        elif index < fanout:
            older = ()
        else:
            older = tuple(range(fanout))
        yield _record(index, older)


def _evidence(scenario: str, vertices: int, fanout: int) -> Iterator[PrecedenceEvidence]:
    for index in range(vertices):
        older: tuple[int, ...]
        if scenario in {"long_chain", "repeated_cutoff"}:
            older = () if index == 0 else (index - 1,)
        elif index < fanout:
            older = ()
        else:
            older = tuple(range(fanout))
        for predecessor in older:
            yield PrecedenceEvidence(
                observation_key="diagnostic-observation",
                revision_id=_revision_id(index),
                superseded_revision_id=_revision_id(predecessor),
                policy=PRECEDENCE_BINDING,
                evidence=("synthetic bounded PIT diagnostic edge",),
                knowledge_time=_BASE,
            )


class _MemorySampler:
    """Collect bounded periodic RSS/cgroup samples; unavailable sources are explicit."""

    def __init__(self, interval_seconds: float = 0.05) -> None:
        self.interval_seconds = interval_seconds
        self.samples: list[dict[str, int | str | None]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _read(path: Path) -> int | None:
        try:
            return int(path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None

    def sample(self, phase: str) -> None:
        if len(self.samples) >= 256:
            return
        rss_kib: int | None = None
        try:
            for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
                if line.startswith("VmRSS:"):
                    rss_kib = int(line.split()[1])
                    break
        except (OSError, ValueError, IndexError):
            pass
        cgroup_path: str | None = None
        current: int | None = None
        limit: int | str | None = None
        try:
            for line in Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines():
                if line.startswith("0::"):
                    cgroup_path = line[3:]
                    break
            if cgroup_path is not None:
                root = Path("/sys/fs/cgroup") / cgroup_path.lstrip("/")
                current = self._read(root / "memory.current")
                raw_limit = (root / "memory.max").read_text(encoding="ascii").strip()
                limit = "max" if raw_limit == "max" else int(raw_limit)
        except (OSError, ValueError):
            pass
        self.samples.append(
            {
                "phase": phase,
                "monotonic_ns": time.monotonic_ns(),
                "rss_kib": rss_kib,
                "cgroup_path": cgroup_path,
                "cgroup_memory_current_bytes": current,
                "cgroup_memory_max_bytes": limit,
            }
        )

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.sample("periodic")

    def __enter__(self) -> _MemorySampler:
        self.sample("start")
        self._thread = threading.Thread(target=self._run, name="pit-diag-memory", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self.sample("end")


def _scratch_filesystem(path: Path) -> dict[str, int | str | None]:
    stats = os.statvfs(path)
    result: dict[str, int | str | None] = {
        "available_bytes": stats.f_bavail * stats.f_frsize,
        "total_bytes": stats.f_blocks * stats.f_frsize,
        "mount": None,
        "filesystem_type": None,
    }
    try:
        target = str(path.resolve())
        candidates: list[tuple[int, str, str]] = []
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            left, right = line.split(" - ", 1)
            fields = left.split()
            mount = fields[4].replace("\\040", " ")
            if target == mount or target.startswith(mount.rstrip("/") + "/"):
                candidates.append((len(mount), mount, right.split()[0]))
        if candidates:
            _, result["mount"], result["filesystem_type"] = max(candidates)
    except (OSError, ValueError, IndexError):
        pass
    return result


def _table_count(db: sqlite3.Connection, table: str) -> int:
    if table not in {"revisions", "edges", "candidates", "visited", "eliminated"}:
        raise ValueError("unexpected SQLite diagnostic table")
    return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _pragma_settings(db: sqlite3.Connection) -> dict[str, int | str]:
    names = ("cache_size", "mmap_size", "temp_store", "journal_mode", "automatic_index")
    return {name: db.execute(f"PRAGMA {name}").fetchone()[0] for name in names}


def _statement_kind(sql: str) -> str:
    match = re.match(r"\s*(?:--[^\n]*\n\s*)*([A-Za-z]+)", sql)
    return match.group(1).upper() if match else "OTHER"


def _record_statement(counter: Counter[str], sql: str) -> None:
    counter.update((_statement_kind(sql),))


def _file_bytes(directory: Path) -> tuple[int, int]:
    db_path = directory / "graph.sqlite3"
    db_bytes = db_path.stat().st_size
    scratch_bytes = sum(path.stat().st_size for path in directory.iterdir() if path.is_file())
    return db_bytes, scratch_bytes


def _cutoffs(scenario: str, vertices: int, count: int) -> tuple[datetime, ...]:
    if scenario != "repeated_cutoff":
        return (_BASE + timedelta(days=1),)
    stride = max(1, vertices // count)
    indices = list(range(stride - 1, vertices, stride))[:count]
    if len(indices) < count:
        indices.append(vertices - 1)
    return tuple(_BASE + timedelta(seconds=index + 1) for index in indices[:count])


def run_diagnostic(
    scenario: str,
    *,
    vertices: int = 128,
    fanout: int = 8,
    cutoffs: int = 8,
    scratch_root: Path,
) -> dict[str, Any]:
    """Run one explicitly bounded graph shape and return JSON-serializable evidence."""
    if scenario not in {"long_chain", "wide_dag", "repeated_cutoff"}:
        raise ValueError("unknown PIT graph diagnostic scenario")
    if type(vertices) is not int or not 16 <= vertices <= _MAX_VERTICES:
        raise ValueError(f"vertices must be between 16 and {_MAX_VERTICES}")
    if type(fanout) is not int or not 2 <= fanout <= _MAX_FANOUT:
        raise ValueError(f"fanout must be between 2 and {_MAX_FANOUT}")
    if type(cutoffs) is not int or not 1 <= cutoffs <= _MAX_CUTOFFS:
        raise ValueError(f"cutoffs must be between 1 and {_MAX_CUTOFFS}")
    root = scratch_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    sql_counts: Counter[str] = Counter()
    samples: list[dict[str, int | str | None]]
    started = time.perf_counter_ns()
    phase_durations: dict[str, float] = {}
    with _MemorySampler() as memory:
        with SQLitePitGraph(root, cutoff=_BASE + timedelta(days=2)) as graph:
            graph.database.set_trace_callback(
                lambda sql: sql_counts.update((_statement_kind(sql),))
            )
            build_start = time.perf_counter_ns()
            graph.build(
                _records(scenario, vertices, fanout),
                _evidence(scenario, vertices, fanout),
                _Availability(scenario, vertices),
            )
            phase_durations["build_seconds"] = (time.perf_counter_ns() - build_start) / 1e9
            graph.database.set_trace_callback(None)
            memory.sample("after_build")
            actual_vertices = _table_count(graph.database, "revisions")
            actual_edges = _table_count(graph.database, "edges")
            observed: list[dict[str, Any]] = []
            for ordinal, instant in enumerate(_cutoffs(scenario, vertices, cutoffs)):
                counts: Counter[str] = Counter()
                graph.database.set_trace_callback(partial(_record_statement, counts))
                started_cutoff = time.perf_counter_ns()
                head_count, singleton = graph.head_summary(instant, emit=lambda *_: None)
                elapsed = (time.perf_counter_ns() - started_cutoff) / 1e9
                graph.database.set_trace_callback(None)
                candidates = _table_count(graph.database, "candidates")
                visited = _table_count(graph.database, "visited")
                eliminated = _table_count(graph.database, "eliminated")
                edge_rows = int(
                    graph.database.execute(
                        "SELECT COUNT(*) FROM edges AS e JOIN visited AS v ON v.revision_id=e.newer"
                    ).fetchone()[0]
                )
                # head_summary issues one indexed adjacency query for each candidate seed and
                # each visited node; this SQL-derived count describes their total output rows.
                seed_edge_rows = int(
                    graph.database.execute(
                        "SELECT COUNT(*) FROM edges AS e "
                        "JOIN candidates AS c ON c.revision_id=e.newer"
                    ).fetchone()[0]
                )
                observed.append(
                    {
                        "cutoff_ordinal": ordinal,
                        "cutoff_utc": instant.isoformat(),
                        "elapsed_seconds": elapsed,
                        "sql_statements": dict(sorted(counts.items())),
                        "candidate_nodes": candidates,
                        "visited_predecessor_nodes": visited,
                        "eliminated_candidate_nodes": eliminated,
                        "visited_adjacency_edge_rows": edge_rows,
                        "candidate_seed_edge_rows": seed_edge_rows,
                        "head_count": head_count,
                        "singleton_head": singleton,
                    }
                )
                memory.sample(f"after_cutoff_{ordinal}")
            graph.database.set_trace_callback(None)
            settings = _pragma_settings(graph.database)
            db_bytes, scratch_bytes = _file_bytes(graph.directory)
            directory = graph.directory
            memory.sample("before_close")
        samples = memory.samples
    phase_durations["total_seconds"] = (time.perf_counter_ns() - started) / 1e9
    return {
        "diagnostic": "sqlite_pit_graph_capacity@1",
        "acceptance_scope": "diagnostic_only_not_E1_CAP_1",
        "scenario": scenario,
        "input": {"vertices": vertices, "fanout": fanout, "cutoffs_requested": cutoffs},
        "graph": {"V": actual_vertices, "E": actual_edges, "K": len(observed)},
        "metric_semantics": {
            "visited_predecessor_nodes": (
                "rows in SQLite visited table; candidate seeds are excluded unless reachable "
                "as predecessors"
            ),
            "candidate_seed_edge_rows": (
                "edge rows returned by one adjacency lookup per candidate seed"
            ),
            "visited_adjacency_edge_rows": "edge rows attached to rows in SQLite visited table",
        },
        "build_sql_statements": dict(sorted(sql_counts.items())),
        "cutoffs": observed,
        "sqlite_settings": settings,
        "database_bytes": db_bytes,
        "scratch_directory_bytes": scratch_bytes,
        "scratch_filesystem": _scratch_filesystem(root),
        "elapsed": phase_durations,
        "memory_samples": samples,
        "scratch_directory_cleaned": not directory.exists(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vertices", type=int, default=128, help=f"16..{_MAX_VERTICES}")
    parser.add_argument("--fanout", type=int, default=8, help=f"2..{_MAX_FANOUT}")
    parser.add_argument("--cutoffs", type=int, default=8, help=f"1..{_MAX_CUTOFFS}")
    parser.add_argument(
        "--scratch", type=Path, required=True, help="explicit scratch filesystem path"
    )
    parser.add_argument(
        "--scenario", choices=("all", "long_chain", "wide_dag", "repeated_cutoff"), default="all"
    )
    args = parser.parse_args(argv)
    scenarios = (
        ("long_chain", "wide_dag", "repeated_cutoff")
        if args.scenario == "all"
        else (args.scenario,)
    )
    results = [
        run_diagnostic(
            scenario,
            vertices=args.vertices,
            fanout=args.fanout,
            cutoffs=args.cutoffs,
            scratch_root=args.scratch,
        )
        for scenario in scenarios
    ]
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

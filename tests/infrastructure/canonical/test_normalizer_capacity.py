"""E1-CAP-1 structural capacity tests: nothing the bounded path holds or reads grows with the unit.

Review 2026-09-27-e1 asks for structure, not a single maximum: every container the production
path holds — however deep (the normalizer's pins and caches, the verifier's archive cache and
its spooled parse, a survey's plan, the write's bookkeeping, the returned result) — and every
Arrow read must have the same size for a unit of 12 batches and one of 48, at one fixed
microbatch, narrow window and D2 row batch. Each container is compared **by path** (frame, local,
attribute / item chain), so one that grows with the batch count cannot hide behind a larger
fixed-size one, and every size must also stay under an explicit bound.

Covered operations: a fresh write, a crash after some batches and its resume, a replay, a reader
pinned like PIT proving the batch that holds a committed row (the row is read from the table, not
assumed), the verifier's strict proof of every window, and ``close``. ``collect_unit_rows`` is the
explicitly unbounded whole-unit form and is not traced; the Iceberg metadata PyIceberg loads for
each read (one snapshot and one manifest per commit) is the catalog's, measured by
``infrastructure/tools/normalizer_memory_probe.py``, not by these tests.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pytest

from infrastructure.canonical import normalizer as nz
from infrastructure.canonical.normalizer import CanonicalNormalizer
from infrastructure.catalog.phase1_tables import PHASE1_TABLES
from infrastructure.parser import archive_spool, binance_archive
from infrastructure.parser.archive_spool import SpooledArchive
from infrastructure.pit import view as pit_view
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision import row_integrity
from infrastructure.revision.row_integrity import PersistedRowVerifier
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import (
    Crash,
    ProxyCatalog,
    RestHarness,
    StepClock,
    utc,
)

K_ARCHIVE = utc(2024, 1, 2)
K_NORM = utc(2024, 1, 3)
#: One fixed configuration for every unit size: microbatch, narrow window, D2 row batch.
CHUNK = 4
NARROW = 8
D2_BATCH = 4
#: Unit sizes: 6 and 24 Canonical batches (multiples of every window, so last windows match).
#: A per-batch container then reaches 24 > ``CACHE_MAX``, a per-row one 96 > ``BOUND``.
SMALL, LARGE = 24, 96
#: Widest row any Phase 1 table has: a row dict's own size (a column count, not a row count).
WIDEST_ROW = max(len(definition.arrow_schema) for definition in PHASE1_TABLES)
#: The largest fixed cache on the path (the verifier's proven row batches / checkpoints).
CACHE_MAX = 16
#: Anything the bounded path holds: a window of rows, a row's columns, a fixed cache.
BOUND = max(CHUNK, NARROW, D2_BATCH, WIDEST_ROW, CACHE_MAX) + 1

_TRACED = {
    module.__file__ for module in (nz, row_integrity, archive_spool, binance_archive, pit_view)
}


# =========================================================================================
# deep container audit
# =========================================================================================


def _size(value: Any) -> int | None:
    """Cardinality of a container (``None``: not a container). A ``range`` is O(1) whatever
    its length and text is a value, so neither counts."""
    if isinstance(value, pa.Table | pa.RecordBatch):
        return int(value.num_rows)
    if isinstance(value, pa.Array | pa.ChunkedArray):
        return len(value)
    if isinstance(value, list | tuple | set | frozenset | dict):
        return len(value)
    if isinstance(value, Mapping):
        return len(value)
    return None


def _ours(value: Any) -> bool:
    """Objects of this code base's infrastructure (their fields are audited too)."""
    return type(value).__module__.startswith("infrastructure.")


@dataclass
class _Audit:
    """Largest cardinality seen per container path, at every return (and ``yield``) of a traced
    frame: a container accumulating across a loop stays bound until its frame returns, and one
    kept on an object is reachable from every later frame's ``self`` / arguments."""

    largest: dict[str, int] = field(default_factory=dict)
    arrow_reads: list[tuple[str, int]] = field(default_factory=list)

    def walk(self, value: Any, path: str, seen: set[int], depth: int = 0) -> None:
        if depth > 8 or id(value) in seen:
            return
        size = _size(value)
        if size is not None:
            seen.add(id(value))
            if size > self.largest.get(path, -1):
                self.largest[path] = size
            items: Any = value.values() if isinstance(value, Mapping) else value
            if not isinstance(value, pa.Table | pa.RecordBatch | pa.Array | pa.ChunkedArray):
                for item in items:
                    self.walk(item, f"{path}[]", seen, depth + 1)
            return
        if not _ours(value):
            return
        seen.add(id(value))
        names: list[str] = []
        if is_dataclass(value):
            names = [item.name for item in fields(value)]
        elif hasattr(value, "__dict__"):
            names = list(vars(value))
        for slots in (getattr(cls, "__slots__", ()) for cls in type(value).__mro__):
            names.extend([slots] if isinstance(slots, str) else slots)
        for name in dict.fromkeys(names):
            if hasattr(value, name):
                self.walk(getattr(value, name), f"{path}.{name}", seen, depth + 1)

    def frame(self, frame: Any, returned: Any = None) -> None:
        where = f"{frame.f_code.co_name}"
        seen: set[int] = set()
        for name, value in list(frame.f_locals.items()):
            self.walk(value, f"{where}:{name}", seen)
        if returned is not None:
            self.walk(returned, f"{where}:<return>", seen)

    def tracer(self, frame: Any, event: str, arg: Any) -> Any:
        if frame.f_code.co_filename not in _TRACED:
            return None

        def local(frame: Any, event: str, arg: Any) -> Any:
            if event == "return":
                self.frame(frame, arg)
            return local

        return local

    def __enter__(self) -> _Audit:
        self._previous = sys.gettrace()
        sys.settrace(self.tracer)
        return self

    def __exit__(self, *exc: object) -> None:
        sys.settrace(self._previous)


@dataclass
class _ReadLog(ProxyCatalog):
    """Every scan of every table, however narrow: ``(table, rows returned)``."""

    reads: list[tuple[str, int]] = field(default_factory=list)

    def scan_columns(self, table: str, **kwargs: Any) -> Any:
        result = self.inner.scan_columns(table, **kwargs)
        self.reads.append((table, result.num_rows))
        return result


# =========================================================================================
# the traced operations
# =========================================================================================


def _archive(h: RestHarness, rows: int, request_id: str, first_ms: int) -> str:
    return c.ingest_archive(
        h,
        "agg_trades",
        ss.archive_agg_lines(ss.agg_items(rows, first_id=1 + first_ms // 1_000, first_ms=first_ms)),
        knowledge=K_ARCHIVE,
        request_id=request_id,
        store_microbatch_rows=D2_BATCH,
    )


def _normalizer(adapter: Any, h: RestHarness, spools: Path, **kwargs: Any) -> CanonicalNormalizer:
    return CanonicalNormalizer(
        adapter,
        h.storage,
        microbatch_rows=CHUNK,
        narrow_rows=NARROW,
        spool_dir=spools,
        **kwargs,
    )


def _committed_seq(h: RestHarness, unit: str) -> int:
    """A committed row's ``arrival_seq`` of the unit, read from the table (middle by rank)."""
    seqs: list[int] = sorted(
        row["arrival_seq"] for row in h.rows(c.TRADES) if row["lineage_source_revision_id"] == unit
    )
    return seqs[len(seqs) // 2]


def _operations(h: RestHarness, unit: str, spools: Path) -> dict[str, Callable[[], Any]]:
    """Each bounded operation over ``unit``; ``MAIN`` in order leaves it fully normalized."""
    log = _ReadLog(h.adapter)
    crash = ProxyCatalog(
        log,  # type: ignore[arg-type]
        after=ss.crash_after_commits(5, table=c.TRADES.table),
    )

    def crash_mid_write() -> None:
        with pytest.raises(Crash):
            _normalizer(crash, h, spools, clock=StepClock(start=K_NORM)).normalize_unit(
                c.ARCHIVE_AGGS.table, unit
            )

    def resume() -> Any:
        out = _normalizer(log, h, spools, clock=StepClock(start=K_NORM)).normalize_unit(
            c.ARCHIVE_AGGS.table, unit
        )
        assert out.committed_batches == out.batch_count - 5 and out.snapshot_id
        return out

    def write() -> Any:
        out = _normalizer(log, h, spools, clock=StepClock(start=K_NORM)).normalize_unit(
            c.ARCHIVE_AGGS.table, unit
        )
        assert out.committed_batches == out.batch_count and out.snapshot_id
        return out

    def replay() -> Any:
        out = _normalizer(log, h, spools, clock=StepClock(start=K_NORM)).normalize_unit(
            c.ARCHIVE_AGGS.table, unit
        )
        assert out.replayed
        return out

    def pinned_read() -> Any:
        heads = {d.table: h.head(d.table) for d in (c.ARCHIVE_AGGS, c.ARCHIVES, c.TRADES)}
        view = PinnedCatalogView(log, {table: head for table, head in heads.items() if head})
        seq = _committed_seq(h, unit)
        with _normalizer(view, h, spools) as reader:
            rows = reader.verify_unit(c.ARCHIVE_AGGS.table, unit, arrival_seqs={seq})
            assert seq in {row["arrival_seq"] for row in rows} and len(rows) == CHUNK
            # The same reader again (PIT reads many slices of one unit): caches stay bounded.
            reader.verify_unit(c.ARCHIVE_AGGS.table, unit, arrival_seqs={seq + CHUNK})
        return rows

    def verifier_proof() -> None:
        raw = sorted(h.rows(c.ARCHIVE_AGGS), key=lambda row: row["archive_line_number"])
        raw = [row for row in raw if row["archive_revision_id"] == unit]
        with PersistedRowVerifier(
            log, h.storage, cache_archives=True, spool_dir=spools, spool_rows=CHUNK
        ) as verifier:
            for start in range(0, len(raw), CHUNK):
                verifier.verify_archive_elements(
                    c.ARCHIVE_AGGS, "agg_trades", ss.SYMBOL, raw[start : start + CHUNK]
                )

    h.__dict__["_capacity_reads"] = log.reads
    return {
        "crash": crash_mid_write,
        "resume": resume,
        "write": write,
        "replay": replay,
        "pinned-read": pinned_read,
        "verifier": verifier_proof,
    }


#: Crash mid-write, resume, replay, a pinned reader, the verifier's proof of every window.
MAIN = ("crash", "resume", "replay", "pinned-read", "verifier")


@dataclass
class _Run:
    largest: dict[str, int]
    reads: list[tuple[str, int]]
    blocks: list[int]


@pytest.fixture
def blocks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Rows of every spooled block read, in order."""
    read: list[int] = []
    original = SpooledArchive._chunk

    def logged(self: SpooledArchive, chunk: int) -> Any:
        table = original(self, chunk)
        read.append(table.num_rows)
        return table

    monkeypatch.setattr(SpooledArchive, "_chunk", logged)
    return read


def _run(
    h: RestHarness,
    rows: int,
    request_id: str,
    first_ms: int,
    spools: Path,
    blocks: list[int],
    ops: tuple[str, ...] = MAIN,
) -> _Run:
    unit = _archive(h, rows, request_id, first_ms)
    operations = _operations(h, unit, spools)
    before = len(blocks)
    audit = _Audit()
    for name in ops:
        with audit:
            operations[name]()
    reads: list[tuple[str, int]] = h.__dict__.pop("_capacity_reads")
    return _Run(audit.largest, reads, blocks[before:])


def _grown(small: _Run, large: _Run) -> dict[str, tuple[int | None, int]]:
    """Paths beyond the fixed bound, or larger for the larger unit — unless the path is a fixed
    cache not yet full for the smaller one (then both stay within ``CACHE_MAX``)."""
    return {
        path: (small.largest.get(path), size)
        for path, size in large.largest.items()
        if size > BOUND or (size > small.largest.get(path, 0) and size > CACHE_MAX)
    }


# =========================================================================================
# the tests
# =========================================================================================


def test_nothing_the_bounded_path_holds_or_reads_grows_with_the_unit(
    h: RestHarness, tmp_path: Path, blocks: list[int]
) -> None:
    spools = tmp_path / "spools"
    spools.mkdir()
    small = _run(h, SMALL, "small", ss.T0, spools, blocks)
    large = _run(h, LARGE, "large", ss.T0 + 3_600_000, spools, blocks)
    # The audit sees the path's real collections: a window of rows, the verifier's caches…
    assert max(small.largest.values()) >= CHUNK
    assert any(path.startswith("_raw_window:rows") for path in small.largest)
    assert any("._archives" in path for path in small.largest)
    # …and no path is beyond the fixed bound, nor larger for 48 batches than for 12.
    grown = _grown(small, large)
    assert grown == {}, grown
    assert max(large.largest.values()) <= BOUND, max(large.largest.items(), key=lambda i: i[1])
    # Every Arrow read of every table and every spool block: one window (+ 1 row), for both.
    for run in (small, large):
        assert run.reads and max(rows for _, rows in run.reads) <= max(CHUNK, NARROW, D2_BATCH) + 1
        assert run.blocks and max(run.blocks) <= CHUNK
    # Nothing is left on disk once the verifiers and readers are closed.
    assert not any(spools.iterdir())


def test_the_audit_catches_a_collection_that_grows_with_the_batches(
    h: RestHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocks: list[int]
) -> None:
    """Mutation check of the audit itself: a per-batch list kept by the normalizer (what the
    ``done`` / ``commits`` bookkeeping was) is found, deep in the instance, by path."""
    spools = tmp_path / "spools"
    spools.mkdir()
    original = CanonicalNormalizer._plan_snapshots

    def keeping(self: CanonicalNormalizer, *args: Any, **kwargs: Any) -> Any:
        kept = self.__dict__.setdefault("_kept", {"all": []})
        for item in original(self, *args, **kwargs):
            kept["all"].append(item)
            yield item

    monkeypatch.setattr(CanonicalNormalizer, "_plan_snapshots", keeping)
    small = _run(h, SMALL, "small", ss.T0, spools, blocks, ("write", "replay"))
    large = _run(h, LARGE, "large", ss.T0 + 3_600_000, spools, blocks, ("write", "replay"))
    flagged = _grown(small, large)
    assert any(path.endswith("self._kept[]") for path in flagged), flagged
    assert all(path.endswith("self._kept[]") or "._kept[]" in path for path in flagged), flagged

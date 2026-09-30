"""Canonical normalizer (Phase 1 E1; ADR-0023 §3 / §4 / §7, ADR-0028 §1 ~ §6).

One call normalizes one **unit**: all Raw element revisions of one Raw source revision (an archive
revision, or a REST response revision's own elements) of one data type, under normalizer
``hlens.canonical.binance-spot.normalizer@1.0.0``. Memory is bounded by the microbatch, not by
the unit (G3-S): a unit is read, proven and written in windows of Raw positions.

1. **pin** — the Raw element, Raw source and Canonical table heads are read twice and must agree;
   every later read of the call time-travels to exactly those snapshots (``PinnedCatalogView``),
   so no verdict is ever drawn from a moving table;
2. **prove** (no clock, no write) — window by window, every Raw row of the unit is proven by the
   shared ``PersistedRowVerifier`` (D3E-R2 / R3); the unit's Raw positions must then be distinct
   and, for an archive, exactly the ``1 … N`` lines of its object; a REST response may lack only the
   elements of its body that another committed page delivered first (G2-R1a / RT-3: any other
   missing element is a store that stopped half-way, ``CanonicalUnitIncomplete``, and nothing is
   written; ``check_rest_page``, which the E3 report shares: G2-R3a). Batch ``i`` is the ``i``-th
   rank slice of ``chunk`` positions. The committed batch ids are the plan (E1-R3): unit size ``N``
   and microbatch size; committed batches must be a contiguous prefix of it, committed once each
   and in order (the only history a writer produces), each holding exactly the rows its window
   normalizes to (content, fingerprint, row count) under the block base and ready time recovered
   from them, and the unit's committed rows must be exactly those batches' rows;
3. **write** — only if nothing of the unit is committed: a fresh block above the table's largest
   ``arrival_seq`` and **one** reading of the injected UTC clock, never before a Raw
   ``knowledge_time`` (refused, not raised). Each missing batch
   ``<normalizer>@<version>.<source revision>.<unit rows>.<chunk>.<index>`` is re-read from the
   pinned Raw snapshot, normalized, committed with the expected parent and read back at its own
   snapshot (exact rows, each once; revision ids unique in the rows' own event-time range);
4. **close** — at the final snapshot the unit's rows are exactly ``base + position`` of its Raw
   rows and nothing else sits in its block.

A unit whose Raw rows changed after it was first normalized no longer matches its committed plan
and fails closed: normalize a Raw unit only after its ingest returned. Readers (``verify_unit``)
take a normalized unit only whole: a committed prefix of its plan is ``CanonicalUnitIncomplete``
(G2-R1a / RT-1), resolved by rerunning ``normalize_unit``. Nothing is repaired or
rewritten; there is no journal or sidecar. The normalizer never reads the Raw evidence table:
cross-channel edges are mapped at PIT time (ADR-0028 §3.2).

The committed plan is kept as three ints — unit size, microbatch size, committed batches
(E1-CAP-1): a batch's snapshot is streamed from the pinned history only when that batch is
proven or reported as already committed, and never kept per batch.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import struct
import tempfile
from bisect import bisect_left
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast, overload

from pyiceberg.expressions import (
    And,
    BooleanExpression,
    EqualTo,
    GreaterThanOrEqual,
    In,
    LessThan,
    LessThanOrEqual,
)

from core.contracts.catalog import (
    BatchConflict,
    CommitConflict,
    CommitOutcome,
    CommitRequest,
    SnapshotInfo,
    TableNotFound,
)
from core.contracts.storage import StorageAdapter
from infrastructure import contract_version
from infrastructure.canonical import rules
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision.row_integrity import (
    PageElement,
    PersistedRowVerifier,
    batch,
    batch_rows,
    check_batch_snapshot,
    history_from,
)
from infrastructure.revision.store import RevisionCatalog
from infrastructure.streaming.runs import RunLimits, RunRef, RunSetBuilder, iter_run

__all__ = [
    "DEFAULT_MICROBATCH_ROWS",
    "MAX_MICROBATCH_ROWS",
    "CanonicalNormalizeConflict",
    "CanonicalNormalizeError",
    "CanonicalNormalizer",
    "CanonicalUnitIncomplete",
    "CanonicalUnitNormalized",
    "check_rest_page",
    "unit_batch_id",
]

DEFAULT_MICROBATCH_ROWS: Final = 25_000
MAX_MICROBATCH_ROWS: Final = 250_000
_ATTEMPTS: Final = 8
#: Observation keys per ``IN`` lookup of the elements another page delivered first.
_KEY_CHUNK: Final = 256
_ZERO: Final = timedelta(0)
_ROWS_DIGITS: Final = 10
_CHUNK_DIGITS: Final = 6
_INDEX_DIGITS: Final = 8
#: Units whose unit-wide facts an immutable-view normalizer keeps (G3-S3).
_FACT_CACHE: Final = 2
#: Proven committed batches an immutable-view normalizer keeps (each <= one microbatch).
_BATCH_CACHE: Final = 2
_POSITION_DB_CACHE_KIB: Final = 1024
_POSITION_INSERT_ROWS: Final = 2048
_POSITION_INT: Final = struct.Struct(">q")


def _prepare_scratch_directory(directory: Path) -> Path:
    """Create and prove the configured persistent scratch root writable before catalog access."""
    if not isinstance(directory, Path) or not directory.is_absolute():
        raise CanonicalNormalizeError("canonical scratch directory must be an absolute Path")
    try:
        root = directory.resolve(strict=False)
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise OSError("path is not a directory")
        probe = root / f".hlens-scratch-check-{os.getpid()}-{os.urandom(8).hex()}"
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.close(fd)
        finally:
            probe.unlink(missing_ok=True)
        return root
    except (OSError, RuntimeError) as exc:
        raise CanonicalNormalizeError(
            f"canonical scratch directory is not usable: {directory}"
        ) from exc


@contextmanager
def _scan_rows(
    catalog: Any,
    table: str,
    *,
    columns: Sequence[str],
    row_filter: BooleanExpression,
    snapshot_id: str | None = None,
) -> Iterator[Iterator[Mapping[str, Any]]]:
    """Yield row mappings from bounded catalog batches and always close the reader."""
    reader = catalog.scan_column_batches(
        table, columns=columns, row_filter=row_filter, snapshot_id=snapshot_id
    )

    def rows() -> Iterator[Mapping[str, Any]]:
        for record_batch in reader:
            yield from record_batch.to_pylist()

    try:
        yield rows()
    except BaseException:
        # Keep the scan/validation failure that caused cleanup; reader.close() is allowed to
        # fail while unwinding too, but must not replace the original integrity/storage error.
        close = getattr(reader, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        raise
    else:
        close = getattr(reader, "close", None)
        if callable(close):
            close()


class CanonicalNormalizeError(Exception):
    """Base class of normalizer failures that must not be papered over."""


class CanonicalNormalizeConflict(CanonicalNormalizeError):
    """The unit cannot be normalized honestly now (clock, contention); nothing is committed."""


class CanonicalUnitIncomplete(CanonicalNormalizeError, CatalogIntegrityError):
    """A unit is not complete at the pinned snapshots: fail closed, nothing written (G2-R1a).

    Two states, neither of which a reader or the normalizer may take for a whole unit:

    - its **Canonical** plan is only partly committed (the normalizer stopped between batches):
      readers refuse it until a rerun commits the missing batches (RT-1);
    - its **Raw** REST response lacks an element its body holds that no other committed page
      delivered first (the store stopped between element batches): the normalizer refuses it,
      before any clock reading or commit, until the store's rerun completes the page (RT-3).

    It is a ``CatalogIntegrityError`` so every fail-closed caller refuses it; the distinct type
    tells a caller that rerunning the unfinished writer, not repair, is the way out.
    """


class _PositionIndex(Sequence[int]):
    """A sorted, disk-backed position sequence with bounded in-memory SQLite state."""

    def __init__(self, scratch_directory: Path) -> None:
        self._temporary = tempfile.TemporaryDirectory(
            prefix="hlens-positions-", dir=str(scratch_directory)
        )
        self._database_path = os.path.join(self._temporary.name, "positions.sqlite3")
        self._rank_path = os.path.join(self._temporary.name, "ranks.bin")
        try:
            self._connection = sqlite3.connect(self._database_path)
            self._connection.execute(f"PRAGMA cache_size = -{_POSITION_DB_CACHE_KIB}")
            self._connection.execute("PRAGMA temp_store = FILE")
            self._connection.execute("PRAGMA journal_mode = OFF")
            self._connection.execute("PRAGMA synchronous = OFF")
            self._connection.execute("CREATE TABLE positions (position INTEGER NOT NULL)")
            self._connection.execute("CREATE INDEX positions_position ON positions(position)")
        except BaseException:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            self._temporary.cleanup()
            raise
        self._size = 0
        self._fd: int | None = None
        self._closed = False

    def add_batch(self, positions: Iterable[int]) -> None:
        if self._closed or self._fd is not None:
            raise RuntimeError("position index is finalized")
        self._connection.executemany(
            "INSERT INTO positions(position) VALUES (?)", ((value,) for value in positions)
        )

    def finalize(self) -> None:
        if self._closed or self._fd is not None:
            raise RuntimeError("position index is finalized")
        self._connection.commit()
        try:
            with open(self._rank_path, "wb") as ranks:
                cursor = self._connection.execute(
                    "SELECT position FROM positions ORDER BY position"
                )
                for (position,) in cursor:
                    ranks.write(_POSITION_INT.pack(position))
                    self._size += 1
            self._connection.close()
            self._fd = os.open(self._rank_path, os.O_RDONLY)
        except Exception:
            self.close()
            raise

    def __len__(self) -> int:
        return self._size

    @overload
    def __getitem__(self, rank: int) -> int: ...

    @overload
    def __getitem__(self, rank: slice) -> Sequence[int]: ...

    def __getitem__(self, rank: int | slice) -> int | Sequence[int]:
        if isinstance(rank, slice):
            start, stop, step = rank.indices(self._size)
            return _PositionSlice(self, range(start, stop, step))
        if rank < 0:
            rank += self._size
        if not 0 <= rank < self._size:
            raise IndexError("position rank out of range")
        if self._fd is None:
            raise RuntimeError("position index is closed or not finalized")
        value = os.pread(self._fd, _POSITION_INT.size, rank * _POSITION_INT.size)
        if len(value) != _POSITION_INT.size:
            raise OSError("position rank file ended unexpectedly")
        return cast(int, _POSITION_INT.unpack(value)[0])

    def __iter__(self) -> Iterator[int]:
        yield from self._iter_ranks(range(self._size))

    def _iter_ranks(self, selection: range) -> Iterator[int]:
        if self._fd is None:
            raise RuntimeError("position index is closed or not finalized")
        if not selection:
            return
        if selection.step != 1:
            for rank in selection:
                yield self[rank]
            return
        with open(self._rank_path, "rb") as rank_file:
            rank_file.seek(selection.start * _POSITION_INT.size)
            remaining = len(selection)
            chunk_size = 64 * 1024 - (64 * 1024 % _POSITION_INT.size)
            while remaining:
                count = min(remaining, chunk_size // _POSITION_INT.size)
                data = rank_file.read(count * _POSITION_INT.size)
                if len(data) != count * _POSITION_INT.size:
                    raise OSError("position rank file ended unexpectedly")
                yield from (value for (value,) in _POSITION_INT.iter_unpack(data))
                remaining -= count

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        fd, self._fd = self._fd, None
        try:
            if fd is not None:
                os.close(fd)
        finally:
            try:
                self._connection.close()
            except sqlite3.ProgrammingError:
                pass
            finally:
                self._temporary.cleanup()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class _PositionSlice(Sequence[int]):
    """A lazy sequence slice that never copies a position range into memory."""

    def __init__(self, positions: _PositionIndex, ranks: range) -> None:
        self._positions = positions
        self._ranks = ranks

    def __len__(self) -> int:
        return len(self._ranks)

    @overload
    def __getitem__(self, rank: int) -> int: ...

    @overload
    def __getitem__(self, rank: slice) -> Sequence[int]: ...

    def __getitem__(self, rank: int | slice) -> int | Sequence[int]:
        if isinstance(rank, slice):
            start, stop, step = rank.indices(len(self))
            return _PositionSlice(self._positions, self._ranks[start:stop:step])
        if rank < 0:
            rank += len(self)
        if not 0 <= rank < len(self):
            raise IndexError("position slice rank out of range")
        return self._positions[self._ranks[rank]]

    def __iter__(self) -> Iterator[int]:
        yield from self._positions._iter_ranks(self._ranks)


class _OffsetSequence(Sequence[int]):
    """A lazy integer offset view used for exact committed-row comparisons."""

    def __init__(self, values: Sequence[int], offset: int) -> None:
        self._values = values
        self._offset = offset

    def __len__(self) -> int:
        return len(self._values)

    @overload
    def __getitem__(self, rank: int) -> int: ...

    @overload
    def __getitem__(self, rank: slice) -> Sequence[int]: ...

    def __getitem__(self, rank: int | slice) -> int | Sequence[int]:
        if isinstance(rank, slice):
            return _OffsetSequence(self._values[rank], self._offset)
        return self._values[rank] + self._offset

    def __iter__(self) -> Iterator[int]:
        for value in self._values:
            yield value + self._offset


class _OffsetMembership(Collection[int]):
    """Membership view for REST element indices backed by one-based Raw positions."""

    def __init__(self, positions: Sequence[int], offset: int) -> None:
        self._positions = positions
        self._offset = offset

    def __contains__(self, value: object) -> bool:
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        rank = bisect_left(self._positions, value + self._offset)
        return rank < len(self._positions) and self._positions[rank] == value + self._offset

    def __iter__(self) -> Iterator[int]:
        for position in self._positions:
            yield position - self._offset

    def __len__(self) -> int:
        return len(self._positions)


def _equals(column: str, value: object) -> BooleanExpression:
    return EqualTo(column, value)  # type: ignore[call-arg, arg-type]


def _between(column: str, low: object, high: object) -> BooleanExpression:
    """``low <= column <= high``."""
    return And(
        GreaterThanOrEqual(column, low),  # type: ignore[call-arg, arg-type]
        LessThanOrEqual(column, high),  # type: ignore[call-arg, arg-type]
    )


def _from(column: str, low: object, below: object) -> BooleanExpression:
    """``low <= column < below``."""
    return And(
        GreaterThanOrEqual(column, low),  # type: ignore[call-arg, arg-type]
        LessThan(column, below),  # type: ignore[call-arg, arg-type]
    )


def unit_batch_id(source_revision_id: str, unit_rows: int, chunk: int, index: int) -> str:
    """Stable id of the ``index``-th Canonical microbatch of one unit's plan.

    The plan — unit size and microbatch size — is part of every batch id (E1-R1, E1-R3): a
    crash-recovery re-plan reads it back from the committed ids and reproduces them whatever the
    restarted process is configured with, while a Raw unit that changed after it was normalized
    no longer matches its committed plan and fails closed.
    """
    return (
        f"{_unit_prefix(source_revision_id)}{unit_rows:0{_ROWS_DIGITS}d}.{chunk:0{_CHUNK_DIGITS}d}"
        f".{index:0{_INDEX_DIGITS}d}"
    )


def _unit_prefix(source_revision_id: str) -> str:
    return f"{rules.NORMALIZER_ID}@{rules.NORMALIZER_VERSION}.{source_revision_id}."


@dataclass(frozen=True, slots=True)
class _CommittedPlan:
    """What the unit's committed batch ids say about the plan that wrote them.

    Three ints (E1-CAP-1): unit size, microbatch size and how many batches are committed. The
    committed batches are ``0 … count - 1``, each committed once and in order (proven by
    ``_committed_plan`` / ``_check_plan``); a batch's snapshot is located in the pinned history
    only when that batch is proven or reported (``_plan_snapshots``), never kept per batch.
    """

    unit_rows: int
    chunk: int
    count: int


@dataclass(slots=True)
class _CommittedTimes:
    """Bounded scan summary for one unit's persisted integer/time/version columns."""

    seqs: _PositionIndex
    seq_count: int
    seq_null: bool
    low: int | None
    high: int | None
    ready: datetime | None
    ready_multiple_or_null: bool
    versions: tuple[object, ...]

    def close(self) -> None:
        self.seqs.close()


@dataclass(frozen=True, slots=True)
class CanonicalUnitNormalized:
    """Bounded summary of normalizing one unit.

    Use ``CanonicalNormalizer.iter_revision_ids`` when the full ordered ID stream is needed.
    """

    raw_table: str
    source_revision_id: str
    canonical_table: str
    #: ``None`` for a unit without element revisions (e.g. an empty REST page).
    arrival_seq_base: int | None
    knowledge_time: datetime | None
    revision_count: int
    batch_count: int
    replayed_batch_count: int

    @property
    def replayed(self) -> bool:
        return self.replayed_batch_count == self.batch_count


@dataclass(frozen=True, slots=True)
class _Pin:
    """One call's fixed view: every read goes through ``catalog``."""

    catalog: PinnedCatalogView
    canonical_head: str | None
    verifier: PersistedRowVerifier


@dataclass(frozen=True, slots=True)
class _UnitFacts:
    """A unit's proven unit-wide facts (no Raw or Canonical rows)."""

    positions: _PositionIndex
    plan: _CommittedPlan | None
    base: int | None
    ready: datetime | None
    #: The contract version the unit's committed rows record (ADR-0052 versioned replay, V1).
    version: str | None = None


@dataclass(frozen=True, slots=True)
class _Survey:
    """What the proving pass established about the unit at the pinned view."""

    unit_rows: int
    #: Latest Raw ``knowledge_time`` of the unit (``None`` for an empty unit).
    raw_floor: datetime | None
    plan: _CommittedPlan | None
    #: Recovered from the committed rows when ``plan`` is set.
    base: int | None
    ready: datetime | None
    #: The committed rows, window by window (only collected when asked for).
    committed_rows: tuple[Mapping[str, Any], ...]
    #: The unit's Raw positions, ascending and distinct (batches are rank slices of them).
    positions: _PositionIndex | None = None
    #: Recovered from the committed rows when ``plan`` is set: the unit is rebuilt, and
    #: completed, at this contract version (ADR-0052 versioned replay, V1 / V2).
    version: str | None = None
    #: When ``plan`` is set: digest of the committed batches' snapshots as the proving pass
    #: checked them, newest first (``_fold``); the write re-locates exactly these (E1-CAP-1).
    committed_digest: bytes | None = None


class CanonicalNormalizer:
    """Appends Canonical revisions for verified Raw units; idempotent and re-runnable.

    One writer per Canonical table (ADR-0023 §7); concurrent writers are tolerated: a lost
    expected-parent race restarts the unit from a fresh pin, so a unit another writer finished
    is adopted (its base and ready time), never written twice.
    """

    def __init__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        *,
        scratch_directory: Path,
        clock: Callable[[], datetime] | None = None,
        microbatch_rows: int = DEFAULT_MICROBATCH_ROWS,
    ) -> None:
        if not isinstance(microbatch_rows, int) or isinstance(microbatch_rows, bool):
            raise CanonicalNormalizeError("microbatch_rows must be an int")
        if not 1 <= microbatch_rows <= MAX_MICROBATCH_ROWS:
            raise CanonicalNormalizeError(
                f"microbatch_rows must be between 1 and {MAX_MICROBATCH_ROWS}"
            )
        self._adapter = adapter
        self._storage = storage
        self._scratch_directory = _prepare_scratch_directory(scratch_directory)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._microbatch = microbatch_rows
        #: On a ``PinnedCatalogView`` nothing can change under the normalizer (it cannot write
        #: there either), so its pins — with their archive caches — and each unit's unit-wide
        #: facts are proven once and reused by every later call (G3-S3: a reader of many time
        #: slices of one unit).
        self._frozen = isinstance(adapter, PinnedCatalogView)
        self._pins: dict[tuple[str, ...], _Pin] = {}
        self._facts: dict[tuple[str, str], _UnitFacts] = {}
        self._batches: dict[tuple[str, str, int], tuple[Mapping[str, Any], ...]] = {}

    def close(self) -> None:
        """Release temporary rank files retained by immutable-view unit facts."""
        for facts in self._facts.values():
            facts.positions.close()
        self._facts.clear()
        self._batches.clear()
        self._pins.clear()

    def __enter__(self) -> CanonicalNormalizer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------ entry points

    def normalize_unit(self, raw_table: str, source_revision_id: str) -> CanonicalUnitNormalized:
        channel = self._channel(raw_table, source_revision_id)
        last_error: Exception | None = None
        for _ in range(_ATTEMPTS):
            pin = self._pin(channel, source_revision_id)
            survey = self._survey(pin, channel, source_revision_id, keep_rows=False)
            try:
                if survey.unit_rows == 0:
                    return CanonicalUnitNormalized(
                        raw_table=raw_table,
                        source_revision_id=source_revision_id,
                        canonical_table=channel.canonical.table,
                        arrival_seq_base=None,
                        knowledge_time=None,
                        revision_count=0,
                        batch_count=0,
                        replayed_batch_count=0,
                    )
                if survey.plan is None:
                    assert survey.raw_floor is not None
                    base, ready = self._allocate(channel, survey.raw_floor)
                    chunk = self._microbatch
                    # A unit with nothing committed is a new write group: the current version (V2).
                    version = contract_version.new_group_version()
                else:
                    assert survey.base is not None and survey.ready is not None
                    assert survey.version is not None
                    base, ready, chunk = survey.base, survey.ready, survey.plan.chunk
                    version = survey.version  # completed at its recorded version, never mixed
                try:
                    batch_count, revision_count, replayed_batch_count = self._write(
                        pin, channel, source_revision_id, survey, (base, ready, version), chunk
                    )
                except CommitConflict as exc:
                    last_error = exc  # another writer moved the table: start over, adopt its work
                    continue
                except BatchConflict as exc:
                    if survey.plan is None:
                        # A rival allocated first (its own block and clock reading) and committed
                        # this batch id: start over and adopt its plan — the clock is never read
                        # again.
                        last_error = exc
                        continue
                    # Our plan was recovered from committed batches, which every writer recovers
                    # identically: another content under the same id is corruption, not contention.
                    raise CatalogIntegrityError(
                        f"a Canonical batch of unit {source_revision_id} is committed with other "
                        f"content: {exc}"
                    ) from None
                return CanonicalUnitNormalized(
                    raw_table=raw_table,
                    source_revision_id=source_revision_id,
                    canonical_table=channel.canonical.table,
                    arrival_seq_base=base,
                    knowledge_time=ready,
                    revision_count=revision_count,
                    batch_count=batch_count,
                    replayed_batch_count=replayed_batch_count,
                )
            finally:
                if survey.positions is not None:
                    survey.positions.close()
        raise CanonicalNormalizeConflict(
            f"unit {source_revision_id} of {raw_table} lost {_ATTEMPTS} commit races"
        ) from last_error

    def iter_revision_ids(self, result: CanonicalUnitNormalized) -> Iterator[str]:
        """Re-prove and stream one unit's Canonical revision IDs in Raw position order.

        The default result deliberately contains no O(N) ID collection. This explicit iterator
        pins a consistent current view, revalidates the normalized unit, and yields one bounded
        microbatch at a time. Close it early to release the survey's disk-backed position index.
        """
        if type(result) is not CanonicalUnitNormalized:
            raise CanonicalNormalizeError("result must be a CanonicalUnitNormalized")
        if (
            any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in (
                    result.revision_count,
                    result.batch_count,
                    result.replayed_batch_count,
                )
            )
            or result.replayed_batch_count > result.batch_count
        ):
            raise CanonicalNormalizeError("result summary counts are invalid")
        channel = self._channel(result.raw_table, result.source_revision_id)
        if result.canonical_table != channel.canonical.table:
            raise CanonicalNormalizeError("result canonical_table does not match its raw_table")
        if result.revision_count == 0:
            if (
                result.batch_count != 0
                or result.replayed_batch_count != 0
                or result.arrival_seq_base is not None
                or result.knowledge_time is not None
            ):
                raise CanonicalNormalizeError("empty result has inconsistent summary fields")

        pin = self._pin(channel, result.source_revision_id)
        survey = self._survey(pin, channel, result.source_revision_id, keep_rows=False)
        try:
            if result.revision_count > 0 and survey.plan is not None:
                _require_complete(channel, result.source_revision_id, survey.plan, survey.unit_rows)
            summary_mismatch = survey.unit_rows != result.revision_count
            if result.revision_count == 0:
                summary_mismatch = summary_mismatch or any(
                    (
                        result.batch_count != 0,
                        survey.plan is not None,
                        survey.base is not None,
                        survey.ready is not None,
                    )
                )
            else:
                summary_mismatch = summary_mismatch or any(
                    (
                        survey.plan is None,
                        survey.plan is not None and survey.plan.count != result.batch_count,
                        survey.base != result.arrival_seq_base,
                        survey.ready != result.knowledge_time,
                        survey.version is None,
                    )
                )
            if summary_mismatch or survey.positions is None:
                raise CatalogIntegrityError(
                    f"normalized result for {result.source_revision_id} no longer matches its "
                    "committed Canonical unit"
                )
            if result.revision_count == 0:
                return
            assert survey.plan is not None
            assert survey.base is not None
            assert survey.ready is not None
            assert survey.version is not None
            assert survey.positions is not None
            positions = survey.positions
            for index in range(result.batch_count):
                low, high, end = _batch_window(positions, survey.plan.chunk, index)
                raw = self._raw_window(
                    pin,
                    channel,
                    result.source_revision_id,
                    low,
                    high,
                    expected_rows=end - index * survey.plan.chunk,
                )
                planned = self._planned(channel, raw, survey.base, survey.ready, survey.version)
                yield from (row["revision_id"] for row in planned)
        finally:
            if survey.positions is not None:
                survey.positions.close()

    def verify_unit(
        self,
        raw_table: str,
        source_revision_id: str,
        *,
        arrival_seqs: Iterable[int] | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        """The unit's committed Canonical rows, proven; nothing is written and no clock is read.

        The committed batch ids give the plan (unit size, microbatch size). Every Raw row of
        the unit is proven, the committed rows must be exactly what their Raw rows normalize to
        under the recovered block base and ready time, the committed batches must be the whole
        plan (a prefix is ``CanonicalUnitIncomplete``, G2-R1a) with their exact fingerprints, and
        the committed rows must be exactly those batches' rows; a REST unit's missing elements
        must be held by other committed pages. A unit with no committed batch returns no rows.
        Run on a catalog view pinned to a manifest's snapshots, it proves what those snapshots
        held (Phase 1 F1).

        With ``arrival_seqs`` (G3-S2) only the committed batches holding those numbers are proven
        and returned — their Raw rows, fingerprints and exact content — while every unit-wide
        fact (plan, distinct Raw positions and an archive's whole object, block base and ready
        time, the unit's committed rows being exactly its committed batches') is still checked
        from narrow columns. A reader of one time window of a large unit thus proves what it
        reads without holding the unit.
        """
        channel = self._channel(raw_table, source_revision_id)
        pin = self._pin(channel, source_revision_id)
        if arrival_seqs is None:
            survey = self._survey(pin, channel, source_revision_id, keep_rows=True)
            try:
                _require_complete(channel, source_revision_id, survey.plan, survey.unit_rows)
                return survey.committed_rows
            finally:
                if survey.positions is not None:
                    survey.positions.close()
        # A caller may transfer a built-in set's ownership for this read-only proof path. Do not
        # copy it: selector consumes its per-unit set before calling us, and verification only
        # iterates these values. General iterables retain the historical frozen snapshot.
        if type(arrival_seqs) in (set, frozenset):
            seqs = cast(Collection[int], arrival_seqs)
        else:
            seqs = frozenset(arrival_seqs)
        return self._verify_batches(pin, channel, source_revision_id, seqs)

    def _stage_verified_unit(
        self,
        raw_table: str,
        source_revision_id: str,
        *,
        arrival_seqs: Iterable[int],
        sink: Callable[[Mapping[str, Any], int, int], None],
        request_capacity: int,
        merge_fanout: int,
        run_limits: RunLimits,
    ) -> None:
        """Prove a bounded selector's requested unit rows directly into private staging.

        The sink is an unfinalized, caller-owned staging writer, not a consumer. A later batch or
        unit may still fail, so callers must not finalize or expose its contents until the whole
        selector proof has succeeded. The public ``verify_unit`` tuple contract is unchanged.
        Proof-batch cache reads/writes are disabled here so a cached tuple cannot overlap the
        bounded run with an additional O(batch) retained proof collection.
        """
        channel = self._channel(raw_table, source_revision_id)
        pin = self._pin(channel, source_revision_id)
        self._verify_batches_to(
            pin,
            channel,
            source_revision_id,
            arrival_seqs,
            sink=None,
            use_batch_cache=False,
            ordered_sink=sink,
            request_capacity=request_capacity,
            merge_fanout=merge_fanout,
            run_limits=run_limits,
        )

    def _verify_batches(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        seqs: Collection[int],
    ) -> tuple[Mapping[str, Any], ...]:
        """The committed batches holding ``seqs``, proven, over the unit-wide facts (G3-S2)."""
        kept: list[Mapping[str, Any]] = []
        self._verify_batches_to(
            pin,
            channel,
            source_revision_id,
            seqs,
            sink=kept.append,
            use_batch_cache=True,
        )
        return tuple(kept)

    def _verify_batches_to(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        seqs: Iterable[int],
        *,
        sink: Callable[[Mapping[str, Any]], None] | None,
        use_batch_cache: bool,
        ordered_sink: Callable[[Mapping[str, Any], int, int], None] | None = None,
        request_capacity: int | None = None,
        merge_fanout: int | None = None,
        run_limits: RunLimits | None = None,
    ) -> None:
        """Private sink form shared by the tuple API and bounded selector staging."""
        facts = self._unit_facts(pin, channel, source_revision_id)
        try:
            if facts.plan is None or facts.base is None or facts.ready is None:
                return
            assert facts.version is not None
            plan, base, ready = facts.plan, facts.base, facts.ready
            _require_complete(channel, source_revision_id, plan, len(facts.positions))
            # Public verify_unit keeps its compatibility collection path. PIT's private bounded
            # path streams revision-sorted requests into an ordered RunSet of unique batch IDs,
            # replacing the all-history `wanted` set while preserving newest-first proof order.
            wanted_root: RunRef | None = None
            if ordered_sink is not None:
                if request_capacity is None or merge_fanout is None or run_limits is None:
                    raise CanonicalNormalizeError(
                        "bounded proof staging requires explicit run bounds"
                    )
                previous_batch: int | None = None
                with RunSetBuilder(
                    self._storage,
                    key=lambda row: -cast(int, row["batch_index"]),
                    capacity=request_capacity,
                    merge_fanout=merge_fanout,
                    limits=run_limits,
                ) as wanted_builder:
                    previous_seq: int | None = None
                    for seq in seqs:
                        if previous_seq is not None and seq < previous_seq:
                            raise CatalogIntegrityError(
                                f"{channel.canonical.table}: bounded arrival requests are not "
                                "sorted"
                            )
                        previous_seq = seq
                        rank = bisect_left(cast(Sequence[int], facts.positions), seq - base)
                        if rank >= len(facts.positions) or facts.positions[rank] != seq - base:
                            continue
                        batch_index = rank // plan.chunk
                        if batch_index != previous_batch:
                            wanted_builder.add({"batch_index": batch_index})
                            previous_batch = batch_index
                    wanted_root = wanted_builder.finish()
            else:
                wanted = _batches_holding(facts.positions, plan.chunk, base, seqs)
            cached: dict[int, tuple[Mapping[str, Any], ...]] = {}
            for index in (
                wanted if ordered_sink is None and self._frozen and use_batch_cache else ()
            ):
                rows = self._batches.get((channel.element.table, source_revision_id, index))
                if rows is not None:
                    cached[index] = rows

            def emit(index: int, rows: Sequence[Mapping[str, Any]]) -> None:
                for row_ordinal, row in enumerate(rows):
                    if ordered_sink is None:
                        assert sink is not None
                        sink(row)
                    else:
                        ordered_sink(row, index, row_ordinal)

            if ordered_sink is not None:
                # Bounded selector staging follows the already-validated reverse snapshot stream.
                # Do not collect one SnapshotInfo per requested batch. Explicit scalar checks
                # make the expected descending order and complete requested set visible here.
                if wanted_root is not None:
                    for index, snapshot in self._plan_snapshots_for_run(
                        pin, channel, source_revision_id, plan, wanted_root
                    ):
                        low, high, end = _batch_window(facts.positions, plan.chunk, index)
                        raw = self._raw_window(
                            pin,
                            channel,
                            source_revision_id,
                            low,
                            high,
                            expected_rows=end - index * plan.chunk,
                        )
                        self._prove(pin, channel, raw)
                        planned = self._planned(channel, raw, base, ready, facts.version)
                        check_batch_snapshot(
                            channel.canonical,
                            unit_batch_id(source_revision_id, plan.unit_rows, plan.chunk, index),
                            snapshot,
                            planned,
                        )
                        self._check_committed_window(pin, channel, base, low, high, planned)
                        emit(index, planned)
                return

            assert ordered_sink is None
            snapshots = dict(
                self._plan_snapshots(pin, channel, source_revision_id, plan, wanted - cached.keys())
            )
            for index in sorted(wanted):
                key = (channel.element.table, source_revision_id, index)
                proven = cached.get(index)
                if proven is not None:
                    emit(index, proven)
                    continue
                low, high, end = _batch_window(facts.positions, plan.chunk, index)
                raw = self._raw_window(
                    pin,
                    channel,
                    source_revision_id,
                    low,
                    high,
                    expected_rows=end - index * plan.chunk,
                )
                self._prove(pin, channel, raw)
                planned = self._planned(channel, raw, base, ready, facts.version)
                check_batch_snapshot(
                    channel.canonical,
                    unit_batch_id(source_revision_id, plan.unit_rows, plan.chunk, index),
                    snapshots[index],
                    planned,
                )
                self._check_committed_window(pin, channel, base, low, high, planned)
                emit(index, planned)
                if self._frozen and use_batch_cache:
                    while len(self._batches) >= _BATCH_CACHE:
                        self._batches.pop(next(iter(self._batches)))
                    self._batches[key] = tuple(planned)
        finally:
            if not self._frozen:
                facts.positions.close()

    def _plan_snapshots_for_run(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        plan: _CommittedPlan,
        wanted_root: RunRef,
    ) -> Iterator[tuple[int, SnapshotInfo]]:
        """Merge newest-first batch requests with the pinned reverse snapshot history."""
        changed = (
            f"{channel.canonical.table}: the batches of unit {source_revision_id} no longer read "
            "as the plan proven at the pinned head"
        )
        with iter_run(self._storage, wanted_root) as rows:
            wanted = next(rows, None)
            wanted_index = None if wanted is None else wanted["batch_index"]
            expected = plan.count - 1
            missing = False
            previous_wanted: int | None = None
            for unit_rows, chunk, index, snapshot in self._unit_batches(
                pin, channel, source_revision_id
            ):
                if (unit_rows, chunk) != (plan.unit_rows, plan.chunk) or index != expected:
                    raise CatalogIntegrityError(changed)
                expected -= 1
                while wanted_index is not None and wanted_index > index:
                    if previous_wanted is not None and wanted_index >= previous_wanted:
                        raise CatalogIntegrityError(
                            f"{channel.canonical.table}: bounded batch requests are not descending"
                        )
                    previous_wanted = wanted_index
                    missing = True
                    wanted = next(rows, None)
                    wanted_index = None if wanted is None else wanted["batch_index"]
                if wanted_index == index:
                    yield index, snapshot
                    previous_wanted = index
                    wanted = next(rows, None)
                    wanted_index = None if wanted is None else wanted["batch_index"]
                    if wanted_index is None:
                        return
            while wanted_index is not None:
                if previous_wanted is not None and wanted_index >= previous_wanted:
                    raise CatalogIntegrityError(
                        f"{channel.canonical.table}: bounded batch requests are not descending"
                    )
                previous_wanted = wanted_index
                missing = True
                wanted = next(rows, None)
                wanted_index = None if wanted is None else wanted["batch_index"]
            if missing:
                raise CatalogIntegrityError(
                    f"{channel.canonical.table}: requested batch snapshot is missing"
                )

    def _unit_facts(
        self, pin: _Pin, channel: rules.RawChannel, source_revision_id: str
    ) -> _UnitFacts:
        """Every unit-wide fact, from narrow columns; kept per unit on an immutable view."""
        key = (channel.element.table, source_revision_id)
        cached = self._facts.get(key) if self._frozen else None
        if cached is not None:
            return cached
        positions, symbol = self._positions(pin, channel, source_revision_id)
        try:
            facts = self._unit_facts_for_positions(
                pin, channel, source_revision_id, positions, symbol
            )
        except BaseException:
            positions.close()
            raise
        if self._frozen:
            while len(self._facts) >= _FACT_CACHE:
                _, evicted = self._facts.popitem()
                evicted.positions.close()
            previous = self._facts.pop(key, None)
            if previous is not None:
                previous.positions.close()
            self._facts[key] = facts
        return facts

    def _unit_facts_for_positions(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        positions: _PositionIndex,
        symbol: str | None,
    ) -> _UnitFacts:
        table = channel.canonical.table
        if not positions:
            self._prove_source(pin, channel, source_revision_id)
        self._check_positions(pin, channel, source_revision_id, positions, symbol)
        plan, ordered = self._committed_plan(pin, channel, source_revision_id)
        committed = self._committed_times(pin, channel, source_revision_id)
        try:
            facts = _UnitFacts(positions, None, None, None)
            if not positions:
                if committed.seq_count or plan is not None:
                    raise CatalogIntegrityError(
                        f"{table} holds Canonical rows or batches of unit {source_revision_id} "
                        "that "
                        "has no Raw element revision"
                    )
            elif plan is None:
                if committed.seq_count:
                    raise CatalogIntegrityError(
                        f"{table}: unit {source_revision_id} has committed rows but no committed "
                        "batch"
                    )
            else:
                base, ready, version = self._recover(channel, source_revision_id, committed)
                _check_plan(channel, source_revision_id, plan, ordered, len(positions))
                covered = min(plan.count * plan.chunk, len(positions))
                if not _same_index_numbers(
                    committed.seqs,
                    committed.seq_count,
                    committed.seq_null,
                    _OffsetSequence(positions[:covered], base),
                ):
                    raise CatalogIntegrityError(
                        f"{table}: the committed rows of unit {source_revision_id} are not exactly "
                        "the rows of its committed batches (rows deleted or added)"
                    )
                facts = _UnitFacts(positions, plan, base, ready, version)
            self._check_rest_unit(pin, channel, source_revision_id, positions)
            return facts
        finally:
            committed.close()

    # ------------------------------------------------------------------ pin

    def _channel(self, raw_table: str, source_revision_id: str) -> rules.RawChannel:
        try:
            channel = rules.raw_channel_of(raw_table)
        except rules.CanonicalRuleViolation as exc:
            raise CanonicalNormalizeError(str(exc)) from None
        if not isinstance(source_revision_id, str) or not source_revision_id:
            raise CanonicalNormalizeError("source_revision_id must be a non-empty string")
        return channel

    def _pin(self, channel: rules.RawChannel, source_revision_id: str) -> _Pin:
        """Heads read twice and equal: at one instant between, all three held them."""
        tables = (channel.element.table, channel.source.table, channel.canonical.table)
        if self._frozen and tables in self._pins:
            return self._pins[tables]
        for _ in range(_ATTEMPTS):
            heads = tuple(self._head(table) for table in tables)
            if tuple(self._head(table) for table in tables) != heads:
                continue
            catalog = PinnedCatalogView(
                self._adapter,
                {table: head for table, head in zip(tables, heads, strict=True) if head},
            )
            pin = _Pin(
                catalog=catalog,
                canonical_head=heads[2],
                verifier=PersistedRowVerifier(catalog, self._storage, cache_archives=True),
            )
            if self._frozen:
                self._pins[tables] = pin
            return pin
        raise CanonicalNormalizeConflict(
            f"{' / '.join(tables)} kept moving: no pinned read of unit {source_revision_id}"
        )

    # ------------------------------------------------------------------ prove (no writes)

    def _survey(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        *,
        keep_rows: bool,
    ) -> _Survey:
        """The proving pass, then (REST) the unit's completeness against its page (RT-3).

        Completeness is judged last, so every committed-state defect keeps its own verdict.
        """
        survey = self._survey_unit(pin, channel, source_revision_id, keep_rows=keep_rows)
        assert survey.positions is not None
        try:
            self._check_rest_unit(pin, channel, source_revision_id, survey.positions)
            return survey
        except BaseException:
            survey.positions.close()
            raise

    def _survey_unit(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        *,
        keep_rows: bool,
    ) -> _Survey:
        positions, symbol = self._positions(pin, channel, source_revision_id)
        try:
            return self._survey_with_positions(
                pin,
                channel,
                source_revision_id,
                keep_rows=keep_rows,
                positions=positions,
                symbol=symbol,
            )
        except BaseException:
            positions.close()
            raise

    def _survey_with_positions(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        *,
        keep_rows: bool,
        positions: _PositionIndex,
        symbol: str | None,
    ) -> _Survey:
        floor: datetime | None = None
        for low, high, expected_rows in _proof_windows(positions, self._microbatch):
            raw = self._raw_window(
                pin,
                channel,
                source_revision_id,
                low,
                high,
                expected_rows=expected_rows,
            )
            self._prove(pin, channel, raw)
            latest = max(row["knowledge_time"] for row in raw)
            floor = latest if floor is None else max(floor, latest)
        unit_rows = len(positions)
        if unit_rows == 0:
            self._prove_source(pin, channel, source_revision_id)
        self._check_positions(pin, channel, source_revision_id, positions, symbol)
        plan, ordered = self._committed_plan(pin, channel, source_revision_id)
        committed = self._committed_times(pin, channel, source_revision_id)
        try:
            return self._survey_with_committed_times(
                pin,
                channel,
                source_revision_id,
                keep_rows=keep_rows,
                positions=positions,
                symbol=symbol,
                floor=floor,
                unit_rows=unit_rows,
                plan=plan,
                ordered=ordered,
                committed=committed,
            )
        finally:
            committed.close()

    def _survey_with_committed_times(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        *,
        keep_rows: bool,
        positions: _PositionIndex,
        symbol: str | None,
        floor: datetime | None,
        unit_rows: int,
        plan: _CommittedPlan | None,
        ordered: bool,
        committed: _CommittedTimes,
    ) -> _Survey:
        table = channel.canonical.table
        if unit_rows == 0:
            if committed.seq_count or plan is not None:
                raise CatalogIntegrityError(
                    f"{table} holds Canonical rows or batches of unit {source_revision_id} that "
                    "has no Raw element revision"
                )
            return _Survey(0, None, None, None, None, (), positions)
        if plan is None:
            if committed.seq_count:
                raise CatalogIntegrityError(
                    f"{table}: unit {source_revision_id} has committed rows but no committed batch"
                )
            return _Survey(unit_rows, floor, None, None, None, (), positions)
        base, ready, version = self._recover(channel, source_revision_id, committed)
        _check_plan(channel, source_revision_id, plan, ordered, unit_rows)
        # Newest first, i.e. highest index first: each batch's snapshot is streamed from the
        # pinned history as it is proven; per batch only requested output rows are kept.
        kept: list[list[Mapping[str, Any]]] = []
        digest = hashlib.sha256()
        covered = min(plan.count * plan.chunk, unit_rows)
        for index, snapshot in self._plan_snapshots(pin, channel, source_revision_id, plan, None):
            low, high, end = _batch_window(positions, plan.chunk, index)
            raw = self._raw_window(
                pin,
                channel,
                source_revision_id,
                low,
                high,
                expected_rows=end - index * plan.chunk,
            )
            planned = self._planned(channel, raw, base, ready, version)
            check_batch_snapshot(
                channel.canonical,
                unit_batch_id(source_revision_id, plan.unit_rows, plan.chunk, index),
                snapshot,
                planned,
            )
            self._check_committed_window(pin, channel, base, low, high, planned)
            if not keep_rows:
                # A replay re-checks what its first run read back (E2 of review E).
                self._check_unique(pin.catalog, channel, planned, None)
            digest.update(_fold(index, snapshot))
            if keep_rows:
                kept.append(planned)
        if not _same_index_numbers(
            committed.seqs,
            committed.seq_count,
            committed.seq_null,
            _OffsetSequence(positions[:covered], base),
        ):
            raise CatalogIntegrityError(
                f"{table}: the committed rows of unit {source_revision_id} are not exactly the "
                "rows of its committed batches (rows deleted or added)"
            )
        return _Survey(
            unit_rows,
            floor,
            plan,
            base,
            ready,
            tuple(row for rows in reversed(kept) for row in rows),
            positions,
            version,
            digest.digest(),
        )

    def _positions(
        self, pin: _Pin, channel: rules.RawChannel, source_revision_id: str
    ) -> tuple[_PositionIndex, str | None]:
        """Disk-sorted Raw positions of a single-symbol unit."""
        column = _position_column(channel)
        reader = pin.catalog.scan_column_batches(
            channel.element.table,
            columns=(column, "symbol"),
            row_filter=_equals(channel.lineage_column, source_revision_id),
        )
        offset = 0 if channel.name == "archive" else 1
        index = _PositionIndex(self._scratch_directory)
        symbol: str | None = None
        try:
            try:
                for record_batch in reader:
                    values = record_batch.column(record_batch.schema.get_field_index(column))
                    if values.null_count:
                        raise CatalogIntegrityError(
                            f"{channel.element.table}: a Raw position is null"
                        )
                    symbols = record_batch.column(record_batch.schema.get_field_index("symbol"))
                    for value in symbols:
                        row_symbol = value.as_py()
                        if not isinstance(row_symbol, str) or not row_symbol:
                            raise CatalogIntegrityError(
                                f"{channel.element.table}: a Raw symbol is null or invalid"
                            )
                        if symbol is None:
                            symbol = row_symbol
                        elif row_symbol != symbol:
                            raise CatalogIntegrityError(
                                f"{channel.element.table}: unit {source_revision_id} contains "
                                "rows for multiple symbols"
                            )
                    raw_values = values.to_pylist()
                    for start in range(0, len(raw_values), _POSITION_INSERT_ROWS):
                        index.add_batch(
                            value + offset
                            for value in raw_values[start : start + _POSITION_INSERT_ROWS]
                        )
            except BaseException:
                close = getattr(reader, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:
                        pass
                raise
            else:
                close = getattr(reader, "close", None)
                if close is not None:
                    close()
            index.finalize()
            return index, symbol
        except BaseException:
            index.close()
            raise

    def _check_positions(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        positions: Sequence[int],
        symbol: str | None,
    ) -> None:
        """Proven Raw positions are distinct; an archive unit is its whole object, lines 1 … N.

        A REST response may lack positions: elements another page delivered first are never
        re-written under it (D3E), so its own positions can have gaps (review E-1) — but only
        those (G2-R1a / RT-3: ``_check_rest_unit``, judged after the committed state). An
        archive revision's rows are its parsed object's lines, all of them (review E-3): a unit
        missing its last lines is truncated, not smaller.
        """
        table = channel.element.table
        if any(a == b for a, b in zip(positions, positions[1:], strict=False)):
            raise CatalogIntegrityError(
                f"{table}: unit {source_revision_id} holds one Raw position twice"
            )
        if channel.name != "archive" or not positions or symbol is None:
            return
        expected = pin.verifier.archive_row_count(channel.data_type, symbol, source_revision_id)
        if positions[0] != 1 or positions[-1] != len(positions) or len(positions) != expected:
            raise CatalogIntegrityError(
                f"{table}: the rows of archive revision {source_revision_id} are not exactly the "
                f"{expected} lines of its object"
            )

    def _check_rest_unit(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        positions: Sequence[int],
    ) -> None:
        """A REST unit is its whole page (``check_rest_page``); an archive is judged by
        ``_check_positions`` (its object's ``1 … N`` lines)."""
        if channel.name == "archive":
            return
        check_rest_page(
            pin.catalog,
            pin.verifier,
            channel,
            source_revision_id,
            _OffsetMembership(positions, 1),
        )

    def _raw_window(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        low: int,
        high: int,
        *,
        expected_rows: int,
    ) -> list[Mapping[str, Any]]:
        """The unit's Raw rows at positions ``low … high``, in position order."""
        offset = 0 if channel.name == "archive" else 1
        columns = tuple(field.name for field in channel.element.arrow_schema)
        rows: list[Mapping[str, Any]] = []
        with _scan_rows(
            pin.catalog,
            channel.element.table,
            columns=columns,
            row_filter=And(
                _equals(channel.lineage_column, source_revision_id),
                _between(_position_column(channel), low - offset, high - offset),
            ),
        ) as scanned:
            for row in scanned:
                rows.append(row)
                if len(rows) > expected_rows:
                    raise CatalogIntegrityError(
                        f"{channel.element.table}: Raw window {low}..{high} contains extra rows"
                    )
        if len(rows) != expected_rows:
            raise CatalogIntegrityError(
                f"{channel.element.table}: Raw window {low}..{high} has {len(rows)} rows; "
                f"expected {expected_rows}"
            )
        return sorted(rows, key=lambda row: (rules.position_of(channel, row), row["revision_id"]))

    def _prove(
        self, pin: _Pin, channel: rules.RawChannel, raw_rows: Sequence[Mapping[str, Any]]
    ) -> None:
        """Every Raw row of the window is proven down to its source revision (D3E-R2)."""
        if channel.name == "archive":
            pin.verifier.verify_archive_elements(
                channel.element, channel.data_type, raw_rows[0]["symbol"], raw_rows
            )
        else:
            pin.verifier.verify_rest_elements(channel.element, channel.data_type, raw_rows)

    def _prove_source(self, pin: _Pin, channel: rules.RawChannel, source_revision_id: str) -> None:
        """A unit without element revisions must still name one committed source revision."""
        count = 0
        with _scan_rows(
            pin.catalog,
            channel.source.table,
            columns=("revision_id",),
            row_filter=_equals("revision_id", source_revision_id),
        ) as found:
            for _ in found:
                count += 1
                if count > 1:
                    break
        if count != 1:
            raise CanonicalNormalizeError(
                f"{source_revision_id} is not one committed revision of {channel.source.table}"
            )

    def _committed_plan(
        self, pin: _Pin, channel: rules.RawChannel, source_revision_id: str
    ) -> tuple[_CommittedPlan | None, bool]:
        """The plan the unit's batch ids record up to the pinned head (``None`` if none), and
        whether its batches were committed in order.

        One walk of the pinned history keeping a few ints (E1-CAP-1). Every batch id of the unit
        must be well formed (refused at once) and of one lawful plan, and no batch committed
        twice. A lawful writer commits batch ``i`` only once ``0 … i - 1`` are committed (its own
        commits, or a replay confirming them), so newest first the indices run
        ``count - 1, …, 0``: an index met again inside that run is a batch committed twice, any
        other step a hole or an out-of-order commit. Holes are reported by ``_check_plan``, where
        the contiguous-prefix check has always been judged (after the Raw unit size).
        """
        table = channel.canonical.table
        plan: tuple[int, int] | None = None
        mixed = False
        count = 0
        newest: int | None = None
        previous: int | None = None
        duplicate: int | None = None
        ordered = True
        for unit_rows, chunk, index, _ in self._unit_batches(pin, channel, source_revision_id):
            if plan is None:
                plan = (unit_rows, chunk)
            elif plan != (unit_rows, chunk):
                mixed = True
            count += 1
            if mixed or duplicate is not None or not ordered:
                continue  # already refused; the walk goes on only for malformed ids
            if previous is None or newest is None:
                newest = index
            elif previous <= index <= newest:
                duplicate = index
            elif index != previous - 1:
                ordered = False
            previous = index
        if plan is None:
            return None, True
        if mixed:
            raise CatalogIntegrityError(
                f"{table}: unit {source_revision_id} has batches of more than one plan"
            )
        unit_rows, chunk = plan
        if unit_rows < 1 or not 1 <= chunk <= MAX_MICROBATCH_ROWS:
            raise CatalogIntegrityError(
                f"{table}: unit {source_revision_id} records no lawful plan"
            )
        if duplicate is not None:
            raise CatalogIntegrityError(
                f"{table} has rows of batch "
                f"{unit_batch_id(source_revision_id, unit_rows, chunk, duplicate)} but more "
                "than one snapshot committing it"
            )
        return (
            _CommittedPlan(unit_rows=unit_rows, chunk=chunk, count=count),
            ordered and previous == 0,
        )

    def _unit_batches(
        self, pin: _Pin, channel: rules.RawChannel, source_revision_id: str
    ) -> Iterator[tuple[int, int, int, SnapshotInfo]]:
        """``(unit rows, chunk, index, snapshot)`` of each batch of the unit up to the pinned
        head, newest first, streamed from its history; a malformed batch id fails closed."""
        table = channel.canonical.table
        prefix = _unit_prefix(source_revision_id)
        widths = (_ROWS_DIGITS, _CHUNK_DIGITS, _INDEX_DIGITS)
        for snapshot in history_from(self._adapter, table, pin.canonical_head):
            batch_id = snapshot.batch_id
            if batch_id is None or not batch_id.startswith(prefix):
                continue
            parts = batch_id[len(prefix) :].split(".")
            if len(parts) != 3 or any(
                len(part) != width or not part.isascii() or not part.isdigit()
                for part, width in zip(parts, widths, strict=True)
            ):
                raise CatalogIntegrityError(f"{table} has a malformed batch id {batch_id!r}")
            unit_rows, chunk, index = (int(part) for part in parts)
            yield unit_rows, chunk, index, snapshot

    def _plan_snapshots(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        plan: _CommittedPlan,
        wanted: Collection[int] | None,
    ) -> Iterator[tuple[int, SnapshotInfo]]:
        """``(index, snapshot)`` of the plan's committed batches (``wanted`` only, if given),
        newest first, streamed from the pinned history — never collected (E1-CAP-1).

        The walk re-proves what ``_committed_plan`` proved of the same pinned history (one plan,
        indices ``count - 1, …, 0`` in order, each once) and stops once nothing more is wanted.
        """
        remaining = plan.count if wanted is None else len(wanted)
        if remaining == 0:
            return
        changed = (
            f"{channel.canonical.table}: the batches of unit {source_revision_id} no longer read "
            "as the plan proven at the pinned head"
        )
        expected = plan.count - 1
        for unit_rows, chunk, index, snapshot in self._unit_batches(
            pin, channel, source_revision_id
        ):
            if (unit_rows, chunk) != (plan.unit_rows, plan.chunk) or index != expected:
                raise CatalogIntegrityError(changed)
            expected -= 1
            if wanted is None or index in wanted:
                yield index, snapshot
                remaining -= 1
                if remaining == 0:
                    return
        raise CatalogIntegrityError(changed)

    def _committed_times(
        self, pin: _Pin, channel: rules.RawChannel, source_revision_id: str
    ) -> _CommittedTimes:
        """Stream committed unit facts; retain only scalar summaries and a disk-sorted seq index."""
        index = _PositionIndex(self._scratch_directory)
        reader: Any | None = None
        count = 0
        seq_null = False
        low: int | None = None
        high: int | None = None
        ready: datetime | None = None
        ready_multiple_or_null = False
        versions: list[object] = []
        try:
            reader = pin.catalog.scan_column_batches(
                channel.canonical.table,
                columns=("arrival_seq", "knowledge_time", "contract_schema_version"),
                row_filter=self._unit_filter(channel, source_revision_id),
            )
            for record_batch in reader:
                seq_values = record_batch.column(record_batch.schema.get_field_index("arrival_seq"))
                ready_values = record_batch.column(
                    record_batch.schema.get_field_index("knowledge_time")
                )
                version_values = record_batch.column(
                    record_batch.schema.get_field_index("contract_schema_version")
                )
                count += record_batch.num_rows
                seq_null = seq_null or bool(seq_values.null_count)
                raw_seqs = seq_values.to_pylist()
                non_null_seqs = [value for value in raw_seqs if value is not None]
                if non_null_seqs:
                    batch_low, batch_high = min(non_null_seqs), max(non_null_seqs)
                    low = batch_low if low is None else min(low, batch_low)
                    high = batch_high if high is None else max(high, batch_high)
                    for start in range(0, len(non_null_seqs), _POSITION_INSERT_ROWS):
                        index.add_batch(non_null_seqs[start : start + _POSITION_INSERT_ROWS])
                for value in ready_values.to_pylist():
                    if value is None:
                        ready_multiple_or_null = True
                    elif ready is None:
                        ready = value
                    elif value != ready:
                        ready_multiple_or_null = True
                for value in version_values.to_pylist():
                    if value not in versions and len(versions) < 2:
                        versions.append(value)
            close = getattr(reader, "close", None)
            if close is not None:
                close()
            index.finalize()
            return _CommittedTimes(
                index,
                count,
                seq_null,
                low,
                high,
                ready,
                ready_multiple_or_null,
                tuple(versions),
            )
        except BaseException:
            close = getattr(reader, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
            index.close()
            raise

    def _recover(
        self,
        channel: rules.RawChannel,
        source_revision_id: str,
        committed: _CommittedTimes,
    ) -> tuple[int, datetime, str]:
        """Block base, ready time and contract version of a unit with committed batches: from its
        rows, never read (the version: ADR-0052 versioned replay, V1)."""
        table = channel.canonical.table
        if committed.seq_count == 0:
            raise CatalogIntegrityError(
                f"{table}: unit {source_revision_id} has committed batches but their rows are gone"
            )
        low, high = committed.low, committed.high
        if low is None or high is None:
            raise CatalogIntegrityError(
                f"{table}: the committed rows of unit {source_revision_id} are not exactly "
                "the rows of its committed batches (rows deleted or added)"
            )
        base = (low // rules.ARRIVAL_SEQ_STRIDE) * rules.ARRIVAL_SEQ_STRIDE
        if (
            high >= base + rules.ARRIVAL_SEQ_STRIDE
            or committed.ready_multiple_or_null
            or committed.ready is None
        ):
            raise CatalogIntegrityError(
                f"{table}: the committed rows of one unit disagree on their block base or "
                "knowledge_time"
            )
        try:
            checked = rules.check_block_base(base)
        except rules.CanonicalRuleViolation as exc:
            raise CatalogIntegrityError(f"{table}: {exc}") from None
        version = contract_version.recorded_version(
            committed.versions, what=f"{table}: unit {source_revision_id}"
        )
        return checked, committed.ready, version

    def _planned(
        self,
        channel: rules.RawChannel,
        raw_rows: Sequence[Mapping[str, Any]],
        base: int,
        ready: datetime,
        version: str,
    ) -> list[Mapping[str, Any]]:
        """The window's Canonical rows, in Raw position order, as the table stores them, built at
        the unit's contract ``version`` (ADR-0052 versioned replay, V1 / V2)."""
        try:
            built = [
                rules.canonical_row(
                    channel, row, base=base, ready_time=ready, contract_schema_version=version
                )
                for row in raw_rows
            ]
        except rules.CanonicalRuleViolation as exc:
            raise CanonicalNormalizeError(f"unit cannot be normalized: {exc}") from None
        return batch_rows(batch(channel.canonical, built))

    def _check_committed_window(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        base: int,
        low: int,
        high: int,
        planned: Sequence[Mapping[str, Any]],
    ) -> None:
        """The window's slice of the block holds exactly the planned rows, each once."""
        _exact(
            channel,
            self._scan_block(pin.catalog, channel, base + low, base + high, None, len(planned)),
            planned,
            committed=True,
        )

    # ------------------------------------------------------------------ write

    def _allocate(self, channel: rules.RawChannel, floor: datetime) -> tuple[int, datetime]:
        """A fresh block and the unit's one clock reading (nothing of the unit is committed)."""
        largest = self._adapter.max_int64(
            channel.canonical.table, "arrival_seq", check=_check_arrival
        )
        try:
            base = rules.next_block_base(largest)
        except rules.CanonicalRuleViolation as exc:
            raise CanonicalNormalizeError(str(exc)) from None
        ready = self._clock()
        if not isinstance(ready, datetime) or ready.tzinfo is None or ready.utcoffset() != _ZERO:
            raise CanonicalNormalizeError("the clock must return timezone-aware UTC")
        if ready < floor:
            raise CanonicalNormalizeConflict(
                "the normalizer ready time precedes a Raw knowledge_time: refusing to backfill"
            )
        return base, ready

    def _write(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        survey: _Survey,
        block: tuple[int, datetime, str],
        chunk: int,
    ) -> tuple[int, int, int]:
        """Commit missing batches and return only fixed-size unit summary values.

        ``block`` is the unit's block base, ready time and contract version.
        """
        base, ready, version = block
        definition = channel.canonical
        unit_rows = survey.unit_rows
        parent = pin.canonical_head
        committed_count = self._replayed_commits(pin, channel, source_revision_id, survey)
        replayed_count = committed_count
        positions = survey.positions
        assert positions is not None
        batch_count = -(-unit_rows // chunk)
        for index in range(committed_count, batch_count):
            low, high, end = _batch_window(positions, chunk, index)
            batch_id = unit_batch_id(source_revision_id, unit_rows, chunk, index)
            planned = self._planned(
                channel,
                self._raw_window(
                    pin,
                    channel,
                    source_revision_id,
                    low,
                    high,
                    expected_rows=end - index * chunk,
                ),
                base,
                ready,
                version,
            )
            table = batch(definition, planned)
            request = CommitRequest(
                table=definition.table,
                batch_id=batch_id,
                batch_fingerprint=definition.fingerprint_rule.fingerprint(table),
                row_count=len(planned),
                expected_parent_snapshot_id=parent,
            )
            result = self._adapter.commit_batch(request, table)
            if result.outcome == CommitOutcome.ALREADY_COMMITTED:
                replayed_count += 1
            parent = result.snapshot.snapshot_id
            self._read_back(channel, base, low, high, planned, parent)
        self._close(channel, source_revision_id, base, positions, parent)
        return batch_count, unit_rows, replayed_count

    def _replayed_commits(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        survey: _Survey,
    ) -> int:
        """Re-prove the existing committed prefix and return only its batch count.

        Their snapshots are streamed from the pinned history again (nothing per batch was kept,
        E1-CAP-1) and must be exactly the ones the proving pass checked against their re-read
        Raw rows (``committed_digest``).
        """
        plan = survey.plan
        if plan is None:
            return 0
        table = channel.canonical.table
        digest = hashlib.sha256()
        assert survey.positions is not None
        for index, snapshot in self._plan_snapshots(pin, channel, source_revision_id, plan, None):
            _, _, end = _batch_window(survey.positions, plan.chunk, index)
            if snapshot.added_rows != end - index * plan.chunk:
                raise CatalogIntegrityError(
                    f"batch {snapshot.batch_id} of {table} was committed with other content"
                )
            digest.update(_fold(index, snapshot))
        if survey.committed_digest is None or digest.digest() != survey.committed_digest:
            raise CatalogIntegrityError(
                f"{table}: the committed batches of unit {source_revision_id} are not the ones "
                "the proving pass checked"
            )
        return plan.count

    def _read_back(
        self,
        channel: rules.RawChannel,
        base: int,
        low: int,
        high: int,
        planned: Sequence[Mapping[str, Any]],
        snapshot_id: str | None,
    ) -> None:
        """At the batch's own snapshot: exactly its rows in its block slice; ids held once."""
        _exact(
            channel,
            self._scan_block(
                self._adapter,
                channel,
                base + low,
                base + high,
                snapshot_id,
                len(planned),
            ),
            planned,
            committed=False,
        )
        self._check_unique(self._adapter, channel, planned, snapshot_id)

    def _check_unique(
        self,
        catalog: Any,
        channel: rules.RawChannel,
        planned: Sequence[Mapping[str, Any]],
        snapshot_id: str | None,
    ) -> None:
        """Each planned revision id is held by exactly one row of its symbol and time range.

        A revision id names its observation, so an honest duplicate shares the symbol and time
        of its original (one partition range); a row lying about them is not scanned here but is
        refused by every PIT proof of the unit it claims (G3-S; review E-2).
        """
        definition = channel.canonical
        column = _time_column(channel)
        times = [row[column] for row in planned]
        held = {row["revision_id"]: 0 for row in planned}
        with _scan_rows(
            catalog,
            definition.table,
            columns=("revision_id",),
            row_filter=And(
                _equals("symbol", planned[0]["symbol"]),
                _between(column, min(times), max(times)),
            ),
            snapshot_id=snapshot_id,
        ) as found:
            for item in found:
                revision_id = item["revision_id"]
                if revision_id in held:
                    held[revision_id] += 1
        for row in planned:
            if held[row["revision_id"]] != 1:
                raise CatalogIntegrityError(
                    f"{definition.table}: revision_id {row['revision_id']} is not held by "
                    "exactly one row"
                )

    def _close(
        self,
        channel: rules.RawChannel,
        source_revision_id: str,
        base: int,
        positions: Sequence[int],
        snapshot_id: str | None,
    ) -> None:
        """The unit's rows are exactly ``base + position`` of its Raw rows; nothing else is in
        the block."""
        table = channel.canonical.table
        expected = _OffsetSequence(positions, base)
        seqs, seq_count, seq_null = _scan_integer_index(
            self._adapter,
            table,
            self._unit_filter(channel, source_revision_id),
            snapshot_id,
            scratch_directory=self._scratch_directory,
        )
        try:
            block, block_count, block_null = _scan_integer_index(
                self._adapter,
                table,
                _from("arrival_seq", base, base + rules.ARRIVAL_SEQ_STRIDE),
                snapshot_id,
                scratch_directory=self._scratch_directory,
            )
            try:
                if not _same_index_numbers(seqs, seq_count, seq_null, expected) or not (
                    _same_index_numbers(block, block_count, block_null, expected)
                ):
                    raise CatalogIntegrityError(
                        f"{table}: unit {source_revision_id} does not read back as exactly its "
                        f"{len(positions)} numbers of block {base}"
                    )
            finally:
                block.close()
        finally:
            seqs.close()

    # ------------------------------------------------------------------ catalog helpers

    def _unit_filter(self, channel: rules.RawChannel, source_revision_id: str) -> BooleanExpression:
        return And(
            _equals("lineage_source_revision_id", source_revision_id),
            _equals("lineage_raw_table", channel.element.table),
        )

    def _scan_block(
        self,
        catalog: Any,
        channel: rules.RawChannel,
        first: int,
        last: int,
        snapshot_id: str | None,
        expected_rows: int,
    ) -> list[Mapping[str, Any]]:
        definition = channel.canonical
        columns = tuple(field.name for field in definition.arrow_schema)
        rows: list[Mapping[str, Any]] = []
        with _scan_rows(
            catalog,
            definition.table,
            columns=columns,
            row_filter=_between("arrival_seq", first, last),
            snapshot_id=snapshot_id,
        ) as scanned:
            for row in scanned:
                rows.append(row)
                if len(rows) > expected_rows:
                    raise CatalogIntegrityError(
                        f"{definition.table}: arrival_seq window {first}..{last} has extra rows"
                    )
        return rows

    def _head(self, table: str) -> str | None:
        info = self._adapter.load_table(table)
        if info is None:
            raise TableNotFound(f"table {table} does not exist; create the Phase 1 tables first")
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id


def check_rest_page(
    catalog: RevisionCatalog,
    verifier: PersistedRowVerifier,
    channel: rules.RawChannel,
    response_revision_id: str,
    own: Collection[int],
) -> None:
    """Every element the response's body holds is its own row or another page's (RT-3).

    ``own`` are the ``element_index`` values committed under the response in ``channel``'s
    element table; ``catalog`` / ``verifier`` must read one pinned state. The response revision
    is proven and its body strictly re-decoded (``page_elements``); each element not held under
    this response must be held — exactly once, proven lawful — under **another** committed
    response revision (the store skips such elements: they are never re-written, ADR-0027).
    Anything else is a page whose element batches stopped half-way (or never started):
    ``CanonicalUnitIncomplete``. The normalizer (E1) and the partition report (E3, hence F3)
    share this one judgement.

    The response's ``element_count`` alone cannot tell a skipped element from a lost one (both
    are simply absent), so completeness is proven element by element from the body.

    Memory is bounded (G2-R3a): the history of the missing elements' observation keys is read
    ``_KEY_CHUNK`` keys at a time and only three narrow columns of it are kept, for the rows
    whose ``revision_id`` is a missing element's; full rows are then read back only for those
    holders (at most the page's ``element_count``) and proven by ``verify_rest_elements``.
    """
    if channel.name == "archive":
        raise CanonicalNormalizeError(f"{channel.element.table} is not a REST element table")
    table = channel.element.table
    response, elements = verifier.page_elements(channel.data_type, response_revision_id)
    body_indices = {element.element_index for element in elements}
    if any(position not in body_indices for position in own):
        raise CatalogIntegrityError(
            f"{table}: unit {response_revision_id} holds positions its body does not"
        )
    missing = [element for element in elements if element.element_index not in own]
    if not missing:
        return
    symbol = _equals("symbol", response["symbol"])
    holders = _holders(catalog, table, symbol, missing)
    unheld = [
        element.element_index
        for element in missing
        if holders.get(element.revision_id, (0, "", ""))[0] != 1
        or holders[element.revision_id][1] == response_revision_id
    ]
    if unheld:
        raise CanonicalUnitIncomplete(
            f"{table}: response revision {response_revision_id} holds {len(elements)} "
            f"element(s) but element(s) {unheld[:8]} are committed neither under it nor "
            "under another page: its element batches stopped half-way (rerun the store)"
        )
    rows: dict[str, Mapping[str, Any]] = {}
    columns = tuple(field.name for field in channel.element.arrow_schema)
    wanted = sorted({element.revision_id for element in missing})
    for start in range(0, len(wanted), _KEY_CHUNK):
        ids = wanted[start : start + _KEY_CHUNK]
        keys = sorted({holders[revision][2] for revision in ids})
        with _scan_rows(
            catalog,
            table,
            columns=columns,
            row_filter=And(
                symbol,
                And(
                    In("observation_key", keys),  # type: ignore[call-arg, arg-type]
                    In("revision_id", ids),  # type: ignore[call-arg, arg-type]
                ),
            ),
        ) as scanned:
            for row in scanned:
                if row["revision_id"] in rows:
                    raise CatalogIntegrityError(
                        f"{table}: element revision {row['revision_id']} is held twice"
                    )
                rows[row["revision_id"]] = row
    if len(rows) != len(wanted):
        raise CatalogIntegrityError(
            f"{table}: the holders of response revision {response_revision_id}'s missing "
            "elements did not read back"
        )
    verifier.verify_rest_elements(
        channel.element, channel.data_type, [rows[element.revision_id] for element in missing]
    )


def _holders(
    catalog: RevisionCatalog,
    table: str,
    symbol: BooleanExpression,
    missing: Sequence[PageElement],
) -> dict[str, tuple[int, str, str]]:
    """Per missing element revision id: ``(rows holding it, a holder's response, its key)``.

    Rows are those of the symbol whose ``observation_key`` is a missing element's key; the
    response / key are only meaningful when the count is 1 (then they are the one holder's).
    """
    wanted_ids = {element.revision_id for element in missing}
    keys = sorted({element.observation_key for element in missing})
    found: dict[str, tuple[int, str, str]] = {}
    for start in range(0, len(keys), _KEY_CHUNK):
        with _scan_rows(
            catalog,
            table,
            columns=("revision_id", "response_revision_id", "observation_key"),
            row_filter=And(
                symbol,
                In("observation_key", keys[start : start + _KEY_CHUNK]),  # type: ignore[call-arg, arg-type]
            ),
        ) as scanned:
            for row in scanned:
                revision = row["revision_id"]
                if revision not in wanted_ids:
                    continue
                lineage = row["response_revision_id"]
                key = row["observation_key"]
                count = found.get(revision, (0, lineage, key))[0]
                found[revision] = (count + 1, lineage, key)
    return found


def _require_complete(
    channel: rules.RawChannel,
    source_revision_id: str,
    plan: _CommittedPlan | None,
    unit_rows: int,
) -> None:
    """A reader takes a normalized unit only whole: every batch of its plan committed (RT-1).

    A unit with no committed batch is simply not normalized (it has no rows to read); a
    committed prefix of the plan is a normalization that stopped half-way, never a smaller unit.
    """
    if plan is None:
        return
    planned = -(-unit_rows // plan.chunk)
    if plan.count != planned:
        raise CanonicalUnitIncomplete(
            f"{channel.canonical.table}: unit {source_revision_id} has {plan.count} of the "
            f"{planned} batches of its plan committed: its normalization stopped half-way "
            "(rerun the normalizer)"
        )


def _check_plan(
    channel: rules.RawChannel,
    source_revision_id: str,
    plan: _CommittedPlan,
    ordered: bool,
    unit_rows: int,
) -> None:
    """The committed plan fits the Raw unit: its size, and batches ``0 … count - 1`` committed
    in order (``ordered``, from ``_committed_plan``), none beyond the plan."""
    table = channel.canonical.table
    if plan.unit_rows != unit_rows:
        raise CatalogIntegrityError(
            f"{table}: unit {source_revision_id} was normalized as {plan.unit_rows} rows but "
            f"its Raw unit now has {unit_rows}: the Raw unit changed"
        )
    if not ordered:
        raise CatalogIntegrityError(
            f"{table}: the batches of unit {source_revision_id} are not a contiguous prefix "
            "committed in order"
        )
    if (plan.count - 1) * plan.chunk >= unit_rows:
        raise CatalogIntegrityError(
            f"{table}: batch {plan.count - 1} of unit {source_revision_id} lies beyond its plan"
        )


def _fold(index: int, snapshot: SnapshotInfo) -> bytes:
    """One committed batch's snapshot identity and recorded content, as digest input."""
    fields = (
        str(index),
        snapshot.snapshot_id,
        str(snapshot.batch_id),
        str(snapshot.batch_fingerprint),
        str(snapshot.added_rows),
    )
    return "\x1f".join(fields).encode() + b"\x1e"


def _position_column(channel: rules.RawChannel) -> str:
    return "archive_line_number" if channel.name == "archive" else "element_index"


def _time_column(channel: rules.RawChannel) -> str:
    """The Canonical table's partitioning time column."""
    return "event_time" if channel.data_type == "agg_trades" else "interval_start"


def _proof_windows(positions: Sequence[int], size: int) -> Iterator[tuple[int, int, int]]:
    """Disjoint rank windows; a repeated position group may extend a window past ``size``."""
    start = 0
    while start < len(positions):
        low = positions[start]
        end = min(start + size, len(positions))
        high = positions[end - 1]
        while end < len(positions) and positions[end] == high:
            end += 1  # never split rows sharing a position across windows
        yield low, high, end - start
        start = end


def _batch_window(positions: Sequence[int], chunk: int, index: int) -> tuple[int, int, int]:
    """Batch ``index`` = ranks ``[index*chunk, end)``: its lowest and highest position, ``end``."""
    start = index * chunk
    end = min(start + chunk, len(positions))
    return positions[start], positions[end - 1], end


def _batches_holding(
    positions: Sequence[int], chunk: int, base: int, seqs: Iterable[int]
) -> set[int]:
    """Indices of the batches whose rows carry any of ``seqs`` (others name no row of the unit)."""
    found: set[int] = set()
    for seq in seqs:
        rank = bisect_left(positions, seq - base)
        if rank < len(positions) and positions[rank] == seq - base:
            found.add(rank // chunk)
    return found


def _scan_integer_index(
    catalog: Any,
    table: str,
    row_filter: BooleanExpression,
    snapshot_id: str | None,
    *,
    scratch_directory: Path,
) -> tuple[_PositionIndex, int, bool]:
    """Stream one arrival_seq column into a disk-sorted index at the requested snapshot."""
    index = _PositionIndex(scratch_directory)
    reader: Any | None = None
    count = 0
    has_null = False
    try:
        reader = catalog.scan_column_batches(
            table,
            columns=("arrival_seq",),
            row_filter=row_filter,
            snapshot_id=snapshot_id,
        )
        for record_batch in reader:
            values = record_batch.column(record_batch.schema.get_field_index("arrival_seq"))
            count += record_batch.num_rows
            has_null = has_null or bool(values.null_count)
            non_null = [value for value in values.to_pylist() if value is not None]
            for start in range(0, len(non_null), _POSITION_INSERT_ROWS):
                index.add_batch(non_null[start : start + _POSITION_INSERT_ROWS])
        close = getattr(reader, "close", None)
        if close is not None:
            close()
        index.finalize()
        return index, count, has_null
    except BaseException:
        close = getattr(reader, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass
        index.close()
        raise


def _same_index_numbers(
    values: _PositionIndex, count: int, has_null: bool, expected: Sequence[int]
) -> bool:
    """The streamed values are exactly the ascending, distinct expected values, each once."""
    if has_null or count != len(expected) or len(values) != count:
        return False
    return all(actual == wanted for actual, wanted in zip(values, expected, strict=True))


def _exact(
    channel: rules.RawChannel,
    found: Iterable[Mapping[str, Any]],
    planned: Sequence[Mapping[str, Any]],
    *,
    committed: bool,
) -> None:
    """``found`` is exactly ``planned``: same revisions, each once, every column equal."""
    table = channel.canonical.table
    expected = {row["revision_id"]: row for row in planned}
    seen: set[str] = set()
    for row in found:
        revision = row["revision_id"]
        if revision in seen:
            raise CatalogIntegrityError(
                f"{table}: Canonical revision {revision} is committed twice"
            )
        seen.add(revision)
        wanted = expected.get(revision)
        if wanted is None:
            raise CatalogIntegrityError(
                f"{table}: committed Canonical revision {revision} is not what its Raw row "
                "normalizes to"
                if committed
                else f"{table}: {revision} reads back in a block slice it does not belong to"
            )
        mismatched = sorted(name for name, value in wanted.items() if row[name] != value)
        if mismatched:
            raise CatalogIntegrityError(
                f"{table}: committed Canonical revision {revision} disagrees with its "
                f"re-normalized row: {mismatched}"
                if committed
                else f"{table}: {revision} reads back differently: {mismatched}"
            )
    if seen != set(expected):
        raise CatalogIntegrityError(
            f"{table}: the committed rows of a unit are not exactly the rows of its committed "
            "batches (rows deleted or added)"
            if committed
            else f"{table}: a unit reads back with other revisions"
        )


def _check_arrival(value: int) -> None:
    """Every streamed Canonical ``arrival_seq`` must lie inside ``[0, 2**62)``."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise CatalogIntegrityError("a committed Canonical arrival_seq is not an integer")
    if not 0 < value < rules.ARRIVAL_SEQ_LIMIT or value % rules.ARRIVAL_SEQ_STRIDE == 0:
        raise CatalogIntegrityError(
            f"committed Canonical arrival_seq {value} is outside its interval or a block base"
        )

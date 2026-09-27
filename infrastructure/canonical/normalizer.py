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
   and microbatch size; committed batches must be a contiguous prefix of it, each holding exactly
   the rows its window normalizes to (content, fingerprint, row count) under the block base and
   ready time recovered from them, and the unit's committed rows must be exactly those batches'
   rows;
3. **write** — only if nothing of the unit is committed: a fresh block above the table's largest
   ``arrival_seq`` and **one** reading of the injected UTC clock, never before a Raw
   ``knowledge_time`` (refused, not raised). Each missing batch
   ``<normalizer>@<version>.<source revision>.<unit rows>.<chunk>.<index>`` is re-read from the
   pinned Raw snapshot, normalized, committed with the expected parent and read back at its own
   snapshot (exact rows, each once; revision ids unique in the rows' own event-time range);
4. **close** — at the final snapshot the unit's rows are exactly ``base + position`` of its Raw
   rows and nothing else sits in its block.

No step holds the unit, or anything per batch of it (E1-CAP-1). An archive unit's positions are
proven to be exactly its object's lines ``1 … N`` in fixed narrow windows and are then the O(1)
``range(1, N + 1)``; a REST unit is one page, at most ``PAGE_LIMIT`` positions. The committed plan
is three ints — unit size, microbatch, committed batches — proven by one ordered walk of the
pinned history (the batches are ``0 … count - 1``, each committed once and in order); batch
snapshots are streamed from that history when a batch is proven, and the write resumes at
``count``. Block base and ready time come from one committed row; every unit-wide equality —
committed rows against their plan, the closing block — is checked over tiles of whole batches
(at most ``narrow_rows`` rows) plus one-row probes outside them. Every read of the Raw element
or Canonical table is capped at one microbatch or one narrow window (+ 1 row), or one page for a
REST unit, and the result reports counts, not a list per batch. The strict D1 re-parse that
``PersistedRowVerifier`` proves archive rows against is spooled to disk and read back one window
of lines at a time (``spool_archive``). ``collect_unit_rows`` is the one explicitly unbounded form
(it returns every row); no production path calls it — readers use ``verify_unit(...,
arrival_seqs=...)``. What still grows with the table's commits is the Iceberg metadata itself
(one snapshot and one manifest per committed batch), which PyIceberg loads for every read.

A unit whose Raw rows changed after it was first normalized no longer matches its committed plan
and fails closed: normalize a Raw unit only after its ingest returned. Readers (``verify_unit``)
take a normalized unit only whole: a committed prefix of its plan is ``CanonicalUnitIncomplete``
(G2-R1a / RT-1), resolved by rerunning ``normalize_unit``. Nothing is repaired or
rewritten; there is no journal or sidecar. The normalizer never reads the Raw evidence table:
cross-channel edges are mapped at PIT time (ADR-0028 §3.2).
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Self

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.compute as pc  # type: ignore[import-untyped]
from pyiceberg.expressions import (
    And,
    BooleanExpression,
    EqualTo,
    GreaterThan,
    GreaterThanOrEqual,
    In,
    IsNull,
    LessThan,
    LessThanOrEqual,
    Or,
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
from infrastructure.canonical import rules
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.parser.archive_spool import DEFAULT_SPOOL_CHUNK_ROWS
from infrastructure.pit.view import PinnedCatalogView
from infrastructure.revision.rest_identity import PAGE_LIMIT
from infrastructure.revision.row_integrity import (
    PageElement,
    PersistedRowVerifier,
    batch,
    batch_rows,
    check_batch_snapshot,
    history_from,
    ordered_batches,
)
from infrastructure.revision.store import RevisionCatalog

__all__ = [
    "DEFAULT_MICROBATCH_ROWS",
    "DEFAULT_NARROW_ROWS",
    "MAX_NARROW_ROWS",
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
#: Rows per narrow (one to three column) read of the unit-wide checks: Raw positions, committed
#: ``arrival_seq`` / ``knowledge_time`` and the closing block (E1-CAP-1). A fixed window, never
#: the unit: at least the microbatch, so a check reads whole batches.
DEFAULT_NARROW_ROWS: Final = 65_536
#: Hard ceiling for caller-selected narrow reads. Raising this requires changing the E1 capacity
#: budget deliberately; a runtime configuration cannot make unit-wide checks wider than it.
MAX_NARROW_ROWS: Final = 65_536
#: Revision ids per uniqueness lookup (``_check_unique``).
_UNIQUE_CHUNK: Final = 4_096
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

    Three ints (E1-CAP-1): the committed batches are proven to be exactly ``0 … count - 1``,
    each committed once and in order (``ordered_batches``); their snapshots are streamed from
    the pinned history when needed (``_plan_snapshots``), never collected.
    """

    unit_rows: int
    chunk: int
    count: int

    @property
    def planned(self) -> int:
        """Batches of the whole plan."""
        return -(-self.unit_rows // self.chunk)


@dataclass(frozen=True, slots=True)
class CanonicalUnitNormalized:
    """Result of normalizing one unit."""

    raw_table: str
    source_revision_id: str
    canonical_table: str
    #: ``None`` for a unit without element revisions (e.g. an empty REST page).
    arrival_seq_base: int | None
    knowledge_time: datetime | None
    #: Canonical revisions of the unit (one per Raw element revision; E1-CAP-1: a count, not
    #: the ids — read them from the table, batch by batch, when needed).
    row_count: int
    #: Batches of the unit's plan (``0`` for an empty unit) and how many of them this call
    #: committed; the others were already committed (E1-CAP-1: counts, not a list per batch).
    batch_count: int
    committed_batches: int
    #: The Canonical snapshot the unit was closed at (``None`` for an empty unit).
    snapshot_id: str | None

    @property
    def replayed(self) -> bool:
        """Nothing was committed by this call (every batch was already committed)."""
        return self.committed_batches == 0


@dataclass(frozen=True, slots=True)
class _Pin:
    """One call's fixed view: every read goes through ``catalog``."""

    catalog: PinnedCatalogView
    canonical_head: str | None
    verifier: PersistedRowVerifier


@dataclass(frozen=True, slots=True)
class _UnitFacts:
    """A unit's proven unit-wide facts (no Raw or Canonical rows)."""

    #: ``range`` for an archive unit, at most ``PAGE_LIMIT`` ints for a REST one.
    positions: Sequence[int]
    plan: _CommittedPlan | None
    base: int | None
    ready: datetime | None


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
    #: The committed rows, window by window (only ``collect_unit_rows`` collects them).
    committed_rows: tuple[Mapping[str, Any], ...]
    #: The unit's Raw positions, ascending and distinct (batches are rank slices of them);
    #: ``range`` for an archive unit, at most ``PAGE_LIMIT`` ints for a REST one.
    positions: Sequence[int] = ()


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
        clock: Callable[[], datetime] | None = None,
        microbatch_rows: int = DEFAULT_MICROBATCH_ROWS,
        narrow_rows: int = DEFAULT_NARROW_ROWS,
        spool_dir: Path | None = None,
    ) -> None:
        for name, value in (("microbatch_rows", microbatch_rows), ("narrow_rows", narrow_rows)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise CanonicalNormalizeError(f"{name} must be an int")
        if not 1 <= microbatch_rows <= MAX_MICROBATCH_ROWS:
            raise CanonicalNormalizeError(
                f"microbatch_rows must be between 1 and {MAX_MICROBATCH_ROWS}"
            )
        if not 1 <= narrow_rows <= MAX_NARROW_ROWS:
            raise CanonicalNormalizeError(f"narrow_rows must be between 1 and {MAX_NARROW_ROWS}")
        self._adapter = adapter
        self._storage = storage
        self._clock = clock or (lambda: datetime.now(UTC))
        self._microbatch = microbatch_rows
        #: Narrow checks read whole batches of the plan they check, at least one per read.
        self._narrow = narrow_rows
        #: Where the verifier spools re-parsed archive objects (a disk directory; E1-CAP-1).
        self._spool_dir = spool_dir
        #: On a ``PinnedCatalogView`` nothing can change under the normalizer (it cannot write
        #: there either), so its pins — with their archive caches — and each unit's unit-wide
        #: facts are proven once and reused by every later call (G3-S3: a reader of many time
        #: slices of one unit).
        self._frozen = isinstance(adapter, PinnedCatalogView)
        self._pins: dict[tuple[str, ...], _Pin] = {}
        self._facts: dict[tuple[str, str], _UnitFacts] = {}
        self._batches: dict[tuple[str, str, int], tuple[Mapping[str, Any], ...]] = {}

    def close(self) -> None:
        """Delete the spooled archive objects of the pins this normalizer keeps (idempotent)."""
        for pin in self._pins.values():
            pin.verifier.close()
        self._pins.clear()
        self._facts.clear()
        self._batches.clear()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _done_with(self, pin: _Pin) -> None:
        """A pin nobody keeps: its spools go now, not at garbage collection."""
        if not self._frozen:
            pin.verifier.close()

    # ------------------------------------------------------------------ entry points

    def normalize_unit(self, raw_table: str, source_revision_id: str) -> CanonicalUnitNormalized:
        channel = self._channel(raw_table, source_revision_id)
        last_error: Exception | None = None
        for _ in range(_ATTEMPTS):
            pin = self._pin(channel, source_revision_id)
            try:
                survey = self._survey(pin, channel, source_revision_id, keep_rows=False)
                if survey.unit_rows == 0:
                    return CanonicalUnitNormalized(
                        raw_table=raw_table,
                        source_revision_id=source_revision_id,
                        canonical_table=channel.canonical.table,
                        arrival_seq_base=None,
                        knowledge_time=None,
                        row_count=0,
                        batch_count=0,
                        committed_batches=0,
                        snapshot_id=None,
                    )
                if survey.plan is None:
                    assert survey.raw_floor is not None
                    base, ready = self._allocate(channel, survey.raw_floor)
                    chunk = self._microbatch
                else:
                    assert survey.base is not None and survey.ready is not None
                    base, ready, chunk = survey.base, survey.ready, survey.plan.chunk
                try:
                    committed, snapshot = self._write(
                        pin, channel, source_revision_id, survey, base, ready, chunk
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
                    # identically: another content under the same id is corruption, not
                    # contention.
                    raise CatalogIntegrityError(
                        f"a Canonical batch of unit {source_revision_id} is committed with other "
                        f"content: {exc}"
                    ) from None
            finally:
                self._done_with(pin)
            return CanonicalUnitNormalized(
                raw_table=raw_table,
                source_revision_id=source_revision_id,
                canonical_table=channel.canonical.table,
                arrival_seq_base=base,
                knowledge_time=ready,
                row_count=survey.unit_rows,
                batch_count=-(-survey.unit_rows // chunk),
                committed_batches=committed,
                snapshot_id=snapshot,
            )
        raise CanonicalNormalizeConflict(
            f"unit {source_revision_id} of {raw_table} lost {_ATTEMPTS} commit races"
        ) from last_error

    def verify_unit(
        self,
        raw_table: str,
        source_revision_id: str,
        *,
        arrival_seqs: Iterable[int],
    ) -> tuple[Mapping[str, Any], ...]:
        """The committed batches holding ``arrival_seqs``, proven; no write, no clock (G3-S2).

        The committed batch ids give the plan (unit size, microbatch size). Every unit-wide fact
        is checked — the plan is committed whole (a prefix is ``CanonicalUnitIncomplete``,
        G2-R1a), the Raw positions are distinct and an archive's whole object, block base and
        ready time agree, the unit's committed rows are exactly its committed batches' — from
        narrow reads of a fixed window; the batches holding those numbers are then proven like
        a full proof proves each batch (their Raw rows, fingerprints and exact content) and
        returned. A unit with no committed batch returns no rows. Run on a catalog view pinned
        to a manifest's snapshots, it proves what those snapshots held (Phase 1 F1).

        Memory is one microbatch per asked batch plus fixed windows, never the unit (E1-CAP-1);
        ``collect_unit_rows`` is the separate, explicitly unbounded whole-unit form.
        """
        channel = self._channel(raw_table, source_revision_id)
        pin = self._pin(channel, source_revision_id)
        try:
            return self._verify_batches(pin, channel, source_revision_id, frozenset(arrival_seqs))
        finally:
            self._done_with(pin)

    def collect_unit_rows(
        self, raw_table: str, source_revision_id: str
    ) -> tuple[Mapping[str, Any], ...]:
        """**Every** committed Canonical row of the unit, proven — memory O(unit rows).

        A convenience for tests, debugging and exports of small units: it runs the full proof
        (the replay's survey, batch by batch) and additionally keeps and returns every row. No
        production path calls it — ``normalize_unit`` proves without keeping rows and readers
        use ``verify_unit(..., arrival_seqs=...)`` (E1-CAP-1). A committed prefix of the plan is
        ``CanonicalUnitIncomplete`` (RT-1); a unit with no committed batch returns no rows.
        """
        channel = self._channel(raw_table, source_revision_id)
        pin = self._pin(channel, source_revision_id)
        try:
            survey = self._survey(pin, channel, source_revision_id, keep_rows=True)
        finally:
            self._done_with(pin)
        _require_complete(channel, source_revision_id, survey.plan)
        return survey.committed_rows

    def _verify_batches(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        seqs: frozenset[int],
    ) -> tuple[Mapping[str, Any], ...]:
        """The committed batches holding ``seqs``, proven, over the unit-wide facts (G3-S2)."""
        facts = self._unit_facts(pin, channel, source_revision_id)
        if facts.plan is None or facts.base is None or facts.ready is None:
            return ()
        plan, base, ready = facts.plan, facts.base, facts.ready
        _require_complete(channel, source_revision_id, plan)
        wanted = {
            index
            for index in _batches_holding(facts.positions, plan.chunk, base, seqs)
            if index < plan.count
        }
        kept: list[Mapping[str, Any]] = []
        unproven: set[int] = set()
        for index in sorted(wanted):
            key = (channel.element.table, source_revision_id, index)
            proven = self._batches.get(key) if self._frozen else None
            if proven is None:
                unproven.add(index)
        snapshots = dict(self._plan_snapshots(pin, channel, source_revision_id, plan, unproven))
        for index in sorted(wanted):
            key = (channel.element.table, source_revision_id, index)
            proven = self._batches.get(key) if self._frozen else None
            if proven is not None:
                kept.extend(proven)
                continue
            low, high, end = _batch_window(facts.positions, plan.chunk, index)
            raw = self._raw_window(
                pin, channel, source_revision_id, low, high, end - index * plan.chunk
            )
            self._prove(pin, channel, raw)
            planned = self._planned(channel, raw, base, ready)
            check_batch_snapshot(
                channel.canonical,
                unit_batch_id(source_revision_id, plan.unit_rows, plan.chunk, index),
                snapshots[index],
                planned,
            )
            self._check_committed_window(pin, channel, base, low, high, planned)
            kept.extend(planned)
            if self._frozen:
                # Consecutive time slices share at most their boundary batch: keep the last few.
                while len(self._batches) >= _BATCH_CACHE:
                    self._batches.pop(next(iter(self._batches)))
                self._batches[key] = tuple(planned)
        return tuple(kept)

    def _unit_facts(
        self, pin: _Pin, channel: rules.RawChannel, source_revision_id: str
    ) -> _UnitFacts:
        """Every unit-wide fact, from narrow reads; kept per unit on an immutable view."""
        key = (channel.element.table, source_revision_id)
        cached = self._facts.get(key) if self._frozen else None
        if cached is not None:
            return cached
        positions = self._positions(pin, channel, source_revision_id)
        if not positions:
            self._prove_source(pin, channel, source_revision_id)
        plan, base, ready = self._committed_state(pin, channel, source_revision_id, positions)
        if plan is not None:
            assert base is not None and ready is not None
            self._check_unit_numbers(
                pin, channel, source_revision_id, positions, plan.chunk, plan.count, base, ready
            )
        facts = _UnitFacts(positions, plan, base, ready)
        self._check_rest_unit(pin, channel, source_revision_id, positions)
        if self._frozen:
            while len(self._facts) >= _FACT_CACHE:
                self._facts.pop(next(iter(self._facts)))
            self._facts[key] = facts
        return facts

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
                verifier=PersistedRowVerifier(
                    catalog,
                    self._storage,
                    cache_archives=True,
                    spool_dir=self._spool_dir,
                    spool_rows=min(DEFAULT_SPOOL_CHUNK_ROWS, self._microbatch),
                ),
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
        self._check_rest_unit(pin, channel, source_revision_id, survey.positions)
        return survey

    def _survey_unit(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        *,
        keep_rows: bool,
    ) -> _Survey:
        positions = self._positions(pin, channel, source_revision_id)
        floor: datetime | None = None
        for low, high, count in _proof_windows(positions, self._microbatch):
            raw = self._raw_window(pin, channel, source_revision_id, low, high, count)
            self._prove(pin, channel, raw)
            latest = max(row["knowledge_time"] for row in raw)
            floor = latest if floor is None else max(floor, latest)
        unit_rows = len(positions)
        if unit_rows == 0:
            self._prove_source(pin, channel, source_revision_id)
        plan, base, ready = self._committed_state(pin, channel, source_revision_id, positions)
        if unit_rows == 0:
            return _Survey(0, None, None, None, None, (), ())
        if plan is None:
            return _Survey(unit_rows, floor, None, None, None, (), positions)
        assert base is not None and ready is not None
        kept: list[list[Mapping[str, Any]]] = []
        # Newest first, i.e. highest index first: the snapshots stream from the pinned history.
        for index, snapshot in self._plan_snapshots(pin, channel, source_revision_id, plan, None):
            low, high, end = _batch_window(positions, plan.chunk, index)
            raw = self._raw_window(
                pin, channel, source_revision_id, low, high, end - index * plan.chunk
            )
            planned = self._planned(channel, raw, base, ready)
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
            if keep_rows:
                kept.append(planned)  # collect_unit_rows only
        self._check_unit_numbers(
            pin, channel, source_revision_id, positions, plan.chunk, plan.count, base, ready
        )
        rows = tuple(row for batch_rows in reversed(kept) for row in batch_rows)
        return _Survey(unit_rows, floor, plan, base, ready, rows, positions)

    def _committed_state(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        positions: Sequence[int],
    ) -> tuple[_CommittedPlan | None, int | None, datetime | None]:
        """The unit's committed plan, block base and ready time, the plan checked against it.

        The plan must fit the Raw unit (its size, a contiguous prefix of batches inside it); a
        unit without committed batches must have no committed rows. The caller then proves the
        committed rows are exactly its batches' (``_check_unit_numbers``) — after checking each
        batch, so a batch's own defect keeps its own verdict.
        """
        table = channel.canonical.table
        plan = self._committed_plan(pin, channel, source_revision_id)
        unit_rows = len(positions)
        if unit_rows == 0:
            if plan is not None or self._has_unit_rows(pin, channel, source_revision_id):
                raise CatalogIntegrityError(
                    f"{table} holds Canonical rows or batches of unit {source_revision_id} that "
                    "has no Raw element revision"
                )
            return None, None, None
        if plan is None:
            if self._has_unit_rows(pin, channel, source_revision_id):
                raise CatalogIntegrityError(
                    f"{table}: unit {source_revision_id} has committed rows but no committed batch"
                )
            return None, None, None
        base, ready = self._recover(pin, channel, source_revision_id)
        if plan.unit_rows != unit_rows:
            raise CatalogIntegrityError(
                f"{table}: unit {source_revision_id} was normalized as {plan.unit_rows} rows but "
                f"its Raw unit now has {unit_rows}: the Raw unit changed"
            )
        # ``_committed_plan`` proved the batches are exactly ``0 … count - 1`` (in order, once).
        if (plan.count - 1) * plan.chunk >= unit_rows:
            raise CatalogIntegrityError(
                f"{table}: batch {plan.count - 1} of unit {source_revision_id} lies beyond its plan"
            )
        return plan, base, ready

    def _positions(
        self, pin: _Pin, channel: rules.RawChannel, source_revision_id: str
    ) -> Sequence[int]:
        """The unit's Raw positions, ascending and distinct, proven without holding them.

        An archive unit must be exactly its object's lines ``1 … N`` (review E-3: a unit missing
        its last lines is truncated, not smaller). That is proven one window of ``narrow_rows``
        positions at a time — one narrow column, each read capped at one row beyond its window —
        plus two one-row probes (a null position, a position outside ``1 … N``); the positions
        are then ``range(1, N + 1)``, O(1) whatever ``N`` (E1-CAP-1).

        A REST unit is one response's elements: at most ``PAGE_LIMIT`` (the frozen query limit a
        page never exceeds), read capped at ``PAGE_LIMIT + 1`` and held. It may lack positions:
        elements another page delivered first are never re-written under it (D3E), so its own
        positions can have gaps (review E-1) — but only those (G2-R1a / RT-3:
        ``_check_rest_unit``, judged after the committed state).
        """
        table = channel.element.table
        column = _position_column(channel)
        unit = _equals(channel.lineage_column, source_revision_id)
        twice = f"{table}: unit {source_revision_id} holds one Raw position twice"
        if channel.name != "archive":
            found = pin.catalog.scan_columns(
                table, columns=(column,), row_filter=unit, limit=PAGE_LIMIT + 1
            ).column(column)
            if found.null_count:
                raise CatalogIntegrityError(f"{table}: a Raw position is null")
            if len(found) > PAGE_LIMIT:
                raise CatalogIntegrityError(
                    f"{table}: unit {source_revision_id} holds more rows than one page of "
                    f"{PAGE_LIMIT} elements"
                )
            positions = sorted(value + 1 for value in found.to_pylist())
            if any(a == b for a, b in zip(positions, positions[1:], strict=False)):
                raise CatalogIntegrityError(twice)
            return tuple(positions)
        sample = pin.catalog.scan_columns(table, columns=("symbol",), row_filter=unit, limit=1)
        if not sample.num_rows:
            return range(1, 1)
        null = IsNull(column)  # type: ignore[call-arg, arg-type]
        if self._any_row(pin, table, column, And(unit, null)):
            raise CatalogIntegrityError(f"{table}: a Raw position is null")
        symbol = sample.column("symbol")[0].as_py()
        expected = pin.verifier.archive_row_count(channel.data_type, symbol, source_revision_id)
        whole = (
            f"{table}: the rows of archive revision {source_revision_id} are not exactly the "
            f"{expected} lines of its object"
        )
        outside = Or(
            LessThan(column, 1),  # type: ignore[call-arg, arg-type]
            GreaterThan(column, expected),  # type: ignore[call-arg, arg-type]
        )
        if self._any_row(pin, table, column, And(unit, outside)):
            raise CatalogIntegrityError(whole)
        for low in range(1, expected + 1, self._narrow):
            high = min(low + self._narrow - 1, expected)
            size = high - low + 1
            found = pin.catalog.scan_columns(
                table,
                columns=(column,),
                row_filter=And(unit, _between(column, low, high)),
                limit=size + 1,
            ).column(column)
            # In [low, high]: distinct and ``size`` of them is exactly the window's lines.
            if pc.count_distinct(found).as_py() != len(found):
                raise CatalogIntegrityError(twice)
            if len(found) != size:
                raise CatalogIntegrityError(whole)
        return range(1, expected + 1)

    def _check_rest_unit(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        positions: Sequence[int],
    ) -> None:
        """A REST unit is its whole page (``check_rest_page``); an archive is judged by
        ``_positions`` (its object's ``1 … N`` lines)."""
        if channel.name == "archive":
            return
        check_rest_page(
            pin.catalog,
            pin.verifier,
            channel,
            source_revision_id,
            {position - 1 for position in positions},
        )

    def _raw_window(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        low: int,
        high: int,
        count: int,
    ) -> list[Mapping[str, Any]]:
        """The unit's ``count`` Raw rows at positions ``low … high``, in position order.

        ``_positions`` proved the unit's positions distinct, ``count`` of them in the range; the
        read is capped at ``count + 1`` rows, and any other number of rows there fails closed.
        """
        offset = 0 if channel.name == "archive" else 1
        columns = tuple(field.name for field in channel.element.arrow_schema)
        rows: list[Mapping[str, Any]] = pin.catalog.scan_columns(
            channel.element.table,
            columns=columns,
            row_filter=And(
                _equals(channel.lineage_column, source_revision_id),
                _between(_position_column(channel), low - offset, high - offset),
            ),
            limit=count + 1,
        ).to_pylist()
        if len(rows) != count:
            raise CatalogIntegrityError(
                f"{channel.element.table}: unit {source_revision_id} holds {len(rows)} Raw rows "
                f"at positions {low} … {high}, not the {count} its positions name"
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
        found = pin.catalog.scan_columns(
            channel.source.table,
            columns=("revision_id",),
            row_filter=_equals("revision_id", source_revision_id),
            limit=2,
        ).to_pylist()
        if len(found) != 1:
            raise CanonicalNormalizeError(
                f"{source_revision_id} is not one committed revision of {channel.source.table}"
            )

    def _committed_plan(
        self, pin: _Pin, channel: rules.RawChannel, source_revision_id: str
    ) -> _CommittedPlan | None:
        """The plan the unit's batch ids record up to the pinned head; ``None`` if none.

        One ordered walk of the pinned history keeping three ints (E1-CAP-1): every batch id
        of the unit is well formed and of one lawful plan, and the batches are ``0 … count - 1``,
        each committed once and in order (``ordered_batches``).
        """
        table = channel.canonical.table
        plans: list[tuple[int, int]] = []
        count = 0
        for _ in self._plan_walk(pin, channel, source_revision_id, plans):
            count += 1
        if not plans:
            return None
        [(unit_rows, chunk)] = plans
        if unit_rows < 1 or not 1 <= chunk <= MAX_MICROBATCH_ROWS:
            raise CatalogIntegrityError(
                f"{table}: unit {source_revision_id} records no lawful plan"
            )
        return _CommittedPlan(unit_rows=unit_rows, chunk=chunk, count=count)

    def _plan_walk(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        plans: list[tuple[int, int]],
    ) -> Iterator[tuple[int, SnapshotInfo]]:
        """The unit's batches at the pinned head, newest first; ``plans`` gets their one plan."""
        table = channel.canonical.table
        prefix = _unit_prefix(source_revision_id)
        widths = (_ROWS_DIGITS, _CHUNK_DIGITS, _INDEX_DIGITS)

        def index_of(batch_id: str) -> int | None:
            if not batch_id.startswith(prefix):
                return None
            parts = batch_id[len(prefix) :].split(".")
            if len(parts) != 3 or any(
                len(part) != width or not part.isascii() or not part.isdigit()
                for part, width in zip(parts, widths, strict=True)
            ):
                raise CatalogIntegrityError(f"{table} has a malformed batch id {batch_id!r}")
            unit_rows, chunk, index = (int(part) for part in parts)
            if not plans:
                plans.append((unit_rows, chunk))
            elif plans[0] != (unit_rows, chunk):
                raise CatalogIntegrityError(
                    f"{table}: unit {source_revision_id} has batches of more than one plan"
                )
            return index

        def name_of(index: int) -> str:
            unit_rows, chunk = plans[0]
            return unit_batch_id(source_revision_id, unit_rows, chunk, index)

        return ordered_batches(
            history_from(self._adapter, table, pin.canonical_head),
            table,
            index_of,
            name_of,
            f"batches of unit {source_revision_id}",
        )

    def _plan_snapshots(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        plan: _CommittedPlan,
        wanted: Collection[int] | None,
    ) -> Iterator[tuple[int, SnapshotInfo]]:
        """``(index, snapshot)`` of the plan's committed batches (``wanted`` only, if given),
        newest first, streamed from the pinned history — never collected (E1-CAP-1)."""
        if wanted is not None and not wanted:
            return
        found = 0
        for index, snapshot in self._plan_walk(pin, channel, source_revision_id, []):
            if wanted is None or index in wanted:
                yield index, snapshot
                found += 1
                if wanted is not None and found == len(wanted):
                    return  # ``_committed_plan`` proved each index committed once

    def _any_row(self, pin: _Pin, table: str, column: str, row_filter: BooleanExpression) -> bool:
        """Whether ``row_filter`` matches a row at the pinned view (reads at most one)."""
        return bool(
            pin.catalog.scan_columns(
                table, columns=(column,), row_filter=row_filter, limit=1
            ).num_rows
        )

    def _has_unit_rows(self, pin: _Pin, channel: rules.RawChannel, source_revision_id: str) -> bool:
        return self._any_row(
            pin,
            channel.canonical.table,
            "arrival_seq",
            self._unit_filter(channel, source_revision_id),
        )

    def _recover(
        self, pin: _Pin, channel: rules.RawChannel, source_revision_id: str
    ) -> tuple[int, datetime]:
        """Block base and ready time of a unit with committed batches: from one of its rows.

        Never read from the clock. Every other committed row of the unit must then agree, which
        ``_check_unit_numbers`` proves batch by batch.
        """
        table = channel.canonical.table
        found = pin.catalog.scan_columns(
            table,
            columns=("arrival_seq", "knowledge_time"),
            row_filter=self._unit_filter(channel, source_revision_id),
            limit=1,
        ).to_pylist()
        if not found:
            raise CatalogIntegrityError(
                f"{table}: unit {source_revision_id} has committed batches but their rows are gone"
            )
        seq, ready = found[0]["arrival_seq"], found[0]["knowledge_time"]
        if not isinstance(seq, int) or ready is None:
            raise CatalogIntegrityError(_disagree(table))
        try:
            base = rules.check_block_base(
                (seq // rules.ARRIVAL_SEQ_STRIDE) * rules.ARRIVAL_SEQ_STRIDE
            )
        except rules.CanonicalRuleViolation as exc:
            raise CatalogIntegrityError(f"{table}: {exc}") from None
        return base, ready

    def _check_unit_numbers(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        positions: Sequence[int],
        chunk: int,
        batches: int,
        base: int,
        ready: datetime,
    ) -> None:
        """The unit's committed rows are exactly ``base + position`` of its first ``batches``
        batches, all at ``ready``: tiles of whole batches, at most ``narrow_rows`` (or one batch)
        each, of ``arrival_seq`` / ``knowledge_time`` (each read capped at its tile + 1 row),
        then a one-row probe for any row of the unit outside the tiles."""
        table = channel.canonical.table
        unit = self._unit_filter(channel, source_revision_id)
        extra = (
            f"{table}: the committed rows of unit {source_revision_id} are not exactly the rows "
            "of its committed batches (rows deleted or added)"
        )
        for start, end, low, high in _tiles(positions, chunk, batches, self._narrow):
            found = pin.catalog.scan_columns(
                table,
                columns=("arrival_seq", "knowledge_time"),
                row_filter=And(unit, _between("arrival_seq", base + low, base + high)),
                limit=end - start + 1,
            )
            if pc.unique(found.column("knowledge_time")).to_pylist() not in ([], [ready]):
                raise CatalogIntegrityError(_disagree(table))
            if not _same_numbers(
                found.column("arrival_seq"), [base + p for p in positions[start:end]]
            ):
                raise CatalogIntegrityError(extra)
        covered = min(batches * chunk, len(positions))
        stray = pin.catalog.scan_columns(
            table,
            columns=("arrival_seq",),
            row_filter=And(unit, _outside(base + positions[0], base + positions[covered - 1])),
            limit=1,
        ).column("arrival_seq")
        if len(stray):
            seq = stray[0].as_py()
            if seq is None or not base <= seq < base + rules.ARRIVAL_SEQ_STRIDE:
                raise CatalogIntegrityError(_disagree(table))
            raise CatalogIntegrityError(extra)

    def _planned(
        self,
        channel: rules.RawChannel,
        raw_rows: Sequence[Mapping[str, Any]],
        base: int,
        ready: datetime,
    ) -> list[Mapping[str, Any]]:
        """The window's Canonical rows, in Raw position order, as the table stores them."""
        try:
            built = [
                rules.canonical_row(channel, row, base=base, ready_time=ready) for row in raw_rows
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
        base: int,
        ready: datetime,
        chunk: int,
    ) -> tuple[int, str | None]:
        """Commit the plan's missing batches in order, each read back at its own snapshot.

        The committed batches are the plan's prefix ``0 … count - 1`` (proven by the survey),
        so the missing ones are simply the rest: nothing per batch is kept (E1-CAP-1). Returns
        how many batches this call committed and the snapshot the unit was closed at.
        """
        definition = channel.canonical
        unit_rows = survey.unit_rows
        committed = 0
        parent = pin.canonical_head
        positions = survey.positions
        first = 0 if survey.plan is None else survey.plan.count
        for index in range(first, -(-unit_rows // chunk)):
            low, high, end = _batch_window(positions, chunk, index)
            batch_id = unit_batch_id(source_revision_id, unit_rows, chunk, index)
            planned = self._planned(
                channel,
                self._raw_window(pin, channel, source_revision_id, low, high, end - index * chunk),
                base,
                ready,
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
            parent = result.snapshot.snapshot_id
            self._read_back(channel, base, low, high, planned, parent)
            if result.outcome is CommitOutcome.COMMITTED:
                committed += 1
        self._close(channel, source_revision_id, base, positions, chunk, parent)
        return committed, parent

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
                self._adapter, channel, base + low, base + high, snapshot_id, len(planned)
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
        refused by every PIT proof of the unit it claims (G3-S; review E-2). The ids are looked
        up ``_UNIQUE_CHUNK`` at a time, each read capped at the chunk + 1 row (E1-CAP-1): exactly
        ``len(chunk)`` distinct ids among the rows found means each is held exactly once.
        """
        definition = channel.canonical
        column = _time_column(channel)
        times = [row[column] for row in planned]
        where = And(
            _equals("symbol", planned[0]["symbol"]), _between(column, min(times), max(times))
        )
        ids = [row["revision_id"] for row in planned]
        for start in range(0, len(ids), _UNIQUE_CHUNK):
            chunk = ids[start : start + _UNIQUE_CHUNK]
            found = catalog.scan_columns(
                definition.table,
                columns=("revision_id",),
                row_filter=And(where, In("revision_id", chunk)),  # type: ignore[call-arg, arg-type]
                limit=len(chunk) + 1,
                snapshot_id=snapshot_id,
            ).column("revision_id")
            if len(found) != len(chunk) or pc.count_distinct(found).as_py() != len(chunk):
                counts = pc.value_counts(found)
                held = {item["values"].as_py(): item["counts"].as_py() for item in counts}
                revision = next(item for item in chunk if held.get(item) != 1)
                raise CatalogIntegrityError(
                    f"{definition.table}: revision_id {revision} is not held by exactly one row"
                )

    def _close(
        self,
        channel: rules.RawChannel,
        source_revision_id: str,
        base: int,
        positions: Sequence[int],
        chunk: int,
        snapshot_id: str | None,
    ) -> None:
        """The unit's rows are exactly ``base + position`` of its Raw rows; nothing else is in
        the block.

        Tiles of whole batches, at most ``narrow_rows`` (or one batch) each, three narrow columns
        read capped at the tile + 1 row: exactly the tile's numbers, every row the unit's; then
        one-row probes for any row of the block, or of the unit, outside the tiles. Together: the
        unit's rows and the block's rows are both exactly ``base + position`` (E1-CAP-1: never
        the whole unit at once).
        """
        table = channel.canonical.table
        failure = (
            f"{table}: unit {source_revision_id} does not read back as exactly its "
            f"{len(positions)} numbers of block {base}"
        )
        batches = -(-len(positions) // chunk)
        for start, end, low, high in _tiles(positions, chunk, batches, self._narrow):
            found = self._adapter.scan_columns(
                table,
                columns=("arrival_seq", "lineage_source_revision_id", "lineage_raw_table"),
                row_filter=_between("arrival_seq", base + low, base + high),
                limit=end - start + 1,
                snapshot_id=snapshot_id,
            )
            if (
                not _same_numbers(
                    found.column("arrival_seq"), [base + p for p in positions[start:end]]
                )
                or pc.unique(found.column("lineage_source_revision_id")).to_pylist()
                != [source_revision_id]
                or pc.unique(found.column("lineage_raw_table")).to_pylist()
                != [channel.element.table]
            ):
                raise CatalogIntegrityError(failure)
        first, last = base + positions[0], base + positions[-1]
        for row_filter in (
            And(
                _from("arrival_seq", base, base + rules.ARRIVAL_SEQ_STRIDE),
                _outside(first, last),
            ),
            And(self._unit_filter(channel, source_revision_id), _outside(first, last)),
        ):
            if self._adapter.scan_columns(
                table,
                columns=("arrival_seq",),
                row_filter=row_filter,
                limit=1,
                snapshot_id=snapshot_id,
            ).num_rows:
                raise CatalogIntegrityError(failure)

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
        planned: int,
    ) -> list[Mapping[str, Any]]:
        """The block slice's rows, capped at ``planned + 1``: any more cannot all be planned
        (one is then a duplicate or foreign revision, which ``_exact`` refuses)."""
        definition = channel.canonical
        columns = tuple(field.name for field in definition.arrow_schema)
        rows: list[Mapping[str, Any]] = catalog.scan_columns(
            definition.table,
            columns=columns,
            row_filter=_between("arrival_seq", first, last),
            limit=planned + 1,
            snapshot_id=snapshot_id,
        ).to_pylist()
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
    own = set(own)
    if not own <= {element.element_index for element in elements}:
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
        for row in catalog.scan_columns(
            table,
            columns=columns,
            row_filter=And(
                symbol,
                And(
                    In("observation_key", keys),  # type: ignore[call-arg, arg-type]
                    In("revision_id", ids),  # type: ignore[call-arg, arg-type]
                ),
            ),
        ).to_pylist():
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
    wanted = pa.array(sorted({element.revision_id for element in missing}), type=pa.string())
    keys = sorted({element.observation_key for element in missing})
    found: dict[str, tuple[int, str, str]] = {}
    for start in range(0, len(keys), _KEY_CHUNK):
        chunk = catalog.scan_columns(
            table,
            columns=("revision_id", "response_revision_id", "observation_key"),
            row_filter=And(
                symbol,
                In("observation_key", keys[start : start + _KEY_CHUNK]),  # type: ignore[call-arg, arg-type]
            ),
        )
        chunk = chunk.filter(pc.is_in(chunk.column("revision_id"), value_set=wanted))
        for revision, lineage, key in zip(
            chunk.column("revision_id").to_pylist(),
            chunk.column("response_revision_id").to_pylist(),
            chunk.column("observation_key").to_pylist(),
            strict=True,
        ):
            count = found.get(revision, (0, lineage, key))[0]
            found[revision] = (count + 1, lineage, key)
    return found


def _require_complete(
    channel: rules.RawChannel, source_revision_id: str, plan: _CommittedPlan | None
) -> None:
    """A reader takes a normalized unit only whole: every batch of its plan committed (RT-1).

    A unit with no committed batch is simply not normalized (it has no rows to read); a
    committed prefix of the plan is a normalization that stopped half-way, never a smaller unit.
    """
    if plan is None:
        return
    if plan.count != plan.planned:
        raise CanonicalUnitIncomplete(
            f"{channel.canonical.table}: unit {source_revision_id} has {plan.count} of the "
            f"{plan.planned} batches of its plan committed: its normalization stopped half-way "
            "(rerun the normalizer)"
        )


def _position_column(channel: rules.RawChannel) -> str:
    return "archive_line_number" if channel.name == "archive" else "element_index"


def _time_column(channel: rules.RawChannel) -> str:
    """The Canonical table's partitioning time column."""
    return "event_time" if channel.data_type == "agg_trades" else "interval_start"


def _proof_windows(positions: Sequence[int], size: int) -> Iterator[tuple[int, int, int]]:
    """``(low, high, count)``: disjoint position ranges covering every position, each holding
    ``count`` of them — ``<= size`` unless one position repeats (then never split)."""
    start = 0
    while start < len(positions):
        low = positions[start]
        end = min(start + size, len(positions))
        high = positions[end - 1]
        while end < len(positions) and positions[end] == high:
            end += 1  # never split rows sharing a position across windows
        yield low, high, end - start
        start = end


def _tiles(
    positions: Sequence[int], chunk: int, batches: int, span: int
) -> Iterator[tuple[int, int, int, int]]:
    """Tiles of whole batches ``< batches``: ranks ``[start, end)`` and the position range each
    answers for; a tile holds ``max(1, span // chunk)`` batches (at most ``max(span, chunk)``
    rows, whatever the unit's size).

    The range runs from the tile's first position up to just below the next tile's first, the
    last one to its own last position, so the ranges tile ``positions[0] … positions[covered-1]``
    with no hole: a row at a position no Raw row has (a REST gap) still falls in some tile.
    """
    covered = min(batches * chunk, len(positions))
    step = max(1, span // chunk) * chunk
    for start in range(0, covered, step):
        end = min(start + step, covered)
        high = positions[end] - 1 if end < covered else positions[covered - 1]
        yield start, end, positions[start], high


def _outside(first: int, last: int) -> BooleanExpression:
    """``arrival_seq < first or arrival_seq > last``."""
    return Or(
        LessThan("arrival_seq", first),  # type: ignore[call-arg, arg-type]
        GreaterThan("arrival_seq", last),  # type: ignore[call-arg, arg-type]
    )


def _disagree(table: str) -> str:
    return f"{table}: the committed rows of one unit disagree on their block base or knowledge_time"


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


def _same_numbers(values: pa.Array, expected: Sequence[int]) -> bool:
    """``values`` is exactly the ascending, distinct ``expected``, each once (any order)."""
    if len(values) != len(expected):
        return False
    if not expected:
        return True
    if values.null_count:
        return False
    ordered = pc.take(values, pc.sort_indices(values))
    if isinstance(ordered, pa.ChunkedArray):
        ordered = ordered.combine_chunks()
    return bool(ordered.equals(pa.array(expected, type=ordered.type)))


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

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
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.compute as pc  # type: ignore[import-untyped]
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
from infrastructure.revision.store import BatchCommit, RevisionCatalog

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
    """What the unit's committed batch ids say about the plan that wrote them."""

    unit_rows: int
    chunk: int
    batches: Mapping[int, SnapshotInfo]


@dataclass(frozen=True, slots=True)
class CanonicalUnitNormalized:
    """Result of normalizing one unit."""

    raw_table: str
    source_revision_id: str
    canonical_table: str
    #: ``None`` for a unit without element revisions (e.g. an empty REST page).
    arrival_seq_base: int | None
    knowledge_time: datetime | None
    #: Canonical revision ids in Raw position order.
    revision_ids: tuple[str, ...]
    commits: tuple[BatchCommit, ...]

    @property
    def replayed(self) -> bool:
        return all(commit.replayed for commit in self.commits)


@dataclass(frozen=True, slots=True)
class _Pin:
    """One call's fixed view: every read goes through ``catalog``."""

    catalog: PinnedCatalogView
    canonical_head: str | None
    verifier: PersistedRowVerifier


@dataclass(frozen=True, slots=True)
class _UnitFacts:
    """A unit's proven unit-wide facts (no Raw or Canonical rows)."""

    positions: tuple[int, ...]
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
    #: The committed rows, window by window (only collected when asked for).
    committed_rows: tuple[Mapping[str, Any], ...]
    #: Revision ids of the committed windows, in position order.
    committed_ids: tuple[str, ...]
    #: The unit's Raw positions, ascending and distinct (batches are rank slices of them).
    positions: tuple[int, ...] = ()


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
    ) -> None:
        if not isinstance(microbatch_rows, int) or isinstance(microbatch_rows, bool):
            raise CanonicalNormalizeError("microbatch_rows must be an int")
        if not 1 <= microbatch_rows <= MAX_MICROBATCH_ROWS:
            raise CanonicalNormalizeError(
                f"microbatch_rows must be between 1 and {MAX_MICROBATCH_ROWS}"
            )
        self._adapter = adapter
        self._storage = storage
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

    # ------------------------------------------------------------------ entry points

    def normalize_unit(self, raw_table: str, source_revision_id: str) -> CanonicalUnitNormalized:
        channel = self._channel(raw_table, source_revision_id)
        last_error: Exception | None = None
        for _ in range(_ATTEMPTS):
            pin = self._pin(channel, source_revision_id)
            survey = self._survey(pin, channel, source_revision_id, keep_rows=False)
            if survey.unit_rows == 0:
                return CanonicalUnitNormalized(
                    raw_table=raw_table,
                    source_revision_id=source_revision_id,
                    canonical_table=channel.canonical.table,
                    arrival_seq_base=None,
                    knowledge_time=None,
                    revision_ids=(),
                    commits=(),
                )
            if survey.plan is None:
                assert survey.raw_floor is not None
                base, ready = self._allocate(channel, survey.raw_floor)
                chunk = self._microbatch
            else:
                assert survey.base is not None and survey.ready is not None
                base, ready, chunk = survey.base, survey.ready, survey.plan.chunk
            try:
                commits, revision_ids = self._write(
                    pin, channel, source_revision_id, survey, base, ready, chunk
                )
            except CommitConflict as exc:
                last_error = exc  # another writer moved the table: start over, adopt its work
                continue
            except BatchConflict as exc:
                if survey.plan is None:
                    # A rival allocated first (its own block and clock reading) and committed
                    # this batch id: start over and adopt its plan — the clock is never read again.
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
                revision_ids=revision_ids,
                commits=tuple(commits),
            )
        raise CanonicalNormalizeConflict(
            f"unit {source_revision_id} of {raw_table} lost {_ATTEMPTS} commit races"
        ) from last_error

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
            _require_complete(channel, source_revision_id, survey.plan, survey.unit_rows)
            return survey.committed_rows
        return self._verify_batches(pin, channel, source_revision_id, frozenset(arrival_seqs))

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
        _require_complete(channel, source_revision_id, plan, len(facts.positions))
        wanted = _batches_holding(facts.positions, plan.chunk, base, seqs) & set(plan.batches)
        kept: list[Mapping[str, Any]] = []
        for index in sorted(wanted):
            key = (channel.element.table, source_revision_id, index)
            proven = self._batches.get(key) if self._frozen else None
            if proven is not None:
                kept.extend(proven)
                continue
            low, high, _ = _batch_window(facts.positions, plan.chunk, index)
            raw = self._raw_window(pin, channel, source_revision_id, low, high)
            self._prove(pin, channel, raw)
            planned = self._planned(channel, raw, base, ready)
            check_batch_snapshot(
                channel.canonical,
                unit_batch_id(source_revision_id, plan.unit_rows, plan.chunk, index),
                plan.batches[index],
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
        """Every unit-wide fact, from narrow columns; kept per unit on an immutable view."""
        key = (channel.element.table, source_revision_id)
        cached = self._facts.get(key) if self._frozen else None
        if cached is not None:
            return cached
        table = channel.canonical.table
        positions, symbol = self._positions(pin, channel, source_revision_id)
        if not positions:
            self._prove_source(pin, channel, source_revision_id)
        self._check_positions(pin, channel, source_revision_id, positions, symbol)
        plan = self._committed_plan(pin, channel, source_revision_id)
        seqs, readies = self._committed_times(pin, channel, source_revision_id)
        facts = _UnitFacts(tuple(positions), None, None, None)
        if not positions:
            if len(seqs) or plan is not None:
                raise CatalogIntegrityError(
                    f"{table} holds Canonical rows or batches of unit {source_revision_id} that "
                    "has no Raw element revision"
                )
        elif plan is None:
            if len(seqs):
                raise CatalogIntegrityError(
                    f"{table}: unit {source_revision_id} has committed rows but no committed batch"
                )
        else:
            base, ready = self._recover(channel, source_revision_id, seqs, readies)
            if plan.unit_rows != len(positions):
                raise CatalogIntegrityError(
                    f"{table}: unit {source_revision_id} was normalized as {plan.unit_rows} rows "
                    f"but its Raw unit now has {len(positions)}: the Raw unit changed"
                )
            indices = sorted(plan.batches)
            if indices != list(range(len(indices))):
                raise CatalogIntegrityError(
                    f"{table}: the batches of unit {source_revision_id} are not a contiguous prefix"
                )
            if indices and indices[-1] * plan.chunk >= len(positions):
                raise CatalogIntegrityError(
                    f"{table}: batch {indices[-1]} of unit {source_revision_id} lies beyond its "
                    "plan"
                )
            covered = min(len(indices) * plan.chunk, len(positions))
            if not _same_numbers(seqs, [base + position for position in positions[:covered]]):
                raise CatalogIntegrityError(
                    f"{table}: the committed rows of unit {source_revision_id} are not exactly "
                    "the rows of its committed batches (rows deleted or added)"
                )
            facts = _UnitFacts(tuple(positions), plan, base, ready)
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
        table = channel.canonical.table
        positions, symbol = self._positions(pin, channel, source_revision_id)
        floor: datetime | None = None
        for low, high in _proof_windows(positions, self._microbatch):
            raw = self._raw_window(pin, channel, source_revision_id, low, high)
            self._prove(pin, channel, raw)
            latest = max(row["knowledge_time"] for row in raw)
            floor = latest if floor is None else max(floor, latest)
        unit_rows = len(positions)
        if unit_rows == 0:
            self._prove_source(pin, channel, source_revision_id)
        self._check_positions(pin, channel, source_revision_id, positions, symbol)
        plan = self._committed_plan(pin, channel, source_revision_id)
        seqs, readies = self._committed_times(pin, channel, source_revision_id)
        if unit_rows == 0:
            if len(seqs) or plan is not None:
                raise CatalogIntegrityError(
                    f"{table} holds Canonical rows or batches of unit {source_revision_id} that "
                    "has no Raw element revision"
                )
            return _Survey(0, None, None, None, None, (), (), ())
        if plan is None:
            if len(seqs):
                raise CatalogIntegrityError(
                    f"{table}: unit {source_revision_id} has committed rows but no committed batch"
                )
            return _Survey(unit_rows, floor, None, None, None, (), (), tuple(positions))
        base, ready = self._recover(channel, source_revision_id, seqs, readies)
        if plan.unit_rows != unit_rows:
            raise CatalogIntegrityError(
                f"{table}: unit {source_revision_id} was normalized as {plan.unit_rows} rows but "
                f"its Raw unit now has {unit_rows}: the Raw unit changed"
            )
        indices = sorted(plan.batches)
        if indices != list(range(len(indices))):
            raise CatalogIntegrityError(
                f"{table}: the batches of unit {source_revision_id} are not a contiguous prefix"
            )
        if indices and indices[-1] * plan.chunk >= unit_rows:
            raise CatalogIntegrityError(
                f"{table}: batch {indices[-1]} of unit {source_revision_id} lies beyond its plan"
            )
        kept: list[Mapping[str, Any]] = []
        ids: list[str] = []
        covered = min(len(indices) * plan.chunk, unit_rows)
        for index in indices:
            low, high, _ = _batch_window(positions, plan.chunk, index)
            raw = self._raw_window(pin, channel, source_revision_id, low, high)
            planned = self._planned(channel, raw, base, ready)
            check_batch_snapshot(
                channel.canonical,
                unit_batch_id(source_revision_id, plan.unit_rows, plan.chunk, index),
                plan.batches[index],
                planned,
            )
            self._check_committed_window(pin, channel, base, low, high, planned)
            if not keep_rows:
                # A replay re-checks what its first run read back (E2 of review E).
                self._check_unique(pin.catalog, channel, planned, None)
            ids.extend(row["revision_id"] for row in planned)
            if keep_rows:
                kept.extend(planned)
        if not _same_numbers(seqs, [base + position for position in positions[:covered]]):
            raise CatalogIntegrityError(
                f"{table}: the committed rows of unit {source_revision_id} are not exactly the "
                "rows of its committed batches (rows deleted or added)"
            )
        return _Survey(
            unit_rows, floor, plan, base, ready, tuple(kept), tuple(ids), tuple(positions)
        )

    def _positions(
        self, pin: _Pin, channel: rules.RawChannel, source_revision_id: str
    ) -> tuple[list[int], str | None]:
        """Sorted Raw positions of the unit (one int per row; duplicates kept) and its symbol."""
        column = _position_column(channel)
        found = pin.catalog.scan_columns(
            channel.element.table,
            columns=(column, "symbol"),
            row_filter=_equals(channel.lineage_column, source_revision_id),
        )
        values = found.column(column)
        if values.null_count:
            raise CatalogIntegrityError(f"{channel.element.table}: a Raw position is null")
        offset = 0 if channel.name == "archive" else 1
        symbol = found.column("symbol")[0].as_py() if found.num_rows else None
        return sorted(value + offset for value in values.to_pylist()), symbol

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
            {position - 1 for position in positions},
        )

    def _raw_window(
        self,
        pin: _Pin,
        channel: rules.RawChannel,
        source_revision_id: str,
        low: int,
        high: int,
    ) -> list[Mapping[str, Any]]:
        """The unit's Raw rows at positions ``low … high``, in position order."""
        offset = 0 if channel.name == "archive" else 1
        columns = tuple(field.name for field in channel.element.arrow_schema)
        rows: list[Mapping[str, Any]] = pin.catalog.scan_columns(
            channel.element.table,
            columns=columns,
            row_filter=And(
                _equals(channel.lineage_column, source_revision_id),
                _between(_position_column(channel), low - offset, high - offset),
            ),
        ).to_pylist()
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
        ).to_pylist()
        if len(found) != 1:
            raise CanonicalNormalizeError(
                f"{source_revision_id} is not one committed revision of {channel.source.table}"
            )

    def _committed_plan(
        self, pin: _Pin, channel: rules.RawChannel, source_revision_id: str
    ) -> _CommittedPlan | None:
        """The plan the unit's batch ids record up to the pinned head; ``None`` if none."""
        table = channel.canonical.table
        prefix = _unit_prefix(source_revision_id)
        plans: set[tuple[int, int]] = set()
        batches: dict[int, list[SnapshotInfo]] = {}
        for snapshot in history_from(self._adapter, table, pin.canonical_head):
            batch_id = snapshot.batch_id
            if batch_id is None or not batch_id.startswith(prefix):
                continue
            parts = batch_id[len(prefix) :].split(".")
            widths = (_ROWS_DIGITS, _CHUNK_DIGITS, _INDEX_DIGITS)
            if len(parts) != 3 or any(
                len(part) != width or not part.isascii() or not part.isdigit()
                for part, width in zip(parts, widths, strict=True)
            ):
                raise CatalogIntegrityError(f"{table} has a malformed batch id {batch_id!r}")
            unit_rows, chunk, index = (int(part) for part in parts)
            plans.add((unit_rows, chunk))
            batches.setdefault(index, []).append(snapshot)
        if not plans:
            return None
        if len(plans) != 1:
            raise CatalogIntegrityError(
                f"{table}: unit {source_revision_id} has batches of more than one plan"
            )
        [(unit_rows, chunk)] = plans
        if unit_rows < 1 or not 1 <= chunk <= MAX_MICROBATCH_ROWS:
            raise CatalogIntegrityError(
                f"{table}: unit {source_revision_id} records no lawful plan"
            )
        for index, snapshots in batches.items():
            if len(snapshots) != 1:
                raise CatalogIntegrityError(
                    f"{table} has rows of batch "
                    f"{unit_batch_id(source_revision_id, unit_rows, chunk, index)} but "
                    f"{len(snapshots)} snapshots committing it"
                )
        return _CommittedPlan(
            unit_rows=unit_rows,
            chunk=chunk,
            batches={index: snapshots[0] for index, snapshots in sorted(batches.items())},
        )

    def _committed_times(
        self, pin: _Pin, channel: rules.RawChannel, source_revision_id: str
    ) -> tuple[pa.Array, pa.Array]:
        """``arrival_seq`` and ``knowledge_time`` of every committed row of the unit (Arrow)."""
        found = pin.catalog.scan_columns(
            channel.canonical.table,
            columns=("arrival_seq", "knowledge_time"),
            row_filter=self._unit_filter(channel, source_revision_id),
        )
        return found.column("arrival_seq"), found.column("knowledge_time")

    def _recover(
        self,
        channel: rules.RawChannel,
        source_revision_id: str,
        seqs: pa.Array,
        readies: pa.Array,
    ) -> tuple[int, datetime]:
        """Block base and ready time of a unit with committed batches: from its rows, never read."""
        table = channel.canonical.table
        if not len(seqs):
            raise CatalogIntegrityError(
                f"{table}: unit {source_revision_id} has committed batches but their rows are gone"
            )
        bounds = pc.min_max(seqs).as_py()
        low, high = bounds["min"], bounds["max"]
        base = (low // rules.ARRIVAL_SEQ_STRIDE) * rules.ARRIVAL_SEQ_STRIDE
        distinct = pc.unique(readies).to_pylist()
        if high >= base + rules.ARRIVAL_SEQ_STRIDE or len(distinct) != 1 or distinct[0] is None:
            raise CatalogIntegrityError(
                f"{table}: the committed rows of one unit disagree on their block base or "
                "knowledge_time"
            )
        try:
            return rules.check_block_base(base), distinct[0]
        except rules.CanonicalRuleViolation as exc:
            raise CatalogIntegrityError(f"{table}: {exc}") from None

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
            self._scan_block(pin.catalog, channel, base + low, base + high, None),
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
    ) -> tuple[list[BatchCommit], tuple[str, ...]]:
        """Commit the plan's missing batches in order, each read back at its own snapshot."""
        definition = channel.canonical
        unit_rows = survey.unit_rows
        done = {} if survey.plan is None else dict(survey.plan.batches)
        parent = pin.canonical_head
        commits: list[BatchCommit] = []
        ids: list[str] = list(survey.committed_ids)
        positions = survey.positions
        for index in range(-(-unit_rows // chunk)):
            low, high, end = _batch_window(positions, chunk, index)
            batch_id = unit_batch_id(source_revision_id, unit_rows, chunk, index)
            if index in done:
                commits.append(
                    BatchCommit(
                        table=definition.table,
                        batch_id=batch_id,
                        snapshot_id=done[index].snapshot_id,
                        outcome=CommitOutcome.ALREADY_COMMITTED,
                        row_count=end - index * chunk,
                    )
                )
                continue
            planned = self._planned(
                channel,
                self._raw_window(pin, channel, source_revision_id, low, high),
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
            ids.extend(row["revision_id"] for row in planned)
            commits.append(
                BatchCommit(
                    table=definition.table,
                    batch_id=batch_id,
                    snapshot_id=parent,
                    outcome=result.outcome,
                    row_count=len(planned),
                )
            )
        self._close(channel, source_revision_id, base, positions, parent)
        return commits, tuple(ids)

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
            self._scan_block(self._adapter, channel, base + low, base + high, snapshot_id),
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
        found = catalog.scan_columns(
            definition.table,
            columns=("revision_id",),
            row_filter=And(
                _equals("symbol", planned[0]["symbol"]),
                _between(column, min(times), max(times)),
            ),
            snapshot_id=snapshot_id,
        ).column("revision_id")
        counts = pc.value_counts(found)
        held = {item["values"].as_py(): item["counts"].as_py() for item in counts}
        for row in planned:
            if held.get(row["revision_id"]) != 1:
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
        seqs = self._adapter.scan_columns(
            table,
            columns=("arrival_seq",),
            row_filter=self._unit_filter(channel, source_revision_id),
            snapshot_id=snapshot_id,
        ).column("arrival_seq")
        block = self._adapter.scan_columns(
            table,
            columns=("arrival_seq",),
            row_filter=_from("arrival_seq", base, base + rules.ARRIVAL_SEQ_STRIDE),
            snapshot_id=snapshot_id,
        ).column("arrival_seq")
        expected = [base + position for position in positions]
        if not _same_numbers(seqs, expected) or not _same_numbers(block, expected):
            raise CatalogIntegrityError(
                f"{table}: unit {source_revision_id} does not read back as exactly its "
                f"{len(positions)} numbers of block {base}"
            )

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
    ) -> list[Mapping[str, Any]]:
        definition = channel.canonical
        columns = tuple(field.name for field in definition.arrow_schema)
        rows: list[Mapping[str, Any]] = catalog.scan_columns(
            definition.table,
            columns=columns,
            row_filter=_between("arrival_seq", first, last),
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
    if len(plan.batches) != planned:
        raise CanonicalUnitIncomplete(
            f"{channel.canonical.table}: unit {source_revision_id} has {len(plan.batches)} of the "
            f"{planned} batches of its plan committed: its normalization stopped half-way "
            "(rerun the normalizer)"
        )


def _position_column(channel: rules.RawChannel) -> str:
    return "archive_line_number" if channel.name == "archive" else "element_index"


def _time_column(channel: rules.RawChannel) -> str:
    """The Canonical table's partitioning time column."""
    return "event_time" if channel.data_type == "agg_trades" else "interval_start"


def _proof_windows(positions: Sequence[int], size: int) -> Iterator[tuple[int, int]]:
    """Disjoint position ranges covering every position, each spanning <= ``size`` rows."""
    start = 0
    while start < len(positions):
        low = positions[start]
        end = min(start + size, len(positions))
        high = positions[end - 1]
        while end < len(positions) and positions[end] == high:
            end += 1  # never split rows sharing a position across windows
        yield low, high
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

"""Canonical normalizer (Phase 1 E1; ADR-0023 §3 / §4 / §7, ADR-0028 §1 ~ §6).

One call normalizes one **unit**: all Raw element revisions of one Raw source revision (an archive
revision, or a REST response revision's own elements) of one data type, under normalizer
``hlens.canonical.binance-spot.normalizer@1.0.0``:

1. a pinned read — Raw element table, Raw source table and Canonical table heads identical before
   and after — of the unit's Raw rows and of the unit's already committed Canonical rows; every Raw
   row is proven by the shared ``PersistedRowVerifier`` (D3E-R2) first. A verdict (pass or
   integrity failure) drawn from a view whose heads moved is discarded and the read repeated;
2. the unit's block base and ready time: recovered from committed Canonical rows of the unit if
   any (they must all agree), otherwise a fresh block above the table's largest ``arrival_seq`` and
   **one** reading of the injected UTC clock, taken after every proof and before any commit, never
   before a Raw ``knowledge_time`` (refused, not raised);
3. one Canonical row per Raw row (``rules.canonical_row``) in Raw position order, deterministic
   microbatches ``<normalizer>@<version>.<source revision>.<unit rows>.<chunk>.<index>``; committed
   batches must carry
   exactly the planned rows (fingerprint, row count), partially committed ones fail closed,
   missing ones are committed with the expected parent snapshot;
4. read-back: the unit's rows, their revision ids and arrival numbers are committed exactly once.

A unit whose Raw rows changed after it was first normalized (so a committed batch no longer
matches its plan) fails closed: normalize a Raw unit only after its ingest returned. Nothing is
repaired or rewritten; there is no journal or sidecar. The normalizer never reads the Raw evidence
table: cross-channel edges are mapped at PIT time (ADR-0028 §3.2).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from pyiceberg.expressions import And, BooleanExpression, EqualTo, In

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
from infrastructure.catalog.definitions import RegisteredTableDefinition
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.revision.row_integrity import (
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
    "CanonicalUnitNormalized",
    "unit_batch_id",
]

DEFAULT_MICROBATCH_ROWS: Final = 25_000
MAX_MICROBATCH_ROWS: Final = 250_000
_ATTEMPTS: Final = 8
_KEY_CHUNK: Final = 256
_ZERO: Final = timedelta(0)
_ROWS_DIGITS: Final = 10
_CHUNK_DIGITS: Final = 6
_INDEX_DIGITS: Final = 8


class CanonicalNormalizeError(Exception):
    """Base class of normalizer failures that must not be papered over."""


class CanonicalNormalizeConflict(CanonicalNormalizeError):
    """The unit cannot be normalized honestly now (clock, contention); nothing is committed."""


def _equals(column: str, value: object) -> BooleanExpression:
    return EqualTo(column, value)  # type: ignore[call-arg, arg-type]


def _member(column: str, values: Iterable[object]) -> BooleanExpression:
    return In(column, set(values))  # type: ignore[call-arg, arg-type]


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
class _Pinned:
    canonical_head: str | None
    raw_rows: tuple[Mapping[str, Any], ...]
    committed: tuple[Mapping[str, Any], ...]


class CanonicalNormalizer:
    """Appends Canonical revisions for verified Raw units; idempotent and re-runnable.

    One writer per Canonical table (ADR-0023 §7); concurrent writers are tolerated: a lost
    expected-parent race restarts the unit from a fresh pinned read, so a unit another writer
    finished is adopted (its base and ready time), never written twice.
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
        self._clock = clock or (lambda: datetime.now(UTC))
        self._microbatch = microbatch_rows
        self._verifier = PersistedRowVerifier(adapter, storage)

    # ------------------------------------------------------------------ entry point

    def normalize_unit(self, raw_table: str, source_revision_id: str) -> CanonicalUnitNormalized:
        channel = self._channel(raw_table, source_revision_id)
        last_error: Exception | None = None
        for _ in range(_ATTEMPTS):
            pinned = self._pinned_read(channel, source_revision_id)
            plan = self._committed_plan(channel, source_revision_id, pinned.canonical_head)
            if not pinned.raw_rows:
                self._check_empty_unit(channel, source_revision_id, pinned, plan)
                return CanonicalUnitNormalized(
                    raw_table=raw_table,
                    source_revision_id=source_revision_id,
                    canonical_table=channel.canonical.table,
                    arrival_seq_base=None,
                    knowledge_time=None,
                    revision_ids=(),
                    commits=(),
                )
            if plan is None:
                if pinned.committed:
                    raise CatalogIntegrityError(
                        f"{channel.canonical.table}: unit {source_revision_id} has committed "
                        "rows but no committed batch"
                    )
                base, ready = self._allocate(channel, pinned)
                chunk = self._microbatch
            else:
                base, ready = self._recover(channel, source_revision_id, pinned, plan)
                chunk = plan.chunk
            planned = self._plan(channel, pinned, base, ready)
            self._check_committed_batches(channel, source_revision_id, pinned, plan, planned)
            try:
                commits = self._commit(channel, source_revision_id, pinned, plan, planned, chunk)
            except CommitConflict as exc:
                last_error = exc  # another writer moved the table: start over, adopt its work
                continue
            except BatchConflict as exc:
                if plan is None:
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
            self._verify_readback(channel, source_revision_id, planned)
            return CanonicalUnitNormalized(
                raw_table=raw_table,
                source_revision_id=source_revision_id,
                canonical_table=channel.canonical.table,
                arrival_seq_base=base,
                knowledge_time=planned[0]["knowledge_time"],
                revision_ids=tuple(row["revision_id"] for row in planned),
                commits=tuple(commits),
            )
        raise CanonicalNormalizeConflict(
            f"unit {source_revision_id} of {raw_table} lost {_ATTEMPTS} commit races"
        ) from last_error

    def verify_unit(self, raw_table: str, source_revision_id: str) -> tuple[Mapping[str, Any], ...]:
        """The unit's committed Canonical rows, proven; nothing is written and no clock is read.

        The committed batch ids give the plan (unit size, microbatch size). Every Raw row of
        the unit is proven, the committed rows must be exactly what their Raw rows normalize to
        under the recovered block base and ready time, the committed batches must be a
        contiguous prefix of that plan with their exact fingerprints, and the committed rows must
        be exactly those batches' rows. Run on a catalog view pinned to a manifest's snapshots,
        it proves what those snapshots held (Phase 1 F1).
        """
        channel = self._channel(raw_table, source_revision_id)
        pinned = self._pinned_read(channel, source_revision_id)
        plan = self._committed_plan(channel, source_revision_id, pinned.canonical_head)
        if not pinned.raw_rows:
            self._check_empty_unit(channel, source_revision_id, pinned, plan)
            return ()
        if plan is None:
            if pinned.committed:
                raise CatalogIntegrityError(
                    f"{channel.canonical.table}: unit {source_revision_id} has committed rows "
                    "but no committed batch"
                )
            return ()
        base, ready = self._recover(channel, source_revision_id, pinned, plan)
        planned = self._plan(channel, pinned, base, ready)
        self._check_committed_batches(channel, source_revision_id, pinned, plan, planned)
        committed = {row["revision_id"] for row in pinned.committed}
        return tuple(row for row in planned if row["revision_id"] in committed)

    # ------------------------------------------------------------------ the committed plan

    def _channel(self, raw_table: str, source_revision_id: str) -> rules.RawChannel:
        try:
            channel = rules.raw_channel_of(raw_table)
        except rules.CanonicalRuleViolation as exc:
            raise CanonicalNormalizeError(str(exc)) from None
        if not isinstance(source_revision_id, str) or not source_revision_id:
            raise CanonicalNormalizeError("source_revision_id must be a non-empty string")
        return channel

    def _committed_plan(
        self, channel: rules.RawChannel, source_revision_id: str, head: str | None
    ) -> _CommittedPlan | None:
        """The plan the unit's batch ids record up to the pinned ``head``; ``None`` if none."""
        table = channel.canonical.table
        prefix = _unit_prefix(source_revision_id)
        plans: set[tuple[int, int]] = set()
        batches: dict[int, list[SnapshotInfo]] = {}
        for snapshot in history_from(self._adapter, table, head):
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

    def _check_empty_unit(
        self,
        channel: rules.RawChannel,
        source_revision_id: str,
        pinned: _Pinned,
        plan: _CommittedPlan | None,
    ) -> None:
        if pinned.committed or plan is not None:
            raise CatalogIntegrityError(
                f"{channel.canonical.table} holds Canonical rows or batches of unit "
                f"{source_revision_id} that has no Raw element revision"
            )

    def _check_committed_batches(
        self,
        channel: rules.RawChannel,
        source_revision_id: str,
        pinned: _Pinned,
        plan: _CommittedPlan | None,
        planned: Sequence[Mapping[str, Any]],
    ) -> None:
        """Committed batches = a contiguous prefix of the recorded plan, exact; rows = theirs."""
        if plan is None:
            return
        table = channel.canonical.table
        if plan.unit_rows != len(planned):
            raise CatalogIntegrityError(
                f"{table}: unit {source_revision_id} was normalized as {plan.unit_rows} rows but "
                f"its Raw unit now has {len(planned)}: the Raw unit changed"
            )
        indices = sorted(plan.batches)
        if indices != list(range(len(indices))):
            raise CatalogIntegrityError(
                f"{table}: the batches of unit {source_revision_id} are not a contiguous prefix"
            )
        covered: set[str] = set()
        for index in indices:
            rows = planned[index * plan.chunk : (index + 1) * plan.chunk]
            if not rows:
                raise CatalogIntegrityError(
                    f"{table}: batch {index} of unit {source_revision_id} lies beyond its plan"
                )
            check_batch_snapshot(
                channel.canonical,
                unit_batch_id(source_revision_id, plan.unit_rows, plan.chunk, index),
                plan.batches[index],
                rows,
            )
            covered.update(row["revision_id"] for row in rows)
        committed = {row["revision_id"] for row in pinned.committed}
        if committed != covered:
            raise CatalogIntegrityError(
                f"{table}: the committed rows of unit {source_revision_id} are not exactly the "
                "rows of its committed batches (rows deleted or added)"
            )

    # ------------------------------------------------------------------ pinned read

    def _pinned_read(self, channel: rules.RawChannel, source_revision_id: str) -> _Pinned:
        tables = (channel.element.table, channel.source.table, channel.canonical.table)
        for _ in range(_ATTEMPTS):
            heads = tuple(self._head(table) for table in tables)
            try:
                raw_rows = self._scan(
                    channel.element, _equals(channel.lineage_column, source_revision_id)
                )
                self._prove(channel, source_revision_id, raw_rows)
                committed = self._scan(
                    channel.canonical,
                    And(
                        _equals("lineage_source_revision_id", source_revision_id),
                        _equals("lineage_raw_table", channel.element.table),
                    ),
                )
            except CatalogIntegrityError:
                if tuple(self._head(table) for table in tables) == heads:
                    raise  # judged on one fixed view: the failure stands
                continue
            if tuple(self._head(table) for table in tables) == heads:
                return _Pinned(
                    canonical_head=heads[2],
                    raw_rows=tuple(raw_rows),
                    committed=tuple(committed),
                )
        raise CanonicalNormalizeConflict(
            f"{' / '.join(tables)} kept moving: no pinned read of unit {source_revision_id}"
        )

    def _prove(
        self,
        channel: rules.RawChannel,
        source_revision_id: str,
        raw_rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Every Raw row of the unit is proven down to its source revision (D3E-R2)."""
        if not raw_rows:
            found = self._adapter.scan_columns(
                channel.source.table,
                columns=("revision_id",),
                row_filter=_equals("revision_id", source_revision_id),
            ).to_pylist()
            if len(found) != 1:
                raise CanonicalNormalizeError(
                    f"{source_revision_id} is not one committed revision of {channel.source.table}"
                )
            return
        if channel.name == "archive":
            self._verifier.verify_archive_elements(
                channel.element, channel.data_type, raw_rows[0]["symbol"], raw_rows
            )
        else:
            self._verifier.verify_rest_elements(channel.element, channel.data_type, raw_rows)

    # ------------------------------------------------------------------ plan (no writes)

    def _recover(
        self,
        channel: rules.RawChannel,
        source_revision_id: str,
        pinned: _Pinned,
        plan: _CommittedPlan,
    ) -> tuple[int, datetime]:
        """Block base and ready time of a unit with committed batches: from its rows, never read."""
        table = channel.canonical.table
        if not pinned.committed:
            raise CatalogIntegrityError(
                f"{table}: unit {source_revision_id} has committed batches but their rows are gone"
            )
        positions = {row["revision_id"]: rules.position_of(channel, row) for row in pinned.raw_rows}
        bases: set[int] = set()
        readies: set[datetime] = set()
        for row in pinned.committed:
            position = positions.get(row["lineage_raw_revision_id"])
            if position is None:
                raise CatalogIntegrityError(
                    f"{table}: Canonical revision {row['revision_id']} names Raw revision "
                    f"{row['lineage_raw_revision_id']} that is not part of its unit"
                )
            bases.add(row["arrival_seq"] - position)
            readies.add(row["knowledge_time"])
        if len(bases) != 1 or len(readies) != 1:
            raise CatalogIntegrityError(
                f"{table}: the committed rows of one unit disagree on their block base or "
                "knowledge_time"
            )
        try:
            return rules.check_block_base(bases.pop()), readies.pop()
        except rules.CanonicalRuleViolation as exc:
            raise CatalogIntegrityError(f"{table}: {exc}") from None

    def _allocate(self, channel: rules.RawChannel, pinned: _Pinned) -> tuple[int, datetime]:
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
        floor = max(row["knowledge_time"] for row in pinned.raw_rows)
        if ready < floor:
            raise CanonicalNormalizeConflict(
                "the normalizer ready time precedes a Raw knowledge_time: refusing to backfill"
            )
        return base, ready

    def _plan(
        self,
        channel: rules.RawChannel,
        pinned: _Pinned,
        base: int,
        ready: datetime,
    ) -> list[Mapping[str, Any]]:
        """Normalised expected rows in Raw position order; committed rows must reproduce."""
        ordered = sorted(pinned.raw_rows, key=lambda row: rules.position_of(channel, row))
        try:
            built = [
                rules.canonical_row(channel, row, base=base, ready_time=ready) for row in ordered
            ]
        except rules.CanonicalRuleViolation as exc:
            raise CanonicalNormalizeError(f"unit cannot be normalized: {exc}") from None
        planned = batch_rows(batch(channel.canonical, built))
        by_id = {row["revision_id"]: row for row in planned}
        seen: set[str] = set()
        for row in pinned.committed:
            revision = row["revision_id"]
            if revision in seen:
                raise CatalogIntegrityError(
                    f"{channel.canonical.table}: Canonical revision {revision} is committed twice"
                )
            seen.add(revision)
            expected = by_id.get(revision)
            if expected is None:
                raise CatalogIntegrityError(
                    f"{channel.canonical.table}: committed Canonical revision {revision} is not "
                    "what its Raw row normalizes to"
                )
            mismatched = sorted(name for name, value in expected.items() if row[name] != value)
            if mismatched:
                raise CatalogIntegrityError(
                    f"{channel.canonical.table}: committed Canonical revision {revision} "
                    f"disagrees with its re-normalized row: {mismatched}"
                )
        return planned

    # ------------------------------------------------------------------ write

    def _commit(
        self,
        channel: rules.RawChannel,
        source_revision_id: str,
        pinned: _Pinned,
        plan: _CommittedPlan | None,
        planned: Sequence[Mapping[str, Any]],
        chunk: int,
    ) -> list[BatchCommit]:
        """Commit the plan's missing batches in order (committed ones were checked already)."""
        definition = channel.canonical
        unit_rows = len(planned)
        done = {} if plan is None else dict(plan.batches)
        parent = pinned.canonical_head
        commits: list[BatchCommit] = []
        for index, offset in enumerate(range(0, unit_rows, chunk)):
            rows = list(planned[offset : offset + chunk])
            batch_id = unit_batch_id(source_revision_id, unit_rows, chunk, index)
            if index in done:
                commits.append(
                    BatchCommit(
                        table=definition.table,
                        batch_id=batch_id,
                        snapshot_id=done[index].snapshot_id,
                        outcome=CommitOutcome.ALREADY_COMMITTED,
                        row_count=len(rows),
                    )
                )
                continue
            table = batch(definition, rows)
            request = CommitRequest(
                table=definition.table,
                batch_id=batch_id,
                batch_fingerprint=definition.fingerprint_rule.fingerprint(table),
                row_count=len(rows),
                expected_parent_snapshot_id=parent,
            )
            result = self._adapter.commit_batch(request, table)
            parent = result.snapshot.snapshot_id
            commits.append(
                BatchCommit(
                    table=definition.table,
                    batch_id=batch_id,
                    snapshot_id=parent,
                    outcome=result.outcome,
                    row_count=len(rows),
                )
            )
        return commits

    def _verify_readback(
        self,
        channel: rules.RawChannel,
        source_revision_id: str,
        planned: Sequence[Mapping[str, Any]],
    ) -> None:
        """Each planned row is committed once, exactly; its id and arrival number are its own."""
        definition = channel.canonical
        rows = self._scan(
            definition,
            And(
                _equals("lineage_source_revision_id", source_revision_id),
                _equals("lineage_raw_table", channel.element.table),
            ),
        )
        expected = {row["revision_id"]: row for row in planned}
        if sorted(row["revision_id"] for row in rows) != sorted(expected):
            raise CatalogIntegrityError(
                f"{definition.table}: unit {source_revision_id} reads back with other revisions"
            )
        for row in rows:
            mismatched = sorted(
                name for name, value in expected[row["revision_id"]].items() if row[name] != value
            )
            if mismatched:
                raise CatalogIntegrityError(
                    f"{definition.table}: {row['revision_id']} reads back differently: {mismatched}"
                )
        for column in ("revision_id", "arrival_seq"):
            values = sorted(row[column] for row in planned)
            counts: dict[Any, int] = {}
            for offset in range(0, len(values), _KEY_CHUNK):
                found = self._adapter.scan_columns(
                    definition.table,
                    columns=(column,),
                    row_filter=_member(column, values[offset : offset + _KEY_CHUNK]),
                ).to_pylist()
                for item in found:
                    counts[item[column]] = counts.get(item[column], 0) + 1
            wrong = [value for value in values if counts.get(value) != 1]
            if wrong:
                raise CatalogIntegrityError(
                    f"{definition.table}: {column} {wrong[0]} is not held by exactly one row"
                )

    # ------------------------------------------------------------------ catalog helpers

    def _scan(
        self, definition: RegisteredTableDefinition, row_filter: BooleanExpression
    ) -> list[Mapping[str, Any]]:
        columns = tuple(field.name for field in definition.arrow_schema)
        rows: list[Mapping[str, Any]] = self._adapter.scan_columns(
            definition.table, columns=columns, row_filter=row_filter
        ).to_pylist()
        return rows

    def _head(self, table: str) -> str | None:
        info = self._adapter.load_table(table)
        if info is None:
            raise TableNotFound(f"table {table} does not exist; create the Phase 1 tables first")
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id


def _check_arrival(value: int) -> None:
    """Every streamed Canonical ``arrival_seq`` must lie inside ``[0, 2**62)``."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise CatalogIntegrityError("a committed Canonical arrival_seq is not an integer")
    if not 0 < value < rules.ARRIVAL_SEQ_LIMIT or value % rules.ARRIVAL_SEQ_STRIDE == 0:
        raise CatalogIntegrityError(
            f"committed Canonical arrival_seq {value} is outside its interval or a block base"
        )

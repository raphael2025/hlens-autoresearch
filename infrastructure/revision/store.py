"""Append-only Raw revision store for the Binance spot archive slice (Phase 1 D2).

One entry point turns a D0 ``CollectedObject`` plus a strict D1 parse outcome into durable,
append-only revisions (ADR-0023 §4 / §7, 03-data.md §7.1 / §7.4):

1. the archive revision row in ``raw.binance_spot_archives`` — one stable batch, and the
   **allocation of this archive's arrival-sequence block**;
2. the parsed rows in ``raw.binance_spot_agg_trades`` / ``raw.binance_spot_klines_1m`` — bounded
   ``pyarrow.Table`` microbatches with stable batch ids, committed in order after the archive row.

Everything else follows from those two steps:

- **Replay** — the same object and the same parse produce the same revision ids, the same block
  base (read back from the committed archive row) and the same batches, so every commit comes
  back ``already_committed``: no new snapshot, no new row, no new arrival sequence number. The
  fast path never trusts a batch id alone: each batch is rebuilt from the immutable object and
  the catalog adapter recomputes its fingerprint from the actual batch before answering.
- **Recovery** — there is no journal. A crash between any two commits is repaired by calling
  ``ingest`` again: the archive row (if committed) yields the block base and the two knowledge-axis
  times, the immutable object plus the D1 parser yield the rows, and the missing microbatches are
  the ones the catalog does not already have.
- **Replacement** — a new checksum at the same official path is a new object, a new revision and a
  new set of parsed rows. Nothing is overwritten or deleted. Unless the *source* proves an order
  (it does not, see ``precedence``), both revisions stay maximal heads and the result reports a
  competing-head conflict instead of choosing one.
- **Rejection** — a rejected parse never produces parsed Raw rows, and never produces an archive
  revision either: ADR-0023's failure semantics give a failed payload no ``knowledge_time``, and
  the archive row cannot exist without one. The verified immutable object stays in the warehouse
  and the D1 quality event is returned; persisting quality events is batch E (honest boundary).

``arrival_seq`` is audit / idempotency / recovery only. It is allocated here, written here, and
read here **only** to allocate the next block and to recover a block base — never to order,
compare or select revisions.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, Self

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.expressions import BooleanExpression, EqualTo

from core.contracts.catalog import (
    CommitConflict,
    CommitOutcome,
    CommitRequest,
    CommitResult,
    SnapshotInfo,
    TableInfo,
    TableNotFound,
)
from core.contracts.collector import CollectedObject, CollectionResult, SourceBinding
from core.contracts.revision import (
    AvailabilityDecision,
    ObservationTimes,
    PolicyBinding,
    PolicyRole,
    PrecedenceEvidence,
    RevisionGraph,
    RevisionRecord,
)
from core.contracts.storage import StorageAdapter
from core.domain.base import contract_schema_version_scope
from infrastructure import contract_version
from infrastructure.catalog.definitions import RegisteredTableDefinition
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_AGG_TRADES,
    BINANCE_SPOT_ARCHIVES,
    BINANCE_SPOT_KLINES_1M,
)
from infrastructure.parser import (
    ArchiveParseRequest,
    ArchiveRejection,
    ParsedArchive,
    ParseOutcome,
)
from infrastructure.parser.binance_archive import (
    PARSER_BINDING,
    ParserQualityEvent,
    SpooledArchive,
    parse_archive_spooled,
)
from infrastructure.revision import identity
from infrastructure.revision.availability import (
    AVAILABILITY_BINDING,
    AvailabilitySubject,
    decide_availability,
    rule_for,
)
from infrastructure.revision.precedence import (
    PRECEDENCE_BINDING,
    PrecedenceViolation,
    RevisionFacts,
    maximal_heads,
    supersedes_for,
)

__all__ = [
    "ARCHIVE_TABLE",
    "DEFAULT_MICROBATCH_ROWS",
    "MAX_MICROBATCH_ROWS",
    "ROW_TABLES",
    "ArchiveContext",
    "ArchiveIngested",
    "ArchiveRejected",
    "AvailabilityGapSummary",
    "BatchCommit",
    "IngestOutcome",
    "RawRevisionStore",
    "RevisionCatalog",
    "RevisionStoreError",
    "RevisionStoreConflict",
]


def _equals(column: str, value: str) -> BooleanExpression:
    """``column == value`` as a PyIceberg expression (never a string built from data).

    ``EqualTo`` accepts ``(term, literal)`` at runtime; its generic base confuses the type
    checker, so the narrow call is isolated here instead of being ignored at every call site.
    """
    return EqualTo(column, value)  # type: ignore[call-arg, arg-type]


ARCHIVE_TABLE: Final = BINANCE_SPOT_ARCHIVES.table
ROW_TABLES: Final[Mapping[str, str]] = {
    "agg_trades": BINANCE_SPOT_AGG_TRADES.table,
    "klines_1m": BINANCE_SPOT_KLINES_1M.table,
}
_ROW_DEFINITIONS: Final[Mapping[str, RegisteredTableDefinition]] = {
    "agg_trades": BINANCE_SPOT_AGG_TRADES,
    "klines_1m": BINANCE_SPOT_KLINES_1M,
}
_ROW_SUBJECTS: Final[Mapping[str, AvailabilitySubject]] = {
    "agg_trades": AvailabilitySubject.AGG_TRADE,
    "klines_1m": AvailabilitySubject.KLINE_1M,
}

#: Rows per microbatch (ADR-0023 §7: bounded ``pyarrow.Table`` batches, rebuildable on retry).
DEFAULT_MICROBATCH_ROWS: Final = 25_000
#: Upper bound accepted from configuration; keeps one batch far below the WSL memory budget.
MAX_MICROBATCH_ROWS: Final = 250_000
#: Attempts per commit when another writer moved the table head (single writer: a safety net).
_COMMIT_ATTEMPTS: Final = 8
_DAY: Final = timedelta(days=1)
_ZERO: Final = timedelta(0)


class RevisionStoreError(Exception):
    """Base class of ingest failures that must not be papered over."""


class RevisionStoreConflict(RevisionStoreError):
    """Persisted state disagrees with the request; nothing is written (fail closed)."""


@dataclass(frozen=True, slots=True)
class ArchiveContext:
    """What the archive revision row records about *how* the object was collected."""

    request_id: str
    data_type: str
    collector_id: str
    collector_version: str
    source: SourceBinding

    def __post_init__(self) -> None:
        if self.data_type not in ROW_TABLES:
            raise RevisionStoreError(f"unsupported data_type {self.data_type!r}")
        if (
            self.source.source_id != identity.ARCHIVE_SOURCE_ID
            or self.source.version != identity.ARCHIVE_SOURCE_VERSION
        ):
            raise RevisionStoreError(
                f"unsupported source {self.source.source_id}@{self.source.version}"
            )

    @classmethod
    def from_result(cls, result: CollectionResult) -> Self:
        """The context shared by every object of one ``CollectionResult``."""
        return cls(
            request_id=result.request.request_id,
            data_type=result.request.data_type,
            collector_id=result.collector_id,
            collector_version=result.collector_version,
            source=result.request.source,
        )


@dataclass(frozen=True, slots=True)
class BatchCommit:
    """One committed (or replayed) batch."""

    table: str
    batch_id: str
    snapshot_id: str
    outcome: CommitOutcome
    row_count: int

    @property
    def replayed(self) -> bool:
        return self.outcome is CommitOutcome.ALREADY_COMMITTED


@dataclass(frozen=True, slots=True)
class AvailabilityGapSummary:
    """How many revisions of one table were written with the same availability evidence gap.

    The gap itself is persisted per row in ``availability_evidence_gap``; this is the aggregate a
    caller can log or hand to the future quality-report writer (batch E) without holding millions
    of identical strings.
    """

    table: str
    gap: str
    revision_count: int


@dataclass(frozen=True, slots=True)
class ArchiveIngested:
    """Result of persisting one archive revision and its parsed rows."""

    archive_revision_id: str
    observation_key: str
    arrival_seq_base: int
    archive_commit: BatchCommit
    rows_table: str
    row_commits: tuple[BatchCommit, ...]
    row_count: int
    row_revision_count: int
    availability_gaps: tuple[AvailabilityGapSummary, ...]
    maximal_heads: tuple[str, ...]
    supersedes: tuple[str, ...]
    competing_revision_ids: tuple[str, ...]

    @property
    def replayed(self) -> bool:
        """True when nothing new was committed: every batch came back ``already_committed``."""
        return self.archive_commit.replayed and all(commit.replayed for commit in self.row_commits)

    @property
    def has_competing_heads(self) -> bool:
        """True when this observation key has more than one maximal head (fail closed)."""
        return len(self.maximal_heads) > 1

    @property
    def snapshot_ids(self) -> tuple[str, ...]:
        return (self.archive_commit.snapshot_id, *(c.snapshot_id for c in self.row_commits))


@dataclass(frozen=True, slots=True)
class ArchiveRejected:
    """A strict D1 rejection: no archive revision, no parsed rows, no snapshot."""

    rejection: ArchiveRejection

    @property
    def quality_event(self) -> ParserQualityEvent:
        """The D1 quality event; persisting it is batch E (honest boundary)."""
        return self.rejection.quality_event()


type IngestOutcome = ArchiveIngested | ArchiveRejected


class RevisionCatalog(Protocol):
    """The catalog capability D2 needs: the core Protocol plus C3's bounded projection reads.

    ``PyIcebergCatalogAdapter`` satisfies it. Declaring it here keeps the store testable with a
    proxy (crash / race injection) without widening the frozen ``CatalogAdapter`` Protocol.
    ``scan_column_batches`` streams the same projection as ``scan_columns`` as Arrow record
    batches; callers close the returned iterator (when it offers ``close``) if they stop early.
    """

    def load_table(self, table: str) -> TableInfo | None: ...

    def get_snapshot(self, table: str, snapshot_id: str) -> SnapshotInfo: ...

    def commit_batch(self, request: CommitRequest, batch: pa.Table) -> CommitResult: ...

    def scan_columns(
        self,
        table: str,
        *,
        columns: Sequence[str],
        row_filter: BooleanExpression = ...,
        limit: int | None = ...,
        snapshot_id: str | None = ...,
    ) -> pa.Table: ...

    def scan_column_batches(
        self,
        table: str,
        *,
        columns: Sequence[str],
        row_filter: BooleanExpression = ...,
        snapshot_id: str | None = ...,
    ) -> Iterator[pa.RecordBatch]: ...

    def max_int64(
        self,
        table: str,
        column: str,
        *,
        row_filter: BooleanExpression = ...,
        check: Callable[[int], None] | None = ...,
    ) -> int | None: ...


class RawRevisionStore:
    """Persists archive and parsed Raw revisions through the C2/C3 catalog adapter.

    One writer per table (ADR-0023 §7). The store owns no state between calls: every fact it
    needs after a restart is read back from Iceberg, from the immutable archive object or from
    the deterministic D1 parser.
    """

    def __init__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        *,
        clock: Callable[[], datetime] | None = None,
        microbatch_rows: int = DEFAULT_MICROBATCH_ROWS,
        availability_binding: PolicyBinding = AVAILABILITY_BINDING,
        precedence_binding: PolicyBinding = PRECEDENCE_BINDING,
    ) -> None:
        if not isinstance(microbatch_rows, int) or isinstance(microbatch_rows, bool):
            raise RevisionStoreError("microbatch_rows must be an int")
        if not 1 <= microbatch_rows <= MAX_MICROBATCH_ROWS:
            raise RevisionStoreError(f"microbatch_rows must be between 1 and {MAX_MICROBATCH_ROWS}")
        self._adapter = adapter
        self._storage = storage
        self._clock = clock or (lambda: datetime.now(UTC))
        self._microbatch_rows = microbatch_rows
        self._availability = availability_binding
        self._precedence = precedence_binding

    # ------------------------------------------------------------------ entry points

    def archive_revision_id(self, collected: CollectedObject, context: ArchiveContext) -> str:
        """The stable revision id of ``collected`` (also the D1 parse request's binding)."""
        return self._identify(collected, context)[1]

    def ingest(self, collected: CollectedObject, context: ArchiveContext) -> IngestOutcome:
        """Parse the published object with D1 and persist the resulting revisions."""
        observation_key, revision_id = self._identify(collected, context)
        request = ArchiveParseRequest.for_collected_object(
            collected, data_type=context.data_type, archive_revision_id=revision_id
        )
        outcome = parse_archive_spooled(request, self._storage)
        if isinstance(outcome, ArchiveRejection):
            return self._persist(collected, context, outcome, observation_key, revision_id)
        try:
            return self._persist(collected, context, outcome, observation_key, revision_id)
        finally:
            outcome.close()

    def ingest_parsed(
        self, collected: CollectedObject, context: ArchiveContext, outcome: ParseOutcome
    ) -> IngestOutcome:
        """Persist an already computed strict parse outcome for ``collected``."""
        observation_key, revision_id = self._identify(collected, context)
        return self._persist(collected, context, outcome, observation_key, revision_id)

    def ingest_collection(self, result: CollectionResult) -> tuple[IngestOutcome, ...]:
        """Ingest every object of one ``CollectionResult`` in its canonical (key) order."""
        context = ArchiveContext.from_result(result)
        return tuple(self.ingest(collected, context) for collected in result.objects)

    # ------------------------------------------------------------------ identity

    def _identify(self, collected: CollectedObject, context: ArchiveContext) -> tuple[str, str]:
        """``(observation_key, revision_id)``, after binding the object to the official path."""
        return identify_archive(collected, context)

    # ------------------------------------------------------------------ persistence

    def _persist(
        self,
        collected: CollectedObject,
        context: ArchiveContext,
        outcome: ParseOutcome | SpooledArchive,
        observation_key: str,
        revision_id: str,
    ) -> IngestOutcome:
        if isinstance(outcome, ArchiveRejection):
            if outcome.archive_revision_id != revision_id:
                raise RevisionStoreError("the rejection belongs to another archive revision")
            return ArchiveRejected(rejection=outcome)
        if not isinstance(outcome, (ParsedArchive, SpooledArchive)):
            raise RevisionStoreError(
                "outcome must be a ParsedArchive, SpooledArchive, or an ArchiveRejection"
            )
        self._check_parsed(collected, context, outcome, revision_id)

        stored = self._stored_archive_row(revision_id)
        if stored is None:
            # A new write group: the archive revision and its rows at the current version.
            version = contract_version.new_group_version()
            base, times, archive_commit, supersedes = self._append_archive(
                collected, context, observation_key, revision_id, version
            )
        else:
            self._verify_stored_archive(stored, collected, context, observation_key)
            # Its rows are written / replayed at the version it was committed with
            # (ADR-0052 versioned replay, V1 / V2).
            version = contract_version.replay_version(
                stored.get("contract_schema_version"),
                what=f"committed archive revision {revision_id}",
            )
            base = int(stored["arrival_seq"])
            times = _times_from_row(stored)
            supersedes = tuple(stored["supersedes"])
            archive_commit = BatchCommit(
                table=ARCHIVE_TABLE,
                batch_id=_archive_batch_id(revision_id, base),
                snapshot_id=self._snapshot_of_batch(
                    ARCHIVE_TABLE, _archive_batch_id(revision_id, base)
                ),
                outcome=CommitOutcome.ALREADY_COMMITTED,
                row_count=1,
            )

        row_commits, row_revisions = self._append_rows(
            outcome, context, revision_id, base, times, version
        )
        heads, competing = self._heads(observation_key)
        gaps = self._gap_summaries(context, row_revisions)
        return ArchiveIngested(
            archive_revision_id=revision_id,
            observation_key=observation_key,
            arrival_seq_base=base,
            archive_commit=archive_commit,
            rows_table=ROW_TABLES[context.data_type],
            row_commits=row_commits,
            row_count=outcome.row_count,
            row_revision_count=row_revisions,
            availability_gaps=gaps,
            maximal_heads=heads,
            supersedes=supersedes,
            competing_revision_ids=competing,
        )

    def _check_parsed(
        self,
        collected: CollectedObject,
        context: ArchiveContext,
        parsed: ParsedArchive | SpooledArchive,
        revision_id: str,
    ) -> None:
        """The parse must be the strict D1 result *of this object*, by this parser version."""
        if parsed.archive_revision_id != revision_id:
            raise RevisionStoreError("the parse belongs to another archive revision")
        if parsed.parser != PARSER_BINDING:
            raise RevisionStoreError("the parse was produced by another parser binding")
        if parsed.object_ref != collected.ref:
            raise RevisionStoreError("the parse refers to another published object")
        if (
            parsed.symbol != collected.symbol
            or parsed.data_type != context.data_type
            or parsed.coverage_start != collected.coverage_start
            or parsed.coverage_end != collected.coverage_end
        ):
            raise RevisionStoreError("the parse does not cover this object")

    # ------------------------------------------------------------------ archive revision

    def _append_archive(
        self,
        collected: CollectedObject,
        context: ArchiveContext,
        observation_key: str,
        revision_id: str,
        version: str,
    ) -> tuple[int, ObservationTimes, BatchCommit, tuple[str, ...]]:
        """Allocate a block, build the one-row batch and commit it (the allocation itself)."""
        definition = BINANCE_SPOT_ARCHIVES
        last_error: CommitConflict | None = None
        for _ in range(_COMMIT_ATTEMPTS):
            parent = self._current_snapshot_id(ARCHIVE_TABLE)
            base = identity.arrival_block_base(self._max_archive_arrival_seq())
            knowledge_time = self._knowledge_time(collected.retrieved_at)
            decision = decide_availability(
                AvailabilitySubject.ARCHIVE,
                event_time=collected.coverage_start,
                event_end_time=collected.coverage_end,
                ingest_time=collected.retrieved_at,
                knowledge_time=knowledge_time,
                binding=self._availability,
            )
            known = self._known_facts(observation_key)
            candidate = RevisionFacts(
                observation_key=observation_key,
                revision_id=revision_id,
                source_identity=identity.archive_source_identity(),
                payload_hash=collected.ref.sha256,
            )
            try:
                supersedes, evidence, _unordered = supersedes_for(
                    candidate, known, knowledge_time=knowledge_time, binding=self._precedence
                )
            except PrecedenceViolation as exc:
                raise RevisionStoreConflict(str(exc)) from exc
            record = RevisionRecord(
                schema_version=version,
                observation_key=observation_key,
                revision_id=revision_id,
                source_id=identity.archive_source_identity(),
                payload_hash=collected.ref.sha256,
                arrival_seq=base,
                supersedes=supersedes,
                availability=decision,
            )
            RevisionGraph(revisions=(record,), precedence_evidence=evidence)
            batch = _archive_batch(definition, record, evidence, collected, context)
            request = CommitRequest(
                table=ARCHIVE_TABLE,
                batch_id=_archive_batch_id(revision_id, base),
                batch_fingerprint=definition.fingerprint_rule.fingerprint(batch),
                row_count=1,
                expected_parent_snapshot_id=parent,
            )
            try:
                result = self._adapter.commit_batch(request, batch)
            except CommitConflict as exc:
                # Another commit moved the head: re-read, re-allocate, rebuild. A failed
                # attempt's block base is never reused (gaps are allowed, ADR-0023 §4).
                last_error = exc
                continue
            return (
                base,
                record.availability.times,
                BatchCommit(
                    table=ARCHIVE_TABLE,
                    batch_id=request.batch_id,
                    snapshot_id=result.snapshot.snapshot_id,
                    outcome=result.outcome,
                    row_count=1,
                ),
                supersedes,
            )
        raise RevisionStoreConflict(
            f"could not allocate an arrival-sequence block for {revision_id} after "
            f"{_COMMIT_ATTEMPTS} attempts"
        ) from last_error

    def _knowledge_time(self, ingest_time: datetime) -> datetime:
        """The local time this revision became selectable; monotonicity is not assumed."""
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != _ZERO:
            raise RevisionStoreError("the clock must return timezone-aware UTC")
        if now < ingest_time:
            raise RevisionStoreConflict(
                "knowledge_time would precede ingest_time: refusing to guess a local clock"
            )
        return now

    def _verify_stored_archive(
        self,
        stored: Mapping[str, Any],
        collected: CollectedObject,
        context: ArchiveContext,
        observation_key: str,
    ) -> None:
        """A replay fast path must agree with what is actually persisted, field by field."""
        expected: Mapping[str, Any] = {
            "observation_key": observation_key,
            "source_id": identity.archive_source_identity(),
            "payload_hash": collected.ref.sha256,
            "data_type": context.data_type,
            "symbol": collected.symbol,
            "coverage_start": collected.coverage_start,
            "coverage_end": collected.coverage_end,
            "source_uri": collected.source_uri,
            "source_sha256": collected.source_sha256,
            "object_key": collected.ref.key,
            "object_uri": collected.ref.uri,
            "object_sha256": collected.ref.sha256,
            "object_size_bytes": collected.ref.size,
            "source_binding_id": context.source.source_id,
            "source_binding_version": context.source.version,
            "availability_policy_id": self._availability.policy_id,
            "availability_policy_version": self._availability.version,
            "availability_policy_hash": self._availability.policy_hash,
        }
        mismatched = sorted(key for key, value in expected.items() if stored.get(key) != value)
        if mismatched:
            raise RevisionStoreConflict(
                f"the persisted archive revision disagrees with this request: {mismatched}"
            )
        base = stored["arrival_seq"]
        if not isinstance(base, int) or base % identity.ARRIVAL_SEQ_STRIDE != 0:
            raise RevisionStoreConflict("the persisted arrival_seq is not a block base")

    # ------------------------------------------------------------------ parsed rows

    def _append_rows(
        self,
        parsed: ParsedArchive | SpooledArchive,
        context: ArchiveContext,
        revision_id: str,
        base: int,
        times: ObservationTimes,
        version: str,
    ) -> tuple[tuple[BatchCommit, ...], int]:
        definition = _ROW_DEFINITIONS[context.data_type]
        subject = _ROW_SUBJECTS[context.data_type]
        source_identity = identity.row_source_identity(revision_id)
        chunks: Iterable[pa.Table]
        if isinstance(parsed, ParsedArchive):
            chunks = (
                parsed.rows.slice(offset, self._microbatch_rows)
                for offset in range(0, parsed.row_count, self._microbatch_rows)
            )
        else:
            chunks = self._spooled_microbatches(parsed)
        commits: list[BatchCommit] = []
        revisions = 0
        for index, chunk in enumerate(chunks):
            records = _row_records(
                chunk,
                data_type=context.data_type,
                symbol=parsed.symbol,
                time_unit=parsed.time_unit.value,
                source_identity=source_identity,
                base=base,
                times=times,
                subject=subject,
                binding=self._availability,
                contract_schema_version=version,
            )
            # Cross-record invariants (unique ids and sequence numbers, no duplicate payload,
            # no cycles) are proven on the contracts, not on the Arrow batch. Uniqueness across
            # microbatches follows from the parser's strictly increasing keys within one archive.
            RevisionGraph(revisions=records)
            batch = _row_batch(definition, records, chunk, context.data_type)
            commits.append(
                self._commit_rows(
                    definition,
                    batch,
                    batch_id=_row_batch_id(revision_id, index),
                    row_count=len(records),
                )
            )
            revisions += len(records)
        if revisions != parsed.row_count:
            raise RevisionStoreConflict("row revisions do not account for the parsed rows")
        return tuple(commits), revisions

    def _spooled_microbatches(self, parsed: SpooledArchive) -> Iterator[pa.Table]:
        """Assemble bounded write batches from the parser's fixed-size record batches."""
        pending: list[pa.RecordBatch] = []
        pending_rows = 0
        with parsed.open_cursor() as cursor:
            for source_batch in cursor:
                offset = 0
                while offset < source_batch.num_rows:
                    take = min(
                        source_batch.num_rows - offset,
                        self._microbatch_rows - pending_rows,
                    )
                    pending.append(source_batch.slice(offset, take))
                    pending_rows += take
                    offset += take
                    if pending_rows == self._microbatch_rows:
                        yield pa.Table.from_batches(pending)
                        pending.clear()
                        pending_rows = 0
        if pending_rows:
            yield pa.Table.from_batches(pending)

    def _commit_rows(
        self,
        definition: RegisteredTableDefinition,
        batch: pa.Table,
        *,
        batch_id: str,
        row_count: int,
    ) -> BatchCommit:
        fingerprint = definition.fingerprint_rule.fingerprint(batch)
        last_error: CommitConflict | None = None
        for _ in range(_COMMIT_ATTEMPTS):
            request = CommitRequest(
                table=definition.table,
                batch_id=batch_id,
                batch_fingerprint=fingerprint,
                row_count=row_count,
                expected_parent_snapshot_id=self._current_snapshot_id(definition.table),
            )
            try:
                result = self._adapter.commit_batch(request, batch)
            except CommitConflict as exc:
                last_error = exc
                continue
            return BatchCommit(
                table=definition.table,
                batch_id=batch_id,
                snapshot_id=result.snapshot.snapshot_id,
                outcome=result.outcome,
                row_count=row_count,
            )
        raise RevisionStoreConflict(
            f"batch {batch_id} of {definition.table} lost {_COMMIT_ATTEMPTS} commit races"
        ) from last_error

    def _gap_summaries(
        self, context: ArchiveContext, row_revisions: int
    ) -> tuple[AvailabilityGapSummary, ...]:
        summaries: list[AvailabilityGapSummary] = []
        archive_gap = rule_for(AvailabilitySubject.ARCHIVE).gap
        if archive_gap is not None:
            summaries.append(AvailabilityGapSummary(ARCHIVE_TABLE, archive_gap, 1))
        row_gap = rule_for(_ROW_SUBJECTS[context.data_type]).gap
        if row_gap is not None and row_revisions:
            summaries.append(
                AvailabilityGapSummary(ROW_TABLES[context.data_type], row_gap, row_revisions)
            )
        return tuple(summaries)

    # ------------------------------------------------------------------ catalog reads

    def _current_snapshot_id(self, table: str) -> str | None:
        info = self._adapter.load_table(table)
        if info is None:
            raise TableNotFound(f"table {table} does not exist; create the Phase 1 tables first")
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id

    def _max_archive_arrival_seq(self) -> int | None:
        """The largest committed archive ``arrival_seq``: the block allocation anchor.

        The reduction streams: the adapter folds bounded Arrow record batches into one running
        maximum, so the archive history is never materialised as a single table however long it
        grows (the per-file I/O cost does grow with it). Every streamed value must be a legal
        block base, so a corrupted anchor fails closed instead of seeding an allocation.
        """
        return self._adapter.max_int64(
            ARCHIVE_TABLE, "arrival_seq", check=_check_archive_arrival_seq
        )

    def _stored_archive_row(self, revision_id: str) -> Mapping[str, Any] | None:
        rows = self._adapter.scan_columns(
            ARCHIVE_TABLE,
            columns=_ARCHIVE_LOOKUP_COLUMNS,
            row_filter=_equals("revision_id", revision_id),
            limit=2,
        ).to_pylist()
        if not rows:
            return None
        if len(rows) > 1:
            raise CatalogIntegrityError(
                f"archive revision {revision_id} is committed more than once"
            )
        row: Mapping[str, Any] = rows[0]
        return row

    def _known_facts(self, observation_key: str) -> tuple[RevisionFacts, ...]:
        """Facts of the already committed revisions of ``observation_key`` (never their order)."""
        rows = self._adapter.scan_columns(
            ARCHIVE_TABLE,
            columns=(
                "observation_key",
                "revision_id",
                "source_id",
                "payload_hash",
                "source_revision_id",
                "source_revision_time",
            ),
            row_filter=_equals("observation_key", observation_key),
        ).to_pylist()
        return tuple(
            RevisionFacts(
                observation_key=row["observation_key"],
                revision_id=row["revision_id"],
                source_identity=row["source_id"],
                payload_hash=row["payload_hash"],
                source_revision_id=row["source_revision_id"],
                source_revision_time=row["source_revision_time"],
            )
            for row in rows
        )

    def _heads(self, observation_key: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """``(maximal heads, competing revision ids)`` of one archive observation key.

        The revisions and their persisted precedence edges are validated as a ``RevisionGraph``
        first (no cycles, no self reference, no cross-key edge, every declared edge backed by
        evidence), so a corrupt graph fails closed instead of yielding a head.
        """
        rows = self._adapter.scan_columns(
            ARCHIVE_TABLE,
            columns=_ARCHIVE_REVISION_COLUMNS,
            row_filter=_equals("observation_key", observation_key),
        ).to_pylist()
        records = tuple(_record_from_row(row) for row in rows)
        evidence = tuple(item for row in rows for item in _evidence_from_row(row))
        RevisionGraph(revisions=records, precedence_evidence=evidence)
        heads = maximal_heads(records, evidence)
        competing = heads if len(heads) > 1 else ()
        return heads, competing

    def _snapshot_of_batch(self, table: str, batch_id: str) -> str:
        """The snapshot that carries ``batch_id`` on the main branch (replay bookkeeping)."""
        info = self._adapter.load_table(table)
        if info is None:  # pragma: no cover - the archive row was just read from this table
            raise TableNotFound(f"table {table} does not exist")
        snapshot = info.current_snapshot
        if snapshot is not None:
            history = getattr(self._adapter, "history", None)
            if callable(history):
                snapshots = history(table, snapshot.snapshot_id)
            else:
                snapshots = None
            if snapshots is not None:
                for snapshot in snapshots:
                    if snapshot.batch_id == batch_id:
                        return str(snapshot.snapshot_id)
            else:
                # Brent's cycle detector uses a checkpoint id plus two counters rather than an
                # O(history) set of visited snapshot ids. Keep this newest-first walk and stop
                # immediately when the requested batch is found.
                checkpoint_id: str | None = None
                power = 1
                distance = 0
                while snapshot is not None:
                    if checkpoint_id is None:
                        checkpoint_id = snapshot.snapshot_id
                    else:
                        distance += 1
                    if snapshot.snapshot_id == checkpoint_id and distance > 0:
                        raise CatalogIntegrityError(
                            f"{table} has a cycle in snapshot ancestry at {snapshot.snapshot_id}"
                        )
                    if snapshot.batch_id == batch_id:
                        return snapshot.snapshot_id
                    if distance == power:
                        checkpoint_id = snapshot.snapshot_id
                        power *= 2
                        distance = 0
                    parent = snapshot.parent_snapshot_id
                    snapshot = None if parent is None else self._adapter.get_snapshot(table, parent)
        raise CatalogIntegrityError(
            f"{table} has a row of batch {batch_id} but no snapshot that committed it"
        )


# --------------------------------------------------------------------------- identity


def identify_archive(collected: CollectedObject, context: ArchiveContext) -> tuple[str, str]:
    """``(observation_key, revision_id)``, after binding the object to the official path.

    The one binding rule of an archive revision to its published object, shared by the store's
    ingest and the persisted-row verifier (D3E-R2): the same inputs can never be judged twice
    by two rules.
    """
    if not isinstance(collected, CollectedObject):
        raise RevisionStoreError("collected must be a CollectedObject")
    if not isinstance(context, ArchiveContext):
        raise RevisionStoreError("context must be an ArchiveContext")
    start = collected.coverage_start
    if (start.hour, start.minute, start.second, start.microsecond) != (0, 0, 0, 0):
        raise RevisionStoreError("archive coverage must start at a UTC midnight")
    if collected.coverage_end - start != _DAY:
        raise RevisionStoreError("archive coverage must be exactly one UTC day")
    day = start.date()
    relative = identity.archive_relative_path(context.data_type, collected.symbol, day)
    if not collected.source_uri.endswith(f"/{relative}"):
        raise RevisionStoreError(
            f"source URI does not end in the official archive path {relative!r}"
        )
    expected_key = identity.archive_object_key(
        context.data_type, collected.symbol, day, collected.ref.sha256
    )
    if collected.ref.key != expected_key:
        raise RevisionStoreError(
            "published object key is not the content-addressed key of this archive"
        )
    # The archive payload hash is a *source* claim: it must come from the official
    # ``.CHECKSUM`` and must already have been verified against the bytes this machine
    # stored. Without that declaration there is nothing to bind the revision to, and the
    # local object hash must never be passed off as one (D2-R1).
    if collected.source_sha256 is None:
        raise RevisionStoreError(
            "the archive carries no official .CHECKSUM declaration: a Binance archive "
            "revision's payload hash may not be taken from the local object hash"
        )
    try:
        identity.check_sha256(collected.source_sha256, "source_sha256")
    except identity.IdentityViolation as exc:
        raise RevisionStoreError(str(exc)) from exc
    if collected.source_sha256 != collected.ref.sha256:
        raise RevisionStoreError("source checksum and stored object disagree")
    observation_key = identity.archive_observation_key(context.data_type, collected.symbol, day)
    revision_id = identity.revision_id(
        observation_key, identity.archive_source_identity(), collected.ref.sha256
    )
    return observation_key, revision_id


# --------------------------------------------------------------------------- allocation anchor


def _check_archive_arrival_seq(value: int) -> None:
    """Validate one streamed anchor value; anything unlawful fails closed (never allocates).

    Applied per value *inside* the streaming reduction, so no unbounded bookkeeping is needed:
    uniqueness is already guaranteed by the expected-parent commit and the single writer per
    table (ADR-0023 §7), and this only has to prove that what is committed is a legal block base.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise CatalogIntegrityError("a committed archive arrival_seq is not an integer")
    if value < 0 or value > identity.MAX_ARRIVAL_SEQ:
        raise CatalogIntegrityError("a committed archive arrival_seq is outside the int64 range")
    if value % identity.ARRIVAL_SEQ_STRIDE != 0:
        raise CatalogIntegrityError("a committed archive arrival_seq is not a block base")


# --------------------------------------------------------------------------- record building


def _times_from_row(row: Mapping[str, Any]) -> ObservationTimes:
    """The two-axis times of a persisted archive revision (the anchor a recovery reuses)."""
    return ObservationTimes(
        event_time=row["event_time"],
        event_end_time=row["event_end_time"],
        source_time=row["source_time"],
        available_time=row["available_time"],
        ingest_time=row["ingest_time"],
        knowledge_time=row["knowledge_time"],
        declared_latency=timedelta(microseconds=row["declared_latency_us"]),
    )


def _decision_from_row(row: Mapping[str, Any]) -> AvailabilityDecision:
    return AvailabilityDecision(
        times=_times_from_row(row),
        policy=PolicyBinding(
            role=PolicyRole.AVAILABILITY,
            policy_id=row["availability_policy_id"],
            version=row["availability_policy_version"],
            policy_hash=row["availability_policy_hash"],
        ),
        evidence=tuple(row["availability_evidence"]),
        evidence_gap=row["availability_evidence_gap"],
    )


def _record_from_row(row: Mapping[str, Any]) -> RevisionRecord:
    """The contract record of a persisted row, rebuilt at the row's recorded version (V1)."""
    version = contract_version.replay_version(
        row["contract_schema_version"], what=f"archive revision {row['revision_id']}"
    )
    with contract_schema_version_scope(version):
        return RevisionRecord(
            observation_key=row["observation_key"],
            revision_id=row["revision_id"],
            source_id=row["source_id"],
            payload_hash=row["payload_hash"],
            arrival_seq=row["arrival_seq"],
            supersedes=tuple(row["supersedes"]),
            source_revision_id=row["source_revision_id"],
            source_revision_time=row["source_revision_time"],
            availability=_decision_from_row(row),
        )


def _evidence_from_row(row: Mapping[str, Any]) -> tuple[PrecedenceEvidence, ...]:
    """The row's in-row edges, rebuilt at the row's recorded version (V1)."""
    version = contract_version.replay_version(
        row["contract_schema_version"], what=f"archive revision {row['revision_id']}"
    )
    with contract_schema_version_scope(version):
        return _edges_of(row)


def _edges_of(row: Mapping[str, Any]) -> tuple[PrecedenceEvidence, ...]:
    return tuple(
        PrecedenceEvidence(
            observation_key=row["observation_key"],
            revision_id=row["revision_id"],
            superseded_revision_id=edge["superseded_revision_id"],
            policy=PolicyBinding(
                role=PolicyRole.PRECEDENCE,
                policy_id=edge["policy_id"],
                version=edge["policy_version"],
                policy_hash=edge["policy_hash"],
            ),
            evidence=tuple(edge["evidence"]),
            knowledge_time=edge["knowledge_time"],
        )
        for edge in row["precedence_evidence"]
    )


def _row_records(
    chunk: pa.Table,
    *,
    data_type: str,
    symbol: str,
    time_unit: str,
    source_identity: str,
    base: int,
    times: ObservationTimes,
    subject: AvailabilitySubject,
    binding: PolicyBinding,
    contract_schema_version: str | None = None,
) -> tuple[RevisionRecord, ...]:
    """One ``RevisionRecord`` per parsed row, validated before anything is mapped to Arrow.

    ``contract_schema_version`` is the archive revision's (one write group, ADR-0052 versioned
    replay, V1 / V2); ``None`` only for the rows of a new archive revision (the current version).
    """
    version = (
        contract_version.new_group_version()
        if contract_schema_version is None
        else contract_version.replay_version(contract_schema_version, what="an archive revision")
    )
    records: list[RevisionRecord] = []
    for row in chunk.to_pylist():
        if data_type == "agg_trades":
            observation_key = identity.agg_trade_observation_key(symbol, row["agg_trade_id"])
            payload_hash = identity.agg_trade_payload_hash(symbol, time_unit, row)
            event_time, event_end_time = row["event_time"], None
        else:
            observation_key = identity.kline_1m_observation_key(symbol, row["interval_start"])
            payload_hash = identity.kline_1m_payload_hash(symbol, time_unit, row)
            event_time, event_end_time = row["interval_start"], row["interval_end"]
        decision = decide_availability(
            subject,
            event_time=event_time,
            event_end_time=event_end_time,
            ingest_time=times.ingest_time,
            knowledge_time=times.knowledge_time,
            binding=binding,
        )
        records.append(
            RevisionRecord(
                schema_version=version,
                observation_key=observation_key,
                revision_id=identity.revision_id(observation_key, source_identity, payload_hash),
                source_id=source_identity,
                payload_hash=payload_hash,
                arrival_seq=identity.row_arrival_seq(base, row["archive_line_number"]),
                availability=decision,
            )
        )
    return tuple(records)


# --------------------------------------------------------------------------- Arrow mapping


def _archive_batch_id(revision_id: str, base: int) -> str:
    """Stable id of the one-row archive batch; the block base makes a retry's batch distinct."""
    return f"{revision_id}.archive.{base}"


def _row_batch_id(revision_id: str, index: int) -> str:
    """Stable id of the ``index``-th row microbatch of one archive revision."""
    return f"{revision_id}.rows.{index:08d}"


def _revision_columns(
    records: Sequence[RevisionRecord], evidence_by_revision: Mapping[str, Any]
) -> dict[str, Any]:
    """The revision block shared by the three Raw tables, in contract field order."""
    times = [record.availability.times for record in records]
    return {
        "observation_key": [record.observation_key for record in records],
        "revision_id": [record.revision_id for record in records],
        "source_id": [record.source_id for record in records],
        "payload_hash": [record.payload_hash for record in records],
        "arrival_seq": [record.arrival_seq for record in records],
        "supersedes": [list(record.supersedes) for record in records],
        "source_revision_id": [record.source_revision_id for record in records],
        "source_revision_time": [record.source_revision_time for record in records],
        "source_time": [item.source_time for item in times],
        "available_time": [item.available_time for item in times],
        "ingest_time": [item.ingest_time for item in times],
        "knowledge_time": [item.knowledge_time for item in times],
        "declared_latency_us": [
            item.declared_latency // timedelta(microseconds=1) for item in times
        ],
        "availability_policy_id": [record.availability.policy.policy_id for record in records],
        "availability_policy_version": [record.availability.policy.version for record in records],
        "availability_policy_hash": [record.availability.policy.policy_hash for record in records],
        "availability_evidence": [list(record.availability.evidence) for record in records],
        "availability_evidence_gap": [record.availability.evidence_gap for record in records],
        "precedence_evidence": [
            evidence_by_revision.get(record.revision_id, []) for record in records
        ],
        "contract_schema_version": [record.schema_version for record in records],
    }


def _edge_values(evidence: Iterable[PrecedenceEvidence]) -> list[dict[str, Any]]:
    return [
        {
            "superseded_revision_id": item.superseded_revision_id,
            "policy_id": item.policy.policy_id,
            "policy_version": item.policy.version,
            "policy_hash": item.policy.policy_hash,
            "evidence": list(item.evidence),
            "knowledge_time": item.knowledge_time,
        }
        for item in evidence
    ]


def _archive_batch(
    definition: RegisteredTableDefinition,
    record: RevisionRecord,
    evidence: Sequence[PrecedenceEvidence],
    collected: CollectedObject,
    context: ArchiveContext,
) -> pa.Table:
    """The one-row ``raw.binance_spot_archives`` batch of one archive revision."""
    source_sha256 = collected.source_sha256
    if source_sha256 is None:  # pragma: no cover - ``_identify`` already fails closed
        raise RevisionStoreError(
            "an archive row needs the official .CHECKSUM declaration as its source_sha256"
        )
    times = record.availability.times
    columns = _revision_columns((record,), {record.revision_id: _edge_values(evidence)})
    columns.update(
        {
            "event_time": [times.event_time],
            "event_end_time": [times.event_end_time],
            "source_binding_id": [context.source.source_id],
            "source_binding_version": [context.source.version],
            "collector_id": [context.collector_id],
            "collector_version": [context.collector_version],
            "collection_request_id": [context.request_id],
            "data_type": [context.data_type],
            "symbol": [collected.symbol],
            "coverage_start": [collected.coverage_start],
            "coverage_end": [collected.coverage_end],
            "source_uri": [collected.source_uri],
            "retrieved_at": [collected.retrieved_at],
            # The source's own declaration (verified in ``_identify``), never the local hash.
            "source_sha256": [source_sha256],
            "object_key": [collected.ref.key],
            "object_uri": [collected.ref.uri],
            "object_sha256": [collected.ref.sha256],
            "object_size_bytes": [collected.ref.size],
            "source_metadata": [
                [
                    {"name": name, "value": collected.source_metadata[name]}
                    for name in sorted(collected.source_metadata)
                ]
            ],
        }
    )
    return _table(definition, columns, 1)


def _row_batch(
    definition: RegisteredTableDefinition,
    records: Sequence[RevisionRecord],
    chunk: pa.Table,
    data_type: str,
) -> pa.Table:
    """One parsed-row microbatch: revision block + lineage + the parser's native columns."""
    count = len(records)
    columns = _revision_columns(records, {})
    columns.update(
        {
            "symbol": [_symbol_of(record) for record in records],
            "archive_revision_id": [record.source_id.split(":", 1)[1] for record in records],
            "archive_line_number": chunk.column("archive_line_number"),
            "parser_id": [PARSER_BINDING.policy_id] * count,
            "parser_version": [PARSER_BINDING.version] * count,
            "parser_hash": [PARSER_BINDING.policy_hash] * count,
        }
    )
    native: tuple[str, ...]
    if data_type == "agg_trades":
        columns["event_time"] = chunk.column("event_time")
        native = (
            "agg_trade_id",
            "price",
            "quantity",
            "first_trade_id",
            "last_trade_id",
            "timestamp_raw",
            "is_buyer_maker",
            "is_best_match",
        )
    else:
        columns["interval_start"] = chunk.column("interval_start")
        columns["interval_end"] = chunk.column("interval_end")
        native = (
            "open_time_raw",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time_raw",
            "quote_asset_volume",
            "number_of_trades",
            "taker_buy_base_asset_volume",
            "taker_buy_quote_asset_volume",
            "ignore_raw",
        )
    for column in native:
        columns[column] = chunk.column(column)
    return _table(definition, columns, count)


def _symbol_of(record: RevisionRecord) -> str:
    """The symbol carried by a row observation key (``venue:market:kind:symbol:...``)."""
    return record.observation_key.split(":")[3]


def _table(
    definition: RegisteredTableDefinition, columns: Mapping[str, Any], rows: int
) -> pa.Table:
    """Build the batch exactly in the registered Arrow schema; any drift fails closed here."""
    schema = definition.arrow_schema
    missing = sorted({field.name for field in schema} ^ set(columns))
    if missing:
        raise RevisionStoreError(
            f"batch for {definition.table} does not match its schema: {missing}"
        )
    batch = pa.Table.from_pydict(
        {field.name: columns[field.name] for field in schema}, schema=schema
    )
    if batch.num_rows != rows:
        raise RevisionStoreError(f"batch for {definition.table} has {batch.num_rows} rows")
    return batch


#: Columns read back to recover / verify one archive revision.
_ARCHIVE_LOOKUP_COLUMNS: Final[tuple[str, ...]] = (
    "observation_key",
    "revision_id",
    "source_id",
    "payload_hash",
    "arrival_seq",
    "supersedes",
    "source_revision_id",
    "source_revision_time",
    "event_time",
    "event_end_time",
    "source_time",
    "available_time",
    "ingest_time",
    "knowledge_time",
    "declared_latency_us",
    "availability_policy_id",
    "availability_policy_version",
    "availability_policy_hash",
    "availability_evidence",
    "availability_evidence_gap",
    "data_type",
    "symbol",
    "coverage_start",
    "coverage_end",
    "source_uri",
    "source_sha256",
    "object_key",
    "object_uri",
    "object_sha256",
    "object_size_bytes",
    "source_binding_id",
    "source_binding_version",
    "contract_schema_version",
)
#: Columns needed to rebuild the ``RevisionRecord`` / ``PrecedenceEvidence`` graph of one key.
_ARCHIVE_REVISION_COLUMNS: Final[tuple[str, ...]] = (
    "observation_key",
    "revision_id",
    "source_id",
    "payload_hash",
    "arrival_seq",
    "supersedes",
    "source_revision_id",
    "source_revision_time",
    "event_time",
    "event_end_time",
    "source_time",
    "available_time",
    "ingest_time",
    "knowledge_time",
    "declared_latency_us",
    "availability_policy_id",
    "availability_policy_version",
    "availability_policy_hash",
    "availability_evidence",
    "availability_evidence_gap",
    "precedence_evidence",
    "contract_schema_version",
)

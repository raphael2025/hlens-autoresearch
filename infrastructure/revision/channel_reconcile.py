"""Cross-channel reconciler and graph guard for D-33 option A (Phase 1 D3E; ADR-0027 §4 / §9 / §11).

For one affected partition ``(data_type, symbol, UTC day)`` — after an ingest on either channel, or
as an independent re-run — the reconciler:

1. pins the snapshots it reads: the REST element and response tables, the archive element and
   archive revision tables and the precedence-evidence table. A read — and every verdict drawn
   from it, a pass or an integrity failure — counts only if all five heads are the same before
   and after it (heads only move forward); otherwise it is read again, a bounded number of times;
2. takes every REST element revision of that day and every revision of the same observation keys
   on both channels, and **proves each one** with the shared ``PersistedRowVerifier`` (D3E-R2)
   before anything is compared: REST rows down to a lawful response revision, its published body
   and its exact element batch; archive rows down to a lawful archive revision, its published
   object and its exact row batch. A row that does not reproduce is ``CatalogIntegrityError``:
   no comparison, no finding, no clock reading, no edge;
3. runs the accepted pure policy ``compare_channels`` on every archive × REST pair of proven
   rows. Projection, decimal and unit rules are **not** re-implemented here;
4. ``INTEGRITY_VIOLATION`` aborts with ``CatalogIntegrityError`` before any edge is committed;
   ``MISMATCH`` / ``INCOMPARABLE`` produce no edge and a deterministic finding; a key without a
   counterpart is not an error and records nothing;
5. for ``EQUAL`` pairs the time-free ``edge_id`` is looked up in
   ``raw.binance_spot_precedence_evidence`` first. A committed edge is re-verified field for field
   (policy, both ends and tables, projection hash, evidence items, the snapshots it pinned, a
   knowledge time not before either revision) and reused with its **first** knowledge time; only
   missing edges get the injected UTC clock — read once, after every comparison, before the
   commit — and are committed in stable order with content-derived batch ids, then read back.

Edges are evidence-only rows: direction archive revision → REST revision, in their own table.
No revision row's ``supersedes`` is ever written or rewritten.

The graph guard (``check_arrival_seq`` / ``assemble_channel_graph``) is the pre-step of any graph
that aggregates both channels for one observation key: archive ``arrival_seq`` in ``[0, 2**62)``,
REST in ``[2**62, 2**63)``, unique inside the graph; a wrong interval, a negative value, a bool,
an int64 overflow or any collision fails closed. ``arrival_seq`` never orders anything.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.expressions import (
    And,
    BooleanExpression,
    EqualTo,
    GreaterThanOrEqual,
    In,
    LessThan,
)

from core.contracts.catalog import (
    BatchConflict,
    CommitConflict,
    CommitRequest,
    SnapshotInfo,
    SnapshotNotFound,
    TableNotFound,
)
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
from infrastructure.catalog.definitions import RegisteredTableDefinition
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_AGG_TRADES,
    BINANCE_SPOT_ARCHIVES,
    BINANCE_SPOT_KLINES_1M,
    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_KLINES_1M,
    BINANCE_SPOT_REST_RESPONSES,
)
from infrastructure.revision import identity as archive_identity
from infrastructure.revision import rest_identity
from infrastructure.revision.channel_precedence import (
    DELIVERY_CHANNEL_BINDING,
    Channel,
    ChannelComparison,
    ChannelEdge,
    ChannelPrecedenceViolation,
    ChannelRevision,
    ComparisonOutcome,
    build_channel_edge,
    compare_channels,
)
from infrastructure.revision.row_integrity import (
    MAX_ELEMENT_MICROBATCH_ROWS,
    PersistedRowVerifier,
    history_from,
)
from infrastructure.revision.store import BatchCommit, RevisionCatalog
from infrastructure.streaming.runs import RunLimits, RunRef, RunSetBuilder, iter_run

__all__ = [
    "DEFAULT_EDGE_MICROBATCH_ROWS",
    "EVIDENCE_TABLE",
    "FINDING_CHANNEL_INCOMPARABLE",
    "FINDING_CHANNEL_MISMATCH",
    "ArrivalSeqRangeViolation",
    "ChannelFinding",
    "ChannelReconcileConflict",
    "ChannelReconcileError",
    "ChannelReconciled",
    "ChannelReconciler",
    "ReconciledEdge",
    "VerifiedEdgeRunParams",
    "assemble_channel_graph",
    "check_arrival_seq",
    "evidence_from_row",
    "revision_record_from_row",
]

EVIDENCE_TABLE: Final = BINANCE_SPOT_PRECEDENCE_EVIDENCE.table
FINDING_CHANNEL_MISMATCH: Final = "channel_projection_mismatch"
FINDING_CHANNEL_INCOMPARABLE: Final = "channel_projection_incomparable"
DEFAULT_EDGE_MICROBATCH_ROWS: Final = 10_000
MAX_EDGE_MICROBATCH_ROWS: Final = 100_000

_ARCHIVE_TABLES: Final[Mapping[str, RegisteredTableDefinition]] = {
    "agg_trades": BINANCE_SPOT_AGG_TRADES,
    "klines_1m": BINANCE_SPOT_KLINES_1M,
}
_REST_TABLES: Final[Mapping[str, RegisteredTableDefinition]] = {
    "agg_trades": BINANCE_SPOT_REST_AGG_TRADES,
    "klines_1m": BINANCE_SPOT_REST_KLINES_1M,
}
_TIME_COLUMNS: Final[Mapping[str, str]] = {
    "agg_trades": "event_time",
    "klines_1m": "interval_start",
}
_ATTEMPTS: Final = 8
#: Keys per ``IN`` scan (bounded predicate size; the pinned read spans all chunks).
_KEY_CHUNK: Final = 500
_ZERO: Final = timedelta(0)
_DAY: Final = timedelta(days=1)


class ChannelReconcileError(Exception):
    """Base class of reconcile failures that must not be papered over."""


class ChannelReconcileConflict(ChannelReconcileError):
    """The reconcile cannot complete honestly now (clock, contention); nothing is committed."""


class ArrivalSeqRangeViolation(CatalogIntegrityError):
    """An ``arrival_seq`` is outside its channel interval or collides inside one graph."""


def _equals(column: str, value: object) -> BooleanExpression:
    return EqualTo(column, value)  # type: ignore[call-arg, arg-type]


def _member(column: str, values: Iterable[object]) -> BooleanExpression:
    return In(column, set(values))  # type: ignore[call-arg, arg-type]


def _at_least(column: str, value: object) -> BooleanExpression:
    return GreaterThanOrEqual(column, value)  # type: ignore[call-arg, arg-type]


def _below(column: str, value: object) -> BooleanExpression:
    return LessThan(column, value)  # type: ignore[call-arg, arg-type]


def _all(*parts: BooleanExpression) -> BooleanExpression:
    result = parts[0]
    for part in parts[1:]:
        result = And(result, part)
    return result


# =========================================================================================
# graph guard
# =========================================================================================


def check_arrival_seq(channel: Channel, value: object) -> int:
    """``value`` if it lies in ``channel``'s interval; anything else fails closed."""
    if not isinstance(channel, Channel):
        raise ArrivalSeqRangeViolation("channel must be a Channel")
    try:
        if channel is Channel.ARCHIVE:
            return rest_identity.check_archive_interval_arrival_seq(value)
        return rest_identity.check_rest_arrival_seq(value)
    except rest_identity.RestIdentityViolation as exc:
        raise ArrivalSeqRangeViolation(f"{channel.value} arrival_seq: {exc}") from None


def _channel_of(record: RevisionRecord) -> Channel:
    """The channel a revision's source identity names; the claim is checked, never trusted."""
    if record.source_id == rest_identity.rest_source_identity():
        return Channel.REST
    prefix = f"{archive_identity.archive_source_identity()}:"
    if record.source_id.startswith(prefix) and len(record.source_id) > len(prefix):
        return Channel.ARCHIVE
    raise ArrivalSeqRangeViolation(
        f"revision {record.revision_id} has a source identity of neither channel"
    )


def assemble_channel_graph(
    archive: Sequence[RevisionRecord],
    rest: Sequence[RevisionRecord],
    evidence: Sequence[PrecedenceEvidence] = (),
) -> RevisionGraph:
    """Aggregate both channels into one ``RevisionGraph`` behind the arrival range guard.

    Each record must sit in the list of the channel its source identity names, inside that
    channel's interval; no two records may share an ``arrival_seq``. Only then is the contract
    graph built (unique ids and payloads, no cross-key claims, no cycles, evidence-backed edges).
    """
    seen: dict[int, str] = {}
    for expected, records in ((Channel.ARCHIVE, archive), (Channel.REST, rest)):
        for record in records:
            if not isinstance(record, RevisionRecord):
                raise ArrivalSeqRangeViolation("a graph member is not a RevisionRecord")
            if _channel_of(record) is not expected:
                raise ArrivalSeqRangeViolation(
                    f"revision {record.revision_id} is not a {expected.value} revision"
                )
            value = check_arrival_seq(expected, record.arrival_seq)
            holder = seen.get(value)
            if holder is not None:
                raise ArrivalSeqRangeViolation(
                    f"arrival_seq {value} collides: {holder} and {record.revision_id}"
                )
            seen[value] = record.revision_id
    try:
        return RevisionGraph(revisions=(*archive, *rest), precedence_evidence=tuple(evidence))
    except ValueError as exc:
        raise CatalogIntegrityError(f"cross-channel revision graph is invalid: {exc}") from None


def revision_record_from_row(row: Mapping[str, Any]) -> RevisionRecord:
    """The contract ``RevisionRecord`` of one persisted element row (either channel)."""
    event_end = row.get("event_end_time", row.get("interval_end"))
    times = ObservationTimes(
        event_time=row["event_time"] if "event_time" in row else row["interval_start"],
        event_end_time=event_end,
        source_time=row["source_time"],
        available_time=row["available_time"],
        ingest_time=row["ingest_time"],
        knowledge_time=row["knowledge_time"],
        declared_latency=timedelta(microseconds=row["declared_latency_us"]),
    )
    decision = AvailabilityDecision(
        times=times,
        policy=PolicyBinding(
            role=PolicyRole.AVAILABILITY,
            policy_id=row["availability_policy_id"],
            version=row["availability_policy_version"],
            policy_hash=row["availability_policy_hash"],
        ),
        evidence=tuple(row["availability_evidence"]),
        evidence_gap=row["availability_evidence_gap"],
    )
    return RevisionRecord(
        schema_version=row["contract_schema_version"],
        observation_key=row["observation_key"],
        revision_id=row["revision_id"],
        source_id=row["source_id"],
        payload_hash=row["payload_hash"],
        arrival_seq=row["arrival_seq"],
        supersedes=tuple(row["supersedes"]),
        source_revision_id=row["source_revision_id"],
        source_revision_time=row["source_revision_time"],
        availability=decision,
    )


def evidence_from_row(row: Mapping[str, Any]) -> PrecedenceEvidence:
    """The complete ``PrecedenceEvidence`` (both ends explicit) of one evidence-table row."""
    return PrecedenceEvidence(
        schema_version=row["contract_schema_version"],
        observation_key=row["observation_key"],
        revision_id=row["revision_id"],
        superseded_revision_id=row["superseded_revision_id"],
        policy=PolicyBinding(
            role=PolicyRole.PRECEDENCE,
            policy_id=row["policy_id"],
            version=row["policy_version"],
            policy_hash=row["policy_hash"],
        ),
        evidence=tuple(row["evidence"]),
        knowledge_time=row["knowledge_time"],
    )


# =========================================================================================
# results
# =========================================================================================


@dataclass(frozen=True, slots=True)
class ChannelFinding:
    """A non-equal archive / REST pair: no edge, competing heads stay (persisting it is E)."""

    code: str
    observation_key: str
    archive_revision_id: str
    rest_revision_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReconciledEdge:
    """One edge of the partition; ``reused`` means its first committed record was re-verified."""

    edge: ChannelEdge
    reused: bool

    @property
    def edge_id(self) -> str:
        return self.edge.edge_id


@dataclass(frozen=True, slots=True)
class ChannelReconciled:
    """Result of reconciling one ``(data_type, symbol, day)`` partition."""

    data_type: str
    symbol: str
    day: date
    #: Snapshots the comparisons read (``None`` for a table that has no snapshot yet).
    rest_snapshot_id: str | None
    archive_snapshot_id: str | None
    evidence_snapshot_id: str | None
    edges: tuple[ReconciledEdge, ...]
    findings: tuple[ChannelFinding, ...]
    commits: tuple[BatchCommit, ...]

    @property
    def edge_ids(self) -> tuple[str, ...]:
        return tuple(item.edge_id for item in self.edges)

    @property
    def new_edge_ids(self) -> tuple[str, ...]:
        return tuple(item.edge_id for item in self.edges if not item.reused)


# =========================================================================================
# pinned read
# =========================================================================================


@dataclass(frozen=True, slots=True)
class _Pinned:
    rest_snapshot: str | None
    responses_snapshot: str | None
    archive_snapshot: str | None
    archives_snapshot: str | None
    evidence_snapshot: str | None
    #: Every row below is proven (``PersistedRowVerifier``) against these five heads.
    rest_rows: tuple[Mapping[str, Any], ...]
    archive_rows: tuple[Mapping[str, Any], ...]
    units: Mapping[str, str]
    evidence_rows: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _PinnedRuns:
    """Fixed-capacity staged source rows for the verified-edge iterator path."""

    rest_snapshot: str | None
    responses_snapshot: str | None
    archive_snapshot: str | None
    archives_snapshot: str | None
    evidence_snapshot: str | None
    day_keys: RunRef | None
    rest_rows: RunRef | None
    archive_rows: RunRef | None
    evidence_rows: RunRef | None


@dataclass(frozen=True, slots=True)
class _Plan:
    partition: tuple[str, str, date]
    pinned: _Pinned
    comparisons: tuple[ChannelComparison, ...]
    findings: tuple[ChannelFinding, ...]
    existing: Mapping[str, ChannelEdge]
    missing: tuple[ChannelComparison, ...]
    records: Mapping[str, tuple[tuple[RevisionRecord, ...], tuple[RevisionRecord, ...]]]


@dataclass(frozen=True, slots=True)
class VerifiedEdgeRunParams:
    """Explicit staging limits for :meth:`ChannelReconciler.iter_verified_edges`.

    The current D3E graph/provenance plan still has upstream working-set boundaries; this type
    only chooses the fixed-capacity staging and merge shape.
    """

    row_capacity: int
    merge_fanout: int
    limits: RunLimits

    def __post_init__(self) -> None:
        if isinstance(self.row_capacity, bool) or self.row_capacity <= 0:
            raise ChannelReconcileError("edge run row_capacity must be positive")
        if isinstance(self.merge_fanout, bool) or self.merge_fanout < 2:
            raise ChannelReconcileError("edge run merge_fanout must be at least 2")


class ChannelReconciler:
    """Appends missing D-33 evidence-only edges for one partition; idempotent and re-runnable.

    The evidence table has one writer (ADR-0023 §7); concurrent runs are tolerated: a lost
    expected-parent race or an identical edge set committed by another run restarts the whole
    pinned read, and the other run's first knowledge time is what gets reused.
    """

    def __init__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        *,
        clock: Callable[[], datetime] | None = None,
        edge_microbatch_rows: int = DEFAULT_EDGE_MICROBATCH_ROWS,
    ) -> None:
        if not isinstance(edge_microbatch_rows, int) or isinstance(edge_microbatch_rows, bool):
            raise ChannelReconcileError("edge_microbatch_rows must be an int")
        if not 1 <= edge_microbatch_rows <= MAX_EDGE_MICROBATCH_ROWS:
            raise ChannelReconcileError(
                f"edge_microbatch_rows must be between 1 and {MAX_EDGE_MICROBATCH_ROWS}"
            )
        self._adapter = adapter
        self._storage = storage
        self._clock = clock or (lambda: datetime.now(UTC))
        self._microbatch = edge_microbatch_rows
        # The REST store's own verifier: one set of provenance rules for both consumers.
        self._verifier = PersistedRowVerifier(adapter, storage)

    def reconcile(self, data_type: str, symbol: str, day: date) -> ChannelReconciled:
        self._check_partition(data_type, symbol, day)
        last_error: Exception | None = None
        for _ in range(_ATTEMPTS):
            plan = self._plan(data_type, symbol, day)
            edges: dict[str, ReconciledEdge] = {
                edge_id: ReconciledEdge(edge=edge, reused=True)
                for edge_id, edge in plan.existing.items()
            }
            commits: list[BatchCommit] = []
            if plan.missing:
                fresh = self._fresh_edges(plan)
                try:
                    commits = self._commit_edges(plan, fresh)
                except (CommitConflict, BatchConflict) as exc:
                    # Another run moved the evidence table: start over from a new pinned read,
                    # so its first knowledge time is reused rather than a second one minted.
                    last_error = exc
                    continue
                for edge in fresh:
                    edges[edge.edge_id] = ReconciledEdge(edge=edge, reused=False)
            self._verify_readback(tuple(item.edge for item in edges.values()))
            return ChannelReconciled(
                data_type=data_type,
                symbol=symbol,
                day=day,
                rest_snapshot_id=plan.pinned.rest_snapshot,
                archive_snapshot_id=plan.pinned.archive_snapshot,
                evidence_snapshot_id=(
                    commits[-1].snapshot_id if commits else plan.pinned.evidence_snapshot
                ),
                edges=tuple(edges[edge_id] for edge_id in sorted(edges)),
                findings=plan.findings,
                commits=tuple(commits),
            )
        raise ChannelReconcileConflict(
            f"reconcile of {data_type} {symbol} {day} lost {_ATTEMPTS} evidence-table races"
        ) from last_error

    def verified_edges(self, data_type: str, symbol: str, day: date) -> tuple[ChannelEdge, ...]:
        """The partition's **committed** edges, each re-verified; nothing is written or stamped.

        The same pinned read, row proofs and edge re-derivation as ``reconcile``, without the
        clock or a commit: equal pairs that have no committed edge yet are **not** returned — an
        edge exists only once it is persisted (ADR-0027 §4.6). Run on a catalog view pinned to a
        manifest's snapshots (``infrastructure.pit.view``), it answers "which verified edges did
        those snapshots hold" (Phase 1 F1, ADR-0028 §3.2).
        """
        self._check_partition(data_type, symbol, day)
        plan = self._plan(data_type, symbol, day)
        return tuple(plan.existing[edge_id] for edge_id in sorted(plan.existing))

    @contextmanager
    def iter_verified_edges(
        self,
        data_type: str,
        symbol: str,
        day: date,
        *,
        params: VerifiedEdgeRunParams,
    ) -> Iterator[Iterator[ChannelEdge]]:
        """Expose a validated day's committed edges as a sorted, closeable stream.

        Source rows and provenance are scanned in fixed key chunks, staged in bounded sorted
        runs, verified before output is exposed, and replayed in deterministic ``edge_id``
        order. ``verified_edges`` remains the compatibility tuple API. The graph verifier still
        materializes one observation key's revision/head state; this method does not claim an
        end-to-end or complete E1-CAP-1 process bound.
        """
        if not isinstance(params, VerifiedEdgeRunParams):
            raise ChannelReconcileError("params must be VerifiedEdgeRunParams")
        self._check_partition(data_type, symbol, day)
        root = self._verified_edge_run(data_type, symbol, day, params)

        @contextmanager
        def rows() -> Iterator[Iterator[ChannelEdge]]:
            if root is None:
                yield iter(())
                return
            with iter_run(self._storage, root) as stored_rows:

                def edges() -> Iterator[ChannelEdge]:
                    for row in stored_rows:
                        yield ChannelEdge(
                            edge_id=row["edge_id"],
                            evidence=evidence_from_row(row),
                            revision_table=row["revision_table"],
                            superseded_table=row["superseded_table"],
                            revision_snapshot_id=row["revision_snapshot_id"],
                            superseded_snapshot_id=row["superseded_snapshot_id"],
                            projection_sha256=row["projection_sha256"],
                        )

                yield edges()

        with rows() as stream:
            yield stream

    def _check_partition(self, data_type: str, symbol: str, day: date) -> None:
        if data_type not in _REST_TABLES:
            raise ChannelReconcileError(f"unsupported data_type {data_type!r}")
        try:
            rest_identity.agg_trade_observation_key(symbol, 0)
        except rest_identity.RestIdentityViolation as exc:
            raise ChannelReconcileError(str(exc)) from None
        if not isinstance(day, date) or isinstance(day, datetime):
            raise ChannelReconcileError("day must be a datetime.date (UTC)")

    # ------------------------------------------------------------------ plan (no writes)

    def _plan(
        self,
        data_type: str,
        symbol: str,
        day: date,
        *,
        edge_run_params: VerifiedEdgeRunParams | None = None,
    ) -> _Plan:
        pinned = self._pinned_read(data_type, symbol, day)
        archive_by_key: dict[str, list[ChannelRevision]] = {}
        rest_by_key: dict[str, list[ChannelRevision]] = {}
        records: dict[str, tuple[list[RevisionRecord], list[RevisionRecord]]] = {}
        for channel, rows, snapshot in (
            (Channel.ARCHIVE, pinned.archive_rows, pinned.archive_snapshot),
            (Channel.REST, pinned.rest_rows, pinned.rest_snapshot),
        ):
            seen: set[str] = set()
            for row in rows:
                if row["revision_id"] in seen:
                    raise CatalogIntegrityError(
                        f"{channel.value} revision {row['revision_id']} is committed twice"
                    )
                seen.add(row["revision_id"])
                revision = self._channel_revision(channel, data_type, row, snapshot, pinned.units)
                target = archive_by_key if channel is Channel.ARCHIVE else rest_by_key
                target.setdefault(revision.observation_key, []).append(revision)
                try:
                    record = revision_record_from_row(row)
                except ValueError as exc:
                    raise CatalogIntegrityError(
                        f"{revision.table}: row {row['revision_id']} is not a lawful revision "
                        f"({exc})"
                    ) from None
                pair = records.setdefault(revision.observation_key, ([], []))
                pair[0 if channel is Channel.ARCHIVE else 1].append(record)

        comparisons: list[ChannelComparison] = []
        findings: list[ChannelFinding] = []
        integrity: list[str] = []
        for key in sorted(rest_by_key):
            for archive in sorted(archive_by_key.get(key, ()), key=lambda item: item.revision_id):
                for rest in sorted(rest_by_key[key], key=lambda item: item.revision_id):
                    comparison = compare_channels(archive, rest)
                    comparisons.append(comparison)
                    if comparison.outcome is ComparisonOutcome.INTEGRITY_VIOLATION:
                        integrity.append(
                            f"{key} {archive.revision_id} / {rest.revision_id}: "
                            f"{'; '.join(comparison.reasons)}"
                        )
                    elif comparison.outcome is not ComparisonOutcome.EQUAL:
                        findings.append(
                            ChannelFinding(
                                code=(
                                    FINDING_CHANNEL_MISMATCH
                                    if comparison.outcome is ComparisonOutcome.MISMATCH
                                    else FINDING_CHANNEL_INCOMPARABLE
                                ),
                                observation_key=key,
                                archive_revision_id=archive.revision_id,
                                rest_revision_id=rest.revision_id,
                                reasons=comparison.reasons,
                            )
                        )
        if integrity:
            raise CatalogIntegrityError(
                f"stored revisions contradict themselves; no edge is written: {integrity[0]}"
            )

        equal = {
            _edge_id(comparison): comparison
            for comparison in comparisons
            if comparison.outcome is ComparisonOutcome.EQUAL
        }
        existing = self._existing_edges(pinned.evidence_rows, equal)
        self._verify_edge_provenance(data_type, symbol, day, pinned, run_params=edge_run_params)
        missing = tuple(equal[edge_id] for edge_id in sorted(set(equal) - set(existing)))
        frozen_records = {
            key: (tuple(pair[0]), tuple(pair[1])) for key, pair in sorted(records.items())
        }
        # The guard over every graph this partition touches, with every edge already known.
        for key, (archive_records, rest_records) in frozen_records.items():
            assemble_channel_graph(
                archive_records,
                rest_records,
                tuple(
                    edge.evidence
                    for edge in existing.values()
                    if edge.evidence.observation_key == key
                ),
            )
        return _Plan(
            partition=(data_type, symbol, day),
            pinned=pinned,
            comparisons=tuple(comparisons),
            findings=tuple(findings),
            existing=existing,
            missing=missing,
            records=frozen_records,
        )

    def _channel_revision(
        self,
        channel: Channel,
        data_type: str,
        row: Mapping[str, Any],
        snapshot: str | None,
        units: Mapping[str, str],
    ) -> ChannelRevision:
        if snapshot is None:  # pragma: no cover - a row was read, so the table has a snapshot
            raise CatalogIntegrityError("a revision row was read from a table without a snapshot")
        if channel is Channel.ARCHIVE:
            unit = units.get(row["archive_revision_id"])
            if unit is None:
                raise CatalogIntegrityError(
                    f"archive row {row['revision_id']} names an archive revision that is not "
                    "committed for this data type and symbol"
                )
        else:
            if row["source_id"] != rest_identity.rest_source_identity():
                raise CatalogIntegrityError(
                    f"REST row {row['revision_id']} carries another source identity"
                )
            unit = rest_identity.DECLARED_TIME_UNIT
        try:
            return ChannelRevision(
                channel=channel,
                data_type=data_type,
                observation_key=row["observation_key"],
                revision_id=row["revision_id"],
                source_id=row["source_id"],
                payload_hash=row["payload_hash"],
                knowledge_time=row["knowledge_time"],
                snapshot_id=snapshot,
                time_unit=unit,
                row=row,
            )
        except ChannelPrecedenceViolation as exc:
            raise CatalogIntegrityError(
                f"{channel.value} row {row['revision_id']} cannot be compared: {exc}"
            ) from None

    def _verify_edge_provenance(
        self,
        data_type: str,
        symbol: str,
        day: date,
        pinned: _Pinned,
        *,
        run_params: VerifiedEdgeRunParams | None = None,
    ) -> None:
        """Every committed edge row of the partition is exactly what an edge batch committed.

        D3E-R3: an edge row is not a free input either. Each edge batch snapshot (id prefix
        ``<policy>@<version>.edges.<data type>.<symbol>.<day>.``) that can hold an edge of this
        partition's keys is re-read by time travel — rows at the snapshot minus rows at its
        parent — and must match its committed row count, content-derived id and fingerprint. The
        partition's current edge rows must then be exactly those committed rows: a row no edge
        batch committed (forged or re-committed with another ``knowledge_time``), a row two
        batches committed, or a committed row of these keys that is gone fails closed.

        Cross-day keys: an aggTrade key's REST revisions can fall on different UTC days, so
        every partition holding one of them reads the same revisions and derives the same edges,
        and whichever reconciles first commits them under **its own** day prefix. The batches
        checked are therefore those of every UTC day on which a REST revision of this
        partition's keys falls (a legitimate batch of another day only ever holds edges of that
        day's keys, and a key belongs to a day because one of its REST revisions does; REST rows
        are append-only). A batch is always re-read with the key set of the partition that wrote
        it — never a subset — so its complete row set is what gets re-checked.
        """
        if run_params is not None:
            self._verify_edge_provenance_bounded(data_type, symbol, day, pinned, run_params)
            return
        head = pinned.evidence_snapshot
        keys = sorted({row["observation_key"] for row in pinned.rest_rows})
        column = _TIME_COLUMNS[data_type]
        # The partitions whose edge batches can hold an edge of these keys, and their key sets.
        partitions: dict[str, tuple[date, Sequence[str]]] = {
            _edge_batch_prefix((data_type, symbol, day)): (day, keys)
        }
        for other in sorted({_utc_day(row[column]) for row in pinned.rest_rows} - {day}):
            partitions[_edge_batch_prefix((data_type, symbol, other))] = (
                other,
                self._day_keys(data_type, symbol, other, pinned.rest_snapshot),
            )
        committed: dict[str, Mapping[str, Any]] = {}
        committed_builder: RunSetBuilder | None = None
        current_builder: RunSetBuilder | None = None
        own_builder: RunSetBuilder | None = None
        if run_params is not None:

            def provenance_key(row: Mapping[str, Any]) -> tuple[str, str]:
                return row["observation_key"], row["edge_id"]

            committed_builder = RunSetBuilder(
                self._storage,
                key=provenance_key,
                capacity=run_params.row_capacity,
                merge_fanout=run_params.merge_fanout,
                limits=run_params.limits,
            )
            current_builder = RunSetBuilder(
                self._storage,
                key=provenance_key,
                capacity=run_params.row_capacity,
                merge_fanout=run_params.merge_fanout,
                limits=run_params.limits,
            )
            own_builder = RunSetBuilder(
                self._storage,
                key=lambda row: row["observation_key"],
                capacity=run_params.row_capacity,
                merge_fanout=run_params.merge_fanout,
                limits=run_params.limits,
            )
            for row in pinned.evidence_rows:
                current_builder.add(row)
            for row in pinned.rest_rows:
                own_builder.add({"observation_key": row["observation_key"]})
        history = getattr(self._adapter, "history", None)

        def snapshots() -> Iterable[SnapshotInfo]:
            if head is None:
                return
            if callable(history):
                yield from history(EVIDENCE_TABLE, head)
                return
            snapshot_id: str | None = head
            seen: set[str] = set()
            while snapshot_id is not None:
                if snapshot_id in seen:
                    raise CatalogIntegrityError(
                        f"{EVIDENCE_TABLE} has a cycle in snapshot ancestry at {snapshot_id}"
                    )
                seen.add(snapshot_id)
                snapshot = self._adapter.get_snapshot(EVIDENCE_TABLE, snapshot_id)
                yield snapshot
                snapshot_id = snapshot.parent_snapshot_id

        for snapshot in snapshots():
            parent = snapshot.parent_snapshot_id
            owner = None
            if snapshot.batch_id is not None:
                owner = next(
                    (
                        partition
                        for prefix, partition in partitions.items()
                        if snapshot.batch_id.startswith(prefix)
                    ),
                    None,
                )
            if owner is not None:
                owner_day, owner_keys = owner
                if committed_builder is not None and run_params is not None:
                    added_rows: Iterable[Mapping[str, Any]] = self._edge_batch_rows_bounded(
                        (data_type, symbol, owner_day),
                        owner_keys,
                        snapshot,
                        parent,
                        run_params,
                    )
                    for row in added_rows:
                        committed_builder.add(row)
                else:
                    added = self._edge_batch_rows(
                        (data_type, symbol, owner_day), owner_keys, snapshot, parent
                    )
                    for edge_id, row in added.items():
                        if edge_id in committed:
                            raise CatalogIntegrityError(
                                f"evidence edge {edge_id} is committed twice"
                            )
                        committed[edge_id] = row
        if committed_builder is None:
            current = {row["edge_id"]: row for row in pinned.evidence_rows}
            own = set(keys)
            for edge_id, row in current.items():
                if committed.get(edge_id) != row:
                    raise CatalogIntegrityError(
                        f"evidence edge {edge_id} is not exactly what an edge batch of this "
                        "partition committed"
                    )
            missing = sorted(
                edge_id
                for edge_id, row in committed.items()
                if row["observation_key"] in own and edge_id not in current
            )
            if missing:
                raise CatalogIntegrityError(f"committed evidence edge {missing[0]} is gone")
        else:
            assert current_builder is not None and own_builder is not None
            committed_root = committed_builder.finish()
            current_root = current_builder.finish()
            own_root = own_builder.finish()
            with ExitStack() as stack:
                committed_rows = (
                    iter(())
                    if committed_root is None
                    else stack.enter_context(iter_run(self._storage, committed_root))
                )
                current_rows = (
                    iter(())
                    if current_root is None
                    else stack.enter_context(iter_run(self._storage, current_root))
                )
                own_rows = (
                    iter(())
                    if own_root is None
                    else stack.enter_context(iter_run(self._storage, own_root))
                )

                def owned_committed() -> Iterable[Mapping[str, Any]]:
                    unique_keys = (
                        row["observation_key"]
                        for _, group in itertools.groupby(
                            own_rows, key=lambda item: item["observation_key"]
                        )
                        for row in (next(group),)
                    )
                    own_key = next(unique_keys, None)
                    for row in committed_rows:
                        key = row["observation_key"]
                        while own_key is not None and own_key < key:
                            own_key = next(unique_keys, None)
                        if own_key == key:
                            yield row

                committed_for_partition = iter(owned_committed())
                committed_row = next(committed_for_partition, None)
                current_row = next(current_rows, None)
                while committed_row is not None or current_row is not None:
                    if committed_row is None:
                        edge_id = "unknown" if current_row is None else current_row["edge_id"]
                        raise CatalogIntegrityError(
                            f"evidence edge {edge_id} "
                            "is not exactly what an "
                            "edge batch of this partition committed"
                        )
                    if current_row is None:
                        raise CatalogIntegrityError(
                            f"committed evidence edge {committed_row['edge_id']} is gone"
                        )
                    committed_key = (committed_row["observation_key"], committed_row["edge_id"])
                    current_key = (current_row["observation_key"], current_row["edge_id"])
                    if committed_key < current_key:
                        raise CatalogIntegrityError(
                            f"committed evidence edge {committed_row['edge_id']} is gone"
                        )
                    if current_key < committed_key:
                        raise CatalogIntegrityError(
                            f"evidence edge {current_row['edge_id']} is not exactly what an "
                            "edge batch of this partition committed"
                        )
                    if committed_row != current_row:
                        raise CatalogIntegrityError(
                            f"evidence edge {current_row['edge_id']} is not exactly what an "
                            "edge batch of this partition committed"
                        )
                    committed_row = next(committed_for_partition, None)
                    current_row = next(current_rows, None)

    def _verify_edge_provenance_bounded(
        self,
        data_type: str,
        symbol: str,
        day: date,
        pinned: _Pinned,
        params: VerifiedEdgeRunParams,
    ) -> None:
        """Prove edge provenance using sorted owner-day, key, current and committed runs."""
        head = pinned.evidence_snapshot
        own_keys_root = self._day_keys_run(data_type, symbol, day, pinned.rest_snapshot, params)
        if own_keys_root is None:
            return

        column = _TIME_COLUMNS[data_type]
        rest_definition = _REST_TABLES[data_type]
        with RunSetBuilder(
            self._storage,
            key=lambda row: row["day"],
            capacity=params.row_capacity,
            merge_fanout=params.merge_fanout,
            limits=params.limits,
        ) as owner_day_builder:
            with iter_run(self._storage, own_keys_root) as own_key_rows:
                pending: list[str] = []
                for key_row in own_key_rows:
                    pending.append(key_row["observation_key"])
                    if len(pending) == _KEY_CHUNK:
                        owner_day_builder.extend(
                            {"day": _utc_day(row[column])}
                            for row in self._scan_batch_rows(
                                rest_definition,
                                _all(
                                    _equals("symbol", symbol),
                                    _member("observation_key", pending),
                                ),
                                pinned.rest_snapshot,
                                columns=(column,),
                            )
                        )
                        pending.clear()
                if pending:
                    owner_day_builder.extend(
                        {"day": _utc_day(row[column])}
                        for row in self._scan_batch_rows(
                            rest_definition,
                            _all(
                                _equals("symbol", symbol),
                                _member("observation_key", pending),
                            ),
                            pinned.rest_snapshot,
                            columns=(column,),
                        )
                    )
            owner_day_root = owner_day_builder.finish()
        if owner_day_root is None:
            raise CatalogIntegrityError("a partition with REST keys has no REST owner day")

        def provenance_key(row: Mapping[str, Any]) -> tuple[str, str]:
            return row["observation_key"], row["edge_id"]

        def snapshots() -> Iterable[SnapshotInfo]:
            if head is None:
                return
            history = getattr(self._adapter, "history", None)
            if callable(history):
                yield from history(EVIDENCE_TABLE, head)
            else:
                yield from history_from(self._adapter, EVIDENCE_TABLE, head)

        with RunSetBuilder(
            self._storage,
            key=provenance_key,
            capacity=params.row_capacity,
            merge_fanout=params.merge_fanout,
            limits=params.limits,
        ) as committed_builder:
            with iter_run(self._storage, owner_day_root) as owner_day_rows:
                previous_day: date | None = None
                for owner_row in owner_day_rows:
                    owner_day = owner_row["day"]
                    if owner_day == previous_day:
                        continue
                    previous_day = owner_day
                    owner_keys_root = self._day_keys_run(
                        data_type, symbol, owner_day, pinned.rest_snapshot, params
                    )
                    if owner_keys_root is None:
                        raise CatalogIntegrityError(
                            f"REST owner partition {owner_day} has no current keys"
                        )
                    prefix = _edge_batch_prefix((data_type, symbol, owner_day))
                    for snapshot in snapshots():
                        if snapshot.batch_id is None or not snapshot.batch_id.startswith(prefix):
                            continue
                        for row in self._edge_batch_rows_bounded(
                            (data_type, symbol, owner_day),
                            owner_keys_root,
                            snapshot,
                            snapshot.parent_snapshot_id,
                            params,
                        ):
                            committed_builder.add(row)
            committed_root = committed_builder.finish()

        with RunSetBuilder(
            self._storage,
            key=provenance_key,
            capacity=params.row_capacity,
            merge_fanout=params.merge_fanout,
            limits=params.limits,
        ) as current_builder:
            with iter_run(self._storage, own_keys_root) as own_key_rows:
                current_key_chunk: list[str] = []
                for key_row in own_key_rows:
                    current_key_chunk.append(key_row["observation_key"])
                    if len(current_key_chunk) == _KEY_CHUNK:
                        current_builder.extend(
                            self._scan_batch_rows(
                                BINANCE_SPOT_PRECEDENCE_EVIDENCE,
                                _member("observation_key", current_key_chunk),
                                head,
                            )
                        )
                        current_key_chunk.clear()
                if current_key_chunk:
                    current_builder.extend(
                        self._scan_batch_rows(
                            BINANCE_SPOT_PRECEDENCE_EVIDENCE,
                            _member("observation_key", current_key_chunk),
                            head,
                        )
                    )
            current_root = current_builder.finish()

        with ExitStack() as stack:
            committed_rows = (
                iter(())
                if committed_root is None
                else stack.enter_context(iter_run(self._storage, committed_root))
            )
            current_rows = (
                iter(())
                if current_root is None
                else stack.enter_context(iter_run(self._storage, current_root))
            )
            own_key_rows = stack.enter_context(iter_run(self._storage, own_keys_root))

            def owned_committed() -> Iterable[Mapping[str, Any]]:
                unique_keys = (
                    row["observation_key"]
                    for _, group in itertools.groupby(
                        own_key_rows, key=lambda item: item["observation_key"]
                    )
                    for row in (next(group),)
                )
                own_key = next(unique_keys, None)
                for row in committed_rows:
                    row_key = row["observation_key"]
                    while own_key is not None and own_key < row_key:
                        own_key = next(unique_keys, None)
                    if own_key == row_key:
                        yield row

            expected_rows = iter(owned_committed())
            expected = next(expected_rows, None)
            actual = next(current_rows, None)
            while expected is not None or actual is not None:
                if expected is None:
                    actual_id = "unknown" if actual is None else actual["edge_id"]
                    raise CatalogIntegrityError(
                        f"evidence edge {actual_id} is not exactly what an edge batch of this "
                        "partition committed"
                    )
                if actual is None:
                    raise CatalogIntegrityError(
                        f"committed evidence edge {expected['edge_id']} is gone"
                    )
                expected_key = (expected["observation_key"], expected["edge_id"])
                actual_key = (actual["observation_key"], actual["edge_id"])
                if expected_key < actual_key:
                    raise CatalogIntegrityError(
                        f"committed evidence edge {expected['edge_id']} is gone"
                    )
                if actual_key < expected_key:
                    raise CatalogIntegrityError(
                        f"evidence edge {actual['edge_id']} is not exactly what an edge batch of "
                        "this partition committed"
                    )
                if expected != actual:
                    raise CatalogIntegrityError(
                        f"evidence edge {actual['edge_id']} is not exactly what an edge batch "
                        "of this partition committed"
                    )
                expected = next(expected_rows, None)
                actual = next(current_rows, None)

    def _edge_batch_rows(
        self,
        partition: tuple[str, str, date],
        keys: Sequence[str],
        snapshot: SnapshotInfo,
        parent: str | None,
    ) -> dict[str, Mapping[str, Any]]:
        """The complete row set of one edge batch of ``partition``, proven to reproduce it.

        ``keys`` is that partition's key set now: a superset of the one the batch was derived
        from (REST rows are append-only), so the time-travel diff holds every row it added.
        """
        definition = BINANCE_SPOT_PRECEDENCE_EVIDENCE
        after = self._rows_at(keys, snapshot.snapshot_id)
        before = {} if parent is None else self._rows_at(keys, parent)
        added = {edge_id: row for edge_id, row in after.items() if edge_id not in before}
        ordered = [added[edge_id] for edge_id in sorted(added)]
        table = pa.Table.from_pylist([dict(row) for row in ordered], schema=definition.arrow_schema)
        if (
            len(ordered) != snapshot.added_rows
            or snapshot.batch_id != _edge_batch_id(partition, added)
            or snapshot.batch_fingerprint != definition.fingerprint_rule.fingerprint(table)
        ):
            raise CatalogIntegrityError(
                f"edge batch {snapshot.batch_id} no longer reproduces from its snapshot"
            )
        return added

    def _edge_batch_rows_bounded(
        self,
        partition: tuple[str, str, date],
        keys_root: RunRef,
        snapshot: SnapshotInfo,
        parent: str | None,
        params: VerifiedEdgeRunParams,
    ) -> Iterable[Mapping[str, Any]]:
        """Verify one edge batch through external row/id sorts; yield only after full proof.

        The catalog batch is bounded by the D3E writer's explicit microbatch ceiling. Whole
        partition snapshots on either side are external sorted runs; their diff retains only one
        row per reader plus the verified batch (at most ``MAX_EDGE_MICROBATCH_ROWS``) needed by
        the frozen PyArrow fingerprint rule.
        """
        if snapshot.added_rows > MAX_EDGE_MICROBATCH_ROWS:
            raise CatalogIntegrityError(
                f"edge batch {snapshot.batch_id} declares {snapshot.added_rows} rows, above the "
                f"D3E writer limit {MAX_EDGE_MICROBATCH_ROWS}"
            )
        after_root = self._rows_at_run(keys_root, snapshot.snapshot_id, params)
        before_root = None if parent is None else self._rows_at_run(keys_root, parent, params)
        added_rows: list[Mapping[str, Any]] = []

        @contextmanager
        def rows_or_empty(root: RunRef | None) -> Iterator[Iterator[Mapping[str, Any]]]:
            if root is None:
                yield iter(())
                return
            with iter_run(self._storage, root) as rows:
                yield rows

        after_cm = rows_or_empty(after_root)
        before_cm = rows_or_empty(before_root)
        with after_cm as after_iter, before_cm as before_iter:
            after_row = next(after_iter, None)
            before_row = next(before_iter, None)
            while after_row is not None:
                if before_row is None or after_row["edge_id"] < before_row["edge_id"]:
                    added_rows.append(after_row)
                    if len(added_rows) > MAX_EDGE_MICROBATCH_ROWS:
                        raise CatalogIntegrityError(
                            f"edge batch {snapshot.batch_id} exceeds the D3E writer limit"
                        )
                    after_row = next(after_iter, None)
                elif before_row["edge_id"] < after_row["edge_id"]:
                    before_row = next(before_iter, None)
                else:
                    after_row = next(after_iter, None)
                    before_row = next(before_iter, None)
        definition = BINANCE_SPOT_PRECEDENCE_EVIDENCE
        table = pa.Table.from_pylist(
            [dict(row) for row in added_rows], schema=definition.arrow_schema
        )
        added_ids = [row["edge_id"] for row in added_rows]
        if (
            len(added_rows) != snapshot.added_rows
            or snapshot.batch_id != _edge_batch_id(partition, added_ids)
            or snapshot.batch_fingerprint != definition.fingerprint_rule.fingerprint(table)
        ):
            raise CatalogIntegrityError(
                f"edge batch {snapshot.batch_id} no longer reproduces from its snapshot"
            )
        yield from added_rows

    def _rows_at_run(
        self,
        keys_root: RunRef,
        snapshot_id: str,
        params: VerifiedEdgeRunParams,
    ) -> RunRef | None:
        """Read evidence rows for a sorted key run into an edge-id sorted run."""
        definition = BINANCE_SPOT_PRECEDENCE_EVIDENCE
        with RunSetBuilder(
            self._storage,
            key=lambda row: row["edge_id"],
            capacity=params.row_capacity,
            merge_fanout=params.merge_fanout,
            limits=params.limits,
        ) as builder:
            with iter_run(self._storage, keys_root) as key_rows:
                pending: list[str] = []
                for key_row in key_rows:
                    pending.append(key_row["observation_key"])
                    if len(pending) == _KEY_CHUNK:
                        for row in self._scan_batch_rows(
                            definition,
                            _member("observation_key", pending),
                            snapshot_id,
                        ):
                            builder.add(row)
                        pending.clear()
                if pending:
                    for row in self._scan_batch_rows(
                        definition,
                        _member("observation_key", pending),
                        snapshot_id,
                    ):
                        builder.add(row)
            return builder.finish()

    def _day_keys(
        self, data_type: str, symbol: str, day: date, rest_snapshot: str | None
    ) -> list[str]:
        """The observation keys of one partition, read at the pinned REST snapshot."""
        if rest_snapshot is None:  # pragma: no cover - a REST row was read, so it has one
            raise CatalogIntegrityError("a REST row was read from a table without a snapshot")
        start = datetime.combine(day, time(), tzinfo=UTC)
        column = _TIME_COLUMNS[data_type]
        rows = self._adapter.scan_columns(
            _REST_TABLES[data_type].table,
            columns=("observation_key",),
            row_filter=_all(
                _equals("symbol", symbol),
                _at_least(column, start),
                _below(column, start + _DAY),
            ),
            snapshot_id=rest_snapshot,
        ).to_pylist()
        return sorted({row["observation_key"] for row in rows})

    def _day_keys_run(
        self,
        data_type: str,
        symbol: str,
        day: date,
        rest_snapshot: str | None,
        params: VerifiedEdgeRunParams,
    ) -> RunRef | None:
        """Distinct keys of one pinned REST day, externally sorted and duplicate folded."""
        if rest_snapshot is None:
            return None
        start = datetime.combine(day, time(), tzinfo=UTC)
        column = _TIME_COLUMNS[data_type]
        definition = _REST_TABLES[data_type]
        with RunSetBuilder(
            self._storage,
            key=lambda row: row["observation_key"],
            capacity=params.row_capacity,
            merge_fanout=params.merge_fanout,
            limits=params.limits,
        ) as scanned:
            for row in self._scan_batch_rows(
                definition,
                _all(
                    _equals("symbol", symbol),
                    _at_least(column, start),
                    _below(column, start + _DAY),
                ),
                rest_snapshot,
                columns=("observation_key",),
            ):
                scanned.add({"observation_key": row["observation_key"]})
            scanned_root = scanned.finish()
        if scanned_root is None:
            return None
        with (
            RunSetBuilder(
                self._storage,
                key=lambda row: row["observation_key"],
                capacity=params.row_capacity,
                merge_fanout=params.merge_fanout,
                limits=params.limits,
            ) as unique,
            iter_run(self._storage, scanned_root) as scanned_rows,
        ):
            previous: str | None = None
            for row in scanned_rows:
                key = row["observation_key"]
                if key != previous:
                    unique.add(row)
                    previous = key
            return unique.finish()

    def _rows_at(self, keys: Sequence[str], snapshot_id: str) -> dict[str, Mapping[str, Any]]:
        columns = tuple(field.name for field in BINANCE_SPOT_PRECEDENCE_EVIDENCE.arrow_schema)
        rows: dict[str, Mapping[str, Any]] = {}
        for offset in range(0, len(keys), _KEY_CHUNK):
            for row in self._adapter.scan_columns(
                EVIDENCE_TABLE,
                columns=columns,
                row_filter=_member("observation_key", keys[offset : offset + _KEY_CHUNK]),
                snapshot_id=snapshot_id,
            ).to_pylist():
                rows[row["edge_id"]] = row
        return rows

    def _existing_edges(
        self,
        rows: Sequence[Mapping[str, Any]],
        equal: Mapping[str, ChannelComparison],
    ) -> dict[str, ChannelEdge]:
        """Every committed edge of these keys, re-verified; an unexplained row fails closed."""
        existing: dict[str, ChannelEdge] = {}
        known_snapshots: set[tuple[str, str]] = set()
        for row in rows:
            edge_id = row["edge_id"]
            if edge_id in existing:
                raise CatalogIntegrityError(f"evidence edge {edge_id} is committed twice")
            comparison = equal.get(edge_id)
            if comparison is None:
                raise CatalogIntegrityError(
                    f"evidence edge {edge_id} does not describe an equal archive / REST pair "
                    "of the pinned snapshots"
                )
            pins = (
                (comparison.archive.table, row["revision_table"], row["revision_snapshot_id"]),
                (comparison.rest.table, row["superseded_table"], row["superseded_snapshot_id"]),
            )
            for table, stored_table, snapshot_id in pins:
                if stored_table != table:
                    raise CatalogIntegrityError(
                        f"evidence edge {edge_id} names {stored_table!r} instead of {table!r}"
                    )
                if (table, snapshot_id) in known_snapshots:
                    continue
                try:
                    self._adapter.get_snapshot(table, snapshot_id)
                except SnapshotNotFound:
                    raise CatalogIntegrityError(
                        f"evidence edge {edge_id} pins snapshot {snapshot_id!r} that {table} "
                        "does not have"
                    ) from None
                known_snapshots.add((table, snapshot_id))
            try:
                archive = dataclasses.replace(
                    comparison.archive, snapshot_id=row["revision_snapshot_id"]
                )
                rest = dataclasses.replace(
                    comparison.rest, snapshot_id=row["superseded_snapshot_id"]
                )
                rebuilt = build_channel_edge(
                    compare_channels(archive, rest),
                    knowledge_time=row["knowledge_time"],
                    # at the version the edge row was committed with (ADR-0052, V1)
                    contract_schema_version=row["contract_schema_version"],
                )
            except (ChannelPrecedenceViolation, ValueError) as exc:
                raise CatalogIntegrityError(
                    f"evidence edge {edge_id} is not lawful: {exc}"
                ) from None
            expected = _normalised_row(rebuilt)
            mismatched = sorted(name for name, value in expected.items() if row[name] != value)
            if mismatched:
                raise CatalogIntegrityError(
                    f"evidence edge {edge_id} disagrees with its re-derived record: {mismatched}"
                )
            existing[edge_id] = rebuilt
        return existing

    # ------------------------------------------------------------------ write

    def _fresh_edges(self, plan: _Plan) -> list[ChannelEdge]:
        """Missing edges, stamped with one clock reading taken after every comparison."""
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != _ZERO:
            raise ChannelReconcileError("the clock must return timezone-aware UTC")
        floor = max(
            max(item.archive.knowledge_time, item.rest.knowledge_time) for item in plan.missing
        )
        if now < floor:
            raise ChannelReconcileConflict(
                "the edge knowledge_time would precede a revision's knowledge_time: refusing to "
                "backfill an edge"
            )
        fresh = [build_channel_edge(item, knowledge_time=now) for item in plan.missing]
        touched = {edge.evidence.observation_key for edge in fresh}
        known: dict[str, list[ChannelEdge]] = {}
        for edge in (*plan.existing.values(), *fresh):
            known.setdefault(edge.evidence.observation_key, []).append(edge)
        for key in sorted(touched):
            edges = known[key]
            archive_records, rest_records = plan.records[key]
            assemble_channel_graph(
                archive_records, rest_records, tuple(edge.evidence for edge in edges)
            )
        return fresh

    def _commit_edges(self, plan: _Plan, fresh: Sequence[ChannelEdge]) -> list[BatchCommit]:
        definition = BINANCE_SPOT_PRECEDENCE_EVIDENCE
        parent = plan.pinned.evidence_snapshot
        commits: list[BatchCommit] = []
        ordered = sorted(fresh, key=lambda edge: edge.edge_id)
        for offset in range(0, len(ordered), self._microbatch):
            chunk = ordered[offset : offset + self._microbatch]
            batch = pa.Table.from_pylist(
                [edge.row() for edge in chunk], schema=definition.arrow_schema
            )
            request = CommitRequest(
                table=EVIDENCE_TABLE,
                batch_id=_edge_batch_id(plan.partition, (edge.edge_id for edge in chunk)),
                batch_fingerprint=definition.fingerprint_rule.fingerprint(batch),
                row_count=len(chunk),
                expected_parent_snapshot_id=parent,
            )
            result = self._adapter.commit_batch(request, batch)
            parent = result.snapshot.snapshot_id
            commits.append(
                BatchCommit(
                    table=EVIDENCE_TABLE,
                    batch_id=request.batch_id,
                    snapshot_id=parent,
                    outcome=result.outcome,
                    row_count=len(chunk),
                )
            )
        return commits

    def _verify_readback(self, edges: Sequence[ChannelEdge]) -> None:
        """Every edge of this partition is committed exactly once, exactly as expected."""
        if not edges:
            return
        by_id = {edge.edge_id: edge for edge in edges}
        rows: list[Mapping[str, Any]] = []
        ids = sorted(by_id)
        for offset in range(0, len(ids), _KEY_CHUNK):
            rows.extend(
                self._scan(
                    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
                    _member("edge_id", ids[offset : offset + _KEY_CHUNK]),
                )
            )
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["edge_id"]] = counts.get(row["edge_id"], 0) + 1
            expected = _normalised_row(by_id[row["edge_id"]])
            mismatched = sorted(name for name, value in expected.items() if row[name] != value)
            if mismatched:
                raise CatalogIntegrityError(
                    f"evidence edge {row['edge_id']} reads back differently: {mismatched}"
                )
        wrong = sorted(edge_id for edge_id in ids if counts.get(edge_id) != 1)
        if wrong:
            raise CatalogIntegrityError(f"evidence edge {wrong[0]} is not committed exactly once")

    # ------------------------------------------------------------------ reads

    def _pinned_read(self, data_type: str, symbol: str, day: date) -> _Pinned:
        """Rows of the partition, proven lawful, all judged against one set of five heads."""
        rest_def, archive_def = _REST_TABLES[data_type], _ARCHIVE_TABLES[data_type]
        tables = (
            rest_def.table,
            BINANCE_SPOT_REST_RESPONSES.table,
            archive_def.table,
            BINANCE_SPOT_ARCHIVES.table,
            EVIDENCE_TABLE,
        )
        start = datetime.combine(day, time(), tzinfo=UTC)
        column = _TIME_COLUMNS[data_type]
        for _ in range(_ATTEMPTS):
            heads = tuple(self._head(table) for table in tables)
            try:
                day_rows = self._scan(
                    rest_def,
                    _all(
                        _equals("symbol", symbol),
                        _at_least(column, start),
                        _below(column, start + _DAY),
                    ),
                )
                keys = sorted({row["observation_key"] for row in day_rows})
                rest_rows = self._by_keys(rest_def, symbol, keys)
                archive_rows = self._by_keys(archive_def, symbol, keys)
                # Proven before anything is compared (D3E-R2): lineage, times, policy, batch.
                self._verifier.verify_rest_elements(rest_def, data_type, rest_rows)
                archives = self._verifier.verify_archive_elements(
                    archive_def, data_type, symbol, archive_rows
                )
                evidence_rows: list[Mapping[str, Any]] = []
                for offset in range(0, len(keys), _KEY_CHUNK):
                    evidence_rows.extend(
                        self._scan(
                            BINANCE_SPOT_PRECEDENCE_EVIDENCE,
                            _member("observation_key", keys[offset : offset + _KEY_CHUNK]),
                        )
                    )
            except CatalogIntegrityError:
                if tuple(self._head(table) for table in tables) == heads:
                    raise  # judged on one fixed view: the failure stands
                continue  # a head moved: the verdict mixed two snapshots, read again
            if tuple(self._head(table) for table in tables) != heads:
                continue  # a head moved during the read: the rows are not one snapshot's
            return _Pinned(
                rest_snapshot=heads[0],
                responses_snapshot=heads[1],
                archive_snapshot=heads[2],
                archives_snapshot=heads[3],
                evidence_snapshot=heads[4],
                rest_rows=tuple(rest_rows),
                archive_rows=tuple(archive_rows),
                units={archive_id: item.time_unit.value for archive_id, item in archives.items()},
                evidence_rows=tuple(evidence_rows),
            )
        raise ChannelReconcileConflict(
            f"the tables kept moving: no pinned read was possible after {_ATTEMPTS} attempts"
        )

    def _pinned_read_runs(
        self,
        data_type: str,
        symbol: str,
        day: date,
        params: VerifiedEdgeRunParams,
    ) -> _PinnedRuns:
        """Pin the five D3E heads and stage relevant source rows in bounded sorted runs."""
        rest_def, archive_def = _REST_TABLES[data_type], _ARCHIVE_TABLES[data_type]
        tables = (
            rest_def.table,
            BINANCE_SPOT_REST_RESPONSES.table,
            archive_def.table,
            BINANCE_SPOT_ARCHIVES.table,
            EVIDENCE_TABLE,
        )
        for _ in range(_ATTEMPTS):
            heads = tuple(self._head(table) for table in tables)
            try:
                day_keys = self._day_keys_run(data_type, symbol, day, heads[0], params)
                rest_rows = self._rows_for_keys_run(
                    rest_def,
                    symbol,
                    day_keys,
                    heads[0],
                    params,
                    sort_key=lambda row: (
                        row["observation_key"],
                        row["response_revision_id"],
                        row["revision_id"],
                    ),
                )
                archive_rows = self._rows_for_keys_run(
                    archive_def,
                    symbol,
                    day_keys,
                    heads[2],
                    params,
                    sort_key=lambda row: (
                        row["observation_key"],
                        row["archive_revision_id"],
                        row["revision_id"],
                    ),
                )
                evidence_rows = self._rows_for_keys_run(
                    BINANCE_SPOT_PRECEDENCE_EVIDENCE,
                    None,
                    day_keys,
                    heads[4],
                    params,
                    sort_key=lambda row: (row["observation_key"], row["edge_id"]),
                )
            except CatalogIntegrityError:
                if tuple(self._head(table) for table in tables) == heads:
                    raise
                continue
            if tuple(self._head(table) for table in tables) != heads:
                continue
            return _PinnedRuns(
                rest_snapshot=heads[0],
                responses_snapshot=heads[1],
                archive_snapshot=heads[2],
                archives_snapshot=heads[3],
                evidence_snapshot=heads[4],
                day_keys=day_keys,
                rest_rows=rest_rows,
                archive_rows=archive_rows,
                evidence_rows=evidence_rows,
            )
        raise ChannelReconcileConflict(
            f"the tables kept moving: no pinned read was possible after {_ATTEMPTS} attempts"
        )

    def _rows_for_keys_run(
        self,
        definition: RegisteredTableDefinition,
        symbol: str | None,
        keys_root: RunRef | None,
        snapshot_id: str | None,
        params: VerifiedEdgeRunParams,
        *,
        sort_key: Callable[[Mapping[str, Any]], Any],
    ) -> RunRef | None:
        """Read keys in fixed-size IN chunks and spill projected source batches immediately."""
        if keys_root is None or snapshot_id is None:
            return None
        with RunSetBuilder(
            self._storage,
            key=sort_key,
            capacity=params.row_capacity,
            merge_fanout=params.merge_fanout,
            limits=params.limits,
        ) as builder:
            with iter_run(self._storage, keys_root) as key_rows:
                pending: list[str] = []

                def flush() -> None:
                    if not pending:
                        return
                    filters = [_member("observation_key", pending)]
                    if symbol is not None:
                        filters.append(_equals("symbol", symbol))
                    builder.extend(self._scan_batch_rows(definition, _all(*filters), snapshot_id))
                    pending.clear()

                for key_row in key_rows:
                    pending.append(key_row["observation_key"])
                    if len(pending) == _KEY_CHUNK:
                        flush()
                flush()
            return builder.finish()

    def _verify_unique_key_column(
        self,
        definition: RegisteredTableDefinition,
        keys_root: RunRef | None,
        snapshot_id: str | None,
        column: str,
        params: VerifiedEdgeRunParams,
        *,
        label: str,
    ) -> None:
        """Prove uniqueness across a partition by adjacent values in an external sort."""
        if keys_root is None or snapshot_id is None:
            return
        with RunSetBuilder(
            self._storage,
            key=lambda row: row["value"],
            capacity=params.row_capacity,
            merge_fanout=params.merge_fanout,
            limits=params.limits,
        ) as builder:
            with iter_run(self._storage, keys_root) as key_rows:
                pending: list[str] = []

                def flush() -> None:
                    if not pending:
                        return
                    filters = [_member("observation_key", pending)]
                    builder.extend(
                        {"value": row[column]}
                        for row in self._scan_batch_rows(
                            definition,
                            _all(*filters),
                            snapshot_id,
                            columns=(column,),
                        )
                    )
                    pending.clear()

                for key_row in key_rows:
                    pending.append(key_row["observation_key"])
                    if len(pending) == _KEY_CHUNK:
                        flush()
                flush()
            root = builder.finish()
        if root is None:
            return
        with iter_run(self._storage, root) as values:
            previous: object = object()
            first = True
            for row in values:
                value = row["value"]
                if not first and value == previous:
                    raise CatalogIntegrityError(
                        f"{definition.table}: {label} {value!r} is committed more than once"
                    )
                first = False
                previous = value

    def _verified_edge_run(
        self,
        data_type: str,
        symbol: str,
        day: date,
        params: VerifiedEdgeRunParams,
    ) -> RunRef | None:
        """Validate a pinned partition key by key and seal its committed edges before replay."""
        rest_def, archive_def = _REST_TABLES[data_type], _ARCHIVE_TABLES[data_type]
        tables = (
            rest_def.table,
            BINANCE_SPOT_REST_RESPONSES.table,
            archive_def.table,
            BINANCE_SPOT_ARCHIVES.table,
            EVIDENCE_TABLE,
        )
        for _ in range(_ATTEMPTS):
            pinned = self._pinned_read_runs(data_type, symbol, day, params)
            with (
                RunSetBuilder(
                    self._storage,
                    key=lambda row: row["edge_id"],
                    capacity=params.row_capacity,
                    merge_fanout=params.merge_fanout,
                    limits=params.limits,
                ) as output,
                RunSetBuilder(
                    self._storage,
                    key=lambda row: row["archive_revision_id"],
                    capacity=params.row_capacity,
                    merge_fanout=params.merge_fanout,
                    limits=params.limits,
                ) as archive_parents,
            ):
                try:
                    self._verify_edge_provenance_bounded(
                        data_type,
                        symbol,
                        day,
                        _Pinned(
                            rest_snapshot=pinned.rest_snapshot,
                            responses_snapshot=pinned.responses_snapshot,
                            archive_snapshot=pinned.archive_snapshot,
                            archives_snapshot=pinned.archives_snapshot,
                            evidence_snapshot=pinned.evidence_snapshot,
                            rest_rows=(),
                            archive_rows=(),
                            units={},
                            evidence_rows=(),
                        ),
                        params,
                    )
                    self._verify_unique_key_column(
                        rest_def,
                        pinned.day_keys,
                        pinned.rest_snapshot,
                        "revision_id",
                        params,
                        label="revision_id",
                    )
                    self._verify_unique_key_column(
                        rest_def,
                        pinned.day_keys,
                        pinned.rest_snapshot,
                        "arrival_seq",
                        params,
                        label="arrival_seq",
                    )
                    self._verify_unique_key_column(
                        archive_def,
                        pinned.day_keys,
                        pinned.archive_snapshot,
                        "revision_id",
                        params,
                        label="revision_id",
                    )
                    self._verify_unique_key_column(
                        archive_def,
                        pinned.day_keys,
                        pinned.archive_snapshot,
                        "arrival_seq",
                        params,
                        label="arrival_seq",
                    )
                    if pinned.day_keys is not None:
                        with ExitStack() as stack:
                            keys = stack.enter_context(iter_run(self._storage, pinned.day_keys))
                            rest_rows = (
                                iter(())
                                if pinned.rest_rows is None
                                else stack.enter_context(iter_run(self._storage, pinned.rest_rows))
                            )
                            archive_rows = (
                                iter(())
                                if pinned.archive_rows is None
                                else stack.enter_context(
                                    iter_run(self._storage, pinned.archive_rows)
                                )
                            )
                            evidence_rows = (
                                iter(())
                                if pinned.evidence_rows is None
                                else stack.enter_context(
                                    iter_run(self._storage, pinned.evidence_rows)
                                )
                            )
                            rest_groups = iter(
                                itertools.groupby(rest_rows, key=lambda row: row["observation_key"])
                            )
                            archive_groups = iter(
                                itertools.groupby(
                                    archive_rows, key=lambda row: row["observation_key"]
                                )
                            )
                            evidence_groups = iter(
                                itertools.groupby(
                                    evidence_rows, key=lambda row: row["observation_key"]
                                )
                            )
                            rest_group = next(rest_groups, None)
                            archive_group = next(archive_groups, None)
                            evidence_group = next(evidence_groups, None)
                            for key_row in keys:
                                observation_key = key_row["observation_key"]
                                rest_records: list[RevisionRecord] = []
                                rest_revision_root: RunRef | None = None
                                with RunSetBuilder(
                                    self._storage,
                                    key=lambda item: item["revision_id"],
                                    capacity=params.row_capacity,
                                    merge_fanout=params.merge_fanout,
                                    limits=params.limits,
                                ) as rest_revisions:
                                    if rest_group is None or rest_group[0] != observation_key:
                                        raise CatalogIntegrityError(
                                            f"REST key {observation_key} has no staged history rows"
                                        )
                                    response_groups = itertools.groupby(
                                        rest_group[1], key=lambda row: row["response_revision_id"]
                                    )
                                    for _, response_group in response_groups:
                                        response_rows: list[Mapping[str, Any]] = []
                                        for response_row in response_group:
                                            if len(response_rows) == MAX_ELEMENT_MICROBATCH_ROWS:
                                                raise CatalogIntegrityError(
                                                    "REST response group exceeds its fixed page "
                                                    f"limit {MAX_ELEMENT_MICROBATCH_ROWS}"
                                                )
                                            response_rows.append(response_row)
                                        self._verifier.verify_rest_elements(
                                            rest_def, data_type, response_rows
                                        )
                                        for row in response_rows:
                                            revision = self._channel_revision(
                                                Channel.REST,
                                                data_type,
                                                row,
                                                pinned.rest_snapshot,
                                                {},
                                            )
                                            rest_revisions.add(
                                                {
                                                    "revision_id": revision.revision_id,
                                                    "row": dict(row),
                                                    "channel": Channel.REST.value,
                                                    "time_unit": revision.time_unit,
                                                }
                                            )
                                            try:
                                                rest_records.append(revision_record_from_row(row))
                                            except ValueError as exc:
                                                raise CatalogIntegrityError(
                                                    f"{revision.table}: row {row['revision_id']} "
                                                    f"is not a lawful revision ({exc})"
                                                ) from None
                                    rest_revision_root = rest_revisions.finish()
                                rest_group = next(rest_groups, None)

                                archive_records: list[RevisionRecord] = []
                                archive_revision_root: RunRef | None = None
                                if archive_group is not None and archive_group[0] < observation_key:
                                    raise CatalogIntegrityError(
                                        f"archive key {archive_group[0]} is absent from "
                                        "REST day keys"
                                    )
                                with RunSetBuilder(
                                    self._storage,
                                    key=lambda item: item["revision_id"],
                                    capacity=params.row_capacity,
                                    merge_fanout=params.merge_fanout,
                                    limits=params.limits,
                                ) as archive_revisions:
                                    if (
                                        archive_group is not None
                                        and archive_group[0] == observation_key
                                    ):
                                        previous_revision_id: str | None = None
                                        for row in archive_group[1]:
                                            if row["revision_id"] == previous_revision_id:
                                                raise CatalogIntegrityError(
                                                    f"archive revision {row['revision_id']} is "
                                                    "committed twice"
                                                )
                                            previous_revision_id = row["revision_id"]
                                            verified = self._verifier.verify_archive_elements(
                                                archive_def, data_type, symbol, (row,)
                                            )
                                            archive = verified[row["archive_revision_id"]]
                                            archive_parents.add(
                                                {
                                                    "archive_revision_id": archive.revision_id,
                                                    "arrival_seq": archive.row["arrival_seq"],
                                                }
                                            )
                                            units = {archive.revision_id: archive.time_unit.value}
                                            revision = self._channel_revision(
                                                Channel.ARCHIVE,
                                                data_type,
                                                row,
                                                pinned.archive_snapshot,
                                                units,
                                            )
                                            archive_revisions.add(
                                                {
                                                    "revision_id": revision.revision_id,
                                                    "row": dict(row),
                                                    "channel": Channel.ARCHIVE.value,
                                                    "time_unit": revision.time_unit,
                                                }
                                            )
                                            try:
                                                archive_records.append(
                                                    revision_record_from_row(row)
                                                )
                                            except ValueError as exc:
                                                raise CatalogIntegrityError(
                                                    f"{revision.table}: row {row['revision_id']} "
                                                    f"is not a lawful revision ({exc})"
                                                ) from None
                                        archive_group = next(archive_groups, None)
                                    archive_revision_root = archive_revisions.finish()

                                if (
                                    evidence_group is not None
                                    and evidence_group[0] < observation_key
                                ):
                                    raise CatalogIntegrityError(
                                        f"evidence key {evidence_group[0]} is absent from "
                                        "REST day keys"
                                    )
                                has_current_edges = (
                                    evidence_group is not None
                                    and evidence_group[0] == observation_key
                                )

                                rest_records.sort(key=lambda item: item.revision_id)
                                archive_records.sort(key=lambda item: item.revision_id)

                                def channel_revision_from_run_row(
                                    item: Mapping[str, Any], snapshot_id: str | None
                                ) -> ChannelRevision:
                                    channel = Channel(item["channel"])
                                    row = item["row"]
                                    units = (
                                        {row["archive_revision_id"]: item["time_unit"]}
                                        if channel is Channel.ARCHIVE
                                        else {}
                                    )
                                    return self._channel_revision(
                                        channel, data_type, row, snapshot_id, units
                                    )

                                def find_channel_revision(
                                    root: RunRef | None,
                                    revision_id: str,
                                    snapshot_id: str | None,
                                ) -> ChannelRevision | None:
                                    if root is None:
                                        return None
                                    with iter_run(self._storage, root) as revision_rows:
                                        for revision_row in revision_rows:
                                            if revision_row["revision_id"] == revision_id:
                                                return channel_revision_from_run_row(
                                                    revision_row, snapshot_id
                                                )
                                            if revision_row["revision_id"] > revision_id:
                                                break
                                    return None

                                with RunSetBuilder(
                                    self._storage,
                                    key=lambda row: row["edge_id"],
                                    capacity=params.row_capacity,
                                    merge_fanout=params.merge_fanout,
                                    limits=params.limits,
                                ) as equal_pairs:
                                    with ExitStack() as pair_stack:
                                        archive_revision_rows = (
                                            iter(())
                                            if archive_revision_root is None
                                            else pair_stack.enter_context(
                                                iter_run(self._storage, archive_revision_root)
                                            )
                                        )
                                        for archive_row in archive_revision_rows:
                                            archive_revision = channel_revision_from_run_row(
                                                archive_row, pinned.archive_snapshot
                                            )
                                            if rest_revision_root is None:
                                                continue
                                            with iter_run(
                                                self._storage, rest_revision_root
                                            ) as rest_revision_rows:
                                                for rest_row in rest_revision_rows:
                                                    rest = channel_revision_from_run_row(
                                                        rest_row, pinned.rest_snapshot
                                                    )
                                                    comparison = compare_channels(
                                                        archive_revision, rest
                                                    )
                                                    if (
                                                        comparison.outcome
                                                        is ComparisonOutcome.INTEGRITY_VIOLATION
                                                    ):
                                                        raise CatalogIntegrityError(
                                                            "stored revisions contradict "
                                                            "themselves; no edge is written: "
                                                            f"{observation_key} "
                                                            f"{archive_revision.revision_id} / "
                                                            f"{rest.revision_id}: "
                                                            f"{'; '.join(comparison.reasons)}"
                                                        )
                                                    if (
                                                        comparison.outcome
                                                        is ComparisonOutcome.EQUAL
                                                    ):
                                                        equal_pairs.add(
                                                            {
                                                                "edge_id": _edge_id(comparison),
                                                                "archive_revision_id": (
                                                                    archive_revision.revision_id
                                                                ),
                                                                "rest_revision_id": (
                                                                    rest.revision_id
                                                                ),
                                                            }
                                                        )
                                    equal_root = equal_pairs.finish()

                                graph_evidence: list[PrecedenceEvidence] = []
                                with ExitStack() as stack:
                                    expected_pairs = (
                                        iter(())
                                        if equal_root is None
                                        else stack.enter_context(
                                            iter_run(self._storage, equal_root)
                                        )
                                    )
                                    current_edge_rows: Iterator[Mapping[str, Any]] = iter(())
                                    if (
                                        evidence_group is not None
                                        and evidence_group[0] == observation_key
                                    ):
                                        current_edge_rows = evidence_group[1]
                                    expected_pair = next(expected_pairs, None)
                                    current_edge = next(current_edge_rows, None)
                                    previous_edge_id: str | None = None
                                    while current_edge is not None:
                                        edge_id = current_edge["edge_id"]
                                        while (
                                            expected_pair is not None
                                            and expected_pair["edge_id"] < edge_id
                                        ):
                                            expected_pair = next(expected_pairs, None)
                                        if (
                                            expected_pair is None
                                            or expected_pair["edge_id"] != edge_id
                                        ):
                                            raise CatalogIntegrityError(
                                                f"evidence edge {edge_id} does not describe an "
                                                "equal archive / REST pair of the pinned snapshots"
                                            )
                                        if edge_id == previous_edge_id:
                                            raise CatalogIntegrityError(
                                                f"evidence edge {edge_id} is committed twice"
                                            )
                                        archive_revision_id = expected_pair["archive_revision_id"]
                                        rest_revision_id = expected_pair["rest_revision_id"]
                                        if (
                                            current_edge["revision_id"] != archive_revision_id
                                            or current_edge["superseded_revision_id"]
                                            != rest_revision_id
                                        ):
                                            raise CatalogIntegrityError(
                                                f"evidence edge {edge_id} does not match its "
                                                "archive / REST revision ids"
                                            )
                                        found_archive_revision = find_channel_revision(
                                            archive_revision_root,
                                            archive_revision_id,
                                            pinned.archive_snapshot,
                                        )
                                        found_rest_revision = find_channel_revision(
                                            rest_revision_root,
                                            rest_revision_id,
                                            pinned.rest_snapshot,
                                        )
                                        if (
                                            found_archive_revision is None
                                            or found_rest_revision is None
                                        ):
                                            raise CatalogIntegrityError(
                                                f"evidence edge {edge_id} references a missing "
                                                "source revision"
                                            )
                                        comparison = compare_channels(
                                            found_archive_revision, found_rest_revision
                                        )
                                        if (
                                            comparison.outcome is not ComparisonOutcome.EQUAL
                                            or _edge_id(comparison) != edge_id
                                        ):
                                            raise CatalogIntegrityError(
                                                f"evidence edge {edge_id} does not describe an "
                                                "equal archive / REST pair of the pinned snapshots"
                                            )
                                        edge = self._existing_edges(
                                            (current_edge,), {edge_id: comparison}
                                        )[edge_id]
                                        graph_evidence.append(edge.evidence)
                                        output.add(edge.row())
                                        previous_edge_id = edge_id
                                        expected_pair = next(expected_pairs, None)
                                        current_edge = next(current_edge_rows, None)
                                if has_current_edges:
                                    evidence_group = next(evidence_groups, None)
                                assemble_channel_graph(
                                    archive_records,
                                    rest_records,
                                    graph_evidence,
                                )
                            if (
                                rest_group is not None
                                or archive_group is not None
                                or evidence_group is not None
                            ):
                                raise CatalogIntegrityError(
                                    "a staged row references a key absent from the pinned REST day"
                                )
                    archive_parent_root = archive_parents.finish()
                    if archive_parent_root is not None:
                        with (
                            iter_run(self._storage, archive_parent_root) as parent_rows,
                            RunSetBuilder(
                                self._storage,
                                key=lambda row: row["arrival_seq"],
                                capacity=params.row_capacity,
                                merge_fanout=params.merge_fanout,
                                limits=params.limits,
                            ) as arrivals,
                        ):
                            previous_archive_id: str | None = None
                            for parent_row in parent_rows:
                                archive_id = parent_row["archive_revision_id"]
                                if archive_id == previous_archive_id:
                                    continue
                                arrivals.add({"arrival_seq": parent_row["arrival_seq"]})
                                previous_archive_id = archive_id
                            archive_arrival_root = arrivals.finish()
                        if archive_arrival_root is not None:
                            with iter_run(self._storage, archive_arrival_root) as arrival_rows:
                                previous_arrival_seq: int | None = None
                                for arrival in arrival_rows:
                                    value = arrival["arrival_seq"]
                                    if value == previous_arrival_seq:
                                        raise CatalogIntegrityError(
                                            f"archive arrival block {value} is held twice"
                                        )
                                    previous_arrival_seq = value
                except CatalogIntegrityError:
                    if tuple(self._head(table) for table in tables) != (
                        pinned.rest_snapshot,
                        pinned.responses_snapshot,
                        pinned.archive_snapshot,
                        pinned.archives_snapshot,
                        pinned.evidence_snapshot,
                    ):
                        continue
                    raise
                if tuple(self._head(table) for table in tables) != (
                    pinned.rest_snapshot,
                    pinned.responses_snapshot,
                    pinned.archive_snapshot,
                    pinned.archives_snapshot,
                    pinned.evidence_snapshot,
                ):
                    continue
                return output.finish()
        raise ChannelReconcileConflict(
            "the tables kept moving: no verified edge stream was possible after "
            f"{_ATTEMPTS} attempts"
        )

    def _by_keys(
        self, definition: RegisteredTableDefinition, symbol: str, keys: Sequence[str]
    ) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        for offset in range(0, len(keys), _KEY_CHUNK):
            rows.extend(
                self._scan(
                    definition,
                    _all(
                        _equals("symbol", symbol),
                        _member("observation_key", keys[offset : offset + _KEY_CHUNK]),
                    ),
                )
            )
        return rows

    def _scan(
        self, definition: RegisteredTableDefinition, row_filter: BooleanExpression
    ) -> list[Mapping[str, Any]]:
        columns = tuple(field.name for field in definition.arrow_schema)
        rows: list[Mapping[str, Any]] = self._adapter.scan_columns(
            definition.table, columns=columns, row_filter=row_filter
        ).to_pylist()
        return rows

    def _scan_batch_rows(
        self,
        definition: RegisteredTableDefinition,
        row_filter: BooleanExpression,
        snapshot_id: str | None,
        *,
        columns: Sequence[str] | None = None,
    ) -> Iterable[Mapping[str, Any]]:
        """Yield projected rows from a fixed snapshot and always release the batch reader."""
        if snapshot_id is None:
            return
        scan_batches = getattr(self._adapter, "scan_column_batches", None)
        if not callable(scan_batches):
            raise ChannelReconcileError(
                "bounded verified-edge streaming requires scan_column_batches"
            )
        reader = scan_batches(
            definition.table,
            columns=(
                tuple(field.name for field in definition.arrow_schema)
                if columns is None
                else tuple(columns)
            ),
            row_filter=row_filter,
            snapshot_id=snapshot_id,
        )
        try:
            for batch in reader:
                yield from batch.to_pylist()
        finally:
            close = getattr(reader, "close", None)
            if callable(close):
                close()

    def _head(self, table: str) -> str | None:
        info = self._adapter.load_table(table)
        if info is None:
            raise TableNotFound(f"table {table} does not exist; create the Phase 1 tables first")
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id


def _edge_id(comparison: ChannelComparison) -> str:
    return rest_identity.edge_id(
        DELIVERY_CHANNEL_BINDING,
        comparison.observation_key,
        comparison.archive.revision_id,
        comparison.rest.revision_id,
    )


def _utc_day(value: object) -> date:
    """The UTC day of a persisted time column; anything but an aware UTC instant fails closed."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != _ZERO:
        raise CatalogIntegrityError(f"a REST row carries a time that is not UTC: {value!r}")
    return value.date()


def _edge_batch_prefix(partition: tuple[str, str, date]) -> str:
    """Batch ids of one partition's edges share this prefix (D3E-R3: provenance is findable)."""
    data_type, symbol, day = partition
    policy = DELIVERY_CHANNEL_BINDING
    return f"{policy.policy_id}@{policy.version}.edges.{data_type}.{symbol}.{day.isoformat()}."


def _edge_batch_id(partition: tuple[str, str, date], edge_ids: Iterable[str]) -> str:
    """Content-derived batch id: the same missing edge set always maps to the same batch."""
    digest = hashlib.sha256("\n".join(sorted(edge_ids)).encode("utf-8")).hexdigest()
    return f"{_edge_batch_prefix(partition)}{digest}"


def _normalised_row(edge: ChannelEdge) -> Mapping[str, Any]:
    """The edge row exactly as the evidence table stores and returns it."""
    batch = pa.Table.from_pylist([edge.row()], schema=BINANCE_SPOT_PRECEDENCE_EVIDENCE.arrow_schema)
    rows: list[Mapping[str, Any]] = batch.to_pylist()
    return rows[0]

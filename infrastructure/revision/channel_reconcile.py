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
from collections.abc import Callable, Iterable, Mapping, Sequence
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
from infrastructure.revision.row_integrity import PersistedRowVerifier
from infrastructure.revision.store import BatchCommit, RevisionCatalog

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
class _Plan:
    partition: tuple[str, str, date]
    pinned: _Pinned
    comparisons: tuple[ChannelComparison, ...]
    findings: tuple[ChannelFinding, ...]
    existing: Mapping[str, ChannelEdge]
    missing: tuple[ChannelComparison, ...]
    records: Mapping[str, tuple[tuple[RevisionRecord, ...], tuple[RevisionRecord, ...]]]


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

    def _plan(self, data_type: str, symbol: str, day: date) -> _Plan:
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
        self._verify_edge_provenance(data_type, symbol, day, pinned)
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
        self, data_type: str, symbol: str, day: date, pinned: _Pinned
    ) -> None:
        """Every committed edge row of the partition is exactly what an edge batch committed.

        D3E-R3: an edge row is not a free input either. Each of this partition's edge batch
        snapshots (id prefix ``<policy>@<version>.edges.<data type>.<symbol>.<day>.``) is
        re-read by time travel — rows at the snapshot minus rows at its parent — and must match
        its committed row count, content-derived id and fingerprint. The partition's current edge
        rows must then be exactly those committed rows: a row no edge batch committed (forged or
        re-committed with another ``knowledge_time``) or a committed row that is gone fails
        closed.
        """
        definition = BINANCE_SPOT_PRECEDENCE_EVIDENCE
        head = pinned.evidence_snapshot
        keys = sorted({row["observation_key"] for row in pinned.rest_rows})
        prefix = _edge_batch_prefix((data_type, symbol, day))
        committed: dict[str, Mapping[str, Any]] = {}
        snapshot_id = head
        while snapshot_id is not None:
            snapshot = self._adapter.get_snapshot(EVIDENCE_TABLE, snapshot_id)
            parent = snapshot.parent_snapshot_id
            if snapshot.batch_id is not None and snapshot.batch_id.startswith(prefix):
                after = self._rows_at(keys, snapshot.snapshot_id)
                before = {} if parent is None else self._rows_at(keys, parent)
                added = {edge_id: row for edge_id, row in after.items() if edge_id not in before}
                ordered = [added[edge_id] for edge_id in sorted(added)]
                table = pa.Table.from_pylist(
                    [dict(row) for row in ordered], schema=definition.arrow_schema
                )
                if (
                    len(ordered) != snapshot.added_rows
                    or snapshot.batch_id != _edge_batch_id((data_type, symbol, day), added)
                    or snapshot.batch_fingerprint != definition.fingerprint_rule.fingerprint(table)
                ):
                    raise CatalogIntegrityError(
                        f"edge batch {snapshot.batch_id} no longer reproduces from its snapshot"
                    )
                for edge_id, row in added.items():
                    if edge_id in committed:
                        raise CatalogIntegrityError(f"evidence edge {edge_id} is committed twice")
                    committed[edge_id] = row
            snapshot_id = parent
        current = {row["edge_id"]: row for row in pinned.evidence_rows}
        for edge_id, row in current.items():
            if committed.get(edge_id) != row:
                raise CatalogIntegrityError(
                    f"evidence edge {edge_id} is not exactly what an edge batch of this "
                    "partition committed"
                )
        missing = sorted(set(committed) - set(current))
        if missing:
            raise CatalogIntegrityError(f"committed evidence edge {missing[0]} is gone")

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
                    compare_channels(archive, rest), knowledge_time=row["knowledge_time"]
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

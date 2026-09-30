"""Listing history derivation and its point-in-time read (Phase 1 E2; ADR-0029 §2 / §3).

``ListingDeriver`` turns proven exchangeInfo snapshot revisions into
``canonical.instrument_listings`` revisions under ``binance.spot.listing-status@1.0.0`` and
``binance.spot.listing-observation@1.0.0`` (``listing_rules``):

1. **pin** — the listing head is read first, then the snapshot-table head (every listing batch
   therefore names a Raw snapshot inside the pinned Raw history); all reads time-travel to them;
2. **prove** (no clock, no write) — every Raw snapshot row is rebuilt from its own first
   delivery's verified checkpoint and the Raw history must be nothing but one-row appends
   (``ExchangeInfoRowVerifier``). Every committed listing batch is then re-derived: its batch id
   names the Raw snapshot it read; the derivation over exactly the Raw rows of that snapshot, minus
   what was committed before the batch, must be exactly the batch's rows (content, order,
   consecutive ``arrival_seq`` above everything earlier, one ``knowledge_time`` not before any Raw
   ``knowledge_time`` it read, fingerprint, row counts). Nothing else may ever have been committed
   to the table;
3. **write** — only the derived revisions not yet committed, as **one** batch
   ``binance.spot.listing-status@1.0.0.from.<raw snapshot id>`` with the expected parent, one
   reading of the injected UTC clock; then the new head is proven again (read-back).

Derivation is a pure function of the set of snapshots, so re-running it is idempotent and
arrival order never changes a precedence edge. A snapshot that arrives late may insert a change
between committed observations (the missing revisions are appended) or move a committed change
point; the second leaves a committed revision outside the current chain — reported as
``listing_history_diverged`` and, being a second head, refused by every point-in-time read after
its availability (fail closed, never rewritten).

``listing_at`` is the point-in-time read a universe build consumes (ADR-0024 §3, ADR-0029 §3). It
answers ``constructible`` only if, at ``(simulation_time, knowledge_cutoff)``: one episode is
known, exactly one maximal head is available, the latest visible observation is resolved and
untied, and the chain re-derived from the visible observations ends at that head. Otherwise it is
``unconstructible`` with a stable reason — before the first local observation it is always
``no_visible_listing`` (the historical-availability evidence gap, ADR-0029 "open obligations").

``listing_at`` optionally takes ``pit``: a ``PointInTimeSpec`` that may bind the ADR-0051 listing
backfill assumption (``infrastructure.universe.listing_assumption``, D-LIST). When it is bound (and
only then) a read that would otherwise be ``no_visible_listing`` because ``simulation_time``
precedes the episode's first local observation may instead be answered from the assumption, under
its own applicability conditions (never for a second episode, never a suspended / delisted
inference, only the one venue symbol its policy table names). Omitting ``pit`` (the default) leaves
every result exactly as before this assumption existed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from core.contracts.catalog import (
    BatchConflict,
    CommitConflict,
    CommitRequest,
    SnapshotInfo,
    TableNotFound,
)
from core.contracts.revision import (
    PointInTimeSpec,
    PolicyBinding,
    PrecedenceEvidence,
    RevisionRecord,
)
from core.contracts.storage import StorageAdapter
from core.contracts.universe import (
    ListingHistory,
    ListingRevision,
    SelectedRevisionLineage,
)
from infrastructure import contract_version
from infrastructure.canonical import listing_rules as lr
from infrastructure.canonical.listing_bounded_verify import (
    BoundedListingReplayProof,
    FindingSink,
    verify_listing_history_bounded,
)
from infrastructure.canonical.listing_history_runs import listing_history_prefix_run
from infrastructure.canonical.listing_prefix_index import (
    ListingPrefixIndex,
    build_listing_prefix_index,
)
from infrastructure.canonical.listing_runs import listing_observations_run
from infrastructure.canonical.rules import SYMBOLS
from infrastructure.catalog.bounded_metadata import BoundedIcebergMetadata, BoundedMetadataLimits
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_EXCHANGE_INFO,
    CANONICAL_INSTRUMENT_LISTINGS,
)
from infrastructure.revision.exchange_info_store import (
    ExchangeInfoRowVerifier,
    ProvenSnapshotTable,
)
from infrastructure.revision.row_integrity import batch, check_batch_snapshot, history_from
from infrastructure.revision.store import BatchCommit, RevisionCatalog
from infrastructure.streaming.runs import RunLimits, RunRef
from infrastructure.universe import listing_assumption as backfill

__all__ = [
    "FINDING_HISTORY_DIVERGED",
    "BoundedListingReplayInputs",
    "LISTINGS_TABLE",
    "ListingDeriveConflict",
    "ListingDeriveError",
    "ListingDeriver",
    "ListingPointInTime",
    "ListingUnconstructible",
    "ListingsDerived",
    "ListingsVerified",
    "UnconstructibleReason",
]

LISTINGS_TABLE: Final = CANONICAL_INSTRUMENT_LISTINGS.table
_RAW_TABLE: Final = BINANCE_SPOT_EXCHANGE_INFO.table
_ATTEMPTS: Final = 8
_ZERO: Final = timedelta(0)
FINDING_HISTORY_DIVERGED: Final = "listing_history_diverged"


class ListingDeriveError(Exception):
    """Base class of listing derivation failures that must not be papered over."""


class ListingDeriveConflict(ListingDeriveError):
    """The derivation cannot be done honestly now (clock, contention); nothing is committed."""


class ListingUnconstructible(Exception):
    """A universe build asked for a listing the point-in-time read refuses (fail closed)."""


class UnconstructibleReason:
    """Stable reasons of an ``unconstructible`` point-in-time listing read."""

    NO_VISIBLE_LISTING: Final = "no_visible_listing"
    MULTIPLE_EPISODES: Final = "multiple_episodes"
    COMPETING_HEADS: Final = "competing_heads"
    UNRESOLVED_OBSERVATION: Final = "unresolved_observation"
    OBSERVATION_TIE: Final = "observation_tie"
    LISTING_NOT_DERIVED: Final = "listing_not_derived"


# =========================================================================================
# results
# =========================================================================================


@dataclass(frozen=True, slots=True)
class ListingsVerified:
    """The proven state at one pair of heads (no write, no clock)."""

    raw_snapshot_id: str | None
    listing_snapshot_id: str | None
    #: Every committed listing row, by revision id.
    rows: Mapping[str, Mapping[str, Any]]
    #: The chains derived from every proven snapshot, per venue symbol.
    chains: Mapping[str, lr.ListingChain]
    #: Committed revisions outside the current chains (late snapshots moved a change point).
    diverged: tuple[str, ...]
    findings: tuple[lr.ListingFinding, ...]


@dataclass(frozen=True, slots=True)
class ListingsDerived:
    """Result of one ``derive`` call."""

    raw_snapshot_id: str | None
    listing_snapshot_id: str | None
    #: Revision ids this call committed, in batch order (empty when nothing was missing).
    new_revision_ids: tuple[str, ...]
    commit: BatchCommit | None
    diverged: tuple[str, ...]
    findings: tuple[lr.ListingFinding, ...]

    @property
    def replayed(self) -> bool:
        return self.commit is None


@dataclass(frozen=True, slots=True)
class BoundedListingReplayInputs:
    """Immutable scratch references prepared for bounded historical replay.

    These refs are not themselves an acceptance verdict: the verifier must consume the mapped
    listing-batch run and compare each historical batch before reporting success.
    """

    raw_snapshot_id: str | None
    listing_snapshot_id: str | None
    raw_metadata: BoundedIcebergMetadata
    listing_metadata: BoundedIcebergMetadata
    raw_rows: RunRef | None
    observations: RunRef | None
    prefix_index: ListingPrefixIndex | None
    prefix_roots: RunRef | None
    listing_batches: RunRef | None


@dataclass(frozen=True, slots=True)
class ListingPointInTime:
    """The point-in-time listing of one venue symbol (``listing`` is set iff constructible)."""

    venue_symbol: str
    simulation_time: datetime
    knowledge_cutoff: datetime
    constructible: bool
    reason: str | None
    listing: ListingRevision | None
    tradable: bool | None
    lineage: SelectedRevisionLineage | None
    #: The selected revision's availability evidence gap (the manifest binds it to the quality
    #: report that records it; writing that report is not this read's business).
    evidence_gap: str | None = None
    detail: str = ""
    #: Set only when this read's ``constructible`` came from the ADR-0051 backfill assumption
    #: (D-LIST) rather than a genuinely available revision (``listing`` is still the real,
    #: unmodified first revision; only the read's decision to treat it as available moved).
    assumed: bool = False
    assumption: backfill.AssumedListingInterval | None = None

    def require_constructible(self) -> ListingRevision:
        """The selected revision, or ``ListingUnconstructible`` (universe build fails closed)."""
        if not self.constructible or self.listing is None:
            raise ListingUnconstructible(
                f"{self.venue_symbol} at simulation {self.simulation_time.isoformat()} / knowledge "
                f"{self.knowledge_cutoff.isoformat()}: {self.reason}; {self.detail}"
            )
        return self.listing


# =========================================================================================
# the deriver
# =========================================================================================


class ListingDeriver:
    """Derives, proves and reads the Binance spot listing history (one writer per table)."""

    def __init__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        *,
        market_data_base_url: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapter = adapter
        self._storage = storage
        self._clock = clock or (lambda: datetime.now(UTC))
        try:
            self._verifier = ExchangeInfoRowVerifier(adapter, storage, market_data_base_url)
        except ValueError as exc:
            raise ListingDeriveError(f"invalid market-data origin: {exc}") from None

    def close(self) -> None:
        self._verifier.close()

    def bounded_replay_inputs(
        self,
        *,
        snapshot_ids: Mapping[str, str | None] | None = None,
        scratch_storage: StorageAdapter,
        metadata_limits: BoundedMetadataLimits,
        capacity: int,
        merge_fanout: int,
        limits: RunLimits,
        max_record_bytes: int,
        max_run_object_bytes: int,
        prefix_leaf_max_records: int,
        prefix_fanout: int,
        prefix_max_node_bytes: int,
        prefix_max_record_bytes: int,
    ) -> BoundedListingReplayInputs:
        """Prepare bounded Raw, observation and per-snapshot prefix runs at pinned table heads.

        This is a preparation primitive, not a proof of persisted listing batch contents. The
        exact replay verifier consumes the returned refs and checks each batch fingerprint/row.
        Metadata ancestry is parsed under the explicit ``metadata_limits``. Backend scan planning
        and total process RSS remain outside this path's guarantee, so this does not claim
        E1-CAP-1.
        """
        pin = getattr(self._adapter, "pin_bounded_metadata", None)
        if not callable(pin):
            raise CatalogIntegrityError(
                "bounded Listing verification requires PyIceberg bounded metadata support"
            )
        if snapshot_ids is not None:
            if set(snapshot_ids) != {LISTINGS_TABLE, _RAW_TABLE}:
                raise CatalogIntegrityError(
                    "bounded Listing replay needs exact Listing and Raw snapshot bindings"
                )
            pin_at = getattr(self._adapter, "pin_bounded_metadata_at", None)
            if not callable(pin_at):
                raise CatalogIntegrityError(
                    "bounded Listing replay cannot pin caller-supplied PIT snapshot IDs"
                )
            listing_metadata = pin_at(
                LISTINGS_TABLE,
                snapshot_ids[LISTINGS_TABLE],
                storage=scratch_storage,
                limits=metadata_limits,
            )
            raw_metadata = pin_at(
                _RAW_TABLE,
                snapshot_ids[_RAW_TABLE],
                storage=scratch_storage,
                limits=metadata_limits,
            )
        else:
            listing_metadata = pin(
                LISTINGS_TABLE,
                storage=scratch_storage,
                limits=metadata_limits,
            )
            raw_metadata = pin(
                _RAW_TABLE,
                storage=scratch_storage,
                limits=metadata_limits,
            )
        raw_head = _pinned_head(raw_metadata)
        listing_head = _pinned_head(listing_metadata)
        raw_rows = self._verifier.verify_table_bounded(
            raw_head,
            scratch_storage=scratch_storage,
            bounded_metadata=raw_metadata,
            capacity=capacity,
            merge_fanout=merge_fanout,
            limits=limits,
            max_record_bytes=max_record_bytes,
        )
        if raw_rows is None:
            if listing_head is not None:
                raise CatalogIntegrityError(
                    f"{LISTINGS_TABLE} has history but the pinned Raw table is empty"
                )
            return BoundedListingReplayInputs(
                raw_snapshot_id=None,
                listing_snapshot_id=None,
                raw_metadata=raw_metadata,
                listing_metadata=listing_metadata,
                raw_rows=None,
                observations=None,
                prefix_index=None,
                prefix_roots=None,
                listing_batches=None,
            )
        observations = listing_observations_run(
            raw_rows,
            storage=scratch_storage,
            capacity=capacity,
            merge_fanout=merge_fanout,
            limits=limits,
            max_record_bytes=max_record_bytes,
            max_run_object_bytes=max_run_object_bytes,
        )
        if observations is None:
            raise CatalogIntegrityError("a non-empty Raw proof produced no observation run")
        prefix_index, prefix_roots = build_listing_prefix_index(
            raw_rows,
            storage=scratch_storage,
            raw_sort_capacity=capacity,
            raw_sort_merge_fanout=merge_fanout,
            raw_sort_limits=limits,
            raw_max_record_bytes=max_record_bytes,
            raw_max_run_object_bytes=max_run_object_bytes,
            root_refs_capacity=capacity,
            root_refs_merge_fanout=merge_fanout,
            root_refs_limits=limits,
            leaf_max_records=prefix_leaf_max_records,
            fanout=prefix_fanout,
            max_node_bytes=prefix_max_node_bytes,
            max_record_bytes=prefix_max_record_bytes,
        )
        listing_batches = listing_history_prefix_run(
            self._adapter,
            listing_head,
            prefix_roots,
            bounded_metadata=listing_metadata,
            storage=scratch_storage,
            capacity=capacity,
            merge_fanout=merge_fanout,
            limits=limits,
            max_record_bytes=max_record_bytes,
            max_run_object_bytes=max_run_object_bytes,
        )
        return BoundedListingReplayInputs(
            raw_snapshot_id=raw_head,
            listing_snapshot_id=listing_head,
            raw_metadata=raw_metadata,
            listing_metadata=listing_metadata,
            raw_rows=raw_rows,
            observations=observations,
            prefix_index=prefix_index,
            prefix_roots=prefix_roots,
            listing_batches=listing_batches,
        )

    def verify_bounded(
        self,
        *,
        snapshot_ids: Mapping[str, str | None] | None = None,
        scratch_storage: StorageAdapter,
        metadata_limits: BoundedMetadataLimits,
        capacity: int,
        merge_fanout: int,
        limits: RunLimits,
        max_record_bytes: int,
        max_run_object_bytes: int,
        prefix_leaf_max_records: int,
        prefix_fanout: int,
        prefix_max_node_bytes: int,
        prefix_max_record_bytes: int,
        row_chunk_capacity: int,
        max_hash_chunk_bytes: int,
        finding_sink: FindingSink,
    ) -> BoundedListingReplayProof:
        """Prove persisted Listing batches with bounded row streams and historical Raw prefixes.

        The v1/v2 ``verify`` API and its materialized result are unchanged. This additive path
        writes only caller-provided scratch storage, does not read the clock, and checks each
        persisted batch's rows, knowledge-time floor, recorded contract version, and exact C3
        batch fingerprint. Metadata ancestry uses the explicit bounded pin supplied by this
        method; backend scan planning and total process RSS remain outside this guarantee.
        """
        inputs = self.bounded_replay_inputs(
            snapshot_ids=snapshot_ids,
            scratch_storage=scratch_storage,
            metadata_limits=metadata_limits,
            capacity=capacity,
            merge_fanout=merge_fanout,
            limits=limits,
            max_record_bytes=max_record_bytes,
            max_run_object_bytes=max_run_object_bytes,
            prefix_leaf_max_records=prefix_leaf_max_records,
            prefix_fanout=prefix_fanout,
            prefix_max_node_bytes=prefix_max_node_bytes,
            prefix_max_record_bytes=prefix_max_record_bytes,
        )
        return verify_listing_history_bounded(
            self._adapter,
            inputs,
            evidence_storage=self._storage,
            scratch_storage=scratch_storage,
            capacity=capacity,
            merge_fanout=merge_fanout,
            limits=limits,
            max_record_bytes=max_record_bytes,
            max_run_object_bytes=max_run_object_bytes,
            row_chunk_capacity=row_chunk_capacity,
            max_hash_chunk_bytes=max_hash_chunk_bytes,
            listing_metadata=inputs.listing_metadata,
            prefix_leaf_max_records=prefix_leaf_max_records,
            prefix_fanout=prefix_fanout,
            prefix_max_node_bytes=prefix_max_node_bytes,
            prefix_max_record_bytes=prefix_max_record_bytes,
            finding_sink=finding_sink,
        )

    # ------------------------------------------------------------------ entry points

    def derive(self) -> ListingsDerived:
        """Append every derived listing revision not yet committed (idempotent, re-runnable)."""
        last_error: Exception | None = None
        for _ in range(_ATTEMPTS):
            listing_head, raw_head = self._heads()
            state, raw = self._prove(listing_head, raw_head)
            missing = _missing(state.chains, state.rows)
            if not missing:
                return ListingsDerived(
                    raw_snapshot_id=raw_head,
                    listing_snapshot_id=listing_head,
                    new_revision_ids=(),
                    commit=None,
                    diverged=state.diverged,
                    findings=state.findings,
                )
            assert raw_head is not None  # a derived revision needs a snapshot row
            ready = self._ready(raw.rows)
            base = _next_arrival(state.rows)
            rows = [
                lr.listing_columns(planned, arrival_seq=base + index, knowledge_time=ready)
                for index, planned in enumerate(missing)
            ]
            table = batch(CANONICAL_INSTRUMENT_LISTINGS, rows)
            request = CommitRequest(
                table=LISTINGS_TABLE,
                batch_id=lr.batch_id_for(raw_head),
                batch_fingerprint=CANONICAL_INSTRUMENT_LISTINGS.fingerprint_rule.fingerprint(table),
                row_count=len(rows),
                expected_parent_snapshot_id=listing_head,
            )
            try:
                result = self._adapter.commit_batch(request, table)
            except (CommitConflict, BatchConflict) as exc:
                last_error = exc  # another writer moved the table: prove again, adopt its work
                continue
            new_head = result.snapshot.snapshot_id
            after, _ = self._prove(new_head, raw_head)
            ids = tuple(planned.revision_id for planned in missing)
            if any(
                after.rows.get(revision) != row for revision, row in zip(ids, rows, strict=True)
            ):
                raise CatalogIntegrityError(f"{LISTINGS_TABLE} does not read back as committed")
            return ListingsDerived(
                raw_snapshot_id=raw_head,
                listing_snapshot_id=new_head,
                new_revision_ids=ids,
                commit=BatchCommit(
                    table=LISTINGS_TABLE,
                    batch_id=request.batch_id,
                    snapshot_id=new_head,
                    outcome=result.outcome,
                    row_count=len(rows),
                ),
                diverged=after.diverged,
                findings=after.findings,
            )
        raise ListingDeriveConflict(
            f"{LISTINGS_TABLE}: the derivation lost {_ATTEMPTS} commit races"
        ) from last_error

    def verify(self) -> ListingsVerified:
        """The proven state at the current heads; nothing is written and no clock is read."""
        listing_head, raw_head = self._heads()
        return self._prove(listing_head, raw_head)[0]

    def listing_at(
        self,
        venue_symbol: str,
        simulation_time: datetime,
        knowledge_cutoff: datetime,
        *,
        pit: PointInTimeSpec | None = None,
    ) -> ListingPointInTime:
        """What a universe build may use for ``venue_symbol`` at the two cutoffs (fail closed).

        ``pit``, when given, may bind the ADR-0051 listing backfill assumption (D-LIST); omitting
        it (the default) is byte-for-byte the read this method gave before that assumption
        existed.
        """
        for label, value in (
            ("simulation_time", simulation_time),
            ("knowledge_cutoff", knowledge_cutoff),
        ):
            if (
                not isinstance(value, datetime)
                or value.tzinfo is None
                or value.utcoffset() != _ZERO
            ):
                raise ListingDeriveError(f"{label} must be timezone-aware UTC")
        if venue_symbol not in lr.FIRST_SLICE_ASSETS:
            raise ListingDeriveError(f"{venue_symbol!r} is not a first-slice venue symbol")
        backfill_binding = None if pit is None else backfill.bound_binding(pit)
        listing_head, raw_head = self._heads()
        state, raw = self._prove(listing_head, raw_head)
        return _select(
            venue_symbol,
            simulation_time,
            knowledge_cutoff,
            state,
            raw,
            backfill_binding=backfill_binding,
        )

    # ------------------------------------------------------------------ prove

    def _heads(self) -> tuple[str | None, str | None]:
        """Listing head first, then the Raw head: every listing batch names a pinned Raw state."""
        return self._head(LISTINGS_TABLE), self._head(_RAW_TABLE)

    def _prove(
        self, listing_head: str | None, raw_head: str | None
    ) -> tuple[ListingsVerified, ProvenSnapshotTable]:
        raw = self._verifier.verify_table(raw_head)
        committed: dict[str, Mapping[str, Any]] = {}
        largest: int | None = None
        history = list(history_from(self._adapter, LISTINGS_TABLE, listing_head))
        for snapshot in reversed(history):
            largest = self._prove_batch(snapshot, raw, committed, largest)
        if listing_head is not None:
            final = self._scan_listings(listing_head)
            if {row["revision_id"] for row in final} != set(committed):  # pragma: no cover
                raise CatalogIntegrityError(f"{LISTINGS_TABLE}: rows outside its batches")
        chains = _chains(raw.rows)
        derived = {planned.revision_id for chain in chains.values() for planned in chain.revisions}
        diverged = tuple(sorted(set(committed) - derived))
        findings = [finding for chain in chains.values() for finding in chain.findings]
        for revision in diverged:
            row = committed[revision]
            findings.append(
                lr.ListingFinding(
                    code=FINDING_HISTORY_DIVERGED,
                    venue_symbol=_venue_symbol(row),
                    observed_at=row["ingest_time"],
                    snapshot_revision_ids=(row["lineage_source_revision_id"],),
                    detail=(
                        f"committed listing revision {revision} is not in the chain derived from "
                        "every proven snapshot (a late snapshot moved a change point); it is "
                        "never rewritten and every read where it is available fails closed"
                    ),
                )
            )
        state = ListingsVerified(
            raw_snapshot_id=raw_head,
            listing_snapshot_id=listing_head,
            rows=dict(committed),
            chains=chains,
            diverged=diverged,
            findings=tuple(
                sorted(findings, key=lambda item: (item.venue_symbol, item.observed_at, item.code))
            ),
        )
        return state, raw

    def _prove_batch(
        self,
        snapshot: SnapshotInfo,
        raw: ProvenSnapshotTable,
        committed: dict[str, Mapping[str, Any]],
        largest: int | None,
    ) -> int | None:
        """One committed listing batch re-derived from the Raw snapshot it names."""
        raw_snapshot = lr.parse_batch_id(snapshot.batch_id)
        if raw_snapshot is None:
            raise CatalogIntegrityError(
                f"{LISTINGS_TABLE} snapshot {snapshot.snapshot_id} is not a listing derivation "
                f"batch ({snapshot.batch_id!r})"
            )
        read = raw.rows_at(raw_snapshot)
        rows_now = self._scan_listings(snapshot.snapshot_id)
        by_id: dict[str, Mapping[str, Any]] = {}
        for row in rows_now:
            if row["revision_id"] in by_id:
                raise CatalogIntegrityError(
                    f"{LISTINGS_TABLE}: revision {row['revision_id']} is committed twice"
                )
            by_id[row["revision_id"]] = row
        if not set(committed) <= set(by_id) or any(
            by_id[revision] != row for revision, row in committed.items()
        ):
            raise CatalogIntegrityError(
                f"{LISTINGS_TABLE} snapshot {snapshot.snapshot_id} lost or changed earlier rows"
            )
        new = {revision: row for revision, row in by_id.items() if revision not in committed}
        chains = _chains(read)
        planned = _missing(chains, committed)
        # Order is proven below: every column is compared, arrival_seq (= plan position) included.
        if not planned or set(new) != {item.revision_id for item in planned}:
            raise CatalogIntegrityError(
                f"{LISTINGS_TABLE} batch {snapshot.batch_id} is not what Raw snapshot "
                f"{raw_snapshot} derives to"
            )
        for chain in chains.values():
            for item in chain.revisions:
                earlier = committed.get(item.revision_id)
                if earlier is not None and list(earlier["supersedes"]) != list(item.supersedes):
                    raise CatalogIntegrityError(
                        f"{LISTINGS_TABLE}: revision {item.revision_id} supersedes another "
                        "revision than its derived chain"
                    )
        readies = {row["knowledge_time"] for row in new.values()}
        if len(readies) != 1:
            raise CatalogIntegrityError(
                f"{LISTINGS_TABLE} batch {snapshot.batch_id} rows disagree on knowledge_time"
            )
        [ready] = readies
        if read and ready < max(row["knowledge_time"] for row in read):
            raise CatalogIntegrityError(
                f"{LISTINGS_TABLE} batch {snapshot.batch_id} is known before a Raw row it read"
            )
        base = 0 if largest is None else largest + 1
        # One batch is one write group: re-derived at the version it was committed with
        # (ADR-0052 versioned replay, V1).
        version = contract_version.recorded_version(
            [row["contract_schema_version"] for row in new.values()],
            what=f"{LISTINGS_TABLE} batch {snapshot.batch_id}",
        )
        try:
            rebuilt = [
                lr.listing_columns(
                    item,
                    arrival_seq=base + index,
                    knowledge_time=ready,
                    contract_schema_version=version,
                )
                for index, item in enumerate(planned)
            ]
        except lr.ListingRuleViolation as exc:
            raise CatalogIntegrityError(
                f"{LISTINGS_TABLE} batch {snapshot.batch_id} is not lawful: {exc}"
            ) from None
        for row in rebuilt:
            stored = new[row["revision_id"]]
            mismatched = sorted(name for name, value in row.items() if stored.get(name) != value)
            if mismatched:
                raise CatalogIntegrityError(
                    f"{LISTINGS_TABLE}: revision {row['revision_id']} disagrees with its "
                    f"re-derivation: {mismatched}"
                )
        if snapshot.total_rows != len(by_id):
            raise CatalogIntegrityError(
                f"{LISTINGS_TABLE} snapshot {snapshot.snapshot_id} counts other rows"
            )
        check_batch_snapshot(
            CANONICAL_INSTRUMENT_LISTINGS, snapshot.batch_id or "", snapshot, rebuilt
        )
        committed.update(new)
        return base + len(rebuilt) - 1

    def _scan_listings(self, snapshot_id: str) -> list[Mapping[str, Any]]:
        columns = tuple(field.name for field in CANONICAL_INSTRUMENT_LISTINGS.arrow_schema)
        rows: list[Mapping[str, Any]] = self._adapter.scan_columns(
            LISTINGS_TABLE, columns=columns, snapshot_id=snapshot_id
        ).to_pylist()
        return rows

    # ------------------------------------------------------------------ helpers

    def _ready(self, raw_rows: Sequence[Mapping[str, Any]]) -> datetime:
        """The derivation's one clock reading: never before a Raw ``knowledge_time`` it read."""
        ready = self._clock()
        if not isinstance(ready, datetime) or ready.tzinfo is None or ready.utcoffset() != _ZERO:
            raise ListingDeriveError("the clock must return timezone-aware UTC")
        floor = max(row["knowledge_time"] for row in raw_rows)
        if ready < floor:
            raise ListingDeriveConflict(
                "the derivation clock precedes a Raw knowledge_time: refusing to backfill"
            )
        return ready

    def _head(self, table: str) -> str | None:
        info = self._adapter.load_table(table)
        if info is None:
            raise TableNotFound(f"table {table} does not exist; create the Phase 1 tables first")
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id


# =========================================================================================
# pure helpers
# =========================================================================================


def _pinned_head(metadata: BoundedIcebergMetadata) -> str | None:
    selected = metadata.selected_snapshot_id
    if selected is None:
        return None
    metadata.require_snapshot(selected)
    return selected


def _chains(rows: Sequence[Mapping[str, Any]]) -> dict[str, lr.ListingChain]:
    observations = lr.observations_from_rows(rows)
    return {
        symbol: lr.derive_chain(symbol, observations[symbol]) for symbol in sorted(observations)
    }


def _missing(
    chains: Mapping[str, lr.ListingChain], committed: Mapping[str, Mapping[str, Any]]
) -> list[lr.PlannedListing]:
    """Derived revisions not yet committed: symbols sorted, chain order (the batch order)."""
    return [
        planned
        for symbol in sorted(chains)
        for planned in chains[symbol].revisions
        if planned.revision_id not in committed
    ]


def _next_arrival(rows: Mapping[str, Mapping[str, Any]]) -> int:
    return 0 if not rows else max(row["arrival_seq"] for row in rows.values()) + 1


def _venue_symbol(row: Mapping[str, Any]) -> str:
    for venue_symbol, instrument in SYMBOLS.items():
        if instrument.symbol == row["episode_symbol"]:
            return venue_symbol
    return str(row["episode_symbol"])


def _heads_among(
    candidates: Sequence[RevisionRecord],
    known: Sequence[RevisionRecord],
    evidence: Sequence[PrecedenceEvidence],
) -> list[str]:
    """Candidates no other candidate supersedes, directly or through any known revision."""
    edges: dict[str, set[str]] = {}
    for record in known:
        edges.setdefault(record.revision_id, set()).update(record.supersedes)
    for item in evidence:
        edges.setdefault(item.revision_id, set()).add(item.superseded_revision_id)
    ids = {record.revision_id for record in candidates}
    beaten: set[str] = set()
    for start in ids:
        stack = list(edges.get(start, ()))
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(edges.get(node, ()))
        beaten.update(seen & ids)
    return sorted(ids - beaten)


def _select(
    venue_symbol: str,
    simulation_time: datetime,
    knowledge_cutoff: datetime,
    state: ListingsVerified,
    raw: ProvenSnapshotTable,
    *,
    backfill_binding: PolicyBinding | None = None,
) -> ListingPointInTime:
    def refuse(reason: str, detail: str) -> ListingPointInTime:
        return ListingPointInTime(
            venue_symbol=venue_symbol,
            simulation_time=simulation_time,
            knowledge_cutoff=knowledge_cutoff,
            constructible=False,
            reason=reason,
            listing=None,
            tradable=None,
            lineage=None,
            detail=detail,
        )

    canonical = SYMBOLS[venue_symbol].symbol
    known_rows = [
        row
        for row in state.rows.values()
        if row["episode_symbol"] == canonical and row["knowledge_time"] <= knowledge_cutoff
    ]
    records: list[RevisionRecord] = []
    evidence: list[PrecedenceEvidence] = []
    revisions: list[ListingRevision] = []
    for row in known_rows:
        record, edges = lr.listing_record_from_row(row)
        records.append(record)
        evidence.extend(item for item in edges if item.knowledge_time <= knowledge_cutoff)
        revisions.append(lr.listing_revision_from_row(row))
    try:
        ListingHistory(revisions=tuple(revisions), precedence_evidence=tuple(evidence))
    except ValueError as exc:
        raise CatalogIntegrityError(
            f"{LISTINGS_TABLE}: not a lawful listing history: {exc}"
        ) from None
    # Only episodes a strategy could have seen at simulation_time count (E2-R1, cursor review):
    # a revision available later must not change the answer at simulation_time.
    episodes = {
        row["observation_key"] for row in known_rows if row["available_time"] <= simulation_time
    }
    if len(episodes) > 1:
        return refuse(
            UnconstructibleReason.MULTIPLE_EPISODES,
            f"{len(episodes)} episodes of {canonical} are known: the degraded key cannot say "
            "which one is the instrument",
        )
    candidates = [
        record for record in records if record.availability.times.available_time <= simulation_time
    ]
    if not candidates:
        assumed = _assumed_first_candidate(
            venue_symbol,
            simulation_time,
            knowledge_cutoff,
            known_rows,
            raw,
            binding=backfill_binding,
        )
        if assumed is not None:
            return assumed
        return refuse(
            UnconstructibleReason.NO_VISIBLE_LISTING,
            "no listing revision is available: simulation_time precedes the first local "
            "observation or the knowledge cutoff precedes its derivation",
        )
    heads = _heads_among(candidates, records, evidence)
    if len(heads) != 1:
        return refuse(UnconstructibleReason.COMPETING_HEADS, f"maximal heads {heads}")
    [head] = heads
    visible = [
        observation
        for observation in lr.observations_from_rows(raw.rows)[venue_symbol]
        if observation.raw_knowledge_time <= knowledge_cutoff
        and observation.retrieved_at <= simulation_time
    ]
    latest = max(observation.retrieved_at for observation in visible) if visible else None
    newest = [observation for observation in visible if observation.retrieved_at == latest]
    if len(newest) > 1:
        return refuse(UnconstructibleReason.OBSERVATION_TIE, f"{len(newest)} snapshots at {latest}")
    if newest:
        kind, code = lr.classify(newest[0])
        if kind is lr.ObservationClass.UNRESOLVED:
            return refuse(
                UnconstructibleReason.UNRESOLVED_OBSERVATION,
                f"the latest visible observation ({newest[0].snapshot_revision_id} at "
                f"{newest[0].retrieved_at.isoformat()}) is {code}: no inference",
            )
    chain = lr.derive_chain(venue_symbol, visible)
    if chain.stopped_at is not None:
        return refuse(UnconstructibleReason.OBSERVATION_TIE, f"chain stopped at {chain.stopped_at}")
    if not chain.revisions or chain.revisions[-1].revision_id != head:
        return refuse(
            UnconstructibleReason.LISTING_NOT_DERIVED,
            f"the visible observations derive to "
            f"{chain.revisions[-1].revision_id if chain.revisions else 'nothing'}, the selected "
            f"head is {head}: the derivation lags or diverged",
        )
    row = next(row for row in known_rows if row["revision_id"] == head)
    listing = lr.listing_revision_from_row(row)
    tradable = any(
        interval.tradable_from <= simulation_time
        and (interval.tradable_until is None or simulation_time < interval.tradable_until)
        for interval in listing.tradable_intervals
    )
    return ListingPointInTime(
        venue_symbol=venue_symbol,
        simulation_time=simulation_time,
        knowledge_cutoff=knowledge_cutoff,
        constructible=True,
        reason=None,
        listing=listing,
        tradable=tradable,
        lineage=SelectedRevisionLineage(
            canonical_table=LISTINGS_TABLE,
            canonical_revision_id=head,
            raw_table=row["lineage_raw_table"],
            raw_revision_id=row["lineage_raw_revision_id"],
            source_table=row["lineage_source_table"],
            source_revision_id=row["lineage_source_revision_id"],
        ),
        evidence_gap=row["availability_evidence_gap"],
    )


def _assumed_first_candidate(
    venue_symbol: str,
    simulation_time: datetime,
    knowledge_cutoff: datetime,
    known_rows: Sequence[Mapping[str, Any]],
    raw: ProvenSnapshotTable,
    *,
    binding: PolicyBinding | None,
) -> ListingPointInTime | None:
    """The ADR-0051 backfill assumption's answer, or ``None`` when it does not apply (D-LIST).

    Only reached when no revision is genuinely available at ``simulation_time`` (``_select``'s
    ``candidates`` was empty). Fails closed to ``None`` (the caller keeps refusing
    ``no_visible_listing``) unless every condition of ``listing_assumption.assumption_applies``
    holds for the episode's one, real, unmodified first revision.
    """
    if binding is None:
        return None
    floor = backfill.backfill_floor_for(venue_symbol, binding)
    if floor is None:
        return None
    # The committed first revision of this episode (supersedes == ()), if exactly one is visible
    # under the knowledge cutoff. Never observed, or a diverged / duplicated first revision, both
    # leave this list not-a-singleton and the assumption refuses.
    first_rows = [row for row in known_rows if not row["supersedes"]]
    if len(first_rows) != 1:
        return None
    [first_row] = first_rows
    # Re-derive the chain from every knowledge-visible raw observation (no simulation_time gate,
    # unlike the later "visible" chain check): the committed first row must be exactly what an
    # honest re-derivation still gives as this episode's first revision (no competing head, not
    # listing_history_diverged).
    knowledge_visible = [
        observation
        for observation in lr.observations_from_rows(raw.rows)[venue_symbol]
        if observation.raw_knowledge_time <= knowledge_cutoff
    ]
    chain = lr.derive_chain(venue_symbol, knowledge_visible)
    chain_first_id = chain.revisions[0].revision_id if chain.revisions else None
    first_observed_from = first_row["episode_tradable_from"]
    if not backfill.assumption_applies(
        backfill_floor=floor,
        simulation_time=simulation_time,
        first_observed_from=first_observed_from,
        first_status=first_row["status"],
        chain_first_revision_id=chain_first_id,
        committed_first_revision_id=first_row["revision_id"],
    ):
        return None
    interval = backfill.assumed_interval(
        venue_symbol=venue_symbol,
        backfill_floor=floor,
        first_observed_from=first_observed_from,
        stored_available_time=first_row["available_time"],
        binding=binding,
    )
    listing = lr.listing_revision_from_row(first_row)
    return ListingPointInTime(
        venue_symbol=venue_symbol,
        simulation_time=simulation_time,
        knowledge_cutoff=knowledge_cutoff,
        constructible=True,
        reason=None,
        listing=listing,
        tradable=True,
        lineage=SelectedRevisionLineage(
            canonical_table=LISTINGS_TABLE,
            canonical_revision_id=first_row["revision_id"],
            raw_table=first_row["lineage_raw_table"],
            raw_revision_id=first_row["lineage_raw_revision_id"],
            source_table=first_row["lineage_source_table"],
            source_revision_id=first_row["lineage_source_revision_id"],
        ),
        evidence_gap=first_row["availability_evidence_gap"],
        assumed=True,
        assumption=interval,
        detail=(
            f"{backfill.ASSUMPTION_ID}@{binding.version}: assumed listed member from "
            f"{interval.backfill_floor.isoformat()} (backfill_floor) to "
            f"{interval.first_observed_from.isoformat()} (first observed TRADING); not an "
            "exchange-declared listing date"
        ),
    )

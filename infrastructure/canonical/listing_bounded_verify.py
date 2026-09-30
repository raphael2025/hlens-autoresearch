"""Exact historical Listing batch replay over disk-backed Raw prefix inputs."""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from core.contracts.storage import StorageAdapter
from infrastructure import contract_version
from infrastructure.canonical import listing_rules as lr
from infrastructure.canonical.listing_prefix_index import ListingPrefixIndex
from infrastructure.canonical.listing_runs import iter_planned_listing_revisions
from infrastructure.catalog.fingerprint import PYARROW_BATCH_FINGERPRINT_RULE_ID
from infrastructure.catalog.fingerprint_stream import fingerprint_run
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import CANONICAL_INSTRUMENT_LISTINGS
from infrastructure.revision.store import RevisionCatalog as StoreCatalog
from infrastructure.streaming.runs import RunLimits, RunRef, RunSetBuilder, iter_run

if TYPE_CHECKING:
    from infrastructure.canonical.listings import BoundedListingReplayInputs

LISTINGS_TABLE = CANONICAL_INSTRUMENT_LISTINGS.table

type FindingSink = Callable[[str, str, Any, Iterable[str], Callable[[int], str]], None]


@dataclass(frozen=True, slots=True)
class BoundedListingReplayProof:
    """Disk-backed proof outputs; callers consume refs rather than materialized histories."""

    raw_snapshot_id: str | None
    listing_snapshot_id: str | None
    committed_rows: RunRef | None
    diverged_revision_ids: RunRef | None
    committed_row_count: int
    diverged_count: int
    revision_index_node_write_count: int
    revision_index_node_write_bytes: int


def verify_listing_history_bounded(
    catalog: StoreCatalog,
    inputs: BoundedListingReplayInputs,
    *,
    evidence_storage: StorageAdapter,
    scratch_storage: StorageAdapter,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
    max_record_bytes: int,
    max_run_object_bytes: int,
    row_chunk_capacity: int,
    max_hash_chunk_bytes: int,
    prefix_leaf_max_records: int,
    prefix_fanout: int,
    prefix_max_node_bytes: int,
    prefix_max_record_bytes: int,
    finding_sink: FindingSink,
) -> BoundedListingReplayProof:
    """Re-derive each historic batch and compare exact rows and frozen C3 fingerprints.

    Catalog ancestry metadata retains its adapter-defined working set; PyIceberg's current
    ``history`` implementation keeps ``metadata.snapshots`` O(H). Scratch and row streams here
    are bounded by explicit RunSet and byte limits; this is not an E1-CAP-1 claim.
    """
    for name, value, minimum in (
        ("capacity", capacity, 1),
        ("merge_fanout", merge_fanout, 2),
        ("max_record_bytes", max_record_bytes, 1),
        ("max_run_object_bytes", max_run_object_bytes, 1),
        ("row_chunk_capacity", row_chunk_capacity, 1),
        ("max_hash_chunk_bytes", max_hash_chunk_bytes, 16),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    if not isinstance(limits, RunLimits):
        raise ValueError("limits must be RunLimits")
    if evidence_storage is scratch_storage:
        raise ValueError("scratch_storage must be a distinct adapter from evidence_storage")

    current_rows = _scan_current_rows(
        catalog,
        inputs.listing_snapshot_id,
        scratch_storage,
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
        max_record_bytes=max_record_bytes,
        max_run_object_bytes=max_run_object_bytes,
    )
    if (current_rows is None) != (inputs.listing_batches is None):
        raise CatalogIntegrityError(
            "Listing current rows and snapshot history disagree on emptiness"
        )

    revision_index = ListingPrefixIndex(
        scratch_storage,
        leaf_max_records=prefix_leaf_max_records,
        fanout=prefix_fanout,
        max_node_bytes=prefix_max_node_bytes,
        max_record_bytes=prefix_max_record_bytes,
    )
    current_reader: Iterator[Mapping[str, Any]] | None = None
    stack = ExitStack()
    try:
        if current_rows is not None:
            current_reader = stack.enter_context(
                iter_run(scratch_storage, current_rows, max_object_bytes=max_run_object_bytes)
            )
        if inputs.listing_batches is not None:
            batches = stack.enter_context(
                iter_run(
                    scratch_storage,
                    inputs.listing_batches,
                    max_object_bytes=max_run_object_bytes,
                )
            )
            prior_total = 0
            for batch_ref in batches:
                batch_info = _mapping(batch_ref.get("snapshot"), "listing SnapshotInfo")
                _text(batch_info.get("snapshot_id"), "snapshot_id")
                if lr.parse_batch_id(batch_info.get("batch_id")) != batch_ref.get(
                    "raw_snapshot_id"
                ):
                    raise CatalogIntegrityError("Listing batch ID differs from its Raw prefix")
                added_rows = _integer(batch_info.get("added_rows"), "added_rows", minimum=1)
                total_rows = _integer(batch_info.get("total_rows"), "total_rows", minimum=1)
                if total_rows != prior_total + added_rows:
                    raise CatalogIntegrityError(
                        f"{LISTINGS_TABLE} history is not a pure sequence of one-batch appends"
                    )
                batch_rows = _capture_batch_rows(
                    current_reader,
                    scratch_storage,
                    capacity=capacity,
                    merge_fanout=merge_fanout,
                    limits=limits,
                    start=prior_total,
                    count=added_rows,
                    max_record_bytes=max_record_bytes,
                )
                index = inputs.prefix_index
                if index is None:
                    raise CatalogIntegrityError("a Listing batch has no Raw prefix index")
                raw_floor = batch_ref.get("raw_knowledge_floor")
                if not isinstance(raw_floor, datetime) or raw_floor.tzinfo is None:
                    raise CatalogIntegrityError("Raw prefix has no knowledge-time floor")
                plans, plans_by_id = _derive_prefix_plans(
                    index,
                    batch_ref.get("root"),
                    scratch_storage,
                    capacity=capacity,
                    merge_fanout=merge_fanout,
                    limits=limits,
                    max_record_bytes=max_record_bytes,
                    max_run_object_bytes=max_run_object_bytes,
                )
                new_orders = _new_plan_orders(
                    scratch_storage,
                    plans_by_id,
                    revision_index,
                    capacity=capacity,
                    merge_fanout=merge_fanout,
                    limits=limits,
                    max_run_object_bytes=max_run_object_bytes,
                )
                if new_orders is None or new_orders.record_count != added_rows:
                    raise CatalogIntegrityError(
                        f"{batch_info.get('batch_id')} is not the complete derived Listing batch"
                    )
                _check_batch_replay(
                    scratch_storage,
                    batch_rows,
                    plans,
                    new_orders,
                    batch_info,
                    raw_floor,
                    arrival_base=prior_total,
                    capacity=capacity,
                    merge_fanout=merge_fanout,
                    limits=limits,
                    max_record_bytes=max_record_bytes,
                    max_run_object_bytes=max_run_object_bytes,
                    row_chunk_capacity=row_chunk_capacity,
                    max_hash_chunk_bytes=max_hash_chunk_bytes,
                )
                _index_committed_batch(
                    revision_index,
                    scratch_storage,
                    batch_rows,
                    max_run_object_bytes=max_run_object_bytes,
                )
                prior_total = total_rows
            if current_rows is None or prior_total != current_rows.record_count:
                raise CatalogIntegrityError(
                    f"{LISTINGS_TABLE} head row count differs from its snapshot history"
                )
            if current_reader is not None and next(current_reader, None) is not None:
                raise CatalogIntegrityError(f"{LISTINGS_TABLE} head contains unbatched rows")
    finally:
        stack.close()

    committed_rows = _sort_current_by_revision(
        scratch_storage,
        current_rows,
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
        max_record_bytes=max_record_bytes,
        max_run_object_bytes=max_run_object_bytes,
    )
    diverged = _find_diverged(
        scratch_storage,
        inputs.prefix_index,
        revision_index,
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
        max_record_bytes=max_record_bytes,
        max_run_object_bytes=max_run_object_bytes,
        finding_sink=finding_sink,
    )
    return BoundedListingReplayProof(
        raw_snapshot_id=inputs.raw_snapshot_id,
        listing_snapshot_id=inputs.listing_snapshot_id,
        committed_rows=committed_rows,
        diverged_revision_ids=diverged,
        committed_row_count=0 if committed_rows is None else committed_rows.record_count,
        diverged_count=0 if diverged is None else diverged.record_count,
        revision_index_node_write_count=revision_index.stats.node_write_count,
        revision_index_node_write_bytes=revision_index.stats.node_write_bytes,
    )


def _scan_current_rows(
    catalog: StoreCatalog,
    snapshot_id: str | None,
    storage: StorageAdapter,
    *,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
    max_record_bytes: int,
    max_run_object_bytes: int,
) -> RunRef | None:
    if snapshot_id is None:
        return None
    columns = tuple(field.name for field in CANONICAL_INSTRUMENT_LISTINGS.arrow_schema)
    reader = catalog.scan_column_batches(LISTINGS_TABLE, columns=columns, snapshot_id=snapshot_id)
    builder = RunSetBuilder(
        storage,
        key=lambda row: row["arrival_seq"],
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
    )
    row_count = 0
    with builder:
        try:
            for record_batch in reader:
                for index in range(record_batch.num_rows):
                    [raw] = record_batch.slice(index, 1).to_pylist()
                    if not isinstance(raw, Mapping):
                        raise CatalogIntegrityError("Listing row batch contains a non-mapping")
                    row = dict(raw)
                    _check_bounded_row(row, max_record_bytes)
                    _integer(row.get("arrival_seq"), "arrival_seq", minimum=0)
                    builder.add(row)
                    row_count += 1
        finally:
            _close(reader)
        result = builder.finish()
    if result is None:
        return None
    with iter_run(storage, result, max_object_bytes=max_run_object_bytes) as rows:
        expected = 0
        for sorted_row in rows:
            if sorted_row.get("arrival_seq") != expected:
                raise CatalogIntegrityError("Listing arrival_seq is not contiguous from zero")
            expected += 1
    if row_count != result.record_count:
        raise CatalogIntegrityError("Listing head scan count differs from its sorted run")
    return result


def _capture_batch_rows(
    current_reader: Iterator[Mapping[str, Any]] | None,
    storage: StorageAdapter,
    *,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
    start: int,
    count: int,
    max_record_bytes: int,
) -> RunRef:
    if current_reader is None:
        raise CatalogIntegrityError("Listing snapshot history has no current rows")
    builder = RunSetBuilder(
        storage,
        key=lambda row: row["arrival_seq"],
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
    )
    with builder:
        for expected_arrival in range(start, start + count):
            try:
                row = next(current_reader)
            except StopIteration as exc:
                raise CatalogIntegrityError("Listing snapshot history exceeds head rows") from exc
            if row.get("arrival_seq") != expected_arrival:
                raise CatalogIntegrityError("Listing batch arrival_seq is missing or reordered")
            _check_bounded_row(row, max_record_bytes)
            builder.add(row)
        result = builder.finish()
    if result is None or result.record_count != count:
        raise CatalogIntegrityError("Listing batch failed to spool its exact row count")
    return result


def _derive_prefix_plans(
    index: ListingPrefixIndex,
    root_document: object,
    storage: StorageAdapter,
    *,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
    max_record_bytes: int,
    max_run_object_bytes: int,
) -> tuple[RunRef, RunRef]:
    plans = RunSetBuilder(
        storage,
        key=lambda row: row["plan_order"],
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
    )
    ids = RunSetBuilder(
        storage,
        key=lambda row: row["revision_id"],
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
    )
    exhausted = False

    def observations() -> Generator[Mapping[str, Any]]:
        nonlocal exhausted
        if root_document is not None and not isinstance(root_document, Mapping):
            raise CatalogIntegrityError("Listing batch has a malformed Raw prefix root")
        yield from index.iter_rows_for_root_document(root_document)
        exhausted = True

    def consume_finding(
        _code: str,
        _symbol: str,
        _instant: Any,
        revision_ids: Iterable[str],
        _detail: Callable[[int], str],
    ) -> None:
        for _ in revision_ids:
            pass

    observation_stream = observations()
    planned_stream = iter_planned_listing_revisions(
        observation_stream, max_record_bytes=max_record_bytes, finding_sink=consume_finding
    )
    with plans, ids, closing(observation_stream), closing(planned_stream):
        ordinal = 0
        for planned in planned_stream:
            row = lr.listing_columns(
                planned,
                arrival_seq=0,
                knowledge_time=planned.observation.raw_knowledge_time,
            )
            _check_bounded_row(row, max_record_bytes)
            plans.add({"plan_order": ordinal, "row": row})
            ids.add({"revision_id": planned.revision_id, "plan_order": ordinal, "row": row})
            ordinal += 1
        if not exhausted:
            raise CatalogIntegrityError("listing plan derivation did not exhaust its Raw prefix")
        plan_ref = plans.finish()
        id_ref = ids.finish()
    if plan_ref is None or id_ref is None:
        raise CatalogIntegrityError("persisted Listing batch re-derives to no revisions")
    _assert_unique_id_run(storage, id_ref, max_run_object_bytes)
    return plan_ref, id_ref


def _new_plan_orders(
    storage: StorageAdapter,
    plans_by_id: RunRef,
    revision_index: ListingPrefixIndex,
    *,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
    max_run_object_bytes: int,
) -> RunRef | None:
    new_orders = RunSetBuilder(
        storage,
        key=lambda row: row["plan_order"],
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
    )
    with new_orders:
        with iter_run(storage, plans_by_id, max_object_bytes=max_run_object_bytes) as plans:
            for plan in plans:
                revision_id = plan["revision_id"]
                old = revision_index.lookup(
                    _REVISION_INDEX_SYMBOL, _REVISION_INDEX_TIME, revision_id
                )
                if old is not None:
                    previous_row = old.get("committed")
                    if not isinstance(previous_row, Mapping):
                        raise CatalogIntegrityError("revision index has a malformed committed row")
                    if list(previous_row.get("supersedes", ())) != list(
                        plan["row"].get("supersedes", ())
                    ):
                        raise CatalogIntegrityError(
                            f"Listing revision {revision_id} supersedes another revision"
                        )
                    continue
                new_orders.add({"plan_order": plan["plan_order"]})
        return new_orders.finish()


_REVISION_INDEX_SYMBOL = "\x00listing-revision-id-index"
_REVISION_INDEX_TIME = datetime(1970, 1, 1, tzinfo=UTC)


def _index_committed_batch(
    index: ListingPrefixIndex,
    storage: StorageAdapter,
    batch: RunRef,
    *,
    max_run_object_bytes: int,
) -> None:
    with iter_run(storage, batch, max_object_bytes=max_run_object_bytes) as rows:
        for row in rows:
            revision_id = _text(row.get("revision_id"), "revision_id")
            if index.lookup(_REVISION_INDEX_SYMBOL, _REVISION_INDEX_TIME, revision_id) is not None:
                raise CatalogIntegrityError(f"Listing revision {revision_id} is committed twice")
            ingest_time = row.get("ingest_time")
            if not isinstance(ingest_time, datetime):
                raise CatalogIntegrityError("Listing row has an invalid ingest_time")
            index.insert(
                {
                    "venue_symbol": _REVISION_INDEX_SYMBOL,
                    "requested_at": _REVISION_INDEX_TIME,
                    "retrieved_at": _REVISION_INDEX_TIME,
                    "raw_knowledge_time": _REVISION_INDEX_TIME,
                    "snapshot_revision_id": revision_id,
                    "committed": {
                        "supersedes": list(row.get("supersedes", ())),
                        "base_asset": row.get("base_asset"),
                        "quote_asset": row.get("quote_asset"),
                        "ingest_time": ingest_time.isoformat(),
                        "lineage_source_revision_id": row.get("lineage_source_revision_id"),
                    },
                }
            )


def _sort_current_by_revision(
    storage: StorageAdapter,
    current_rows: RunRef | None,
    *,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
    max_record_bytes: int,
    max_run_object_bytes: int,
) -> RunRef | None:
    if current_rows is None:
        return None
    builder = RunSetBuilder(
        storage,
        key=lambda row: row["revision_id"],
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
    )
    with builder:
        with iter_run(storage, current_rows, max_object_bytes=max_run_object_bytes) as rows:
            for row in rows:
                _check_bounded_row(row, max_record_bytes)
                builder.add(dict(row))
        result = builder.finish()
    if result is None or result.record_count != current_rows.record_count:
        raise CatalogIntegrityError("Listing rows failed their revision-order sort")
    _assert_unique_id_run(storage, result, max_run_object_bytes)
    return result


def _check_batch_replay(
    storage: StorageAdapter,
    stored_rows: RunRef,
    plans: RunRef,
    new_orders: RunRef,
    snapshot: Mapping[str, Any],
    raw_floor: datetime | None,
    *,
    arrival_base: int,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
    max_record_bytes: int,
    max_run_object_bytes: int,
    row_chunk_capacity: int,
    max_hash_chunk_bytes: int,
) -> None:
    with iter_run(storage, stored_rows, max_object_bytes=max_run_object_bytes) as rows:
        first = next(rows, None)
    if first is None:
        raise CatalogIntegrityError("Listing batch row run is empty")
    ready = first.get("knowledge_time")
    version = first.get("contract_schema_version")
    if not isinstance(ready, datetime) or ready.tzinfo is None or ready.utcoffset() != timedelta(0):
        raise CatalogIntegrityError("Listing batch has an invalid knowledge_time")
    if not isinstance(version, str) or not version:
        raise CatalogIntegrityError("Listing batch has no contract schema version")
    version = contract_version.recorded_version(
        [version], what=f"{LISTINGS_TABLE} batch {snapshot.get('batch_id')}"
    )
    if raw_floor is not None and ready < raw_floor:
        raise CatalogIntegrityError("Listing batch is known before a Raw row it read")

    schema = CANONICAL_INSTRUMENT_LISTINGS.arrow_schema
    fingerprint, _stats = fingerprint_run(
        storage,
        stored_rows,
        schema,
        max_record_bytes=max_record_bytes,
        max_run_object_bytes=max_run_object_bytes,
        row_chunk_capacity=row_chunk_capacity,
        max_hash_chunk_bytes=max_hash_chunk_bytes,
    )
    batch_fingerprint = snapshot.get("batch_fingerprint")
    added = _integer(snapshot.get("added_rows"), "added_rows", minimum=1)
    if fingerprint != batch_fingerprint or added != stored_rows.record_count:
        raise CatalogIntegrityError(
            f"batch {snapshot.get('batch_id')} of {LISTINGS_TABLE} has a different fingerprint"
        )
    if CANONICAL_INSTRUMENT_LISTINGS.fingerprint_rule.rule_id != PYARROW_BATCH_FINGERPRINT_RULE_ID:
        raise CatalogIntegrityError("Listing table no longer uses the C3 batch fingerprint rule")

    with (
        iter_run(storage, plans, max_object_bytes=max_run_object_bytes) as all_plans,
        iter_run(storage, new_orders, max_object_bytes=max_run_object_bytes) as selected,
        iter_run(storage, stored_rows, max_object_bytes=max_run_object_bytes) as stored,
    ):
        chosen = next(selected, None)
        row_index = 0
        for plan in all_plans:
            if chosen is None:
                break
            if chosen["plan_order"] < plan["plan_order"]:
                raise CatalogIntegrityError("Listing replay plan order is incomplete")
            if chosen["plan_order"] != plan["plan_order"]:
                continue
            try:
                actual = next(stored)
            except StopIteration as exc:
                raise CatalogIntegrityError("Listing batch has fewer rows than its plan") from exc
            expected = dict(plan["row"])
            expected["arrival_seq"] = arrival_base + row_index
            expected["knowledge_time"] = ready
            expected["contract_schema_version"] = version
            for evidence in expected["precedence_evidence"]:
                evidence["knowledge_time"] = ready
            _check_bounded_row(expected, max_record_bytes)
            if actual != expected:
                mismatch = sorted(
                    key
                    for key in set(actual) | set(expected)
                    if actual.get(key) != expected.get(key)
                )
                raise CatalogIntegrityError(
                    f"Listing row {actual.get('revision_id')} differs from its replay: "
                    f"{[(name, actual.get(name), expected.get(name)) for name in mismatch]}"
                )
            row_index += 1
            chosen = next(selected, None)
        if chosen is not None or row_index != stored_rows.record_count:
            raise CatalogIntegrityError("Listing batch rows do not equal newly derived revisions")


def _find_diverged(
    storage: StorageAdapter,
    index: ListingPrefixIndex | None,
    revision_index: ListingPrefixIndex,
    *,
    capacity: int,
    merge_fanout: int,
    limits: RunLimits,
    max_record_bytes: int,
    max_run_object_bytes: int,
    finding_sink: FindingSink,
) -> RunRef | None:
    if index is None:
        if revision_index.root is None:
            return None
        raise CatalogIntegrityError("committed Listing rows have no Raw prefix index")
    current_plans = RunSetBuilder(
        storage,
        key=lambda row: row["revision_id"],
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
    )
    diverged = RunSetBuilder(
        storage,
        key=lambda row: row["revision_id"],
        capacity=capacity,
        merge_fanout=merge_fanout,
        limits=limits,
    )

    def consume_finding(
        code: str,
        symbol: str,
        instant: Any,
        revision_ids: Iterable[str],
        detail: Callable[[int], str],
    ) -> None:
        finding_sink(code, symbol, instant, revision_ids, detail)

    def observations() -> Generator[Mapping[str, Any]]:
        yield from index.iter_rows()

    observations_exhausted = False

    def complete_observations() -> Generator[Mapping[str, Any]]:
        nonlocal observations_exhausted
        yield from observations()
        observations_exhausted = True

    observation_stream = complete_observations()
    planned_stream = iter_planned_listing_revisions(
        observation_stream, max_record_bytes=max_record_bytes, finding_sink=consume_finding
    )
    with current_plans, diverged:
        with closing(observation_stream), closing(planned_stream):
            for planned in planned_stream:
                current_plans.add({"revision_id": planned.revision_id})
        if not observations_exhausted:
            raise CatalogIntegrityError("current Listing chain derivation was not exhausted")
        current_ref = current_plans.finish()
        with ExitStack() as stack:
            current_reader = (
                iter(())
                if current_ref is None
                else stack.enter_context(
                    iter_run(storage, current_ref, max_object_bytes=max_run_object_bytes)
                )
            )
            current = next(current_reader, None)
            indexed_rows = revision_index.iter_rows()
            try:
                for indexed in indexed_rows:
                    revision_id = indexed["snapshot_revision_id"]
                    committed = indexed.get("committed")
                    if not isinstance(committed, Mapping):
                        raise CatalogIntegrityError("revision index has a malformed committed row")
                    while current is not None and current["revision_id"] < revision_id:
                        current = next(current_reader, None)
                    if current is None or current["revision_id"] != revision_id:
                        row = {
                            "revision_id": revision_id,
                            "venue_symbol": _venue_symbol(committed),
                            "observed_at": _parse_utc(committed.get("ingest_time"), "ingest_time"),
                            "source_revision_id": _text(
                                committed.get("lineage_source_revision_id"),
                                "lineage_source_revision_id",
                            ),
                        }
                        diverged.add(row)

                        def detail(_count: int, rev: str = revision_id) -> str:
                            return (
                                f"committed listing revision {rev} is not in the chain derived "
                                "from every proven snapshot (a late snapshot moved a change "
                                "point); it is never rewritten and every read where it is "
                                "available fails closed"
                            )

                        _call_finding_sink(
                            finding_sink,
                            "listing_history_diverged",
                            row["venue_symbol"],
                            row["observed_at"],
                            iter((row["source_revision_id"],)),
                            detail,
                        )
            finally:
                indexed_rows.close()
        return diverged.finish()


def _assert_unique_id_run(storage: StorageAdapter, run: RunRef, max_run_object_bytes: int) -> None:
    with iter_run(storage, run, max_object_bytes=max_run_object_bytes) as rows:
        previous: str | None = None
        for row in rows:
            revision_id = row.get("revision_id")
            if not isinstance(revision_id, str) or not revision_id:
                raise CatalogIntegrityError("Listing revision ID run contains invalid text")
            if revision_id == previous:
                raise CatalogIntegrityError(f"Listing revision {revision_id} is committed twice")
            previous = revision_id


def _check_bounded_row(row: Mapping[str, Any], maximum: int) -> None:
    from infrastructure.revision.exchange_info_store import ExchangeInfoRowVerifier

    try:
        ExchangeInfoRowVerifier._check_bounded_row(row, maximum)
    except Exception as exc:
        raise CatalogIntegrityError(f"{LISTINGS_TABLE} row exceeds its byte limit") from exc


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CatalogIntegrityError(f"{label} is not a mapping")
    return value


def _integer(value: object, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CatalogIntegrityError(f"{label} is invalid")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CatalogIntegrityError(f"{label} is invalid")
    return value


def _venue_symbol(row: Mapping[str, Any]) -> str:
    for symbol, (base, quote) in lr.FIRST_SLICE_ASSETS.items():
        if row.get("base_asset") == base and row.get("quote_asset") == quote:
            return symbol
    raise CatalogIntegrityError("committed Listing row has an unknown asset pair")


def _parse_utc(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise CatalogIntegrityError(f"{label} is not canonical UTC text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CatalogIntegrityError(f"{label} is not canonical UTC text") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise CatalogIntegrityError(f"{label} is not canonical UTC text")
    return parsed


def _close(resource: object) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        close()


def _call_finding_sink(
    sink: FindingSink,
    code: str,
    symbol: str,
    instant: Any,
    revision_ids: Iterable[str],
    detail: Callable[[int], str],
) -> None:
    state = {"count": 0, "exhausted": False}

    def ids() -> Iterator[str]:
        for revision in revision_ids:
            state["count"] += 1
            yield revision
        state["exhausted"] = True

    sink(code, symbol, instant, ids(), detail)
    if not state["exhausted"]:
        raise CatalogIntegrityError("Listing finding sink did not consume every revision ID")

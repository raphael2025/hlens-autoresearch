"""REST response / element revision store (Phase 1 D3E; ADR-0027 §2 / §3 / §8 / §9 / §11).

The store turns one **committed** D3D logical collection attempt into append-only Raw revisions:

1. ``raw.binance_spot_rest_responses`` — one response revision per complete 200 page, empty,
   overshooting and decoder-rejected pages included (s1). Each *new* response revision allocates
   one REST ``arrival_seq`` block from this anchor table: ``[2**62, 2**63)``, stride ``2**32``,
   the response row at the block base;
2. ``raw.binance_spot_rest_agg_trades`` / ``raw.binance_spot_rest_klines_1m`` — one element
   revision per decoded element of an accepted page, in deterministic microbatches (s2); element
   ``i`` takes ``base + i + 1`` and inherits the response row's ``ingest_time`` /
   ``knowledge_time``.

Trust boundary: the store never collects. It locates the collection checkpoint (c3) of a
``CollectionRequest`` and re-verifies it with the accepted D3D reader — request fingerprint, chain
order, page keys, page identity, content-addressed body ``ObjectRef``, body bytes re-hashed, HTTP
200, header allowlist, decoder binding, a strict **re-decode** of every page with the frozen D3C
decoder and a field-for-field reproduction of every checkpoint. The reader is a collector instance
whose only transport refuses every request, and no collect path is ever called. A missing
collection checkpoint fails closed; a drifted one is ``RestCheckpointIntegrityError``.

Identity, keys, payload hashes, object keys and the arrival layout come from ``rest_identity``
only; availability from ``rest_availability`` only. Decimal element fields are hashed exactly as
they are stored: the natives pass through the table's own ``decimal(38, 18)`` Arrow type first,
the way D2 hashes the D1 parser's Arrow rows, so a row read back from Iceberg re-derives its own
payload hash (the D-33 reconciler depends on that).

Idempotency and recovery (no journal, no mutable sidecar):

- a response revision is keyed by page identity + body bytes. Whoever delivers it again (a replay
  or another logical attempt) finds the committed row: no new revision, no new block, no new
  snapshot, and the first delivery's provenance, block base and ``knowledge_time`` stay;
- same page identity, different bytes → a second response revision (no ``supersedes``) and a
  ``rest_response_competing_payload`` finding; response revisions are never selected by PIT;
- the same element delivered again with the same payload keeps its first lineage, arrival
  number and times; a different payload for the same observation key is a second revision and a
  competing-payload finding (never an edge: ``binance.spot.rest-revision@1.0.0``);
- a crash anywhere after c3 is repaired by calling ``ingest_collection`` again: committed rows
  give back their block base and times, the immutable body plus the decoder give back the
  elements, and only batches the catalog does not have are committed. Every fast path rebuilds
  the expected rows and compares them with what is actually committed, including the snapshot's
  batch fingerprint; any disagreement is ``CatalogIntegrityError``.

Every committed row the store adopts, compares or reports is proven lawful first, never trusted
for its identity alone (D3E-R1):

- a response revision (ours or a competitor) is rebuilt column for column from its own inputs —
  page query + origin, published body, block base, request / retrieval / knowledge times and a
  validated first-delivery provenance — and must hold its arrival block alone and be the only,
  exact content of its one batch snapshot;
- an element revision (owned, adopted from another page or competing) must name exactly one
  such lawful response revision that accepted a page of its data type and symbol with enough
  elements; the row is rebuilt from its native fields plus that response's block base and times
  and must match column for column, its arrival number held by it alone. What is *not* proven for
  a revision first delivered by another page is that the other page's body really held it at
  that index (re-decoding it needs that page's chain context);
- element rows and their lineage responses live in two tables and the catalog reads current
  heads, so the whole read is judged only if both heads are unchanged before and after it
  (bounded re-reads, then ``RestRevisionStoreConflict``); a mixed view is never judged.

Quality findings (decoder facts, rejections, competing payloads) are returned deterministically
in the outcome. Persisting them is batch E; this module invents no public taxonomy.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Final, Self

import httpx
import pyarrow as pa  # type: ignore[import-untyped]
from pyiceberg.expressions import And, BooleanExpression, EqualTo, In

from core.contracts.catalog import (
    BatchConflict,
    CommitConflict,
    CommitOutcome,
    CommitRequest,
    SnapshotInfo,
    TableNotFound,
)
from core.contracts.collector import CollectionRequest, CollectionResult, UnsupportedRequest
from core.contracts.revision import AvailabilityDecision, RevisionRecord
from core.contracts.storage import IntegrityViolation, StorageAdapter, StorageError
from core.domain.base import canonical_json
from infrastructure.catalog.definitions import RegisteredTableDefinition
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_REST_AGG_TRADES,
    BINANCE_SPOT_REST_KLINES_1M,
    BINANCE_SPOT_REST_RESPONSES,
)
from infrastructure.collector import binance_rest as d3d
from infrastructure.parser.binance_rest import (
    DECODER_BINDING,
    RestAggTradeElement,
    RestKline1mElement,
    RestPageDecoded,
    RestPageRejection,
    RestRejectionCode,
)
from infrastructure.revision import rest_identity
from infrastructure.revision.rest_availability import (
    RestAvailabilitySubject,
    RestAvailabilityViolation,
    decide_rest_availability,
)
from infrastructure.revision.store import BatchCommit, RevisionCatalog

__all__ = [
    "DEFAULT_ELEMENT_MICROBATCH_ROWS",
    "ELEMENT_TABLES",
    "FINDING_AGG_TRADE_COMPETING",
    "FINDING_DECODER_REJECTED",
    "FINDING_KLINE_COMPETING",
    "FINDING_RESPONSE_COMPETING",
    "MAX_ELEMENT_MICROBATCH_ROWS",
    "RESPONSE_TABLE",
    "RestCheckpointIntegrityError",
    "RestCollectionStored",
    "RestPageStored",
    "RestQualityFinding",
    "RestRevisionStore",
    "RestRevisionStoreConflict",
    "RestRevisionStoreError",
]

RESPONSE_TABLE: Final = BINANCE_SPOT_REST_RESPONSES.table
ELEMENT_TABLES: Final[Mapping[str, str]] = {
    "agg_trades": BINANCE_SPOT_REST_AGG_TRADES.table,
    "klines_1m": BINANCE_SPOT_REST_KLINES_1M.table,
}
_ELEMENT_DEFINITIONS: Final[Mapping[str, RegisteredTableDefinition]] = {
    "agg_trades": BINANCE_SPOT_REST_AGG_TRADES,
    "klines_1m": BINANCE_SPOT_REST_KLINES_1M,
}
_ELEMENT_SUBJECTS: Final[Mapping[str, RestAvailabilitySubject]] = {
    "agg_trades": RestAvailabilitySubject.AGG_TRADE,
    "klines_1m": RestAvailabilitySubject.KLINE_1M,
}


def _native_columns(definition: RegisteredTableDefinition) -> tuple[str, ...]:
    """The decoder's native element fields: every column after the decoder binding."""
    names = [field.name for field in definition.arrow_schema]
    return tuple(names[names.index("decoder_hash") + 1 :])


_ELEMENT_NATIVE_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = {
    data_type: _native_columns(definition) for data_type, definition in _ELEMENT_DEFINITIONS.items()
}

#: Rows per element microbatch. A page holds at most ``PAGE_LIMIT`` elements, so one batch per
#: page by default; the microbatch plan (and therefore every batch id) depends on this value.
DEFAULT_ELEMENT_MICROBATCH_ROWS: Final = rest_identity.PAGE_LIMIT
MAX_ELEMENT_MICROBATCH_ROWS: Final = rest_identity.PAGE_LIMIT
#: Attempts per commit when another writer moved the table head (and per pinned read).
_COMMIT_ATTEMPTS: Final = 8
#: Values per ``IN`` filter of a bounded lookup (lineage responses, arrival holders).
_KEY_CHUNK: Final = 256
_ZERO: Final = timedelta(0)
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MINUTE: Final = timedelta(minutes=1)

#: Internal finding codes (ADR-0027 open obligations: aligned with batch E's full taxonomy later).
FINDING_RESPONSE_COMPETING: Final = "rest_response_competing_payload"
FINDING_AGG_TRADE_COMPETING: Final = "rest_agg_trade_competing_payload"
FINDING_KLINE_COMPETING: Final = "rest_kline_1m_competing_payload"
FINDING_DECODER_REJECTED: Final = "rest_page_decoder_rejected"
_ELEMENT_COMPETING: Final[Mapping[str, str]] = {
    "agg_trades": FINDING_AGG_TRADE_COMPETING,
    "klines_1m": FINDING_KLINE_COMPETING,
}

#: Response columns that record the *first delivery* (provenance), not the revision identity.
_PROVENANCE_COLUMNS: Final[tuple[str, ...]] = (
    "collector_id",
    "collector_version",
    "collection_request_id",
    "page_index",
    "requested_at",
    "retrieved_at",
    "source_metadata",
    "decode_outcome",
    "decode_rejection_code",
    "element_count",
    "answered_start",
    "answered_end",
)
#: Every response revision is a complete 200 page (s1); other statuses never reach the store.
_HTTP_OK: Final = 200
_ACCEPTED: Final = "accepted"
_REJECTED: Final = "rejected"


class RestRevisionStoreError(Exception):
    """Base class of REST store failures that must not be papered over."""


class RestRevisionStoreConflict(RestRevisionStoreError):
    """The request cannot be honoured against persisted state; nothing more is written."""


class RestCheckpointIntegrityError(RestRevisionStoreError):
    """A committed D3D checkpoint or response body does not reproduce (fail closed)."""


def _equals(column: str, value: object) -> BooleanExpression:
    """``column == value`` as a PyIceberg expression (never a string built from data)."""
    return EqualTo(column, value)  # type: ignore[call-arg, arg-type]


def _member(column: str, values: Iterable[object]) -> BooleanExpression:
    """``column IN values`` as a PyIceberg expression."""
    return In(column, set(values))  # type: ignore[call-arg, arg-type]


def _both(left: BooleanExpression, right: BooleanExpression) -> BooleanExpression:
    return And(left, right)


def _refuse_network(request: httpx.Request) -> httpx.Response:
    raise RestRevisionStoreError("the REST revision store never touches the network")


def _at_ms(epoch_ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=epoch_ms)


# =========================================================================================
# results
# =========================================================================================


@dataclass(frozen=True, slots=True)
class RestQualityFinding:
    """One immutable, internal quality finding (persisting it is batch E).

    ``code`` is either a D3C ``RestQualityFactType`` value, ``rest_page_decoder_rejected`` or a
    competing-payload code; ``related_revision_ids`` lists the other revisions of the same
    observation key for a competing payload (sorted, set semantics).
    """

    code: str
    data_type: str
    symbol: str
    page_index: int
    observation_key: str
    revision_id: str
    related_revision_ids: tuple[str, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class RestPageStored:
    """The response revision (and elements) one committed page maps to."""

    symbol: str
    page_index: int
    page_identity_sha256: str
    observation_key: str
    response_revision_id: str
    arrival_seq_base: int
    ingest_time: datetime
    knowledge_time: datetime
    #: True when the committed response row records *this* request / page as its first delivery.
    first_delivery: bool
    decode_outcome: str
    response_commit: BatchCommit
    element_commits: tuple[BatchCommit, ...]
    #: Revision ids of this delivery's decoded elements, in element order.
    element_revision_ids: tuple[str, ...]
    #: The subset whose committed lineage is this response revision (element order).
    owned_element_revision_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RestCollectionStored:
    """Result of persisting one committed logical collection attempt."""

    request_id: str
    data_type: str
    #: ``succeeded`` or ``failed`` (a decoder-rejected page stops the attempt; it is still stored).
    collection_outcome: str
    result: CollectionResult | None
    pages: tuple[RestPageStored, ...]
    findings: tuple[RestQualityFinding, ...]

    @property
    def commits(self) -> tuple[BatchCommit, ...]:
        return tuple(
            commit
            for page in self.pages
            for commit in (page.response_commit, *page.element_commits)
        )

    @property
    def replayed(self) -> bool:
        """True when nothing new was committed: every batch came back already committed."""
        return all(commit.replayed for commit in self.commits)

    @property
    def new_snapshot_ids(self) -> tuple[str, ...]:
        return tuple(commit.snapshot_id for commit in self.commits if not commit.replayed)


# =========================================================================================
# internal plan types
# =========================================================================================


@dataclass(frozen=True, slots=True)
class _Collection:
    request: CollectionRequest
    outcome: str
    result: CollectionResult | None
    chains: tuple[tuple[str, tuple[d3d._PageRecord, ...]], ...]


@dataclass(frozen=True, slots=True)
class _ResponseState:
    observation_key: str
    revision_id: str
    base: int
    ingest_time: datetime
    knowledge_time: datetime
    first_delivery: bool
    stored_outcome: str
    stored_element_count: int | None
    commit: BatchCommit
    competing: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PlannedElement:
    position: int
    element_index: int
    observation_key: str
    revision_id: str
    payload_hash: str
    row: Mapping[str, Any]


# =========================================================================================
# the store
# =========================================================================================


class RestRevisionStore:
    """Persists committed REST collection attempts as response and element revisions.

    One writer per table (ADR-0023 §7); concurrent writers are tolerated through expected-parent
    commits and bounded retries. The store keeps no state between calls: after a restart every
    fact comes back from Iceberg, the immutable checkpoints and bodies, and the frozen decoder.
    """

    def __init__(
        self,
        adapter: RevisionCatalog,
        storage: StorageAdapter,
        *,
        market_data_base_url: str,
        clock: Callable[[], datetime] | None = None,
        element_microbatch_rows: int = DEFAULT_ELEMENT_MICROBATCH_ROWS,
    ) -> None:
        if not isinstance(element_microbatch_rows, int) or isinstance(
            element_microbatch_rows, bool
        ):
            raise RestRevisionStoreError("element_microbatch_rows must be an int")
        if not 1 <= element_microbatch_rows <= MAX_ELEMENT_MICROBATCH_ROWS:
            raise RestRevisionStoreError(
                f"element_microbatch_rows must be between 1 and {MAX_ELEMENT_MICROBATCH_ROWS}"
            )
        self._adapter = adapter
        self._storage = storage
        self._clock = clock or (lambda: datetime.now(UTC))
        self._microbatch = element_microbatch_rows
        try:
            # The accepted D3D checkpoint reader. Its only transport refuses every request and
            # no collect path is ever called: the store re-verifies, it never fetches.
            self._reader = d3d.BinanceSpotRestCollector(
                storage,
                market_data_base_url=market_data_base_url,
                http_connect_timeout_seconds=1.0,
                http_read_timeout_seconds=1.0,
                http_max_retries=0,
                http_user_agent="hlens-d3e-checkpoint-reader/1.0.0",
                max_pages_per_collect=d3d.MIN_PAGES_PER_COLLECT,
                min_request_interval_ms=d3d.MIN_REQUEST_INTERVAL_MS,
                max_retry_after_seconds=d3d.MIN_RETRY_AFTER_SECONDS,
                max_response_bytes=d3d.MAX_RESPONSE_BYTES,
                http_transport=httpx.MockTransport(_refuse_network),
            )
        except ValueError as exc:
            raise RestRevisionStoreError(f"invalid market-data origin: {exc}") from None
        self._origin = self._reader.descriptor.network_origins[0]

    def close(self) -> None:
        self._reader.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------ entry point

    def ingest_collection(self, request: CollectionRequest) -> RestCollectionStored:
        """Persist the committed attempt ``request`` (s1 then s2, page by page, in chain order)."""
        if not isinstance(request, CollectionRequest):
            raise RestRevisionStoreError("request must be a CollectionRequest")
        collection = self._load_collection(request)
        pages: list[RestPageStored] = []
        findings: list[RestQualityFinding] = []
        for symbol, records in collection.chains:
            for page in records:
                stored, page_findings = self._store_page(request, symbol, page)
                pages.append(stored)
                findings.extend(page_findings)
        return RestCollectionStored(
            request_id=request.request_id,
            data_type=request.data_type,
            collection_outcome=collection.outcome,
            result=collection.result,
            pages=tuple(pages),
            findings=tuple(findings),
        )

    # ------------------------------------------------------------------ c3: committed checkpoint

    def _load_collection(self, request: CollectionRequest) -> _Collection:
        """The committed collection checkpoint, re-verified field for field (never fetched)."""
        reader = self._reader
        try:
            start_ms, end_ms = reader._validate_request(request)
        except UnsupportedRequest as exc:
            raise RestRevisionStoreError(f"not a REST collection request: {exc}") from None
        fingerprint = d3d._request_fingerprint(request)
        root = f"{d3d.CHECKPOINT_PREFIX}/{d3d._sha256_hex(request.request_id.encode('utf-8'))}"
        key = f"{root}/collection.json"
        try:
            payload = reader._read_object(key)
            if payload is None:
                raise RestRevisionStoreError(
                    f"request {request.request_id!r} has no committed collection checkpoint "
                    "(c3): the store only persists finished attempts and never collects"
                )
            document = d3d._parse_checkpoint(payload, d3d.COLLECTION_CHECKPOINT_KIND)
            if d3d._mapping_field(document, "request") != fingerprint:
                raise RestRevisionStoreConflict(
                    f"request {request.request_id!r} is committed with different request content"
                )
            chains = reader._verify_chains(key, document, fingerprint, request, start_ms, end_ms)
            if not chains:
                raise d3d._CheckpointInvalid(f"{key!r} records no chain at all")
            outcome = d3d._str_field(document, "outcome")
            result: CollectionResult | None
            if outcome == "failed":
                failure = d3d._mapping_field(document, "failure")
                reader._check_failure_shape(failure, chains)
                result = None
                rebuilt = d3d._collection_document(
                    fingerprint, chains, result=None, failure=dict(failure)
                )
            elif outcome == "succeeded":
                if [symbol for symbol, _ in chains] != list(request.symbols):
                    raise d3d._CheckpointInvalid(f"{key!r} succeeds without every symbol")
                for symbol, records in chains:
                    last = records[-1]
                    if last.rejection is not None or last.summary.stop_reason is None:
                        raise d3d._CheckpointInvalid(f"{key!r} has an unterminated {symbol} chain")
                result = reader._assemble(request, start_ms, end_ms, chains)
                rebuilt = d3d._collection_document(fingerprint, chains, result=result, failure=None)
            else:
                raise d3d._CheckpointInvalid(f"{key!r} carries an unknown outcome")
            if canonical_json(rebuilt).encode("utf-8") != payload:
                raise d3d._CheckpointInvalid(f"{key!r} does not reproduce field for field")
        except d3d._CheckpointInvalid as exc:
            raise RestCheckpointIntegrityError(
                f"committed checkpoint does not reproduce: {exc}"
            ) from None
        except IntegrityViolation as exc:
            raise RestCheckpointIntegrityError(
                f"a committed object does not match its reference: {exc}"
            ) from None
        except StorageError as exc:
            raise RestRevisionStoreError(f"storage failure: {exc}") from exc
        except ValueError as exc:
            # A malformed committed reference (e.g. an ``ObjectRef`` that no longer validates).
            raise RestCheckpointIntegrityError(
                f"committed checkpoint does not validate: {exc}"
            ) from None
        return _Collection(
            request=request,
            outcome=outcome,
            result=result,
            chains=tuple((symbol, tuple(records)) for symbol, records in chains),
        )

    # ------------------------------------------------------------------ one page

    def _store_page(
        self, request: CollectionRequest, symbol: str, page: d3d._PageRecord
    ) -> tuple[RestPageStored, list[RestQualityFinding]]:
        data_type = request.data_type
        state = self._store_response(request, page)
        findings: list[RestQualityFinding] = []
        if state.competing:
            findings.append(
                RestQualityFinding(
                    code=FINDING_RESPONSE_COMPETING,
                    data_type=data_type,
                    symbol=symbol,
                    page_index=page.page_index,
                    observation_key=state.observation_key,
                    revision_id=state.revision_id,
                    related_revision_ids=state.competing,
                    detail=(
                        f"page identity {page.page_identity} answered with "
                        f"{len(state.competing) + 1} different bodies; response revisions are "
                        "never selected"
                    ),
                )
            )
        outcome = page.outcome
        commits: tuple[BatchCommit, ...] = ()
        element_ids: tuple[str, ...] = ()
        owned: tuple[str, ...] = ()
        if isinstance(outcome, RestPageRejection):
            findings.append(
                RestQualityFinding(
                    code=FINDING_DECODER_REJECTED,
                    data_type=data_type,
                    symbol=symbol,
                    page_index=page.page_index,
                    observation_key=state.observation_key,
                    revision_id=state.revision_id,
                    related_revision_ids=(),
                    detail=f"{outcome.code.value}: {outcome.detail}",
                )
            )
        elif isinstance(outcome, RestPageDecoded):
            for fact in outcome.summary.quality_facts:
                findings.append(
                    RestQualityFinding(
                        code=fact.fact_type.value,
                        data_type=data_type,
                        symbol=symbol,
                        page_index=page.page_index,
                        observation_key=state.observation_key,
                        revision_id=state.revision_id,
                        related_revision_ids=(),
                        detail=(
                            f"{fact.unit.value} [{fact.range_start}, {fact.range_end}) "
                            f"occurrences={fact.occurrences} missing={fact.missing}: {fact.detail}"
                        ),
                    )
                )
            commits, element_ids, owned, competing = self._store_elements(
                request, symbol, page, outcome, state
            )
            findings.extend(competing)
        else:  # pragma: no cover - the verified reader only yields these two outcomes
            raise RestRevisionStoreError("unknown page decode outcome")
        return (
            RestPageStored(
                symbol=symbol,
                page_index=page.page_index,
                page_identity_sha256=page.page_identity,
                observation_key=state.observation_key,
                response_revision_id=state.revision_id,
                arrival_seq_base=state.base,
                ingest_time=state.ingest_time,
                knowledge_time=state.knowledge_time,
                first_delivery=state.first_delivery,
                decode_outcome=_REJECTED if isinstance(outcome, RestPageRejection) else _ACCEPTED,
                response_commit=state.commit,
                element_commits=commits,
                element_revision_ids=element_ids,
                owned_element_revision_ids=owned,
            ),
            findings,
        )

    # ------------------------------------------------------------------ s1: response revision

    def _store_response(self, request: CollectionRequest, page: d3d._PageRecord) -> _ResponseState:
        definition = BINANCE_SPOT_REST_RESPONSES
        observation_key = rest_identity.response_observation_key(page.page_identity)
        revision_id = rest_identity.revision_id(
            observation_key, rest_identity.rest_source_identity(), page.body.sha256
        )
        last_error: Exception | None = None
        for _ in range(_COMMIT_ATTEMPTS):
            # Order matters: the parent first, then every read. A commit that lands after the
            # parent was read makes the expected-parent commit below fail and we start over.
            parent = self._current_snapshot_id(RESPONSE_TABLE)
            ours, competing = self._response_rows(observation_key, revision_id)
            if ours is not None:
                adopted = self._adopt_response(request, page, ours, competing)
                if self._current_snapshot_id(RESPONSE_TABLE) != parent:
                    # The judgement mixed two snapshots of the response table: read again.
                    last_error = CommitConflict(f"{RESPONSE_TABLE} moved during the adopt read")
                    continue
                return adopted
            base = self._next_block_base()
            knowledge_time = self._knowledge_time(page.retrieved_at)
            row = self._response_row(request, page, base=base, knowledge_time=knowledge_time)
            batch = _batch(definition, [row])
            commit = CommitRequest(
                table=RESPONSE_TABLE,
                batch_id=_response_batch_id(revision_id, base),
                batch_fingerprint=definition.fingerprint_rule.fingerprint(batch),
                row_count=1,
                expected_parent_snapshot_id=parent,
            )
            try:
                result = self._adapter.commit_batch(commit, batch)
            except (CommitConflict, BatchConflict) as exc:
                # Another writer moved the head (or won the same revision with another clock
                # reading): re-read everything. A lost block base is never reused.
                last_error = exc
                continue
            stored = self._verify_response_readback(revision_id, base, _batch_rows(batch)[0])
            return _ResponseState(
                observation_key=observation_key,
                revision_id=revision_id,
                base=base,
                ingest_time=stored["ingest_time"],
                knowledge_time=stored["knowledge_time"],
                first_delivery=True,
                stored_outcome=stored["decode_outcome"],
                stored_element_count=stored["element_count"],
                commit=BatchCommit(
                    table=RESPONSE_TABLE,
                    batch_id=commit.batch_id,
                    snapshot_id=result.snapshot.snapshot_id,
                    outcome=result.outcome,
                    row_count=1,
                ),
                competing=competing,
            )
        raise RestRevisionStoreConflict(
            f"could not allocate or pin a REST arrival block for {revision_id} after "
            f"{_COMMIT_ATTEMPTS} attempts"
        ) from last_error

    def _adopt_response(
        self,
        request: CollectionRequest,
        page: d3d._PageRecord,
        stored: Mapping[str, Any],
        competing: tuple[str, ...],
    ) -> _ResponseState:
        """A committed response revision: verify it field for field, then reuse it as is."""
        base = stored["arrival_seq"]
        _check_block_base(base)
        same_delivery = stored["collection_request_id"] == request.request_id
        provenance = None if same_delivery else {name: stored[name] for name in _PROVENANCE_COLUMNS}
        try:
            expected = self._response_row(
                request,
                page,
                base=base,
                knowledge_time=stored["knowledge_time"],
                provenance=provenance,
            )
        except (RestAvailabilityViolation, rest_identity.RestIdentityViolation, ValueError) as exc:
            raise CatalogIntegrityError(
                f"committed response revision {stored['revision_id']} is not lawful: {exc}"
            ) from None
        expected_row = _batch_rows(_batch(BINANCE_SPOT_REST_RESPONSES, [expected]))[0]
        mismatched = sorted(name for name, value in expected_row.items() if stored[name] != value)
        if mismatched:
            raise CatalogIntegrityError(
                f"committed response revision {stored['revision_id']} disagrees with its "
                f"re-verified page: {mismatched}"
            )
        batch_id = _response_batch_id(stored["revision_id"], base)
        snapshot = self._verify_committed_batch(
            BINANCE_SPOT_REST_RESPONSES, batch_id, [expected_row]
        )
        self._verify_response_readback(stored["revision_id"], base, expected_row)
        return _ResponseState(
            observation_key=stored["observation_key"],
            revision_id=stored["revision_id"],
            base=base,
            ingest_time=stored["ingest_time"],
            knowledge_time=stored["knowledge_time"],
            first_delivery=same_delivery,
            stored_outcome=stored["decode_outcome"],
            stored_element_count=stored["element_count"],
            commit=BatchCommit(
                table=RESPONSE_TABLE,
                batch_id=batch_id,
                snapshot_id=snapshot.snapshot_id,
                outcome=CommitOutcome.ALREADY_COMMITTED,
                row_count=1,
            ),
            competing=competing,
        )

    def _response_row(
        self,
        request: CollectionRequest,
        page: d3d._PageRecord,
        *,
        base: int,
        knowledge_time: datetime,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The response row of ``page``; ``provenance`` substitutes another first delivery."""
        query = page.query
        if provenance is None:
            outcome = page.outcome
            if isinstance(outcome, RestPageRejection):
                decoded: dict[str, Any] = {
                    "decode_outcome": _REJECTED,
                    "decode_rejection_code": outcome.code.value,
                    "element_count": None,
                    "answered_start": None,
                    "answered_end": None,
                }
            else:
                summary = outcome.summary
                if summary.element_count != len(outcome.elements):
                    raise RestRevisionStoreError("page summary does not count its elements")
                answered = summary.answered
                decoded = {
                    "decode_outcome": _ACCEPTED,
                    "decode_rejection_code": None,
                    "element_count": summary.element_count,
                    "answered_start": None if answered.is_empty else _at_ms(answered.start_ms),
                    "answered_end": None if answered.is_empty else _at_ms(answered.end_ms),
                }
            provenance = {
                "collector_id": d3d.REST_COLLECTOR_ID,
                "collector_version": d3d.REST_COLLECTOR_VERSION,
                "collection_request_id": request.request_id,
                "page_index": page.page_index,
                "requested_at": page.requested_at,
                "retrieved_at": page.retrieved_at,
                "source_metadata": [
                    {"name": name, "value": page.http_metadata[name]}
                    for name in sorted(page.http_metadata)
                ],
                **decoded,
            }
        else:
            _check_provenance_shape(provenance)
        return _response_columns(
            data_type=request.data_type,
            source_binding=(request.source.source_id, request.source.version),
            query=query,
            origin=self._origin,
            page_identity=page.page_identity,
            source_uri=page.source_uri,
            http_status=page.http_status,
            body=(page.body.key, page.body.uri, page.body.sha256, page.body.size),
            base=base,
            knowledge_time=knowledge_time,
            provenance=provenance,
        )

    def _response_rows(
        self, observation_key: str, revision_id: str
    ) -> tuple[Mapping[str, Any] | None, tuple[str, ...]]:
        """``(our committed row or None, other revision ids of this page identity)``."""
        rows = self._scan(BINANCE_SPOT_REST_RESPONSES, _equals("observation_key", observation_key))
        ours = [row for row in rows if row["revision_id"] == revision_id]
        if len(ours) > 1:
            raise CatalogIntegrityError(f"response revision {revision_id} is committed twice")
        others: set[str] = set()
        for row in rows:
            derived = rest_identity.revision_id(
                row["observation_key"], row["source_id"], row["payload_hash"]
            )
            if derived != row["revision_id"] or row["source_id"] != (
                rest_identity.rest_source_identity()
            ):
                raise CatalogIntegrityError(
                    f"committed response revision {row['revision_id']} does not re-derive its id"
                )
            if row["revision_id"] != revision_id:
                others.add(row["revision_id"])
        if len(others) + len(ours) != len(rows):
            raise CatalogIntegrityError(f"page identity {observation_key} repeats a revision")
        # A competing revision is a business fact only if it is itself a lawful committed row.
        self._verify_response_facts(rows)
        return (ours[0] if ours else None), tuple(sorted(others))

    def _lawful_response_row(self, stored: Mapping[str, Any]) -> None:
        """Rebuild a committed response row from its own inputs; every column must reproduce.

        The inputs are the page query + origin (page identity, URI, key), the body digest (its
        published object), the block base, ``requested_at`` / ``retrieved_at`` /
        ``knowledge_time`` and the first delivery's validated provenance. Everything else — the
        availability decision, policy binding, source / collector / decoder bindings, schema
        version, empty edges and ``supersedes`` — is derived from the frozen rules and compared.
        """
        revision = stored.get("revision_id")
        try:
            _check_block_base(stored["arrival_seq"])
            data_type = stored["data_type"]
            if data_type not in ELEMENT_TABLES:
                raise ValueError(f"unsupported data_type {data_type!r}")
            query = rest_identity.RestPageQuery.from_pairs(
                data_type, _query_pairs(stored["request_query"])
            )
            origin = stored["request_origin"]
            page_identity = rest_identity.page_identity_sha256(query, origin)
            body_key = rest_identity.response_object_key(
                data_type, query.symbol, stored["payload_hash"], page_identity
            )
            provenance = {name: stored[name] for name in _PROVENANCE_COLUMNS}
            _check_provenance_shape(provenance)
            _check_provenance_values(provenance)
            body = self._storage.lookup(body_key)
            if body is None:
                raise ValueError(f"its response body {body_key!r} is not published")
            expected = _response_columns(
                data_type=data_type,
                source_binding=(d3d.REST_SOURCE.source_id, d3d.REST_SOURCE.version),
                query=query,
                origin=origin,
                page_identity=page_identity,
                source_uri=rest_identity.page_source_uri(query, origin),
                http_status=_HTTP_OK,
                body=(body.key, body.uri, body.sha256, body.size),
                base=stored["arrival_seq"],
                knowledge_time=stored["knowledge_time"],
                provenance=provenance,
            )
        except IntegrityViolation as exc:
            raise CatalogIntegrityError(
                f"the body of committed response revision {revision} does not match: {exc}"
            ) from None
        except StorageError as exc:
            raise RestRevisionStoreError(f"storage failure: {exc}") from exc
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise CatalogIntegrityError(
                f"committed response revision {revision} is not lawful: {exc}"
            ) from None
        normalised = _batch_rows(_batch(BINANCE_SPOT_REST_RESPONSES, [expected]))[0]
        mismatched = sorted(
            name
            for name, value in normalised.items()
            if name not in stored or stored[name] != value
        )
        if mismatched:
            raise CatalogIntegrityError(
                f"committed response revision {revision} does not reproduce from its own "
                f"inputs: {mismatched}"
            )

    def _verify_response_facts(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Each row is lawful, holds its block alone and is its batch's only, exact content."""
        if not rows:
            return
        for row in rows:
            self._lawful_response_row(row)
        by_base: dict[int, str] = {}
        for row in rows:
            if by_base.setdefault(row["arrival_seq"], row["revision_id"]) != row["revision_id"]:
                raise CatalogIntegrityError(
                    f"REST arrival block {row['arrival_seq']} is held by two response revisions"
                )
        bases = sorted(by_base)
        holders: dict[int, list[str]] = {}
        for offset in range(0, len(bases), _KEY_CHUNK):
            found = self._adapter.scan_columns(
                RESPONSE_TABLE,
                columns=("revision_id", "arrival_seq"),
                row_filter=_member("arrival_seq", bases[offset : offset + _KEY_CHUNK]),
            ).to_pylist()
            for item in found:
                holders.setdefault(item["arrival_seq"], []).append(item["revision_id"])
        for base, revision in by_base.items():
            if holders.get(base) != [revision]:
                raise CatalogIntegrityError(
                    f"REST arrival block {base} is not held by response revision {revision} alone"
                )
        batches = {_response_batch_id(row["revision_id"], row["arrival_seq"]): row for row in rows}
        snapshots = _snapshots_of_batches(self._adapter, RESPONSE_TABLE, batches)
        for batch_id, row in batches.items():
            found_snapshots = snapshots[batch_id]
            if len(found_snapshots) != 1:
                raise CatalogIntegrityError(
                    f"{RESPONSE_TABLE} has rows of batch {batch_id} but {len(found_snapshots)} "
                    "snapshots committing it"
                )
            _check_batch_snapshot(BINANCE_SPOT_REST_RESPONSES, batch_id, found_snapshots[0], [row])

    def _verify_response_readback(
        self, revision_id: str, base: int, expected: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Exactly one committed row carries ``revision_id`` and exactly one carries ``base``."""
        rows = self._scan(BINANCE_SPOT_REST_RESPONSES, _equals("revision_id", revision_id), limit=2)
        if len(rows) != 1:
            raise CatalogIntegrityError(
                f"response revision {revision_id} is committed {len(rows)} times"
            )
        mismatched = sorted(name for name, value in expected.items() if rows[0][name] != value)
        if mismatched:
            raise CatalogIntegrityError(
                f"response revision {revision_id} reads back differently: {mismatched}"
            )
        holders = self._scan(BINANCE_SPOT_REST_RESPONSES, _equals("arrival_seq", base), limit=2)
        if len(holders) != 1 or holders[0]["revision_id"] != revision_id:
            raise CatalogIntegrityError(f"REST arrival block {base} is not held by one revision")
        return rows[0]

    def _next_block_base(self) -> int:
        """The next REST block base above every committed response ``arrival_seq``."""
        largest = self._adapter.max_int64(RESPONSE_TABLE, "arrival_seq", check=_check_block_base)
        try:
            return rest_identity.rest_arrival_block_base(largest)
        except rest_identity.RestArrivalSeqOverflow as exc:
            raise RestRevisionStoreError(str(exc)) from None
        except rest_identity.RestIdentityViolation as exc:
            raise CatalogIntegrityError(f"corrupt REST arrival anchor: {exc}") from None

    def _knowledge_time(self, ingest_time: datetime) -> datetime:
        """The local time the response revision becomes selectable (after all verification)."""
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != _ZERO:
            raise RestRevisionStoreError("the clock must return timezone-aware UTC")
        if now < ingest_time:
            raise RestRevisionStoreConflict(
                "knowledge_time would precede ingest_time: refusing to guess a local clock"
            )
        return now

    # ------------------------------------------------------------------ s2: element revisions

    def _store_elements(
        self,
        request: CollectionRequest,
        symbol: str,
        page: d3d._PageRecord,
        decoded: RestPageDecoded,
        state: _ResponseState,
    ) -> tuple[tuple[BatchCommit, ...], tuple[str, ...], tuple[str, ...], list[RestQualityFinding]]:
        data_type = request.data_type
        elements = decoded.elements
        if state.stored_outcome != _ACCEPTED or state.stored_element_count != len(elements):
            # The response revision's knowledge_time was stamped after *its* first decode. An
            # element may only inherit it if that decode released exactly these elements.
            raise RestRevisionStoreConflict(
                f"response revision {state.revision_id} was first decoded as "
                f"{state.stored_outcome} with {state.stored_element_count} element(s); this "
                f"delivery decodes {len(elements)}: inheriting its knowledge_time would backfill"
            )
        if not elements:
            return (), (), (), []
        definition = _ELEMENT_DEFINITIONS[data_type]
        planned = _plan_elements(definition, data_type, symbol, elements, state)
        keys = sorted({item.observation_key for item in planned})
        commits: list[BatchCommit] = []
        last_error: Exception | None = None
        for _ in range(_COMMIT_ATTEMPTS):
            parent, existing = self._pinned_element_rows(definition, data_type, symbol, keys)
            status = _classify(planned, existing, state.revision_id)
            commits = []
            try:
                for index, members in _microbatches(planned, status, self._microbatch):
                    batch_id = _element_batch_id(state.revision_id, index)
                    kinds = {status[item.revision_id] for item in members}
                    rows = [item.row for item in members]
                    if kinds == {"owned"}:
                        snapshot = self._verify_committed_batch(definition, batch_id, rows)
                        commits.append(
                            BatchCommit(
                                table=definition.table,
                                batch_id=batch_id,
                                snapshot_id=snapshot.snapshot_id,
                                outcome=CommitOutcome.ALREADY_COMMITTED,
                                row_count=len(rows),
                            )
                        )
                        continue
                    if kinds != {"new"}:
                        raise CatalogIntegrityError(
                            f"element batch {batch_id} is only partially committed"
                        )
                    batch = _batch(definition, rows)
                    commit = CommitRequest(
                        table=definition.table,
                        batch_id=batch_id,
                        batch_fingerprint=definition.fingerprint_rule.fingerprint(batch),
                        row_count=len(rows),
                        expected_parent_snapshot_id=parent,
                    )
                    try:
                        result = self._adapter.commit_batch(commit, batch)
                    except BatchConflict as exc:
                        raise CatalogIntegrityError(
                            f"element batch {batch_id} is committed with other content"
                        ) from exc
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
            except CommitConflict as exc:
                last_error = exc
                continue
            break
        else:
            raise RestRevisionStoreConflict(
                f"element batches of {state.revision_id} lost {_COMMIT_ATTEMPTS} commit races"
            ) from last_error

        # Read back: every element this response owns is committed exactly once, as planned.
        _, final = self._pinned_element_rows(definition, data_type, symbol, keys)
        status = _classify(planned, final, state.revision_id)
        if any(status[item.revision_id] == "new" for item in planned):
            raise CatalogIntegrityError(
                f"an element of {state.revision_id} is missing after its batch committed"
            )
        owned = tuple(item.revision_id for item in planned if status[item.revision_id] == "owned")
        findings = _competing_findings(data_type, symbol, page.page_index, planned, final)
        return (
            tuple(commits),
            tuple(item.revision_id for item in planned),
            owned,
            findings,
        )

    def _pinned_element_rows(
        self,
        definition: RegisteredTableDefinition,
        data_type: str,
        symbol: str,
        keys: Sequence[str],
    ) -> tuple[str | None, list[Mapping[str, Any]]]:
        """``(element head, rows)`` read and verified against one fixed pair of table heads.

        The element rows and their lineage response rows live in two tables and the catalog
        reads current heads only, so both heads are read before and after the whole read and
        verification; a moved head (heads only move forward) discards the judgement and reads
        again, a bounded number of times.
        """
        for _ in range(_COMMIT_ATTEMPTS):
            heads = (
                self._current_snapshot_id(definition.table),
                self._current_snapshot_id(RESPONSE_TABLE),
            )
            rows = self._element_rows(definition, data_type, symbol, keys)
            if (
                self._current_snapshot_id(definition.table),
                self._current_snapshot_id(RESPONSE_TABLE),
            ) == heads:
                return heads[0], rows
        raise RestRevisionStoreConflict(
            f"{definition.table} / {RESPONSE_TABLE} kept moving: no pinned read of "
            f"{len(keys)} element key(s) after {_COMMIT_ATTEMPTS} attempts"
        )

    def _element_rows(
        self,
        definition: RegisteredTableDefinition,
        data_type: str,
        symbol: str,
        keys: Sequence[str],
    ) -> list[Mapping[str, Any]]:
        """Every committed revision of ``keys``, each proven lawful down to its lineage."""
        rows = self._scan(
            definition, _both(_equals("symbol", symbol), _member("observation_key", keys))
        )
        seen_ids: set[str] = set()
        seen_seqs: set[int] = set()
        for row in rows:
            _verify_element_identity(definition.table, data_type, row)
            if row["revision_id"] in seen_ids:
                raise CatalogIntegrityError(
                    f"element revision {row['revision_id']} is committed twice"
                )
            seen_ids.add(row["revision_id"])
            if row["arrival_seq"] in seen_seqs:
                raise CatalogIntegrityError(f"arrival_seq {row['arrival_seq']} is not unique")
            seen_seqs.add(row["arrival_seq"])
        self._verify_element_lineage(definition, data_type, rows)
        return rows

    def _verify_element_lineage(
        self,
        definition: RegisteredTableDefinition,
        data_type: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Each element row is exactly what its lineage response revision must have released.

        ``response_revision_id`` must name exactly one committed, lawful (fully re-derived),
        accepted response revision of the same data type and symbol whose element count covers
        ``element_index``. The element row is then rebuilt from its native fields and that
        response's block base and times — arrival number, availability decision, decoder
        binding, schema version, empty edges and ``supersedes`` — and must match column for
        column. Its arrival number must be held by this revision alone.
        """
        if not rows:
            return
        table = definition.table
        lineage_ids = sorted({row["response_revision_id"] for row in rows})
        found: dict[str, list[Mapping[str, Any]]] = {}
        for offset in range(0, len(lineage_ids), _KEY_CHUNK):
            chunk = lineage_ids[offset : offset + _KEY_CHUNK]
            for response in self._scan(BINANCE_SPOT_REST_RESPONSES, _member("revision_id", chunk)):
                found.setdefault(response["revision_id"], []).append(response)
        for lineage_id in lineage_ids:
            count = len(found.get(lineage_id, ()))
            if count != 1:
                raise CatalogIntegrityError(
                    f"{table}: lineage response revision {lineage_id} is committed {count} "
                    "time(s); an element must name exactly one committed response"
                )
        lineage = {lineage_id: found[lineage_id][0] for lineage_id in lineage_ids}
        self._verify_response_facts([lineage[lineage_id] for lineage_id in lineage_ids])
        for row in rows:
            response = lineage[row["response_revision_id"]]
            revision = row["revision_id"]
            if (
                response["data_type"] != data_type
                or response["symbol"] != row["symbol"]
                or response["decode_outcome"] != _ACCEPTED
            ):
                raise CatalogIntegrityError(
                    f"{table}: element {revision} names response revision {response['revision_id']}"
                    f" that did not accept a {data_type} page of {row['symbol']}"
                )
            index = row["element_index"]
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < response["element_count"]
            ):
                raise CatalogIntegrityError(
                    f"{table}: element {revision} has element_index {index!r} outside the "
                    f"{response['element_count']} element(s) of its lineage response"
                )
            try:
                *_, expected = _element_columns(
                    definition,
                    data_type,
                    row["symbol"],
                    {name: row[name] for name in _ELEMENT_NATIVE_COLUMNS[data_type]},
                    element_index=index,
                    response_revision_id=response["revision_id"],
                    base=response["arrival_seq"],
                    ingest_time=response["ingest_time"],
                    knowledge_time=response["knowledge_time"],
                )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise CatalogIntegrityError(
                    f"{table}: element {revision} cannot be what its lineage response "
                    f"released: {exc}"
                ) from None
            mismatched = sorted(
                name for name, value in expected.items() if name not in row or row[name] != value
            )
            if mismatched:
                raise CatalogIntegrityError(
                    f"{table}: element {revision} does not inherit its lineage response "
                    f"{response['revision_id']}: {mismatched}"
                )
        by_seq = {row["arrival_seq"]: row["revision_id"] for row in rows}
        seqs = sorted(by_seq)
        holders: dict[int, list[str]] = {}
        for offset in range(0, len(seqs), _KEY_CHUNK):
            held = self._adapter.scan_columns(
                table,
                columns=("revision_id", "arrival_seq"),
                row_filter=_member("arrival_seq", seqs[offset : offset + _KEY_CHUNK]),
            ).to_pylist()
            for item in held:
                holders.setdefault(item["arrival_seq"], []).append(item["revision_id"])
        for seq, revision in by_seq.items():
            if holders.get(seq) != [revision]:
                raise CatalogIntegrityError(
                    f"{table}: arrival_seq {seq} is not held by element {revision} alone"
                )

    # ------------------------------------------------------------------ catalog helpers

    def _scan(
        self,
        definition: RegisteredTableDefinition,
        row_filter: BooleanExpression,
        *,
        limit: int | None = None,
    ) -> list[Mapping[str, Any]]:
        columns = tuple(field.name for field in definition.arrow_schema)
        rows: list[Mapping[str, Any]] = self._adapter.scan_columns(
            definition.table, columns=columns, row_filter=row_filter, limit=limit
        ).to_pylist()
        return rows

    def _current_snapshot_id(self, table: str) -> str | None:
        info = self._adapter.load_table(table)
        if info is None:
            raise TableNotFound(f"table {table} does not exist; create the Phase 1 tables first")
        return None if info.current_snapshot is None else info.current_snapshot.snapshot_id

    def _verify_committed_batch(
        self,
        definition: RegisteredTableDefinition,
        batch_id: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> SnapshotInfo:
        """The snapshot that committed ``batch_id`` must carry exactly these rows' fingerprint."""
        snapshot = _snapshot_of_batch(self._adapter, definition.table, batch_id)
        _check_batch_snapshot(definition, batch_id, snapshot, rows)
        return snapshot


# =========================================================================================
# pure helpers
# =========================================================================================


def _response_batch_id(revision_id: str, base: int) -> str:
    """Stable id of the one-row response batch; the block base keeps a lost race distinct."""
    return f"{revision_id}.response.{base}"


def _element_batch_id(response_revision_id: str, index: int) -> str:
    """Stable id of the ``index``-th element microbatch of one response revision."""
    return f"{response_revision_id}.elements.{index:08d}"


def _check_block_base(value: object) -> None:
    """A committed response ``arrival_seq`` must be a REST block base (else fail closed)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise CatalogIntegrityError("a committed REST response arrival_seq is not an integer")
    try:
        rest_identity.check_rest_arrival_seq(value)
    except rest_identity.RestIdentityViolation as exc:
        raise CatalogIntegrityError(f"corrupt REST response arrival_seq: {exc}") from None
    if (value - rest_identity.REST_ARRIVAL_SEQ_BASE) % rest_identity.REST_ARRIVAL_SEQ_STRIDE:
        raise CatalogIntegrityError(f"REST response arrival_seq {value} is not a block base")


def _check_provenance_shape(provenance: Mapping[str, Any]) -> None:
    """A foreign first delivery's decode columns must at least be self-consistent."""
    outcome = provenance["decode_outcome"]
    code = provenance["decode_rejection_code"]
    count = provenance["element_count"]
    start, end = provenance["answered_start"], provenance["answered_end"]
    if outcome == _ACCEPTED:
        if code is not None or not isinstance(count, int) or count < 0:
            raise ValueError("an accepted page has a count and no rejection code")
    elif outcome == _REJECTED:
        if code is None or count is not None or start is not None or end is not None:
            raise ValueError("a rejected page has a code and no count or answered interval")
    else:
        raise ValueError(f"unknown decode outcome {outcome!r}")
    if (start is None) != (end is None) or (start is not None and not start < end):
        raise ValueError("the answered interval is malformed")
    if provenance["collector_id"] != d3d.REST_COLLECTOR_ID or (
        provenance["collector_version"] != d3d.REST_COLLECTOR_VERSION
    ):
        raise ValueError("the first delivery names another collector")


def _check_provenance_values(provenance: Mapping[str, Any]) -> None:
    """The first delivery's recorded values must be ones the D3D / D3C pipeline can produce."""
    request_id = provenance["collection_request_id"]
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("the first delivery has no collection request id")
    page_index = provenance["page_index"]
    if not isinstance(page_index, int) or isinstance(page_index, bool) or page_index < 0:
        raise ValueError("the first delivery's page_index is not a non-negative int")
    names: list[str] = []
    for item in provenance["source_metadata"]:
        if set(item) != {"name", "value"} or not isinstance(item["value"], str):
            raise ValueError("source_metadata items are (name, value) text pairs")
        names.append(item["name"])
    if names != sorted(set(names)) or not set(names) <= d3d.ALLOWED_RESPONSE_HEADERS:
        raise ValueError("source_metadata is not the sorted header allowlist projection")
    code = provenance["decode_rejection_code"]
    if code is not None and code not in {item.value for item in RestRejectionCode}:
        raise ValueError(f"unknown decoder rejection code {code!r}")
    count = provenance["element_count"]
    if count is not None and count > rest_identity.PAGE_LIMIT:
        raise ValueError("an accepted page holds more elements than one page can")
    for label in ("answered_start", "answered_end"):
        value = provenance[label]
        if value is not None and (value - _EPOCH) % timedelta(milliseconds=1):
            raise ValueError(f"{label} is not a whole millisecond")


def _query_pairs(text: object) -> list[tuple[str, str]]:
    """``name=value&…`` back into pairs; the query rule then demands the canonical encoding."""
    if not isinstance(text, str) or not text:
        raise ValueError("request_query is empty")
    pairs: list[tuple[str, str]] = []
    for part in text.split("&"):
        name, separator, value = part.partition("=")
        if not separator or not name or "=" in value:
            raise ValueError("request_query is not name=value pairs")
        pairs.append((name, value))
    return pairs


def _response_columns(
    *,
    data_type: str,
    source_binding: tuple[str, str],
    query: rest_identity.RestPageQuery,
    origin: str,
    page_identity: str,
    source_uri: str,
    http_status: int,
    body: tuple[str, str, str, int],
    base: int,
    knowledge_time: datetime,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Every column of one response revision from its inputs (the single builder, s1)."""
    body_key, body_uri, body_sha256, body_size = body
    observation_key = rest_identity.response_observation_key(page_identity)
    source = rest_identity.rest_source_identity()
    requested_at = provenance["requested_at"]
    retrieved_at = provenance["retrieved_at"]
    decision = decide_rest_availability(
        RestAvailabilitySubject.RESPONSE,
        event_time=requested_at,
        event_end_time=retrieved_at,
        ingest_time=retrieved_at,
        knowledge_time=knowledge_time,
    )
    record = RevisionRecord(
        observation_key=observation_key,
        revision_id=rest_identity.revision_id(observation_key, source, body_sha256),
        source_id=source,
        payload_hash=body_sha256,
        arrival_seq=base,
        availability=decision,
    )
    row = _revision_block(record)
    row.update(
        {
            "event_time": requested_at,
            "event_end_time": retrieved_at,
            "source_binding_id": source_binding[0],
            "source_binding_version": source_binding[1],
            "data_type": data_type,
            "symbol": query.symbol,
            "request_origin": origin,
            "request_path": query.path,
            "request_query": query.query_string(),
            "declared_time_unit": rest_identity.DECLARED_TIME_UNIT,
            "page_limit": query.limit,
            "page_identity_sha256": page_identity,
            "source_uri": source_uri,
            "http_status": http_status,
            "object_key": body_key,
            "object_uri": body_uri,
            "object_sha256": body_sha256,
            "object_size_bytes": body_size,
            "decoder_id": DECODER_BINDING.policy_id,
            "decoder_version": DECODER_BINDING.version,
            "decoder_hash": DECODER_BINDING.policy_hash,
            **provenance,
        }
    )
    return row


def _revision_block(record: RevisionRecord) -> dict[str, Any]:
    """The revision columns shared by the REST tables, from a validated contract record."""
    times = record.availability.times
    decision: AvailabilityDecision = record.availability
    return {
        "observation_key": record.observation_key,
        "revision_id": record.revision_id,
        "source_id": record.source_id,
        "payload_hash": record.payload_hash,
        "arrival_seq": record.arrival_seq,
        "supersedes": list(record.supersedes),
        "source_revision_id": record.source_revision_id,
        "source_revision_time": record.source_revision_time,
        "source_time": times.source_time,
        "available_time": times.available_time,
        "ingest_time": times.ingest_time,
        "knowledge_time": times.knowledge_time,
        "declared_latency_us": times.declared_latency // timedelta(microseconds=1),
        "availability_policy_id": decision.policy.policy_id,
        "availability_policy_version": decision.policy.version,
        "availability_policy_hash": decision.policy.policy_hash,
        "availability_evidence": list(decision.evidence),
        "availability_evidence_gap": decision.evidence_gap,
        # REST 1.0.0 never produces an edge inside the channel (binance.spot.rest-revision).
        "precedence_evidence": [],
        "contract_schema_version": record.schema_version,
    }


def _batch(definition: RegisteredTableDefinition, rows: Sequence[Mapping[str, Any]]) -> pa.Table:
    """Rows → a batch in the registered Arrow schema; any column drift fails closed here."""
    names = [field.name for field in definition.arrow_schema]
    for row in rows:
        drift = sorted(set(names) ^ set(row))
        if drift:
            raise RestRevisionStoreError(f"row for {definition.table} drifts: {drift}")
    batch = pa.Table.from_pylist([dict(row) for row in rows], schema=definition.arrow_schema)
    if batch.num_rows != len(rows):  # pragma: no cover - from_pylist keeps the row count
        raise RestRevisionStoreError(f"batch for {definition.table} lost rows")
    return batch


def _batch_rows(batch: pa.Table) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = batch.to_pylist()
    return rows


def _snapshot_of_batch(adapter: RevisionCatalog, table: str, batch_id: str) -> SnapshotInfo:
    """The main-branch snapshot that committed ``batch_id`` (exactly one), else fail closed."""
    found = _snapshots_of_batches(adapter, table, [batch_id])[batch_id]
    if len(found) != 1:
        raise CatalogIntegrityError(
            f"{table} has rows of batch {batch_id} but {len(found)} snapshots committing it"
        )
    return found[0]


def _snapshots_of_batches(
    adapter: RevisionCatalog, table: str, batch_ids: Iterable[str]
) -> dict[str, list[SnapshotInfo]]:
    """Main-branch snapshots committing each of ``batch_ids``, in one walk of the history."""
    wanted: dict[str, list[SnapshotInfo]] = {batch_id: [] for batch_id in batch_ids}
    info = adapter.load_table(table)
    if info is None:
        raise TableNotFound(f"table {table} does not exist")
    snapshot = info.current_snapshot
    while snapshot is not None:
        if snapshot.batch_id in wanted:
            wanted[snapshot.batch_id].append(snapshot)
        parent = snapshot.parent_snapshot_id
        snapshot = None if parent is None else adapter.get_snapshot(table, parent)
    return wanted


def _check_batch_snapshot(
    definition: RegisteredTableDefinition,
    batch_id: str,
    snapshot: SnapshotInfo,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    """The snapshot committing ``batch_id`` must carry exactly these rows' fingerprint."""
    batch = _batch(definition, [dict(row) for row in rows])
    if snapshot.batch_fingerprint != definition.fingerprint_rule.fingerprint(
        batch
    ) or snapshot.added_rows != len(rows):
        raise CatalogIntegrityError(
            f"batch {batch_id} of {definition.table} was committed with other content"
        )


def _normalised_natives(
    definition: RegisteredTableDefinition, natives: Sequence[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """Native fields exactly as the table stores them (``decimal(38, 18)`` included)."""
    if not natives:
        return []
    names = list(natives[0])
    schema = pa.schema([definition.arrow_schema.field(name) for name in names])
    return _batch_rows(pa.Table.from_pylist([dict(item) for item in natives], schema=schema))


def _plan_elements(
    definition: RegisteredTableDefinition,
    data_type: str,
    symbol: str,
    elements: Sequence[Any],
    state: _ResponseState,
) -> list[_PlannedElement]:
    """One fully built, normalised row per decoded element (no catalog I/O)."""
    natives = _normalised_natives(definition, [element.native_fields() for element in elements])
    planned: list[_PlannedElement] = []
    previous_index = -1
    for position, (element, native) in enumerate(zip(elements, natives, strict=True)):
        if element.element_index <= previous_index:
            raise RestRevisionStoreError("decoded elements are not in response order")
        previous_index = element.element_index
        if set(native) != set(_ELEMENT_NATIVE_COLUMNS[data_type]):
            raise RestRevisionStoreError("decoded native fields drift from the element table")
        if data_type == "agg_trades":
            if not isinstance(element, RestAggTradeElement):
                raise RestRevisionStoreError("an aggTrades page decoded a non-aggTrade")
            if element.event_time != _at_ms(native["timestamp_raw"]):
                raise RestRevisionStoreError("decoded event_time is not T in milliseconds")
        else:
            if not isinstance(element, RestKline1mElement):
                raise RestRevisionStoreError("a klines page decoded a non-kline")
            start = _at_ms(native["open_time_raw"])
            end = _at_ms(native["close_time_raw"] + 1)
            if (element.interval_start, element.interval_end) != (start, end) or (
                end - start != _MINUTE
            ):
                raise RestRevisionStoreError("decoded interval is not the declared minute")
        try:
            key, revision_id, payload, row = _element_columns(
                definition,
                data_type,
                symbol,
                native,
                element_index=element.element_index,
                response_revision_id=state.revision_id,
                base=state.base,
                ingest_time=state.ingest_time,
                knowledge_time=state.knowledge_time,
            )
        except (RestAvailabilityViolation, rest_identity.RestIdentityViolation) as exc:
            raise RestRevisionStoreConflict(
                f"element {element.element_index} of {state.revision_id} cannot inherit the "
                f"response revision's times: {exc}"
            ) from None
        planned.append(
            _PlannedElement(
                position=position,
                element_index=element.element_index,
                observation_key=key,
                revision_id=revision_id,
                payload_hash=payload,
                row=row,
            )
        )
    if len({item.revision_id for item in planned}) != len(planned):
        raise RestRevisionStoreError("one page decoded the same element revision twice")
    return planned


def _element_columns(
    definition: RegisteredTableDefinition,
    data_type: str,
    symbol: str,
    native: Mapping[str, Any],
    *,
    element_index: int,
    response_revision_id: str,
    base: int,
    ingest_time: datetime,
    knowledge_time: datetime,
) -> tuple[str, str, str, Mapping[str, Any]]:
    """``(key, revision id, payload hash, normalised row)`` of one element of a response.

    The single builder (s2): the element's times come from its native fields, its arrival
    number from the response's block base, its ``ingest_time`` / ``knowledge_time`` from the
    response revision; bindings, schema version and empty edges from the frozen rules.
    """
    subject = _ELEMENT_SUBJECTS[data_type]
    source = rest_identity.rest_source_identity()
    times: dict[str, Any]
    if data_type == "agg_trades":
        event_time = _at_ms(native["timestamp_raw"])
        times = {"event_time": event_time}
        key = rest_identity.agg_trade_observation_key(symbol, native["agg_trade_id"])
        payload = rest_identity.agg_trade_payload_hash(symbol, dict(native))
        decision = decide_rest_availability(
            subject, event_time=event_time, ingest_time=ingest_time, knowledge_time=knowledge_time
        )
    else:
        start = _at_ms(native["open_time_raw"])
        end = _at_ms(native["close_time_raw"] + 1)
        times = {"interval_start": start, "interval_end": end}
        key = rest_identity.kline_1m_observation_key(symbol, start)
        payload = rest_identity.kline_1m_payload_hash(symbol, dict(native))
        decision = decide_rest_availability(
            subject,
            event_time=start,
            event_end_time=end,
            ingest_time=ingest_time,
            knowledge_time=knowledge_time,
        )
    revision_id = rest_identity.revision_id(key, source, payload)
    record = RevisionRecord(
        observation_key=key,
        revision_id=revision_id,
        source_id=source,
        payload_hash=payload,
        arrival_seq=rest_identity.element_arrival_seq(base, element_index),
        availability=decision,
    )
    row = _revision_block(record)
    row.update(times)
    row.update(
        {
            "symbol": symbol,
            "response_revision_id": response_revision_id,
            "element_index": element_index,
            "decoder_id": DECODER_BINDING.policy_id,
            "decoder_version": DECODER_BINDING.version,
            "decoder_hash": DECODER_BINDING.policy_hash,
            **native,
        }
    )
    return key, revision_id, payload, _batch_rows(_batch(definition, [row]))[0]


def _verify_element_identity(table: str, data_type: str, row: Mapping[str, Any]) -> None:
    """A committed element row must re-derive its key, payload hash and id (REST rule)."""
    try:
        symbol = row["symbol"]
        if data_type == "agg_trades":
            key = rest_identity.agg_trade_observation_key(symbol, row["agg_trade_id"])
            payload = rest_identity.agg_trade_payload_hash(symbol, dict(row))
            times_ok = row["event_time"] == _at_ms(row["timestamp_raw"])
        else:
            key = rest_identity.kline_1m_observation_key(symbol, row["interval_start"])
            payload = rest_identity.kline_1m_payload_hash(symbol, dict(row))
            times_ok = row["interval_start"] == _at_ms(row["open_time_raw"]) and row[
                "interval_end"
            ] == _at_ms(row["close_time_raw"] + 1)
        source = rest_identity.rest_source_identity()
        derived = rest_identity.revision_id(key, source, payload)
        rest_identity.check_rest_arrival_seq(row["arrival_seq"])
    except (rest_identity.RestIdentityViolation, KeyError, TypeError) as exc:
        raise CatalogIntegrityError(f"{table}: a committed row is not lawful ({exc})") from None
    for label, stored, expected in (
        ("observation_key", row["observation_key"], key),
        ("source_id", row["source_id"], source),
        ("payload_hash", row["payload_hash"], payload),
        ("revision_id", row["revision_id"], derived),
    ):
        if stored != expected:
            raise CatalogIntegrityError(
                f"{table}: committed {label} of {row['revision_id']} does not re-derive"
            )
    if not times_ok:
        raise CatalogIntegrityError(f"{table}: committed times of {row['revision_id']} drift")


def _classify(
    planned: Sequence[_PlannedElement],
    existing: Sequence[Mapping[str, Any]],
    response_revision_id: str,
) -> dict[str, str]:
    """``owned`` (committed by this response, as planned), ``foreign`` or ``new`` per element."""
    by_id = {row["revision_id"]: row for row in existing}
    status: dict[str, str] = {}
    for item in planned:
        row = by_id.get(item.revision_id)
        if row is None:
            status[item.revision_id] = "new"
        elif row["response_revision_id"] == response_revision_id:
            mismatched = sorted(name for name, value in item.row.items() if row[name] != value)
            if mismatched:
                raise CatalogIntegrityError(
                    f"committed element {item.revision_id} disagrees with its re-decoded "
                    f"element: {mismatched}"
                )
            status[item.revision_id] = "owned"
        else:
            status[item.revision_id] = "foreign"
    return status


def _microbatches(
    planned: Sequence[_PlannedElement], status: Mapping[str, str], size: int
) -> list[tuple[int, list[_PlannedElement]]]:
    """Deterministic batches by element position; foreign elements are never re-written."""
    batches: list[tuple[int, list[_PlannedElement]]] = []
    for index, offset in enumerate(range(0, len(planned), size)):
        members = [
            item
            for item in planned[offset : offset + size]
            if status[item.revision_id] in {"owned", "new"}
        ]
        if members:
            batches.append((index, members))
    return batches


def _competing_findings(
    data_type: str,
    symbol: str,
    page_index: int,
    planned: Sequence[_PlannedElement],
    existing: Sequence[Mapping[str, Any]],
) -> list[RestQualityFinding]:
    """Other REST revisions of the same element key with a different payload (fail closed)."""
    by_key: dict[str, set[str]] = {}
    for row in existing:
        by_key.setdefault(row["observation_key"], set()).add(row["revision_id"])
    findings: list[RestQualityFinding] = []
    for item in planned:
        others = tuple(sorted(by_key.get(item.observation_key, set()) - {item.revision_id}))
        if others:
            findings.append(
                RestQualityFinding(
                    code=_ELEMENT_COMPETING[data_type],
                    data_type=data_type,
                    symbol=symbol,
                    page_index=page_index,
                    observation_key=item.observation_key,
                    revision_id=item.revision_id,
                    related_revision_ids=others,
                    detail=(
                        f"{len(others) + 1} REST revisions of one element with different "
                        "payloads: competing heads, no edge (binance.spot.rest-revision@1.0.0)"
                    ),
                )
            )
    return findings

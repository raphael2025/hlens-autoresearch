"""Replayable Binance public spot exchangeInfo snapshot collector (Phase 1 E2; ADR-0029 §1).

``binance.spot.public-exchange-info@1.0.0`` serves exactly one source binding,
``binance.public.spot.exchange-info@1.0.0``, and reaches exactly one market-data-only endpoint of
the configured origin: ``GET /api/v3/exchangeInfo`` with the single parameter ``symbols`` set to
the first-slice pair. There is no account, order or signed endpoint, no API key is read from
anywhere, and no redirect is ever followed.

What this module owns:

- **the request gate** — an ``ExchangeInfoRequest`` must name this source and exactly the
  first-slice symbols, and the URL is built from the structured ``ExchangeInfoQuery`` and then
  re-parsed against the allowlist; anything else is refused **before any network I/O**;
- **the commitments** — the answer's bytes are published first as an immutable,
  content-addressed object, then the logical attempt as one immutable snapshot checkpoint
  (write-once through ``StorageAdapter.publish``; identical content is idempotent, other content is
  ``ObjectConflict`` and the first writer wins). No mutable sidecar, no overwrite;
- **replay** — the same ``request_id`` again (same instance, a rebuilt one, after a crash) returns
  the committed snapshot, strictly re-verified and re-decoded, or replays the same stable
  rejection, **without touching the network**. A ``request_id`` reused for other request content
  fails closed.

The wire is the accepted D3D wire, reused rather than re-implemented: one owned HTTP client (no
auth, hooks, cookies, redirects or environment trust), bounded retries, ``Retry-After``, a minimum
request spacing, a streaming body bound, the response-header allowlist with credential-shaped
names refused, and the two local instants ``requested_at`` / ``retrieved_at``. Network, rate-limit,
non-200 and transport failures raise ``CollectionFailed`` and commit nothing. A decoder rejection
is committed as the attempt's stable failure and replayed as ``ExchangeInfoSnapshotRejected``; it is
never a Raw revision (ADR-0029 "failure semantics").
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Self

import httpx

from core.contracts import _uri
from core.contracts.collector import (
    REQUEST_ID_PATTERN,
    CollectionFailed,
    CollectorDescriptor,
    SourceBinding,
    UnsupportedRequest,
)
from core.contracts.storage import (
    ObjectConflict,
    ObjectRef,
    StageRequest,
    StorageAdapter,
    StorageError,
)
from core.domain.base import canonical_json
from infrastructure.collector import binance_rest as d3d
from infrastructure.contract_version import PHASE1_PUBLICATION_VERSION
from infrastructure.parser.binance_exchange_info import (
    EXCHANGE_INFO_DECODER_BINDING,
    MAX_BODY_LIMIT_BYTES,
    MIN_BODY_LIMIT_BYTES,
    ExchangeInfoDecoded,
    ExchangeInfoDecodeRequestError,
    ExchangeInfoRejection,
    ExchangeInfoSymbol,
    decode_exchange_info,
)
from infrastructure.revision.exchange_info_identity import (
    EXCHANGE_INFO_PATH,
    EXCHANGE_INFO_SOURCE_ID,
    EXCHANGE_INFO_SOURCE_VERSION,
    EXCHANGE_INFO_SYMBOLS,
    ExchangeInfoIdentityViolation,
    ExchangeInfoQuery,
    request_identity_sha256,
    request_source_uri,
    response_object_key,
)
from infrastructure.settings import Settings

__all__ = [
    "CHECKPOINT_PREFIX",
    "CHECKPOINT_VERSION",
    "EXCHANGE_INFO_COLLECTOR_ID",
    "EXCHANGE_INFO_COLLECTOR_VERSION",
    "EXCHANGE_INFO_SOURCE",
    "SNAPSHOT_CHECKPOINT_KIND",
    "BinanceSpotExchangeInfoCollector",
    "ExchangeInfoCheckpointInvalid",
    "ExchangeInfoRequest",
    "ExchangeInfoSnapshot",
    "ExchangeInfoSnapshotRejected",
    "checkpoint_key",
]

EXCHANGE_INFO_COLLECTOR_ID: Final = "binance.spot.public-exchange-info"
EXCHANGE_INFO_COLLECTOR_VERSION: Final = "1.0.0"
EXCHANGE_INFO_SOURCE: Final = SourceBinding(
    schema_version=PHASE1_PUBLICATION_VERSION,
    source_id=EXCHANGE_INFO_SOURCE_ID,
    version=EXCHANGE_INFO_SOURCE_VERSION,
)

CHECKPOINT_PREFIX: Final = "raw/binance/spot/exchange-info/collections"
SNAPSHOT_CHECKPOINT_KIND: Final = "hlens.binance.spot.exchange-info.snapshot-checkpoint"
#: Versioned **internal** specification of the checkpoint document; not a public contract.
CHECKPOINT_VERSION: Final = "1.0.0"

_REQUEST_ID_RE: Final = re.compile(REQUEST_ID_PATTERN)
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
_STAGE_CHUNK_SIZE: Final = 64 * 1024
_ACCEPTED: Final = "accepted"
_REJECTED: Final = "rejected"


class ExchangeInfoSnapshotRejected(CollectionFailed):
    """The committed answer of this attempt was rejected by the decoder (stable; no revision)."""


class ExchangeInfoCheckpointInvalid(CollectionFailed):
    """A committed checkpoint or body does not reproduce (fail closed)."""


# ======================================================================================
# request / result
# ======================================================================================


@dataclass(frozen=True, slots=True)
class ExchangeInfoRequest:
    """One logical snapshot attempt. Only the defaults are servable in 1.0.0.

    Deliberately not validated on construction: a request outside the allowlist must still be
    constructible so the collector can refuse it (``UnsupportedRequest``) before any I/O.
    """

    request_id: str
    source: SourceBinding = EXCHANGE_INFO_SOURCE
    symbols: tuple[str, ...] = EXCHANGE_INFO_SYMBOLS


@dataclass(frozen=True, slots=True)
class ExchangeInfoSnapshot:
    """One committed, verified snapshot: what was asked, what came back, what it decodes to."""

    request: ExchangeInfoRequest
    query: ExchangeInfoQuery
    origin: str
    request_identity: str
    source_uri: str
    body: ObjectRef
    requested_at: datetime
    retrieved_at: datetime
    http_status: int
    #: Allow-listed response headers, sorted by name.
    http_metadata: tuple[tuple[str, str], ...]
    max_body_bytes: int
    decoded: ExchangeInfoDecoded
    checkpoint_key: str


@dataclass(frozen=True, slots=True)
class _Record:
    """A verified checkpoint, accepted or rejected."""

    snapshot: ExchangeInfoSnapshot | None
    rejection: ExchangeInfoRejection | None


def checkpoint_key(request_id: str) -> str:
    """The one immutable checkpoint object of a logical attempt."""
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    return f"{CHECKPOINT_PREFIX}/{digest}/snapshot.json"


def _chunks(payload: bytes) -> Iterator[bytes]:
    for start in range(0, len(payload), _STAGE_CHUNK_SIZE):
        yield payload[start : start + _STAGE_CHUNK_SIZE]
    if not payload:
        yield b""


def _fingerprint(request: ExchangeInfoRequest) -> dict[str, Any]:
    """Everything a ``request_id`` is bound to; reusing the id for other content fails closed."""
    return {
        "request_id": request.request_id,
        "source_id": request.source.source_id,
        "source_version": request.source.version,
        "symbols": list(request.symbols),
    }


def _symbol_document(item: ExchangeInfoSymbol) -> dict[str, Any]:
    return {
        "symbol": item.symbol,
        "status": item.status,
        "base_asset": item.base_asset,
        "quote_asset": item.quote_asset,
    }


def _document(
    request: ExchangeInfoRequest,
    *,
    query: ExchangeInfoQuery,
    identity: str,
    source_uri: str,
    body: ObjectRef,
    requested_at: datetime,
    retrieved_at: datetime,
    http_status: int,
    metadata: tuple[tuple[str, str], ...],
    max_body_bytes: int,
    outcome: ExchangeInfoDecoded | ExchangeInfoRejection,
) -> dict[str, Any]:
    accepted = None
    rejected = None
    if isinstance(outcome, ExchangeInfoDecoded):
        accepted = {
            "server_time_raw": outcome.server_time_raw,
            "symbols": [_symbol_document(item) for item in outcome.symbols],
            "missing_symbols": list(outcome.missing_symbols),
        }
    else:
        rejected = {"code": outcome.code.value, "detail": outcome.detail}
    return {
        "checkpoint": SNAPSHOT_CHECKPOINT_KIND,
        "checkpoint_version": CHECKPOINT_VERSION,
        "collector": {
            "collector_id": EXCHANGE_INFO_COLLECTOR_ID,
            "version": EXCHANGE_INFO_COLLECTOR_VERSION,
        },
        "request": _fingerprint(request),
        "query": {"symbols": list(query.symbols)},
        "request_identity_sha256": identity,
        "source_uri": source_uri,
        "body": {"key": body.key, "uri": body.uri, "sha256": body.sha256, "size": body.size},
        "requested_at": requested_at.isoformat(),
        "retrieved_at": retrieved_at.isoformat(),
        "http": {"status": http_status, "metadata": dict(metadata)},
        "decoder": {
            "policy_id": EXCHANGE_INFO_DECODER_BINDING.policy_id,
            "version": EXCHANGE_INFO_DECODER_BINDING.version,
            "policy_hash": EXCHANGE_INFO_DECODER_BINDING.policy_hash,
        },
        "decoder_max_body_bytes": max_body_bytes,
        "outcome": _REJECTED if rejected is not None else _ACCEPTED,
        "accepted": accepted,
        "rejected": rejected,
    }


def _rejection_message(rejection: ExchangeInfoRejection) -> str:
    return (
        f"exchangeInfo snapshot rejected by {EXCHANGE_INFO_DECODER_BINDING.policy_id}@"
        f"{EXCHANGE_INFO_DECODER_BINDING.version}: {rejection.code.value}: {rejection.detail}"
    )


# ======================================================================================
# collector
# ======================================================================================


class BinanceSpotExchangeInfoCollector:
    """Synchronous snapshot collector for ``GET /api/v3/exchangeInfo`` (first-slice symbols)."""

    def __init__(
        self,
        storage: StorageAdapter,
        *,
        market_data_base_url: str,
        http_connect_timeout_seconds: float,
        http_read_timeout_seconds: float,
        http_max_retries: int,
        http_user_agent: str,
        min_request_interval_ms: int,
        max_retry_after_seconds: int,
        max_response_bytes: int,
        http_transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        """``http_transport`` is the only HTTP seam (tests use ``httpx.MockTransport``).

        As in D3D-R1 there is no way to hand in an ``httpx.Client``: the accepted wire builds and
        owns its client, with every state that could act after the allowlist pinned off.
        """
        self._storage = storage
        # The accepted D3D wire: origin validation, owned client, retries, bounds, header
        # allowlist, local instants. Only its HTTP GET is used; it never pages or stores here.
        self._wire = d3d.BinanceSpotRestCollector(
            storage,
            market_data_base_url=market_data_base_url,
            http_connect_timeout_seconds=http_connect_timeout_seconds,
            http_read_timeout_seconds=http_read_timeout_seconds,
            http_max_retries=http_max_retries,
            http_user_agent=http_user_agent,
            max_pages_per_collect=d3d.MIN_PAGES_PER_COLLECT,
            min_request_interval_ms=min_request_interval_ms,
            max_retry_after_seconds=max_retry_after_seconds,
            max_response_bytes=max_response_bytes,
            http_transport=http_transport,
            clock=clock,
            monotonic=monotonic,
            sleeper=sleeper,
        )
        self._origin = self._wire.descriptor.network_origins[0]
        self._max_response_bytes = max_response_bytes
        self._descriptor = CollectorDescriptor(
            collector_id=EXCHANGE_INFO_COLLECTOR_ID,
            version=EXCHANGE_INFO_COLLECTOR_VERSION,
            sources=(EXCHANGE_INFO_SOURCE,),
            network_origins=(self._origin,),
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        storage: StorageAdapter,
        *,
        http_transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> Self:
        """Mechanical construction from ``Settings`` (the market-data base and REST bounds)."""
        return cls(
            storage,
            market_data_base_url=str(settings.binance_market_data_base_url),
            http_connect_timeout_seconds=settings.http_connect_timeout_seconds,
            http_read_timeout_seconds=settings.http_read_timeout_seconds,
            http_max_retries=settings.http_max_retries,
            http_user_agent=settings.http_user_agent,
            min_request_interval_ms=settings.binance_rest_min_request_interval_ms,
            max_retry_after_seconds=settings.binance_rest_max_retry_after_seconds,
            max_response_bytes=settings.binance_rest_max_response_bytes,
            http_transport=http_transport,
            clock=clock,
            monotonic=monotonic,
            sleeper=sleeper,
        )

    @property
    def descriptor(self) -> CollectorDescriptor:
        return self._descriptor

    @property
    def origin(self) -> str:
        return self._origin

    def close(self) -> None:
        """Close the client the wire owns; idempotent."""
        self._wire.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ---------------------------------------------------------------- entry points

    def collect(self, request: ExchangeInfoRequest) -> ExchangeInfoSnapshot:
        """The committed snapshot of ``request``: replayed if committed, else fetched once."""
        query = self._validate_request(request)
        replayed = self.replay(request)
        if replayed is not None:
            return replayed
        url = self._request_url(query)
        identity = self._identity(query)
        requested_at, retrieved_at, metadata, body = self._wire._http_get(url)
        body_sha256 = hashlib.sha256(body).hexdigest()
        try:
            body_key = response_object_key(body_sha256, identity)
        except ExchangeInfoIdentityViolation as exc:  # pragma: no cover - validated inputs
            raise CollectionFailed(f"cannot address the response body: {exc}") from None
        try:
            body_ref = self._publish(body_key, body)
        except ObjectConflict as exc:  # pragma: no cover - content addressed
            raise CollectionFailed(f"the response body key is taken: {exc}") from None
        outcome = self._decode(body, self._max_response_bytes)
        document = _document(
            request,
            query=query,
            identity=identity,
            source_uri=url,
            body=body_ref,
            requested_at=requested_at,
            retrieved_at=retrieved_at,
            http_status=200,
            metadata=tuple(sorted(metadata.items())),
            max_body_bytes=self._max_response_bytes,
            outcome=outcome,
        )
        try:
            self._publish(checkpoint_key(request.request_id), canonical_json(document).encode())
        except ObjectConflict:
            pass  # another writer committed this attempt first: its checkpoint wins
        committed = self.replay(request)
        if committed is None:  # pragma: no cover - the key exists after a publish
            raise CollectionFailed("the committed snapshot checkpoint vanished")
        return committed

    def replay(self, request: ExchangeInfoRequest) -> ExchangeInfoSnapshot | None:
        """The committed snapshot of ``request`` (never the network), or ``None`` if none.

        A committed rejection raises ``ExchangeInfoSnapshotRejected``; a checkpoint or body that
        does not reproduce raises ``ExchangeInfoCheckpointInvalid``.
        """
        self._validate_request(request)
        key = checkpoint_key(request.request_id)
        try:
            payload = self._read(key)
            if payload is None:
                return None
            record = self._verify(key, payload, request)
        except d3d._CheckpointInvalid as exc:
            raise ExchangeInfoCheckpointInvalid(
                f"committed checkpoint does not reproduce: {exc}"
            ) from exc
        except StorageError as exc:
            raise ExchangeInfoCheckpointInvalid(f"storage failure: {exc}") from exc
        if record.rejection is not None:
            raise ExchangeInfoSnapshotRejected(_rejection_message(record.rejection))
        assert record.snapshot is not None
        return record.snapshot

    # ---------------------------------------------------------------- request gate

    def _validate_request(self, request: object) -> ExchangeInfoQuery:
        """Everything checkable before the first byte leaves this process."""
        if not isinstance(request, ExchangeInfoRequest):
            raise UnsupportedRequest("request must be an ExchangeInfoRequest")
        if not isinstance(request.request_id, str) or not _REQUEST_ID_RE.fullmatch(
            request.request_id
        ):
            raise UnsupportedRequest("request_id is not a valid request identity")
        if request.source != EXCHANGE_INFO_SOURCE:
            raise UnsupportedRequest(
                f"unsupported source {request.source.source_id}@{request.source.version}"
            )
        try:
            return ExchangeInfoQuery(request.symbols)
        except ExchangeInfoIdentityViolation as exc:
            raise UnsupportedRequest(f"unsupported symbols: {exc}") from None

    def _identity(self, query: ExchangeInfoQuery) -> str:
        try:
            return request_identity_sha256(query, self._origin)
        except ExchangeInfoIdentityViolation as exc:  # pragma: no cover - origin validated
            raise CollectionFailed(f"cannot derive the request identity: {exc}") from None

    def _request_url(self, query: ExchangeInfoQuery) -> str:
        """Build the URL from the structured query and re-check it structurally.

        Callers can never hand in a URL or a parameter mapping: the only input is an
        ``ExchangeInfoQuery``, whose construction is itself the allowlist. This second,
        independent pass fails **before** any socket is opened if anything upstream is wrong.
        """
        try:
            url = request_source_uri(query, self._origin)
        except ExchangeInfoIdentityViolation as exc:  # pragma: no cover - origin validated
            raise CollectionFailed(f"cannot build the request URL: {exc}") from None
        if not _uri.is_visible_ascii(url):
            raise CollectionFailed("refusing a non visible-ASCII request URL")
        parts = _uri.split(url)
        if parts.scheme != "https" or parts.authority is None:
            raise CollectionFailed("refusing a non-HTTPS request URL")
        if not _uri.is_host_port(parts.authority) or f"https://{parts.authority}" != self._origin:
            raise CollectionFailed("refusing a request URL outside the market-data origin")
        if parts.path != EXCHANGE_INFO_PATH:
            raise CollectionFailed("refusing a request URL outside the frozen endpoint")
        if parts.fragment is not None:
            raise CollectionFailed("refusing a request URL with a fragment")
        if parts.query is None or parts.query != query.query_string():
            raise CollectionFailed("refusing a request URL whose query is not the canonical one")
        try:
            parsed = ExchangeInfoQuery.from_query_string(parts.query)
        except ExchangeInfoIdentityViolation as exc:
            raise CollectionFailed(f"refusing a request URL query: {exc}") from None
        if parsed != query:  # pragma: no cover - the canonical query is the only parse
            raise CollectionFailed("refusing a request URL that does not round-trip")
        return url

    @staticmethod
    def _decode(body: bytes, max_body_bytes: int) -> ExchangeInfoDecoded | ExchangeInfoRejection:
        try:
            return decode_exchange_info(
                body, requested_symbols=EXCHANGE_INFO_SYMBOLS, max_body_bytes=max_body_bytes
            )
        except ExchangeInfoDecodeRequestError as exc:  # pragma: no cover - bounded by the wire
            raise CollectionFailed(f"the snapshot cannot be decoded as requested: {exc}") from None

    # ---------------------------------------------------------------- storage

    def _publish(self, key: str, payload: bytes) -> ObjectRef:
        digest = hashlib.sha256(payload).hexdigest()
        try:
            staged = self._storage.stage(
                StageRequest(key=key, expected_sha256=digest, expected_size=len(payload)),
                _chunks(payload),
            )
            return self._storage.publish(staged).ref
        except ObjectConflict:
            raise
        except StorageError as exc:
            raise CollectionFailed(f"cannot publish {key!r}: {exc}") from exc

    def _read(self, key: str) -> bytes | None:
        ref = self._storage.lookup(key)
        if ref is None:
            return None
        with self._storage.open_read(ref) as handle:
            data = handle.read()
        if hashlib.sha256(data).hexdigest() != ref.sha256 or len(data) != ref.size:
            raise d3d._CheckpointInvalid(f"{key!r} does not match its published reference")
        return data

    # ---------------------------------------------------------------- checkpoint recovery

    def _verify(self, key: str, payload: bytes, request: ExchangeInfoRequest) -> _Record:
        """Re-read, re-verify and **re-decode** a committed checkpoint; any drift fails closed."""
        document = _parse_checkpoint(payload)
        if d3d._mapping_field(document, "request") != _fingerprint(request):
            raise d3d._CheckpointInvalid(
                "this request_id is already committed with different request content"
            )
        query_document = d3d._mapping_field(document, "query")
        symbols = d3d._list_field(query_document, "symbols")
        try:
            query = ExchangeInfoQuery(tuple(symbols))
        except ExchangeInfoIdentityViolation as exc:
            raise d3d._CheckpointInvalid(f"{key!r} carries another query: {exc}") from None
        identity = self._identity(query)
        if d3d._str_field(document, "request_identity_sha256") != identity:
            raise d3d._CheckpointInvalid(f"{key!r} does not carry the canonical request identity")
        source_uri = d3d._str_field(document, "source_uri")
        if source_uri != self._request_url(query):
            raise d3d._CheckpointInvalid(f"{key!r} does not carry this request's URI")

        body_document = d3d._mapping_field(document, "body")
        body_ref = ObjectRef(
            key=d3d._str_field(body_document, "key"),
            uri=d3d._str_field(body_document, "uri"),
            sha256=d3d._str_field(body_document, "sha256"),
            size=d3d._int_field(body_document, "size"),
        )
        if _SHA256_RE.fullmatch(body_ref.sha256) is None:
            raise d3d._CheckpointInvalid(f"{key!r} carries a non-canonical body sha256")
        if body_ref.key != response_object_key(body_ref.sha256, identity):
            raise d3d._CheckpointInvalid(
                f"{key!r} carries a body key that is not content-addressed"
            )
        published = self._storage.lookup(body_ref.key)
        if published is None:
            raise d3d._CheckpointInvalid(f"{key!r} references a body that is not published")
        if published != body_ref:
            raise d3d._CheckpointInvalid(f"{key!r} references a body whose reference has drifted")
        with self._storage.open_read(published) as handle:
            body = handle.read()
        if hashlib.sha256(body).hexdigest() != body_ref.sha256 or len(body) != body_ref.size:
            raise d3d._CheckpointInvalid(f"{key!r} references a body that does not match its hash")

        requested_at = d3d._utc_field(document, "requested_at")
        retrieved_at = d3d._utc_field(document, "retrieved_at")
        if retrieved_at <= requested_at:
            raise d3d._CheckpointInvalid(f"{key!r} records a non-advancing clock")
        http = d3d._mapping_field(document, "http")
        status = d3d._int_field(http, "status")
        if status != 200:
            raise d3d._CheckpointInvalid(f"{key!r} records a non-200 answer")
        metadata = d3d.BinanceSpotRestCollector._verified_metadata(
            d3d._mapping_field(http, "metadata"), key
        )
        decoder = d3d._mapping_field(document, "decoder")
        if (
            d3d._str_field(decoder, "policy_id") != EXCHANGE_INFO_DECODER_BINDING.policy_id
            or d3d._str_field(decoder, "version") != EXCHANGE_INFO_DECODER_BINDING.version
            or d3d._str_field(decoder, "policy_hash") != EXCHANGE_INFO_DECODER_BINDING.policy_hash
        ):
            raise d3d._CheckpointInvalid(f"{key!r} was decoded by another decoder binding")
        max_body_bytes = d3d._int_field(document, "decoder_max_body_bytes")
        if not MIN_BODY_LIMIT_BYTES <= max_body_bytes <= MAX_BODY_LIMIT_BYTES:
            raise d3d._CheckpointInvalid(f"{key!r} carries an out-of-range body limit")
        try:
            outcome = decode_exchange_info(
                body, requested_symbols=query.symbols, max_body_bytes=max_body_bytes
            )
        except ExchangeInfoDecodeRequestError as exc:  # pragma: no cover - range checked above
            raise d3d._CheckpointInvalid(f"{key!r} does not re-decode: {exc}") from None
        pairs = tuple(sorted(metadata.items()))
        rebuilt = _document(
            request,
            query=query,
            identity=identity,
            source_uri=source_uri,
            body=body_ref,
            requested_at=requested_at,
            retrieved_at=retrieved_at,
            http_status=status,
            metadata=pairs,
            max_body_bytes=max_body_bytes,
            outcome=outcome,
        )
        if canonical_json(rebuilt).encode("utf-8") != payload:
            raise d3d._CheckpointInvalid(f"{key!r} does not reproduce field for field")
        if isinstance(outcome, ExchangeInfoRejection):
            return _Record(snapshot=None, rejection=outcome)
        return _Record(
            snapshot=ExchangeInfoSnapshot(
                request=request,
                query=query,
                origin=self._origin,
                request_identity=identity,
                source_uri=source_uri,
                body=body_ref,
                requested_at=requested_at,
                retrieved_at=retrieved_at,
                http_status=status,
                http_metadata=pairs,
                max_body_bytes=max_body_bytes,
                decoded=outcome,
                checkpoint_key=key,
            ),
            rejection=None,
        )


def _parse_checkpoint(payload: bytes) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise d3d._CheckpointInvalid("checkpoint bytes are not UTF-8") from exc
    try:
        document, end = d3d._CHECKPOINT_DECODER.raw_decode(text)
    except ValueError as exc:
        raise d3d._CheckpointInvalid(f"checkpoint is not JSON: {exc}") from None
    if end != len(text) or not isinstance(document, dict):
        raise d3d._CheckpointInvalid("checkpoint must be exactly one JSON object")
    if d3d._str_field(document, "checkpoint") != SNAPSHOT_CHECKPOINT_KIND:
        raise d3d._CheckpointInvalid("checkpoint kind does not match")
    if d3d._str_field(document, "checkpoint_version") != CHECKPOINT_VERSION:
        raise d3d._CheckpointInvalid("checkpoint version is not supported")
    collector: Mapping[str, Any] = d3d._mapping_field(document, "collector")
    if (
        d3d._str_field(collector, "collector_id") != EXCHANGE_INFO_COLLECTOR_ID
        or d3d._str_field(collector, "version") != EXCHANGE_INFO_COLLECTOR_VERSION
    ):
        raise d3d._CheckpointInvalid("checkpoint was written by another collector")
    return document

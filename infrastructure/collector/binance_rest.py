"""Replayable Binance public spot REST collector (Phase 1 D3D; ADR-0027 §5 ~ §9, §12).

``binance.spot.public-rest@1.0.0`` serves exactly one source binding,
``binance.public.spot.rest@1.0.0``, and reaches exactly two market-data-only endpoints of the
configured origin: ``GET /api/v3/aggTrades`` and ``GET /api/v3/klines`` with ``interval=1m``.
There is no account, order or signed endpoint here, no API key is read from anywhere, and no
redirect is ever followed.

What this module owns:

- **the wire** — structured endpoint allow-listing, bounded retries, ``Retry-After``, a minimum
  spacing between requests, a streaming body bound and the two local wall-clock instants
  (``requested_at`` before the request reaches the transport, ``retrieved_at`` after the final
  entity byte);
- **the commitments** — the first answer to a page is published as an immutable,
  content-addressed object and then as an immutable page checkpoint; a finished logical attempt
  is published as an immutable collection checkpoint. Every commitment goes through
  ``StorageAdapter.publish``: write-once, idempotent on identical content, ``ObjectConflict`` on
  different content. There is no mutable sidecar and no overwrite;
- **the accounting** — objects and a single honest tail gap that together cover the requested
  window exactly, derived deterministically from the committed checkpoints.

What this module deliberately does **not** own: the page envelope and the pagination cursors
(``binance.spot.rest.decoder@1.0.0``, D3C), page / revision identity
(``hlens.binance.spot.rest-revision-identity@1.0.0``, D3B), and every Iceberg write, revision,
lineage or precedence edge (D3E). It never re-implements a second continuation or stop rule: the
next query is always the previous page summary's exact ``next_query``.

Replay (ADR-0027 §5 / §9): the same ``request_id`` re-collected — on the same instance, on a
rebuilt instance, or after a crash — returns the first committed objects and gaps, or replays the
same stable failure, **without touching the network**. Only pages that were never committed are
fetched. A ``request_id`` reused for different request content fails closed before any request.

Honest failure (ADR-0027 §7): a page budget exhaustion, 429, 418, 5xx, transport failure, a
truncated or oversized body, a 4xx or a decoder rejection all raise ``CollectionFailed``. None of
them is ever laundered into a ``SOURCE_ABSENT`` gap or into a successful result. ``SOURCE_ABSENT``
is only the tail after a short, empty or unclosed-tail page, and its detail states what the exact
query was answered at ``retrieved_at`` — never that the market was idle.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any, Final, Self

import httpx

from core.contracts import _uri
from core.contracts.collector import (
    CollectedObject,
    CollectionFailed,
    CollectionRequest,
    CollectionResult,
    CollectorDescriptor,
    CoverageGap,
    GapReason,
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
from core.domain.base import FrozenMapping, canonical_json
from infrastructure.parser.binance_rest import (
    DECODER_BINDING,
    MAX_BODY_LIMIT_BYTES,
    MIN_BODY_LIMIT_BYTES,
    AnsweredInterval,
    RestDecodeRequestError,
    RestPageDecoded,
    RestPageDecodeRequest,
    RestPageRejection,
    RestPageSummary,
    RestQualityFact,
    decode_rest_page,
)
from infrastructure.revision.rest_identity import (
    DATA_TYPES,
    RestIdentityViolation,
    RestPageQuery,
    page_identity_sha256,
    page_source_uri,
    response_object_key,
)
from infrastructure.settings import Settings

__all__ = [
    "ALLOWED_RESPONSE_HEADERS",
    "CHECKPOINT_PREFIX",
    "CHECKPOINT_VERSION",
    "COLLECTION_CHECKPOINT_KIND",
    "MAX_PAGES_PER_COLLECT",
    "MAX_REQUEST_INTERVAL_MS",
    "MAX_RESPONSE_BYTES",
    "MAX_RETRY_AFTER_SECONDS",
    "MIN_PAGES_PER_COLLECT",
    "MIN_REQUEST_INTERVAL_MS",
    "MIN_RESPONSE_BYTES",
    "MIN_RETRY_AFTER_SECONDS",
    "PAGE_CHECKPOINT_KIND",
    "REST_COLLECTOR_ID",
    "REST_COLLECTOR_VERSION",
    "REST_SOURCE",
    "SUPPORTED_DATA_TYPES",
    "SUPPORTED_SYMBOLS",
    "BinanceSpotRestCollector",
]

REST_COLLECTOR_ID: Final[str] = "binance.spot.public-rest"
REST_COLLECTOR_VERSION: Final[str] = "1.0.0"
REST_SOURCE: Final[SourceBinding] = SourceBinding(
    source_id="binance.public.spot.rest",
    version="1.0.0",
)
#: ADR-0022 §1 first slice; the identity rule would accept any venue-native upper-case symbol.
SUPPORTED_SYMBOLS: Final[frozenset[str]] = frozenset({"BTCUSDT", "ETHUSDT"})
SUPPORTED_DATA_TYPES: Final[frozenset[str]] = frozenset(DATA_TYPES)

# --------------------------------------------------------------------------- frozen setting bounds
#: ``HLENS_BINANCE_REST_MAX_PAGES_PER_COLLECT`` (ADR-0027 §12 / 03-data.md §6.2).
MIN_PAGES_PER_COLLECT: Final = 1
MAX_PAGES_PER_COLLECT: Final = 5_000
#: ``HLENS_BINANCE_REST_MIN_REQUEST_INTERVAL_MS``.
MIN_REQUEST_INTERVAL_MS: Final = 50
MAX_REQUEST_INTERVAL_MS: Final = 60_000
#: ``HLENS_BINANCE_REST_MAX_RETRY_AFTER_SECONDS``.
MIN_RETRY_AFTER_SECONDS: Final = 1
MAX_RETRY_AFTER_SECONDS: Final = 3_600
#: ``HLENS_BINANCE_REST_MAX_RESPONSE_BYTES``; the decoder fixes the same range as its argument.
MIN_RESPONSE_BYTES: Final = MIN_BODY_LIMIT_BYTES
MAX_RESPONSE_BYTES: Final = MAX_BODY_LIMIT_BYTES

# --------------------------------------------------------------------------- checkpoint layout
CHECKPOINT_PREFIX: Final = "raw/binance/spot/rest/collections"
PAGE_CHECKPOINT_KIND: Final = "hlens.binance.spot.rest.page-checkpoint"
COLLECTION_CHECKPOINT_KIND: Final = "hlens.binance.spot.rest.collection-checkpoint"
#: Versioned **internal** specification of the two checkpoint documents; not a public contract.
CHECKPOINT_VERSION: Final = "1.0.0"

#: The only response headers ever persisted or echoed. Everything else is dropped, and any
#: credential-shaped header name rejects the whole page (ADR-0027 safety boundary).
ALLOWED_RESPONSE_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "content-type",
        "content-length",
        "content-encoding",
        "date",
        "etag",
        "last-modified",
        "server",
        "x-mbx-used-weight",
        "x-mbx-used-weight-1m",
    }
)
#: Credential-shaped name fragments; the same list the collector contract applies to source
#: metadata keys. A response carrying any of them is refused rather than partially recorded.
_CREDENTIAL_NAME_PARTS: Final[tuple[str, ...]] = (
    "auth",
    "cookie",
    "credential",
    "key",
    "password",
    "secret",
    "signature",
    "token",
)

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND: Final = timedelta(milliseconds=1)
_STREAM_CHUNK_SIZE: Final = 64 * 1024
_STAGE_CHUNK_SIZE: Final = 64 * 1024
#: ``Retry-After`` as pure decimal seconds (RFC 9110 delay-seconds). A date form never matches.
_DELAY_SECONDS_RE: Final = re.compile(r"[0-9]{1,10}")
_CONTENT_LENGTH_RE: Final = re.compile(r"[0-9]{1,19}")
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
_RETRYABLE_TRANSPORT: Final = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


class _CheckpointInvalid(Exception):
    """A committed checkpoint does not reproduce; never surfaces, always becomes a failure."""


# ======================================================================================
# small helpers
# ======================================================================================


def _iso(value: datetime) -> str:
    return value.isoformat()


def _at_ms(epoch_ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=epoch_ms)


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _chunks(payload: bytes) -> Iterator[bytes]:
    for start in range(0, len(payload), _STAGE_CHUNK_SIZE):
        yield payload[start : start + _STAGE_CHUNK_SIZE]
    if not payload:
        yield b""


def _has_credential_shape(name: str) -> bool:
    lowered = name.lower()
    return any(part in lowered for part in _CREDENTIAL_NAME_PARTS)


def _transport_failure_message(exc: BaseException) -> str:
    """Never echo a URL or header: the message is the exception class alone."""
    return f"HTTP transport failure: {type(exc).__name__}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names):
        raise _CheckpointInvalid("a checkpoint object repeats a key")
    return dict(pairs)


_CHECKPOINT_DECODER: Final = json.JSONDecoder(object_pairs_hook=_no_duplicate_keys)


def _in_range(value: object, low: int, high: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an int")
    if not low <= value <= high:
        raise ValueError(f"{label} must be in [{low}, {high}]")
    return value


def _validate_market_data_origin(base_url: object) -> str:
    """Exactly ``https://host[:port]``; nothing is trimmed, lower-cased or otherwise rewritten.

    A path (including ``/api``), a query, a fragment, credentials, an out-of-range or
    zero-padded port, an upper-case host and any percent escape are refused. The single
    concession — identical to the accepted D0-R2 archive base rule — is that a bare ``/`` path is
    the origin's root rather than a path segment, because ``AnyHttpUrl`` renders the frozen
    default that way.
    """
    if not isinstance(base_url, str) or not base_url:
        raise ValueError("market-data base URL must not be blank")
    if base_url != base_url.strip():
        raise ValueError("market-data base URL must not have leading or trailing whitespace")
    if not _uri.is_visible_ascii(base_url):
        raise ValueError("market-data base URL must be visible ASCII without whitespace")
    parts = _uri.split(base_url)
    if parts.scheme != "https" or parts.authority is None:
        raise ValueError("market-data base URL must be https://host[:port]")
    if not _uri.is_host_port(parts.authority):
        raise ValueError(
            "market-data base URL authority must be a lower-case host with an optional "
            "port 1~65535 and no credentials"
        )
    if parts.query is not None:
        raise ValueError("market-data base URL must not carry a query")
    if parts.fragment is not None:
        raise ValueError("market-data base URL must not carry a fragment")
    if parts.path not in {"", "/"}:
        raise ValueError("market-data base URL must be an origin without a path")
    return f"https://{parts.authority}"


def _owned_client(
    transport: httpx.BaseTransport | None, timeout: httpx.Timeout, user_agent: str
) -> httpx.Client:
    """The only client this module ever sends through (D3D-R1).

    Every state that could act after the endpoint allowlist is pinned off: no auth, no request /
    response hooks, no redirects, no environment trust (so neither ``NETRC`` credentials nor
    ``*_PROXY`` rerouting apply), and a cookie jar whose policy refuses every cookie, so a
    ``Set-Cookie`` answer can never be replayed on a later request.
    """
    return httpx.Client(
        transport=transport,
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": user_agent},
        auth=None,
        cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
        event_hooks={"request": [], "response": []},
        trust_env=False,
    )


def _epoch_ms(value: datetime, label: str) -> int:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise UnsupportedRequest(f"{label} must be UTC")
    delta = value - _EPOCH
    if delta < timedelta(0):
        raise UnsupportedRequest(f"{label} must not precede the UNIX epoch")
    if delta % _MILLISECOND:
        raise UnsupportedRequest(f"{label} must fall on a whole millisecond")
    return delta // _MILLISECOND


# ======================================================================================
# checkpoint documents
# ======================================================================================


def _request_fingerprint(request: CollectionRequest) -> dict[str, Any]:
    """Everything a ``request_id`` is bound to; reusing the id for other content fails closed."""
    return {
        "request_id": request.request_id,
        "source_id": request.source.source_id,
        "source_version": request.source.version,
        "data_type": request.data_type,
        "symbols": list(request.symbols),
        "coverage_start": _iso(request.coverage_start),
        "coverage_end": _iso(request.coverage_end),
    }


def _query_document(query: RestPageQuery) -> dict[str, Any]:
    return {
        "data_type": query.data_type,
        "symbol": query.symbol,
        "from_id": query.from_id,
        "start_time_ms": query.start_time_ms,
        "limit": query.limit,
        "pairs": [[name, value] for name, value in query.pairs()],
    }


def _fact_document(fact: RestQualityFact) -> dict[str, Any]:
    return {
        "fact_type": fact.fact_type.value,
        "data_type": fact.data_type,
        "symbol": fact.symbol,
        "page_index": fact.page_index,
        "unit": fact.unit.value,
        "range_start": fact.range_start,
        "range_end": fact.range_end,
        "occurrences": fact.occurrences,
        "missing": fact.missing,
        "detail": fact.detail,
    }


def _summary_document(summary: RestPageSummary) -> dict[str, Any]:
    return {
        "element_count": summary.element_count,
        "served_count": summary.served_count,
        "answered": {"start_ms": summary.answered.start_ms, "end_ms": summary.answered.end_ms},
        "stop_reason": None if summary.stop_reason is None else summary.stop_reason.value,
        "next_query": None if summary.next_query is None else _query_document(summary.next_query),
        "quality_facts": [_fact_document(fact) for fact in summary.quality_facts],
        "last_agg_trade_id": summary.last_agg_trade_id,
        "last_event_time_ms": summary.last_event_time_ms,
        "last_trade_id": summary.last_trade_id,
        "last_closed_open_ms": summary.last_closed_open_ms,
    }


def _rejection_document(rejection: RestPageRejection) -> dict[str, Any]:
    return {
        "code": rejection.code.value,
        "detail": rejection.detail,
        "element_index": rejection.element_index,
        "field_name": rejection.field_name,
        "body_size_bytes": rejection.body_size_bytes,
    }


@dataclass(frozen=True, slots=True)
class _PageRecord:
    """One committed page: what was asked, what came back and what the decoder made of it."""

    symbol: str
    page_index: int
    query: RestPageQuery
    page_identity: str
    source_uri: str
    body: ObjectRef
    requested_at: datetime
    retrieved_at: datetime
    http_status: int
    http_metadata: dict[str, str]
    max_body_bytes: int
    outcome: RestPageDecoded | RestPageRejection
    checkpoint_key: str

    @property
    def summary(self) -> RestPageSummary:
        if isinstance(self.outcome, RestPageRejection):
            raise _CheckpointInvalid("a rejected page has no summary")
        return self.outcome.summary

    @property
    def rejection(self) -> RestPageRejection | None:
        return self.outcome if isinstance(self.outcome, RestPageRejection) else None


def _page_document(fingerprint: dict[str, Any], record: _PageRecord) -> dict[str, Any]:
    accepted = None if record.rejection is not None else _summary_document(record.summary)
    rejected = None if record.rejection is None else _rejection_document(record.rejection)
    return {
        "checkpoint": PAGE_CHECKPOINT_KIND,
        "checkpoint_version": CHECKPOINT_VERSION,
        "collector": {"collector_id": REST_COLLECTOR_ID, "version": REST_COLLECTOR_VERSION},
        "request": fingerprint,
        "symbol": record.symbol,
        "page_index": record.page_index,
        "page_identity_sha256": record.page_identity,
        "query": _query_document(record.query),
        "source_uri": record.source_uri,
        "body": {
            "key": record.body.key,
            "uri": record.body.uri,
            "sha256": record.body.sha256,
            "size": record.body.size,
        },
        "requested_at": _iso(record.requested_at),
        "retrieved_at": _iso(record.retrieved_at),
        "http": {"status": record.http_status, "metadata": dict(record.http_metadata)},
        "decoder": {
            "policy_id": DECODER_BINDING.policy_id,
            "version": DECODER_BINDING.version,
            "policy_hash": DECODER_BINDING.policy_hash,
        },
        "decoder_max_body_bytes": record.max_body_bytes,
        "outcome": "rejected" if record.rejection is not None else "accepted",
        "accepted": accepted,
        "rejected": rejected,
    }


def _object_document(item: CollectedObject) -> dict[str, Any]:
    return {
        "key": item.ref.key,
        "uri": item.ref.uri,
        "sha256": item.ref.sha256,
        "size": item.ref.size,
        "symbol": item.symbol,
        "coverage_start": _iso(item.coverage_start),
        "coverage_end": _iso(item.coverage_end),
        "source_uri": item.source_uri,
        "retrieved_at": _iso(item.retrieved_at),
        "source_metadata": dict(item.source_metadata),
    }


def _gap_document(gap: CoverageGap) -> dict[str, Any]:
    return {
        "symbol": gap.symbol,
        "coverage_start": _iso(gap.coverage_start),
        "coverage_end": _iso(gap.coverage_end),
        "reason": gap.reason.value,
        "detail": gap.detail,
    }


def _chain_documents(chains: list[tuple[str, list[_PageRecord]]]) -> list[dict[str, Any]]:
    return [
        {
            "symbol": symbol,
            "page_count": len(pages),
            "page_checkpoint_keys": [page.checkpoint_key for page in pages],
        }
        for symbol, pages in chains
    ]


def _collection_document(
    fingerprint: dict[str, Any],
    chains: list[tuple[str, list[_PageRecord]]],
    *,
    result: CollectionResult | None,
    failure: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "checkpoint": COLLECTION_CHECKPOINT_KIND,
        "checkpoint_version": CHECKPOINT_VERSION,
        "collector": {"collector_id": REST_COLLECTOR_ID, "version": REST_COLLECTOR_VERSION},
        "request": fingerprint,
        "chains": _chain_documents(chains),
        "outcome": "failed" if failure is not None else "succeeded",
        "result": (
            None
            if result is None
            else {
                "objects": [_object_document(item) for item in result.objects],
                "gaps": [_gap_document(gap) for gap in result.gaps],
            }
        ),
        "failure": failure,
    }


def _failure_document(symbol: str, page: _PageRecord) -> dict[str, Any]:
    rejection = page.rejection
    if rejection is None:  # pragma: no cover - only built for a rejected page
        raise _CheckpointInvalid("a stable failure needs a rejected page")
    return {
        "classification": "decoder_rejected",
        "symbol": symbol,
        "page_index": page.page_index,
        "page_checkpoint_key": page.checkpoint_key,
        "code": rejection.code.value,
        "detail": rejection.detail,
    }


def _failure_message(failure: dict[str, Any]) -> str:
    return (
        f"rest page rejected by {DECODER_BINDING.policy_id}@{DECODER_BINDING.version}: "
        f"{failure['symbol']} page {failure['page_index']} {failure['code']}: {failure['detail']}"
    )


# ======================================================================================
# checkpoint readers (strict, fail closed)
# ======================================================================================


def _field(document: Mapping[str, Any], name: str) -> Any:
    if name not in document:
        raise _CheckpointInvalid(f"checkpoint field {name!r} is missing")
    return document[name]


def _str_field(document: Mapping[str, Any], name: str) -> str:
    value = _field(document, name)
    if not isinstance(value, str):
        raise _CheckpointInvalid(f"checkpoint field {name!r} must be a string")
    return value


def _int_field(document: Mapping[str, Any], name: str) -> int:
    value = _field(document, name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _CheckpointInvalid(f"checkpoint field {name!r} must be an int")
    return value


def _optional_int_field(document: Mapping[str, Any], name: str) -> int | None:
    value = _field(document, name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise _CheckpointInvalid(f"checkpoint field {name!r} must be an int or null")
    return value


def _mapping_field(document: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = _field(document, name)
    if not isinstance(value, dict):
        raise _CheckpointInvalid(f"checkpoint field {name!r} must be an object")
    return value


def _list_field(document: Mapping[str, Any], name: str) -> list[Any]:
    value = _field(document, name)
    if not isinstance(value, list):
        raise _CheckpointInvalid(f"checkpoint field {name!r} must be an array")
    return value


def _utc_field(document: Mapping[str, Any], name: str) -> datetime:
    text = _str_field(document, name)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _CheckpointInvalid(f"checkpoint field {name!r} is not an ISO instant") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise _CheckpointInvalid(f"checkpoint field {name!r} must be UTC")
    if _iso(parsed) != text:
        raise _CheckpointInvalid(f"checkpoint field {name!r} is not canonically encoded")
    return parsed


def _parse_query(document: Mapping[str, Any]) -> RestPageQuery:
    try:
        query = RestPageQuery(
            data_type=_str_field(document, "data_type"),
            symbol=_str_field(document, "symbol"),
            from_id=_optional_int_field(document, "from_id"),
            start_time_ms=_optional_int_field(document, "start_time_ms"),
            limit=_int_field(document, "limit"),
        )
    except RestIdentityViolation as exc:
        raise _CheckpointInvalid(f"checkpoint query is not constructible: {exc}") from None
    if _query_document(query) != dict(document):
        raise _CheckpointInvalid("checkpoint query is not in its canonical encoding")
    return query


def _parse_checkpoint(payload: bytes, kind: str) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _CheckpointInvalid("checkpoint bytes are not UTF-8") from exc
    try:
        document, end = _CHECKPOINT_DECODER.raw_decode(text)
    except json.JSONDecodeError as exc:
        raise _CheckpointInvalid(f"checkpoint is not JSON: {exc.msg}") from None
    if end != len(text):
        raise _CheckpointInvalid("checkpoint carries trailing content")
    if not isinstance(document, dict):
        raise _CheckpointInvalid("checkpoint must be a JSON object")
    if _str_field(document, "checkpoint") != kind:
        raise _CheckpointInvalid("checkpoint kind does not match")
    if _str_field(document, "checkpoint_version") != CHECKPOINT_VERSION:
        raise _CheckpointInvalid("checkpoint version is not supported")
    collector = _mapping_field(document, "collector")
    if (
        _str_field(collector, "collector_id") != REST_COLLECTOR_ID
        or _str_field(collector, "version") != REST_COLLECTOR_VERSION
    ):
        raise _CheckpointInvalid("checkpoint was written by another collector")
    return document


# ======================================================================================
# collector
# ======================================================================================


class _Budget:
    """New (first-time HTTP) pages this ``collect`` may still fetch; replays never draw on it."""

    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


class BinanceSpotRestCollector:
    """Synchronous ``CollectorAdapter`` for the Binance public spot market-data REST endpoints."""

    def __init__(
        self,
        storage: StorageAdapter,
        *,
        market_data_base_url: str,
        http_connect_timeout_seconds: float,
        http_read_timeout_seconds: float,
        http_max_retries: int,
        http_user_agent: str,
        max_pages_per_collect: int,
        min_request_interval_ms: int,
        max_retry_after_seconds: int,
        max_response_bytes: int,
        http_transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        """``http_transport`` is the only HTTP seam (tests use ``httpx.MockTransport``).

        D3D-R1: there is deliberately no way to hand in an ``httpx.Client``. A caller's client
        carries default headers, cookies, auth and request event hooks that act *after* the
        endpoint allowlist, so it could add credentials or rewrite a validated request to another
        origin or an account path. The collector always builds and owns its own client.
        """
        if http_connect_timeout_seconds <= 0 or http_read_timeout_seconds <= 0:
            raise ValueError("HTTP timeouts must be positive")
        if http_max_retries < 0:
            raise ValueError("http_max_retries must be >= 0")
        if not http_user_agent.strip():
            raise ValueError("http_user_agent must not be blank")

        self._storage = storage
        self._origin = _validate_market_data_origin(market_data_base_url)
        self._max_retries = http_max_retries
        self._max_pages = _in_range(
            max_pages_per_collect,
            MIN_PAGES_PER_COLLECT,
            MAX_PAGES_PER_COLLECT,
            "max_pages_per_collect",
        )
        self._min_interval_seconds = (
            _in_range(
                min_request_interval_ms,
                MIN_REQUEST_INTERVAL_MS,
                MAX_REQUEST_INTERVAL_MS,
                "min_request_interval_ms",
            )
            / 1000
        )
        self._max_retry_after = _in_range(
            max_retry_after_seconds,
            MIN_RETRY_AFTER_SECONDS,
            MAX_RETRY_AFTER_SECONDS,
            "max_retry_after_seconds",
        )
        self._max_response_bytes = _in_range(
            max_response_bytes, MIN_RESPONSE_BYTES, MAX_RESPONSE_BYTES, "max_response_bytes"
        )
        self._timeout = httpx.Timeout(
            connect=http_connect_timeout_seconds,
            read=http_read_timeout_seconds,
            write=http_read_timeout_seconds,
            pool=http_connect_timeout_seconds,
        )
        self._user_agent = http_user_agent.strip()
        self._clock = clock or _utc_now
        self._monotonic = monotonic or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._last_send_monotonic: float | None = None
        self._client = _owned_client(http_transport, self._timeout, self._user_agent)
        self._descriptor = CollectorDescriptor(
            collector_id=REST_COLLECTOR_ID,
            version=REST_COLLECTOR_VERSION,
            sources=(REST_SOURCE,),
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
        """Mechanical construction from ``Settings``; the archive base is never read here."""
        return cls(
            storage,
            market_data_base_url=str(settings.binance_market_data_base_url),
            http_connect_timeout_seconds=settings.http_connect_timeout_seconds,
            http_read_timeout_seconds=settings.http_read_timeout_seconds,
            http_max_retries=settings.http_max_retries,
            http_user_agent=settings.http_user_agent,
            max_pages_per_collect=settings.binance_rest_max_pages_per_collect,
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

    def close(self) -> None:
        """Close the client this instance owns; idempotent."""
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ---------------------------------------------------------------- entry point

    def collect(self, request: CollectionRequest) -> CollectionResult:
        target_start_ms, target_end_ms = self._validate_request(request)
        fingerprint = _request_fingerprint(request)
        root = f"{CHECKPOINT_PREFIX}/{_sha256_hex(request.request_id.encode('utf-8'))}"
        collection_key = f"{root}/collection.json"

        replayed = self._replay_collection(
            collection_key, fingerprint, request, target_start_ms, target_end_ms
        )
        if replayed is not None:
            return replayed

        budget = _Budget(self._max_pages)
        chains: list[tuple[str, list[_PageRecord]]] = []
        failure: dict[str, Any] | None = None
        try:
            for symbol in request.symbols:
                pages = self._run_chain(
                    request, symbol, target_start_ms, target_end_ms, root, budget, fingerprint
                )
                chains.append((symbol, pages))
                rejection = pages[-1].rejection
                if rejection is not None:
                    failure = _failure_document(symbol, pages[-1])
                    break
        except _CheckpointInvalid as exc:
            raise CollectionFailed(f"committed checkpoint does not reproduce: {exc}") from exc
        except StorageError as exc:
            raise CollectionFailed(f"storage failure: {exc}") from exc

        if failure is not None:
            document = _collection_document(fingerprint, chains, result=None, failure=failure)
        else:
            if [symbol for symbol, _ in chains] != list(request.symbols):
                raise CollectionFailed(  # pragma: no cover - the loop covers every symbol
                    "a successful collection must terminate every requested symbol's chain"
                )
            document = _collection_document(
                fingerprint,
                chains,
                result=self._assemble(request, target_start_ms, target_end_ms, chains),
                failure=None,
            )
        self._commit_collection(collection_key, document)
        # Whoever published first wins: read the committed checkpoint back and verify it strictly
        # rather than trusting the in-memory value (identical bytes simply verify to the same
        # result). A stable failure replays as the same ``CollectionFailed``.
        committed = self._replay_collection(
            collection_key, fingerprint, request, target_start_ms, target_end_ms
        )
        if committed is None:  # pragma: no cover - the key exists after a publish
            raise CollectionFailed("the committed collection checkpoint vanished")
        return committed

    # ---------------------------------------------------------------- request gate

    def _validate_request(self, request: CollectionRequest) -> tuple[int, int]:
        """Everything checkable before the first byte leaves this process."""
        if request.source != REST_SOURCE:
            raise UnsupportedRequest(
                f"unsupported source {request.source.source_id}@{request.source.version}"
            )
        if request.data_type not in SUPPORTED_DATA_TYPES:
            raise UnsupportedRequest(f"unsupported data_type {request.data_type!r}")
        unsupported = [s for s in request.symbols if s not in SUPPORTED_SYMBOLS]
        if unsupported:
            raise UnsupportedRequest(f"unsupported symbols: {unsupported!r}")
        start = _epoch_ms(request.coverage_start, "coverage_start")
        end = _epoch_ms(request.coverage_end, "coverage_end")
        if request.data_type == "klines_1m" and (start % 60_000 or end % 60_000):
            raise UnsupportedRequest("a 1m klines collection window must be minute-aligned")
        return start, end

    # ---------------------------------------------------------------- chain driver

    def _run_chain(
        self,
        request: CollectionRequest,
        symbol: str,
        target_start_ms: int,
        target_end_ms: int,
        root: str,
        budget: _Budget,
        fingerprint: dict[str, Any],
    ) -> list[_PageRecord]:
        pages: list[_PageRecord] = []
        previous: RestPageSummary | None = None
        page_index = 0
        while True:
            query = self._first_query(request.data_type, symbol, target_start_ms, previous)
            key = f"{root}/{symbol}/{page_index:08d}.page.json"
            record = self._load_page(
                key, fingerprint, symbol, page_index, previous, target_start_ms, target_end_ms
            )
            if record is None:
                if not budget.take():
                    raise CollectionFailed(
                        f"page budget of {self._max_pages} new page(s) exhausted before "
                        f"{symbol} page {page_index}; the attempt stays resumable"
                    )
                record = self._fetch_page(
                    key,
                    fingerprint,
                    symbol,
                    page_index,
                    query,
                    previous,
                    target_start_ms,
                    target_end_ms,
                )
            if record.query != query:
                raise _CheckpointInvalid(
                    f"committed page {page_index} of {symbol} answers another query"
                )
            pages.append(record)
            if record.rejection is not None:
                return pages
            summary = record.summary
            if summary.stop_reason is not None:
                return pages
            previous = summary
            page_index += 1

    def _first_query(
        self,
        data_type: str,
        symbol: str,
        target_start_ms: int,
        previous: RestPageSummary | None,
    ) -> RestPageQuery:
        """Page 0 is the ADR's first page; every later page is the decoder's exact next query."""
        if previous is not None:
            if previous.next_query is None:  # pragma: no cover - the driver checks stop_reason
                raise _CheckpointInvalid("the previous page stopped the chain")
            return previous.next_query
        try:
            if data_type == "agg_trades":
                return RestPageQuery.agg_trades_from_start(symbol, target_start_ms)
            return RestPageQuery.klines_from_start(symbol, target_start_ms)
        except RestIdentityViolation as exc:
            raise UnsupportedRequest(f"the first page query is not constructible: {exc}") from None

    # ---------------------------------------------------------------- fetch one page

    def _fetch_page(
        self,
        key: str,
        fingerprint: dict[str, Any],
        symbol: str,
        page_index: int,
        query: RestPageQuery,
        previous: RestPageSummary | None,
        target_start_ms: int,
        target_end_ms: int,
    ) -> _PageRecord:
        identity = self._page_identity(query)
        url = self._page_url(query)
        requested_at, retrieved_at, metadata, body = self._http_get(url)

        # c1 — the bytes get their immutable, content-addressed object first.
        body_sha256 = _sha256_hex(body)
        try:
            body_key = response_object_key(query.data_type, symbol, body_sha256, identity)
        except RestIdentityViolation as exc:  # pragma: no cover - inputs are already validated
            raise CollectionFailed(f"cannot address the response body: {exc}") from None
        body_ref = self._publish_bytes(body_key, body, label="response body")

        # c2 — decode the page and publish the page checkpoint.
        outcome = self._decode(
            query, page_index, previous, retrieved_at, target_start_ms, target_end_ms, body
        )
        record = _PageRecord(
            symbol=symbol,
            page_index=page_index,
            query=query,
            page_identity=identity,
            source_uri=self._page_source_uri(query),
            body=body_ref,
            requested_at=requested_at,
            retrieved_at=retrieved_at,
            http_status=200,
            http_metadata=metadata,
            max_body_bytes=self._max_response_bytes,
            outcome=outcome,
            checkpoint_key=key,
        )
        payload = canonical_json(_page_document(fingerprint, record)).encode("utf-8")
        try:
            self._publish_bytes(key, payload, label="page checkpoint")
        except ObjectConflict:
            winner = self._load_page(
                key, fingerprint, symbol, page_index, previous, target_start_ms, target_end_ms
            )
            if winner is None:  # pragma: no cover - the key exists after a conflict
                raise CollectionFailed("the winning page checkpoint vanished") from None
            return winner
        return record

    def _decode(
        self,
        query: RestPageQuery,
        page_index: int,
        previous: RestPageSummary | None,
        retrieved_at: datetime,
        target_start_ms: int,
        target_end_ms: int,
        body: bytes,
    ) -> RestPageDecoded | RestPageRejection:
        try:
            decode_request = RestPageDecodeRequest(
                query=query,
                retrieved_at=retrieved_at,
                target_start_ms=target_start_ms,
                target_end_ms=target_end_ms,
                max_body_bytes=self._max_response_bytes,
                page_index=page_index,
                previous=previous,
            )
            return decode_rest_page(decode_request, body)
        except RestDecodeRequestError as exc:
            raise CollectionFailed(f"the page cannot be decoded as requested: {exc}") from None

    # ---------------------------------------------------------------- HTTP

    def _page_identity(self, query: RestPageQuery) -> str:
        try:
            return page_identity_sha256(query, self._origin)
        except RestIdentityViolation as exc:  # pragma: no cover - the origin is validated once
            raise CollectionFailed(f"cannot derive the page identity: {exc}") from None

    def _page_source_uri(self, query: RestPageQuery) -> str:
        try:
            return page_source_uri(query, self._origin)
        except RestIdentityViolation as exc:  # pragma: no cover - the origin is validated once
            raise CollectionFailed(f"cannot derive the page URI: {exc}") from None

    def _page_url(self, query: RestPageQuery) -> str:
        """Build the request URL from the structured query and re-check it structurally.

        Callers can never hand in a URL or a free-form parameter mapping: the only input is a
        ``RestPageQuery``, whose construction is itself the parameter allowlist. This second,
        independent pass exists so that a mistake anywhere upstream fails **before** any socket
        is opened.
        """
        url = self._page_source_uri(query)
        if not _uri.is_visible_ascii(url) or "%" in url:
            raise CollectionFailed("refusing a non visible-ASCII or percent-escaped page URL")
        parts = _uri.split(url)
        if parts.scheme != "https" or parts.authority is None:
            raise CollectionFailed("refusing a non-HTTPS page URL")
        if not _uri.is_host_port(parts.authority):
            raise CollectionFailed("refusing a page URL with an invalid authority")
        if f"https://{parts.authority}" != self._origin:
            raise CollectionFailed("refusing a page URL outside the market-data origin")
        if parts.path != DATA_TYPES.get(query.data_type):
            raise CollectionFailed("refusing a page URL outside the frozen endpoint set")
        if parts.fragment is not None:
            raise CollectionFailed("refusing a page URL with a fragment")
        if parts.query is None or parts.query != query.query_string():
            raise CollectionFailed("refusing a page URL whose query is not the canonical one")
        pairs: list[tuple[str, str]] = []
        for item in parts.query.split("&"):
            name, separator, value = item.partition("=")
            if not separator or not name:
                raise CollectionFailed("refusing a page URL with a malformed query")
            pairs.append((name, value))
        try:
            parsed = RestPageQuery.from_pairs(query.data_type, pairs)
        except RestIdentityViolation as exc:
            raise CollectionFailed(f"refusing a page URL query: {exc}") from None
        if parsed != query:
            raise CollectionFailed("refusing a page URL that does not round-trip to its query")
        return url

    def _assert_outgoing(self, request: httpx.Request, url: str) -> None:
        """The request about to leave is exactly the validated one, carrying no credential.

        Defence in depth only: the owned client has no hooks, auth, cookies or environment
        trust, so nothing can change the request between this check and the transport.
        """
        if request.method != "GET" or str(request.url) != url:
            raise CollectionFailed("refusing an outgoing request that differs from the page URL")
        for name in request.headers.keys():
            if _has_credential_shape(name):
                raise CollectionFailed("refusing an outgoing request with a credential header")

    def _throttle(self) -> None:
        """At least ``min_request_interval_ms`` between two requests, retries included."""
        last = self._last_send_monotonic
        if last is not None:
            waited = self._monotonic() - last
            if waited < self._min_interval_seconds:
                self._sleeper(self._min_interval_seconds - waited)
        self._last_send_monotonic = self._monotonic()

    def _http_get(self, url: str) -> tuple[datetime, datetime, dict[str, str], bytes]:
        """One page: bounded attempts, no redirect, bounded body, two honest local instants."""
        attempts = 1 + self._max_retries
        last_error: Exception | None = None
        for attempt in range(attempts):
            response: httpx.Response | None = None
            try:
                self._throttle()
                requested_at = self._checked_now("requested_at")
                try:
                    built = self._client.build_request(
                        "GET",
                        url,
                        headers={"User-Agent": self._user_agent},
                        timeout=self._timeout,
                    )
                    self._assert_outgoing(built, url)
                    response = self._client.send(built, stream=True, follow_redirects=False)
                except httpx.HTTPError as exc:
                    last_error = exc
                    if isinstance(exc, _RETRYABLE_TRANSPORT) and attempt + 1 < attempts:
                        continue
                    raise CollectionFailed(_transport_failure_message(exc)) from exc

                status = response.status_code
                if 300 <= status < 400:
                    raise CollectionFailed(f"redirects are forbidden: HTTP {status}")
                if status == 418:
                    raise CollectionFailed("HTTP 418: the venue has banned this client; stopping")
                if status == 429:
                    delay = self._retry_after_seconds(response.headers)
                    if delay is None:
                        raise CollectionFailed(
                            "HTTP 429 without a decimal-second Retry-After within "
                            f"{self._max_retry_after}s"
                        )
                    if attempt + 1 >= attempts:
                        raise CollectionFailed("exhausted retries for HTTP 429")
                    self._sleeper(delay)
                    last_error = CollectionFailed("retryable HTTP 429")
                    continue
                if 500 <= status < 600:
                    last_error = CollectionFailed(f"retryable HTTP {status}")
                    if attempt + 1 < attempts:
                        continue
                    raise CollectionFailed(f"exhausted retries for HTTP {status}")
                if status != 200:
                    raise CollectionFailed(f"HTTP {status}")

                metadata = self._response_metadata(response.headers)
                declared = self._declared_length(response.headers)
                coded = self._has_content_coding(response.headers)
                if declared is not None and not coded and declared > self._max_response_bytes:
                    raise CollectionFailed(
                        f"declared body of {declared} bytes exceeds the "
                        f"{self._max_response_bytes} byte limit"
                    )
                try:
                    body = self._read_bounded(response)
                except httpx.HTTPError as exc:
                    last_error = exc
                    if isinstance(exc, _RETRYABLE_TRANSPORT) and attempt + 1 < attempts:
                        continue
                    raise CollectionFailed(_transport_failure_message(exc)) from exc
                retrieved_at = self._checked_now("retrieved_at")
                if declared is not None and not coded and declared != len(body):
                    raise CollectionFailed(
                        f"Content-Length {declared} disagrees with the {len(body)} byte entity"
                    )
                if retrieved_at <= requested_at:
                    raise CollectionFailed(
                        "the local clock did not advance between the request and the last byte"
                    )
                return requested_at, retrieved_at, metadata, body
            finally:
                if response is not None:
                    response.close()

        raise CollectionFailed(  # pragma: no cover - the loop always raises or returns
            f"exhausted retries: {last_error}"
        )

    def _checked_now(self, label: str) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise CollectionFailed(f"{label} must be a timezone-aware datetime")
        if value.utcoffset() != timedelta(0):
            raise CollectionFailed(f"{label} must be UTC")
        if value < _EPOCH:
            raise CollectionFailed(f"{label} must not precede the UNIX epoch")
        return value

    def _retry_after_seconds(self, headers: httpx.Headers) -> float | None:
        raw = headers.get("retry-after")
        if not isinstance(raw, str) or _DELAY_SECONDS_RE.fullmatch(raw) is None:
            return None
        value = int(raw)
        if value > self._max_retry_after:
            return None
        return float(value)

    @staticmethod
    def _declared_length(headers: httpx.Headers) -> int | None:
        raw = headers.get("content-length")
        if raw is None:
            return None
        if not isinstance(raw, str) or _CONTENT_LENGTH_RE.fullmatch(raw) is None:
            raise CollectionFailed("refusing a malformed Content-Length")
        return int(raw)

    @staticmethod
    def _has_content_coding(headers: httpx.Headers) -> bool:
        """``Content-Length`` counts coded bytes, so equality only holds without a coding."""
        raw = headers.get("content-encoding")
        if not isinstance(raw, str):
            return False
        return raw.strip().lower() not in {"", "identity"}

    def _read_bounded(self, response: httpx.Response) -> bytes:
        """Count decoded bytes while streaming: a decompression bomb dies at the limit."""
        buffer = bytearray()
        for chunk in response.iter_bytes(chunk_size=_STREAM_CHUNK_SIZE):
            if not chunk:
                continue
            if len(buffer) + len(chunk) > self._max_response_bytes:
                raise CollectionFailed(
                    f"response body exceeds the {self._max_response_bytes} byte limit"
                )
            buffer.extend(chunk)
        return bytes(buffer)

    def _response_metadata(self, headers: httpx.Headers) -> dict[str, str]:
        """Explicit allowlist; any credential-shaped header name refuses the whole page."""
        for name in headers.keys():
            if _has_credential_shape(name):
                raise CollectionFailed(
                    "refusing a response carrying a credential-shaped header name"
                )
        metadata: dict[str, str] = {}
        for name in sorted(ALLOWED_RESPONSE_HEADERS):
            raw = headers.get(name)
            if raw is None:
                continue
            if not isinstance(raw, str):  # pragma: no cover - httpx yields str
                raise CollectionFailed("refusing a non-textual response header value")
            value = raw.strip()
            if not value.isascii() or not value.isprintable():
                raise CollectionFailed(f"refusing a non visible-ASCII {name} header value")
            metadata[name] = value
        return metadata

    # ---------------------------------------------------------------- storage

    def _publish_bytes(self, key: str, payload: bytes, *, label: str) -> ObjectRef:
        digest = _sha256_hex(payload)
        try:
            staged = self._storage.stage(
                StageRequest(key=key, expected_sha256=digest, expected_size=len(payload)),
                _chunks(payload),
            )
            return self._storage.publish(staged).ref
        except ObjectConflict:
            raise
        except StorageError as exc:
            raise CollectionFailed(f"cannot publish the {label}: {exc}") from exc

    def _commit_collection(self, key: str, document: dict[str, Any]) -> None:
        """Publish the collection checkpoint; an ``ObjectConflict`` means another writer won."""
        payload = canonical_json(document).encode("utf-8")
        try:
            self._publish_bytes(key, payload, label="collection checkpoint")
        except ObjectConflict:
            return

    def _read_object(self, key: str) -> bytes | None:
        ref = self._storage.lookup(key)
        if ref is None:
            return None
        with self._storage.open_read(ref) as handle:
            data = handle.read()
        if _sha256_hex(data) != ref.sha256 or len(data) != ref.size:  # pragma: no cover
            raise _CheckpointInvalid(f"{key!r} does not match its published reference")
        return data

    # ---------------------------------------------------------------- page recovery

    def _load_page(
        self,
        key: str,
        fingerprint: dict[str, Any],
        symbol: str,
        page_index: int,
        previous: RestPageSummary | None,
        target_start_ms: int,
        target_end_ms: int,
    ) -> _PageRecord | None:
        payload = self._read_object(key)
        if payload is None:
            return None
        record = self._verify_page(
            key, payload, fingerprint, symbol, page_index, previous, target_start_ms, target_end_ms
        )
        return record

    def _verify_page(
        self,
        key: str,
        payload: bytes,
        fingerprint: dict[str, Any],
        symbol: str,
        page_index: int,
        previous: RestPageSummary | None,
        target_start_ms: int,
        target_end_ms: int,
    ) -> _PageRecord:
        """Re-read, re-verify and **re-decode** a committed page; any drift fails closed."""
        document = _parse_checkpoint(payload, PAGE_CHECKPOINT_KIND)
        if _mapping_field(document, "request") != fingerprint:
            raise _CheckpointInvalid(f"{key!r} belongs to another request content")
        if _str_field(document, "symbol") != symbol:
            raise _CheckpointInvalid(f"{key!r} belongs to another symbol")
        if _int_field(document, "page_index") != page_index:
            raise _CheckpointInvalid(f"{key!r} belongs to another page index")

        decoder = _mapping_field(document, "decoder")
        if (
            _str_field(decoder, "policy_id") != DECODER_BINDING.policy_id
            or _str_field(decoder, "version") != DECODER_BINDING.version
            or _str_field(decoder, "policy_hash") != DECODER_BINDING.policy_hash
        ):
            raise _CheckpointInvalid(f"{key!r} was decoded by another decoder binding")

        query = _parse_query(_mapping_field(document, "query"))
        identity = self._page_identity(query)
        if _str_field(document, "page_identity_sha256") != identity:
            raise _CheckpointInvalid(f"{key!r} does not carry this page's canonical identity")
        if _str_field(document, "source_uri") != self._page_source_uri(query):
            raise _CheckpointInvalid(f"{key!r} does not carry this page's request URI")

        body_document = _mapping_field(document, "body")
        body_ref = ObjectRef(
            key=_str_field(body_document, "key"),
            uri=_str_field(body_document, "uri"),
            sha256=_str_field(body_document, "sha256"),
            size=_int_field(body_document, "size"),
        )
        if _SHA256_RE.fullmatch(body_ref.sha256) is None:
            raise _CheckpointInvalid(f"{key!r} carries a non-canonical body sha256")
        try:
            expected_key = response_object_key(query.data_type, symbol, body_ref.sha256, identity)
        except RestIdentityViolation as exc:
            raise _CheckpointInvalid(f"{key!r} carries an unaddressable body: {exc}") from None
        if body_ref.key != expected_key:
            raise _CheckpointInvalid(f"{key!r} carries a body key that is not content-addressed")
        published = self._storage.lookup(body_ref.key)
        if published is None:
            raise _CheckpointInvalid(f"{key!r} references a body that is not published")
        if published != body_ref:
            raise _CheckpointInvalid(f"{key!r} references a body whose reference has drifted")
        with self._storage.open_read(published) as handle:
            body = handle.read()
        if _sha256_hex(body) != body_ref.sha256 or len(body) != body_ref.size:
            raise _CheckpointInvalid(f"{key!r} references a body that does not match its hash")

        requested_at = _utc_field(document, "requested_at")
        retrieved_at = _utc_field(document, "retrieved_at")
        if retrieved_at <= requested_at:
            raise _CheckpointInvalid(f"{key!r} records a non-advancing clock")

        http = _mapping_field(document, "http")
        status = _int_field(http, "status")
        if status != 200:
            raise _CheckpointInvalid(f"{key!r} records a non-200 answer")
        metadata = self._verified_metadata(_mapping_field(http, "metadata"), key)
        try:
            max_body_bytes = _in_range(
                _int_field(document, "decoder_max_body_bytes"),
                MIN_RESPONSE_BYTES,
                MAX_RESPONSE_BYTES,
                "decoder_max_body_bytes",
            )
        except ValueError as exc:
            raise _CheckpointInvalid(f"{key!r} carries an out-of-range body limit") from exc

        try:
            decode_request = RestPageDecodeRequest(
                query=query,
                retrieved_at=retrieved_at,
                target_start_ms=target_start_ms,
                target_end_ms=target_end_ms,
                max_body_bytes=max_body_bytes,
                page_index=page_index,
                previous=previous,
            )
            outcome = decode_rest_page(decode_request, body)
        except RestDecodeRequestError as exc:
            raise _CheckpointInvalid(f"{key!r} does not re-decode: {exc}") from None

        record = _PageRecord(
            symbol=symbol,
            page_index=page_index,
            query=query,
            page_identity=identity,
            source_uri=_str_field(document, "source_uri"),
            body=body_ref,
            requested_at=requested_at,
            retrieved_at=retrieved_at,
            http_status=status,
            http_metadata=metadata,
            max_body_bytes=max_body_bytes,
            outcome=outcome,
            checkpoint_key=key,
        )
        rebuilt = canonical_json(_page_document(fingerprint, record))
        if rebuilt.encode("utf-8") != payload:
            raise _CheckpointInvalid(f"{key!r} does not reproduce field for field")
        return record

    @staticmethod
    def _verified_metadata(document: Mapping[str, Any], key: str) -> dict[str, str]:
        metadata: dict[str, str] = {}
        for name, value in document.items():
            if name not in ALLOWED_RESPONSE_HEADERS or _has_credential_shape(name):
                raise _CheckpointInvalid(f"{key!r} carries a header outside the allowlist")
            if not isinstance(value, str) or not value.isascii() or not value.isprintable():
                raise _CheckpointInvalid(f"{key!r} carries a non visible-ASCII header value")
            metadata[name] = value
        return metadata

    # ---------------------------------------------------------------- collection recovery

    def _replay_collection(
        self,
        key: str,
        fingerprint: dict[str, Any],
        request: CollectionRequest,
        target_start_ms: int,
        target_end_ms: int,
    ) -> CollectionResult | None:
        """Return the first committed result, replay the first stable failure, or ``None``.

        Never touches the network: everything comes from immutable objects.
        """
        try:
            payload = self._read_object(key)
            if payload is None:
                return None
            document = _parse_checkpoint(payload, COLLECTION_CHECKPOINT_KIND)
            if _mapping_field(document, "request") != fingerprint:
                raise _CheckpointInvalid(
                    "this request_id is already committed with different request content"
                )
            chains = self._verify_chains(
                key, document, fingerprint, request, target_start_ms, target_end_ms
            )
            if not chains:
                raise _CheckpointInvalid(f"{key!r} records no chain at all")
            outcome = _str_field(document, "outcome")
            if outcome == "failed":
                failure = _mapping_field(document, "failure")
                self._check_failure_shape(failure, chains)
                rebuilt = _collection_document(
                    fingerprint, chains, result=None, failure=dict(failure)
                )
                if canonical_json(rebuilt).encode("utf-8") != payload:
                    raise _CheckpointInvalid(f"{key!r} does not reproduce field for field")
                raise CollectionFailed(_failure_message(failure))
            if outcome != "succeeded":
                raise _CheckpointInvalid(f"{key!r} carries an unknown outcome")
            if [symbol for symbol, _ in chains] != list(request.symbols):
                raise _CheckpointInvalid(f"{key!r} succeeds without every requested symbol")
            for symbol, pages in chains:
                if pages[-1].rejection is not None or pages[-1].summary.stop_reason is None:
                    raise _CheckpointInvalid(f"{key!r} records an unterminated chain for {symbol}")
            result = self._assemble(request, target_start_ms, target_end_ms, chains)
            rebuilt = _collection_document(fingerprint, chains, result=result, failure=None)
            if canonical_json(rebuilt).encode("utf-8") != payload:
                raise _CheckpointInvalid(f"{key!r} does not reproduce field for field")
            return result
        except _CheckpointInvalid as exc:
            raise CollectionFailed(f"committed checkpoint does not reproduce: {exc}") from exc
        except StorageError as exc:
            raise CollectionFailed(f"storage failure: {exc}") from exc

    def _verify_chains(
        self,
        key: str,
        document: Mapping[str, Any],
        fingerprint: dict[str, Any],
        request: CollectionRequest,
        target_start_ms: int,
        target_end_ms: int,
    ) -> list[tuple[str, list[_PageRecord]]]:
        root = key.rsplit("/", 1)[0]
        entries = _list_field(document, "chains")
        symbols = [_str_field(entry, "symbol") for entry in entries if isinstance(entry, dict)]
        if len(symbols) != len(entries):
            raise _CheckpointInvalid(f"{key!r} carries a malformed chain entry")
        if symbols != [s for s in request.symbols if s in set(symbols)] or len(set(symbols)) != len(
            symbols
        ):
            raise _CheckpointInvalid(f"{key!r} carries chains in a non-canonical order")
        chains: list[tuple[str, list[_PageRecord]]] = []
        for entry in entries:
            symbol = _str_field(entry, "symbol")
            keys = _list_field(entry, "page_checkpoint_keys")
            if _int_field(entry, "page_count") != len(keys) or not keys:
                raise _CheckpointInvalid(f"{key!r} carries an empty or miscounted chain")
            pages: list[_PageRecord] = []
            previous: RestPageSummary | None = None
            for page_index, page_key in enumerate(keys):
                expected = f"{root}/{symbol}/{page_index:08d}.page.json"
                if page_key != expected:
                    raise _CheckpointInvalid(f"{key!r} lists a page checkpoint out of place")
                payload = self._read_object(expected)
                if payload is None:
                    raise _CheckpointInvalid(f"{expected!r} is missing")
                record = self._verify_page(
                    expected,
                    payload,
                    fingerprint,
                    symbol,
                    page_index,
                    previous,
                    target_start_ms,
                    target_end_ms,
                )
                pages.append(record)
                if record.rejection is not None:
                    if page_index + 1 != len(keys):
                        raise _CheckpointInvalid(f"{key!r} continues past a rejected page")
                    break
                summary = record.summary
                if summary.stop_reason is not None:
                    if page_index + 1 != len(keys):
                        raise _CheckpointInvalid(f"{key!r} continues past a terminated chain")
                    break
                previous = summary
            else:
                raise _CheckpointInvalid(f"{key!r} records a chain that never terminates")
            chains.append((symbol, pages))
        return chains

    @staticmethod
    def _check_failure_shape(
        failure: Mapping[str, Any], chains: list[tuple[str, list[_PageRecord]]]
    ) -> None:
        if _str_field(failure, "classification") != "decoder_rejected":
            raise _CheckpointInvalid("only a decoder rejection is a stable failure")
        symbol, pages = chains[-1]
        if _str_field(failure, "symbol") != symbol:
            raise _CheckpointInvalid("the recorded failure is not on the last chain")
        if _failure_document(symbol, pages[-1]) != dict(failure):
            raise _CheckpointInvalid("the recorded failure does not reproduce")

    # ---------------------------------------------------------------- result assembly

    def _assemble(
        self,
        request: CollectionRequest,
        target_start_ms: int,
        target_end_ms: int,
        chains: list[tuple[str, list[_PageRecord]]],
    ) -> CollectionResult:
        """Objects and at most one honest tail gap per symbol, from the committed pages only."""
        objects: list[CollectedObject] = []
        gaps: list[CoverageGap] = []
        for symbol, pages in chains:
            upper = target_start_ms
            for page in pages:
                answered: AnsweredInterval = page.summary.answered
                upper = max(upper, answered.end_ms)
                low = max(answered.start_ms, target_start_ms)
                high = min(answered.end_ms, target_end_ms)
                if high <= low:
                    continue
                objects.append(
                    CollectedObject(
                        ref=page.body,
                        symbol=symbol,
                        coverage_start=_at_ms(low),
                        coverage_end=_at_ms(high),
                        source_uri=page.source_uri,
                        retrieved_at=page.retrieved_at,
                        source_sha256=None,
                        source_metadata=FrozenMapping(page.http_metadata),
                    )
                )
            if upper < target_end_ms:
                gaps.append(
                    CoverageGap(
                        symbol=symbol,
                        coverage_start=_at_ms(upper),
                        coverage_end=_at_ms(target_end_ms),
                        reason=GapReason.SOURCE_ABSENT,
                        detail=_gap_detail(request.data_type, symbol, pages[-1], upper),
                    )
                )
        return CollectionResult(
            request=request,
            collector_id=REST_COLLECTOR_ID,
            collector_version=REST_COLLECTOR_VERSION,
            objects=tuple(objects),
            gaps=tuple(gaps),
        )


def _gap_detail(data_type: str, symbol: str, page: _PageRecord, upper_ms: int) -> str:
    """The ADR-0027 §7 wording: what this exact query was answered, and nothing more."""
    summary = page.summary
    return (
        f"rest {data_type} {symbol} page {page.page_index}: "
        f"GET {page.query.path}?{page.query.query_string()} returned "
        f"{summary.served_count} of {page.query.limit} at {_iso(page.retrieved_at)}; "
        f"nothing at or after {_iso(_at_ms(upper_ms))} was served; "
        f"body sha256 {page.body.sha256}; "
        "records the source's answer only, not an absence of market activity"
    )

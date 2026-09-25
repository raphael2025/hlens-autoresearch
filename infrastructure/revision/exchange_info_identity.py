"""exchangeInfo snapshot identity ``hlens.binance.spot.exchange-info-identity@1.0.0`` (Phase 1 E2).

ADR-0029 §1: every successful ``GET /api/v3/exchangeInfo`` answer is one **Raw source revision** of
source ``binance.public.spot.exchange-info@1.0.0``. Identity and replay follow the ADR-0027 §2
"request identity + bytes" rule, as a **new, independently hashed** rule: this module never imports
the archive or REST identity rules, so neither can move.

What the rule fixes:

- **the query allowlist** — ``ExchangeInfoQuery`` is the only way to name a request. Its one
  parameter is ``symbols``, and its one value in 1.0.0 is the first-slice pair
  ``["BTCUSDT","ETHUSDT"]`` (ADR-0022 §1 / ADR-0029 §1). No other parameter (``symbol``,
  ``symbolStatus``, ``permissions``, ``showPermissionSets``, anything credential-shaped) and no
  other symbol set is constructible, and a query string that is not the exact canonical encoding
  does not parse;
- **the request identity** — ``{method, origin, path, query, rule}`` with the configured
  market-data-only origin; ``request_identity_sha256`` is its canonical-JSON SHA-256. The logical
  attempt (``request_id``), the clock and the attempt number are not part of it;
- **the revision** — ``observation_key = binance:spot:exchange-info:<request_identity_sha256>``,
  channel-level source identity ``binance.public.spot.exchange-info@1.0.0``, payload hash = the
  SHA-256 of the response entity body, ``revision_id = rev1-<sha256>`` of ``{rule id + version +
  hash, observation key, source identity, payload hash}``. The same bytes for the same request are
  the same revision (replay); other bytes are another revision of the same key. Snapshots of one
  key are never ordered against each other here — the listing derivation orders *observations*
  by ``retrieved_at`` under its own precedence policy (ADR-0029 §2);
- **the object key** — content addressed by the body SHA-256;
- ``arrival_seq`` — ``[0, 2**62)`` of this table only: the next number above the largest committed
  one. It orders nothing; it only has to be unique inside the table's revision graph.

No I/O, no clock, no network: every function here is a pure function of its arguments.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

from core.contracts.collector import NETWORK_ORIGIN_PATTERN
from core.domain.base import canonical_json

__all__ = [
    "EXCHANGE_INFO_ARRIVAL_SEQ_LIMIT",
    "EXCHANGE_INFO_IDENTITY_HASH",
    "EXCHANGE_INFO_IDENTITY_RULE_ID",
    "EXCHANGE_INFO_IDENTITY_RULE_VERSION",
    "EXCHANGE_INFO_IDENTITY_SPEC",
    "EXCHANGE_INFO_PATH",
    "EXCHANGE_INFO_SOURCE_ID",
    "EXCHANGE_INFO_SOURCE_VERSION",
    "EXCHANGE_INFO_SYMBOLS",
    "ExchangeInfoIdentityViolation",
    "ExchangeInfoQuery",
    "check_arrival_seq",
    "exchange_info_source_identity",
    "next_arrival_seq",
    "request_identity_document",
    "request_identity_sha256",
    "request_source_uri",
    "response_object_key",
    "revision_id",
    "snapshot_observation_key",
]

EXCHANGE_INFO_IDENTITY_RULE_ID: Final = "hlens.binance.spot.exchange-info-identity"
EXCHANGE_INFO_IDENTITY_RULE_VERSION: Final = "1.0.0"

VENUE: Final = "binance"
MARKET: Final = "spot"
EXCHANGE_INFO_SOURCE_ID: Final = "binance.public.spot.exchange-info"
EXCHANGE_INFO_SOURCE_VERSION: Final = "1.0.0"
EXCHANGE_INFO_PATH: Final = "/api/v3/exchangeInfo"
#: The one symbol set of 1.0.0: the ADR-0022 §1 first slice, in canonical (sorted) order.
EXCHANGE_INFO_SYMBOLS: Final[tuple[str, ...]] = ("BTCUSDT", "ETHUSDT")
#: ``arrival_seq`` interval of ``raw.binance_spot_exchange_info`` (an Iceberg ``long``).
EXCHANGE_INFO_ARRIVAL_SEQ_LIMIT: Final = 1 << 62

_SYMBOLS_PARAMETER: Final = "symbols"
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_ORIGIN_RE: Final = re.compile(NETWORK_ORIGIN_PATTERN)
_OBJECT_KEY_PREFIX: Final = "raw/binance/spot/exchange-info/responses"


class ExchangeInfoIdentityViolation(ValueError):
    """An identity input is not well formed; nothing may be derived from it (fail closed)."""


def _digest(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def _check_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ExchangeInfoIdentityViolation(f"{label} must be lower-case SHA-256 hex")
    return value


def _check_origin(origin: object) -> str:
    """Exact ``https://host[:port]`` (the configured market-data-only origin)."""
    if not isinstance(origin, str) or _ORIGIN_RE.fullmatch(origin) is None:
        raise ExchangeInfoIdentityViolation(
            f"origin {origin!r} is not an exact https://host[:port]"
        )
    authority = origin.removeprefix("https://")
    if ":" in authority:
        port = authority.rsplit(":", 1)[1]
        if port.startswith("0") or not 1 <= int(port) <= 65535:
            raise ExchangeInfoIdentityViolation(f"origin {origin!r} has an invalid port")
    return origin


# --------------------------------------------------------------------------- the query allowlist


@dataclass(frozen=True, slots=True)
class ExchangeInfoQuery:
    """The one allow-listed ``GET /api/v3/exchangeInfo`` query of source 1.0.0.

    Construction is the allowlist: ``symbols`` must be exactly ``EXCHANGE_INFO_SYMBOLS``; there is
    no field for any other parameter.
    """

    symbols: tuple[str, ...] = EXCHANGE_INFO_SYMBOLS

    def __post_init__(self) -> None:
        if not isinstance(self.symbols, tuple) or self.symbols != EXCHANGE_INFO_SYMBOLS:
            raise ExchangeInfoIdentityViolation(
                f"exchangeInfo symbols must be exactly {list(EXCHANGE_INFO_SYMBOLS)}"
            )

    @property
    def path(self) -> str:
        return EXCHANGE_INFO_PATH

    def symbols_value(self) -> str:
        """The ``symbols`` value as sent: a compact JSON array of the sorted symbols."""
        return json.dumps(list(self.symbols), separators=(",", ":"))

    def pairs(self) -> tuple[tuple[str, str], ...]:
        """The canonical ``(name, value)`` pairs (unencoded values)."""
        return ((_SYMBOLS_PARAMETER, self.symbols_value()),)

    def query_string(self) -> str:
        """``symbols=<value>`` with every non-alphanumeric byte percent-encoded (upper-case hex)."""
        return f"{_SYMBOLS_PARAMETER}={quote(self.symbols_value(), safe='')}"

    @classmethod
    def from_query_string(cls, text: object) -> ExchangeInfoQuery:
        """Parse a query string; only the exact canonical encoding is accepted.

        Unknown, repeated or missing parameters, another symbol set, another encoding of the same
        set and every credential-shaped name fail closed.
        """
        if not isinstance(text, str) or not text:
            raise ExchangeInfoIdentityViolation("the exchangeInfo query is empty")
        names: list[str] = []
        values: list[str] = []
        for part in text.split("&"):
            name, separator, value = part.partition("=")
            if not separator or not name:
                raise ExchangeInfoIdentityViolation("the exchangeInfo query is not name=value")
            names.append(name)
            values.append(value)
        if names != [_SYMBOLS_PARAMETER]:
            extra = sorted(set(names) - {_SYMBOLS_PARAMETER})
            raise ExchangeInfoIdentityViolation(
                f"the exchangeInfo query must carry exactly one symbols parameter "
                f"(not in the allowlist: {extra})"
            )
        query = cls()
        if query.query_string() != text:
            raise ExchangeInfoIdentityViolation(
                "the exchangeInfo query is not the canonical encoding of the first-slice symbols"
            )
        return query


# --------------------------------------------------------------------------- request identity


def request_identity_document(query: ExchangeInfoQuery, origin: str) -> dict[str, Any]:
    """The canonical request identity; the logical request id is not part of it."""
    if not isinstance(query, ExchangeInfoQuery):
        raise ExchangeInfoIdentityViolation("query must be an ExchangeInfoQuery")
    return {
        "method": "GET",
        "origin": _check_origin(origin),
        "path": query.path,
        "query": [[name, value] for name, value in query.pairs()],
        "rule": f"{EXCHANGE_INFO_IDENTITY_RULE_ID}@{EXCHANGE_INFO_IDENTITY_RULE_VERSION}",
    }


def request_identity_sha256(query: ExchangeInfoQuery, origin: str) -> str:
    """SHA-256 of the canonical request identity document."""
    return _digest(request_identity_document(query, origin))


def request_source_uri(query: ExchangeInfoQuery, origin: str) -> str:
    """The exact request URI (``origin + path + '?' + canonical query``)."""
    if not isinstance(query, ExchangeInfoQuery):
        raise ExchangeInfoIdentityViolation("query must be an ExchangeInfoQuery")
    return f"{_check_origin(origin)}{query.path}?{query.query_string()}"


def snapshot_observation_key(request_identity: str) -> str:
    """Observation key of a snapshot revision: the answer to one exact request."""
    identity = _check_sha256(request_identity, "request_identity_sha256")
    return f"{VENUE}:{MARKET}:exchange-info:{identity}"


def exchange_info_source_identity() -> str:
    """Channel-level source identity of every snapshot revision."""
    return f"{EXCHANGE_INFO_SOURCE_ID}@{EXCHANGE_INFO_SOURCE_VERSION}"


def response_object_key(body_sha256: str, request_identity: str) -> str:
    """Content-addressed warehouse key of one response body."""
    body = _check_sha256(body_sha256, "body sha256")
    identity = _check_sha256(request_identity, "request_identity_sha256")
    return f"{_OBJECT_KEY_PREFIX}/{body}/{identity}.json"


def revision_id(observation_key: str, source_identity: str, payload_hash: str) -> str:
    """Stable snapshot revision identity: same inputs → same id, whenever they arrive."""
    if not isinstance(observation_key, str) or not observation_key:
        raise ExchangeInfoIdentityViolation("observation_key must be a non-empty string")
    if not isinstance(source_identity, str) or not source_identity:
        raise ExchangeInfoIdentityViolation("source_identity must be a non-empty string")
    document = {
        "rule": EXCHANGE_INFO_IDENTITY_RULE_ID,
        "rule_version": EXCHANGE_INFO_IDENTITY_RULE_VERSION,
        "rule_hash": EXCHANGE_INFO_IDENTITY_HASH,
        "observation_key": observation_key,
        "source_identity": source_identity,
        "payload_hash": _check_sha256(payload_hash, "payload_hash"),
    }
    return f"rev1-{_digest(document)}"


# --------------------------------------------------------------------------- arrival_seq


def check_arrival_seq(value: object) -> int:
    """``value`` if it lies in ``[0, 2**62)``; otherwise fail closed."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ExchangeInfoIdentityViolation("arrival_seq must be an int")
    if not 0 <= value < EXCHANGE_INFO_ARRIVAL_SEQ_LIMIT:
        raise ExchangeInfoIdentityViolation(f"arrival_seq {value} is outside [0, 2**62)")
    return value


def next_arrival_seq(largest: int | None) -> int:
    """The number after every committed ``arrival_seq`` (``0`` for an empty table)."""
    if largest is None:
        return 0
    check_arrival_seq(largest)
    if largest + 1 >= EXCHANGE_INFO_ARRIVAL_SEQ_LIMIT:
        raise ExchangeInfoIdentityViolation("exchangeInfo arrival sequence space exhausted")
    return largest + 1


#: The complete, versioned identity rule. ``EXCHANGE_INFO_IDENTITY_HASH`` is its JSON SHA-256.
EXCHANGE_INFO_IDENTITY_SPEC: Final[dict[str, Any]] = {
    "rule": EXCHANGE_INFO_IDENTITY_RULE_ID,
    "version": EXCHANGE_INFO_IDENTITY_RULE_VERSION,
    "adr": "ADR-0029",
    "venue": VENUE,
    "market": MARKET,
    "source": {"source_id": EXCHANGE_INFO_SOURCE_ID, "version": EXCHANGE_INFO_SOURCE_VERSION},
    "separate_from": [
        "hlens.binance.spot.raw-revision-identity@1.0.0",
        "hlens.binance.spot.rest-revision-identity@1.0.0",
    ],
    "request_identity": {
        "document": ["method", "origin", "path", "query", "rule"],
        "method": "GET",
        "origin": "configured market-data-only https://host[:port], exact",
        "path": EXCHANGE_INFO_PATH,
        "query_allowlist": {_SYMBOLS_PARAMETER: list(EXCHANGE_INFO_SYMBOLS)},
        "symbols_value": "compact JSON array of the sorted symbols",
        "query_encoding": "symbols=<percent-encoded value, upper-case hex, only A-Z a-z 0-9 kept>",
        "never_sent": ["symbol", "symbolStatus", "permissions", "showPermissionSets"],
        "excluded_inputs": ["request_id", "requested_at", "retrieved_at", "attempt number"],
        "digest": "sha256 of canonical JSON",
    },
    "observation_key": "<venue>:<market>:exchange-info:<request_identity_sha256>",
    "source_identity": "<source_id>@<source_version> (channel level)",
    "payload_hash": "sha256 of the response entity body",
    "object_key": f"{_OBJECT_KEY_PREFIX}/<body sha256>/<request_identity_sha256>.json",
    "revision_id": {
        "format": "rev1-<sha256 hex>",
        "document": [
            "rule",
            "rule_version",
            "rule_hash",
            "observation_key",
            "source_identity",
            "payload_hash",
        ],
        "same_bytes_same_request": "replay: the same revision, never a second row",
        "other_bytes_same_request": "another revision of the same key; never ordered here",
    },
    "arrival_seq": {
        "interval": [0, EXCHANGE_INFO_ARRIVAL_SEQ_LIMIT],
        "allocation": "largest committed + 1 (0 for an empty table)",
        "anchor_table": "raw.binance_spot_exchange_info",
        "semantics": "audit, idempotency and recovery only; never precedence or selection",
    },
}
EXCHANGE_INFO_IDENTITY_HASH: Final = _digest(EXCHANGE_INFO_IDENTITY_SPEC)

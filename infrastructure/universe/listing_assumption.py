"""Listing history "observed-state backfill" assumption (ADR-0051 §2, D-LIST; Claude PM accepted
2026-09-28 under Raphael's 2026-09-28 delegation; policy table populated under ADR-0100 item 5).

Stored listing revisions stay exactly as ADR-0029 derives them: ``tradable_from`` is this
installation's own first observation of ``TRADING``, never an exchange-declared listing date, and
before that first observation a point-in-time read is ``no_visible_listing`` (ADR-0029 §3). A
``PointInTimeSpec`` that **binds** one version of
``hlens.listing.observed-state-backfill-assumption`` in its ``availability_bindings`` lets a
listing read additionally treat, for **one venue symbol** named in *that version's* policy table
and **only** its episode's first (unsuperseded) revision, the half-open span
``[backfill_floor, first_observed_tradable_from)`` as an assumed ``listed`` member interval, with
that first revision's effective ``available_time = min(stored, backfill_floor)`` (never later than
stored). This module mirrors ``infrastructure/pit/assumption.py`` (ADR-0032, its declared
structural template): identity constants, a hashed spec, a ``PolicyBinding``, an
``assumption_bound()`` gate, and the pure interval / applicability math.

Versions (ADR-0051 §2: "任何下界变化或新增标的 = 新政策版本"; each version's table is part of
its hashed spec, and every published version stays bindable so old specs replay bit for bit):

- ``1.0.0`` — the table ships **empty** (the authoring batch had no network access to check the
  official archive index). Bound, it never fires for anyone: fail closed by construction.
- ``1.1.0`` (current) — ``BTCUSDT`` and ``ETHUSDT``, each with ``backfill_floor`` = the UTC day of
  the earliest official 1m kline **daily** archive file on ``data.binance.vision`` (ADR-0051 §2:
  "该标的在官方归档站点上最早的 1m K 线日归档所在日"), verified live on 2026-09-30 (UTC) and
  recorded, with source URLs, in ``POLICY_EVIDENCE`` (hashed into the spec) and in
  ``docs/architecture/evidence/binance-spot-listing.md`` §5. The floor is an archive-existence
  lower bound, not an exchange-declared listing date.

Applicability (ADR-0051 §2, all of the following, checked by ``assumption_applies``):

- the venue symbol is a key of the bound version's policy table;
- the episode's first committed revision (``supersedes == ()``) has ``status == listed`` (source
  ``TRADING``) — always true of how ``infrastructure.canonical.listing_rules.derive_chain`` starts
  an episode, checked again here defensively, never assumed;
- that committed first revision is exactly what a fresh derivation from every knowledge-visible raw
  observation still derives as the episode's first revision (no competing head, not
  ``listing_history_diverged``);
- ``backfill_floor <= simulation_time < first_observed_tradable_from``;
- the knowledge axis is untouched: ``knowledge_cutoff`` must not precede the observation's
  ``knowledge_time`` — enforced by the caller only ever offering an already knowledge-visible row.

Never: a second or later episode; any ``suspended`` / ``delisted`` inference; a venue symbol outside
the table; market data (that is ADR-0032, unrelated). Stored revisions, evidence gaps and the
knowledge axis are untouched.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from core.contracts.revision import PointInTimeSpec, PolicyBinding, PolicyRole
from core.domain.base import canonical_json
from infrastructure.contract_version import PHASE1_PUBLICATION_VERSION

__all__ = [
    "ASSUMPTION_BINDING",
    "ASSUMPTION_BINDINGS",
    "ASSUMPTION_BINDING_1_0_0",
    "ASSUMPTION_ID",
    "ASSUMPTION_SPEC",
    "ASSUMPTION_SPEC_1_0_0",
    "ASSUMPTION_VERSION",
    "ASSUMPTION_VERSION_1_0_0",
    "POLICY_EVIDENCE",
    "POLICY_TABLE",
    "POLICY_TABLE_1_0_0",
    "AssumedListingInterval",
    "AssumptionSpecError",
    "assumed_interval",
    "assumption_applies",
    "assumption_bound",
    "backfill_floor_for",
    "bound_binding",
    "policy_table_for",
]

ASSUMPTION_ID: Final = "hlens.listing.observed-state-backfill-assumption"


def _day_boundary(value: datetime) -> bool:
    return (
        value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
        and (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0)
    )


def _check_table(version: str, table: Mapping[str, datetime]) -> None:
    for symbol, floor in table.items():  # pragma: no cover - guards a frozen table's shape
        if not _day_boundary(floor):
            raise ValueError(
                f"{ASSUMPTION_ID}@{version}: backfill_floor of {symbol!r} must be a UTC day "
                "boundary"
            )


def _hash(spec: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


def _binding(version: str, policy_hash: str) -> PolicyBinding:
    return PolicyBinding(
        schema_version=PHASE1_PUBLICATION_VERSION,
        role=PolicyRole.AVAILABILITY,
        policy_id=ASSUMPTION_ID,
        version=version,
        policy_hash=policy_hash,
    )


_ADR_LINE: Final = (
    "ADR-0051 (D-LIST); accepted by Claude PM 2026-09-28 under Raphael's 2026-09-28 "
    "delegation (CLAUDE.md §0, PROJECT_STATUS §6 D-PM-AUTH)"
)
_BINDING_LINE: Final = (
    "only when bound in PointInTimeSpec.availability_bindings (exact version + hash)"
)
_TABLE_SHAPE: Final = "venue_symbol -> backfill_floor (UTC day boundary)"
_TABLE_MEANING: Final = (
    "archive-existence lower bound (earliest official 1m kline daily archive file "
    "for the symbol), not an exchange-declared listing date"
)
_APPLIES_TO: Final[dict[str, Any]] = {
    "episode": "the one episode's first (unsuperseded, supersedes == ()) listing revision only",
    "conditions": [
        "venue_symbol is a key of policy_table",
        "the first committed revision's status is listed (source status TRADING)",
        "the first committed revision is exactly the episode's first revision re-derived from "
        "every knowledge-visible raw observation (no competing head, not "
        "listing_history_diverged)",
        "backfill_floor <= simulation_time < first observed tradable_from",
        "knowledge_cutoff is not earlier than the observation's knowledge_time (the caller "
        "only ever offers an already knowledge-visible committed row)",
    ],
}
_EFFECT: Final[dict[str, Any]] = {
    "assumed_member_interval": "[backfill_floor, first observed tradable_from)",
    "assumed_status": "listed",
    "effective_available_time": "min(stored available_time, backfill_floor), never later than "
    "the stored value",
}
_NEVER: Final[list[str]] = [
    "a suspended or delisted inference",
    "a second or later episode",
    "a venue_symbol outside policy_table",
    "a symbol never locally observed at all",
    "market data (prices / bars): archive event-time availability is ADR-0032, unrelated",
]
_UNCHANGED: Final = "stored revisions, evidence gaps (still listed and bound), the knowledge axis"

# ----------------------------------------------------------------------------------- 1.0.0

ASSUMPTION_VERSION_1_0_0: Final = "1.0.0"

#: The frozen "1.0.0" table: **empty** (no network access was authorized to check the official
#: archive index when it was authored). Bound, it means ``assumption_applies`` never returns True
#: for anyone: fail closed by construction. Kept verbatim so a spec binding 1.0.0 replays exactly.
POLICY_TABLE_1_0_0: Final[Mapping[str, datetime]] = MappingProxyType({})
_check_table(ASSUMPTION_VERSION_1_0_0, POLICY_TABLE_1_0_0)

#: Byte-for-byte the spec published as 1.0.0 (hash
#: ``ec89145ffce3197864e9150a6776963d27819f68c66d644238d3999e5c15b4e9``); never edit.
ASSUMPTION_SPEC_1_0_0: Final[dict[str, Any]] = {
    "rule": ASSUMPTION_ID,
    "version": ASSUMPTION_VERSION_1_0_0,
    "adr": _ADR_LINE,
    "binding": _BINDING_LINE,
    "policy_table": {
        "shape": _TABLE_SHAPE,
        "entries": {
            symbol: floor.isoformat() for symbol, floor in sorted(POLICY_TABLE_1_0_0.items())
        },
        "meaning": _TABLE_MEANING,
        "status": "pending evidence capture in docs/architecture/evidence/binance-spot-listing.md; "
        "empty until an authorized, live archive-index check records a dated lower bound per "
        "symbol (ADR-0051 §2); an empty table means the assumption never applies to anyone",
    },
    "applies_to": _APPLIES_TO,
    "effect": _EFFECT,
    "never": _NEVER,
    "unchanged": _UNCHANGED,
}
ASSUMPTION_SPEC_HASH_1_0_0: Final = _hash(ASSUMPTION_SPEC_1_0_0)
ASSUMPTION_BINDING_1_0_0: Final = _binding(ASSUMPTION_VERSION_1_0_0, ASSUMPTION_SPEC_HASH_1_0_0)

# ----------------------------------------------------------------------------------- 1.1.0

ASSUMPTION_VERSION: Final = "1.1.0"

_ARCHIVE_INDEX: Final = (
    "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
    "?prefix=data/spot/daily/klines/{symbol}/1m/&delimiter=/"
)
_ARCHIVE_FILE: Final = (
    "https://data.binance.vision/data/spot/daily/klines/{symbol}/1m/{symbol}-1m-{day}.zip"
)
_MONTHLY_INDEX: Final = (
    "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
    "?prefix=data/spot/monthly/klines/{symbol}/1m/&delimiter=/"
)
_EXCHANGE_INFO: Final = "https://data-api.binance.vision/api/v3/exchangeInfo?symbol={symbol}"
_FIRST_KLINE: Final = (
    "https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=1m&startTime=0&limit=1"
)

#: venue_symbol -> backfill_floor (UTC day boundary: 00:00:00 UTC), the "1.1.0" policy table
#: (ADR-0051 §2): the UTC day of the lexicographically first ``<SYM>-1m-YYYY-MM-DD.zip`` key under
#: ``data/spot/daily/klines/<SYM>/1m/`` of the official archive bucket (an S3 listing is returned
#: in ascending key order, so the first key of an un-markered listing is the earliest day).
#: Verified live 2026-09-30 (UTC); see ``POLICY_EVIDENCE``. Any change or addition = a new version.
POLICY_TABLE: Final[dict[str, datetime]] = {
    "BTCUSDT": datetime(2017, 8, 17, tzinfo=UTC),
    "ETHUSDT": datetime(2017, 8, 17, tzinfo=UTC),
}
_check_table(ASSUMPTION_VERSION, POLICY_TABLE)


def _evidence(symbol: str, *, etag: str, last_modified: str, open_ms: int) -> dict[str, Any]:
    day = POLICY_TABLE[symbol].date().isoformat()
    return {
        "verified_at_utc": "2026-09-30T18:19:55Z/2026-09-30T18:20:07Z",
        "archive_index_url": _ARCHIVE_INDEX.format(symbol=symbol),
        "earliest_daily_archive_file": _ARCHIVE_FILE.format(symbol=symbol, day=day),
        "earliest_daily_archive_file_etag": etag,
        "earliest_daily_archive_file_last_modified_utc": last_modified,
        "monthly_index_url": _MONTHLY_INDEX.format(symbol=symbol),
        "earliest_monthly_archive_month": day[:7],
        "first_kline_url": _FIRST_KLINE.format(symbol=symbol),
        "first_kline_open_time_ms": open_ms,
        "first_kline_open_time_utc": datetime.fromtimestamp(open_ms / 1000, tz=UTC).isoformat(),
        "exchange_info_url": _EXCHANGE_INFO.format(symbol=symbol),
        "exchange_info_status_at_verification": "TRADING",
        "note": "backfill_floor is the UTC day of the earliest daily archive file (ADR-0051 §2), "
        "which starts before the first kline's open time; api.binance.com refused this "
        "verification from a restricted location, so exchangeInfo / klines were read from the "
        "market-data-only base data-api.binance.vision (ADR-0022; not claimed identical to it)",
    }


#: Per-symbol provenance of the "1.1.0" table (hashed into the spec). Values as returned by the
#: live read-only checks on 2026-09-30 (UTC); no market data is stored, only these facts.
POLICY_EVIDENCE: Final[dict[str, dict[str, Any]]] = {
    "BTCUSDT": _evidence(
        "BTCUSDT",
        etag="014a8d177da3bf9839c293f341804d26",
        last_modified="2023-07-18T04:11:20Z",
        open_ms=1502942400000,
    ),
    "ETHUSDT": _evidence(
        "ETHUSDT",
        etag="174be4804f5a23dfe3d93fa8ff98af83",
        last_modified="2023-07-18T08:55:23Z",
        open_ms=1502942400000,
    ),
}
if set(POLICY_EVIDENCE) != set(POLICY_TABLE):  # pragma: no cover - guards a frozen table
    raise ValueError(f"{ASSUMPTION_ID}@{ASSUMPTION_VERSION}: every entry needs its evidence")

ASSUMPTION_SPEC: Final[dict[str, Any]] = {
    "rule": ASSUMPTION_ID,
    "version": ASSUMPTION_VERSION,
    "adr": _ADR_LINE,
    "binding": _BINDING_LINE,
    "supersedes": f"{ASSUMPTION_ID}@{ASSUMPTION_VERSION_1_0_0} (empty table; still bindable)",
    "policy_table": {
        "shape": _TABLE_SHAPE,
        "entries": {symbol: floor.isoformat() for symbol, floor in sorted(POLICY_TABLE.items())},
        "meaning": _TABLE_MEANING,
        "status": "verified by a live, read-only archive-index check (ADR-0100 item 5); evidence "
        "recorded per symbol below and in docs/architecture/evidence/binance-spot-listing.md §5",
        "evidence": {symbol: POLICY_EVIDENCE[symbol] for symbol in sorted(POLICY_EVIDENCE)},
    },
    "applies_to": _APPLIES_TO,
    "effect": _EFFECT,
    "never": _NEVER,
    "unchanged": _UNCHANGED,
}
ASSUMPTION_SPEC_HASH: Final = _hash(ASSUMPTION_SPEC)
ASSUMPTION_BINDING: Final = _binding(ASSUMPTION_VERSION, ASSUMPTION_SPEC_HASH)

# ------------------------------------------------------------------------------- registry

#: Every published version, by version string: each stays bindable (replayable) forever.
ASSUMPTION_BINDINGS: Final[Mapping[str, PolicyBinding]] = MappingProxyType(
    {
        ASSUMPTION_VERSION_1_0_0: ASSUMPTION_BINDING_1_0_0,
        ASSUMPTION_VERSION: ASSUMPTION_BINDING,
    }
)


class AssumptionSpecError(ValueError):
    """The spec names the assumption policy with another version or hash (fail closed)."""


def bound_binding(spec: PointInTimeSpec) -> PolicyBinding | None:
    """The one published version of this assumption ``spec`` binds, or ``None`` when unbound.

    Naming the policy more than once, or with a version / hash that is not exactly a published
    one (``ASSUMPTION_BINDINGS``), is refused.
    """
    named = [b for b in spec.availability_bindings if b.policy_id == ASSUMPTION_ID]
    if not named:
        return None
    if len(named) != 1 or ASSUMPTION_BINDINGS.get(named[0].version) != named[0]:
        raise AssumptionSpecError(
            f"{ASSUMPTION_ID} must be bound exactly once as one of "
            f"{sorted(ASSUMPTION_BINDINGS)} with its exact hash"
        )
    return named[0]


def assumption_bound(spec: PointInTimeSpec) -> bool:
    """Whether ``spec`` binds a published version; another version / hash is refused."""
    return bound_binding(spec) is not None


def policy_table_for(binding: PolicyBinding) -> Mapping[str, datetime]:
    """The policy table of the published version ``binding`` names (exact hash required)."""
    if binding == ASSUMPTION_BINDING_1_0_0:
        return POLICY_TABLE_1_0_0
    if binding == ASSUMPTION_BINDING:
        return POLICY_TABLE  # read at call time (the current version's module-level table)
    raise AssumptionSpecError(f"{ASSUMPTION_ID}: {binding.version} is not a published version")


def backfill_floor_for(venue_symbol: str, binding: PolicyBinding | None = None) -> datetime | None:
    """The bound version's table lookup (current version when ``binding`` is omitted) —
    ``None`` when ``venue_symbol`` is outside it."""
    table = policy_table_for(ASSUMPTION_BINDING if binding is None else binding)
    return table.get(venue_symbol)


@dataclass(frozen=True, slots=True)
class AssumedListingInterval:
    """The one backward extension this assumption can give an episode's first revision."""

    venue_symbol: str
    backfill_floor: datetime
    first_observed_from: datetime
    effective_available_time: datetime
    #: The published version whose table gave ``backfill_floor``.
    binding: PolicyBinding = ASSUMPTION_BINDING


def assumption_applies(
    *,
    backfill_floor: datetime | None,
    simulation_time: datetime,
    first_observed_from: datetime,
    first_status: str,
    chain_first_revision_id: str | None,
    committed_first_revision_id: str,
) -> bool:
    """Every ADR-0051 §2 condition at once (pure; the caller supplies each fact once)."""
    if backfill_floor is None:
        return False
    if backfill_floor >= first_observed_from:
        return False
    if not (backfill_floor <= simulation_time < first_observed_from):
        return False
    if first_status != "listed":
        return False
    if chain_first_revision_id != committed_first_revision_id:
        return False
    return True


def assumed_interval(
    *,
    venue_symbol: str,
    backfill_floor: datetime,
    first_observed_from: datetime,
    stored_available_time: datetime,
    binding: PolicyBinding = ASSUMPTION_BINDING,
) -> AssumedListingInterval:
    """The assumed interval and effective available time (call only once ``assumption_applies``)."""
    if binding not in ASSUMPTION_BINDINGS.values():
        raise AssumptionSpecError(f"{ASSUMPTION_ID}: {binding.version} is not a published version")
    if backfill_floor >= first_observed_from:
        raise AssumptionSpecError(
            f"{ASSUMPTION_ID}: backfill_floor must strictly precede the first observed "
            "tradable_from"
        )
    effective = min(stored_available_time, backfill_floor)
    if effective > stored_available_time:  # pragma: no cover - min() cannot produce this
        raise AssumptionSpecError(
            f"{ASSUMPTION_ID}: effective available_time moved later than stored"
        )
    return AssumedListingInterval(
        venue_symbol=venue_symbol,
        backfill_floor=backfill_floor,
        first_observed_from=first_observed_from,
        effective_available_time=effective,
        binding=binding,
    )

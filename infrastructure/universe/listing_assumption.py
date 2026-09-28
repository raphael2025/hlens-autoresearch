"""Listing history "observed-state backfill" assumption (ADR-0051 §2, D-LIST; Claude PM accepted
2026-09-28 under Raphael's 2026-09-28 delegation).

Stored listing revisions stay exactly as ADR-0029 derives them: ``tradable_from`` is this
installation's own first observation of ``TRADING``, never an exchange-declared listing date, and
before that first observation a point-in-time read is ``no_visible_listing`` (ADR-0029 §3). A
``PointInTimeSpec`` that **binds**
``hlens.listing.observed-state-backfill-assumption@1.0.0`` in its ``availability_bindings`` lets a
listing read additionally treat, for **one venue symbol** named in this policy's table and **only**
its episode's first (unsuperseded) revision, the half-open span
``[backfill_floor, first_observed_tradable_from)`` as an assumed ``listed`` member interval, with
that first revision's effective ``available_time = min(stored, backfill_floor)`` (never later than
stored). This module mirrors ``infrastructure/pit/assumption.py`` (ADR-0032, its declared
structural template): identity constants, a hashed spec, a ``PolicyBinding``, an
``assumption_bound()`` gate, and the pure interval / applicability math. Unlike ADR-0032's archive
latency (a documented, static protocol fact), each policy-table entry's ``backfill_floor`` must be
sourced from a live check of the official archive index (ADR-0051 §2: "该标的在官方归档站点上最早的
1m K 线日归档所在日") recorded in ``docs/architecture/evidence/binance-spot-listing.md`` — network
access this module's authoring batch did not have. ``POLICY_TABLE`` therefore ships **empty**: no
symbol is in it, so the assumption, even when bound, never fires for anyone (fail closed by
construction, exactly like leaving the assumption unbound). Populating a verified
``(BTCUSDT, ETHUSDT)`` entry is a follow-up that only ever adds table rows under the same
``1.0.0`` identity's declared shape — never invented here.

Applicability (ADR-0051 §2, all of the following, checked by ``assumption_applies``):

- the venue symbol is a key of ``POLICY_TABLE``;
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
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from core.contracts.revision import PointInTimeSpec, PolicyBinding, PolicyRole
from core.domain.base import canonical_json
from infrastructure.contract_version import PHASE1_PUBLICATION_VERSION

__all__ = [
    "ASSUMPTION_BINDING",
    "ASSUMPTION_ID",
    "ASSUMPTION_SPEC",
    "ASSUMPTION_VERSION",
    "POLICY_TABLE",
    "AssumedListingInterval",
    "AssumptionSpecError",
    "assumed_interval",
    "assumption_applies",
    "assumption_bound",
    "backfill_floor_for",
]

ASSUMPTION_ID: Final = "hlens.listing.observed-state-backfill-assumption"
ASSUMPTION_VERSION: Final = "1.0.0"

#: venue_symbol -> backfill_floor (UTC day boundary: 00:00:00 UTC), the "1.0.0" policy table
#: (ADR-0051 §2). **Empty pending evidence**: no network access was authorized to check the
#: official archive index for the earliest 1m kline daily file of BTCUSDT / ETHUSDT and record it
#: in docs/architecture/evidence/binance-spot-listing.md (ADR-0051 §2, "下界的取值规则"). An empty
#: table means ``assumption_applies`` never returns True for anyone: fail closed by construction,
#: not a guess. Any addition is a new fact for this same "1.0.0" table (a *wrong* date is worse
#: than an absent one), never a new version by itself unless the shape of the rule changes.
POLICY_TABLE: Final[dict[str, datetime]] = {}


def _day_boundary(value: datetime) -> bool:
    return (
        value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
        and (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0)
    )


for _symbol, _floor in POLICY_TABLE.items():  # pragma: no cover - guards a frozen table's shape
    if not _day_boundary(_floor):
        raise ValueError(
            f"{ASSUMPTION_ID}: backfill_floor of {_symbol!r} must be a UTC day boundary"
        )

ASSUMPTION_SPEC: Final[dict[str, Any]] = {
    "rule": ASSUMPTION_ID,
    "version": ASSUMPTION_VERSION,
    "adr": "ADR-0051 (D-LIST); accepted by Claude PM 2026-09-28 under Raphael's 2026-09-28 "
    "delegation (CLAUDE.md §0, PROJECT_STATUS §6 D-PM-AUTH)",
    "binding": "only when bound in PointInTimeSpec.availability_bindings (exact version + hash)",
    "policy_table": {
        "shape": "venue_symbol -> backfill_floor (UTC day boundary)",
        "entries": {symbol: floor.isoformat() for symbol, floor in sorted(POLICY_TABLE.items())},
        "meaning": "archive-existence lower bound (earliest official 1m kline daily archive file "
        "for the symbol), not an exchange-declared listing date",
        "status": "pending evidence capture in docs/architecture/evidence/binance-spot-listing.md; "
        "empty until an authorized, live archive-index check records a dated lower bound per "
        "symbol (ADR-0051 §2); an empty table means the assumption never applies to anyone",
    },
    "applies_to": {
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
    },
    "effect": {
        "assumed_member_interval": "[backfill_floor, first observed tradable_from)",
        "assumed_status": "listed",
        "effective_available_time": "min(stored available_time, backfill_floor), never later than "
        "the stored value",
    },
    "never": [
        "a suspended or delisted inference",
        "a second or later episode",
        "a venue_symbol outside policy_table",
        "a symbol never locally observed at all",
        "market data (prices / bars): archive event-time availability is ADR-0032, unrelated",
    ],
    "unchanged": "stored revisions, evidence gaps (still listed and bound), the knowledge axis",
}
ASSUMPTION_SPEC_HASH: Final = hashlib.sha256(
    canonical_json(ASSUMPTION_SPEC).encode("utf-8")
).hexdigest()
ASSUMPTION_BINDING: Final = PolicyBinding(
    schema_version=PHASE1_PUBLICATION_VERSION,
    role=PolicyRole.AVAILABILITY,
    policy_id=ASSUMPTION_ID,
    version=ASSUMPTION_VERSION,
    policy_hash=ASSUMPTION_SPEC_HASH,
)


class AssumptionSpecError(ValueError):
    """The spec names the assumption policy with another version or hash (fail closed)."""


def assumption_bound(spec: PointInTimeSpec) -> bool:
    """Whether ``spec`` binds exactly this assumption; another version / hash is refused."""
    named = [b for b in spec.availability_bindings if b.policy_id == ASSUMPTION_ID]
    if not named:
        return False
    if named != [ASSUMPTION_BINDING]:
        raise AssumptionSpecError(
            f"{ASSUMPTION_ID} must be bound exactly once as {ASSUMPTION_VERSION} with its hash"
        )
    return True


def backfill_floor_for(venue_symbol: str) -> datetime | None:
    """This policy's table lookup — ``None`` when ``venue_symbol`` is outside it."""
    return POLICY_TABLE.get(venue_symbol)


@dataclass(frozen=True, slots=True)
class AssumedListingInterval:
    """The one backward extension this assumption can give an episode's first revision."""

    venue_symbol: str
    backfill_floor: datetime
    first_observed_from: datetime
    effective_available_time: datetime


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
) -> AssumedListingInterval:
    """The assumed interval and effective available time (call only once ``assumption_applies``)."""
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
    )

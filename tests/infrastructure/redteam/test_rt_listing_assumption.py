"""G2-style red team: the ADR-0051 listing backfill assumption (D-LIST) must never make a symbol
appear that this installation never locally observed as ``TRADING``, and must never leak assumed
membership into a period the real history shows suspended ("delisted" in the everyday sense ADR-
0051 §2 forbids inferring: "1.0.0 永不回填暂停或下架").

Same harness as ``tests/infrastructure/canonical/test_listings.py`` / ``test_listings_assumption``:
real collector checkpoints (mock venue), real snapshot store, real SQLite catalog, the real
``ListingDeriver``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.contracts.revision import PointInTimeSpec, PolicyBinding, PolicyRole
from infrastructure.canonical.listings import ListingPointInTime, UnconstructibleReason
from infrastructure.universe import listing_assumption as backfill
from tests.infrastructure.revision import exchange_info_support as xs
from tests.infrastructure.revision.exchange_info_support import KNOWLEDGE, T1, T2, TRADING, Harness

LATE: datetime = KNOWLEDGE + timedelta(days=30)
FLOOR: datetime = datetime(2026, 8, 1, tzinfo=UTC)
BTC_HALT: dict[str, str | None] = {"BTCUSDT": "HALT", "ETHUSDT": "TRADING"}
NEVER_TRADING: dict[str, str | None] = {"BTCUSDT": "HALT", "ETHUSDT": None}


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    with xs.harness(tmp_path) as opened:
        yield opened


def _binding(
    role: PolicyRole, policy_id: str, *, version: str = "1.0.0", digit: str = "a"
) -> PolicyBinding:
    return PolicyBinding(role=role, policy_id=policy_id, version=version, policy_hash=digit * 64)


def _bound_pit(simulation: datetime, cutoff: datetime) -> PointInTimeSpec:
    base = _binding(PolicyRole.AVAILABILITY, "binance.spot.exchange-info-publication")
    pit_binding = _binding(PolicyRole.POINT_IN_TIME, "hlens.pit.maximal-head", digit="b")
    precedence = _binding(PolicyRole.PRECEDENCE, "binance.spot.listing-observation", digit="c")
    parser = _binding(PolicyRole.PARSER, "binance.spot.listing-status", digit="d")
    return PointInTimeSpec(
        name="hlens.pit.maximal-head",
        version="1.0.0",
        simulation_time=simulation,
        knowledge_cutoff=cutoff,
        snapshot_bindings={"canonical.instrument_listings": "1"},
        point_in_time_binding=pit_binding,
        availability_bindings=(base, backfill.ASSUMPTION_BINDING),
        precedence_bindings=(precedence,),
        parser_bindings=(parser,),
    )


def at(
    h: Harness, simulation: datetime, cutoff: datetime, pit: PointInTimeSpec
) -> ListingPointInTime:
    deriver = h.deriver()
    try:
        return deriver.listing_at("BTCUSDT", simulation, cutoff, pit=pit)
    finally:
        deriver.close()


def derive(h: Harness) -> None:
    deriver = h.deriver()
    try:
        deriver.derive()
    finally:
        deriver.close()


def _refused(point: ListingPointInTime) -> None:
    assert not point.constructible
    assert point.reason == UnconstructibleReason.NO_VISIBLE_LISTING
    assert point.assumed is False
    assert point.assumption is None
    assert point.listing is None


# ------------------------------------------------------------- never locally observed at all


def test_a_symbol_never_observed_by_this_installation_is_never_assumed(
    monkeypatch: pytest.MonkeyPatch, h: Harness
) -> None:
    """Nothing was ever collected: the raw and listing tables both exist and are both empty."""
    monkeypatch.setattr(backfill, "POLICY_TABLE", {"BTCUSDT": FLOOR})
    _refused(at(h, FLOOR, LATE, _bound_pit(FLOOR, LATE)))


def test_a_symbol_only_ever_observed_suspended_never_traded_is_never_assumed(
    monkeypatch: pytest.MonkeyPatch, h: Harness
) -> None:
    """HALT before any TRADING observation creates no episode at all (ADR-0029 §2): there is
    nothing for the assumption to extend, no matter how early ``backfill_floor`` is."""
    monkeypatch.setattr(backfill, "POLICY_TABLE", {"BTCUSDT": FLOOR})
    h.observe("snap-1", NEVER_TRADING, T1)
    derive(h)
    _refused(at(h, FLOOR, LATE, _bound_pit(FLOOR, LATE)))
    _refused(at(h, T1, LATE, _bound_pit(T1, LATE)))  # even at the HALT observation itself


def test_a_symbol_outside_the_real_shipped_policy_table_is_never_assumed(h: Harness) -> None:
    """No monkeypatch: the real, currently-empty ``POLICY_TABLE`` (ADR-0051 §2, evidence pending)
    cannot let even a fully-observed, cleanly-derived BTCUSDT episode appear via the assumption."""
    h.observe("snap-1", TRADING, T1)
    derive(h)
    _refused(at(h, FLOOR, LATE, _bound_pit(FLOOR, LATE)))


# ------------------------------------------------------------- suspended / "delisted" periods


def test_the_assumption_cannot_extend_into_a_suspended_period(
    monkeypatch: pytest.MonkeyPatch, h: Harness
) -> None:
    """TRADING at T1, HALT at T2: the assumption may (legitimately) extend [floor, T1) backward,
    but querying inside [T2, ...) — the real, currently-suspended tail — must never come back
    ``assumed`` or ``tradable``, whatever the policy table says."""
    monkeypatch.setattr(backfill, "POLICY_TABLE", {"BTCUSDT": FLOOR})
    h.observe("snap-1", TRADING, T1)
    h.observe("snap-2", BTC_HALT, T2, server_time=2)
    derive(h)
    after_halt = T2 + timedelta(hours=1)
    point = at(h, after_halt, LATE, _bound_pit(after_halt, LATE))
    assert point.constructible  # a real, observed answer: not a visibility gap
    assert point.tradable is False  # genuinely suspended
    assert point.assumed is False  # never presented as an assumption's doing
    assert point.assumption is None


def test_a_late_snapshot_that_diverges_the_first_revision_blocks_the_assumption_too(
    monkeypatch: pytest.MonkeyPatch, h: Harness
) -> None:
    """A late, earlier TRADING observation makes the first revision's identity ambiguous
    (``listing_history_diverged`` / a second episode, ADR-0029 #5): the assumption must fail
    closed exactly like every other read does, never guess which "first" revision is real."""
    monkeypatch.setattr(backfill, "POLICY_TABLE", {"BTCUSDT": FLOOR})
    h.collect("snap-1", TRADING, T1)
    h.collect("snap-2", TRADING, T2, server_time=2)
    h.ingest("snap-2")
    derive(h)  # commits an episode whose first revision is T2's observation
    h.ingest("snap-1")  # the earlier TRADING observation arrives late: T2's revision diverges
    derive(h)
    # Both episodes are now known; MULTIPLE_EPISODES governs at a time both are available, well
    # after T1. Before either is available the assumption still needs a single, undiverged first
    # revision — the re-derived chain no longer agrees with what was committed as "first".
    point = at(h, FLOOR, LATE, _bound_pit(FLOOR, LATE))
    assert point.assumed is False
    assert point.reason in {
        UnconstructibleReason.NO_VISIBLE_LISTING,
        UnconstructibleReason.MULTIPLE_EPISODES,
    }

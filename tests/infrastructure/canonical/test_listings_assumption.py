"""ADR-0051 listing backfill assumption wired into ``ListingDeriver.listing_at`` (D-LIST, first
implementation phase: only the ``pit`` parameter and the assumed-answer branch, not the universe
builder or dataset manifest — those are the second phase).

Same harness as ``test_listings.py``: real collector checkpoints (mock venue), real snapshot
store, real SQLite catalog, the real ``ListingDeriver``. ``POLICY_TABLE`` ships empty (no network
access was authorized to gather the real ``backfill_floor`` evidence), so it is monkeypatched here
to exercise the "in table" branch; ``ASSUMPTION_BINDING`` itself (the identity a spec must bind) is
the real, unpatched module constant throughout.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.contracts.revision import PointInTimeSpec, PolicyBinding, PolicyRole
from core.contracts.universe import TradableInterval
from infrastructure.canonical.listings import ListingPointInTime, UnconstructibleReason
from infrastructure.universe import listing_assumption as backfill
from tests.infrastructure.revision import exchange_info_support as xs
from tests.infrastructure.revision.exchange_info_support import (
    KNOWLEDGE,
    LISTINGS,
    T1,
    T2,
    TRADING,
    Harness,
)

LATE: datetime = KNOWLEDGE + timedelta(days=30)
FLOOR: datetime = datetime(2026, 8, 1, tzinfo=UTC)
BTC_HALT: dict[str, str | None] = {"BTCUSDT": "HALT", "ETHUSDT": "TRADING"}


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    with xs.harness(tmp_path) as opened:
        yield opened


def _binding(
    role: PolicyRole, policy_id: str, *, version: str = "1.0.0", digit: str = "a"
) -> PolicyBinding:
    return PolicyBinding(role=role, policy_id=policy_id, version=version, policy_hash=digit * 64)


def _pit(simulation: datetime, cutoff: datetime, *, assumed: bool) -> PointInTimeSpec:
    """A minimal, otherwise-lawful spec; ``assumed`` decides whether it binds D-LIST."""
    base = _binding(PolicyRole.AVAILABILITY, "binance.spot.exchange-info-publication")
    availability = (base, backfill.ASSUMPTION_BINDING) if assumed else (base,)
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
        availability_bindings=availability,
        precedence_bindings=(precedence,),
        parser_bindings=(parser,),
    )


def at(
    h: Harness,
    simulation: datetime,
    cutoff: datetime = LATE,
    *,
    pit: PointInTimeSpec | None = None,
) -> ListingPointInTime:
    deriver = h.deriver()
    try:
        return deriver.listing_at("BTCUSDT", simulation, cutoff, pit=pit)
    finally:
        deriver.close()


def derive(h: Harness) -> Any:
    deriver = h.deriver()
    try:
        return deriver.derive()
    finally:
        deriver.close()


# --------------------------------------------------------------------------- unbound: unchanged


def test_omitting_pit_is_byte_for_byte_the_old_read(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backfill, "POLICY_TABLE", {"BTCUSDT": FLOOR})
    h.observe("snap-1", TRADING, T1)
    derive(h)
    midpoint = FLOOR + (T1 - FLOOR) / 2
    without_pit = at(h, midpoint, LATE)
    with_unbound_pit = at(h, midpoint, LATE, pit=_pit(midpoint, LATE, assumed=False))
    for point in (without_pit, with_unbound_pit):
        assert not point.constructible
        assert point.reason == UnconstructibleReason.NO_VISIBLE_LISTING
        assert point.assumed is False
        assert point.assumption is None
    # And the genuinely-available case is unaffected too.
    real = at(h, T1, LATE)
    real_with_pit = at(h, T1, LATE, pit=_pit(T1, LATE, assumed=True))
    assert real.constructible and real_with_pit.constructible
    assert real.listing == real_with_pit.listing
    assert real.tradable == real_with_pit.tradable is True
    assert real.assumed is False and real_with_pit.assumed is False  # a real answer, not assumed


def test_a_policy_table_entry_for_another_symbol_does_not_apply_here(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backfill, "POLICY_TABLE", {"ETHUSDT": FLOOR})  # BTCUSDT is queried below
    h.observe("snap-1", TRADING, T1)
    derive(h)
    point = at(h, FLOOR, LATE, pit=_pit(FLOOR, LATE, assumed=True))
    assert not point.constructible
    assert point.reason == UnconstructibleReason.NO_VISIBLE_LISTING
    assert point.assumed is False


# --------------------------------------------------------------------------- bound: the assumption


def test_the_assumption_extends_the_first_revision_back_to_the_floor(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backfill, "POLICY_TABLE", {"BTCUSDT": FLOOR})
    h.observe("snap-1", TRADING, T1)
    derive(h)
    pit = _pit(FLOOR, LATE, assumed=True)

    at_floor = at(h, FLOOR, LATE, pit=pit)
    assert at_floor.constructible and at_floor.tradable is True
    assert at_floor.assumed is True
    assert at_floor.assumption is not None
    assert (at_floor.assumption.backfill_floor, at_floor.assumption.first_observed_from) == (
        FLOOR,
        T1,
    )
    assert at_floor.assumption.effective_available_time == FLOOR  # min(T1, FLOOR), never later
    # The underlying revision itself is the real, unmodified first revision (ADR-0051 §1: storage
    # and default behaviour never change).
    assert at_floor.listing is not None
    assert at_floor.listing.episode.tradable_from == T1
    open_interval = (TradableInterval(tradable_from=T1, tradable_until=None),)
    assert at_floor.listing.tradable_intervals == open_interval

    midpoint = FLOOR + (T1 - FLOOR) / 2
    at_mid = at(h, midpoint, LATE, pit=pit)
    assert at_mid.constructible and at_mid.assumed is True

    at_first_observation = at(h, T1, LATE, pit=pit)
    assert at_first_observation.constructible and at_first_observation.assumed is False  # real now

    just_before_floor = at(h, FLOOR - timedelta(microseconds=1), LATE, pit=pit)
    assert not just_before_floor.constructible
    assert just_before_floor.reason == UnconstructibleReason.NO_VISIBLE_LISTING
    assert just_before_floor.assumed is False


def test_a_knowledge_cutoff_before_the_observation_is_known_still_refuses(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The knowledge axis is untouched by the assumption (ADR-0051 §2): a cutoff before the
    observation was locally known must still be refused, exactly as ADR-0029 already requires."""
    monkeypatch.setattr(backfill, "POLICY_TABLE", {"BTCUSDT": FLOOR})
    h.observe("snap-1", TRADING, T1)
    derive(h)
    [row] = [r for r in h.rows(LISTINGS.table) if r["symbol"] == "BTC-USDT"]
    known = row["knowledge_time"]

    too_early = known - xs.MS
    point = at(h, FLOOR, too_early, pit=_pit(FLOOR, too_early, assumed=True))
    assert not point.constructible
    assert point.reason == UnconstructibleReason.NO_VISIBLE_LISTING
    assert point.assumed is False

    just_known = at(h, FLOOR, known, pit=_pit(FLOOR, known, assumed=True))
    assert just_known.constructible and just_known.assumed is True


def test_suspension_after_the_first_observation_is_never_assumed(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A period the real history shows suspended must read suspended even when the assumption is
    bound: the assumption only ever extends the episode's first interval backward."""
    monkeypatch.setattr(backfill, "POLICY_TABLE", {"BTCUSDT": FLOOR})
    h.observe("snap-1", TRADING, T1)
    h.observe("snap-2", BTC_HALT, T2, server_time=2)
    derive(h)
    pit = _pit(T2 + timedelta(minutes=1), LATE, assumed=True)
    after_halt = at(h, T2 + timedelta(minutes=1), LATE, pit=pit)
    assert after_halt.constructible
    assert after_halt.tradable is False
    assert after_halt.assumed is False  # a real (suspended) answer, never an assumed one

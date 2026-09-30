"""ADR-0051 listing backfill assumption (D-LIST): identity, ``assumption_bound`` and the pure
applicability / interval math of ``infrastructure/universe/listing_assumption.py``.

No catalog, no clock, no I/O — everything here is a pure function or a frozen constant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.contracts.revision import PointInTimeSpec, PolicyBinding, PolicyRole
from core.domain.base import FrozenMapping
from infrastructure.universe import listing_assumption as backfill

FLOOR: datetime = datetime(2017, 8, 17, tzinfo=UTC)
FIRST_OBSERVED: datetime = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
US: timedelta = timedelta(microseconds=1)


def _binding(
    role: PolicyRole, policy_id: str, *, version: str = "1.0.0", digit: str = "a"
) -> PolicyBinding:
    return PolicyBinding(role=role, policy_id=policy_id, version=version, policy_hash=digit * 64)


def _spec(*extra_availability: PolicyBinding) -> PointInTimeSpec:
    """A minimal, otherwise-lawful spec; ``extra_availability`` is appended, never replaces."""
    base = _binding(PolicyRole.AVAILABILITY, "binance.spot.exchange-info-publication")
    pit_binding = _binding(PolicyRole.POINT_IN_TIME, "hlens.pit.maximal-head", digit="b")
    precedence = _binding(PolicyRole.PRECEDENCE, "binance.spot.listing-observation", digit="c")
    parser = _binding(PolicyRole.PARSER, "binance.spot.listing-status", digit="d")
    return PointInTimeSpec(
        name="hlens.pit.maximal-head",
        version="1.0.0",
        simulation_time=FIRST_OBSERVED,
        knowledge_cutoff=FIRST_OBSERVED + timedelta(days=1),
        snapshot_bindings=FrozenMapping({"canonical.instrument_listings": "1"}),
        point_in_time_binding=pit_binding,
        availability_bindings=(base, *extra_availability),
        precedence_bindings=(precedence,),
        parser_bindings=(parser,),
    )


# --------------------------------------------------------------------------- identity


def test_identity_is_fixed_by_the_adr() -> None:
    assert backfill.ASSUMPTION_ID == "hlens.listing.observed-state-backfill-assumption"
    assert backfill.ASSUMPTION_VERSION == "1.1.0"
    assert backfill.ASSUMPTION_BINDING.role is PolicyRole.AVAILABILITY
    assert backfill.ASSUMPTION_BINDING.policy_id == backfill.ASSUMPTION_ID
    assert backfill.ASSUMPTION_BINDING.version == backfill.ASSUMPTION_VERSION
    assert backfill.ASSUMPTION_SPEC["rule"] == backfill.ASSUMPTION_ID
    assert backfill.ASSUMPTION_SPEC["version"] == backfill.ASSUMPTION_VERSION


def test_every_published_version_stays_bindable() -> None:
    assert dict(backfill.ASSUMPTION_BINDINGS) == {
        "1.0.0": backfill.ASSUMPTION_BINDING_1_0_0,
        "1.1.0": backfill.ASSUMPTION_BINDING,
    }
    assert backfill.ASSUMPTION_BINDING_1_0_0.policy_hash != backfill.ASSUMPTION_BINDING.policy_hash


def test_the_1_0_0_spec_and_hash_are_frozen_for_replay() -> None:
    """A spec bound to 1.0.0 before the table was populated must replay bit for bit."""
    assert backfill.ASSUMPTION_BINDING_1_0_0.version == "1.0.0"
    assert backfill.ASSUMPTION_SPEC_1_0_0["version"] == "1.0.0"
    assert (
        backfill.ASSUMPTION_BINDING_1_0_0.policy_hash
        == "ec89145ffce3197864e9150a6776963d27819f68c66d644238d3999e5c15b4e9"
    )


def test_the_1_0_0_policy_table_stays_empty() -> None:
    """1.0.0 was published before any live archive-index check (ADR-0051 §2): its table is empty
    and binding it can never make anyone appear, exactly as conservative as leaving it unbound."""
    assert dict(backfill.POLICY_TABLE_1_0_0) == {}
    assert backfill.backfill_floor_for("BTCUSDT", backfill.ASSUMPTION_BINDING_1_0_0) is None
    assert backfill.backfill_floor_for("ETHUSDT", backfill.ASSUMPTION_BINDING_1_0_0) is None
    assert backfill.ASSUMPTION_SPEC_1_0_0["policy_table"]["entries"] == {}


def test_the_1_1_0_policy_table_holds_the_verified_archive_floors() -> None:
    """Earliest official 1m kline daily archive day of each first-slice symbol, verified live on
    2026-09-30 (ADR-0100 item 5); each entry carries its provenance in the hashed spec."""
    expected = {"BTCUSDT": FLOOR, "ETHUSDT": FLOOR}
    assert backfill.POLICY_TABLE == expected
    assert backfill.backfill_floor_for("BTCUSDT") == FLOOR
    assert backfill.backfill_floor_for("ETHUSDT", backfill.ASSUMPTION_BINDING) == FLOOR
    assert backfill.backfill_floor_for("SOLUSDT") is None
    entries = backfill.ASSUMPTION_SPEC["policy_table"]["entries"]
    assert entries == {symbol: floor.isoformat() for symbol, floor in expected.items()}
    evidence = backfill.ASSUMPTION_SPEC["policy_table"]["evidence"]
    assert set(evidence) == set(expected)
    for symbol, item in evidence.items():
        assert item["earliest_daily_archive_file"].endswith(f"/{symbol}-1m-2017-08-17.zip")
        assert item["first_kline_open_time_utc"] == "2017-08-17T04:00:00+00:00"
        assert item["exchange_info_status_at_verification"] == "TRADING"
        assert item["verified_at_utc"].startswith("2026-09-30T")


def test_an_unpublished_binding_has_no_table() -> None:
    wrong = backfill.ASSUMPTION_BINDING.model_copy(update={"policy_hash": "0" * 64})
    with pytest.raises(backfill.AssumptionSpecError):
        backfill.policy_table_for(wrong)
    with pytest.raises(backfill.AssumptionSpecError):
        backfill.backfill_floor_for("BTCUSDT", wrong)


# --------------------------------------------------------------------------- assumption_bound


def test_an_unbound_spec_is_not_bound() -> None:
    assert backfill.assumption_bound(_spec()) is False


def test_a_spec_binding_the_exact_binding_is_bound() -> None:
    assert backfill.assumption_bound(_spec(backfill.ASSUMPTION_BINDING)) is True
    assert backfill.bound_binding(_spec(backfill.ASSUMPTION_BINDING)) == backfill.ASSUMPTION_BINDING


def test_a_spec_binding_the_frozen_1_0_0_version_is_still_bound() -> None:
    spec = _spec(backfill.ASSUMPTION_BINDING_1_0_0)
    assert backfill.assumption_bound(spec) is True
    assert backfill.bound_binding(spec) == backfill.ASSUMPTION_BINDING_1_0_0


def test_a_version_with_another_versions_hash_is_refused() -> None:
    crossed = backfill.ASSUMPTION_BINDING.model_copy(
        update={"policy_hash": backfill.ASSUMPTION_BINDING_1_0_0.policy_hash}
    )
    with pytest.raises(backfill.AssumptionSpecError):
        backfill.assumption_bound(_spec(crossed))


def test_binding_two_versions_at_once_is_refused() -> None:
    both = _spec(backfill.ASSUMPTION_BINDING_1_0_0, backfill.ASSUMPTION_BINDING)
    with pytest.raises(backfill.AssumptionSpecError):
        backfill.assumption_bound(both)


def test_a_wrong_version_is_refused_not_silently_ignored() -> None:
    wrong = backfill.ASSUMPTION_BINDING.model_copy(update={"version": "1.0.1"})
    with pytest.raises(backfill.AssumptionSpecError):
        backfill.assumption_bound(_spec(wrong))


def test_a_wrong_hash_is_refused_not_silently_ignored() -> None:
    wrong = backfill.ASSUMPTION_BINDING.model_copy(update={"policy_hash": "0" * 64})
    with pytest.raises(backfill.AssumptionSpecError):
        backfill.assumption_bound(_spec(wrong))


def test_a_different_policy_bound_alongside_does_not_trip_this_one() -> None:
    other_id = "hlens.availability.archive-event-time-assumption"  # the ADR-0032 assumption
    other = _binding(PolicyRole.AVAILABILITY, other_id, digit="e")
    assert backfill.assumption_bound(_spec(other)) is False


# --------------------------------------------------------------------------- assumption_applies


def _applies(sim: datetime, **overrides: object) -> bool:
    defaults: dict[str, object] = {
        "backfill_floor": FLOOR,
        "simulation_time": sim,
        "first_observed_from": FIRST_OBSERVED,
        "first_status": "listed",
        "chain_first_revision_id": "r1",
        "committed_first_revision_id": "r1",
    }
    defaults.update(overrides)
    return backfill.assumption_applies(**defaults)  # type: ignore[arg-type]


def test_the_lower_bound_of_the_assumed_interval_is_inclusive() -> None:
    assert _applies(FLOOR) is True
    assert _applies(FLOOR - US) is False


def test_the_upper_bound_of_the_assumed_interval_is_exclusive() -> None:
    assert _applies(FIRST_OBSERVED - US) is True
    assert _applies(FIRST_OBSERVED) is False
    assert _applies(FIRST_OBSERVED + timedelta(days=1)) is False


def test_not_applicable_outside_the_policy_table() -> None:
    assert _applies(FLOOR, backfill_floor=None) is False


def test_not_applicable_when_the_first_observation_is_not_listed() -> None:
    assert _applies(FLOOR, first_status="suspended") is False


def test_not_applicable_when_the_chain_disagrees_a_competing_head_or_divergence() -> None:
    assert _applies(FLOOR, chain_first_revision_id="other-revision") is False
    assert _applies(FLOOR, chain_first_revision_id=None) is False  # no chain at all (a tie / gap)


def test_not_applicable_when_the_floor_is_not_before_the_first_observation() -> None:
    assert _applies(FIRST_OBSERVED, backfill_floor=FIRST_OBSERVED) is False
    later_floor = FIRST_OBSERVED + timedelta(days=2)
    assert _applies(FIRST_OBSERVED + timedelta(days=1), backfill_floor=later_floor) is False


# --------------------------------------------------------------------------- assumed_interval


def test_assumed_interval_reports_the_backward_extension() -> None:
    result = backfill.assumed_interval(
        venue_symbol="BTCUSDT",
        backfill_floor=FLOOR,
        first_observed_from=FIRST_OBSERVED,
        stored_available_time=FIRST_OBSERVED,
    )
    assert (result.venue_symbol, result.backfill_floor, result.first_observed_from) == (
        "BTCUSDT",
        FLOOR,
        FIRST_OBSERVED,
    )
    assert result.effective_available_time == FLOOR


def test_assumed_interval_never_moves_later_than_the_stored_available_time() -> None:
    earlier_than_floor = FLOOR - timedelta(days=1)
    result = backfill.assumed_interval(
        venue_symbol="BTCUSDT",
        backfill_floor=FLOOR,
        first_observed_from=FIRST_OBSERVED,
        stored_available_time=earlier_than_floor,
    )
    assert result.effective_available_time == earlier_than_floor  # min(): never later than stored
    assert result.effective_available_time <= earlier_than_floor


def test_assumed_interval_rejects_a_floor_not_before_the_observation() -> None:
    with pytest.raises(backfill.AssumptionSpecError):
        backfill.assumed_interval(
            venue_symbol="BTCUSDT",
            backfill_floor=FIRST_OBSERVED,
            first_observed_from=FIRST_OBSERVED,
            stored_available_time=FIRST_OBSERVED,
        )

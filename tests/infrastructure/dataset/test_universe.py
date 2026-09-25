"""F2 historical tradable universe (ADR-0024 acceptance #1, #3, #5, #6, #8, #9, #10, #11, #15).

Real exchangeInfo snapshots → real listing derivation → ``UniverseBuilder`` at bound snapshots.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from core.contracts.universe import (
    ExclusionReason,
    FilterComparator,
    MetricBasis,
    UniverseFilter,
)
from infrastructure.canonical import listing_rules as lr
from infrastructure.canonical.listings import UnconstructibleReason
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_EXCHANGE_INFO,
    CANONICAL_INSTRUMENT_LISTINGS,
)
from infrastructure.universe.builder import (
    FIRST_SLICE_UNIVERSE,
    UniverseFilterUnavailable,
    UniverseSpecError,
    UniverseUnconstructible,
)
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import L1, L2, L3, SIM, World
from tests.infrastructure.revision.rest_store_support import utc

LISTINGS = CANONICAL_INSTRUMENT_LISTINGS.table
BTC_HALT = {"BTCUSDT": "HALT", "ETHUSDT": "TRADING"}


def test_the_first_slice_spec_is_registered_with_venue_symbols() -> None:
    assert (FIRST_SLICE_UNIVERSE.name, FIRST_SLICE_UNIVERSE.version) == (
        "binance.spot.btc-eth",
        "1.0.0",
    )
    assert FIRST_SLICE_UNIVERSE.symbols == ("BTCUSDT", "ETHUSDT")
    assert FIRST_SLICE_UNIVERSE.filters == ()
    # Golden: the manifest binds this hash; any change must be a new version.
    assert FIRST_SLICE_UNIVERSE.binding().spec_hash == FIRST_SLICE_UNIVERSE.content_hash()


def test_members_at_a_point(w: World) -> None:
    w.listed()
    built = w.universe().build(FIRST_SLICE_UNIVERSE, w.spec())
    assert [ds.symbol_of(m) for m in built.members] == ["BTC-USDT", "ETH-USDT"]
    assert all(m.effective_from is None for m in built.members) and built.exclusions == ()
    assert [ds.episode_of(m).tradable_from for m in built.members] == [L1, L1]  # observed-from
    assert {item.canonical_table for item in built.lineage} == {LISTINGS}
    assert {item.raw_table for item in built.lineage} == {BINANCE_SPOT_EXCHANGE_INFO.table}
    assert len(built.evidence_gaps) == 2  # every listing revision: available_time = ingest_time
    assert built.member_spans == {"BTCUSDT": ((None, None),), "ETHUSDT": ((None, None),)}


def test_halt_and_resume_inside_an_interval(w: World) -> None:
    """ADR-0024 #3: suspended = excluded, resumed = same episode again, history retained."""
    w.listed(ds.TRADING, L1)
    w.listed(BTC_HALT, L2)
    w.listed(ds.TRADING, L3)
    built = w.universe().build(FIRST_SLICE_UNIVERSE, w.spec(interval=(L1, SIM)))
    btc = [m for m in built.members if ds.symbol_of(m) == "BTC-USDT"]
    assert [(m.effective_from, m.effective_until) for m in btc] == [(L1, L2), (L3, SIM)]
    assert len({m.episode for m in btc}) == 1
    [halted] = built.exclusions
    assert (halted.effective_from, halted.effective_until) == (L2, L3)
    assert halted.reason is ExclusionReason.NOT_TRADABLE
    assert len({m.listing_revision_id for m in btc} | {halted.listing_revision_id}) == 3
    [eth] = [m for m in built.members if ds.symbol_of(m) == "ETH-USDT"]
    assert (eth.effective_from, eth.effective_until) == (L1, SIM)
    assert built.member_spans["BTCUSDT"] == ((L1, L2), (L3, SIM))


def test_the_knowledge_axis_hides_later_derivations(w: World) -> None:
    """ADR-0024 #5: an early cutoff does not see the later halt; its answer never changes."""
    w.listed(ds.TRADING, L1)
    early_cutoff = w.x.clock.now - timedelta(microseconds=1)  # before the halt is known
    early = w.universe().build(FIRST_SLICE_UNIVERSE, w.spec(cutoff=early_cutoff))
    w.listed(BTC_HALT, L2)
    again = w.universe().build(FIRST_SLICE_UNIVERSE, w.spec(cutoff=early_cutoff))
    late = w.universe().build(FIRST_SLICE_UNIVERSE, w.spec())
    assert again == early and len(early.members) == 2
    assert [ds.symbol_of(e) for e in late.exclusions] == ["BTC-USDT"]


def test_before_the_first_observation_the_build_fails_closed(w: World) -> None:
    """ADR-0024 #6 / ADR-0029 #6: never today's list, never a silent exclusion."""
    w.listed(ds.TRADING, L2)
    with pytest.raises(UniverseUnconstructible) as caught:
        w.universe().build(FIRST_SLICE_UNIVERSE, w.spec(at=L2 - timedelta(microseconds=1)))
    assert caught.value.reason == UnconstructibleReason.NO_VISIBLE_LISTING
    with pytest.raises(UniverseUnconstructible):  # an interval reaching back fails as a whole
        w.universe().build(FIRST_SLICE_UNIVERSE, w.spec(interval=(L1, SIM)))


def test_an_unknown_status_fails_closed(w: World) -> None:
    """ADR-0029 #4: no inference from an unlisted status or a missing symbol."""
    w.listed(ds.TRADING, L1)
    w.listed({"BTCUSDT": "PRE_TRADING", "ETHUSDT": "TRADING"}, L2)
    with pytest.raises(UniverseUnconstructible) as caught:
        w.universe().build(FIRST_SLICE_UNIVERSE, w.spec())
    assert caught.value.reason == UnconstructibleReason.UNRESOLVED_OBSERVATION
    before = w.universe().build(FIRST_SLICE_UNIVERSE, w.spec(at=L1 + timedelta(hours=1)))
    assert len(before.members) == 2  # earlier simulation times are unaffected


def test_rebuilding_is_bit_identical(w: World) -> None:
    """ADR-0024 #9."""
    w.listed(ds.TRADING, L1)
    w.listed(BTC_HALT, L2)
    spec = w.spec(interval=(L1, SIM))
    first = w.universe().build(FIRST_SLICE_UNIVERSE, spec)
    w.listed(ds.TRADING, L3)  # later knowledge does not reach the bound snapshots
    w.h.reopen()
    second = w.universe().build(FIRST_SLICE_UNIVERSE, spec)
    assert second == first


def test_filters_are_refused_without_point_in_time_inputs(w: World) -> None:
    """ADR-0024 #8: no filter metric can be evaluated yet; no threshold is invented."""
    w.listed()
    filtered = FIRST_SLICE_UNIVERSE.model_copy(
        update={
            "version": "1.1.0",
            "filters": (
                UniverseFilter(
                    filter_id="liquidity",
                    feature_name="volume_1d",
                    feature_version="1.0.0",
                    feature_hash="0" * 64,
                    metric_basis=MetricBasis.POINT_IN_TIME,
                    comparator=FilterComparator.AT_LEAST,
                    threshold=1.0,
                ),
            ),
        }
    )
    with pytest.raises(UniverseFilterUnavailable, match="liquidity"):
        w.universe().build(filtered, w.spec())


def test_unregistered_specs_and_missing_bindings_are_refused(w: World) -> None:
    w.listed()
    renamed = FIRST_SLICE_UNIVERSE.model_copy(update={"symbols": ("BTCUSDT",)})
    with pytest.raises(UniverseSpecError, match="not registered"):
        w.universe().build(renamed, w.spec())
    spec = w.spec()
    without = spec.model_copy(
        update={
            "precedence_bindings": tuple(
                item
                for item in spec.precedence_bindings
                if item.policy_id != lr.LISTING_OBSERVATION_ID
            )
        }
    )
    with pytest.raises(UniverseSpecError, match="listing-observation"):
        w.universe().build(FIRST_SLICE_UNIVERSE, without)
    with pytest.raises(UniverseSpecError, match="listing history is missing"):
        w.universe().build(FIRST_SLICE_UNIVERSE, w.spec(skip=(LISTINGS,)))


def test_the_universe_reads_only_the_bound_snapshots(w: World) -> None:
    w.listed(ds.TRADING, L1)
    spec = w.spec(at=utc(2023, 11, 30))
    w.listed(BTC_HALT, L2)  # derived after the spec was pinned
    built = w.universe().build(FIRST_SLICE_UNIVERSE, spec)
    assert built.exclusions == () and len(built.members) == 2

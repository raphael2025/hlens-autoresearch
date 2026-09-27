"""G2 / missing listings, simulations before the first observation, late snapshots.

The universe (F2) is the first gate of every build: without a visible listing for every spec
symbol at the simulation instant, or with a listing history a late snapshot made ambiguous,
no dataset may exist, while manifests built before the late snapshot keep reproducing.
"""

from __future__ import annotations

from typing import Any

import pytest

from infrastructure.canonical.listings import UnconstructibleReason
from infrastructure.universe.builder import UniverseUnconstructible
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import L1, L2, L3, SIM, World
from tests.infrastructure.redteam import redteam_support as rt
from tests.infrastructure.revision.rest_store_support import utc


def _unconstructible(w: World, reason: str, **spec: Any) -> None:
    before = rt.outputs(w)
    with pytest.raises(UniverseUnconstructible) as caught:
        rt.build(w, **spec)
    assert caught.value.reason == reason
    assert rt.outputs(w) == before


def test_a_symbol_absent_from_every_snapshot_is_never_assumed_listed(w: World) -> None:
    w.listed({"BTCUSDT": "TRADING", "ETHUSDT": None}, L1)
    w.trades()
    w.report()
    _unconstructible(w, UnconstructibleReason.NO_VISIBLE_LISTING)


def test_an_interval_opening_before_the_first_observation_is_refused(w: World) -> None:
    w.listed(ds.TRADING, L2)
    w.trades()
    w.report()
    _unconstructible(w, UnconstructibleReason.NO_VISIBLE_LISTING, interval=(L1, SIM))


def test_a_knowledge_cutoff_before_the_listing_is_known_is_refused(w: World) -> None:
    """The observation instant is L1, but it is known (Raw and Canonical) only from the E2
    clock on: a spec whose cutoff lies in between must not see the listing."""
    w.listed(ds.TRADING, L1)
    w.trades()
    w.report()
    cutoff = utc(2023, 11, 24)
    assert L1 < cutoff < ds.LISTING_CLOCK
    _unconstructible(w, UnconstructibleReason.NO_VISIBLE_LISTING, at=cutoff, cutoff=cutoff)


def test_a_late_snapshot_that_moves_a_change_point_after_a_build(w: World) -> None:
    w.x.collect("snap-1", ds.TRADING, L1, server_time=1)
    w.x.collect("snap-2", ds.ETH_HALT, L2, server_time=2)
    w.x.collect("snap-3", ds.ETH_HALT, L3, server_time=3)
    w.x.ingest("snap-1")
    w.x.ingest("snap-3")
    w.derive()
    w.trades()
    w.report(symbols=("BTCUSDT",))
    built = rt.build(w)  # ETH halted since L3: an exclusion, BTC a member
    assert [ds.symbol_of(item) for item in built.manifest.exclusions] == ["ETH-USDT"]

    w.x.ingest("snap-2")  # arrives late: the halt was already observed at L2
    w.derive()
    w.report(symbols=("BTCUSDT",))
    _unconstructible(w, UnconstructibleReason.COMPETING_HEADS)
    replay = rt.build(w, built.manifest.point_in_time)
    assert replay.replayed and replay.manifest == built.manifest

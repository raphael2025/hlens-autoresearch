"""G2-style red team, end to end through ``UniverseBuilder`` and ``DatasetBuilder``: the ADR-0051
listing backfill assumption (D-LIST) must never make a never-observed, never-traded, suspended or
vanished ("delisted" in the everyday sense) symbol a member, and an assumed member can never be
passed off as observed (or the reverse) in a manifest.

``test_rt_listing_assumption`` attacks ``ListingDeriver.listing_at`` directly; this module drives
the same attacks through the universe build (v2 ``build`` and v3 ``cursor``) and the F3 manifest
store on one real SQLite world. ``POLICY_TABLE`` is monkeypatched to name **both** symbols, the
most permissive table possible, so any refusal below is the assumption's own boundary, not a
missing table entry.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.contracts.revision import PointInTimeSpec
from core.contracts.universe import ResearchDatasetManifest, UniverseMember
from infrastructure.canonical import rules
from infrastructure.canonical.listings import UnconstructibleReason
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import DATASET_MANIFESTS, DATASET_SELECTIONS
from infrastructure.revision.exchange_info_availability import EXCHANGE_INFO_AVAILABILITY_BINDING
from infrastructure.universe import listing_assumption as backfill
from infrastructure.universe.builder import FIRST_SLICE_UNIVERSE, UniverseUnconstructible
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import END, L1, L2, L3, SIM, START, World
from tests.infrastructure.revision.rest_store_support import utc

FLOOR = utc(2023, 11, 1)
TABLE = {"BTCUSDT": FLOOR, "ETHUSDT": FLOOR}
ETH_NEVER_LISTED: dict[str, str | None] = {"BTCUSDT": "TRADING", "ETHUSDT": None}
ETH_ONLY_HALTED: dict[str, str | None] = {"BTCUSDT": "TRADING", "ETHUSDT": "HALT"}
ETH_VANISHED: dict[str, str | None] = {"BTCUSDT": "TRADING", "ETHUSDT": None}
BTC_HALT: dict[str, str | None] = {"BTCUSDT": "HALT", "ETHUSDT": "TRADING"}


@pytest.fixture(autouse=True)
def table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backfill, "POLICY_TABLE", TABLE)


def bound(w: World, **kwargs: Any) -> PointInTimeSpec:
    return w.spec(
        availability_bindings=(
            rules.AVAILABILITY_BINDING,
            EXCHANGE_INFO_AVAILABILITY_BINDING,
            backfill.ASSUMPTION_BINDING,
        ),
        **kwargs,
    )


def refused_everywhere(w: World, spec: PointInTimeSpec) -> UniverseUnconstructible:
    """v2 ``build`` and v3 ``cursor`` both fail closed; the v2 error is returned."""
    with pytest.raises(UniverseUnconstructible) as caught:
        w.universe().build(FIRST_SLICE_UNIVERSE, spec)
    cursor = w.universe().cursor(FIRST_SLICE_UNIVERSE, spec, run_params=ds.UNIVERSE_RUN_PARAMS)
    with pytest.raises(UniverseUnconstructible), cursor.members() as members:
        list(members)
    return caught.value


# ------------------------------------------------------------------ never observed / never traded


@pytest.mark.parametrize(
    "statuses", [ETH_NEVER_LISTED, ETH_ONLY_HALTED], ids=["never-listed", "only-halted"]
)
def test_a_symbol_never_observed_trading_never_becomes_a_member(
    w: World, statuses: dict[str, str | None]
) -> None:
    """ETHUSDT is in the policy table and the spec binds the assumption, but this installation
    never observed it ``TRADING``: no episode exists, so nothing can be extended backward. The
    whole universe fails closed (never "BTC only"), before and after the observation instant."""
    w.listed(statuses, L1)
    for spec in (bound(w, interval=(FLOOR, SIM)), bound(w, at=FLOOR), bound(w, at=SIM)):
        error = refused_everywhere(w, spec)
        assert error.venue_symbol == "ETHUSDT"
        assert error.reason == UnconstructibleReason.NO_VISIBLE_LISTING


# ------------------------------------------------------------------ suspended / vanished later


def test_a_suspension_is_never_bridged_by_the_assumption(w: World) -> None:
    """TRADING at L1, HALT at L2, TRADING at L3: the only assumed span is [floor, L1); the halt
    stays an exclusion and every later span is the observed history's."""
    w.listed(ds.TRADING, L1)
    w.listed(BTC_HALT, L2)
    w.listed(ds.TRADING, L3)
    spec = bound(w, interval=(FLOOR, SIM))
    built = w.universe().build(FIRST_SLICE_UNIVERSE, spec)
    btc = sorted(
        (m.effective_from, m.effective_until, m.assumption is not None)
        for m in built.members
        if ds.symbol_of(m) == "BTC-USDT"
    )
    assert btc == [(FLOOR, L1, True), (L1, L2, False), (L3, SIM, False)]
    [halted] = built.exclusions
    assert (ds.symbol_of(halted), halted.effective_from, halted.effective_until) == (
        "BTC-USDT",
        L2,
        L3,
    )
    spans = {symbol: (a.effective_from, a.effective_until) for symbol, a in built.assumed.items()}
    assert spans == {"BTCUSDT": (FLOOR, L1), "ETHUSDT": (FLOOR, L1)}
    with (
        w.universe()
        .cursor(FIRST_SLICE_UNIVERSE, spec, run_params=ds.UNIVERSE_RUN_PARAMS)
        .members() as members
    ):
        assert set(members) == set(built.members)


def test_a_symbol_that_vanished_from_exchange_info_is_not_kept_by_the_assumption(
    w: World,
) -> None:
    """ETHUSDT disappears from exchangeInfo at L2 (the everyday "delisted"): ADR-0029 leaves it
    unresolved and the universe fails closed from then on; the assumption, which only ever looks
    backward from the first observation, cannot keep it a member."""
    w.listed(ds.TRADING, L1)
    w.listed(ETH_VANISHED, L2)
    error = refused_everywhere(w, bound(w, interval=(FLOOR, SIM)))
    assert error.venue_symbol == "ETHUSDT"
    assert error.reason == UnconstructibleReason.UNRESOLVED_OBSERVATION
    # Inside the assumed window only, the build is lawful (nothing after the floor is inferred).
    early = w.universe().build(FIRST_SLICE_UNIVERSE, bound(w, interval=(FLOOR, L2)))
    eth = [m for m in early.members if ds.symbol_of(m) == "ETH-USDT"]
    assert [(m.effective_from, m.effective_until, m.assumption is not None) for m in eth] == [
        (FLOOR, L1, True),
        (L1, L2, False),
    ]


# ------------------------------------------------------------------ manifest forgeries


def _revalidated(manifest: ResearchDatasetManifest, **update: Any) -> ResearchDatasetManifest:
    forged = manifest.model_copy(update=update)
    return ResearchDatasetManifest.model_validate_json(forged.model_dump_json())


def _relabel(members: tuple[UniverseMember, ...], *, assumed: bool) -> tuple[UniverseMember, ...]:
    binding = backfill.ASSUMPTION_BINDING if assumed else None
    return tuple(member.model_copy(update={"assumption": binding}) for member in members)


def test_assumed_and_observed_members_cannot_be_swapped_in_a_manifest(w: World) -> None:
    w.listed(ds.TRADING, L1)
    w.trades()
    w.report()
    built = w.builder().build(
        FIRST_SLICE_UNIVERSE, bound(w, interval=(FLOOR, SIM)), "agg_trades", START, END
    )
    genuine = built.manifest
    assert any(m.assumption is not None for m in genuine.members)
    assert any(m.assumption is None for m in genuine.members)
    forgeries = {
        # the assumption hidden: assumed spans presented as observed history
        "assumption-hidden": _revalidated(
            genuine, members=_relabel(genuine.members, assumed=False)
        ),
        # observed history presented as assumed
        "assumption-claimed": _revalidated(
            genuine, members=_relabel(genuine.members, assumed=True)
        ),
        # the assumed spans dropped: a manifest pretending the window started at L1
        "assumed-dropped": _revalidated(
            genuine, members=tuple(m for m in genuine.members if m.assumption is None)
        ),
    }
    store = w.builder().manifests()
    for name, forged in forgeries.items():
        assert forged != genuine, name
        before = (w.h.head(DATASET_MANIFESTS.table), w.h.head(DATASET_SELECTIONS.table))
        with pytest.raises(CatalogIntegrityError):
            store.persist(forged)
        assert (w.h.head(DATASET_MANIFESTS.table), w.h.head(DATASET_SELECTIONS.table)) == before
    assert store.load(genuine.content_hash()) == genuine

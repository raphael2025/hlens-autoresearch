"""ADR-0051 second phase: the listing backfill assumption through ``UniverseBuilder`` (D-LIST).

Real stores, real exchangeInfo snapshots, real listing derivation (``dataset_support.World``);
``build()`` (v2) and ``cursor()`` (v3) are both driven. The current version's ``POLICY_TABLE``
(1.1.0: real 2017 archive floors; 1.0.0 stays empty) is monkeypatched with an arbitrary floor
near the fixtures to exercise the in-table branch; ``ASSUMPTION_BINDING`` (the identity a spec
binds) is the real, unpatched module constant throughout.

Checked here: an unbound spec answers exactly as before; a bound spec adds, per symbol, one member
span ``[backfill_floor, first observation)`` carrying ``UniverseMember.assumption`` and listed in
``UniverseBuilt.assumed`` / ``UniverseSpanCursor.assumed()``, never merged with the observed span;
the cited revision's lineage and "observed-from" evidence gap are listed as usual; v3 equals v2; a
wrongly versioned binding is refused eagerly; before the floor the build still fails closed.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from core.contracts.revision import PointInTimeSpec, PolicyBinding, PolicyRole
from core.contracts.universe import UniverseMember
from infrastructure.canonical import rules
from infrastructure.canonical.listings import UnconstructibleReason
from infrastructure.contract_version import PHASE1_PUBLICATION_VERSION
from infrastructure.dataset.sources import UniverseRunParams
from infrastructure.pit.runs import RunLimits
from infrastructure.revision.exchange_info_availability import EXCHANGE_INFO_AVAILABILITY_BINDING
from infrastructure.universe import listing_assumption as backfill
from infrastructure.universe.builder import (
    FIRST_SLICE_UNIVERSE,
    AssumedMembership,
    UniverseBuilt,
    UniverseSpanCursor,
    UniverseSpecError,
    UniverseUnconstructible,
)
from tests.infrastructure.dataset import dataset_support as ds
from tests.infrastructure.dataset.dataset_support import L1, SIM, World
from tests.infrastructure.revision.rest_store_support import utc

#: An arbitrary UTC day boundary before the first local observation (L1): not a real floor.
FLOOR = utc(2023, 11, 1)
RUN_PARAMS = UniverseRunParams(
    capacity=1,
    merge_fanout=2,
    limits=RunLimits(leaf_max_records=1, leaf_max_bytes=4096, fanout=2),
)
TABLE = {"BTCUSDT": FLOOR, "ETHUSDT": FLOOR}


@pytest.fixture
def w(tmp_path: Path) -> Iterator[World]:
    with ds.sqlite_world(tmp_path) as opened:
        yield opened


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backfill, "POLICY_TABLE", TABLE)


def bound(
    w: World, *, assumption: PolicyBinding = backfill.ASSUMPTION_BINDING, **kwargs: Any
) -> PointInTimeSpec:
    """``w.spec(**kwargs)`` that also binds the ADR-0051 assumption (exact id, version, hash)."""
    return w.spec(
        availability_bindings=(
            rules.AVAILABILITY_BINDING,
            EXCHANGE_INFO_AVAILABILITY_BINDING,
            assumption,
        ),
        **kwargs,
    )


def _members(cursor: UniverseSpanCursor) -> tuple[UniverseMember, ...]:
    with cursor.members() as members:
        return tuple(members)


def _assumed(cursor: UniverseSpanCursor) -> tuple[AssumedMembership, ...]:
    with cursor.assumed() as assumed:
        return tuple(assumed)


def _spans(members: tuple[UniverseMember, ...]) -> list[tuple[str, Any, Any, bool]]:
    return sorted(
        (ds.symbol_of(m), m.effective_from, m.effective_until, m.assumption is not None)
        for m in members
    )


def _same_universe(a: UniverseBuilt, b: UniverseBuilt) -> None:
    assert a.members == b.members
    assert [m.content_hash() for m in a.members] == [m.content_hash() for m in b.members]
    assert a.exclusions == b.exclusions
    assert a.lineage == b.lineage
    assert a.evidence_gaps == b.evidence_gaps
    assert a.member_spans == b.member_spans
    assert a.assumed == b.assumed == {}


# ============================================================================ unbound: unchanged


def test_binding_changes_nothing_once_every_symbol_is_observed(w: World, table: None) -> None:
    """After the first observation the assumption never applies: bound and unbound agree field
    for field (v2 and v3), and no member carries ``assumption``."""
    w.listed(ds.TRADING, L1)
    universe = w.universe()
    cases: tuple[dict[str, Any], ...] = ({"interval": (L1, SIM)}, {"at": SIM})
    for kwargs in cases:
        plain = universe.build(FIRST_SLICE_UNIVERSE, w.spec(**kwargs))
        with_binding = universe.build(FIRST_SLICE_UNIVERSE, bound(w, **kwargs))
        _same_universe(plain, with_binding)
        assert all(m.assumption is None for m in plain.members)
        cursor = universe.cursor(
            FIRST_SLICE_UNIVERSE, bound(w, **kwargs), run_params=ds.UNIVERSE_RUN_PARAMS
        )
        assert set(_members(cursor)) == set(plain.members)
        assert _assumed(cursor) == ()


def test_an_unbound_spec_still_refuses_before_the_first_observation(w: World, table: None) -> None:
    """ADR-0051 §1: only an explicit binding lets the table matter; unbound, a window starting
    before the first local observation is ``no_visible_listing`` exactly as ADR-0029 §3 says."""
    w.listed(ds.TRADING, L1)
    for spec in (w.spec(interval=(FLOOR, SIM)), w.spec(at=FLOOR)):
        with pytest.raises(UniverseUnconstructible) as caught:
            w.universe().build(FIRST_SLICE_UNIVERSE, spec)
        assert caught.value.reason == UnconstructibleReason.NO_VISIBLE_LISTING
        with pytest.raises(UniverseUnconstructible):
            _members(
                w.universe().cursor(FIRST_SLICE_UNIVERSE, spec, run_params=ds.UNIVERSE_RUN_PARAMS)
            )


# ============================================================================ bound: assumed


def test_a_bound_interval_adds_one_assumed_span_per_symbol(w: World, table: None) -> None:
    w.listed(ds.TRADING, L1)
    built = w.universe().build(FIRST_SLICE_UNIVERSE, bound(w, interval=(FLOOR, SIM)))

    assert _spans(built.members) == [
        ("BTC-USDT", FLOOR, L1, True),
        ("BTC-USDT", L1, SIM, False),
        ("ETH-USDT", FLOOR, L1, True),
        ("ETH-USDT", L1, SIM, False),
    ]
    assert built.exclusions == ()
    for member in built.members:
        if member.assumption is not None:
            assert member.assumption == backfill.ASSUMPTION_BINDING
            assert member.assumption.schema_version == PHASE1_PUBLICATION_VERSION
            assert member.assumption.role is PolicyRole.AVAILABILITY
    # The assumed span cites the real, unmodified first revision (and so the same episode) as the
    # observed span after it: one revision per symbol, one lineage entry, one listed gap each.
    by_symbol: dict[str, set[str]] = {}
    for member in built.members:
        by_symbol.setdefault(ds.symbol_of(member), set()).add(member.listing_revision_id)
    assert all(len(ids) == 1 for ids in by_symbol.values())
    cited = {revision for ids in by_symbol.values() for revision in ids}
    assert {item.canonical_revision_id for item in built.lineage} == cited
    # ADR-0029 "observed-from" evidence gaps: listed exactly as without the assumption.
    observed = w.universe().build(FIRST_SLICE_UNIVERSE, w.spec(interval=(L1, SIM)))
    assert built.evidence_gaps == observed.evidence_gaps
    assert {revision for revision, _gap in built.evidence_gaps} == cited
    assert built.lineage == observed.lineage

    assert built.member_spans == {
        "BTCUSDT": ((FLOOR, L1), (L1, SIM)),
        "ETHUSDT": ((FLOOR, L1), (L1, SIM)),
    }
    assert sorted(built.assumed) == ["BTCUSDT", "ETHUSDT"]
    for venue_symbol, membership in built.assumed.items():
        [assumed_member] = [
            m
            for m in built.members
            if m.assumption is not None and ds.symbol_of(m) == rules.SYMBOLS[venue_symbol].symbol
        ]
        assert membership == AssumedMembership(
            venue_symbol=venue_symbol,
            listing_revision_id=assumed_member.listing_revision_id,
            effective_from=FLOOR,
            effective_until=L1,
            backfill_floor=FLOOR,
            first_observed_from=L1,
            effective_available_time=FLOOR,  # min(stored, floor): never later than stored
            binding=backfill.ASSUMPTION_BINDING,
        )


def test_the_v3_cursor_gives_exactly_the_v2_answer(w: World, table: None) -> None:
    w.listed(ds.TRADING, L1)
    spec = bound(w, interval=(FLOOR, SIM))
    built = w.universe().build(FIRST_SLICE_UNIVERSE, spec)
    cursor = w.universe().cursor(FIRST_SLICE_UNIVERSE, spec, run_params=ds.UNIVERSE_RUN_PARAMS)
    members = _members(cursor)
    assert set(members) == set(built.members)
    assert [m.content_hash() for m in sorted(members, key=_key)] == [
        m.content_hash() for m in sorted(built.members, key=_key)
    ]
    assert _assumed(cursor) == tuple(built.assumed.values())
    with cursor.listing_lineage() as lineage:
        assert set(lineage) == set(built.lineage)
    with cursor.evidence_gaps() as gaps:
        assert set(gaps) == set(built.evidence_gaps)
    with cursor.member_spans() as spans:
        v3_spans: dict[str, list[tuple[Any, Any]]] = {}
        for symbol, start, end in spans:
            v3_spans.setdefault(symbol, []).append((start, end))
    assert {symbol: tuple(items) for symbol, items in v3_spans.items()} == built.member_spans


def _key(member: UniverseMember) -> tuple[str, datetime]:
    assert member.effective_from is not None
    return member.episode.observation_key(), member.effective_from


def test_a_point_inside_the_assumed_window(w: World, table: None) -> None:
    w.listed(ds.TRADING, L1)
    at = utc(2023, 11, 5)
    built = w.universe().build(FIRST_SLICE_UNIVERSE, bound(w, at=at))
    assert _spans(built.members) == [
        ("BTC-USDT", None, None, True),
        ("ETH-USDT", None, None, True),
    ]
    spans = {symbol: (m.effective_from, m.effective_until) for symbol, m in built.assumed.items()}
    assert spans == {"BTCUSDT": (None, None), "ETHUSDT": (None, None)}
    cursor = w.universe().cursor(
        FIRST_SLICE_UNIVERSE, bound(w, at=at), run_params=ds.UNIVERSE_RUN_PARAMS
    )
    assert set(_members(cursor)) == set(built.members)


# ============================================================================ fail closed


def test_the_policy_named_with_another_hash_or_version_is_refused_eagerly(
    w: World, table: None
) -> None:
    w.listed(ds.TRADING, L1)
    for wrong in (
        PolicyBinding(
            schema_version=PHASE1_PUBLICATION_VERSION,
            role=PolicyRole.AVAILABILITY,
            policy_id=backfill.ASSUMPTION_ID,
            version=backfill.ASSUMPTION_VERSION,
            policy_hash="f" * 64,
        ),
        PolicyBinding(
            schema_version=PHASE1_PUBLICATION_VERSION,
            role=PolicyRole.AVAILABILITY,
            policy_id=backfill.ASSUMPTION_ID,
            version="1.0.1",
            policy_hash=backfill.ASSUMPTION_BINDING.policy_hash,
        ),
    ):
        spec = w.spec(
            interval=(FLOOR, SIM),
            availability_bindings=(
                rules.AVAILABILITY_BINDING,
                EXCHANGE_INFO_AVAILABILITY_BINDING,
                wrong,
            ),
        )
        with pytest.raises(UniverseSpecError, match=backfill.ASSUMPTION_ID):
            w.universe().build(FIRST_SLICE_UNIVERSE, spec)
        with pytest.raises(UniverseSpecError, match=backfill.ASSUMPTION_ID):
            w.universe().cursor(FIRST_SLICE_UNIVERSE, spec, run_params=ds.UNIVERSE_RUN_PARAMS)


def test_before_the_floor_the_bound_build_still_fails_closed(w: World, table: None) -> None:
    w.listed(ds.TRADING, L1)
    early = FLOOR - timedelta(microseconds=1)
    for spec in (bound(w, interval=(early, SIM)), bound(w, at=early)):
        with pytest.raises(UniverseUnconstructible) as caught:
            w.universe().build(FIRST_SLICE_UNIVERSE, spec)
        assert caught.value.reason == UnconstructibleReason.NO_VISIBLE_LISTING
        with pytest.raises(UniverseUnconstructible):
            _members(
                w.universe().cursor(FIRST_SLICE_UNIVERSE, spec, run_params=ds.UNIVERSE_RUN_PARAMS)
            )


def test_the_real_empty_policy_table_never_assumes(w: World) -> None:
    """No monkeypatch: the frozen 1.0.0 table is empty, so even a spec binding 1.0.0 refuses."""
    w.listed(ds.TRADING, L1)
    spec = bound(w, assumption=backfill.ASSUMPTION_BINDING_1_0_0, interval=(FLOOR, SIM))
    with pytest.raises(UniverseUnconstructible) as caught:
        w.universe().build(FIRST_SLICE_UNIVERSE, spec)
    assert caught.value.reason == UnconstructibleReason.NO_VISIBLE_LISTING

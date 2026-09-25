"""E2 listing history: ADR-0029 acceptance matrix #1 ~ #6 (#7 is the collector's) + adversarial.

Real collector checkpoints (mock venue), real snapshot store, real SQLite catalog, the real
``ListingDeriver``. Nothing is faked but the transport and the clocks.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.contracts.catalog import CommitRequest
from core.contracts.universe import (
    DegradedEpisodeKey,
    EpisodeIdentityBasis,
    ListingHistory,
    ListingStatus,
    TradableInterval,
)
from core.domain.specs import InstrumentType
from infrastructure.canonical import listing_rules as lr
from infrastructure.canonical.listings import (
    FINDING_HISTORY_DIVERGED,
    ListingDeriveConflict,
    ListingDeriveError,
    ListingPointInTime,
    ListingUnconstructible,
    UnconstructibleReason,
)
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.revision.exchange_info_availability import (
    ExchangeInfoAvailabilitySubject,
    exchange_info_gap_for,
)
from infrastructure.revision.row_integrity import batch
from tests.infrastructure.revision import exchange_info_support as xs
from tests.infrastructure.revision.exchange_info_support import (
    EXCHANGE_INFO,
    LISTINGS,
    T1,
    T2,
    T3,
    T4,
    TRADING,
    Harness,
)
from tests.infrastructure.revision.rest_store_support import Crash, ProxyCatalog

LATE: datetime = xs.KNOWLEDGE + timedelta(days=30)
BTC_HALT: dict[str, str | None] = {"BTCUSDT": "HALT", "ETHUSDT": "TRADING"}
FORGED_ID = "lrev1-" + "f" * 64


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    with xs.harness(tmp_path) as opened:
        yield opened


def listings(h: Harness, symbol: str = "BTC-USDT") -> list[dict[str, Any]]:
    rows = [row for row in h.rows(LISTINGS.table) if row["symbol"] == symbol]
    return sorted(rows, key=lambda row: row["arrival_seq"])


def at(h: Harness, simulation: datetime, cutoff: datetime = LATE) -> ListingPointInTime:
    deriver = h.deriver()
    try:
        return deriver.listing_at("BTCUSDT", simulation, cutoff)
    finally:
        deriver.close()


def derive(h: Harness) -> Any:
    deriver = h.deriver()
    try:
        return deriver.derive()
    finally:
        deriver.close()


def interval(start: datetime, end: datetime | None) -> TradableInterval:
    return TradableInterval(tradable_from=start, tradable_until=end)


# --------------------------------------------------------------------------- policies


def test_the_two_listing_policies_are_versioned_and_hashed() -> None:
    assert (lr.LISTING_STATUS_BINDING.policy_id, lr.LISTING_STATUS_BINDING.version) == (
        "binance.spot.listing-status",
        "1.0.0",
    )
    assert lr.LISTING_STATUS_BINDING.role.value == "parser"
    assert (
        lr.LISTING_OBSERVATION_BINDING.policy_id,
        lr.LISTING_OBSERVATION_BINDING.role.value,
    ) == (
        "binance.spot.listing-observation",
        "precedence",
    )
    # Golden: any rule change moves these values (and must move the version).
    assert lr.LISTING_STATUS_HASH == (
        "a7a1b35783e6c6a427c2ed74b48b66dbe1484aab17f0e1084b717b192b738454"
    )
    assert lr.LISTING_OBSERVATION_HASH == (
        "e2ce33d4f2f96d451760a62e1b9e55baf8b0ea416931aef3ea51c7f27be9b15c"
    )
    assert "delisted" not in {status.value for status in lr.ObservationClass}


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("TRADING", lr.ObservationClass.LISTED),
        ("HALT", lr.ObservationClass.SUSPENDED),
        ("BREAK", lr.ObservationClass.SUSPENDED),
        ("END_OF_DAY", lr.ObservationClass.SUSPENDED),
        ("CANCEL_ONLY", lr.ObservationClass.SUSPENDED),
        ("PRE_TRADING", lr.ObservationClass.UNRESOLVED),
        ("DELISTED", lr.ObservationClass.UNRESOLVED),
        (None, lr.ObservationClass.UNRESOLVED),
    ],
)
def test_the_status_mapping_never_infers(status: str | None, expected: lr.ObservationClass) -> None:
    observation = lr.Observation("BTCUSDT", "rev1-x", T1 - xs.MS, T1, T1, status, "BTC", "USDT")
    assert lr.classify(observation)[0] is expected
    mismatched = lr.Observation("BTCUSDT", "rev1-x", T1 - xs.MS, T1, T1, "TRADING", "XBT", "USDT")
    assert lr.classify(mismatched) == (
        lr.ObservationClass.UNRESOLVED,
        lr.FINDING_INSTRUMENT_MISMATCH,
    )


# --------------------------------------------------------------------------- #1


def test_1_a_first_trading_snapshot_is_one_observed_from_revision(h: Harness) -> None:
    stored = h.observe("snap-1", TRADING, T1)
    result = derive(h)
    assert len(result.new_revision_ids) == 2 and result.findings == ()
    [row] = listings(h)
    listing = lr.listing_revision_from_row(row)
    assert listing.episode == DegradedEpisodeKey(
        basis=EpisodeIdentityBasis.DEGRADED_SYMBOL_START,
        venue="binance",
        instrument_type=InstrumentType.SPOT,
        symbol="BTC-USDT",
        tradable_from=T1,
    )
    assert listing.tradable_intervals == (interval(T1, None),)
    assert listing.status is ListingStatus.LISTED and listing.source_status == "TRADING"
    assert (listing.instrument.base, listing.instrument.quote) == ("BTC", "USDT")
    assert listing.status_reason is not None and "observed-from" in listing.status_reason
    assert "not an exchange-declared listing time" in listing.status_reason
    assert row["availability_evidence_gap"] == exchange_info_gap_for(
        ExchangeInfoAvailabilitySubject.LISTING_OBSERVATION
    )
    times = listing.revision.availability.times
    assert times.available_time == times.ingest_time == T1
    assert times.knowledge_time >= stored.knowledge_time
    assert listing.revision.supersedes == () and row["precedence_evidence"] == []
    # Lineage: both hops are the observing snapshot revision of the snapshot table.
    assert (row["lineage_raw_table"], row["lineage_source_table"]) == (EXCHANGE_INFO.table,) * 2
    assert row["lineage_raw_revision_id"] == row["lineage_source_revision_id"] == stored.revision_id
    assert row["source_id"] == (
        f"binance.spot.listing-status@1.0.0|{EXCHANGE_INFO.table}|{stored.revision_id}"
    )
    assert row["revision_id"].startswith("lrev1-")
    point = at(h, T1)
    assert point.constructible and point.tradable and point.listing == listing
    assert point.lineage is not None and point.lineage.source_revision_id == stored.revision_id
    assert point.evidence_gap == row["availability_evidence_gap"]


# --------------------------------------------------------------------------- #2


def test_2_repeated_unchanged_snapshots_only_reach_raw(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    derive(h)
    head = h.head(LISTINGS.table)
    h.observe("snap-2", TRADING, T2, server_time=2)
    h.observe("snap-3", TRADING, T3, server_time=3)
    again = derive(h)
    assert again.replayed and again.new_revision_ids == ()
    assert h.head(LISTINGS.table) == head
    assert len(h.rows(EXCHANGE_INFO.table)) == 3 and len(listings(h)) == 1
    assert derive(h).replayed  # re-running is idempotent


# --------------------------------------------------------------------------- #3


def test_3_trading_halt_trading_is_one_episode_two_intervals_and_a_chain(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    h.observe("snap-2", BTC_HALT, T2, server_time=2)
    h.observe("snap-3", TRADING, T3, server_time=3)
    derive(h)
    first, halted, resumed = (lr.listing_revision_from_row(row) for row in listings(h))
    assert {item.episode for item in (first, halted, resumed)} == {first.episode}
    assert halted.status is ListingStatus.SUSPENDED and halted.source_status == "HALT"
    assert halted.tradable_intervals == (interval(T1, T2),)  # closed at the observation instant
    assert resumed.status is ListingStatus.LISTED
    assert resumed.tradable_intervals == (interval(T1, T2), interval(T3, None))
    assert halted.revision.supersedes == (first.revision.revision_id,)
    assert resumed.revision.supersedes == (halted.revision.revision_id,)
    rows = listings(h)
    evidence = rows[1]["precedence_evidence"]
    assert [item["policy_id"] for item in evidence] == ["binance.spot.listing-observation"]
    assert rows[1]["supersedes"] == [first.revision.revision_id]
    history = ListingHistory(
        revisions=(first, halted, resumed),
        precedence_evidence=tuple(
            edge for row in rows for edge in lr.listing_record_from_row(row)[1]
        ),
    )
    assert len(history.revisions) == 3
    # The ETH chain is unaffected by BTC's halt.
    assert len(listings(h, "ETH-USDT")) == 1
    expected = [(T1, True, first), (T2, False, halted), (T3, True, resumed)]
    for simulation, tradable, listing in expected:
        point = at(h, simulation + timedelta(minutes=1))
        assert point.constructible and point.tradable is tradable
        assert point.listing == listing


def test_3_deriving_after_each_snapshot_equals_deriving_once(h: Harness, tmp_path: Path) -> None:
    h.observe("snap-1", TRADING, T1)
    derive(h)
    h.observe("snap-2", BTC_HALT, T2, server_time=2)
    derive(h)
    h.observe("snap-3", TRADING, T3, server_time=3)
    derive(h)
    stepwise = [row["revision_id"] for row in listings(h)]
    with xs.harness(tmp_path / "once") as other:
        other.observe("snap-1", TRADING, T1)
        other.observe("snap-2", BTC_HALT, T2, server_time=2)
        other.observe("snap-3", TRADING, T3, server_time=3)
        derive(other)
        assert [row["revision_id"] for row in listings(other)] == stepwise


# --------------------------------------------------------------------------- #4


@pytest.mark.parametrize(
    ("statuses", "code"),
    [
        ({"BTCUSDT": "PRE_TRADING", "ETHUSDT": "TRADING"}, lr.FINDING_STATUS_UNKNOWN),
        ({"BTCUSDT": None, "ETHUSDT": "TRADING"}, lr.FINDING_SYMBOL_MISSING),
    ],
)
def test_4_unknown_status_or_missing_symbol_infers_nothing_and_fails_closed(
    h: Harness, statuses: Mapping[str, str | None], code: str
) -> None:
    h.observe("snap-1", TRADING, T1)
    derive(h)
    before = [row["revision_id"] for row in listings(h)]
    h.observe("snap-2", statuses, T2, server_time=2)
    result = derive(h)
    assert result.new_revision_ids == ()  # no inference: no delisting, no suspension
    assert [row["revision_id"] for row in listings(h)] == before
    [finding] = [item for item in result.findings if item.venue_symbol == "BTCUSDT"]
    assert finding.code == code and finding.observed_at == T2
    assert all(row["status"] != "delisted" for row in h.rows(LISTINGS.table))
    blocked = at(h, T2)
    assert not blocked.constructible
    assert blocked.reason == UnconstructibleReason.UNRESOLVED_OBSERVATION
    with pytest.raises(ListingUnconstructible, match="unresolved_observation"):
        blocked.require_constructible()
    assert at(h, T2 - xs.MS).constructible  # before the unresolved observation: unaffected
    # A new explicit observation lifts the block (no revision: the status did not change).
    h.observe("snap-3", TRADING, T3, server_time=3)
    assert derive(h).new_revision_ids == ()
    assert at(h, T3).constructible and at(h, T3).tradable
    assert not at(h, T3 - xs.MS).constructible


def test_4_an_instrument_mismatch_is_unresolved_too(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    derive(h)
    h.observe("snap-2", TRADING, T2, server_time=2, bases={"BTCUSDT": "XBT"})
    result = derive(h)
    assert result.new_revision_ids == ()
    assert [item.code for item in result.findings] == [lr.FINDING_INSTRUMENT_MISMATCH]
    assert at(h, T2).reason == UnconstructibleReason.UNRESOLVED_OBSERVATION


def test_4_nothing_before_the_first_trading_observation(h: Harness) -> None:
    h.observe("snap-1", {"BTCUSDT": "PRE_TRADING", "ETHUSDT": None}, T1)
    h.observe("snap-2", {"BTCUSDT": "HALT", "ETHUSDT": None}, T2, server_time=2)
    result = derive(h)
    assert result.new_revision_ids == () and h.rows(LISTINGS.table) == []
    assert {item.code for item in result.findings} == {
        lr.FINDING_STATUS_UNKNOWN,
        lr.FINDING_SUSPENDED_BEFORE_TRADING,
        lr.FINDING_SYMBOL_MISSING,
    }
    assert at(h, T3).reason == UnconstructibleReason.NO_VISIBLE_LISTING
    h.observe("snap-3", TRADING, T3, server_time=3)
    derive(h)
    [row] = listings(h)
    assert row["episode_tradable_from"] == T3  # the first TRADING observation, not the first seen


# --------------------------------------------------------------------------- #5


def test_5_out_of_order_arrival_derives_the_in_order_history(h: Harness, tmp_path: Path) -> None:
    h.collect("snap-1", TRADING, T1)
    h.collect("snap-2", BTC_HALT, T2, server_time=2)
    h.collect("snap-3", TRADING, T3, server_time=3)
    h.ingest("snap-1")
    h.ingest("snap-3")
    derive(h)
    assert len(listings(h)) == 1  # snap-3 alone is no change
    late = h.ingest("snap-2")
    assert late.arrival_seq == 2  # arrived last ...
    derive(h)
    rows = listings(h)
    # ... yet ordered by retrieved_at: T1 listed → T2 suspended → T3 listed.
    assert [row["ingest_time"] for row in rows] == [T1, T2, T3]
    assert rows[2]["supersedes"] == [rows[1]["revision_id"]]
    with xs.harness(tmp_path / "in-order") as other:
        for request_id, statuses, instant, server in (
            ("snap-1", TRADING, T1, 1_788_000_000_000),
            ("snap-2", BTC_HALT, T2, 2),
            ("snap-3", TRADING, T3, 3),
        ):
            other.observe(request_id, statuses, instant, server_time=server)
        derive(other)
        assert [row["revision_id"] for row in listings(other)] == [
            row["revision_id"] for row in rows
        ]
    verified = h.deriver().verify()
    assert verified.diverged == ()


def test_5_a_late_snapshot_that_moves_a_change_point_fails_closed(h: Harness) -> None:
    h.collect("snap-1", TRADING, T1)
    h.collect("snap-2", BTC_HALT, T2, server_time=2)
    h.collect("snap-3", BTC_HALT, T3, server_time=3)
    h.ingest("snap-1")
    h.ingest("snap-3")
    derive(h)
    stale = listings(h)[-1]
    assert stale["tradable_intervals"][-1]["tradable_until"] == T3
    h.ingest("snap-2")
    result = derive(h)
    assert result.diverged == (stale["revision_id"],)
    assert FINDING_HISTORY_DIVERGED in {item.code for item in result.findings}
    # Correct where it can be: [T2, T3) is suspended from T2 on.
    point = at(h, T2 + timedelta(minutes=1))
    assert point.constructible and not point.tradable
    assert point.listing is not None and point.listing.tradable_intervals == (interval(T1, T2),)
    # From the stale revision's availability on: two heads, never a guess.
    assert at(h, T3).reason == UnconstructibleReason.COMPETING_HEADS
    # The stale revision is never rewritten.
    assert stale in listings(h)


def test_5_a_late_earlier_first_observation_is_a_second_episode_and_fails_closed(
    h: Harness,
) -> None:
    h.collect("snap-1", TRADING, T1)
    h.collect("snap-2", TRADING, T2, server_time=2)
    h.ingest("snap-2")
    derive(h)
    [stale] = listings(h)
    assert stale["episode_tradable_from"] == T2
    h.ingest("snap-1")  # the earlier TRADING observation arrives late
    result = derive(h)
    assert stale["revision_id"] in result.diverged
    assert {row["episode_tradable_from"] for row in listings(h)} == {T1, T2}
    # Two known episodes of one symbol: which one is the instrument cannot be said, at any time.
    for simulation in (T1, T3):
        assert at(h, simulation).reason == UnconstructibleReason.MULTIPLE_EPISODES
    # Before the stale episode was derived, the knowledge axis still answers.
    assert at(h, T1, stale["knowledge_time"] - xs.MS).reason == (
        UnconstructibleReason.NO_VISIBLE_LISTING
    )


def test_5_ties_cannot_be_ordered_and_stop_the_chain(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    h.collect("snap-2a", BTC_HALT, T2, server_time=2)
    h.collect("snap-2b", TRADING, T2, server_time=3)
    h.ingest("snap-2a")
    h.ingest("snap-2b")
    result = derive(h)
    assert len(listings(h)) == 1
    assert lr.FINDING_OBSERVATION_TIE in {item.code for item in result.findings}
    assert at(h, T2).reason == UnconstructibleReason.OBSERVATION_TIE
    assert at(h, T2 - xs.MS).constructible


# --------------------------------------------------------------------------- #6


def test_6_before_the_first_local_observation_no_universe_can_be_built(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    result = derive(h)
    point = at(h, T1 - xs.MS)
    assert not point.constructible
    assert point.reason == UnconstructibleReason.NO_VISIBLE_LISTING
    with pytest.raises(ListingUnconstructible):
        point.require_constructible()
    historical = at(h, datetime(2017, 8, 17, tzinfo=T1.tzinfo))
    assert historical.reason == UnconstructibleReason.NO_VISIBLE_LISTING
    # Knowledge axis: before the derivation was known, nothing is visible either.
    rows = listings(h)
    known = rows[0]["knowledge_time"]
    assert result.commit is not None
    assert at(h, T2, known - xs.MS).reason == UnconstructibleReason.NO_VISIBLE_LISTING
    assert at(h, T2, known).constructible


def test_6_a_derivation_lagging_the_snapshots_fails_closed(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    derive(h)
    h.observe("snap-2", BTC_HALT, T2, server_time=2)  # known to Raw, not yet derived
    point = at(h, T2)
    assert point.reason == UnconstructibleReason.LISTING_NOT_DERIVED
    derive(h)
    assert at(h, T2).constructible and at(h, T2).tradable is False


# --------------------------------------------------------------------------- recovery / races


def test_a_crash_after_the_listing_commit_is_adopted_on_rerun(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)

    def crash(request: CommitRequest, result: object) -> None:
        raise Crash("after commit")

    deriver = h.deriver(ProxyCatalog(h.adapter, after=crash))
    with pytest.raises(Crash):
        deriver.derive()
    deriver.close()
    head = h.head(LISTINGS.table)
    assert derive(h).replayed and h.head(LISTINGS.table) == head
    assert len(h.rows(LISTINGS.table)) == 2


def test_a_rival_derivation_is_adopted_not_duplicated(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    fired: list[str] = []

    def rival(request: CommitRequest) -> None:
        if request.table == LISTINGS.table and not fired:
            fired.append(request.batch_id)
            derive(h)

    deriver = h.deriver(ProxyCatalog(h.adapter, before=rival))
    result = deriver.derive()
    deriver.close()
    assert fired and result.replayed  # the rival committed first; nothing was missing any more
    rows = h.rows(LISTINGS.table)
    assert len(rows) == len({row["revision_id"] for row in rows}) == 2


def test_a_clock_before_a_raw_knowledge_time_is_refused(h: Harness) -> None:
    stored = h.observe("snap-1", TRADING, T1)
    h.clock.now = stored.knowledge_time - xs.MS
    with pytest.raises(ListingDeriveConflict, match="backfill"):
        derive(h)
    assert h.rows(LISTINGS.table) == []


# --------------------------------------------------------------------------- forged listings


def _commit_listing(h: Harness, rows: Sequence[Mapping[str, Any]], batch_id: str) -> None:
    table = batch(LISTINGS, [dict(row) for row in rows])
    h.adapter.commit_batch(
        CommitRequest(
            table=LISTINGS.table,
            batch_id=batch_id,
            batch_fingerprint=LISTINGS.fingerprint_rule.fingerprint(table),
            row_count=len(rows),
            expected_parent_snapshot_id=h.head(LISTINGS.table),
        ),
        table,
    )


@pytest.mark.parametrize(
    ("forge", "message"),
    [
        # a listing nobody observed, under a lawful-looking batch id
        (
            lambda row: dict(row, arrival_seq=2, revision_id=FORGED_ID, status="suspended"),
            "not what Raw snapshot",
        ),
        # an earlier tradable_from than any observation
        (
            lambda row: dict(
                row,
                arrival_seq=2,
                revision_id=FORGED_ID,
                tradable_intervals=[
                    {"tradable_from": T1 - timedelta(days=9), "tradable_until": None}
                ],
            ),
            "not what Raw snapshot",
        ),
        # a duplicate of a real revision
        (lambda row: dict(row, arrival_seq=2), "committed twice"),
    ],
)
def test_forged_listing_rows_make_the_history_unprovable(
    h: Harness, forge: Any, message: str
) -> None:
    h.observe("snap-1", TRADING, T1)
    derive(h)
    [row] = listings(h)
    h.observe("snap-2", TRADING, T2, server_time=2)  # a new Raw snapshot a forger can name
    raw_head = h.head(EXCHANGE_INFO.table)
    assert raw_head is not None
    _commit_listing(h, [forge(row)], lr.batch_id_for(raw_head))
    with pytest.raises(CatalogIntegrityError, match=message):
        derive(h)
    with pytest.raises(CatalogIntegrityError):
        at(h, T2)


def test_a_listing_batch_under_a_foreign_batch_id_is_refused(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    deriver = h.deriver()
    state = deriver.verify()
    deriver.close()
    planned = [item for chain in state.chains.values() for item in chain.revisions]
    rows = [
        lr.listing_columns(item, arrival_seq=index, knowledge_time=LATE)
        for index, item in enumerate(planned)
    ]
    _commit_listing(h, rows, "hand-written-listings")
    with pytest.raises(CatalogIntegrityError, match="not a listing derivation batch"):
        derive(h)


def test_a_listing_batch_with_a_backdated_knowledge_time_is_refused(h: Harness) -> None:
    stored = h.observe("snap-1", TRADING, T1)
    deriver = h.deriver()
    state = deriver.verify()
    deriver.close()
    planned = [item for chain in state.chains.values() for item in chain.revisions]
    # The builder itself refuses a backdated row, so the forger edits a lawful one.
    with pytest.raises(lr.ListingRuleViolation):
        lr.listing_columns(planned[0], arrival_seq=0, knowledge_time=stored.knowledge_time - xs.MS)
    rows = [
        dict(
            lr.listing_columns(item, arrival_seq=index, knowledge_time=LATE),
            knowledge_time=stored.knowledge_time - xs.MS,
        )
        for index, item in enumerate(planned)
    ]
    raw_head = h.head(EXCHANGE_INFO.table)
    assert raw_head is not None
    _commit_listing(h, rows, lr.batch_id_for(raw_head))
    with pytest.raises(CatalogIntegrityError, match="known before a Raw row"):
        derive(h)


def test_a_forged_snapshot_row_blocks_every_listing_read(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    derive(h)
    [raw] = h.rows(EXCHANGE_INFO.table)
    forged = dict(raw, arrival_seq=1, symbols=[dict(raw["symbols"][0], status="HALT")])
    table = batch(EXCHANGE_INFO, [forged])
    h.adapter.commit_batch(
        CommitRequest(
            table=EXCHANGE_INFO.table,
            batch_id=f"{raw['revision_id']}.snapshot.1",
            batch_fingerprint=EXCHANGE_INFO.fingerprint_rule.fingerprint(table),
            row_count=1,
            expected_parent_snapshot_id=h.head(EXCHANGE_INFO.table),
        ),
        table,
    )
    with pytest.raises(CatalogIntegrityError):
        at(h, T2)
    with pytest.raises(CatalogIntegrityError):
        derive(h)


def test_point_in_time_reads_refuse_unlawful_arguments(h: Harness) -> None:
    deriver = h.deriver()
    try:
        for simulation, cutoff in (
            (T1.replace(tzinfo=None), LATE),
            (T1, LATE.replace(tzinfo=None)),
        ):
            with pytest.raises(ListingDeriveError, match="UTC"):
                deriver.listing_at("BTCUSDT", simulation, cutoff)
        with pytest.raises(ListingDeriveError, match="first-slice"):
            deriver.listing_at("BNBUSDT", T1, LATE)
    finally:
        deriver.close()


def test_the_later_eth_chain_is_independent(h: Harness) -> None:
    h.observe("snap-1", {"BTCUSDT": "TRADING", "ETHUSDT": None}, T1)
    h.observe("snap-2", TRADING, T4, server_time=2)
    derive(h)
    [eth] = listings(h, "ETH-USDT")
    assert eth["episode_tradable_from"] == T4
    deriver = h.deriver()
    try:
        assert deriver.listing_at("ETHUSDT", T1, LATE).reason in {
            UnconstructibleReason.NO_VISIBLE_LISTING,
            UnconstructibleReason.UNRESOLVED_OBSERVATION,
        }
        assert deriver.listing_at("ETHUSDT", T4, LATE).constructible
    finally:
        deriver.close()

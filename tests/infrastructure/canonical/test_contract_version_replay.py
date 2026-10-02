"""ADR-0052 versioned replay of Canonical units (evidence of 31943c5, turned into the mechanism).

Before the mechanism, ``canonical_row`` stamped the **live** ``CONTRACT_SCHEMA_VERSION`` and the
normalizer re-fingerprinted (``row_integrity.check_batch_snapshot``) and compared column by column
(``_exact``) every committed unit with rows rebuilt at that live version: a 2.0.0 -> 2.1.0 bump made
re-processing a committed 2.0.0 unit fail with ``batch ... was committed with other content``
(the strict-xfail evidence committed in M0).

Now (ADR-0052 "Implementation note — versioned replay", V1 / V2): the unit's contract version is a
write-time fact recovered from its committed rows, like its block base and ready time. A committed
unit — whole or partly written — is rebuilt, compared and completed at that recorded version; only
a unit with nothing committed is written at the current version
(``infrastructure.contract_version.new_group_version``). The checks are as strict as before: the
batch fingerprint still tells versions apart, and a unit that records two versions, or a version
this code never published, fails closed before any clock reading or write.

At the current version (M1) a later writer is simulated by replacing the new-group version source;
the persisted rows are real committed Iceberg snapshots (SQLite catalog).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from pyiceberg.expressions import EqualTo

from core.domain.base import CONTRACT_SCHEMA_VERSION, PUBLISHED_CONTRACT_SCHEMA_VERSIONS
from infrastructure import contract_version
from infrastructure.canonical import rules
from infrastructure.canonical.normalizer import DEFAULT_MICROBATCH_ROWS, unit_batch_id
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.revision.row_integrity import batch, batch_rows, check_batch_snapshot
from tests.infrastructure.canonical import canonical_support as c
from tests.infrastructure.contract_era import written_at
from tests.infrastructure.e2e import first_slice_support as fs
from tests.infrastructure.revision import rest_store_support as ss
from tests.infrastructure.revision.rest_store_support import (
    Crash,
    ProxyCatalog,
    RestHarness,
    StepClock,
    utc,
)

K_ARCHIVE = utc(2023, 12, 1)
K_NORM = utc(2023, 12, 6)
#: A later minor no code has published: what a later writer would stamp.
LATER = "2.99.0"


def _state(h: RestHarness) -> tuple[Any, ...]:
    return (h.head(c.TRADES.table), h.rows(c.TRADES))


def _versions(h: RestHarness) -> set[str]:
    return {row["contract_schema_version"] for row in h.rows(c.TRADES)}


def _later_writer(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A writer whose new groups would be stamped ``LATER``; records every call."""
    calls: list[str] = []

    def later() -> str:
        calls.append(LATER)
        return LATER

    monkeypatch.setattr(contract_version, "new_group_version", later)
    return calls


def _committed_unit(h: RestHarness, version: str = CONTRACT_SCHEMA_VERSION) -> str:
    archive = c.ingest_archive(
        h, "agg_trades", ss.archive_agg_lines(ss.agg_items(3)), knowledge=K_ARCHIVE
    )
    c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    rows = h.rows(c.TRADES)
    assert rows and _versions(h) == {version}
    return archive


def _crash_partial(h: RestHarness, count: int, microbatch_rows: int) -> str:
    """A unit whose normalization stopped after its first batch (committed at the current
    version). Only the pre-ADR-0108 per-batch layout can stop half way, so the fixture writes that
    old history (``legacy``); completing it is the read-only compatibility of ADR-0108 §7."""
    items = ss.agg_items(count)
    archive = c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(items), knowledge=K_ARCHIVE)
    proxy = ProxyCatalog(h.adapter, after=ss.crash_after_commits(1, table=c.TRADES.table))
    with pytest.raises(Crash):
        c.normalizer(
            h,
            clock=StepClock(start=K_NORM),
            adapter=proxy,
            microbatch_rows=microbatch_rows,
            legacy=True,
        ).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    return archive


def _raw(h: RestHarness) -> list[dict[str, Any]]:
    return sorted(h.rows(c.ARCHIVE_AGGS), key=lambda row: row["archive_line_number"])


def test_replay_after_a_minor_bump_is_idempotent(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M0 strict xfail, now passing: the replay rebuilds at the unit's recorded version."""
    archive = _committed_unit(h)
    before = _state(h)
    calls = _later_writer(monkeypatch)
    clock = StepClock(start=K_NORM)
    again = c.normalizer(h, clock=clock).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert again.replayed
    assert _state(h) == before  # read as recorded, not rewritten
    assert calls == [] and clock.calls == 0  # the writer's version is never asked on a replay
    verified = c.normalizer(h, clock=StepClock(start=K_NORM)).verify_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    assert {row["contract_schema_version"] for row in verified} == {CONTRACT_SCHEMA_VERSION}


def test_the_batch_check_still_tells_contract_versions_apart(h: RestHarness) -> None:
    """Strictness is unchanged: rows of the unit rebuilt at another version do not match the
    committed batch (the failure the M0 evidence pinned), whatever the rest of the row."""
    archive = _committed_unit(h)
    [snapshot] = [s for s in h.history(c.TRADES.table) if s.batch_id and archive in s.batch_id]
    channel = rules.raw_channel_of(c.ARCHIVE_AGGS.table)
    [base] = {row["arrival_seq"] // rules.ARRIVAL_SEQ_STRIDE for row in h.rows(c.TRADES)}

    def rebuilt(version: str) -> list[Any]:
        built = [
            rules.canonical_row(
                channel,
                raw,
                base=base * rules.ARRIVAL_SEQ_STRIDE,
                ready_time=K_NORM,
                contract_schema_version=version,
            )
            for raw in _raw(h)
        ]
        return batch_rows(batch(c.TRADES, built))

    assert snapshot.batch_id is not None
    check_batch_snapshot(c.TRADES, snapshot.batch_id, snapshot, rebuilt(CONTRACT_SCHEMA_VERSION))
    with pytest.raises(CatalogIntegrityError, match="was committed with other content"):
        check_batch_snapshot(c.TRADES, snapshot.batch_id, snapshot, rebuilt(LATER))


def test_the_row_column_is_the_envelope_of_the_record_it_maps(h: RestHarness) -> None:
    """One source (Codex M0 condition 4): the column is the version the builder was given."""
    c.ingest_archive(h, "agg_trades", ss.archive_agg_lines(ss.agg_items(1)), knowledge=K_ARCHIVE)
    channel = rules.raw_channel_of(c.ARCHIVE_AGGS.table)
    [raw] = _raw(h)
    for version in (CONTRACT_SCHEMA_VERSION, LATER):
        row = rules.canonical_row(
            channel, raw, base=0, ready_time=K_NORM, contract_schema_version=version
        )
        assert row["contract_schema_version"] == version
    with pytest.raises(TypeError, match="contract_schema_version"):
        rules.canonical_row(channel, raw, base=0, ready_time=K_NORM)  # type: ignore[call-arg]


def test_replay_at_the_recorded_version_is_idempotent(h: RestHarness) -> None:
    """Control: without any change the same replay is idempotent (the harness is not the cause)."""
    archive = _committed_unit(h)
    before = _state(h)
    again = c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    assert again.replayed
    assert _state(h) == before


def test_a_partial_unit_is_completed_at_its_recorded_version(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _crash_partial(h, 5, 2)
    assert len(h.rows(c.TRADES)) == 2 and _versions(h) == {CONTRACT_SCHEMA_VERSION}
    calls = _later_writer(monkeypatch)
    clock = StepClock(start=K_NORM)
    out = c.normalizer(h, clock=clock).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert out.batch_count == 3 and out.replayed_batch_count == 1
    assert len(h.rows(c.TRADES)) == 5
    assert _versions(h) == {CONTRACT_SCHEMA_VERSION}  # completed at the recorded version
    assert calls == [] and clock.calls == 0


def test_a_new_unit_is_written_at_the_writers_version(
    h: RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V2 through the one seam: a unit with nothing committed asks the writer's version once."""
    archive = c.ingest_archive(
        h, "agg_trades", ss.archive_agg_lines(ss.agg_items(2)), knowledge=K_ARCHIVE
    )
    calls = _later_writer(monkeypatch)
    c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert calls == [LATER] and _versions(h) == {LATER}


def test_a_unit_recording_two_versions_fails_closed(h: RestHarness) -> None:
    archive = _crash_partial(h, 5, 2)
    channel = rules.raw_channel_of(c.ARCHIVE_AGGS.table)
    [base] = {row["arrival_seq"] // rules.ARRIVAL_SEQ_STRIDE for row in h.rows(c.TRADES)}
    forged = [
        rules.canonical_row(
            channel,
            raw,
            base=base * rules.ARRIVAL_SEQ_STRIDE,
            ready_time=K_NORM,
            contract_schema_version=LATER,
        )
        for raw in _raw(h)[2:4]
    ]
    h.forge_rows(c.TRADES, forged, unit_batch_id(archive, 5, 2, 1))
    before = _state(h)
    with pytest.raises(CatalogIntegrityError, match="records 2 contract versions"):
        c.normalizer(h, clock=StepClock(start=K_NORM)).verify_unit(c.ARCHIVE_AGGS.table, archive)
    clock = StepClock(start=K_NORM)
    with pytest.raises(CatalogIntegrityError, match="records 2 contract versions"):
        c.normalizer(h, clock=clock).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert clock.calls == 0 and _state(h) == before


def test_a_unit_recorded_at_an_unpublished_version_fails_closed(h: RestHarness) -> None:
    archive = c.ingest_archive(
        h, "agg_trades", ss.archive_agg_lines(ss.agg_items(3)), knowledge=K_ARCHIVE
    )
    channel = rules.raw_channel_of(c.ARCHIVE_AGGS.table)
    base = rules.next_block_base(None)
    forged = [
        rules.canonical_row(
            channel, raw, base=base, ready_time=K_NORM, contract_schema_version=LATER
        )
        for raw in _raw(h)
    ]
    h.forge_rows(c.TRADES, forged, unit_batch_id(archive, 3, DEFAULT_MICROBATCH_ROWS, 0))
    before = _state(h)
    for read in ("verify_unit", "normalize_unit"):
        clock = StepClock(start=K_NORM)
        with pytest.raises(CatalogIntegrityError, match="not one of the published"):
            getattr(c.normalizer(h, clock=clock), read)(c.ARCHIVE_AGGS.table, archive)
        assert clock.calls == 0
    assert _state(h) == before


def test_committed_batches_without_version_evidence_fail_closed(h: RestHarness) -> None:
    """A unit's batches committed but its rows gone: no recorded version to replay at."""
    archive = _committed_unit(h)
    h.delete_rows(c.TRADES, EqualTo("lineage_source_revision_id", archive))  # type: ignore[call-arg, arg-type]
    before = _state(h)
    clock = StepClock(start=K_NORM)
    with pytest.raises(CatalogIntegrityError, match="rows are gone"):
        c.normalizer(h, clock=clock).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert clock.calls == 0 and _state(h) == before


# =========================================================================================
# units committed at every earlier published version are read and replayed at the current version.
# =========================================================================================

#: Every published version older than the current one: each must replay unchanged.
PRIOR = PUBLISHED_CONTRACT_SCHEMA_VERSIONS[:-1]
prior_versions = pytest.mark.parametrize("old", PRIOR)


def test_the_bump_is_real() -> None:
    assert CONTRACT_SCHEMA_VERSION == "2.6.0"
    assert PUBLISHED_CONTRACT_SCHEMA_VERSIONS == (*PRIOR, CONTRACT_SCHEMA_VERSION)


@prior_versions
def test_a_committed_earlier_unit_replays_unchanged_after_the_bump(
    h: RestHarness, old: str
) -> None:
    with written_at(old):
        archive = _committed_unit(h, old)
    before = _state(h)
    clock = StepClock(start=K_NORM)
    again = c.normalizer(h, clock=clock).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert again.replayed and clock.calls == 0
    assert _state(h) == before and _versions(h) == {old}
    verified = c.normalizer(h, clock=StepClock(start=K_NORM)).verify_unit(
        c.ARCHIVE_AGGS.table, archive
    )
    assert {row["contract_schema_version"] for row in verified} == {old}
    records = c.records(h, verified[0]["observation_key"])
    assert {record.schema_version for record in records} == {old}  # read as recorded


@prior_versions
def test_an_earlier_partial_unit_is_completed_at_its_version_after_the_bump(
    h: RestHarness, old: str
) -> None:
    with written_at(old):
        archive = _crash_partial(h, 5, 2)
    assert _versions(h) == {old}
    clock = StepClock(start=K_NORM + timedelta(hours=1))
    out = c.normalizer(h, clock=clock).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert out.batch_count == 3 and out.replayed_batch_count == 1
    assert clock.calls == 0 and len(h.rows(c.TRADES)) == 5
    assert _versions(h) == {old}  # never a mixed unit


@prior_versions
def test_a_unit_mixing_an_earlier_and_the_current_fails_closed(h: RestHarness, old: str) -> None:
    with written_at(old):
        archive = _crash_partial(h, 5, 2)
    channel = rules.raw_channel_of(c.ARCHIVE_AGGS.table)
    [base] = {row["arrival_seq"] // rules.ARRIVAL_SEQ_STRIDE for row in h.rows(c.TRADES)}
    forged = [
        rules.canonical_row(
            channel,
            raw,
            base=base * rules.ARRIVAL_SEQ_STRIDE,
            ready_time=K_NORM,
            contract_schema_version=CONTRACT_SCHEMA_VERSION,
        )
        for raw in _raw(h)[2:4]
    ]
    h.forge_rows(c.TRADES, forged, unit_batch_id(archive, 5, 2, 1))
    before = _state(h)
    clock = StepClock(start=K_NORM)
    with pytest.raises(CatalogIntegrityError, match="records 2 contract versions"):
        c.normalizer(h, clock=clock).normalize_unit(c.ARCHIVE_AGGS.table, archive)
    assert clock.calls == 0 and _state(h) == before


@prior_versions
def test_an_earlier_and_the_current_units_share_one_table(h: RestHarness, old: str) -> None:
    """V6: an earlier-version unit and a current one side by side; both verify, replay, select."""
    with written_at(old):
        btc = fs.ingest_archive_for(
            h,
            "agg_trades",
            "BTCUSDT",
            ss.archive_agg_lines(ss.agg_items(3)),
            knowledge=K_ARCHIVE,
            request_id="archive-btc",
        )
        c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(c.ARCHIVE_AGGS.table, btc)
    eth = fs.ingest_archive_for(
        h,
        "agg_trades",
        "ETHUSDT",
        ss.archive_agg_lines(ss.agg_items(2)),
        knowledge=K_ARCHIVE,
        request_id="archive-eth",
    )
    c.normalizer(h, clock=StepClock(start=K_NORM)).normalize_unit(c.ARCHIVE_AGGS.table, eth)
    by_symbol: dict[str, set[str]] = {}
    for row in h.rows(c.TRADES):
        by_symbol.setdefault(row["symbol"], set()).add(row["contract_schema_version"])
    assert by_symbol == {"BTC-USDT": {old}, "ETH-USDT": {CONTRACT_SCHEMA_VERSION}}
    raw_versions = {row["contract_schema_version"] for row in h.rows(c.ARCHIVES)}
    assert raw_versions == {old, CONTRACT_SCHEMA_VERSION}

    before = _state(h)
    for unit in (btc, eth):
        n = c.normalizer(h, clock=StepClock(start=K_NORM))
        assert n.normalize_unit(c.ARCHIVE_AGGS.table, unit).replayed
        assert n.verify_unit(c.ARCHIVE_AGGS.table, unit)
    assert _state(h) == before

    for unit, version in ((btc, old), (eth, CONTRACT_SCHEMA_VERSION)):
        verified = c.normalizer(h, clock=StepClock(start=K_NORM)).verify_unit(
            c.ARCHIVE_AGGS.table, unit
        )
        assert {row["contract_schema_version"] for row in verified} == {version}
        for committed in verified:
            [record] = [
                item for item in c.records(h, committed["observation_key"])
                if item.revision_id == committed["revision_id"]
            ]  # fmt: skip
            assert record.schema_version == version

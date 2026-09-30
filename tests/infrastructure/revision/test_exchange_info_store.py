"""E2 snapshot store: one Raw source revision per committed snapshot (ADR-0029 §1).

Real collector checkpoints (mock venue), real SQLite catalog, real store. The catalog proxy from
the D3E tests injects crashes and interleaved writers around commits.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.contracts.catalog import CommitRequest
from core.domain.base import canonical_json
from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.collector.binance_exchange_info import ExchangeInfoRequest
from infrastructure.revision.exchange_info_availability import (
    ExchangeInfoAvailabilitySubject,
    exchange_info_gap_for,
)
from infrastructure.revision.exchange_info_store import (
    ExchangeInfoRowVerifier,
    ExchangeInfoStoreConflict,
    ExchangeInfoStoreError,
    snapshot_batch_id,
    snapshot_columns,
)
from infrastructure.revision.row_integrity import batch
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.streaming.runs import RunLimits, iter_run
from tests.infrastructure.revision import exchange_info_support as xs
from tests.infrastructure.revision.exchange_info_support import (
    EXCHANGE_INFO,
    T1,
    T2,
    TRADING,
    Harness,
)
from tests.infrastructure.revision.rest_store_support import Crash, ProxyCatalog

TABLE = EXCHANGE_INFO.table


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    with xs.harness(tmp_path) as opened:
        yield opened


def _commit_forged(h: Harness, row: Mapping[str, Any], batch_id: str) -> None:
    table = batch(EXCHANGE_INFO, [row])
    h.adapter.commit_batch(
        CommitRequest(
            table=TABLE,
            batch_id=batch_id,
            batch_fingerprint=EXCHANGE_INFO.fingerprint_rule.fingerprint(table),
            row_count=1,
            expected_parent_snapshot_id=h.head(TABLE),
        ),
        table,
    )


def _verify(h: Harness) -> list[Mapping[str, Any]]:
    verifier = ExchangeInfoRowVerifier(h.adapter, h.storage, xs.ORIGIN)
    try:
        return list(verifier.verify_table(h.head(TABLE)).rows)
    finally:
        verifier.close()


# --------------------------------------------------------------------------- the revision


def test_a_committed_snapshot_becomes_one_proven_raw_source_revision(h: Harness) -> None:
    snapshot = h.collect("snap-1", {"BTCUSDT": "TRADING", "ETHUSDT": None}, T1, server_time=5)
    stored = h.ingest("snap-1")
    [row] = h.rows(TABLE)
    assert stored.revision_id == row["revision_id"] and stored.first_delivery
    assert not stored.replayed and stored.arrival_seq == row["arrival_seq"] == 0
    assert stored.missing_symbols == ("ETHUSDT",)
    assert row["observation_key"] == f"binance:spot:exchange-info:{snapshot.request_identity}"
    assert row["source_id"] == "binance.public.spot.exchange-info@1.0.0"
    assert row["payload_hash"] == row["object_sha256"] == snapshot.body.sha256
    assert (row["event_time"], row["event_end_time"]) == (T1 - xs.MS, T1)
    assert row["available_time"] == row["ingest_time"] == row["retrieved_at"] == T1
    assert row["knowledge_time"] == xs.KNOWLEDGE
    assert row["availability_evidence"] == []
    assert row["availability_evidence_gap"] == exchange_info_gap_for(
        ExchangeInfoAvailabilitySubject.SNAPSHOT
    )
    assert row["supersedes"] == [] and row["precedence_evidence"] == []
    assert row["requested_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert row["symbols"] == [
        {"symbol": "BTCUSDT", "status": "TRADING", "base_asset": "BTC", "quote_asset": "USDT"}
    ]
    assert row["server_time_raw"] == 5 and row["http_status"] == 200
    assert row["request_path"] == "/api/v3/exchangeInfo"
    assert _verify(h) == [row]


def test_bounded_raw_table_proof_spools_sorted_rows_without_proof_cache(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    h.observe("snap-2", {"BTCUSDT": "HALT", "ETHUSDT": "TRADING"}, T2)
    scratch = LocalFileStorageAdapter(
        (h.tmp_path / "scratch-warehouse").as_uri(),
        (h.tmp_path / "scratch-stage").as_uri(),
    )
    verifier = ExchangeInfoRowVerifier(h.adapter, h.storage, xs.ORIGIN)
    try:
        root = verifier.verify_table_bounded(
            h.head(TABLE),
            scratch_storage=scratch,
            capacity=1,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=2, leaf_max_bytes=8192, fanout=2),
            max_record_bytes=4096,
        )
        assert root is not None and root.record_count == 2
        assert verifier._proven == {}
        with iter_run(scratch, root) as rows:
            actual = list(rows)
        assert [item["sort_key"] for item in actual] == sorted(item["sort_key"] for item in actual)
        assert sorted(item["row"]["arrival_seq"] for item in actual) == [0, 1]
    finally:
        verifier.close()
        scratch.close()


def test_bounded_raw_table_proof_rejects_row_over_byte_cap(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    scratch = LocalFileStorageAdapter(
        (h.tmp_path / "scratch-warehouse").as_uri(),
        (h.tmp_path / "scratch-stage").as_uri(),
    )
    verifier = ExchangeInfoRowVerifier(h.adapter, h.storage, xs.ORIGIN)
    try:
        with pytest.raises(CatalogIntegrityError, match="max_record_bytes"):
            verifier.verify_table_bounded(
                h.head(TABLE),
                scratch_storage=scratch,
                capacity=1,
                merge_fanout=2,
                limits=RunLimits(leaf_max_records=2, leaf_max_bytes=8192, fanout=2),
                max_record_bytes=32,
            )
    finally:
        verifier.close()
        scratch.close()


def test_bounded_raw_table_proof_checks_each_snapshot_batch_fingerprint(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    scratch = LocalFileStorageAdapter(
        (h.tmp_path / "scratch-warehouse").as_uri(),
        (h.tmp_path / "scratch-stage").as_uri(),
    )
    verifier = ExchangeInfoRowVerifier(h.adapter, h.storage, xs.ORIGIN)

    class WrongFingerprintCatalog:
        def __getattr__(self, name: str) -> Any:
            return getattr(h.adapter, name)

        def get_snapshot(self, table: str, snapshot_id: str) -> Any:
            snapshot = h.adapter.get_snapshot(table, snapshot_id)
            if table == TABLE:
                return snapshot.model_copy(update={"batch_fingerprint": "0" * 64})
            return snapshot

        def history(self, table: str, snapshot_id: str) -> Any:
            for snapshot in h.adapter.history(table, snapshot_id):
                if table == TABLE:
                    yield snapshot.model_copy(update={"batch_fingerprint": "0" * 64})
                else:
                    yield snapshot

    verifier._catalog = WrongFingerprintCatalog()
    try:
        with pytest.raises(CatalogIntegrityError, match="other content"):
            verifier.verify_table_bounded(
                h.head(TABLE),
                scratch_storage=scratch,
                capacity=1,
                merge_fanout=2,
                limits=RunLimits(leaf_max_records=2, leaf_max_bytes=8192, fanout=2),
                max_record_bytes=4096,
            )
    finally:
        verifier.close()
        scratch.close()


def test_bounded_raw_table_proof_rejects_inconsistent_history_metadata(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    scratch = LocalFileStorageAdapter(
        (h.tmp_path / "scratch-warehouse").as_uri(),
        (h.tmp_path / "scratch-stage").as_uri(),
    )
    verifier = ExchangeInfoRowVerifier(h.adapter, h.storage, xs.ORIGIN)

    class WrongHistoryCatalog:
        def __getattr__(self, name: str) -> Any:
            return getattr(h.adapter, name)

        def history(self, table: str, snapshot_id: str) -> Any:
            for snapshot in h.adapter.history(table, snapshot_id):
                if table == TABLE:
                    yield snapshot.model_copy(update={"total_rows": snapshot.total_rows + 1})
                else:
                    yield snapshot

    verifier._catalog = WrongHistoryCatalog()
    try:
        with pytest.raises(CatalogIntegrityError, match="inconsistent total_rows"):
            verifier.verify_table_bounded(
                h.head(TABLE),
                scratch_storage=scratch,
                capacity=1,
                merge_fanout=2,
                limits=RunLimits(leaf_max_records=2, leaf_max_bytes=8192, fanout=2),
                max_record_bytes=4096,
            )
    finally:
        verifier.close()
        scratch.close()


def test_bounded_raw_table_proof_closes_catalog_reader_after_iteration_error(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    scratch = LocalFileStorageAdapter(
        (h.tmp_path / "scratch-warehouse").as_uri(),
        (h.tmp_path / "scratch-stage").as_uri(),
    )
    verifier = ExchangeInfoRowVerifier(h.adapter, h.storage, xs.ORIGIN)
    opened: list[Any] = []

    class FailingReader:
        def __init__(self, inner: Any) -> None:
            self.inner = inner
            self.closed = False

        def __iter__(self) -> Any:
            iterator = iter(self.inner)
            yield next(iterator)
            raise RuntimeError("injected reader failure")

        def close(self) -> None:
            self.closed = True
            self.inner.close()

    class FailingCatalog:
        def __getattr__(self, name: str) -> Any:
            return getattr(h.adapter, name)

        def scan_column_batches(self, *args: Any, **kwargs: Any) -> Any:
            reader = FailingReader(h.adapter.scan_column_batches(*args, **kwargs))
            opened.append(reader)
            return reader

    verifier._catalog = FailingCatalog()
    try:
        with pytest.raises(RuntimeError, match="injected reader failure"):
            verifier.verify_table_bounded(
                h.head(TABLE),
                scratch_storage=scratch,
                capacity=1,
                merge_fanout=2,
                limits=RunLimits(leaf_max_records=2, leaf_max_bytes=8192, fanout=2),
                max_record_bytes=4096,
            )
        assert len(opened) == 1 and opened[0].closed
    finally:
        verifier.close()
        scratch.close()


def test_bounded_raw_row_byte_cap_is_exact_for_unicode_and_escaped_text() -> None:
    for row in ({"label": "雪"}, {"label": 'quote" line\n slash\\'}):
        exact = len(canonical_json(row).encode("utf-8")) + 1
        ExchangeInfoRowVerifier._check_bounded_row(row, exact)
        with pytest.raises(CatalogIntegrityError, match="max_record_bytes"):
            ExchangeInfoRowVerifier._check_bounded_row(row, exact - 1)


def test_bounded_raw_table_proof_rejects_shared_evidence_storage(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    verifier = ExchangeInfoRowVerifier(h.adapter, h.storage, xs.ORIGIN)
    try:
        with pytest.raises(ValueError, match="distinct adapter"):
            verifier.verify_table_bounded(
                h.head(TABLE),
                scratch_storage=h.storage,
                capacity=1,
                merge_fanout=2,
                limits=RunLimits(leaf_max_records=2, leaf_max_bytes=8192, fanout=2),
                max_record_bytes=4096,
            )
    finally:
        verifier.close()


def test_bounded_raw_table_proof_empty_head_has_no_run(h: Harness) -> None:
    scratch = LocalFileStorageAdapter(
        (h.tmp_path / "scratch-warehouse").as_uri(),
        (h.tmp_path / "scratch-stage").as_uri(),
    )
    verifier = ExchangeInfoRowVerifier(h.adapter, h.storage, xs.ORIGIN)
    try:
        assert (
            verifier.verify_table_bounded(
                None,
                scratch_storage=scratch,
                capacity=1,
                merge_fanout=2,
                limits=RunLimits(leaf_max_records=2, leaf_max_bytes=8192, fanout=2),
                max_record_bytes=4096,
            )
            is None
        )
        assert not list((h.tmp_path / "scratch-warehouse").rglob("*.jsonl"))
    finally:
        verifier.close()
        scratch.close()


def test_later_snapshots_are_further_revisions_of_the_same_key(h: Harness) -> None:
    first = h.observe("snap-1", TRADING, T1)
    second = h.observe("snap-2", TRADING, T2, server_time=2)
    assert first.observation_key == second.observation_key
    assert first.revision_id != second.revision_id
    assert (first.arrival_seq, second.arrival_seq) == (0, 1)
    assert len(_verify(h)) == 2


# --------------------------------------------------------------------------- idempotency


def test_ingesting_again_is_a_replay_that_commits_nothing(h: Harness) -> None:
    first = h.observe("snap-1", TRADING, T1)
    head = h.head(TABLE)
    again = h.ingest("snap-1")
    assert again.replayed and again.commit.snapshot_id == first.commit.snapshot_id
    assert (again.revision_id, again.arrival_seq, again.knowledge_time) == (
        first.revision_id,
        first.arrival_seq,
        first.knowledge_time,
    )
    assert h.head(TABLE) == head and len(h.rows(TABLE)) == 1


def test_the_same_bytes_from_another_attempt_adopt_the_first_delivery(h: Harness) -> None:
    first = h.observe("snap-1", TRADING, T1)
    h.collect("snap-2", TRADING, T2)  # the identical body (same serverTime) answered later
    other = h.ingest("snap-2")
    assert other.revision_id == first.revision_id and other.replayed
    assert not other.first_delivery
    [row] = h.rows(TABLE)
    assert row["collection_request_id"] == "snap-1" and row["retrieved_at"] == T1


def test_a_crash_after_the_commit_is_repaired_by_ingesting_again(h: Harness) -> None:
    h.collect("snap-1", TRADING, T1)

    def crash(request: CommitRequest, result: object) -> None:
        raise Crash("after commit")

    proxy = ProxyCatalog(h.adapter, after=crash)
    with h.store(proxy) as store, pytest.raises(Crash):
        store.ingest_snapshot(ExchangeInfoRequest("snap-1"))
    head = h.head(TABLE)
    recovered = h.ingest("snap-1")
    assert recovered.replayed and h.head(TABLE) == head and len(h.rows(TABLE)) == 1


def test_a_writer_that_moves_the_head_first_is_retried_not_overwritten(h: Harness) -> None:
    h.collect("snap-1", TRADING, T1)
    h.collect("snap-2", TRADING, T2, server_time=2)
    fired: list[str] = []

    def rival(request: CommitRequest) -> None:
        if not fired:
            fired.append(request.batch_id)
            h.ingest("snap-2")

    with h.store(ProxyCatalog(h.adapter, before=rival)) as store:
        ours = store.ingest_snapshot(ExchangeInfoRequest("snap-1"))
    rows = sorted(h.rows(TABLE), key=lambda row: row["arrival_seq"])
    assert [row["collection_request_id"] for row in rows] == ["snap-2", "snap-1"]
    assert ours.arrival_seq == 1 and fired
    assert len(_verify(h)) == 2


# --------------------------------------------------------------------------- fail closed


def test_only_committed_accepted_snapshots_become_revisions(h: Harness) -> None:
    with pytest.raises(ExchangeInfoStoreError, match="no committed snapshot"):
        h.ingest("never-collected")
    h.serve(b'{"serverTime":1,"symbols":[],"x":0.5}')
    h.wire_clock.at(T1)
    with h.collector() as collector, pytest.raises(Exception, match="fractional"):
        collector.collect(ExchangeInfoRequest("bad"))
    with pytest.raises(ExchangeInfoStoreError, match="never a Raw revision"):
        h.ingest("bad")
    assert h.rows(TABLE) == [] and h.head(TABLE) is None


def test_a_clock_before_the_ingest_time_is_refused(h: Harness) -> None:
    h.collect("snap-1", TRADING, T1)
    h.clock.now = T1 - xs.MS
    with pytest.raises(ExchangeInfoStoreConflict, match="precede"):
        h.ingest("snap-1")
    assert h.rows(TABLE) == []


# --------------------------------------------------------------------------- forged rows


def test_a_forged_row_under_our_revision_id_is_never_adopted(h: Harness) -> None:
    snapshot = h.collect("snap-1", TRADING, T1)
    row = snapshot_columns(snapshot, arrival_seq=0, knowledge_time=xs.KNOWLEDGE)
    forged = dict(row, symbols=[dict(row["symbols"][0], status="HALT"), row["symbols"][1]])
    _commit_forged(h, forged, snapshot_batch_id(row["revision_id"], 0))
    with pytest.raises(CatalogIntegrityError, match="disagrees"):
        h.ingest("snap-1")


@pytest.mark.parametrize(
    ("forge", "message"),
    [
        # a status nobody observed
        (
            lambda row: (
                dict(row, arrival_seq=1, symbols=[dict(row["symbols"][0], status="HALT")]),
                snapshot_batch_id(row["revision_id"], 1),
            ),
            "disagrees with its verified checkpoint",
        ),
        # a delivery that has no checkpoint behind it
        (
            lambda row: (
                dict(row, arrival_seq=1, collection_request_id="invented"),
                snapshot_batch_id(row["revision_id"], 1),
            ),
            "no committed checkpoint",
        ),
        # a second copy of a real row under a lawful-looking batch id
        (
            lambda row: (dict(row, arrival_seq=1), snapshot_batch_id(row["revision_id"], 1)),
            "held by 2 rows",
        ),
        # a real row's copy under a foreign batch id
        (
            lambda row: (dict(row, arrival_seq=1), "someone-else.batch"),
            "not a one-row snapshot append",
        ),
        # knowledge before ingest
        (
            lambda row: (
                dict(row, arrival_seq=1, knowledge_time=T1 - xs.MS),
                snapshot_batch_id(row["revision_id"], 1),
            ),
            "not lawful",
        ),
    ],
)
def test_forged_rows_make_the_table_unprovable(h: Harness, forge: Any, message: str) -> None:
    h.observe("snap-1", TRADING, T1)
    [row] = h.rows(TABLE)
    forged, batch_id = forge(row)
    _commit_forged(h, forged, batch_id)
    with pytest.raises(CatalogIntegrityError, match=message):
        _verify(h)


def test_a_rewritten_checkpoint_body_makes_committed_rows_unprovable(h: Harness) -> None:
    h.observe("snap-1", TRADING, T1)
    verifier = ExchangeInfoRowVerifier(h.adapter, h.storage, xs.ORIGIN)
    snapshot = verifier.load_snapshot(ExchangeInfoRequest("snap-1"))
    verifier.close()
    assert snapshot is not None
    path = xs_path(h, snapshot.body.key)
    path.chmod(0o644)
    path.write_bytes(xs.body({"BTCUSDT": "HALT", "ETHUSDT": "TRADING"}))
    with pytest.raises(CatalogIntegrityError, match="does not reproduce"):
        _verify(h)


def xs_path(h: Harness, key: str) -> Path:
    from infrastructure.settings import local_file_uri_to_path

    ref = h.storage.lookup(key)
    assert ref is not None
    return local_file_uri_to_path(ref.uri, field_name="object uri")


def test_the_row_builder_refuses_unlawful_inputs(h: Harness) -> None:
    snapshot = h.collect("snap-1", TRADING, T1)
    with pytest.raises(ValueError):
        snapshot_columns(snapshot, arrival_seq=-1, knowledge_time=xs.KNOWLEDGE)
    with pytest.raises(ValueError):
        snapshot_columns(snapshot, arrival_seq=0, knowledge_time=T1 - xs.MS)
    with pytest.raises(ValueError):
        snapshot_columns(replace(snapshot, requested_at=T1), arrival_seq=0, knowledge_time=T1)

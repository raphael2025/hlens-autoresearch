"""Bounded listing observation inputs preserve the proved Raw snapshot membership."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from infrastructure.canonical import listing_rules as lr
from infrastructure.canonical.listing_runs import (
    iter_planned_listing_revisions,
    listing_observations_run,
)
from infrastructure.revision.exchange_info_store import ExchangeInfoRowVerifier
from infrastructure.storage import LocalFileStorageAdapter
from infrastructure.streaming.runs import RunLimits, iter_run
from tests.infrastructure.revision import exchange_info_support as xs
from tests.infrastructure.revision.exchange_info_support import T1, Harness


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    with xs.harness(tmp_path) as opened:
        yield opened


def test_listing_observations_run_keeps_snapshot_membership_and_orders_high_cardinality(
    h: Harness,
) -> None:
    for index in range(9):
        at = T1 + timedelta(seconds=index)
        h.observe(
            f"listing-{index}",
            {"BTCUSDT": f"UNKNOWN_{index}", "ETHUSDT": "TRADING"},
            at,
        )
    scratch = LocalFileStorageAdapter(
        (h.tmp_path / "scratch-warehouse").as_uri(),
        (h.tmp_path / "scratch-stage").as_uri(),
    )
    verifier = ExchangeInfoRowVerifier(h.adapter, h.storage, xs.ORIGIN)
    try:
        raw_root = verifier.verify_table_bounded(
            h.head(xs.EXCHANGE_INFO.table),
            scratch_storage=scratch,
            capacity=2,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=4, leaf_max_bytes=32768, fanout=2),
            max_record_bytes=16384,
        )
        assert raw_root is not None and raw_root.record_count == 9
        root = listing_observations_run(
            raw_root,
            storage=scratch,
            capacity=2,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=4, leaf_max_bytes=32768, fanout=2),
            max_record_bytes=4096,
        )
        assert root is not None and root.record_count == 18
        with iter_run(scratch, root) as reader:
            observations = list(reader)
        keys = [
            (row["venue_symbol"], row["retrieved_at"], row["snapshot_revision_id"])
            for row in observations
        ]
        assert keys == sorted(keys)
        assert {row["snapshot_ordinal"] for row in observations} == set(range(9))
        assert all(row["snapshot_id"] for row in observations)
    finally:
        verifier.close()
        scratch.close()


def test_listing_observations_run_preserves_missing_symbol_as_unresolved(h: Harness) -> None:
    h.observe("missing-eth", {"BTCUSDT": "TRADING", "ETHUSDT": None}, T1)
    scratch = LocalFileStorageAdapter(
        (h.tmp_path / "scratch-warehouse").as_uri(),
        (h.tmp_path / "scratch-stage").as_uri(),
    )
    verifier = ExchangeInfoRowVerifier(h.adapter, h.storage, xs.ORIGIN)
    try:
        raw_root = verifier.verify_table_bounded(
            h.head(xs.EXCHANGE_INFO.table),
            scratch_storage=scratch,
            capacity=1,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=2, leaf_max_bytes=32768, fanout=2),
            max_record_bytes=16384,
        )
        assert raw_root is not None
        root = listing_observations_run(
            raw_root,
            storage=scratch,
            capacity=1,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=2, leaf_max_bytes=32768, fanout=2),
            max_record_bytes=4096,
        )
        assert root is not None
        with iter_run(scratch, root) as reader:
            by_symbol = {row["venue_symbol"]: row for row in reader}
        assert by_symbol["ETHUSDT"]["status"] is None
        assert by_symbol["ETHUSDT"]["snapshot_ordinal"] == 0
    finally:
        verifier.close()
        scratch.close()


def test_streaming_chain_preserves_all_revision_ids_and_findings(h: Harness) -> None:
    for index in range(9):
        h.observe(
            f"chain-{index}",
            {"BTCUSDT": "TRADING" if index % 2 == 0 else "HALT", "ETHUSDT": "TRADING"},
            T1 + timedelta(seconds=index),
            server_time=index,
        )
    scratch = LocalFileStorageAdapter(
        (h.tmp_path / "scratch-warehouse").as_uri(),
        (h.tmp_path / "scratch-stage").as_uri(),
    )
    verifier = ExchangeInfoRowVerifier(h.adapter, h.storage, xs.ORIGIN)
    try:
        raw_root = verifier.verify_table_bounded(
            h.head(xs.EXCHANGE_INFO.table),
            scratch_storage=scratch,
            capacity=2,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=4, leaf_max_bytes=32768, fanout=2),
            max_record_bytes=16384,
        )
        assert raw_root is not None
        observation_root = listing_observations_run(
            raw_root,
            storage=scratch,
            capacity=2,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=4, leaf_max_bytes=32768, fanout=2),
            max_record_bytes=4096,
        )
        assert observation_root is not None
        with iter_run(scratch, observation_root) as reader:
            observations = list(reader)
        expected_ids: list[str] = []
        expected_findings: list[tuple[str, str, object, tuple[str, ...], str]] = []
        for symbol in sorted(lr.FIRST_SLICE_ASSETS):
            typed = [
                lr.Observation(
                    venue_symbol=row["venue_symbol"],
                    snapshot_revision_id=row["snapshot_revision_id"],
                    requested_at=row["requested_at"],
                    retrieved_at=row["retrieved_at"],
                    raw_knowledge_time=row["raw_knowledge_time"],
                    status=row["status"],
                    base_asset=row["base_asset"],
                    quote_asset=row["quote_asset"],
                )
                for row in observations
                if row["venue_symbol"] == symbol
            ]
            legacy = lr.derive_chain(symbol, typed)
            expected_ids.extend(item.revision_id for item in legacy.revisions)
            expected_findings.extend(
                (
                    finding.code,
                    finding.venue_symbol,
                    finding.observed_at,
                    finding.snapshot_revision_ids,
                    finding.detail,
                )
                for finding in legacy.findings
            )
        actual_findings: list[tuple[str, str, object, tuple[str, ...], str]] = []

        def collect_finding(
            code: str,
            symbol: str,
            at: datetime,
            revision_ids: Iterable[str],
            detail_for_count: Callable[[int], str],
        ) -> None:
            ids = tuple(revision_ids)
            actual_findings.append((code, symbol, at, ids, detail_for_count(len(ids))))

        with iter_run(scratch, observation_root) as reader:
            actual_ids = [
                item.revision_id
                for item in iter_planned_listing_revisions(
                    reader,
                    max_record_bytes=16384,
                    finding_sink=collect_finding,
                )
            ]
        assert actual_ids == expected_ids
        assert actual_findings == expected_findings
    finally:
        verifier.close()
        scratch.close()


def test_streaming_chain_spills_all_same_instant_tie_ids(h: Harness) -> None:
    for index in range(5):
        h.observe(
            f"tie-{index}",
            {"BTCUSDT": "TRADING", "ETHUSDT": "TRADING"},
            T1,
            server_time=index,
        )
    scratch = LocalFileStorageAdapter(
        (h.tmp_path / "scratch-warehouse").as_uri(),
        (h.tmp_path / "scratch-stage").as_uri(),
    )
    verifier = ExchangeInfoRowVerifier(h.adapter, h.storage, xs.ORIGIN)
    try:
        raw_root = verifier.verify_table_bounded(
            h.head(xs.EXCHANGE_INFO.table),
            scratch_storage=scratch,
            capacity=1,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=2, leaf_max_bytes=32768, fanout=2),
            max_record_bytes=16384,
        )
        assert raw_root is not None
        observation_root = listing_observations_run(
            raw_root,
            storage=scratch,
            capacity=1,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=2, leaf_max_bytes=32768, fanout=2),
            max_record_bytes=4096,
        )
        assert observation_root is not None
        seen: list[tuple[str, tuple[str, ...], str]] = []

        def collect(
            code: str,
            symbol: str,
            at: datetime,
            ids: Iterable[str],
            detail_for_count: Callable[[int], str],
        ) -> None:
            revision_ids = tuple(ids)
            seen.append((code, revision_ids, detail_for_count(len(revision_ids))))

        with iter_run(scratch, observation_root) as reader:
            assert (
                list(
                    iter_planned_listing_revisions(
                        reader,
                        max_record_bytes=16384,
                        finding_sink=collect,
                    )
                )
                == []
            )
        assert len(seen) == 2
        assert all(code == lr.FINDING_OBSERVATION_TIE for code, _, _ in seen)
        assert all(len(ids) == 5 for _, ids, _ in seen)
        assert all("5 snapshots observed" in detail for _, _, detail in seen)
    finally:
        verifier.close()
        scratch.close()

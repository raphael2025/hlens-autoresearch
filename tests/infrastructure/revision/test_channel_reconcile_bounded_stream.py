"""Focused input-side bounded stream checks for the D3E verified-edge reader."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.catalog.phase1_tables import (
    BINANCE_SPOT_AGG_TRADES,
    BINANCE_SPOT_REST_AGG_TRADES,
)
from infrastructure.revision.channel_reconcile import ChannelReconciler, VerifiedEdgeRunParams
from infrastructure.streaming.runs import RunLimits
from tests.infrastructure.collector import rest_support
from tests.infrastructure.revision import rest_store_support as revision_support


@pytest.fixture
def revision_harness(tmp_path: Path) -> Iterator[revision_support.RestHarness]:
    with revision_support.sqlite_harness(tmp_path) as opened:
        yield opened


def _ingest_rest(harness: revision_support.RestHarness, count: int, *, request_id: str) -> None:
    items = revision_support.agg_items(count)
    rest_support.queue_agg_chain(
        harness.venue, revision_support.SYMBOL, revision_support.T0, [items]
    )
    request = revision_support.agg_request(request_id, start_ms=revision_support.T0)
    collected = harness.collect(request)
    assert not isinstance(collected, Exception)
    harness.store(
        clock=revision_support.StepClock(datetime(2025, 2, 1, tzinfo=UTC))
    ).ingest_collection(request)


def _params(capacity: int) -> VerifiedEdgeRunParams:
    return VerifiedEdgeRunParams(
        row_capacity=capacity,
        merge_fanout=2,
        limits=RunLimits(leaf_max_records=capacity, leaf_max_bytes=4096, fanout=2),
    )


def test_many_keys_spill_and_iterator_does_not_call_materializing_plan(
    revision_harness: revision_support.RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ingest_rest(revision_harness, 128, request_id="bounded-many-keys")
    reconciler = ChannelReconciler(revision_harness.adapter, revision_harness.storage)

    def forbidden_plan(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("bounded edge iterator fell back to the materializing _plan")

    monkeypatch.setattr(reconciler, "_plan", forbidden_plan)
    with reconciler.iter_verified_edges(
        "agg_trades",
        revision_support.SYMBOL,
        revision_support.DAY,
        params=_params(3),
    ) as stream:
        assert tuple(stream) == ()


def test_late_verification_failure_exposes_no_staged_edge_prefix(
    revision_harness: revision_support.RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = revision_support.agg_items(8)
    revision_harness.ingest_archive(
        "agg_trades",
        revision_support.archive_agg_lines(items),
        clock=revision_support.StepClock(datetime(2025, 1, 2, tzinfo=UTC)),
        retrieved_at=datetime(2023, 11, 16, tzinfo=UTC),
    )
    _ingest_rest(revision_harness, len(items), request_id="bounded-failure")
    reconciler = ChannelReconciler(revision_harness.adapter, revision_harness.storage)
    reconciler.reconcile(
        "agg_trades",
        revision_support.SYMBOL,
        revision_support.DAY,
    )
    original = reconciler._verifier.verify_rest_elements
    calls = 0

    def fail_after_two(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise CatalogIntegrityError("injected persisted row verification failure")
        original(*args, **kwargs)

    monkeypatch.setattr(reconciler._verifier, "verify_rest_elements", fail_after_two)
    with pytest.raises(CatalogIntegrityError, match="injected persisted row"):
        with reconciler.iter_verified_edges(
            "agg_trades",
            revision_support.SYMBOL,
            revision_support.DAY,
            params=_params(2),
        ) as stream:
            next(iter(stream), None)
            pytest.fail("the iterator must not expose rows before validation completes")


def test_single_key_edge_fan_in_is_merged_one_edge_at_a_time(
    revision_harness: revision_support.RestHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_item = revision_support.agg_items(1)[0]
    versions = []
    for index in range(4):
        item = dict(original_item)
        item["p"] = f"{Decimal('92792.05') + Decimal(index) / Decimal(100):.8f}"
        versions.append(item)

    for index, item in enumerate(versions):
        revision_harness.ingest_archive(
            "agg_trades",
            revision_support.archive_agg_lines([item]),
            clock=revision_support.StepClock(datetime(2025, 1, 2 + index, tzinfo=UTC)),
            retrieved_at=datetime(2023, 11, 16 + index, tzinfo=UTC),
            request_id=f"bounded-archive-{index}",
        )
        rest_support.queue_agg_chain(
            revision_harness.venue,
            revision_support.SYMBOL,
            revision_support.T0,
            [[item]],
        )
        request = revision_support.agg_request(
            f"bounded-rest-{index}", start_ms=revision_support.T0
        )
        collected = revision_harness.collect(request)
        assert not isinstance(collected, Exception)
        revision_harness.store(
            clock=revision_support.StepClock(datetime(2025, 2, 1 + index, tzinfo=UTC))
        ).ingest_collection(request)

    archive_rows = revision_harness.rows(BINANCE_SPOT_AGG_TRADES)
    rest_rows = revision_harness.rows(BINANCE_SPOT_REST_AGG_TRADES)
    assert len(archive_rows) == len(rest_rows) == 4
    reconciler = ChannelReconciler(revision_harness.adapter, revision_harness.storage)
    reconciler.reconcile("agg_trades", revision_support.SYMBOL, revision_support.DAY)
    expected = reconciler.verified_edges(
        "agg_trades", revision_support.SYMBOL, revision_support.DAY
    )
    assert len(expected) == 4

    observed: list[tuple[int, int]] = []
    original_existing = reconciler._existing_edges

    def observe_edge_batch(*args: Any, **kwargs: Any) -> Any:
        observed.append((len(args[0]), len(args[1])))
        return original_existing(*args, **kwargs)

    monkeypatch.setattr(reconciler, "_existing_edges", observe_edge_batch)
    with reconciler.iter_verified_edges(
        "agg_trades",
        revision_support.SYMBOL,
        revision_support.DAY,
        params=_params(2),
    ) as stream:
        actual = tuple(stream)

    expected_sorted = tuple(sorted(expected, key=lambda edge: edge.edge_id))
    assert [edge.edge_id for edge in actual] == [edge.edge_id for edge in expected_sorted]
    assert [edge.row() for edge in actual] == [edge.row() for edge in expected_sorted]
    assert observed == [(1, 1)] * len(expected)

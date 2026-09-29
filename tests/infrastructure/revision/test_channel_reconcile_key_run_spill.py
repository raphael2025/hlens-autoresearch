"""Single-key history checks for per-key D3E staging."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from infrastructure.revision.channel_reconcile import ChannelReconciler, VerifiedEdgeRunParams
from infrastructure.streaming.runs import RunLimits
from tests.infrastructure.collector import rest_support
from tests.infrastructure.revision import rest_store_support as revision_support


def test_single_key_rest_history_spills_without_materializing_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with revision_support.sqlite_harness(tmp_path) as harness:
        original = revision_support.agg_items(1)[0]
        for index in range(16):
            item = dict(original)
            item["p"] = f"{Decimal('92792.05') + Decimal(index) / Decimal(100):.8f}"
            rest_support.queue_agg_chain(
                harness.venue,
                revision_support.SYMBOL,
                revision_support.T0,
                [[item]],
            )
            request = revision_support.agg_request(
                f"single-key-spill-{index}", start_ms=revision_support.T0
            )
            collected = harness.collect(request)
            assert not isinstance(collected, Exception)
            harness.store(
                clock=revision_support.StepClock(datetime(2025, 2, 1 + index, tzinfo=UTC))
            ).ingest_collection(request)

        reconciler = ChannelReconciler(harness.adapter, harness.storage)

        def forbidden_plan(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("bounded edge iterator fell back to the materializing _plan")

        monkeypatch.setattr(reconciler, "_plan", forbidden_plan)
        params = VerifiedEdgeRunParams(
            row_capacity=2,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2),
        )
        with reconciler.iter_verified_edges(
            "agg_trades", revision_support.SYMBOL, revision_support.DAY, params=params
        ) as stream:
            assert tuple(stream) == ()

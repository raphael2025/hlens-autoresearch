"""Single-key history checks for per-key D3E staging."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.revision import channel_reconcile as channel_reconcile_module
from infrastructure.revision.channel_reconcile import (
    ChannelReconciler,
    VerifiedEdgeRunParams,
    _assert_acyclic_run,
)
from infrastructure.streaming.runs import RunLimits, RunSetBuilder
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


def test_archive_rest_single_key_multi_history_edge_order_matches_materializer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with revision_support.sqlite_harness(tmp_path) as harness:
        original = revision_support.agg_items(1)[0]
        versions = []
        for index in range(3):
            item = dict(original)
            item["p"] = f"{Decimal('92792.05') + Decimal(index) / Decimal(100):.8f}"
            versions.append(item)

        for index, item in enumerate(versions):
            harness.ingest_archive(
                "agg_trades",
                revision_support.archive_agg_lines([item]),
                clock=revision_support.StepClock(datetime(2025, 1, 2 + index, tzinfo=UTC)),
                retrieved_at=datetime(2023, 11, 16 + index, tzinfo=UTC),
                request_id=f"single-key-archive-{index}",
            )
            rest_support.queue_agg_chain(
                harness.venue,
                revision_support.SYMBOL,
                revision_support.T0,
                [[item]],
            )
            request = revision_support.agg_request(
                f"single-key-rest-{index}", start_ms=revision_support.T0
            )
            collected = harness.collect(request)
            assert not isinstance(collected, Exception)
            harness.store(
                clock=revision_support.StepClock(datetime(2025, 2, 1 + index, tzinfo=UTC))
            ).ingest_collection(request)

        reconciler = ChannelReconciler(harness.adapter, harness.storage)
        reconciler.reconcile("agg_trades", revision_support.SYMBOL, revision_support.DAY)
        expected_ids = tuple(
            edge.edge_id
            for edge in sorted(
                reconciler.verified_edges(
                    "agg_trades", revision_support.SYMBOL, revision_support.DAY
                ),
                key=lambda edge: edge.edge_id,
            )
        )

        def forbidden_plan(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("bounded edge iterator fell back to the materializing _plan")

        monkeypatch.setattr(reconciler, "_plan", forbidden_plan)
        maximum_pending = 0
        finalized_roots: list[tuple[int, int]] = []
        base_builder = channel_reconcile_module.RunSetBuilder

        class ObservedRunSetBuilder(base_builder):  # type: ignore[misc, valid-type]
            def add(self, row: Any) -> None:
                nonlocal maximum_pending
                super().add(row)
                maximum_pending = max(maximum_pending, len(self._rows))

            def finish(self) -> Any:
                root = super().finish()
                if root is not None:
                    finalized_roots.append((root.record_count, root.leaf_count))
                return root

        monkeypatch.setattr(channel_reconcile_module, "RunSetBuilder", ObservedRunSetBuilder)
        params = VerifiedEdgeRunParams(
            row_capacity=2,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2),
        )
        with reconciler.iter_verified_edges(
            "agg_trades", revision_support.SYMBOL, revision_support.DAY, params=params
        ) as stream:
            actual_ids = tuple(edge.edge_id for edge in stream)
        assert actual_ids == expected_ids
        assert maximum_pending <= params.row_capacity
        assert any(records >= 3 and leaves >= 2 for records, leaves in finalized_roots)


def test_external_kahn_accepts_dangling_chain_and_rejects_cycle(tmp_path: Path) -> None:
    with revision_support.sqlite_harness(tmp_path) as harness:
        params = VerifiedEdgeRunParams(
            row_capacity=2,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=2, leaf_max_bytes=4096, fanout=2),
        )
        with RunSetBuilder(
            harness.storage,
            key=lambda row: (row["newer"], row["older"]),
            capacity=2,
            merge_fanout=2,
            limits=params.limits,
        ) as edges:
            edges.add({"newer": "r3", "older": "r2"})
            edges.add({"newer": "r2", "older": "r1"})
            edges.add({"newer": "r1", "older": "dangling"})
            acyclic_root = edges.finish()
        _assert_acyclic_run(harness.storage, acyclic_root, params)

        with RunSetBuilder(
            harness.storage,
            key=lambda row: (row["newer"], row["older"]),
            capacity=2,
            merge_fanout=2,
            limits=params.limits,
        ) as edges:
            edges.add({"newer": "a", "older": "b"})
            edges.add({"newer": "b", "older": "a"})
            cyclic_root = edges.finish()
        with pytest.raises(
            CatalogIntegrityError,
            match=r"supersedes 图不得成环：涉及 \['a', 'b'\]",
        ):
            _assert_acyclic_run(harness.storage, cyclic_root, params)

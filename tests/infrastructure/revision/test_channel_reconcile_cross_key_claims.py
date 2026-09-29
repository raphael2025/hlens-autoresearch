"""External cross-key revision identity claim checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.catalog.iceberg_adapter import CatalogIntegrityError
from infrastructure.revision.channel_reconcile import (
    VerifiedEdgeRunParams,
    _assert_single_key_claims,
)
from infrastructure.streaming.runs import RunLimits, RunSetBuilder
from tests.infrastructure.revision import rest_store_support as revision_support


def test_cross_key_revision_claim_is_rejected_after_sorted_spill(tmp_path: Path) -> None:
    with revision_support.sqlite_harness(tmp_path) as harness:
        params = VerifiedEdgeRunParams(
            row_capacity=1,
            merge_fanout=2,
            limits=RunLimits(leaf_max_records=1, leaf_max_bytes=4096, fanout=2),
        )
        with RunSetBuilder(
            harness.storage,
            key=lambda row: (row["revision_id"], row["observation_key"]),
            capacity=params.row_capacity,
            merge_fanout=params.merge_fanout,
            limits=params.limits,
        ) as claims:
            claims.add({"revision_id": "shared", "observation_key": "key-a"})
            claims.add({"revision_id": "shared", "observation_key": "key-b"})
            claims.add({"revision_id": "shared", "observation_key": "key-c"})
            claims.add({"revision_id": "same-key", "observation_key": "key-a"})
            claims.add({"revision_id": "same-key", "observation_key": "key-a"})
            root = claims.finish()

        with pytest.raises(
            CatalogIntegrityError,
            match=(
                r"revision_id 'shared' 跨 observation_key 归属冲突：同时被 "
                r"\['key-a', 'key-b'\] 认领"
            ),
        ):
            _assert_single_key_claims(harness.storage, root)
